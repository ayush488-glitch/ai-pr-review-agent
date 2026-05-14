"""Phase 10 Smoke Tests: Observability & Tracing.

20 tests across 5 groups:
  Group A: Events (3 tests)       -- ReviewEvent StrEnum correctness
  Group B: Tracing (5 tests)      -- TraceContext, TraceSpan, traced(), cost tags
  Group C: Logging (4 tests)      -- StructuredLogger JSON schema, named helpers
  Group D: Alerting (5 tests)     -- AlertLevel order, rule evaluation, AlertManager
  Group E: Audit (3 tests)        -- AuditLogger write/read, never-raise contract

No real infrastructure needed: no Redis, no Postgres, no OTel collector.
AuditLogger tests use a tmp_path fixture to avoid file-system side effects.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# GROUP A: Events (3 tests)
# ─────────────────────────────────────────────────────────────────────────────

from backend.observability.events import ReviewEvent


def test_review_event_is_str():
    """ReviewEvent members must be plain strings (StrEnum contract)."""
    assert isinstance(ReviewEvent.REVIEW_STARTED, str)
    assert ReviewEvent.REVIEW_STARTED == "review.started"


def test_review_event_covers_lifecycle():
    """All major lifecycle stages must have a corresponding event."""
    required = {
        "WEBHOOK_RECEIVED",
        "REVIEW_STARTED",
        "REVIEW_COMPLETED",
        "REVIEW_FAILED",
        "AGENT_INVOKED",
        "AGENT_COMPLETED",
        "LLM_CALLED",
        "VERDICT_EMITTED",
        "HITL_ESCALATED",
    }
    names = {e.name for e in ReviewEvent}
    missing = required - names
    assert not missing, f"Missing events: {missing}"


def test_review_event_dot_naming_convention():
    """All events use dot-separated lowercase format (e.g. 'llm.called')."""
    for event in ReviewEvent:
        val = event.value  # use .value explicitly -- StrEnum shim on 3.10 makes str() include class name
        assert "." in val, f"Event {event!r} missing dot separator"
        assert val == val.lower(), f"Event {event!r} value {val!r} not lowercase"


# ─────────────────────────────────────────────────────────────────────────────
# GROUP B: Tracing (5 tests)
# ─────────────────────────────────────────────────────────────────────────────

from backend.observability.tracing import (
    TraceContext,
    traced,
)


def test_trace_context_create_and_current():
    """TraceContext.create() registers itself in the current contextvars context."""
    review_id = f"review_{uuid.uuid4().hex[:8]}"
    ctx = TraceContext.create(review_id)
    assert ctx.trace_id == review_id
    assert TraceContext.current() is ctx


def test_traced_creates_span_and_measures_duration():
    """traced() context manager creates a span with duration_ms set after exit."""
    ctx = TraceContext.create(f"review_{uuid.uuid4().hex[:8]}")
    with traced("test_operation") as span:
        assert span.duration_ms is None  # not finished yet
        time.sleep(0.01)  # 10ms

    assert span.duration_ms is not None
    assert span.duration_ms >= 5.0  # at least 5ms
    assert span in ctx.spans


def test_traced_captures_exception_on_span():
    """traced() records the exception type on span.error without swallowing it."""
    ctx = TraceContext.create(f"review_{uuid.uuid4().hex[:8]}")
    with pytest.raises(ValueError):
        with traced("failing_op") as span:
            raise ValueError("test error")

    assert span.error is not None
    assert "ValueError" in span.error
    assert span.duration_ms is not None  # span still finishes


def test_add_cost_tag_sets_all_fields():
    """add_cost_tag() must set all four required LLM attribution tags."""
    ctx = TraceContext.create(f"review_{uuid.uuid4().hex[:8]}")
    with traced("llm_call") as span:
        span.add_cost_tag(
            model="claude-3-5-sonnet",
            input_tokens=1000,
            output_tokens=200,
            cost_usd=0.0045,
        )

    assert span.tags["llm.model"] == "claude-3-5-sonnet"
    assert span.tags["llm.input_tokens"] == 1000
    assert span.tags["llm.output_tokens"] == 200
    assert span.tags["llm.total_tokens"] == 1200
    assert span.tags["llm.cost_usd"] == 0.0045


def test_trace_context_cost_rollup():
    """total_cost_usd() sums llm.cost_usd across all spans."""
    ctx = TraceContext.create(f"review_{uuid.uuid4().hex[:8]}")
    for cost in [0.001, 0.002, 0.003]:
        with traced("llm_call") as span:
            span.add_cost_tag(
                model="gpt-4o-mini",
                input_tokens=100,
                output_tokens=50,
                cost_usd=cost,
            )

    total = ctx.total_cost_usd()
    assert abs(total - 0.006) < 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# GROUP C: Logging (4 tests)
# ─────────────────────────────────────────────────────────────────────────────

from backend.observability.logging import (
    get_logger,
    log_llm_call,
    log_review_verdict,
    log_tool_call,
)


def test_structured_logger_returns_json_compatible_dict():
    """StructuredLogger._emit must return a JSON-serializable dict."""
    logger = get_logger("test.logger")
    entry = logger.info("test.event", "Hello structured world", trace_id="tr1")

    # Must be JSON-serializable
    dumped = json.dumps(entry)
    parsed = json.loads(dumped)

    assert parsed["event"] == "test.event"
    assert parsed["message"] == "Hello structured world"
    assert parsed["trace_id"] == "tr1"
    assert parsed["level"] == "INFO"
    assert "timestamp" in parsed
    assert "service" in parsed


def test_log_llm_call_includes_all_required_fields():
    """log_llm_call must include model, tokens, cost, and latency."""
    logger = get_logger("test.llm")
    entry = log_llm_call(
        logger,
        trace_id="review_001",
        agent_type="security",
        model="claude-3-5-sonnet",
        input_tokens=2000,
        output_tokens=300,
        cost_usd=0.0087,
        latency_ms=450.0,
    )

    assert entry["agent_type"] == "security"
    assert entry["model"] == "claude-3-5-sonnet"
    assert entry["input_tokens"] == 2000
    assert entry["output_tokens"] == 300
    assert entry["total_tokens"] == 2300
    assert entry["cost_usd"] == 0.0087
    assert entry["latency_ms"] == 450.0


def test_log_tool_call_success_vs_failure_event():
    """log_tool_call uses different events for success vs failure."""
    logger = get_logger("test.tool")
    success_entry = log_tool_call(
        logger,
        trace_id="tr",
        agent_type="security",
        tool_name="check_secrets_pattern",
        success=True,
        latency_ms=12.5,
    )
    fail_entry = log_tool_call(
        logger,
        trace_id="tr",
        agent_type="security",
        tool_name="run_syntax_check",
        success=False,
        latency_ms=5.0,
        error_message="timeout",
    )

    assert success_entry["event"] == "tool.called"
    assert fail_entry["event"] == "tool.failed"
    assert fail_entry["error_message"] == "timeout"


def test_log_review_verdict_rounds_cost():
    """log_review_verdict must round cost_usd to 6 decimal places."""
    logger = get_logger("test.verdict")
    entry = log_review_verdict(
        logger,
        trace_id="review_xyz",
        final_verdict="APPROVE",
        hitl_triggered=False,
        agent_count=4,
        total_cost_usd=0.0087654321,
        total_tokens=6000,
        total_latency_ms=1234.5,
    )

    # 0.0087654321 rounded to 6dp = 0.008765
    assert entry["total_cost_usd"] == round(0.0087654321, 6)
    assert entry["final_verdict"] == "APPROVE"
    assert entry["hitl_triggered"] is False


# ─────────────────────────────────────────────────────────────────────────────
# GROUP D: Alerting (5 tests)
# ─────────────────────────────────────────────────────────────────────────────

from backend.observability.alerting import (
    AlertLevel,
    AlertManager,
    MetricSnapshot,
)


def test_alert_level_order():
    """PAGE must be more severe than URGENT > WARNING > INFO."""
    assert AlertLevel.PAGE.is_at_least(AlertLevel.URGENT)
    assert AlertLevel.URGENT.is_at_least(AlertLevel.WARNING)
    assert AlertLevel.WARNING.is_at_least(AlertLevel.INFO)
    assert not AlertLevel.INFO.is_at_least(AlertLevel.WARNING)


def test_no_alerts_when_all_metrics_normal():
    """AlertManager must return empty list for healthy metrics."""
    manager = AlertManager()
    snapshot = MetricSnapshot(
        error_rate=0.01,
        p99_latency_ms=500.0,
        token_inflation=1.05,
        hitl_rate=0.05,
        avg_cost_per_review=0.02,
    )
    fired = manager.check_conditions(snapshot)
    assert fired == []


def test_high_error_rate_fires_page_alert():
    """error_rate > 0.50 must fire a PAGE-level alert."""
    manager = AlertManager()
    snapshot = MetricSnapshot(error_rate=0.60)
    fired = manager.check_conditions(snapshot)
    page_alerts = [a for a in fired if a.level == AlertLevel.PAGE]
    assert len(page_alerts) >= 1
    assert any(a.rule_name == "critical_error_rate" for a in page_alerts)


def test_token_inflation_fires_warning():
    """token_inflation > 1.30 must fire a WARNING."""
    manager = AlertManager()
    snapshot = MetricSnapshot(token_inflation=1.45)
    fired = manager.check_conditions(snapshot)
    warning_alerts = [a for a in fired if a.level == AlertLevel.WARNING]
    assert any(a.rule_name == "token_inflation" for a in warning_alerts)


def test_highest_level_returns_page_when_multiple_fired():
    """highest_level() returns the most severe fired alert."""
    manager = AlertManager()
    # Triggers both PAGE (error_rate) and WARNING (token_inflation)
    snapshot = MetricSnapshot(error_rate=0.70, token_inflation=1.50)
    level = manager.highest_level(snapshot)
    assert level == AlertLevel.PAGE


# ─────────────────────────────────────────────────────────────────────────────
# GROUP E: Audit (3 tests)
# ─────────────────────────────────────────────────────────────────────────────

from backend.observability.audit import AuditLogger


def test_audit_logger_write_and_read(tmp_path: Path):
    """AuditLogger.log_verdict_emitted + read_recent round-trip."""
    audit = AuditLogger(path=tmp_path / "test_audit.jsonl")
    audit.log_verdict_emitted(
        review_id="review_001",
        repo="org/repo",
        pr_number=42,
        final_verdict="APPROVE",
        verdict_breakdown={"security": "APPROVE", "quality": "APPROVE"},
        hitl_triggered=False,
        total_cost_usd=0.0045,
        total_tokens=4500,
    )

    entries = audit.read_recent(limit=10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["event"] == "verdict.emitted"
    assert entry["review_id"] == "review_001"
    assert entry["final_verdict"] == "APPROVE"
    assert entry["hitl_triggered"] is False
    assert entry["total_tokens"] == 4500


def test_audit_logger_never_raises_on_bad_path():
    """AuditLogger must not raise even if the path is unwritable.

    opensre contract: "Never raises on write failure."
    We simulate by passing a path inside a non-existent read-only directory.
    The logger should swallow the OSError and log a warning instead.
    """
    # Use a path we definitely can't write to in tests
    audit = AuditLogger(path=Path("/nonexistent/deep/dir/audit.jsonl"))
    # Must not raise
    audit.log_review_failed(
        review_id="r1",
        repo="org/repo",
        pr_number=1,
        error_type="TimeoutError",
        error_message="Workflow timed out after 30s",
    )


def test_audit_logger_read_by_review_id(tmp_path: Path):
    """read_by_review returns only entries for the requested review_id."""
    audit = AuditLogger(path=tmp_path / "multi_audit.jsonl")

    for rid, verdict in [("review_A", "APPROVE"), ("review_B", "REQUEST_CHANGES")]:
        audit.log_verdict_emitted(
            review_id=rid,
            repo="org/repo",
            pr_number=1,
            final_verdict=verdict,
            verdict_breakdown={},
            hitl_triggered=False,
            total_cost_usd=0.001,
            total_tokens=100,
        )

    entries_a = audit.read_by_review("review_A")
    assert len(entries_a) == 1
    assert entries_a[0]["final_verdict"] == "APPROVE"

    entries_b = audit.read_by_review("review_B")
    assert len(entries_b) == 1
    assert entries_b[0]["final_verdict"] == "REQUEST_CHANGES"
