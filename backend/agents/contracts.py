# backend/agents/contracts.py
#
# Phase 8: Formal typed contracts for the multi-agent system.
#
# WHY THIS FILE EXISTS:
# Before Phase 8, agents received positional string arguments and returned
# findings as a raw list. There was no formal contract — any caller could
# pass any string and hope the agent understood it.
#
# This file formalises THREE contracts:
#
#   1. AgentTask (INPUT CONTRACT):
#      A typed dataclass that every agent receives as its input.
#      All agents receive the same shape of input — consistent, validated, documented.
#      Added: `peer_context` — structured summaries of other agents' findings,
#      allowing agents to avoid double-flagging and gain cross-domain awareness.
#
#   2. AgentVerdict (PER-AGENT OUTPUT CONTRACT — verdict dimension):
#      A per-agent conclusion SEPARATE from the system-wide ReviewVerdict.
#      ReviewVerdict is the FINAL system decision (APPROVE / REQUEST_CHANGES /
#      NEEDS_HUMAN_REVIEW). AgentVerdict is what each INDIVIDUAL agent concludes
#      based solely on its own findings. The arbitrator reads all 4 AgentVerdicts
#      and applies the Safety-Threshold Rule before producing a ReviewVerdict.
#
#   3. VerdictBreakdown (ARBITRATION AUDIT LOG):
#      A list of per-agent verdict records emitted after aggregation.
#      WIKI: Confidence-Weighted-Voting.md — "Hidden Conflict anti-pattern:
#        silently resolving agent disagreements without recording which agents
#        said what. Fix: emit full verdict_breakdown and individual_verdicts
#        in the audit log so conflicts are traceable."
#      Every aggregate_results call emits a VerdictBreakdown. It is stored
#      in state and will be surfaced in the Phase 17 trace viewer.
#
# WIKI CITATIONS:
#   WorkTask-Contract.md:
#     "Each worker needs enough context to solve its problem, but not so much
#      that you blow token budgets."
#   Fan-Out-Fan-In.md / Safety-Threshold-Rule.md:
#     "A single agent saying 'remove' downgrades to 'label' to reduce false
#      positives."
#   Confidence-Weighted-Voting.md:
#     "Merge strategies matter: voting, confidence weighting, safety thresholds,
#      and all-must-pass rules are equally important as the agents themselves."
#
# DEPENDENCY DIRECTION:
#   contracts.py lives in backend/agents/ (the agents layer).
#   It imports ONLY from backend/models/ (the models layer — one level below).
#   Nothing in core/ or orchestrator/ is imported here.
#   This keeps the dependency arrow pointing inward:
#     core <- models <- tools/memory <- agents <- orchestrator
#
# NO CIRCULAR IMPORTS:
#   nodes.py (orchestrator layer) imports AgentTask from here.
#   base_agent.py (agents layer) imports AgentTask from here.
#   contracts.py itself imports only from models/enums.py.

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# =============================================================================
# AgentVerdict
#
# Each specialist agent produces its own verdict based solely on its findings.
# This is NOT the final system verdict (ReviewVerdict) — it is the raw signal
# from one domain expert.
#
# WHY THREE VALUES AND NOT TWO (approve/reject)?
#   APPROVE:          No significant issues found in this agent's domain.
#   REQUEST_CHANGES:  HIGH severity issue found. Confident enough to block.
#   CRITICAL_BLOCK:   CRITICAL severity issue found. Immediate escalation signal.
#                     Treated differently from REQUEST_CHANGES in arbitration:
#                     the Safety-Threshold Rule requires 2+ agents to agree on
#                     CRITICAL_BLOCK before the system escalates to HITL.
#                     One miscalibrated agent should not trigger immediate escalation.
#
# WHY NOT JUST USE FindingSeverity?
#   FindingSeverity is per-finding (individual issue severity).
#   AgentVerdict is per-agent (overall conclusion of this domain expert).
#   An agent might find 3 MEDIUM issues but still conclude APPROVE (no blockers).
#   The mapping: CRITICAL finding -> CRITICAL_BLOCK, HIGH -> REQUEST_CHANGES, else -> APPROVE.
# =============================================================================

