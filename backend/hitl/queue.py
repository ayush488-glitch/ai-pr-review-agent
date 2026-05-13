# backend/hitl/queue.py
#
# HITL Queue — Phase 19.
#
# RESPONSIBILITY:
#   Enqueue escalated reviews into the HITL queue and dequeue them for
#   human processing.
#
# STORAGE DESIGN (Polyglot-Persistence.md + Derived-Data-Systems.md wiki):
#
#   Postgres (system of record):
#     HITLReview row is written FIRST, before Redis.
#     Status: "pending" until human acts.
#     If Redis is lost, the queue can be rebuilt from:
#       SELECT * FROM hitl_reviews WHERE status = 'pending'
#
#   Redis (derived, ephemeral):
#     Key: "hitl:queue"  — Redis LIST (LPUSH on enqueue, BRPOP on dequeue)
#     Each item is the hitl_review.id (UUID string).
#     TTL: none — Redis LIST is persistent until explicitly removed.
#     Purpose: fast O(1) enqueue/dequeue without polling Postgres.
#
#   WHY BOTH? (Stability-Patterns.md wiki — "View other systems with suspicion"):
#     Redis free tier can be lost (Upstash eviction, Railway Redis failures).
#     Postgres is the durable safety net.
#     Redis is the fast operational path.
#     They stay in sync via the enqueue/dequeue contract:
#       enqueue: Postgres first, Redis second.
#       rebuild: if Redis queue is empty, re-hydrate from Postgres pending rows.
#
# NOTIFICATION:
#   Slack webhook called AFTER Postgres+Redis write succeeds.
#   (demo-day-readiness Bug #5 pattern: save first, notify second.)
#   Slack failure must NOT fail the enqueue — wrapped in try/except.
#   (Stability-Patterns.md: "External dependencies will stab you in the back.")
#
# DEMO-DAY PITFALL (Transactions-and-Isolation.md wiki):
#   enqueue() is NOT idempotent by design. If the same workflow_id is enqueued
#   twice (e.g. webhook retry), two HITLReview rows are created.
#   The deduplication guard lives in the caller (post_review node checks
#   existing HITLReview rows before calling enqueue).
#   This matches the idempotency-at-enqueue-layer principle (demo-day-readiness Bug #4).

import json
import logging
import os
from datetime import datetime, timezone

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import HITLReview
from backend.database.postgres import get_session_factory

logger = logging.getLogger(__name__)

REDIS_QUEUE_KEY = "hitl:queue"


