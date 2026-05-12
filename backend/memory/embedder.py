# backend/memory/embedder.py
#
# OpenAI embedding client — the bridge between raw text and vector space.
#
# MODEL CHOICE: text-embedding-3-small
# (From RAG-Architecture.md wiki, "Dense Embeddings"):
#   "Dense (embeddings) captures semantic understanding:
#    'maximum thrust at depth' even if phrased differently."
#   text-embedding-3-small: 1536 dimensions, $0.02/1M tokens, fast.
#   text-embedding-3-large: 3072 dimensions, 5x cost, marginal gain for code.
#   For code semantics (function bodies, class signatures), 1536 dims is sufficient.
#
# CHUNKING STRATEGY: chunk by FILE, not by token count
# (From RAG-Architecture.md wiki, "Token-Limit Chunking" Anti-Pattern):
#   "Splitting documents purely on character/token count destroys section
#    hierarchy, making it impossible to trace answers to their source location."
#   For code: a FILE is the natural structural unit. Splitting mid-function
#   destroys the semantic unit the agent needs to reason about.
#   If a file is very large (>8000 chars), we truncate rather than split —
#   the first 8000 chars of a file contain the most semantically dense content
#   (imports, class signatures, key functions at the top).
#   This is a deliberate trade-off: truncation vs. destroying structure.
#
# INPUT LENGTH GUARD: 8000 chars
# OpenAI's text-embedding-3-small supports 8191 tokens (~32k chars) input.
# We cap at 8000 chars (a conservative character-based approximation of ~2000 tokens)
# to ensure we stay well within the model's limit without needing a tokenizer.
# (Production-Hardening.md: "Validate input length before calling external APIs.")
#
# ASYNC CLIENT:
# AsyncOpenAI is used throughout. All calls are async coroutines.
# The embedder is stateless — no singleton, no connection pool.
# Each call creates its own API request. OpenAI handles connection pooling.
#
# GRACEFUL DEGRADATION:
# (From Production-Hardening.md wiki, "Circuit Breaker" + "Graceful Degradation"):
#   Every public method catches ALL exceptions and re-raises them as a simple
#   EmbeddingError. The caller (context_retriever.py) catches EmbeddingError
#   and returns "" — the pipeline continues without RAG context.
#   The embedder NEVER crashes the review pipeline.

import logging

from openai import AsyncOpenAI

from backend.config.settings import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum input length for embedding.
# (See module docstring for rationale: character-based approximation of token limit.)
MAX_EMBED_CHARS = 8_000

# Embedding vector dimensionality for text-embedding-3-small.
# Must match the Qdrant collection vector size (defined in qdrant_client.py).
EMBEDDING_DIMENSIONS = 1536


class EmbeddingError(Exception):
    """
    Raised when an embedding API call fails.

    Callers (specifically context_retriever.py) catch this and return ""
    (graceful degradation: no RAG context, review still runs).
    Never propagates to the user or crashes the review.
    (Production-Hardening.md: "Circuit Breaker — fail gracefully on external API failure.")
    """
    pass


# ---------------------------------------------------------------------------
# Core embedding functions
# ---------------------------------------------------------------------------

async def embed_text(text: str) -> list[float]:
    """
    Embeds a single text string into a 1536-dimensional vector.

    Designed for: embedding a PR diff summary or a code search query.
    One API call per call to this function.

    INPUT LENGTH GUARD:
    If text > MAX_EMBED_CHARS, truncates to MAX_EMBED_CHARS.
    We truncate from the end (preserve the beginning — imports and
    class signatures are at the top and are semantically richest).
    (RAG-Architecture.md: "structure-aware chunking preserves traceability.")

    Args:
        text: The text to embed. Will be truncated if over 8000 chars.

    Returns:
        List of 1536 floats (the embedding vector).

    Raises:
        EmbeddingError: on any OpenAI API failure. Caller handles gracefully.
    """
    if not text or not text.strip():
        # Empty text: return zero vector rather than calling the API.
        # Zero vector has cosine similarity 0 with everything — effectively
        # returns no relevant results in Qdrant. Safe fallback.
        logger.debug("embed_text | empty_text | returning zero vector")
        return [0.0] * EMBEDDING_DIMENSIONS

    # Truncate to MAX_EMBED_CHARS if needed
    original_len = len(text)
    if original_len > MAX_EMBED_CHARS:
        text = text[:MAX_EMBED_CHARS]
        logger.debug(
            "embed_text | truncated | original_chars=%d truncated_to=%d",
            original_len, MAX_EMBED_CHARS,
        )

    cfg = get_settings()
    client = AsyncOpenAI(api_key=cfg.openai_api_key)

    try:
        response = await client.embeddings.create(
            model=cfg.openai_embedding_model,
            input=text,
        )
        vector = response.data[0].embedding

        logger.debug(
            "embed_text | success | model=%s dims=%d input_chars=%d",
            cfg.openai_embedding_model, len(vector), len(text),
        )

        return vector

    except Exception as e:
        raise EmbeddingError(
            f"OpenAI embedding call failed: {type(e).__name__}: {e}"
        ) from e


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embeds a list of texts in a single API call (batch mode).

    Designed for: embedding multiple code chunks at index time.
    One API call for all texts (much more efficient than N separate calls).
    OpenAI's embedding API accepts up to ~2048 inputs per request.

    BATCH TRUNCATION:
    Each text in the batch is truncated independently to MAX_EMBED_CHARS.
    If any text is empty, it gets a zero vector (same as embed_text).

    Args:
        texts: List of strings to embed. Empty list returns [].

    Returns:
        List of 1536-dimensional vectors, one per input text.
        Order is preserved: result[i] corresponds to texts[i].

    Raises:
        EmbeddingError: on any OpenAI API failure.
    """
    if not texts:
        return []

    # Truncate each text individually (preserve structural unit per chunk)
    truncated_texts: list[str] = []
    empty_indices: set[int] = set()

    for i, text in enumerate(texts):
        if not text or not text.strip():
            truncated_texts.append("")   # placeholder
            empty_indices.add(i)
        elif len(text) > MAX_EMBED_CHARS:
            truncated_texts.append(text[:MAX_EMBED_CHARS])
        else:
            truncated_texts.append(text)

    # Separate non-empty texts for the API call
    non_empty = [(i, t) for i, t in enumerate(truncated_texts) if i not in empty_indices]

    if not non_empty:
        # All texts were empty
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    cfg = get_settings()
    client = AsyncOpenAI(api_key=cfg.openai_api_key)

    try:
        indices, batch_texts = zip(*non_empty)
        response = await client.embeddings.create(
            model=cfg.openai_embedding_model,
            input=list(batch_texts),
        )

        logger.debug(
            "embed_batch | success | model=%s batch_size=%d",
            cfg.openai_embedding_model, len(batch_texts),
        )

        # Reconstruct the full results list (empty indices get zero vectors)
        results: list[list[float]] = [[0.0] * EMBEDDING_DIMENSIONS] * len(texts)
        for position, embedding_obj in zip(indices, response.data):
            results[position] = embedding_obj.embedding

        return results

    except Exception as e:
        raise EmbeddingError(
            f"OpenAI batch embedding call failed: {type(e).__name__}: {e}"
        ) from e
