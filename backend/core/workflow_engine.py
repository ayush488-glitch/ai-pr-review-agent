# backend/core/workflow_engine.py
#
# Abstract interface for the workflow orchestration engine.
#
# WHY AN INTERFACE?
# ADR-001 says we use LangGraph now but want to be able to swap to Temporal later.
# If every module imports directly from langgraph, swapping engines means
# touching every module. That is expensive and risky.
#
# Instead: every module imports WorkflowEngine from here.
# The LangGraph implementation lives in backend/orchestrator/langgraph_engine.py
# It implements this interface.
# If we swap to Temporal: write a new class that implements WorkflowEngine.
# Nothing else in the codebase changes.
#
# This is the Dependency Inversion Principle:
#   High-level modules (orchestrator, webhook_receiver) depend on this abstraction.
#   Low-level modules (langgraph_engine) implement this abstraction.
#   Neither depends on each other directly.
#
# DESIGN BY CONTRACT (from pragmatic-programmer wiki):
#   The old return type was dict[str, Any] — that is not a contract, it is
#   "returns anything". A caller had no idea what keys exist in the dict.
#   WorkflowResult below is the real contract:
#     Postcondition: "I promise to return an object with EXACTLY these fields."
#   Callers get type safety. Mypy can verify it. Tests can assert on real fields.

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.models.enums import FindingCategory, FindingSeverity, ReviewStatus, ReviewVerdict


# -----------------------------------------------------------------------------
# WorkflowResult - the typed return contract for WorkflowEngine
#
# Every method on WorkflowEngine returns this.
# No more dict[str, Any] — the contract is explicit.
# -----------------------------------------------------------------------------

class AgentFindingSummary(BaseModel):
    """
    A single finding from one specialist agent, included in the workflow result.

    This is a lightweight summary — the full Finding model with file paths,
    line numbers, and suggested fixes lives in backend/models/review.py.
    This summary is what the workflow engine cares about at the orchestration level:
    severity and category are enough for the orchestrator to make routing decisions
    (e.g. CRITICAL -> ping human immediately).
    """
    agent_type: str
    # Which severity bucket this falls in (CRITICAL, HIGH, MEDIUM, LOW)
    severity: FindingSeverity
    # Which domain produced it (SECURITY, QUALITY, TEST_COVERAGE, DOCUMENTATION)
    category: FindingCategory
    # One sentence. The full explanation is in the Finding model.
    summary: str
    # How confident the agent is in this finding (0.0 - 1.0)
    # Below settings.confidence_threshold -> goes to HITL queue
    confidence: float = Field(ge=0.0, le=1.0)


class WorkflowResult(BaseModel):
    """
    The typed return value of WorkflowEngine.run() and WorkflowEngine.resume().

    This IS the Design by Contract postcondition for the WorkflowEngine interface.
    Any implementation of WorkflowEngine MUST return an object of this shape.

    Why Pydantic here instead of a dataclass?
    - Pydantic validates field types at construction time (catches agent bugs early)
    - Serializes to JSON automatically (needed when we store results in Postgres)
    - Works with FastAPI response models directly
    """

    # Unique identifier for this workflow run.
    # Format: "{repo_full_name}:{pr_number}:{head_commit_sha}"
    # e.g. "org/repo:42:a3f8c1d"
    workflow_id: str

    # Final state of the workflow after this run completes.
    # Will be COMPLETED on success, FAILED on unrecoverable error.
    status: ReviewStatus

    # The overall verdict: APPROVE, REQUEST_CHANGES, or NEEDS_HUMAN_REVIEW
    # None if the workflow did not reach the aggregation step (e.g. it failed early)
    verdict: ReviewVerdict | None = None

    # Findings from all agents that ran.
    # Empty list if no agents ran (e.g. workflow failed before agents started).
    findings: list[AgentFindingSummary] = Field(default_factory=list)

    # How many agents ran successfully (out of 4 total).
    agents_completed: int = 0

    # How many agents failed (timed out, LLM error, etc.)
    agents_failed: int = 0

    # When the workflow started running (set by the engine at run() time)
    started_at: datetime | None = None

    # When the workflow finished (set by the engine when status becomes terminal)
    completed_at: datetime | None = None

    # If status is FAILED, the human-readable reason why.
    # Empty string if the workflow succeeded.
    error_message: str = ""

    # Arbitrary metadata the engine wants to store.
    # Use sparingly — if you find yourself using this often, add a typed field instead.
    # Good use: LangGraph thread_id for checkpoint resumption.
    # Bad use: dumping the entire agent output here.
    metadata: dict[str, Any] = Field(default_factory=dict)