class AgentVerdict(str, Enum):
    """
    The per-agent conclusion, distinct from the system-wide ReviewVerdict.

    Produced by _derive_per_agent_verdict() in BaseAgent.
    Read by aggregate_results() in nodes.py to apply the Safety-Threshold Rule.
    """

    # No HIGH or CRITICAL findings. Agent clears its domain.
    APPROVE = "approve"

    # At least one HIGH finding. Warrants requesting changes, but not a full HITL escalation.
    REQUEST_CHANGES = "request_changes"

    # At least one CRITICAL finding. Safety-Threshold Rule: 2+ agents must agree
    # before the system escalates this to NEEDS_HUMAN_REVIEW.
    CRITICAL_BLOCK = "critical_block"


# =============================================================================
# PeerFindingSummary
#
# A compact summary of one agent's findings, shared with other agents
# via AgentTask.peer_context. This is the inter-agent context passing protocol.
#
# WHY COMPACT AND NOT THE FULL FINDING LIST?
#   WIKI: WorkTask-Contract.md — "Give workers only the context keys they need."
#   Passing the full AgentFinding list from a prior agent would:
#     1. Bloat the context window (each finding is ~200 tokens)
#     2. Risk confusing the agent (it might over-anchor on prior agent's framing)
#   A compact summary (agent_type, finding_count, highest_severity, file_paths)
#   gives enough cross-domain awareness to avoid duplicate flagging without
#   blowing the token budget.
#
# PHASE 8 SCOPE:
#   peer_context is populated IN THE SEQUENTIAL CASE only.
#   In the current parallel fan-out (Phase 4), all agents start simultaneously —
#   there are no prior agent results to share at fan-out time.
#   peer_context will be populated in Phase 20 (reflection loop) when a second
#   sequential pass is triggered. For now, agents receive peer_context=[] and
#   the system works exactly as before — this is additive, not breaking.
# =============================================================================

@dataclass(frozen=True)
class PeerFindingSummary:
    """
    Compact cross-agent context: summary of what another agent found.

    Passed via AgentTask.peer_context so agents can see whether a file
    was already flagged by another domain expert.

    frozen=True: immutable — these summaries are read-only to the receiving agent.
    """

    # Which agent produced these findings.
    agent_type: str            # "security", "quality", "test", "docs"

    # How many findings that agent produced.
    finding_count: int

    # The most serious finding that agent produced.
    # Allows the receiving agent to know the current threat level.
    highest_severity: str      # "critical", "high", "medium", "low", or "none"

    # File paths that agent flagged (deduplicated).
    # Allows the receiving agent to know which files are already under scrutiny.
    flagged_files: tuple[str, ...]   # tuple (not list) because frozen dataclass


# =============================================================================
# AgentTask (INPUT CONTRACT)
#
# The typed input every agent receives. Replaces the 5 positional string
# arguments that used to be passed directly to agent.analyze().
#
# WHY A DATACLASS AND NOT A PYDANTIC MODEL?
#   AgentTask never leaves the process. It is created inside _call_agent_real()
#   in nodes.py and consumed immediately by agent.analyze(). It does not need:
#     - JSON serialization (never hits an API or DB)
#     - Field validators (caller is trusted internal code)
#     - OpenAPI schema generation
#   dataclass(frozen=True) gives us: immutability, repr, eq, type hints.
#   Lightweight, no Pydantic overhead.
#
# WHY frozen=True?
#   Agents must not mutate their input. If an agent could mutate AgentTask,
#   it could affect other agents (they share the same state dict via LangGraph).
#   frozen=True makes mutation a TypeError at runtime, not a silent bug.
# =============================================================================

