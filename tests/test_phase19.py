# tests/smoke_phase19.py
#
# Phase 19 Gate Smoke Test — Human-in-the-Loop (HITL)
#
# DESIGN PHILOSOPHY:
#   No live network. No real Postgres. No Redis. No GitHub token.
#   All DB calls use SQLite in-memory (aiosqlite).
#   Redis is mocked with AsyncMock.
#   GitHub client is mocked (duck-typed minimal stub).
#
# WIKI: release-it / Stability-Patterns
#   "Fast tests encourage running tests often."
#   -> This suite completes in < 5 seconds with zero I/O.
#
# WIKI: DDIA / Reliability-Scalability
#   "In fault-tolerant systems, it can make sense to trigger faults deliberately."
#   -> We inject Redis failure in test 4 to verify graceful degradation.
#
# ASSERTIONS (7 total):
#   1. Escalation rule 2: 3+ CRITICAL agent verdicts -> should_escalate=True
#   2. Escalation rule 3: confidence < 0.40         -> should_escalate=True
#   3. Escalation rule 0: healthy review             -> should_escalate=False
#   4. enqueue_hitl_review: Postgres row written, Redis push called, returns UUID
#   5. enqueue_hitl_review: Redis failure -> row still written, no exception raised
#   6. resolve_dispute: approve -> HITLReview status=approved, DisputeResult correct
#   7. resolve_dispute: second call on same id -> DisputeAlreadyResolved raised

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

# ─── path setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ──────────────────────────────────────────────────────────────────────────────

# SQLAlchemy async + aiosqlite for in-memory Postgres simulation
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import event

# ─── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("smoke_phase19")

# ─── result tracking ──────────────────────────────────────────────────────────
PASS = []
FAIL = []

def ok(name: str):
    PASS.append(name)
    print(f"  PASS  {name}")

def fail(name: str, reason: str):
    FAIL.append(name)
    print(f"  FAIL  {name}")
    print(f"        reason: {reason}")


# ─── SQLite in-memory engine ──────────────────────────────────────────────────
# WHY: We don't want to hit real Postgres in CI or demo.
# aiosqlite lets us run async SQLAlchemy against SQLite.
# We create all tables fresh before each test that needs DB.

from backend.database.models import Base  # all ORM models

async def make_test_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    # SQLite doesn't support FOR UPDATE — we patch it in the test that needs it.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def make_test_session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return factory()


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_agent_results(critical_count: int = 0, verdict: str = "REQUEST_CHANGES") -> list[dict]:
    """Build a minimal agent_results list with the requested number of CRITICAL items."""
    results = []
    severities = ["CRITICAL"] * critical_count
    # fill the rest with HIGH so we always have 4 agents
    while len(severities) < 4:
        severities.append("HIGH")
    for i, sev in enumerate(severities):
        results.append({
            "agent": f"agent_{i}",
            "verdict": verdict,
            "confidence": 0.85,
            "findings": [{"severity": sev, "summary": f"finding_{i}"}],
        })
    return results


# =============================================================================
# TEST 1 — Escalation rule 2: 3+ CRITICAL agents -> should_escalate=True
# =============================================================================
def test_escalation_critical_threshold():
    name = "escalation | 3+ CRITICAL agents -> should_escalate=True"
    try:
        from backend.hitl.escalation import should_escalate

        result = should_escalate(
            security_agent_failed=False,
            critical_agent_count=3,
            overall_confidence=0.75,
            successful_agent_count=4,
            total_agent_count=4,
        )

        assert result.should_escalate is True, f"expected True, got {result.should_escalate}"
        assert "rule2" in result.rule_triggered, f"expected rule2, got {result.rule_triggered}"
        ok(name)
    except Exception as e:
        fail(name, str(e))


# =============================================================================
# TEST 2 — Escalation rule 3: low confidence -> should_escalate=True
# =============================================================================
def test_escalation_low_confidence():
    name = "escalation | confidence < 0.40 -> should_escalate=True"
    try:
        from backend.hitl.escalation import should_escalate

        result = should_escalate(
            security_agent_failed=False,
            critical_agent_count=0,
            overall_confidence=0.30,   # below 0.40 threshold
            successful_agent_count=4,
            total_agent_count=4,
        )

        assert result.should_escalate is True, f"expected True, got {result.should_escalate}"
        assert "rule3" in result.rule_triggered, f"expected rule3, got {result.rule_triggered}"
        ok(name)
    except Exception as e:
        fail(name, str(e))


# =============================================================================
# TEST 3 — Escalation: healthy review -> should_escalate=False
# =============================================================================
def test_escalation_no_trigger():
    name = "escalation | healthy review -> should_escalate=False"
    try:
        from backend.hitl.escalation import should_escalate

        result = should_escalate(
            security_agent_failed=False,
            critical_agent_count=1,   # only 1 CRITICAL
            overall_confidence=0.82,
            successful_agent_count=4,
            total_agent_count=4,
        )

        assert result.should_escalate is False, f"expected False, got {result.should_escalate}"
        assert result.rule_triggered == "none", f"expected 'none', got {result.rule_triggered}"
        ok(name)
    except Exception as e:
        fail(name, str(e))


