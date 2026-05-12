"""Span-based distributed tracing for the PR Review Agent.

DESIGN OVERVIEW
  Every PR review produces one TraceContext keyed by review_id.
  The context lives in a Python contextvars.ContextVar so it propagates
  through async/await chains without explicit parameter threading.

  Span tree for a typical review:
    SPAN: review_workflow (root)
      SPAN: webhook_validation
      SPAN: agent_fan_out
        SPAN: security_agent.analyze
          SPAN: llm.call           <- tags: model, tokens, cost_usd
          SPAN: tool.check_secrets
        SPAN: quality_agent.analyze
          SPAN: llm.call
        SPAN: test_agent.analyze
          SPAN: llm.call
        SPAN: docs_agent.analyze
          SPAN: llm.call
      SPAN: aggregate_results

WHY contextvars NOT thread-local?
  We use asyncio throughout (FastAPI + ARQ).  threading.local() would share
  state across coroutines on the same thread.  contextvars.ContextVar isolates
  per-coroutine, which is what we want.
  opensre's prototype used a plain dict (_local_storage) -- fine for sync CLI,
  wrong for async HTTP servers.  Wiki: "Build observability first."

OTel INTEGRATION (Phase 13)
  The OTel SDK is imported lazily inside _try_otel_export() so the module
  loads cleanly in tests with no running collector.  When settings.otel_endpoint
  is set, spans are forwarded to a Jaeger/Grafana Tempo OTLP endpoint.
  This matches our Phase 6 pattern: RAG is an enhancement, never a hard dep.

COST ATTRIBUTION
  Wiki: "Attach cost_usd and token counts as span tags on every LLM call" to
  fix the FlatCostVisibility anti-pattern.  Every TraceSpan exposes
  add_cost_tag() as a named helper so callers never forget the tag name.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Generator, Optional

# Module-level ContextVar: holds the active TraceContext for the current coroutine.
# Initialised to None -- code that doesn't care about tracing never touches it.
_current_trace: ContextVar[Optional["TraceContext"]] = ContextVar(
    "_current_trace", default=None
)


@dataclass
class TraceSpan:
    """A single named operation within a request trace.

    Mutable while open; frozen (duration_ms set) once finish() is called.
    Do NOT share spans across coroutines -- each coroutine's traced() call
    creates its own span inside the shared TraceContext.
    """

    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    operation_name: str
    start_time: float  # time.monotonic() for duration; epoch stored separately
    start_epoch: str   # ISO-8601 UTC -- human-readable in JSON exports
    end_time: Optional[float] = field(default=None)
    duration_ms: Optional[float] = field(default=None)
    # tags: structured metadata -- NOT logged during execution, exported at end
    # Wiki: every span should carry cost_usd and token counts as tags.
    tags: dict[str, Any] = field(default_factory=dict)
    # logs: timestamped in-span events (error messages, retries, etc.)
    logs: list[dict[str, Any]] = field(default_factory=list)
    # error: non-None if the span finished due to an exception
    error: Optional[str] = field(default=None)

    def finish(self, *, error: Optional[str] = None) -> None:
        """Mark span complete.  Idempotent -- safe to call multiple times."""
        if self.duration_ms is not None:
            return  # already finished
        self.end_time = time.monotonic()
        self.duration_ms = (self.end_time - self.start_time) * 1000.0
        self.error = error

    def add_tag(self, key: str, value: Any) -> None:
        """Attach structured metadata.  Called after span creation, before finish."""
        self.tags[key] = value

    def add_cost_tag(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        """Named helper so LLM call sites always attach the exact right tag set.

        Wiki: "Attach cost_usd and token counts as span tags on every LLM call
        to fix the FlatCostVisibility anti-pattern."
        """
        self.tags["llm.model"] = model
        self.tags["llm.input_tokens"] = input_tokens
        self.tags["llm.output_tokens"] = output_tokens
        self.tags["llm.total_tokens"] = input_tokens + output_tokens
        self.tags["llm.cost_usd"] = cost_usd

    def log_event(self, message: str, level: str = "info") -> None:
        """Record a timestamped in-span log entry.

        Use for significant mid-span events (retry attempt, cache miss).
        For standalone structured logs use StructuredLogger instead.
        Wiki: "Traces are for the happy path. When things go wrong, you need logs."
        """
        self.logs.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "message": message,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize span to a JSON-safe dict for export/storage."""
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "operation_name": self.operation_name,
            "start_epoch": self.start_epoch,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "tags": self.tags,
            "logs": self.logs,
        }


