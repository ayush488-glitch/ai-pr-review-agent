# backend/api/queue.py
#
# REST API endpoint for the review processing queue.
#
# ENDPOINTS:
#   GET /api/v1/queue              — reviews that are in-flight or awaiting HITL
#
# PHASE 19 STUBS (placeholder comments for HITL actions):
#   POST /api/v1/queue/{id}/approve  — human approves a HITL review
#   POST /api/v1/queue/{id}/reject   — human rejects a HITL review
#
# WHY A SEPARATE ROUTER FROM reviews.py?
# reviews.py is the read API for completed review history.
# queue.py is the operational API — it shows live system state and
# will grow into the HITL action API in Phase 19.
# Keeping them separate respects the Single Responsibility Principle and
# makes Phase 19 changes isolated (WIKI: CCP, Common-Closure-Principle,
# clean-architecture: "things that change together, stay together").
#
# DEPENDENCY DIRECTION:
#   queue.py imports from: database.repository, database.postgres,
#                          api.schemas, auth.dependencies
#   queue.py does NOT import from: agents, orchestrator, tools

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas import (
    QueueItem,
    QueueResponse,
    review_record_to_queue_item,
)
from backend.auth.dependencies import require_auth
from backend.database.postgres import get_db
from backend.database.repository import list_queue_items

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["queue"],
)


# =============================================================================
# GET /api/v1/queue
#
# Returns in-flight reviews and reviews awaiting human approval.
# =============================================================================

@router.get(
    "/queue",
    response_model=QueueResponse,
    summary="Get review queue",
    description=(
        "Returns reviews that are either currently being processed, "
        "or completed but awaiting human approval (needs_human_review=true). "
        "Results are sorted oldest-first (FIFO processing order). "
        "Use this endpoint to monitor pipeline health and identify HITL backlog."
    ),
)
async def get_queue_endpoint(
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Max items to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of items to skip (for pagination).",
    ),
    _auth: None = Depends(require_auth),
    session: AsyncSession = Depends(get_db),
) -> QueueResponse:
    """
    Get the current review queue.

    Returns two categories of items:
      1. Active reviews (status NOT IN completed, failed):
         These are moving through the pipeline. Normal to see these.
         If a review stays in the same status for >5 minutes, investigate.

      2. HITL-pending reviews (status=completed, needs_human_review=true):
         These completed but the agent had low confidence.
         A human reviewer must inspect and approve/reject via Phase 19 actions.

    Response shape:
        {
            "items": [...],    # list of QueueItem objects
            "total": 3,        # total items in queue (not just this page)
            "limit": 50,
            "offset": 0
        }
    """
    records, total = await list_queue_items(
        session,
        limit=limit,
        offset=offset,
    )

    # Convert to queue DTOs at the adapter boundary.
    items: list[QueueItem] = [review_record_to_queue_item(r) for r in records]

    logger.info(
        "GET /api/v1/queue | limit=%d offset=%d -> %d/%d items",
        limit, offset, len(items), total,
    )

    return QueueResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


# =============================================================================
# PHASE 19 PLACEHOLDERS
#
# The HITL action endpoints (approve, reject) belong here.
# They are NOT implemented yet — Phase 19 owns that work.
# The stubs are documented here so Phase 19 can find the right file.
# =============================================================================

# TODO Phase 19: POST /api/v1/queue/{review_id}/approve
#   - Load the review from Postgres
#   - Validate status == "completed" and needs_human_review == True
#   - Call github_client.post_review() with the approved findings
#   - Update review: needs_human_review=False, github_review_id=<id>
#   - Log the human approval decision to the audit trail (Phase 15)
#   - Require REVIEWER or ADMIN role (Phase 11 RBAC)

# TODO Phase 19: POST /api/v1/queue/{review_id}/reject
#   - Load the review from Postgres
#   - Validate status == "completed" and needs_human_review == True
#   - Mark all findings human_approved=False (do not post to GitHub)
#   - Update review: needs_human_review=False, status="dismissed"
#   - Log the human rejection decision to the audit trail (Phase 15)
#   - Require REVIEWER or ADMIN role (Phase 11 RBAC)

# TODO Phase 19: POST /api/v1/queue/{review_id}/findings/{finding_id}/dispute
#   - Called by the PR author to dispute a specific finding
#   - Creates a dispute record in Postgres
#   - Notifies the original reviewer (if HITL approved the finding)
#   - Feeds back into Phase 20 learning pipeline (disputed findings as negatives)
