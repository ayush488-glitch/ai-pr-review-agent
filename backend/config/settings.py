# backend/config/settings.py
#
# Single source of truth for all application configuration.
#
# HOW THIS WORKS:
# pydantic-settings reads values from two places, in this order:
#   1. Actual environment variables (what production uses)
#   2. A .env file (what local development uses)
# Fields defined below with no default are REQUIRED.
# The app will refuse to start if they are missing.
#
# FIX (Orthogonality / GlobalDataCoupling):
#   Previously this file had a module-level singleton:
#     settings = get_settings()   <- at the bottom of the file
#   That global caused every file that imported it to drag in ALL env var
#   requirements at import time, making unit tests impossible without
#   a full .env file. The wiki says:
#     "Explicitly pass any required context into your modules."
#   Now: use get_settings() as a FastAPI Depends() parameter where possible.
#   For non-FastAPI code (job queue, background workers) call get_settings()
#   explicitly at the call site — not at module import time.

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All application configuration in one place.

    Field names are lowercase in Python but map to UPPERCASE env vars.
    e.g. settings.redis_url reads from the REDIS_URL environment variable.

    Fields with no default are REQUIRED. The app will not start without them.
    Fields with a default are OPTIONAL (have a reasonable fallback).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Don't crash if .env file doesn't exist (fine in production / CI)
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # GitHub
    # -------------------------------------------------------------------------

    # Secret used to verify incoming webhook signatures.
    # Set this to the same value you put in the GitHub webhook settings page.
    # REQUIRED — no default because there is no safe fallback.
    github_webhook_secret: str

    # Token used to call the GitHub REST API.
    # Needs scopes: pull_requests (read + write), contents (read)
    # REQUIRED — agent cannot post review comments without this.
    github_token: str

    # Base URL for the GitHub REST API.
    # Default: https://api.github.com (GitHub.com)
    # Override for GitHub Enterprise: https://github.mycompany.com/api/v3
    #
    # WHY CONFIGURABLE?
    # WIKI: Operations-Patterns.md
    #   "Don't call it hostname just because it is a hostname.
    #    Name the property authenticationServer."
    # -> We name this github_api_base_url, not just github_host, to make
    #    the purpose immediately clear to operators.
    #
    # SECONDARY BENEFIT (testability):
    #   Smoke tests set this to the httpx MockTransport URL.
    #   No live network calls in CI. No GitHub token needed in tests.
    github_api_base_url: str = "https://api.github.com"

    # GitHub caps review body text at 65536 UTF-8 characters.
    # A POST /pulls/{n}/reviews with body > 65536 returns 422 Unprocessable.
    # WIKI: Stability-Antipatterns.md
    #   "Optimism bias: assuming edge cases won't occur in production."
    #   -> A large PR with 20+ findings CAN hit this limit. We truncate
    #      proactively with a visible "[truncated]" notice rather than crashing.
    review_body_max_characters: int = 65536

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------

    # Redis connection URL. Format: redis://host:port/db_number
    # Used for: job queue, idempotency keys, workflow checkpoints
    redis_url: str = "redis://localhost:6379/0"

    # -------------------------------------------------------------------------
    # Postgres
    # -------------------------------------------------------------------------

    # Postgres connection URL.
    # Format: postgresql+asyncpg://user:password@host:port/dbname
    # Used for: persistent review history, findings, audit log
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/pr_review_agent"

    # -------------------------------------------------------------------------
    # Qdrant
    # -------------------------------------------------------------------------

    # Qdrant vector store URL.
    # Used for: codebase RAG (repo files embedded and indexed here)
    qdrant_url: str = "http://localhost:6333"

    # Name of the Qdrant collection that stores codebase embeddings.
    qdrant_collection_name: str = "codebase_embeddings"

    # Qdrant API key. Required for Qdrant Cloud; leave empty for local Qdrant.
    # Default empty string = local Qdrant (no auth required).
    # (Production-Hardening.md: "Optional deps have sane defaults for local dev.")
    qdrant_api_key: str = ""

    # OpenAI embedding model.
    # text-embedding-3-small: 1536 dims, fast, cost-effective for code semantics.
    # Must match EMBEDDING_DIMENSIONS in backend/memory/embedder.py (1536).
    # (RAG-Architecture.md: "Dense embeddings capture semantic understanding.")
    openai_embedding_model: str = "text-embedding-3-small"

    # -------------------------------------------------------------------------
    # LLM Providers
    # -------------------------------------------------------------------------

    # OpenAI API key — used for quality, test, docs agents (cheaper models)
    # REQUIRED
    openai_api_key: str

    # Anthropic API key — used for security agent (stronger reasoning)
    # REQUIRED
    anthropic_api_key: str

    # -------------------------------------------------------------------------
    # Application Settings
    # -------------------------------------------------------------------------

    # Environment name. Controls logging verbosity and error detail level.
    # Allowed values: "development", "staging", "production"
    app_env: str = "development"

    # Log level for the application logger
    log_level: str = "INFO"

    # How many PR reviews can run in parallel at once.
    # Each review spawns 4 agents. Keep low in dev to avoid API rate limits.
    max_concurrent_reviews: int = 3

    # Confidence threshold for auto-posting findings.
    # Findings below this score go to the HITL approval queue.
    # Value between 0.0 and 1.0.
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # Maximum time (in seconds) a single PR review is allowed to run.
    # After this, WorkflowTimeoutError is raised.
    workflow_timeout_seconds: int = 300  # 5 minutes

    # -------------------------------------------------------------------------
    # API Authentication
    # -------------------------------------------------------------------------

    # API key for the REST API layer (GET /api/v1/reviews, /queue, etc.).
    # Used by backend/auth/dependencies.py — require_auth() checks this.
    #
    # In development (app_env="development"): not checked (auth is bypassed).
    # In production: callers must send X-API-Key: <this value> on every request.
    #
    # PHASE 11 REPLACEMENT:
    # When Phase 11 adds JWT authentication, this field becomes the fallback
    # for service-to-service calls (e.g. the ARQ worker polling its own API).
    # Human users will use JWT Bearer tokens instead.
    #
    # DEFAULT: empty string.
    # An empty api_key in production causes require_auth() to return HTTP 500
    # (misconfiguration), not silently pass. Operators are forced to set this.
    api_key: str = Field(default="", description="API key for the REST API. Required in production.")

    # -------------------------------------------------------------------------
    # Derived properties
    # Not read from env vars — computed from other settings.
    # -------------------------------------------------------------------------

    @property
    def is_development(self) -> bool:
        """True when running locally. Enables Swagger UI, extra debug logging."""
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        """True in production. Disables debug features, enables strict error handling."""
        return self.app_env == "production"


@lru_cache()
def get_settings() -> Settings:
    """
    Returns the Settings singleton.

    lru_cache() means this function body runs exactly once.
    Every subsequent call returns the cached Settings object.
    This means we read from environment variables once at startup,
    not on every request.

    TWO WAYS TO USE THIS:

    1. As a FastAPI dependency (preferred for route handlers):

        from fastapi import Depends
        from backend.config.settings import get_settings, Settings

        @router.post("/webhook/github")
        async def my_endpoint(settings: Settings = Depends(get_settings)):
            secret = settings.github_webhook_secret

       This way tests can override settings without touching env vars:
        app.dependency_overrides[get_settings] = lambda: Settings(
            github_webhook_secret="test-secret",
            github_token="test-token",
            openai_api_key="test-key",
            anthropic_api_key="test-key",
        )

    2. For non-FastAPI code (background workers, job queue, CLI):

        from backend.config.settings import get_settings
        settings = get_settings()   # call explicitly, not at module import time

       This is fine because lru_cache ensures it only reads env vars once.
    """
    return Settings()
