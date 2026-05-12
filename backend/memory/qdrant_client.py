# backend/memory/qdrant_client.py
#
# Qdrant vector store client — the ONLY place in the codebase that talks to Qdrant.
#
# ROLE IN THE POLYGLOT PERSISTENCE STACK:
# (From Polyglot-Persistence.md wiki):
#   "Use the right store for each access pattern."
#   Qdrant handles: "find code chunks semantically similar to this diff"
#   That is a vector similarity search — it belongs in a vector DB, not Postgres.
#   Postgres excels at: structured relational queries (give me HIGH findings for repo X).
#   Qdrant excels at: approximate nearest neighbor search over embeddings.
#
# METADATA-FIRST FILTERING:
# (From RAG-Architecture.md wiki, "Metadata-First Filtering" pattern):
#   "Apply metadata filtering first to reduce search space, then apply semantic
#    search only within the filtered set."
#   Our corpus has a clear deterministic metadata field: repo_full_name.
#   "Similar code" from a different repo is noise, not signal.
#   We apply a Qdrant Filter(must=[FieldCondition(key="repo_full_name", ...)]) FIRST,
#   then run vector similarity within that filtered set.
#   This is faster AND more accurate than pure semantic search across all repos.
#
# COLLECTION DESIGN:
#   Collection name: "code_chunks" (one collection for all repos)
#   Vector size: 1536 (must match EMBEDDING_DIMENSIONS in embedder.py)
#   Distance: Cosine (standard for semantic similarity)
#   Payload per point: repo_full_name, pr_number, file_path, chunk_text
#
#   WHY ONE COLLECTION FOR ALL REPOS AND NOT ONE PER REPO?
#   (Production-Hardening.md: "Operational simplicity beats theoretical purity.")
#   One collection per repo means:
#     - Dynamic collection creation on first PR per repo
#     - Collection cleanup when repos are deleted
#     - Harder to run cross-repo analysis in the future
#   One shared collection with repo_full_name as payload filter is simpler.
#   Qdrant's filter is efficient (BM25-like scan over payload index).
#
# GRACEFUL DEGRADATION — THE #1 CONSTRAINT:
# (From Production-Hardening.md wiki, "Circuit Breaker Pattern"):
#   "When an agent starts misbehaving, you don't want it to fail millions of users."
#   Qdrant is an OPTIONAL enhancement. If it is down:
#     - upsert_code_chunks() logs a warning, returns None silently
#     - search_similar_code() logs a warning, returns [] silently
#     - ensure_collection() logs a warning, returns False silently
#   The caller (context_retriever.py) treats [] as "no context" and returns "".
#   The review pipeline NEVER fails because Qdrant is unavailable.
#   This is the most important invariant in this file. Do not break it.
#
# LAW OF DEMETER (from Pragmatic Programmer wiki):
#   context_retriever.py does not know about qdrant-client library internals.
#   It calls search_similar_code() and gets back a list of plain dicts.
#   If we switch from qdrant-client to a different vector DB library,
#   only this file changes. context_retriever.py is unaffected.

import logging
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from backend.config.settings import get_settings
from backend.memory.embedder import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The single shared Qdrant collection for all code chunks.
# (See module docstring for "one collection for all repos" rationale.)
COLLECTION_NAME = "code_chunks"

# How many similar chunks to retrieve per search.
# 5 is a conservative default — enough context without flooding the agent prompt.
DEFAULT_TOP_K = 5


