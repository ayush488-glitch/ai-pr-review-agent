# backend/agents/base_agent.py
#
# BaseAgent — the abstract parent class for all specialist agents.
#
# WHAT THIS FILE IS:
# Every specialist agent (SecurityAgent, QualityAgent, TestAgent, DocsAgent)
# is a subclass of BaseAgent. The base class handles everything that is common
# to all agents so that each specialist focuses only on what makes it unique.
#
# WHAT THE BASE CLASS DOES (so each specialist doesn't have to):
#   1. CONTEXT TRUNCATION:
#      The diff can be enormous (50k+ tokens for a large PR).
#      We truncate it to the agent's context_budget_tokens BEFORE the LLM call.
#      Truncation is from the bottom — top of the diff has the most important code.
#
#   2. PROMPT ASSEMBLY:
#      The LLM prompt has 4 sections (primacy/recency principle from wiki):
#        [1] MOST IMPORTANT INSTRUCTION: "Return JSON. Nothing else."  <- first = remembered
#        [2] Agent's specific instructions (e.g., "Look for SQL injection, XSS...")
#        [3] PR context (title, description, file count, author)
#        [4] The diff itself  <- last = also remembered (recency effect)
#
#   3. THE LLM CALL:
#      Routes to call_openai() or call_anthropic() based on ModelConfig.provider.
#      Returns LLMResponse with parsed content + token counts.
#
#   4. OUTPUT GUARDRAIL (most important):
#      LLMs sometimes return:
#        - Text that isn't JSON at all ("I found 3 issues: 1. ...")
#        - JSON with the wrong schema ({"issues": [...]} instead of {"findings": [...]})
#        - A dict when we expected a list, or vice versa
#        - A list of strings instead of a list of dicts
#      The guardrail handles all these cases:
#        - Valid JSON list of dicts -> parse each into AgentFindingRaw -> AgentFinding
#        - Valid JSON dict with "findings" key -> use that list
#        - Valid JSON but wrong shape -> return empty findings + confidence=0.3 -> HITL
#        - Invalid JSON -> _try_extract_json() attempted, then empty + confidence=0.3 -> HITL
#      confidence=0.3 is below our HITL threshold -> human reviews instead of auto-posting.
#
#   5. AGENT RESULT ASSEMBLY:
#      Returns AgentResult (the Phase 4 model) with findings, confidence, token usage.
#
# WHAT EACH SPECIALIST PROVIDES (by implementing abstract methods):
#   - agent_type property: AgentType.SECURITY / .QUALITY / .TEST / .DOCS
#   - _system_prompt(): the agent's specific instructions to the LLM
#     This is where the real expertise lives. SecurityAgent's prompt is different from
#     QualityAgent's prompt — that's the entire difference between them.
#
# CONTEXT BUDGET (token-based truncation):
# We use a rough approximation: 1 token ≈ 4 characters (standard English/code text).
# This is not exact — real tokenization is BPE-based and model-specific.
# For code, actual token/char ratio is closer to 1:3 (more tokens per char than prose).
# We use 1:4 as a conservative estimate, so we slightly under-use the budget.
# This is safe — better to under-use than to exceed the context window and get an error.
# Phase 10 (Observability) will add real token counting (tiktoken for OpenAI).

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from backend.agents.contracts import AgentTask, AgentVerdict
from backend.core.exceptions import PromptNotFoundError
from backend.models.enums import AgentType, FindingSeverity
from backend.models.findings import AgentFinding, AgentFindingRaw
from backend.tools.llm_client import LLMClient, LLMResponse, llm_client
from backend.tools.model_router import ModelConfig, get_model_config

logger = logging.getLogger(__name__)

# Characters per token approximation.
# 1 token ≈ 4 characters for English/code.
# We use this to estimate how many characters fit in our token budget.
# CHARS_PER_TOKEN = 4 means: if budget is 8000 tokens -> allow 32000 chars of diff.
CHARS_PER_TOKEN = 4


