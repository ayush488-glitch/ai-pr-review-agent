# tests/smoke_phase3b.py
#
# Smoke tests for Phase 3 completion: REST API layer.
#
# TESTS (9 total):
#   1. GET /api/v1/reviews — empty database -> empty list
#   2. GET /api/v1/reviews — with 2 reviews -> returns both summaries
#   3. GET /api/v1/reviews?repo=owner/repo — filter by repo works
#   4. GET /api/v1/reviews?status=completed — filter by status works
#   5. GET /api/v1/reviews/{id} — existing review -> returns detail with findings
#   6. GET /api/v1/reviews/{id} — non-existent id -> 404
#   7. GET /api/v1/queue — empty database -> empty queue
#   8. GET /api/v1/queue — with pending reviews -> returns them
#   9. GET /api/v1/reviews — missing auth in prod mode -> 401
#
# STRATEGY:
#   - pytest-asyncio mode=auto: all async def tests run with their own managed loop.
#   - httpx.AsyncClient with ASGI transport: bypasses lifespan so we avoid connecting
#     to real Redis/Postgres/Qdrant.
#   - SQLite in-memory via aiosqlite: same event loop as the requests, so inserts
#     are visible to GET requests immediately.
#   - All DB writes use the same event loop as the requests (same pytest-asyncio loop),
#     so in-memory SQLite connections are shared within a test.
#
# WHY NOT TestClient?
#   TestClient runs its own anyio event loop and DOES trigger lifespan events,
#   which try to connect to real Redis/Postgres.
#   httpx.AsyncClient(transport=ASGITransport(app)) does NOT trigger lifespan
#   (no lifespan= parameter on ASGITransport means it never calls startup/shutdown).
#   This gives us pure HTTP adapter tests with zero infrastructure.

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Set required env vars BEFORE importing anything from the backend.
# Settings validation runs at import time.
# ---------------------------------------------------------------------------

import os
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("APP_ENV", "development")

from backend.config.settings import Settings, get_settings
from backend.database.postgres import Base, get_db
from backend.database.models import PRReviewRecord, FindingRecord
from backend.main import app

# ---------------------------------------------------------------------------
# In-memory SQLite test engine.
#
# We use a *named* shared-memory SQLite database so that multiple connections
# within the same process see the same data.
#
# "file:testdb_ph3b?mode=memory&cache=shared" is a URI-format SQLite name.
# Any connection using this URI shares the same in-memory database.
# "check_same_thread": False is required for SQLite + async.
# ---------------------------------------------------------------------------
TEST_DB_URL = "sqlite+aiosqlite:///file:testdb_ph3b?mode=memory&cache=shared&uri=true"

test_engine = create_async_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
)
TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a SQLite test session instead of Postgres."""
    async with TestSessionLocal() as session:
        yield session


def override_get_settings() -> Settings:
    """Return test-friendly settings."""
    return Settings(
        github_webhook_secret="test-secret",
        github_token="test-token",
        openai_api_key="test-key",
        anthropic_api_key="test-key",
        database_url=TEST_DB_URL,
        app_env="development",
        api_key="test-api-key",
    )


def override_get_settings_prod() -> Settings:
    """Production-mode settings for auth enforcement test."""
    return Settings(
        github_webhook_secret="test-secret",
        github_token="test-token",
        openai_api_key="test-key",
        anthropic_api_key="test-key",
        database_url=TEST_DB_URL,
        app_env="production",
        api_key="correct-key",
    )


# ---------------------------------------------------------------------------
# Module-scoped DB setup.
# Runs once per test module — creates all tables in the shared in-memory DB.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
async def create_tables():
    """Create SQLite tables once for this test module."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Teardown: drop all tables so a re-run starts clean.
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ---------------------------------------------------------------------------
# Per-test fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def apply_dep_overrides():
    """Override FastAPI dependencies to use test DB and settings."""
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings] = override_get_settings
    yield
    app.dependency_overrides.clear()


