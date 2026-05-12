# backend/tools/capability_scope.py
#
# Capability Scope — per-agent tool allowlists, enforced at call time.
#
# WHY THIS EXISTS (wiki: Client-Isolation-Layer):
#   "Security boundaries are hard to retrofit: Design them in from the beginning."
#   "Cross-domain tool access is dangerous: Forbid by default. Explicit allowlisting only."
#   "Monolithic agent with 100+ tools causes wild hallucination and intent confusion."
#
#   Each specialist agent has a MINIMUM set of tools it needs. SecurityAgent
#   needs secret scanning; DocsAgent does not. Giving every agent every tool:
#     1. Increases hallucination risk (LLM picks wrong tool from a huge list)
#     2. Violates least-privilege (DocsAgent calling dependency advisories is wrong)
#     3. Makes audit trails noisy (hard to tell intentional from accidental tool calls)
#
# HOW IT WORKS:
#   - CAPABILITY_MAP maps AgentType -> CapabilityScope (frozenset of allowed tool names)
#   - check_capability(agent_type, tool_name) is called by BaseAgent.call_tool()
#     BEFORE the registry is consulted
#   - If the agent doesn't have the capability, CapabilityViolationError is raised
#     (not silently ignored — a policy violation is a deployment bug)
#
# WHERE CAPABILITY IS CHECKED:
#   BaseAgent.call_tool() calls check_capability() before dispatching to tool_registry.
#   The order is:
#     1. check_capability()    <- scope gate (this file)
#     2. tool_registry.call()  <- execution gate (tool_registry.py)
#     3. sandbox.run_*()       <- resource gate (sandbox.py) if applicable
#   Three independent gates, each with a different concern.
#
# DEPENDENCY: models/enums (AgentType) — no upward deps

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.models.enums import AgentType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CapabilityViolationError(Exception):
    """
    Raised when an agent attempts to call a tool outside its capability scope.

    WHY a rich exception (not just a string):
        Monitoring and audit systems need structured fields. If this fires in
        production it's a potential prompt-injection or misconfiguration event
        and we want clear forensics: which agent tried to call which tool.

    Attributes:
        agent_type: the AgentType that made the violating call
        tool_name:  the tool name that was attempted
        allowed:    the frozenset of tools this agent IS allowed to call
    """

    def __init__(
        self,
        agent_type: AgentType,
        tool_name: str,
        allowed: frozenset[str],
    ) -> None:
        self.agent_type = agent_type
        self.tool_name = tool_name
        self.allowed = allowed
        super().__init__(
            f"Agent '{agent_type.value}' attempted to call tool '{tool_name}', "
            f"which is outside its capability scope. "
            f"Allowed tools for this agent: {sorted(allowed)}. "
            "This is a policy violation — check for prompt injection or misconfiguration."
        )


# ---------------------------------------------------------------------------
# CapabilityScope dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CapabilityScope:
    """
    The complete capability declaration for one agent type.

    WHY frozen=True:
        Capability scopes are policy, not runtime state.
        They must never change after the server starts.
        Immutability makes this invariant enforceable by the type system.

    Fields:
        agent_type:         the AgentType this scope applies to
        allowed_tool_names: frozenset of tool names this agent may call
        rationale:          human-readable explanation (helps reviewers understand
                            why each agent has exactly the tools it has)
    """
    agent_type: AgentType
    allowed_tool_names: frozenset[str]
    rationale: str


# ---------------------------------------------------------------------------
# CAPABILITY_MAP — the authoritative policy table
#
# DESIGN PRINCIPLE (wiki: Client-Isolation-Layer):
#   "Span of control matters: An orchestrator managing 5 specialists is more
#    reliable than one managing 50."
#   Each agent gets only the tools that are semantically meaningful for its role.
#
# HOW TO READ THIS TABLE:
#   Each row = one agent + its allowed tools + the reasoning.
#   If you want to add a tool to an agent's scope, add the tool_name here
#   AND document why. Undocumented tool access is a code review red flag.
# ---------------------------------------------------------------------------

