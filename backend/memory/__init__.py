# backend/memory/__init__.py
#
# The memory module manages all storage layers:
#   - Redis (short-term: idempotency keys, workflow checkpoints, job queue)
#   - Qdrant (vector store: codebase embeddings for RAG)
#   - Postgres (long-term: review history, findings, audit log)
#
# DEPENDENCY RULE (ADR-002):
#   memory may depend on: core, models, config
#   memory must NOT depend on: agents, orchestrator, tools
#
# Populated in Phase 6.
