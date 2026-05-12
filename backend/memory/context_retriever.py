# backend/memory/context_retriever.py
#
# The single entry point for RAG context retrieval.
#
# WHAT THIS FILE DOES:
#   1. Takes a PR diff as the "query" — what are we looking for?
#   2. Embeds the first MAX_QUERY_CHARS of the diff (a semantic fingerprint)
#   3. Asks Qdrant: "what code have we seen before that looks like this?"
#   4. Formats the top results as a plain text context string
#   5. Returns that string to be injected into the agent prompts
#
# SINGLE ENTRY POINT PRINCIPLE (from Decoupling-and-Law-of-Demeter.md wiki):
#   "A module should talk to its immediate neighbors, not reach through them."
#   nodes.py calls retrieve_context_for_diff() ONLY.
#   It does not import embedder.py or qdrant_client.py.
#   If we replace Qdrant with Weaviate, only this file and qdrant_client.py change.
#   nodes.py is untouched.
#
# GRACEFUL DEGRADATION — THE CARDINAL RULE:
# (From Production-Hardening.md wiki, "Defense in Depth"):
#   "Each guardrail is independent. Each can block."
#   Applied here as: each failure point is independent. Each returns "".
#   Failure at any step returns "" immediately:
#     - Empty diff         -> return ""
#     - Embed fails        -> return ""  (EmbeddingError caught)
#     - Qdrant unavailable -> return ""  (search returns [] on error)
#     - No results found   -> return ""
#   The review pipeline receives "" and runs with diff-only analysis.
#   This is NOT a degraded experience — it is the DESIGNED fallback.
#   RAG context is enhancement, not foundation.
#
# QUERY CONSTRUCTION:
# (From RAG-Architecture.md wiki):
#   "Dense embeddings capture semantic understanding."
#   We embed the FIRST part of the diff (the changed code) not the entire diff.
#   The first part of a unified diff contains the most semantically dense content:
#   file headers, hunk headers, the actual changed lines.
#   We cap at MAX_QUERY_CHARS to stay within embedding API limits.
#
# CONTEXT FORMAT:
# The output is a plain text block:
#   --- Relevant prior code context ---
#   [File: src/auth.py (from PR #17)]
#   <code chunk text>
#   ---
#   [File: src/utils.py (from PR #15)]
#   <code chunk text>
#   ---
#
# This format was chosen because:
#   1. Agents get a labeled context block they can cite ("see prior context for auth.py")
#   2. File names are explicit — agents can say "auth.py was seen before and had pattern X"
#   3. PR number is included — human reviewers can trace the provenance
#
# (RAG-Architecture.md: "Citation grounding: every retrieved chunk must carry provenance.")

import logging

from backend.config.settings import Settings, get_settings
from backend.memory.embedder import EmbeddingError, embed_text
from backend.memory.qdrant_client import search_similar_code

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many chars of the diff to embed as the search query.
# The first 2000 chars of a unified diff contains the key semantic signal:
# file paths, hunk headers, the first few changed functions.
# Embedding the full diff (potentially 100k chars) would exceed API limits
# and introduce noise from less-relevant later hunks.
MAX_QUERY_CHARS = 2_000

# Minimum cosine similarity score to include a result.
# Qdrant always returns top_k results even if they are not very similar.
# We filter out low-relevance results (score below this threshold).
# 0.5 is conservative — we want genuinely similar code, not just the
# "least dissimilar" chunk in the collection.
MIN_SIMILARITY_SCORE = 0.5


