# tests/smoke_phase7.py
#
# Phase 7 Gate Smoke Test — GitHub Integration
#
# DESIGN PHILOSOPHY:
#   No live network calls. No GitHub token needed. No CI secrets.
#   All HTTP responses are mocked with httpx.MockTransport.
#
# WIKI: release-it / Stability-Patterns.md
#   "Fast tests encourage running tests often."
#   -> This suite completes in < 3 seconds with zero I/O.
#
# WIKI: Operations-Patterns.md
#   "Trust, but verify."
#   -> We verify the exact exception types, not just that "some error happened".
#   -> We verify retry counts (3 retries on 5xx, 0 retries on 404).
#   -> We verify the exact fields parsed from the mock JSON responses.
#
# ASSERTIONS (9 total — must all pass for Phase 7 gate to open):
#   1. PRMetadata parses GitHub JSON correctly (title, author, sha, base branch)
#   2. get_pr_diff returns raw diff string from mock server
#   3. get_pr_files returns list[PRFile] with correct field mapping
#   4. 5xx response triggers retry — exactly 3 HTTP calls made
#   5. 404 response raises GitHubNotFoundError immediately (no retry)
#   6. 429 response raises GitHubRateLimitError with retry_after_seconds
#   7. GitHubRateLimitError carries correct retry_after value from Retry-After header
#   8. PRReviewState has pr_title + author_login fields (metadata wired into state)
#   9. Rate limit warning fires when X-RateLimit-Remaining < 100

import asyncio
import json
import logging
import sys
import time
from unittest.mock import patch

import httpx

# ─── path setup ──────────────────────────────────────────────────────────────
# Ensure backend package is importable from the project root.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ─────────────────────────────────────────────────────────────────────────────


def make_settings(**overrides):
    """
    Builds a minimal Settings instance for tests.
    github_api_base_url is set to "http://mock-github.local" so GitHubClient
    routes requests through MockTransport without touching api.github.com.
    """
    from backend.config.settings import Settings
    defaults = dict(
        openai_api_key="test-key",
        github_webhook_secret="test-secret",
        github_token="test-token",
        github_api_base_url="http://mock-github.local",
        anthropic_api_key="test-key",
        database_url="sqlite+aiosqlite:///:memory:",
        qdrant_url="http://localhost:9999",  # unreachable — not used in Phase 7
        qdrant_collection_name="test-collection",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ─── Mock response helpers ────────────────────────────────────────────────────

SAMPLE_PR_JSON = {
    "number": 42,
    "title": "feat: add payment retry logic",
    "body": "This PR adds exponential backoff to the payment processor.",
    "user": {"login": "jsmith"},
    "head": {"sha": "a3f8c1d9e2b4f6a8c0d2e4f6a8b0c2d4e6f8a0b2"},
    "base": {"ref": "main"},
    "changed_files": 3,
}

SAMPLE_DIFF = """\
diff --git a/src/payments/processor.py b/src/payments/processor.py
index 1a2b3c4..5d6e7f8 100644
--- a/src/payments/processor.py
+++ b/src/payments/processor.py
@@ -10,6 +10,14 @@ def charge(amount, card_token):
+    retries = 0
+    while retries < 3:
+        try:
+            return stripe.charge(amount, card_token)
+        except stripe.CardError:
+            retries += 1
+            time.sleep(2 ** retries)
"""

SAMPLE_FILES_JSON = [
    {
        "filename": "src/payments/processor.py",
        "status": "modified",
        "additions": 8,
        "deletions": 0,
        "changes": 8,
        "patch": "@@ -10,6 +10,14 @@ def charge(amount, card_token):",
    },
    {
        "filename": "tests/test_processor.py",
        "status": "modified",
        "additions": 12,
        "deletions": 1,
        "changes": 13,
        "patch": "@@ -1,5 +1,16 @@ import pytest",
    },
    {
        "filename": "src/payments/__init__.py",
        "status": "modified",
        "additions": 1,
        "deletions": 0,
        "changes": 1,
        "patch": None,  # test: missing patch handled gracefully
    },
]


def mock_response(
    status_code: int,
    body,
    headers: dict | None = None,
) -> httpx.Response:
    """
    Builds a fake httpx.Response for use with MockTransport.
    body can be a dict (JSON) or a string (raw diff).
    """
    if isinstance(body, (dict, list)):
        content = json.dumps(body).encode()
        content_type = "application/json"
    else:
        content = body.encode() if isinstance(body, str) else body
        content_type = "text/plain"

    default_headers = {
        "Content-Type": content_type,
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Remaining": "4900",
        "X-RateLimit-Reset": "1715000000",
    }
    if headers:
        default_headers.update(headers)

    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=default_headers,
    )


