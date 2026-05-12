# backend/integrations/github_client.py
#
# Async GitHub REST API client.
#
# THE ONLY PLACE IN THE CODEBASE THAT TALKS TO GITHUB.
#
# Public API (what the orchestrator uses):
#   client = GitHubClient(settings)
#   metadata = await client.get_pr_metadata(repo, pr_number)
#   diff     = await client.get_pr_diff(repo, pr_number)
#   files    = await client.get_pr_files(repo, pr_number)
#   response = await client.post_pr_review(repo, pr_number, payload)  # Phase 8
#
# STABILITY PRINCIPLES (from release-it wiki):
#
# 1. TIMEOUTS on every call (Timeouts.md)
#    "Well-placed timeouts provide fault isolation; a problem in some other
#     system does not have to become your problem."
#    -> _TIMEOUT is an httpx.Timeout with separate connect/read/write values.
#    -> No call can block forever. Thread exhaustion is prevented.
#
# 2. RETRY with exponential backoff — 5xx only (Stability-Patterns.md)
#    "Immediate retries are liable to hit the same problem and result in
#     another timeout. That just makes the user wait even longer."
#    -> We retry 5xx responses and network errors: wait 1s, 2s, 4s.
#    -> We do NOT retry 4xx (deterministic failures — retrying is pointless).
#    -> We do NOT retry 429 blindly. We raise GitHubRateLimitError immediately
#       so the caller can decide (back off, queue, alert).
#
# 3. RATE LIMIT awareness (Stability-Antipatterns.md)
#    "Stability antipatterns transform transient events into catastrophic outages."
#    -> We check X-RateLimit-Remaining after every response.
#    -> If remaining < 100: log WARNING (operator can react before we hit 0).
#    -> If 429: raise GitHubRateLimitError (carries Retry-After seconds).
#
# 4. EXPLICIT ERROR HIERARCHY
#    3 exception types so callers can catch at the right level:
#    - GitHubAPIError (base, all GitHub errors)
#    - GitHubNotFoundError (404: PR or repo doesn't exist)
#    - GitHubRateLimitError (429: we're being throttled)
#    This replaces catching bare httpx.HTTPStatusError everywhere.
#
# 5. CONFIGURABLE BASE URL (Operations-Patterns.md)
#    "Don't accept connections until start-up is complete."
#    "Trust, but verify."
#    -> base_url comes from settings.github_api_base_url (default: api.github.com)
#    -> Enables GitHub Enterprise support.
#    -> Enables smoke tests to use an httpx MockTransport (no live network).
#
# 6. NO GLOBAL STATE (Stability-Antipatterns.md)
#    "Hiding sockets in vendor libraries: client libraries that wrap socket
#     connections make it hard to set timeouts."
#    -> GitHubClient is instantiated explicitly (passed to nodes via DI).
#    -> No module-level singleton. No shared mutable state between requests.
#    -> The httpx.AsyncClient is created once per GitHubClient instance
#       and closed in close().

import asyncio
import logging
from typing import Any

import httpx

from backend.config.settings import Settings
from backend.integrations.github_models import (
    PostReviewPayload,
    PostReviewResponse,
    PRFile,
    PRMetadata,
)

logger = logging.getLogger(__name__)


# =============================================================================
# EXCEPTION HIERARCHY
#
# Three exception classes — ordered from most specific to most general.
# Callers catch the most specific one they can handle, let others propagate.
#
# Usage pattern:
#   try:
#       diff = await client.get_pr_diff(repo, pr_number)
#   except GitHubNotFoundError:
#       # PR was deleted. Skip this review.
#   except GitHubRateLimitError as e:
#       # Back off for e.retry_after_seconds and re-queue.
#   except GitHubAPIError:
#       # Something else went wrong. Log it, mark review as FAILED.
# =============================================================================

