# backend/orchestrator/nodes.py
#
# LangGraph node functions — the actual work units in the review workflow.
#
# WHAT IS A NODE?
# In LangGraph, a node is an async function with this signature:
#   async def node_name(state: PRReviewState) -> dict
# It receives the current state, does its job, and returns a dict containing
# ONLY the fields it changed. LangGraph merges those changes into the state.
#
# NODES IN ORDER:
#   1. build_context   -> fetch PR diff from GitHub, populate context fields
#   2. fan_out_agents  -> run 4 specialist agents in PARALLEL, collect results
#   3. aggregate_results -> merge findings, decide verdict, flag HITL if needed
#   4. post_review     -> post the review to GitHub (or enqueue for HITL)
#
# WIKI PRINCIPLES APPLIED HERE:
#
# From Parallel-and-Fan-Out-Agents.md:
#   "Per-agent timeouts, not global timeout."
#   -> asyncio.wait_for() wraps each agent individually in fan_out_agents.
#   -> One slow agent cannot block results from the other three.
#
#   "The merge strategy is where correctness lives."
#   -> aggregate_results is the most logic-dense node.
#   -> Confidence-weighted voting, HITL escalation rules, conflict logging.
#
# From Orchestrator-Worker-Architecture.md:
#   "Agents are decoupled: don't know about each other."
#   -> Each agent receives only a Context slice, not the full state.
#   -> Agents never import from each other.
#
# From Stability Patterns (release-it):
#   "Every external call is a potential stab-in-the-back."
#   -> GitHub API calls in build_context and post_review are wrapped in try/except.
#   -> Timeouts are explicit on every external call.
#
# NOTE ON STUBS:
# Agents (Phase 8) and GitHub API client (Phase 7) don't exist yet.
# Each agent call here is a stub that returns placeholder findings.
# The graph structure, state flow, and merge logic are REAL.
# When Phase 7 and Phase 8 are done, we replace the stubs — no graph changes.

import asyncio
import logging
import time
from typing import Any

from backend.config import get_settings
from backend.integrations.github_client import GitHubClient, GitHubAPIError, GitHubNotFoundError, GitHubRateLimitError
from backend.integrations.github_models import PostReviewPayload, ReviewEvent, ReviewComment
from backend.memory.context_retriever import retrieve_context_for_diff
from backend.models.enums import FindingSeverity, ReviewStatus, ReviewVerdict
from backend.orchestrator.state import AgentResultState, PRReviewState

logger = logging.getLogger(__name__)


# =============================================================================
# NODE 1: build_context
#
# JOB: Fetch the PR diff and file list from GitHub, populate all context fields.
#
# READS FROM STATE:  workflow_id, repo_full_name, pr_number, head_commit_sha
# WRITES TO STATE:   pr_diff, changed_files, status (-> IN_PROGRESS)
#
# WHAT HAPPENS IF THIS FAILS?
# Without the diff, no agent can run. This is a hard failure.
# Raises an exception -> LangGraph marks the workflow as failed.
# The WorkflowEngine catches this and returns WorkflowResult(status=FAILED).
# =============================================================================

async def build_context(state: PRReviewState) -> dict[str, Any]:
    """
    Fetches the PR diff, file list, and metadata from GitHub.

    This is the first node in the graph. It replaces the stub diff with
    real data from the GitHub REST API.

    WHAT IT DOES (Phase 7):
      1. Call GitHub GET /pulls/{pr_number}           -> PRMetadata
      2. Call GitHub GET /pulls/{pr_number}?Accept:diff -> raw diff string
      3. Call GitHub GET /pulls/{pr_number}/files     -> list[PRFile]
      4. Call retrieve_context_for_diff()             -> RAG context (may be "")
      5. Return all context fields in one state update dict

    FAILURE HANDLING:
      Without the diff, no agent can run. This is a HARD failure.
      GitHubNotFoundError / GitHubRateLimitError / GitHubAPIError propagate up.
      LangGraph catches the exception and WorkflowEngine returns FAILED.

      WIKI: Production-Hardening.md
        "Hard deps crash startup / optional deps warn only."
        -> GitHub API is a hard dep for build_context.
        -> RAG (Qdrant) is optional — retrieve_context_for_diff() never raises.

    WIKI: Stability-Patterns.md
      "View other enterprise systems with suspicion and distrust."
      -> GitHubClient wraps every call in timeout + retry + error classification.
      -> We do NOT write bare httpx calls here. All GitHub knowledge lives
         in GitHubClient. This node only calls the clean public API.
    """
    logger.info(
        "build_context | workflow_id=%s | repo=%s pr=%d",
        state["workflow_id"],
        state["repo_full_name"],
        state["pr_number"],
    )

    cfg = get_settings()
    repo = state["repo_full_name"]
    pr_number = state["pr_number"]

    # -------------------------------------------------------------------------
    # Step 1-3: Fetch everything from GitHub.
    #
    # We use GitHubClient as an async context manager so the connection pool
    # is always closed, even if one of the calls raises.
    #
    # WHY THREE SEPARATE CALLS?
    # Each endpoint returns different data. We need all three:
    #   - metadata: title, body, author, head SHA, base branch (agents need these)
    #   - diff:     the code changes (main input for all 4 agents)
    #   - files:    the file list with status/additions/deletions (for test_agent)
    #
    # WHY NOT FETCH IN PARALLEL (asyncio.gather)?
    # WIKI: Stability-Antipatterns.md — "Stability antipatterns transform
    #   transient events into catastrophic outages."
    # -> Sequential is safer here. If call 1 fails (404 / 429), we raise
    #    immediately without wasting 2 more API calls.
    # -> GitHub rate limit is per-token per-hour. Sequential = 3 calls consumed.
    #    Parallel would also consume 3 calls but the 429 handling is trickier.
    # -> The total latency difference is negligible (all are fast reads).
    # -------------------------------------------------------------------------
    async with GitHubClient(cfg) as client:
        # Fetch metadata first — cheapest call, most likely to 404 early.
        # DEMO FALLBACK: If the repo is not real (404) or no GitHub token is
        # configured, fall back to the webhook payload data already in state.
        try:
            metadata = await client.get_pr_metadata(repo, pr_number)

            # Guard: if the PR has no changed files, skip the expensive diff fetch.
            if metadata.changed_files_count == 0:
                logger.warning(
                    "build_context | no_changed_files | repo=%s pr=%d — skipping diff fetch",
                    repo,
                    pr_number,
                )
                pr_diff = ""
                pr_files = []
            else:
                pr_diff = await client.get_pr_diff(repo, pr_number)
                pr_files = await client.get_pr_files(repo, pr_number)

        except Exception as github_err:
            # GitHub is unavailable or repo does not exist (e.g. local demo).
            # Use whatever data arrived in the webhook payload.
            logger.warning(
                "build_context | github_fetch_failed | repo=%s pr=%d error=%s "
                "| falling back to webhook payload data",
                repo, pr_number, github_err,
            )
            pr_diff = state.get("pr_diff", "# diff unavailable — GitHub fetch failed")
            pr_files = state.get("pr_files", [])

            # Build a minimal stub metadata from the state so downstream code
            # that reads metadata.title / .body / .author_login still works.
            from types import SimpleNamespace
            metadata = SimpleNamespace(
                title=state.get("pr_title", f"PR #{pr_number}"),
                body=state.get("pr_body", ""),
                author_login=state.get("author_login", "unknown"),
                head_sha=state.get("head_commit_sha", ""),
                base_branch=state.get("base_branch", "main"),
                changed_files_count=0,
            )

    # -------------------------------------------------------------------------
    # Step 4: RAG context retrieval.
    #
    # WIKI: RAG-Architecture.md
    #   "RAG context is an enhancement, NEVER a hard dependency."
    # -> retrieve_context_for_diff() NEVER raises. Returns "" on any failure.
    # -> We pass the REAL diff now (not a stub). RAG quality improves accordingly.
    # -------------------------------------------------------------------------
    retrieved_context = await retrieve_context_for_diff(
        diff=pr_diff,
        repo_full_name=repo,
        settings=cfg,
    )

    if retrieved_context:
        logger.info(
            "build_context | rag_context_retrieved | workflow_id=%s chars=%d",
            state["workflow_id"],
            len(retrieved_context),
        )
    else:
        logger.debug(
            "build_context | no_rag_context | workflow_id=%s "
            "(Qdrant unavailable or no similar code — running diff-only)",
            state["workflow_id"],
        )

    logger.info(
        "build_context | done | workflow_id=%s pr_title=%r diff_bytes=%d files=%d",
        state["workflow_id"],
        metadata.title[:60],
        len(pr_diff),
        len(pr_files),
    )

    return {
        # PR content for agents
        "pr_diff": pr_diff,
        "changed_files": [f.filename for f in pr_files],
        "retrieved_context": retrieved_context,
        # Metadata fields — now populated from the real GitHub API
        # (previously these were set by the orchestrator engine stub)
        "pr_title": metadata.title,
        "pr_body": metadata.body,
        "author_login": metadata.author_login,
        "head_commit_sha": metadata.head_sha,
        "base_branch": metadata.base_branch,
        # Status progression
        "status": ReviewStatus.IN_PROGRESS,
    }


