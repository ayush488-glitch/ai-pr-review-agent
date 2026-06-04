"""
backend/reliability/circuit_breaker.py

Phase 12: Circuit Breaker — CLOSED/OPEN/HALF_OPEN State Machine
=================================================================
Prevents cascading failures when a downstream dependency (LLM API, tool,
vector store) is misbehaving.

Design rationale
----------------
release-it/Circuit-Breaker.md:
  "When an agent starts misbehaving you don't want it to fail millions of
   users. Circuit breakers trip automatically and route traffic elsewhere."

release-it/Bulkheads.md:
  "Cascading Failures: A failure in one layer propagates upward through
   tightly coupled layers until the entire system is down.
   Fix: Use Circuit Breakers and Bulkheads to isolate failure domains."

BULKHEAD PRINCIPLE: each agent type gets its own CircuitBreaker instance
(AGENT_BREAKERS dict). The security agent failing does NOT trip the quality
agent's breaker. This is the bulkhead — independent failure domains.

State machine:
  CLOSED     -- Normal operation. Calls pass through.
                On N consecutive failures -> transition to OPEN.

  OPEN       -- Failing fast. All calls raise CircuitOpenError immediately
                without attempting the underlying call.
                After recovery_timeout_s has elapsed -> transition to HALF_OPEN.

  HALF_OPEN  -- Probing recovery. Allows half_open_max_calls through.
                On success -> CLOSED. On failure -> OPEN (reset timer).

Thread/coroutine safety
-----------------------
State transitions use a threading.Lock so the breaker is safe under both
threading and asyncio (FastAPI + ARQ both run in async context but
threading.Lock is safe to acquire from a coroutine running on a single thread
event loop). If multiple event loops or threads are expected, swap for
asyncio.Lock and gate with async_call() only.

Asyncio note: async_call() releases the GIL during the actual coroutine
execution, so the lock is acquired only for the state-check bookkeeping,
not during the (potentially slow) LLM call itself.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict

# Python 3.10 StrEnum shim
try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State vocabulary
# ---------------------------------------------------------------------------

class BreakerState(StrEnum):
    CLOSED    = "closed"     # Normal — calls pass through
    OPEN      = "open"       # Failing fast — calls rejected immediately
    HALF_OPEN = "half_open"  # Probing — limited calls allowed


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BreakerConfig:
    """
    Policy for one CircuitBreaker instance.

    Fields:
        failure_threshold    -- Consecutive failures that trip CLOSED -> OPEN.
        recovery_timeout_s   -- Seconds to wait in OPEN before probing (HALF_OPEN).
        half_open_max_calls  -- How many probe calls to allow in HALF_OPEN.
                                Usually 1 — pass one call, if it succeeds -> CLOSED.
        name                 -- Human-readable identifier for logs and health endpoints.
    """
    failure_threshold: int = 5
    recovery_timeout_s: float = 60.0
    half_open_max_calls: int = 1
    name: str = "unnamed"


# Default configs — tuned for our three integration categories

DEFAULT_LLM_BREAKER_CONFIG = BreakerConfig(
    failure_threshold=5,
    recovery_timeout_s=60.0,
    name="llm_api",
)

DEFAULT_TOOL_BREAKER_CONFIG = BreakerConfig(
    failure_threshold=3,   # Tools are cheap — trip faster
    recovery_timeout_s=30.0,
    name="tool_sandbox",
)

DEFAULT_VECTOR_BREAKER_CONFIG = BreakerConfig(
    failure_threshold=3,
    recovery_timeout_s=30.0,
    name="tiger",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CircuitOpenError(Exception):
    """
    Raised when call() is attempted while the breaker is OPEN.

    The caller should catch this and either return a degraded result or
    escalate to HITL. Do NOT retry — the whole point is to stop hammering
    the failing dependency.

    Wiki ref: "Circuit breakers trip automatically and route traffic
    elsewhere." Retrying on CircuitOpenError defeats the pattern.
    """
    def __init__(self, breaker_name: str, retry_after_s: float | None = None) -> None:
        self.breaker_name = breaker_name
        self.retry_after_s = retry_after_s
        msg = f"Circuit breaker '{breaker_name}' is OPEN."
        if retry_after_s is not None:
            msg += f" Retry after {retry_after_s:.1f}s."
        super().__init__(msg)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """
    CLOSED/OPEN/HALF_OPEN circuit breaker wrapping any callable.

    Usage (sync):
        breaker = CircuitBreaker(BreakerConfig(name="my_api"))
        result = breaker.call(my_fn, arg1, kwarg=v)

    Usage (async):
        result = await breaker.async_call(my_coro_fn, arg1, kwarg=v)

    The breaker is stateful — keep one instance per integration point
    and reuse it across calls. Do NOT create a new instance per call.
    """

    def __init__(self, config: BreakerConfig | None = None) -> None:
        self._config = config or BreakerConfig()
        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._half_open_calls = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public call surfaces
    # ------------------------------------------------------------------

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Call fn(*args, **kwargs) through the circuit breaker (sync version).

        Raises CircuitOpenError if the breaker is OPEN.
        Transitions state on success/failure.
        """
        self._check_state()
        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise exc

    async def async_call(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """
        Async version. fn must be a coroutine function.
        The lock is acquired only for bookkeeping, not during await.
        """
        self._check_state()
        try:
            result = await fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise exc

    # ------------------------------------------------------------------
    # State inspection
    # ------------------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        """Current breaker state (thread-safe read)."""
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def state_summary(self) -> dict:
        """
        Serialisable summary for health endpoints and the Phase 13 /health route.
        """
        with self._lock:
            self._maybe_transition_to_half_open()
            retry_after: float | None = None
            if self._state == BreakerState.OPEN and self._last_failure_time:
                elapsed = time.monotonic() - self._last_failure_time
                remaining = self._config.recovery_timeout_s - elapsed
                retry_after = max(0.0, remaining)
            return {
                "name": self._config.name,
                "state": self._state.value,
                "failure_count": self._failure_count,
                "failure_threshold": self._config.failure_threshold,
                "retry_after_s": retry_after,
            }

    def reset(self) -> None:
        """
        Manually reset to CLOSED with zero failure count.
        Useful in tests and after a known incident is resolved.
        """
        with self._lock:
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_calls = 0

    # ------------------------------------------------------------------
    # Internal state machine
    # ------------------------------------------------------------------

    def _check_state(self) -> None:
        """
        Called before every attempt. Raises CircuitOpenError if OPEN,
        transitions to HALF_OPEN if recovery timeout has elapsed.
        """
        with self._lock:
            self._maybe_transition_to_half_open()
            if self._state == BreakerState.OPEN:
                retry_after: float | None = None
                if self._last_failure_time:
                    elapsed = time.monotonic() - self._last_failure_time
                    retry_after = max(0.0, self._config.recovery_timeout_s - elapsed)
                raise CircuitOpenError(self._config.name, retry_after)
            if self._state == BreakerState.HALF_OPEN:
                if self._half_open_calls >= self._config.half_open_max_calls:
                    # Already probing — reject additional calls
                    raise CircuitOpenError(self._config.name, retry_after=0.0)
                self._half_open_calls += 1

    def _maybe_transition_to_half_open(self) -> None:
        """
        Check if enough time has elapsed since the last failure to attempt
        recovery. Must be called while the lock is held.
        """
        if (
            self._state == BreakerState.OPEN
            and self._last_failure_time is not None
            and (time.monotonic() - self._last_failure_time)
            >= self._config.recovery_timeout_s
        ):
            self._state = BreakerState.HALF_OPEN
            self._half_open_calls = 0
            logger.info(
                "CircuitBreaker '%s': OPEN -> HALF_OPEN (recovery probe).",
                self._config.name,
            )

    def _on_success(self) -> None:
        """Called after a successful call. Resets to CLOSED from any state."""
        with self._lock:
            if self._state in (BreakerState.HALF_OPEN, BreakerState.OPEN):
                logger.info(
                    "CircuitBreaker '%s': %s -> CLOSED (recovery confirmed).",
                    self._config.name,
                    self._state.value,
                )
            self._state = BreakerState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
            self._half_open_calls = 0

    def _on_failure(self) -> None:
        """
        Called after a failed call. Increments counter and trips to OPEN
        once the failure_threshold is reached.
        """
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == BreakerState.HALF_OPEN:
                # Probe failed — go back to OPEN and reset timer
                self._state = BreakerState.OPEN
                self._half_open_calls = 0
                logger.warning(
                    "CircuitBreaker '%s': HALF_OPEN -> OPEN (probe failed).",
                    self._config.name,
                )
            elif (
                self._state == BreakerState.CLOSED
                and self._failure_count >= self._config.failure_threshold
            ):
                self._state = BreakerState.OPEN
                logger.error(
                    "CircuitBreaker '%s': CLOSED -> OPEN "
                    "(%d consecutive failures >= threshold %d).",
                    self._config.name,
                    self._failure_count,
                    self._config.failure_threshold,
                )


# ---------------------------------------------------------------------------
# Global registry — one breaker per named integration point (bulkhead)
# ---------------------------------------------------------------------------
# Wiki ref: release-it/Bulkheads.md "Partition systems into independent
# compartments so that failure in one section does not sink the entire vessel."

_REGISTRY: Dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_breaker(name: str, config: BreakerConfig | None = None) -> CircuitBreaker:
    """
    Get or create the CircuitBreaker for a named integration point.

    This is the factory to use in production code. It ensures there is
    exactly one breaker per name (singleton per integration point).

    If the breaker does not exist yet and no config is provided, a
    default BreakerConfig(name=name) is used.

    Example:
        breaker = get_breaker("security_agent_llm", DEFAULT_LLM_BREAKER_CONFIG)
        result = await breaker.async_call(llm_client.call, prompt)
    """
    with _REGISTRY_LOCK:
        if name not in _REGISTRY:
            cfg = config or BreakerConfig(name=name)
            _REGISTRY[name] = CircuitBreaker(cfg)
        return _REGISTRY[name]


def reset_all_breakers() -> None:
    """
    Reset every registered breaker to CLOSED. Used in test teardown
    and post-incident recovery scripts.
    """
    with _REGISTRY_LOCK:
        for breaker in _REGISTRY.values():
            breaker.reset()
        logger.info("All circuit breakers reset to CLOSED.")


def list_breaker_summaries() -> list[dict]:
    """
    Return state_summary() for every registered breaker.
    Used by the Phase 13 /health endpoint.
    """
    with _REGISTRY_LOCK:
        names = list(_REGISTRY.keys())
    return [get_breaker(name).state_summary() for name in names]
