# tests/smoke_phase5b.py
#
# Smoke tests for the Phase 5 prompt registry.
#
# WHAT WE'RE TESTING:
#   1. All four agent prompts load correctly from disk (v1.txt files)
#   2. "latest" version resolution works (highest integer N)
#   3. Missing version raises PromptNotFoundError (not silent fallback)
#   4. Unknown agent type raises PromptNotFoundError
#   5. Cache works: second call returns same object (no second disk read)
#   6. BaseAgent._get_prompt_with_fallback() uses registry as primary source
#   7. Fallback to _system_prompt() works when registry would fail
#   8. list_versions() returns sorted version list
#   9. Prompt content matches what the template file contains
#
# STRATEGY:
# All tests run with the real filesystem — the template .txt files must be
# deployed alongside the code. No mocking of the registry itself.
# We DO test the fallback path by temporarily clearing the registry cache
# and pointing it at a nonexistent directory.
#
# NO external services needed (no LLM calls, no Redis, no Postgres, no Qdrant).

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.exceptions import PromptNotFoundError
from backend.models.enums import AgentType
from backend.prompts.registry import PromptRegistry, registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_registry() -> PromptRegistry:
    """Returns a fresh PromptRegistry instance with an empty cache for isolation."""
    r = PromptRegistry()
    r._clear_cache()
    return r


# ---------------------------------------------------------------------------
# Test 1-4: Basic load per agent type
# ---------------------------------------------------------------------------

def test_load_security_v1():
    """security/v1.txt must load and contain the OWASP header."""
    r = fresh_registry()
    content = r.load_prompt(AgentType.SECURITY, "v1")
    assert len(content) > 100, "Security prompt should be substantial"
    assert "OWASP" in content or "injection" in content.lower(), (
        "Security prompt should mention injection or OWASP"
    )

def test_load_quality_v1():
    """quality/v1.txt must load and contain SOLID reference."""
    r = fresh_registry()
    content = r.load_prompt(AgentType.QUALITY, "v1")
    assert len(content) > 100
    assert "SOLID" in content, "Quality prompt should mention SOLID"

def test_load_test_v1():
    """test/v1.txt must load and contain assertion reference."""
    r = fresh_registry()
    content = r.load_prompt(AgentType.TEST, "v1")
    assert len(content) > 100
    assert "assert" in content.lower(), "Test prompt should mention assertions"

def test_load_docs_v1():
    """docs/v1.txt must load and contain docstring reference."""
    r = fresh_registry()
    content = r.load_prompt(AgentType.DOCS, "v1")
    assert len(content) > 100
    assert "docstring" in content.lower(), "Docs prompt should mention docstrings"


# ---------------------------------------------------------------------------
# Test 5: "latest" resolves to v1 (only one version exists right now)
# ---------------------------------------------------------------------------

def test_latest_resolves_to_v1_for_security():
    """
    With only v1.txt in templates/security/, 'latest' must resolve to 'v1'.
    This tests the integer-sort resolution logic in _resolve_latest().
    """
    r = fresh_registry()
    latest_content = r.load_prompt(AgentType.SECURITY, "latest")
    explicit_content = r.load_prompt(AgentType.SECURITY, "v1")
    # Both calls should return identical content.
    assert latest_content == explicit_content, (
        "'latest' should resolve to v1 when v1 is the only version"
    )


# ---------------------------------------------------------------------------
# Test 6: Missing version raises PromptNotFoundError
# ---------------------------------------------------------------------------

def test_missing_version_raises():
    """
    Requesting a version that doesn't exist on disk must raise PromptNotFoundError.
    It must NOT return an empty string or a default prompt.
    """
    r = fresh_registry()
    with pytest.raises(PromptNotFoundError) as exc_info:
        r.load_prompt(AgentType.SECURITY, "v999")
    err = exc_info.value
    assert err.agent_type == "security"
    assert err.version == "v999"


# ---------------------------------------------------------------------------
# Test 7: Unknown/missing agent directory raises PromptNotFoundError
# ---------------------------------------------------------------------------

def test_missing_agent_directory_raises():
    """
    If the templates directory for an agent doesn't exist, raise PromptNotFoundError.
    We test this by mocking _TEMPLATES_ROOT to a temp nonexistent path.
    """
    import backend.prompts.registry as reg_module

    r = fresh_registry()
    # Temporarily point the templates root at a nonexistent directory.
    fake_root = Path("/tmp/nonexistent_templates_dir_xyz")
    with patch.object(reg_module, "_TEMPLATES_ROOT", fake_root):
        with pytest.raises(PromptNotFoundError):
            r.load_prompt(AgentType.SECURITY, "latest")


