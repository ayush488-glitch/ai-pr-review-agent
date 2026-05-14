# tests/smoke_phase7b.py
#
# Phase 7 smoke tests — Tool Registry, Sandbox, Capability Scope, BaseAgent.call_tool()
#
# WHAT IS TESTED:
#   1. ToolRegistry: register + get + list_names
#   2. ToolRegistry: calling unknown tool raises KeyError
#   3. ToolRegistry: missing required arg raises ValueError
#   4. check_secrets_pattern: detects hardcoded password
#   5. check_secrets_pattern: clean text returns found=False
#   6. Sandbox (Python): valid Python syntax returns valid=True
#   7. Sandbox (Python): invalid Python syntax returns valid=False
#   8. Sandbox: disallowed language raises SandboxViolationError
#   9. Capability scope: SecurityAgent allowed check_secrets_pattern
#  10. Capability scope: DocsAgent NOT allowed run_syntax_check
#  11. BaseAgent.call_tool(): CapabilityViolationError when agent exceeds scope
#  12. BaseAgent.call_tool(): SecurityAgent CAN call check_secrets_pattern
#
# DEPENDENCIES:
#   None of these tests hit the real LLM, database, or Qdrant.
#   The search_similar_findings tool degrades gracefully when Qdrant is unavailable
#   (returns {"results": [], "count": 0, "degraded": True}), so we can test it.
#
# HOW TO RUN:
#   cd ~/Desktop/ai-pr-review-agent
#   python3 -m pytest tests/smoke_phase7b.py -v

import pytest

from backend.tools.tool_registry import (
    ToolRegistry,
    ToolDefinition,
    ToolSchema,
    tool_registry,
)
from backend.tools.sandbox import Sandbox, SandboxViolationError
from backend.tools.capability_scope import (
    CapabilityViolationError,
    check_capability,
    get_allowed_tools,
    raise_if_not_allowed,
)
from backend.models.enums import AgentType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_minimal_tool(name: str) -> ToolDefinition:
    """Helper: create a valid ToolDefinition for testing registry operations."""
    return ToolDefinition(
        schema=ToolSchema(
            name=name,
            description="Test tool for registry tests.",
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            output_description='{"result": str}',
        ),
        handler=lambda args: {"result": args["x"].upper()},
    )


# ---------------------------------------------------------------------------
# Test 1: ToolRegistry.register() and .get()
# ---------------------------------------------------------------------------

def test_registry_register_and_get():
    """
    Registering a tool and fetching it back by name must work.
    The schema must be preserved exactly.
    """
    reg = ToolRegistry()
    tool = make_minimal_tool("test_echo")
    reg.register(tool)

    fetched = reg.get("test_echo")
    assert fetched.schema.name == "test_echo"
    assert "x" in fetched.schema.input_schema["properties"]


# ---------------------------------------------------------------------------
# Test 2: ToolRegistry.list_names()
# ---------------------------------------------------------------------------

def test_registry_list_names():
    """list_names() returns all registered tool names, sorted."""
    reg = ToolRegistry()
    reg.register(make_minimal_tool("alpha"))
    reg.register(make_minimal_tool("beta"))
    reg.register(make_minimal_tool("gamma"))

    names = reg.list_names()
    assert names == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# Test 3: Unknown tool name raises KeyError
# ---------------------------------------------------------------------------

def test_registry_unknown_tool_raises():
    """
    Calling an unregistered tool must raise KeyError immediately.
    This prevents agents from silently failing on hallucinated tool names.
    """
    reg = ToolRegistry()
    with pytest.raises(KeyError, match="Unknown tool"):
        reg.call("nonexistent_tool", {})


# ---------------------------------------------------------------------------
# Test 4: Missing required argument raises ValueError
# ---------------------------------------------------------------------------

def test_registry_missing_required_arg_raises():
    """
    Calling a tool without its required arguments must raise ValueError.
    The error message should name the missing field.
    """
    reg = ToolRegistry()
    reg.register(make_minimal_tool("test_req"))

    with pytest.raises(ValueError, match="missing required arguments"):
        reg.call("test_req", {})  # "x" is required but not provided


# ---------------------------------------------------------------------------
# Test 5: check_secrets_pattern detects hardcoded password
# ---------------------------------------------------------------------------

def test_check_secrets_detects_password():
    """
    The secrets scanner must fire on a literal password assignment.
    This is the highest-confidence detection pattern (no false negatives expected).
    """
    diff_with_secret = """
+    password = "supersecretpassword123"
+    db_host = "localhost"
"""
    result = tool_registry.call("check_secrets_pattern", {"text": diff_with_secret})

    assert result["found"] is True
    assert result["match_count"] >= 1
    # The match should reference the generic_assignment pattern
    pattern_names = [m["pattern_name"] for m in result["matches"]]
    assert "generic_assignment" in pattern_names


# ---------------------------------------------------------------------------
# Test 6: check_secrets_pattern returns found=False on clean text
# ---------------------------------------------------------------------------

def test_check_secrets_clean_diff():
    """
    A diff with no secrets must return found=False.
    False positives here would create noise in every review.
    """
    clean_diff = """
+def calculate_total(items):
+    return sum(item.price for item in items)
"""
    result = tool_registry.call("check_secrets_pattern", {"text": clean_diff})

    assert result["found"] is False
    assert result["match_count"] == 0
    assert result["matches"] == []


# ---------------------------------------------------------------------------
# Test 7: check_secrets_pattern detects AWS access key
# ---------------------------------------------------------------------------

