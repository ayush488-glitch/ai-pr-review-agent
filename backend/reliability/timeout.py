"""
backend/reliability/timeout.py

Phase 12: Per-Agent Timeouts and Partial Results
==================================================
Applies individual timeouts to each parallel agent so fast agents return
immediately while slow agents are awaited independently.

Design rationale
----------------
llmops-ai-agents/Per-Agent-Timeout:
  "Apply timeouts individually to each parallel agent so fast agents return
   immediately while slow agents are awaited independently."

Anti-pattern being avoided — Global Timeout Blocking:
  "Applying a single timeout to the entire gather() call causes fast agents'
   results to be discarded when one slow agent exceeds the limit."

llmops-ai-agents/Partial-Results-Doctrine:
  "Accept and use incomplete results when some agents timeout or fail,
   flagging missing agents for reprocessing rather than blocking on full
   completion. Better 75% of truth than 100% latency."

In our LangGraph workflow (Phase 4 nodes.py), all 4 agents run in parallel
via asyncio.gather. Without per-agent timeouts:
  - If the security agent hangs on a slow LLM call, the quality/test/docs
    agents all wait until the global timeout fires, discarding their results.
With per-agent timeouts (this module):
  - Each agent has its own asyncio.wait_for wrapper.
  - Fast agents return immediately.
  - Slow agents get AgentTimeoutError after their individual deadline.
  - aggregate_results() receives a mix of AgentOutput and AgentTimeoutError.
  - partial_review=True is set in PRReviewState (already defined in Phase 8).

This module is intentionally low-dependency. It imports nothing from the
agents layer. The orchestrator (nodes.py) passes agent coroutine functions
in and gets back a heterogeneous list of results + errors.

release-it/Fail-Fast.md:
  "Well-placed timeouts provide fault isolation. A problem in some other
   system does not have to become your problem."
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TimeoutConfig:
    """
    Per-category timeout budget in seconds.

    Fields:
        agent_timeout_s    -- Maximum time for a single agent's analyze() call.
                              Covers the full chain: prompt build -> LLM call -> parse.
        llm_timeout_s      -- Maximum time for one LLM API call (subset of agent).
                              The LLMClient wraps its calls with this timeout.
        tool_timeout_s     -- Maximum time for a single tool execution in the sandbox.
        workflow_timeout_s -- Hard upper bound on the full review workflow.
                              If this fires, ALL agents are cancelled and the
                              review is marked as TIMEOUT in PRReviewState.
    """
    agent_timeout_s: float = 30.0
    llm_timeout_s: float = 25.0
    tool_timeout_s: float = 10.0
    workflow_timeout_s: float = 120.0


DEFAULT_TIMEOUTS = TimeoutConfig()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AgentTimeoutError(Exception):
    """
    Raised when an individual agent exceeds its per-agent timeout.

    Unlike asyncio.TimeoutError (which is generic), this carries the agent
    name and the configured timeout so aggregate_results() can log exactly
    which agent was slow and how long it was given.

    The orchestrator's aggregate_results() catches this and sets:
      - partial_review = True in PRReviewState
      - records the timed-out agent in the verdict_breakdown as TIMEOUT
    """
    def __init__(self, agent_name: str, timeout_seconds: float) -> None:
        self.agent_name = agent_name
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Agent '{agent_name}' timed out after {timeout_seconds:.1f}s."
        )


# ---------------------------------------------------------------------------
# Single-coroutine timeout wrapper
# ---------------------------------------------------------------------------

async def with_timeout(
    coro: Awaitable[T],
    seconds: float,
    name: str = "unknown",
) -> T:
    """
    Await coro with a deadline of `seconds`. Converts asyncio.TimeoutError
    into a typed AgentTimeoutError.

    Args:
        coro    -- The coroutine to await (e.g. agent.analyze(...)).
        seconds -- Deadline in seconds.
        name    -- Human-readable label for the operation (logged on timeout).

    Returns the coroutine result on success.
    Raises AgentTimeoutError on timeout.
    Re-raises any other exception from the coroutine unchanged.

    Usage:
        result = await with_timeout(agent.analyze(task=t), seconds=30, name="security")
    """
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "with_timeout: '%s' timed out after %.1fs.", name, seconds
        )
        raise AgentTimeoutError(agent_name=name, timeout_seconds=seconds)


# ---------------------------------------------------------------------------
# Parallel fan-out with per-agent timeouts
# ---------------------------------------------------------------------------

async def run_agents_with_per_agent_timeout(
    agent_fns: list[tuple[str, Callable[[], Awaitable[Any]]]],
    timeout_s: float = DEFAULT_TIMEOUTS.agent_timeout_s,
) -> list[Any | AgentTimeoutError]:
    """
    Run multiple agent coroutines in parallel, each with its own timeout.

    This is the correct pattern for fan-out in our LangGraph workflow.
    Each agent gets exactly timeout_s seconds. If it exceeds that, it
    receives an AgentTimeoutError in the result list — other agents are
    NOT cancelled and their results are preserved.

    Args:
        agent_fns  -- List of (name, coroutine_factory) pairs.
                      The factory is a zero-argument callable that returns a
                      coroutine when called (not the coroutine itself, so it
                      can be created fresh for each run).
        timeout_s  -- Per-agent timeout in seconds (same for all agents;
                      override individually by wrapping with with_timeout()
                      before passing in if needed).

    Returns:
        A list of the same length as agent_fns. Each element is either:
          - The agent's return value (success), or
          - An AgentTimeoutError instance (timeout), or
          - An Exception instance (other failure, if return_exceptions=True).

    Note: asyncio.gather(return_exceptions=True) is used so that one
    agent's failure does not cancel the others. This implements the
    Partial Results Doctrine.

    Wiki ref: llmops Per-Agent-Timeout "Fast agents return immediately
    while slow agents are awaited independently — not a global timeout."

    Example:
        results = await run_agents_with_per_agent_timeout([
            ("security", lambda: security_agent.analyze(task=t)),
            ("quality",  lambda: quality_agent.analyze(task=t)),
        ], timeout_s=30)

        for name, result in zip(agent_names, results):
            if isinstance(result, AgentTimeoutError):
                # Flag partial_review, record in verdict_breakdown
                ...
            elif isinstance(result, Exception):
                # Other error — handle or re-raise
                ...
            else:
                # Normal AgentOutput
                ...
    """
    async def _guarded(name: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Wrap one agent call with its individual timeout."""
        try:
            return await with_timeout(factory(), seconds=timeout_s, name=name)
        except AgentTimeoutError as exc:
            # Return the error as a value — don't propagate so others proceed
            return exc
        except Exception as exc:
            # Return other exceptions as values too (consistent with Partial Results)
            logger.error(
                "run_agents_with_per_agent_timeout: agent '%s' raised %s: %s",
                name,
                type(exc).__name__,
                exc,
            )
            return exc

    coroutines = [_guarded(name, factory) for name, factory in agent_fns]
    # return_exceptions=True is belt-and-suspenders in case _guarded leaks
    results = await asyncio.gather(*coroutines, return_exceptions=True)
    return list(results)


# ---------------------------------------------------------------------------
# Workflow-level hard timeout
# ---------------------------------------------------------------------------

async def run_with_workflow_timeout(
    coro: Awaitable[T],
    config: TimeoutConfig = DEFAULT_TIMEOUTS,
) -> T:
    """
    Apply the hard workflow-level timeout to the entire review pipeline.

    If this fires, all in-flight agent coroutines are cancelled. The
    orchestrator catches this and marks the review as TIMEOUT (not APPROVE
    or BLOCK) so the HITL queue can pick it up.

    Usage (in arq_worker.py or langgraph_engine.py):
        result = await run_with_workflow_timeout(engine.run(state), config)
    """
    try:
        return await asyncio.wait_for(coro, timeout=config.workflow_timeout_s)
    except asyncio.TimeoutError:
        logger.error(
            "run_with_workflow_timeout: full workflow exceeded %.1fs hard limit.",
            config.workflow_timeout_s,
        )
        raise AgentTimeoutError(
            agent_name="workflow",
            timeout_seconds=config.workflow_timeout_s,
        )