# ---------------------------------------------------------------------------
# Test 8: Cache returns same object on second call (no re-read from disk)
# ---------------------------------------------------------------------------

def test_cache_hit_on_second_call(caplog):
    """
    Second call with the same (agent, version) must hit the cache.
    We verify by checking that 'prompt_loaded' is logged only once
    (cache hits log 'prompt_cache_hit', not 'prompt_loaded').
    """
    r = fresh_registry()
    with caplog.at_level(logging.DEBUG, logger="backend.prompts.registry"):
        content1 = r.load_prompt(AgentType.QUALITY, "v1")
        content2 = r.load_prompt(AgentType.QUALITY, "v1")

    assert content1 == content2
    # 'prompt_loaded' appears exactly once — the second call hits cache
    loaded_msgs = [r for r in caplog.records if "prompt_loaded" in r.message]
    cache_msgs  = [r for r in caplog.records if "prompt_cache_hit" in r.message]
    assert len(loaded_msgs) == 1, "Should load from disk exactly once"
    assert len(cache_msgs)  == 1, "Second call should be a cache hit"


# ---------------------------------------------------------------------------
# Test 9: list_versions() returns sorted list
# ---------------------------------------------------------------------------

def test_list_versions_returns_sorted():
    """
    list_versions() must return all discovered versions in ascending order.
    Right now each agent has exactly one version: ["v1"].
    """
    r = fresh_registry()
    for agent_type in [AgentType.SECURITY, AgentType.QUALITY, AgentType.TEST, AgentType.DOCS]:
        versions = r.list_versions(agent_type)
        assert versions == ["v1"], (
            f"{agent_type.value} should have exactly ['v1'], got {versions}"
        )


# ---------------------------------------------------------------------------
# Test 10: Prompt content matches template file content exactly
# ---------------------------------------------------------------------------

def test_prompt_content_matches_template_file():
    """
    The content returned by the registry must exactly match what's in the
    template file (modulo leading/trailing whitespace which .strip() removes).
    """
    import backend.prompts.registry as reg_module

    r = fresh_registry()
    template_path = reg_module._TEMPLATES_ROOT / "security" / "v1.txt"
    expected = template_path.read_text(encoding="utf-8").strip()
    actual = r.load_prompt(AgentType.SECURITY, "v1")
    assert actual == expected, "Registry content must match template file verbatim"


# ---------------------------------------------------------------------------
# Test 11: BaseAgent._get_prompt_with_fallback() uses registry as primary
# ---------------------------------------------------------------------------

def test_base_agent_uses_registry_as_primary():
    """
    _get_prompt_with_fallback() should return the registry content (not the
    inline _system_prompt() fallback) when the registry has a valid template.

    We verify by checking that the returned content matches the template file.
    """
    import backend.prompts.registry as reg_module
    from backend.agents.security_agent import SecurityAgent

    # Clear cache to ensure a fresh load
    PromptRegistry._clear_cache()

    agent = SecurityAgent()
    content = agent._get_prompt_with_fallback("security")

    template_path = reg_module._TEMPLATES_ROOT / "security" / "v1.txt"
    expected = template_path.read_text(encoding="utf-8").strip()
    assert content == expected, (
        "_get_prompt_with_fallback() should return registry content, not inline fallback"
    )


# ---------------------------------------------------------------------------
# Test 12: _get_prompt_with_fallback() falls back to _system_prompt() on error
# ---------------------------------------------------------------------------

def test_base_agent_falls_back_when_registry_fails(caplog):
    """
    When the registry raises PromptNotFoundError, _get_prompt_with_fallback()
    must fall back to _system_prompt() and log a WARNING.
    """
    import backend.prompts.registry as reg_module
    from backend.agents.security_agent import SecurityAgent

    PromptRegistry._clear_cache()
    agent = SecurityAgent()

    fake_root = Path("/tmp/nonexistent_templates_xyz")
    with patch.object(reg_module, "_TEMPLATES_ROOT", fake_root):
        with caplog.at_level(logging.WARNING, logger="backend.agents.base_agent"):
            content = agent._get_prompt_with_fallback("security")

    # The fallback must return something (not empty)
    assert len(content) > 50, "Fallback _system_prompt() should return non-empty content"
    # Must have logged a WARNING
    warnings = [r for r in caplog.records if "prompt_registry_miss" in r.message]
    assert len(warnings) >= 1, "Should log a warning when falling back"
