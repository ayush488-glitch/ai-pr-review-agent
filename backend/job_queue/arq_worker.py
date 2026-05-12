# backend/job_queue/arq_worker.py
#
# ARQ async job queue — the bridge between webhook receipt and review execution.
#
# THE PROBLEM THIS SOLVES:
# GitHub sends a webhook and expects a response within 10 seconds.
# A full PR review (4 agents, LLM calls) takes 60-120 seconds.
# If we ran the review synchronously inside the webhook handler, GitHub would
# time out and retry, creating duplicate reviews.
#
# THE SOLUTION:
# Webhook handler -> enqueues a job in Redis (< 100ms) -> returns 200 to GitHub
# ARQ worker      -> picks up the job from Redis -> runs the full review
#
# HOW ARQ WORKS:
# ARQ uses two Redis operations:
#   PRODUCER side (webhook router): LPUSH arq:queue:default [job_payload_json]
#   CONSUMER side (this worker):    BRPOP arq:queue:default [blocks until job appears]
#
# BRPOP is "blocking pop" — the worker sleeps at Redis waiting for a job.
# When a job appears, Redis wakes the worker immediately (no polling loop).
# This is the "push-based notification" pattern from Stream-Processing-Patterns.md
# (as opposed to the anti-pattern of polling a database for new rows).
#
# WORKER FUNCTIONS:
# An ARQ worker function is a coroutine that ARQ calls when a job is dequeued.
# It receives a Context object (ARQ-provided) plus any arguments the producer sent.
# It returns any value (ARQ logs it) or raises an exception (ARQ retries).
#
# RETRY BEHAVIOR:
# By default, ARQ retries a failed job 5 times with exponential backoff.
# Our job is idempotent (idempotency key in Redis prevents duplicate reviews)
# so retries are safe.
#
# FROM Fault-Tolerance.md (distributed-systems wiki):
# "Combine checkpointing with message logging."
# ARQ + LangGraph checkpointing together mean:
#   - ARQ: if the worker crashes, the job is requeued (message logging)
#   - LangGraph: if the review was mid-graph, resume() continues from the checkpoint
# Together: exactly-once review semantics despite worker crashes.

import logging

# Configure logging for the ARQ worker process.
# main.py configures logging for the API process, but the worker is a separate
# process that never imports main.py — so we configure it here directly.
# Without this, all logger.info/error calls in this file are silently dropped.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

from typing import Any

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from backend.config import get_settings
from backend.core import (
    DuplicateWebhookError,
    MemoryStoreError,
    WorkflowNotFoundError,
)
from backend.memory.redis_client import redis_client
from backend.orchestrator.langgraph_engine import LangGraphEngine

logger = logging.getLogger(__name__)

# Module-level engine instance.
# LangGraphEngine is stateless (no per-review state) — safe to share.
_engine = LangGraphEngine()


# =============================================================================
# WORKER FUNCTION: run_pr_review
#
# This is the function ARQ calls when it dequeues a PR review job.
# ARQ passes `ctx` (ARQ context dict) as the first argument automatically.
# The remaining arguments match what enqueue_review_job() passes.
# =============================================================================

