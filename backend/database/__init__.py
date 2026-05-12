# backend/database/__init__.py
#
# Makes backend/database/ a Python package.
#
# LAYER ROLE (from Polyglot-Persistence.md wiki):
#   This package is the STRUCTURED PERSISTENCE layer.
#   Postgres handles: relational review history, findings, audit queries.
#   Rule: "Use the right store for each access pattern."
#   Postgres excels at: "give me all HIGH findings for repo X in the last 30 days"
#   That query is a structured JOIN+FILTER — it belongs in a relational DB,
#   not in Redis (ephemeral) or Qdrant (vector similarity only).
#
# DEPENDENCY DIRECTION (from modular-architecture):
#   core <- models <- tools/memory <- agents <- orchestrator
#   database/ sits at the tools/memory layer.
#   It imports from backend.models (enums) but nothing above it imports from here
#   except orchestrator and main.py.
