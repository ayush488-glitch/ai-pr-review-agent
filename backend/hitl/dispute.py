# backend/hitl/dispute.py
#
# HITL Dispute Use Case — Phase 19.
#
# RESPONSIBILITY:
#   Handle a human reviewer's decision on a HITL-escalated review.
#   Apply the decision atomically, post to GitHub, trigger feedback recording.
#
# DESIGN (Clean-Architecture.md wiki — Business-Rules):
#   This is a USE CASE, not an Entity.
#   It orchestrates:
#     - HITLReview (Entity) — status transition
#     - GitHub client — post verdict (delivery mechanism, wrapped as detail)
#     - feedback.py — record training signal
#   It does NOT import from FastAPI or the HTTP layer.
#   hitl_router.py calls resolve_dispute() with plain data — no HTTP types in.
#
# ATOMIC STATUS TRANSITION (Transactions-and-Isolation.md wiki):
#   Two concurrent reviewers must not both claim the same HITL item.
#   Pattern: SELECT FOR UPDATE inside a transaction.
#   First reviewer wins; second sees the row already transitioned and gets
#   a DisputeAlreadyResolved exception.
#   (Transactions-and-Isolation.md: "Lock the row before the read-modify-write.")
#
# VERDICT POSTING (demo-day-readiness Bug #5 pattern):
#   DB state update FIRST. GitHub post SECOND.
#   If GitHub fails, the HITLReview is still marked resolved in Postgres
#   and feedback is still recorded.
#   posted_to_github=False persists for visibility — the review can be
#   retried via the retry endpoint (Phase 17).

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import HITLReview
from backend.hitl.feedback import record_feedback

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions — plain Python, no HTTP status codes.
# hitl_router translates these to HTTP responses.
# (Clean-Architecture: Use Cases must not know about HTTP.)
# ---------------------------------------------------------------------------
class HITLReviewNotFound(Exception):
    """Raised when the requested HITLReview does not exist."""


class DisputeAlreadyResolved(Exception):
    """Raised when two concurrent reviewers try to resolve the same item."""
    def __init__(self, current_status: str):
        self.current_status = current_status
        super().__init__(f"Review already in status '{current_status}'.")


class InvalidVerdict(Exception):
    """Raised when the human provides an unrecognised verdict value."""


# ---------------------------------------------------------------------------
# DisputeRequest / DisputeResult
#
# Plain dataclasses — input/output of resolve_dispute().
# No FastAPI/Pydantic models here (those live in hitl_router.py).
# (Clean-Architecture: "Request/response models must be independent.")
# ---------------------------------------------------------------------------
VALID_HUMAN_VERDICTS = frozenset({"approve", "request_changes", "dismiss"})


@dataclass
class DisputeRequest:
    """Input to resolve_dispute(). All fields plain Python primitives."""
    hitl_review_id: str
    human_verdict: str          # "approve" / "request_changes" / "dismiss"
    reason: str                 # required on override, optional on confirmation
    reviewer_id: str            # GitHub handle, email, or system identifier


@dataclass
class DisputeResult:
    """Output of resolve_dispute()."""
    hitl_review_id: str
    previous_status: str
    new_status: str
    human_verdict: str
    posted_to_github: bool
    feedback_id: str            # UUID of the HITLFeedback row created