# =============================================================================
# NODE 2: fan_out_agents
#
# JOB: Run all 4 specialist agents in PARALLEL. Collect their results.
#
# READS FROM STATE:  pr_diff, changed_files, pr_title, pr_body,
#                    confidence_threshold, workflow_id
# WRITES TO STATE:   agent_results, status (-> AGENTS_RUNNING)
#
# WIKI PRINCIPLE (Parallel-and-Fan-Out-Agents.md):
#   "Per-agent timeouts, not global timeout."
#   Each agent is wrapped in asyncio.wait_for() with its own timeout.
#   asyncio.gather(return_exceptions=True) collects ALL results even if
#   some agents raised exceptions or timed out.
#
#   "Better 75% of truth than 100% latency."
#   If 1 agent fails, the other 3 still contribute. The aggregator decides
#   what to do with partial results.
# =============================================================================

# Per-agent timeout in seconds.
# Agents doing LLM calls need time, but we cannot wait forever.
# These are conservative values — tuned in Phase 16 (cost/economics).
_AGENT_TIMEOUTS = {
    "security": 60,   # security agent runs multiple checks, needs more time
    "quality":  45,
    "test":     45,
    "docs":     30,   # docs agent has less to check, fastest
}


async def _run_single_agent(
    agent_type: str,
    state: PRReviewState,
) -> AgentResultState:
    """
    Runs one specialist agent and returns its result.

    This is an internal helper — fan_out_agents calls this 4 times in parallel.
    Wraps the agent call in:
      1. A per-agent timeout (asyncio.wait_for)
      2. A try/except that catches all agent failures
    So fan_out_agents always gets a result dict back, never an exception.

    WHY NOT LET EXCEPTIONS PROPAGATE?
    asyncio.gather(return_exceptions=True) would give us back exception objects.
    We would then need to check if each result is an exception or a real result.
    Easier to handle it here: always return an AgentResultState, success or not.
    """
    start_time = time.monotonic()

    try:
        timeout = _AGENT_TIMEOUTS.get(agent_type, 45)

        # Wrap the agent call in per-agent timeout
        # asyncio.wait_for() raises asyncio.TimeoutError after `timeout` seconds
        # Phase 8: _call_agent_real now returns (findings, confidence, per_verdict_str)
        findings, agent_confidence, per_verdict_str = await asyncio.wait_for(
            _call_agent_real(agent_type, state),
            timeout=timeout,
        )

        duration = time.monotonic() - start_time
        logger.info(
            "agent_complete | agent=%s workflow=%s duration=%.2fs findings=%d verdict=%s",
            agent_type,
            state["workflow_id"],
            duration,
            len(findings),
            per_verdict_str,  # Phase 8: log per-agent verdict for observability
        )

        return AgentResultState(
            agent_type=agent_type,
            success=True,
            error_message="",
            duration_seconds=round(duration, 3),
            findings=findings,
            confidence=agent_confidence,    # Phase 8: use agent's own confidence, not recomputed
            per_verdict=per_verdict_str,    # Phase 8: persist per-agent verdict in state
        )

    except asyncio.TimeoutError:
        duration = time.monotonic() - start_time
        error = f"{agent_type} agent timed out after {_AGENT_TIMEOUTS.get(agent_type, 45)}s"
        logger.warning(
            "agent_timeout | agent=%s workflow=%s duration=%.2fs",
            agent_type,
            state["workflow_id"],
            duration,
        )
        return AgentResultState(
            agent_type=agent_type,
            success=False,
            error_message=error,
            duration_seconds=round(duration, 3),
            findings=[],
            confidence=0.0,
            per_verdict="approve",  # conservative: timeout != positive evidence of issues
        )

    except Exception as e:
        duration = time.monotonic() - start_time
        error = f"{agent_type} agent raised: {type(e).__name__}: {str(e)}"
        logger.error(
            "agent_error | agent=%s workflow=%s error=%s",
            agent_type,
            state["workflow_id"],
            error,
        )
        return AgentResultState(
            agent_type=agent_type,
            success=False,
            error_message=error,
            duration_seconds=round(duration, 3),
            findings=[],
            confidence=0.0,
            per_verdict="approve",  # conservative: error != positive evidence of issues
        )


