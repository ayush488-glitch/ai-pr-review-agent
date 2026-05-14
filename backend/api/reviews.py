# backend/api/reviews.py
#
# REST API endpoints for PR review history.
#
# ENDPOINTS:
#   GET /api/v1/reviews              — paginated list of all reviews
#   GET /api/v1/reviews/{review_id}  — full detail for one review, with findings
#
# DESIGN NOTES:
#
# HUMBLE ROUTER PATTERN (WIKI: Humble-Object-Pattern, clean-architecture):
#   These route handlers contain minimal logic. Their job is:
#     1. Validate incoming query parameters (FastAPI handles this via types)
#     2. Call the repository (data fetch)
#     3. Convert ORM objects to DTOs (via schemas.py converters)
#     4. Return the response
#   Business decisions (routing, verdict logic) belong in the orchestrator.
#   SQL belongs in repository.py.
#   Type conversion belongs in schemas.py.
#   This file is the "humble shell" — thin, easy to read, hard to put logic in.
#
# CACHE-ASIDE READ FOR GET /reviews/{id} (WIKI: Polyglot-Persistence.md):
#   "Read from cache first. On miss, read from Postgres. Write back to cache."
#   For a live review (status = in_progress, agents_running), the most
#   up-to-date STATUS is in Redis (set by the worker every few seconds).
#   But full review data (findings, verdict) only exists in Postgres.
#   Strategy:
#     - Always fetch from Postgres (the authoritative store for full data).
#     - Overlay the Redis-cached status if available (fresher than Postgres).
#   This gives us fast live status WITHOUT needing a full Postgres row update
#   on every intermediate state transition.
#
# VERSIONED PREFIX (/api/v1/):
#   All endpoints are under /api/v1/ so we can introduce /api/v2/ without
#   breaking existing clients. The version is in the path, not a header —
#   header versioning is harder to route at the load balancer level.
#   (Evolvability principle, DDIA wiki.)
#
# DEPENDENCY DIRECTION:
#   reviews.py imports from: database.repository, database.postgres,
#                            api.schemas, auth.dependencies, config.settings,
#                            memory.redis_client
#   reviews.py does NOT import from: agents, orchestrator, tools

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.schemas import (
    ReviewDetail,
    ReviewListResponse,
    ReviewSummary,
    review_record_to_detail,
    review_record_to_summary,
)
from backend.auth.dependencies import require_auth
from backend.database.postgres import get_db
from backend.database.repository import get_review, list_reviews
from backend.memory.redis_client import redis_client

logger = logging.getLogger(__name__)

# Router with versioned prefix.
# Registered in main.py: app.include_router(reviews_router)
router = APIRouter(
    prefix="/api/v1",
    tags=["reviews"],
)


# =============================================================================
# GET /api/v1/reviews
#
# Returns a paginated list of review summaries.
# =============================================================================