# ---------------------------------------------------------------------------
# enqueue_hitl_review
#
# Called by: backend/orchestrator/nodes.py (post_review node, HITL branch)
#
# Contract:
#   1. Write HITLReview row to Postgres (system of record) — FIRST.
#   2. Push UUID to Redis list — SECOND (derived).
#   3. Notify Slack — THIRD (best-effort, must not raise).
#   Returns the newly created HITLReview.id on success.
#
# (Production-Hardening.md: "Persistence must always happen, regardless of
#  downstream delivery outcome." — same principle as save-before-post.)
# ---------------------------------------------------------------------------
async def enqueue_hitl_review(
    *,
    redis_client: Redis,
    review_id: str,
    repo_full_name: str,
    pr_number: int,
    agent_verdict: str,
    escalation_reason: str,
    findings_snapshot: list[dict],
    overall_confidence: float,
) -> str:
    """
    Persist an escalated review to the HITL queue.

    Steps:
      1. Write HITLReview to Postgres.
      2. Push to Redis list.
      3. Notify Slack (optional, best-effort).

    Returns:
        hitl_review_id: str — the UUID of the newly created HITLReview row.

    Raises:
        Exception if Postgres write fails (non-recoverable, caller should log+fail).
        Never raises for Redis or Slack failures (degraded mode only).
    """
    # Serialize findings for storage.
    # (Polyglot-Persistence.md: "self-contained document — one query sufficient.")
    findings_json = json.dumps(findings_snapshot, default=str)

    # --- Step 1: Postgres (system of record) ---
    # Must succeed before we touch Redis.
    # Using get_session_factory() directly (NOT get_db()) because we are NOT
    # inside a FastAPI request context here — we are called from the ARQ worker.
    # (demo-day-readiness pitfall #2: async with get_db() fails if get_db uses yield)
    session_factory = get_session_factory()
    hitl_review_id: str = ""

    async with session_factory() as session:
        async with session.begin():
            # Build HITLReview row.
            hitl_review = HITLReview(
                review_id=review_id,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                agent_verdict=agent_verdict,
                escalation_reason=escalation_reason,
                findings_snapshot=findings_json,
                overall_confidence=overall_confidence,
                status="pending",
            )
            session.add(hitl_review)
            # Flush inside the transaction to get the generated UUID.
            await session.flush()
            hitl_review_id = hitl_review.id

    logger.info(
        "hitl_queue | enqueued_to_postgres | hitl_id=%s review_id=%s repo=%s pr=%d",
        hitl_review_id, review_id, repo_full_name, pr_number,
    )

    # --- Step 2: Redis (derived, ephemeral) ---
    # Push the UUID onto the Redis list. If Redis fails, the item is still
    # safe in Postgres. The queue can be rebuilt from Postgres if needed.
    # (Stability-Patterns.md: "Staying up is more than half the battle.")
    try:
        await redis_client.lpush(REDIS_QUEUE_KEY, hitl_review_id)
        logger.info(
            "hitl_queue | pushed_to_redis | hitl_id=%s key=%s",
            hitl_review_id, REDIS_QUEUE_KEY,
        )
    except Exception as redis_err:
        # Non-fatal: the item is persisted in Postgres. The GET /hitl/queue
        # endpoint falls back to Postgres when Redis is unavailable.
        logger.warning(
            "hitl_queue | redis_push_failed | hitl_id=%s error=%s | "
            "item safe in postgres, queue degraded",
            hitl_review_id, redis_err,
        )

    # --- Step 3: Slack notification (best-effort) ---
    # (Stability-Patterns.md: "External dependencies will stab you in the back.")
    await _notify_slack(
        hitl_review_id=hitl_review_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        agent_verdict=agent_verdict,
        escalation_reason=escalation_reason,
        overall_confidence=overall_confidence,
    )

    return hitl_review_id


