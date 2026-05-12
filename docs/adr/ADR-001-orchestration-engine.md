# ADR-001: Orchestration Engine Selection

## Status
Accepted

## Date
2026-05-12

## Context

The PR Review Agent needs a workflow engine to do three things:

1. Coordinate 4 parallel sub-agents (security, quality, test, docs) that run at the same time
2. Persist workflow state between steps so a crash does not lose work in progress
3. Handle retries cleanly when an LLM call or tool call fails

Two candidates were evaluated.

---

## Option A: LangGraph

LangGraph is a Python-native library that lets you define agent workflows
as a graph of nodes and edges.

How it works:
- You define nodes (functions or LLM calls)
- You define edges (what runs next, and under what condition)
- Parallel fan-out is done via the Send API (run multiple nodes at the same time)
- State is a typed Python dict that flows through the graph
- Checkpointing saves the state to Redis or Postgres at each node boundary
  so if the process crashes, it resumes from the last saved checkpoint

Pros:
- Runs inside our Python process, zero extra infrastructure
- Parallel fan-out is a first-class feature (exactly what we need for 4 agents)
- Checkpointing to Redis works with the same Redis we use for the job queue
- Tight integration with LLM tool-calling (OpenAI, Anthropic, etc.)
- Fast to iterate on locally

Cons:
- Newer library, API may evolve
- Not battle-hardened at very large scale (thousands of concurrent workflows)

---

## Option B: Temporal

Temporal is a distributed workflow engine. You write workflow code in Python,
and Temporal handles durability, retries, and state persistence.

Pros:
- Extremely battle-hardened (used by Uber, Netflix, etc.)
- Excellent durability guarantees
- Good fit at large scale

Cons:
- Requires running a Temporal server (separate Docker process or managed service)
- Requires Temporal worker processes separate from our FastAPI app
- Adds meaningful operational overhead before we understand our own workflow shapes
- Overkill for a team just starting out

---

## Decision

Use LangGraph as the orchestration engine for Phases 1 through 12.

The key discipline that makes this decision safe is this:

  All workflow execution goes through a single abstract interface defined in
  backend/core/workflow_engine.py

  The interface has three methods:
    - run(workflow_id, input)     -> start a workflow
    - resume(workflow_id, state)  -> resume a checkpointed workflow
    - get_state(workflow_id)      -> read current workflow state

  The LangGraph implementation lives in backend/orchestrator/langgraph_engine.py
  It implements the interface above.

  If we need Temporal in Phase 13+, we write a Temporal implementation of the same
  interface and swap it in. Nothing else in the codebase changes.

This is the "defer decisions" principle: we make the cheaper decision now and keep
the door open to the better decision when scale actually demands it.

---

## When to Revisit

Revisit this decision at Phase 13 (Infrastructure) if any of these are true:
- Concurrent workflow count exceeds 50 per minute sustained
- We need cross-service workflow coordination (microservices era)
- A workflow failure causes unacceptable data loss despite Redis checkpointing

---

## Consequences

- No Temporal infrastructure to manage in local dev or early staging
- Parallel fan-out works via LangGraph Send API
- Checkpointing uses Redis (same store as job queue, one fewer dependency)
- backend/core/workflow_engine.py interface must be defined before any workflow code
- All orchestrator code imports from core.workflow_engine, never from langgraph directly
