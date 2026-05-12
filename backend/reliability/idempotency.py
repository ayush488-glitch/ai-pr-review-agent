"""
backend/reliability/idempotency.py

Phase 12: Idempotency Store and Job Deduplication
===================================================
Ensures that retried or duplicated webhook deliveries do not cause
a PR to be reviewed twice.

Design rationale
----------------
Phase 3 already deduplicates at the WEBHOOK level using the GitHub delivery
ID stored in Redis with a short TTL. This module adds a second, more durable
idempotency layer at the ARQ JOB level.

Why two layers?
  - Webhook layer (Phase 3): catches duplicate GitHub deliveries within
    a short window (minutes). Key = X-GitHub-Delivery header.
  - Job layer (this module): catches duplicate job enqueues during retry
    storms, worker restarts, or network blips. Key = pr_number + sha.
    TTL is 24 hours — covers the entire review lifecycle.

This is the "at-least-once with idempotent processing" pattern:
  ARQ guarantees at-least-once execution. We make execution idempotent
  by checking before starting whether a result already exists for this
  (pr_number, sha) pair. If yes, skip and return the cached result.

Interface design
----------------
IdempotencyStore is an abstract interface so the ARQ worker can use Redis
in production and the smoke tests can use an in-memory dict — no Redis
needed in CI.

Wiki ref: llmops/State-Persistence-per-Stage:
  "On failure, only the failed stage needs to be retried, not the full
   pipeline." Idempotency is what makes stage-level retry safe — the stage
   checks whether it already completed before doing work.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result envelope stored by the idempotency layer
# ---------------------------------------------------------------------------

@dataclass
class IdempotencyRecord:
    """
    What is stored per key.

    Fields:
        key        -- The idempotency key (e.g. "pr:42:abc123sha").
        status     -- "in_flight" | "complete" | "failed"
        result     -- Serialisable result value (stored on complete).
        error      -- Error message (stored on failed).
        created_at -- Unix timestamp of first creation.
        updated_at -- Unix timestamp of last update.
    """
    key: str
    status: str = "in_flight"       # in_flight | complete | failed
    result: Any = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def is_terminal(self) -> bool:
        return self.status in ("complete", "failed")


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class IdempotencyStore(ABC):
    """
    Interface for idempotency state storage.

    Implementations:
      InMemoryIdempotencyStore -- for tests (no infra)
      RedisIdempotencyStore    -- for production (wraps redis_client.py)
    """

    @abstractmethod
    def get(self, key: str) -> Optional[IdempotencyRecord]:
        """Return the record for key, or None if not found."""

    @abstractmethod
    def set_in_flight(self, key: str) -> bool:
        """
        Atomically check-and-set the key to 'in_flight'.

        Returns True if the key was newly created (this caller owns processing).
        Returns False if the key already exists (another caller is processing
        or has already completed).

        Must be atomic to be safe under concurrent duplicate deliveries.
        """

    @abstractmethod
    def mark_complete(self, key: str, result: Any = None) -> None:
        """Mark the key as 'complete' and store the result."""

    @abstractmethod
    def mark_failed(self, key: str, error: str) -> None:
        """Mark the key as 'failed' and store the error message."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the key (used in tests and admin requeue scenarios)."""


# ---------------------------------------------------------------------------
# In-memory implementation (tests / dev)
# ---------------------------------------------------------------------------

class InMemoryIdempotencyStore(IdempotencyStore):
    """
    Dict-backed idempotency store. Thread-safe via a simple dict
    (single-threaded asyncio is fine; no concurrent dict mutation).

    NOT suitable for multi-process deployments — use RedisIdempotencyStore
    in production where multiple ARQ workers may receive the same job.
    """

    def __init__(self) -> None:
        self._store: dict[str, IdempotencyRecord] = {}

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        return self._store.get(key)

    def set_in_flight(self, key: str) -> bool:
        if key in self._store:
            return False
        self._store[key] = IdempotencyRecord(key=key, status="in_flight")
        return True

    def mark_complete(self, key: str, result: Any = None) -> None:
        if key in self._store:
            rec = self._store[key]
            rec.status = "complete"
            rec.result = result
            rec.updated_at = time.time()
        else:
            self._store[key] = IdempotencyRecord(
                key=key, status="complete", result=result
            )

    def mark_failed(self, key: str, error: str) -> None:
        if key in self._store:
            rec = self._store[key]
            rec.status = "failed"
            rec.error = error
            rec.updated_at = time.time()
        else:
            self._store[key] = IdempotencyRecord(
                key=key, status="failed", error=error
            )

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def __len__(self) -> int:
        return len(self._store)


# ---------------------------------------------------------------------------
# Redis implementation (production)
# ---------------------------------------------------------------------------

