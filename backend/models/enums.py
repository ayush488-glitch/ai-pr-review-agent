# backend/models/enums.py
#
# All shared enums for the PR Review Agent.
#
# WHY ENUMS?
# Without enums, different modules write status values as raw strings:
#   module A writes:  "in_progress"
#   module B checks:  "IN_PROGRESS"
#   module C checks:  "inprogress"
# All three mean the same thing but none of them match. Silent bugs.
#
# With enums, every module imports ReviewStatus and uses ReviewStatus.IN_PROGRESS.
# The value is defined exactly once. Typos become ImportErrors, not silent bugs.
#
# RULE: If a field can only take a fixed set of values, it is an enum.
#       No raw strings for status, severity, category, or role.

from enum import Enum


# -----------------------------------------------------------------------------
# Review Workflow Status
# This is the state machine defined in Phase 3.
# Every PR review job moves through these states in order (happy path).
# -----------------------------------------------------------------------------

class ReviewStatus(str, Enum):
    """
    The state of a PR review job.

    Inherits from str so these values serialize cleanly to JSON as strings.
    e.g. ReviewStatus.QUEUED serializes as "queued" not as an object.

    Happy path: RECEIVED -> QUEUED -> IN_PROGRESS -> AGENTS_RUNNING
                -> AGGREGATING -> POSTING -> COMPLETED

    Error paths: any state can transition to FAILED or RETRYING
    """

    # Webhook arrived and passed validation. Job not yet in queue.
    RECEIVED = "received"

    # Job is sitting in Redis queue. Waiting for a worker to pick it up.
    QUEUED = "queued"

    # Orchestrator picked up the job. Building PR context and codebase RAG.
    IN_PROGRESS = "in_progress"

    # All 4 sub-agents are running in parallel.
    AGENTS_RUNNING = "agents_running"

    # Orchestrator is combining results from all 4 agents.
    AGGREGATING = "aggregating"

    # Writing review comments back to the GitHub PR.
    POSTING = "posting"

    # All comments posted. Review is done.
    COMPLETED = "completed"

    # A transient error occurred. Will be retried automatically.
    RETRYING = "retrying"

    # An unrecoverable error occurred. Needs human attention.
    FAILED = "failed"


# -----------------------------------------------------------------------------
# Finding Severity
# How serious is the issue an agent found?
# -----------------------------------------------------------------------------

class FindingSeverity(str, Enum):
    """
    How serious a finding is. Used to decide:
    - Whether to block the PR
    - Whether to page a human immediately
    - How prominently to display it in the dashboard
    """

    # Must fix before merge. Pings a human immediately (HITL touchpoint 3).
    # Example: hardcoded AWS credentials, SQL injection vulnerability.
    CRITICAL = "critical"

    # Should fix before merge. High confidence auto-posts.
    # Example: missing input validation, incorrect null check.
    HIGH = "high"

    # Nice to fix. Posts automatically if confidence is above threshold.
    # Example: missing error handling on a non-critical path.
    MEDIUM = "medium"

    # Informational. Always auto-posts, never blocks.
    # Example: variable could be more descriptive, minor style issue.
    LOW = "low"


# -----------------------------------------------------------------------------
# Finding Category
# Which agent produced this finding?
# -----------------------------------------------------------------------------

class FindingCategory(str, Enum):
    """
    What domain this finding belongs to.
    Maps directly to which sub-agent produced it.
    """
    SECURITY = "security"       # SecurityAgent - vulnerabilities, secrets, injection
    QUALITY = "quality"         # QualityAgent - correctness, logic, code smells
    TEST_COVERAGE = "test"      # TestAgent - missing tests, uncovered edge cases
    DOCUMENTATION = "docs"      # DocsAgent - missing or outdated docs


# -----------------------------------------------------------------------------
# Review Verdict
# The overall conclusion of the review.
# -----------------------------------------------------------------------------

class ReviewVerdict(str, Enum):
    """
    The overall verdict the orchestrator reaches after combining all agent findings.

    APPROVE:              No significant issues. Agent approves the PR.
    REQUEST_CHANGES:      Significant issues found. Agent requests changes.
    NEEDS_HUMAN_REVIEW:   Agent is not confident enough to decide.
                          Goes to HITL approval queue (touchpoint 1).
    """
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


# -----------------------------------------------------------------------------
# Agent Types
# Which specialist agent produced a finding.
# Also used by the model router to look up the correct model config.
# -----------------------------------------------------------------------------

class AgentType(str, Enum):
    """
    Identifies which specialist sub-agent produced a finding or result.

    SECURITY:  Looks for OWASP Top 10 vulnerabilities, injection flaws, secrets.
               Uses claude-3-5-sonnet (strong reasoning).
    QUALITY:   Looks for SOLID violations, complexity, anti-patterns.
               Uses gpt-4o-mini (fast, cheap).
    TEST:      Looks for test coverage gaps, assertion-free tests.
               Uses gpt-4o-mini.
    DOCS:      Looks for missing docstrings, type hints, README gaps.
               Uses gpt-4o-mini (cheapest task).
    """
    SECURITY = "security"
    QUALITY  = "quality"
    TEST     = "test"
    DOCS     = "docs"


# -----------------------------------------------------------------------------
# PR Event Actions (from GitHub webhook payload)
# Which action on the PR triggered the webhook.
# -----------------------------------------------------------------------------

class PREventAction(str, Enum):
    """
    The 'action' field from a GitHub pull_request webhook payload.
    We only process OPENED, REOPENED, and SYNCHRONIZE (new commits pushed).
    The webhook receiver filters out all other actions early.
    """
    OPENED      = "opened"
    REOPENED    = "reopened"
    SYNCHRONIZE = "synchronize"   # new commits pushed to an open PR
    CLOSED      = "closed"
    EDITED      = "edited"
    LABELED     = "labeled"
    UNLABELED   = "unlabeled"
    ASSIGNED    = "assigned"
    UNASSIGNED  = "unassigned"


# -----------------------------------------------------------------------------
# User Roles (RBAC)
# What a user is allowed to do in the system.
# -----------------------------------------------------------------------------

class UserRole(str, Enum):
    """
    Roles used for Role-Based Access Control (RBAC).
    Defined here, enforced in backend/auth.

    DEVELOPER:  Can view reviews, dispute findings, see the dashboard.
    REVIEWER:   All of DEVELOPER + can approve/reject items in the HITL queue.
    ADMIN:      All of REVIEWER + can configure the agent, manage repos, view costs.
    OVERRIDE:   Special role. Can force-approve a PR the agent blocked.
                Override is always logged in the audit trail with a reason.
    """
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    ADMIN = "admin"
    OVERRIDE = "override"


# -----------------------------------------------------------------------------
# GitHub Webhook Event Types
# Only the ones we care about.
# -----------------------------------------------------------------------------

class WebhookEventType(str, Enum):
    """
    GitHub webhook event types we handle.
    We ignore all other event types silently.
    """
    PULL_REQUEST = "pull_request"


class PullRequestAction(str, Enum):
    """
    Actions on a pull_request event that trigger a review.
    We ignore: closed, labeled, assigned, etc.
    """
    OPENED = "opened"           # Brand new PR
    SYNCHRONIZE = "synchronize" # New commit pushed to existing PR
    REOPENED = "reopened"       # Previously closed PR was reopened
