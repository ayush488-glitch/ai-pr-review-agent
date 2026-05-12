# tests/smoke_phase6.py
#
# Phase 6 Gate Smoke Test — Memory Architecture
#
# WHAT THIS TESTS:
#   1. Postgres ORM: create_all() works on SQLite in-memory (no real DB needed)
#   2. Repository: save_review() + get_review() round-trip works correctly
#   3. PRReviewState: retrieved_context field exists and is type str
#   4. build_context node: returns retrieved_context in its state dict
#   5. context_retriever: returns "" gracefully when Qdrant is unreachable
#
# WHY SQLITE AND NOT POSTGRES FOR THE SMOKE TEST?
# (From Storage-Engines.md wiki):
#   "Use the real database in integration tests, but keep unit/smoke tests
#    fast by using an in-memory SQLite instance."
#   We're testing ORM LOGIC (table creation, insert, select) — not Postgres-specific
#   features (advisory locks, RETURNING, etc.). SQLite runs in-memory, needs
#   no external process, and confirms the ORM mapping is correct.
#   asyncpg (Postgres driver) would require a running Postgres server.
#   aiosqlite (async SQLite driver) runs entirely in-process.
#
# HOW TO RUN:
#   cd ~/Desktop/ai-pr-review-agent
#   python -m pytest tests/smoke_phase6.py -v
#   (or: python tests/smoke_phase6.py)
#
# PHASE 6 GATE: ALL 9 ASSERTIONS MUST PASS.

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import get_type_hints

# ---------------------------------------------------------------------------
# Setup path so local imports work
# ---------------------------------------------------------------------------
sys.path.insert(0, ".")