@dataclass
class AgentOutput:
    """
    The return type of BaseAgent.analyze().

    WHY NOT USE THE EXISTING AgentResult FROM models/review.py?
    AgentResult (in review.py) was designed for Phase 3 — it has fields like
    agent_name, category, duration_seconds that match the orchestrator's
    state schema. It uses Finding (not AgentFinding) for its findings list.

    AgentOutput is the agents layer's OWN return type. It lives here,
    in the agents module. It is then converted to finding dicts by
    _call_agent_real() in nodes.py at the orchestrator boundary.

    This keeps the dependency direction clean:
      agents layer -> produces AgentOutput
      orchestrator boundary -> converts AgentOutput to state dicts
      state/AgentResult -> used by the orchestrator internally

    FIELDS:
    """
    # Which agent produced this output
    agent_type: AgentType

    # The validated findings from the LLM
    findings: list[AgentFinding] = field(default_factory=list)

    # Mean confidence across findings (0.0 - 1.0)
    # Low confidence (< threshold) -> HITL queue
    confidence: float = 0.5

    # Total tokens used (input + output) for cost tracking
    tokens_used: int = 0

    # None on success; error message if the LLM call failed
    error_message: str | None = None

    # Phase 8: per-agent verdict derived from findings.
    # APPROVE:          no HIGH or CRITICAL findings
    # REQUEST_CHANGES:  at least one HIGH finding
    # CRITICAL_BLOCK:   at least one CRITICAL finding
    #
    # This is SEPARATE from the system-wide ReviewVerdict in models/enums.py.
    # ReviewVerdict is the FINAL decision after arbitration across all 4 agents.
    # AgentVerdict is the raw signal from one domain expert BEFORE arbitration.
    #
    # Default: APPROVE — if the agent failed entirely (error_message set),
    # we conservatively treat it as APPROVE rather than blocking.
    # The aggregator handles failed agents via the successful_agents count check.
    per_verdict: AgentVerdict = AgentVerdict.APPROVE


