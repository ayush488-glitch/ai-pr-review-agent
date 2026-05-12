# tests/smoke_phase8b.py
#
# Phase 8: Multi-Agent Systems — smoke tests.
#
# What this tests:
#   1. AgentTask construction and immutability (frozen dataclass)
#   2. AgentVerdict enum values
#   3. PeerFindingSummary construction
#   4. VerdictRecord.to_dict() serialization
#   5. _derive_per_agent_verdict() — severity -> verdict mapping
#   6. BaseAgent.analyze() with AgentTask (new task= kwarg)
#   7. BaseAgent.analyze() with old positional args (backward compat)
#   8. Safety-Threshold Rule — 1 CRITICAL_BLOCK -> REQUEST_CHANGES
#   9. Safety-Threshold Rule — 2+ CRITICAL_BLOCK -> NEEDS_HUMAN_REVIEW
#  10. Conflict detection (agents disagree)
#  11. Partial review flag (< 4 agents succeeded)
#  12. verdict_breakdown always emitted (even on APPROVE)
#  13. aggregate_results Rule 1: security failure -> HITL
#  14. aggregate_results Rule 4: < 2 agents -> HITL
#  15. aggregate_results Rule 3: low confidence -> HITL
#  16. aggregate_results happy path: all approve -> APPROVE
#
# All tests are synchronous where possible (no LLM calls, no DB).
# aggregate_results tests mock the state dict directly.

import asyncio
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =============================================================================
# Fixtures and helpers
# =============================================================================

def _make_finding_dict(severity: str, category: str = "security", summary: str = "test") -> dict:
    """Creates a minimal finding dict (the shape stored in state)."""
    return {
        "severity": severity,
        "category": category,
        "summary": summary,
        "file_path": "src/auth.py",
        "line_start": 10,
        "line_end": 12,
        "suggestion": "Fix this.",
        "confidence": 0.9,
        "agent_type": "security",
    }


def _make_agent_result(
    agent_type: str,
    success: bool = True,
    findings: list | None = None,
    confidence: float = 0.85,
    per_verdict: str = "approve",
    error_message: str = "",
) -> dict:
    """Creates a minimal AgentResultState dict (as stored in LangGraph state)."""
    return {
        "agent_type": agent_type,
        "success": success,
        "error_message": error_message,
        "duration_seconds": 1.0,
        "findings": findings or [],
        "confidence": confidence,
        "per_verdict": per_verdict,
    }


def _make_state(agent_results: list) -> dict:
    """
    Creates a minimal PRReviewState dict for testing aggregate_results.
    Sets confidence_threshold=0.7 (a reasonable production value).
    """
    return {
        "workflow_id": "test-workflow-id",
        "repo_full_name": "acme/payment-service",
        "pr_number": 42,
        "pr_title": "feat: add retry logic",
        "pr_body": "Adds exponential backoff to payment processor.",
        "pr_diff": "--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1,3 +1,4 @@\n+import os",
        "author_login": "jsmith",
        "head_commit_sha": "abc123",
        "base_branch": "main",
        "changed_files": ["src/auth.py"],
        "retrieved_context": "",
        "idempotency_key": "test-key",
        "agent_results": agent_results,
        "verdict": None,
        "final_findings": [],
        "overall_confidence": 0.0,
        "needs_human_review": False,
        "human_review_reason": "",
        "status": "running",
        "confidence_threshold": 0.7,
        "review_posted": False,
        "github_review_id": None,
    }


# =============================================================================
# Test 1: AgentTask construction and basic field access
# =============================================================================

def test_agent_task_construction():
    """AgentTask can be built from all fields. Fields are accessible."""
    from backend.agents.contracts import AgentTask

    task = AgentTask(
        diff="--- a/auth.py",
        pr_title="fix: auth bypass",
        pr_description="Closes #99",
        repo_name="acme/api",
        retrieved_context="some context",
        changed_files=("src/auth.py", "tests/test_auth.py"),
        peer_context=(),
    )

    assert task.diff == "--- a/auth.py"
    assert task.pr_title == "fix: auth bypass"
    assert task.repo_name == "acme/api"
    assert "src/auth.py" in task.changed_files
    assert task.peer_context == ()


# =============================================================================
# Test 2: AgentTask is immutable (frozen=True)
# =============================================================================

