# TigerData x AI PR Review Agent — Full Integration Plan
Ayush Singh · May 2026

---

## What This Plan Covers

The existing project uses three separate stores:
- Qdrant for vector/RAG memory
- PostgreSQL (3 plain tables) for structured history
- Redis for job queue + short-term memory

We collapse the first two into one Tiger Cloud Postgres.
Qdrant is replaced entirely by pgvectorscale.
The plain Postgres tables stay but gain TimescaleDB superpowers.
Redis stays (ARQ job queue is orthogonal — it's fine).

Result: one connection pool, one backup policy, one place to reason about data.
Tiger becomes the spine. Everything meaningful flows through it.

---

## Current Architecture (what exists)

```
backend/
  database/
    models.py        <- 3 SQLAlchemy tables: pr_review_records, finding_records, repo_file_index
    postgres.py      <- async engine + session factory (asyncpg)
    repository.py    <- CRUD helpers
  memory/
    qdrant_client.py <- Qdrant vector search (REPLACED by Tiger)
    context_retriever.py <- RAG pipeline wrapping Qdrant
    embedder.py      <- OpenAI text-embedding-3-small calls
    redis_client.py  <- stays (job queue)
  observability/
    events.py        <- in-memory event bus + Postgres writes (UPGRADED to hypertable)
    tracing.py       <- OTel spans (WIRED to agent_events)
    audit.py         <- audit trail reads (FROM hypertable)
  economics/
    cost_repository.py <- per-span cost writes (MOVED to agent_events)
    budget.py          <- budget cap reads (FROM continuous aggregate)
  data/
    ingestion.py     <- chunk + embed + write to Qdrant (CHANGED to Tiger)
    freshness.py     <- repo_file_index staleness checks (STAYS, table moves to Tiger)
```

---

## Tiger Integration Points — 3 Roles, One DB

### Role 1: Semantic Memory (replaces Qdrant)
Table: `code_chunks`
Extension: pgvectorscale (DiskANN index)
Affected modules: memory/, data/

### Role 2: Agent Events (upgrades plain Postgres writes)
Table: `agent_events` (hypertable)
Extension: timescaledb
Affected modules: observability/, economics/

### Role 3: Live Dashboards (new capability)
View: `agent_health_1m` (continuous aggregate)
Affected modules: api/economics_router.py, frontend/src/app/economics/

---

## Complete Schema — 5 Objects

```sql
-- ============================================================
-- STEP 0: Extensions (run once on Tiger Cloud)
-- ============================================================
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;  -- also installs pgvector

-- ============================================================
-- ROLE 1: Semantic Memory with pgvectorscale
-- ============================================================

CREATE TABLE code_chunks (
  id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  repo         TEXT         NOT NULL,
  path         TEXT         NOT NULL,
  symbol       TEXT,                        -- function/class name if applicable
  chunk_index  INT          NOT NULL,       -- order within file (for context stitching)
  content      TEXT         NOT NULL,
  embedding    VECTOR(1536) NOT NULL,       -- text-embedding-3-small
  token_count  INT,
  updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- StreamingDiskANN index — replaces Qdrant HNSW
CREATE INDEX code_chunks_emb_idx ON code_chunks
  USING diskann (embedding vector_cosine_ops);

-- Lookup index for freshness checks
CREATE INDEX code_chunks_repo_path ON code_chunks (repo, path, updated_at DESC);

-- ============================================================
-- ROLE 2: Agent Events Hypertable (the spine)
-- ============================================================

CREATE TABLE agent_events (
  ts            TIMESTAMPTZ  NOT NULL,
  review_id     UUID         NOT NULL,
  agent         TEXT         NOT NULL,    -- security | quality | tests | docs | aggregator | system
  span_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
  parent_span   UUID,
  event_type    TEXT         NOT NULL,    -- span.start | span.end | llm.call | tool.call | decision | escalation
  model         TEXT,                    -- gpt-4o | claude-3-5-sonnet etc.
  tokens_in     INT,
  tokens_out    INT,
  cost_usd      NUMERIC(10,6),
  latency_ms    INT,
  outcome       TEXT,                    -- approved | request_changes | critical_block | escalated
  confidence    NUMERIC(4,3),            -- 0.000 to 1.000
  payload       JSONB
);

-- Convert to hypertable — partition by day
SELECT create_hypertable('agent_events', by_range('ts', INTERVAL '1 day'));

-- Query indexes
CREATE INDEX ae_review_ts    ON agent_events (review_id, ts DESC);
CREATE INDEX ae_agent_ts     ON agent_events (agent, ts DESC);
CREATE INDEX ae_event_type   ON agent_events (event_type, ts DESC);

-- ============================================================
-- ROLE 3: Continuous Aggregates (live dashboards)
-- ============================================================

-- Per-minute health view (cost + latency + rejection rate per agent)
CREATE MATERIALIZED VIEW agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 minute', ts)                                          AS bucket,
  agent,
  count(*) FILTER (WHERE event_type = 'llm.call')                     AS llm_calls,
  sum(cost_usd)                                                        AS cost_usd,
  sum(tokens_in)                                                       AS tokens_in,
  sum(tokens_out)                                                      AS tokens_out,
  approx_percentile(0.95, percentile_agg(latency_ms))                  AS p95_ms,
  approx_percentile(0.50, percentile_agg(latency_ms))                  AS p50_ms,
  count(*) FILTER (WHERE outcome = 'rejected')::float
    / NULLIF(count(*) FILTER (WHERE outcome IS NOT NULL), 0)           AS rejection_rate,
  count(*) FILTER (WHERE outcome = 'escalated')::float
    / NULLIF(count(*) FILTER (WHERE outcome IS NOT NULL), 0)           AS escalation_rate
FROM agent_events
GROUP BY bucket, agent
WITH NO DATA;

SELECT add_continuous_aggregate_policy('agent_health_1m',
  start_offset      => INTERVAL '2 hours',
  end_offset        => INTERVAL '1 minute',
  schedule_interval => INTERVAL '1 minute');

-- Per-PR cost rollup (hourly, for economics dashboard)
CREATE MATERIALIZED VIEW pr_cost_hourly
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 hour', ts)     AS bucket,
  review_id,
  sum(cost_usd)                  AS total_cost_usd,
  sum(tokens_in + tokens_out)    AS total_tokens,
  count(DISTINCT agent)          AS agents_used,
  max(confidence)                AS max_confidence,
  max(ts) - min(ts)              AS wall_time
FROM agent_events
GROUP BY bucket, review_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('pr_cost_hourly',
  start_offset      => INTERVAL '3 days',
  end_offset        => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour');

-- ============================================================
-- EXISTING TABLES (keep, migrate to Tiger instance)
-- ============================================================

CREATE TABLE pr_review_records (
  id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  pr_number         INT         NOT NULL,
  repo_full_name    TEXT        NOT NULL,
  head_sha          TEXT        NOT NULL,
  verdict           TEXT        NOT NULL,
  confidence_score  NUMERIC(4,3),
  review_body       TEXT,
  github_review_id  BIGINT,
  status            TEXT        NOT NULL DEFAULT 'pending',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE finding_records (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  review_id      UUID        NOT NULL REFERENCES pr_review_records(id),
  agent          TEXT        NOT NULL,
  severity       TEXT        NOT NULL,
  category       TEXT        NOT NULL,
  file_path      TEXT,
  line_number    INT,
  description    TEXT        NOT NULL,
  suggestion     TEXT,
  confidence     NUMERIC(4,3),
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX finding_repo_sev ON finding_records (review_id, severity);

CREATE TABLE repo_file_index (
  repo_path       TEXT        PRIMARY KEY,  -- "owner/repo:filepath"
  last_indexed_at TIMESTAMPTZ NOT NULL,
  token_count     INT,
  chunk_count     INT
);
```

---

## Files That Change — Module by Module

### 1. backend/database/postgres.py  [MODIFIED]

Add Tiger-specific initialization on startup:
- CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE
- CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE
- Create all tables if not exist (idempotent)
- Create hypertable via create_hypertable() (idempotent — check_exists=true)
- Create continuous aggregate policies

New function: `async def init_tiger_schema(engine)` called from main.py lifespan.

### 2. backend/database/models.py  [MODIFIED]

Current: 3 SQLAlchemy ORM classes.

Changes:
- Keep pr_review_records, finding_records, repo_file_index as SQLAlchemy models
- Add AgentEvent as a plain dataclass (NOT SQLAlchemy) — we write it raw via asyncpg
  for maximum throughput, no ORM overhead on the hot path
- Add CodeChunk as a plain dataclass with vector field
- Add a raw SQL helper: `insert_agent_event(pool, event: AgentEvent)` using asyncpg
  executemany for batch writes

Reason: hypertable inserts are high-frequency (every LLM call). SQLAlchemy ORM adds
~2ms overhead per row from Python-side object hydration. Raw asyncpg is 10x faster.

### 3. backend/memory/tiger_client.py  [NEW — replaces qdrant_client.py]

```python
class TigerMemoryClient:
    def __init__(self, pool: asyncpg.Pool): ...

    async def upsert_chunks(self, chunks: list[CodeChunk]) -> None:
        # INSERT INTO code_chunks ... ON CONFLICT (repo, path, chunk_index) DO UPDATE
        # Uses asyncpg.copy_records_to_table for bulk (fastest path)

    async def search(
        self,
        query_embedding: list[float],
        repo: str,
        top_k: int = 10,
        hybrid: bool = True
    ) -> list[CodeChunk]:
        # Semantic: ORDER BY embedding <=> $1 LIMIT top_k (DiskANN via pgvectorscale)
        # Keyword (hybrid): tsvector full-text on content column
        # Merge with RRF (reciprocal rank fusion): 1/(k + rank_semantic) + 1/(k + rank_keyword)
        # Freshness decay: multiply score by exp(-hours_since_update / 168)  <- 1 week half-life

    async def delete_stale_chunks(self, repo: str, before: datetime) -> int:
        # DELETE FROM code_chunks WHERE repo=$1 AND updated_at < $2

    async def health_check(self) -> dict:
        # SELECT count(*), max(updated_at) FROM code_chunks
```

### 4. backend/memory/context_retriever.py  [MODIFIED]

Current: wraps Qdrant search.
Change: swap Qdrant calls for TigerMemoryClient.search().
Interface stays the same (retrieve_context(diff_text, repo) -> list[str]).
No agent code changes needed.

### 5. backend/memory/qdrant_client.py  [DELETED]

Remove. No longer needed.

### 6. backend/data/ingestion.py  [MODIFIED]

Current: chunks files, embeds, writes to Qdrant.
Change:
- Chunk logic stays
- Embed via embedder.py stays
- Write destination: TigerMemoryClient.upsert_chunks() instead of Qdrant
- Update repo_file_index (same table, now on Tiger)

### 7. backend/observability/events.py  [MODIFIED — major]

Current: EventBus with in-memory subscribers + Postgres writes to pr_review_records.

Changes:
- Add `async def emit_agent_event(pool, event: AgentEvent)` — raw asyncpg insert
  to agent_events hypertable
- Every span start/end, every LLM call, every tool call emits a row
- Keep existing EventBus for in-process pub/sub (used for streaming to frontend)
- The hypertable write is fire-and-forget (background task, never blocks the agent)

AgentEvent fields (dataclass):
```python
@dataclass
class AgentEvent:
    ts: datetime           # UTC now()
    review_id: UUID
    agent: str             # "security" | "quality" | "tests" | "docs" | "aggregator"
    span_id: UUID          # gen per event
    parent_span: UUID | None
    event_type: str        # "span.start" | "llm.call" | "tool.call" | "decision" | "span.end"
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    latency_ms: int | None
    outcome: str | None    # "approved" | "request_changes" | "critical_block" | "escalated"
    confidence: float | None
    payload: dict | None   # raw LLM output, tool args, etc.
```

### 8. backend/economics/cost_repository.py  [MODIFIED]

Current: writes cost rows to a separate cost_records table, reads back for budget checks.

Changes:
- Stop writing to cost_records (deprecate that table)
- Reads from agent_health_1m continuous aggregate instead:
  ```sql
  SELECT sum(cost_usd) FROM agent_health_1m
  WHERE bucket >= now() - INTERVAL '24 hours'
  ```
- get_daily_cost(): reads from pr_cost_hourly aggregate
- get_per_agent_cost(): reads from agent_health_1m
- Both queries hit pre-materialized views — sub-millisecond even at terabyte scale

### 9. backend/economics/budget.py  [MODIFIED]

Current: reads from cost_records, hard-stops if daily budget exceeded.
Change: same logic, source = agent_health_1m aggregate.
Add: per-PR budget cap from pr_cost_hourly (e.g. max $0.50 per PR review).

### 10. backend/api/economics_router.py  [MODIFIED — adds 3 new endpoints]

New endpoints powered by Tiger aggregates:

GET /economics/agent-health
  -> SELECT * FROM agent_health_1m WHERE bucket >= now() - INTERVAL '1 hour' ORDER BY bucket DESC
  -> Returns per-agent cost/latency/rejection over last hour

GET /economics/pr-cost/{review_id}
  -> SELECT * FROM pr_cost_hourly WHERE review_id=$1
  -> Full cost breakdown for a specific PR

GET /economics/daily-summary
  -> Rolls up agent_health_1m over 24h
  -> Returns total cost, total tokens, p95 latency per agent

These feed the frontend dashboard. Response is pre-aggregated by Tiger, not by Python.

### 11. backend/observability/audit.py  [MODIFIED]

Current: writes audit rows to a plain Postgres table.
Change: audit trail IS the agent_events hypertable.

get_audit_trail(review_id) -> reads from agent_events WHERE review_id=$1 ORDER BY ts.
Full immutable chronological trace. No extra table needed.

### 12. backend/config/settings.py  [MODIFIED]

Add:
```python
TIGER_DATABASE_URL: str = ""      # Tiger Cloud connection string (for production)
TIGER_POOL_SIZE: int = 10
TIGER_MAX_OVERFLOW: int = 20
```

If TIGER_DATABASE_URL is empty, fall back to DATABASE_URL (dev mode with local timescaledb-ha Docker).

### 13. docker-compose.yml + docker-compose.dev.yml  [MODIFIED]

Replace:
```yaml
postgres:
  image: postgres:15
```

With:
```yaml
postgres:
  image: timescale/timescaledb-ha:pg16
  environment:
    POSTGRES_PASSWORD: password
    POSTGRES_DB: pr_review
  ports:
    - "5432:5432"
  volumes:
    - pgdata:/var/lib/postgresql/data
```

timescaledb-ha includes: TimescaleDB + pgvectorscale + pgvector + PostGIS.
No separate Qdrant container needed. Remove qdrant from docker-compose.

### 14. requirements.txt  [MODIFIED]

Add:
```
pgvector>=0.3.0       # Python VECTOR type support for asyncpg
psycopg2-binary       # sync driver (for migrations + admin scripts)
```

Remove:
```
qdrant-client         # no longer needed
```

### 15. scripts/migrations/2026-06-tiger-init.sql  [NEW]

Full idempotent SQL migration:
- CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE
- CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE
- CREATE TABLE IF NOT EXISTS code_chunks (...)
- CREATE INDEX IF NOT EXISTS code_chunks_emb_idx (...)
- CREATE TABLE IF NOT EXISTS agent_events (...)
- SELECT create_hypertable('agent_events', ..., if_not_exists => true)
- CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m (...)
- SELECT add_continuous_aggregate_policy(...) -- idempotent via if_not_exists

### 16. .env.example  [MODIFIED]

Add:
```
TIGER_DATABASE_URL=postgres://tsdbadmin:password@your-service.tigerdata.cloud:5432/tsdb?sslmode=require
```

Remove:
```
QDRANT_URL=...
QDRANT_API_KEY=...
```

### 17. frontend/src/app/economics/page.tsx  [MODIFIED]

Current: shows cost data from /economics endpoint.

New sections:
- Agent Health panel: calls GET /economics/agent-health
  shows per-agent sparkline: cost/minute, p95 latency, rejection rate
- PR Cost panel: shows total tokens + cost for current viewed PR
- Live refresh: SWR with refreshInterval=60000 (reads from 1-min aggregate)

### 18. frontend/src/lib/types.ts  [MODIFIED]

Add types:
```typescript
interface AgentHealthBucket {
  bucket: string
  agent: string
  llm_calls: number
  cost_usd: number
  p95_ms: number
  rejection_rate: number
  escalation_rate: number
}

interface PRCostSummary {
  review_id: string
  total_cost_usd: number
  total_tokens: number
  agents_used: number
  wall_time_ms: number
}
```

---

## What Does NOT Change

- LangGraph graph.py / nodes.py / state.py — orchestration is untouched
- All 4 specialist agents (security, quality, test, docs) — no changes
- HITL queue (hitl/) — no changes
- GitHub client (integrations/) — no changes
- ARQ worker + Redis — job queue stays Redis-backed
- Prompt registry (prompts/) — untouched
- Auth / RBAC — untouched
- Reliability (retries, circuit breakers) — untouched
- Frontend pages except economics — untouched

---

## Migration Strategy — Phased, Zero Downtime

### Phase A: Infra Swap (no code changes yet)
1. Provision Tiger Cloud instance (or run timescaledb-ha Docker locally)
2. Run 2026-06-tiger-init.sql migration
3. Verify extensions: `SELECT extname FROM pg_extension`
4. Verify hypertable: `SELECT * FROM timescaledb_information.hypertables`
5. Verify diskann index: `SELECT * FROM pg_indexes WHERE indexname = 'code_chunks_emb_idx'`

### Phase B: Dual-Write Events (safe rollout)
1. Deploy new observability/events.py with Tiger writes
2. Keep existing Postgres writes for 1 week (dual-write)
3. Verify agent_events fills up correctly during real PR reviews
4. Cut over economics/ reads to aggregates
5. Remove old cost_records writes

### Phase C: Memory Swap (Qdrant → Tiger)
1. Re-index all repos into code_chunks (run data/ingestion.py against Tiger)
2. Deploy new context_retriever.py pointing to TigerMemoryClient
3. Run parallel test: same query against Qdrant and Tiger, diff results
4. When recall quality matches, remove QDRANT_URL from env
5. Shut down Qdrant container

### Phase D: Dashboard
1. Deploy economics_router.py changes
2. Deploy frontend economics page changes
3. Live demo: watch p95 latency update in real time as a PR review runs

---

## Chapter Alignment (for the video)

Ch 04  Tiger Cloud setup
  - Provision Tiger Cloud instance
  - Run tiger-init.sql via psql
  - Wire TIGER_DATABASE_URL into settings.py
  - Show Tiger MCP in coding agent: introspect hypertable schema live

Ch 05  The Events Spine (hypertable)
  - agent_events hypertable schema design walkthrough
  - Emit first real event from orchestrator/nodes.py
  - SELECT from agent_events, show time-ordered trace of a full PR review
  - Tiger MCP: run SELECT on agent_events, explain chunking

Ch 06  Semantic Memory on pgvectorscale
  - DROP qdrant_client.py, create tiger_client.py
  - CREATE TABLE code_chunks + diskann index
  - First chunk ingestion, first semantic search query
  - Show DiskANN vs HNSW: same cosine ops, 28x lower p95 latency claim
  - Hybrid retrieval: tsvector + vector, RRF merge

Ch 11  Continuous Aggregates = Live Dashboard
  - CREATE MATERIALIZED VIEW agent_health_1m
  - add_continuous_aggregate_policy
  - Run a PR review, watch the aggregate refresh
  - Hook into economics_router.py, hit the API
  - Frontend: sparkline charts pulling from the aggregate

Ch 12  Token Cost Attribution
  - cost_usd field in agent_events
  - pr_cost_hourly aggregate
  - per-PR budget cap check reading from aggregate
  - Show: one Tiger query = entire cost ledger for all time

Ch 13  Tiger MCP Integration
  - Configure Tiger MCP in Claude/Cursor config
  - Show: coding agent introspects agent_events schema
  - Show: agent runs EXPLAIN ANALYZE on a query, suggests index
  - Show: agent adds a new column to payload JSONB, runs migration
  - Meta moment: the tool being built is also building itself

Ch 14  Data Engineering (embedding freshness)
  - Ingestion pipeline: chunk -> embed -> upsert to code_chunks
  - Freshness decay logic (score * exp(-age/168h))
  - repo_file_index still tracks last_indexed_at
  - Show: staleness query — files not updated in 7 days

Ch 16  Economics and Cost Control
  - Budget cap reads from agent_health_1m
  - Per-PR cost cap from pr_cost_hourly
  - "Under $0.10 per review" goal — show live cost as PR runs

Ch 20  Continuous Learning via Aggregates
  - Drift detection: rejection_rate in agent_health_1m trending up = prompt drift
  - Trigger reflection loop when rejection_rate > 0.15 over 24h window
  - All powered by the same continuous aggregate

---

## Concrete Things Tiger Enables That Plain Postgres/Qdrant Cannot

1. Sub-millisecond cost queries at terabyte scale
   agent_health_1m is pre-materialized. SELECT sum(cost_usd) FROM agent_health_1m
   is <1ms at any scale. Without continuous aggregates, this is a GROUP BY scan
   over millions of rows that slows as the table grows.

2. DiskANN vs HNSW (Qdrant default)
   28x lower p95 latency at 50M vectors vs Pinecone.
   Same cosine ops syntax. No separate service to manage.
   Freshness decay is a plain SQL expression — impossible in Qdrant without custom scoring.

3. Audit trail = trace viewer = cost ledger = ONE table
   agent_events hypertable is the single source of truth for:
   - OTel-style spans (debugging)
   - Cost attribution (economics)
   - Compliance audit trail (security/governance)
   - LLM-as-judge input (eval regression)
   In the current project, these are 4 separate stores.

4. Time-travel debugging
   SELECT * FROM agent_events WHERE review_id=$1 ORDER BY ts
   gives you the full step-by-step replay of any past PR review.
   Impossible with Qdrant (no event storage) and clunky with plain Postgres
   (no efficient time-ordered chunked storage).

5. Drift detection without a separate MLOps tool
   rejection_rate in agent_health_1m is just SQL.
   Compare last 7 days vs previous 7 days = one query.
   No Prometheus, no Grafana, no separate telemetry stack needed for the demo.

---

## Dependency Graph of Changes

```
tiger-init.sql
     |
     +-- postgres.py (init_tiger_schema)
     |        |
     |        +-- main.py (call on lifespan startup)
     |
     +-- tiger_client.py (new)
     |        |
     |        +-- context_retriever.py (swap Qdrant -> Tiger)
     |        |        |
     |        |        +-- agents/* (no changes, just uses retriever interface)
     |        |
     |        +-- ingestion.py (swap Qdrant writes -> Tiger)
     |
     +-- events.py (add emit_agent_event)
              |
              +-- orchestrator/nodes.py (call emit_agent_event at each node)
              |
              +-- tools/llm_client.py (call emit_agent_event for each LLM call)
              |
              +-- economics/cost_repository.py (reads from aggregates)
              |
              +-- economics/budget.py (reads from aggregates)
              |
              +-- api/economics_router.py (new aggregate endpoints)
              |
              +-- observability/audit.py (reads from hypertable)
              |
              +-- frontend/src/app/economics/ (new dashboard panels)
```

---

## Work Estimate by Phase

Phase A (Infra):      1 day
Phase B (Events):     1.5 days — most impactful, highest priority
Phase C (Memory):     1.5 days — Qdrant replacement
Phase D (Dashboard):  1 day — frontend + new API endpoints

Total: ~5 days of focused work.
For the video: each phase maps to 1-2 chapters.

---

## What to Demo Live on Camera (the Tiger moments)

1. Tiger MCP in Claude: run `\d agent_events` to introspect live schema
2. Run a real PR review. Open psql. Run:
   SELECT agent, sum(cost_usd), approx_percentile(0.95, percentile_agg(latency_ms))
   FROM agent_health_1m WHERE bucket > now() - INTERVAL '5 minutes' GROUP BY agent;
   — shows real-time cost and p95 latency, populated by the review that just ran.
3. Open the frontend economics page. The sparkline updates every minute automatically.
4. Run DiskANN search: show the EXPLAIN ANALYZE output. Index scan, not seq scan.
5. Kill Qdrant container. System still works. Tiger handled it.

---

## Open Questions Before Build Starts

1. Tiger Cloud region — same as Railway (us-east) to minimize latency?
2. Embedding model — keep text-embedding-3-small (1536d) or downsize to 3-large-256d?
   256d = 6x less storage, DiskANN still faster than HNSW at same recall.
3. TIGER_DATABASE_URL separate from DATABASE_URL?
   Recommendation: yes, one connection pool per schema owner.
   Tiger Cloud = separate billing/audit from local Postgres.
4. Keep Qdrant as fallback for dev with no Tiger Cloud access?
   Recommendation: no. timescaledb-ha Docker gives full Tiger locally in 30 seconds.
5. Does the video show Tiger Cloud UI (console) or just psql/MCP?
   Both — Tiger Console shows visual chunk interval graph + hypertable stats.

---

Ayush Singh · ai-pr-review-agent-tigerdata · June 2026
