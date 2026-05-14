# tests/smoke_phase14.py
#
# Phase 14 smoke tests — Data Engineering (ingestion pipeline + freshness).
#
# WHAT WE TEST:
#   1. filter_code_files() correctly keeps code, drops docs/configs/binaries
#   2. get_stale_files() returns all files on first run (nothing in DB)
#   3. get_stale_files() returns only changed files on second run (freshness works)
#   4. ingest_repository() calls embed + upsert for stale files only
#   5. ingest_repository() skips re-embed when SHA unchanged (idempotent)
#   6. ingest_repository() continues past per-file errors (fault isolation)
#   7. qdrant_client.search_similar_code() uses .query_points() not .search()
#
# APPROACH:
#   All external I/O (GitHub API, OpenAI embeddings, Qdrant, Postgres) is mocked.
#   Tests run fully offline — no containers needed.
#   (demo-day-readiness Bug #7: no integration test until demo day. We fix that here.)
#
# RUN:
#   cd ~/Desktop/ai-pr-review-agent
#   python3 -m pytest tests/smoke_phase14.py -v

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Test 1: filter_code_files
#
# The most basic unit — pure function, no I/O.
# ---------------------------------------------------------------------------
from backend.data.ingestion import filter_code_files


def test_filter_code_files_keeps_source_code():
    """Code files with recognized extensions are kept."""
    tree = [
        {"path": "src/auth.py",         "sha": "aaa", "size": 1000, "type": "blob"},
        {"path": "api/server.ts",        "sha": "bbb", "size": 2000, "type": "blob"},
        {"path": "cmd/main.go",          "sha": "ccc", "size": 500,  "type": "blob"},
    ]
    result = filter_code_files(tree)
    assert len(result) == 3
    assert all(f["type"] == "blob" for f in result)


def test_filter_code_files_drops_non_code():
    """Docs, configs, lock files, and directories are excluded."""
    tree = [
        {"path": "README.md",             "sha": "ddd", "size": 500,  "type": "blob"},
        {"path": "package-lock.json",     "sha": "eee", "size": 90000,"type": "blob"},
        {"path": ".github/workflows/ci.yml","sha": "fff","size": 1000, "type": "blob"},
        {"path": "src",                   "sha": "ggg", "size": 0,    "type": "tree"},
    ]
    result = filter_code_files(tree)
    assert len(result) == 0


def test_filter_code_files_drops_large_files():
    """Files over MAX_FILE_BYTES (100 KB) are skipped even if .py."""
    from backend.data.ingestion import MAX_FILE_BYTES
    tree = [
        {"path": "big_model.py", "sha": "hhh", "size": MAX_FILE_BYTES + 1, "type": "blob"},
        {"path": "small.py",     "sha": "iii", "size": MAX_FILE_BYTES - 1, "type": "blob"},
    ]
    result = filter_code_files(tree)
    assert len(result) == 1
    assert result[0]["path"] == "small.py"


# ---------------------------------------------------------------------------
# Test 2 & 3: get_stale_files (freshness logic)
#
# Uses an in-memory SQLite database so we don't need Postgres running.
# ---------------------------------------------------------------------------
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.data.freshness import get_stale_files, mark_files_embedded
from backend.database.postgres import Base


@pytest_asyncio.fixture
async def db_session():
    """
    Provides a fresh in-memory SQLite AsyncSession for each test.

    We create the schema via create_all() — the same pattern used in production
    on first startup. This also validates that RepoFileIndexRecord creates its
    table correctly.
    """
    # SQLite async requires aiosqlite driver
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # Import models to register them with Base.metadata
    from backend.database import models as _models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_get_stale_files_all_new(db_session):
    """
    First run: all files are stale (no rows in DB yet).
    get_stale_files should return all entries in file_sha_map.
    """
    file_sha_map = {
        "src/auth.py":   "sha_auth_v1",
        "src/models.py": "sha_models_v1",
        "api/server.py": "sha_server_v1",
    }
    stale = await get_stale_files(db_session, "octocat/test-repo", file_sha_map)

    # All 3 files are new — all should be stale
    assert len(stale) == 3
    assert set(stale.keys()) == set(file_sha_map.keys())


