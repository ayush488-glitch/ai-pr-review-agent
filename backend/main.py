# backend/main.py
#
# The FastAPI application entry point.
#
# This file does three things:
#   1. Creates the FastAPI app instance with metadata
#   2. Registers all routers (webhook, and later: reviews, hitl, auth, etc.)
#   3. Defines startup and shutdown lifecycle events
#
# HOW TO RUN:
#   uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
#
#   --reload    auto-restarts when you change a file (development only)
#   --host 0.0.0.0  accessible from outside localhost (needed for webhook tunnels)
#   --port 8000     the port the app listens on
#
# ONCE RUNNING:
#   API docs (Swagger UI): http://localhost:8000/docs
#   Alternative docs:      http://localhost:8000/redoc
#   Health check:          http://localhost:8000/health
#
# FIX (Design by Contract / Don't rely on deprecated APIs):
#   @app.on_event("startup") was deprecated in FastAPI 0.93.
#   The modern pattern is a lifespan context manager.
#   It uses a single async function with a yield — code before yield runs at
#   startup, code after yield runs at shutdown. Easier to reason about,
#   supported by pytest-asyncio fixtures, and won't break on FastAPI upgrades.
#
# FIX (DRY — single source of truth for version):
#   Version used to be hardcoded as "0.1.0" here AND in README.
#   Now it is read from pyproject.toml via importlib.metadata.
#   When we bump the version in pyproject.toml, all places update automatically.

import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config.settings import get_settings
from backend.database.postgres import init_db
from backend.memory.context_retriever import retrieve_context_for_diff  # noqa: F401 (used in routes)
from backend.database.postgres import init_tiger_schema  # TIGER: replaces ensure_collection
from backend.memory.redis_client import redis_client
from backend.webhook_receiver.router import router as webhook_router

# Phase 3 REST API routers
from backend.api.reviews import router as reviews_router
from backend.api.queue import router as queue_router
from backend.api.hitl_router import hitl_router       # Phase 19: HITL queue + dispute
from backend.api.economics_router import economics_router  # Phase 16: cost & budget

# Phase 12 circuit breaker registry — surfaced in /health
from backend.reliability.circuit_breaker import list_breaker_summaries

# -------------------------------------------------------------------------
# Read version from pyproject.toml (single source of truth)
# PackageNotFoundError happens in development before the package is installed.
# In that case we fall back to "dev". In production (installed via pip/uv) it
# reads the real version from the package metadata.
# -------------------------------------------------------------------------
try:
    APP_VERSION = version("ai-pr-review-agent")
except PackageNotFoundError:
    APP_VERSION = "dev"

