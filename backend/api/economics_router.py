# backend/api/economics_router.py
#
# Phase 16 — Economics & Cost Control REST API.
#
# ENDPOINTS (all auth-gated):
#   GET /api/v1/economics/summary
#         { today_usd, last_7d_usd, last_30d_usd,
#           by_model_30d, by_agent_30d, totals... }
#
#   GET /api/v1/economics/budget
#         { daily_cap_usd, daily_spent_usd, daily_headroom_usd,
#           daily_utilization, per_review_cap_usd, exceeded }
#
#   GET /api/v1/economics/timeseries?days=30
#         [ {date, cost_usd, call_count}, ... ]   ascending by date
#
#   GET /api/v1/economics/workflow/{workflow_id:path}
#         Per-PR-review cost rollup. workflow_id is "owner/repo:pr:sha" so we
#         use the :path converter (same fix as the reviews router).
#
# HUMBLE ROUTER PATTERN: this file does parsing + auth + DTO mapping only.
# All aggregation lives in backend.economics.cost_repository / budget.

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.auth.dependencies import require_auth
from backend.economics import (
    BudgetGuard,
    get_daily_timeseries,
    get_summary,
    get_workflow_cost,
)

logger = logging.getLogger(__name__)

economics_router = APIRouter(prefix="/api/v1/economics", tags=["economics"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class SummaryResponse(BaseModel):
    today_usd: float
    last_7d_usd: float
    last_30d_usd: float
    by_model_30d: dict[str, float]
    by_agent_30d: dict[str, float]
    call_count_30d: int
    total_input_tokens_30d: int
    total_output_tokens_30d: int


class BudgetStatusResponse(BaseModel):
    daily_cap_usd: float
    daily_spent_usd: float
    daily_headroom_usd: float
    daily_utilization: float = Field(..., description="0.0-1.0+ ; >=1.0 means cap reached")
    per_review_cap_usd: float
    exceeded: bool


class DailyPointResponse(BaseModel):
    date: str
    cost_usd: float
    call_count: int


class WorkflowCostResponse(BaseModel):
    workflow_id: str
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    by_agent: dict[str, float]
    by_model: dict[str, float]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@economics_router.get("/summary", response_model=SummaryResponse)
async def summary(_auth: None = Depends(require_auth)) -> SummaryResponse:
    """
    Top-level cost snapshot for the dashboard's primary cost card.
    One round trip — backs the today / 7d / 30d numbers AND the breakdown pies.
    """
    s = await get_summary()
    return SummaryResponse(
        today_usd=s.today_usd,
        last_7d_usd=s.last_7d_usd,
        last_30d_usd=s.last_30d_usd,
        by_model_30d=s.by_model_30d,
        by_agent_30d=s.by_agent_30d,
        call_count_30d=s.call_count_30d,
        total_input_tokens_30d=s.total_input_tokens_30d,
        total_output_tokens_30d=s.total_output_tokens_30d,
    )


@economics_router.get("/budget", response_model=BudgetStatusResponse)
async def budget(_auth: None = Depends(require_auth)) -> BudgetStatusResponse:
    """
    Daily budget gauge. Returns headroom + utilization for a progress bar.
    """
    status_dict: dict[str, Any] = await BudgetGuard().status()
    return BudgetStatusResponse(**status_dict)


@economics_router.get("/timeseries", response_model=list[DailyPointResponse])
async def timeseries(
    days: int = Query(30, ge=1, le=365),
    _auth: None = Depends(require_auth),
) -> list[DailyPointResponse]:
    """
    Per-UTC-day cost points for charting. Dense (zeros for missing days).
    Default window: 30 days. Cap: 365.
    """
    points = await get_daily_timeseries(days=days)
    return [DailyPointResponse(date=p.date, cost_usd=p.cost_usd, call_count=p.call_count) for p in points]


@economics_router.get("/workflow/{workflow_id:path}", response_model=WorkflowCostResponse)
async def workflow_cost(
    workflow_id: str,
    _auth: None = Depends(require_auth),
) -> WorkflowCostResponse:
    """
    Cost rollup for one PR review run.

    workflow_id format: "owner/repo:pr_number:commit_sha". We use the :path
    converter because the id contains slashes (same convention as the reviews
    router fix in commit 428c1d8).
    """
    if not workflow_id or len(workflow_id) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workflow_id must be 1..255 chars",
        )

    rollup = await get_workflow_cost(workflow_id)
    return WorkflowCostResponse(
        workflow_id=rollup.workflow_id,
        total_cost_usd=rollup.total_cost_usd,
        total_input_tokens=rollup.total_input_tokens,
        total_output_tokens=rollup.total_output_tokens,
        call_count=rollup.call_count,
        by_agent=rollup.by_agent,
        by_model=rollup.by_model,
    )
