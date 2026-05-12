# backend/webhook_receiver/__init__.py
#
# The webhook_receiver module owns one responsibility:
#   Receive raw HTTP requests from GitHub and turn them into queued jobs.
#
# DEPENDENCY RULE (ADR-002):
#   webhook_receiver may depend on: core, models, config
#   webhook_receiver must NOT depend on: agents, orchestrator, memory, tools
#   (it only enqueues — it does not run the review)
#
# WHAT THIS MODULE EXPORTS:
#   - router: the FastAPI APIRouter to mount in main.py

from backend.webhook_receiver.router import router

__all__ = ["router"]
