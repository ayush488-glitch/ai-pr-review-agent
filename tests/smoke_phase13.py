"""
tests/smoke_phase13.py

Phase 13: Infrastructure & Deployment — smoke tests
=====================================================
No Docker required. Tests verify:
  A — /health and /health/live endpoint structure (mocked deps)
  B — Settings load all required fields without error
  C — fixtures/sample_pr_opened.json is valid and has correct schema
"""

import sys
import os
import json

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ---------------------------------------------------------------------------
# Set required env vars BEFORE any backend import.
#
# backend/main.py has a module-level `settings = get_settings()` call that
# runs at import time and validates all required fields. Without these env
# vars set here first, pydantic-settings raises ValidationError and the
# entire test module fails to load.
#
# We use safe placeholder values — no real services are contacted.
# All service-check functions (_check_postgres etc.) are mocked per-test.
# ---------------------------------------------------------------------------
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("GITHUB_TOKEN", "ghp_test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")

# Clear lru_cache so Settings picks up the env vars we just set
# (cache may already be populated if another test file ran first).
from backend.config.settings import get_settings
get_settings.cache_clear()

# Import backend.main NOW (env vars are set, cache cleared above).
# All subsequent patch() calls patch the already-loaded module's attributes,
# which is the correct pattern — no re-import needed per test.
import backend.main as _main_module  # noqa: E402 (intentional late import)

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


# ===========================================================================
# GROUP A — /health endpoints
# ===========================================================================

class TestHealthEndpoints:
    """
    Test the /health and /health/live endpoints without real infrastructure.
    We mock the three _check_* coroutines so no Postgres/Redis/Qdrant needed.
    """

    def _all_mocks(self, *, pg="ok", redis="ok", qdrant="ok", breakers=None):
        """
        Return a list of patch() context managers covering every external call
        made during lifespan startup and the /health endpoint.

        WHY patch.object for lifespan targets:
          The lifespan calls `redis_client.connect()` where `redis_client` is
          the singleton instance imported into backend.main as:
            from backend.memory.redis_client import redis_client
          Patching the dotted string "backend.memory.redis_client.redis_client.connect"
          does NOT work — patch() cannot traverse into instance attributes via dotted
          strings. We must patch the instance attribute directly via patch.object.
          Same rule applies to init_db and ensure_collection which are also imported
          names in backend.main's namespace.
        """
        from unittest.mock import patch as _patch, AsyncMock as _AM
        breakers = breakers or []
        return [
            # Lifespan: redis_client is an instance; patch its methods directly.
            _patch.object(_main_module.redis_client, "connect",    new=_AM()),
            _patch.object(_main_module.redis_client, "disconnect", new=_AM()),
            # Lifespan: init_db and ensure_collection are imported names in main.
            _patch.object(_main_module, "init_db",          new=_AM()),
            _patch.object(_main_module, "ensure_collection", new=_AM(return_value=True)),
            # /health endpoint: the three check coroutines and circuit breaker list.
            _patch.object(_main_module, "_check_postgres",        new=_AM(return_value=pg)),
            _patch.object(_main_module, "_check_redis",           new=_AM(return_value=redis)),
            _patch.object(_main_module, "_check_qdrant",          new=_AM(return_value=qdrant)),
            _patch.object(_main_module, "list_breaker_summaries", return_value=breakers),
        ]

    @pytest.fixture
    def client(self):
        """
        TestClient with all infra mocked. Lifespan runs but hits mocks,
        so no real Redis/Postgres/Qdrant needed.
        """
        from contextlib import ExitStack
        from fastapi.testclient import TestClient
        with ExitStack() as stack:
            for p in self._all_mocks():
                stack.enter_context(p)
            with TestClient(_main_module.app, raise_server_exceptions=True) as c:
                yield c

    def test_liveness_returns_200(self, client):
        """GET /health/live must always return 200 while the process is up."""
        resp = client.get("/health/live")
        assert resp.status_code == 200

    def test_liveness_has_status_ok(self, client):
        """Liveness body must contain status=ok and a version field."""
        resp = client.get("/health/live")
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body

    def test_readiness_200_when_all_ok(self, client):
        """
        When all three service checks return 'ok', /health must return 200.
        """
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_readiness_body_structure(self, client):
        """
        /health response must contain status, version, env, services,
        and circuit_breakers keys.
        """
        resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert "version" in body
        assert "env" in body
        assert "services" in body
        assert "circuit_breakers" in body

    def test_readiness_services_all_ok(self, client):
        """All three services must be reported as 'ok' in the healthy case."""
        resp = client.get("/health")
        services = resp.json()["services"]
        assert services["postgres"] == "ok"
        assert services["redis"] == "ok"
        assert services["qdrant"] == "ok"

    def test_readiness_503_when_postgres_down(self):
        """
        When Postgres check fails, /health must return 503 and status='degraded'.
        Postgres is a hard dependency — its failure degrades the service.
        """
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._all_mocks(pg="error: connection refused"):
                stack.enter_context(p)
            with TestClient(_main_module.app, raise_server_exceptions=False) as c:
                resp = c.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"

    def test_readiness_503_when_redis_down(self):
        """
        Redis is a hard dependency (job queue + idempotency).
        Its failure must yield 503 + status='degraded'.
        """
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._all_mocks(redis="error: timeout"):
                stack.enter_context(p)
            with TestClient(_main_module.app, raise_server_exceptions=False) as c:
                resp = c.get("/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"

    def test_readiness_200_when_only_qdrant_down(self):
        """
        Qdrant is a soft dependency — RAG is disabled but reviews still run.
        Qdrant failure must NOT degrade the overall status to 503.
        """
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._all_mocks(qdrant="degraded (collection missing — RAG disabled)"):
                stack.enter_context(p)
            with TestClient(_main_module.app, raise_server_exceptions=False) as c:
                resp = c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert "degraded" in resp.json()["services"]["qdrant"]

    def test_circuit_breakers_in_health_response(self):
        """
        Circuit breaker summaries from Phase 12 must appear in /health.
        """
        fake_breakers = [
            {"name": "security_agent", "state": "CLOSED", "failure_count": 0},
            {"name": "quality_agent",  "state": "CLOSED", "failure_count": 0},
        ]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._all_mocks(breakers=fake_breakers):
                stack.enter_context(p)
            with TestClient(_main_module.app, raise_server_exceptions=False) as c:
                resp = c.get("/health")
        assert resp.json()["circuit_breakers"] == fake_breakers


# ===========================================================================
# GROUP B — Settings
# ===========================================================================

class TestSettings:
    """
    Verify Settings loads cleanly with all required fields.
    We don't need a real .env file — we pass values directly.
    """

    def _make_settings(self, **overrides):
        """Helper: build a Settings with valid required fields + any overrides."""
        from backend.config.settings import Settings
        defaults = dict(
            github_webhook_secret="test-secret",
            github_token="ghp_test",
            openai_api_key="sk-test",
            anthropic_api_key="sk-ant-test",
        )
        defaults.update(overrides)
        return Settings(**defaults)

    def test_settings_load_without_env_file(self):
        """Settings with all required fields must load without raising."""
        s = self._make_settings()
        assert s.github_webhook_secret == "test-secret"

    def test_default_database_url(self):
        """Default DATABASE_URL must point to localhost Postgres."""
        s = self._make_settings()
        assert "localhost" in s.database_url or "postgres" in s.database_url

    def test_default_redis_url(self):
        """Default REDIS_URL must be a redis:// URL."""
        s = self._make_settings()
        assert s.redis_url.startswith("redis://")

    def test_default_qdrant_url(self):
        """Default QDRANT_URL must be an HTTP URL on port 6333."""
        s = self._make_settings()
        assert "6333" in s.qdrant_url

    def test_is_development_true_by_default(self):
        """Default app_env is 'development' so is_development must be True."""
        s = self._make_settings()
        assert s.is_development is True

    def test_is_production_false_by_default(self):
        """is_production must be False in development mode."""
        s = self._make_settings()
        assert s.is_production is False

    def test_confidence_threshold_within_range(self):
        """confidence_threshold must be between 0.0 and 1.0."""
        s = self._make_settings()
        assert 0.0 <= s.confidence_threshold <= 1.0

    def test_max_concurrent_reviews_positive(self):
        """max_concurrent_reviews must be a positive integer."""
        s = self._make_settings()
        assert s.max_concurrent_reviews > 0


# ===========================================================================
# GROUP C — Fixture file
# ===========================================================================

FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fixtures",
    "sample_pr_opened.json",
)

