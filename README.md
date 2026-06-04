# AI PR Review Agent

A production-grade, open source AI Pull Request Review Agent. A developer opens a PR. A webhook fires. Four specialist sub-agents run in parallel — security, code quality, test coverage, docs. Each one reasons over the diff plus codebase context retrieved via semantic search. An aggregator merges findings into a single structured review and posts it back to the PR. Low-confidence findings route to a human approval queue.

Every phase has a gate: tests pass, evals pass, a written checkpoint before the next phase begins.

---

## What It Does

- Receives a GitHub PR webhook
- Runs 4 parallel specialist sub-agents: security, quality, test coverage, docs
- Each agent reasons about its domain using the PR diff + codebase context (RAG via pgvectorscale)
- Posts structured review comments back to the GitHub PR
- Routes low-confidence findings to a human approval queue (HITL)
- Every agent action, LLM call, and decision is recorded in a Tiger Cloud hypertable
- Real-time cost and latency dashboards powered by Tiger continuous aggregates
- Learns from merged vs rejected reviews over time

---

## Data Layer — Tiger Cloud (TimescaleDB)

Most AI projects end up juggling three separate stores: a vector DB for RAG, a time-series store for traces, and Postgres for structured data. This project uses [Tiger Cloud](https://tigerdata.com) — a managed TimescaleDB instance — to collapse all three into one Postgres database.

One connection pool. One backup policy. One place to reason about the data.

### Three roles, one database

| Layer | Tiger Feature | What it does |
|---|---|---|
| Semantic memory | pgvectorscale DiskANN | Stores chunked code, ADRs, and prior reviews. 4 specialist agents query it for context on every PR. Replaces Qdrant entirely. |
| Agent events | Hypertables | Every span, LLM call, tool call, and decision lands in one time-ordered table: `agent_events`. Powers the trace viewer, audit trail, and cost ledger. |
| Live dashboards | Continuous aggregates | Real-time rollups for cost per PR, p95 latency per agent, rejection rate. Materialized so the dashboard stays fast as history grows from GBs to TBs. |
| Cost control | Hypertables + aggregates | Token cost attribution per agent span. Budget caps read from the same aggregate the dashboard does. |
| Coding agent | Tiger MCP | The coding agent driving the build is wired to Tiger via MCP. It introspects schemas, runs queries, and verifies migrations live. |

### Schema sketch

```sql
-- The events spine — every agent action as a time-ordered row
CREATE TABLE agent_events (
  ts          TIMESTAMPTZ NOT NULL,
  review_id   UUID        NOT NULL,
  agent       TEXT        NOT NULL,   -- security | quality | tests | docs
  event_type  TEXT        NOT NULL,   -- span.start | llm.call | tool.call | decision
  model       TEXT,
  tokens_in   INT,
  tokens_out  INT,
  cost_usd    NUMERIC(10,6),
  latency_ms  INT,
  outcome     TEXT,
  payload     JSONB
);

SELECT create_hypertable('agent_events', 'ts', chunk_time_interval => INTERVAL '1 day');

-- Continuous aggregate: per-agent cost and latency, refreshed every minute
CREATE MATERIALIZED VIEW agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 minute', ts)    AS bucket,
  agent,
  sum(cost_usd)                  AS cost_usd,
  approx_percentile(0.95, percentile_agg(latency_ms)) AS p95_ms
FROM agent_events
GROUP BY bucket, agent;

-- Semantic memory with pgvectorscale DiskANN
CREATE TABLE code_chunks (
  id        UUID PRIMARY KEY,
  repo      TEXT NOT NULL,
  path      TEXT NOT NULL,
  content   TEXT NOT NULL,
  embedding VECTOR(1536) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX code_chunks_emb_idx ON code_chunks
  USING diskann (embedding vector_cosine_ops);
```

---

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python 3.10) |
| Orchestration | LangGraph (parallel fan-out, checkpointing) |
| Job Queue | Redis + ARQ |
| Memory | Tiger Cloud (pgvectorscale DiskANN + hypertables) |
| LLM | OpenAI GPT-4o (routing per agent) |
| Sandbox | Docker (isolated code execution) |
| Frontend | Next.js (review dashboard, HITL queue, trace viewer) |
| Observability | OpenTelemetry + Tiger hypertables |
| Deploy | Railway |

---

## Architecture

```
GitHub PR webhook
       |
       v
FastAPI ingress  (idempotency key + HMAC)
       |
       v  enqueue(review_job)
ARQ Worker - LangGraph orchestrator
       |
       +---> security_agent
       +---> quality_agent
       +---> tests_agent
       +---> docs_agent
                |
                v
         aggregator --> HITL?
                |
                v
         post_to_github
       |         |         |
       v         v         v
  Tiger       Tiger       Tiger
  pgvector-   hyper-      continuous
  scale       tables      aggregates
  (memory)    (events)    (dashboard)
```