# -----------------------------------------------------------------------------
# WorkflowEngine - the abstract interface
# -----------------------------------------------------------------------------

class WorkflowEngine(ABC):
    """
    Abstract base class for workflow orchestration engines.

    A workflow in our system represents one PR review job.
    It has a unique ID, an input (the webhook event), and moves through states
    defined in ReviewStatus.

    CONTRACT (Design by Contract):
      Precondition for run():
        - workflow_id must be non-empty string
        - input_data must contain at minimum: repo_full_name, pr_number, head_commit_sha
      Postcondition for run():
        - Returns WorkflowResult with status COMPLETED or FAILED
        - If COMPLETED: verdict is non-None, findings is populated
        - If FAILED: error_message is non-empty
      Invariant:
        - workflow_id never changes once assigned
        - status only moves forward in the state machine (never backward)

    Any class that inherits from WorkflowEngine must implement all 3 methods.
    If it does not, Python raises TypeError at instantiation time.
    """

    @abstractmethod
    async def run(
        self,
        workflow_id: str,
        input_data: dict[str, Any],
    ) -> WorkflowResult:
        """
        Start a new workflow for a PR review job.

        Args:
            workflow_id:
                Unique identifier for this review job.
                Format: "{repo_full_name}:{pr_number}:{head_commit_sha}"
                This makes idempotency checks easy — same PR + same commit
                = same workflow_id = we already ran this.

            input_data:
                The parsed webhook event data as a plain dict.
                Required keys: repo_full_name, pr_number, head_commit_sha,
                               pr_title, pr_body, diff_url, author_login

        Returns:
            WorkflowResult with status=COMPLETED and verdict set, or
            WorkflowResult with status=FAILED and error_message set.

        Raises:
            WorkflowError: if the workflow fails in a way that cannot be
                           represented as a WorkflowResult (e.g. engine crash).
            WorkflowTimeoutError: if the workflow exceeds max allowed time.
        """
        ...

    @abstractmethod
    async def resume(
        self,
        workflow_id: str,
    ) -> WorkflowResult:
        """
        Resume a previously checkpointed workflow from where it left off.

        This is how we handle server crashes mid-review.
        If the server restarts while a review is running, we call resume()
        on any workflows that were IN_PROGRESS at shutdown time.
        The engine loads the last checkpoint from Redis and continues from there.

        Args:
            workflow_id:
                The ID of the workflow to resume.
                Must exist in the checkpoint store (Redis).

        Returns:
            WorkflowResult — same shape as run().

        Raises:
            WorkflowError: if the checkpoint cannot be found or workflow fails.
        """
        ...

    @abstractmethod
    async def get_state(
        self,
        workflow_id: str,
    ) -> WorkflowResult | None:
        """
        Read the current state of a workflow without running or resuming it.

        Used by:
        - The API endpoint that the dashboard polls for review status
        - The job queue to check if a workflow is already running (idempotency)
        - The HITL system to read what findings are pending approval

        Args:
            workflow_id: The ID of the workflow to inspect.

        Returns:
            WorkflowResult representing the current state.
            None if no workflow with this ID exists.
        """
        ...
