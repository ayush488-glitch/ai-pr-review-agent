# backend/core/__init__.py
#
# The core module exports the foundational abstractions and exceptions.
#
# DEPENDENCY RULE (ADR-002):
#   core depends on NOTHING inside backend/.
#   Everything else is allowed to depend on core.
#   If you find yourself importing from backend.models or backend.config
#   inside backend/core/ — stop. That is a dependency inversion violation.
#
# WHAT THIS MODULE EXPORTS:
#   - WorkflowEngine: the abstract orchestration interface
#   - WorkflowResult: the typed return contract for WorkflowEngine
#   - All custom exceptions

from backend.core.exceptions import (
    AgentError,
    AgentOutputValidationError,
    ConfigurationError,
    DuplicateJobError,
    DuplicateWebhookError,
    GitHubAPIError,
    GitHubRateLimitError,
    JobEnqueueError,
    MemoryStoreError,
    PRReviewAgentError,
    PromptNotFoundError,
    WebhookParseError,
    WebhookValidationError,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowTimeoutError,
)
from backend.core.workflow_engine import WorkflowEngine, WorkflowResult, AgentFindingSummary

__all__ = [
    # Exceptions
    "PRReviewAgentError",
    "WebhookValidationError",
    "WebhookParseError",
    "JobEnqueueError",
    "DuplicateJobError",
    "DuplicateWebhookError",
    "WorkflowError",
    "WorkflowNotFoundError",
    "WorkflowTimeoutError",
    "AgentError",
    "AgentOutputValidationError",
    "GitHubAPIError",
    "GitHubRateLimitError",
    "MemoryStoreError",
    "ConfigurationError",
    "PromptNotFoundError",
    # Orchestration interface
    "WorkflowEngine",
    "WorkflowResult",
    "AgentFindingSummary",
]
