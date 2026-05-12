# backend/memory/redis_client.py
#
# Redis client — the ONLY place in the codebase that talks to Redis directly.
#
# WHY A WRAPPER INSTEAD OF USING redis DIRECTLY?
# Without this wrapper, any module that needs Redis would:
#   1. Create its own connection
#   2. Know the Redis URL (config coupling)
#   3. Handle connection errors on its own
#   4. Need to be updated if we switch Redis libraries
#
# With this wrapper:
#   - ONE connection pool, shared across all uses (efficient)
#   - ONE place to handle connection errors
#   - The rest of the codebase calls: await redis_client.check_idempotency_key("...")
#     It does not know or care that Redis is involved
#
# LAW OF DEMETER APPLIED:
# The router does not call redis.get(key). It calls check_idempotency_key(key).
# The router only talks to its immediate neighbor (this client).
# It does not reach through to the Redis library.
#
# FROM Stream-Processing-Patterns.md (DDIA wiki):
# "Use a durable message broker, not polling a DB."
# Redis with LPUSH/BRPOP is the durable broker pattern — not polling.
# Our idempotency keys use Redis SETEX (set with expiry) — fast, atomic, durable.
#
# FROM Stability Patterns (release-it wiki):
# "Every external call is a potential stab-in-the-back."
# Every public method here wraps its Redis call in try/except.
# Callers get a clean error (MemoryStoreError) not a raw redis exception.

import logging
from typing import Any

import redis.asyncio as aioredis

from backend.config import get_settings
from backend.core import MemoryStoreError

logger = logging.getLogger(__name__)

# How long an idempotency key lives in Redis (in seconds).
# After this, the key expires and the same PR could theoretically be reviewed again.
# 24 hours is long enough to cover all GitHub webhook retry windows.
IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24  # 24 hours

# How long a workflow checkpoint lives in Redis.
# After this, resume() will not find the checkpoint.
# 7 days gives plenty of time for manual investigation of stuck reviews.
CHECKPOINT_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