def test_agent_task_is_frozen():
    """AgentTask fields cannot be mutated — frozen dataclass."""
    from backend.agents.contracts import AgentTask

    task = AgentTask(
        diff="diff",
        pr_title="title",
        pr_description="desc",
        repo_name="repo",
    )

    with pytest.raises(FrozenInstanceError):
        task.diff = "mutated"  # type: ignore[misc]


# =============================================================================
# Test 3: AgentTask defaults
# =============================================================================

def test_agent_task_defaults():
    """AgentTask has sensible defaults for optional fields."""
    from backend.agents.contracts import AgentTask

    task = AgentTask(
        diff="diff",
        pr_title="title",
        pr_description="desc",
        repo_name="repo",
    )

    assert task.retrieved_context == ""
    assert task.changed_files == ()
    assert task.peer_context == ()


# =============================================================================
# Test 4: AgentVerdict enum values
# =============================================================================

def test_agent_verdict_values():
    """AgentVerdict has the three expected string values."""
    from backend.agents.contracts import AgentVerdict

    assert AgentVerdict.APPROVE.value == "approve"
    assert AgentVerdict.REQUEST_CHANGES.value == "request_changes"
    assert AgentVerdict.CRITICAL_BLOCK.value == "critical_block"


# =============================================================================
# Test 5: PeerFindingSummary construction
# =============================================================================

def test_peer_finding_summary():
    """PeerFindingSummary is frozen and stores the expected fields."""
    from backend.agents.contracts import PeerFindingSummary

    summary = PeerFindingSummary(
        agent_type="security",
        finding_count=3,
        highest_severity="high",
        flagged_files=("src/auth.py", "src/secrets.py"),
    )

    assert summary.agent_type == "security"
    assert summary.finding_count == 3
    assert "src/auth.py" in summary.flagged_files

    # frozen
    with pytest.raises(FrozenInstanceError):
        summary.finding_count = 999  # type: ignore[misc]


# =============================================================================
# Test 6: VerdictRecord.to_dict() serialization
# =============================================================================

def test_verdict_record_to_dict():
    """VerdictRecord.to_dict() returns plain strings (no enum objects)."""
    from backend.agents.contracts import AgentVerdict, VerdictRecord

    record = VerdictRecord(
        agent_type="security",
        succeeded=True,
        verdict=AgentVerdict.CRITICAL_BLOCK,
        confidence=0.95,
        finding_count=2,
        error_message="",
    )

    d = record.to_dict()

    # verdict must be a string, not an enum
    assert d["verdict"] == "critical_block"
    assert isinstance(d["verdict"], str)
    assert d["agent_type"] == "security"
    assert d["succeeded"] is True
    assert d["confidence"] == 0.95
    assert d["finding_count"] == 2


# =============================================================================
# Test 7: _derive_per_agent_verdict — CRITICAL finding -> CRITICAL_BLOCK
# =============================================================================

def test_derive_verdict_critical():
    """A CRITICAL finding -> CRITICAL_BLOCK verdict."""
    from backend.agents.contracts import AgentVerdict
    from backend.agents.security_agent import SecurityAgent
    from backend.models.enums import FindingSeverity
    from backend.models.findings import AgentFinding

    agent = SecurityAgent()
    finding = AgentFinding(
        severity=FindingSeverity.CRITICAL,
        category="security",
        summary="SQL injection",
        file_path="src/db.py",
        line_start=42,
        line_end=42,
        suggestion="Use parameterized queries",
        confidence=0.98,
        agent_type="security",
    )

    verdict = agent._derive_per_agent_verdict([finding])
    assert verdict == AgentVerdict.CRITICAL_BLOCK


# =============================================================================
# Test 8: _derive_per_agent_verdict — HIGH finding -> REQUEST_CHANGES
# =============================================================================

def test_derive_verdict_high():
    """A HIGH finding (no CRITICAL) -> REQUEST_CHANGES verdict."""
    from backend.agents.contracts import AgentVerdict
    from backend.agents.security_agent import SecurityAgent
    from backend.models.enums import FindingSeverity
    from backend.models.findings import AgentFinding

    agent = SecurityAgent()
    finding = AgentFinding(
        severity=FindingSeverity.HIGH,
        category="security",
        summary="Weak encryption",
        file_path="src/crypto.py",
        line_start=10,
        line_end=10,
        suggestion="Use AES-256",
        confidence=0.85,
        agent_type="security",
    )

    verdict = agent._derive_per_agent_verdict([finding])
    assert verdict == AgentVerdict.REQUEST_CHANGES


