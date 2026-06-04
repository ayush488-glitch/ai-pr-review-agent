-- =============================================================================
-- 2026-06-tiger-init.sql
--
-- Tiger Cloud (TimescaleDB) schema for AI PR Review Agent.
-- Idempotent: safe to run multiple times.
--
-- Run against Tiger Cloud:
--   psql "$TIGER_DATABASE_URL" -f scripts/migrations/2026-06-tiger-init.sql
--
-- Or locally with timescaledb-ha Docker:
--   psql "postgres://postgres:password@localhost:5432/pr_review" -f scripts/migrations/2026-06-tiger-init.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- STEP 0: Extensions
-- CASCADE installs pgvector as a dependency of vectorscale automatically.
-- ---------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;   -- also installs pgvector

-- ---------------------------------------------------------------------------
-- STEP 1: Semantic Memory — code_chunks (replaces Qdrant)
--
-- Stores chunked code files, ADRs, and prior reviews.
-- The DiskANN index powers ANN search — replaces Qdrant's HNSW collection.
-- 256-dim text-embedding-3-large truncated outperforms 1536-dim small
-- at 6x less storage with the same recall curve on code.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS code_chunks (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    repo         TEXT         NOT NULL,
    path         TEXT         NOT NULL,
    symbol       TEXT,                          -- function/class name (nullable)
    chunk_index  INT          NOT NULL,         -- order within file (for context stitching)
    content      TEXT         NOT NULL,
    embedding    VECTOR(256)  NOT NULL,         -- text-embedding-3-large, 256 dims
    token_count  INT,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- StreamingDiskANN index (pgvectorscale) — cosine similarity
-- Replaces Qdrant's HNSW. 28x lower p95 latency at 50M vectors.
CREATE INDEX IF NOT EXISTS code_chunks_emb_idx
    ON code_chunks USING diskann (embedding vector_cosine_ops);

-- Lookup index for freshness queries
CREATE INDEX IF NOT EXISTS code_chunks_repo_path_idx
    ON code_chunks (repo, path, updated_at DESC);

-- Unique constraint: each (repo, path, chunk_index) is one chunk
CREATE UNIQUE INDEX IF NOT EXISTS code_chunks_unique_idx
    ON code_chunks (repo, path, chunk_index);

-- Full-text search index for hybrid retrieval (semantic + keyword)
ALTER TABLE code_chunks
    ADD COLUMN IF NOT EXISTS content_tsv TSVECTOR
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

CREATE INDEX IF NOT EXISTS code_chunks_fts_idx
    ON code_chunks USING GIN (content_tsv);

-- ---------------------------------------------------------------------------
-- STEP 2: Agent Events Hypertable — the spine
--
-- Every span, LLM call, tool call, and decision lands here.
-- Powers: trace viewer, audit trail, cost ledger, drift detector.
-- One table replaces: OTel spans, cost_records, audit logs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_events (
    ts            TIMESTAMPTZ  NOT NULL,
    review_id     UUID         NOT NULL,
    agent         TEXT         NOT NULL,   -- security | quality | tests | docs | aggregator | system
    span_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
    parent_span   UUID,
    event_type    TEXT         NOT NULL,   -- span.start | span.end | llm.call | tool.call | decision | escalation
    model         TEXT,                   -- gpt-4o | claude-3-5-sonnet | etc.
    tokens_in     INT,
    tokens_out    INT,
    cost_usd      NUMERIC(10,6),
    latency_ms    INT,
    outcome       TEXT,                   -- approved | request_changes | critical_block | escalated
    confidence    NUMERIC(4,3),           -- 0.000 to 1.000
    payload       JSONB
);

-- Convert to TimescaleDB hypertable — partition by 1 day
SELECT create_hypertable(
    'agent_events',
    by_range('ts', INTERVAL '1 day'),
    if_not_exists => TRUE
);

-- Query indexes
CREATE INDEX IF NOT EXISTS ae_review_ts_idx
    ON agent_events (review_id, ts DESC);

CREATE INDEX IF NOT EXISTS ae_agent_ts_idx
    ON agent_events (agent, ts DESC);

CREATE INDEX IF NOT EXISTS ae_event_type_idx
    ON agent_events (event_type, ts DESC);

CREATE INDEX IF NOT EXISTS ae_outcome_idx
    ON agent_events (outcome, ts DESC)
    WHERE outcome IS NOT NULL;

-- ---------------------------------------------------------------------------
-- STEP 3: Continuous Aggregates — live dashboards
--
-- Pre-materialized rollups. Sub-millisecond reads at terabyte scale.
-- The dashboard stays fast as history grows from GB to TB.
-- ---------------------------------------------------------------------------

-- agent_health_1m: per-agent cost + latency + rejection rate, refreshed every minute
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_health_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts)                                           AS bucket,
    agent,
    count(*) FILTER (WHERE event_type = 'llm.call')                      AS llm_calls,
    sum(cost_usd)                                                         AS cost_usd,
    sum(tokens_in)                                                        AS tokens_in,
    sum(tokens_out)                                                       AS tokens_out,
    approx_percentile(0.95, percentile_agg(latency_ms))                   AS p95_ms,
    approx_percentile(0.50, percentile_agg(latency_ms))                   AS p50_ms,
    count(*) FILTER (WHERE outcome = 'rejected')::float
        / NULLIF(count(*) FILTER (WHERE outcome IS NOT NULL), 0)          AS rejection_rate,
    count(*) FILTER (WHERE outcome = 'escalated')::float
        / NULLIF(count(*) FILTER (WHERE outcome IS NOT NULL), 0)          AS escalation_rate