# ─── Test suite ──────────────────────────────────────────────────────────────

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

    print("\n=== Phase 7 Smoke Test — GitHub Integration ===\n")

    # ─── Test 1: PRMetadata parses GitHub JSON correctly ─────────────────────
    async def test_pr_metadata_parsing():
        from backend.integrations.github_client import GitHubClient
        from backend.integrations.github_models import PRMetadata

        def handler(request: httpx.Request) -> httpx.Response:
            return mock_response(200, SAMPLE_PR_JSON)

        settings = make_settings()
        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(settings, transport)

        async with client:
            metadata = await client.get_pr_metadata("jsmith/payments", 42)

        assert isinstance(metadata, PRMetadata), "Should return PRMetadata"
        assert metadata.number == 42
        assert metadata.title == "feat: add payment retry logic"
        assert metadata.author_login == "jsmith"
        assert metadata.head_sha == "a3f8c1d9e2b4f6a8c0d2e4f6a8b0c2d4e6f8a0b2"
        assert metadata.base_branch == "main"
        assert metadata.changed_files_count == 3

    assert_test("PRMetadata parses GitHub JSON correctly", test_pr_metadata_parsing)

    # ─── Test 2: get_pr_diff returns raw diff string ──────────────────────────
    async def test_get_pr_diff():
        from backend.integrations.github_client import GitHubClient

        def handler(request: httpx.Request) -> httpx.Response:
            return mock_response(200, SAMPLE_DIFF)

        settings = make_settings()
        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(settings, transport)

        async with client:
            diff = await client.get_pr_diff("jsmith/payments", 42)

        assert isinstance(diff, str), "Diff should be a string"
        assert "diff --git" in diff, "Diff should contain git diff header"
        assert "processor.py" in diff, "Diff should contain the changed file"
        assert len(diff) > 50, "Diff should not be empty"

    assert_test("get_pr_diff returns raw diff string", test_get_pr_diff)

    # ─── Test 3: get_pr_files returns list[PRFile] ────────────────────────────
    async def test_get_pr_files():
        from backend.integrations.github_client import GitHubClient
        from backend.integrations.github_models import PRFile, PRFileStatus

        def handler(request: httpx.Request) -> httpx.Response:
            # Return the list, then empty list on page 2 to terminate pagination
            if "page=2" in str(request.url):
                return mock_response(200, [])
            return mock_response(200, SAMPLE_FILES_JSON)

        settings = make_settings()
        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(settings, transport)

        async with client:
            files = await client.get_pr_files("jsmith/payments", 42)

        assert len(files) == 3, f"Expected 3 files, got {len(files)}"
        assert all(isinstance(f, PRFile) for f in files)

        proc = next(f for f in files if "processor" in f.filename)
        assert proc.status == PRFileStatus.MODIFIED
        assert proc.additions == 8
        assert proc.deletions == 0

        init_file = next(f for f in files if "__init__" in f.filename)
        assert init_file.patch is None, "Missing patch should be None, not crash"

    assert_test("get_pr_files returns list[PRFile] with correct fields", test_get_pr_files)

    # ─── Test 4: 5xx triggers exactly 3 retry attempts ────────────────────────
    async def test_retry_on_5xx():
        from backend.integrations.github_client import GitHubAPIError

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return mock_response(503, {"message": "Service Unavailable"})

        settings = make_settings()
        transport = httpx.MockTransport(handler)

        # Patch asyncio.sleep to skip actual delays (keep test fast)
        with patch("asyncio.sleep", return_value=None):
            client = _build_client_with_transport(settings, transport)
            try:
                async with client:
                    await client.get_pr_metadata("jsmith/payments", 42)
                assert False, "Should have raised GitHubAPIError"
            except GitHubAPIError as e:
                assert e.status_code == 503
                # 3 retries = 3 HTTP calls total (no 0th attempt + retries pattern,
                # just 3 attempts total: attempt 0, 1, 2)
                assert call_count == 3, (
                    f"Expected exactly 3 HTTP calls (3 retries), got {call_count}"
                )

    assert_test("5xx response triggers exactly 3 retry attempts", test_retry_on_5xx)

    # ─── Test 5: 404 raises GitHubNotFoundError, no retry ─────────────────────
    async def test_404_raises_not_found():
        from backend.integrations.github_client import GitHubNotFoundError

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return mock_response(404, {"message": "Not Found"})

        settings = make_settings()
        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(settings, transport)

        try:
            async with client:
                await client.get_pr_metadata("jsmith/deleted-repo", 999)
            assert False, "Should have raised GitHubNotFoundError"
        except GitHubNotFoundError as e:
            assert e.status_code == 404
            # 404 is deterministic. Should NOT retry. Exactly 1 call.
            assert call_count == 1, (
                f"404 should NOT retry. Expected 1 call, got {call_count}"
            )

    assert_test("404 raises GitHubNotFoundError without retrying", test_404_raises_not_found)

    # ─── Test 6: 429 raises GitHubRateLimitError ──────────────────────────────
    async def test_429_raises_rate_limit_error():
        from backend.integrations.github_client import GitHubRateLimitError

        def handler(request: httpx.Request) -> httpx.Response:
            return mock_response(
                429,
                {"message": "API rate limit exceeded"},
                headers={"Retry-After": "120"},
            )

        settings = make_settings()
        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(settings, transport)

        try:
            async with client:
                await client.get_pr_metadata("jsmith/payments", 42)
            assert False, "Should have raised GitHubRateLimitError"
        except GitHubRateLimitError as e:
            assert e.status_code == 429

    assert_test("429 raises GitHubRateLimitError", test_429_raises_rate_limit_error)

    # ─── Test 7: GitHubRateLimitError carries correct retry_after ─────────────
    async def test_rate_limit_retry_after():
        from backend.integrations.github_client import GitHubRateLimitError

        def handler(request: httpx.Request) -> httpx.Response:
            return mock_response(
                429,
                {"message": "API rate limit exceeded"},
                headers={"Retry-After": "300"},
            )

        settings = make_settings()
        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(settings, transport)

        try:
            async with client:
                await client.get_pr_diff("jsmith/payments", 42)
            assert False, "Should have raised GitHubRateLimitError"
        except GitHubRateLimitError as e:
            assert e.retry_after_seconds == 300, (
                f"Expected retry_after_seconds=300, got {e.retry_after_seconds}"
            )

    assert_test("GitHubRateLimitError carries correct retry_after_seconds", test_rate_limit_retry_after)

    # ─── Test 8: PRReviewState has pr_title + author_login fields ─────────────
    async def test_state_has_metadata_fields():
        from backend.orchestrator.state import PRReviewState

        # This is a TYPE-LEVEL test. We just check the TypedDict keys.
        # No instantiation needed — TypedDict __annotations__ holds all keys.
        annotations = PRReviewState.__annotations__
        required_phase7_fields = [
            "pr_title",
            "pr_body",
            "author_login",
            "head_commit_sha",
            "base_branch",
            "retrieved_context",
        ]
        missing = [f for f in required_phase7_fields if f not in annotations]
        assert not missing, (
            f"PRReviewState is missing Phase 7 fields: {missing}"
        )

    assert_test("PRReviewState has all Phase 7 metadata fields", test_state_has_metadata_fields)

    # ─── Test 9: Rate limit warning fires when X-RateLimit-Remaining < 100 ────
    async def test_rate_limit_warning_logged():
        import logging

        def handler(request: httpx.Request) -> httpx.Response:
            return mock_response(
                200,
                SAMPLE_PR_JSON,
                headers={
                    "X-RateLimit-Remaining": "42",  # below warning threshold of 100
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Reset": "1715000000",
                },
            )

        settings = make_settings()
        transport = httpx.MockTransport(handler)
        client = _build_client_with_transport(settings, transport)

        # Capture WARNING log messages from the github_client logger
        warning_messages = []

        class WarningCapture(logging.Handler):
            def emit(self, record):
                if record.levelno == logging.WARNING:
                    warning_messages.append(record.getMessage())

        capture = WarningCapture()
        gh_logger = logging.getLogger("backend.integrations.github_client")
        gh_logger.addHandler(capture)
        gh_logger.setLevel(logging.WARNING)

        try:
            async with client:
                await client.get_pr_metadata("jsmith/payments", 42)
        finally:
            gh_logger.removeHandler(capture)

        rate_warnings = [m for m in warning_messages if "rate_limit_low" in m]
        assert len(rate_warnings) >= 1, (
            f"Expected at least 1 rate_limit_low warning, got {len(rate_warnings)}. "
            f"All warnings: {warning_messages}"
        )

    assert_test("Rate limit warning fires when X-RateLimit-Remaining < 100", test_rate_limit_warning_logged)

    # ─── Summary ──────────────────────────────────────────────────────────────
    print(f"\nResults: {passed}/{total} passed")

    if passed == total:
        print("Phase 7 GATE: PASSED — all assertions green.")
    else:
        print("Phase 7 GATE: FAILED — fix failing assertions before moving to Phase 8.")
        failing = [(n, e) for n, ok, e in results if not ok]
        for name, err in failing:
            print(f"  FAIL: {name}")
            print(f"        {type(err).__name__}: {err}")
        sys.exit(1)


