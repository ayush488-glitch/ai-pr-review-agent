# backend/integrations/__init__.py
#
# The integrations layer — adapters between the orchestrator and external APIs.
#
# WHAT THIS LAYER IS:
#   A set of thin async clients that translate between the external world
#   (GitHub REST API, in Phase 7) and the domain models the orchestrator
#   understands (PRMetadata, PRFile, etc.).
#
# WHAT THIS LAYER IS NOT:
#   - It does NOT contain business logic. No verdict decisions here.
#   - It does NOT know about LangGraph state, agents, or workflow engine.
#   - It does NOT import from backend/orchestrator/ or backend/agents/.
#
# WIKI: DDIA / Data-System-Architecture-Patterns.md
#   "The API hides implementation details."
#   -> The orchestrator calls github_client.get_pr_diff().
#      It does not know about HTTPX, pagination, diff MIME types,
#      or GitHub's rate limit headers. All of that lives here.
#
# WIKI: Clean Architecture / Dependency-Rule.md
#   "Source code dependencies must point inward."
#   -> Dependency direction: orchestrator -> integrations -> (external HTTP)
#   -> integrations never imports from orchestrator.
#
# DEPENDENCY DIRECTION (for the whole project):
#   core <- models <- tools/memory <- agents <- orchestrator
#                  <- integrations <- orchestrator
#
# That is: integrations sits at the same level as tools/memory.
# Both are infrastructure adapters. Both are called by orchestrator nodes.
