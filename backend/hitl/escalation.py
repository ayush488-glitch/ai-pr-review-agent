# backend/hitl/escalation.py
#
# HITL Escalation Policy — Phase 19.
#
# RESPONSIBILITY:
#   Given the output of aggregate_results, decide WHETHER a review should be
#   escalated to the HITL queue instead of auto-posted to GitHub.
#
# DESIGN (Clean-Architecture.md wiki — Business-Rules):
#   This module is a pure ENTITY — escalation rules are Critical Business Rules
#   that exist independent of any delivery mechanism.
#   - Zero imports from FastAPI, Starlette, or HTTP types.
#   - Zero database calls. Operates only on plain Python data.
#   - The orchestrator (nodes.py) calls should_escalate() and passes the result
#     to queue.py if True. Dependency flows inward.
#
# ESCALATION RULES (locked in Phase 0 cognitive design, threshold adjusted Phase 18):
#   Rule 1: Security agent failed       -> cannot safely approve -> HITL
#   Rule 2: 3+ agents say CRITICAL      -> Safety-Threshold rule -> HITL
#   Rule 3: Overall confidence < 0.40   -> agent is uncertain    -> HITL
#   Rule 4: Only 1 agent succeeded      -> too little data        -> HITL
#
# WHY THESE RULES LIVE HERE (not in nodes.py):
#   nodes.py already has escalation logic but it is INLINE — interleaved with
#   LangGraph state manipulation. That makes the rules hard to test, reuse, or
#   change independently. This module extracts the rules into a named, testable
#   function. nodes.py will call this function in Phase 19 instead of re-implementing.
#
# THRESHOLD HISTORY:
#   Phase 8:  threshold = 2+ CRITICAL (too aggressive, hid reviews silently)
#   Phase 18: threshold = 3+ CRITICAL (demo-day-readiness pitfall #34)
#   Phase 19: threshold = 3+ CRITICAL (confirmed, no change)

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EscalationResult
#
# Plain dataclass — no DB, no HTTP. The output of should_escalate().
# (Clean-Architecture Business-Rules: "Use simple data structures for
#  use case input/output. No framework coupling.")
# ---------------------------------------------------------------------------
@dataclass
class EscalationResult:
    """
    The verdict of the escalation policy check.

    should_escalate: True  -> route to HITL queue.
    should_escalate: False -> auto-post to GitHub.
    rule_triggered:  which rule fired (for logging + HITL row).
    reason:          human-readable explanation (stored on HITLReview row).
    """
    should_escalate: bool
    rule_triggered: str   # "none", "rule1_security_failure", "rule2_critical_threshold",
                          #  "rule3_low_confidence", "rule4_insufficient_agents"
    reason: str


# ---------------------------------------------------------------------------
# Escalation thresholds (module-level constants so they can be overridden
# in tests without monkey-patching buried logic)
# ---------------------------------------------------------------------------
CRITICAL_AGENT_THRESHOLD = 2   # rule2: 2+ agents CRITICAL -> HITL
CONFIDENCE_THRESHOLD = 0.40    # rule3: confidence < 0.40  -> HITL
MIN_SUCCESSFUL_AGENTS = 2      # rule4: <2 successful       -> HITL