class BaseAgent(ABC):
    """
    Abstract base class for all specialist agents.

    Subclasses MUST implement:
      - agent_type (property): returns the AgentType enum for this agent
      - _system_prompt(): returns the system prompt string for the LLM

    Subclasses MAY override:
      - analyze(): if they need to do something non-standard (rare)

    LIFECYCLE:
    One BaseAgent instance per review call.
    Agents are NOT shared between reviews — they are created fresh each time
    inside the fan_out_agents node. This avoids any state leaking between reviews.
    """

    def __init__(self, client: LLMClient | None = None) -> None:
        """
        Args:
            client: the LLM client to use. Default: the module-level singleton.
                    Pass a custom client in tests to mock LLM calls without hitting the API.
        """
        self._client = client or llm_client

    @property
    @abstractmethod
    def agent_type(self) -> AgentType:
        """
        Which specialist type this agent is.
        Used to:
          - Look up the ModelConfig from model_router.py
          - Tag each AgentFinding with the producing agent
          - Route the AgentResult back to the correct slot in the graph state
        """
        ...

    @abstractmethod
    def _system_prompt(self) -> str:
        """
        The LLM's instructions for this specific agent.

        MUST:
          - Tell the LLM exactly what kinds of issues to look for
          - Specify the output format (JSON array with exact field names)
          - Be specific enough that the LLM doesn't hallucinate issues
          - Be concise enough to fit in the context budget

        The base class adds the JSON format requirement at the TOP of the prompt
        (primacy principle: most important instruction first).
        Subclasses just describe the domain expertise.
        """
        ...

    async def analyze(
        self,
        diff: str = "",
        pr_title: str = "",
        pr_description: str = "",
        repo_name: str = "",
        retrieved_context: str = "",
        task: "AgentTask | None" = None,
    ) -> AgentOutput:
        """
        Runs one specialist review pass on the PR diff.

        This is the main method. Called by the fan_out_agents node for each agent.

        CALLING CONVENTIONS (Phase 8 transition):
          Old call (still supported):
            await agent.analyze(diff=..., pr_title=..., pr_description=...,
                                repo_name=..., retrieved_context=...)
          New call (preferred, Phase 8+):
            await agent.analyze(task=AgentTask(...))
          When `task` is provided, it takes precedence over the individual arguments.
          This allows callers to migrate incrementally without breaking anything.

        Steps:
          1. Resolve inputs: unpack AgentTask or use positional args
          2. Get model config (model name, provider, context budget)
          3. Truncate diff to context budget
          4. Assemble the full prompt (with optional RAG context + peer context)
          5. Call the LLM
          6. Apply output guardrail (parse + validate)
          7. Derive per-agent verdict from findings (Phase 8)
          8. Return AgentOutput

        Args:
            diff:               The raw git diff string for this PR.
            pr_title:           The PR title.
            pr_description:     The PR body text.
            repo_name:          "owner/repo" string.
            retrieved_context:  RAG-retrieved prior code context (Phase 6).
            task:               AgentTask (Phase 8). When provided, overrides
                                all positional args above.

        Returns:
            AgentOutput — always returns something (never raises).
            On total failure: returns AgentOutput with empty findings, confidence=0.3,
            error_message set, per_verdict=APPROVE (conservative — no positive evidence
            of issues, so don't block; aggregator handles via successful_agents count).
        """
        config = get_model_config(self.agent_type)
        agent_name = self.agent_type.value  # "security", "quality", etc.

        # -----------------------------------------------------------------------
        # STEP 0: Resolve inputs — AgentTask takes precedence over positional args.
        #
        # Phase 8 transition pattern: callers can pass an AgentTask for the new
        # typed contract, OR the original positional args for backward compatibility.
        # When task is provided, unpack it. Otherwise use the positional args.
        #
        # WHY NOT REMOVE THE OLD SIGNATURE?
        #   Tests (smoke_phase5b.py, etc.) call analyze() with positional args.
        #   Removing them would break all existing tests.
        #   Additive change: the new `task` kwarg is optional (default None).
        # -----------------------------------------------------------------------
        if task is not None:
            _diff = task.diff
            _pr_title = task.pr_title
            _pr_description = task.pr_description
            _repo_name = task.repo_name
            _retrieved_context = task.retrieved_context
            _peer_context = task.peer_context
            _workflow_id = task.workflow_id
        else:
            _diff = diff
            _pr_title = pr_title
            _pr_description = pr_description
            _repo_name = repo_name
            _retrieved_context = retrieved_context
            _peer_context = ()  # no peer context in old-style calls
            _workflow_id = None  # Phase 16: legacy callers have no workflow id

        # STEP 1: Truncate the diff to this agent's context budget.
        truncated_diff = _truncate_to_budget(_diff, config.context_budget_tokens)
        was_truncated = len(truncated_diff) < len(_diff)
        if was_truncated:
            logger.info(
                "diff_truncated | agent=%s original_chars=%d truncated_chars=%d",
                agent_name, len(diff), len(truncated_diff),
            )

        # STEP 2: Assemble the full prompt for the LLM.
        # Primary source: prompt registry (versioned .txt file from disk).
        # Fallback source: _system_prompt() inline string defined in each subclass.
        # This two-level strategy means:
        #   - Registry is the canonical source in production (enables versioning, A/B test)
        #   - _system_prompt() is a safety net: if a deployment is missing the templates/
        #     directory, the agent still works instead of crashing entirely.
        # (LLMOps-Essentials.md: "Skip any layer and your car becomes a liability.")
        agent_system_instructions = self._get_prompt_with_fallback(agent_name)

        # Structure (primacy/recency: most important first AND last):
        #   [SYSTEM PROMPT]  = format instruction + agent expertise
        #   [USER MESSAGE]   = PR context + RAG context (if any) + diff
        system = _build_system_prompt(agent_system_instructions)
        user_message = _build_user_message(
            diff=truncated_diff,
            pr_title=_pr_title,
            pr_description=_pr_description,
            repo_name=_repo_name,
            was_truncated=was_truncated,
            retrieved_context=_retrieved_context,
            peer_context=_peer_context,
        )

        # STEP 3: Call the LLM.
        # Phase 16: enforce daily budget cap + tag the call for cost
        # attribution. BudgetExceededError -> degraded result that triggers
        # HITL via the existing low-confidence escalation path.
        from backend.economics import BudgetExceededError, BudgetGuard
        from backend.observability.workflow_context import set_workflow_context

        try:
            await BudgetGuard().check_daily_budget()
        except BudgetExceededError as bx:
            logger.warning(
                "agent_skipped_budget_exceeded | agent=%s spent=$%.4f cap=$%.2f",
                agent_name, bx.current_spend_usd, bx.cap_usd,
            )
            return AgentOutput(
                agent_type=self.agent_type,
                findings=[],
                confidence=0.3,  # low → triggers HITL escalation aggregator
                tokens_used=0,
                error_message=f"daily_budget_exceeded: {bx}",
                per_verdict=AgentVerdict.CRITICAL_BLOCK,
            )

        # Tag the call so llm_client persists it with the right (workflow_id,
        # agent_type) attribution. ContextVar is per-asyncio-task, and each
        # fan-out agent runs in its own task, so we don't need to reset on
        # exit — siblings get isolated context automatically.
        set_workflow_context(workflow_id=_workflow_id, agent_type=agent_name)
        try:
            response: LLMResponse = await self._dispatch_llm_call(
                config=config,
                system=system,
                user_message=user_message,
            )
        except Exception as e:
            # LLM call failed entirely (network, auth, all retries exhausted).
            # Return a safe empty result that triggers HITL.
            logger.error(
                "agent_llm_call_failed | agent=%s error=%s",
                agent_name, str(e), exc_info=True,
            )
            return AgentOutput(
                agent_type=self.agent_type,
                findings=[],
                confidence=0.3,   # Low confidence -> HITL
                tokens_used=0,
                error_message=f"LLM call failed: {str(e)}",
                per_verdict=AgentVerdict.APPROVE,  # no positive evidence of issues
            )

        # STEP 4: Apply the output guardrail.
        # Parses the LLM's response into a list of AgentFinding.
        # On any parse failure: returns [] findings and confidence=0.3.
        findings, confidence = _apply_output_guardrail(
            response=response,
            agent_type=agent_name,
        )

        logger.info(
            "agent_complete | agent=%s findings=%d confidence=%.2f "
            "input_tokens=%d output_tokens=%d cost=$%.6f",
            agent_name, len(findings), confidence,
            response.input_tokens, response.output_tokens,
            response.estimated_cost_usd,
        )

        return AgentOutput(
            agent_type=self.agent_type,
            findings=findings,
            confidence=confidence,
            tokens_used=response.input_tokens + response.output_tokens,
            error_message=None,
            per_verdict=self._derive_per_agent_verdict(findings),
        )

    def _get_prompt_with_fallback(self, agent_name: str) -> str:
        """
        Loads the prompt from the registry, falling back to _system_prompt() on error.

        TWO-LEVEL PROMPT STRATEGY:
          Level 1 (primary): PromptRegistry reads templates/{agent}/v{N}.txt from disk.
                             This is the versioned, code-reviewable, A/B-testable path.
          Level 2 (fallback): _system_prompt() inline string in each agent subclass.
                             This runs only if the registry raises PromptNotFoundError,
                             which means the templates/ directory is missing from the
                             deployment (bad deploy, not a logic error).

        WHY HAVE A FALLBACK AT ALL?
        The inline _system_prompt() method keeps each agent self-contained. If the
        deployment is broken (missing templates dir), the system degrades gracefully
        instead of crashing. In production, the registry is always the active path.

        The fallback is logged as a WARNING so it shows up in Prometheus/Alertmanager
        (Phase 10 will wire these logs to OTel spans). A WARNING here means:
        "the prompt registry is not working — please fix the deployment."
        """
        # Lazy import to avoid circular imports at module level.
        # prompts.registry imports backend.core.exceptions and backend.models.enums,
        # both of which are below agents/ in the dependency graph.
        # Importing at call time means the module graph stays clean at import time.
        from backend.prompts.registry import registry

        try:
            return registry.load_prompt(self.agent_type)
        except PromptNotFoundError as e:
            logger.warning(
                "prompt_registry_miss_falling_back | agent=%s error=%s "
                "— using inline _system_prompt() fallback. "
                "Check that backend/prompts/templates/ was deployed correctly.",
                agent_name, str(e),
            )
            return self._system_prompt()

    # -----------------------------------------------------------------------
    # Phase 8: Per-agent verdict derivation
    # -----------------------------------------------------------------------

    def _derive_per_agent_verdict(self, findings: list[AgentFinding]) -> AgentVerdict:
        """
        Derives this agent's verdict from its findings list.

        MAPPING LOGIC:
          - Any CRITICAL finding -> CRITICAL_BLOCK
            (highest priority: immediate escalation signal)
          - Any HIGH finding (no critical) -> REQUEST_CHANGES
            (warrants blocking the PR, but not yet immediate HITL)
          - Only MEDIUM/LOW findings, or no findings -> APPROVE
            (either nothing wrong, or issues are informational)

        WHY CRITICAL_BLOCK IS SEPARATE FROM REQUEST_CHANGES:
          The Safety-Threshold Rule (wiki: Safety-Threshold-Rule.md) requires
          2+ agents to agree on CRITICAL_BLOCK before the system escalates to
          NEEDS_HUMAN_REVIEW. This prevents a single miscalibrated agent from
          triggering HITL on every PR. The aggregator reads all 4 AgentVerdicts
          and counts how many agents said CRITICAL_BLOCK before acting.

        WHY APPROVE WHEN AGENT FAILED (no findings)?
          If an agent fails entirely, it returns findings=[] with error_message set.
          We return APPROVE here because we have NO POSITIVE EVIDENCE of issues —
          the agent couldn't check. The aggregator handles the failure separately
          via the successful_agents count threshold (Rule 4 in aggregate_results).
          We don't want agent failure to be treated the same as "found a CRITICAL issue".

        Args:
            findings: the validated findings from this agent's LLM call.

        Returns:
            AgentVerdict enum value.
        """
        has_critical = any(
            f.severity == FindingSeverity.CRITICAL
            for f in findings
        )
        if has_critical:
            return AgentVerdict.CRITICAL_BLOCK

        has_high = any(
            f.severity == FindingSeverity.HIGH
            for f in findings
        )
        if has_high:
            return AgentVerdict.REQUEST_CHANGES

        return AgentVerdict.APPROVE

    # -----------------------------------------------------------------------
    # Phase 7: Tool invocation
    # -----------------------------------------------------------------------

    def call_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """
        Call a registered tool, enforcing this agent's capability scope first.

        THREE-GATE PATTERN (wiki: Autonomous-Action-Agents):
          Gate 1 — capability_scope.raise_if_not_allowed():
                   Is this agent ALLOWED to call this tool at all?
                   Raises CapabilityViolationError on policy violation.
          Gate 2 — tool_registry.call():
                   Does this tool EXIST and are required args present?
                   Raises KeyError on unknown tool, ValueError on missing args.
          Gate 3 — sandbox (inside the tool handler, if applicable):
                   Is the execution SAFE? (timeout, output limit, language allowlist)

        WHY scope is checked FIRST (before registry):
            If we checked the registry first, a valid tool name would pass the
            KeyError guard even if the agent isn't allowed to call it.
            The capability check is a security gate — it must come before execution.

        WHY synchronous (not async):
            The current tool handlers are all synchronous (regex, subprocess).
            Async tools (HTTP-based advisories in Phase 14) will need an
            async variant. For Phase 7 sync is correct and simpler.

        Args:
            tool_name: name of a registered tool (e.g. "check_secrets_pattern")
            args:      arguments dict matching the tool's input_schema

        Returns:
            dict[str, Any] — tool result (shape documented per-tool in ToolSchema)

        Raises:
            CapabilityViolationError: agent is not allowed to call this tool
            KeyError:                 tool is not registered
            ValueError:               required args are missing
        """
        # Gate 1: capability scope check
        # Lazy import to keep the dependency direction clean:
        # agents/ -> tools/ is correct; we don't want tools/ to import from agents/
        from backend.tools.capability_scope import raise_if_not_allowed

        raise_if_not_allowed(self.agent_type, tool_name)

        # Gate 2 + 3: registry dispatch (and sandbox inside handler if applicable)
        from backend.tools.tool_registry import tool_registry

        logger.debug(
            "tool_call | agent=%s tool=%s args_keys=%s",
            self.agent_type.value,
            tool_name,
            list(args.keys()),
        )
        result = tool_registry.call(tool_name, args)

        logger.debug(
            "tool_result | agent=%s tool=%s result_keys=%s",
            self.agent_type.value,
            tool_name,
            list(result.keys()),
        )
        return result

    async def _dispatch_llm_call(
        self,
        config: ModelConfig,
        system: str,
        user_message: str,
    ) -> LLMResponse:
        """
        Routes the LLM call to the correct provider based on ModelConfig.

        Args:
            config:       ModelConfig from model_router.py
            system:       The assembled system prompt
            user_message: The user turn (PR context + diff)

        Returns:
            LLMResponse from llm_client.call_openai() or call_anthropic()

        Raises:
            AgentError if the LLM call fails after retries.
            (Caught in analyze() above, which returns a safe empty result.)
        """
        messages = [{"role": "user", "content": user_message}]

        if config.provider == "openai":
            return await self._client.call_openai(
                model=config.model_name,
                messages=messages,
                system_prompt=system,
                json_mode=True,
                max_tokens=config.max_response_tokens,
            )
        elif config.provider == "anthropic":
            return await self._client.call_anthropic(
                model=config.model_name,
                messages=messages,
                system_prompt=system,
                max_tokens=config.max_response_tokens,
            )
        else:
            raise ValueError(
                f"Unknown provider '{config.provider}' in ModelConfig. "
                f"Add a branch in BaseAgent._dispatch_llm_call()."
            )


