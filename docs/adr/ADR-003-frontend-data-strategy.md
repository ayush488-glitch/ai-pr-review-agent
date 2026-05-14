# ADR-003: Frontend Data Strategy — Polling over SSE (for now)

Status: Accepted
Date: 2026-05-14
Phase: 2 (Frontend Engineering)

## Context

Phase 2 requires a "streaming review feed" per the README. The backend exposes
synchronous REST endpoints (/api/v1/reviews, /api/v1/hitl/queue, etc.) but does
NOT currently expose a Server-Sent Events or WebSocket endpoint. The Phase 10
ReviewEvent lifecycle is in place (16 events) but is not wired to an outbound
push channel.

Two options were on the table for wiring the dashboard's "live" feel.

## Options

### Option A — Polling via SWR (refreshInterval=5s)
- Pros: ships in this session, zero backend work, robust to disconnects,
  trivially cacheable, fits the existing REST surface.
- Cons: 5s lag worst case; small constant request load (~12 req/min/tab idle).

### Option B — Server-Sent Events
- Pros: true push, sub-second updates, lower bytes-on-wire at idle.
- Cons: requires a new backend endpoint that subscribes to ReviewEvent
  emissions, plus a Redis pub/sub or asyncio broadcast layer. Touches Phase 10
  and reliability/idempotency concerns. Out of scope for Phase 2.

## Decision

**Option A.** Ship polling now. A single SWR provider in `src/lib/swr.tsx` sets
`refreshInterval: 5000` for every `useSWR` call. The data layer (`src/lib/api.ts`)
is the only place that talks to the backend — swapping to SSE later is a
one-file migration.

## Swap Contract

When SSE is added (post-Phase 16, likely with Phase 17 dev experience tooling):

1. Backend exposes `GET /api/v1/events/stream` returning an SSE feed of
   ReviewEvent JSON objects, filterable by `?review_id=`.
2. Frontend adds `src/lib/events.ts` with an `EventSource` wrapper.
3. Pages that currently use `useSWR(...)` for review or HITL state subscribe
   to the stream and call `mutate(key)` on relevant events.
4. SWR `refreshInterval` is dropped to `0` (manual revalidate only) to avoid
   double-fetching once the stream is live.

The frontend never poll-fetches inside components directly; all fetches go
through `api.ts`. This is the orthogonality property that makes the swap cheap.

## When To Revisit

- If polling load becomes visible at >50 active dashboard tabs.
- When Phase 17 (Developer Experience) builds the trace viewer — that page
  WILL need true streaming and is the natural moment to add SSE.
- If a customer demo requires "instant" UI feedback below the 5s polling
  budget.

## Consequences

- 5s perceived latency on dashboard updates. Acceptable for an internal review
  tool.
- One round-trip per polling tick per open tab. Negligible at current scale.
- HITL decisions trigger an immediate `router.refresh()` after POST so the
  decider does not wait the full 5s to see their action reflected.
- Document the polling cadence in the UI footer so operators know it isn't
  push-based.