@pytest.mark.asyncio
async def test_get_stale_files_freshness_skip(db_session):
    """
    Second run: files with unchanged SHAs are skipped; changed file is stale.

    This is the core freshness invariant:
      Same SHA -> fresh -> skip
      Different SHA -> stale -> re-embed
    """
    repo = "octocat/test-repo"

    # Simulate first run: mark 2 files as embedded
    await mark_files_embedded(db_session, repo, "src/auth.py",   "sha_auth_v1",   1)
    await mark_files_embedded(db_session, repo, "src/models.py", "sha_models_v1", 1)

    # Second run: auth.py unchanged, models.py changed, server.py new
    file_sha_map = {
        "src/auth.py":   "sha_auth_v1",      # same SHA -> fresh -> skip
        "src/models.py": "sha_models_v2",    # different SHA -> stale
        "api/server.py": "sha_server_v1",    # new file -> stale
    }
    stale = await get_stale_files(db_session, repo, file_sha_map)

    # Only models.py (changed) and server.py (new) should be stale
    assert len(stale) == 2
    assert "src/auth.py" not in stale, "Fresh file must not be re-embedded"
    assert "src/models.py" in stale
    assert "api/server.py" in stale


@pytest.mark.asyncio
async def test_mark_files_embedded_upsert(db_session):
    """
    mark_files_embedded is idempotent — calling it twice with the same path
    updates the row (upsert), not inserts a duplicate.
    """
    repo = "octocat/test-repo"
    path = "src/auth.py"

    await mark_files_embedded(db_session, repo, path, "sha_v1", 1)
    await mark_files_embedded(db_session, repo, path, "sha_v2", 2)  # update

    # After second call, should reflect sha_v2
    from sqlalchemy import select
    from backend.database.models import RepoFileIndexRecord

    stmt = select(RepoFileIndexRecord).where(RepoFileIndexRecord.file_path == path)
    result = await db_session.execute(stmt)
    rows = result.scalars().all()

    assert len(rows) == 1, "Upsert must not create duplicate rows"
    assert rows[0].file_sha == "sha_v2"
    assert rows[0].chunk_count == 2


# ---------------------------------------------------------------------------
# Test 4 & 5: ingest_repository — full pipeline with mocks
#
# Mocks: httpx (GitHub API), embed_text, upsert_code_chunks, get_session_factory
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_repository_embeds_stale_files():
    """
    ingest_repository() calls embed + upsert for each stale file.

    Setup:
      - GitHub returns 2 code files
      - Both are new (nothing in DB) -> both are stale
      - embed and upsert should each be called twice
    """
    # --- Mock GitHub tree API response ---
    fake_tree = {
        "truncated": False,
        "tree": [
            {"path": "src/auth.py",   "sha": "sha_auth",   "size": 500,  "type": "blob"},
            {"path": "src/models.py", "sha": "sha_models", "size": 800,  "type": "blob"},
            {"path": "README.md",     "sha": "sha_readme", "size": 200,  "type": "blob"},
        ]
    }
    fake_repo = {"default_branch": "main"}
    fake_branch = {"commit": {"commit": {"tree": {"sha": "tree_sha_abc"}}}}
    fake_file_content = {
        "content": __import__("base64").b64encode(b"def example(): pass\n").decode() + "\n"
    }

    # Build a side_effect sequence for httpx GET calls:
    # 1. /repos/{repo}      -> fake_repo
    # 2. /repos/{repo}/branches/main -> fake_branch
    # 3. /repos/{repo}/git/trees/... -> fake_tree
    # 4. /repos/{repo}/contents/src/auth.py   -> fake_file_content
    # 5. /repos/{repo}/contents/src/models.py -> fake_file_content
    get_responses = []
    for resp_json in [fake_repo, fake_branch, fake_tree, fake_file_content, fake_file_content]:
        mock_resp = MagicMock()
        mock_resp.json.return_value = resp_json
        mock_resp.raise_for_status = MagicMock()
        get_responses.append(mock_resp)

    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(side_effect=get_responses)
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)

    # Mock session for freshness (returns no rows = all files stale)
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_result = MagicMock()
    mock_result.all.return_value = []  # empty DB -> all stale
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.merge = AsyncMock()
    mock_session.commit = AsyncMock()

    mock_factory = MagicMock(return_value=mock_session)

    fake_vector = [0.1] * 1536  # text-embedding-3-small dimension

    with patch("httpx.AsyncClient", return_value=mock_http_client), \
         patch("backend.data.ingestion.get_settings") as mock_cfg, \
         patch("backend.data.ingestion.get_session_factory", return_value=mock_factory), \
         patch("backend.data.ingestion.embed_text", new_callable=AsyncMock, return_value=fake_vector), \
         patch("backend.data.ingestion.upsert_code_chunks", new_callable=AsyncMock) as mock_upsert, \
         patch("backend.data.ingestion.get_stale_files", new_callable=AsyncMock,
               return_value={"src/auth.py": "sha_auth", "src/models.py": "sha_models"}), \
         patch("backend.data.ingestion.mark_files_embedded", new_callable=AsyncMock):

        mock_cfg.return_value.github_token = "fake-token"

        from backend.data.ingestion import ingest_repository
        summary = await ingest_repository("octocat/test-repo")

    # Both stale files should be embedded and upserted
    assert summary["embedded"] == 2
    assert summary["stale_files"] == 2
    assert summary["errors"] == 0
    assert mock_upsert.call_count == 2