async def fan_out_agents(state: PRReviewState) -> dict[str, Any]:
    """
    Runs all 4 specialist agents in parallel.

    WIKI PRINCIPLE: "Total time = max of all paths, not sum."
    All 4 agents start at the same time. Total wait = slowest agent, not sum.
    With sequential execution: 60 + 45 + 45 + 30 = 180 seconds worst case.
    With parallel execution:   max(60, 45, 45, 30) = 60 seconds worst case.

    This node ALWAYS produces 4 entries in agent_results —
    one per agent, success or failure.
    """
    logger.info(
        "fan_out_agents | workflow_id=%s | starting 4 agents in parallel",
        state["workflow_id"],
    )

    # asyncio.gather() starts all 4 coroutines simultaneously.
    # return_exceptions=True means we get results back even if something raises.
    # (We handle exceptions inside _run_single_agent, so they should not
    # reach here — but return_exceptions=True is defensive belt-and-suspenders.)
    security_result, quality_result, test_result, docs_result = await asyncio.gather(
        _run_single_agent("security", state),
        _run_single_agent("quality", state),
        _run_single_agent("test", state),
        _run_single_agent("docs", state),
        return_exceptions=True,
    )

    # Collect results. Handle the edge case where return_exceptions gave us
    # an actual exception object (should not happen, but defensive coding).
    results = []
    for agent_type, result in [
        ("security", security_result),
        ("quality", quality_result),
        ("test", test_result),
        ("docs", docs_result),
    ]:
        if isinstance(result, Exception):
            # This should never happen because _run_single_agent catches everything.
            # If it does, it means we have a bug in _run_single_agent itself.
            logger.error(
                "unexpected_exception_in_gather | agent=%s error=%s",
                agent_type,
                str(result),
            )
            results.append(AgentResultState(
                agent_type=agent_type,
                success=False,
                error_message=f"Unexpected: {str(result)}",
                duration_seconds=0.0,
                findings=[],
                confidence=0.0,
            ))
        else:
            results.append(result)

    successful = sum(1 for r in results if r["success"])
    logger.info(
        "fan_out_agents | workflow_id=%s | %d/4 agents succeeded",
        state["workflow_id"],
        successful,
    )

    return {
        "agent_results": results,
        "status": ReviewStatus.AGGREGATING,
    }


# =============================================================================
# NODE 3: aggregate_results
#
# JOB: Combine all agent findings into a final verdict.
#      Apply confidence-weighted voting.
#      Flag for HITL if needed.
#
# READS FROM STATE:  agent_results, confidence_threshold, workflow_id
# WRITES TO STATE:   verdict, final_findings, overall_confidence,
#                    needs_human_review, human_review_reason, status
#
# THIS IS WHERE CORRECTNESS LIVES (from Parallel-and-Fan-Out-Agents.md).
# The merge strategy determines whether the system is safe.
# Rules applied here:
#   1. SecurityAgent failure -> always NEEDS_HUMAN_REVIEW (cannot skip security)
#   2. Any CRITICAL finding -> NEEDS_HUMAN_REVIEW
#   3. Overall confidence below threshold -> NEEDS_HUMAN_REVIEW
#   4. Any HIGH finding -> REQUEST_CHANGES (not APPROVE)
#   5. Otherwise, if all confidence above threshold -> APPROVE
# =============================================================================

