"""Phase 5 smoke test."""
import asyncio
import sys
sys.path.insert(0, "/Users/ayushsingh/Desktop/ai-pr-review-agent")

from backend.models.enums import AgentType, FindingSeverity, FindingCategory
from backend.models.findings import AgentFinding, AgentFindingRaw
from backend.agents.base_agent import (
    _truncate_to_budget,
    _build_system_prompt,
    _apply_output_guardrail,
    AgentOutput,
)
from backend.tools.model_router import get_model_config
from backend.agents.security_agent import SecurityAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.test_agent import TestAgent
from backend.agents.docs_agent import DocsAgent
from backend.tools.llm_client import LLMResponse
from unittest.mock import AsyncMock, MagicMock

print("PHASE 5 SMOKE TEST")
print("=" * 60)

# ── TEST 1: AgentFindingRaw normalizes messy LLM output ──────────────────────
print("\n[1] AgentFindingRaw normalization")
raw = AgentFindingRaw(severity="HIGH", category="Security", summary="SQL injection risk",
                       file_path="src/auth.py", line_start=42, confidence="0.92")
assert raw.severity == "high", f"got {raw.severity!r}"
assert raw.category == "security", f"got {raw.category!r}"
assert raw.confidence == 0.92, f"got {raw.confidence!r}"
print("  severity HIGH -> 'high'  OK")
print("  category Security -> 'security'  OK")
print("  confidence '0.92' -> 0.92  OK")

# ── TEST 2: to_finding() ──────────────────────────────────────────────────────
print("\n[2] AgentFindingRaw.to_finding()")
finding = raw.to_finding(agent_type="security")
assert isinstance(finding, AgentFinding)
assert finding.severity == FindingSeverity.HIGH
assert finding.category == FindingCategory.SECURITY
assert finding.confidence == 0.92
print(f"  AgentFinding: severity={finding.severity.value} category={finding.category.value}  OK")

# ── TEST 3: to_state_dict() returns plain strings ─────────────────────────────
print("\n[3] AgentFinding.to_state_dict()")
d = finding.to_state_dict()
assert isinstance(d["severity"], str)
assert d["severity"] == "high"
assert d["category"] == "security"
print("  severity is str 'high' (not enum)  OK")
print("  category is str 'security' (not enum)  OK")

# ── TEST 4: _truncate_to_budget() ────────────────────────────────────────────
print("\n[4] _truncate_to_budget()")
short = "line1\nline2\nline3"
assert _truncate_to_budget(short, 100) == short
long_diff = "a" * 1000 + "\n" + "b" * 1000
result = _truncate_to_budget(long_diff, 10)
assert "[DIFF TRUNCATED" in result
assert len(result) < len(long_diff)
print("  Short diff (<budget): unchanged  OK")
print("  Long diff (>budget): truncated with notice  OK")

# ── TEST 5: _build_system_prompt() primacy ────────────────────────────────────
print("\n[5] _build_system_prompt() primacy principle")
system = _build_system_prompt("You are a security expert. Look for SQL injection.")
json_pos   = system.find("CRITICAL INSTRUCTION")
agent_pos  = system.find("security expert")
assert json_pos < agent_pos, "JSON instruction must come before agent instructions"
assert system.startswith("CRITICAL INSTRUCTION")
print(f"  JSON instruction FIRST (pos={json_pos} < agent pos={agent_pos})  OK")

# ── TEST 6: _apply_output_guardrail() ─────────────────────────────────────────
print("\n[6] _apply_output_guardrail() — 5 cases")

def make_resp(content, valid=True, model="gpt-4o-mini"):
    return LLMResponse(content=content, input_tokens=50, output_tokens=30,
                       model_used=model, latency_seconds=0.5,
                       estimated_cost_usd=0.0005, is_valid_json=valid)

# Case A: valid findings key
f1, c1 = _apply_output_guardrail(
    make_resp({"findings": [{"severity":"high","category":"security","summary":"X","confidence":0.9}]}),
    "security")
assert len(f1) == 1 and c1 > 0.5
print(f"  Case A (findings key): {len(f1)} finding  OK")