# =============================================================================
# Test 9: _derive_per_agent_verdict — only MEDIUM/LOW -> APPROVE
# =============================================================================

def test_derive_verdict_approve():
    """Only MEDIUM/LOW findings -> APPROVE verdict (no blockers)."""
    from backend.agents.contracts import AgentVerdict
    from backend.agents.security_agent import SecurityAgent
    from backend.models.enums import FindingSeverity
    from backend.models.findings import AgentFinding

    agent = SecurityAgent()
    findings = [
        AgentFinding(
            severity=FindingSeverity.MEDIUM,
            category="security",
            summary="Consider adding CSRF token",
            file_path="src/views.py",
            line_start=5,
            line_end=5,
            suggestion="Add CSRF protection",
            confidence=0.7,
            agent_type="security",
        ),
        AgentFinding(
            severity=FindingSeverity.LOW,
            category="security",
            summary="Log all auth events",
            file_path="src/auth.py",
            line_start=20,
            line_end=20,
            suggestion="Add INFO log",
            confidence=0.6,
            agent_type="security",
        ),
    ]

    verdict = agent._derive_per_agent_verdict(findings)
    assert verdict == AgentVerdict.APPROVE


# =============================================================================
# Test 10: _derive_per_agent_verdict — empty findings -> APPROVE
# =============================================================================

def test_derive_verdict_no_findings():
    """Empty findings list -> APPROVE (nothing found)."""
    from backend.agents.contracts import AgentVerdict
    from backend.agents.security_agent import SecurityAgent

    agent = SecurityAgent()
    verdict = agent._derive_per_agent_verdict([])
    assert verdict == AgentVerdict.APPROVE


# =============================================================================
# Test 11: Safety-Threshold Rule — 1 CRITICAL_BLOCK agent -> REQUEST_CHANGES
# =============================================================================

@pytest.mark.asyncio
async def test_safety_threshold_one_critical_block():
    """
    1 agent says CRITICAL_BLOCK, 3 say APPROVE.
    Safety-Threshold Rule: only 1 -> do NOT escalate to HITL.
    Result: REQUEST_CHANGES (because there are HIGH findings from that agent).
    """
    from backend.orchestrator.nodes import aggregate_results
    from backend.models.enums import ReviewVerdict

    # Security agent: CRITICAL_BLOCK verdict, has a critical finding
    security = _make_agent_result(
        "security", success=True,
        findings=[_make_finding_dict("critical")],
        confidence=0.9,
        per_verdict="critical_block",
    )
    # Other 3 agents: clean
    quality = _make_agent_result("quality", per_verdict="approve", confidence=0.85)
    test_ag = _make_agent_result("test", per_verdict="approve", confidence=0.85)
    docs    = _make_agent_result("docs", per_verdict="approve", confidence=0.85)

    state = _make_state([security, quality, test_ag, docs])

    result = await aggregate_results(state)

    # Must NOT be HITL — only 1 CRITICAL_BLOCK agent
    assert result["verdict"] != ReviewVerdict.NEEDS_HUMAN_REVIEW
    assert result["needs_human_review"] is False
    # Should be REQUEST_CHANGES (there's a critical finding -> has_high is True)
    assert result["verdict"] == ReviewVerdict.REQUEST_CHANGES


# =============================================================================
# Test 12: Safety-Threshold Rule — 2 CRITICAL_BLOCK agents -> HITL
# =============================================================================

@pytest.mark.asyncio
async def test_safety_threshold_two_critical_blocks():
    """
    2 agents say CRITICAL_BLOCK.
    Safety-Threshold Rule: 2+ -> escalate to NEEDS_HUMAN_REVIEW.
    """
    from backend.orchestrator.nodes import aggregate_results
    from backend.models.enums import ReviewVerdict

    security = _make_agent_result(
        "security", success=True,
        findings=[_make_finding_dict("critical")],
        confidence=0.9, per_verdict="critical_block",
    )
    quality = _make_agent_result(
        "quality", success=True,
        findings=[_make_finding_dict("critical", category="quality")],
        confidence=0.88, per_verdict="critical_block",
    )
    test_ag = _make_agent_result("test", per_verdict="approve", confidence=0.85)
    docs    = _make_agent_result("docs", per_verdict="approve", confidence=0.85)

    state = _make_state([security, quality, test_ag, docs])

    result = await aggregate_results(state)

    # 2+ CRITICAL_BLOCK agents -> HITL
    assert result["verdict"] == ReviewVerdict.NEEDS_HUMAN_REVIEW
    assert result["needs_human_review"] is True
    assert "Safety-Threshold Rule" in result["human_review_reason"]