# ---------------------------------------------------------------------------
# PRIVATE HELPERS (module-level functions, not methods)
# These are pure functions — no side effects, no class state.
# Pure functions are easier to test, read, and reason about.
# ---------------------------------------------------------------------------

def _truncate_to_budget(diff: str, budget_tokens: int) -> str:
    """
    Truncates a diff string to fit within a token budget.

    Uses the CHARS_PER_TOKEN approximation.
    Truncates from the BOTTOM of the diff (top = most important code).

    If the diff fits within the budget, returns it unchanged.
    If it doesn't, truncates and appends a truncation notice.
    The truncation notice tells the LLM "there is more diff that you didn't see"
    so it can note this in its analysis instead of assuming it saw everything.

    WHY A NOTICE?
    Without a notice, the LLM might say "no issues found" because it didn't
    see the last 40% of the diff. With a notice, it can caveat its findings.
    """
    max_chars = budget_tokens * CHARS_PER_TOKEN
    if len(diff) <= max_chars:
        return diff

    truncated = diff[:max_chars]

    # Try to truncate at a clean line boundary to avoid cutting a diff hunk mid-line.
    # rfind('\n') finds the last newline before our cut point.
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars // 2:  # only use it if we're not cutting too much
        truncated = truncated[:last_newline]

    chars_removed = len(diff) - len(truncated)
    truncation_notice = (
        f"\n\n[DIFF TRUNCATED: {chars_removed} characters removed from the end. "
        f"Your analysis covers the first {len(truncated)} characters of the diff. "
        f"Note any findings based on incomplete context.]"
    )
    return truncated + truncation_notice


