# backend/api/schemas.py
#
# Pydantic response DTOs for the REST API layer.
#
# WHY SEPARATE DTOs INSTEAD OF RETURNING ORM OBJECTS DIRECTLY?
#
# WIKI: Interface Adapters Layer (clean-architecture)
#   "When passing data across a boundary, it is always in the form
#    most convenient for the inner circle."
#   Applied here in reverse: we are the outermost layer. We convert
#   inner-circle ORM objects (PRReviewRecord, FindingRecord) into
#   clean Pydantic shapes before returning them to API callers.
#
# Concrete reasons:
#   1. ORM objects carry lazy-loaded relationships and SQLAlchemy internals.
#      Pydantic cannot serialize them directly — you get "greenlet" errors
#      or unexpected queries triggered during serialization.
#   2. The API response shape is a public contract. The DB schema is internal.
#      If we rename a column in Postgres, the API shape stays stable.
#   3. Some fields are computed (finding_count), transformed (datetime -> ISO),
#      or omitted (diff_hash is internal, never expose it to callers).
#
# DESIGN PRINCIPLE (Humble Object, clean-architecture wiki):
#   The converter functions (review_record_to_summary, review_record_to_detail)
#   are the "testable core" — they contain all the mapping logic.
#   The router endpoints are the "humble shell" — they only call repository
#   functions and pass results through the converter.
#   This means we can unit-test all mapping logic without a running HTTP server.
#
# TWO RESPONSE GRANULARITIES (following pagination best practice):
#   ReviewSummary — list view. No findings embedded. Saves bandwidth.
#   ReviewDetail  — single-item view. Includes full findings list.
#
# This pattern is from REST API design: "list endpoints return summaries,
# detail endpoints return the full object." Embedding 50 findings in every
# row of a paginated list would be wasteful.

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from backend.database.models import FindingRecord, PRReviewRecord


# ---------------------------------------------------------------------------
# FindingResponse
#
# One finding as returned by the API. Maps 1:1 to FindingRecord columns,
# with no SQLAlchemy internals exposed.
# ---------------------------------------------------------------------------
class FindingResponse(BaseModel):
    """API representation of a single code finding."""

    id: str
    review_id: str
    agent_type: str
    severity: str          # "critical" | "high" | "medium" | "low"
    category: str          # "security" | "quality" | "test_coverage" | "documentation"
    summary: str
    file_path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    suggestion: Optional[str] = None
    confidence: float
    created_at: datetime

    # Pydantic v2: allow construction from ORM objects directly
    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# ReviewSummary
#
# List-view DTO. Returned in GET /api/v1/reviews response.
# Intentionally excludes findings — callers fetch those via GET /reviews/{id}.
#
# BANDWIDTH CONSIDERATION:
# A review with 20 findings would be ~5KB per row if findings were embedded.
# With 100 reviews on a page that is 500KB per list response — unacceptable.
# finding_count is a cheap computed integer that gives the caller enough
# information to decide whether to fetch the full detail.
# ---------------------------------------------------------------------------
class ReviewSummary(BaseModel):
    """Compact review representation for list views. No findings included."""

    id: str
    repo_full_name: str
    pr_number: int
    pr_title: str
    head_commit_sha: str
    verdict: Optional[str] = None     # None while in-progress
    status: str
    overall_confidence: Optional[float] = None
    needs_human_review: bool
    finding_count: int = Field(
        description="Number of findings. Fetch GET /reviews/{id} for the full list."
    )
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# ReviewDetail
#
# Full review representation for GET /api/v1/reviews/{id}.
# Includes the complete findings list.
# ---------------------------------------------------------------------------
class ReviewDetail(BaseModel):
    """Complete review representation. Includes all findings."""

    id: str
    repo_full_name: str
    pr_number: int
    pr_title: str
    head_commit_sha: str
    verdict: Optional[str] = None
    status: str
    overall_confidence: Optional[float] = None
    needs_human_review: bool
    human_review_reason: str
    github_review_id: Optional[int] = None
    findings: list[FindingResponse]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# QueueItem
#
# Queue-view DTO for GET /api/v1/queue.
# Fields relevant to a human reviewer deciding what to look at.
# Emphasises: confidence (how uncertain the agent was), reason for HITL,
# and how long ago it was created (triage by age).
# ---------------------------------------------------------------------------
class QueueItem(BaseModel):
    """
    A review that is either in-flight or awaiting human attention.

    Used by operators to monitor active reviews and by the HITL UI
    (Phase 19) to present items needing human decisions.
    """

    id: str
    repo_full_name: str
    pr_number: int
    pr_title: str
    status: str
    # needs_human_review=True means low confidence — human should inspect before posting.
    needs_human_review: bool
    human_review_reason: str
    overall_confidence: Optional[float] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Pagination Envelopes