# ---------------------------------------------------------------------------
# ensure_collection()
#
# Creates the Qdrant collection if it doesn't exist.
# Called once at startup (from main.py lifespan).
# Safe to call multiple times (idempotent).
# ---------------------------------------------------------------------------
async def ensure_collection() -> bool:
    """
    Creates the code_chunks collection if it doesn't already exist.

    Idempotent: calling this multiple times is safe.
    Returns True if the collection exists (or was just created).
    Returns False if Qdrant is unreachable (graceful degradation).

    (Production-Hardening.md: "Fail gracefully on optional dependencies.")
    """
    cfg = get_settings()
    try:
        client = AsyncQdrantClient(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key or None,   # None if not set (local Qdrant)
            timeout=10,
        )

        # Check if collection exists already
        collections = await client.get_collections()
        existing_names = {c.name for c in collections.collections}

        if COLLECTION_NAME in existing_names:
            logger.info("ensure_collection | already_exists | name=%s", COLLECTION_NAME)
            await client.close()
            return True

        # Create the collection with cosine distance + 1536 dims
        # (Must match EMBEDDING_DIMENSIONS in embedder.py)
        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=EMBEDDING_DIMENSIONS,
                distance=Distance.COSINE,
            ),
        )

        logger.info(
            "ensure_collection | created | name=%s dims=%d distance=COSINE",
            COLLECTION_NAME, EMBEDDING_DIMENSIONS,
        )

        await client.close()
        return True

    except Exception as e:
        # Graceful degradation: Qdrant being down does not crash startup.
        # (Production-Hardening.md: "log warning, do NOT raise on optional deps.")
        logger.warning(
            "ensure_collection | qdrant_unavailable | %s: %s",
            type(e).__name__, e,
        )
        return False


# ---------------------------------------------------------------------------
# upsert_code_chunks()
#
# Index code chunks from a PR into Qdrant for future retrieval.
# Called after a PR review to build up the codebase context over time.
# ---------------------------------------------------------------------------
async def upsert_code_chunks(
    pr_number: int,
    repo_full_name: str,
    file_chunks: list[dict[str, Any]],
) -> None:
    """
    Upserts code chunks into Qdrant for RAG context.

    Each chunk becomes one Qdrant PointStruct with:
      - A UUID point ID (stable across upserts for the same chunk)
      - A 1536-dim vector from the chunk's embedded text
      - Payload: repo_full_name, pr_number, file_path, chunk_text

    PAYLOAD DESIGN:
    The payload carries chunk_text so that when we retrieve it, we have
    the actual code text to include in the agent's context.
    (RAG-Architecture.md: "Citation grounding: every retrieved chunk must
    carry provenance — file path and PR number.")
    Without chunk_text in payload, we'd need a second DB lookup to get the text.

    UPSERT NOT INSERT:
    We use upsert (not insert) so that re-reviewing the same PR replaces old
    vectors rather than accumulating duplicates.
    Point IDs are deterministic: uuid5(NAMESPACE_URL, f"{repo}:{pr}:{file_path}")
    Same file in same PR always gets the same ID -> idempotent upsert.

    Args:
        pr_number:      GitHub PR number
        repo_full_name: "owner/repo" string
        file_chunks:    List of dicts, each with:
                          - "file_path": str  (e.g. "src/auth.py")
                          - "text":      str  (the chunk content)
                          - "vector":    list[float]  (pre-computed embedding)

    Returns:
        None. On any Qdrant failure, logs warning and returns silently.
        (Graceful degradation — callers do not need to handle errors.)
    """
    if not file_chunks:
        return

    cfg = get_settings()
    try:
        client = AsyncQdrantClient(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key or None,
            timeout=30,   # longer timeout for batch upserts
        )

        # Build PointStruct list from file_chunks
        points: list[PointStruct] = []
        for chunk in file_chunks:
            file_path = chunk.get("file_path", "")
            text = chunk.get("text", "")
            vector = chunk.get("vector", [])

            if not vector or not text:
                logger.debug(
                    "upsert_code_chunks | skip_empty_chunk | file=%s", file_path
                )
                continue

            # Deterministic point ID: same file+PR+repo always produces same ID.
            # This makes upsert idempotent — re-indexing the same PR replaces vectors.
            point_id_str = f"{repo_full_name}:{pr_number}:{file_path}"
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, point_id_str))

            points.append(PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    # Metadata for filtering (RAG-Architecture.md: "metadata-first")
                    "repo_full_name": repo_full_name,
                    "pr_number": pr_number,
                    # Provenance for citation grounding
                    "file_path": file_path,
                    # The actual text — needed to build the context string
                    "chunk_text": text,
                },
            ))

        if not points:
            logger.debug(
                "upsert_code_chunks | no_valid_chunks | repo=%s pr=%d",
                repo_full_name, pr_number,
            )
            await client.close()
            return

        # Upsert in one batch call
        await client.upsert(
            collection_name=COLLECTION_NAME,
            points=points,
        )

        logger.info(
            "upsert_code_chunks | success | repo=%s pr=%d chunks=%d",
            repo_full_name, pr_number, len(points),
        )

        await client.close()

    except Exception as e:
        # Graceful degradation: failed indexing does not crash the review.
        # The review has already completed — Qdrant is for FUTURE context retrieval.
        logger.warning(
            "upsert_code_chunks | qdrant_error | repo=%s pr=%d error=%s: %s",
            repo_full_name, pr_number, type(e).__name__, e,
        )