def _build_system_prompt(agent_specific_instructions: str) -> str:
    """
    Builds the full system prompt for the LLM.

    STRUCTURE (primacy/recency principle from wiki):
      [1] MOST IMPORTANT: JSON format requirement  <- FIRST (primacy)
      [2] Agent's domain-specific instructions
      (The diff is in the user turn, last -> recency)

    The JSON format block is placed FIRST so the model remembers it throughout
    its long generation. If we put it last, the model might start generating
    markdown prose before "remembering" it was supposed to return JSON.

    The format requires:
      - A JSON object with a "findings" key containing an array
      - Each element is an AgentFinding (see models/findings.py for field names)
    """
    json_format_block = """\
CRITICAL INSTRUCTION — READ THIS FIRST:
You MUST respond with ONLY a valid JSON object. No prose, no markdown, no explanation.
The JSON must have exactly this structure:
{
  "findings": [
    {
      "severity": "critical|high|medium|low",
      "category": "security|quality|test_coverage|documentation|performance|architecture",
      "summary": "One sentence describing the issue.",
      "file_path": "path/to/file.py or null",
      "line_start": 42,
      "line_end": 45,
      "suggestion": "Concrete fix suggestion or null.",
      "confidence": 0.95
    }
  ]
}
If you find no issues, return: {"findings": []}
Do NOT add any text outside the JSON object.
"""

    return json_format_block + "\n\n" + agent_specific_instructions


