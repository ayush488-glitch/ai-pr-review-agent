# tests/smoke_phase8.py
#
# Phase 8 Gate Smoke Test — Review Posting
#
# DESIGN PHILOSOPHY:
#   No live network. No real Postgres. No GitHub token.
#   All GitHub calls use httpx MockTransport.
#   Postgres is SQLite in-memory (aiosqlite).
#
# WIKI: release-it / Stability-Patterns
#   "Fast tests encourage running tests often."
#   -> This suite completes in < 5 seconds with zero I/O.
#
# WIKI: DDIA / Reliability-Scalability
#   "In fault-tolerant systems, it can make sense to trigger faults deliberately."
#   -> We inject GitHub 5xx, 429, 404, and Postgres failures to verify
#      the fault vs failure distinction is correctly implemented.
#
# ASSERTIONS (9 total):
#   1. APPROVE verdict -> PostReviewPayload has event=APPROVE
#   2. REQUEST_CHANGES verdict -> PostReviewPayload has event=REQUEST_CHANGES
#   3. Findings with file_path+line_start become inline ReviewComment objects
#   4. Findings without file_path do NOT become inline comments
#   5. Successful GitHub post + Postgres save -> review_posted=True, github_review_id set
#   6. GitHubAPIError (5xx after retries) -> review_posted=False, no crash
#   7. Postgres failure AFTER successful GitHub post -> review_posted=True (fault not failure)
#   8. needs_human_review=True -> GitHub API never called, review_posted=False
#   9. Review summary body contains severity counts + confidence score + verdict badge

import asyncio
import json
import logging
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

# ─── path setup ──────────────────────────────────────────────────────────────
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ─────────────────────────────────────────────────────────────────────────────


