# backend/data/ingestion.py
#
# Repository ingestion pipeline.
# Converts a GitHub repo's source files into Tiger Cloud vector embeddings.
#
# DESIGN PHILOSOPHY (Batch-Processing-Patterns.md wiki):
#   "Immutable inputs and explicit outputs: a batch job reads input without
#    modifying it and writes output to a new location. This makes reruns safe."
#
#   GitHub file content is the IMMUTABLE SOURCE. Tiger code_chunks is the DERIVED STORE.
#   This pipeline is safe to re-run at any time — same input produces identical
#   output. If code_chunks is wiped, re-running ingestion restores it fully.
#
# TIGER CHANGE:
#   Previously: upsert_code_chunks() from backend.memory.qdrant_client
#   Now:        TigerMemoryClient.upsert_chunks() via tiger_client.py
#   Same logical interface. Tiger uses DiskANN (pgvectorscale) instead of HNSW (Qdrant).
#   The code_chunks table lives on the Tiger Cloud Postgres instance.
#   Freshness tracking (repo_file_index) is on the same Tiger instance.
#
# COMPOSABLE STAGES (Unix philosophy from Batch wiki):
#   "Each program does one thing well. Programs can be composed via pipes."
#   Each stage below is a pure async function. They compose sequentially:
#     fetch_repo_tree -> filter_code_files -> fetch_file_content
#     -> chunk (split into CodeChunk objects) -> embed -> upsert (Tiger)
#     -> mark_freshness (via freshness.py)
#   Any stage can fail independently without corrupting others.
#
# PER-FILE ERROR ISOLATION:
#   A single file failing to fetch/embed does NOT abort the whole ingestion.
#   The pipeline logs a warning and continues.
#
# FRESHNESS CHECK (Derived-Data-Systems.md wiki):
#   We compare GitHub's current blob SHA against what we last embedded.
#   Same SHA = file unchanged = skip. Different SHA = stale = re-embed.
#   This makes incremental re-indexing cheap: only changed files are re-embedded.
#
# CODE FILE EXTENSIONS:
#   We index source code only — not docs, configs, or binary files.

import logging
from typing import Any

import httpx

from backend.config.settings import get_settings
from backend.data.freshness import get_stale_files, mark_files_embedded
from backend.database.postgres import get_session_factory
from backend.memory.embedder import EmbeddingError, embed_text
from backend.memory.tiger_client import CodeChunk, get_tiger_memory  # TIGER: was qdrant_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# File extensions we consider "source code worth indexing."
# (RAG-Architecture.md: "match retrieval strategy to corpus shape")
# Exclusions: markdown, JSON, YAML, lock files, compiled outputs.
CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".go", ".java", ".rb", ".rs", ".cpp",
    ".c", ".h", ".cs", ".swift", ".kt",
    ".scala", ".php", ".sh", ".bash",
}

# GitHub REST API base URL.
GITHUB_API_BASE = "https://api.github.com"

# Max file size to embed (bytes). Files larger than this are skipped.
# Rationale: embedder.py caps at 8000 chars anyway, but fetching a 500KB
# binary that happens to have a .py extension wastes network and API quota.
MAX_FILE_BYTES = 100_000  # 100 KB