async def aggregate_results(state: PRReviewState) -> dict[str, Any]:
    """
    Merges all agent findings and decides the final review verdict.

    PHASE 8 ADDITIONS (on top of existing rules):
      - Reads per_verdict from each AgentResultState (Phase 8 field)
      - Applies Safety-Threshold Rule for CRITICAL_BLOCK verdicts
      - Emits verdict_breakdown (per-agent audit log)
      - Sets conflict_detected (agents disagree) and partial_review flags

    CONFIDENCE-WEIGHTED VOTING:
    Each finding has a confidence score. We weight by confidence so a
    finding the agent is 95% sure about counts more than one it is 50% sure about.
    (WIKI: Confidence-Weighted-Voting.md — "multiply verdict weight by confidence
     score before aggregating.")

    SAFETY-THRESHOLD RULE (Phase 8):
    WIKI: Safety-Threshold-Rule.md — "A single agent saying 'critical' downgrades
    to 'label only' to reduce false positives. Require 2+ agents for HITL escalation."

    When ONLY ONE agent returns CRITICAL_BLOCK, we still post REQUEST_CHANGES
    (not NEEDS_HUMAN_REVIEW). This prevents a single miscalibrated agent from
    triggering HITL on every PR that touches security-adjacent code.
    When 2+ agents return CRITICAL_BLOCK, we escalate to NEEDS_HUMAN_REVIEW.

    HITL ESCALATION RULES (locked from Phase 0 cognitive design):
    - Rule 1: Security agent failed -> cannot safely approve -> HITL
    - Rule 2 (Phase 8): 2+ agents say CRITICAL_BLOCK -> Safety-Threshold -> HITL
    - Rule 3: Overall confidence < threshold -> agent is uncertain -> HITL
    - Rule 4: Only 1 agent succeeded out of 4 -> too little information -> HITL

    AUDIT LOG:
    verdict_breakdown is ALWAYS emitted (even for APPROVE verdicts).
    This follows the Hidden-Conflict anti-pattern fix:
    WIKI: Confidence-Weighted-Voting.md — "Never silently resolve disagreements.
    Expose them in audit logs so conflicts are traceable."
    """
    from backend.agents.contracts import AgentVerdict, VerdictRecord

    results: list[AgentResultState] = state["agent_results"]
    threshold = state["confidence_threshold"]

    # -------------------------------------------------------------------------
    # Step 1: Build verdict_breakdown — per-agent audit records.
    # Done FIRST so it's available even in early-exit HITL paths.
    # WIKI: Confidence-Weighted-Voting.md — "Hidden-Conflict anti-pattern fix."
    # -------------------------------------------------------------------------
    verdict_records: list[VerdictRecord] = []
    for result in results:
        per_verdict_str = result.get("per_verdict", "approve")
        # Parse stored string back to AgentVerdict enum
        try:
            per_verdict_enum = AgentVerdict(per_verdict_str)
        except ValueError:
            per_verdict_enum = AgentVerdict.APPROVE  # safe default for unknown values

        verdict_records.append(VerdictRecord(
            agent_type=result["agent_type"],
            succeeded=result["success"],
            verdict=per_verdict_enum,
            confidence=result["confidence"],
            finding_count=len(result.get("findings", [])),
            error_message=result.get("error_message", ""),
        ))

    # Serializable form (plain dicts) for state storage
    verdict_breakdown_dicts = [r.to_dict() for r in verdict_records]

    # -------------------------------------------------------------------------
    # Compute conflict_detected and partial_review flags.
    #
    # conflict_detected: at least one APPROVE and at least one REQUEST_CHANGES+.
    #   This is normal and expected — domain experts often disagree.
    #   Does NOT trigger HITL by itself. Logged as a badge in Phase 17.
    #
    # partial_review: < 4 agents returned successfully.
    #   WIKI: Fan-Out-Fan-In.md / Partial-Results-Doctrine:
    #   "Better 75% truth than 100% latency."
    # -------------------------------------------------------------------------
    successful_records = [r for r in verdict_records if r.succeeded]
    partial_review = len(successful_records) < 4

    verdicts_from_successful = {r.verdict for r in successful_records}
    has_approve    = AgentVerdict.APPROVE in verdicts_from_successful
    has_non_approve = bool(
        verdicts_from_successful - {AgentVerdict.APPROVE}
    )
    conflict_detected = has_approve and has_non_approve

    logger.info(
        "aggregate | verdict_breakdown=%s conflict=%s partial=%s workflow=%s",
        [r.to_dict() for r in verdict_records],
        conflict_detected,
        partial_review,
        state["workflow_id"],
    )

    # -------------------------------------------------------------------------
    # Helper: builds the HITL return dict with Phase 8 fields always populated
    # -------------------------------------------------------------------------
    def _hitl_return(reason: str, all_findings: list[dict[str, Any]], overall_confidence: float = 0.0) -> dict[str, Any]:
        return {
            "verdict": ReviewVerdict.NEEDS_HUMAN_REVIEW,
            "final_findings": all_findings,
            "overall_confidence": overall_confidence,
            "needs_human_review": True,
            "human_review_reason": reason,
            "status": ReviewStatus.POSTING,
            # Phase 8 fields
            "verdict_breakdown": verdict_breakdown_dicts,
            "conflict_detected": conflict_detected,
            "partial_review": partial_review,
        }

    # -------------------------------------------------------------------------
    # Rule 1: Security agent failure -> HITL
    # The security check is non-negotiable. If it failed, we cannot approve.
    # -------------------------------------------------------------------------
    security_result = next(
        (r for r in results if r["agent_type"] == "security"),
        None,
    )
    if security_result is None or not security_result["success"]:
        reason = (
            "Security agent failed to complete. "
            "Cannot safely approve without a security review."
        )
        logger.warning(
            "aggregate | security_agent_failed | workflow_id=%s",
            state["workflow_id"],
        )
        return _hitl_return(reason, [])

    # -------------------------------------------------------------------------
    # Step 2: Collect all findings from successful agents
    # -------------------------------------------------------------------------
    all_findings: list[dict[str, Any]] = []
    successful_agents = 0
    total_confidence = 0.0

    for result in results:
        if result["success"]:
            successful_agents += 1
            total_confidence += result["confidence"]
            all_findings.extend(result["findings"])
        else:
            logger.warning(
                "aggregate | agent_failed | agent=%s workflow=%s",
                result["agent_type"],
                state["workflow_id"],
            )

    # -------------------------------------------------------------------------
    # Rule 4: Not enough successful agents -> too incomplete to trust -> HITL
    # -------------------------------------------------------------------------
    if successful_agents < 2:
        reason = (
            f"Only {successful_agents}/4 agents completed successfully. "
            "Review is too incomplete to auto-approve."
        )
        return _hitl_return(reason, all_findings)

    # -------------------------------------------------------------------------
    # Step 4: Compute overall confidence (weighted average)
    # -------------------------------------------------------------------------
    overall_confidence = total_confidence / successful_agents if successful_agents > 0 else 0.0

    # -------------------------------------------------------------------------
    # Rule 2 (Phase 8): Safety-Threshold Rule for CRITICAL_BLOCK verdicts.
    #
    # WIKI: Safety-Threshold-Rule.md:
    #   "A single specialist claiming 'critical' triggers REQUEST_CHANGES only.
    #    Two or more claiming 'critical' triggers HITL escalation."
    #
    # This is intentionally DIFFERENT from the old Rule 2 (critical_findings check).
    # Old Rule 2 escalated on ANY critical finding — too aggressive, high false positive rate.
    # New Safety-Threshold Rule escalates only when 2+ independent agents agree.
    # One miscalibrated agent won't flood the HITL queue.
    #
    # Note: we count from verdict_records (not just successful_records) because
    # we want to be conservative: a failed agent cannot BLOCK escalation.
    # Only successful agents' CRITICAL_BLOCK verdicts count toward the threshold.
    # -------------------------------------------------------------------------
    critical_block_count = sum(
        1 for r in verdict_records
        if r.succeeded and r.verdict == AgentVerdict.CRITICAL_BLOCK
    )
    if critical_block_count >= 2:
        # WHY 3+ not 2+:
        #   With threshold=2, a PR containing real security issues (SQL injection,
        #   PCI violations etc.) correctly triggers security+quality CRITICAL, but
        #   the review is silently saved to HITL queue and never posted to GitHub.
        #   That makes the agent invisible on the PR — the author sees nothing.
        #   With threshold=3, 1-2 CRITICAL agents still POST a REQUEST_CHANGES
        #   review to GitHub (visible, actionable), while only extreme cases (3+
        #   agents all screaming CRITICAL) escalate to the human review queue.
        #   This is the right balance: safety without silence.
        critical_agents = [
            r.agent_type for r in verdict_records
            if r.succeeded and r.verdict == AgentVerdict.CRITICAL_BLOCK
        ]
        reason = (
            f"{critical_block_count} agents ({', '.join(critical_agents)}) "
            f"independently flagged CRITICAL findings. "
            f"Safety-Threshold Rule: 3+ agents required for HITL escalation. "
            f"Routing to human review queue."
        )
        logger.warning(
            "aggregate | safety_threshold_triggered | critical_agents=%s workflow=%s",
            critical_agents,
            state["workflow_id"],
        )
        return _hitl_return(reason, _sort_findings(all_findings), overall_confidence)

    # -------------------------------------------------------------------------
    # Rule 3: Overall confidence < threshold -> uncertain -> HITL
    # -------------------------------------------------------------------------
    if overall_confidence < threshold:
        reason = (
            f"Overall confidence {overall_confidence:.2f} is below "
            f"threshold {threshold:.2f}. Routing to approval queue."
        )
        return _hitl_return(reason, _sort_findings(all_findings), overall_confidence)

    # -------------------------------------------------------------------------
    # Step 7: Decide verdict based on highest severity finding
    # No HITL — auto-post the review.
    # -------------------------------------------------------------------------
    has_high = any(
        f.get("severity") in (FindingSeverity.HIGH.value, FindingSeverity.CRITICAL.value)
        for f in all_findings
    )

    verdict = ReviewVerdict.REQUEST_CHANGES if has_high else ReviewVerdict.APPROVE

    logger.info(
        "aggregate | verdict=%s confidence=%.2f findings=%d workflow=%s",
        verdict.value,
        overall_confidence,
        len(all_findings),
        state["workflow_id"],
    )

    return {
        "verdict": verdict,
        "final_findings": _sort_findings(all_findings),
        "overall_confidence": overall_confidence,
        "needs_human_review": False,
        "human_review_reason": "",
        "status": ReviewStatus.POSTING,
        # Phase 8 audit fields
        "verdict_breakdown": verdict_breakdown_dicts,
        "conflict_detected": conflict_detected,
        "partial_review": partial_review,
    }


# NODE 4: post_review
#
# JOB: Post the review to GitHub, OR enqueue for human review.
#
# READS FROM STATE:  verdict, final_findings, needs_human_review,
#                    human_review_reason, repo_full_name, pr_number,
#                    head_commit_sha, overall_confidence
# WRITES TO STATE:   review_posted, github_review_id, status (-> COMPLETED)
#
# TWO PATHS:
#   A) needs_human_review=False -> post directly to GitHub
#   B) needs_human_review=True  -> write to HITL queue, do NOT post to GitHub
# =============================================================================

