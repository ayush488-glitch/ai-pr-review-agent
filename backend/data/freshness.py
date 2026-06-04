# backend/data/freshness.py
#
# Embedding freshness tracker.
#
# ROLE IN THE SYSTEM:
#   Qdrant is a DERIVED DATA STORE — it can be rebuilt from the primary source
#   (GitHub file content) at any time. But rebuilding everything on every
#   ingestion run is wasteful. This module tracks WHAT has already been
#   embedded so we only re-embed files that changed.
#
# DERIVED DATA PATTERN (Derived-Data-Systems.md wiki):
#   "Secondary indexes can be rebuilt from the primary source at any time.
#    The source of truth is always the primary store."
#
#   PRIMARY: GitHub file content (identified by blob SHA)
#   DERIVED:  Qdrant vector embeddings
#   TRACKER:  repo_file_index table in Postgres (what SHA we last embedded)
#
#   The blob SHA is GitHub's content-addressable identifier for a file blob.
#   If the file content changes, GitHub assigns a new SHA. If SHA matches what
#   we have in repo_file_index, the file is FRESH. If not (new file, or content
#   changed), the file is STALE and must be re-embedded.
#
# SESSION PATTERN (demo-day-readiness skill, Bug #2):
#   get_db() is defined as `async def ... yield` — an async GENERATOR.
#   You cannot use `async with get_db()` — it raises AttributeError: __aenter__.
#   CORRECT pattern: use async_sessionmaker directly (get_session_factory()).
#   This module receives AsyncSession objects from the caller — the caller
#   is responsible for session lifecycle. This keeps freshness.py testable
#   (tests can pass a SQLite in-memory session directly).
#
# SCHEMA NOTE (Encoding-and-Schema-Evolution.md wiki):
#   "Adding a new table is always safe — additive schema changes require no
#    migration and cannot break existing code that doesn't reference the table."
#   RepoFileIndexRecord is a NEW table — no existing code is affected.

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import RepoFileIndexRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TIGER: repo_file_index table is now on the Tiger Cloud instance (same connection pool as code_chunks)
# get_stale_files
#
# Given a repo and a {file_path: blob_sha} map of the repo's CURRENT state,
# returns a {file_path: blob_sha} map of files that need re-embedding.
#
# A file is STALE if:
#   a. It has never been embedded (no row in repo_file_index), OR
#   b. Its current SHA differs from the SHA we last embedded.
#
# A file is FRESH if its current SHA exactly matches what's in repo_file_index.
#
# BATCH QUERY: We load all existing rows for this repo in ONE query, then
# do an in-Python comparison. This avoids N+1 queries (one query per file).
# (Storage-Engines.md wiki: "Index the columns you filter on." We filter on
#  repo_full_name — it has an index in the ORM definition.)
# ---------------------------------------------------------------------------
async def get_stale_files(
    session: AsyncSession,
    repo_full_name: str,
    file_sha_map: dict[str, str],
) -> dict[str, str]:
    """
    Return {file_path: current_sha} for files that need re-embedding.

    Args:
        session:       Active async SQLAlchemy session.
        repo_full_name: e.g. "octocat/hello-world"
        file_sha_map:  {file_path: current_blob_sha} from GitHub tree API.

    Returns:
        Subset of file_sha_map where the file is new or its SHA changed.
    """
    if not file_sha_map:
        return {}

    # Load all currently-known embeddings for this repo in one query.
    # We only need file_path and file_sha — SELECT specific columns for efficiency.
    stmt = select(
        RepoFileIndexRecord.file_path,
        RepoFileIndexRecord.file_sha,
    ).where(RepoFileIndexRecord.repo_full_name == repo_full_name)

    result = await session.execute(stmt)
    rows = result.all()

    # Build {file_path: embedded_sha} from DB
    embedded: dict[str, str] = {row.file_path: row.file_sha for row in rows}

    logger.debug(
        "get_stale_files | repo=%s known_embedded=%d current_files=%d",
        repo_full_name, len(embedded), len(file_sha_map),
    )

    # Compare: stale if not in DB, or SHA changed
    stale: dict[str, str] = {}
    for file_path, current_sha in file_sha_map.items():
        known_sha = embedded.get(file_path)
        if known_sha is None:
            # Never embedded before — new file
            stale[file_path] = current_sha
        elif known_sha != current_sha:
            # SHA changed — file was modified since last embed
            stale[file_path] = current_sha
        # else: SHA matches — file is fresh, skip

    logger.info(
        "get_stale_files | repo=%s stale=%d fresh=%d",
        repo_full_name, len(stale), len(file_sha_map) - len(stale),
    )
    return stale