# =============================================================================
# Test 13: verdict_breakdown always emitted
# =============================================================================

@pytest.mark.asyncio
async def test_verdict_breakdown_always_emitted():
    """
    verdict_breakdown is present in the return dict for EVERY verdict type.
    Tests the APPROVE path (no issues found).
    """
    from backend.orchestrator.nodes import aggregate_results
    from backend.models.enums import ReviewVerdict

    results = [
        _make_agent_result("security", per_verdict="approve", confidence=0.9),
        _make_agent_result("quality", per_verdict="approve", confidence=0.88),
        _make_agent_result("test", per_verdict="approve", confidence=0.85),
        _make_agent_result("docs", per_verdict="approve", confidence=0.82),
    ]
    state = _make_state(results)

    result = await aggregate_results(state)

    assert result["verdict"] == ReviewVerdict.APPROVE
    assert "verdict_breakdown" in result
    assert len(result["verdict_breakdown"]) == 4
    # Each record is a dict with the expected keys
    for record in result["verdict_breakdown"]:
        assert "agent_type" in record
        assert "verdict" in record
        assert "confidence" in record
        assert isinstance(record["verdict"], str)  # not an enum


# =============================================================================
# Test 14: conflict_detected flag — agents disagree
# =============================================================================

@pytest.mark.asyncio
async def test_conflict_detected_when_agents_disagree():
    """
    2 agents APPROVE, 1 REQUEST_CHANGES, 1 APPROVE.
    conflict_detected=True but still not HITL (no CRITICAL_BLOCK threshold met).
    """
    from backend.orchestrator.nodes import aggregate_results

    results = [
        _make_agent_result("security", per_verdict="request_changes", confidence=0.88,
                           findings=[_make_finding_dict("high")]),
        _make_agent_result("quality", per_verdict="approve", confidence=0.85),
        _make_agent_result("test", per_verdict="approve", confidence=0.85),
        _make_agent_result("docs", per_verdict="approve", confidence=0.85),
    ]
    state = _make_state(results)

    result = await aggregate_results(state)

    assert result["conflict_detected"] is True
    # Still auto-posts as REQUEST_CHANGES — conflict alone doesn't block
    assert result["needs_human_review"] is False


# =============================================================================
# Test 15: partial_review flag — 3/4 agents succeeded
# =============================================================================

@pytest.mark.asyncio
async def test_partial_review_flag():
    """
    3 agents succeeded, 1 failed (not security).
    partial_review=True, but still proceeds (3 >= 2 threshold).
    """
    from backend.orchestrator.nodes import aggregate_results

    results = [
        _make_agent_result("security", per_verdict="approve", confidence=0.9),
        _make_agent_result("quality", per_verdict="approve", confidence=0.85),
        _make_agent_result("test", per_verdict="approve", confidence=0.85),
        _make_agent_result("docs", success=False, per_verdict="approve",
                           error_message="Timeout", confidence=0.0),
    ]
    state = _make_state(results)

    result = await aggregate_results(state)

    assert result["partial_review"] is True
    assert result["needs_human_review"] is False


# =============================================================================
# Test 16: Rule 1 — security agent failure -> HITL
# =============================================================================

@pytest.mark.asyncio
async def test_rule1_security_failure_hitl():
    """Security agent failure -> NEEDS_HUMAN_REVIEW regardless of others."""
    from backend.orchestrator.nodes import aggregate_results
    from backend.models.enums import ReviewVerdict

    results = [
        _make_agent_result("security", success=False, error_message="LLM timeout",
                           per_verdict="approve", confidence=0.0),
        _make_agent_result("quality", per_verdict="approve", confidence=0.9),
        _make_agent_result("test", per_verdict="approve", confidence=0.9),
        _make_agent_result("docs", per_verdict="approve", confidence=0.9),
    ]
    state = _make_state(results)

    result = await aggregate_results(state)

    assert result["verdict"] == ReviewVerdict.NEEDS_HUMAN_REVIEW
    assert "Security agent failed" in result["human_review_reason"]


# =============================================================================
# Test 17: Rule 4 — only 1 agent succeeded -> HITL
# =============================================================================

