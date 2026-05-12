# backend/core/exceptions.py
#
# All custom exceptions for the PR Review Agent.
#
# WHY CUSTOM EXCEPTIONS?
# When a module raises a plain Exception("something went wrong"), the caller
# has no idea what went wrong or how to handle it.
# When a module raises WebhookValidationError, the caller knows exactly:
#   1. What category of failure this is
#   2. How to respond (e.g. return 401 to GitHub)
#   3. Whether to retry or not
#
# RULE: Every module in this project raises one of these exceptions.
#       Never raise a raw Exception or ValueError in business logic.


class PRReviewAgentError(Exception):
    """
    Base class for all exceptions in this project.
    Every custom exception below inherits from this.
    This lets callers catch all our exceptions with a single except clause
    if they want to, while still being able to catch specific ones.
    """
    pass


# -----------------------------------------------------------------------------
# Webhook Exceptions
# -----------------------------------------------------------------------------

class WebhookValidationError(PRReviewAgentError):
    """
    Raised when a GitHub webhook fails HMAC signature validation.
    This means the request did not come from GitHub, or the secret is wrong.
    The webhook receiver should return HTTP 401 when this is raised.
    """
    pass


class WebhookParseError(PRReviewAgentError):
    """
    Raised when a valid (signature-verified) webhook payload cannot be parsed
    into our expected model. Usually means GitHub changed their payload format
    or we received an event type we do not support.
    The webhook receiver should return HTTP 400 when this is raised.
    """
    pass


# -----------------------------------------------------------------------------
# Job Queue Exceptions
# -----------------------------------------------------------------------------

class JobEnqueueError(PRReviewAgentError):
    """
    Raised when a job cannot be placed into the Redis queue.
    Usually means Redis is down or unreachable.
    The webhook receiver should return HTTP 503 when this is raised
    so GitHub knows to retry the webhook delivery.
    """
    pass


class DuplicateJobError(PRReviewAgentError):
    """
    Raised when we detect we have already processed this exact webhook event.
    This is the idempotency check - GitHub replays webhooks on timeout.
    Not an error per se - the caller should return HTTP 200 silently.
    """
    pass


class DuplicateWebhookError(PRReviewAgentError):
    """
    Alias for DuplicateJobError, used in Phase 4 job queue layer.
    Raised by enqueue_review_job() when the idempotency key already exists.
    Includes the idempotency_key so the caller can log which event was skipped.
    Not an error — the caller should return HTTP 200 silently.
    """
    def __init__(self, message: str, idempotency_key: str | None = None):
        super().__init__(message)
        self.idempotency_key = idempotency_key


# -----------------------------------------------------------------------------
# Orchestrator Exceptions
# -----------------------------------------------------------------------------

class WorkflowError(PRReviewAgentError):
    """
    Raised when the LangGraph workflow encounters an unrecoverable error.
    Includes the workflow_id so we can look up the failed run in the database.
    """
    def __init__(self, message: str, workflow_id: str | None = None):
        super().__init__(message)
        self.workflow_id = workflow_id


class WorkflowTimeoutError(WorkflowError):
    """
    Raised when the agent workflow exceeds its maximum allowed time.
    This is the circuit breaker for infinite agent loops.
    """
    pass


class WorkflowNotFoundError(WorkflowError):
    """
    Raised when a workflow_id is looked up but no corresponding record exists.
    Can occur if a job is dequeued after its checkpoint expired, or if the
    workflow_id was generated incorrectly.
    """
    pass


# -----------------------------------------------------------------------------
# Agent Exceptions
# -----------------------------------------------------------------------------

class AgentError(PRReviewAgentError):
    """
    Raised when a specialist sub-agent (security, quality, test, docs)
    fails to complete its analysis. Includes the agent name so we know
    which one failed.
    """
    def __init__(self, message: str, agent_name: str | None = None):
        super().__init__(message)
        self.agent_name = agent_name


class AgentOutputValidationError(AgentError):
    """
    Raised when an agent returns output that does not match our Pydantic schema.
    This means the LLM returned something in the wrong format.
    The caller should retry with a stricter output prompt.
    """
    pass


# -----------------------------------------------------------------------------
# GitHub API Exceptions
# -----------------------------------------------------------------------------

class GitHubAPIError(PRReviewAgentError):
    """
    Raised when the GitHub REST API returns an error response.
    Includes the HTTP status code so the caller can decide whether to retry.
    """
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitHubRateLimitError(GitHubAPIError):
    """
    Raised when we hit GitHub's API rate limit (HTTP 429).
    The caller should back off and retry after the reset time.
    """
    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message, status_code=429)
        self.retry_after_seconds = retry_after_seconds


# -----------------------------------------------------------------------------
# Memory / Storage Exceptions
# -----------------------------------------------------------------------------

class MemoryStoreError(PRReviewAgentError):
    """
    Raised when a read or write to any memory store (Redis, Qdrant, Postgres) fails.
    Includes the store name so we know which one is down.
    """
    def __init__(self, message: str, store: str | None = None):
        super().__init__(message)
        self.store = store


# -----------------------------------------------------------------------------
# Configuration Exceptions
# -----------------------------------------------------------------------------

class ConfigurationError(PRReviewAgentError):
    """
    Raised when a required environment variable is missing or has an invalid value.
    This should only happen at startup. If it happens at runtime, something is very wrong.
    """
    pass


# -----------------------------------------------------------------------------
# Prompt Registry Exceptions
# -----------------------------------------------------------------------------

class PromptNotFoundError(PRReviewAgentError):
    """
    Raised by PromptRegistry when a template file is missing or empty.

    WHY NOT SILENTLY FALL BACK?
    A missing prompt is a deployment error — either the templates/ directory
    was not shipped with the code, or a version number was typed incorrectly.
    Silent fallback would hide this and produce inconsistent agent behavior.
    Fail loudly so the deployment error is caught before it affects reviews.

    The agent's inline _system_prompt() method serves as the fallback at the
    higher call site (BaseAgent.analyze()), not here.

    Fields:
        agent_type: e.g. "security", "quality"
        version:    e.g. "v1", "latest"
        message:    Human-readable description of what is missing.
    """
    def __init__(self, agent_type: str, version: str, message: str):
        super().__init__(message)
        self.agent_type = agent_type
        self.version = version