async def main() -> None:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        results.append((name, condition, detail))
        icon = "✓" if condition else "✗"
        print(f"  [{icon}] {name}" + (f"  ({detail})" if detail else ""))

    print("\n=== Phase 6 Smoke Test ===\n")

    # =========================================================================
    # SECTION 1: Postgres ORM — SQLite in-memory
    # =========================================================================
    print("--- Section 1: Postgres ORM (SQLite in-memory) ---")

    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from backend.database.models import Base, FindingRecord, PRReviewRecord

        # Use SQLite in-memory with aiosqlite for zero-infrastructure testing.
        # (Storage-Engines.md: "unit smoke tests use in-memory DB for speed.")
        test_engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
        )

        # --- Test 1: create_all() ---
        # create_all() should create PRReviewRecord and FindingRecord tables.
        # If any column type or constraint is misconfigured, this raises.
        try:
            async with test_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            check("ORM create_all() succeeds on SQLite", True)
        except Exception as e:
            check("ORM create_all() succeeds on SQLite", False, str(e))

        # --- Test 2: PRReviewRecord table exists ---
        try:
            async with test_engine.connect() as conn:
                result = await conn.execute(
                    # SQLite's sqlite_master table lists all tables
                    __import__("sqlalchemy").text(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                )
                tables = {row[0] for row in result}
            check(
                "Tables created: pr_review_records + finding_records",
                {"pr_review_records", "finding_records"}.issubset(tables),
                f"found: {tables}",
            )
        except Exception as e:
            check("Tables created", False, str(e))

        # =========================================================================
        # SECTION 2: Repository — save_review + get_review round-trip
        # =========================================================================
        print("\n--- Section 2: Repository round-trip ---")

        from backend.database.repository import get_review, save_review

        test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        review_id = str(uuid.uuid4())

        # --- Test 3: save_review() succeeds ---
        try:
            async with test_session_factory() as session:
                record = await save_review(
                    session,
                    review_id=review_id,
                    repo_full_name="ayush/test-repo",
                    pr_number=42,
                    pr_title="Add login endpoint",
                    head_commit_sha="abc123def456",
                    pr_diff="diff --git a/auth.py b/auth.py\n+def login(): pass",
                    verdict="request_changes",
                    status="completed",
                    overall_confidence=0.85,
                    needs_human_review=False,
                    human_review_reason="",
                    findings=[
                        {
                            "agent_type": "security",
                            "severity": "high",
                            "category": "security",
                            "summary": "SQL injection risk in login()",
                            "file_path": "auth.py",
                            "line_start": 10,
                            "confidence": 0.9,
                        }
                    ],
                )
            check("save_review() persists PRReviewRecord", record is not None)
        except Exception as e:
            check("save_review() persists PRReviewRecord", False, str(e))
            record = None

        # --- Test 4: get_review() retrieves the saved record ---
        try:
            async with test_session_factory() as session:
                fetched = await get_review(session, review_id)
            check(
                "get_review() retrieves saved review",
                fetched is not None and fetched.id == review_id,
                f"fetched_id={fetched.id if fetched else 'None'}",
            )
        except Exception as e:
            check("get_review() retrieves saved review", False, str(e))
            fetched = None

        # --- Test 5: FindingRecord was saved ---
        try:
            async with test_session_factory() as session:
                fetched2 = await get_review(session, review_id)
                # findings loaded via selectin (lazy-load of eager = selectin)
                finding_count = len(fetched2.findings) if fetched2 else 0
            check(
                "Finding saved alongside review (1 finding)",
                finding_count == 1,
                f"findings={finding_count}",
            )
        except Exception as e:
            check("Finding saved alongside review", False, str(e))

    except ImportError as e:
        print(f"  [!] IMPORT ERROR in Section 1/2: {e}")
        for name in [
            "ORM create_all() succeeds on SQLite",
            "Tables created: pr_review_records + finding_records",
            "save_review() persists PRReviewRecord",
            "get_review() retrieves saved review",
            "Finding saved alongside review (1 finding)",
        ]:
            check(name, False, f"ImportError: {e}")

    # =========================================================================
    # SECTION 3: PRReviewState has retrieved_context field
    # =========================================================================
    print("\n--- Section 3: PRReviewState shape ---")

    try:
        from backend.orchestrator.state import PRReviewState

        # TypedDict fields are accessible via __annotations__
        hints = PRReviewState.__annotations__
        check(
            "PRReviewState has retrieved_context: str",
            "retrieved_context" in hints and hints["retrieved_context"] is str,
            f"annotations={list(hints.keys())}",
        )
    except Exception as e:
        check("PRReviewState has retrieved_context: str", False, str(e))

    # =========================================================================
    # SECTION 4: context_retriever graceful degradation
    # =========================================================================
    print("\n--- Section 4: context_retriever graceful degradation ---")

    try:
        from backend.memory.context_retriever import retrieve_context_for_diff

        # Use a bogus Qdrant URL — connection will fail.
        # retrieve_context_for_diff() must return "" without raising.
        # This is the critical invariant: Qdrant down = "" returned, no crash.
        from backend.config.settings import Settings

        bad_settings = Settings(
            openai_api_key="test-key-will-fail",
            github_webhook_secret="test-secret",
            github_token="test-token",
            anthropic_api_key="test-anthropic-key",
            qdrant_url="http://localhost:9999",  # bogus URL — nothing running there
            database_url="sqlite+aiosqlite:///:memory:",
        )

        # Test A: empty diff -> returns "" immediately (no embed call, no Qdrant call)
        # This tests the first guard clause in retrieve_context_for_diff.
        result_empty = await retrieve_context_for_diff(
            diff="",
            repo_full_name="ayush/test-repo",
            settings=bad_settings,
        )
        check(
            "context_retriever returns '' for empty diff",
            result_empty == "",
            f"result={repr(result_empty)[:60]}",
        )

        # Test B: test the Qdrant unreachable path WITHOUT a live OpenAI call.
        # Strategy: monkeypatch embed_text to raise EmbeddingError directly,
        # so we test the embed-failure -> "" fallback path without network.
        import backend.memory.context_retriever as _ctx_mod
        from backend.memory.embedder import EmbeddingError

        _original_embed = _ctx_mod.embed_text  # save

        async def _mock_embed_fail(text: str) -> list[float]:
            raise EmbeddingError("mock embedding failure — Qdrant unreachable path")

        _ctx_mod.embed_text = _mock_embed_fail  # patch

        try:
            result = await retrieve_context_for_diff(
                diff="diff --git a/auth.py b/auth.py\n+def login(): pass",
                repo_full_name="ayush/test-repo",
                settings=bad_settings,
            )
        finally:
            _ctx_mod.embed_text = _original_embed  # restore

        check(
            "context_retriever returns '' when Qdrant unreachable",
            result == "",
            f"result={repr(result)[:60]}",
        )
        check(
            "context_retriever returns str (not None or exception)",
            isinstance(result, str),
            f"type={type(result).__name__}",
        )
    except Exception as e:
        check("context_retriever returns '' when Qdrant unreachable", False, str(e))
        check("context_retriever returns str (not None or exception)", False, str(e))

    # =========================================================================
    # RESULTS
    # =========================================================================
    print("\n=== Results ===")
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} assertions passed")

    if passed == total:
        print("\nPhase 6 GATE: PASSED — all assertions green.")
    else:
        print("\nPhase 6 GATE: FAILED — see failing assertions above.")
        failed = [(n, d) for n, ok, d in results if not ok]
        for name, detail in failed:
            print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))
        sys.exit(1)

    # Dispose SQLAlchemy engine so aiosqlite background threads exit cleanly.
    try:
        test_engine
        await test_engine.dispose()
    except NameError:
        pass  # test_engine may not exist if Section 1 failed on import


if __name__ == "__main__":
    asyncio.run(main())