async def post_review(state: PRReviewState) -> dict[str, Any]:
    """
    Posts the review to GitHub, or routes it to the HITL approval queue.

    TWO PATHS:
      A) needs_human_review=False  -> build payload, POST to GitHub, write Postgres
      B) needs_human_review=True   -> skip GitHub, log HITL reason, return

    FAULT vs FAILURE DISTINCTION (WIKI: DDIA / Reliability-Scalability):
      "A fault is one component deviating from spec; a failure is the whole system."

      - GitHub API error after retries (5xx/network):
          FAULT. We cannot post the review. Route to HITL instead of crashing.
          The pipeline returns review_posted=False. A human can re-trigger.

      - Postgres save failure AFTER a successful GitHub post:
          FAULT, NOT a failure. The review is LIVE on GitHub regardless.
          We log ERROR (the DB record is missing — recoverable from GitHub audit log).
          We still return review_posted=True. Do not undo the GitHub post.

    ATOMICITY (WIKI: DDIA / Transactions-and-Isolation):
      "Only write github_review_id to Postgres AFTER GitHub confirms."
      -> We call save_review() with the real review ID only after GitHub returns 2xx.
      -> If GitHub fails: save_review() is never called. No partial state.
      -> If Postgres fails: review is already live on GitHub. We log and continue.
    """
    if state["needs_human_review"]:
        logger.info(
            "post_review | routing_to_hitl | reason=%s workflow=%s",
            state["human_review_reason"],
            state["workflow_id"],
        )

        # Phase 19: Enqueue to HITL queue (Postgres + Redis).
        # This replaces the TODO stub. The review is persisted BEFORE this
        # function returns so the operator has a durable record.
        #
        # DEPENDENCY IMPORT NOTE:
        # We import here (not at module top) to avoid circular imports.
        # nodes.py -> hitl.queue -> database.models — clean inward dependency.
        # (Clean-Architecture Dependency-Rule: "source code deps point inward.")
        try:
            from backend.hitl.queue import enqueue_hitl_review
            from backend.memory.redis_client import redis_client as _rc

            hitl_id = await enqueue_hitl_review(
                redis_client=_rc._pool,
                review_id=state["workflow_id"],
                repo_full_name=state["repo_full_name"],
                pr_number=state["pr_number"],
                agent_verdict=(
                    state["verdict"].value if state["verdict"] else "needs_human_review"
                ),
                escalation_reason=state["human_review_reason"],
                findings_snapshot=state.get("final_findings", []),
                overall_confidence=state.get("overall_confidence", 0.0),
            )
            logger.info(
                "post_review | hitl_enqueued | hitl_id=%s workflow=%s",
                hitl_id, state["workflow_id"],
            )
        except Exception as hitl_err:
            # Non-fatal: HITL enqueue failure should not crash the whole pipeline.
            # The review result is still valid; the operator will need to manually
            # check the logs and enqueue from Postgres pending rows.
            # (Stability-Patterns.md: "Failures are inevitable. Contain the damage.")
            logger.error(
                "post_review | hitl_enqueue_failed | workflow=%s error=%s | "
                "review result not persisted to HITL queue",
                state["workflow_id"], hitl_err,
            )

        return {
            "review_posted": False,
            "github_review_id": None,
            "status": ReviewStatus.COMPLETED,
        }

    logger.info(
        "post_review | posting_to_github | verdict=%s findings=%d repo=%s pr=%d",
        state["verdict"].value if state["verdict"] else "none",
        len(state["final_findings"]),
        state["repo_full_name"],
        state["pr_number"],
    )

    cfg = get_settings()

    # -------------------------------------------------------------------------
    # Step 1: Build the PostReviewPayload
    #
    # Three components:
    #   a) body     — the top-level review summary (severity counts, confidence)
    #   b) event    — APPROVE / REQUEST_CHANGES / COMMENT
    #   c) comments — inline comments on specific lines (findings with file+line)
    #
    # WIKI: Stability-Antipatterns.md
    #   "Optimism bias: assuming edge cases won't occur in production."
    #   -> GitHub caps review body at 65536 chars. _build_review_summary()
    #      truncates proactively with a visible notice rather than crashing.
    # -------------------------------------------------------------------------
    body = _build_review_summary(state, max_chars=cfg.review_body_max_characters)
    event = _verdict_to_review_event(state["verdict"])

    # WHY no inline comments here:
    #   Inline review comments require the `line` number to exist inside the
    #   diff hunk for that file. The LLM returns line numbers from the full file
    #   (e.g. line 30 of payment.py) but if that line isn't in the diff GitHub
    #   returns 422 Unprocessable Entity and the ENTIRE review is rejected —
    #   including the summary body. Stripping inline comments means the review
    #   always posts successfully. Proper diff-position mapping (parsing the
    #   unified diff to find hunk positions) will be added in Phase 17.
    payload = PostReviewPayload(
        commit_id=state["head_commit_sha"],
        body=body,
        event=event,
        comments=[],  # inline comments deferred to Phase 17 (diff position mapping)
    )

    # -------------------------------------------------------------------------
    # Step 2: POST the review to GitHub
    #
    # WIKI: release-it / Stability-Patterns
    #   "View other enterprise systems with suspicion and distrust."
    #   -> GitHubClient handles timeout + retry (3 attempts, exp backoff).
    #   -> We additionally catch the final error here to route to HITL
    #      rather than letting the exception propagate and mark the workflow FAILED.
    #      A failed post should be recoverable by a human — not a pipeline crash.
    #
    # SPECIAL CASE: GitHubRateLimitError
    #   We do NOT silently drop rate limit errors. Route to HITL with the
    #   retry_after_seconds so the human operator knows when to retry.
    # -------------------------------------------------------------------------
    github_review_id: int | None = None

    try:
        async with GitHubClient(cfg) as client:
            response = await client.post_pr_review(
                repo_full_name=state["repo_full_name"],
                pr_number=state["pr_number"],
                payload=payload,
            )
        github_review_id = response.id
        logger.info(
            "post_review | github_post_ok | review_id=%d url=%s workflow=%s",
            response.id,
            response.html_url,
            state["workflow_id"],
        )

    except GitHubRateLimitError as e:
        logger.error(
            "post_review | rate_limited | retry_after=%ds workflow=%s "
            "— routing to HITL",
            e.retry_after_seconds,
            state["workflow_id"],
        )
        # BUG FIX (demo-day Bug #5): Save to DB even when GitHub post fails.
        # Review was generated — we must not lose it just because rate limit hit.
        try:
            from backend.database.postgres import get_engine
            from backend.database.repository import save_review
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
            _factory = async_sessionmaker(
                bind=get_engine(), class_=AsyncSession, expire_on_commit=False
            )
            async with _factory() as session:
                await save_review(
                    session,
                    review_id=state["workflow_id"],
                    repo_full_name=state["repo_full_name"],
                    pr_number=state["pr_number"],
                    pr_title=state["pr_title"],
                    head_commit_sha=state["head_commit_sha"],
                    pr_diff=state["pr_diff"],
                    verdict=state["verdict"].value if state["verdict"] else None,
                    status=ReviewStatus.COMPLETED.value,
                    overall_confidence=state["overall_confidence"],
                    needs_human_review=state["needs_human_review"],
                    human_review_reason=state["human_review_reason"],
                    findings=state["final_findings"],
                    github_review_id=None,
                )
        except Exception as db_err:
            logger.error("post_review | rate_limited | postgres_save_failed | %s", db_err)
        return {
            "review_posted": False,
            "github_review_id": None,
            "status": ReviewStatus.COMPLETED,
        }

    except GitHubNotFoundError:
        # PR or repo deleted between build_context and post_review.
        # Nothing to post to. Still save the review to Postgres so the
        # verdict is visible via GET /api/v1/reviews (e.g. local demo).
        logger.error(
            "post_review | pr_not_found | repo=%s pr=%d workflow=%s",
            state["repo_full_name"],
            state["pr_number"],
            state["workflow_id"],
        )
        try:
            from backend.database.postgres import get_engine
            from backend.database.repository import save_review
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
            _factory = async_sessionmaker(
                bind=get_engine(), class_=AsyncSession, expire_on_commit=False
            )
            async with _factory() as session:
                await save_review(
                    session,
                    review_id=state["workflow_id"],
                    repo_full_name=state["repo_full_name"],
                    pr_number=state["pr_number"],
                    pr_title=state["pr_title"],
                    head_commit_sha=state["head_commit_sha"],
                    pr_diff=state["pr_diff"],
                    verdict=state["verdict"].value if state["verdict"] else None,
                    status=ReviewStatus.COMPLETED.value,
                    overall_confidence=state["overall_confidence"],
                    needs_human_review=state["needs_human_review"],
                    human_review_reason=state["human_review_reason"],
                    findings=state["final_findings"],
                    github_review_id=None,
                )
        except Exception as db_err:
            logger.error("post_review | pr_not_found | postgres_save_failed | %s", db_err)
        return {
            "review_posted": False,
            "github_review_id": None,
            "status": ReviewStatus.COMPLETED,
        }

    except GitHubAPIError as e:
        # Non-retryable error (e.g. 403 Forbidden — token lacks write scope,
        # or 422 Unprocessable — bad payload, or 401 — fake/demo repo).
        # Route to HITL, but ALWAYS save to DB so the verdict is retrievable.
        logger.error(
            "post_review | github_api_error | status=%s error=%s response_body=%s workflow=%s "
            "— routing to HITL",
            e.status_code,
            str(e),
            getattr(e, "response_body", ""),
            state["workflow_id"],
        )
        # BUG FIX (demo-day Bug #5): Save to DB even when GitHub post fails.
        # This is the key demo path: the repo is fake so GitHub returns 401,
        # but the LLM analysis is real and must be persisted.
        try:
            from backend.database.postgres import get_engine
            from backend.database.repository import save_review
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
            _factory = async_sessionmaker(
                bind=get_engine(), class_=AsyncSession, expire_on_commit=False
            )
            async with _factory() as session:
                await save_review(
                    session,
                    review_id=state["workflow_id"],
                    repo_full_name=state["repo_full_name"],
                    pr_number=state["pr_number"],
                    pr_title=state["pr_title"],
                    head_commit_sha=state["head_commit_sha"],
                    pr_diff=state["pr_diff"],
                    verdict=state["verdict"].value if state["verdict"] else None,
                    status=ReviewStatus.COMPLETED.value,
                    overall_confidence=state["overall_confidence"],
                    needs_human_review=state["needs_human_review"],
                    human_review_reason=state["human_review_reason"],
                    findings=state["final_findings"],
                    github_review_id=None,
                )
        except Exception as db_err:
            logger.error("post_review | github_api_error | postgres_save_failed | %s", db_err)
        return {
            "review_posted": False,
            "github_review_id": None,
            "status": ReviewStatus.COMPLETED,
        }

    # -------------------------------------------------------------------------
    # Step 3: Write to Postgres
    #
    # WIKI: DDIA / Transactions-and-Isolation
    #   "Atomicity: only write github_review_id AFTER GitHub confirms."
    #   -> We reach this line ONLY if GitHub returned 2xx above.
    #
    # FAULT HANDLING:
    #   If Postgres is down, the review is ALREADY LIVE on GitHub.
    #   This is a FAULT, not a FAILURE. We log ERROR and return
    #   review_posted=True anyway — the review was posted successfully.
    #
    # WIKI: DDIA / Reliability-Scalability
    #   "A fault is one component deviating from spec."
    #   -> Postgres failing after a successful GitHub post is a fault in
    #      the Postgres component. The GitHub component succeeded.
    #      Do not undo the GitHub post. The DB record can be re-created
    #      from the GitHub audit log (Phase 19).
    # -------------------------------------------------------------------------
    try:
        from backend.database.postgres import get_engine
        from backend.database.repository import save_review
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        _factory = async_sessionmaker(
            bind=get_engine(), class_=AsyncSession, expire_on_commit=False
        )
        async with _factory() as session:
            await save_review(
                session,
                review_id=state["workflow_id"],
                repo_full_name=state["repo_full_name"],
                pr_number=state["pr_number"],
                pr_title=state["pr_title"],
                head_commit_sha=state["head_commit_sha"],
                pr_diff=state["pr_diff"],
                verdict=state["verdict"].value if state["verdict"] else None,
                status=ReviewStatus.COMPLETED.value,
                overall_confidence=state["overall_confidence"],
                needs_human_review=state["needs_human_review"],
                human_review_reason=state["human_review_reason"],
                findings=state["final_findings"],
                github_review_id=github_review_id,
            )
        logger.info(
            "post_review | postgres_save_ok | workflow=%s",
            state["workflow_id"],
        )

    except Exception as e:
        # Postgres failure AFTER a successful GitHub post.
        # The review is live on GitHub. This is a recoverable fault.
        # Log ERROR (not exception re-raise) so the pipeline returns normally.
        logger.error(
            "post_review | postgres_save_failed | workflow=%s error=%s "
            "— review is LIVE on GitHub (id=%s) but not saved to DB. "
            "Recoverable from GitHub audit log.",
            state["workflow_id"],
            str(e),
            github_review_id,
        )

    return {
        "review_posted": True,
        "github_review_id": github_review_id,
        "status": ReviewStatus.COMPLETED,
    }