# GitHub API timeout (seconds). Per-request, not total pipeline timeout.
GITHUB_REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Stage 1: fetch_repo_tree
#
# Fetches the complete file tree of a repo at its HEAD commit.
# Returns a flat list of file dicts: {path, sha, size, type}.
# Only returns "blob" entries (files, not directories).
#
# WHY recursive=1: GitHub's tree API supports fetching the entire tree in
# one request using ?recursive=1. Without it, we'd have to walk each
# directory with separate requests — O(depth * branching_factor) API calls
# instead of one.
#
# (Batch-Processing-Patterns.md: "Prefer sequential I/O over many round trips.")
# ---------------------------------------------------------------------------
async def fetch_repo_tree(
    repo_full_name: str,
    token: str,
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """
    Fetch the full file tree of a repo (HEAD commit, recursive).

    Returns list of dicts with keys: path, sha, size, type.
    Returns [] if the repo is unreachable (graceful degradation).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Step 1: get default branch SHA
    repo_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}"
    try:
        resp = await client.get(repo_url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
        resp.raise_for_status()
        repo_data = resp.json()
        default_branch = repo_data.get("default_branch", "main")
    except httpx.HTTPStatusError as e:
        logger.warning(
            "fetch_repo_tree | repo_not_found | repo=%s status=%d",
            repo_full_name, e.response.status_code,
        )
        return []
    except Exception as e:
        logger.warning(
            "fetch_repo_tree | github_error | repo=%s error=%s",
            repo_full_name, e,
        )
        return []

    # Step 2: get HEAD commit SHA for default branch
    branch_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/branches/{default_branch}"
    try:
        resp = await client.get(branch_url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
        resp.raise_for_status()
        branch_data = resp.json()
        tree_sha = branch_data["commit"]["commit"]["tree"]["sha"]
    except Exception as e:
        logger.warning(
            "fetch_repo_tree | branch_error | repo=%s branch=%s error=%s",
            repo_full_name, default_branch, e,
        )
        return []

    # Step 3: fetch full tree recursively (one API call for entire repo)
    tree_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/git/trees/{tree_sha}?recursive=1"
    try:
        resp = await client.get(tree_url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
        resp.raise_for_status()
        tree_data = resp.json()

        # "truncated" means the repo has >100,000 files. Extremely rare.
        # We log a warning but still process what we got.
        if tree_data.get("truncated"):
            logger.warning(
                "fetch_repo_tree | tree_truncated | repo=%s "
                "items_returned=%d (repo has >100k files)",
                repo_full_name, len(tree_data.get("tree", [])),
            )

        # Return only blobs (files, not trees/directories)
        blobs = [
            item for item in tree_data.get("tree", [])
            if item.get("type") == "blob"
        ]

        logger.info(
            "fetch_repo_tree | done | repo=%s total_blobs=%d",
            repo_full_name, len(blobs),
        )
        return blobs

    except Exception as e:
        logger.warning(
            "fetch_repo_tree | tree_error | repo=%s error=%s",
            repo_full_name, e,
        )
        return []


# ---------------------------------------------------------------------------
# Stage 2: filter_code_files
#
# Filters a tree to code files only.
# Applies two filters:
#   1. Extension must be in CODE_EXTENSIONS
#   2. File size must be <= MAX_FILE_BYTES (skip large files)
#
# (RAG-Architecture.md: "Metadata-first filtering reduces search space and
#  improves accuracy." We apply this at ingestion too — index only what
#  an agent can meaningfully reason about.)
# ---------------------------------------------------------------------------
def filter_code_files(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Filter tree entries to indexable source code files.

    Returns subset of tree entries that are small enough and have
    a recognized source code extension.
    """
    result = []
    skipped_ext = 0
    skipped_size = 0

    for item in tree:
        path: str = item.get("path", "")
        size: int = item.get("size", 0)

        # Check extension
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        if ext.lower() not in CODE_EXTENSIONS:
            skipped_ext += 1
            continue

        # Check size
        if size > MAX_FILE_BYTES:
            skipped_size += 1
            logger.debug(
                "filter_code_files | skip_large | path=%s size=%d", path, size
            )
            continue

        result.append(item)

    logger.info(
        "filter_code_files | done | kept=%d skipped_ext=%d skipped_size=%d",
        len(result), skipped_ext, skipped_size,
    )
    return result


# ---------------------------------------------------------------------------
# Stage 3: fetch_file_content
#
# Fetches the raw text content of a single file from GitHub.
# Uses the blob SHA (from the tree) to fetch via the blob API.
# Falls back to the contents API if blob fetch fails.
#
# WHY blob API over contents API:
#   The blob API returns base64-encoded raw bytes. The contents API has a
#   1MB limit and is slower for large files. For code files <= 100KB, both
#   work — we use contents API as it returns utf-8 decoded text directly.
# ---------------------------------------------------------------------------
async def fetch_file_content(
    repo_full_name: str,
    file_path: str,
    token: str,
    client: httpx.AsyncClient,
) -> str | None:
    """
    Fetch the raw text content of a file from GitHub.

    Returns None if the file cannot be fetched (graceful degradation).
    Binary files that cannot be decoded as UTF-8 are also returned as None.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/contents/{file_path}"
    try:
        resp = await client.get(url, headers=headers, timeout=GITHUB_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # GitHub returns base64-encoded content for file contents
        import base64
        content_b64 = data.get("content", "")
        if not content_b64:
            return None

        # Remove newlines GitHub inserts into base64 output
        content_bytes = base64.b64decode(content_b64.replace("\n", ""))

        # Attempt UTF-8 decode — skip binary files
        try:
            return content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            logger.debug(
                "fetch_file_content | binary_skip | path=%s", file_path
            )
            return None

    except httpx.HTTPStatusError as e:
        logger.warning(
            "fetch_file_content | http_error | path=%s status=%d",
            file_path, e.response.status_code,
        )
        return None
    except Exception as e:
        logger.warning(
            "fetch_file_content | error | path=%s error=%s", file_path, e
        )
        return None


# ---------------------------------------------------------------------------
# ingest_repository
#
# Main entry point. Orchestrates all stages for a single repository.
#
# FLOW:
#   1. fetch_repo_tree -> full file list with SHAs
#   2. filter_code_files -> code files only
#   3. freshness check -> skip already-embedded files with same SHA
#   4. For each stale file:
#      a. fetch_file_content
#      b. embed_text (embedder.py)
#      c. upsert_code_chunks (qdrant_client.py)
#      d. mark_files_embedded (freshness.py)
#   5. Log summary: total / stale / embedded / skipped
#
# CALLED BY: arq_worker.py after a review completes (background job).
# NOT called from the review pipeline itself — ingestion is async/best-effort.
#
# IDEMPOTENT: Safe to call multiple times for the same repo.
#   Files with unchanged SHAs are skipped. New/changed files are re-embedded.
#   (Batch-Processing-Patterns.md: "Re-runnable batch jobs are a feature.")
# ---------------------------------------------------------------------------
async def ingest_repository(repo_full_name: str) -> dict[str, int]:
    """
    Ingest all code files from a GitHub repository into Qdrant.

    Returns a summary dict:
      total_files    — total code files in repo
      stale_files    — files that needed re-embedding (new or changed)
      embedded       — files successfully embedded
      skipped_fresh  — files skipped because SHA unchanged
      errors         — files that failed to fetch or embed
    """
    cfg = get_settings()

    if not cfg.github_token:
        logger.warning(
            "ingest_repository | no_github_token | repo=%s skipping ingestion",
            repo_full_name,
        )
        return {"total_files": 0, "stale_files": 0, "embedded": 0,
                "skipped_fresh": 0, "errors": 0}

    summary = {
        "total_files": 0,
        "stale_files": 0,
        "embedded": 0,
        "skipped_fresh": 0,
        "errors": 0,
    }

    logger.info("ingest_repository | start | repo=%s", repo_full_name)

    # Use a single httpx.AsyncClient for all GitHub requests.
    # (Reuse TCP connections across file fetches — reduces latency.)
    async with httpx.AsyncClient() as client:

        # Stage 1: fetch the full file tree
        tree = await fetch_repo_tree(repo_full_name, cfg.github_token, client)
        if not tree:
            logger.warning(
                "ingest_repository | empty_tree | repo=%s "
                "(repo may not exist or token has no access)",
                repo_full_name,
            )
            return summary

        # Stage 2: filter to code files only
        code_files = filter_code_files(tree)
        summary["total_files"] = len(code_files)

        if not code_files:
            logger.info(
                "ingest_repository | no_code_files | repo=%s", repo_full_name
            )
            return summary

        # Stage 3: freshness check
        # Build {file_path: blob_sha} for all code files
        file_sha_map = {f["path"]: f["sha"] for f in code_files}

        # Get async session factory (NOT async with get_db() — Bug #2 in skill)
        # get_db() is an async generator (yield), not a context manager.
        # We must use the async_sessionmaker directly.
        session_factory = get_session_factory()

        async with session_factory() as session:
            stale_files = await get_stale_files(session, repo_full_name, file_sha_map)

        summary["stale_files"] = len(stale_files)
        summary["skipped_fresh"] = len(code_files) - len(stale_files)

        logger.info(
            "ingest_repository | freshness_check | repo=%s "
            "total=%d stale=%d fresh=%d",
            repo_full_name, len(code_files),
            len(stale_files), summary["skipped_fresh"],
        )

        if not stale_files:
            logger.info(
                "ingest_repository | all_fresh | repo=%s nothing to embed",
                repo_full_name,
            )
            return summary

        # Stage 4: embed and upsert each stale file
        # Per-file error isolation: one bad file doesn't abort the rest.
        # (Distributed-Systems-Fault-Model.md: "A fault is not a failure.")
        for file_path, file_sha in stale_files.items():

            # Stage 4a: fetch raw content
            content = await fetch_file_content(
                repo_full_name, file_path, cfg.github_token, client
            )
            if content is None:
                summary["errors"] += 1
                continue

            # Stage 4b: embed the content
            try:
                vector = await embed_text(content)
            except EmbeddingError as e:
                logger.warning(
                    "ingest_repository | embed_error | repo=%s path=%s error=%s",
                    repo_full_name, file_path, e,
                )
                summary["errors"] += 1
                continue

            # Stage 4c: upsert into Tiger Cloud (code_chunks table, DiskANN index)
            # TIGER: was upsert_code_chunks() to Qdrant.
            # Now: TigerMemoryClient.upsert_chunks() with CodeChunk dataclass.
            # Same file-as-unit chunking strategy. Payload stays in the content + symbol fields.
            # (RAG-Architecture.md: "Every chunk must carry provenance.")
            chunk = CodeChunk(
                repo=repo_full_name,
                path=file_path,
                content=content[:8000],   # consistent with embedder MAX_EMBED_CHARS
                embedding=vector,
                chunk_index=0,            # single-chunk-per-file strategy (Phase 6)
                token_count=len(content.split()),
            )
            tiger = get_tiger_memory()
            await tiger.upsert_chunks([chunk])

            # Stage 4d: record freshness
            async with session_factory() as session:
                await mark_files_embedded(
                    session=session,
                    repo_full_name=repo_full_name,
                    file_path=file_path,
                    file_sha=file_sha,
                    chunk_count=1,  # one chunk per file (file-as-unit strategy)
                )

            summary["embedded"] += 1
            logger.debug(
                "ingest_repository | embedded | repo=%s path=%s",
                repo_full_name, file_path,
            )

    logger.info(
        "ingest_repository | complete | repo=%s "
        "total=%d stale=%d embedded=%d skipped=%d errors=%d",
        repo_full_name,
        summary["total_files"],
        summary["stale_files"],
        summary["embedded"],
        summary["skipped_fresh"],
        summary["errors"],
    )
    return summary
