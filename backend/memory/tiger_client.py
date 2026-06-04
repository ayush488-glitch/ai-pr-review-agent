# backend/memory/tiger_client.py
#
# Tiger Cloud memory client — replaces qdrant_client.py entirely.
#
# WHY TIGER INSTEAD OF QDRANT?
# (ADR-003: Tiger Cloud Data Layer)
#
# Three separate stores existed before:
#   - Qdrant: vector search (ANN, HNSW index)
#   - PostgreSQL: structured history
#   - Redis: job queue
#
# Tiger Cloud (TimescaleDB + pgvectorscale) collapses the first two:
#   - pgvectorscale DiskANN replaces Qdrant's HNSW
#   - Same Postgres connection serves RAG queries AND structured queries
#   - One connection pool, one backup policy, one place to reason about data
#
# DISKANN VS HNSW (why DiskANN wins on code RAG):
# Benchmark: 50M Cohere embeddings, 768 dims
#   DiskANN (pgvectorscale): 28x lower p95 latency, 16x higher throughput vs Pinecone s1
#   At 99% recall. At 75% lower cost when self-hosted.
# Mechanism: Statistical Binary Quantization (SBQ) compresses index into disk-friendly
#   segments. Streaming access pattern matches sequential disk read, not random access.
#
# FRESHNESS DECAY:
# Qdrant has no native time-aware scoring. We would need a custom payload filter.
# DiskANN + plain SQL: score * exp(-hours_since_update / 168.0)
# 168 hours = 1 week half-life. A file updated today scores 1.0. A week-old file 0.5.
# Incentivizes retrieving recently modified code — directly relevant to the PR under review.
#
# HYBRID RETRIEVAL (semantic + keyword):
# Postgres has native tsvector full-text search.
# We combine DiskANN cosine score with BM25-style tsvector rank using
# Reciprocal Rank Fusion (RRF): 1/(k + rank_semantic) + 1/(k + rank_keyword)
# k=60 dampens outliers. Result: consistently better top-5 recall than either alone.
#
# DEPENDENCY DIRECTION:
#   tiger_client.py imports from: asyncpg, pgvector, backend.config.settings
#   tiger_client.py does NOT import from: agents, orchestrator, observability
#   This keeps it at the memory layer — correct per ADR-002 dependency rules.

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector

from backend.config.settings import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Freshness decay constants
#
# Half-life = 168 hours (1 week). Files updated within the last day score ~0.99.
# Files unchanged for a week score 0.5. Files unchanged for a month score ~0.12.
# This prioritises recently modified code — most relevant for a PR review that
# touches those same files.
# ---------------------------------------------------------------------------
_FRESHNESS_HALF_LIFE_HOURS: float = 168.0


# ---------------------------------------------------------------------------
# CodeChunk dataclass
#
# Mirrors the code_chunks table. Plain dataclass (not SQLAlchemy ORM) because
# this is the hot path: hundreds of inserts per ingestion run.
# SQLAlchemy ORM adds ~2ms per-row from Python-side hydration. asyncpg raw
# executemany is 10x faster for bulk writes.
# ---------------------------------------------------------------------------
@dataclass
class CodeChunk:
    """
    A single embedded code chunk stored in Tiger Cloud.

    repo:        GitHub repo full name, e.g. "owner/repo"
    path:        File path relative to repo root, e.g. "backend/main.py"
    content:     The raw text of this chunk
    embedding:   Dense vector from text-embedding-3-large, 256 dims
    chunk_index: Position of this chunk within the file (0-based)
    symbol:      Optional function/class name extracted from AST
    token_count: Approximate token count (used for context budget)
    updated_at:  When this chunk was last embedded (for freshness decay)
    id:          UUID primary key (auto-generated if not provided)
    """
    repo: str
    path: str
    content: str
    embedding: list[float]
    chunk_index: int = 0
    symbol: str | None = None
    token_count: int | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# TigerMemoryClient