# ---------------------------------------------------------------------------
# mark_files_embedded
#
# Upserts a row into repo_file_index to record that a file has been embedded.
# Called AFTER successful embedding and Qdrant upsert.
#
# UPSERT LOGIC:
#   If the file has never been embedded: INSERT a new row.
#   If it was previously embedded (SHA changed, re-embedded): UPDATE the row.
#
# WHY UPSERT AFTER (not before) embedding:
#   If embedding fails mid-way, we don't record it as done.
#   Next ingestion run will correctly identify it as stale and retry.
#   (demo-day-readiness Bug #4: idempotency keys set before work = false
#    "already done" on retry. Same anti-pattern — avoid it here.)
#
# MERGE STRATEGY:
#   We use SQLAlchemy's merge() which does a SELECT first, then INSERT or
#   UPDATE. This is correct for a low-volume operation (one row per file
#   per ingestion run). For bulk ingestion of thousands of files, a
#   PostgreSQL INSERT ... ON CONFLICT would be more efficient — that's a
#   future optimisation if needed (Phase 20 continuous learning may add it).
# ---------------------------------------------------------------------------
async def mark_files_embedded(
    session: AsyncSession,
    repo_full_name: str,
    file_path: str,
    file_sha: str,
    chunk_count: int,
) -> None:
    """
    Record that a file has been successfully embedded into Qdrant.

    Upserts a row in repo_file_index. Safe to call multiple times —
    subsequent calls update embedded_at and file_sha.

    Args:
        session:        Active async SQLAlchemy session.
        repo_full_name: e.g. "octocat/hello-world"
        file_path:      Relative file path, e.g. "src/auth.py"
        file_sha:       GitHub blob SHA of the embedded version.
        chunk_count:    Number of chunks generated (currently always 1,
                        file-as-unit strategy).
    """
    # Primary key: "{repo_full_name}:{file_path}"
    # VARCHAR(128) check: repo is ~30 chars, path is ~80 chars, ":" is 1 = ~111 total.
    # (demo-day-readiness Bug #1: VARCHAR(36) too small for non-UUID PKs.)
    record_id = f"{repo_full_name}:{file_path}"

    # Defensive assertion — catch PK overflow in dev before it silently truncates
    # in Postgres.
    assert len(record_id) <= 128, (
        f"RepoFileIndex PK too long ({len(record_id)} chars): {record_id[:80]}..."
    )

    # merge() = SELECT + (INSERT or UPDATE) in one operation.
    # expire_on_commit=False is set on the session factory (data-systems-engineering
    # skill: mandatory for async sessions to prevent MissingGreenlet on attribute access).
    record = RepoFileIndexRecord(
        id=record_id,
        repo_full_name=repo_full_name,
        file_path=file_path,
        file_sha=file_sha,
        embedded_at=datetime.now(timezone.utc),
        chunk_count=chunk_count,
    )

    # merge() handles both insert and update atomically
    await session.merge(record)
    await session.commit()

    logger.debug(
        "mark_files_embedded | done | repo=%s path=%s sha=%s chunks=%d",
        repo_full_name, file_path, file_sha[:8], chunk_count,
    )
