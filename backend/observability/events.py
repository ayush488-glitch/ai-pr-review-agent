"""Typed event vocabulary for the PR Review Agent observability layer.

WHY A TYPED ENUM?
  Unstructured string event names drift over time -- 'review.started' becomes
  'review_started' becomes 'ReviewStarted' in different parts of the codebase.
  A StrEnum is the single source of truth: every emitted event, every log entry,
  every audit record uses ReviewEvent.X so grep finds everything.

  Borrowed pattern from opensre analytics/events.py (analytics/events.py:8-60),
  which uses the same StrEnum approach for all lifecycle events.

  Wiki: "Emit every log entry as a JSON object with a consistent schema" --
  typed events are the schema anchor that makes the promise enforceable.

NAMING CONVENTION
  Format: <resource>.<action>  (lowercase, dot-separated)
  resource: webhook | review | agent | llm | tool | verdict | hitl | audit
  action:   past-tense verb (received, started, completed, failed, escalated, ...)
"""

from __future__ import annotations

# StrEnum was added in Python 3.11; provide a compat shim for 3.10.
try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


class ReviewEvent(StrEnum):
    # ── Webhook lifecycle ────────────────────────────────────────────────────
    # Fired by the webhook receiver when a GitHub PR event arrives.
    WEBHOOK_RECEIVED = "webhook.received"

    # Fired after HMAC-SHA256 validation passes and the event is deduplicated.
    WEBHOOK_VALIDATED = "webhook.validated"

    # ── Review workflow lifecycle ────────────────────────────────────────────
    # Fired when the ARQ worker picks up a review job and begins the workflow.
    REVIEW_STARTED = "review.started"

    # Fired when all 4 agents have returned and aggregate_results() has run.
    REVIEW_COMPLETED = "review.completed"

    # Fired when the workflow raises an unhandled exception.
    REVIEW_FAILED = "review.failed"

    # ── Per-agent lifecycle ──────────────────────────────────────────────────
    # Fired at the start of each specialist agent's analyze() call.
    AGENT_INVOKED = "agent.invoked"

    # Fired when an agent's analyze() returns successfully with an AgentOutput.
    AGENT_COMPLETED = "agent.completed"

    # Fired when an agent raises an exception (caught by the node, not re-raised).
    AGENT_FAILED = "agent.failed"

    # ── LLM call lifecycle ───────────────────────────────────────────────────
    # Fired on every call to LLMClient.call() -- carries token counts + cost.
    # Wiki: "Attach cost_usd and token counts as span tags on every LLM call."
    LLM_CALLED = "llm.called"

    # Fired when the LLM call fails (timeout, rate-limit, provider error).
    LLM_FAILED = "llm.failed"

    # ── Tool call lifecycle ──────────────────────────────────────────────────
    # Fired on every tool execution through the ToolRegistry.
    TOOL_CALLED = "tool.called"

    # Fired when a tool execution raises an exception.
    TOOL_FAILED = "tool.failed"

    # ── Verdict lifecycle ────────────────────────────────────────────────────
    # Fired when aggregate_results() produces a final ReviewVerdict.
    VERDICT_EMITTED = "verdict.emitted"

    # Fired when the Safety-Threshold Rule triggers HITL escalation
    # (2+ CRITICAL_BLOCK agents -> human review queue).
    HITL_ESCALATED = "hitl.escalated"

    # ── Evaluation lifecycle ─────────────────────────────────────────────────
    # Fired when the regression gate runs (Phase 9 integration).
    EVAL_GATE_RUN = "eval.gate.run"

    # Fired when the regression gate blocks a deployment.
    EVAL_GATE_BLOCKED = "eval.gate.blocked"