# ---------------------------------------------------------------------------
# retrieve_context_for_diff()
#
# The public interface. This is the only function nodes.py calls.
# ---------------------------------------------------------------------------
async def retrieve_context_for_diff(
    diff: str,
    repo_full_name: str,
    settings: Settings | None = None,
) -> str:
    """
    Retrieves RAG context for a PR diff.

    Queries Qdrant for code chunks similar to this diff and returns them
    as a formatted context string for injection into agent prompts.

    GRACEFUL DEGRADATION:
    This function NEVER raises. On any failure, it returns "".
    The caller (build_context node in nodes.py) uses the result as:
        retrieved_context = await retrieve_context_for_diff(diff, repo, settings)
        # retrieved_context is always a str — may be empty string

    Args:
        diff:           The PR unified diff string
        repo_full_name: "owner/repo" — scopes retrieval to this repo only
        settings:       Optional Settings instance. If None, calls get_settings().
                        (Dependency injection for testability —
                         Design-by-Contract.md: "pass context explicitly.")

    Returns:
        A formatted context string (may be empty string "").
        Empty string means: no relevant prior context found, or retrieval failed.
        The caller must handle "" as a valid response.
    """
    # --- Guard: empty diff ---
    if not diff or not diff.strip():
        logger.debug(
            "retrieve_context | empty_diff | repo=%s -> returning ''", repo_full_name
        )
        return ""

    # --- Guard: empty repo ---
    if not repo_full_name or not repo_full_name.strip():
        logger.debug("retrieve_context | empty_repo -> returning ''")
        return ""

    cfg = settings or get_settings()

    # Step 1: Build the query text.
    # We use the first MAX_QUERY_CHARS of the diff as our semantic query.
    # (See module docstring for why we take the first part, not a summary.)
    query_text = diff[:MAX_QUERY_CHARS]

    # Step 2: Embed the query text.
    # EmbeddingError means OpenAI is down or the API key is wrong.
    # Either way: return "" and let the review run without RAG context.
    try:
        query_vector = await embed_text(query_text)
    except EmbeddingError as e:
        logger.warning(
            "retrieve_context | embed_failed | repo=%s error=%s -> returning ''",
            repo_full_name, str(e),
        )
        return ""
    except Exception as e:
        # Defensive catch for unexpected errors (e.g. network timeout not wrapped)
        logger.warning(
            "retrieve_context | embed_unexpected_error | repo=%s error=%s: %s -> returning ''",
            repo_full_name, type(e).__name__, e,
        )
        return ""

    # Step 3: Search Qdrant.
    # search_similar_code() already handles all Qdrant errors and returns [].
    # (See qdrant_client.py for graceful degradation implementation.)
    results = await search_similar_code(
        query_vector=query_vector,
        repo_full_name=repo_full_name,
        top_k=5,
    )

    # Step 4: Filter by minimum similarity score.
    # (RAG-Architecture.md: "If both agree, confidence rises; if neither agrees, skip.")
    relevant_results = [r for r in results if r.get("score", 0.0) >= MIN_SIMILARITY_SCORE]

    if not relevant_results:
        logger.debug(
            "retrieve_context | no_relevant_results | repo=%s score_threshold=%.2f -> ''",
            repo_full_name, MIN_SIMILARITY_SCORE,
        )
        return ""

    # Step 5: Format the context string.
    context = _format_context(relevant_results)

    logger.info(
        "retrieve_context | success | repo=%s chunks=%d",
        repo_full_name, len(relevant_results),
    )

    return context


# ---------------------------------------------------------------------------
# PRIVATE HELPERS
# ---------------------------------------------------------------------------

def _format_context(results: list[dict]) -> str:
    """
    Formats Qdrant search results as a plain text context block.

    Each result becomes a labeled section with file path, PR number, and code.
    The format is designed to be injected directly into an agent's user message.

    WHY PLAIN TEXT AND NOT JSON?
    The context is part of an LLM prompt, not an API response.
    Plain text is more token-efficient and agents reason about it more naturally.
    JSON keys add token overhead without adding semantics.

    CITATION GROUNDING (RAG-Architecture.md):
    Every chunk is labeled with its source: file path AND PR number.
    Agents can include this provenance in their findings:
    "Similar pattern was seen in auth.py (PR #17) — check for same issue."
    """
    lines: list[str] = ["--- Relevant prior code context ---"]

    for result in results:
        file_path = result.get("file_path", "unknown")
        chunk_text = result.get("chunk_text", "")
        pr_number = result.get("pr_number", "?")
        score = result.get("score", 0.0)

        # Skip chunks with empty content (shouldn't happen but defensive)
        if not chunk_text.strip():
            continue

        lines.append(f"\n[File: {file_path} | from PR #{pr_number} | similarity: {score:.2f}]")
        lines.append(chunk_text)
        lines.append("---")

    if len(lines) <= 1:
        # Only the header was added (all chunks had empty text)
        return ""

    return "\n".join(lines)
