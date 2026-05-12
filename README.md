# AI PR Review Agent

A production-grade AI Pull Request Review Agent.
A developer opens a PR. The agent reviews it automatically and posts structured
comments back. Multiple specialist sub-agents run in parallel. Humans stay in
the loop at the right checkpoints.

---

## What It Does

- Receives a GitHub PR webhook
- Runs 4 parallel specialist sub-agents: security, quality, test coverage, docs
- Each agent reasons about its domain using the PR diff + codebase context (RAG)
- Posts structured review comments back to the GitHub PR
- Routes low-confidence findings to a human approval queue
- Learns from merged vs rejected reviews over time

---

## Stack

  Backend:       FastAPI (Python)
  Orchestration: LangGraph (parallel fan-out, checkpointing)
  Job Queue:     Redis (rq)
  Memory:        Qdrant (vector/RAG) + Redis (short-term) + Postgres (structured)
  Sandbox:       Docker (isolated code execution, untrusted code)
  Frontend:      Next.js (review dashboard, HITL queue, trace viewer)
  Observability: OpenTelemetry (traces, token cost per agent span)
  CI/CD:         GitHub Actions with eval gates before every deploy

---

## Architecture

Modular Monolith for Phases 1-12. See docs/adr/ADR-002-architecture-style.md.
Orchestration engine: LangGraph. See docs/adr/ADR-001-orchestration-engine.md.

---

## 20-Phase Build Roadmap

  Phase 0:  Cognitive Design             - what the system thinks, autonomy level, HITL
  Phase 1:  System Architecture          - module boundaries, dependency rules, ADRs
  Phase 2:  Frontend Engineering         - dashboard shell, streaming, HITL queue UI
  Phase 3:  Backend & API Layer          - FastAPI skeleton, webhook receiver, state machine
  Phase 4:  Workflow Orchestration       - LangGraph graph, parallel fan-out, checkpointing
  Phase 5:  LLM & Reasoning Layer        - model routing, prompt registry, output schemas
  Phase 6:  Memory Architecture          - RAG pipeline, hybrid retrieval, freshness
  Phase 7:  Tooling & Sandboxing         - tool registry, Docker sandbox, capability scoping
  Phase 8:  Multi-Agent Systems          - 4 specialist agents, contracts, arbitration
  Phase 9:  Evaluation Systems           - golden dataset, LLM-as-judge, regression gates
  Phase 10: Observability & Tracing      - OTel spans, token cost attribution, alerts
  Phase 11: Security Architecture        - threat model, prompt injection, RBAC, audit trail
  Phase 12: Reliability Engineering      - retries, circuit breakers, idempotency, checkpointing
  Phase 13: Infrastructure & Deployment  - Docker Compose, k8s manifests, semantic cache
  Phase 14: Data Engineering             - ingestion pipeline, embedding freshness, schemas
  Phase 15: Governance & Compliance      - audit logs, explainability, data residency
  Phase 16: Economics & Cost Control     - token cost tracking, budget caps, routing efficiency
  Phase 17: Developer Experience         - prompt playground, trace viewer, replay tool
  Phase 18: CI/CD for AI                 - prompt versioning, eval gates, canary releases
  Phase 19: Human-in-the-Loop            - approval queue, escalation, dispute API, feedback
  Phase 20: Continuous Learning          - reflection loop, learning pipeline, convergence

---

## Local Development (available after Phase 3)

  cp .env.example .env          # fill in your GitHub token + secrets
  docker compose up             # starts Redis, Postgres, Qdrant
  uvicorn backend.main:app --reload

---

## Project Structure

  backend/        Python backend (modular monolith, 11 modules)
  frontend/       Next.js dashboard
  prompts/        Version-controlled prompt files per agent
  eval/           Golden dataset + regression test configs
  infra/          Dockerfiles, k8s manifests, scripts
  docs/adr/       Architecture Decision Records
  docs/runbooks/  Operational runbooks
  .github/        GitHub Actions workflows

---

## Key Constraints

  - Any developer can point this at their own GitHub repo
  - GitHub API downtime handled via idempotent webhook replay
  - Phase 7 sandbox hardening is a gate before Phase 8 begins
  - Every phase has a documented gate before the next phase starts
  - Prompts are versioned in git, deployed through eval gates only
