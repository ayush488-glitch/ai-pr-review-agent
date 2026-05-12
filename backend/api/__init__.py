# backend/api/__init__.py
#
# The HTTP adapter layer — FastAPI routers that translate HTTP requests
# into repository/service calls and HTTP responses.
#
# WHAT BELONGS HERE (Interface Adapters Layer, clean-architecture wiki):
#   - FastAPI router files (reviews.py, queue.py, etc.)
#   - Pydantic response DTO schemas (schemas.py)
#   - The auth dependency (auth/dependencies.py — enforced at this layer)
#
# WHAT DOES NOT BELONG HERE:
#   - Business logic (no if/else decisions about review verdicts)
#   - SQL queries (all SQL lives in database/repository.py)
#   - LLM calls (agents, orchestrator)
#
# DEPENDENCY DIRECTION (from clean-architecture wiki, Interface Adapters Layer):
#   api/ imports from: database.repository, database.postgres, config.settings
#   api/ does NOT import from: agents, orchestrator, tools, memory
#
# The webhook_receiver/ package is a separate adapter (inbound events).
# This api/ package is the query/read adapter (dashboard, HITL UI, integrations).
#
# As phases complete, new routers are added here:
#   reviews.py  — GET /api/v1/reviews, GET /api/v1/reviews/{id}     (Phase 3)
#   queue.py    — GET /api/v1/queue                                  (Phase 3)
#   hitl.py     — POST /api/v1/queue/{id}/approve, /reject           (Phase 19)
#   auth.py     — POST /api/v1/auth/login, GET /api/v1/auth/me       (Phase 11)