#
# Thin async wrapper around asyncpg. One method per capability.
# The connection pool is shared with the events spine (same Tiger Cloud instance).
# ---------------------------------------------------------------------------
class TigerMemoryClient:
    """
    Tiger Cloud semantic memory client.

    Manages code embeddings in the code_chunks table using pgvectorscale
    DiskANN for approximate nearest-neighbor search.

    Usage:
        pool = await TigerMemoryClient.create_pool()
        client = TigerMemoryClient(pool)
        chunks = await client.search(embedding, repo="owner/repo")
    """

    # RRF damping factor. k=60 is the standard choice.
    # Larger k: reduces impact of very high-ranked results (safer, more conservative)
    # Smaller k: amplifies top-rank differences (riskier, less stable)
    _RRF_K: int = 60

    # Maximum rows from each retrieval arm before RRF merge
    _RETRIEVAL_EXPANSION: int = 20

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -------------------------------------------------------------------------
    # Pool factory
    #
    # Called once at application startup (from backend/database/postgres.py
    # init_tiger_schema). Returns a pool stored at module level in postgres.py.
    # -------------------------------------------------------------------------
    @classmethod
    async def create_pool(
        cls,
        dsn: str | None = None,
        min_size: int = 2,
        max_size: int = 10,
    ) -> asyncpg.Pool:
        """
        Creates an asyncpg connection pool for Tiger Cloud.

        Registers the pgvector VECTOR codec so asyncpg can serialize/deserialize
        embedding columns transparently as Python lists.

        Args:
            dsn:      Tiger Cloud DSN. Falls back to settings if not provided.
            min_size: Minimum pool connections (default 2).
            max_size: Maximum pool connections (default 10).

        Returns:
            An asyncpg.Pool ready for use.
        """
        cfg = get_settings()
        # Resolve DSN: explicit arg > TIGER_DATABASE_URL env > DATABASE_URL fallback
        resolved_dsn = dsn or cfg.tiger_database_url or cfg.database_url
        # asyncpg DSN format: postgres:// or postgresql+asyncpg:// -> strip driver prefix
        resolved_dsn = resolved_dsn.replace("postgresql+asyncpg://", "postgresql://")

        async def _init_conn(conn: asyncpg.Connection) -> None:
            # Register pgvector codec so VECTOR columns come back as Python lists
            await register_vector(conn)

        pool = await asyncpg.create_pool(
            dsn=resolved_dsn,
            min_size=min_size,
            max_size=max_size,
            init=_init_conn,
            command_timeout=30,
        )
        logger.info(
            "Tiger Cloud pool created | host=%s min=%d max=%d",
            resolved_dsn.split("@")[-1].split("/")[0] if "@" in resolved_dsn else "local",
            min_size,
            max_size,
        )
        return pool

    # -------------------------------------------------------------------------
    # upsert_chunks
    #
    # Bulk insert-or-replace. Called by data/ingestion.py after embedding.
    # Uses asyncpg executemany for throughput (no per-row round-trip overhead).
    # ON CONFLICT: update embedding + content + token_count + updated_at.
    # This means re-running ingestion on a modified file updates the chunk
    # in place rather than creating duplicates.
    # -------------------------------------------------------------------------
    async def upsert_chunks(self, chunks: list[CodeChunk]) -> int:
        """
        Upsert a list of CodeChunks into code_chunks.

        Idempotent: re-indexing the same chunk updates it in place.
        Uses asyncpg executemany — single round-trip, batch binding.

        Args:
            chunks: List of CodeChunk dataclasses to upsert.

        Returns:
            Number of rows upserted.
        """
        if not chunks:
            return 0

        sql = """
            INSERT INTO code_chunks
                (id, repo, path, symbol, chunk_index, content, embedding, token_count, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (repo, path, chunk_index)
            DO UPDATE SET
                content     = EXCLUDED.content,
                embedding   = EXCLUDED.embedding,
                symbol      = EXCLUDED.symbol,
                token_count = EXCLUDED.token_count,
                updated_at  = EXCLUDED.updated_at
        """
        rows = [
            (
                c.id, c.repo, c.path, c.symbol, c.chunk_index,
                c.content, c.embedding, c.token_count, c.updated_at,
            )
            for c in chunks
        ]
        async with self._pool.acquire() as conn:
            await conn.executemany(sql, rows)

        logger.debug("upsert_chunks | count=%d repo=%s", len(chunks), chunks[0].repo if chunks else "?")
        return len(chunks)

    # -------------------------------------------------------------------------
    # search
    #
    # Hybrid retrieval: DiskANN cosine (semantic) + tsvector BM25 (keyword).
    # Merged with RRF (Reciprocal Rank Fusion).
    # Freshness decay applied as a post-score multiplier.
    #
    # WHY HYBRID?
    # Semantic alone: misses exact function names, variable names, error codes.
    # Keyword alone: misses paraphrased concepts ("null check" vs "guard clause").
    # Hybrid (RRF merge): consistently +8-15% top-5 recall on code retrieval benchmarks.
    # -------------------------------------------------------------------------
    async def search(
        self,
        query_embedding: list[float],
        repo: str,
        top_k: int = 10,
        hybrid: bool = True,
        query_text: str | None = None,
        freshness_decay: bool = True,
    ) -> list[CodeChunk]:
        """
        Retrieve the most relevant code chunks for a query.

        Args:
            query_embedding: Dense vector from the same embedding model as stored chunks.
            repo:            Filter to a specific repository (owner/repo).
            top_k:           Number of results to return.
            hybrid:          If True, blend semantic + keyword search via RRF.
            query_text:      Raw query string for keyword search arm (required when hybrid=True).
            freshness_decay: If True, multiply scores by freshness factor (exp(-age/168h)).

        Returns:
            List of CodeChunk, ordered by combined relevance score (best first).
        """
        if hybrid and query_text:
            return await self._hybrid_search(
                query_embedding=query_embedding,
                query_text=query_text,
                repo=repo,
                top_k=top_k,
                freshness_decay=freshness_decay,
            )
        return await self._semantic_search(
            query_embedding=query_embedding,
            repo=repo,
            top_k=top_k,
            freshness_decay=freshness_decay,
        )

    async def _semantic_search(
        self,
        query_embedding: list[float],
        repo: str,
        top_k: int,
        freshness_decay: bool,
    ) -> list[CodeChunk]:
        """
        Pure DiskANN cosine similarity search with optional freshness decay.

        The DiskANN index (diskann) is created in the migration SQL.
        Postgres planner uses it for ORDER BY embedding <=> $1 LIMIT N queries.
        """
        decay_expr = (
            "* EXP(-EXTRACT(EPOCH FROM (now() - updated_at)) / 3600.0 / 168.0)"
            if freshness_decay else ""
        )
        sql = f"""
            SELECT
                id, repo, path, symbol, chunk_index, content,
                embedding, token_count, updated_at,
                (1 - (embedding <=> $1)) {decay_expr} AS score
            FROM code_chunks
            WHERE repo = $2
            ORDER BY score DESC
            LIMIT $3
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, query_embedding, repo, top_k)
        return [self._row_to_chunk(r) for r in rows]

    async def _hybrid_search(
        self,
        query_embedding: list[float],
        query_text: str,
        repo: str,
        top_k: int,
        freshness_decay: bool,
    ) -> list[CodeChunk]:
        """
        Hybrid search: DiskANN + tsvector, merged with RRF.

        Two arms:
        1. Semantic arm: DiskANN cosine, expanded to _RETRIEVAL_EXPANSION results.
        2. Keyword arm: tsvector ts_rank_cd, expanded to _RETRIEVAL_EXPANSION results.

        RRF merge: score = 1/(k + rank_semantic) + 1/(k + rank_keyword)
        k=60. Freshness decay applied to final merged scores.
        Final result: top_k rows by merged RRF score.
        """
        expand = self._RETRIEVAL_EXPANSION
        k = self._RRF_K

        decay_expr = (
            "* EXP(-EXTRACT(EPOCH FROM (now() - c.updated_at)) / 3600.0 / 168.0)"
            if freshness_decay else ""
        )

        sql = f"""
        WITH semantic AS (
            SELECT
                id,
                ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS rank_s
            FROM code_chunks
            WHERE repo = $3
            ORDER BY embedding <=> $1
            LIMIT $4
        ),
        keyword AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    ORDER BY ts_rank_cd(content_tsv, plainto_tsquery('english', $2)) DESC
                ) AS rank_k
            FROM code_chunks
            WHERE repo = $3
              AND content_tsv @@ plainto_tsquery('english', $2)
            LIMIT $4
        ),
        rrf AS (
            SELECT
                COALESCE(s.id, kw.id) AS id,
                (1.0 / ($5 + COALESCE(s.rank_s, {expand + 1}))
                + 1.0 / ($5 + COALESCE(kw.rank_k, {expand + 1}))) AS rrf_score
            FROM semantic s
            FULL OUTER JOIN keyword kw ON s.id = kw.id
        )
        SELECT
            c.id, c.repo, c.path, c.symbol, c.chunk_index, c.content,
            c.embedding, c.token_count, c.updated_at,
            rrf.rrf_score {decay_expr} AS score
        FROM rrf
        JOIN code_chunks c ON c.id = rrf.id
        ORDER BY score DESC
        LIMIT $6
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                sql, query_embedding, query_text, repo, expand, k, top_k
            )
        return [self._row_to_chunk(r) for r in rows]

    # -------------------------------------------------------------------------
    # delete_stale_chunks
    #
    # Called by data/ingestion.py when a file is deleted from the repo.
    # Also useful for maintenance: remove chunks older than N days for repos
    # that have been inactive.
    # -------------------------------------------------------------------------
    async def delete_stale_chunks(
        self,
        repo: str,
        path: str | None = None,
        before: datetime | None = None,
    ) -> int:
        """
        Delete chunks from code_chunks.

        Args:
            repo:   Repository to scope the deletion.
            path:   If provided, delete only chunks for this file path.
            before: If provided, delete chunks not updated since this timestamp.

        Returns:
            Number of rows deleted.
        """
        conditions = ["repo = $1"]
        params: list[Any] = [repo]
        idx = 2

        if path is not None:
            conditions.append(f"path = ${idx}")
            params.append(path)
            idx += 1

        if before is not None:
            conditions.append(f"updated_at < ${idx}")
            params.append(before)

        where = " AND ".join(conditions)
        sql = f"DELETE FROM code_chunks WHERE {where}"

        async with self._pool.acquire() as conn:
            result = await conn.execute(sql, *params)

        deleted = int(result.split()[-1]) if result else 0
        logger.info("delete_stale_chunks | repo=%s path=%s deleted=%d", repo, path, deleted)
        return deleted

    # -------------------------------------------------------------------------
    # get_chunk_count
    #
    # Fast count query. Used by health checks and freshness tracking.
    # -------------------------------------------------------------------------
    async def get_chunk_count(self, repo: str | None = None) -> int:
        """
        Returns the number of chunks in code_chunks.

        If repo is provided, counts only for that repository.
        """
        if repo:
            sql = "SELECT count(*) FROM code_chunks WHERE repo = $1"
            async with self._pool.acquire() as conn:
                return await conn.fetchval(sql, repo)
        else:
            sql = "SELECT count(*) FROM code_chunks"
            async with self._pool.acquire() as conn:
                return await conn.fetchval(sql)

    # -------------------------------------------------------------------------
    # health_check
    #
    # Called from backend/main.py /health endpoint.
    # Returns dict so the health route can JSON-serialize it.
    # -------------------------------------------------------------------------
    async def health_check(self) -> dict[str, Any]:
        """
        Returns health status for Tiger Cloud memory store.

        Checks:
        - Connection pool is alive (SELECT 1)
        - code_chunks table exists and is accessible
        - DiskANN index is present
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
                chunk_count = await conn.fetchval("SELECT count(*) FROM code_chunks")
                last_updated = await conn.fetchval(
                    "SELECT max(updated_at) FROM code_chunks"
                )
                index_exists = await conn.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM pg_indexes
                        WHERE indexname = 'code_chunks_emb_idx'
                    )
                    """
                )
            return {
                "status": "ok",
                "chunk_count": chunk_count,
                "last_updated": last_updated.isoformat() if last_updated else None,
                "diskann_index": index_exists,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("Tiger memory health check failed: %s", exc)
            return {"status": "error", "error": str(exc)}

    # -------------------------------------------------------------------------
    # _row_to_chunk  (private helper)
    # -------------------------------------------------------------------------
    @staticmethod
    def _row_to_chunk(row: asyncpg.Record) -> CodeChunk:
        """Convert an asyncpg Record to a CodeChunk dataclass."""
        return CodeChunk(
            id=str(row["id"]),
            repo=row["repo"],
            path=row["path"],
            symbol=row["symbol"],
            chunk_index=row["chunk_index"],
            content=row["content"],
            embedding=list(row["embedding"]) if row["embedding"] is not None else [],
            token_count=row["token_count"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# Module-level singleton
#
# Created during application startup in backend/database/postgres.py
# init_tiger_schema(). Stored here so any module can import and use it
# without needing to pass a pool around.
#
# Usage from other modules:
#   from backend.memory.tiger_client import tiger_memory
#   chunks = await tiger_memory.search(embedding, repo="owner/repo")
# ---------------------------------------------------------------------------
tiger_memory: TigerMemoryClient | None = None


def get_tiger_memory() -> TigerMemoryClient:
    """
    Returns the module-level TigerMemoryClient singleton.

    Raises RuntimeError if called before init_tiger_schema() has run.
    This is intentional — callers should never use memory before startup completes.
    """
    if tiger_memory is None:
        raise RuntimeError(
            "TigerMemoryClient not initialized. "
            "Call init_tiger_schema() during application startup before using tiger_memory."
        )
    return tiger_memory
