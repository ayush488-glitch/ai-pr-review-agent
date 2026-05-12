# backend/orchestrator/graph.py
#
# Assembles the LangGraph StateGraph from the node functions in nodes.py.
#
# WHAT IS A STATEGRAPH?
# A StateGraph is a directed graph where:
#   - NODES are async functions (defined in nodes.py)
#   - EDGES define execution order (which node runs after which)
#   - STATE (PRReviewState) is passed between nodes automatically
#
# OUR GRAPH SHAPE:
#
#   START
#     |
#     v
#   build_context          <- fetch PR diff from GitHub
#     |
#     v
#   fan_out_agents         <- run 4 agents in parallel (AGENTS_RUNNING)
#     |
#     v
#   aggregate_results      <- merge findings, decide verdict (AGGREGATING)
#     |
#     v
#   post_review            <- post to GitHub or HITL queue (POSTING)
#     |
#     v
#   END
#
# WHY LANGGRAPH AND NOT PLAIN ASYNCIO?
# We could write this as 4 async function calls in sequence. But LangGraph gives us:
#
#   1. CHECKPOINTING: after each node, LangGraph saves state to Redis.
#      If the server crashes mid-review, resume() reloads from the last checkpoint
#      and continues from there — not from the beginning.
#      (From distributed-systems wiki: "combine checkpointing with message logging.")
#
#   2. STATE MANAGEMENT: LangGraph merges partial state updates automatically.
#      Each node only returns what it changed — no need to copy the full state.
#
#   3. OBSERVABILITY HOOKS: LangGraph emits events before and after each node.
#      Phase 10 (Observability) will attach OpenTelemetry spans to these events
#      without changing the node functions at all.
#
#   4. CONDITIONAL EDGES: we can add branching logic later
#      (e.g. skip post_review and go straight to HITL if needed)
#      without rewriting the nodes.
#
# THE CHECKPOINTER:
# LangGraph's RedisSaver stores one checkpoint per node completion.
# The key format is: "checkpoint:{workflow_id}:{node_name}"
# resume() fetches the latest checkpoint and starts from the next node.

import logging

from langgraph.graph import END, START, StateGraph

from backend.orchestrator.nodes import (
    aggregate_results,
    build_context,
    fan_out_agents,
    post_review,
)
from backend.orchestrator.state import PRReviewState

logger = logging.getLogger(__name__)


def build_review_graph():
    """
    Constructs and compiles the PR review StateGraph.

    RETURNS a compiled LangGraph graph — a callable object.
    To run a review: await graph.ainvoke(initial_state, config={"configurable": {"thread_id": workflow_id}})

    WHY IS THIS A FUNCTION AND NOT MODULE-LEVEL CODE?
    If we built the graph at module import time, we could not inject the checkpointer
    at runtime (the checkpointer needs a live Redis connection).
    Calling build_review_graph() at startup (after Redis connects) is the clean pattern.

    CHECKPOINTER:
    For now: no checkpointer (MemorySaver is used as a placeholder).
    Phase 4 gate will replace this with RedisSaver once Redis is connected.
    The graph shape and node wiring is identical — only the checkpointer changes.
    """
    # Step 1: Create a StateGraph that uses PRReviewState as its state schema.
    # LangGraph reads the TypedDict annotations to know what fields exist.
    workflow = StateGraph(PRReviewState)

    # Step 2: Register each node function with a name.
    # The name is used in edges, checkpoints, and log messages.
    workflow.add_node("build_context", build_context)
    workflow.add_node("fan_out_agents", fan_out_agents)
    workflow.add_node("aggregate_results", aggregate_results)
    workflow.add_node("post_review", post_review)

    # Step 3: Wire the edges (execution order).
    # START -> build_context: the graph always starts here
    workflow.add_edge(START, "build_context")

    # build_context -> fan_out_agents: always runs after context is ready
    workflow.add_edge("build_context", "fan_out_agents")

    # fan_out_agents -> aggregate_results: always runs after all agents finish
    workflow.add_edge("fan_out_agents", "aggregate_results")

    # aggregate_results -> post_review: always runs (post_review handles HITL internally)
    # NOTE: We could add a conditional edge here to route to a HITL node instead.
    # For Phase 4, post_review handles both paths internally.
    # Phase 19 will split this into: post_review (auto) vs hitl_queue (human).
    workflow.add_edge("aggregate_results", "post_review")

    # post_review -> END: the graph is done
    workflow.add_edge("post_review", END)

    # Step 4: Set the entry point explicitly.
    # LangGraph needs to know where to start. START -> build_context already
    # does this, but set_entry_point makes it explicit and self-documenting.
    workflow.set_entry_point("build_context")

    # Step 5: Compile the graph.
    # compile() validates the graph (checks for unreachable nodes, missing edges)
    # and returns a CompiledGraph object that can be invoked.
    #
    # CHECKPOINTER NOTE:
    # In production (after Phase 4 Redis setup): pass checkpointer=redis_saver
    # For now: no checkpointer. State lives only in memory during this run.
    # This means resume() is not yet functional — we add that in the Redis section.
    compiled = workflow.compile()

    logger.info("PR review graph compiled successfully. Nodes: %s", list(workflow.nodes))

    return compiled


# Module-level compiled graph instance.
# Built once when this module is first imported.
# The LangGraph engine imports this directly.
#
# WHY MODULE-LEVEL?
# The compiled graph is stateless — it does not hold any per-review data.
# Per-review state lives in the checkpointer (keyed by workflow_id/thread_id).
# So we can safely share one compiled graph across all concurrent reviews.
#
# CONCURRENCY:
# LangGraph compiled graphs are safe to use concurrently.
# Each .ainvoke() call gets its own state isolated by thread_id (= workflow_id).
review_graph = build_review_graph()