def _build_client_with_transport(settings, transport: httpx.MockTransport):
    """
    Constructs a GitHubClient but replaces its internal httpx.AsyncClient
    with one backed by the MockTransport.

    This lets us test GitHubClient's full logic (retry, error handling,
    header parsing) without any live network calls.

    WHY THIS APPROACH INSTEAD OF MONKEYPATCHING _request()?
    WIKI: Stability-Patterns.md
      "Test all failure scenarios."
    -> We want to test the FULL retry loop, header parsing, and exception
       classification — not just the business logic.
    -> Injecting MockTransport tests the real code path end-to-end.
    -> Monkeypatching _request() would skip the very code we want to test.
    """
    from backend.integrations.github_client import GitHubClient

    client = GitHubClient(settings)
    # Replace the internal httpx.AsyncClient with one that uses MockTransport.
    # We close the original (no connections yet — just cleanup) and build a new one.
    # The new client reuses the same headers and timeout config.
    original_headers = dict(client._http.headers)
    original_timeout = client._http.timeout

    # Close original (no-op: no connections opened yet) — suppress the warning
    # that comes from garbage-collecting an unclosed client.
    import asyncio as _asyncio
    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(client._http.aclose())
        else:
            loop.run_until_complete(client._http.aclose())
    except Exception:
        pass  # Best-effort cleanup of the original client

    client._http = httpx.AsyncClient(
        base_url=settings.github_api_base_url,
        headers=original_headers,
        timeout=original_timeout,
        transport=transport,
        follow_redirects=True,
    )
    return client


if __name__ == "__main__":
    run_all_tests()
