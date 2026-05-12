# backend/job_queue/__init__.py
#
# Job queue module: ARQ-based async job queue backed by Redis.
#
# ALLOWED DEPENDENCIES:
#   - backend.core, backend.config, backend.models
#   - backend.memory (Redis client)
#   - backend.orchestrator (runs the workflow engine)
#
# FORBIDDEN:
#   - backend.webhook_receiver (callers, not callees)
#
# EXPORTS:
from backend.job_queue.arq_worker import WorkerSettings, enqueue_review_job

__all__ = ["WorkerSettings", "enqueue_review_job"]
