# backend/models/review.py
#
# Core Pydantic models for the PR review workflow.
#
# WHAT IS PYDANTIC?
# Pydantic is a data validation library. You define a class with typed fields.
# When you create an instance, Pydantic validates every field automatically.
# If a field is wrong type or missing, you get a clear error immediately.
# This is how we avoid silent data corruption as data flows between modules.
#
# RULE: No business logic in models. Models are data shapes only.
#       A model never calls an API, never queries a database, never makes decisions.
#       It only holds and validates data.
#
# These models represent the data at different stages of a PR review:
#   1. A Finding     = one issue an agent discovered
#   2. An AgentResult = everything one specialist agent produced
#   3. A PRReview    = the complete review of one PR (combines all agent results)

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from backend.models.enums import (
    FindingCategory,
    FindingSeverity,
    ReviewStatus,
    ReviewVerdict,
)


# -----------------------------------------------------------------------------
# Finding
# The atomic unit of a PR review. One issue. One agent. One location in code.
# -----------------------------------------------------------------------------

class Finding(BaseModel):
    """
    A single issue found by a specialist agent.

    Example:
        Finding(
            id=uuid4(),
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.CRITICAL,
            file_path="src/auth/login.py",
            line_start=42,
            line_end=44,
            title="SQL query built with string concatenation",
            description="User input is concatenated directly into the SQL query...",
            suggestion="Use parameterized queries: cursor.execute(sql, (user_id,))",
            confidence=0.95,
        )
    """

    # Unique ID for this finding.
    # uuid4() generates a random UUID. We call it as a default_factory so each
    # Finding gets its own UUID, not the same one shared across all instances.
    id: UUID = Field(default_factory=uuid4)

    # Which agent produced this finding
    category: FindingCategory

    # How serious is this
    severity: FindingSeverity

    # Path to the file where the issue is, relative to repo root
    # e.g. "src/auth/login.py"
    file_path: str

    # Line numbers where the issue is (both inclusive)
    # These map to GitHub PR review comment line positions
    line_start: int
    line_end: int

    # Short title - used as the GitHub comment header
    # Keep under 80 characters
    title: str

    # Full explanation of the issue. Written for the developer reading it.
    # Should explain: what the problem is, why it matters, what could go wrong.
    description: str

    # Concrete fix suggestion.
    # If possible, show the corrected code snippet.
    suggestion: str

    # How confident the agent is in this finding. Value between 0.0 and 1.0.
    # Below CONFIDENCE_THRESHOLD in settings -> goes to HITL approval queue.
    # At or above threshold -> auto-posts to GitHub.
    confidence: float = Field(ge=0.0, le=1.0)

    # Whether a human has reviewed this finding (in the HITL queue)
    # None = not yet reviewed by a human
    # True = human approved posting this
    # False = human rejected this (will not be posted)
    human_approved: bool | None = None

    # If the developer disputed this finding after it was posted
    disputed: bool = False

    # Timestamp when this finding was created
    created_at: datetime = Field(default_factory=datetime.utcnow)


# -----------------------------------------------------------------------------
# AgentResult
# Everything one specialist agent produced for a PR.
# -----------------------------------------------------------------------------

class AgentResult(BaseModel):
    """
    The complete output of one specialist sub-agent for a PR review.

    Each agent returns one AgentResult. The orchestrator collects all 4
    and combines them into a PRReview.
    """

    # Which agent produced this
    agent_name: str  # e.g. "security_agent", "quality_agent"

    # Which category of findings this agent looked for
    category: FindingCategory

    # The list of findings this agent discovered.
    # Empty list is valid - it means the agent found no issues in its domain.
    findings: list[Finding] = Field(default_factory=list)

    # How long the agent took to run, in seconds.
    # Used for cost tracking and performance monitoring.
    duration_seconds: float

    # Total LLM tokens used by this agent for this PR.
    # Used for cost attribution (Phase 16).
    tokens_used: int = 0

    # Whether this agent completed successfully.
    # False means it errored out - orchestrator handles partial results.
    success: bool = True

    # Error message if success is False
    error_message: str | None = None


# -----------------------------------------------------------------------------
# PRReview
# The complete review of one PR. The top-level object.
# -----------------------------------------------------------------------------

class PRReview(BaseModel):
    """
    The complete review of one Pull Request.

    This is the top-level object that flows through the entire system:
    - Created by the webhook receiver when a PR event arrives
    - Updated by the orchestrator as the review progresses
    - Stored in Postgres for history and learning
    - Read by the dashboard to show review status
    """

    # Unique ID for this review. Used as the workflow_id in the engine.
    id: UUID = Field(default_factory=uuid4)

    # GitHub repository full name. Format: "owner/repo"
    # e.g. "ayush488-glitch/my-project"
    repo_full_name: str

    # PR number on GitHub
    pr_number: int

    # PR title from GitHub
    pr_title: str

    # GitHub username of whoever opened the PR
    pr_author: str

    # The git commit SHA at the HEAD of the PR branch.
    # This is part of idempotency: same PR + same commit = same review.
    head_commit_sha: str

    # URL to fetch the PR diff from GitHub API
    diff_url: str

    # Current state of this review in the workflow state machine
    status: ReviewStatus = ReviewStatus.RECEIVED

    # All findings across all 4 agents (populated after AGGREGATING state)
    findings: list[Finding] = Field(default_factory=list)

    # Individual results from each agent (populated after AGENTS_RUNNING state)
    agent_results: list[AgentResult] = Field(default_factory=list)

    # The overall verdict (populated after AGGREGATING state)
    verdict: ReviewVerdict | None = None

    # Total tokens used across all agents for this review
    # Sum of AgentResult.tokens_used for all agents
    total_tokens_used: int = 0

    # Estimated cost of this review in USD (calculated in Phase 16)
    estimated_cost_usd: float = 0.0

    # When the review was created (webhook received)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # When the review was last updated
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # When the review completed (status == COMPLETED or FAILED)
    completed_at: datetime | None = None
