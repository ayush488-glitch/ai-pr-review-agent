# backend/config/__init__.py
#
# The config module exports the Settings class and the get_settings() function.
#
# DEPENDENCY RULE (ADR-002):
#   config may depend on: nothing inside backend/
#   config must NOT import from backend.core, backend.models, or any other module
#   (pydantic-settings is an external library, that is fine)
#
# HOW TO USE:
#
#   In FastAPI route handlers (preferred):
#     from fastapi import Depends
#     from backend.config import Settings, get_settings
#
#     @router.post("/something")
#     async def my_handler(cfg: Settings = Depends(get_settings)):
#         secret = cfg.github_webhook_secret
#
#   In background workers / non-FastAPI code:
#     from backend.config import get_settings
#     cfg = get_settings()   # call explicitly, not at module top-level

from backend.config.settings import Settings, get_settings

__all__ = [
    "Settings",
    "get_settings",
]
