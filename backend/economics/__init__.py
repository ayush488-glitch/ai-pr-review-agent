# backend/economics/__init__.py
#
# Phase 16 — Economics & Cost Control.
#
# This package owns:
#   - cost_repository.py    Append + aggregate LLMCallLog rows.
#   - budget.py             BudgetGuard: hard daily-cap enforcement.
#   - routing_advisor.py    Advisory model recommendations (no auto-switching).
#
# Public API (what other modules should import):
#
#     from backend.economics import (
#         record_llm_call,        # fire-and-forget persistence
#         get_daily_spend,        # current UTC-day total in USD
#         get_workflow_cost,      # cost rollup for one PR review run
#         BudgetGuard,            # check before LLM calls
#         BudgetExceededError,    # raised on cap breach
#     )
#
# Wiki anchors:
#   LLMOps-Essentials.md "Cost Control" — daily/per-request budget caps.
#   LLMOps-Essentials.md "Putting It All Together" — 4-layer agent (cost is layer 4).
#   Storage-Engines.md "Append-only log tables" — LLMCallLog design.

from backend.economics.cost_repository import (
    record_llm_call,
    get_daily_spend,
    get_workflow_cost,
    get_summary,
    get_daily_timeseries,
)
from backend.economics.budget import BudgetGuard, BudgetExceededError
from backend.economics.routing_advisor import recommend_model, would_have_saved

__all__ = [
    "record_llm_call",
    "get_daily_spend",
    "get_workflow_cost",
    "get_summary",
    "get_daily_timeseries",
    "BudgetGuard",
    "BudgetExceededError",
    "recommend_model",
    "would_have_saved",
]
