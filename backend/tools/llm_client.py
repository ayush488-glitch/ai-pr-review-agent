# backend/tools/llm_client.py
#
# LLM API client — the ONLY place that calls OpenAI or Anthropic directly.
#
# WHY A WRAPPER?
# The same reason redis_client.py exists. Without this:
#   - SecurityAgent imports openai directly -> tightly coupled to OpenAI
#   - Swapping to Anthropic means touching every agent file
#   - Rate limit handling is duplicated across 4 agents
#   - Token counting happens inconsistently
#
# With this wrapper:
#   - Agents call: await llm_client.call(model="gpt-4o-mini", messages=[...])
#   - They never import openai or anthropic directly
#   - Rate limit + retry logic lives in ONE place
#   - Token counting is automatic and centralized
#
# PROVIDERS SUPPORTED:
#   OpenAI  -> gpt-4o, gpt-4o-mini, gpt-3.5-turbo
#   Anthropic -> claude-3-5-sonnet-20241022, claude-3-haiku-20240307
#
# STRUCTURED OUTPUT:
# Both providers support structured output (JSON mode).
# OpenAI:    response_format={"type": "json_object"} in the API call
# Anthropic: system prompt instruction + xml tags in response
# This client normalizes both into: returns parsed dict | raises LLMOutputError.
#
# RETRY STRATEGY (from Stability Patterns wiki):
# "Every external call is a potential stab-in-the-back."
# We retry on:
#   - 429 RateLimitError  -> wait for retry-after header, then retry (up to 3x)
#   - 500/502/503          -> exponential backoff, up to 3 retries
# We do NOT retry on:
#   - 400 BadRequest (prompt too long) -> caller needs to truncate
#   - 401 Unauthorized (bad key)       -> config error, no point retrying
#   - Content policy violation          -> log and return empty findings
#
# TOKEN COUNTING:
# Every call returns a LLMResponse with input_tokens and output_tokens.
# The model router uses this for cost attribution per agent (Phase 10 full tracing).

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
import openai