@pytest.mark.asyncio
async def test_rule4_too_few_agents_hitl():
    """
    Only 1 agent succeeded (quality) + security is ok but quality only gives us 1 success.
    Wait — security must succeed for Rule 4 to be checked. Let's have security + 0 others.
    """
    from backend.orchestrator.nodes import aggregate_results
    from backend.models.enums import ReviewVerdict

    results = [
        _make_agent_result("security", per_verdict="approve", confidence=0.9),
        _make_agent_result("quality", success=False, error_message="fail",
                           per_verdict="approve", confidence=0.0),
        _make_agent_result("test", success=False, error_message="fail",
                           per_verdict="approve", confidence=0.0),
        _make_agent_result("docs", success=False, error_message="fail",
                           per_verdict="approve", confidence=0.0),
    ]
    state = _make_state(results)

    result = await aggregate_results(state)

    assert result["verdict"] == ReviewVerdict.NEEDS_HUMAN_REVIEW
    assert "1/4 agents" in result["human_review_reason"]


# =============================================================================
# Test 18: Rule 3 — low overall confidence -> HITL
# =============================================================================

@pytest.mark.asyncio
async def test_rule3_low_confidence_hitl():
    """Low overall confidence -> NEEDS_HUMAN_REVIEW."""
    from backend.orchestrator.nodes import aggregate_results
    from backend.models.enums import ReviewVerdict

    results = [
        _make_agent_result("security", per_verdict="approve", confidence=0.5),
        _make_agent_result("quality", per_verdict="approve", confidence=0.4),
        _make_agent_result("test", per_verdict="approve", confidence=0.5),
        _make_agent_result("docs", per_verdict="approve", confidence=0.4),
    ]
    state = _make_state(results)

    result = await aggregate_results(state)

    # Average confidence = (0.5+0.4+0.5+0.4)/4 = 0.45 < threshold 0.7
    assert result["verdict"] == ReviewVerdict.NEEDS_HUMAN_REVIEW
    assert "confidence" in result["human_review_reason"]


# =============================================================================
# Test 19: BaseAgent.analyze() accepts AgentTask via new task= kwarg
# =============================================================================

@pytest.mark.asyncio
async def test_base_agent_accepts_agent_task():
    """
    BaseAgent.analyze() with task= kwarg correctly unpacks AgentTask and
    returns an AgentOutput with per_verdict set.
    """
    from backend.agents.contracts import AgentTask, AgentVerdict
    from backend.agents.security_agent import SecurityAgent
    from backend.tools.llm_client import LLMResponse

    # Mock the LLM client to return a clean "no findings" response
    mock_response = LLMResponse(
        content='{"findings": []}',
        input_tokens=100,
        output_tokens=20,
        model_used="claude-3-haiku-20240307",
        latency_seconds=0.5,
    )
    mock_client = AsyncMock()
    mock_client.complete = AsyncMock(return_value=mock_response)

    agent = SecurityAgent(client=mock_client)

    task = AgentTask(
        diff="--- a/auth.py\n+++ b/auth.py\n@@ -1 +1,2 @@\n+import hashlib",
        pr_title="fix: improve hashing",
        pr_description="Use SHA-256 instead of MD5",
        repo_name="acme/api",
    )

    output = await agent.analyze(task=task)

    # Should succeed, return empty findings, per_verdict=APPROVE
    assert output.error_message is None
    assert output.per_verdict == AgentVerdict.APPROVE
    assert output.findings == []


# =============================================================================
# Test 20: BaseAgent.analyze() with old positional args still works
# =============================================================================

@pytest.mark.asyncio
async def test_base_agent_backward_compat():
    """
    BaseAgent.analyze() with old positional args still returns per_verdict.
    This ensures Phase 5 smoke tests won't break.
    """
    from backend.agents.contracts import AgentVerdict
    from backend.agents.security_agent import SecurityAgent
    from backend.tools.llm_client import LLMResponse

    mock_response = LLMResponse(
        content='{"findings": []}',
        input_tokens=50,
        output_tokens=10,
        model_used="gpt-4o-mini",
        latency_seconds=0.3,
    )
    mock_client = AsyncMock()
    mock_client.complete = AsyncMock(return_value=mock_response)

    agent = SecurityAgent(client=mock_client)

    # Old calling convention: positional args, no task=
    output = await agent.analyze(
        diff="--- a/file.py",
        pr_title="chore: cleanup",
        pr_description="",
        repo_name="acme/api",
    )

    assert output.per_verdict == AgentVerdict.APPROVE
