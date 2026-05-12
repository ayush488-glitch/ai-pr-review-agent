"""
backend/security/__init__.py

Phase 11: Security Architecture
================================
Public API surface for the security module. Everything the rest of the
application needs lives here — importers should never reach into sub-modules.

Exports are grouped by concern:

  Masking         — PII / secret redaction before LLM calls
  Injection Guard — Prompt injection detection and sanitisation
  RBAC            — Role-based access control for API operators
  Threat Model    — Unified threat assessment entry-point

Wiki ref: Production-Hardening "Guards on the input side. Guards on the
output side. Guards around tool calls. Each guardrail is independent. Each
can block." (llmops-ai-agents/concepts/Production-Hardening.md)
"""

from backend.security.masking import (
    MaskingContext,
    MaskingPolicy,
    SensitiveKind,
    redact_text,
    unmask_text,
)
from backend.security.injection_guard import (
    InjectionPattern,
    InjectionResult,
    PromptInjectionDetector,
    check_pr_for_injection,
)
from backend.security.rbac import (
    Permission,
    PermissionDeniedError,
    RBACPolicy,
    Role,
    require_permission,
)
from backend.security.threat_model import (
    ThreatAssessment,
    ThreatScore,
    ThreatSeverity,
    ThreatVector,
    assess_pr_diff,
)

__all__ = [
    # Masking
    "MaskingContext",
    "MaskingPolicy",
    "SensitiveKind",
    "redact_text",
    "unmask_text",
    # Injection Guard
    "InjectionPattern",
    "InjectionResult",
    "PromptInjectionDetector",
    "check_pr_for_injection",
    # RBAC
    "Permission",
    "PermissionDeniedError",
    "RBACPolicy",
    "Role",
    "require_permission",
    # Threat Model
    "ThreatAssessment",
    "ThreatScore",
    "ThreatSeverity",
    "ThreatVector",
    "assess_pr_diff",
]