from backend.core.exceptions import AgentError
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 16 — fire-and-forget cost log writer.
#
# Called after each successful LLM call. Reads the active workflow context
# (set by base_agent.analyze) so we never have to thread a workflow_id arg
# through every retry path. Failures here are swallowed inside
# record_llm_call so a DB hiccup cannot break the review pipeline.
# ---------------------------------------------------------------------------
async def _persist_call_log(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_seconds: float,
    is_valid_json: bool,
) -> None:
    try:
        # Lazy imports to avoid circular import at module load time
        # (economics imports models, models import Base, Base depends on settings).
        from backend.economics import record_llm_call
        from backend.observability.workflow_context import get_workflow_context
        ctx = get_workflow_context()
        await record_llm_call(
            workflow_id=ctx.workflow_id,
            agent_type=ctx.agent_type,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_seconds * 1000.0,
            is_valid_json=is_valid_json,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("llm_call_log_helper_failed | error=%s", exc)


# ---------------------------------------------------------------------------
# Token cost table (USD per 1000 tokens, as of mid-2025)
# Used to compute estimated cost per call for observability.
# Phase 16 (Economics) will move this to a database for runtime updates.
# ---------------------------------------------------------------------------
_TOKEN_COSTS: dict[str, dict[str, float]] = {
    # OpenAI models
    "gpt-4o":            {"input": 0.005,   "output": 0.015},
    "gpt-4o-mini":       {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo":     {"input": 0.0005,  "output": 0.0015},
    # Anthropic models
    "claude-3-5-sonnet-20241022": {"input": 0.003,  "output": 0.015},
    "claude-3-haiku-20240307":    {"input": 0.00025,"output": 0.00125},
}


@dataclass
class LLMResponse:
    """
    The structured response from one LLM API call.

    WHY A DATACLASS AND NOT PYDANTIC?
    LLMResponse is internal to the tools layer — it never leaves the codebase
    (not stored, not serialized to JSON, not returned via API).
    Dataclass is lighter than Pydantic for pure in-process data.

    FIELDS:
    """
    # The parsed content from the LLM.
    # For JSON mode calls: a parsed dict.
    # For text mode calls: a string.
    content: dict | str

    # How many tokens the prompt used (input side).
    # Billed by the provider. Used for cost tracking.
    input_tokens: int

    # How many tokens the response used (output side).
    # Usually much smaller than input for structured output calls.
    output_tokens: int

    # Which model actually served this request.
    # May differ from requested model if provider falls back.
    model_used: str

    # Wall-clock seconds for this API call.
    # Used by Phase 10 (Observability) to identify slow models.
    latency_seconds: float

    # Estimated cost in USD for this call.
    # Computed from token counts + cost table above.
    estimated_cost_usd: float = 0.0

    # Whether the response content was valid JSON (for JSON mode calls).
    # False means output guardrail caught malformed output.
    is_valid_json: bool = True


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Estimates the cost of one LLM API call in USD.

    Returns 0.0 if the model is not in our cost table (unknown models).
    We never crash because of missing cost data — we just log it.

    HOW THE MATH WORKS:
    Cost table stores: price per 1000 tokens.
    Actual tokens used: input_tokens + output_tokens.
    Cost = (input_tokens / 1000) * input_price + (output_tokens / 1000) * output_price

    EXAMPLE:
    gpt-4o-mini, 1000 input tokens, 200 output tokens:
    = (1000/1000) * 0.00015 + (200/1000) * 0.0006
    = 0.00015 + 0.00012
    = $0.00027 per call
    """
    costs = _TOKEN_COSTS.get(model, {})
    if not costs:
        return 0.0
    input_cost  = (input_tokens  / 1000) * costs.get("input",  0.0)
    output_cost = (output_tokens / 1000) * costs.get("output", 0.0)
    return round(input_cost + output_cost, 8)


class LLMClient:
    """
    Async LLM client supporting OpenAI and Anthropic.

    LIFECYCLE:
    This client is stateless — it creates API client objects per call.
    No shared state between calls (thread-safe, safe to use concurrently).
    The API keys are read from Settings at call time.

    USAGE:
    From an agent:
      response = await llm_client.call_openai(
          model="gpt-4o-mini",
          messages=[{"role": "user", "content": "..."}],
          system_prompt="You are a code quality reviewer...",
      )
      findings = response.content  # already parsed dict
    """

    # Maximum number of retries on transient failures (rate limit, 5xx)
    MAX_RETRIES = 3

    # Base delay for exponential backoff in seconds.
    # Retry 1: 1s, Retry 2: 2s, Retry 3: 4s
    BASE_RETRY_DELAY = 1.0

    async def call_openai(
        self,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        json_mode: bool = True,
        max_tokens: int = 2048,
        api_key: str | None = None,
    ) -> LLMResponse:
        """
        Makes one call to the OpenAI API.

        Args:
            model:         OpenAI model name. e.g. "gpt-4o-mini"
            messages:      List of {role, content} dicts. role is "user" or "assistant".
                           Do NOT include the system message here — pass it as system_prompt.
            system_prompt: The agent's instructions. Sent as {"role": "system", "content": ...}
                           Kept separate from messages so we can update the prompt
                           without touching the message history.
            json_mode:     If True, tells OpenAI to return valid JSON (enforced server-side).
                           Always True for our structured output calls.
            max_tokens:    Maximum tokens in the response. Default 2048 is enough for
                           a list of 10-15 findings with summaries and suggestions.
            api_key:       OpenAI API key. If None, reads from OPENAI_API_KEY env var.

        Returns:
            LLMResponse with parsed content, token counts, latency, and cost.

        Raises:
            AgentError: if the call fails after all retries.
        """
        from backend.config import get_settings
        cfg = get_settings()
        key = api_key or cfg.openai_api_key

        client = openai.AsyncOpenAI(api_key=key)

        # Build the full messages list: system first, then user messages
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        # OpenAI JSON mode config
        # response_format={"type": "json_object"} tells OpenAI:
        #   "Your response MUST be valid JSON. If it is not, you will be penalized."
        # This is enforced server-side — OpenAI will repair the JSON if needed.
        response_format = {"type": "json_object"} if json_mode else {"type": "text"}

        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=full_messages,
                    response_format=response_format,
                    max_tokens=max_tokens,
                    temperature=0.1,  # low temperature = more deterministic, less hallucination
                )

                latency = time.monotonic() - start
                raw_content = response.choices[0].message.content or "{}"
                input_tokens  = response.usage.prompt_tokens
                output_tokens = response.usage.completion_tokens

                # Parse JSON
                is_valid_json = True
                try:
                    parsed = json.loads(raw_content)
                except json.JSONDecodeError:
                    logger.warning(
                        "openai_json_parse_failed | model=%s attempt=%d | "
                        "trying partial extraction",
                        model, attempt,
                    )
                    parsed = _try_extract_json(raw_content)
                    is_valid_json = bool(parsed)

                cost = _compute_cost(model, input_tokens, output_tokens)
                logger.info(
                    "openai_call | model=%s input_tokens=%d output_tokens=%d "
                    "latency=%.2fs cost=$%.6f",
                    model, input_tokens, output_tokens, latency, cost,
                )

                await _persist_call_log(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    latency_seconds=latency,
                    is_valid_json=is_valid_json,
                )

                return LLMResponse(
                    content=parsed,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model_used=model,
                    latency_seconds=round(latency, 3),
                    estimated_cost_usd=cost,
                    is_valid_json=is_valid_json,
                )

            except openai.RateLimitError as e:
                last_error = e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                # Check if the API told us how long to wait
                retry_after = getattr(e, "retry_after", delay)
                logger.warning(
                    "openai_rate_limit | model=%s attempt=%d/%d | waiting %.1fs",
                    model, attempt + 1, self.MAX_RETRIES, retry_after,
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(retry_after or delay)

            except openai.APIStatusError as e:
                last_error = e
                # 4xx errors (except 429) are not retryable
                if e.status_code and 400 <= e.status_code < 500 and e.status_code != 429:
                    logger.error(
                        "openai_client_error | model=%s status=%d | not retrying",
                        model, e.status_code,
                    )
                    raise AgentError(
                        f"OpenAI API client error {e.status_code}: {str(e)}",
                        agent_name=model,
                    ) from e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    "openai_server_error | model=%s attempt=%d/%d | waiting %.1fs",
                    model, attempt + 1, self.MAX_RETRIES, delay,
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(delay)

            except Exception as e:
                last_error = e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    "openai_unexpected_error | model=%s attempt=%d/%d error=%s",
                    model, attempt + 1, self.MAX_RETRIES, str(e),
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(delay)

        raise AgentError(
            f"OpenAI call failed after {self.MAX_RETRIES + 1} attempts: {last_error}",
            agent_name=model,
        ) from last_error

    async def call_anthropic(
        self,
        model: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        max_tokens: int = 2048,
        api_key: str | None = None,
    ) -> LLMResponse:
        """
        Makes one call to the Anthropic API.

        Anthropic does not have a native JSON mode like OpenAI.
        Instead, we:
          1. Add JSON formatting instructions to the system prompt
          2. Add a "prefill" technique: start the assistant's response with "{"
             so the model knows it must complete a JSON object
          3. Parse the response, falling back to partial extraction on failure

        Args:
            model:        Anthropic model name. e.g. "claude-3-5-sonnet-20241022"
            messages:     List of {role, content} dicts.
            system_prompt: Agent instructions + JSON format requirement.
            max_tokens:   Maximum tokens in the response.
            api_key:      Anthropic API key. If None, reads from ANTHROPIC_API_KEY env var.

        Returns:
            LLMResponse — same shape as call_openai().

        Raises:
            AgentError: if the call fails after all retries.
        """
        from backend.config import get_settings
        cfg = get_settings()
        key = api_key or cfg.anthropic_api_key

        client = anthropic.AsyncAnthropic(api_key=key)

        # Add JSON instruction to system prompt for Anthropic
        # (OpenAI handles this via response_format, Anthropic via prompt)
        json_system = (
            system_prompt
            + "\n\nCRITICAL: You MUST respond with ONLY valid JSON. "
            "No preamble, no explanation, no markdown code blocks. "
            "Start your response with { and end with }."
        )

        # Anthropic "prefill" technique:
        # Add an assistant turn that starts with "{" — this forces the model
        # to continue the JSON object rather than starting with prose.
        # This is the standard technique for reliable JSON from Anthropic models.
        prefill_messages = list(messages) + [{"role": "assistant", "content": "{"}]

        last_error: Exception | None = None
        for attempt in range(self.MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=json_system,
                    messages=prefill_messages,
                    temperature=0.1,
                )

                latency = time.monotonic() - start

                # Anthropic response: response.content is a list of ContentBlock.
                # We only care about the text block.
                raw_content = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        raw_content += block.text

                # Re-attach the prefill "{" that we put in the assistant turn
                # (Anthropic does NOT include the prefill in the response)
                full_json = "{" + raw_content

                input_tokens  = response.usage.input_tokens
                output_tokens = response.usage.output_tokens

                is_valid_json = True
                try:
                    parsed = json.loads(full_json)
                except json.JSONDecodeError:
                    logger.warning(
                        "anthropic_json_parse_failed | model=%s attempt=%d",
                        model, attempt,
                    )
                    parsed = _try_extract_json(full_json)
                    is_valid_json = bool(parsed)

                cost = _compute_cost(model, input_tokens, output_tokens)
                logger.info(
                    "anthropic_call | model=%s input_tokens=%d output_tokens=%d "
                    "latency=%.2fs cost=$%.6f",
                    model, input_tokens, output_tokens, latency, cost,
                )

                await _persist_call_log(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    latency_seconds=latency,
                    is_valid_json=is_valid_json,
                )

                return LLMResponse(
                    content=parsed,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    model_used=model,
                    latency_seconds=round(latency, 3),
                    estimated_cost_usd=cost,
                    is_valid_json=is_valid_json,
                )

            except anthropic.RateLimitError as e:
                last_error = e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    "anthropic_rate_limit | model=%s attempt=%d/%d | waiting %.1fs",
                    model, attempt + 1, self.MAX_RETRIES, delay,
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(delay)

            except anthropic.APIStatusError as e:
                last_error = e
                if e.status_code and 400 <= e.status_code < 500 and e.status_code != 429:
                    raise AgentError(
                        f"Anthropic API client error {e.status_code}: {str(e)}",
                        agent_name=model,
                    ) from e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    "anthropic_server_error | model=%s attempt=%d/%d | waiting %.1fs",
                    model, attempt + 1, self.MAX_RETRIES, delay,
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(delay)

            except Exception as e:
                last_error = e
                delay = self.BASE_RETRY_DELAY * (2 ** attempt)
                logger.warning(
                    "anthropic_unexpected | model=%s attempt=%d/%d error=%s",
                    model, attempt + 1, self.MAX_RETRIES, str(e),
                )
                if attempt < self.MAX_RETRIES:
                    await asyncio.sleep(delay)

        raise AgentError(
            f"Anthropic call failed after {self.MAX_RETRIES + 1} attempts: {last_error}",
            agent_name=model,
        ) from last_error


def _try_extract_json(text: str) -> dict | list:
    """
    Output guardrail: tries to extract valid JSON from partially malformed LLM output.

    LLMs sometimes wrap their JSON in markdown code blocks:
      ```json
      { "findings": [...] }
      ```
    Or add a short preamble:
      "Here are the findings:\n{ ... }"

    This function tries three strategies in order:
      1. Strip markdown code fences, parse
      2. Find the first { or [ and parse from there
      3. Give up and return empty dict

    WIKI PRINCIPLE (Production-Hardening.md):
    "Redact before block" — extract partial signal before giving up entirely.
    An empty dict triggers low confidence -> HITL, which is better than crashing.
    """
    if not text:
        return {}

    # Strategy 1: strip markdown code fences
    cleaned = text.strip()
    for fence in ["```json", "```JSON", "```"]:
        if cleaned.startswith(fence):
            cleaned = cleaned[len(fence):]
            break
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 2: find the first { or [ character and parse from there
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = cleaned.find(start_char)
        if start == -1:
            continue
        # Find the last matching closing character
        end = cleaned.rfind(end_char)
        if end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue

    # Strategy 3: give up
    logger.error("json_extraction_failed | could not extract JSON from LLM output")
    return {}


# Module-level singleton.
# Stateless — safe to share across all concurrent agent calls.
llm_client = LLMClient()
