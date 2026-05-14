# backend/economics/budget.py
#
# Phase 16 — BudgetGuard: hard daily-cap enforcement on LLM spend.
#
# CONTRACT:
#   - check_daily_budget() is called BEFORE expensive LLM calls.
#   - On breach: raises BudgetExceededError. Caller (base_agent) catches and
#     returns a degraded AgentOutput (confidence=0.3, no findings) — this
#     naturally routes the review through the HITL queue without any extra
#     plumbing, because the review aggregator already escalates low-confidence
#     reviews.
#
# WHY ONLY THE DAILY CAP?
#   Per-review caps require coordinating across parallel fan-out agents (4
#   agents firing simultaneously cannot share a precise running total without
#   a distributed lock). We surface per-review spend as a *metric* on the
#   summary endpoint and let Phase 20 use those numbers to retrain routing
#   decisions. Daily cap is the single number that actually matters for "did
#   we burn $10k overnight".
#
# FAIL-OPEN BEHAVIOR:
#   If the daily-spend query itself fails (DB hiccup), we log and PASS the
#   budget check. Telemetry failures must not block the pipeline. The wiki is
#   explicit: "Telemetry that fails the request is anti-telemetry."

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from backend.config.settings import Settings, get_settings
from backend.economics.cost_repository import get_daily_spend

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when the daily LLM-spend cap would be breached by the next call."""

    def __init__(self, current_spend_usd: float, cap_usd: float, headroom_usd: float):
        self.current_spend_usd = current_spend_usd
        self.cap_usd = cap_usd
        self.headroom_usd = headroom_usd
        super().__init__(
            f"Daily LLM budget cap of ${cap_usd:.2f} reached. "
            f"Spent so far today: ${current_spend_usd:.4f}. "
            f"Headroom: ${headroom_usd:.4f}. "
            f"Review will be routed to HITL until the next UTC day."
        )


class BudgetGuard:
    """
    Enforces the daily LLM-spend cap.

    Usage (in agent code):
        guard = BudgetGuard()
        await guard.check_daily_budget()   # raises BudgetExceededError on breach
        # ... proceed with LLM call
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()

    @property
    def daily_cap_usd(self) -> float:
        return float(self._settings.daily_budget_usd)

    @property
    def per_review_cap_usd(self) -> float:
        return float(self._settings.per_review_budget_usd)

    async def current_daily_spend_usd(self) -> float:
        """Return the cumulative USD spent in the current UTC day."""
        return await get_daily_spend(at=datetime.now(timezone.utc))

    async def check_daily_budget(self) -> None:
        """
        Raise BudgetExceededError if the daily cap is already met.

        Note: the check is "already met", not "would be exceeded by this call".
        We don't know the cost of the next call until after it runs (token
        counts depend on the response). The cap is therefore a soft
        upper bound: actual spend can overshoot by at most one call's worth.
        For a $50/day cap and typical $0.01-0.10 calls, that's ≤ 0.2% slip.
        """
        try:
            spent = await self.current_daily_spend_usd()
        except Exception as exc:
            logger.warning("budget_check_query_failed | failing_open | error=%s", exc)
            return  # fail-open

        cap = self.daily_cap_usd
        if cap <= 0:
            return  # cap=0 means disabled
        if spent >= cap:
            raise BudgetExceededError(
                current_spend_usd=spent,
                cap_usd=cap,
                headroom_usd=max(0.0, cap - spent),
            )

    async def status(self) -> dict:
        """Return a JSON-friendly status object for the /budget endpoint."""
        spent = await self.current_daily_spend_usd()
        cap = self.daily_cap_usd
        headroom = max(0.0, cap - spent)
        utilization = (spent / cap) if cap > 0 else 0.0
        return {
            "daily_cap_usd": round(cap, 4),
            "daily_spent_usd": round(spent, 6),
            "daily_headroom_usd": round(headroom, 6),
            "daily_utilization": round(utilization, 4),
            "per_review_cap_usd": round(self.per_review_cap_usd, 4),
            "exceeded": spent >= cap and cap > 0,
        }
