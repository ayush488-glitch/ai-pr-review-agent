"""
backend/reliability/retry.py

Phase 12: Exponential Backoff with Jitter
==========================================
Retries failing calls with exponential delay and randomised jitter.

Design rationale
----------------
release-it/Fail-Fast.md: "Immediate retries are liable to hit the same
problem and result in another timeout. That just makes the user wait even
longer for his error message."

The fix is exponential backoff: each successive attempt waits 2^n × base_delay.
Jitter (a random fraction of the delay) is added to prevent the thundering herd:
if 50 workers all retry at the same second after a blip, they all hit the
recovering service simultaneously and can knock it back down.

    delay(attempt n) = min(base * 2^n, max_delay) + uniform(0, delay * jitter_factor)

Two surfaces:
  - retry_with_backoff() / async_retry_with_backoff(): procedural wrappers
    for one-off call sites (e.g. tool invocations inside agent code).
  - @retryable(config): decorator for functions that should always be retried
    (e.g. LLMClient.call(), qdrant upsert).

Both surfaces accept a RetryConfig so callers control the policy without
hardcoding magic numbers inside business logic.

Retryable exceptions
--------------------
Only exceptions listed in RetryConfig.retryable_exceptions are retried.
All others are re-raised immediately. This prevents retrying on errors that
will never recover (e.g. ValidationError, PermissionDeniedError).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence, Type, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetryConfig:
    """
    Policy for retry behaviour.

    Fields:
        max_attempts         -- Total attempts (1 = no retry, 3 = 1 try + 2 retries).
        base_delay_s         -- Initial delay in seconds before first retry.
        max_delay_s          -- Upper bound on delay (caps exponential growth).
        jitter               -- Whether to add random jitter to each delay.
        jitter_factor        -- Fraction of computed delay added as max jitter.
                                Default 0.25 means up to 25% extra randomness.
        retryable_exceptions -- Only these exception types trigger a retry.
                                Default: broad (Exception), override to narrow.
    """
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: bool = True
    jitter_factor: float = 0.25
    retryable_exceptions: tuple[Type[Exception], ...] = field(
        default_factory=lambda: (Exception,)
    )


# Pre-configured policies for the two most common call sites

DEFAULT_LLM_RETRY = RetryConfig(
    # LLM APIs: transient rate-limits and 5xx errors. 3 attempts with 1s base.
    max_attempts=3,
    base_delay_s=1.0,
    max_delay_s=30.0,
    jitter=True,
)

DEFAULT_TOOL_RETRY = RetryConfig(
    # Tool sandbox calls: fast recovery, fewer retries.
    max_attempts=2,
    base_delay_s=0.5,
    max_delay_s=10.0,
    jitter=True,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RetryExhaustedError(Exception):
    """
    Raised when all retry attempts are consumed without success.

    Wraps the last exception so callers can inspect the root cause.
    """
    def __init__(self, attempts: int, last_exception: Exception) -> None:
        self.attempts = attempts
        self.last_exception = last_exception
        super().__init__(
            f"Retry exhausted after {attempts} attempt(s). "
            f"Last error: {type(last_exception).__name__}: {last_exception}"
        )


# ---------------------------------------------------------------------------
# Delay calculation
# ---------------------------------------------------------------------------

def _compute_delay(attempt: int, config: RetryConfig) -> float:
    """
    Compute sleep duration for attempt N (0-indexed).

    Formula: min(base * 2^attempt, max_delay) + jitter
    Jitter: uniform(0, delay * jitter_factor)
    """
    delay = min(config.base_delay_s * (2 ** attempt), config.max_delay_s)
    if config.jitter:
        delay += random.uniform(0, delay * config.jitter_factor)
    return delay


# ---------------------------------------------------------------------------
# Synchronous retry
# ---------------------------------------------------------------------------

def retry_with_backoff(
    fn: Callable[..., Any],
    config: RetryConfig = DEFAULT_LLM_RETRY,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Call fn(*args, **kwargs) with exponential backoff retry.

    Retries only on exceptions in config.retryable_exceptions.
    Raises RetryExhaustedError if all attempts fail.

    Usage:
        result = retry_with_backoff(my_fn, DEFAULT_LLM_RETRY, arg1, kwarg=v)
    """
    last_exc: Exception | None = None

    for attempt in range(config.max_attempts):
        try:
            return fn(*args, **kwargs)
        except config.retryable_exceptions as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt < config.max_attempts - 1:
                delay = _compute_delay(attempt, config)
                logger.warning(
                    "retry_with_backoff: attempt %d/%d failed (%s: %s). "
                    "Retrying in %.2fs.",
                    attempt + 1,
                    config.max_attempts,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "retry_with_backoff: all %d attempts exhausted. Last: %s",
                    config.max_attempts,
                    exc,
                )
        except Exception as exc:
            # Non-retryable — re-raise immediately without consuming attempts
            raise exc

    raise RetryExhaustedError(config.max_attempts, last_exc)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Asynchronous retry
