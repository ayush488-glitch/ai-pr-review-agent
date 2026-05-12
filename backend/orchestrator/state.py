# backend/orchestrator/state.py
#
# The LangGraph workflow state — the shared data structure that travels
# through every node in the graph.
#
# MENTAL MODEL:
# Think of the state like a whiteboard in a review meeting room.
# Every participant (node) can read from the whiteboard and write to it.
# The orchestrator sets up the whiteboard at the start.
# Each specialist agent reads what they need, writes their findings.
# The aggregator reads all findings and writes the final verdict.
# The poster reads the verdict and findings to format GitHub comments.
#
# WHY TypedDict AND NOT A PYDANTIC MODEL?
# LangGraph requires TypedDict for state — it's the framework's contract.
# LangGraph reads field annotations to know what to merge when nodes return
# partial updates. With Pydantic, it cannot do that merge automatically.
# We use Pydantic for data that leaves the system (API responses, DB records).
# We use TypedDict for data that stays inside the graph.
#
# HOW STATE UPDATES WORK IN LANGGRAPH:
# Each node returns a dict with ONLY the fields it changed.
# LangGraph merges that partial dict into the full state.
# Example: fan_out_agents node only returns {"agent_results": [...]}
# LangGraph keeps all other fields unchanged.
# You never need to copy the entire state forward — just return what changed.
#
# WIKI PRINCIPLE (Orchestrator-Worker-Architecture.md):
# "Each worker needs enough context to solve its problem, but not so much
# that you blow token budgets."
# -> State is the full picture. Each node receives the full state but
#    is responsible for only reading the fields it needs.
# -> We document per field: which nodes READ it, which nodes WRITE it.

from typing import Any
from typing_extensions import TypedDict

from backend.core.workflow_engine import AgentFindingSummary, WorkflowResult
from backend.models.enums import ReviewStatus, ReviewVerdict


class AgentResultState(TypedDict):
    """
    The result from one specialist agent after it finishes running.

    This lives inside PRReviewState.agent_results — one entry per agent.
    The aggregator node reads all of these to produce the final verdict.

    WHY A SEPARATE TYPED DICT AND NOT JUST AgentFindingSummary?
    AgentFindingSummary is a Pydantic model for data leaving the system.
    AgentResultState is the in-graph representation. It carries extra fields
    the graph needs (like whether the agent succeeded or timed out) that
    don't need to be in the external API response.
    """

    # Which agent produced this result.
    # Values: "security", "quality", "test", "docs"
    # Matches AgentType enum values (defined as strings here to avoid
    # importing enum into the state layer — keeps state.py dependency-free).
    agent_type: str

    # True if the agent completed successfully.
    # False if it timed out or raised an exception.
    # The aggregator uses this to decide if the result is trustworthy.
    success: bool

    # If success is False, what went wrong.
    # Empty string when success is True.
    error_message: str

    # How long this agent took in seconds.
    # Used for cost attribution and latency tracking (Phase 10).
    duration_seconds: float

    # The findings this agent produced.
    # Empty list if the agent failed or found nothing.
    # Each finding is a dict matching AgentFindingSummary fields.
    # (Stored as dicts here so LangGraph can serialize/deserialize cleanly.)
    findings: list[dict[str, Any]]

    # The agent's overall confidence in its findings (0.0 - 1.0).
    # Aggregated confidence across all findings.
    # If below settings.confidence_threshold -> goes to HITL queue.
    confidence: float

    # Phase 8: per-agent verdict derived from findings (AgentVerdict enum value as string).
    # Values: "approve", "request_changes", "critical_block"
    # Stored as string (not enum) to keep state.py dependency-free.
    # The aggregator reads this to apply the Safety-Threshold Rule.
    # Default "approve" if the agent failed — no positive evidence of issues.
    per_verdict: str