# =============================================================================
# TEST 4 — enqueue_hitl_review: Postgres row written, Redis push called
# =============================================================================
async def test_enqueue_writes_postgres_and_redis():
    name = "enqueue_hitl_review | writes Postgres row, pushes to Redis"
    try:
        engine = await make_test_engine()

        # Mock Redis client — we just need lpush to be called
        mock_redis = AsyncMock()
        mock_redis.lpush = AsyncMock(return_value=1)

        # Mock get_session_factory to return our in-memory SQLite factory
        # WHY: enqueue_hitl_review calls get_session_factory() internally
        # (not get_db()) because it runs in worker context, not request context.
        factory = async_sessionmaker(engine, expire_on_commit=False)

        with patch("backend.hitl.queue.get_session_factory", return_value=factory), \
             patch("backend.hitl.queue._notify_slack", new_callable=AsyncMock):

            from backend.hitl.queue import enqueue_hitl_review

            hitl_id = await enqueue_hitl_review(
                redis_client=mock_redis,
                review_id=str(uuid.uuid4()),
                repo_full_name="ayush/test-repo",
                pr_number=2,
                agent_verdict="REQUEST_CHANGES",
                escalation_reason="3 CRITICAL agents",
                findings_snapshot=[{"severity": "CRITICAL", "summary": "SQL injection"}],
                overall_confidence=0.72,
            )

        # Assert UUID returned
        assert hitl_id, "expected non-empty hitl_id"
        uuid.UUID(hitl_id)  # validates UUID format

        # Assert Redis lpush was called with the returned id
        mock_redis.lpush.assert_called_once()
        call_args = mock_redis.lpush.call_args[0]
        assert call_args[1] == hitl_id, f"Redis push arg mismatch: {call_args}"

        # Assert Postgres row exists
        async with factory() as session:
            from sqlalchemy import select
            from backend.database.models import HITLReview
            row = await session.get(HITLReview, hitl_id)
            assert row is not None, "HITLReview row not found in DB"
            assert row.status == "pending"
            assert row.repo_full_name == "ayush/test-repo"

        ok(name)
    except Exception as e:
        import traceback; traceback.print_exc()
        fail(name, str(e))


# =============================================================================
# TEST 5 — enqueue_hitl_review: Redis failure -> row still written, no raise
# =============================================================================
async def test_enqueue_redis_failure_graceful():
    name = "enqueue_hitl_review | Redis failure -> Postgres row still written, no crash"
    try:
        engine = await make_test_engine()

        # Redis that raises on lpush — simulates Upstash outage
        mock_redis = AsyncMock()
        mock_redis.lpush = AsyncMock(side_effect=ConnectionError("Upstash unavailable"))

        factory = async_sessionmaker(engine, expire_on_commit=False)

        with patch("backend.hitl.queue.get_session_factory", return_value=factory), \
             patch("backend.hitl.queue._notify_slack", new_callable=AsyncMock):

            from backend.hitl.queue import enqueue_hitl_review

            # Must NOT raise even though Redis is dead
            hitl_id = await enqueue_hitl_review(
                redis_client=mock_redis,
                review_id=str(uuid.uuid4()),
                repo_full_name="ayush/test-repo",
                pr_number=3,
                agent_verdict="BLOCK",
                escalation_reason="security agent failed",
                findings_snapshot=[],
                overall_confidence=0.51,
            )

        assert hitl_id, "expected non-empty hitl_id even when Redis fails"

        # Postgres row must still exist
        async with factory() as session:
            from backend.database.models import HITLReview
            row = await session.get(HITLReview, hitl_id)
            assert row is not None, "Postgres row missing after Redis failure"

        ok(name)
    except Exception as e:
        import traceback; traceback.print_exc()
        fail(name, str(e))