def _build_user_message(
    diff: str,
    pr_title: str,
    pr_description: str,
    repo_name: str,
    was_truncated: bool,
    retrieved_context: str = "",
    peer_context: tuple = (),
) -> str:
    """
    Builds the user turn for the LLM (the PR context + optional RAG context + diff).

    STRUCTURE:
      [1] PR metadata (title, description, repo)   <- gives the LLM context
      [2] Truncation warning (if applicable)        <- honesty about incomplete data
      [3] RAG context block (if available)          <- prior code context (Phase 6)
      [4] Peer context block (if available)         <- what other agents found (Phase 8)
      [5] The diff                                  <- LAST (recency effect)

    The diff is placed LAST so it's fresh in the LLM's context when it starts
    generating findings. The LLM's attention mechanism gives slightly more weight
    to recent tokens (recency effect).

    PEER CONTEXT (Phase 8):
    When peer_context is non-empty, we inject a compact summary of what other
    agents found BEFORE the diff. This gives the agent cross-domain awareness:
    it can see "SecurityAgent already flagged auth.py" and avoid duplication.
    WIKI: WorkTask-Contract.md — "Give workers only the context keys they need."
    We pass the COMPACT summary (agent_type, finding_count, highest_severity,
    flagged_files) — NOT the full finding list, to avoid blowing token budgets.
    """
    truncation_warning = ""
    if was_truncated:
        truncation_warning = (
            "\n⚠️  Note: This diff was truncated. "
            "Your analysis covers only the first portion of the full diff.\n"
        )

    # RAG context block — only included if non-empty
    # (RAG-Architecture.md: "RAG is enhancement not foundation.")
    rag_block = ""
    if retrieved_context:
        rag_block = (
            "\n--- PRIOR CODEBASE CONTEXT (from similar PRs in this repo) ---\n"
            "Use this context to identify patterns, similar issues seen before,\n"
            "or code that may be relevant to your analysis. This is supplementary.\n\n"
            f"{retrieved_context}\n"
            "--- END PRIOR CONTEXT ---\n"
        )

    # Peer context block — only included if non-empty (Phase 8 sequential pass)
    # In the parallel fan-out (current Phase 4), this is always empty.
    # WIKI: WorkTask-Contract.md — "Workers are decoupled; they don't know about each other."
    # We expose a SUMMARY only — just enough to avoid duplicate flagging.
    peer_block = ""
    if peer_context:
        lines = ["--- PEER AGENT SUMMARIES (other agents' findings on this PR) ---"]
        lines.append(
            "These summaries show what other specialist agents found. "
            "Use this to avoid flagging the same issues they already caught."
        )
        for p in peer_context:
            files_str = ", ".join(p.flagged_files[:5]) if p.flagged_files else "none"
            lines.append(
                f"  {p.agent_type}: {p.finding_count} finding(s), "
                f"highest_severity={p.highest_severity}, "
                f"flagged_files=[{files_str}]"
            )
        lines.append("--- END PEER SUMMARIES ---")
        peer_block = "\n" + "\n".join(lines) + "\n"

    return (
        f"Repository: {repo_name}\n"
        f"PR Title: {pr_title}\n"
        f"PR Description:\n{pr_description or '(no description provided)'}\n"
        f"{truncation_warning}"
        f"{rag_block}"
        f"{peer_block}"
        f"\n--- DIFF START ---\n"
        f"{diff}\n"
        f"--- DIFF END ---\n"
        f"\nNow analyze this diff and return your findings as JSON."
    )