# -------------------------------------------------------------------------
# Configure the root logger once, here, before anything else runs.
# All modules use logging.getLogger(__name__) — they inherit this config.
# -------------------------------------------------------------------------
settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Lifespan context manager
#
# Everything BEFORE yield runs at startup (before accepting requests).
# Everything AFTER yield runs at shutdown (after the last request finishes).
#
# This replaces the deprecated @app.on_event("startup") / ("shutdown").
# The advantage: startup and shutdown are co-located in one function.
# You can see paired open/close operations next to each other.
#
# Phase 4 will add: Redis connection pool (open before yield, close after)
# Phase 6 will add: Qdrant client init (open before yield, close after)
# -------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    cfg = get_settings()
    logger.info(
        "AI PR Review Agent starting | version=%s env=%s log_level=%s",
        APP_VERSION,
        cfg.app_env,
        cfg.log_level,
    )
    logger.info("Max concurrent reviews: %d", cfg.max_concurrent_reviews)
    logger.info("Confidence threshold:   %.2f", cfg.confidence_threshold)
    logger.info("Workflow timeout:       %ds", cfg.workflow_timeout_seconds)

    # Phase 4: Connect Redis
    # redis_client is a module-level singleton (RedisClient instance).
    # connect() creates the connection pool and pings Redis to confirm it's up.
    #
    # WHY try/except here (not fail-fast):
    #   On Railway, the container must answer /health/live before env vars are
    #   fully propagated or Upstash accepts the first connection. If Redis is
    #   temporarily unreachable at cold boot, we log a warning and continue.
    #   Job submissions will fail gracefully (circuit breaker) until Redis is up.
    #   This is safer than crashing the container and retrying from scratch.
    try:
        await redis_client.connect()
        logger.info("Redis connection pool ready.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis unavailable at startup — job queue degraded: %s", exc)

    # Phase 6: Postgres — create tables via ORM
    #
    # init_db() runs create_all() on the async SQLAlchemy engine.
    # In production this creates tables if they don't exist (safe to call on restart).
    #
    # WHY try/except here (not fail-fast):
    #   Neon serverless Postgres has a ~30s cold start after idle.
    #   On Railway's first boot both the container AND Neon may be waking up
    #   simultaneously. Crashing startup forces a full container restart which
    #   hits Neon again before it's ready — a retry death spiral.
    #   Instead we log a warning and let the /health endpoint surface the error.
    #   The first real request that needs Postgres will retry via SQLAlchemy pool.
    # TODO: Replace with Alembic migrations before v1.0.
    try:
        await init_db()
        logger.info("Postgres tables verified/created.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Postgres unavailable at startup — will retry on first request: %s", exc)

    # -------------------------------------------------------------------------
    # Tiger Cloud — init connection pool + run schema migration
    #
    # init_tiger_schema() creates the asyncpg pool, registers pgvector codec,
    # runs the idempotent DDL from scripts/migrations/2026-06-tiger-init.sql,
    # and wires the TigerMemoryClient singleton.
    #
    # WHY BEST-EFFORT (try/except, not fail-fast):
    #   Tiger Cloud is the memory + events spine, but reviews can technically
    #   run diff-only if Tiger is temporarily unreachable at cold boot.
    #   We log the warning and let the /health endpoint surface the error.
    #   (Production-Hardening.md: "Optional deps log warning, never crash startup.")
    try:
        await init_tiger_schema()
        logger.info("Tiger Cloud ready — memory + events spine online.")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Tiger Cloud unavailable at startup — RAG context and events disabled: %s", exc
        )

    yield  # <-- server is running, accepting requests

    # --- SHUTDOWN ---
    logger.info("AI PR Review Agent shutting down.")

    # Phase 4: Disconnect Redis
    # aclose() waits for in-flight operations to finish, then closes the pool.
    await redis_client.disconnect()


# -------------------------------------------------------------------------
# Create the FastAPI app
# -------------------------------------------------------------------------
app = FastAPI(
    title="AI PR Review Agent",
    description=(
        "A production-grade AI agent that reviews GitHub Pull Requests automatically. "
        "Runs specialist sub-agents in parallel for security, quality, tests, and docs."
    ),
    version=APP_VERSION,
    lifespan=lifespan,
    # In production, hide /docs and /redoc — they expose your API structure.
    docs_url="/docs" if settings.is_development else None,
    redoc_url="/redoc" if settings.is_development else None,
)


# -------------------------------------------------------------------------
# CORS Middleware
#
# Controls which browser origins can call our API.
# Without CORS headers, the frontend dashboard (different port/domain)
# gets blocked by the browser's same-origin policy.
#
# Development: allow all origins (convenient, not for production).
# Production:  restrict to the actual dashboard domain.
# -------------------------------------------------------------------------
if settings.is_development:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://your-dashboard-domain.com"],  # TODO: Phase 13 sets this
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


# -------------------------------------------------------------------------
# Register Routers
#
# Each router owns a group of related endpoints.
# Add routers here as modules are built.
# -------------------------------------------------------------------------

app.include_router(webhook_router)        # POST /webhook/github

# Phase 3 REST API routers
app.include_router(reviews_router)        # GET /api/v1/reviews, GET /api/v1/reviews/{id}
app.include_router(queue_router)          # GET /api/v1/queue
app.include_router(hitl_router)           # Phase 19: GET /api/v1/hitl/queue, POST /api/v1/hitl/{id}/decision
app.include_router(economics_router)      # Phase 16: GET /api/v1/economics/{summary,budget,timeseries,workflow/...}