# =============================================================================
# TEST 6 — resolve_dispute: approve -> status=approved, DisputeResult correct
# =============================================================================
async def test_resolve_dispute_approve():
    name = "resolve_dispute | approve -> HITLReview.status=approved, result correct"
    try:
        engine = await make_test_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        # Pre-insert a HITLReview row in pending status
        hitl_id = str(uuid.uuid4())
        review_id = str(uuid.uuid4())
        from backend.database.models import HITLReview, HITLFeedback

        async with factory() as session:
            async with session.begin():
                row = HITLReview(
                    id=hitl_id,
                    review_id=review_id,
                    repo_full_name="ayush/test-repo",
                    pr_number=2,
                    agent_verdict="REQUEST_CHANGES",
                    escalation_reason="low confidence",
                    findings_snapshot=json.dumps([]),
                    overall_confidence=0.35,
                    status="pending",
                )
                session.add(row)

        # Mock github_client (duck-typed stub)
        mock_github = AsyncMock()
        mock_github.post_review = AsyncMock(return_value={"id": 9999})

        # Mock record_feedback so we don't need its session_factory
        with patch("backend.hitl.feedback.get_session_factory", return_value=factory), \
             patch("backend.hitl.dispute.record_feedback", new_callable=AsyncMock) as mock_feedback:

            mock_feedback.return_value = str(uuid.uuid4())

            from backend.hitl.dispute import resolve_dispute, DisputeRequest

            # SQLite doesn't support SELECT FOR UPDATE — we patch with_for_update()
            # to be a no-op so the test can run without a real Postgres.
            # WHY: with_for_update() is a Postgres-specific lock. The logic is still
            # tested — only the locking mechanism is skipped in SQLite.
            from sqlalchemy import select
            original_select = select

            # Pass a bare session (no active transaction) — resolve_dispute
            # calls session.begin() internally. Starting begin() inside begin() raises.
            async with factory() as session:
                req = DisputeRequest(
                    hitl_review_id=hitl_id,
                    human_verdict="approve",
                    reason="looks good to me",
                    reviewer_id="ayush",
                )

                # Patch with_for_update to no-op for SQLite compatibility
                from unittest.mock import patch as _patch
                import sqlalchemy

                def mock_select(*args, **kwargs):
                    stmt = sqlalchemy.select(*args, **kwargs)
                    stmt.with_for_update = lambda **kw: stmt  # no-op FOR UPDATE
                    return stmt

                with _patch("backend.hitl.dispute.select", side_effect=mock_select):
                    result = await resolve_dispute(
                        session=session,
                        github_client=mock_github,
                        request=req,
                    )

        assert result.new_status == "approved", f"expected 'approved', got {result.new_status}"
        assert result.human_verdict == "approve"
        assert result.hitl_review_id == hitl_id

        ok(name)
    except Exception as e:
        import traceback; traceback.print_exc()
        fail(name, str(e))


# =============================================================================
# TEST 7 — resolve_dispute: second call -> DisputeAlreadyResolved raised
# =============================================================================
async def test_resolve_dispute_double_claim():
    name = "resolve_dispute | second call on resolved id -> DisputeAlreadyResolved"
    try:
        from backend.hitl.dispute import DisputeAlreadyResolved

        # Pre-insert a HITLReview that is ALREADY approved
        engine = await make_test_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)

        hitl_id = str(uuid.uuid4())
        review_id = str(uuid.uuid4())
        from backend.database.models import HITLReview

        async with factory() as session:
            async with session.begin():
                row = HITLReview(
                    id=hitl_id,
                    review_id=review_id,
                    repo_full_name="ayush/test-repo",
                    pr_number=4,
                    agent_verdict="BLOCK",
                    escalation_reason="3 critical",
                    findings_snapshot=json.dumps([]),
                    overall_confidence=0.70,
                    status="approved",   # already resolved
                )
                session.add(row)

        mock_github = AsyncMock()

        with patch("backend.hitl.feedback.get_session_factory", return_value=factory), \
             patch("backend.hitl.dispute.record_feedback", new_callable=AsyncMock):

            from backend.hitl.dispute import resolve_dispute, DisputeRequest
            import sqlalchemy

            def mock_select(*args, **kwargs):
                stmt = sqlalchemy.select(*args, **kwargs)
                stmt.with_for_update = lambda **kw: stmt
                return stmt

            raised = False
            # Pass bare session — resolve_dispute owns the transaction
            async with factory() as session:
                req = DisputeRequest(
                    hitl_review_id=hitl_id,
                    human_verdict="approve",
                    reason="trying again",
                    reviewer_id="bob",
                )
                try:
                    with patch("backend.hitl.dispute.select", side_effect=mock_select):
                        await resolve_dispute(
                            session=session,
                            github_client=mock_github,
                            request=req,
                        )
                except DisputeAlreadyResolved as e:
                    raised = True
                    assert e.current_status == "approved", f"wrong status: {e.current_status}"

        assert raised, "Expected DisputeAlreadyResolved but no exception was raised"
        ok(name)
    except Exception as e:
        import traceback; traceback.print_exc()
        fail(name, str(e))


# =============================================================================
# Runner
# =============================================================================

async def run_async_tests():
    await test_enqueue_writes_postgres_and_redis()
    await test_enqueue_redis_failure_graceful()
    await test_resolve_dispute_approve()
    await test_resolve_dispute_double_claim()


def main():
    start = time.perf_counter()
    print()
    print("=" * 60)
    print("  smoke_phase19.py — Phase 19 HITL Gate Tests")
    print("=" * 60)
    print()

    # Sync tests
    test_escalation_critical_threshold()
    test_escalation_low_confidence()
    test_escalation_no_trigger()

    # Async tests
    asyncio.run(run_async_tests())

    elapsed = time.perf_counter() - start
    print()
    print(f"  Passed: {len(PASS)} / {len(PASS) + len(FAIL)}   ({elapsed:.2f}s)")
    if FAIL:
        print(f"  Failed: {FAIL}")
        print()
        sys.exit(1)
    else:
        print("  All tests passed.")
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