async def run_pr_review(
    ctx: dict[str, Any],
    workflow_id: str,
    input_data: dict[str, Any],
) -> dict[str, Any]:
    """
    ARQ worker function: runs a full PR review.

    This is called by ARQ (not by our code directly).
    ARQ provides `ctx` with metadata about the job (job_id, retry count, etc.).

    Args:
        ctx:         ARQ context (job_id, retry count, etc.) — provided by ARQ
        workflow_id: unique ID for this review (format: repo:pr:commit)
        input_data:  dict with PR details from the webhook event

    Returns:
        dict summarizing the result — ARQ logs this for observability.

    Raises:
        Exception: if the review fails in a way that should trigger ARQ retry.
        (DuplicateWebhookError is NOT re-raised — it is a non-error skip case.)
    """
    job_id = ctx.get("job_id", "unknown")
    retry_count = ctx.get("retry", 0)

    logger.info(
        "job_start | workflow_id=%s job_id=%s retry=%d",
        workflow_id,
        job_id,
        retry_count,
    )

    # -------------------------------------------------------------------------
    # Step 1: Idempotency check
    #
    # Before running the review, check if this exact review already ran.
    # This can happen if:
    #   - ARQ retried a job that already completed
    #   - GitHub replayed the webhook after the first delivery succeeded
    #   - The worker crashed after completing but before ACKing to ARQ
    #
    # Idempotency is enforced at enqueue time (enqueue_review_job checks the key
    # before pushing to ARQ). By the time this worker function runs, the key
    # already exists (it was set by enqueue_review_job). We do NOT re-check here
    # — that would cause every first-run to be skipped as a false duplicate.

    # -------------------------------------------------------------------------
    # Step 2: Update workflow status to IN_PROGRESS in Redis cache
    # -------------------------------------------------------------------------
    await redis_client.set_workflow_status(workflow_id, "in_progress")

    # -------------------------------------------------------------------------
    # Step 3: Run the workflow
    # -------------------------------------------------------------------------
    try:
        result = await _engine.run(workflow_id, input_data)
    except Exception as e:
        # Defensive catch: _engine.run() should never raise (it returns
        # WorkflowResult(status=FAILED) on error). But if LangGraph itself
        # or an unhandled node exception leaks up, we absorb it here so ARQ
        # does not mark the job as failed and retry (which would re-run
        # a review that already completed and saved partially).
        logger.error(
            "run_pr_review | unhandled_engine_error | workflow_id=%s error=%s",
            workflow_id, e, exc_info=True,
        )
        await redis_client.set_workflow_status(workflow_id, "failed")
        return {
            "workflow_id": workflow_id,
            "status": "failed",
            "verdict": None,
            "findings_count": 0,
            "error": str(e),
        }

    # -------------------------------------------------------------------------
    # Step 4: Update final status in Redis cache
    # -------------------------------------------------------------------------
    await redis_client.set_workflow_status(workflow_id, result.status.value)

    logger.info(
        "job_complete | workflow_id=%s status=%s verdict=%s findings=%d",
        workflow_id,
        result.status.value,
        result.verdict.value if result.verdict else "none",
        len(result.findings),
    )

    # -------------------------------------------------------------------------
    # Step 5: Trigger background repository ingestion (Phase 14)
    #
    # After a successful review, we kick off ingestion of the repo's codebase
    # into Qdrant. This runs as a separate ARQ job — non-blocking, best-effort.
    #
    # WHY HERE (not in the review pipeline):
    #   Ingestion is a background maintenance operation, not part of the review
    #   flow. If ingestion fails, the review result is unaffected.
    #   (Batch-Processing-Patterns.md: "composable stages, no shared state.")
    #
    # WHY ONLY ON SUCCESS:
    #   If the review failed (status != "completed"), the diff/context was likely
    #   unusable. No point indexing a repo we couldn't review anyway.
    #
    # GRACEFUL: if enqueueing ingestion fails (Redis hiccup), we log and continue.
    # The review result has already been saved — nothing is lost.
    # -------------------------------------------------------------------------
    if result.status.value == "completed":
        repo_full_name = input_data.get("repo_full_name", "")  # flat key set by webhook router
        if repo_full_name:
            try:
                # Build settings inline — WorkerSettings is defined later in this
                # file, so we can't reference it here without a forward reference.
                arq_redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
                await arq_redis.enqueue_job(
                    "ingest_repository_job",
                    repo_full_name,
                )
                await arq_redis.aclose()
                logger.info(
                    "ingestion_enqueued | repo=%s workflow_id=%s",
                    repo_full_name, workflow_id,
                )
            except Exception as e:
                # Non-fatal: review is already saved. Ingestion will happen
                # on the NEXT successful review for this repo.
                logger.warning(
                    "ingestion_enqueue_failed | repo=%s error=%s: %s",
                    repo_full_name, type(e).__name__, e,
                )

    # Return a summary dict — ARQ logs this. Phase 10 will send this to traces.
    return {
        "workflow_id": workflow_id,
        "status": result.status.value,
        "verdict": result.verdict.value if result.verdict else None,
        "findings_count": len(result.findings),
        "agents_completed": result.agents_completed,
        "agents_failed": result.agents_failed,
    }


# =============================================================================
# PRODUCER HELPER: enqueue_review_job
#
# Called by the webhook router to enqueue a job.
# The router does NOT import ARQ directly — it calls this function.
# (Law of Demeter: router talks to this module, not to ARQ directly.)
# =============================================================================

