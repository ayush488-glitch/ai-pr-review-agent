"""Alert rules and alert manager for the PR Review Agent.

DESIGN OVERVIEW
  AlertManager evaluates a MetricSnapshot (a simple dataclass) against a list
  of AlertRules and returns which rules fired.  No external Prometheus/Grafana
  dependency in Phase 10 -- metrics are passed in by the caller.

  Phase 13 (Infrastructure) will wire a Prometheus /metrics endpoint that
  populates MetricSnapshots from real counters.  Phase 10's AlertManager is
  the evaluation engine; the metric collection plumbing is Phase 13's job.

ALERT TIERS (from wiki Observability chapter)
  PAGE    -- immediate pager; system is down or severely degraded
  URGENT  -- needs attention within 15 minutes (senior on-call)
  WARNING -- schedule a fix; degradation trend visible
  INFO    -- passive log; useful for tuning but not actionable

DEFAULT RULES (from wiki concrete examples)
  PAGE    error_rate > 0.50             (>50% reviews failing)
  URGENT  p99_latency_ms > 10_000       (p99 > 10s)
  WARNING token_inflation > 1.30        (tokens 30% above baseline)
  WARNING hitl_rate > 0.30             (>30% reviews escalated to HITL)
  INFO    avg_cost_per_review > 0.10   ($0.10 per review -- cost drift)

WHY NOT PROMETHEUS ALERTING RULES?
  Those live in a .yaml file and require a running Prometheus + Alertmanager.
  We want alerting logic testable in CI with no infrastructure.  AlertManager
  here is the in-process evaluation layer.  The Phase 13 Prometheus exporter
  will expose the same metrics so external tools can consume them too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# StrEnum compat (Python 3.10 doesn't have it)
try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


class AlertLevel(StrEnum):
    """Severity tiers from wiki Observability chapter.

    Ordered: PAGE > URGENT > WARNING > INFO (higher index = more severe).
    """
    INFO    = "INFO"
    WARNING = "WARNING"
    URGENT  = "URGENT"
    PAGE    = "PAGE"

    def is_at_least(self, other: "AlertLevel") -> bool:
        """True if this level is >= other in severity."""
        _order = [AlertLevel.INFO, AlertLevel.WARNING, AlertLevel.URGENT, AlertLevel.PAGE]
        return _order.index(self) >= _order.index(other)


@dataclass(frozen=True)
class AlertRule:
    """A single alerting rule: condition function + metadata.

    condition: receives a MetricSnapshot, returns True if the rule fires.
    Frozen so rules can be stored in sets and compared by identity.

    Usage:
        rule = AlertRule(
            name="high_error_rate",
            level=AlertLevel.PAGE,
            description="Error rate > 50%",
            condition=lambda m: m.error_rate > 0.50,
        )
    """
    name: str
    level: AlertLevel
    description: str
    condition: Callable[["MetricSnapshot"], bool]

    def check(self, metrics: "MetricSnapshot") -> bool:
        """Evaluate this rule against a snapshot.  Never raises."""
        try:
            return bool(self.condition(metrics))
        except Exception:
            # A broken condition should not crash the review workflow.
            return False


@dataclass
class FiredAlert:
    """One rule that fired against a MetricSnapshot."""
    rule_name: str
    level: AlertLevel
    description: str
    metric_snapshot: "MetricSnapshot"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "level": str(self.level),
            "description": self.description,
        }


@dataclass
class MetricSnapshot:
    """Point-in-time metrics for one review or a rolling window.

    All fields have sensible defaults so partial snapshots are valid.
    Phase 13 will populate these from Prometheus counters + histograms.
    For now, the orchestrator builds a MetricSnapshot after each review
    from TraceContext totals and workflow state.

    Field names map 1:1 to Prometheus metric names (with dots -> underscores):
      error_rate           -> review_error_rate
      p99_latency_ms       -> review_p99_latency_milliseconds
      token_inflation      -> llm_token_inflation_ratio  (current / baseline)
      hitl_rate            -> review_hitl_rate
      avg_cost_per_review  -> review_avg_cost_usd
    """
    # Fraction of reviews that failed (unhandled exception or timeout)
    error_rate: float = 0.0

    # 99th-percentile end-to-end review latency in milliseconds
    p99_latency_ms: float = 0.0

    # Ratio of current tokens/review to baseline tokens/review.
    # 1.0 = on baseline; 1.30 = 30% inflation.
    # Wiki: "Alert when tokens-per-query exceed 130% of baseline."
    token_inflation: float = 1.0

    # Fraction of reviews that triggered HITL escalation (Safety-Threshold Rule)
    hitl_rate: float = 0.0

    # Average dollar cost per completed review
    avg_cost_per_review: float = 0.0

    # Optional: sample size for rolling-window stats
    sample_size: int = 0

    # Free-form extra metrics for custom rules
    extra: dict[str, Any] = field(default_factory=dict)


# ── Default rules (wiki + business context) ───────────────────────────────────

def _build_default_rules() -> list[AlertRule]:
    """Build the default alerting rule set.

    Kept in a function so tests can override without mutating the module-level
    list.  Callers that want different thresholds instantiate AlertManager with
    custom rules.
    """
    return [
        AlertRule(
            name="critical_error_rate",
            level=AlertLevel.PAGE,
            description="Error rate > 50% -- system severely degraded",
            # Wiki: "PAGE on error_rate > 0.5"
            condition=lambda m: m.error_rate > 0.50,
        ),
        AlertRule(
            name="high_p99_latency",
            level=AlertLevel.URGENT,
            description="p99 latency > 10s -- reviews are timing out",
            # Wiki: "URGENT on p99_latency > 10s"
            condition=lambda m: m.p99_latency_ms > 10_000,
        ),
        AlertRule(
            name="token_inflation",
            level=AlertLevel.WARNING,
            description="Token usage 30% above baseline -- prompt regression or context bloat",
            # Wiki: "WARNING on token_inflation > 130% of baseline"
            condition=lambda m: m.token_inflation > 1.30,
        ),
        AlertRule(
            name="high_hitl_rate",
            level=AlertLevel.WARNING,
            description=">30% of reviews escalated to HITL -- Safety-Threshold Rule may be mis-tuned",
            condition=lambda m: m.hitl_rate > 0.30,
        ),
        AlertRule(
            name="cost_drift",
            level=AlertLevel.INFO,
            description="Average cost > $0.10/review -- budget drift, check model routing",
            # Wiki: "INFO on cache_hit_rate < 20%" -- analogous cost-tracking signal
            condition=lambda m: m.avg_cost_per_review > 0.10,
        ),
    ]


DEFAULT_ALERT_RULES: list[AlertRule] = _build_default_rules()


# ── Alert Manager ─────────────────────────────────────────────────────────────


class AlertManager:
    """Evaluate a MetricSnapshot against alerting rules.

    Stateless evaluator -- create once, call check_conditions() repeatedly.
    Thread/coroutine safe because it holds no mutable state.

    Usage:
        manager = AlertManager()
        snapshot = MetricSnapshot(error_rate=0.60)
        fired = manager.check_conditions(snapshot)
        for alert in fired:
            if alert.level.is_at_least(AlertLevel.URGENT):
                notify_oncall(alert)
    """

    def __init__(
        self,
        rules: Optional[list[AlertRule]] = None,
    ) -> None:
        # If no rules provided, use the default set.
        # Pass an explicit empty list [] to disable all alerts (useful in tests).
        self._rules = rules if rules is not None else DEFAULT_ALERT_RULES

    @property
    def rules(self) -> list[AlertRule]:
        return list(self._rules)  # defensive copy

    def check_conditions(self, metrics: MetricSnapshot) -> list[FiredAlert]:
        """Evaluate all rules.  Returns list of fired alerts (may be empty).

        Does NOT raise -- a misbehaving rule condition is silently skipped
        (AlertRule.check() absorbs the exception).  This matches opensre's
        "never crash the main workflow from observability code" principle.
        """
        fired: list[FiredAlert] = []
        for rule in self._rules:
            if rule.check(metrics):
                fired.append(
                    FiredAlert(
                        rule_name=rule.name,
                        level=rule.level,
                        description=rule.description,
                        metric_snapshot=metrics,
                    )
                )
        return fired

    def highest_level(self, metrics: MetricSnapshot) -> Optional[AlertLevel]:
        """Return the highest-severity level from all fired rules, or None.

        Useful for a single-signal status dashboard widget:
            RED = PAGE, ORANGE = URGENT, YELLOW = WARNING, GREEN = None
        """
        fired = self.check_conditions(metrics)
        if not fired:
            return None
        _order = [AlertLevel.INFO, AlertLevel.WARNING, AlertLevel.URGENT, AlertLevel.PAGE]
        return max(fired, key=lambda a: _order.index(a.level)).level

    def add_rule(self, rule: AlertRule) -> None:
        """Add a custom rule at runtime (e.g. loaded from config)."""
        self._rules = list(self._rules) + [rule]