# ---------------------------------------------------------------------------
# resolve_dispute
#
# The core Use Case. Called by hitl_router.py.
#
# Steps:
#   1. Validate human_verdict is a known value.
#   2. Fetch HITLReview row with SELECT FOR UPDATE (atomic claim).
#   3. Assert status is still resolvable (not already resolved by another reviewer).
#   4. Apply new status + human verdict.
#   5. Commit DB transaction.
#   6. Record feedback signal (Phase 20 training data).
#   7. Post to GitHub (best-effort).
#   Returns DisputeResult.
# ---------------------------------------------------------------------------
async def resolve_dispute(
    session: AsyncSession,
    *,
    github_client,      # backend.github.client.GitHubClient — type-hint avoided
                        # to prevent circular import. Duck-typed: must have
                        # post_review(repo, pr_number, verdict, body) method.
    request: DisputeRequest,
) -> DisputeResult:
    """
    Apply a human reviewer's decision to a HITL-escalated review.

    Atomically transitions the HITLReview status, records feedback, and
    posts the final verdict to GitHub.

    Args:
        session:        Async DB session (FastAPI Depends(get_db)).
        github_client:  GitHub API client for posting the review.
        request:        DisputeRequest with reviewer's decision.

    Returns:
        DisputeResult describing what happened.

    Raises:
        HITLReviewNotFound:      If hitl_review_id doesn't exist in DB.
        DisputeAlreadyResolved:  If another reviewer already claimed this item.
        InvalidVerdict:          If human_verdict is not a known value.
    """

    # Step 1: Validate verdict value.
    # (Clean-Architecture Business-Rules: "business rules enforced in use case layer")
    if request.human_verdict not in VALID_HUMAN_VERDICTS:
        raise InvalidVerdict(
            f"'{request.human_verdict}' is not a valid verdict. "
            f"Must be one of: {sorted(VALID_HUMAN_VERDICTS)}"
        )

    # Step 2: Fetch + lock the HITLReview row.
    # SELECT FOR UPDATE prevents two concurrent reviewers from both claiming it.
    # (Transactions-and-Isolation.md: "Lock before read-modify-write.")
    #
    # NOTE: with_for_update() requires the session to be in a transaction.
    # FastAPI's get_db() yields a session with autobegin — the outer transaction
    # is already started on first use. Calling session.begin() again raises
    # InvalidRequestError. Use begin_nested() (SAVEPOINT) instead so we can
    # flush atomically without fighting the outer autobegin transaction.
    # We commit the outer transaction explicitly after the nested block exits.
    async with session.begin_nested():
        result = await session.execute(
            select(HITLReview)
            .where(HITLReview.id == request.hitl_review_id)
            .with_for_update()
        )
        hitl_review = result.scalar_one_or_none()

        if hitl_review is None:
            raise HITLReviewNotFound(
                f"HITLReview '{request.hitl_review_id}' not found."
            )

        # Step 3: Assert still resolvable.
        # Terminal statuses: "approved", "rejected", "dismissed".
        # (Transactions-and-Isolation.md: "check invariant inside the lock")
        previous_status = hitl_review.status
        if previous_status in ("approved", "rejected", "dismissed"):
            raise DisputeAlreadyResolved(previous_status)

        # Step 4: Apply new status + human verdict.
        status_map = {
            "approve": "approved",
            "request_changes": "rejected",   # rejected = agent was right to flag
            "dismiss": "dismissed",
        }
        new_status = status_map[request.human_verdict]

        hitl_review.status = new_status
        hitl_review.human_verdict = request.human_verdict
        hitl_review.human_reason = request.reason
        hitl_review.reviewer_id = request.reviewer_id
        hitl_review.resolved_at = datetime.now(timezone.utc)

        # Step 5: Flush within savepoint — savepoint commits on nested block exit.
        # (demo-day-readiness Bug #5 pattern: save first, post second.)
        await session.flush()

    # Commit the outer autobegin transaction so the DB state is durable
    # before we do GitHub post + feedback recording.
    await session.commit()

    logger.info(
        "hitl_dispute | resolved | hitl_id=%s status=%s->%s "
        "human_verdict=%s reviewer=%s",
        request.hitl_review_id, previous_status, new_status,
        request.human_verdict, request.reviewer_id,
    )

    # Step 6: Record feedback (Phase 20 training signal).
    # Build context snapshot from the findings stored on the HITL row.
    feedback_id = await record_feedback(
        hitl_review_id=request.hitl_review_id,
        repo_full_name=hitl_review.repo_full_name,
        pr_number=hitl_review.pr_number,
        agent_verdict=hitl_review.agent_verdict,
        human_verdict=request.human_verdict,
        reason=request.reason,
        context_snapshot=hitl_review.findings_snapshot,   # already JSON
    )

    # Step 7: Post to GitHub (best-effort).
    # We attempt to post the human's verdict to GitHub as a PR review.
    # If GitHub fails (401, 422, 404), we log and continue — the human decision
    # is already committed to Postgres. The operator can retry via the retry endpoint.
    #
    # Real method: github_client.post_pr_review(repo, pr_number, payload: PostReviewPayload).
    # PostReviewPayload requires commit_id (head SHA). HITLReview.review_id has
    # format "owner/repo:pr_number:commit_sha" — extract the SHA from there.
    posted = False
    try:
        from backend.integrations.github_models import (
            PostReviewPayload,
            ReviewEvent,
        )

        # Map human verdict to GitHub review event.
        # (demo-day-readiness pitfall #35: REQUEST_CHANGES fails when reviewer == PR author.
        #  For HITL we use the same bot account rule. If bot account not set up,
        #  fall back to COMMENT as we did in Phase 8/18.)
        github_event_str = _human_verdict_to_github_event(request.human_verdict)
        github_event = ReviewEvent(github_event_str)
        body = _build_github_review_body(
            hitl_review=hitl_review,
            human_verdict=request.human_verdict,
            reason=request.reason,
            reviewer_id=request.reviewer_id,
        )

        # Extract commit SHA from review_id (format: owner/repo:pr:sha).
        # Fallback: empty string -> GitHub uses latest HEAD.
        commit_id = ""
        try:
            parts = hitl_review.review_id.split(":")
            if len(parts) >= 3:
                commit_id = parts[2]
        except Exception:
            commit_id = ""

        payload = PostReviewPayload(
            commit_id=commit_id,
            body=body,
            event=github_event,
            comments=[],   # no inline comments (Phase 17 restores those)
        )

        await github_client.post_pr_review(
            repo_full_name=hitl_review.repo_full_name,
            pr_number=hitl_review.pr_number,
            payload=payload,
        )

        # Mark posted in DB (non-atomic update — best effort).
        from backend.database.postgres import get_session_factory as _sf
        async with _sf()() as update_session:
            async with update_session.begin():
                r2 = await update_session.execute(
                    select(HITLReview).where(HITLReview.id == request.hitl_review_id)
                )
                row = r2.scalar_one_or_none()
                if row:
                    row.posted_to_github = True

        posted = True
        logger.info(
            "hitl_dispute | posted_to_github | hitl_id=%s pr=%d event=%s",
            request.hitl_review_id, hitl_review.pr_number, github_event_str,
        )
    except Exception as gh_err:
        logger.warning(
            "hitl_dispute | github_post_failed | hitl_id=%s error=%s | "
            "verdict committed to DB, retry available",
            request.hitl_review_id, gh_err,
        )

    return DisputeResult(
        hitl_review_id=request.hitl_review_id,
        previous_status=previous_status,
        new_status=new_status,
        human_verdict=request.human_verdict,
        posted_to_github=posted,
        feedback_id=feedback_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _human_verdict_to_github_event(human_verdict: str) -> str:
    """
    Map human verdict to GitHub review event string.

    REQUEST_CHANGES is used when a dedicated bot account is configured
    (bot account != PR author, so GitHub allows REQUEST_CHANGES).
    Falls back to COMMENT as a safe default.

    (demo-day-readiness pitfall #35: GitHub 422 when reviewer == PR author.)
    """
    # TODO (Phase 19 proper): detect whether GITHUB_BOT_ACCOUNT is set.
    # If bot account configured, use APPROVE / REQUEST_CHANGES.
    # For now, always COMMENT — GitHub returns 422 if reviewer == PR author,
    # which is the case when running with the developer's own GITHUB_TOKEN.
    # The DB still records the true human_verdict; only the GitHub-visible
    # event is downgraded to COMMENT to avoid 422.
    mapping = {
        "approve": "COMMENT",          # COMMENT until dedicated bot account
        "request_changes": "COMMENT",  # COMMENT until dedicated bot account
        "dismiss": "COMMENT",
    }
    return mapping.get(human_verdict, "COMMENT")


def _build_github_review_body(
    *,
    hitl_review: HITLReview,
    human_verdict: str,
    reason: str,
    reviewer_id: str,
) -> str:
    """Build the GitHub review comment body for a human HITL decision."""
    verdict_label = {
        "approve": "APPROVED",
        "request_changes": "CHANGES REQUESTED",
        "dismiss": "DISMISSED",
    }.get(human_verdict, human_verdict.upper())

    return (
        f"## Human Review Decision — {verdict_label}\n\n"
        f"**Reviewer:** {reviewer_id}  \n"
        f"**Decision:** {verdict_label}  \n"
        f"**Reason:** {reason or '_No reason provided._'}\n\n"
        f"---\n"
        f"_This review was escalated from the automated AI PR Review Agent._  \n"
        f"_Escalation reason: {hitl_review.escalation_reason}_  \n"
        f"_Agent confidence: {hitl_review.overall_confidence:.0%}_  \n"
        f"_Original agent verdict: {hitl_review.agent_verdict}_"
    )
