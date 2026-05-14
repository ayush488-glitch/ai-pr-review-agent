# ADR-004 — Cost Control Design (Phase 16)

Status: Accepted
Date: 2026-05-13
Supersedes: none

## Context

The agent makes 4 LLM calls per PR (one per specialist) plus an
aggregator call, across 4 different models (Sonnet, Haiku, gpt-4o,
gpt-4o-mini). Without enforcement, a runaway loop or a hostile PR could
exhaust the daily budget in minutes.

Three failure modes had to be addressed:

1. Total spend exceeds the daily cap.
2. A single PR consumes a disproportionate share of the daily cap.
3. We don't know after the fact where the spend went, so we can't
   tune model choice per agent.

## Decision

We layer three independent controls, each with a single responsibility:

### 1. Hard daily cap — `BudgetGuard`

`backend/economics/budget_guard.py` is invoked at the top of every
`BaseAgent.run()`. It reads `daily_budget_usd` from settings (default
$50), reads the day's running total from Redis, and raises
`BudgetExceededError` before the LLM call. **Hard block, no override.**

Wiki ref: `pragmatic-programmer/concepts/Design-by-Contract.md` — the
precondition is checked at the boundary, not in the middle of the
workflow.

### 2. Per-review advisory cap — metric only

Per-review $0.50 cap is computed and emitted as a metric, but it is
**not** enforced mid-flow. Mid-flow enforcement requires a cross-agent
coordinator (deferred to Phase 20) because individual agents don't know
what their siblings have already spent on the same workflow.

Trade-off accepted: a single bad PR can in theory consume up to 4×
average ($0.16) before all four agents finish. In practice the daily
cap catches systemic abuse; the per-review metric flags outliers for
human review.

### 3. Cost attribution — ContextVar workflow_id

`backend/orchestrator/workflow_context.py` exposes a `ContextVar` set at
the top of each workflow. `economics/cost_recorder.py` reads it lazily
inside the LLM cost callback so every cost row carries
`(workflow_id, agent_type, model, tokens_in, tokens_out, usd)`.

Why ContextVar and not signature plumbing?

We considered passing `workflow_id` explicitly through every function
signature. Rejected because:

- The cost recorder is a cross-cutting concern. It runs in async
  callbacks deep inside the Anthropic / OpenAI SDKs, where we can't
  reach the call site.
- ContextVar correctly propagates across `asyncio.create_task` and
  `await` boundaries.
- Wiki ref: `modular-architecture/cross-cutting-concerns.md` — logging,
  tracing, and cost attribution are the canonical examples; signature
  plumbing creates DRY violations on every new agent.

**Do not refactor this to signature plumbing without re-reading that
wiki section.**

## Consequences

Positive
    - End-to-end attribution: `/api/v1/economics/workflow/{id}` shows
      exactly which agent/model spent what on a given PR.
    - Daily cap is unforgeable — `BudgetGuard` raises before any LLM
      provider call, so a buggy retry loop can't bypass it.
    - The control plane (BudgetGuard, RoutingAdvisor) and the data
      plane (CostRecorder) are decoupled. Phase 20 can swap the advisor
      from logging to acting without touching the recorder.

Negative
    - Per-review enforcement is best-effort until Phase 20 ships the
      coordinator. Documented in `docs/FUTURE_WORK.md`.
    - ContextVar discipline must be respected: every new entry point
      (webhook, retry, replay) must call `set_workflow_context()` or
      cost rows are written with `workflow_id=None`.

## Alternatives considered

A. Signature plumbing for `workflow_id`. Rejected — see above.

B. Per-agent budget pools instead of one daily cap. Rejected — the
   bound that matters is end-of-day spend; per-agent pools would be
   bin-packing complexity for no operator benefit.

C. Synchronous cost ledger writes inside the LLM call. Rejected — adds
   latency on the hot path. Cost rows are recorded asynchronously
   after the LLM call returns.
