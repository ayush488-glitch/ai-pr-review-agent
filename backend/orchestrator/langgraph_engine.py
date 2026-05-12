# backend/orchestrator/langgraph_engine.py
#
# Concrete implementation of the WorkflowEngine interface using LangGraph.
#
# WHAT THIS FILE IS:
# In backend/core/workflow_engine.py we defined the abstract WorkflowEngine.
# That interface says "here is what a workflow engine must be able to do."
# This file is the answer: "here is HOW LangGraph does it."
#
# THE DEPENDENCY INVERSION PRINCIPLE IN ACTION:
# The job queue (Phase 4), webhook router (Phase 3), and HITL system (Phase 19)
# all import WorkflowEngine from backend.core — the abstract interface.
# They do NOT import LangGraphEngine directly.
# This file is only referenced in one place: backend/main.py at startup,
# where we wire the concrete engine into the app.
# If we ever swap to Temporal: write TemporalEngine, change one line in main.py.
# Nothing else changes.
#
# RELATIONSHIP TO graph.py:
# graph.py assembles the nodes into a StateGraph and compiles it.
# This file uses that compiled graph to run reviews.
# graph.py knows about nodes. This file knows about WorkflowEngine contracts.
# They are separate because: graph structure changes (Phase 4-8) vs
# engine interface (stable from Phase 3 onwards).

import logging
from datetime import datetime, timezone
from typing import Any

from backend.config import get_settings
from backend.core.workflow_engine import AgentFindingSummary, WorkflowEngine, WorkflowResult
from backend.models.enums import FindingCategory, FindingSeverity, ReviewStatus, ReviewVerdict
from backend.orchestrator.graph import review_graph
from backend.orchestrator.state import PRReviewState

logger = logging.getLogger(__name__)