@pytest.mark.asyncio
async def test_ingest_repository_skips_fresh_files():
    """
    ingest_repository() skips files with unchanged SHAs.
    When get_stale_files returns empty dict, embed is never called.
    """
    fake_tree = {"truncated": False, "tree": [
        {"path": "src/auth.py", "sha": "sha_auth", "size": 500, "type": "blob"},
    ]}
    fake_repo = {"default_branch": "main"}
    fake_branch = {"commit": {"commit": {"tree": {"sha": "tree_sha"}}}}

    get_responses = []
    for resp_json in [fake_repo, fake_branch, fake_tree]:
        mock_resp = MagicMock()
        mock_resp.json.return_value = resp_json
        mock_resp.raise_for_status = MagicMock()
        get_responses.append(mock_resp)

    mock_http_client = AsyncMock()
    mock_http_client.get = AsyncMock(side_effect=get_responses)
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_factory = MagicMock(return_value=mock_session)

    with patch("httpx.AsyncClient", return_value=mock_http_client), \
         patch("backend.data.ingestion.get_settings") as mock_cfg, \
         patch("backend.data.ingestion.get_session_factory", return_value=mock_factory), \
         patch("backend.data.ingestion.embed_text", new_callable=AsyncMock) as mock_embed, \
         patch("backend.data.ingestion.upsert_code_chunks", new_callable=AsyncMock), \
         patch("backend.data.ingestion.get_stale_files", new_callable=AsyncMock,
               return_value={}):  # all fresh -> nothing stale

        mock_cfg.return_value.github_token = "fake-token"

        from backend.data.ingestion import ingest_repository
        summary = await ingest_repository("octocat/test-repo")

    # No files stale -> embed never called
    assert summary["embedded"] == 0
    assert summary["stale_files"] == 0
    assert summary["skipped_fresh"] == 1
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_repository_isolates_per_file_errors():
    """
    A single file failing to embed does not abort the rest of the pipeline.
    (fault isolation — Distributed-Systems-Fault-Model.md wiki)
    """
    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)

    # fetch_repo_tree returns 2 files, both stale
    fake_repo = {"default_branch": "main"}
    fake_branch = {"commit": {"commit": {"tree": {"sha": "tree_sha"}}}}
    fake_tree = {"truncated": False, "tree": [
        {"path": "src/a.py", "sha": "sha_a", "size": 300, "type": "blob"},
        {"path": "src/b.py", "sha": "sha_b", "size": 300, "type": "blob"},
    ]}
    fake_content = {
        "content": __import__("base64").b64encode(b"x = 1\n").decode() + "\n"
    }
    get_responses = []
    for resp_json in [fake_repo, fake_branch, fake_tree, fake_content, fake_content]:
        mock_resp = MagicMock()
        mock_resp.json.return_value = resp_json
        mock_resp.raise_for_status = MagicMock()
        get_responses.append(mock_resp)
    mock_http_client.get = AsyncMock(side_effect=get_responses)

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.merge = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_factory = MagicMock(return_value=mock_session)

    fake_vector = [0.1] * 1536

    # embed_text: fails for a.py, succeeds for b.py
    from backend.memory.embedder import EmbeddingError
    embed_calls = [EmbeddingError("openai timeout"), fake_vector]

    with patch("httpx.AsyncClient", return_value=mock_http_client), \
         patch("backend.data.ingestion.get_settings") as mock_cfg, \
         patch("backend.data.ingestion.get_session_factory", return_value=mock_factory), \
         patch("backend.data.ingestion.embed_text", new_callable=AsyncMock,
               side_effect=embed_calls), \
         patch("backend.data.ingestion.upsert_code_chunks", new_callable=AsyncMock) as mock_upsert, \
         patch("backend.data.ingestion.get_stale_files", new_callable=AsyncMock,
               return_value={"src/a.py": "sha_a", "src/b.py": "sha_b"}), \
         patch("backend.data.ingestion.mark_files_embedded", new_callable=AsyncMock):

        mock_cfg.return_value.github_token = "fake-token"

        from backend.data.ingestion import ingest_repository
        summary = await ingest_repository("octocat/test-repo")

    # a.py errored, b.py succeeded — pipeline continued past the error
    assert summary["errors"] == 1
    assert summary["embedded"] == 1
    assert mock_upsert.call_count == 1  # only b.py was upserted


