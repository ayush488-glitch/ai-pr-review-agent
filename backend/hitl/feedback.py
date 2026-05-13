# backend/hitl/feedback.py
#
# HITL Feedback — Phase 19.
#
# RESPONSIBILITY:
#   Persist a human HITL decision as a labelled training signal (HITLFeedback row).
#   This is the data pipeline entry point for Phase 20 (Continuous Learning).
#
# DESIGN (Derived-Data-Systems.md wiki):
#   HITLFeedback is DERIVED data.
#   Source of truth: HITLReview row.
#   This module reformats the human decision into a shape suitable for
#   offline dataset construction (Phase 20 fine-tuning pipeline).
#   Can be rebuilt from HITLReview rows at any time.
#
# FEEDBACK TYPES:
#   "override"     — human changed the agent's verdict (agent was wrong)
#   "confirmation" — human agreed with the agent (agent was right)
#   "dismiss"      — human dismissed without a clear verdict (inconclusive)
#
#   Phase 20's reflection loop uses OVERRIDE rows to detect systematic errors.
#   CONFIRMATION rows are also useful — they tell us what the agent got right.
#   (LLMOps-Essentials.md wiki: "Human feedback is the ground truth signal
#    for alignment. Record it faithfully.")
#
# PHASE 20 CONTRACT:
#   This module's output schema (HITLFeedback columns) is stable from Phase 19.
#   Phase 20 reads this table directly — do not rename columns.
#   Add new columns additively if needed (Encoding-and-Schema-Evolution.md).

import logging
import uuid

from backend.database.models import HITLFeedback
from backend.database.postgres import get_session_factory

logger = logging.getLogger(__name__)


async def record_feedback(
    *,
    hitl_review_id: str,
    repo_full_name: str,
    pr_number: int,
    agent_verdict: str,
    human_verdict: str,
    reason: str,
    context_snapshot: str,      # JSON string of findings at decision time
) -> str:
    """
    Persist a HITLFeedback row as a training signal.

    Determines feedback_type (override / confirmation / dismiss) from the
    agent vs human verdict comparison, then writes to DB.

    Args:
        hitl_review_id:   FK to the parent HITLReview.
        repo_full_name:   Denormalized repo name.
        pr_number:        PR number.
        agent_verdict:    What the agents decided.
        human_verdict:    What the human decided ("approve" / "request_changes" / "dismiss").
        reason:           Human's stated reason (empty string if none provided).
        context_snapshot: JSON-serialized findings snapshot from HITLReview row.

    Returns:
        feedback_id: str — UUID of the newly created HITLFeedback row.
    """

    # Classify the feedback type.
    feedback_type = _classify_feedback(
        agent_verdict=agent_verdict,
        human_verdict=human_verdict,
    )

    feedback_id = str(uuid.uuid4())

    # Write HITLFeedback row.
    # Uses get_session_factory() — NOT get_db() — because this may be called
    # from the ARQ worker (outside FastAPI request context).
    # (demo-day-readiness pitfall #2: get_db() is a generator, not context manager)
    session_factory = get_session_factory()

    try:
        async with session_factory() as session:
            async with session.begin():
                feedback = HITLFeedback(
                    id=feedback_id,
                    hitl_review_id=hitl_review_id,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    agent_verdict=agent_verdict,
                    human_verdict=human_verdict,
                    feedback_type=feedback_type,
                    reason=reason,
                    context_snapshot=context_snapshot,
                )
                session.add(feedback)

        logger.info(
            "hitl_feedback | recorded | id=%s hitl_id=%s type=%s "
            "agent=%s human=%s",
            feedback_id, hitl_review_id, feedback_type,
            agent_verdict, human_verdict,
        )
        return feedback_id

    except Exception as err:
        # Feedback recording failure must NOT propagate up and block the dispute resolution.
        # The human's decision is already committed. Feedback is best-effort here.
        # (Stability-Patterns.md: "Failures are inevitable. Contain the damage.")
        #
        # NOTE: If this fails repeatedly, Phase 20 will have incomplete training data.
        # Monitor this path. Add a retry queue in Phase 20 if needed.
        logger.error(
            "hitl_feedback | record_failed | hitl_id=%s error=%s | "
            "dispute already committed, feedback lost for this review",
            hitl_review_id, err,
        )
        # Return a stub ID so DisputeResult.feedback_id is always a string.
        return f"failed:{feedback_id}"


def _classify_feedback(
    *,
    agent_verdict: str,
    human_verdict: str,
) -> str:
    """
    Classify the type of feedback signal.

    "override"     — human changed the verdict (the key learning signal)
    "confirmation" — human agreed with the agent
    "dismiss"      — human dismissed (inconclusive)

    Override detection: agent said X, human said Y, X != Y.
    Normalise both to 3-class space first:
      agent:  "approve" / "request_changes" / "needs_human_review"
      human:  "approve" / "request_changes" / "dismiss"
    """
    if human_verdict == "dismiss":
        return "dismiss"

    # Normalise agent verdict to 2-class: approve or request_changes.
    # "needs_human_review" is the HITL trigger — not a direct verdict.
    # Treat it as "request_changes" (the agent flagged issues).
    agent_normalised = (
        "request_changes"
        if agent_verdict in ("request_changes", "needs_human_review", "critical_block")
        else "approve"
    )

    if human_verdict == agent_normalised:
        return "confirmation"
    else:
        return "override"