class TraceContext:
    """Per-request trace -- collects all spans for one PR review.

    Lifecycle:
        ctx = TraceContext.create(review_id)
        # ... workflow runs, spans are created via traced() context manager ...
        ctx.finish()
        export = ctx.to_dict()   # JSON-safe full trace

    Do NOT instantiate directly -- use TraceContext.create().
    """

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.spans: list[TraceSpan] = []
        self._current_span_id: Optional[str] = None

    @classmethod
    def create(cls, review_id: str) -> "TraceContext":
        """Create a new trace and register it in the current contextvars context.

        Uses review_id as the trace_id for human-readable correlation --
        "review_abc123" in the DB maps directly to trace_id "review_abc123".
        """
        ctx = cls(trace_id=review_id)
        _current_trace.set(ctx)
        return ctx

    @classmethod
    def current(cls) -> Optional["TraceContext"]:
        """Return the active TraceContext for the current coroutine, or None."""
        return _current_trace.get()

    def start_span(
        self,
        operation_name: str,
        *,
        parent_span_id: Optional[str] = None,
    ) -> TraceSpan:
        """Create and register a new span.

        parent_span_id defaults to the currently open span so nesting is
        automatic: calling start_span() inside an existing traced() block
        produces a child span without explicit wiring.
        """
        span = TraceSpan(
            span_id=_generate_span_id(self.trace_id),
            trace_id=self.trace_id,
            parent_span_id=parent_span_id or self._current_span_id,
            operation_name=operation_name,
            start_time=time.monotonic(),
            start_epoch=datetime.now(timezone.utc).isoformat(),
        )
        self.spans.append(span)
        self._current_span_id = span.span_id
        return span

    def end_span(self, span: TraceSpan, *, error: Optional[str] = None) -> None:
        """Mark span finished and restore parent as current."""
        span.finish(error=error)
        # Restore the parent as the active span so the next start_span()
        # gets the correct parent_span_id.
        self._current_span_id = span.parent_span_id

    def total_cost_usd(self) -> float:
        """Sum cost_usd tags across all LLM spans.

        Wiki: "Aggregate by model, user, and query type" -- callers can filter
        by span.tags['llm.model'] for per-model attribution.
        """
        total = 0.0
        for span in self.spans:
            total += float(span.tags.get("llm.cost_usd", 0.0))
        return total

    def total_tokens(self) -> int:
        """Sum total_tokens across all LLM spans."""
        total = 0
        for span in self.spans:
            total += int(span.tags.get("llm.total_tokens", 0))
        return total

    def to_dict(self) -> dict[str, Any]:
        """Export the full trace as a JSON-safe dict.

        Shape matches the OTel Span export format closely enough that
        Phase 13's OTLP exporter can convert it with minimal mapping.
        """
        root_span = self.spans[0] if self.spans else None
        total_ms = None
        if root_span and root_span.duration_ms is not None:
            total_ms = root_span.duration_ms

        return {
            "trace_id": self.trace_id,
            "span_count": len(self.spans),
            "total_duration_ms": total_ms,
            "total_cost_usd": self.total_cost_usd(),
            "total_tokens": self.total_tokens(),
            "spans": [s.to_dict() for s in self.spans],
        }


# ── Context managers ──────────────────────────────────────────────────────────


@contextmanager
def traced(
    operation_name: str,
    *,
    trace_id: Optional[str] = None,
) -> Generator[TraceSpan, None, None]:
    """Synchronous context manager -- creates a span, yields it, finishes on exit.

    Usage:
        with traced("aggregate_results") as span:
            span.add_tag("verdict", "APPROVE")
            result = aggregate_results(state)

    If no TraceContext is active in this coroutine, a new one is created
    automatically using trace_id (or a generated UUID).  This means traced()
    is safe to call even in code paths that don't go through the full review
    workflow (e.g. unit tests).

    Wiki: "Every function call that matters gets traced."
    """
    ctx = _current_trace.get()
    if ctx is None:
        tid = trace_id or f"auto-{uuid.uuid4().hex[:12]}"
        ctx = TraceContext.create(tid)

    span = ctx.start_span(operation_name)
    error_msg: Optional[str] = None
    try:
        yield span
    except Exception as exc:
        # Record the exception on the span so it shows up in the trace export.
        error_msg = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        ctx.end_span(span, error=error_msg)


@asynccontextmanager
async def async_traced(
    operation_name: str,
    *,
    trace_id: Optional[str] = None,
) -> AsyncGenerator[TraceSpan, None]:
    """Async version of traced() -- for use inside async def functions.

    Usage:
        async with async_traced("llm.call") as span:
            response = await llm_client.call(...)
            span.add_cost_tag(
                model=response.model_used,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.estimated_cost_usd,
            )
    """
    ctx = _current_trace.get()
    if ctx is None:
        tid = trace_id or f"auto-{uuid.uuid4().hex[:12]}"
        ctx = TraceContext.create(tid)

    span = ctx.start_span(operation_name)
    error_msg: Optional[str] = None
    try:
        yield span
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        ctx.end_span(span, error=error_msg)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _generate_span_id(trace_id: str) -> str:
    """16-char hex span ID -- stable and unique within a trace.

    Uses time.monotonic() + trace_id hash to avoid collisions in
    parallel fan-out (4 agents running concurrently in the same trace).
    """
    raw = f"{trace_id}-{time.monotonic()}-{uuid.uuid4().hex}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def get_current_trace() -> Optional[TraceContext]:
    """Public accessor -- alternative to TraceContext.current() for imports."""
    return _current_trace.get()
