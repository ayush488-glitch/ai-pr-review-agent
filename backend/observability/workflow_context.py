# backend/observability/workflow_context.py
#
# Phase 16 — Carries the active workflow_id and agent_type across the call
# stack without modifying every function signature.
#
# WHY A CONTEXTVAR (not a thread-local or arg threading)?
#   - asyncio-safe: ContextVar isolates per-task automatically. Two PR reviews
#     running concurrently see distinct values — no cross-contamination.
#   - Zero-touch wiring: tools/llm_client.py reads the current value when it
#     persists an LLMCallLog row, so we don't have to thread a workflow_id
#     argument through llm_client + base_agent + every retry path.
#
# CONTRACT:
#   set_workflow_context(workflow_id="...", agent_type="security")
#   try:
#       await something_that_calls_an_llm()
#   finally:
#       reset_workflow_context(token)

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class WorkflowContext:
    workflow_id: Optional[str]
    agent_type: str    # "security", "quality", "system", etc.


_DEFAULT = WorkflowContext(workflow_id=None, agent_type="system")

_current: ContextVar[WorkflowContext] = ContextVar(
    "ai_pr_review_workflow_context",
    default=_DEFAULT,
)


def get_workflow_context() -> WorkflowContext:
    """Read the current context. Always returns a value (default if unset)."""
    return _current.get()


def set_workflow_context(*, workflow_id: Optional[str], agent_type: str) -> Token:
    """
    Push a new context onto the stack. Returns a token to pass to reset().

    Usage:
        token = set_workflow_context(workflow_id="o/r:1:abc", agent_type="security")
        try:
            ...
        finally:
            reset_workflow_context(token)
    """
    return _current.set(WorkflowContext(workflow_id=workflow_id, agent_type=agent_type))


def reset_workflow_context(token: Token) -> None:
    """Restore the previous context."""
    _current.reset(token)
