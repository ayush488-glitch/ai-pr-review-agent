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


# =============================================================================
# Tiger Cloud aggregate endpoints
# These read from pre-materialized continuous aggregates — sub-millisecond
# at any scale. No GROUP BY scan over raw rows.
# =============================================================================

@economics_router.get("/agent-health", summary="Per-agent health from Tiger aggregate")
async def get_agent_health(
    minutes: int = Query(default=60, ge=1, le=1440, description="Lookback window in minutes"),
) -> list[dict]:
    """
    Returns per-agent cost, p95 latency, and rejection rate over the last N minutes.
    Source: agent_health_1m continuous aggregate (TimescaleDB, refreshed every minute).
    """
    from backend.database.postgres import get_tiger_pool
    pool = get_tiger_pool()
    if pool is None:
        return []
    sql = """
        SELECT
            bucket::text,
            agent,
            COALESCE(llm_calls, 0)         AS llm_calls,
            COALESCE(cost_usd, 0.0)        AS cost_usd,
            COALESCE(tokens_in, 0)         AS tokens_in,
            COALESCE(tokens_out, 0)        AS tokens_out,
            COALESCE(p95_ms, 0.0)          AS p95_ms,
            COALESCE(p50_ms, 0.0)          AS p50_ms,
            COALESCE(rejection_rate, 0.0)  AS rejection_rate,
            COALESCE(escalation_rate, 0.0) AS escalation_rate
        FROM agent_health_1m
        WHERE bucket >= now() - make_interval(mins => $1)
        ORDER BY bucket DESC, agent
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, minutes)
    return [dict(r) for r in rows]


@economics_router.get("/pr-cost/{review_id}", summary="Per-PR cost from Tiger aggregate")
async def get_pr_cost(review_id: str) -> dict:
    """
    Returns total cost, tokens, agents used, and wall time for a specific PR review.
    Source: pr_cost_hourly continuous aggregate (TimescaleDB, refreshed every hour).
    """
    from backend.database.postgres import get_tiger_pool
    import uuid
    pool = get_tiger_pool()
    if pool is None:
        return {"review_id": review_id, "total_cost_usd": 0.0, "total_tokens": 0}
    sql = """
        SELECT
            review_id::text,
            COALESCE(sum(total_cost_usd), 0.0) AS total_cost_usd,
            COALESCE(sum(total_tokens), 0)      AS total_tokens,
            COALESCE(max(agents_used), 0)       AS agents_used,
            COALESCE(max(max_confidence), 0.0)  AS max_confidence
        FROM pr_cost_hourly
        WHERE review_id = $1
        GROUP BY review_id
    """
    try:
        rid = uuid.UUID(review_id)
    except ValueError:
        return {"error": "invalid review_id format"}
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, rid)
    if row is None:
        return {"review_id": review_id, "total_cost_usd": 0.0, "total_tokens": 0, "note": "no data yet"}
    return dict(row)


@economics_router.get("/daily-summary", summary="24h cost + latency summary from Tiger aggregates")
async def get_daily_summary() -> dict:
    """
    Returns a 24-hour rollup: total cost, total tokens, p95 latency per agent,
    and rejection rate per agent.
    Source: agent_health_1m continuous aggregate.
    """
    from backend.database.postgres import get_tiger_pool
    pool = get_tiger_pool()
    if pool is None:
        return {"error": "tiger pool not initialized"}
    sql = """
        SELECT
            agent,
            COALESCE(sum(llm_calls), 0)         AS llm_calls,
            COALESCE(sum(cost_usd), 0.0)        AS cost_usd,
            COALESCE(sum(tokens_in), 0)         AS tokens_in,
            COALESCE(sum(tokens_out), 0)        AS tokens_out,
            COALESCE(max(p95_ms), 0.0)          AS p95_ms,
            COALESCE(avg(rejection_rate), 0.0)  AS avg_rejection_rate
        FROM agent_health_1m
        WHERE bucket >= now() - INTERVAL '24 hours'
        GROUP BY agent
        ORDER BY cost_usd DESC
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return {
        "window": "24h",
        "agents": [dict(r) for r in rows],
        "total_cost_usd": sum(float(r["cost_usd"]) for r in rows),
        "total_tokens": sum(int(r["tokens_in"]) + int(r["tokens_out"]) for r in rows),
    }