@pytest_asyncio.fixture()
async def client():
    """
    httpx AsyncClient with ASGI transport.

    ASGITransport(app) calls the ASGI app directly without running lifespan
    events. This means redis_client.connect(), init_db(), and ensure_collection()
    are NEVER called — no real infrastructure needed.

    WHY NOT TestClient?
    TestClient triggers lifespan which tries to connect to Redis and Postgres.
    ASGITransport skips that entirely. This is the correct approach for testing
    the HTTP adapter layer in isolation.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Mock Redis client (used in GET /reviews/{id} tests).
# ---------------------------------------------------------------------------

class _MockRedisClient:
    """Returns None for all cache reads (cache miss -> fall through to DB)."""
    async def get_cached_review_status(self, review_id: str):
        return None
    async def get_workflow_status(self, workflow_id: str):
        return None


# ---------------------------------------------------------------------------
# Helper: insert a PRReviewRecord + optional FindingRecords into the test DB.
# ---------------------------------------------------------------------------

async def _insert_review(
    status: str = "completed",
    verdict: str | None = "approve",
    repo: str = "owner/repo",
    pr_number: int = 1,
    needs_human_review: bool = False,
    num_findings: int = 0,
) -> str:
    """
    Insert a test PRReviewRecord and optional findings into the SQLite test DB.
    Returns the review_id.

    Called with await _insert_review(...) inside async test functions.
    Uses the same event loop as the test, so the named shared-memory SQLite
    connection sees the inserted rows immediately.
    """
    review_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    async with TestSessionLocal() as session:
        review = PRReviewRecord(
            id=review_id,
            repo_full_name=repo,
            pr_number=pr_number,
            pr_title=f"Test PR #{pr_number}",
            head_commit_sha="abc123",
            diff_hash="deadbeef",
            verdict=verdict,
            status=status,
            overall_confidence=0.85,
            needs_human_review=1 if needs_human_review else 0,
            human_review_reason="low confidence" if needs_human_review else "",
            github_review_id=None,
            created_at=now,
            updated_at=now,
        )
        session.add(review)

        for i in range(num_findings):
            finding = FindingRecord(
                id=str(uuid.uuid4()),
                review_id=review_id,
                repo_full_name=repo,
                agent_type="security",
                severity="high",
                category="security",
                summary=f"Test finding #{i}",
                file_path="src/main.py",
                line_start=10 + i,
                line_end=12 + i,
                suggestion="Fix it.",
                confidence=0.9,
                created_at=now,
            )
            session.add(finding)

        # Commit. SQLAlchemy autobegin starts the transaction on first operation;
        # calling session.begin() here would raise "already in transaction".
        await session.commit()

    return review_id


# ---------------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_1_list_reviews_empty(client):
    """GET /api/v1/reviews on empty DB returns empty list."""
    response = await client.get("/api/v1/reviews")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 50
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_2_list_reviews_with_data(client):
    """GET /api/v1/reviews returns summaries when reviews exist."""
    await _insert_review(repo="owner/repo-a", pr_number=11)
    await _insert_review(repo="owner/repo-b", pr_number=22)

    response = await client.get("/api/v1/reviews")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2
    item = data["items"][0]
    assert "id" in item
    assert "status" in item
    assert "finding_count" in item
    assert "needs_human_review" in item


@pytest.mark.asyncio
async def test_3_list_reviews_filter_by_repo(client):
    """GET /api/v1/reviews?repo=... returns only matching reviews."""
    unique_repo = f"owner/unique-{uuid.uuid4().hex[:8]}"
    await _insert_review(repo=unique_repo, pr_number=99)

    response = await client.get(f"/api/v1/reviews?repo={unique_repo}")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["repo_full_name"] == unique_repo


@pytest.mark.asyncio
async def test_4_list_reviews_filter_by_status(client):
    """GET /api/v1/reviews?status=failed returns only failed reviews."""
    # Use unique repo to avoid cross-test contamination on the status filter
    fail_repo = f"owner/fail-{uuid.uuid4().hex[:8]}"
    await _insert_review(repo=fail_repo, pr_number=101, status="failed", verdict=None)

    response = await client.get("/api/v1/reviews?status=failed")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    for item in data["items"]:
        assert item["status"] == "failed"


@pytest.mark.asyncio
async def test_5_get_review_detail(client):
    """GET /api/v1/reviews/{id} returns full detail with findings."""
    review_id = await _insert_review(
        repo="owner/detail-repo", pr_number=200, num_findings=3
    )

    import backend.api.reviews as reviews_module
    original = reviews_module.redis_client
    reviews_module.redis_client = _MockRedisClient()  # type: ignore
    try:
        response = await client.get(f"/api/v1/reviews/{review_id}")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["id"] == review_id
        assert data["status"] == "completed"
        assert data["verdict"] == "approve"
        assert len(data["findings"]) == 3
        f = data["findings"][0]
        assert "severity" in f
        assert "summary" in f
        assert "agent_type" in f
    finally:
        reviews_module.redis_client = original


@pytest.mark.asyncio
async def test_6_get_review_not_found(client):
    """GET /api/v1/reviews/{id} with non-existent id returns 404."""
    import backend.api.reviews as reviews_module
    original = reviews_module.redis_client
    reviews_module.redis_client = _MockRedisClient()  # type: ignore
    try:
        response = await client.get(f"/api/v1/reviews/{uuid.uuid4()}")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    finally:
        reviews_module.redis_client = original


@pytest.mark.asyncio
async def test_7_queue_empty(client):
    """GET /api/v1/queue returns a valid (possibly empty) queue response."""
    response = await client.get("/api/v1/queue")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["total"], int)


@pytest.mark.asyncio
async def test_8_queue_with_pending_reviews(client):
    """GET /api/v1/queue returns in-progress and HITL reviews."""
    q_repo = f"owner/q-{uuid.uuid4().hex[:8]}"
    # In-flight review
    await _insert_review(repo=q_repo, pr_number=300, status="in_progress", verdict=None)
    # HITL review: completed but needs human approval
    await _insert_review(
        repo=q_repo, pr_number=301,
        status="completed", verdict="approve",
        needs_human_review=True,
    )

    response = await client.get("/api/v1/queue")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2
    statuses = {item["status"] for item in data["items"]}
    assert "in_progress" in statuses


@pytest.mark.asyncio
async def test_9_auth_rejection_in_prod_mode(client):
    """
    GET /api/v1/reviews without X-API-Key in prod mode returns 401.
    GET /api/v1/reviews with correct X-API-Key in prod mode returns 200.
    """
    # Switch settings to production mode for this test only.
    # We can't change the existing `client` fixture's dependency overrides,
    # so we create a fresh client with the prod settings override.
    app.dependency_overrides[get_settings] = override_get_settings_prod
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as prod_client:
        # No key -> 401
        r1 = await prod_client.get("/api/v1/reviews")
        assert r1.status_code == 401, f"Expected 401 without key, got {r1.status_code}: {r1.text}"

        # Wrong key -> 403
        r2 = await prod_client.get("/api/v1/reviews", headers={"X-API-Key": "wrong-key"})
        assert r2.status_code == 403, f"Expected 403 with wrong key, got {r2.status_code}: {r2.text}"

        # Correct key -> 200
        r3 = await prod_client.get("/api/v1/reviews", headers={"X-API-Key": "correct-key"})
        assert r3.status_code == 200, f"Expected 200 with correct key, got {r3.status_code}: {r3.text}"
