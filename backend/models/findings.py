# backend/models/findings.py
#
# The AgentFinding model — the structured output contract between the LLM and our system.
#
# WHAT THIS FILE IS:
# When a specialist agent (SecurityAgent, QualityAgent, etc.) calls an LLM,
# it gets text back. We need that text to be structured — specific fields,
# specific types, no surprises. This file defines that structure.
#
# THE LLM SIDE:
# We send the LLM a system prompt that says:
#   "You MUST respond with a JSON array. Each element must match this schema:
#    {severity, category, summary, file_path, line_start, line_end, suggestion, confidence}"
# The LLM tries to follow that schema. We then Pydantic-validate the response.
# If validation fails -> output guardrail kicks in (see base_agent.py).
#
# WHY PYDANTIC HERE (not TypedDict like state.py)?
# AgentFinding is data that LEAVES the agent layer:
#   - Stored in Postgres (Phase 6)
#   - Returned in API responses (Phase 2)
#   - Serialized to GitHub comment format (Phase 7)
# Pydantic gives us: field validation, JSON serialization, OpenAPI schema generation.
# TypedDict only gives us: type hints. Not enough for boundary data.
#
# WIKI PRINCIPLE (LLMOps-Essentials.md):
# "What is not in the context does not exist for the agent."
# -> The schema here is exactly what goes into the LLM's prompt as instructions.
# -> If we add a field here, we MUST update the system prompt to tell the LLM
#    to fill it. If the prompt doesn't mention it, the LLM won't return it.
#
# TWO MODELS:
# AgentFindingRaw  - what the LLM actually returns (all fields optional, loose types)
#                    Used for parsing. Forgives the LLM when it's slightly wrong.
# AgentFinding     - the validated, normalized finding (strict types, required fields)
#                    Used everywhere else in the system.
# We parse Raw first, then convert to AgentFinding. This two-step approach
# means we can handle LLM quirks (e.g., "HIGH" vs "high") without crashing.

from typing import Any
from pydantic import BaseModel, Field, field_validator

from backend.models.enums import FindingCategory, FindingSeverity


class AgentFindingRaw(BaseModel):
    """
    The raw, lenient version of a finding — used only for parsing LLM output.

    Every field is optional with a safe default.
    Why? Because LLMs sometimes:
      - Return "HIGH" instead of "high"
      - Omit optional fields like line_end or suggestion
      - Return confidence as "0.9" (string) instead of 0.9 (float)
    This model handles all those cases without crashing, so we can extract
    as much signal as possible from imperfect LLM output.

    After parsing: we call .to_finding() to convert to the strict AgentFinding.
    """

    # Who produced this finding. Filled by the agent class, not the LLM.
    # The LLM doesn't know its own name — we set this after parsing.
    agent_type: str = "unknown"

    # "critical", "high", "medium", "low"
    # Case-insensitive: "HIGH", "High", "high" all accepted via validator below.
    severity: str = "low"

    # "security", "quality", "test", "docs"
    category: str = "quality"

    # One sentence describing the issue. Required but has a safe default.
    # The LLM should always fill this — it's the most important field.
    summary: str = "No summary provided."

    # The file path where the issue was found. e.g. "src/auth.py"
    # May be None if the issue is about the PR as a whole (e.g. "no tests added").
    file_path: str | None = None

    # The starting line number of the issue. May be None for PR-level findings.
    line_start: int | None = None

    # The ending line number. Often the same as line_start for single-line issues.
    line_end: int | None = None

    # A concrete suggestion for how to fix the issue.
    # e.g. "Use parameterized queries: cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"
    # Optional — some findings (e.g. "missing tests") are hard to suggest a specific fix.
    suggestion: str | None = None

    # How confident the agent is in this specific finding (0.0 - 1.0).
    # "0.97" (string from LLM) is handled by the validator below.
    confidence: float | str = 0.5

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, v: Any) -> str:
        """
        Normalizes severity to lowercase.
        "HIGH", "High", "HIGH severity" all become "high".

        Called automatically by Pydantic before the field is set.
        mode="before" means: run this BEFORE Pydantic's type check.
        """
        if isinstance(v, str):
            # Strip whitespace, lowercase, take only first word
            # Handles: "HIGH severity", "High   ", "critical!"
            return v.strip().lower().split()[0].rstrip("!")
        return str(v).lower()

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, v: Any) -> str:
        """Normalizes category to lowercase."""
        if isinstance(v, str):
            return v.strip().lower().split()[0]
        return str(v).lower()

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any) -> float:
        """
        Coerces confidence to float.
        Handles: "0.9", "90%", 0.9, 1 all become valid floats.
        Clamps the result to [0.0, 1.0].
        """
        try:
            if isinstance(v, str):
                # Handle percentage strings: "90%" -> 0.9
                v = v.strip().rstrip("%")
                f = float(v)
                if f > 1.0:
                    f = f / 100.0  # "90" -> 0.9
                return max(0.0, min(1.0, f))
            return max(0.0, min(1.0, float(v)))
        except (ValueError, TypeError):
            return 0.5  # safe default when we can't parse

    def to_finding(self, agent_type: str) -> "AgentFinding":
        """
        Converts this raw (lenient) model to a strict AgentFinding.

        Args:
            agent_type: the agent that produced this finding ("security", "quality", etc.)
                        We pass this in because the LLM doesn't know its own type.

        Returns:
            AgentFinding with validated, typed fields.
            Falls back to safe defaults if any field is invalid.
        """
        # Validate severity: if LLM returned something unrecognized, fall back to LOW
        try:
            severity = FindingSeverity(self.severity)
        except ValueError:
            severity = FindingSeverity.LOW

        # Validate category: if unrecognized, use the agent's own category
        try:
            category = FindingCategory(self.category)
        except ValueError:
            # Map agent type to its default category
            _agent_to_category = {
                "security": FindingCategory.SECURITY,
                "quality":  FindingCategory.QUALITY,
                "test":     FindingCategory.TEST_COVERAGE,
                "docs":     FindingCategory.DOCUMENTATION,
            }
            category = _agent_to_category.get(agent_type, FindingCategory.QUALITY)

        confidence = self.confidence if isinstance(self.confidence, float) else 0.5

        return AgentFinding(
            agent_type=agent_type,
            severity=severity,
            category=category,
            summary=self.summary or "No summary provided.",
            file_path=self.file_path,
            line_start=self.line_start,
            line_end=self.line_end,
            suggestion=self.suggestion,
            confidence=confidence,
        )