@dataclass(frozen=True)
class AgentTask:
    """
    The typed input contract for every specialist agent.

    Created by _call_agent_real() in nodes.py from PRReviewState fields.
    Consumed by BaseAgent.analyze().

    All fields documented with: where they come from, how agents use them.
    """

    # -------------------------------------------------------------------------
    # Primary input: the code diff
    # -------------------------------------------------------------------------

    # The raw git diff for this PR (already truncated to context budget in BaseAgent).
    # Source: PRReviewState["pr_diff"]
    # Used by: all agents (this is their primary input)
    diff: str

    # -------------------------------------------------------------------------
    # PR metadata: context about what the PR is trying to do
    # -------------------------------------------------------------------------

    # Human-readable title of the PR.
    # Source: PRReviewState["pr_title"]
    # Used by: docs_agent (checks if code matches stated intent),
    #          quality_agent (checks if naming follows PR's stated pattern)
    pr_title: str

    # The PR body/description (may be empty string if developer skipped it).
    # Source: PRReviewState["pr_body"]
    # Used by: docs_agent (checks if description matches changes),
    #          test_agent (checks if PR description mentions test plan)
    pr_description: str

    # "owner/repo" format. e.g. "acme-corp/payment-service"
    # Source: PRReviewState["repo_full_name"]
    # Used by: all agents (for contextualizing findings — e.g. "in repo X")
    repo_name: str

    # -------------------------------------------------------------------------
    # Changed files: which files this PR touches
    # -------------------------------------------------------------------------

    # List of file paths changed in this PR. e.g. ["src/auth.py", "tests/test_auth.py"]
    # Source: PRReviewState["changed_files"]
    # Used by: test_agent (checks for missing test files),
    #          security_agent (flags if security-sensitive files changed)
    # Stored as tuple (immutable) because this is a frozen dataclass.
    changed_files: tuple[str, ...] = field(default_factory=tuple)

    # -------------------------------------------------------------------------
    # RAG context: prior code knowledge
    # -------------------------------------------------------------------------

    # Formatted snippets of similar code seen in previous PRs (from Qdrant).
    # Source: PRReviewState["retrieved_context"]
    # Used by: all agents (optional — agents run correctly when this is "")
    # CONSTRAINT: agents must NEVER fail or block when retrieved_context is "".
    #   (WIKI: RAG-Architecture.md — "RAG is enhancement, never foundation.")
    retrieved_context: str = ""

    # -------------------------------------------------------------------------
    # Peer context: other agents' findings (inter-agent communication)
    # -------------------------------------------------------------------------

    # Compact summaries of what other agents found (may be empty list).
    # Source: built from agent_results already in state (Phase 8 sequential pass).
    # In the parallel fan-out (Phase 4), this is always [] because agents start
    # simultaneously — there are no prior results to share.
    # Used by: agents to avoid double-flagging files already caught by peers.
    # Stored as tuple (immutable) — each element is a PeerFindingSummary.
    peer_context: tuple["PeerFindingSummary", ...] = field(default_factory=tuple)

    # -------------------------------------------------------------------------
    # Telemetry / cost attribution (Phase 16)
    # -------------------------------------------------------------------------

    # Workflow ID for cost attribution and tracing.
    # Source: PRReviewState["workflow_id"]
    # Used by: BaseAgent.analyze() to set the workflow context so that LLM
    #          calls made inside the agent persist with the correct
    #          (workflow_id, agent_type) attribution in LLMCallLog.
    # Optional + defaulted so existing AgentTask construction sites that don't
    # pass it keep working — the LLM call simply records workflow_id=NULL,
    # which the economics summary endpoint already handles.
    workflow_id: str | None = None


# =============================================================================
# VerdictRecord
#
# One row in the verdict breakdown table — what one agent concluded.
# =============================================================================