# ---------------------------------------------------------------------------
# get_pending_queue
#
# Called by: backend/api/hitl_router.py (GET /api/v1/hitl/queue)
#
# Returns all pending HITLReview rows from Postgres.
# Postgres is the source of truth — Redis is the fast enqueue path but
# the queue listing always reads from Postgres for consistency.
# (Derived-Data-Systems.md: "If there is discrepancy, Postgres wins.")
# ---------------------------------------------------------------------------
async def get_pending_queue(
    session: AsyncSession,
    *,
    repo_full_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[HITLReview]:
    """
    Return pending (unresolved) HITL items from Postgres.

    Args:
        session:        AsyncSession from FastAPI Depends(get_db).
        repo_full_name: Optional filter — only items for this repo.
        limit:          Max rows to return (pagination).
        offset:         Row offset (pagination).

    Returns:
        List of HITLReview ORM objects with status in ('pending', 'in_review').
    """
    stmt = (
        select(HITLReview)
        .where(HITLReview.status.in_(["pending", "in_review"]))
        .order_by(HITLReview.created_at.asc())   # oldest first = FIFO queue semantics
        .limit(limit)
        .offset(offset)
    )

    if repo_full_name:
        stmt = stmt.where(HITLReview.repo_full_name == repo_full_name)

    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# get_hitl_review
#
# Fetch a single HITLReview by ID. Used by the dispute handler and the
# GET /hitl/{id} detail endpoint.
# ---------------------------------------------------------------------------
async def get_hitl_review(
    session: AsyncSession,
    hitl_review_id: str,
) -> HITLReview | None:
    """
    Fetch a single HITLReview row by its UUID.

    Returns None if not found (caller decides 404 vs raise).
    """
    result = await session.execute(
        select(HITLReview).where(HITLReview.id == hitl_review_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# rebuild_redis_queue
#
# Re-hydrate the Redis queue from Postgres pending rows.
# Called on startup or when Redis queue appears empty but Postgres has
# pending items (Redis was lost / evicted).
#
# (Derived-Data-Systems.md: "If you lose derived data, re-create from source.")
# ---------------------------------------------------------------------------
async def rebuild_redis_queue(
    session: AsyncSession,
    redis_client: Redis,
) -> int:
    """
    Re-populate Redis list from Postgres pending HITLReview rows.

    Returns the number of items pushed to Redis.
    Safe to call repeatedly — checks Redis length first and skips if non-empty.
    """
    # Check if Redis queue already has items.
    try:
        queue_len = await redis_client.llen(REDIS_QUEUE_KEY)
        if queue_len > 0:
            logger.debug(
                "hitl_queue | rebuild_skipped | redis_len=%d (already populated)",
                queue_len,
            )
            return 0
    except Exception as err:
        logger.warning("hitl_queue | redis_llen_failed | error=%s", err)
        return 0

    # Fetch pending rows from Postgres.
    result = await session.execute(
        select(HITLReview.id)
        .where(HITLReview.status.in_(["pending", "in_review"]))
        .order_by(HITLReview.created_at.asc())
    )
    pending_ids = [row[0] for row in result.all()]

    if not pending_ids:
        return 0

    # Push all pending IDs to Redis.
    try:
        # RPUSH to preserve FIFO order (oldest = rightmost = dequeued first via BRPOP).
        await redis_client.rpush(REDIS_QUEUE_KEY, *pending_ids)
        logger.info(
            "hitl_queue | rebuild_complete | pushed=%d items to redis",
            len(pending_ids),
        )
        return len(pending_ids)
    except Exception as err:
        logger.warning("hitl_queue | rebuild_redis_push_failed | error=%s", err)
        return 0


# ---------------------------------------------------------------------------
# _notify_slack  (private helper)
#
# Best-effort Slack notification when a review enters the HITL queue.
# Uses SLACK_WEBHOOK_URL env var. If not set, silently skips.
# If the webhook call fails, logs a warning and continues.
#
# (Stability-Patterns.md: "External dependencies will stab you in the back.")
# (demo-day-readiness Bug #5 pattern: save first, notify second, never let
#  notification failure block persistence.)
# ---------------------------------------------------------------------------
async def _notify_slack(
    *,
    hitl_review_id: str,
    repo_full_name: str,
    pr_number: int,
    agent_verdict: str,
    escalation_reason: str,
    overall_confidence: float,
) -> None:
    """
    Post a Slack message to SLACK_WEBHOOK_URL if configured.

    Non-raising: all exceptions are caught and logged as warnings.
    """
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not slack_url:
        logger.debug("hitl_queue | slack_notify_skipped | SLACK_WEBHOOK_URL not set")
        return

    message = {
        "text": (
            f":warning: *HITL Review Required* — `{repo_full_name}` PR #{pr_number}\n"
            f"*Agent verdict:* `{agent_verdict}` | "
            f"*Confidence:* `{overall_confidence:.0%}`\n"
            f"*Reason:* {escalation_reason}\n"
            f"*Review ID:* `{hitl_review_id}`"
        )
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(slack_url, json=message)
            if resp.status_code == 200:
                logger.info(
                    "hitl_queue | slack_notified | hitl_id=%s status=200",
                    hitl_review_id,
                )
            else:
                logger.warning(
                    "hitl_queue | slack_notify_failed | hitl_id=%s status=%d body=%s",
                    hitl_review_id, resp.status_code, resp.text[:200],
                )
    except Exception as err:
        # Non-fatal. The item is already in Postgres.
        logger.warning(
            "hitl_queue | slack_notify_exception | hitl_id=%s error=%s",
            hitl_review_id, err,
        )
