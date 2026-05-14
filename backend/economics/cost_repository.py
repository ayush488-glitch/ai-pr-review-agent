# backend/economics/cost_repository.py
#
# Phase 16 — Persistence + aggregation for LLM cost data.
#
# DESIGN:
#   - record_llm_call():    Fire-and-forget insert. Errors logged, never raised.
#                           Cost telemetry must never break the review pipeline.
#   - get_*():              Read aggregations for the economics API.
#
# WHY FIRE-AND-FORGET?
# (LLMOps-Essentials.md: "Telemetry that fails the request is anti-telemetry.")
# If Postgres is down, we still want the LLM call to succeed and the review to
# proceed. We log the failure to stderr/structured logs and move on.
#
# ASYNC SESSION PATTERN:
# We use the module-level async_sessionmaker via get_engine() — same pattern as
# the rest of the codebase. Every public function is async and acquires its own
# short-lived session.

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.database.models import LLMCallLog
from backend.database.postgres import get_engine

logger = logging.getLogger(__name__)


def _new_session() -> AsyncSession:
    """Return a new async session bound to the module-level engine."""
    factory = async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return factory()


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------
async def record_llm_call(
    *,
    workflow_id: Optional[str],
    agent_type: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: float,
    is_valid_json: bool = True,
) -> None:
    """
    Persist one LLM call row. Never raises on failure.

    Called from tools/llm_client.py after each successful API call. Also called
    by tests to seed cost data deterministically.
    """
    try:
        async with _new_session() as session:
            row = LLMCallLog(
                workflow_id=workflow_id,
                agent_type=agent_type or "system",
                model=model,
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                cost_usd=float(cost_usd),
                latency_ms=float(latency_ms),
                is_valid_json=1 if is_valid_json else 0,
            )
            session.add(row)
            await session.commit()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "llm_call_log_persist_failed | agent=%s model=%s cost=%.6f error=%s",
            agent_type, model, cost_usd, exc,
        )


# ---------------------------------------------------------------------------
# Read path — aggregations
# ---------------------------------------------------------------------------
def _utc_today_start() -> datetime:
    """Return midnight UTC of the current day."""
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)