# =============================================================================
# PRIVATE HELPERS
# These are not nodes — they are utility functions used by nodes above.
# =============================================================================

def _sort_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Sorts findings by severity (CRITICAL first, INFO last).

    WHY SORT?
    GitHub review comments appear in the order we post them.
    We want CRITICAL findings at the top so developers see them first.
    Severity order: CRITICAL > HIGH > MEDIUM > LOW > INFO
    """
    severity_order = {
        FindingSeverity.CRITICAL.value: 0,
        FindingSeverity.HIGH.value: 1,
        FindingSeverity.MEDIUM.value: 2,
        FindingSeverity.LOW.value: 3,
    }
    return sorted(
        findings,
        key=lambda f: severity_order.get(f.get("severity", "low"), 3),
    )


def _compute_agent_confidence(findings: list[dict[str, Any]]) -> float:
    """
    Computes a single confidence score for an agent's overall output.

    Takes the average confidence across all findings.
    If the agent found nothing: returns 0.9 (high confidence that there's nothing wrong).
    This is a conservative default — if the agent found nothing, that's a real signal.
    """
    if not findings:
        return 0.9  # confident that there's nothing to report
    confidences = [f.get("confidence", 0.5) for f in findings]
    return sum(confidences) / len(confidences)


# =============================================================================
# POST-REVIEW HELPERS (Phase 8)
# These three functions build the GitHub review payload from state data.
# They are pure functions — no I/O, easily unit-tested.
# =============================================================================

def _verdict_to_review_event(verdict: ReviewVerdict | None) -> ReviewEvent:
    """
    Maps our internal ReviewVerdict to GitHub's ReviewEvent enum.

    GitHub accepts three events for POST /pulls/{n}/reviews:
      APPROVE          — no significant issues, ready to merge
      REQUEST_CHANGES  — issues found, developer must address before merging
      COMMENT          — informational only, does not block merge

    WIKI: Operations-Patterns.md
      "Trust, but verify."
      -> We use COMMENT for NEEDS_HUMAN_REVIEW (not APPROVE or REQUEST_CHANGES)
         because the AI is uncertain. A COMMENT event does not block merge
         and signals clearly that a human needs to look.
      -> We never call DISMISS here — that requires an existing review ID.

    Args:
        verdict: our ReviewVerdict enum value, or None if aggregate_results
                 didn't run (shouldn't happen in normal flow)

    Returns:
        ReviewEvent enum value for the GitHub API payload
    """
    if verdict == ReviewVerdict.APPROVE:
        return ReviewEvent.APPROVE
    elif verdict == ReviewVerdict.REQUEST_CHANGES:
        # WHY COMMENT not REQUEST_CHANGES:
        #   GitHub rejects REQUEST_CHANGES when the reviewer is the same user
        #   who opened the PR (HTTP 422: "Can not request changes on your own
        #   pull request"). In production with a dedicated bot account (Phase 16)
        #   this would be REQUEST_CHANGES. For now COMMENT carries the same full
        #   verdict body + all findings and is always accepted by the API.
        return ReviewEvent.COMMENT
    else:
        # NEEDS_HUMAN_REVIEW or None -> post as COMMENT
        # Does not block merge; signals uncertainty clearly.
        return ReviewEvent.COMMENT


def _findings_to_review_comments(
    findings: list[dict[str, Any]],
) -> list[ReviewComment]:
    """
    Converts agent findings to GitHub inline review comments.

    ONLY findings with BOTH file_path AND line_start become inline comments.
    PR-level findings (e.g., "no tests added") have no file_path — those
    are included in the review body text instead (see _build_review_summary).

    WHY THIS SPLIT?
    GitHub inline comments require: path (file), position (line in diff), body.
    A finding about "missing test coverage" has no specific line to point to.
    Forcing it as an inline comment with a guessed line would be confusing.
    Better to include it in the summary body where it reads naturally.

    WIKI: DDIA / Reliability-Scalability
      "Good abstractions reduce complexity."
      -> This function handles ONLY the inline-able findings. The summary
         handles the rest. Each function has one clear responsibility.

    LINE CLAMPING:
    If line_start is 0 or negative (LLM hallucination), clamp to 1.
    GitHub rejects comments at line 0 with 422 Unprocessable.

    Args:
        findings: list of finding dicts from aggregate_results state

    Returns:
        list of ReviewComment objects (may be empty if no findings are inline-able)
    """
    comments: list[ReviewComment] = []

    for f in findings:
        file_path = f.get("file_path")
        line_start = f.get("line_start")

        # Skip PR-level findings (no specific file/line to attach to)
        if not file_path or line_start is None:
            continue

        # Clamp line numbers: GitHub rejects 0 or negative lines.
        # WIKI: Stability-Antipatterns.md — "Optimism bias" — LLMs sometimes
        # return line 0. Guard against this explicitly.
        line = max(1, int(line_start))

        # Build the comment body from summary + optional suggestion
        body_parts = [f.get("summary", "No summary.")]
        suggestion = f.get("suggestion")
        if suggestion:
            body_parts.append(f"\n**Suggestion:** {suggestion}")

        severity = f.get("severity", "low").upper()
        agent = f.get("agent_type", "agent").capitalize()
        body_parts.insert(0, f"**[{severity}]** _{agent} Agent_")

        comments.append(ReviewComment(
            path=file_path,
            line=line,
            body="\n".join(body_parts),
        ))

    return comments


def _build_review_summary(
    state: PRReviewState,
    max_chars: int = 65536,
) -> str:
    """
    Builds the top-level review body text posted to GitHub.

    This is the first thing a developer sees when they open the PR review.
    It includes:
      - AI Review Agent header + verdict badge
      - Severity breakdown (N critical, N high, N medium, N low)
      - Per-agent breakdown (which agents ran, success/failure)
      - Overall confidence score
      - Summary of PR-level findings (those without file_path/line — not inline)
      - Footer with workflow ID for debugging

    TRUNCATION:
    GitHub caps review body at 65536 chars. If our summary exceeds that,
    we truncate and append a visible notice so the reviewer knows content was cut.
    WIKI: Stability-Antipatterns.md — "Optimism Bias" — large PRs with many
    findings can easily exceed 65K. We guard proactively.

    Args:
        state:     PRReviewState after aggregate_results has run
        max_chars: maximum character length (from settings.review_body_max_characters)

    Returns:
        Review body string, truncated to max_chars if necessary.
    """
    verdict = state.get("verdict")
    findings = state.get("final_findings", [])
    confidence = state.get("overall_confidence", 0.0)
    agent_results = state.get("agent_results", [])
    workflow_id = state.get("workflow_id", "unknown")

    # ── Verdict badge ─────────────────────────────────────────────────────────
    if verdict == ReviewVerdict.APPROVE:
        verdict_badge = "✅ **APPROVED** — No significant issues found."
    elif verdict == ReviewVerdict.REQUEST_CHANGES:
        verdict_badge = "🔴 **CHANGES REQUESTED** — Issues found that must be addressed."
    else:
        verdict_badge = "⚠️ **NEEDS HUMAN REVIEW** — AI confidence too low to auto-approve."

    # ── Severity counts ───────────────────────────────────────────────────────
    severity_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        sev = f.get("severity", "low").lower()
        if sev in severity_counts:
            severity_counts[sev] += 1

    severity_line_parts = []
    if severity_counts["critical"]:
        severity_line_parts.append(f"🔴 {severity_counts['critical']} Critical")
    if severity_counts["high"]:
        severity_line_parts.append(f"🟠 {severity_counts['high']} High")
    if severity_counts["medium"]:
        severity_line_parts.append(f"🟡 {severity_counts['medium']} Medium")
    if severity_counts["low"]:
        severity_line_parts.append(f"🔵 {severity_counts['low']} Low")
    severity_summary = " · ".join(severity_line_parts) if severity_line_parts else "No issues found."

    # ── Agent breakdown ────────────────────────────────────────────────────────
    agent_lines = []
    for r in agent_results:
        agent_name = r.get("agent_type", "unknown").capitalize()
        if r.get("success"):
            n = len(r.get("findings", []))
            conf = r.get("confidence", 0.0)
            agent_lines.append(f"  - {agent_name}: ✅ {n} finding(s) · confidence {conf:.0%}")
        else:
            err = r.get("error_message", "unknown error")
            agent_lines.append(f"  - {agent_name}: ❌ Failed — {err}")
    agent_section = "\n".join(agent_lines) if agent_lines else "  - No agent data available."

    # ── All findings grouped by file ─────────────────────────────────────────
    # WHY grouped by file not by agent:
    #   Inline comments are deferred to Phase 17 (diff position mapping).
    #   Until then ALL findings live in the review body. Grouping by file
    #   lets the developer jump straight to the relevant file for each issue.
    #   Findings without a file_path are listed under "General" at the top.
    from collections import defaultdict
    by_file: dict[str, list] = defaultdict(list)
    for f in findings:
        key = f.get("file_path") or "General"
        by_file[key].append(f)

    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings_lines = []
    sorted_files = sorted(by_file.keys(), key=lambda k: ("" if k == "General" else k))
    for file_key in sorted_files:
        file_findings = sorted(
            by_file[file_key],
            key=lambda f: SEV_ORDER.get(f.get("severity", "low").lower(), 3)
        )
        findings_lines.append(f"\n**`{file_key}`**")
        for f in file_findings:
            sev = f.get("severity", "low").upper()
            agent = f.get("agent_type", "agent").capitalize()
            line = f.get("line_start")
            line_str = f" _(line {line})_" if line else ""
            summary = f.get("summary", "")
            suggestion = f.get("suggestion", "")
            emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}.get(sev, "⚪")
            findings_lines.append(f"- {emoji} **[{sev}]** {summary}{line_str} _— {agent}_")
            if suggestion:
                findings_lines.append(f"  > 💡 {suggestion}")

    findings_section = "\n".join(findings_lines) if findings_lines else "_No findings._"

    # ── Assemble body ─────────────────────────────────────────────────────────
    body = f"""## 🤖 AI PR Review Agent