def _apply_output_guardrail(
    response: LLMResponse,
    agent_type: str,
) -> tuple[list[AgentFinding], float]:
    """
    Output guardrail: parses and validates the LLM's response.

    This is the most important defensive function in the agent layer.
    It handles everything that can go wrong with LLM output.

    WHAT CAN GO WRONG:
      1. The LLM returned valid JSON but with {"issues": [...]} instead of {"findings": [...]}
         -> We try "findings", then "issues", then "results", then any list value
      2. The LLM returned a list directly instead of {"findings": [...]}
         -> We accept it
      3. The LLM returned garbage (not JSON at all)
         -> _try_extract_json() already attempted in llm_client.py
         -> If response.is_valid_json is False, we return empty + low confidence
      4. Findings are malformed (wrong types, missing fields)
         -> AgentFindingRaw handles loose types, .to_finding() normalizes

    Returns:
        (findings, confidence) tuple where:
          findings: list of validated AgentFinding objects (may be empty)
          confidence: float in [0.0, 1.0]
            - 0.3 if output was malformed (triggers HITL)
            - mean(finding.confidence) if output was valid and has findings
            - 0.7 if output was valid but had zero findings (conservative)

    NEVER RAISES — always returns a safe result.
    """
    agent_name = agent_type

    # Case 1: LLM output was not valid JSON at all
    if not response.is_valid_json or not response.content:
        logger.warning(
            "guardrail_invalid_json | agent=%s | routing to HITL", agent_name
        )
        return [], 0.3

    content = response.content

    # Case 2: LLM returned a dict — extract the findings list from it
    raw_list = None
    if isinstance(content, dict):
        # Try known key names in order of likelihood
        for key in ("findings", "issues", "results", "problems", "violations"):
            if key in content and isinstance(content[key], list):
                raw_list = content[key]
                break
        if raw_list is None:
            # Last resort: take the first list value in the dict
            for val in content.values():
                if isinstance(val, list):
                    raw_list = val
                    break
        if raw_list is None:
            logger.warning(
                "guardrail_no_findings_list | agent=%s content_keys=%s",
                agent_name, list(content.keys()),
            )
            return [], 0.3

    # Case 3: LLM returned a list directly — accept it
    elif isinstance(content, list):
        raw_list = content

    else:
        logger.warning(
            "guardrail_unexpected_content_type | agent=%s type=%s",
            agent_name, type(content).__name__,
        )
        return [], 0.3

    # Now parse each element in the list into an AgentFinding
    findings: list[AgentFinding] = []
    for i, item in enumerate(raw_list):
        if not isinstance(item, dict):
            logger.debug(
                "guardrail_skip_non_dict | agent=%s index=%d type=%s",
                agent_name, i, type(item).__name__,
            )
            continue
        try:
            raw_finding = AgentFindingRaw(**item)
            finding = raw_finding.to_finding(agent_type=agent_name)
            findings.append(finding)
        except Exception as e:
            logger.warning(
                "guardrail_finding_parse_error | agent=%s index=%d error=%s",
                agent_name, i, str(e),
            )
            # Skip this finding — don't fail the whole review for one bad finding

    # Compute the overall agent confidence from the individual finding confidences
    if not findings:
        # Valid JSON, no findings -> conservative confidence (we might have missed something)
        confidence = 0.7
    else:
        # Mean confidence across all findings
        confidence = sum(f.confidence for f in findings) / len(findings)
        confidence = round(confidence, 4)

    return findings, confidence
