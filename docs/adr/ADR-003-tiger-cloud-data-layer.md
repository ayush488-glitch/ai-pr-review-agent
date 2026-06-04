# ADR-003: Tiger Cloud as the Unified Data Layer

**Status:** Accepted  
**Date:** 2026-06-03  
**Author:** Ayush Singh  
**Supersedes:** None (new decision)  
**Related:** ADR-001 (LangGraph orchestrator), ADR-002 (modular monolith)

---

## Context

The v1 architecture used three separate stores:

| Store | Role | Pain point |
|---|---|---|
| Qdrant | Vector search / RAG | Separate container, separate backup, extra dependency |
| PostgreSQL (Neon) | Structured history | No time-series native support, manual cost rollup queries |
| Redis (Upstash) | ARQ job queue | Stays — this decision does not touch the queue |

As the project scales from 10 PRs/day to 10,000, three problems emerge:

1. **Operational complexity.** Three stores means three connection pools, three backup policies, three failure modes to instrument.
2. **Cost attribution is hard.** Per-PR cost rollup requires joining LLM call logs (Postgres) with token records (also Postgres) — no time-series aggregation, full scans at scale.
3. **Vector DB latency degrades.** Qdrant's HNSW index degrades beyond ~10M vectors without reindexing. pgvectorscale's DiskANN maintains 99% recall at 100M+ vectors with disk-streaming access patterns.

## Decision

Replace Qdrant and migrate plain PostgreSQL to **Tiger Cloud (TimescaleDB)**.

Tiger Cloud is a managed TimescaleDB Postgres instance that adds:
- **pgvectorscale**: DiskANN index over 1536-dim embeddings — replaces Qdrant
- **Hypertables**: time-ordered append-only tables with automatic partitioning — replaces manual audit tables
- **Continuous aggregates**: pre-materialized rollups refreshed on a schedule — replaces GROUP BY scans

Redis (Upstash) stays. The job queue has no benefit from Tiger and migrating it adds complexity for no gain.

## Consequences

### What changes

| Module | Before | After |
|---|---|---|
| `backend/memory/` | `qdrant_client.py` + Qdrant HTTP | `tiger_client.py` + pgvectorscale DiskANN |
| `backend/data/ingestion.py` | `upsert_code_chunks()` to Qdrant | `TigerMemoryClient.upsert_chunks()` to Tiger |
| `backend/observability/events.py` | Log-only events | `AgentEvent` dataclass + `emit_agent_event()` to hypertable |
| `backend/economics/cost_repository.py` | Full-scan GROUP BY | Reads from `agent_health_1m` + `pr_cost_hourly` aggregates |
| `backend/database/postgres.py` | SQLAlchemy only | + `init_tiger_schema()` + bare asyncpg pool |
| `docker-compose.yml` | `postgres:15` + `qdrant` | `timescale/timescaledb-ha:pg16` only |

### Why DiskANN beats HNSW at code RAG scale

HNSW (Qdrant) builds the entire index in memory. At 10M 1536-dim vectors that is ~60GB RAM. At 100M vectors: infeasible on a single node.

DiskANN (pgvectorscale) uses **Statistical Binary Quantization (SBQ)**: vectors are compressed to disk-friendly bit segments. The search algorithm reads compressed segments sequentially — matching disk access patterns. Result: 28x lower p95 latency and 16x higher throughput vs Pinecone s1 at 99% recall (Timescale benchmark, 50M Cohere embeddings, 768 dims).

For code review RAG (tens of thousands of files per repo, not billions), DiskANN is strictly better than HNSW and cheaper than a dedicated Qdrant deployment.

### Why Hypertables beat plain Postgres for agent events

Agent events are append-only and time-ordered. Every LLM call, every tool call, every span is a new row — nothing is ever updated. This is a perfect time-series workload.

TimescaleDB hypertables partition the table by time interval (default: 1 day). This means:
- Queries scoped to the last hour scan 1 partition, not the whole table
- Chunk-level compression reduces storage by 90%+ on older data
- `time_bucket()` aggregations run natively with query-planner awareness

Plain Postgres on the same data requires table partitioning by hand and manual `PARTITION BY RANGE` DDL. Hypertables do this automatically.

### Why Continuous Aggregates beat GROUP BY for the economics dashboard

The economics dashboard needs per-agent cost, p95 latency, and rejection rate. With plain Postgres, every page load runs `SELECT agent, sum(cost_usd) FROM agent_events WHERE ts > ... GROUP BY agent`. At 1M rows this is fine. At 100M rows this is a 10-second scan.

Continuous aggregates (`agent_health_1m`, `pr_cost_hourly`) pre-materialize these rollups on a 1-minute and 1-hour schedule respectively. Dashboard queries hit pre-computed rows — sub-millisecond at any scale.

### Trade-offs accepted

| Trade-off | Rationale |
|---|---|
| Tiger Cloud is a paid managed service | Replaces two paid services (Neon + Qdrant). Net cost is neutral or lower. |
| asyncpg pool sits alongside SQLAlchemy pool | SQLAlchemy ORM is needed for structured ORM queries. asyncpg is needed for bulk insert hot paths. Two pools, one host. |
| Migration is not zero-downtime | This is a greenfield integration (new repo). No live data to migrate. |

## Alternatives Considered

| Option | Rejected because |
|---|---|
| Keep Qdrant + add TimescaleDB | Three stores → more complexity, not less |
| Use pgvector without pgvectorscale | No DiskANN. HNSW only. Degrades at scale. |
| Use ClickHouse for events | Not Postgres-compatible. Separate connection. Overkill for this scale. |
| Use a dedicated OTel backend (Jaeger, Tempo) | Adds a 4th store. We already have Postgres. Single store wins. |

## Implementation Phases

- **Phase A (infra):** Provision Tiger Cloud, run `2026-06-tiger-init.sql`, verify extensions
- **Phase B (events):** Wire `emit_agent_event()` in orchestrator nodes and LLM client
- **Phase C (memory):** Delete `qdrant_client.py`, test hybrid retrieval end-to-end
- **Phase D (dashboard):** Wire continuous aggregate endpoints to frontend economics page

## References

- [pgvectorscale benchmarks](https://www.timescale.com/blog/pgvectorscale-vs-pinecone/)
- [TimescaleDB continuous aggregates docs](https://docs.timescale.com/use-timescale/latest/continuous-aggregates/)
- [DiskANN paper](https://proceedings.neurips.cc/paper/2019/hash/09853c7fb1d3f8ee67a61b6bf4a7f8e6-Abstract.html)
- `scripts/migrations/2026-06-tiger-init.sql` — full idempotent schema DDL
- `backend/memory/tiger_client.py` — TigerMemoryClient implementation