class LangGraphEngine(WorkflowEngine):
    """
    WorkflowEngine implementation backed by LangGraph.

    Implements: run(), resume(), get_state()
    All three are defined on the abstract WorkflowEngine base class.
    Python's ABC mechanism guarantees all three are implemented here —
    if any were missing, instantiating this class would raise TypeError.

    THREAD SAFETY:
    This class holds no per-review state. The compiled graph (review_graph)
    is module-level and stateless. Per-review state lives in LangGraph's
    checkpointer (keyed by workflow_id / thread_id).
    Safe to instantiate once and share across concurrent reviews.
    """

    def _build_initial_state(
        self,
        workflow_id: str,
        input_data: dict[str, Any],
    ) -> PRReviewState:
        """
        Constructs the initial PRReviewState from the webhook event input.

        This runs before the graph starts. It populates all the fields that
        the orchestrator knows at the time the webhook was received.
        The build_context node will fill in pr_diff and changed_files
        (the fields that require a GitHub API call).

        DESIGN BY CONTRACT:
          Precondition: input_data contains these keys:
            repo_full_name, pr_number, pr_title, pr_body,
            author_login, head_commit_sha, base_branch
          Postcondition: returns a valid PRReviewState with all required
            fields populated. Agent result fields are empty lists.
        """
        cfg = get_settings()

        return PRReviewState(
            # Identity
            workflow_id=workflow_id,

            # PR context from webhook (known before GitHub API call)
            repo_full_name=input_data["repo_full_name"],
            pr_number=input_data["pr_number"],
            pr_title=input_data.get("pr_title", ""),
            pr_body=input_data.get("pr_body", ""),
            author_login=input_data.get("author_login", ""),
            head_commit_sha=input_data["head_commit_sha"],
            base_branch=input_data.get("base_branch", "main"),

            # These are filled by build_context node (require GitHub API call)
            # If pr_diff was passed in input_data (e.g. demo fixture), use it.
            pr_diff=input_data.get("pr_diff", ""),
            changed_files=[],

            # Agent results (filled by fan_out_agents node)
            agent_results=[],

            # Aggregation results (filled by aggregate_results node)
            verdict=None,
            final_findings=[],
            overall_confidence=0.0,
            needs_human_review=False,
            human_review_reason="",

            # Post-review results (filled by post_review node)
            review_posted=False,
            github_review_id=None,

            # Workflow metadata
            status=ReviewStatus.RECEIVED,

            # Capture threshold at workflow start so a settings change
            # mid-run doesn't affect an in-progress review
            confidence_threshold=cfg.confidence_threshold,
        )

    def _state_to_result(
        self,
        workflow_id: str,
        final_state: dict[str, Any],
        started_at: datetime,
    ) -> WorkflowResult:
        """
        Converts the final LangGraph state dict into a typed WorkflowResult.

        WHY THIS CONVERSION?
        LangGraph returns a plain dict (the TypedDict final state).
        WorkflowResult is our typed contract that the rest of the system expects.
        This method is the boundary: LangGraph internals -> external contract.
        """
        # Convert raw finding dicts back to AgentFindingSummary objects
        findings: list[AgentFindingSummary] = []
        for f in final_state.get("final_findings", []):
            try:
                findings.append(AgentFindingSummary(
                    agent_type=f.get("agent_type", "unknown"),
                    severity=FindingSeverity(f.get("severity", "low")),
                    category=FindingCategory(f.get("category", "quality")),
                    summary=f.get("summary", ""),
                    confidence=float(f.get("confidence", 0.5)),
                ))
            except (ValueError, KeyError) as e:
                logger.warning("Could not parse finding: %s | error: %s", f, str(e))

        # Count agent outcomes
        agent_results = final_state.get("agent_results", [])
        agents_completed = sum(1 for r in agent_results if r.get("success", False))
        agents_failed = len(agent_results) - agents_completed

        # Parse verdict — may be None if workflow failed before aggregation
        raw_verdict = final_state.get("verdict")
        verdict: ReviewVerdict | None = None
        if raw_verdict is not None:
            try:
                verdict = ReviewVerdict(raw_verdict) if isinstance(raw_verdict, str) else raw_verdict
            except ValueError:
                pass

        return WorkflowResult(
            workflow_id=workflow_id,
            status=final_state.get("status", ReviewStatus.COMPLETED),
            verdict=verdict,
            findings=findings,
            agents_completed=agents_completed,
            agents_failed=agents_failed,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            error_message="",
            metadata={
                "overall_confidence": final_state.get("overall_confidence", 0.0),
                "needs_human_review": final_state.get("needs_human_review", False),
                "human_review_reason": final_state.get("human_review_reason", ""),
                "github_review_id": final_state.get("github_review_id"),
            },
        )

    async def run(
        self,
        workflow_id: str,
        input_data: dict[str, Any],
    ) -> WorkflowResult:
        """
        Starts a new PR review workflow and runs it to completion.

        PRECONDITION:
          input_data must contain: repo_full_name, pr_number, head_commit_sha
        POSTCONDITION:
          Returns WorkflowResult with status=COMPLETED or status=FAILED.
          If COMPLETED: verdict is set, findings are populated.
          If FAILED: error_message is set.
        """
        started_at = datetime.now(timezone.utc)
        logger.info("engine.run | workflow_id=%s", workflow_id)

        # Build the initial state with what we know from the webhook event
        initial_state = self._build_initial_state(workflow_id, input_data)

        # LangGraph config: thread_id is how LangGraph keys its checkpoints.
        # Using workflow_id as thread_id means: one checkpoint stream per PR review.
        # resume() can find the right checkpoint by passing the same thread_id.
        config = {"configurable": {"thread_id": workflow_id}}

        try:
            # ainvoke() runs the graph to completion asynchronously.
            # It runs each node in order, checkpointing after each one.
            # Returns the final state dict when the graph reaches END.
            final_state = await review_graph.ainvoke(initial_state, config=config)

            logger.info(
                "engine.run | completed | workflow_id=%s verdict=%s",
                workflow_id,
                final_state.get("verdict"),
            )

            return self._state_to_result(workflow_id, final_state, started_at)

        except Exception as e:
            logger.error(
                "engine.run | failed | workflow_id=%s error=%s",
                workflow_id,
                str(e),
                exc_info=True,
            )
            return WorkflowResult(
                workflow_id=workflow_id,
                status=ReviewStatus.FAILED,
                verdict=None,
                findings=[],
                agents_completed=0,
                agents_failed=4,
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"{type(e).__name__}: {str(e)}",
            )

    async def resume(
        self,
        workflow_id: str,
    ) -> WorkflowResult:
        """
        Resumes a previously checkpointed workflow from its last completed node.

        WHEN THIS IS CALLED:
        1. Server restarts while a review is IN_PROGRESS
        2. On startup, the job queue checks Redis for any unfinished workflows
        3. Those workflows are resumed here instead of restarted from scratch

        HOW LANGGRAPH CHECKPOINTING WORKS:
        After each node completes, LangGraph saves the current state to the
        checkpointer (Redis in production). When we call ainvoke() with the
        same thread_id (=workflow_id), LangGraph detects the existing checkpoint,
        loads the last saved state, and resumes from the next node.
        Nodes that already completed are NOT re-run.

        STUB NOTE:
        Full checkpoint resumption requires the Redis checkpointer to be
        connected (Phase 4 gate task). Currently: without a checkpointer,
        the graph has no checkpoint to resume from — so we re-run from start.
        This will be fixed when we wire in RedisSaver.
        """
        started_at = datetime.now(timezone.utc)
        logger.info("engine.resume | workflow_id=%s", workflow_id)

        config = {"configurable": {"thread_id": workflow_id}}

        try:
            # With a checkpointer: LangGraph finds the last checkpoint for this
            # thread_id and resumes from the next node automatically.
            # Without a checkpointer (current state): runs from the beginning.
            # input=None tells LangGraph to use the checkpointed state as input.
            final_state = await review_graph.ainvoke(None, config=config)

            return self._state_to_result(workflow_id, final_state, started_at)

        except Exception as e:
            logger.error(
                "engine.resume | failed | workflow_id=%s error=%s",
                workflow_id,
                str(e),
                exc_info=True,
            )
            return WorkflowResult(
                workflow_id=workflow_id,
                status=ReviewStatus.FAILED,
                verdict=None,
                findings=[],
                started_at=started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"Resume failed: {type(e).__name__}: {str(e)}",
            )

    async def get_state(
        self,
        workflow_id: str,
    ) -> WorkflowResult | None:
        """
        Reads the current state of a workflow without running it.

        WHEN THIS IS CALLED:
        - Dashboard API polls: "what is the status of review #42?"
        - Job queue idempotency check: "is this PR already being reviewed?"
        - HITL queue: "what findings are waiting for human approval?"

        STUB NOTE:
        With a Redis checkpointer: this reads the latest checkpoint for workflow_id.
        Without one (current state): returns None (no checkpoint to read).
        Fully functional after Phase 4 Redis wiring.
        """
        config = {"configurable": {"thread_id": workflow_id}}

        try:
            state_snapshot = review_graph.get_state(config)
            if state_snapshot is None or state_snapshot.values is None:
                return None

            return self._state_to_result(
                workflow_id,
                state_snapshot.values,
                datetime.now(timezone.utc),
            )

        except Exception as e:
            logger.warning(
                "engine.get_state | error | workflow_id=%s error=%s",
                workflow_id,
                str(e),
            )
            return None