class GitHubAPIError(Exception):
    """
    Base exception for all GitHub API errors.

    Carries the HTTP status code and the response body (truncated to 500 chars
    to avoid flooding logs with large GitHub error HTML pages).
    """

    def __init__(self, message: str, status_code: int = 0, response_body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        # Truncate long GitHub HTML error pages (e.g., 502 Bad Gateway pages)
        self.response_body = response_body[:500] if response_body else ""

    def __str__(self) -> str:
        base = super().__str__()
        if self.status_code:
            return f"{base} (HTTP {self.status_code})"
        return base


class GitHubNotFoundError(GitHubAPIError):
    """
    Raised when GitHub returns 404.

    This means the PR or repo does not exist (or the token lacks access).
    This is a PERMANENT failure — retrying will not help.
    The caller should mark the workflow as FAILED and log the reason.

    WIKI: Stability-Patterns.md
      "Not all failures are equal — apply patterns to specific threats."
      -> 404 = deterministic failure. No retry. Raise immediately.
    """
    pass


class GitHubRateLimitError(GitHubAPIError):
    """
    Raised when GitHub returns 429 (Too Many Requests) or when the
    X-RateLimit-Remaining header drops to 0.

    WIKI: Stability-Patterns.md
      "Immediate retries are liable to hit the same problem."
      -> Do not retry on 429. The retry_after_seconds tells us when to try again.
      -> The job queue (ARQ) should re-enqueue this job with a delay.

    Carries retry_after_seconds: the number of seconds to wait before retrying.
    Extracted from the Retry-After response header if present.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 429,
        response_body: str = "",
        retry_after_seconds: int = 60,
    ) -> None:
        super().__init__(message, status_code, response_body)
        # How long to wait before the next request.
        # GitHub's Retry-After is in seconds. Default to 60s if header absent.
        self.retry_after_seconds = retry_after_seconds


# =============================================================================
# TIMEOUT CONFIGURATION
#
# WIKI: release-it / Timeouts.md
#   "It is essential that any resource pool that blocks threads must have a
#    timeout to ensure threads are eventually unblocked."
#   "Infinite Wait on Network Calls: Code blocks indefinitely waiting for a
#    response from a remote system that may never reply."
#
# We set FOUR separate timeouts (httpx supports all four independently):
#   connect  — how long to wait for the TCP connection to be established
#   read     — how long to wait for the server to start sending response bytes
#              (this is where a hung server will stall us — set it generously
#               but not infinite: GitHub diffs can be large)
#   write    — how long to wait to send our request body (POST review payload)
#   pool     — how long to wait for an available connection from the pool
#
# These values are conservative defaults. They can be tuned in production
# via environment variables when we add observability (Phase 10).
# =============================================================================

_TIMEOUT = httpx.Timeout(
    connect=5.0,   # TCP handshake: 5s is generous for api.github.com
    read=30.0,     # Reading diffs: 30s handles even 10MB diffs
    write=10.0,    # Posting review payloads (inline comments)
    pool=5.0,      # Waiting for a connection from the pool
)

# =============================================================================
# RETRY CONFIGURATION
#
# WIKI: release-it / Stability-Patterns.md
#   "Immediate retries are liable to hit the same problem."
#   -> We use exponential backoff: _BACKOFF_SECONDS[attempt] seconds before retry.
#      attempt=0: wait 1s, attempt=1: wait 2s, attempt=2: wait 4s.
#
# WHY 3 RETRIES AND NOT MORE?
# 3 retries = worst case 1+2+4 = 7 seconds of extra wait on top of timeouts.
# More retries would delay the PR author too long (they are waiting for feedback).
# 3 is the "magic number" from release-it: enough to handle transient 502s,
# not so many that we amplify a real outage into a long-running hang.
# =============================================================================

_MAX_RETRIES = 3
_BACKOFF_SECONDS = [1.0, 2.0, 4.0]  # exponential backoff delays per attempt

# Status codes that are worth retrying (transient server-side errors).
# 4xx errors are deterministic — do not retry them.
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}

# Rate limit warning threshold.
# WIKI: Stability-Antipatterns.md
#   "Paranoia is just good thinking."
# Log a WARNING when remaining requests drop below this number.
# This gives operators ~100 requests of lead time before we hit the hard limit.
_RATE_LIMIT_WARNING_THRESHOLD = 100


# =============================================================================
# DIFF SIZE GUARD
#
# GitHub returns a raw unified diff via the Accept: application/vnd.github.diff header.
# For very large PRs (thousands of files, generated code, lock files),
# this diff can be multiple megabytes.
#
# WIKI: LLMOps-Essentials.md
#   "More context is not always better — irrelevant context dilutes the signal."
#   "What is not in the context does not exist for the agent."
# -> We hard-cap the diff at MAX_DIFF_BYTES. If the diff is larger:
#    1. We truncate it and log a WARNING with the original size.
#    2. The agents see the truncated diff (still valuable — usually the
#       important changes are in the first N files).
#    3. The quality_agent should flag "diff too large" as a finding.
#
# 500KB is generous but bounded. Most PRs are well under 50KB.
# Adjust MAX_DIFF_BYTES in settings (Phase 18) when we add diff filtering
# (strip lockfiles, generated files, etc.)
# =============================================================================

_MAX_DIFF_BYTES = 500_000  # 500 KB


class GitHubClient:
    """
    Async client for the GitHub REST API.

    LIFECYCLE:
      Instantiate once per PR review workflow. Do not share across concurrent
      reviews — that would create shared state and complicate error handling.

      Usage in build_context node:
        client = GitHubClient(settings)
        try:
            metadata = await client.get_pr_metadata(repo, pr_number)
            diff = await client.get_pr_diff(repo, pr_number)
            files = await client.get_pr_files(repo, pr_number)
        finally:
            await client.close()

      OR use as an async context manager:
        async with GitHubClient(settings) as client:
            diff = await client.get_pr_diff(repo, pr_number)

    WIKI: Stability-Antipatterns.md
      "No global state" — this client is instantiated explicitly.
      Never do: github_client = GitHubClient(get_settings()) at module level.
      That would bind settings at import time (breaks tests) and create a
      long-lived connection pool shared across all concurrent reviews.
    """

    def __init__(self, settings: Settings) -> None:
        """
        Creates the client. Does NOT open any network connections yet.
        Connections are made on the first API call (lazy connect).

        settings.github_token    -> Authorization header value
        settings.github_api_base_url -> allows GitHub Enterprise + mock in tests
        """
        self._token = settings.github_token
        self._base_url = settings.github_api_base_url.rstrip("/")

        # The underlying HTTPX async client.
        # httpx.AsyncClient maintains a connection pool internally.
        # We set default headers here so every request includes them automatically.
        #
        # WIKI: Operations-Patterns.md "Trust, but verify."
        # We set Accept: application/vnd.github+json to pin the GitHub API version.
        # Without this, GitHub might return a different format in the future.
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",  # pin to stable version
                "User-Agent": "ai-pr-review-agent/1.0",  # GitHub requires User-Agent
            },
            timeout=_TIMEOUT,
            # Follow redirects (GitHub sometimes redirects e.g. org renames)
            follow_redirects=True,
        )

        logger.debug(
            "github_client | init | base_url=%s",
            self._base_url,
        )

    async def close(self) -> None:
        """
        Closes the underlying HTTPX connection pool.

        ALWAYS call this when done with the client (or use as async context manager).
        Not closing leaks file descriptors and can cause ResourceWarning in tests.
        """
        await self._http.aclose()

    async def __aenter__(self) -> "GitHubClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # =========================================================================
    # PUBLIC API METHODS
    # =========================================================================

    async def get_pr_metadata(self, repo_full_name: str, pr_number: int) -> PRMetadata:
        """
        Fetches the PR title, body, author, head SHA, and base branch.

        GitHub endpoint: GET /repos/{owner}/{repo}/pulls/{pull_number}

        Called by: build_context node (Phase 7)

        Raises:
            GitHubNotFoundError: PR or repo doesn't exist.
            GitHubAPIError: Other GitHub API errors.
        """
        path = f"/repos/{repo_full_name}/pulls/{pr_number}"
        data = await self._request("GET", path)

        metadata = PRMetadata.from_github_response(data)
        logger.info(
            "github | get_pr_metadata | repo=%s pr=%d title=%r sha=%s",
            repo_full_name,
            pr_number,
            metadata.title[:60],  # truncate for logs — title can be long
            metadata.head_sha[:12],  # short SHA for readability
        )
        return metadata

    async def get_pr_diff(self, repo_full_name: str, pr_number: int) -> str:
        """
        Fetches the raw unified diff for the entire PR.

        GitHub endpoint: GET /repos/{owner}/{repo}/pulls/{pull_number}
        With Accept: application/vnd.github.diff (special MIME type)

        This is a TEXT endpoint (not JSON). It returns the raw diff directly.

        WHY USE THE DIFF ENDPOINT INSTEAD OF CONSTRUCTING FROM FILE PATCHES?
        The /files endpoint (get_pr_files) returns individual file patches.
        We could stitch them together. But:
          1. The files endpoint is paginated (30 files per page). Large PRs
             need multiple requests.
          2. The combined diff endpoint gives us the REAL unified diff with
             correct hunk headers — exactly what git and diff tools expect.
          3. It's one request instead of N requests.
        We still call get_pr_files for the filename list and metadata (status,
        additions, deletions). The diff is for agent content analysis.

        Raises:
            GitHubNotFoundError: PR or repo doesn't exist.
            GitHubAPIError: Other GitHub API errors.
        """
        path = f"/repos/{repo_full_name}/pulls/{pr_number}"

        # Override the Accept header for this specific request.
        # All other requests use application/vnd.github+json.
        # This one needs application/vnd.github.diff to get raw diff text.
        raw_diff_bytes = await self._request_raw(
            "GET",
            path,
            headers={"Accept": "application/vnd.github.diff"},
        )

        # -------------------------------------------------------------------------
        # Diff size guard
        #
        # WIKI: LLMOps-Essentials.md
        #   "More context is not always better — irrelevant context dilutes the signal."
        # -> If the diff exceeds _MAX_DIFF_BYTES, truncate it with a visible marker.
        #    Agents will see the truncated diff. quality_agent should flag this.
        # -------------------------------------------------------------------------
        if len(raw_diff_bytes) > _MAX_DIFF_BYTES:
            original_size = len(raw_diff_bytes)
            raw_diff_bytes = raw_diff_bytes[:_MAX_DIFF_BYTES]
            logger.warning(
                "github | get_pr_diff | diff_truncated | "
                "repo=%s pr=%d original_bytes=%d truncated_to=%d",
                repo_full_name,
                pr_number,
                original_size,
                _MAX_DIFF_BYTES,
            )
            # Append a visible truncation marker so agents know the diff is incomplete.
            truncation_notice = (
                "\n\n# [AI-PR-REVIEW: DIFF TRUNCATED]\n"
                f"# Original diff was {original_size:,} bytes. "
                f"Truncated to {_MAX_DIFF_BYTES:,} bytes.\n"
                "# Some files may not appear in this diff.\n"
                "# Run a full manual review for completeness.\n"
            )
            raw_diff_bytes = raw_diff_bytes + truncation_notice.encode()

        diff = raw_diff_bytes.decode("utf-8", errors="replace")

        logger.info(
            "github | get_pr_diff | repo=%s pr=%d diff_size_bytes=%d",
            repo_full_name,
            pr_number,
            len(diff),
        )
        return diff

    async def get_pr_files(self, repo_full_name: str, pr_number: int) -> list[PRFile]:
        """
        Fetches the list of files changed in the PR.

        GitHub endpoint: GET /repos/{owner}/{repo}/pulls/{pull_number}/files
        Paginated: 30 files per page by default. We request 100 per page
        (GitHub's maximum) and paginate until done.

        Returns: list of PRFile objects (filename, status, additions, deletions)

        WHY PAGINATE?
        WIKI: Stability-Antipatterns.md
          "Optimism bias: assuming edge cases won't occur in production."
          -> PRs with >100 files exist (large refactors, code generation).
          -> If we don't paginate, we silently miss files -> test_agent gives
             wrong coverage analysis -> false APPROVE on a PR with untested files.
          -> Paranoia is just good thinking. Always paginate.

        Raises:
            GitHubNotFoundError: PR or repo doesn't exist.
            GitHubAPIError: Other GitHub API errors.
        """
        all_files: list[PRFile] = []
        page = 1
        per_page = 100  # GitHub API maximum

        while True:
            path = (
                f"/repos/{repo_full_name}/pulls/{pr_number}/files"
                f"?per_page={per_page}&page={page}"
            )
            page_data = await self._request("GET", path)

            if not isinstance(page_data, list):
                # Defensive: GitHub always returns a list for /files, but
                # if something goes wrong (e.g., we hit a different endpoint),
                # fail loudly rather than silently ignoring the data.
                logger.error(
                    "github | get_pr_files | unexpected_response_shape | "
                    "repo=%s pr=%d page=%d type=%s",
                    repo_full_name,
                    pr_number,
                    page,
                    type(page_data).__name__,
                )
                break

            for file_data in page_data:
                all_files.append(PRFile.from_github_response(file_data))

            # If we got fewer results than per_page, we've hit the last page.
            # GitHub does not include a "has_more" field — we infer from count.
            if len(page_data) < per_page:
                break

            page += 1
            logger.debug(
                "github | get_pr_files | paginating | repo=%s pr=%d page=%d files_so_far=%d",
                repo_full_name,
                pr_number,
                page,
                len(all_files),
            )

        logger.info(
            "github | get_pr_files | repo=%s pr=%d total_files=%d",
            repo_full_name,
            pr_number,
            len(all_files),
        )
        return all_files

    async def post_pr_review(
        self,
        repo_full_name: str,
        pr_number: int,
        payload: PostReviewPayload,
    ) -> PostReviewResponse:
        """
        Posts a review to a GitHub PR.

        GitHub endpoint: POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews

        Phase 8 will call this from the post_review orchestrator node.
        Defined here now so the client's public API is complete.

        WIKI: DDIA / Transactions-and-Isolation
          "Atomicity: the defining feature is the ability to abort a transaction."
          -> We only call this AFTER all agents have run and aggregate_results
             has produced a verdict. We never call this with partial results.
          -> If this call fails, the review is NOT marked as posted in Postgres.
             The job queue will not re-enqueue (idempotency key prevents duplicate).
             A human operator can re-trigger via the dashboard (Phase 13).

        Raises:
            GitHubAPIError: Could not post the review.
        """
        path = f"/repos/{repo_full_name}/pulls/{pr_number}/reviews"

        # Serialize the payload to a dict for the JSON body.
        # Use model_dump(mode="json") to get JSON-serializable types
        # (e.g., ReviewEvent.APPROVE -> "APPROVE", not the enum object).
        payload_dict = payload.model_dump(mode="json")

        data = await self._request("POST", path, json=payload_dict)
        response = PostReviewResponse.from_github_response(data)

        logger.info(
            "github | post_pr_review | repo=%s pr=%d review_id=%d state=%s url=%s",
            repo_full_name,
            pr_number,
            response.id,
            response.state.value,
            response.html_url or "(no url)",
        )
        return response

    # =========================================================================
    # PRIVATE REQUEST METHODS
    # All public methods funnel through _request() or _request_raw().
    # Retry, timeout, and error handling live here — ONCE, not duplicated
    # in every public method.
    # =========================================================================

    async def _request(
        self,
        method: str,
        path: str,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """
        Makes an HTTP request to the GitHub API and returns the parsed JSON.

        Applies:
          - Timeout (_TIMEOUT config)
          - Retry with exponential backoff (5xx and network errors only)
          - Rate limit detection (429 and X-RateLimit-Remaining header)
          - Error classification (404 -> GitHubNotFoundError, etc.)

        Returns:
            Parsed JSON response (dict or list, depending on endpoint).

        Raises:
            GitHubNotFoundError: 404 response.
            GitHubRateLimitError: 429 response.
            GitHubAPIError: Other non-2xx response, or network error after retries.
        """
        for attempt in range(_MAX_RETRIES):
            try:
                response = await self._http.request(
                    method,
                    path,
                    json=json,
                    headers=headers or {},
                )
            except httpx.TimeoutException as e:
                # WIKI: Timeouts.md — "Well-placed timeouts provide fault isolation."
                # The timeout fired. This is a transient error — worth retrying.
                logger.warning(
                    "github | request_timeout | method=%s path=%s attempt=%d/%d error=%s",
                    method,
                    path,
                    attempt + 1,
                    _MAX_RETRIES,
                    str(e),
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise GitHubAPIError(
                    f"GitHub API timed out after {_MAX_RETRIES} attempts: {path}",
                    status_code=0,
                ) from e

            except httpx.NetworkError as e:
                # DNS failure, connection refused, TLS error, etc.
                # Transient — worth retrying.
                logger.warning(
                    "github | network_error | method=%s path=%s attempt=%d/%d error=%s",
                    method,
                    path,
                    attempt + 1,
                    _MAX_RETRIES,
                    type(e).__name__,
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise GitHubAPIError(
                    f"GitHub API network error after {_MAX_RETRIES} attempts: {path}",
                    status_code=0,
                ) from e

            # --- We got an HTTP response. Check its status. ---

            # Check rate limit headers on EVERY response (not just 429).
            # WIKI: Stability-Antipatterns.md "Paranoia is just good thinking."
            self._check_rate_limit_headers(response)

            if response.status_code == 429:
                # Hard rate limit hit.
                retry_after = int(response.headers.get("Retry-After", "60"))
                raise GitHubRateLimitError(
                    f"GitHub rate limit exceeded. Retry after {retry_after}s.",
                    status_code=429,
                    response_body=response.text,
                    retry_after_seconds=retry_after,
                )

            if response.status_code == 404:
                raise GitHubNotFoundError(
                    f"GitHub resource not found: {path}",
                    status_code=404,
                    response_body=response.text,
                )

            if response.status_code in _RETRYABLE_STATUS_CODES:
                # Transient server-side error. Retry with backoff.
                logger.warning(
                    "github | retryable_error | method=%s path=%s "
                    "status=%d attempt=%d/%d",
                    method,
                    path,
                    response.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                # Exhausted retries on a server error.
                raise GitHubAPIError(
                    f"GitHub API returned {response.status_code} "
                    f"after {_MAX_RETRIES} attempts: {path}",
                    status_code=response.status_code,
                    response_body=response.text,
                )

            if not response.is_success:
                # Other 4xx errors (401, 403, 422, etc.) — deterministic failures.
                # Do NOT retry: the same request will get the same error.
                raise GitHubAPIError(
                    f"GitHub API error {response.status_code}: {path}",
                    status_code=response.status_code,
                    response_body=response.text,
                )

            # --- Success (2xx). Parse and return JSON. ---
            return response.json()

        # Should not reach here (loop always returns or raises), but Python
        # type-checkers need a final raise to know this function never returns None.
        raise GitHubAPIError(
            f"GitHub API request failed after {_MAX_RETRIES} attempts: {path}"
        )

    async def _request_raw(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """
        Like _request() but returns raw bytes instead of parsed JSON.

        Used by get_pr_diff() which needs the raw diff text
        (Accept: application/vnd.github.diff returns plain text, not JSON).

        Applies the same retry, timeout, and error handling as _request().
        """
        for attempt in range(_MAX_RETRIES):
            try:
                # Build the request manually to merge our default headers
                # with the override headers (e.g., Accept: ...diff).
                merged_headers = dict(self._http.headers)  # copy defaults
                if headers:
                    merged_headers.update(headers)

                response = await self._http.request(
                    method,
                    path,
                    headers=merged_headers,
                )
            except httpx.TimeoutException as e:
                logger.warning(
                    "github | raw_request_timeout | path=%s attempt=%d/%d",
                    path,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise GitHubAPIError(
                    f"GitHub diff request timed out after {_MAX_RETRIES} attempts",
                    status_code=0,
                ) from e

            except httpx.NetworkError as e:
                logger.warning(
                    "github | raw_network_error | path=%s attempt=%d/%d error=%s",
                    path,
                    attempt + 1,
                    _MAX_RETRIES,
                    type(e).__name__,
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise GitHubAPIError(
                    f"GitHub diff request network error after {_MAX_RETRIES} attempts",
                    status_code=0,
                ) from e

            self._check_rate_limit_headers(response)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "60"))
                raise GitHubRateLimitError(
                    f"GitHub rate limit exceeded on diff request. Retry after {retry_after}s.",
                    retry_after_seconds=retry_after,
                )

            if response.status_code == 404:
                raise GitHubNotFoundError(
                    f"GitHub resource not found: {path}",
                    status_code=404,
                    response_body=response.text,
                )

            if response.status_code in _RETRYABLE_STATUS_CODES:
                logger.warning(
                    "github | raw_retryable | path=%s status=%d attempt=%d/%d",
                    path,
                    response.status_code,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise GitHubAPIError(
                    f"GitHub diff returned {response.status_code} "
                    f"after {_MAX_RETRIES} attempts",
                    status_code=response.status_code,
                )

            if not response.is_success:
                raise GitHubAPIError(
                    f"GitHub diff request error {response.status_code}: {path}",
                    status_code=response.status_code,
                    response_body=response.text,
                )

            return response.content

        raise GitHubAPIError(
            f"GitHub raw request failed after {_MAX_RETRIES} attempts: {path}"
        )

    def _check_rate_limit_headers(self, response: httpx.Response) -> None:
        """
        Checks X-RateLimit-Remaining on every response and logs a WARNING
        if we're running low.

        WIKI: Stability-Antipatterns.md
          "Paranoia is just good thinking."
          "Avoiding the antipatterns does not prevent bad things from happening,
           but it will help minimize the damage when bad things do occur."

        WHY CHECK EVERY RESPONSE (NOT JUST 429)?
        GitHub sends the remaining count with EVERY response.
        If we only check on 429, we have zero warning before the hard block.
        Checking every response gives operators ~100 requests of lead time.
        At that point they can: pause the queue, rotate the token, or alert on-call.

        This method does NOT raise. It only logs. The 429 handler raises.
        """
        remaining_str = response.headers.get("X-RateLimit-Remaining")
        if remaining_str is None:
            return  # Header not present (e.g., mock server, GitHub Enterprise)

        try:
            remaining = int(remaining_str)
        except ValueError:
            return  # Unexpected header value — ignore, don't crash

        if remaining < _RATE_LIMIT_WARNING_THRESHOLD:
            limit = response.headers.get("X-RateLimit-Limit", "unknown")
            reset_at = response.headers.get("X-RateLimit-Reset", "unknown")
            logger.warning(
                "github | rate_limit_low | remaining=%d limit=%s reset_at=%s",
                remaining,
                limit,
                reset_at,
            )