#
# All list endpoints wrap their items in a pagination envelope.
# This lets callers implement cursor-based pagination without breaking
# changes — we can add next_cursor later without changing the item shape.
#
# WIKI: Reliability-Scalability-Maintainability (DDIA)
#   "Evolvability: make it easy to make changes in the future."
#   Wrapping in an envelope preserves the ability to add pagination metadata
#   (next_cursor, total_pages) later without breaking existing clients.
# ---------------------------------------------------------------------------
class ReviewListResponse(BaseModel):
    """Paginated list of review summaries."""

    items: list[ReviewSummary]
    total: int = Field(description="Total reviews matching the filter (not just this page).")
    limit: int
    offset: int


class QueueResponse(BaseModel):
    """Paginated list of queue items."""

    items: list[QueueItem]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Converter Functions
#
# WIKI: Humble Object Pattern (clean-architecture)
#   These functions ARE the testable core. They contain all mapping logic.
#   The route handlers are the humble shell — they just call these.
#
# PRReviewRecord -> ReviewSummary / ReviewDetail
# FindingRecord  -> FindingResponse
#
# We do the conversion here (not inside the Pydantic model) because some
# fields require non-trivial transformation:
#   - needs_human_review: stored as INTEGER (0/1) in Postgres, must be bool in API
#   - finding_count: computed from len(record.findings), not a column
#
# These functions take ORM objects and return pure Pydantic models.
# They have zero I/O — no DB calls, no Redis calls. Fully unit-testable.
# ---------------------------------------------------------------------------

def finding_record_to_response(record: FindingRecord) -> FindingResponse:
    """
    Converts a FindingRecord ORM object to a FindingResponse DTO.

    Called inside review_record_to_detail() when building the findings list.
    Never called directly from route handlers (route handlers don't know
    about FindingRecord — that would violate the adapter boundary).
    """
    return FindingResponse(
        id=record.id,
        review_id=record.review_id,
        agent_type=record.agent_type,
        severity=record.severity,
        category=record.category,
        summary=record.summary,
        file_path=record.file_path,
        line_start=record.line_start,
        line_end=record.line_end,
        suggestion=record.suggestion,
        confidence=record.confidence,
        created_at=record.created_at,
    )


def review_record_to_summary(record: PRReviewRecord) -> ReviewSummary:
    """
    Converts a PRReviewRecord to a ReviewSummary DTO (no findings).

    Used in list responses where we want compact rows.
    finding_count is computed from the loaded relationship — SQLAlchemy
    already loaded them via selectin on get_review(), and the worker
    loaded them during list_reviews() if needed. We use len() here
    because selectin has already run; this is NOT an extra query.
    """
    return ReviewSummary(
        id=record.id,
        repo_full_name=record.repo_full_name,
        pr_number=record.pr_number,
        pr_title=record.pr_title,
        head_commit_sha=record.head_commit_sha,
        verdict=record.verdict,
        status=record.status,
        overall_confidence=record.overall_confidence,
        # Stored as INTEGER (0/1) in Postgres — convert to bool for the API.
        # WIKI: Interface Adapters Layer — "the boundary converts to the form
        # convenient for the inner circle." Here the outer caller (API consumer)
        # wants a bool; the inner DB stores int. This is the conversion point.
        needs_human_review=bool(record.needs_human_review),
        # finding_count: len() is O(1) on an already-loaded list. Not a DB call.
        finding_count=len(record.findings),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def review_record_to_detail(record: PRReviewRecord) -> ReviewDetail:
    """
    Converts a PRReviewRecord to a ReviewDetail DTO (includes findings).

    Used in single-review responses (GET /reviews/{id}).
    Findings are loaded via selectin on the PRReviewRecord relationship
    — we convert each one with finding_record_to_response().
    """
    return ReviewDetail(
        id=record.id,
        repo_full_name=record.repo_full_name,
        pr_number=record.pr_number,
        pr_title=record.pr_title,
        head_commit_sha=record.head_commit_sha,
        verdict=record.verdict,
        status=record.status,
        overall_confidence=record.overall_confidence,
        needs_human_review=bool(record.needs_human_review),
        human_review_reason=record.human_review_reason,
        github_review_id=record.github_review_id,
        # Convert each FindingRecord to a FindingResponse DTO.
        # This is the only place FindingRecord -> FindingResponse conversion happens.
        findings=[finding_record_to_response(f) for f in record.findings],
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def review_record_to_queue_item(record: PRReviewRecord) -> QueueItem:
    """
    Converts a PRReviewRecord to a QueueItem DTO.

    Used in GET /api/v1/queue responses. Omits findings (not needed by
    the operator overview — full detail is fetched via GET /reviews/{id}).
    """
    return QueueItem(
        id=record.id,
        repo_full_name=record.repo_full_name,
        pr_number=record.pr_number,
        pr_title=record.pr_title,
        status=record.status,
        needs_human_review=bool(record.needs_human_review),
        human_review_reason=record.human_review_reason,
        overall_confidence=record.overall_confidence,
        created_at=record.created_at,
    )