@router.get(
    "/reviews",
    response_model=ReviewListResponse,
    summary="List PR reviews",
    description=(
        "Returns a paginated list of PR reviews. "
        "Filter by repo (owner/repo format) or status. "
        "Results are sorted newest-first."
    ),
)
async def list_reviews_endpoint(
    # Optional filters
    repo: str | None = Query(
        default=None,
        description="Filter by repository (e.g. 'owner/repo'). Omit to return all repos.",
        examples=["acme-corp/payment-service"],
    ),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=(
            "Filter by review status. "
            "Values: received, queued, in_progress, agents_running, "
            "aggregating, posting, completed, failed."
        ),
        examples=["completed"],
    ),
    # Pagination
    limit: int = Query(
        default=50,
        ge=1,
        le=200,
        description="Max reviews to return per page. Max 200.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of reviews to skip (for pagination).",
    ),
    # Dependencies
    _auth: None = Depends(require_auth),      # enforces API key
    session: AsyncSession = Depends(get_db),  # injects DB session
) -> ReviewListResponse:
    """
    List PR reviews with optional filters and pagination.

    Response shape:
        {
            "items": [...],       # list of ReviewSummary objects
            "total": 42,          # total matching reviews (not just this page)
            "limit": 50,
            "offset": 0
        }

    Pagination: use offset to page through results.
        Page 1: offset=0,  limit=50  -> items 1-50
        Page 2: offset=50, limit=50  -> items 51-100
    """
    records, total = await list_reviews(
        session,
        repo_full_name=repo,
        status=status_filter,
        limit=limit,
        offset=offset,
    )

    # Convert ORM objects to DTOs at the adapter boundary.
    # WIKI: Interface Adapters Layer — "convert to the form convenient for the caller."
    summaries: list[ReviewSummary] = [review_record_to_summary(r) for r in records]

    logger.info(
        "GET /api/v1/reviews | repo=%s status=%s limit=%d offset=%d -> %d/%d",
        repo, status_filter, limit, offset, len(summaries), total,
    )

    return ReviewListResponse(
        items=summaries,
        total=total,
        limit=limit,
        offset=offset,
    )


# =============================================================================
# GET /api/v1/reviews/{review_id}
#
# Returns the full review detail, including all findings.
# =============================================================================

@router.get(
    "/reviews/{review_id:path}",
    response_model=ReviewDetail,
    summary="Get PR review detail",
    description=(
        "Returns the full review for a given review ID, including all findings. "
        "For in-progress reviews, the status field reflects the live state "
        "from the Redis cache if available."
    ),
    responses={
        404: {"description": "Review not found."},
    },
)
async def get_review_endpoint(
    review_id: str,
    _auth: None = Depends(require_auth),
    session: AsyncSession = Depends(get_db),
) -> ReviewDetail:
    """
    Get full review detail by ID.

    CACHE-ASIDE STATUS OVERLAY:
    The returned status is the Redis-cached value if available (fresher for
    in-progress reviews), falling back to the Postgres-stored value.
    All other fields come from Postgres (the authoritative durable store).

    Returns 404 if no review with this ID exists.
    """
    # Step 1: Fetch from Postgres (full data, authoritative).
    # get_review() also loads findings via selectin (no N+1 query).
    record = await get_review(session, review_id)

    if record is None:
        logger.info("GET /api/v1/reviews/%s -> 404", review_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Review '{review_id}' not found.",
        )

    # Step 2: Cache-aside status overlay.
    # For in-progress reviews, the worker updates Redis on every state
    # transition (set_workflow_status in arq_worker.py). Redis is faster
    # to update than Postgres (the worker skips a DB write mid-flight).
    # We overlay the Redis status on top of the Postgres record.
    #
    # WIKI: Polyglot-Persistence.md — "Redis = ephemeral cache,
    # Postgres = audit log. Cache holds current status only."
    #
    # Try both cache keys: review:status:{id} (set by orchestrator nodes)
    # and workflow:status:{id} (set by arq_worker.py).
    live_status = await redis_client.get_cached_review_status(review_id)
    if live_status is None:
        # Fallback: check the workflow:status key set by the ARQ worker
        live_status = await redis_client.get_workflow_status(review_id)

    # Mutate the ORM object's status field before converting to DTO.
    # This is safe — we are not persisting this change back to Postgres.
    # The mutation only affects this in-memory object for the duration
    # of this request. SQLAlchemy does not flush unless we call commit().
    if live_status and live_status != record.status:
        logger.debug(
            "GET /api/v1/reviews/%s | overlaying Redis status=%s over Postgres status=%s",
            review_id, live_status, record.status,
        )
        record.status = live_status

    # Step 3: Convert ORM -> DTO at the adapter boundary.
    detail = review_record_to_detail(record)

    logger.info(
        "GET /api/v1/reviews/%s -> status=%s verdict=%s findings=%d",
        review_id, detail.status, detail.verdict, len(detail.findings),
    )

    return detail
