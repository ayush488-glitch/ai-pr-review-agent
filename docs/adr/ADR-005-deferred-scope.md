# ADR-005 — Deferred Scope (Ph15, Ph17, Ph20)

Status: Accepted
Date: 2026-05-15
Related: ADR-001, ADR-002, ADR-003, ADR-004; `docs/FUTURE_WORK.md`

## Context

The roadmap nominally runs through Phase 20. As of this ADR, the v1
demo is functionally complete: webhook → 4 agents → aggregator →
GitHub post → DB persist → /reviews → /economics. End-to-end verified
on PR #8 of `ayush488-glitch/test-pr-review-demo`.

Three phases remain on the roadmap and three were chosen for explicit
deferral rather than partial implementation:

- Phase 15 — Governance & Compliance
- Phase 17 — Developer Experience polish
- Phase 20 — Continuous Learning

This ADR records *why* they are deferred, so the next person
(possibly future me) does not re-litigate the decision and does not
treat the existing scaffolding as half-finished work to be ripped out.

## Decision

Defer Phase 15, 17, and 20 in their entirety. Document the deferral
in `docs/FUTURE_WORK.md`. Keep — not delete — the partial hooks that
already exist in the tree, because removing them now would force
re-instrumentation later when the phase is actually picked up.

The hooks kept on purpose:

| Phase | Hook | Lives in |
|-------|------|----------|
| 15 | HITL audit row | `backend/api/hitl_router.py` |
| 15 | PII masking patterns | `backend/security/masking.py` (not wired to LLM boundary yet) |
| 15 | Spend-by-workflow drill-down | `backend/api/economics_router.py` |
| 17 | `/economics/workflow/{id}` endpoint | already returns; UI consumer missing |
| 17 | Tailwind dark-mode prep | `frontend/tailwind.config.ts` |
| 20 | `routing_advisor.recommend_model()` | logs only, doesn't act |
| 20 | `human_verdict` persisted alongside `agent_verdict` | training signal stored |
| 20 | `BudgetGuard` daily cap | enforced; per-review cap is advisory metric |

## Rationale

### Why defer at all?

The v1 demo is the bar. Every phase past v1 trades demo fitness for
operator ergonomics, compliance posture, or autonomous behavior. None
of those are required for the demo to be honest about what the system
does today.

Wiki ref: `pragmatic-programmer/concepts/Good-Enough-Software.md` —
shipping the smallest credible version and learning from production
beats shipping a maximalist version on a guess.

### Why not delete the half-built hooks?

Two arguments pulled in opposite directions:

1. YAGNI / Dead-Programs-Tell-No-Lies: code that isn't used is a lie.
   Delete it.
2. Reversibility: re-instrumenting cross-cutting concerns (audit logs,
   cost attribution, PII patterns) is expensive and error-prone.
   Adding the hook once at the right boundary is cheap; re-adding it
   six months later when nobody remembers the original boundary is
   not.

We resolved this by drawing a line:

- A hook that is **wired into the call path and emits data** stays —
  even if no consumer exists yet (e.g. `recommend_model()` logging,
  HITL audit row).
- A hook that is **only a stub with no caller** goes — empty
  `infra/` scaffolds, empty `prompts/` dirs at repo root, fallback
  branches whose precondition no longer holds (the FE 404 fallback
  collapsed in commit 035b92b).

Wiki refs:
- `pragmatic-programmer/concepts/Reversibility.md` — keep cheap
  reversible decisions, kill expensive irreversible ones.
- `pragmatic-programmer/concepts/Dead-Programs-Tell-No-Lies.md` —
  applied only to the second category.

### Why these three phases specifically?

**Phase 15** depends on a real compliance customer to define the
retention policy and the role taxonomy. Building it on assumptions
would mean rebuilding it later. Wiki ref:
`engineering-mindset/design-thinking.md` — design follows constraints,
and the constraints aren't here yet.

**Phase 17** is polish on a working dashboard. The marginal value of
each polish item (filters, shortcuts, dark mode) is real but small.
Defer until usage signals tell us *which* polish item users actually
miss.

**Phase 20** requires the cross-agent coordinator and a labelled
calibration set large enough to be statistically meaningful. Neither
exists today. Wiki ref: `mlops/llmops-ai-agents/feedback-loops.md` —
closing the loop on too few samples produces overconfident updates.
Don't.

## Consequences

Positive
    - The repo is honest about what's shipped and what isn't.
      `docs/FUTURE_WORK.md` is the single source of truth for deferred
      scope.
    - Future re-instrumentation cost is paid down (hooks are already
      in place where they belong).
    - The cleanup pass that produced this ADR removed only the
      genuinely dead code (empty scaffolds, resolved-TODO fallbacks,
      unused imports), not the speculative-but-instrumented hooks.

Negative
    - A reader of the codebase might mistake an unused hook for
      forgotten work. Mitigated by `docs/FUTURE_WORK.md` and by this
      ADR being linked from the README.
    - If priorities change and Phase 15/17/20 are abandoned outright
      (not just deferred), the kept hooks become genuine dead code.
      That's a cheap follow-up cleanup, not a present concern.

## Alternatives considered

A. Implement Phase 15/17/20 partially. Rejected — partial governance
   is worse than no governance (false sense of compliance), partial
   continuous learning is dangerous (acting on too few samples), and
   partial DX polish is invisible.

B. Delete every hook that isn't currently consumed. Rejected — see
   "Why not delete the half-built hooks" above. The cost of
   re-instrumentation outweighs the cost of carrying a few documented
   unused hooks.

C. Mark the phases "won't do" instead of "deferred". Rejected — the
   hooks exist precisely because we expect to pick these up. "Won't
   do" would be a lie about intent.
