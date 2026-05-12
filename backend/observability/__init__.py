"""Phase 10: Observability & Tracing.

Public surface for the backend.observability package.
Import everything from here -- internal module structure may change.

    from backend.observability import (
        TraceSpan, TraceContext, traced, async_traced, get_current_trace,
        StructuredLogger, get_logger,
        log_llm_call, log_tool_call, log_agent_verdict, log_review_verdict,
        ReviewEvent,
        AlertLevel, AlertRule, AlertManager, MetricSnapshot, FiredAlert,
        AuditLogger,
    )
"""

from __future__ import annotations

from backend.observability.alerting import (
    AlertLevel,
    AlertManager,
    AlertRule,
    FiredAlert,
    MetricSnapshot,
    DEFAULT_ALERT_RULES,
)
from backend.observability.audit import AuditLogger
from backend.observability.events import ReviewEvent
from backend.observability.logging import (
    StructuredLogger,
    get_logger,
    log_agent_verdict,
    log_llm_call,
    log_review_verdict,
    log_tool_call,
)
from backend.observability.tracing import (
    TraceContext,
    TraceSpan,
    async_traced,
    get_current_trace,
    traced,
)

__all__ = [
    # Tracing
    "TraceSpan",
    "TraceContext",
    "traced",
    "async_traced",
    "get_current_trace",
    # Logging
    "StructuredLogger",
    "get_logger",
    "log_llm_call",
    "log_tool_call",
    "log_agent_verdict",
    "log_review_verdict",
    # Events
    "ReviewEvent",
    # Alerting
    "AlertLevel",
    "AlertRule",
    "AlertManager",
    "MetricSnapshot",
    "FiredAlert",
    "DEFAULT_ALERT_RULES",
    # Audit
    "AuditLogger",
]
