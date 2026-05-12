# backend/data/__init__.py
#
# Data engineering sub-package.
#
# RESPONSIBILITY (from Batch-Processing-Patterns.md wiki):
#   "Immutable inputs, recomputable outputs."
#   This package owns the ingestion pipeline that converts GitHub repo
#   file content (immutable source of truth) into Qdrant vector embeddings
#   (derived data store, recomputable at any time by re-running ingestion).
#
# MODULES:
#   ingestion.py  — fetch repo tree, filter code files, embed, upsert to Qdrant
#   freshness.py  — track which files have been embedded and detect stale entries
#
# DEPENDENCY DIRECTION (from modular-architecture — core <- models <- tools):
#   data/ depends on: memory/, database/, config/
#   data/ does NOT depend on: agents/, orchestrator/, job_queue/
#   job_queue/ triggers ingestion, but imports from data/ — not the reverse.
