# backend/economics/routing_advisor.py
#
# Phase 16 — Advisory model recommendations.
#
# This module produces RECOMMENDATIONS, never automatic switches. Phase 20
# (Continuous Learning) will close the feedback loop: it consumes
# (recommendation, actual_outcome) pairs to learn when to actually switch.
# For now we just compute "what would we have spent on a cheaper model?"
# so the dashboard can show an opportunity-cost number.
#
# Pricing tiers mirror the wiki's complexity-tiered routing pattern:
#   simple   -> gpt-4o-mini
#   medium   -> claude-haiku
#   complex  -> claude-sonnet
#
# (LLMOps-Essentials.md, "Cost Control":
#  "Use cheap models for simple tasks, expensive models only when needed.
#   This alone can cut costs by 50-70%.")

from __future__ import annotations

from typing import Literal

TaskComplexity = Literal["simple", "medium", "complex"]

_RECOMMENDATIONS: dict[str, str] = {
    "simple":  "gpt-4o-mini",
    "medium":  "claude-3-haiku-20240307",
    "complex": "claude-3-5-sonnet-20241022",
}

# Mirror of llm_client._TOKEN_COSTS — kept here intentionally so this module
# does not have to import from tools/ (avoid layering inversion).
_PRICES: dict[str, dict[str, float]] = {
    "gpt-4o":                    {"input": 0.005,   "output": 0.015},
    "gpt-4o-mini":               {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo":             {"input": 0.0005,  "output": 0.0015},
    "claude-3-5-sonnet-20241022":{"input": 0.003,   "output": 0.015},
    "claude-3-haiku-20240307":   {"input": 0.00025, "output": 0.00125},
}


def recommend_model(complexity: TaskComplexity) -> str:
    """Recommend a cheap-tier model for a task complexity level."""
    return _RECOMMENDATIONS.get(complexity, "gpt-4o-mini")


def _cost(model: str, in_tok: int, out_tok: int) -> float:
    p = _PRICES.get(model)
    if not p:
        return 0.0
    return (in_tok / 1000.0) * p["input"] + (out_tok / 1000.0) * p["output"]


def would_have_saved(
    *,
    actual_model: str,
    cheaper_model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """
    Returns USD savings if `cheaper_model` had served this call instead of
    `actual_model`. Negative number means the "cheaper" model would have been
    more expensive (don't recommend it). Returns 0.0 for unknown models.
    """
    actual = _cost(actual_model, input_tokens, output_tokens)
    cheap = _cost(cheaper_model, input_tokens, output_tokens)
    if actual == 0.0 or cheap == 0.0:
        return 0.0
    return round(actual - cheap, 8)
