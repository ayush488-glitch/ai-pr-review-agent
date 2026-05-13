# backend/hitl/__init__.py
#
# Human-in-the-Loop (HITL) subsystem — Phase 19.
#
# SUBSYSTEM STRUCTURE:
#   queue.py      — enqueue/dequeue HITL items (Redis list + Postgres persist)
#   escalation.py — policy rules: when to escalate a review
#   dispute.py    — Use Case: human override logic (approve/reject/edit)
#   feedback.py   — Use Case: persist human decision as Phase 20 training signal
#
# LAYER CONTRACT (Clean-Architecture.md wiki — Business-Rules):
#   This package is a USE CASE layer.
#   It imports from: backend.database (models, postgres)
#   It does NOT import from: backend.api, FastAPI, Starlette, or HTTP types.
#   The hitl_router (backend/api/hitl_router.py) is the delivery mechanism —
#   it calls into this package. Dependency flows inward, never outward.
