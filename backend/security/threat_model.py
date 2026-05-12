"""
backend/security/threat_model.py

Phase 11: Unified Threat Assessment Entry-Point
=================================================
This module is the single place where all security checks are composed into
a structured ThreatAssessment. The webhook receiver calls assess_pr_diff()
before any PR content reaches the agents. The result is attached to the
PRReviewState and written to the AuditLogger.

Architecture: SEPARATE GATE BEFORE THE AGENTS
----------------------------------------------
Wiki: "Guards on the input side. Guards on the output side. Guards around
tool calls. Each guardrail is independent. Each can block."
(llmops-ai-agents/concepts/Production-Hardening.md)

This module implements the INPUT GATE. It is NOT embedded in any agent
prompt. It runs synchronously before the LangGraph workflow begins.

Threat vectors we model:
  PROMPT_INJECTION  -- Adversarial instructions in title, body, or diff
  PII_IN_DIFF       -- Personally Identifiable Information exposed in diff
  SECRETS_IN_DIFF   -- API keys, tokens, passwords committed in diff
  OVERSIZED_PAYLOAD -- Diff exceeds configurable byte limit
  MALFORMED_WEBHOOK -- Payload structural issues (checked upstream, reported here)
  API_KEY_EXPOSURE  -- Any credential pattern in diff content

RecommendedAction mapping:
  ALLOW   -- No threats or LOW severity only
  WARN    -- MEDIUM threats — allow but log prominently
  SANITISE -- HIGH threats — strip injections, redact PII, then continue
  BLOCK   -- CRITICAL threats — do not process, return error to webhook

Note: assess_pr_diff() returns a ThreatAssessment but does NOT enforce the
action. Enforcement lives in the webhook receiver (router.py Phase 3), which
reads recommended_action and decides whether to reject the job. This keeps
the threat model testable in isolation.

Wiki ref: Financial-Security-Controls "Don't try to prove code is correct —
just block known-bad patterns." and "Audit trail matters: log requirement,
generated artifact, validation results, approver, timestamp."
(llmops-ai-agents/concepts/Financial-Security-Controls.md)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

# Python 3.10 StrEnum shim
try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass

from backend.security.masking import MaskingContext, MaskingPolicy, SensitiveKind
from backend.security.injection_guard import (
    InjectionSeverity,
    PromptInjectionDetector,
)

# ---------------------------------------------------------------------------
# Threat vocabulary
# ---------------------------------------------------------------------------

class ThreatVector(StrEnum):
    """The category of threat detected."""
    PROMPT_INJECTION = "prompt_injection"
    PII_IN_DIFF = "pii_in_diff"
    SECRETS_IN_DIFF = "secrets_in_diff"
    OVERSIZED_PAYLOAD = "oversized_payload"
    MALFORMED_WEBHOOK = "malformed_webhook"
    API_KEY_EXPOSURE = "api_key_exposure"


class ThreatSeverity(StrEnum):
    """How severe the threat is. Maps to RecommendedAction below."""
    LOW = "low"        # ALLOW  — noteworthy but not dangerous
    MEDIUM = "medium"  # WARN   — log prominently, proceed
    HIGH = "high"      # SANITISE — strip/redact, then proceed
    CRITICAL = "critical"  # BLOCK — refuse processing


class RecommendedAction(StrEnum):
    """
    What the caller should do based on the overall ThreatAssessment.

    ALLOW    -- proceed normally
    WARN     -- proceed but emit a warning log
    SANITISE -- use sanitized/redacted content for agent processing
    BLOCK    -- reject the request with 400/422
    """
    ALLOW = "allow"
    WARN = "warn"
    SANITISE = "sanitise"
    BLOCK = "block"


# Severity -> RecommendedAction mapping
_SEVERITY_TO_ACTION: dict[ThreatSeverity, RecommendedAction] = {
    ThreatSeverity.LOW: RecommendedAction.ALLOW,
    ThreatSeverity.MEDIUM: RecommendedAction.WARN,
    ThreatSeverity.HIGH: RecommendedAction.SANITISE,
    ThreatSeverity.CRITICAL: RecommendedAction.BLOCK,
}

# Ordering for max() comparison
_SEVERITY_ORDER: dict[ThreatSeverity, int] = {
    ThreatSeverity.LOW: 0,
    ThreatSeverity.MEDIUM: 1,
    ThreatSeverity.HIGH: 2,
    ThreatSeverity.CRITICAL: 3,
}

# Injection severity -> ThreatSeverity translation
_INJECTION_TO_THREAT_SEVERITY: dict[InjectionSeverity, ThreatSeverity] = {
    InjectionSeverity.LOW: ThreatSeverity.LOW,
    InjectionSeverity.MEDIUM: ThreatSeverity.MEDIUM,
    InjectionSeverity.HIGH: ThreatSeverity.HIGH,
    InjectionSeverity.CRITICAL: ThreatSeverity.CRITICAL,
}


# ---------------------------------------------------------------------------
# ThreatScore
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ThreatScore:
    """
    One detected threat instance. Frozen so it can be safely stored in
    the PRReviewState and serialised to the audit trail.
    """
    vector: ThreatVector
    severity: ThreatSeverity
    description: str
    evidence_snippet: str = ""  # Short excerpt, already redacted if sensitive


# ---------------------------------------------------------------------------
# ThreatAssessment
# ---------------------------------------------------------------------------

@dataclass
class ThreatAssessment:
    """
    Structured result of assess_pr_diff(). Attached to PRReviewState.

    Properties:
        scores               -- All individual threat scores found.
        overall_severity     -- Maximum severity across all scores (None if empty).
        recommended_action   -- Derived from overall_severity.
        assessed_at          -- UTC timestamp.
        redacted_diff        -- Diff with secrets and PII replaced by placeholders.
        placeholder_map      -- Placeholder -> original mapping for unmask.
    """
    scores: list[ThreatScore] = field(default_factory=list)
    overall_severity: ThreatSeverity | None = None
    recommended_action: RecommendedAction = RecommendedAction.ALLOW
    assessed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    redacted_diff: str = ""         # Safe diff for agent consumption
    placeholder_map: dict[str, str] = field(default_factory=dict)

    @property
    def is_safe(self) -> bool:
        """True when no threats were found or all are LOW severity."""
        if not self.scores:
            return True
        return self.recommended_action in (
            RecommendedAction.ALLOW,
            RecommendedAction.WARN,
        )

    @property
    def has_critical_threat(self) -> bool:
        return self.overall_severity == ThreatSeverity.CRITICAL

    def to_audit_dict(self) -> dict:
        """
        Serialisable representation for AuditLogger.

        Wiki ref: Write-Once Audit Trail — log requirement, validation
        results, system version, and timestamp.
        """
        return {
            "overall_severity": self.overall_severity,
            "recommended_action": self.recommended_action,
            "assessed_at": self.assessed_at.isoformat(),
            "threat_count": len(self.scores),
            "vectors": [
                {
                    "vector": s.vector,
                    "severity": s.severity,
                    "description": s.description,
                    "evidence_snippet": s.evidence_snippet,
                }
                for s in self.scores
            ],
        }


# ---------------------------------------------------------------------------
# Secret patterns (secrets specifically in diffs, separate from masking)
# ---------------------------------------------------------------------------
# These are patterns that are HIGH risk in any diff — we always flag them
# regardless of the masking policy.

_SECRET_DIFF_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hardcoded_aws_key", re.compile(
        r"\bAKIA[0-9A-Z]{16}\b",
    )),
    ("hardcoded_github_token", re.compile(
        r"\bghp_[A-Za-z0-9]{36}\b",
    )),
    ("hardcoded_jwt", re.compile(
        r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b",
    )),
    ("generic_high_entropy_secret", re.compile(
        r"(?:secret|password|passwd|token|key|credential)[=:\s\"'`]+([A-Za-z0-9+/]{32,})",
        re.IGNORECASE,
    )),
    ("private_key_header", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    )),
    ("base64_encoded_secret", re.compile(
        # Label followed by long base64 string — commonly env var secrets
        r"(?:SECRET|TOKEN|KEY|PASS)[_A-Z]*\s*[=:]\s*([A-Za-z0-9+/]{40,}={0,2})",
        re.IGNORECASE,
    )),
]


# ---------------------------------------------------------------------------
# Default configuration constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_DIFF_BYTES = 1_000_000   # 1 MB — flag diffs larger than this
WARN_DIFF_BYTES = 500_000            # 500 KB — warn but allow


# ---------------------------------------------------------------------------
# Core assessment function
# ---------------------------------------------------------------------------

def assess_pr_diff(
    title: str = "",
    body: str = "",
    diff: str = "",
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    masking_policy: MaskingPolicy | None = None,
) -> ThreatAssessment:
    """
    Run all security checks on a PR and return a ThreatAssessment.

    Checks (in order):
      1. Payload size — oversized diffs can cause token cost explosions and
         are often a sign of generated or obfuscated content.
      2. Prompt injection — scans title, body, and diff.
      3. Secrets in diff — high-value patterns (AWS keys, GitHub tokens, etc.)
         that should block immediately.
      4. PII in diff — via the MaskingContext (email, SSN, credit cards, etc.)

    Args:
        title            -- PR title string.
        body             -- PR body / description string.
        diff             -- Unified diff string.
        max_diff_bytes   -- Diffs larger than this get a CRITICAL score.
        masking_policy   -- Override the default MaskingPolicy.from_env().
                            Pass MaskingPolicy(enabled=False) to skip PII scan.

    Returns:
        ThreatAssessment with redacted_diff safe for agent consumption.
    """
    scores: list[ThreatScore] = []

    # ------------------------------------------------------------------
    # 1. Payload size check
    # ------------------------------------------------------------------
    diff_bytes = len(diff.encode("utf-8"))
    if diff_bytes > max_diff_bytes:
        scores.append(ThreatScore(
            vector=ThreatVector.OVERSIZED_PAYLOAD,
            severity=ThreatSeverity.CRITICAL,
            description=(
                f"Diff size {diff_bytes:,} bytes exceeds hard limit "
                f"{max_diff_bytes:,} bytes. Processing refused."
            ),
            evidence_snippet=f"diff_size={diff_bytes}",
        ))
    elif diff_bytes > WARN_DIFF_BYTES:
        scores.append(ThreatScore(
            vector=ThreatVector.OVERSIZED_PAYLOAD,
            severity=ThreatSeverity.MEDIUM,
            description=(
                f"Diff size {diff_bytes:,} bytes is large (warning threshold: "
                f"{WARN_DIFF_BYTES:,} bytes). Token costs may be high."
            ),
            evidence_snippet=f"diff_size={diff_bytes}",
        ))

    # ------------------------------------------------------------------
    # 2. Prompt injection check
    # ------------------------------------------------------------------
    detector = PromptInjectionDetector()
    injection_result = detector.check_pr(title=title, body=body, diff=diff)
    if injection_result.detected and injection_result.threat_level is not None:
        threat_sev = _INJECTION_TO_THREAT_SEVERITY[injection_result.threat_level]
        scores.append(ThreatScore(
            vector=ThreatVector.PROMPT_INJECTION,
            severity=threat_sev,
            description=(
                f"Prompt injection detected. Matched patterns: "
                f"{', '.join(injection_result.matched_patterns)}"
            ),
            evidence_snippet=_safe_snippet(injection_result.evidence_snippets),
        ))

    # ------------------------------------------------------------------
    # 3. Secrets in diff
    # ------------------------------------------------------------------
    if diff:
        for pattern_name, pattern in _SECRET_DIFF_PATTERNS:
            if pattern.search(diff):
                # This is always HIGH or CRITICAL — committed secrets are severe
                scores.append(ThreatScore(
                    vector=ThreatVector.SECRETS_IN_DIFF,
                    severity=ThreatSeverity.HIGH,
                    description=(
                        f"Potential secret detected in diff ({pattern_name}). "
                        "The value will be redacted before agent processing."
                    ),
                    evidence_snippet=f"pattern={pattern_name}",
                ))

    # ------------------------------------------------------------------
    # 4. PII in diff via MaskingContext
    # ------------------------------------------------------------------
    policy = masking_policy or MaskingPolicy.from_env()
    masking_ctx = MaskingContext(policy)
    redacted_diff, placeholder_map = masking_ctx.redact(diff)

    if placeholder_map:
        # Classify what was found
        kinds_found: set[str] = set()
        for placeholder in placeholder_map:
            # Placeholder format: <KIND_N>
            kind_part = placeholder.strip("<>").rsplit("_", 1)[0].lower()
            kinds_found.add(kind_part)

        pii_kinds = kinds_found & {"email", "ssn", "credit_card", "ip_address"}
        secret_kinds = kinds_found & {"api_key", "token", "password", "private_key"}

        if pii_kinds:
            scores.append(ThreatScore(
                vector=ThreatVector.PII_IN_DIFF,
                severity=ThreatSeverity.HIGH,
                description=(
                    f"PII detected and redacted from diff: {', '.join(sorted(pii_kinds))}. "
                    "Agents will see placeholders, not raw values."
                ),
                evidence_snippet=f"pii_kinds={sorted(pii_kinds)}",
            ))
        if secret_kinds:
            scores.append(ThreatScore(
                vector=ThreatVector.API_KEY_EXPOSURE,
                severity=ThreatSeverity.HIGH,
                description=(
                    f"Credential patterns detected and redacted: {', '.join(sorted(secret_kinds))}. "
                    "Agents will see placeholders, not raw values."
                ),
                evidence_snippet=f"secret_kinds={sorted(secret_kinds)}",
            ))

    # ------------------------------------------------------------------
    # Compute overall severity and recommended action
    # ------------------------------------------------------------------
    overall_severity = _max_severity(scores)
    recommended_action = (
        _SEVERITY_TO_ACTION[overall_severity]
        if overall_severity is not None
        else RecommendedAction.ALLOW
    )

    return ThreatAssessment(
        scores=scores,
        overall_severity=overall_severity,
        recommended_action=recommended_action,
        redacted_diff=redacted_diff,
        placeholder_map=placeholder_map,
    )


def add_malformed_webhook_threat(
    assessment: ThreatAssessment,
    reason: str,
) -> ThreatAssessment:
    """
    Append a MALFORMED_WEBHOOK threat score to an existing ThreatAssessment.

    Called by the webhook receiver when structural validation fails (e.g.
    missing required fields, invalid event type). Malformed webhooks are
    always HIGH severity — they should not proceed to the workflow.
    """
    new_score = ThreatScore(
        vector=ThreatVector.MALFORMED_WEBHOOK,
        severity=ThreatSeverity.HIGH,
        description=f"Webhook payload failed structural validation: {reason}",
        evidence_snippet=reason[:120],
    )
    updated_scores = assessment.scores + [new_score]
    overall = _max_severity(updated_scores)
    action = _SEVERITY_TO_ACTION[overall] if overall else RecommendedAction.ALLOW
    assessment.scores = updated_scores
    assessment.overall_severity = overall
    assessment.recommended_action = action
    return assessment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _max_severity(
    scores: Sequence[ThreatScore],
) -> ThreatSeverity | None:
    """Return the highest severity across all scores, or None if empty."""
    if not scores:
        return None
    return max(scores, key=lambda s: _SEVERITY_ORDER[s.severity]).severity


def _safe_snippet(evidence_snippets: list[str], max_length: int = 200) -> str:
    """Join evidence snippets into a short safe string."""
    combined = " | ".join(evidence_snippets)
    if len(combined) > max_length:
        return combined[:max_length] + "..."
    return combined