# ---------------------------------------------------------------------------
# search_similar_code()
#
# The retrieval half of RAG. Finds code chunks similar to a query vector.
# ---------------------------------------------------------------------------
async def search_similar_code(
    query_vector: list[float],
    repo_full_name: str,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """
    Finds the top-K code chunks most similar to the query vector.

    METADATA-FIRST FILTERING:
    (From RAG-Architecture.md wiki):
    "Apply metadata filtering first to reduce search space, then apply
     semantic search only within the filtered set."

    Step 1: Filter to only chunks where repo_full_name matches.
            This is a deterministic, exact-match filter — no vector math.
            Eliminates all chunks from other repos before vector comparison.
    Step 2: Run cosine similarity search within that filtered set.

    This is both faster (smaller search space) and more accurate (no
    cross-repo false positives). A function named "process_payment" in
    repo A should not surface when reviewing repo B.

    Args:
        query_vector:   1536-dim embedding of the search query (e.g. diff summary)
        repo_full_name: "owner/repo" — only search within this repo's chunks
        top_k:          Max number of results to return (default 5)

    Returns:
        List of dicts (may be empty). Each dict contains:
          - "file_path":   str
          - "chunk_text":  str  (the code content to include in agent context)
          - "score":       float  (cosine similarity, 0.0 to 1.0)
          - "pr_number":   int

        Returns [] on any error (graceful degradation).
    """
    if not query_vector or all(v == 0.0 for v in query_vector):
        # Zero vector (came from empty text in embed_text): no meaningful search.
        return []

    cfg = get_settings()
    try:
        client = AsyncQdrantClient(
            url=cfg.qdrant_url,
            api_key=cfg.qdrant_api_key or None,
            timeout=10,
        )

        # Step 1: Build the metadata filter (repo_full_name exact match)
        # (RAG-Architecture.md: "metadata filtering is faster and more accurate
        #  than pure semantic search on a corpus with deterministic metadata fields")
        repo_filter = Filter(
            must=[
                FieldCondition(
                    key="repo_full_name",
                    match=MatchValue(value=repo_full_name),
                )
            ]
        )

        # Step 2: Vector similarity search with the metadata filter.
        #
        # FIX (Phase 14): AsyncQdrantClient >= 1.7 renamed .search() to
        # .query_points(). The old .search() raises AttributeError at runtime.
        # .query_points() returns a QueryResponse with a .points list of ScoredPoint.
        # (This was the "AsyncQdrantClient has no attribute 'search'" error
        #  seen in worker logs after Phase 13 demo.)
        response = await client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=repo_filter,
            limit=top_k,
            with_payload=True,  # we need the chunk_text from the payload
        )
        hits = response.points  # list[ScoredPoint]

        logger.debug(
            "search_similar_code | repo=%s top_k=%d results=%d",
            repo_full_name, top_k, len(hits),
        )

        # Convert ScoredPoint objects to plain dicts for the caller
        results: list[dict[str, Any]] = []
        for hit in hits:
            payload = hit.payload or {}
            results.append({
                "file_path": payload.get("file_path", ""),
                "chunk_text": payload.get("chunk_text", ""),
                "score": round(float(hit.score), 4),
                "pr_number": payload.get("pr_number"),
            })

        await client.close()
        return results

    except Exception as e:
        # Graceful degradation: Qdrant error returns empty list, not an exception.
        # (Production-Hardening.md: "Circuit Breaker — when Qdrant is down,
        #  context_retriever returns '' and the review still runs with diff only.")
        logger.warning(
            "search_similar_code | qdrant_error | repo=%s error=%s: %s",
            repo_full_name, type(e).__name__, e,
        )
        return []