# TODO: add as phases progress
# app.include_router(auth_router)         # POST /api/v1/auth/login, GET /api/v1/auth/me (Phase 11)


# -------------------------------------------------------------------------
# Health Check — deep (Phase 13)
#
# TWO LEVELS of health endpoints:
#
#   GET /health/live  — liveness probe (k8s: is the process alive?)
#                       Always 200 if the process is up. No external calls.
#                       Kubernetes restarts the pod if this fails.
#
#   GET /health       — readiness probe (k8s: can this pod serve traffic?)
#                       Checks Postgres, Redis, Qdrant reachability.
#                       Returns 200 (all ok) or 503 (degraded).
#                       Load balancers remove the pod from rotation if 503.
#
# release-it/Operations-Patterns: "Don't accept connections until start-up
# is complete." The readiness probe enforces this — traffic only lands on
# a pod that can actually process it.
# -------------------------------------------------------------------------

@app.get(
    "/health/live",
    tags=["ops"],
    summary="Liveness probe",
    description="Returns 200 if the process is running. No dependency checks.",
)
async def liveness() -> dict:
    """Liveness: always 200 while the process is alive."""
    return {"status": "ok", "version": APP_VERSION}


async def _check_postgres() -> str:
    """
    Attempt a trivial async SELECT 1 against Postgres.
    Returns "ok" or an error string. Never raises.
    """
    try:
        from backend.database.postgres import get_engine
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from sqlalchemy import text
        factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


async def _check_redis() -> str:
    """Ping Redis. Returns 'ok' or an error string. Never raises."""
    try:
        pong = await redis_client.ping()
        return "ok" if pong else "error: no PONG response"
    except Exception as exc:
        return f"error: {exc}"


async def _check_tiger() -> str:
    """
    Check Tiger Cloud connection health via TigerMemoryClient.health_check().
    Returns 'ok' or an error string. Never raises.
    """
    try:
        from backend.memory.tiger_client import get_tiger_memory
        client = get_tiger_memory()
        result = await client.health_check()
        if result.get("status") == "ok":
            return f"ok (chunks={result.get('chunk_count', 0)})"
        return f"degraded: {result.get('error', 'unknown')}"
    except Exception as exc:
        return f"error: {exc}"


@app.get(
    "/health",
    tags=["ops"],
    summary="Readiness probe",
    description=(
        "Checks Postgres, Redis, and Tiger Cloud reachability. "
        "Returns 200 when all services are healthy, 503 when any are degraded."
    ),
)
async def health_check() -> JSONResponse:
    """
    Deep readiness check.

    Response shape:
      {
        "status": "ok" | "degraded",
        "version": "0.13.0",
        "env": "development",
        "services": {
          "postgres": "ok" | "error: ...",
          "redis":    "ok" | "error: ...",
          "tiger":    "ok" | "degraded ..." | "error: ..."
        },
        "circuit_breakers": [
          {"name": "...", "state": "CLOSED", "failures": 0}, ...
        ]
      }

    HTTP status: 200 if status == "ok", 503 if "degraded".
    """
    postgres_status = await _check_postgres()
    redis_status = await _check_redis()
    tiger_status = await _check_tiger()

    cfg = get_settings()

    # "degraded" if any hard dependency (Postgres, Redis) is unhealthy.
    # Qdrant failure is soft — reviews still run, just without RAG context.
    hard_ok = (
        postgres_status == "ok"
        and redis_status == "ok"
    )
    overall = "ok" if hard_ok else "degraded"

    body: dict[str, Any] = {
        "status": overall,
        "version": APP_VERSION,
        "env": cfg.app_env,
        "services": {
            "postgres": postgres_status,
            "redis": redis_status,
            "tiger": tiger_status,
        },
        "circuit_breakers": list_breaker_summaries(),
    }

    http_status = 200 if overall == "ok" else 503
    return JSONResponse(content=body, status_code=http_status)
