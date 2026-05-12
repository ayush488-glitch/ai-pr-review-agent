"""
backend/security/masking.py

Phase 11: PII / Secret Redaction
==================================
Redacts sensitive values from text before it is sent to an LLM.
Design follows the opensre MaskingPolicy / MaskingContext pattern
(app/masking/) but scoped to the PR review domain.

Key design decisions:

1. REDACT BEFORE BLOCK (wiki: Production-Hardening "Redact before block —
   apply redactions first"). We replace sensitive values with stable
   placeholders like <API_KEY_0> rather than refusing to process the text.
   This lets the LLM still reason about the diff without seeing raw secrets.

2. STABLE PLACEHOLDER MAP — The same original value always gets the same
   placeholder within one review session (MaskingContext._reverse_map).
   This is critical so the LLM can correlate "API_KEY_0 is used on line 3
   AND line 47" without the values being exposed.

3. ENV-VAR CONFIGURABLE — MaskingPolicy.from_env() reads
   PR_REVIEW_MASK_ENABLED, PR_REVIEW_MASK_KINDS, PR_REVIEW_MASK_EXTRA_REGEX
   so ops teams can tune redaction at deploy time without code changes.

4. OVERLAP RESOLUTION — When two patterns match overlapping spans, the
   longer (earlier) match wins. This prevents partial masking that could
   leak fragments of a secret. Borrowed from opensre _resolve_overlaps.

5. NO PII IN LOGS — MaskingContext is designed to be created once per
   review job and carried through the review pipeline. Callers should
   redact BEFORE logging any diff content. The placeholder map itself is
   safe to log (it contains only placeholder keys, not original values
   at the key level).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sensitive kind vocabulary
# ---------------------------------------------------------------------------

# Wiki ref: opensre IdentifierKind pattern — a Literal type so mypy can
# exhaustively check that all kinds have a corresponding detector.
SensitiveKind = Literal[
    "api_key",
    "token",
    "password",
    "private_key",
    "email",
    "ip_address",
    "ssn",
    "credit_card",
]

ALL_KINDS: tuple[SensitiveKind, ...] = (
    "api_key",
    "token",
    "password",
    "private_key",
    "email",
    "ip_address",
    "ssn",
    "credit_card",
)

# ---------------------------------------------------------------------------
# Built-in regex detectors
# ---------------------------------------------------------------------------
# Each pattern must have at most one capturing group. If a group is present
# we mask only the captured value (contextual match). If there is no group
# we mask the entire match.

_BUILTIN_PATTERNS: dict[str, re.Pattern[str]] = {
    # Generic high-entropy API keys / tokens (32–64 hex chars or base64-ish)
    "api_key": re.compile(
        r"\b(?:api[_-]?key|apikey|api[_-]?secret)[=:\s\"'`]+([A-Za-z0-9+/=_\-]{20,64})\b",
        re.IGNORECASE,
    ),
    # Bearer / Authorization header tokens
    "token": re.compile(
        r"\b(?:bearer|token|access[_-]token|auth[_-]token|secret[_-]token)"
        r"[=:\s\"'`]+([A-Za-z0-9+/=_\-\.]{20,200})",
        re.IGNORECASE,
    ),
    # Password assignments in config / code
    "password": re.compile(
        r"\b(?:password|passwd|pwd)[=:\s\"'`]+([^\s\"'`]{8,64})",
        re.IGNORECASE,
    ),
    # PEM private key headers (multi-line markers count as one match)
    "private_key": re.compile(
        r"(-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
        re.IGNORECASE,
    ),
    # Email addresses
    "email": re.compile(
        r"\b([\w.+-]+@[\w-]+\.[\w.]{2,})\b",
    ),
    # IPv4 addresses (non-private ranges still worth masking in public contexts)
    "ip_address": re.compile(
        r"\b((?:25[0-5]|2[0-4]\d|[01]?\d?\d)"
        r"(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d?\d)){3})\b",
    ),
    # US Social Security Numbers
    "ssn": re.compile(
        r"\b(\d{3}-\d{2}-\d{4})\b",
    ),
    # Credit / debit card numbers (Luhn-valid check is too expensive; regex ok)
    "credit_card": re.compile(
        r"\b((?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12}))\b",
    ),
}


# ---------------------------------------------------------------------------
# MaskingPolicy
# ---------------------------------------------------------------------------

class MaskingPolicy:
    """
    Configures what gets masked.

    Instantiate via from_env() in production code so that deployment-time
    env vars are honoured without restarting the server.

    Attributes:
        enabled         -- Master switch. When False, redact() is a no-op.
        kinds           -- Which SensitiveKinds to detect and mask.
        extra_patterns  -- label -> regex_string: additional patterns injected
                           by the deployer (e.g. internal service names).
    """

    #: Env-var names (same naming convention as opensre)
    _ENV_ENABLED = "PR_REVIEW_MASK_ENABLED"
    _ENV_KINDS = "PR_REVIEW_MASK_KINDS"        # comma-separated SensitiveKind names
    _ENV_EXTRA = "PR_REVIEW_MASK_EXTRA_REGEX"  # JSON {"label": "regex", ...}

    def __init__(
        self,
        *,
        enabled: bool = True,
        kinds: tuple[SensitiveKind, ...] = ALL_KINDS,
        extra_patterns: dict[str, str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.kinds = kinds
        # Validate all extra regexes at construction time so we fail loudly
        # here rather than silently at runtime inside a review.
        self.extra_patterns: dict[str, str] = {}
        for label, pattern in (extra_patterns or {}).items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"MaskingPolicy extra_patterns[{label!r}] is invalid: {exc}"
                ) from exc
            self.extra_patterns[label] = pattern

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MaskingPolicy:
        """Build a policy from environment variables (or an injected dict)."""
        source = env if env is not None else dict(os.environ)

        enabled = _parse_bool(source.get(cls._ENV_ENABLED, "true"))

        kinds_raw = source.get(cls._ENV_KINDS, "")
        if kinds_raw.strip():
            parsed_kinds: list[SensitiveKind] = []
            for k in kinds_raw.split(","):
                k = k.strip()
                if k in ALL_KINDS:
                    parsed_kinds.append(k)  # type: ignore[arg-type]
                else:
                    logger.warning("[masking] ignoring unknown kind: %r", k)
            kinds = tuple(parsed_kinds) if parsed_kinds else ALL_KINDS
        else:
            kinds = ALL_KINDS

        extra_raw = source.get(cls._ENV_EXTRA, "")
        extra_patterns: dict[str, str] = {}
        if extra_raw.strip():
            try:
                extra_patterns = json.loads(extra_raw)
            except json.JSONDecodeError:
                logger.warning("[masking] PR_REVIEW_MASK_EXTRA_REGEX is not valid JSON — ignoring")

        return cls(enabled=enabled, kinds=kinds, extra_patterns=extra_patterns)

    def is_kind_enabled(self, kind: str) -> bool:
        return kind in self.kinds


# ---------------------------------------------------------------------------
# DetectedSpan (internal)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class _DetectedSpan:
    """Internal: a matched sensitive span in text."""
    start: int
    end: int
    kind: str
    value: str  # the actual sensitive value (captured group or full match)


def _find_spans(text: str, policy: MaskingPolicy) -> list[_DetectedSpan]:
    """
    Find all sensitive spans in text.

    Returns spans sorted by (start, -length) so that earlier longer matches
    appear first, enabling the overlap resolver to prefer them.
    """
    spans: list[_DetectedSpan] = []

    for kind, pattern in _BUILTIN_PATTERNS.items():
        if not policy.is_kind_enabled(kind):
            continue
        for m in pattern.finditer(text):
            # If the pattern has a capturing group, mask only the group value
            if m.lastindex:
                start, end = m.span(1)
                value = m.group(1)
            else:
                start, end = m.span()
                value = m.group()
            spans.append(_DetectedSpan(start=start, end=end, kind=kind, value=value))

    # Extra patterns from policy
    for label, pattern_str in policy.extra_patterns.items():
        for m in re.finditer(pattern_str, text):
            if m.lastindex:
                start, end = m.span(1)
                value = m.group(1)
            else:
                start, end = m.span()
                value = m.group()
            spans.append(_DetectedSpan(start=start, end=end, kind=label, value=value))

    return _resolve_overlaps(spans)


def _resolve_overlaps(spans: list[_DetectedSpan]) -> list[_DetectedSpan]:
    """
    Remove overlapping spans, keeping the longer earlier-starting match.

    Wiki ref: opensre _resolve_overlaps — "longer earlier match wins so
    we never corrupt the output."
    """
    # Sort by start ascending, then by length descending (longer wins)
    sorted_spans = sorted(spans, key=lambda s: (s.start, -(s.end - s.start)))
    resolved: list[_DetectedSpan] = []
    last_end = -1
    for span in sorted_spans:
        if span.start >= last_end:
            resolved.append(span)
            last_end = span.end
        # else: overlaps with previous winner — skip
    return resolved


# ---------------------------------------------------------------------------
# MaskingContext
# ---------------------------------------------------------------------------

class MaskingContext:
    """
    Stable masking state for one review session.

    The same original value always maps to the same placeholder within this
    context. This is important for correlated findings: if the LLM sees
    <API_KEY_0> twice it knows it's the same secret.

    Placeholder format: <KIND_N> e.g. <API_KEY_0>, <EMAIL_1>

    Usage:
        ctx = MaskingContext(policy)
        masked_text, placeholder_map = ctx.redact(raw_diff)
        # ... send masked_text to LLM ...
        original_text = ctx.unmask(llm_output)
    """

    def __init__(self, policy: MaskingPolicy) -> None:
        self.policy = policy
        # placeholder -> original value (for unmask)
        self._placeholder_map: dict[str, str] = {}
        # original value -> placeholder (for reuse across calls)
        self._reverse_map: dict[str, str] = {}
        # running counter per kind
        self._counters: dict[str, int] = {}

    def _new_placeholder(self, kind: str) -> str:
        idx = self._counters.get(kind, 0)
        self._counters[kind] = idx + 1
        return f"<{kind.upper()}_{idx}>"

    def _get_or_create_placeholder(self, kind: str, value: str) -> str:
        """Return existing placeholder for value or create a new one."""
        if value in self._reverse_map:
            return self._reverse_map[value]
        placeholder = self._new_placeholder(kind)
        self._placeholder_map[placeholder] = value
        self._reverse_map[value] = placeholder
        return placeholder

    def redact(self, text: str) -> tuple[str, dict[str, str]]:
        """
        Redact all detected sensitive values in text.

        Returns (masked_text, placeholder_map) where placeholder_map maps
        placeholder -> original_value. The map is a snapshot — it is safe to
        log (placeholders only, no raw secrets at the key level).
        """
        if not self.policy.enabled or not text:
            return text, {}

        spans = _find_spans(text, self.policy)
        if not spans:
            return text, {}

        # Build the masked string by walking spans left to right
        parts: list[str] = []
        cursor = 0
        for span in spans:
            parts.append(text[cursor:span.start])
            placeholder = self._get_or_create_placeholder(span.kind, span.value)
            parts.append(placeholder)
            cursor = span.end
        parts.append(text[cursor:])

        return "".join(parts), dict(self._placeholder_map)

    def unmask(self, text: str) -> str:
        """
        Replace all placeholders in text with their original values.

        Used to restore original content when the masked text has been
        processed and we need the real values back (e.g. storing the original
        finding context). Never called before LLM calls — only after.
        """
        result = text
        # Sort by length descending so longer placeholders are not partially
        # matched by shorter ones (e.g. <API_KEY_10> vs <API_KEY_1>)
        for placeholder, original in sorted(
            self._placeholder_map.items(), key=lambda kv: -len(kv[0])
        ):
            result = result.replace(placeholder, original)
        return result

    @property
    def placeholder_map(self) -> dict[str, str]:
        """Read-only snapshot of the current placeholder -> original map."""
        return dict(self._placeholder_map)


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------

def redact_text(
    text: str,
    *,
    policy: MaskingPolicy | None = None,
) -> tuple[str, dict[str, str]]:
    """
    Stateless convenience wrapper: redact a single string.

    Returns (masked_text, placeholder_map). Does NOT allow unmask because
    there is no persistent MaskingContext. Use MaskingContext directly for
    round-trip redact/unmask within a review session.
    """
    _policy = policy or MaskingPolicy.from_env()
    ctx = MaskingContext(_policy)
    return ctx.redact(text)


def unmask_text(text: str, placeholder_map: dict[str, str]) -> str:
    """
    Stateless convenience wrapper: unmask using a provided placeholder_map.
    """
    result = text
    for placeholder, original in sorted(
        placeholder_map.items(), key=lambda kv: -len(kv[0])
    ):
        result = result.replace(placeholder, original)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_bool(value: str) -> bool:
    """Parse a string to bool. Defaults to True for empty / unset."""
    return value.strip().lower() not in {"false", "0", "no", "off", ""}