# ---------------------------------------------------------------------------

async def async_retry_with_backoff(
    fn: Callable[..., Any],
    config: RetryConfig = DEFAULT_LLM_RETRY,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Async version of retry_with_backoff. Uses asyncio.sleep so the event
    loop is not blocked during backoff delays.

    Usage:
        result = await async_retry_with_backoff(my_coro, DEFAULT_LLM_RETRY, arg)
    """
    last_exc: Exception | None = None

    for attempt in range(config.max_attempts):
        try:
            return await fn(*args, **kwargs)
        except config.retryable_exceptions as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt < config.max_attempts - 1:
                delay = _compute_delay(attempt, config)
                logger.warning(
                    "async_retry: attempt %d/%d failed (%s: %s). "
                    "Retrying in %.2fs.",
                    attempt + 1,
                    config.max_attempts,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "async_retry: all %d attempts exhausted. Last: %s",
                    config.max_attempts,
                    exc,
                )
        except Exception as exc:
            raise exc

    raise RetryExhaustedError(config.max_attempts, last_exc)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def retryable(
    config: RetryConfig = DEFAULT_LLM_RETRY,
) -> Callable[[F], F]:
    """
    Decorator that wraps a sync or async function with retry logic.

    Detects coroutine functions automatically and applies the correct wrapper.

    Usage (sync):
        @retryable(DEFAULT_TOOL_RETRY)
        def call_tool(name: str) -> str: ...

    Usage (async):
        @retryable(DEFAULT_LLM_RETRY)
        async def call_llm(prompt: str) -> LLMResponse: ...
    """
    def decorator(fn: F) -> F:
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                last_exc: Exception | None = None
                for attempt in range(config.max_attempts):
                    try:
                        return await fn(*args, **kwargs)
                    except config.retryable_exceptions as exc:  # type: ignore[misc]
                        last_exc = exc
                        if attempt < config.max_attempts - 1:
                            delay = _compute_delay(attempt, config)
                            logger.warning(
                                "@retryable(%s): attempt %d/%d failed. "
                                "Retrying in %.2fs.",
                                fn.__name__,
                                attempt + 1,
                                config.max_attempts,
                                delay,
                            )
                            await asyncio.sleep(delay)
                    except Exception as exc:
                        raise exc
                raise RetryExhaustedError(config.max_attempts, last_exc)  # type: ignore[arg-type]
            return async_wrapper  # type: ignore[return-value]
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                last_exc: Exception | None = None
                for attempt in range(config.max_attempts):
                    try:
                        return fn(*args, **kwargs)
                    except config.retryable_exceptions as exc:  # type: ignore[misc]
                        last_exc = exc
                        if attempt < config.max_attempts - 1:
                            delay = _compute_delay(attempt, config)
                            logger.warning(
                                "@retryable(%s): attempt %d/%d failed. "
                                "Retrying in %.2fs.",
                                fn.__name__,
                                attempt + 1,
                                config.max_attempts,
                                delay,
                            )
                            time.sleep(delay)
                    except Exception as exc:
                        raise exc
                raise RetryExhaustedError(config.max_attempts, last_exc)  # type: ignore[arg-type]
            return sync_wrapper  # type: ignore[return-value]

    return decorator
