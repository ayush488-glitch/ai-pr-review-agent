# Future Work

Phases 15, 17, and 20 are intentionally deferred. This document captures
what already exists in the codebase that supports each phase, what is
missing, and which wiki sections govern the design when the phase is
picked up.

Project status as of this writing: Phases 0, 1, 3-14, 16, 18, 19 shipped
on the backend; Phase 2 (Next.js dashboard) shipped on the frontend.
End-to-end verified on PR #8 of `ayush488-glitch/test-pr-review-demo`.

---

## Phase 15 — Governance & Compliance

What it is
    Auditability, retention policy, PII redaction at the LLM boundary,
    role-based access, data export.

Half-built (already in the tree)
    - `backend/api/hitl_router.py` writes an audit row on every HITL
      decision (decided_by, decided_at, human_verdict, reason).
    - `backend/api/economics_router.py` provides spend-by-workflow
      drill-down (Phase 16) — the substrate for compliance reporting.
    - `backend/security/masking.py` already supports PII patterns; not
      yet wired into the LLM call path.
    - Single API key auth (`API_KEY` env var) — placeholder for RBAC.

Missing
    - Retention policy + scheduled purge job (use arq).
    - PII redaction enforced at `base_agent` LLM boundary, not optional.
    - `/api/v1/export` endpoint (signed URL, scoped by workflow).
    - Role-based access (replace single API key with JWT + roles).
    - Compliance log shipping (S3 / object storage with object-lock).

Wiki references when picked up
    - `security-engineering/threat-modeling.md`
    - `security-engineering/data-protection.md`
    - `production-readiness/observability/audit-logs.md`
    - `pragmatic-programmer/concepts/Design-by-Contract.md`

---

## Phase 17 — Developer Experience

What it is
    Polish on the dashboard: filters, drill-downs, keyboard shortcuts,
    theme toggle, copy-as-markdown.

Half-built
    - Full Next.js dashboard at `frontend/`: `/reviews`, `/reviews/[id]`,
      `/hitl`, `/hitl/[id]`, `/economics`, `/health`.
    - SWR polling at 5s (`frontend/src/lib/swr.tsx`).
    - Single API boundary at `frontend/src/lib/api.ts` (Law of Demeter).
    - Backend `/api/v1/economics/workflow/{id}` endpoint already exists
      with no UI consumer yet.

Missing
    - Review-detail polish (pagination on findings, expand/collapse).
    - Findings filter & search (severity / agent / file).
    - Workflow drill-down page consuming the existing endpoint.
    - Keyboard shortcuts (j/k navigation, ? for help).
    - Copy-as-markdown for review summaries.
    - Light/dark theme toggle (Tailwind `dark:` already prepared).

Wiki references when picked up
    - `modular-architecture/boundaries.md`
    - `engineering-mindset/design-thinking.md`
    - `pragmatic-programmer/concepts/Orthogonality.md` — every new page
      must go through `lib/api.ts`, never fetch directly.

---

## Phase 20 — Continuous Learning

What it is
    Closing the loop between HITL human verdicts and agent behavior:
    auto model-switching, mid-flow per-review caps, confidence
    calibration.

Half-built
    - `backend/economics/routing_advisor.py::recommend_model()` already
      logs recommendations on every agent call.
    - `backend/economics/budget_guard.py` enforces the $50/day hard cap
      at `base_agent` entry.
    - HITL `human_verdict` is persisted alongside `agent_verdict` —
      the labelled training signal is already there, untouched.
    - Per-review $0.50 cap is computed and exposed as a metric (advisory).

Missing
    - Auto-apply `recommend_model()` decisions (route_to_cheaper_model
      flag) instead of only logging them.
    - Mid-flow per-review cap enforcement: needs a cross-agent
      coordinator that can short-circuit downstream agents when the
      $0.50 budget is exhausted.
    - Confidence calibration loop: aggregate `(agent_verdict,
      human_verdict)` pairs and update agent confidence floors.
    - Drift detection on the verdict distribution (alert if APPROVE rate
      changes >2σ in a week).

Wiki references when picked up
    - `mlops/llmops-ai-agents/feedback-loops.md`
    - `distributed-systems/consensus.md` (cross-agent coordinator)
    - `data-systems-engineering/feedback-storage.md`
    - `pragmatic-programmer/concepts/Reversibility.md` — auto
      model-switching is a one-way decision once it's writing to prod
      cost ledgers; gate it behind a feature flag.

---

## Why these are deferred and not deleted

The hooks listed under "half-built" above are kept on purpose. They
were instrumented during their parent phases (HITL audit row in Ph14,
routing_advisor in Ph16) and removing them now to "clean up" would
force re-instrumenting later — exactly the false-economy that
`pragmatic-programmer/concepts/Reversibility.md` warns against.

If you're tempted to delete one of them, read that wiki section first.