CAPABILITY_MAP: dict[AgentType, CapabilityScope] = {

    AgentType.SECURITY: CapabilityScope(
        agent_type=AgentType.SECURITY,
        allowed_tool_names=frozenset({
            "check_secrets_pattern",    # primary tool: deterministic secret detection
            "search_similar_findings",  # secondary: detect systemic/repeated vuln patterns
            "get_dependency_advisory",  # secondary: flag new deps with known CVEs
        }),
        rationale=(
            "SecurityAgent needs secret scanning (deterministic, more reliable than LLM "
            "for literal patterns), similarity search to detect systemic vulnerabilities "
            "across files, and dependency advisory to flag dangerous new packages. "
            "It does NOT need syntax_check — broken syntax is QualityAgent's domain."
        ),
    ),

    AgentType.QUALITY: CapabilityScope(
        agent_type=AgentType.QUALITY,
        allowed_tool_names=frozenset({
            "run_syntax_check",        # primary tool: deterministic broken-code detection
            "search_similar_findings", # secondary: compare code quality patterns across repo
        }),
        rationale=(
            "QualityAgent focuses on code correctness and style. Syntax checking gives it "
            "a deterministic gate for obviously broken code before spending LLM tokens on "
            "analysis. Similarity search helps it flag inconsistencies (same function "
            "implemented differently in two places). "
            "It does NOT need dependency_advisory (security concern) or "
            "check_secrets_pattern (security concern)."
        ),
    ),

    AgentType.TEST: CapabilityScope(
        agent_type=AgentType.TEST,
        allowed_tool_names=frozenset({
            "run_syntax_check",        # primary: test code itself must be syntactically valid
            "search_similar_findings", # secondary: find existing tests to avoid duplication
        }),
        rationale=(
            "TestAgent reviews whether tests are present, correct, and non-redundant. "
            "Syntax checking ensures the test code itself is valid. "
            "Similarity search helps find existing tests for the same behaviour "
            "so the agent can flag missing coverage or duplicated test logic. "
            "It does NOT need check_secrets_pattern or dependency_advisory."
        ),
    ),

    AgentType.DOCS: CapabilityScope(
        agent_type=AgentType.DOCS,
        allowed_tool_names=frozenset({
            "search_similar_findings", # only tool: find how similar code is documented
        }),
        rationale=(
            "DocsAgent reviews docstrings, comments, and README changes. "
            "Similarity search helps it find existing documentation conventions in "
            "the codebase so it can flag style inconsistencies. "
            "It has the smallest capability scope because its work is purely textual — "
            "no syntax execution, no secret scanning, no dependency lookups."
        ),
    ),

}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_allowed_tools(agent_type: AgentType) -> frozenset[str]:
    """
    Return the set of tool names this agent is allowed to call.

    WHY return frozenset (not list):
        frozenset membership check is O(1). Agents may call this on every
        tool invocation. O(1) keeps the hot path fast.
        Immutable return prevents callers from accidentally modifying policy.
    """
    scope = CAPABILITY_MAP.get(agent_type)
    if scope is None:
        # Unknown AgentType — return empty set (deny all).
        # This means a new agent added to AgentType enum without a CAPABILITY_MAP
        # entry gets zero tool access until explicitly scoped. Secure by default.
        logger.warning(
            "No capability scope defined for agent_type=%s. "
            "Returning empty scope (all tools denied). "
            "Add an entry to CAPABILITY_MAP in capability_scope.py.",
            agent_type,
        )
        return frozenset()
    return scope.allowed_tool_names


def check_capability(agent_type: AgentType, tool_name: str) -> bool:
    """
    Return True if agent_type is allowed to call tool_name.

    This is the fast-path check — does NOT raise an exception.
    Use raise_if_not_allowed() when you want the call to fail loudly.
    Use this when you want to branch logic without try/except.
    """
    return tool_name in get_allowed_tools(agent_type)


def raise_if_not_allowed(agent_type: AgentType, tool_name: str) -> None:
    """
    Raise CapabilityViolationError if agent_type is not allowed to call tool_name.

    Called by BaseAgent.call_tool() before every tool dispatch.
    This is the enforcement point.

    WHY raise (not return False):
        A tool call that violates scope is never a recoverable situation from the
        agent's perspective — it means either prompt injection or a deployment bug.
        Raising makes the violation immediately visible in logs, traces, and
        the ARQ worker's exception handler, rather than silently succeeding
        with an empty result.
    """
    allowed = get_allowed_tools(agent_type)
    if tool_name not in allowed:
        logger.error(
            "CAPABILITY_VIOLATION: agent=%s attempted tool=%s allowed=%s",
            agent_type.value,
            tool_name,
            sorted(allowed),
        )
        raise CapabilityViolationError(
            agent_type=agent_type,
            tool_name=tool_name,
            allowed=allowed,
        )

    logger.debug(
        "Capability check passed: agent=%s tool=%s",
        agent_type.value,
        tool_name,
    )
