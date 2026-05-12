# ADR-002: Architecture Style - Modular Monolith

## Status
Accepted

## Date
2026-05-12

## Context

Before writing a single line of code, we need to decide how the system is
structured as a whole. This decision affects everything: how we run it locally,
how we deploy it, how we debug it, and how we scale it later.

Two realistic options for a new project of this size:

---

## Option A: Microservices

Each module (webhook receiver, orchestrator, agents, memory, etc.) is a
separate deployable service with its own process, its own Docker container,
and its own network address.

Pros:
- Each service scales independently
- A crash in one service does not take down the others

Cons:
- On day 1, you have no idea where your real service boundaries are
- Every call between modules becomes a network call (latency, failure, serialization)
- Local development requires running 8+ processes simultaneously
- Distributed tracing is required from the very start
- Deployment complexity is 5x higher before you have a working product

This is a premature optimization. It solves problems we do not yet have.

---

## Option B: Modular Monolith (our choice)

One Python process. One FastAPI app. But the internal code is organized into
modules with strict rules about what can depend on what.

The key insight: a modular monolith is not a "big ball of mud."
It is a disciplined single-process application where module boundaries are
enforced by convention and code review, not by network calls.

When we need to scale later, we extract a module into its own service.
The extraction is clean because the boundaries were clean from the start.

---

## Decision

Build a Modular Monolith for Phases 1 through 12.

---

## The 11 Modules

Each module has a single responsibility. Here is what each one does:

  core
    The foundation. Contains: abstract base classes, shared interfaces,
    constants, exception types, and utility functions.
    No module is allowed to depend on anything except the Python standard library.
    Everything else depends on core. Core depends on nothing.

  models
    Pydantic data schemas only. No business logic. No database calls.
    Contains: PRReview, Finding, AgentResult, ReviewVerdict, WebhookEvent, etc.
    Depends on: core only.

  config
    Loads environment variables and settings using pydantic-settings.
    Single source of truth for all configuration (API keys, DB URLs, thresholds).
    Depends on: core only.

  webhook_receiver
    Receives HTTP POST requests from GitHub.
    Validates the HMAC-SHA256 signature on every request (security gate).
    Parses the payload into a WebhookEvent model.
    Enqueues the job. Returns 200 immediately (do not make GitHub wait).
    Depends on: models, config, job_queue, core.

  job_queue
    Redis-backed queue. Receives WebhookEvent jobs.
    Dispatches jobs to the orchestrator.
    Handles retry logic for failed dispatches.
    Depends on: models, config, core.

  orchestrator
    LangGraph workflow graphs.
    Receives a job, builds the review context, fans out to 4 agents in parallel,
    collects results, produces a ReviewVerdict.
    Depends on: agents, memory, models, core.

  agents
    4 specialist sub-agents. Each has its own folder.
    security_agent:  finds vulnerabilities, injection risks, secrets in code
    quality_agent:   finds correctness bugs, code smells, logic errors
    test_agent:      evaluates test coverage, missing test cases, edge cases
    docs_agent:      finds missing or outdated documentation
    Each agent: receives a code context, calls an LLM, returns structured Finding list.
    Depends on: tools, memory, models, core.

  memory
    Three tiers:
      redis_store:    short-term session memory (current review in progress)
      qdrant:         vector store for codebase RAG (repo files embedded + indexed)
      postgres_store: structured long-term storage (review history, outcomes, disputes)
    Depends on: models, config, core.

  tools
    tool_registry:  catalog of tools each agent is allowed to use
    github_client:  wrapper around GitHub REST API with retry + rate limiting
    sandbox:        Docker-isolated code execution (runs untrusted code safely)
    Depends on: models, config, core.

  auth
    JWT token validation.
    RBAC: roles are DEVELOPER, REVIEWER, ADMIN, OVERRIDE.
    GitHub OAuth flow for web dashboard login.
    Depends on: models, config, core.

  hitl
    Approval queue: stores low-confidence findings waiting for human decision.
    Escalation engine: pages a human when severity is CRITICAL.
    Dispute API: lets a developer mark a posted finding as incorrect.
    Depends on: models, config, auth, core.

  evaluation
    Golden dataset runner: runs known-bug PRs through the agent, checks findings.
    Regression harness: compares current agent output against expected output.
    Depends on: agents, models, core.

  observability
    OpenTelemetry instrumentation.
    Cost attribution: tracks token usage and cost per agent span.
    Injected as middleware, not imported by business logic.
    Depends on: models, config, core.

---

## The Dependency Direction Rule

This rule is the entire discipline of a modular monolith.
Violating it turns a modular monolith into a big ball of mud.

  RULE: Dependencies flow inward only. Outer modules depend on inner modules.
        Inner modules never depend on outer modules.

  Dependency order (innermost to outermost):

    core
      ^
    models, config
      ^
    memory, tools, auth
      ^
    agents
      ^
    orchestrator
      ^
    webhook_receiver, job_queue, hitl, evaluation
      ^
    observability (cross-cutting, injected via middleware)

  If you are writing code in agents/ and find yourself importing from orchestrator/,
  STOP. That is a dependency violation. Refactor before continuing.

  If you are writing code in tools/ and find yourself importing from agents/,
  STOP. Same problem.

  The test for a clean boundary: you can delete any outer module and the inner
  modules still compile and run.

---

## When to Extract a Module into Its Own Service

At Phase 13 (Infrastructure), evaluate these triggers:

  - sandbox needs to scale independently (many PRs with large code execution)
  - webhook_receiver is receiving so many events the job queue becomes a bottleneck
  - orchestrator needs GPU-optimized instances that differ from the API server

Only extract when a trigger is real and measured, not anticipated.

---

## Consequences

- Single process: easy local dev (one uvicorn command runs everything)
- Clean boundaries now: extraction to microservices is cheap later
- All inter-module calls are direct Python function calls (fast, type-safe)
- backend/core/workflow_engine.py interface defined before any workflow code
- Every new module must declare its allowed dependencies in a comment at the top
  of its __init__.py file
