# backend/orchestrator/__init__.py
#
# Orchestrator module: LangGraph workflow engine, state, graph, and nodes.
#
# ALLOWED DEPENDENCIES (ADR-002 dependency rule):
#   - backend.core (abstract interfaces and exceptions)
#   - backend.models (Pydantic models)
#   - backend.config (settings via Depends())
#   - backend.memory (Redis client — for checkpointing)
#
# FORBIDDEN:
#   - backend.agents (agents are called BY the orchestrator, not the reverse)
#   - backend.webhook_receiver (callers, not callees)
#
# EXPORTS:
from backend.orchestrator.langgraph_engine import LangGraphEngine
from backend.orchestrator.state import PRReviewState
from backend.orchestrator.graph import review_graph

__all__ = ["LangGraphEngine", "PRReviewState", "review_graph"]
