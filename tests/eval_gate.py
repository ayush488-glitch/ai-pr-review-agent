# tests/eval_gate.py
#
# WHAT THIS IS:
#   Eval gate — the final CI check before every deploy to Railway.
#   Runs the full 4-agent LangGraph review pipeline in-process using the
#   REAL OpenAI API and the REAL prompt registry, but with mocked GitHub,
#   Redis, and Postgres (so no containers needed).
#
# WHY THIS EXISTS (separate from smoke_phase14.py):
#   Smoke tests mock the LLM. They verify plumbing, not behavior.
#   The eval gate tests the ACTUAL prompts + ACTUAL model calls.
#   If anyone edits backend/prompts/templates/security/v1.txt and the
#   verdict changes from REQUEST_CHANGES to APPROVE, this test fails
#   and blocks the Railway deploy.
#   This is lightweight "prompt versioning in CI" — no separate eval
#   framework needed, just pytest + a known fixture.
#
# FIXTURE:
#   fixtures/sample_pr_opened.json — the same fixture the demo uses.
#   It contains a hardcoded Stripe API key, SQL injection via f-string,
#   no tests for new functions, and no docstrings.
#   The security agent MUST find these. Verdict MUST be request_changes.
#
# ASSERTIONS:
#   1. verdict == "request_changes"  (hardcoded secret triggers this reliably)
#   2. confidence >= 0.70            (agents must be reasonably confident)
#   3. findings >= 10                (fixture has known issues across all 4 agents)
#
# WHAT IS MOCKED (no containers needed):
#   - GitHub API client  (PR fetch → 404 is fine, we provide webhook payload)
#   - Postgres session   (no DB save during the pipeline itself)
#   - Redis / ARQ        (not called by the engine directly)
#   - Qdrant             (RAG returns empty — first PR, no prior index)
#
# WHAT IS REAL (actual network calls):
#   - OpenAI API  (OPENAI_API_KEY from environment)
#   - LangGraph orchestration (all 4 nodes run for real)
#   - Prompt registry (reads from backend/prompts/templates/ on disk)
#
# COST:
#   ~4 LLM calls (one per agent) × ~1500 input tokens × gpt-4o-mini price
#   ≈ $0.001 per run. Acceptable for a deploy gate.
#
# RUN LOCALLY:
#   OPENAI_API_KEY=sk-... python3 -m pytest tests/eval_gate.py -v -s
#
# SKIP IN CI (for PRs, to avoid spend — only run on push to main):
#   Controlled by the ci.yml eval-gate job's trigger condition.

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "sample_pr_opened.json"


def _load_fixture() -> dict:
    """Load the canonical sample PR fixture from disk."""
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def _build_input_data(payload: dict) -> dict:
    """
    Build the input_data dict that the ARQ worker passes to engine.run().

    Must match exactly what the webhook router puts into input_data —
    flat keys, not nested. (demo-day-readiness Pitfall #17: key mismatch
    between enqueue and worker.)
    """
    pr = payload["pull_request"]
    repo = payload["repository"]
    return {
        "repo_full_name":  repo["full_name"],         # "octocat/acme-app"
        "pr_number":       pr["number"],               # 42
        "pr_title":        pr["title"],
        "pr_description":  pr.get("body", ""),
        "head_commit_sha": pr["head"]["sha"],          # 40-char hex SHA
        "base_branch":     pr["base"]["ref"],
        "head_branch":     pr["head"]["ref"],
        "author":          pr["user"]["login"],
        "diff_url":        pr.get("diff_url", ""),
    }


# ---------------------------------------------------------------------------
# Skip guard — only run when OPENAI_API_KEY is present.
#
# WHY: the eval gate makes real OpenAI calls. Running it without a key
# would fail with an auth error, not a meaningful test failure. Skipping
# keeps the test suite green in environments without credentials (local dev
# without .env sourced, or CI PRs where we deliberately skip to save spend).
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping eval gate (real LLM calls required)",
)


