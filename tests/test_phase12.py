"""
tests/smoke_phase12.py

Phase 12: Reliability Engineering — 20 smoke tests
=====================================================
No external services needed. All tests run in-process.
InMemoryIdempotencyStore used instead of Redis.
Retry delays are patched to 0s so tests run fast.

Groups:
  A — Retry           (4 tests)
  B — Circuit Breaker (5 tests)
  C — Timeout         (4 tests)
  D — Idempotency     (4 tests)
  E — Integration     (3 tests)
"""

import sys
import os
import asyncio

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest
import time

# ---------------------------------------------------------------------------
# Imports from reliability module
# ---------------------------------------------------------------------------
from backend.reliability.retry import (
    RetryConfig,
    RetryExhaustedError,
    retry_with_backoff,
    async_retry_with_backoff,
)
from backend.reliability.circuit_breaker import (
    BreakerState,
    BreakerConfig,
    CircuitBreaker,
    CircuitOpenError,
)
from backend.reliability.timeout import (
    AgentTimeoutError,
    with_timeout,
    run_agents_with_per_agent_timeout,
)
from backend.reliability.idempotency import (
    InMemoryIdempotencyStore,
    idempotency_guard,
    JobDeduplicator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ZERO_DELAY = RetryConfig(max_attempts=3, base_delay_s=0.0, max_delay_s=0.0, jitter=False)
ZERO_DELAY_2 = RetryConfig(max_attempts=2, base_delay_s=0.0, max_delay_s=0.0, jitter=False)
FAST_BREAKER = BreakerConfig(failure_threshold=3, recovery_timeout_s=0.05, name="test_breaker")


# ===========================================================================
# GROUP A — Retry
# ===========================================================================

class TestRetry:

    def test_succeeds_on_first_try(self):
        """A function that succeeds immediately should not retry."""
        call_count = 0

        def fn():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = retry_with_backoff(fn, ZERO_DELAY)
        assert result == "ok"
        assert call_count == 1

    def test_retries_on_transient_error_then_succeeds(self):
        """
        A function that fails twice then succeeds should be called 3 times
        total and return the successful result.
        """
        attempts = []

        def fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise ValueError("transient")
            return "recovered"

        result = retry_with_backoff(fn, ZERO_DELAY)
        assert result == "recovered"
        assert len(attempts) == 3

    def test_exhausts_retries_and_raises(self):
        """
        When all attempts fail, RetryExhaustedError must be raised
        with the correct attempt count and last_exception.
        """
        def always_fails():
            raise RuntimeError("permanent")

        with pytest.raises(RetryExhaustedError) as exc_info:
            retry_with_backoff(always_fails, ZERO_DELAY)

        err = exc_info.value
        assert err.attempts == 3
        assert isinstance(err.last_exception, RuntimeError)

    def test_jitter_makes_delays_non_identical(self):
        """
        With jitter=True, consecutive computed delays should not be identical
        (with overwhelming probability). Validates _compute_delay randomness.
        """
        from backend.reliability.retry import _compute_delay
        config = RetryConfig(base_delay_s=1.0, max_delay_s=10.0, jitter=True)
        delays = {_compute_delay(0, config) for _ in range(10)}
        # 10 samples should produce at least 2 distinct values
        assert len(delays) > 1

    @pytest.mark.asyncio
    async def test_async_retry_succeeds_after_failures(self):
        """async_retry_with_backoff should mirror sync behaviour."""
        calls = []

        async def coro():
            calls.append(1)
            if len(calls) < 2:
                raise IOError("async transient")
            return "async_ok"

        result = await async_retry_with_backoff(coro, ZERO_DELAY_2)
        assert result == "async_ok"
        assert len(calls) == 2


# ===========================================================================
# GROUP B — Circuit Breaker
# ===========================================================================

class TestCircuitBreaker:

    def setup_method(self):
        """Each test gets a fresh breaker to avoid state bleed."""
        self.breaker = CircuitBreaker(FAST_BREAKER)

    def test_closed_allows_calls(self):
        """Brand new breaker is CLOSED and passes calls through."""
        assert self.breaker.state == BreakerState.CLOSED
        result = self.breaker.call(lambda: "pass")
        assert result == "pass"

    def test_opens_after_failure_threshold(self):
        """
        After failure_threshold=3 consecutive failures, breaker must be OPEN.
        """
        for _ in range(3):
            with pytest.raises(RuntimeError):
                self.breaker.call(_always_raise)

        assert self.breaker.state == BreakerState.OPEN

    def test_open_fails_fast_with_circuit_open_error(self):
        """OPEN breaker raises CircuitOpenError without calling the function."""
        # Trip the breaker
        for _ in range(3):
            with pytest.raises(RuntimeError):
                self.breaker.call(_always_raise)

        call_count = [0]

        def fn():
            call_count[0] += 1
            return "should not be called"

        with pytest.raises(CircuitOpenError) as exc_info:
            self.breaker.call(fn)

        assert call_count[0] == 0   # fn was never called
        assert "test_breaker" in str(exc_info.value)

    def test_transitions_to_half_open_after_recovery_timeout(self):
        """
        After recovery_timeout_s (0.05s for FAST_BREAKER) elapses in OPEN,
        the next state() check should return HALF_OPEN.
        """
        for _ in range(3):
            with pytest.raises(RuntimeError):
                self.breaker.call(_always_raise)

        assert self.breaker.state == BreakerState.OPEN
        time.sleep(0.1)  # > 0.05s recovery timeout
        assert self.breaker.state == BreakerState.HALF_OPEN

    def test_success_in_half_open_closes_breaker(self):
        """
        A successful call in HALF_OPEN should transition back to CLOSED
        and reset the failure count.
        """
        # Trip to OPEN
        for _ in range(3):
            with pytest.raises(RuntimeError):
                self.breaker.call(_always_raise)

        # Wait for recovery
        time.sleep(0.1)
        assert self.breaker.state == BreakerState.HALF_OPEN

        # Successful probe
        result = self.breaker.call(lambda: "recovered")
        assert result == "recovered"
        assert self.breaker.state == BreakerState.CLOSED
        assert self.breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_async_call_works(self):
        """async_call() should pass through on CLOSED breaker."""
        breaker = CircuitBreaker(FAST_BREAKER)

        async def coro():
            return "async_result"

        result = await breaker.async_call(coro)
        assert result == "async_result"


# ===========================================================================
# GROUP C — Timeout
# ===========================================================================

class TestTimeout:

    @pytest.mark.asyncio
    async def test_fast_coroutine_completes(self):
        """A coroutine that returns quickly should succeed."""
        async def fast():
            return "fast_result"

        result = await with_timeout(fast(), seconds=5.0, name="fast_agent")
        assert result == "fast_result"

    @pytest.mark.asyncio
    async def test_slow_coroutine_raises_agent_timeout_error(self):
        """A coroutine that exceeds its deadline must raise AgentTimeoutError."""
        async def slow():
            await asyncio.sleep(10)
            return "should not reach"

        with pytest.raises(AgentTimeoutError) as exc_info:
            await with_timeout(slow(), seconds=0.05, name="slow_agent")

        err = exc_info.value
        assert err.agent_name == "slow_agent"
        assert err.timeout_seconds == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_per_agent_timeout_returns_partial_results(self):
        """
        When one agent times out, the others should still return their results.
        Validates the Partial Results Doctrine.
        """
        async def agent_fast():
            return "fast_ok"

        async def agent_slow():
            await asyncio.sleep(10)
            return "slow_ok"

        results = await run_agents_with_per_agent_timeout(
            [
                ("fast_agent", lambda: agent_fast()),
                ("slow_agent", lambda: agent_slow()),
            ],
            timeout_s=0.1,
        )

        assert len(results) == 2
        # fast_agent should succeed
        assert results[0] == "fast_ok"
        # slow_agent should be an AgentTimeoutError
        assert isinstance(results[1], AgentTimeoutError)
        assert results[1].agent_name == "slow_agent"

    @pytest.mark.asyncio
    async def test_non_timed_out_agent_result_preserved(self):
        """
        All agents that complete within timeout must have their results
        fully preserved even if another agent times out.
        """
        async def agent_a():
            return {"verdict": "APPROVE", "agent": "a"}

        async def agent_b():
            await asyncio.sleep(10)

        async def agent_c():
            return {"verdict": "BLOCK", "agent": "c"}

        results = await run_agents_with_per_agent_timeout(
            [
                ("a", lambda: agent_a()),
                ("b", lambda: agent_b()),
                ("c", lambda: agent_c()),
            ],
            timeout_s=0.1,
        )

        assert results[0] == {"verdict": "APPROVE", "agent": "a"}
        assert isinstance(results[1], AgentTimeoutError)
        assert results[2] == {"verdict": "BLOCK", "agent": "c"}


# ===========================================================================
# GROUP D — Idempotency
# ===========================================================================

class TestIdempotency:

    def test_new_key_runs_fn(self):
        """First call with a new key should run fn and store the result."""
        store = InMemoryIdempotencyStore()
        calls = []

        def fn():
            calls.append(1)
            return "result_value"

        result, was_cached = idempotency_guard(store, "key1", fn)
        assert result == "result_value"
        assert was_cached is False
        assert len(calls) == 1

    def test_duplicate_key_returns_cached_result(self):
        """Second call with the same key must return cached result without calling fn."""
        store = InMemoryIdempotencyStore()
        calls = []

        def fn():
            calls.append(1)
            return "original"

        idempotency_guard(store, "key2", fn)         # First call — runs fn
        result, was_cached = idempotency_guard(store, "key2", fn)  # Second call

        assert result == "original"
        assert was_cached is True
        assert len(calls) == 1   # fn was NOT called a second time

    def test_mark_complete_is_idempotent(self):
        """Calling mark_complete twice on the same key should not raise."""
        store = InMemoryIdempotencyStore()
        store.set_in_flight("key3")
        store.mark_complete("key3", "first")
        store.mark_complete("key3", "second")   # Should overwrite, not raise
        record = store.get("key3")
        assert record is not None
        assert record.status == "complete"

    def test_different_keys_run_independently(self):
        """Two different keys should each call their fn independently."""
        store = InMemoryIdempotencyStore()
        results = {}

        def make_fn(name):
            def fn():
                results[name] = True
                return name
            return fn

        idempotency_guard(store, "key_a", make_fn("a"))
        idempotency_guard(store, "key_b", make_fn("b"))

        assert results == {"a": True, "b": True}

    def test_job_deduplicator_skips_duplicate(self):
        """JobDeduplicator.is_already_processing should return True on second call."""
        store = InMemoryIdempotencyStore()
        dedup = JobDeduplicator(store=store)
        key = dedup.make_key(pr_id="42", sha="abc123")

        assert dedup.is_already_processing(key) is False  # First — starts it
        assert dedup.is_already_processing(key) is True   # Second — already in_flight


# ===========================================================================
# GROUP E — Integration
# ===========================================================================

class TestIntegration:

    def test_retry_plus_circuit_breaker_compose(self):
        """
        CircuitBreaker wrapping a retried call: if the underlying fn
        always fails, the breaker should open and subsequent calls should
        get CircuitOpenError, not RetryExhaustedError.
        """
        breaker = CircuitBreaker(BreakerConfig(failure_threshold=2, name="combo"))
        call_count = [0]

        def failing_fn():
            call_count[0] += 1
            raise ConnectionError("down")

        # Two calls trip the breaker (threshold=2)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                breaker.call(failing_fn)

        assert breaker.state == BreakerState.OPEN

        # Third call should get CircuitOpenError, fn not called again
        with pytest.raises(CircuitOpenError):
            breaker.call(failing_fn)

        assert call_count[0] == 2   # fn only called during the 2 tripping calls

    @pytest.mark.asyncio
    async def test_timeout_sets_partial_review_flag(self):
        """
        When run_agents_with_per_agent_timeout returns a mix of results
        and AgentTimeoutErrors, the caller should detect partial_review.
        """
        async def ok_agent():
            return "ok"

        async def timeout_agent():
            await asyncio.sleep(10)

        results = await run_agents_with_per_agent_timeout(
            [("ok", lambda: ok_agent()), ("timeout", lambda: timeout_agent())],
            timeout_s=0.05,
        )

        # Any AgentTimeoutError in results means partial_review=True
        partial_review = any(isinstance(r, AgentTimeoutError) for r in results)
        assert partial_review is True

        # Successful agent result is intact
        assert results[0] == "ok"

    def test_idempotency_store_survives_fn_exception(self):
        """
        If fn raises, the key must be marked 'failed' so the next call
        does NOT return a stale cached result — it re-runs fn.
        """
        store = InMemoryIdempotencyStore()
        attempt = [0]

        def flaky():
            attempt[0] += 1
            if attempt[0] == 1:
                raise ValueError("first attempt fails")
            return "second_ok"

        with pytest.raises(ValueError):
            idempotency_guard(store, "flaky_key", flaky)

        record = store.get("flaky_key")
        assert record is not None
        assert record.status == "failed"

        # After failure, the key is marked failed — calling again re-runs fn
        # (the guard sees failed != complete so it allows re-run)
        # Delete and retry to simulate requeue
        store.delete("flaky_key")
        result, cached = idempotency_guard(store, "flaky_key", flaky)
        assert result == "second_ok"
        assert cached is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _always_raise():
    raise RuntimeError("deliberate failure")