# ---------------------------------------------------------------------------
# Test 6: qdrant_client.search_similar_code uses .query_points not .search
#
# This validates the Phase 14 fix for the broken RAG retrieval.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qdrant_search_uses_query_points():
    """
    search_similar_code() must call .query_points(), not .search().

    Before the Phase 14 fix: AsyncQdrantClient had no .search() and raised
    AttributeError. After the fix: .query_points() is called with .points accessor.
    """
    from backend.memory.qdrant_client import search_similar_code

    fake_vector = [0.1] * 1536

    # Mock ScoredPoint return from .query_points()
    mock_point = MagicMock()
    mock_point.payload = {"file_path": "src/auth.py", "chunk_text": "def login(): ...", "pr_number": 0}
    mock_point.score = 0.92

    mock_response = MagicMock()
    mock_response.points = [mock_point]

    mock_qdrant = AsyncMock()
    mock_qdrant.query_points = AsyncMock(return_value=mock_response)
    mock_qdrant.close = AsyncMock()
    # Ensure .search does NOT exist — if the code calls .search it raises AttributeError
    del mock_qdrant.search

    with patch("backend.memory.qdrant_client.AsyncQdrantClient", return_value=mock_qdrant), \
         patch("backend.memory.qdrant_client.get_settings") as mock_cfg:

        mock_cfg.return_value.qdrant_host = "localhost"
        mock_cfg.return_value.qdrant_port = 6335

        results = await search_similar_code(
            query_vector=fake_vector,
            repo_full_name="octocat/test-repo",
            top_k=5,
        )

    # query_points was called — not search
    mock_qdrant.query_points.assert_called_once()

    # Result correctly parsed from .points
    assert len(results) == 1
    assert results[0]["file_path"] == "src/auth.py"
    assert results[0]["score"] == 0.92


# ---------------------------------------------------------------------------
# Test 7: no github token -> graceful skip, no crash
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_repository_no_token():
    """
    ingest_repository() returns a zeroed summary when GITHUB_TOKEN is missing.
    It must not raise — callers treat ingestion as best-effort.
    """
    with patch("backend.data.ingestion.get_settings") as mock_cfg:
        mock_cfg.return_value.github_token = ""  # empty token

        from backend.data.ingestion import ingest_repository
        summary = await ingest_repository("octocat/test-repo")

    assert summary["total_files"] == 0
    assert summary["embedded"] == 0
    # No exception raised — graceful degradation