class PRReviewState(TypedDict):
    """
    The complete state of a PR review workflow.

    This is what LangGraph carries through the graph from node to node.
    Every field is documented with: who writes it, who reads it.

    FIELD LIFECYCLE:
      build_context node  -> writes: repo_full_name, pr_number, pr_title,
                                     pr_body, pr_diff, author_login,
                                     head_commit_sha, base_branch,
                                     changed_files, idempotency_key
      fan_out_agents node -> reads:  pr_diff, changed_files, pr_title, pr_body
                          -> writes: agent_results
      aggregate_results   -> reads:  agent_results, confidence_threshold
                          -> writes: verdict, final_findings, overall_confidence,
                                     needs_human_review, human_review_reason
      post_review node    -> reads:  verdict, final_findings, repo_full_name,
                                     pr_number, head_commit_sha
                          -> writes: review_posted, github_review_id
    """

    # -------------------------------------------------------------------------
    # Identity fields (set at workflow start, never change)
    # -------------------------------------------------------------------------

    # Unique key for this review run.
    # Format: "{repo_full_name}:{pr_number}:{head_commit_sha}"
    # WRITTEN BY: orchestrator before graph starts
    # READ BY: all nodes (for logging), post_review (for GitHub API call)
    workflow_id: str

    # -------------------------------------------------------------------------
    # PR Context fields (written by build_context, read by agents)
    # -------------------------------------------------------------------------

    # "owner/repo" format. e.g. "acme-corp/payment-service"
    # READ BY: post_review (to call GitHub API on the right repo)
    repo_full_name: str

    # The PR number. e.g. 42
    # READ BY: post_review (to post comments to the right PR)
    pr_number: int

    # The PR title. e.g. "feat: add retry logic to payment processor"
    # READ BY: all agents (gives context about the intent of the change)
    pr_title: str

    # The PR description text. May be empty string if developer didn't write one.
    # READ BY: docs_agent (checks if description matches the code changes)
    pr_body: str

    # The raw unified diff of the PR.
    # This is the main input to all agents — the actual code changes.
    # Format: standard git diff output (--- a/file, +++ b/file, @@ -1,4 +1,6 @@)
    # READ BY: all 4 agents
    pr_diff: str

    # The GitHub login of the PR author. e.g. "jsmith"
    # READ BY: post_review (to @mention author in comments if needed)
    author_login: str

    # The full SHA of the latest commit on this PR.
    # e.g. "a3f8c1d9e2b4f6a8c0d2e4f6a8b0c2d4e6f8a0b2"
    # READ BY: post_review (GitHub API requires commit SHA when posting review)
    head_commit_sha: str

    # The base branch this PR targets. Usually "main" or "develop".
    # READ BY: quality_agent (checks if PR follows branch naming conventions)
    base_branch: str

    # List of file paths that were changed in this PR.
    # e.g. ["src/payments/processor.py", "tests/test_processor.py"]
    # READ BY: test_agent (checks test coverage for changed files)
    #          security_agent (flags if security-sensitive files changed)
    changed_files: list[str]

    # -------------------------------------------------------------------------
    # Agent result fields (written by fan_out_agents, read by aggregate_results)
    # -------------------------------------------------------------------------

    # Results from all 4 specialist agents.
    # WRITTEN BY: fan_out_agents (one entry per agent, after parallel run)
    # READ BY: aggregate_results
    # Each entry matches AgentResultState shape.
    agent_results: list[AgentResultState]

    # -------------------------------------------------------------------------
    # Aggregation fields (written by aggregate_results, read by post_review)
    # -------------------------------------------------------------------------

    # The overall verdict after combining all agent findings.
    # APPROVE, REQUEST_CHANGES, or NEEDS_HUMAN_REVIEW
    # WRITTEN BY: aggregate_results
    # READ BY: post_review
    verdict: ReviewVerdict | None

    # The combined, deduplicated, severity-sorted list of findings to post.
    # WRITTEN BY: aggregate_results
    # READ BY: post_review
    # Each entry is a dict matching AgentFindingSummary fields.
    final_findings: list[dict[str, Any]]

    # The weighted average confidence across all agent findings.
    # 0.0 = no confidence, 1.0 = fully confident.
    # WRITTEN BY: aggregate_results
    # READ BY: post_review (to decide comment wording)
    overall_confidence: float

    # True if this review must go to the human approval queue instead of
    # auto-posting. Set when: CRITICAL findings, low confidence, agent failure.
    # WRITTEN BY: aggregate_results
    # READ BY: post_review
    needs_human_review: bool

    # Human-readable explanation of why HITL is needed.
    # Empty string when needs_human_review is False.
    # WRITTEN BY: aggregate_results
    # READ BY: post_review (to log why it went to the queue)
    human_review_reason: str

    # -------------------------------------------------------------------------
    # Phase 8: Arbitration audit fields (written by aggregate_results)
    # -------------------------------------------------------------------------

    # Per-agent verdict breakdown for audit log / Phase 17 trace viewer.
    # Each entry is a VerdictRecord.to_dict() result:
    #   {"agent_type": str, "succeeded": bool, "verdict": str,
    #    "confidence": float, "finding_count": int, "error_message": str}
    # WRITTEN BY: aggregate_results
    # READ BY: Phase 17 trace viewer, Phase 15 audit log
    # WIKI: Confidence-Weighted-Voting.md — "Hidden-Conflict anti-pattern:
    #   silently resolving agent disagreements. Fix: emit full breakdown."
    verdict_breakdown: list[dict[str, Any]]

    # True if agents disagreed on verdict (some APPROVE, some REQUEST_CHANGES+).
    # Does NOT automatically trigger HITL — disagreement is normal.
    # The Safety-Threshold Rule handles actual escalation decisions.
    # WRITTEN BY: aggregate_results
    # READ BY: Phase 17 trace viewer (shows "conflict detected" badge)
    conflict_detected: bool

    # True if < 4 agents returned results (some timed out or failed).
    # PARTIAL RESULTS DOCTRINE (wiki: Fan-Out-Fan-In.md):
    #   "Better 75% truth than 100% latency."
    #   A partial result is surfaced to the user, not silently dropped.
    # WRITTEN BY: aggregate_results
    partial_review: bool

    # -------------------------------------------------------------------------
    # Post-review fields (written by post_review node)
    # -------------------------------------------------------------------------

    # True once the review has been successfully posted to GitHub.
    # WRITTEN BY: post_review
    review_posted: bool

    # The GitHub review ID returned by the GitHub API after posting.
    # None if review was not posted (went to HITL queue or failed).
    # WRITTEN BY: post_review
    # Used for: dispute resolution (Phase 19 can reference this ID)
    github_review_id: int | None

    # -------------------------------------------------------------------------
    # Workflow metadata (written at various points for observability)
    # -------------------------------------------------------------------------

    # Current status in the state machine.
    # Updated by each node as it starts/finishes.
    # WRITTEN BY: every node
    # READ BY: WorkflowEngine.get_state() to answer "what is this review doing?"
    status: ReviewStatus

    # Confidence threshold from settings at the time this workflow started.
    # Captured here so a settings change mid-run doesn't affect an in-progress review.
    # WRITTEN BY: orchestrator before graph starts
    # READ BY: aggregate_results
    confidence_threshold: float

    # -------------------------------------------------------------------------
    # RAG Context (added Phase 6 — Memory Architecture)
    # -------------------------------------------------------------------------

    # RAG-retrieved prior code context for this PR's diff.
    # Contains formatted snippets of similar code seen in previous PRs.
    # Injected into agent prompts as supplementary context (NOT authoritative).
    #
    # DEFAULT: empty string ("").
    # If context_retriever.retrieve_context_for_diff() returns "" (Qdrant down,
    # no similar code found, embedding failed), this field stays "".
    # Agents check: if retrieved_context: (append to prompt) else: (run without it).
    #
    # WRITTEN BY: build_context node (Phase 6 addition)
    # READ BY: fan_out_agents node -> passed to each agent's analyze() call
    #
    # CRITICAL CONSTRAINT (from RAG-Architecture.md + Production-Hardening.md wiki):
    #   "RAG context is an enhancement, NEVER a hard dependency."
    #   Empty retrieved_context is VALID. The pipeline runs correctly with diff only.
    #   No node should fail or block if this field is "".
    retrieved_context: str