class AgentFinding(BaseModel):
    """
    A single validated finding from a specialist agent.

    This is the strict version used everywhere after parsing:
      - Stored in the graph state (as dicts, via .model_dump())
      - Stored in Postgres (Phase 6)
      - Returned in API responses (Phase 2)
      - Posted as GitHub review comments (Phase 7)

    ALL FIELDS ARE TYPED AND REQUIRED (except the optional ones marked with None default).
    This is the real contract — no field can be the wrong type here.

    WHAT EACH FIELD MEANS:
    """

    # Which specialist agent produced this finding.
    # One of: "security", "quality", "test", "docs"
    agent_type: str

    # How serious this finding is.
    # CRITICAL -> immediate HITL (no auto-post)
    # HIGH     -> auto-post as REQUEST_CHANGES if confidence >= threshold
    # MEDIUM   -> auto-post as comment
    # LOW      -> auto-post as suggestion
    severity: FindingSeverity

    # What domain this finding belongs to.
    # Matches the agent type: security->SECURITY, quality->QUALITY, etc.
    category: FindingCategory

    # One-sentence human-readable description of the issue.
    # This is what gets posted as the GitHub review comment body.
    # EXAMPLE: "SQL injection risk: user input directly interpolated into query string."
    summary: str

    # The file path in the repo where the issue was found.
    # None for PR-level findings (e.g., "no test file added for src/payments.py").
    # EXAMPLE: "src/payments/processor.py"
    file_path: str | None = None

    # The line number where the issue starts.
    # None for file-level or PR-level findings.
    # Used to create an inline GitHub review comment on the specific line.
    line_start: int | None = None

    # The line number where the issue ends (for multi-line findings).
    # If the issue is a single line, line_end == line_start.
    line_end: int | None = None

    # A concrete, actionable suggestion for fixing the issue.
    # None if no specific fix can be suggested.
    # EXAMPLE: "Replace with parameterized query:
    #           cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))"
    suggestion: str | None = None

    # How confident the agent is in this specific finding (0.0 - 1.0).
    # High confidence (>= threshold) -> auto-post.
    # Low confidence (< threshold) -> route to HITL queue.
    # EXAMPLE: 0.97 for a clear hardcoded password, 0.55 for a possible timing issue.
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    def to_state_dict(self) -> dict:
        """
        Converts this finding to a plain dict for storing in LangGraph state.

        LangGraph state uses TypedDict with dicts (not Pydantic models) because
        LangGraph needs to serialize/deserialize state through its checkpointer.
        Pydantic models don't round-trip cleanly through that process.

        This method produces the dict format that AgentResultState.findings expects.
        The dict uses .value for enum fields (strings) not enum objects,
        because JSON/Redis serialization cannot handle Python enum objects.
        """
        return {
            "agent_type":  self.agent_type,
            "severity":    self.severity.value,       # "critical" not FindingSeverity.CRITICAL
            "category":    self.category.value,       # "security" not FindingCategory.SECURITY
            "summary":     self.summary,
            "file_path":   self.file_path,
            "line_start":  self.line_start,
            "line_end":    self.line_end,
            "suggestion":  self.suggestion,
            "confidence":  self.confidence,
        }