# ---------------------------------------------------------------------------
# The eval gate test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eval_gate_sample_pr_verdict():
    """
    End-to-end eval gate: full LangGraph pipeline on the known fixture.

    MOCKED:
      - GitHub diff fetch: returns the PR description as a synthetic diff
        (no real GitHub token needed; the fixture's hardcoded secret + SQL
        injection is in the PR body, which the build_context node injects)
      - GitHub post_review: swallowed (no real repo to post to)
      - Postgres session: AsyncMock (no DB write during review pipeline)
      - Qdrant search: returns [] (no prior index, RAG contributes nothing)

    REAL:
      - All 4 agents call OpenAI (OPENAI_API_KEY from env)
      - LangGraph orchestration: all nodes run
      - Prompt registry: reads templates/*.txt from disk
    """
    payload = _load_fixture()
    input_data = _build_input_data(payload)

    # Build a synthetic diff that contains the fixture's known issues.
    # The fixture's PR body describes: hardcoded Stripe key, SQL via f-string,
    # no tests, no docstrings. We embed that into a fake diff so agents see it.
    # This makes the test self-contained — no GitHub token needed to fetch the
    # real diff from api.github.com/repos/octocat/acme-app/pulls/42.
    synthetic_diff = """
diff --git a/payments/stripe_client.py b/payments/stripe_client.py
+++ b/payments/stripe_client.py
+import stripe
+
+# TODO: move to environment variable before prod deploy
+STRIPE_API_KEY = "sk_live_4242424242424242424242424242424242424242"
+stripe.api_key = STRIPE_API_KEY
+
+def charge_customer(amount: int, currency, customer_id):
+    return stripe.PaymentIntent.create(amount=amount, currency=currency, customer=customer_id)

diff --git a/payments/processor.py b/payments/processor.py
+++ b/payments/processor.py
+import db
+
+def process_order(order_id, user_id):
+    result = db.execute(f"SELECT * FROM orders WHERE id = {order_id} AND user_id = {user_id}")
+    if result:
+        result2 = db.execute(f"UPDATE orders SET status='paid' WHERE id = {order_id}")
+    charge_customer(result['amount'], 'usd', user_id)
+    send_email(user_id, 'Your order has been processed')
+    log_to_analytics(order_id)

diff --git a/utils/crypto.py b/utils/crypto.py
+++ b/utils/crypto.py
+import random
+
+def generate_idempotency_key():
+    return str(random.randint(100000, 999999))
"""

    # Workflow ID follows the canonical format: owner/repo:pr_number:sha
    workflow_id = (
        f"{input_data['repo_full_name']}:"
        f"{input_data['pr_number']}:"
        f"{input_data['head_commit_sha']}"
    )

    # -------------------------------------------------------------------
    # MOCK LAYER: patch everything that requires running containers.
    # We patch at the module level where the orchestrator nodes import from,
    # not at the source module, so the patches stick when nodes call them.
    # -------------------------------------------------------------------

    # Mock 1: GitHub client — return our synthetic diff instead of fetching real PR
    mock_github = AsyncMock()
    mock_github.get_pull_request_diff.return_value = synthetic_diff
    mock_github.get_pull_request.return_value = {
        "title": input_data["pr_title"],
        "body":  input_data.get("pr_description", ""),
        "user":  {"login": input_data["author"]},
        "head":  {"sha": input_data["head_commit_sha"]},
    }
    # post_review raises 401 on fake repo — that's fine, review is saved before posting
    mock_github.post_review.side_effect = Exception("eval_gate: skipping GitHub post")

    # Mock 2: Qdrant search — return empty (no codebase index for this fake repo)
    mock_qdrant = MagicMock()
    mock_qdrant.search_similar_code = AsyncMock(return_value=[])

    # Mock 3: Postgres session — we don't care about persistence in the eval gate
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session_factory = MagicMock(return_value=mock_session)

    with (
        patch(
            "backend.orchestrator.nodes.github_client",
            mock_github,
        ),
        patch(
            "backend.memory.qdrant_client.qdrant_client",
            mock_qdrant,
        ),
        patch(
            "backend.orchestrator.nodes.get_session_factory",
            return_value=mock_session_factory,
        ),
        patch(
            "backend.database.postgres.get_session_factory",
            return_value=mock_session_factory,
        ),
    ):
        from backend.orchestrator.langgraph_engine import LangGraphEngine

        engine = LangGraphEngine()
        result = await engine.run(
            workflow_id=workflow_id,
            input_data=input_data,
        )

    # -------------------------------------------------------------------
    # ASSERTIONS
    # -------------------------------------------------------------------

    # 1. The pipeline completed (not timed out or crashed)
    assert result.status.value in ("completed", "needs_human_review"), (
        f"Expected completed or needs_human_review, got: {result.status} "
        f"(error: {result.error_message})"
    )

    # 2. Verdict must be request_changes (hardcoded Stripe key + SQL injection
    #    in the fixture are serious enough to block the PR)
    assert result.verdict is not None, "Verdict is None — pipeline did not produce a decision"
    assert result.verdict.value == "request_changes", (
        f"Expected 'request_changes', got '{result.verdict.value}'. "
        f"A prompt regression may have changed the agents' behavior. "
        f"Review changes to backend/prompts/templates/ before deploying."
    )

    # 3. Overall confidence must be at least 0.70
    assert result.overall_confidence >= 0.70, (
        f"Confidence {result.overall_confidence:.2f} is below threshold 0.70. "
        f"Agents are not confident enough about the known vulnerabilities in the fixture. "
        f"Check if a prompt change reduced detection quality."
    )

    # 4. At least 10 findings across all 4 agents
    #    (fixture has: hardcoded key, SQL injection ×2, weak random, SRP violation,
    #     missing tests, missing type hints, missing docstrings, no README update)
    total_findings = len(result.findings)
    assert total_findings >= 10, (
        f"Only {total_findings} findings — expected at least 10 for this fixture. "
        f"The fixture contains known hardcoded secrets, SQL injection, and missing tests. "
        f"If findings dropped, a prompt may have become less sensitive."
    )

    # 5. Security agent must have found the hardcoded Stripe key
    #    (this is the highest-confidence finding — it MUST be caught)
    security_findings = [
        f for f in result.findings
        if getattr(f, "category", "") in ("security",) or
           "stripe" in str(getattr(f, "summary", "")).lower() or
           "key" in str(getattr(f, "summary", "")).lower() or
           "secret" in str(getattr(f, "summary", "")).lower() or
           "hardcoded" in str(getattr(f, "summary", "")).lower()
    ]
    assert len(security_findings) >= 1, (
        "Security agent did not find the hardcoded Stripe API key in the fixture. "
        "This is a regression — a hardcoded sk_live_ key is the most obvious security issue. "
        "Check backend/prompts/templates/security/v1.txt for prompt regression."
    )

    # Log summary for CI visibility (shows in pytest -s output)
    print(
        f"\n[eval_gate] PASS | verdict={result.verdict.value} "
        f"confidence={result.overall_confidence:.3f} "
        f"findings={total_findings} "
        f"security_findings={len(security_findings)}"
    )