def test_check_secrets_detects_aws_key():
    """AWS AKIA* pattern is high-signal. Must be caught."""
    diff = "+    aws_key = 'AKIAIOSFODNN7EXAMPLE'\n"
    result = tool_registry.call("check_secrets_pattern", {"text": diff})

    assert result["found"] is True
    pattern_names = [m["pattern_name"] for m in result["matches"]]
    assert "aws_access_key" in pattern_names


# ---------------------------------------------------------------------------
# Test 8: Sandbox valid Python syntax returns valid=True
# ---------------------------------------------------------------------------

def test_sandbox_valid_python():
    """
    A syntactically correct Python snippet must pass the sandbox check.
    exit_code=0, valid=True.
    """
    sandbox = Sandbox()
    result = sandbox.run_syntax_check(
        code="def greet(name: str) -> str:\n    return f'Hello, {name}'\n",
        language="python",
    )

    assert result.exit_code == 0
    assert not result.timed_out
    assert result.execution_time_ms >= 0


# ---------------------------------------------------------------------------
# Test 9: Sandbox invalid Python syntax returns exit_code != 0
# ---------------------------------------------------------------------------

def test_sandbox_invalid_python():
    """
    A syntactically broken Python snippet must fail the sandbox check.
    The stderr should mention the syntax error.
    """
    sandbox = Sandbox()
    result = sandbox.run_syntax_check(
        code="def broken_function(\n    # missing closing paren and body\n",
        language="python",
    )

    # exit_code must be non-zero for a syntax error
    assert result.exit_code != 0 or result.timed_out
    # There should be some error output
    # (stderr will contain "SyntaxError" or "EOF")


# ---------------------------------------------------------------------------
# Test 10: Sandbox disallowed language raises SandboxViolationError
# ---------------------------------------------------------------------------

def test_sandbox_disallowed_language_raises():
    """
    Requesting execution in a disallowed language must raise SandboxViolationError
    BEFORE any subprocess is launched. This is the allowlist policy gate.
    """
    sandbox = Sandbox()
    with pytest.raises(SandboxViolationError, match="not in the sandbox allowlist"):
        sandbox.run_syntax_check(code="echo hello", language="bash")


# ---------------------------------------------------------------------------
# Test 11: Capability scope — SecurityAgent allowed check_secrets_pattern
# ---------------------------------------------------------------------------

def test_security_agent_allowed_check_secrets():
    """
    SecurityAgent must be allowed to call check_secrets_pattern.
    This is its primary tool.
    """
    assert check_capability(AgentType.SECURITY, "check_secrets_pattern") is True


# ---------------------------------------------------------------------------
# Test 12: Capability scope — DocsAgent NOT allowed run_syntax_check
# ---------------------------------------------------------------------------

def test_docs_agent_not_allowed_syntax_check():
    """
    DocsAgent must NOT be allowed to call run_syntax_check.
    That is QualityAgent's and TestAgent's tool.
    """
    assert check_capability(AgentType.DOCS, "run_syntax_check") is False


# ---------------------------------------------------------------------------
# Test 13: Capability scope — QualityAgent allowed run_syntax_check
# ---------------------------------------------------------------------------

def test_quality_agent_allowed_syntax_check():
    """QualityAgent must have run_syntax_check in its scope."""
    assert check_capability(AgentType.QUALITY, "run_syntax_check") is True


# ---------------------------------------------------------------------------
# Test 14: raise_if_not_allowed raises CapabilityViolationError
# ---------------------------------------------------------------------------

def test_raise_if_not_allowed_fires():
    """
    raise_if_not_allowed() must raise CapabilityViolationError with structured
    attributes when the agent doesn't have the capability.
    The exception must carry agent_type and tool_name for audit purposes.
    """
    with pytest.raises(CapabilityViolationError) as exc_info:
        raise_if_not_allowed(AgentType.DOCS, "run_syntax_check")

    err = exc_info.value
    assert err.agent_type == AgentType.DOCS
    assert err.tool_name == "run_syntax_check"
    assert "run_syntax_check" not in err.allowed


# ---------------------------------------------------------------------------
# Test 15: BaseAgent.call_tool() enforces scope (CapabilityViolationError)
# ---------------------------------------------------------------------------

def test_base_agent_call_tool_scope_violation():
    """
    An agent calling a tool outside its capability scope via call_tool()
    must raise CapabilityViolationError.
    This tests the enforcement gate in BaseAgent, not just capability_scope directly.
    """
    from backend.agents.docs_agent import DocsAgent

    agent = DocsAgent()
    # DocsAgent is not allowed to call run_syntax_check
    with pytest.raises(CapabilityViolationError) as exc_info:
        agent.call_tool("run_syntax_check", {"code": "print('hi')", "language": "python"})

    assert exc_info.value.agent_type == AgentType.DOCS


# ---------------------------------------------------------------------------
# Test 16: BaseAgent.call_tool() succeeds for allowed tool
# ---------------------------------------------------------------------------

def test_base_agent_call_tool_allowed():
    """
    SecurityAgent calling check_secrets_pattern via call_tool() must succeed
    and return a valid result dict with the expected shape.
    """
    from backend.agents.security_agent import SecurityAgent

    agent = SecurityAgent()
    result = agent.call_tool(
        "check_secrets_pattern",
        {"text": 'api_key = "sk-1234567890abcdef"'},
    )

    # Must be a dict with the expected keys
    assert isinstance(result, dict)
    assert "found" in result
    assert "match_count" in result
    assert "matches" in result
    # Must have detected the API key
    assert result["found"] is True
