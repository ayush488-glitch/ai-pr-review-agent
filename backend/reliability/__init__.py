"""
backend/reliability/__init__.py

Phase 12: Reliability Engineering
===================================
Public API surface for the reliability module.

Modules:
  retry           -- Exponential backoff with jitter, @retryable decorator
  circuit_breaker -- CLOSED/OPEN/HALF_OPEN state machine, per-agent bulkheads
  timeout         -- Per-agent timeouts, partial results, AgentTimeoutError
  idempotency     -- Check-and-set store, ARQ job deduplication

Wiki refs:
  release-it/Circuit-Breaker.md       -- circuit breaker state machine
  release-it/Bulkheads.md             -- partition failure domains per agent
  release-it/Fail-Fast.md             -- timeout + exponential backoff
  llmops/Per-Agent-Timeout            -- per-agent not global timeouts
  llmops/Partial-Results-Doctrine     -- accept incomplete results, flag gaps
  llmops/State-Persistence-per-Stage  -- only retry failed stage, not full pipeline
"""

from backend.reliability.retry import (
    RetryConfig,
    RetryExhaustedError,
    DEFAULT_LLM_RETRY,
    DEFAULT_TOOL_RETRY,
    retry_with_backoff,
    async_retry_with_backoff,
    retryable,
)

from backend.reliability.circuit_breaker import (
    BreakerState,
    BreakerConfig,
    CircuitBreaker,
    CircuitOpenError,
    get_breaker,
    reset_all_breakers,
)

from backend.reliability.timeout import (
    TimeoutConfig,
    AgentTimeoutError,
    DEFAULT_TIMEOUTS,
    with_timeout,
    run_agents_with_per_agent_timeout,
)

from backend.reliability.idempotency import (
    IdempotencyStore,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
    idempotency_guard,
    JobDeduplicator,
)

__all__ = [
    # retry
    "RetryConfig",
    "RetryExhaustedError",
    "DEFAULT_LLM_RETRY",
    "DEFAULT_TOOL_RETRY",
    "retry_with_backoff",
    "async_retry_with_backoff",
    "retryable",
    # circuit breaker
    "BreakerState",
    "BreakerConfig",
    "CircuitBreaker",
    "CircuitOpenError",
    "get_breaker",
    "reset_all_breakers",
    # timeout
    "TimeoutConfig",
    "AgentTimeoutError",
    "DEFAULT_TIMEOUTS",
    "with_timeout",
    "run_agents_with_per_agent_timeout",
    # idempotency
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "RedisIdempotencyStore",
    "idempotency_guard",
    "JobDeduplicator",
]
