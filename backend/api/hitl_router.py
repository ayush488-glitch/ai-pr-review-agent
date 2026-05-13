# backend/api/hitl_router.py
#
# HITL REST API — Phase 19.
#
# ENDPOINTS:
#   GET  /api/v1/hitl/queue               — list pending HITL items
#   GET  /api/v1/hitl/{id}                — detail for one HITL item
#   POST /api/v1/hitl/{id}/decision       — submit human verdict
#   POST /api/v1/hitl/queue/rebuild       — re-hydrate Redis from Postgres
#
# HUMBLE ROUTER PATTERN (clean-architecture wiki — Humble-Object-Pattern):
#   These handlers contain minimal logic:
#     1. Parse/validate request.
#     2. Call into backend.hitl Use Case layer.
#     3. Map Use Case exceptions to HTTP errors.
#     4. Return response DTO.
#   Business logic lives in backend.hitl.dispute / queue / escalation.
#   This file is the delivery mechanism — thin, easy to test.
#
# DEPENDENCY DIRECTION (Clean-Architecture Dependency-Rule):
#   hitl_router.py imports from: backend.hitl (Use Case layer)
#   backend.hitl does NOT import from: backend.api (delivery layer)
#   Arrows point inward: router -> hitl -> models/postgres
#
# VERSIONING:
#   All endpoints under /api/v1/hitl/ — consistent with reviews, queue routers.

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import HITLReview
from backend.database.postgres import get_db
from backend.hitl.dispute import (
    DisputeAlreadyResolved,
    DisputeRequest,
    DisputeResult,
    HITLReviewNotFound,
    InvalidVerdict,
    resolve_dispute,
)
from backend.hitl.queue import get_hitl_review, get_pending_queue, rebuild_redis_queue

logger = logging.getLogger(__name__)

hitl_router = APIRouter(prefix="/api/v1/hitl", tags=["hitl"])


# ---------------------------------------------------------------------------
# Request / Response schemas
#
# Pydantic models live here, NOT in the Use Case layer.
# (Clean-Architecture: "Request/response models must be independent of use cases.")
# ---------------------------------------------------------------------------

class HITLDecisionRequest(BaseModel):
    """Body for POST /api/v1/hitl/{id}/decision."""
    human_verdict: str = Field(
        ...,
        description="Reviewer's verdict: 'approve', 'request_changes', or 'dismiss'.",
    )
    reason: str = Field(
        default="",
        description="Reviewer's explanation. Required when verdict is 'request_changes'.",
    )
    reviewer_id: str = Field(
        ...,
        description="Reviewer identity: GitHub handle, email, or system ID.",
    )


class HITLItemResponse(BaseModel):
    """Response DTO for a single HITL queue item."""
    id: str
    review_id: str
    repo_full_name: str
    pr_number: int
    agent_verdict: str
    human_verdict: str | None
    status: str
    escalation_reason: str
    overall_confidence: float
    posted_to_github: bool
    created_at: str
    resolved_at: str | None

    @classmethod
    def from_orm(cls, row: HITLReview) -> "HITLItemResponse":
        return cls(
            id=row.id,
            review_id=row.review_id,
            repo_full_name=row.repo_full_name,
            pr_number=row.pr_number,
            agent_verdict=row.agent_verdict,
            human_verdict=row.human_verdict,
            status=row.status,
            escalation_reason=row.escalation_reason,
            overall_confidence=row.overall_confidence,
            posted_to_github=bool(row.posted_to_github),
            created_at=row.created_at.isoformat(),
            resolved_at=row.resolved_at.isoformat() if row.resolved_at else None,
        )


class HITLDecisionResponse(BaseModel):
    """Response DTO for POST /api/v1/hitl/{id}/decision."""
    hitl_review_id: str
    previous_status: str
    new_status: str
    human_verdict: str
    posted_to_github: bool
    feedback_id: str