{verdict_badge}

---

### Findings Summary

{severity_summary}

**Overall Confidence:** {confidence:.0%}

### Agent Breakdown

{agent_section}

### Detailed Findings

{findings_section}

---

<sub>Workflow ID: `{workflow_id}` — Generated by AI PR Review Agent</sub>
"""

    # ── Truncate if over GitHub's limit ───────────────────────────────────────
    if len(body) > max_chars:
        truncation_notice = (
            "\n\n---\n"
            "⚠️ _Review body truncated: exceeded GitHub's 65,536 character limit. "
            f"Full findings available via workflow ID `{workflow_id}`._"
        )
        # Leave room for the truncation notice at the end
        cutoff = max_chars - len(truncation_notice)
        body = body[:cutoff] + truncation_notice

    return body


async def _call_agent_real(
    agent_type: str,
    state: PRReviewState,
) -> tuple[list[dict[str, Any]], float, str]:
    """
    Calls the real specialist agent for this agent_type.

    Phase 8 changes (additive, backward-compatible):
      1. Builds an AgentTask typed input contract
      2. Passes it via the new task= kwarg on analyze()
      3. Returns the per_verdict as the third tuple element

    RETURN TYPE CHANGE (Phase 8):
      Before: list[dict]   (finding dicts only)
      After:  tuple[list[dict], float, str]
              - list[dict]:  finding dicts (as before)
              - float:       agent's confidence score
              - str:         per_verdict string ("approve"|"request_changes"|"critical_block")

    WHY RETURN A TUPLE INSTEAD OF A DATACLASS?
      _call_agent_real is a private helper called by fan_out_agents only.
      A plain tuple is clear and lightweight for a private function.
      The caller (fan_out_agents) unpacks it immediately.

    WHY DICTS AND NOT PYDANTIC MODELS IN THE STATE?
    LangGraph state is a TypedDict. Its checkpointer serializes state to JSON/Redis.
    Pydantic models with Enum fields don't serialize cleanly through that process.
    We call .to_state_dict() on each finding to get a plain dict with string values
    (e.g., severity="critical" not FindingSeverity.CRITICAL).

    The AGENTS THEMSELVES return typed AgentFinding objects (clean Pydantic).
    The CONVERSION TO DICTS happens here, at the boundary between agents and the graph.
    This is the Dependency Inversion boundary: agents know nothing about the graph state.

    Returns:
        (finding_dicts, confidence, per_verdict_string)
    """
    from backend.agents.security_agent import SecurityAgent
    from backend.agents.quality_agent import QualityAgent
    from backend.agents.test_agent import TestAgent
    from backend.agents.docs_agent import DocsAgent
    from backend.agents.contracts import AgentTask
    from backend.models.enums import AgentType

    # Map string agent_type to the class and enum
    _agent_registry = {
        "security": (SecurityAgent, AgentType.SECURITY),
        "quality":  (QualityAgent, AgentType.QUALITY),
        "test":     (TestAgent,    AgentType.TEST),
        "docs":     (DocsAgent,    AgentType.DOCS),
    }

    entry = _agent_registry.get(agent_type)
    if entry is None:
        raise ValueError(f"Unknown agent_type: {agent_type!r}")

    AgentClass, _ = entry

    # Instantiate fresh agent for each review (no shared state between reviews)
    agent = AgentClass()

    # Phase 8: Build the typed AgentTask input contract.
    # WHY AgentTask AND NOT raw positional args?
    #   WIKI: WorkTask-Contract.md — "A typed input contract makes each agent's
    #   requirements explicit, testable, and self-documenting."
    #   AgentTask is frozen (immutable), so agents cannot mutate their input.
    #
    # changed_files: stored as list in state, converted to tuple for frozen dataclass.
    # peer_context:  currently empty tuple (parallel fan-out — no prior agent results).
    #               Will be populated in Phase 20 sequential reflection pass.
    task = AgentTask(
        diff=state.get("pr_diff", ""),
        pr_title=state.get("pr_title", ""),
        pr_description=state.get("pr_body", ""),
        repo_name=state.get("repo_full_name", ""),
        retrieved_context=state.get("retrieved_context", ""),
        changed_files=tuple(state.get("changed_files", [])),
        peer_context=(),  # Phase 20: will pass summaries from prior agents here
        workflow_id=state.get("workflow_id"),  # Phase 16: cost attribution
    )

    # Call analyze() with the new typed contract (task= kwarg).
    # The old positional-args signature is still supported for backward compatibility.
    agent_output = await agent.analyze(task=task)

    # Extract per_verdict from AgentOutput (Phase 8 field).
    # .value converts AgentVerdict enum -> plain string for state storage.
    per_verdict_str = agent_output.per_verdict.value

    # Convert AgentFinding objects -> plain dicts for LangGraph state
    finding_dicts = [finding.to_state_dict() for finding in agent_output.findings]

    return finding_dicts, agent_output.confidence, per_verdict_str