class RedisClient:
    """
    Thin wrapper around the async Redis client.

    Exposes only the operations this system needs:
      - idempotency key management (deduplication)
      - job queue operations (Phase 4 job runner)
      - workflow state caching (quick status checks)

    Does NOT expose raw Redis commands to the rest of the codebase.
    If a new operation is needed, add a named method here with a docstring.

    LIFECYCLE:
      Call connect() once at startup (in main.py lifespan).
      Call disconnect() at shutdown.
      All other methods are safe to call between those two points.
    """

    def __init__(self) -> None:
        self._pool: aioredis.Redis | None = None

    async def connect(self) -> None:
        """
        Creates the Redis connection pool.

        Called once at application startup (main.py lifespan, before yield).
        Uses a connection pool (not a single connection) so concurrent
        coroutines can each grab a connection without blocking each other.

        STABILITY PATTERN: includes a health check ping after connecting.
        If Redis is unreachable at startup, we fail fast with a clear error
        rather than discovering it on the first request.
        """
        cfg = get_settings()
        try:
            self._pool = aioredis.from_url(
                cfg.redis_url,
                encoding="utf-8",
                decode_responses=True,
                # Connection pool settings:
                max_connections=20,     # max 20 concurrent Redis connections
                socket_timeout=5.0,     # fail after 5s if Redis doesn't respond
                socket_connect_timeout=5.0,
                retry_on_timeout=True,  # auto-retry once on timeout
            )
            # Health check: ping Redis to confirm it is reachable.
            await self._pool.ping()
            logger.info("Redis connected: %s", cfg.redis_url)
        except Exception as e:
            raise MemoryStoreError(
                f"Could not connect to Redis at {cfg.redis_url}: {e}",
                store="redis",
            ) from e

    async def disconnect(self) -> None:
        """
        Closes the Redis connection pool.

        Called at application shutdown (main.py lifespan, after yield).
        Waits for all pending operations to complete before closing.
        """
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
            logger.info("Redis disconnected.")

    def _require_connected(self) -> aioredis.Redis:
        """
        Returns the pool, or raises if connect() was never called.

        DESIGN BY CONTRACT:
          Precondition: connect() must have been called successfully.
          If not: raises MemoryStoreError immediately (fail fast, not silently).

        Called at the start of every public method to enforce the contract.
        """
        if self._pool is None:
            raise MemoryStoreError(
                "RedisClient.connect() was never called. "
                "Did you forget to initialize Redis in the lifespan handler?",
                store="redis",
            )
        return self._pool

    # -------------------------------------------------------------------------
    # Idempotency Key Operations
    #
    # These prevent a PR from being reviewed twice when GitHub replays a webhook.
    # The key = idempotency_key from WebhookEvent (repo:pr_number:commit_sha).
    # We SET the key when we enqueue a job. We CHECK it when we receive a webhook.
    # If the key exists: we already queued this review -> return 200 silently.
    # -------------------------------------------------------------------------

    async def set_idempotency_key(self, key: str) -> None:
        """
        Marks a review job as queued by setting its idempotency key.

        Uses SETEX (set + expire in one atomic operation).
        Atomic = no race condition where two simultaneous webhooks both
        think the key doesn't exist yet.

        Args:
            key: the idempotency key from WebhookEvent.idempotency_key
                 Format: "{repo_full_name}:{pr_number}:{head_commit_sha}"

        Raises:
            MemoryStoreError: if the Redis operation fails.
        """
        pool = self._require_connected()
        redis_key = f"idempotency:{key}"
        try:
            await pool.setex(redis_key, IDEMPOTENCY_TTL_SECONDS, "queued")
            logger.debug("idempotency_key_set | key=%s", redis_key)
        except Exception as e:
            raise MemoryStoreError(
                f"Failed to set idempotency key '{redis_key}': {e}",
                store="redis",
            ) from e

    async def check_idempotency_key(self, key: str) -> bool:
        """
        Returns True if this review job was already queued.

        Args:
            key: the idempotency key from WebhookEvent.idempotency_key

        Returns:
            True  = key exists = job already queued = skip this webhook
            False = key does not exist = first time we've seen this PR + commit

        Raises:
            MemoryStoreError: if the Redis operation fails.
        """
        pool = self._require_connected()
        redis_key = f"idempotency:{key}"
        try:
            exists = await pool.exists(redis_key)
            return bool(exists)
        except Exception as e:
            raise MemoryStoreError(
                f"Failed to check idempotency key '{redis_key}': {e}",
                store="redis",
            ) from e

    # -------------------------------------------------------------------------
    # Workflow Status Cache
    #
    # Quick key-value store for "what is review X doing right now?"
    # Used by the dashboard API to show live status without hitting Postgres.
    # These are short-lived (expire after workflow completes + buffer time).
    # -------------------------------------------------------------------------

    async def set_workflow_status(self, workflow_id: str, status: str) -> None:
        """
        Caches the current status of a workflow for fast dashboard reads.

        Args:
            workflow_id: the workflow's unique ID
            status: string value of ReviewStatus enum (e.g. "agents_running")
        """
        pool = self._require_connected()
        redis_key = f"workflow:status:{workflow_id}"
        try:
            # 2 hour TTL: long enough to cover any review, short enough to not
            # clutter Redis with old statuses
            await pool.setex(redis_key, 60 * 60 * 2, status)
        except Exception as e:
            # Status cache is best-effort. Log and continue — don't fail the review.
            logger.warning(
                "Failed to cache workflow status | workflow_id=%s error=%s",
                workflow_id,
                str(e),
            )

    async def get_workflow_status(self, workflow_id: str) -> str | None:
        """
        Returns the cached status of a workflow, or None if not found.
        """
        pool = self._require_connected()
        redis_key = f"workflow:status:{workflow_id}"
        try:
            return await pool.get(redis_key)
        except Exception as e:
            logger.warning(
                "Failed to read workflow status | workflow_id=%s error=%s",
                workflow_id,
                str(e),
            )
            return None

    # -------------------------------------------------------------------------
    # Health Check
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Review Status Cache (added Phase 6)
    #
    # Cache-aside pattern for review status.
    # (From Polyglot-Persistence.md wiki, "Caching Patterns"):
    #   "Read from cache first. On miss, read from Postgres. Write back to cache."
    #   Redis = fast O(1) key-value lookup for "what is review X doing now?"
    #   Postgres = the durable source of truth for the full review record.
    #   The cache holds the current status string only — minimal data, short TTL.
    #
    # TTL = 300 seconds (5 minutes).
    # Review statuses are short-lived:
    #   pending -> in_progress -> agents_running -> aggregating -> posting -> completed
    # A typical review completes in <60 seconds. 300s is plenty before TTL expiry.
    # After TTL, the next read goes to Postgres (cache miss -> fall through to DB).
    # Stale cache is acceptable: showing "in_progress" for an extra few minutes
    # is not a correctness issue — it is a display convenience.
    # (Polyglot-Persistence.md: "eventual consistency in the cache layer is acceptable
    #  when the primary store is always authoritative.")
    # -------------------------------------------------------------------------

    async def cache_review_status(
        self, review_id: str, status: str, ttl: int = 300
    ) -> None:
        """
        Caches the current status of a review for fast dashboard reads.

        PATTERN: Cache-aside write path.
        The orchestrator calls this after each status transition.
        Callers read via get_cached_review_status() first; on miss, they
        fall through to Postgres.

        This is BEST-EFFORT: failures are logged but not re-raised.
        A failed cache write means the next read goes to Postgres — no data loss.

        Args:
            review_id: UUID of the review (matches PRReviewRecord.id)
            status:    ReviewStatus enum value as string
            ttl:       TTL in seconds (default 300 = 5 minutes)
        """
        pool = self._require_connected()
        redis_key = f"review:status:{review_id}"
        try:
            await pool.setex(redis_key, ttl, status)
            logger.debug(
                "cache_review_status | review_id=%s status=%s ttl=%ds",
                review_id, status, ttl,
            )
        except Exception as e:
            # Best-effort: log and continue (do not fail the review on cache miss)
            logger.warning(
                "cache_review_status | failed | review_id=%s error=%s",
                review_id, str(e),
            )

    async def get_cached_review_status(self, review_id: str) -> str | None:
        """
        Returns the cached status of a review, or None if not found/expired.

        PATTERN: Cache-aside read path.
        Returns None on cache miss — caller falls through to Postgres.
        Returns the cached status string on hit.

        Args:
            review_id: UUID of the review

        Returns:
            Status string (e.g. "in_progress") or None on cache miss.
            Never raises — returns None on any Redis error.
        """
        pool = self._require_connected()
        redis_key = f"review:status:{review_id}"
        try:
            value = await pool.get(redis_key)
            logger.debug(
                "get_cached_review_status | review_id=%s cache_%s",
                review_id, "hit" if value else "miss",
            )
            return value
        except Exception as e:
            logger.warning(
                "get_cached_review_status | failed | review_id=%s error=%s",
                review_id, str(e),
            )
            return None

    async def ping(self) -> bool:
        """
        Returns True if Redis is reachable, False otherwise.

        Used by the /health endpoint (Phase 13 will add Redis to health checks).
        Never raises — returns False on any error.
        """
        if self._pool is None:
            return False
        try:
            await self._pool.ping()
            return True
        except Exception:
            return False


# Module-level singleton.
# Initialized (connect()) in main.py lifespan startup.
# All modules import this instance:
#   from backend.memory.redis_client import redis_client
redis_client = RedisClient()