# Case B: list directly
f2, c2 = _apply_output_guardrail(
    make_resp([{"severity":"medium","category":"quality","summary":"Y","confidence":0.8}]),
    "quality")
assert len(f2) == 1
print(f"  Case B (list directly): {len(f2)} finding  OK")

# Case C: invalid JSON
f3, c3 = _apply_output_guardrail(make_resp({}, valid=False), "security")
assert len(f3) == 0 and c3 == 0.3
print(f"  Case C (invalid JSON): empty findings, conf=0.3 -> HITL  OK")

# Case D: wrong key 'issues'
f4, c4 = _apply_output_guardrail(
    make_resp({"issues": [{"severity":"low","category":"docs","summary":"Z","confidence":0.7}]}),
    "docs")
assert len(f4) == 1
print(f"  Case D (wrong key 'issues'): {len(f4)} finding recovered  OK")

# Case E: empty findings list
f5, c5 = _apply_output_guardrail(make_resp({"findings": []}), "docs")
assert len(f5) == 0 and c5 == 0.7
print(f"  Case E (empty findings): conf=0.7 (conservative)  OK")

# ── TEST 7: Model router ──────────────────────────────────────────────────────
print("\n[7] Model router")
sec_cfg  = get_model_config(AgentType.SECURITY)
qual_cfg = get_model_config(AgentType.QUALITY)
assert sec_cfg.provider == "anthropic" and "claude" in sec_cfg.model_name
assert sec_cfg.context_budget_tokens == 8000
assert qual_cfg.provider == "openai" and "gpt-4o-mini" in qual_cfg.model_name
print(f"  SECURITY: {sec_cfg.provider}/{sec_cfg.model_name} budget={sec_cfg.context_budget_tokens}  OK")
print(f"  QUALITY:  {qual_cfg.provider}/{qual_cfg.model_name}  OK")

# ── TEST 8: All agent classes ─────────────────────────────────────────────────
print("\n[8] Specialist agent instantiation")
for AgentClass, expected in [(SecurityAgent, AgentType.SECURITY),
                               (QualityAgent,  AgentType.QUALITY),
                               (TestAgent,     AgentType.TEST),
                               (DocsAgent,     AgentType.DOCS)]:
    ag = AgentClass()
    assert ag.agent_type == expected
    assert len(ag._system_prompt()) > 100
    print(f"  {AgentClass.__name__}: {ag.agent_type.value}  OK")

# ── TEST 9: BaseAgent.analyze() with mocked LLM ──────────────────────────────
print("\n[9] BaseAgent.analyze() end-to-end with mock LLM")

mock_response = LLMResponse(
    content={"findings": [{"severity":"critical","category":"security",
                           "summary":"Hardcoded API key","file_path":"src/config.py",
                           "line_start":15,"confidence":0.97}]},
    input_tokens=800, output_tokens=120,
    model_used="claude-3-5-sonnet-20241022",
    latency_seconds=2.3, estimated_cost_usd=0.00312, is_valid_json=True,
)
mock_client = MagicMock()
mock_client.call_anthropic = AsyncMock(return_value=mock_response)
mock_client.call_openai    = AsyncMock(return_value=mock_response)

sec_agent = SecurityAgent(client=mock_client)
result = asyncio.run(sec_agent.analyze(
    diff="+ api_key = 'sk-1234567890abcdef'",
    pr_title="Add config module",
    pr_description="Adds centralized config loading",
    repo_name="acme/backend",
))
assert result.agent_type == AgentType.SECURITY
assert len(result.findings) == 1
assert result.findings[0].severity == FindingSeverity.CRITICAL
assert result.findings[0].confidence == 0.97
assert result.tokens_used == 920
assert result.error_message is None
print(f"  SecurityAgent.analyze() -> findings={len(result.findings)}")
print(f"  severity={result.findings[0].severity.value} conf={result.confidence:.2f} tokens={result.tokens_used}  OK")
print("\n" + "=" * 60)
print("PHASE 5 SMOKE TEST: ALL ASSERTIONS PASSED")