def should_escalate(
    *,
    security_agent_failed: bool,
    critical_agent_count: int,
    overall_confidence: float,
    successful_agent_count: int,
    total_agent_count: int,
) -> EscalationResult:
    """
    Apply escalation policy rules to the aggregate_results output.

    All parameters are keyword-only to prevent positional argument confusion.
    Returns EscalationResult — caller decides what to do with it.

    Args:
        security_agent_failed:   True if the security agent raised an exception
                                 or returned an error verdict.
        critical_agent_count:    Number of agents that returned CRITICAL_BLOCK verdict.
        overall_confidence:      Weighted-average confidence across all agents (0.0–1.0).
        successful_agent_count:  Number of agents that completed without error.
        total_agent_count:       Total agents invoked (normally 4).

    Returns:
        EscalationResult with should_escalate=True if any rule fired.
    """

    # Rule 1: Security agent failure.
    # WHY: A failed security agent means we have no safety analysis.
    # We cannot auto-approve a PR with no security review.
    # (Hierarchical-Agent-Systems.md: "Security boundaries are hard to retrofit.
    #  Design them in from the beginning.")
    if security_agent_failed:
        reason = (
            "Security agent failed — no safety analysis available. "
            "Auto-approval would bypass security review entirely."
        )
        logger.info(
            "hitl_escalation | rule=rule1_security_failure | escalating | reason=%s",
            reason,
        )
        return EscalationResult(
            should_escalate=True,
            rule_triggered="rule1_security_failure",
            reason=reason,
        )

    # Rule 4: Not enough successful agents.
    # WHY: If fewer than MIN_SUCCESSFUL_AGENTS finished, the review is too incomplete
    # to trust. One miscalibrated agent with no counterpart can't be auto-approved.
    # Check Rule 4 before Rule 2 so that the more informative reason fires first
    # when agent count is low AND critical count happens to be high.
    if successful_agent_count < MIN_SUCCESSFUL_AGENTS:
        reason = (
            f"Only {successful_agent_count}/{total_agent_count} agents completed "
            f"(minimum {MIN_SUCCESSFUL_AGENTS} required). "
            "Insufficient data for automated verdict."
        )
        logger.info(
            "hitl_escalation | rule=rule4_insufficient_agents | "
            "successful=%d total=%d | escalating",
            successful_agent_count, total_agent_count,
        )
        return EscalationResult(
            should_escalate=True,
            rule_triggered="rule4_insufficient_agents",
            reason=reason,
        )

    # Rule 2: Critical threshold.
    # WHY: Multiple independent agents all flagging CRITICAL suggests a real,
    # high-severity issue. Threshold=3 prevents one miscalibrated agent from
    # flooding the HITL queue.
    # (demo-day-readiness pitfall #34: threshold=2 was too aggressive.)
    if critical_agent_count >= CRITICAL_AGENT_THRESHOLD:
        reason = (
            f"{critical_agent_count} agents returned CRITICAL_BLOCK verdict "
            f"(threshold: {CRITICAL_AGENT_THRESHOLD}). "
            "High-severity issues require human sign-off."
        )
        logger.info(
            "hitl_escalation | rule=rule2_critical_threshold | "
            "critical_count=%d threshold=%d | escalating",
            critical_agent_count, CRITICAL_AGENT_THRESHOLD,
        )
        return EscalationResult(
            should_escalate=True,
            rule_triggered="rule2_critical_threshold",
            reason=reason,
        )

    # Rule 3: Low confidence.
    # WHY: A low ensemble confidence means the agents disagree or are uncertain.
    # Uncertain auto-verdicts erode trust in the system.
    if overall_confidence < CONFIDENCE_THRESHOLD:
        reason = (
            f"Overall confidence {overall_confidence:.2f} is below threshold "
            f"{CONFIDENCE_THRESHOLD:.2f}. "
            "Agent ensemble too uncertain for automated verdict."
        )
        logger.info(
            "hitl_escalation | rule=rule3_low_confidence | "
            "confidence=%.2f threshold=%.2f | escalating",
            overall_confidence, CONFIDENCE_THRESHOLD,
        )
        return EscalationResult(
            should_escalate=True,
            rule_triggered="rule3_low_confidence",
            reason=reason,
        )

    # No rule fired — safe to auto-post.
    logger.debug(
        "hitl_escalation | no_escalation | "
        "security_ok=True critical=%d confidence=%.2f agents=%d/%d",
        critical_agent_count, overall_confidence,
        successful_agent_count, total_agent_count,
    )
    return EscalationResult(
        should_escalate=False,
        rule_triggered="none",
        reason="",
    )