@dataclass
class VerdictRecord:
    """
    One agent's entry in the VerdictBreakdown audit log.

    Produced by aggregate_results() for EVERY agent, whether it succeeded or not.
    Stored in PRReviewState["verdict_breakdown"] for Phase 17 trace viewer.

    WIKI: Confidence-Weighted-Voting.md
      "When agents disagree, you need explicit rules. Don't hide conflicts;
       expose them in audit logs."
    """

    # Which agent this record is for. e.g. "security", "quality"
    agent_type: str

    # Whether this agent completed successfully.
    # If False, verdict is AgentVerdict.APPROVE (conservative default — we only
    # block when we have positive evidence, not when we have no evidence).
    succeeded: bool

    # This agent's verdict.
    # APPROVE / REQUEST_CHANGES / CRITICAL_BLOCK (or APPROVE if agent failed)
    verdict: AgentVerdict

    # This agent's overall confidence score (0.0 - 1.0).
    # 0.0 if agent failed.
    confidence: float

    # How many findings this agent produced.
    # 0 if agent failed.
    finding_count: int

    # If the agent failed (succeeded=False), the error message.
    # Empty string when succeeded=True.
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """
        Converts to a plain dict for storage in LangGraph state / Postgres.
        Uses .value for the AgentVerdict enum so it serializes to a plain string.
        """
        return {
            "agent_type":    self.agent_type,
            "succeeded":     self.succeeded,
            "verdict":       self.verdict.value,   # "approve" not AgentVerdict.APPROVE
            "confidence":    self.confidence,
            "finding_count": self.finding_count,
            "error_message": self.error_message,
        }


# =============================================================================
# AggregationResult
#
# The typed output of the aggregate_results node.
# Encapsulates the final verdict PLUS the full audit trail.
#
# WHY A DATACLASS AND NOT JUST UPDATING STATE DICTS DIRECTLY?
#   aggregate_results is a pure function (no I/O) — it takes agent results and
#   produces a final verdict. Wrapping its output in a typed dataclass:
#     1. Makes the function testable in isolation (return value is inspectable)
#     2. Documents exactly what the node produces (no implicit state mutations)
#     3. The node unpacks this into the LangGraph state dict at the end
#
# This is the Humble Object pattern (Clean-Architecture wiki):
#   "Separate the logic (testable) from the I/O (state update side effect)."
#   The logic lives in _compute_aggregation() (pure function, returns AggregationResult).
#   The node aggregate_results() calls it and unpacks the result into state.
# =============================================================================

@dataclass
class AggregationResult:
    """
    The complete output of the verdict aggregation step.

    Produced by _compute_aggregation() in nodes.py.
    Unpacked into PRReviewState by aggregate_results().

    Separating the pure computation from the state mutation makes both
    independently testable (Humble Object pattern, Clean-Architecture wiki).
    """

    # The final system-level verdict.
    # APPROVE / REQUEST_CHANGES / NEEDS_HUMAN_REVIEW
    # (This is ReviewVerdict, not AgentVerdict — the system verdict, not a per-agent verdict.)
    verdict: Any                   # ReviewVerdict enum — imported in nodes.py to avoid circular

    # Per-agent breakdown for the audit log.
    # One VerdictRecord per agent (4 records for a normal run).
    verdict_breakdown: list[VerdictRecord]

    # True if agents disagree (some APPROVE, some REQUEST_CHANGES or CRITICAL_BLOCK).
    # Surfaced in the Phase 17 trace viewer as a "conflict detected" badge.
    # Does NOT automatically trigger HITL — disagreement is normal and expected.
    # The arbitration rules (Safety-Threshold etc.) decide the final verdict.
    conflict_detected: bool

    # True if < 4 agents returned results (some timed out or failed).
    # Stored so Phase 17 can show "partial review" warnings.
    is_partial: bool

    # Human-readable reason if NEEDS_HUMAN_REVIEW was triggered.
    # Empty string for APPROVE or REQUEST_CHANGES.
    human_review_reason: str

    # The combined confidence score (weighted average across successful agents).
    overall_confidence: float