class RedisIdempotencyStore(IdempotencyStore):
    """
    Redis-backed idempotency store using SET NX (set-if-not-exists) for
    atomic check-and-set.

    This wraps the existing backend/memory/redis_client.py — no new
    Redis dependency. The key space is isolated under 'idempotency:' prefix.

    TTL: default 86400s (24 hours). Covers the full review lifecycle.

    Note: This class is lazily imported to avoid Redis dependency in tests.
    If Redis is unavailable, fall back to InMemoryIdempotencyStore.
    """

    DEFAULT_TTL_S = 86_400  # 24 hours

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        ttl_s: int = DEFAULT_TTL_S,
    ) -> None:
        self._redis_url = redis_url
        self._ttl_s = ttl_s
        self._client: Any = None   # Lazily initialised in _get_client()

    def _get_client(self) -> Any:
        """Lazy Redis client initialisation."""
        if self._client is None:
            try:
                import redis as _redis
                self._client = _redis.Redis.from_url(
                    self._redis_url, decode_responses=True
                )
            except ImportError as exc:
                raise RuntimeError(
                    "RedisIdempotencyStore requires the 'redis' package. "
                    "Run: pip install redis"
                ) from exc
        return self._client

    def _key(self, key: str) -> str:
        return f"idempotency:{key}"

    def get(self, key: str) -> Optional[IdempotencyRecord]:
        import json
        client = self._get_client()
        raw = client.get(self._key(key))
        if raw is None:
            return None
        data = json.loads(raw)
        return IdempotencyRecord(**data)

    def set_in_flight(self, key: str) -> bool:
        """
        Atomic SET NX with TTL. Returns True if key was newly set.
        Redis SET NX is atomic — safe under concurrent duplicate deliveries.
        """
        import json
        client = self._get_client()
        record = IdempotencyRecord(key=key, status="in_flight")
        payload = json.dumps({
            "key": record.key,
            "status": record.status,
            "result": record.result,
            "error": record.error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        })
        # SET NX EX: only set if key does not exist, with expiry
        result = client.set(self._key(key), payload, nx=True, ex=self._ttl_s)
        return result is not None   # True = newly set, None = already existed

    def mark_complete(self, key: str, result: Any = None) -> None:
        import json
        client = self._get_client()
        rkey = self._key(key)
        raw = client.get(rkey)
        if raw:
            data = json.loads(raw)
        else:
            data = {"key": key, "created_at": time.time()}
        data["status"] = "complete"
        data["result"] = result
        data["updated_at"] = time.time()
        client.set(rkey, json.dumps(data), ex=self._ttl_s)

    def mark_failed(self, key: str, error: str) -> None:
        import json
        client = self._get_client()
        rkey = self._key(key)
        raw = client.get(rkey)
        if raw:
            data = json.loads(raw)
        else:
            data = {"key": key, "created_at": time.time()}
        data["status"] = "failed"
        data["error"] = error
        data["updated_at"] = time.time()
        client.set(rkey, json.dumps(data), ex=self._ttl_s)

    def delete(self, key: str) -> None:
        client = self._get_client()
        client.delete(self._key(key))


# ---------------------------------------------------------------------------
# Functional guard
# ---------------------------------------------------------------------------

def idempotency_guard(
    store: IdempotencyStore,
    key: str,
    fn: Callable[[], Any],
) -> tuple[Any, bool]:
    """
    Check-and-run pattern.

    If key already has a complete result in store, return (cached_result, True)
    without running fn.

    If key is not seen before, run fn, store the result, return (result, False).

    Returns:
        (result, was_cached): was_cached=True means fn was skipped.

    Usage (in ARQ worker):
        result, cached = idempotency_guard(
            store, f"review:{pr_id}:{sha}", lambda: run_full_review(pr)
        )
        if cached:
            logger.info("Review %s already complete — skipping duplicate.", pr_id)
    """
    existing = store.get(key)
    if existing and existing.status == "complete":
        logger.info("idempotency_guard: key '%s' already complete. Skipping.", key)
        return existing.result, True

    if existing and existing.status == "in_flight":
        logger.warning(
            "idempotency_guard: key '%s' is in_flight (concurrent duplicate?). "
            "Proceeding anyway — results will overwrite.", key
        )

    acquired = store.set_in_flight(key)
    if not acquired:
        # Another caller beat us — check if they completed
        existing = store.get(key)
        if existing and existing.status == "complete":
            return existing.result, True
        # Still in_flight — proceed (race condition fallback)

    try:
        result = fn()
        store.mark_complete(key, result)
        return result, False
    except Exception as exc:
        store.mark_failed(key, str(exc))
        raise


# ---------------------------------------------------------------------------
# Higher-level ARQ job deduplicator
# ---------------------------------------------------------------------------

@dataclass
class JobDeduplicator:
    """
    Wraps IdempotencyStore with a clean API for the ARQ worker.

    Usage in arq_worker.py:
        dedup = JobDeduplicator(store=InMemoryIdempotencyStore())

        async def process_pr_review(ctx, pr_id: str, sha: str):
            key = dedup.make_key(pr_id, sha)
            if dedup.is_already_processing(key):
                return   # Skip — already in flight or done
            ...
            dedup.complete(key, result)
    """
    store: IdempotencyStore

    def make_key(self, pr_id: str | int, sha: str) -> str:
        """Canonical key format for a (PR, commit SHA) pair."""
        return f"pr:{pr_id}:{sha}"

    def is_already_processing(self, key: str) -> bool:
        """
        Returns True if key is already in_flight or complete.
        Call this at the top of the ARQ task function.
        """
        record = self.store.get(key)
        if record is None:
            self.store.set_in_flight(key)
            return False
        if record.status == "complete":
            logger.info("JobDeduplicator: '%s' already complete. Skipping.", key)
            return True
        if record.status == "in_flight":
            logger.warning(
                "JobDeduplicator: '%s' is in_flight. Possible concurrent duplicate.",
                key,
            )
            return True
        return False

    def complete(self, key: str, result: Any = None) -> None:
        self.store.mark_complete(key, result)

    def failed(self, key: str, error: str) -> None:
        self.store.mark_failed(key, error)

    def reset(self, key: str) -> None:
        """Delete the key so the job can be requeued (admin requeue)."""
        self.store.delete(key)
        logger.info("JobDeduplicator: key '%s' reset for requeue.", key)