async def get_daily_spend(
    *,
    at: Optional[datetime] = None,
) -> float:
    """
    Total USD spent so far in the current UTC day (or the day containing `at`).

    Used by BudgetGuard before each agent LLM call. Returns 0.0 if the table
    is empty or unreachable (fail-open: never block on telemetry failure).
    """
    target = at or datetime.now(timezone.utc)
    day_start = datetime(target.year, target.month, target.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    try:
        async with _new_session() as session:
            stmt = select(func.coalesce(func.sum(LLMCallLog.cost_usd), 0.0)).where(
                LLMCallLog.created_at >= day_start,
                LLMCallLog.created_at < day_end,
            )
            result = await session.execute(stmt)
            return float(result.scalar() or 0.0)
    except Exception as exc:
        logger.warning("daily_spend_query_failed | error=%s", exc)
        return 0.0


@dataclass
class WorkflowCost:
    """Cost rollup for a single workflow (one PR review run)."""
    workflow_id: str
    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int
    call_count: int
    by_agent: dict[str, float]   # agent_type -> cost_usd
    by_model: dict[str, float]   # model -> cost_usd


async def get_workflow_cost(workflow_id: str) -> WorkflowCost:
    """Cost rollup for one workflow, broken down by agent and model."""
    try:
        async with _new_session() as session:
            stmt = select(LLMCallLog).where(LLMCallLog.workflow_id == workflow_id)
            rows = (await session.execute(stmt)).scalars().all()
    except Exception as exc:
        logger.warning("workflow_cost_query_failed | wf=%s error=%s", workflow_id, exc)
        rows = []

    by_agent: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    total_cost = 0.0
    total_in = 0
    total_out = 0
    for r in rows:
        by_agent[r.agent_type] += r.cost_usd
        by_model[r.model] += r.cost_usd
        total_cost += r.cost_usd
        total_in += r.input_tokens
        total_out += r.output_tokens

    return WorkflowCost(
        workflow_id=workflow_id,
        total_cost_usd=round(total_cost, 6),
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        call_count=len(rows),
        by_agent={k: round(v, 6) for k, v in by_agent.items()},
        by_model={k: round(v, 6) for k, v in by_model.items()},
    )


@dataclass
class EconomicsSummary:
    """Aggregate spend snapshot for the economics summary endpoint."""
    today_usd: float
    last_7d_usd: float
    last_30d_usd: float
    by_model_30d: dict[str, float]
    by_agent_30d: dict[str, float]
    call_count_30d: int
    total_input_tokens_30d: int
    total_output_tokens_30d: int


async def get_summary() -> EconomicsSummary:
    """
    Top-level spend snapshot: today, 7d, 30d totals + 30d breakdowns.

    The dashboard's primary cost card consumes this single endpoint.
    """
    now = datetime.now(timezone.utc)
    today_start = _utc_today_start()
    d7_start = today_start - timedelta(days=6)   # inclusive of today = 7 days
    d30_start = today_start - timedelta(days=29) # inclusive of today = 30 days

    try:
        async with _new_session() as session:
            # 30d window — pull all rows once and bucket in Python.
            # At expected volumes (≤ 10k calls/30d) this is faster than 5 round-trips.
            stmt = select(LLMCallLog).where(LLMCallLog.created_at >= d30_start)
            rows = (await session.execute(stmt)).scalars().all()
    except Exception as exc:
        logger.warning("economics_summary_query_failed | error=%s", exc)
        rows = []

    today = 0.0
    d7 = 0.0
    d30 = 0.0
    by_model: dict[str, float] = defaultdict(float)
    by_agent: dict[str, float] = defaultdict(float)
    in_tok = 0
    out_tok = 0
    for r in rows:
        d30 += r.cost_usd
        by_model[r.model] += r.cost_usd
        by_agent[r.agent_type] += r.cost_usd
        in_tok += r.input_tokens
        out_tok += r.output_tokens
        if r.created_at >= d7_start:
            d7 += r.cost_usd
        if r.created_at >= today_start:
            today += r.cost_usd

    return EconomicsSummary(
        today_usd=round(today, 6),
        last_7d_usd=round(d7, 6),
        last_30d_usd=round(d30, 6),
        by_model_30d={k: round(v, 6) for k, v in by_model.items()},
        by_agent_30d={k: round(v, 6) for k, v in by_agent.items()},
        call_count_30d=len(rows),
        total_input_tokens_30d=in_tok,
        total_output_tokens_30d=out_tok,
    )


@dataclass
class DailyPoint:
    date: str            # ISO YYYY-MM-DD (UTC)
    cost_usd: float
    call_count: int


async def get_daily_timeseries(days: int = 30) -> list[DailyPoint]:
    """
    Per-UTC-day totals for the last `days` days. Fills zeros for missing days
    so the FE chart axis is dense.
    """
    days = max(1, min(days, 365))
    today_start = _utc_today_start()
    window_start = today_start - timedelta(days=days - 1)

    try:
        async with _new_session() as session:
            stmt = select(LLMCallLog).where(LLMCallLog.created_at >= window_start)
            rows = (await session.execute(stmt)).scalars().all()
    except Exception as exc:
        logger.warning("daily_timeseries_query_failed | error=%s", exc)
        rows = []

    buckets: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])  # [cost, count]
    for r in rows:
        key = r.created_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        buckets[key][0] += r.cost_usd
        buckets[key][1] += 1

    points: list[DailyPoint] = []
    for i in range(days):
        d = window_start + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        cost, count = buckets.get(key, [0.0, 0])
        points.append(DailyPoint(date=key, cost_usd=round(cost, 6), call_count=int(count)))
    return points