class HITLQueueResponse(BaseModel):
    """Response DTO for GET /api/v1/hitl/queue."""
    items: list[HITLItemResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# GET /api/v1/hitl/queue
#
# List pending HITL items. Optionally filter by repo.
# Reads from Postgres (system of record) — not Redis.
# (Derived-Data-Systems.md: "If there is discrepancy, Postgres wins.")
# ---------------------------------------------------------------------------
@hitl_router.get(
    "/queue",
    response_model=HITLQueueResponse,
    summary="List pending HITL reviews",
)
async def list_hitl_queue(
    repo: str | None = Query(default=None, description="Filter by repo (owner/repo)."),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> HITLQueueResponse:
    """
    Return all pending and in-review HITL items.

    Ordered oldest-first (FIFO queue semantics).
    """
    items = await get_pending_queue(
        session,
        repo_full_name=repo,
        limit=limit,
        offset=offset,
    )
    return HITLQueueResponse(
        items=[HITLItemResponse.from_orm(item) for item in items],
        total=len(items),
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/hitl/{hitl_id}
#
# Detail view for one HITL item. Includes findings_snapshot.
# ---------------------------------------------------------------------------
@hitl_router.get(
    "/{hitl_id}",
    response_model=dict[str, Any],
    summary="Get HITL item detail",
)
async def get_hitl_item(
    hitl_id: str,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Return full detail for a HITL item, including the findings snapshot.
    """
    row = await get_hitl_review(session, hitl_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"HITL review '{hitl_id}' not found.",
        )

    import json as _json
    findings = []
    if row.findings_snapshot:
        try:
            findings = _json.loads(row.findings_snapshot)
        except Exception:
            findings = []

    return {
        **HITLItemResponse.from_orm(row).model_dump(),
        "findings": findings,
        "human_reason": row.human_reason,
        "reviewer_id": row.reviewer_id,
    }


# ---------------------------------------------------------------------------
# POST /api/v1/hitl/{hitl_id}/decision
#
# Submit a human verdict for a HITL-escalated review.
# Atomically transitions status, records feedback, posts to GitHub.
#
# Uses resolve_dispute() from hitl/dispute.py (Use Case layer).
# Maps Use Case exceptions to HTTP errors — this is the delivery mechanism's job.
# ---------------------------------------------------------------------------
@hitl_router.post(
    "/{hitl_id}/decision",
    response_model=HITLDecisionResponse,
    summary="Submit human verdict on a HITL review",
)
async def submit_decision(
    hitl_id: str,
    body: HITLDecisionRequest,
    session: AsyncSession = Depends(get_db),
) -> HITLDecisionResponse:
    """
    Submit a human reviewer's decision on a HITL-escalated PR review.

    Transitions the item from 'pending'/'in_review' to the appropriate
    terminal status, records feedback, and attempts to post to GitHub.
    """
    # Import GitHub client here to avoid circular import at module load.
    # The GitHub client is a detail (delivery mechanism) from Clean-Architecture's
    # perspective — it belongs outside the Use Case layer.
    #
    # Real module path: backend.integrations.github_client (NOT backend.github.client).
    # GitHubClient takes a Settings object and is an async context manager
    # (httpx.AsyncClient connection pool lifecycle).
    from backend.integrations.github_client import GitHubClient
    from backend.config import get_settings

    settings = get_settings()

    request = DisputeRequest(
        hitl_review_id=hitl_id,
        human_verdict=body.human_verdict,
        reason=body.reason,
        reviewer_id=body.reviewer_id,
    )

    try:
        async with GitHubClient(settings) as github_client:
            result: DisputeResult = await resolve_dispute(
                session=session,
                github_client=github_client,
                request=request,
            )
    except HITLReviewNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )
    except DisputeAlreadyResolved as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Review already resolved with status '{exc.current_status}'.",
        )
    except InvalidVerdict as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    logger.info(
        "hitl_router | decision_submitted | hitl_id=%s verdict=%s reviewer=%s",
        hitl_id, body.human_verdict, body.reviewer_id,
    )

    return HITLDecisionResponse(
        hitl_review_id=result.hitl_review_id,
        previous_status=result.previous_status,
        new_status=result.new_status,
        human_verdict=result.human_verdict,
        posted_to_github=result.posted_to_github,
        feedback_id=result.feedback_id,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/hitl/queue/rebuild
#
# Re-hydrate the Redis queue from Postgres pending rows.
# Admin endpoint — useful after Redis eviction or restart.
# (Derived-Data-Systems.md: "If you lose derived data, re-create from source.")
# ---------------------------------------------------------------------------
@hitl_router.post(
    "/queue/rebuild",
    response_model=dict[str, Any],
    summary="Rebuild Redis queue from Postgres",
)
async def rebuild_queue(
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Re-populate the Redis HITL queue from Postgres pending rows.

    Safe to call repeatedly — checks Redis length first, skips if non-empty.
    """
    from backend.memory.redis_client import get_redis_client

    redis_client = await get_redis_client()
    pushed = await rebuild_redis_queue(session, redis_client)

    return {
        "pushed_to_redis": pushed,
        "message": (
            f"Rebuilt: pushed {pushed} pending items to Redis queue."
            if pushed > 0
            else "Redis queue already populated or no pending items in Postgres."
        ),
    }
