# backend/auth/dependencies.py
#
# FastAPI dependencies for authentication and authorization.
#
# PHASE 3 SCOPE (minimal placeholder with a real gate):
#   require_auth() — checks an API key in the X-API-Key header.
#   In development mode: passes unconditionally.
#   In production mode: rejects requests missing or using the wrong key.
#
# WHY NOT A STUB?
# A stub that always passes would mean production runs unprotected until
# Phase 11 lands. Instead, this is a minimal but real gate:
#   - Dev: open (no key needed, convenient for local testing)
#   - Prod: requires X-API-Key: <API_KEY from env>
# This protects the API surface from day one.
#
# PHASE 11 REPLACEMENT POINT (WIKI: Deferred-Decision-Pattern, clean-architecture):
#   "Placing an interface between business logic and infrastructure enables
#    deferring the choice of implementation without blocking progress."
#   The Dependency Injection approach (Depends(require_auth)) means Phase 11
#   only changes this one file. All routes auto-upgrade with no route changes.
#   Phase 11 will replace the API key check with JWT Bearer token validation
#   and add RBAC (role-based access control) using UserRole enum.
#
# HOW TO USE:
#   In a route handler:
#     @router.get("/reviews")
#     async def list_reviews(
#         _auth: None = Depends(require_auth),
#         ...
#     ):
#
#   In tests — override the dependency to bypass auth:
#     app.dependency_overrides[require_auth] = lambda: None
#
# DEPENDENCY DIRECTION CHECK:
#   auth/dependencies.py imports from: config.settings, fastapi
#   auth/dependencies.py does NOT import from: agents, orchestrator, database
#   Correct — auth is at the outer adapter layer.

import logging

from fastapi import Depends, HTTPException, Request, status

from backend.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def require_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """
    FastAPI dependency that enforces API key authentication.

    BEHAVIOUR:
      Development (app_env == "development"):
        Passes unconditionally. No key needed.
        Logs a debug message so it is visible that auth is bypassed.

      Production / staging (any other app_env):
        Reads the X-API-Key header from the request.
        Compares it against settings.api_key.
        Raises HTTP 401 if the header is missing.
        Raises HTTP 403 if the header is present but the key is wrong.
        Raises HTTP 500 if settings.api_key is empty (misconfiguration).

    WHY SEPARATE 401 vs 403?
    (WIKI: Security-Engineering)
      401 Unauthorized: no credentials provided — client should authenticate.
      403 Forbidden: credentials provided but insufficient — client is known but rejected.
    Distinguishing them makes debugging easier for API consumers.

    TIMING ATTACK CONSIDERATION:
    hmac.compare_digest() is used instead of == to prevent timing-based
    enumeration of the correct API key. An attacker measuring response time
    differences could otherwise determine the key character by character.
    This is low-stakes for an internal tool but good practice to establish.
    (WIKI: Security-Engineering, "constant-time comparison for secrets.")

    PHASE 11 REPLACEMENT:
    This function's signature does not change — it stays async, takes Request
    and Settings, returns None or raises HTTPException. Phase 11 will:
      1. Parse a Bearer JWT from the Authorization header
      2. Validate the JWT signature and expiry
      3. Extract the user's UserRole from the token claims
      4. Inject the role into downstream handlers via a different dependency
    The route handlers that currently depend on require_auth() will not need
    to change — Phase 11 replaces the body of this function.

    Args:
        request:  FastAPI Request object (provides header access)
        settings: Application settings (provides api_key and app_env)

    Raises:
        HTTPException 401: No API key provided (in non-dev environments)
        HTTPException 403: Wrong API key (in non-dev environments)
        HTTPException 500: API_KEY not configured in production

    Returns:
        None on success. FastAPI convention: auth dependencies return None
        and are declared as `_auth: None = Depends(require_auth)`.
    """
    import hmac

    # Development mode: bypass auth entirely.
    # This is intentional and logged so it cannot go unnoticed.
    if settings.is_development:
        logger.debug(
            "require_auth | BYPASSED (app_env=development) | path=%s",
            request.url.path,
        )
        return

    # Production/staging: enforce the API key check.

    # Guard: if api_key is empty, the operator forgot to set it.
    # Fail with 500 (server misconfiguration) not 401 (auth failure) —
    # the problem is the server, not the client.
    if not settings.api_key:
        logger.error(
            "require_auth | API_KEY is not configured | path=%s | "
            "Set the API_KEY environment variable.",
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API key not configured. Contact the system administrator.",
        )

    # Read the API key from the X-API-Key header.
    provided_key = request.headers.get("X-API-Key")

    if not provided_key:
        # No key provided — tell the client to authenticate.
        logger.warning(
            "require_auth | REJECTED (missing X-API-Key) | path=%s | client=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Compare keys using constant-time comparison to prevent timing attacks.
    # hmac.compare_digest() returns False immediately on length mismatch
    # but does NOT short-circuit on character comparison — both strings are
    # always fully traversed, so response time does not leak key length.
    # (WIKI: Security-Engineering — constant-time comparison for secrets.)
    keys_match = hmac.compare_digest(
        provided_key.encode("utf-8"),
        settings.api_key.encode("utf-8"),
    )

    if not keys_match:
        logger.warning(
            "require_auth | REJECTED (wrong API key) | path=%s | client=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    # Key is valid.
    logger.debug(
        "require_auth | OK | path=%s",
        request.url.path,
    )