async def enqueue_review_job(
    workflow_id: str,
    input_data: dict[str, Any],
) -> None:
    """
    Enqueues a PR review job in the ARQ Redis queue.

    Called by backend/webhook_receiver/router.py after webhook validation.
    Returns immediately — the review runs asynchronously in a worker process.

    Steps:
      1. Set idempotency key in Redis (atomic, expires after 24h)
      2. Push job payload to ARQ queue in Redis
      3. Return — webhook router sends 200 to GitHub

    Raises:
        DuplicateWebhookError: if this exact review is already queued.
        MemoryStoreError: if Redis is unavailable.
    """
    cfg = get_settings()

    # -------------------------------------------------------------------------
    # Step 1: Check idempotency BEFORE setting the key.
    # If key exists, raise DuplicateWebhookError -> router returns 200 silently.
    # This is atomic enough: webhook handlers are sequential per PR (GitHub
    # delivers one webhook at a time per repo). True concurrent duplicates
    # are rare — and Redis SETEX is atomic anyway.
    # -------------------------------------------------------------------------
    already_exists = await redis_client.check_idempotency_key(workflow_id)
    if already_exists:
        logger.info(
            "enqueue_skip_duplicate | workflow_id=%s",
            workflow_id,
        )
        raise DuplicateWebhookError(
            f"Review already queued: {workflow_id}",
            idempotency_key=workflow_id,
        )

    # -------------------------------------------------------------------------
    # Step 2: Set the idempotency key BEFORE enqueueing.
    # If we enqueued first and then crashed before setting the key,
    # a replay would not detect the duplicate. Set key first.
    # -------------------------------------------------------------------------
    await redis_client.set_idempotency_key(workflow_id)

    # -------------------------------------------------------------------------
    # Step 3: Connect to ARQ pool and enqueue the job.
    #
    # WHY create_pool() each time instead of a module-level pool?
    # ARQ pools are lightweight (they reuse the same underlying Redis connection).
    # create_pool() is idempotent and fast. Keeping a module-level pool would
    # require its own lifecycle management (startup/shutdown). Not worth it.
    # -------------------------------------------------------------------------
    redis_settings = RedisSettings.from_dsn(cfg.redis_url)
    arq_pool: ArqRedis = await create_pool(redis_settings)

    try:
        job = await arq_pool.enqueue_job(
            "run_pr_review",           # the worker function name (as string)
            workflow_id,               # positional arg 1
            input_data,                # positional arg 2
            _job_id=workflow_id,       # ARQ job ID = workflow_id (for deduplication within ARQ)
        )
        logger.info(
            "job_enqueued | workflow_id=%s arq_job_id=%s",
            workflow_id,
            job.job_id if job else "unknown",
        )
    finally:
        await arq_pool.aclose()


# =============================================================================
# WORKER FUNCTION: ingest_repository_job  (Phase 14)
#
# Background ingestion job — indexes a repo's source files into Qdrant.
# Enqueued by run_pr_review after a successful review completes.
#
# ARQ contract: first arg is ctx (ARQ dict), rest are the job payload args.
# =============================================================================

async def ingest_repository_job(ctx: dict, repo_full_name: str) -> dict:
    """
    ARQ job: index all source files from a GitHub repository into Qdrant.

    Called automatically after every successful PR review.
    Runs in the background — review result is already saved before this starts.

    Returns a summary dict that ARQ logs:
      repo, total_files, stale_files, embedded, skipped_fresh, errors
    """
    # Import here (not at module top) to avoid circular imports at worker startup.
    # data/ imports database/ and memory/ — both are safe at this point since
    # on_startup has already run. But the import is deferred to keep the
    # worker startup path clean and testable.
    from backend.data.ingestion import ingest_repository

    logger.info("ingest_repository_job | start | repo=%s", repo_full_name)

    summary = await ingest_repository(repo_full_name)

    logger.info(
        "ingest_repository_job | complete | repo=%s "
        "total=%d stale=%d embedded=%d skipped=%d errors=%d",
        repo_full_name,
        summary.get("total_files", 0),
        summary.get("stale_files", 0),
        summary.get("embedded", 0),
        summary.get("skipped_fresh", 0),
        summary.get("errors", 0),
    )

    return {"repo": repo_full_name, **summary}


# =============================================================================
# ARQ WORKER SETTINGS
#
# ARQ uses this class to configure the worker process.
# When we run `arq backend.job_queue.arq_worker.WorkerSettings`
# ARQ reads this class to know: which functions to run, Redis URL, concurrency.
# =============================================================================

class WorkerSettings:
    """
    ARQ worker process configuration.

    Start the worker with:
      arq backend.job_queue.arq_worker.WorkerSettings

    ARQ lifecycle hooks (on_startup / on_shutdown) replace FastAPI's lifespan
    for the worker process. Redis must be connected here — the worker does not
    run FastAPI's lifespan handler.
    """

    functions = [run_pr_review, ingest_repository_job]

    redis_settings: RedisSettings = RedisSettings.from_dsn(get_settings().redis_url)

    max_jobs = 10
    job_timeout = 300
    max_tries = 3

    @staticmethod
    async def on_startup(ctx: dict) -> None:
        """Connect the shared redis_client used by run_pr_review."""
        await redis_client.connect()
        logger.info("ARQ worker redis_client connected.")

    @staticmethod
    async def on_shutdown(ctx: dict) -> None:
        """Gracefully disconnect redis_client on worker shutdown."""
        await redis_client.disconnect()
        logger.info("ARQ worker redis_client disconnected.")