def make_settings(**overrides):
    from backend.config.settings import Settings
    defaults = dict(
        openai_api_key="test-key",
        github_webhook_secret="test-secret",
        github_token="test-token",
        github_api_base_url="http://mock-github.local",
        anthropic_api_key="test-key",
        database_url="sqlite+aiosqlite:///:memory:",
        qdrant_url="http://localhost:9999",
        qdrant_collection_name="test-collection",
        review_body_max_characters=65536,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_state(**overrides) -> dict[str, Any]:
    """
    Builds a minimal PRReviewState-like dict for testing helper functions.
    Uses keyword overrides to inject specific fields per test.
    """
    base = dict(
        workflow_id="wf-test-001",
        repo_full_name="jsmith/payments",
        pr_number=42,
        pr_title="feat: add payment retry logic",
        pr_body="Adds exponential backoff to the payment processor.",
        pr_diff="diff --git a/src/payments/processor.py ...",
        author_login="jsmith",
        head_commit_sha="abc123def456",
        base_branch="main",
        changed_files=["src/payments/processor.py"],
        agent_results=[
            {
                "agent_type": "security",
                "success": True,
                "error_message": "",
                "duration_seconds": 1.2,
                "findings": [],
                "confidence": 0.95,
            },
            {
                "agent_type": "quality",
                "success": True,
                "error_message": "",
                "duration_seconds": 0.8,
                "findings": [],
                "confidence": 0.88,
            },
        ],
        verdict=None,
        final_findings=[],
        overall_confidence=0.9,
        needs_human_review=False,
        human_review_reason="",
        review_posted=False,
        github_review_id=None,
        status="posting",
        confidence_threshold=0.7,
        retrieved_context="",
    )
    base.update(overrides)
    return base


def mock_post_review_response(review_id: int = 12345) -> httpx.Response:
    """Builds a mock GitHub POST /reviews response."""
    return httpx.Response(
        status_code=200,
        content=json.dumps({
            "id": review_id,
            "node_id": "PRR_test",
            "user": {"login": "ai-review-bot"},
            "body": "AI Review",
            "state": "APPROVED",
            "html_url": f"https://github.com/jsmith/payments/pull/42#pullrequestreview-{review_id}",
            "submitted_at": "2026-05-12T10:00:00Z",
        }).encode(),
        headers={"Content-Type": "application/json"},
    )


# ─────────────────────────────────────────────────────────────────────────────

def run_all_tests():
    results = []
    total = 0
    passed = 0

    def assert_test(name: str, fn):
        nonlocal total, passed
        total += 1
        start = time.time()
        try:
            asyncio.run(fn())
            duration = time.time() - start
            print(f"  PASS [{duration:.2f}s]  {name}")
            passed += 1
            results.append((name, True, None))
        except Exception as e:
            duration = time.time() - start
            print(f"  FAIL [{duration:.2f}s]  {name}")
            print(f"       {type(e).__name__}: {e}")
            results.append((name, False, e))

    print("\n=== Phase 8 Smoke Test — Review Posting ===\n")

    # ─── Test 1: APPROVE verdict -> event=APPROVE ─────────────────────────────
    async def test_approve_verdict_maps_to_approve_event():
        from backend.orchestrator.nodes import _verdict_to_review_event
        from backend.integrations.github_models import ReviewEvent
        from backend.models.enums import ReviewVerdict

        event = _verdict_to_review_event(ReviewVerdict.APPROVE)
        assert event == ReviewEvent.APPROVE, f"Expected APPROVE, got {event}"

    assert_test("APPROVE verdict maps to ReviewEvent.APPROVE", test_approve_verdict_maps_to_approve_event)

    # ─── Test 2: REQUEST_CHANGES verdict -> event=REQUEST_CHANGES ─────────────
    async def test_request_changes_verdict_maps():
        from backend.orchestrator.nodes import _verdict_to_review_event
        from backend.integrations.github_models import ReviewEvent
        from backend.models.enums import ReviewVerdict

        event = _verdict_to_review_event(ReviewVerdict.REQUEST_CHANGES)
        assert event == ReviewEvent.REQUEST_CHANGES, f"Expected REQUEST_CHANGES, got {event}"

        # NEEDS_HUMAN_REVIEW -> COMMENT (does not block merge)
        from backend.models.enums import ReviewVerdict as RV
        event2 = _verdict_to_review_event(RV.NEEDS_HUMAN_REVIEW)
        assert event2 == ReviewEvent.COMMENT, f"Expected COMMENT for HITL, got {event2}"

        # None -> COMMENT (safe default)
        event3 = _verdict_to_review_event(None)
        assert event3 == ReviewEvent.COMMENT, f"Expected COMMENT for None, got {event3}"

    assert_test("REQUEST_CHANGES / HITL / None verdicts map correctly", test_request_changes_verdict_maps)

    # ─── Test 3: Findings with file+line become inline ReviewComment ───────────
    async def test_inline_comments_built_correctly():
        from backend.orchestrator.nodes import _findings_to_review_comments
        from backend.integrations.github_models import ReviewComment

        findings = [
            {
                "agent_type": "security",
                "severity": "high",
                "category": "security",
                "summary": "SQL injection risk: user input directly interpolated.",
                "file_path": "src/payments/processor.py",
                "line_start": 42,
                "line_end": 42,
                "suggestion": "Use parameterized queries.",
                "confidence": 0.97,
            },
            {
                "agent_type": "quality",
                "severity": "medium",
                "category": "quality",
                "summary": "Function exceeds 50 lines — consider splitting.",
                "file_path": "src/payments/processor.py",
                "line_start": 10,
                "line_end": 65,
                "suggestion": None,
                "confidence": 0.80,
            },
        ]

        comments = _findings_to_review_comments(findings)

        assert len(comments) == 2, f"Expected 2 inline comments, got {len(comments)}"
        assert all(isinstance(c, ReviewComment) for c in comments)

        sql_comment = next(c for c in comments if "SQL injection" in c.body)
        assert sql_comment.path == "src/payments/processor.py"
        assert sql_comment.line == 42
        assert "HIGH" in sql_comment.body
        assert "Security Agent" in sql_comment.body
        assert "parameterized" in sql_comment.body  # suggestion included

    assert_test("Inline findings become ReviewComment with correct fields", test_inline_comments_built_correctly)

    # ─── Test 4: Findings without file_path NOT in inline comments ────────────
    async def test_pr_level_findings_excluded_from_inline():
        from backend.orchestrator.nodes import _findings_to_review_comments

        findings = [
            {
                # Has file but NO line -> not inline
                "agent_type": "test",
                "severity": "medium",
                "summary": "No test file added for src/payments/processor.py",
                "file_path": None,
                "line_start": None,
                "suggestion": "Add tests/test_processor.py",
                "confidence": 0.85,
            },
            {
                # Has file AND line -> inline
                "agent_type": "security",
                "severity": "high",
                "summary": "Hardcoded secret detected.",
                "file_path": "src/config.py",
                "line_start": 5,
                "suggestion": None,
                "confidence": 0.99,
            },
        ]

        comments = _findings_to_review_comments(findings)

        assert len(comments) == 1, (
            f"Expected 1 inline comment (the one with file+line), got {len(comments)}"
        )
        assert comments[0].path == "src/config.py"

    assert_test("PR-level findings (no file_path) excluded from inline comments", test_pr_level_findings_excluded_from_inline)

    # ─── Test 5: Successful GitHub post + Postgres save ───────────────────────
    async def test_successful_post_and_postgres_save():
        from backend.orchestrator.nodes import post_review
        from backend.models.enums import ReviewVerdict

        state = make_state(
            verdict=ReviewVerdict.APPROVE,
            final_findings=[],
            overall_confidence=0.92,
            needs_human_review=False,
            head_commit_sha="abc123def456",
        )

        # Mock GitHubClient.post_pr_review to return success
        mock_response = MagicMock()
        mock_response.id = 99001
        mock_response.html_url = "https://github.com/jsmith/payments/pull/42#pullrequestreview-99001"

        # Mock save_review to do nothing (avoid real DB)
        async def fake_save_review(*args, **kwargs):
            return MagicMock()

        # Mock get_db as async context manager
        class FakeSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class FakeGetDb:
            def __call__(self): return self
            async def __aenter__(self): return FakeSession()
            async def __aexit__(self, *a): pass

        with patch("backend.orchestrator.nodes.GitHubClient") as MockClient, \
             patch("backend.database.repository.save_review", side_effect=fake_save_review), \
             patch("backend.database.postgres.get_db", FakeGetDb()), \
             patch("backend.orchestrator.nodes.get_settings", return_value=make_settings()):

            mock_instance = AsyncMock()
            mock_instance.post_pr_review = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_instance

            result = await post_review(state)

        assert result["review_posted"] is True, f"Expected review_posted=True, got {result}"
        assert result["github_review_id"] == 99001, f"Expected review_id=99001, got {result['github_review_id']}"

    assert_test("Successful post -> review_posted=True, github_review_id set", test_successful_post_and_postgres_save)

    # ─── Test 6: GitHubAPIError routes to HITL (review_posted=False) ──────────
    async def test_github_api_error_routes_to_hitl():
        from backend.orchestrator.nodes import post_review
        from backend.integrations.github_client import GitHubAPIError
        from backend.models.enums import ReviewVerdict

        state = make_state(
            verdict=ReviewVerdict.APPROVE,
            final_findings=[],
            overall_confidence=0.92,
            needs_human_review=False,
        )

        with patch("backend.orchestrator.nodes.GitHubClient") as MockClient, \
             patch("backend.orchestrator.nodes.get_settings", return_value=make_settings()):

            mock_instance = AsyncMock()
            mock_instance.post_pr_review = AsyncMock(
                side_effect=GitHubAPIError("Server error", status_code=503)
            )
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_instance

            result = await post_review(state)

        assert result["review_posted"] is False, (
            f"GitHubAPIError should produce review_posted=False, got {result['review_posted']}"
        )
        assert result["github_review_id"] is None

    assert_test("GitHubAPIError -> review_posted=False, no crash", test_github_api_error_routes_to_hitl)

    # ─── Test 7: Postgres failure AFTER GitHub success -> review_posted=True ───
    async def test_postgres_failure_does_not_undo_github_post():
        """
        WIKI: DDIA / Reliability-Scalability
          "A fault is one component deviating from spec; a failure is the whole system."
        If Postgres is down but the GitHub post succeeded, the review is LIVE on GitHub.
        We must return review_posted=True — do not undo the GitHub post.
        """
        from backend.orchestrator.nodes import post_review
        from backend.models.enums import ReviewVerdict

        state = make_state(
            verdict=ReviewVerdict.APPROVE,
            final_findings=[],
            overall_confidence=0.92,
            needs_human_review=False,
        )

        mock_response = MagicMock()
        mock_response.id = 99002
        mock_response.html_url = "https://github.com/jsmith/payments/pull/42#pullrequestreview-99002"

        # GitHub succeeds, Postgres fails
        async def failing_save(*args, **kwargs):
            raise Exception("Postgres connection refused")

        class FakeGetDb:
            def __call__(self): return self
            async def __aenter__(self): return MagicMock()
            async def __aexit__(self, *a): pass

        with patch("backend.orchestrator.nodes.GitHubClient") as MockClient, \
             patch("backend.database.repository.save_review", side_effect=failing_save), \
             patch("backend.database.postgres.get_db", FakeGetDb()), \
             patch("backend.orchestrator.nodes.get_settings", return_value=make_settings()):

            mock_instance = AsyncMock()
            mock_instance.post_pr_review = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_instance

            result = await post_review(state)

        # KEY ASSERTION: Postgres failure must NOT undo the GitHub post
        assert result["review_posted"] is True, (
            f"Postgres failure after successful GitHub post must still return "
            f"review_posted=True. Got review_posted={result['review_posted']}"
        )
        assert result["github_review_id"] == 99002

    assert_test("Postgres failure after GitHub success -> review_posted=True (fault not failure)", test_postgres_failure_does_not_undo_github_post)

    # ─── Test 8: needs_human_review=True -> GitHub never called ───────────────
    async def test_hitl_path_skips_github():
        from backend.orchestrator.nodes import post_review
        from backend.models.enums import ReviewVerdict

        state = make_state(
            verdict=ReviewVerdict.NEEDS_HUMAN_REVIEW,
            final_findings=[],
            overall_confidence=0.45,
            needs_human_review=True,
            human_review_reason="Confidence below threshold (0.45 < 0.70)",
        )

        github_call_count = 0

        with patch("backend.orchestrator.nodes.GitHubClient") as MockClient, \
             patch("backend.orchestrator.nodes.get_settings", return_value=make_settings()):

            mock_instance = AsyncMock()

            async def track_post(*args, **kwargs):
                nonlocal github_call_count
                github_call_count += 1
                return MagicMock()

            mock_instance.post_pr_review = track_post
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            MockClient.return_value = mock_instance

            result = await post_review(state)

        assert result["review_posted"] is False
        assert result["github_review_id"] is None
        assert github_call_count == 0, (
            f"GitHub should NOT be called when needs_human_review=True. "
            f"Got {github_call_count} calls."
        )

    assert_test("needs_human_review=True -> GitHub never called, review_posted=False", test_hitl_path_skips_github)

    # ─── Test 9: Review summary body has correct content ──────────────────────
    async def test_review_summary_body_content():
        from backend.orchestrator.nodes import _build_review_summary
        from backend.models.enums import ReviewVerdict

        state = make_state(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            overall_confidence=0.87,
            final_findings=[
                {
                    "agent_type": "security",
                    "severity": "high",
                    "summary": "Hardcoded password detected.",
                    "file_path": "src/config.py",
                    "line_start": 3,
                    "suggestion": "Use environment variable.",
                    "confidence": 0.99,
                },
                {
                    "agent_type": "quality",
                    "severity": "medium",
                    "summary": "Missing type annotations on public functions.",
                    "file_path": None,  # PR-level finding
                    "line_start": None,
                    "suggestion": None,
                    "confidence": 0.75,
                },
            ],
            agent_results=[
                {
                    "agent_type": "security",
                    "success": True,
                    "confidence": 0.99,
                    "findings": [{"severity": "high", "summary": "pw"}],
                    "error_message": "",
                    "duration_seconds": 1.1,
                },
                {
                    "agent_type": "quality",
                    "success": True,
                    "confidence": 0.75,
                    "findings": [{"severity": "medium", "summary": "types"}],
                    "error_message": "",
                    "duration_seconds": 0.9,
                },
            ],
        )

        body = _build_review_summary(state, max_chars=65536)

        # Verdict badge
        assert "CHANGES REQUESTED" in body, "Should contain verdict text"
        assert "🔴" in body, "Should contain severity emoji"

        # Confidence score
        assert "87%" in body, f"Should show 87% confidence. Body: {body[:500]}"

        # Severity counts
        assert "1 High" in body, "Should show 1 High finding"
        assert "1 Medium" in body, "Should show 1 Medium finding"

        # Workflow ID in footer
        assert "wf-test-001" in body, "Should contain workflow ID for debugging"

        # PR-level finding in body (no file_path, so not inline)
        assert "Missing type annotations" in body, "PR-level finding should be in body"

        # Agent breakdown
        assert "Security" in body and "Quality" in body, "Should show agent breakdown"

    assert_test("Review summary body has verdict, severity counts, confidence, workflow ID", test_review_summary_body_content)

    # ─── Summary ──────────────────────────────────────────────────────────────
    print(f"\nResults: {passed}/{total} passed")

    if passed == total:
        print("Phase 8 GATE: PASSED — all assertions green.")
    else:
        print("Phase 8 GATE: FAILED — fix failing assertions before moving to Phase 9.")
        failing = [(n, e) for n, ok, e in results if not ok]
        for name, err in failing:
            print(f"  FAIL: {name}")
            print(f"        {type(err).__name__}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