class TestFixture:
    """
    Validate the hand-authored GitHub webhook fixture.
    The demo depends on this file being correct — a malformed fixture
    breaks scripts/demo.sh silently.
    """

    @pytest.fixture(scope="class")
    def payload(self):
        with open(FIXTURE_PATH) as f:
            return json.load(f)

    def test_fixture_is_valid_json(self):
        """The fixture must be parseable as JSON."""
        with open(FIXTURE_PATH) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_fixture_action_is_opened(self, payload):
        """action must be 'opened' — that's the event type we demo."""
        assert payload["action"] == "opened"

    def test_fixture_has_pull_request(self, payload):
        """Top-level 'pull_request' key must be present."""
        assert "pull_request" in payload

    def test_fixture_has_repository(self, payload):
        """Top-level 'repository' key must be present."""
        assert "repository" in payload

    def test_fixture_pr_has_head_sha(self, payload):
        """PR must have a head.sha — used as the review identifier."""
        assert payload["pull_request"]["head"]["sha"]

    def test_fixture_pr_has_diff(self, payload):
        """PR must include a diff string — this is what agents analyse."""
        diff = payload["pull_request"].get("diff", "")
        assert len(diff) > 100  # at least something substantive

    def test_fixture_diff_contains_secret(self, payload):
        """
        The fixture diff must contain the intentional secret pattern
        (hardcoded Stripe key) so the security agent has something to flag.
        This validates the fixture hasn't been accidentally sanitised.
        """
        diff = payload["pull_request"]["diff"]
        assert "sk_live_" in diff or "STRIPE_SECRET_KEY" in diff

    def test_fixture_diff_contains_weak_crypto(self, payload):
        """
        The diff must mention MD5 — flagged by security agent as weak hash.
        """
        diff = payload["pull_request"]["diff"]
        assert "md5" in diff.lower() or "MD5" in diff