FROM agent_events
GROUP BY bucket, agent
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'agent_health_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

-- pr_cost_hourly: per-PR cost + token rollup, refreshed every hour
CREATE MATERIALIZED VIEW IF NOT EXISTS pr_cost_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts)                     AS bucket,
    review_id,
    sum(cost_usd)                                  AS total_cost_usd,
    sum(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0)) AS total_tokens,
    count(DISTINCT agent)                          AS agents_used,
    max(confidence)                                AS max_confidence,
    (max(ts) - min(ts))                            AS wall_time
FROM agent_events
GROUP BY bucket, review_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'pr_cost_hourly',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE
);

-- ---------------------------------------------------------------------------
-- STEP 4: Existing structured tables (migrate to Tiger instance)
--
-- These are the same tables defined in backend/database/models.py.
-- Keeping them on the same Tiger connection eliminates a second DB connection.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pr_review_records (
    id                TEXT        PRIMARY KEY,
    repo_full_name    TEXT        NOT NULL,
    pr_number         INT         NOT NULL,
    pr_title          TEXT        NOT NULL DEFAULT '',
    head_commit_sha   TEXT        NOT NULL DEFAULT '',
    diff_hash         TEXT        NOT NULL DEFAULT '',
    verdict           TEXT,
    status            TEXT        NOT NULL DEFAULT 'pending',
    overall_confidence REAL       NOT NULL DEFAULT 0.0,
    needs_human_review INT        NOT NULL DEFAULT 0,
    human_review_reason TEXT      NOT NULL DEFAULT '',
    github_review_id  BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS prr_repo_status_idx
    ON pr_review_records (repo_full_name, status, created_at DESC);

CREATE TABLE IF NOT EXISTS finding_records (
    id             TEXT        PRIMARY KEY,
    review_id      TEXT        NOT NULL REFERENCES pr_review_records(id),
    repo_full_name TEXT        NOT NULL,
    agent_type     TEXT        NOT NULL,
    severity       TEXT        NOT NULL,
    category       TEXT        NOT NULL,
    summary        TEXT        NOT NULL,
    file_path      TEXT,
    line_start     INT,
    line_end       INT,
    suggestion     TEXT,
    confidence     REAL        NOT NULL DEFAULT 0.5,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fr_review_sev_idx
    ON finding_records (review_id, severity);

CREATE INDEX IF NOT EXISTS fr_repo_sev_idx
    ON finding_records (repo_full_name, severity, created_at DESC);

CREATE TABLE IF NOT EXISTS repo_file_index (
    repo_path       TEXT        PRIMARY KEY,
    last_indexed_at TIMESTAMPTZ NOT NULL,
    token_count     INT,
    chunk_count     INT
);

-- HITL tables
CREATE TABLE IF NOT EXISTS hitl_reviews (
    id               TEXT        PRIMARY KEY,
    review_id        TEXT        NOT NULL,
    repo_full_name   TEXT        NOT NULL,
    pr_number        INT         NOT NULL,
    reason           TEXT        NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'pending',
    assigned_to      TEXT,
    decision         TEXT,
    decision_reason  TEXT,
    decided_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS hitl_status_idx
    ON hitl_reviews (status, created_at DESC);

CREATE TABLE IF NOT EXISTS hitl_feedback (
    id               TEXT        PRIMARY KEY,
    hitl_review_id   TEXT        NOT NULL REFERENCES hitl_reviews(id),
    reviewer         TEXT        NOT NULL,
    rating           INT,
    comment          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LLM call log (kept for backward compat, events spine is canonical going forward)
CREATE TABLE IF NOT EXISTS llm_call_log (
    id               TEXT        PRIMARY KEY,
    workflow_id      TEXT,
    agent_type       TEXT,
    model            TEXT        NOT NULL,
    prompt_tokens    INT         NOT NULL DEFAULT 0,
    completion_tokens INT        NOT NULL DEFAULT 0,
    total_tokens     INT         NOT NULL DEFAULT 0,
    cost_usd         REAL        NOT NULL DEFAULT 0.0,
    latency_ms       INT,
    success          INT         NOT NULL DEFAULT 1,
    error_message    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_workflow_idx
    ON llm_call_log (workflow_id, created_at DESC);

-- =============================================================================
-- Verification queries (run manually after migration):
--
--   SELECT extname FROM pg_extension WHERE extname IN ('timescaledb','vector','vectorscale');
--   SELECT hypertable_name FROM timescaledb_information.hypertables;
--   SELECT view_name FROM timescaledb_information.continuous_aggregates;
--   SELECT indexname FROM pg_indexes WHERE indexname = 'code_chunks_emb_idx';
-- =============================================================================