Modular monolith. One FastAPI service, 11 internal modules. See `docs/adr/ADR-002-architecture-style.md`.

---

## Tiger Cloud Setup

1. Sign up at [tigerdata.com/go/kol](https://tigerdata.com/go/kol) — $1,000 in free credits
2. Create a **Hybrid applications** service (TimescaleDB + pgvectorscale)
3. Copy your connection string and add to `.env`:

```bash
cp .env.example .env
# Set TIGER_DATABASE_URL=postgresql://tsdbadmin:...@host.tsdb.cloud.timescale.com:port/tsdb?sslmode=require
```

4. Run the migration:

```bash
psql $TIGER_DATABASE_URL < scripts/migrations/2026-06-tiger-init.sql
```

5. Wire Tiger MCP into Claude Code (optional, for on-camera demo):

```bash
tiger mcp install   # select Claude Code
```

Full MCP setup guide: `scripts/setup-tiger-mcp.md`

Architecture decision record: `docs/adr/ADR-003-tiger-cloud-data-layer.md`

---

## Local Development

```bash
cp .env.example .env          # fill in TIGER_DATABASE_URL, GITHUB_TOKEN, OPENAI_API_KEY
docker compose up             # starts Redis + API + Worker
```

The API will be available at `http://localhost:8000`.
Health check: `GET /health`

---

## 20-Phase Build Roadmap

Each phase is one chapter in the course. Ends green. Has a written gate before the next phase starts. Tiger Cloud is load-bearing in 5 phases.

| # | Phase | Tiger |
|---|---|---|
| 0 | Cognitive Design — autonomy level, HITL boundaries | |
| 1 | System Architecture — module graph, ADRs | |
| 2 | Frontend Engineering — dashboard shell, streaming | |
| 3 | Backend and API Layer — FastAPI, webhook, idempotency | |
| 4 | Workflow Orchestration — LangGraph, parallel fan-out | |
| 5 | LLM and Reasoning Layer — model routing, prompt registry | |
| 6 | Memory Architecture — RAG on pgvectorscale, hybrid retrieval | Tiger |
| 7 | Tooling and Sandboxing — tool registry, Docker sandbox | |
| 8 | Multi-Agent Systems — 4 specialists, contracts, aggregator | |
| 9 | Evaluation Systems — golden dataset, LLM-as-judge | |
| 10 | Observability and Tracing — OTel spans in agent_events hypertable | Tiger |
| 11 | Security Architecture — threat model, RBAC, audit trail | |
| 12 | Reliability Engineering — retries, circuit breakers, idempotency | |
| 13 | Infrastructure — Tiger Cloud provisioning, Tiger MCP wiring | Tiger |
| 14 | Data Engineering — ingestion pipeline, hypertable schema design | Tiger |
| 15 | Governance and Compliance — audit logs, explainability | |
| 16 | Economics and Cost Control — per-agent cost via continuous aggregates | Tiger |
| 17 | Developer Experience — prompt playground, trace viewer | |
| 18 | CI/CD for AI — prompt versioning, eval gates, canary releases | |
| 19 | Human in the Loop — approval queue, escalation, feedback | |
| 20 | Continuous Learning — drift detection from continuous aggregates | Tiger |

---

## Project Structure

```
backend/
  api/              REST endpoints (webhook, reviews, economics, HITL)
  agents/           4 specialist agents (security, quality, tests, docs)
  config/           Settings, environment
  data/             Ingestion pipeline, embedding, freshness
  database/         Postgres async engine + Tiger pool
  economics/        Cost repository, budget caps
  job_queue/        ARQ worker, job definitions
  memory/           TigerMemoryClient (pgvectorscale + hybrid search)
  observability/    Events spine, OTel traces
  orchestrator/     LangGraph graph, nodes, engine
  reliability/      Circuit breakers, retries
  tools/            Tool registry, Docker sandbox

docs/
  adr/              Architecture Decision Records (ADR-001 to ADR-003)

scripts/
  migrations/       2026-06-tiger-init.sql — idempotent schema DDL
  setup-tiger-mcp.md

eval/               Golden dataset + regression configs
frontend/           Next.js dashboard
prompts/            Versioned prompt files per agent
```

---

## Key Design Decisions

- Tiger replaces both Qdrant (vectors) and plain Postgres (structured) — one connection, one backup
- Redis stays for the ARQ job queue (right tool for that job)
- `agent_events` hypertable is the single source of truth for traces, costs, and audit
- Continuous aggregates keep the dashboard fast at any scale — no full table scans
- DiskANN index over `code_chunks` gives 28x lower p95 latency than Pinecone at 99% recall
- HITL threshold is confidence-weighted — low-confidence findings queue for human review

---

Built by [Ayush Singh](https://github.com/ayush488-glitch)
