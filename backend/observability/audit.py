"""Append-only JSONL audit log for PR Review decisions.

DESIGN OVERVIEW
  Every consequential decision in the review pipeline gets an immutable record
  appended to a JSONL file (one JSON object per line).  Nothing is ever updated
  or deleted -- this is an audit trail, not a database.

  Pattern borrowed directly from opensre guardrails/audit.py (AuditLogger):
    - Constructor takes an optional path (defaults to a sensible location)
    - log() never raises on write failure (observability must not crash reviews)
    - read_recent(limit) returns the tail of the log for dashboards

  Wiki: "In regulated industries, agents accessing sensitive data without
  immutable audit logs creates legal exposure."  Even though PR review is not
  strictly regulated, the pattern costs nothing and pays dividends at Phase 15
  (Governance & Compliance).

JSONL VS DATABASE
  A JSONL file is:
    - Readable by any tool (grep, jq, Python open())
    - Appendable atomically per-line (no transactions needed)
    - Rotatable with logrotate
    - Importable into any log aggregator (Loki, Splunk, etc.)

  Phase 15 will layer structured querying on top -- for now the file is enough.

SENSITIVE DATA POLICY
  We do NOT log PR diff content or full LLM prompt/response text here.
  The audit log is metadata-only: review_id, verdict, agent_types, timestamps.
  This prevents the audit log itself from becoming a data-exfil vector.
  (Phase 11 Security Architecture formalises this into a masking policy.)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.observability.events import ReviewEvent

logger = logging.getLogger(__name__)

# Default path -- relative to project root, overridable via settings.
# Named .jsonl so log aggregators detect it automatically as structured logs.
_DEFAULT_AUDIT_PATH = Path("backend/observability/reviews_audit.jsonl")


class AuditLogger:
    """Append-only JSONL audit log for PR Review decisions.

    Instantiate once and reuse across requests.  Thread-safe for single-writer
    scenarios (FastAPI / ARQ workers each open the file per write, so OS-level
    append semantics give sufficient safety for our single-node Phase 13 setup).

    Phase 13's multi-replica deployment will add a Redis-backed distributed
    audit sink -- the interface here stays the same, the backend swaps out.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _DEFAULT_AUDIT_PATH

    # ── Write helpers ─────────────────────────────────────────────────────────

    def _append(self, entry: dict[str, Any]) -> None:
        """Write one JSON line.  NEVER raises -- mirrors opensre's pattern.

        opensre guardrails/audit.py: "Never raises on write failure" is the
        key contract.  Observability code must not crash the main workflow.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            # Degrade gracefully: log the failure, don't re-raise.
            logger.warning("AuditLogger: failed to write to %s: %s", self._path, exc)

    def log_review_started(
        self,
        *,
        review_id: str,
        repo: str,
        pr_number: int,
        triggered_by: str = "webhook",
    ) -> None:
        """Record that a PR review workflow has started."""
        self._append(
            {
                "timestamp": _now(),
                "event": ReviewEvent.REVIEW_STARTED,
                "review_id": review_id,
                "repo": repo,
                "pr_number": pr_number,
                "triggered_by": triggered_by,
            }
        )

    def log_verdict_emitted(
        self,
        *,
        review_id: str,
        repo: str,
        pr_number: int,
        final_verdict: str,
        verdict_breakdown: dict[str, str],
        hitl_triggered: bool,
        total_cost_usd: float,
        total_tokens: int,
    ) -> None:
        """Record the final aggregate verdict for a completed review.

        verdict_breakdown: {"security": "APPROVE", "quality": "REQUEST_CHANGES", ...}
        We log the breakdown so post-hoc analysis can identify which agent drove HITL.
        """
        self._append(
            {
                "timestamp": _now(),
                "event": ReviewEvent.VERDICT_EMITTED,
                "review_id": review_id,
                "repo": repo,
                "pr_number": pr_number,
                "final_verdict": final_verdict,
                "verdict_breakdown": verdict_breakdown,
                "hitl_triggered": hitl_triggered,
                "total_cost_usd": round(total_cost_usd, 6),
                "total_tokens": total_tokens,
            }
        )

    def log_hitl_escalation(
        self,
        *,
        review_id: str,
        repo: str,
        pr_number: int,
        critical_block_agents: list[str],
        reason: str,
    ) -> None:
        """Record that the Safety-Threshold Rule triggered HITL escalation.

        critical_block_agents: list of agent_type strings that voted CRITICAL_BLOCK.
        reason: human-readable explanation (e.g. "2+ CRITICAL_BLOCK agents").

        This is the most important audit event -- it tells compliance exactly
        why a PR was held for human review rather than auto-approved.
        """
        self._append(
            {
                "timestamp": _now(),
                "event": ReviewEvent.HITL_ESCALATED,
                "review_id": review_id,
                "repo": repo,
                "pr_number": pr_number,
                "critical_block_agents": critical_block_agents,
                "reason": reason,
            }
        )

    def log_review_failed(
        self,
        *,
        review_id: str,
        repo: str,
        pr_number: int,
        error_type: str,
        error_message: str,
    ) -> None:
        """Record that a review workflow failed with an unhandled exception."""
        self._append(
            {
                "timestamp": _now(),
                "event": ReviewEvent.REVIEW_FAILED,
                "review_id": review_id,
                "repo": repo,
                "pr_number": pr_number,
                "error_type": error_type,
                # Truncate to avoid giant stack traces in the audit log.
                # Full traces belong in StructuredLogger / Sentry, not here.
                "error_message": error_message[:500],
            }
        )

    def log_eval_gate(
        self,
        *,
        gate_name: str,
        passed: bool,
        score: float,
        threshold: float,
        blocked_reason: Optional[str] = None,
    ) -> None:
        """Record the outcome of a Phase 9 regression gate check.

        Bridges Phase 9 (Evaluation) with Phase 10 (Observability):
        gate runs are now immutable audit records, not just pytest output.
        """
        event = ReviewEvent.EVAL_GATE_RUN if passed else ReviewEvent.EVAL_GATE_BLOCKED
        entry: dict[str, Any] = {
            "timestamp": _now(),
            "event": event,
            "gate_name": gate_name,
            "passed": passed,
            "score": round(score, 4),
            "threshold": round(threshold, 4),
        }
        if blocked_reason:
            entry["blocked_reason"] = blocked_reason
        self._append(entry)

    # ── Read helpers ──────────────────────────────────────────────────────────

    def read_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent `limit` audit entries as parsed dicts.

        Returns [] if the file does not exist (not an error -- new deployments).
        Mirrors opensre AuditLogger.read_entries(limit).
        """
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").strip().splitlines()
        except OSError:
            return []

        entries: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                # Malformed lines are skipped (e.g. partial write on power loss).
                continue
        return entries

    def read_by_review(self, review_id: str) -> list[dict[str, Any]]:
        """Return all audit entries for a specific review_id.

        Full scan -- acceptable because audit logs are small and rarely queried.
        Phase 15 will add an indexed store if this becomes a bottleneck.
        """
        all_entries = self.read_recent(limit=10_000)
        return [e for e in all_entries if e.get("review_id") == review_id]

    def path(self) -> Path:
        """Return the audit log file path (for display / config validation)."""
        return self._path


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> str:
    """ISO-8601 UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()
