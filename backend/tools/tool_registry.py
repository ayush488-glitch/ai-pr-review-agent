# backend/tools/tool_registry.py
#
# Tool Registry — the single source of truth for every callable tool in the system.
#
# WHY THIS EXISTS (from wiki: Tool-Use-Pattern):
#   "Without tools, an agent is just a conversationalist."
#   "If the LLM does not understand what a tool does, it will misuse it."
#
#   Tools give agents hands. But unrestricted tool access is dangerous — agents can
#   hallucinate tool names, pass wrong argument types, or call write tools when only
#   read access was intended. The registry solves this by:
#     1. Being the ONLY place tools are defined (name, schema, handler — together)
#     2. Validating tool names before execution (unknown tool = KeyError, not silent crash)
#     3. Enforcing typed input schemas so bad args are caught before the handler runs
#
# ARCHITECTURE (Clean Architecture — Interface Adapters layer):
#   - tools/ is one layer below agents/ in the dependency graph
#   - agents call tool_registry.call() — they never import handler functions directly
#   - handler functions live here, not in agents, so agents stay focused on reasoning
#
# WHAT LIVES HERE:
#   ToolSchema     — name + description + JSON Schema for inputs (what the LLM sees)
#   ToolDefinition — schema + the actual Python callable that executes it
#   ToolRegistry   — register / get / list / call
#   4 pre-registered tools (the deterministic ones that don't need LLM calls)
#
# WHAT DOES NOT LIVE HERE:
#   - Capability scope enforcement (which agent can call which tool) -> capability_scope.py
#   - Docker sandboxing for arbitrary code -> sandbox.py (subprocess for now, Docker in Phase 13)
#   - LLM calls -> tools/llm_client.py
#
# DEPENDENCY: core, models, config  (no upward deps — no imports from agents/)

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSchema:
    """
    The public contract for a tool — what the agent (and LLM) sees.

    WHY frozen=True:
        Tool schemas are definitional. Once registered, a schema should never
        mutate. Mutating a schema mid-run would cause agents to form calls
        against a different contract than the one the handler was built for.

    Fields:
        name        — machine identifier, must be unique in the registry
        description — human/LLM-readable purpose. MUST be precise (anti-pattern:
                      vague descriptions cause tool misuse — see wiki)
        input_schema — JSON Schema dict. Only "object" type with "properties" and
                       "required" is supported right now (sufficient for all Phase 7 tools)
        output_description — what the handler returns (helps agents interpret results)
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    output_description: str


@dataclass
class ToolDefinition:
    """
    A registered tool — schema + the Python callable that implements it.

    WHY schema and handler are kept together:
        If they were separate (schema in one dict, handlers in another), it's
        possible to register a schema with no handler, or update one without
        the other. Keeping them co-located makes inconsistency impossible.

    handler signature:  (args: dict) -> dict
        - args is the validated input dict (keys match input_schema)
        - return is always a dict (never a raw scalar, so callers have a
          consistent shape to deserialize)
    """
    schema: ToolSchema
    handler: Callable[[dict[str, Any]], dict[str, Any]]


# ---------------------------------------------------------------------------
# Registry class
# ---------------------------------------------------------------------------

class ToolRegistry:
    """
    Central registry of all tools callable by specialist agents.

    Pattern (wiki: Tool-Use-Pattern):
        "validate tool names and argument schemas against the registered tool
        list before execution" — that is exactly what this registry does.

    Thread safety:
        _tools is populated at module load time (single-threaded import).
        After import, only reads happen. No locking needed.

    Usage:
        from backend.tools.tool_registry import tool_registry
        result = tool_registry.call("check_secrets_pattern", {"text": diff_chunk})
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, tool_def: ToolDefinition) -> None:
        """
        Add a tool to the registry.

        WHY we guard against duplicate names:
            A duplicate registration would silently overwrite the existing
            handler. The second team's tool replaces the first team's without
            any warning. Loud failure is safer.
        """
        name = tool_def.schema.name
        if name in self._tools:
            raise ValueError(
                f"Tool '{name}' is already registered. "
                "Each tool name must be globally unique. "
                "If you are replacing a tool, call deregister() first."
            )
        self._tools[name] = tool_def
        logger.debug("Tool registered: %s", name)

    def deregister(self, name: str) -> None:
        """Remove a tool (used in tests for isolation)."""
        self._tools.pop(name, None)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> ToolDefinition:
        """
        Retrieve a tool by name. Raises KeyError on unknown names.

        WHY KeyError (not None):
            Silent None return means the caller has to remember to check for
            None before using the result. KeyError makes the bug loud
            immediately — the agent tried to invoke a tool that doesn't exist.
        """
        try:
            return self._tools[name]
        except KeyError:
            available = sorted(self._tools.keys())
            raise KeyError(
                f"Unknown tool '{name}'. Available tools: {available}"
            ) from None

    def list_names(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())

    def get_schemas(self) -> list[ToolSchema]:
        """Return all schemas (used to populate LLM tool-call prompts)."""
        return [td.schema for td in self._tools.values()]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """
        Validate the tool name then invoke its handler.

        WHY validation is the registry's responsibility (not the handler's):
            If each handler validates its own inputs, some will forget to.
            Centralised validation here means every tool gets it automatically.

        Input validation is intentionally LIGHT here:
            We check that required fields are present; we do NOT do deep type
            checking. Full JSON Schema validation (jsonschema library) is Phase 17
            — it adds a dependency and latency that isn't worth it for Phase 7.
        """
        tool_def = self.get(name)  # raises KeyError if unknown

        # Validate required arguments are present
        required_fields: list[str] = (
            tool_def.schema.input_schema.get("required", [])
        )
        missing = [f for f in required_fields if f not in args]
        if missing:
            raise ValueError(
                f"Tool '{name}' called with missing required arguments: {missing}. "
                f"Provided: {list(args.keys())}"
            )

        logger.debug("Invoking tool '%s' with args: %s", name, list(args.keys()))
        result = tool_def.handler(args)

        # Handlers must return dicts — this is a hard invariant
        if not isinstance(result, dict):
            raise TypeError(
                f"Tool '{name}' handler returned {type(result).__name__} instead of dict. "
                "All tool handlers must return dict[str, Any]."
            )

        return result


# ---------------------------------------------------------------------------
# Tool 1: check_secrets_pattern
#
# WHY THIS TOOL EXISTS:
#   SecurityAgent's LLM call looks for secrets conceptually, but regex-based
#   detection is deterministic and 100% reliable for literal patterns.
#   Wiki (Autonomous-Action-Agents): "Don't try to prove code is correct
#   (intractable), just block known-bad patterns."
#   This is exactly that — a blocklist-based detector for high-confidence patterns.
#
# PATTERNS COVERED:
#   - Generic: secret/password/key/token = "..." (string literals)
#   - AWS: AKIA... pattern (20-char alphanumeric starting with AKIA)
#   - Private key PEM headers
#   - Connection strings with embedded passwords: "://user:pass@host"
#
# WHY NOT just use truffleHog or gitleaks here:
#   External tools are Phase 13 (infrastructure). For Phase 7 we implement the
#   pattern ourselves to keep the dependency footprint zero.
# ---------------------------------------------------------------------------

# Compiled at module load time — cheap to call repeatedly
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "generic_assignment",
        re.compile(
            r'(?i)(password|passwd|secret|api_?key|auth_?token|access_?token|private_?key)'
            r'\s*[=:]\s*["\']([^"\']{4,})["\']',
            re.MULTILINE,
        ),
    ),
    (
        "aws_access_key",
        re.compile(r'AKIA[0-9A-Z]{16}', re.MULTILINE),
    ),
    (
        "pem_private_key",
        re.compile(r'-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----', re.MULTILINE),
    ),
    (
        "connection_string_with_password",
        re.compile(
            r'[a-zA-Z+]+://[^:@\s]+:[^@\s]{4,}@[^\s]+',
            re.MULTILINE,
        ),
    ),
]


def _check_secrets_pattern_handler(args: dict[str, Any]) -> dict[str, Any]:
    """
    Scan text for hardcoded secrets using compiled regex patterns.

    Returns:
        {
          "found": bool,
          "matches": [{"pattern_name": str, "match_preview": str, "line": int}, ...]
        }

    WHY match_preview (not the full match):
        Full match would include the actual secret value in logs and agent context.
        Preview is the first 20 chars — enough for the agent to understand context
        without leaking the real value into traces or JSON output.
    """
    text: str = args["text"]
    matches: list[dict[str, Any]] = []

    lines = text.splitlines()
    for line_no, line in enumerate(lines, start=1):
        for pattern_name, pattern in _SECRET_PATTERNS:
            for m in pattern.finditer(line):
                # Redact: show only first 20 chars of the matched string
                preview = m.group(0)[:20] + ("..." if len(m.group(0)) > 20 else "")
                matches.append({
                    "pattern_name": pattern_name,
                    "match_preview": preview,
                    "line": line_no,
                })

    return {
        "found": len(matches) > 0,
        "match_count": len(matches),
        "matches": matches,
    }


_TOOL_CHECK_SECRETS = ToolDefinition(
    schema=ToolSchema(
        name="check_secrets_pattern",
        description=(
            "Scan a code diff or file content for hardcoded secrets: passwords, "
            "API keys, AWS credentials, private key PEM headers, and connection "
            "strings with embedded passwords. Returns found=True if any match, "
            "plus line numbers and redacted previews. READ-ONLY — does not modify "
            "any files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The raw text (diff chunk or file content) to scan.",
                }
            },
            "required": ["text"],
        },
        output_description=(
            '{"found": bool, "match_count": int, "matches": [{"pattern_name": str, '
            '"match_preview": str, "line": int}]}'
        ),
    ),
    handler=_check_secrets_pattern_handler,
)


# ---------------------------------------------------------------------------
# Tool 2: run_syntax_check
#
# WHY THIS TOOL EXISTS:
#   TestAgent and QualityAgent look for code that won't compile. An LLM can
#   hallucinate "this looks fine" on broken code. A syntax check is deterministic.
#
# IMPLEMENTATION NOTE:
#   Actual subprocess execution lives in sandbox.py (resource limits, timeout).
#   This tool is a thin wrapper that delegates to the Sandbox class.
#   We import sandbox here (not at module top) to keep the dependency explicit.
# ---------------------------------------------------------------------------

def _run_syntax_check_handler(args: dict[str, Any]) -> dict[str, Any]:
    """
    Run a syntax check on a code snippet.

    Delegates to Sandbox to enforce timeout + output limits.
    Returns:
        {"valid": bool, "errors": [str], "language": str}
    """
    # Lazy import to avoid circular dependency at module level.
    # sandbox.py imports nothing from tools/, so the cycle doesn't exist,
    # but lazy import keeps module load order explicit.
    from backend.tools.sandbox import Sandbox, SandboxViolationError

    code: str = args["code"]
    language: str = args.get("language", "python").lower()

    sandbox = Sandbox()
    try:
        result = sandbox.run_syntax_check(code, language)
    except SandboxViolationError as e:
        return {
            "valid": False,
            "errors": [f"SandboxViolationError: {e}"],
            "language": language,
        }

    return {
        "valid": result.exit_code == 0 and not result.timed_out,
        "errors": [result.stderr] if result.stderr.strip() else [],
        "language": language,
        "execution_time_ms": result.execution_time_ms,
    }


_TOOL_RUN_SYNTAX_CHECK = ToolDefinition(
    schema=ToolSchema(
        name="run_syntax_check",
        description=(
            "Compile-check a code snippet for syntax errors without executing it. "
            "Supports Python (compile), JavaScript and TypeScript (node --check). "
            "Returns valid=True if no syntax errors, plus a list of error messages. "
            "READ-ONLY sandbox — does not install packages, write files, or make "
            "network calls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The source code snippet to check.",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "javascript", "typescript"],
                    "description": (
                        "Programming language of the snippet. "
                        "Determines which checker is invoked."
                    ),
                },
            },
            "required": ["code", "language"],
        },
        output_description=(
            '{"valid": bool, "errors": [str], "language": str, "execution_time_ms": int}'
        ),
    ),
    handler=_run_syntax_check_handler,
)


# ---------------------------------------------------------------------------
# Tool 3: search_similar_findings
#
# WHY THIS TOOL EXISTS:
#   When an agent detects a pattern, it's useful to know: "Has this repo seen
#   this finding before?" If we found SQL injection in auth.py last week and
#   the same pattern appears in payments.py today, that's a systemic issue.
#   This tool wraps the existing Qdrant search from Phase 6 (memory layer).
#
# WHY ALL AGENTS HAVE ACCESS TO THIS (not just SecurityAgent):
#   QualityAgent: "Is this code pattern inconsistent with the rest of the codebase?"
#   TestAgent:    "Are there similar tests in the repo we can reference?"
#   DocsAgent:    "How is similar code documented elsewhere?"
# ---------------------------------------------------------------------------

def _search_similar_findings_handler(args: dict[str, Any]) -> dict[str, Any]:
    """
    Search Qdrant for code chunks or findings similar to the query string.

    WHY graceful fallback (not an exception on Qdrant failure):
        Wiki (context_retriever.py principle): "RAG context is an enhancement,
        never a hard dependency." If Qdrant is unavailable, agents should still
        be able to complete their review — just without similar-finding context.

    Returns:
        {"results": [{"text": str, "score": float, "metadata": dict}], "count": int}
    """
    query: str = args["query"]
    limit: int = int(args.get("limit", 5))

    try:
        # Import the existing memory layer (Phase 6) — lazy to keep dependency direction
        from backend.memory.embedder import embed_text
        # TIGER: was qdrant_client.search_similar_code
        from backend.memory.tiger_client import get_tiger_memory
        # TODO: wire embedding + repo args — see context_retriever.py for pattern
        # chunks = await get_tiger_memory().search(query_embedding, repo=repo, hybrid=True, query_text=query_text, top_k=5)

        embedding = embed_text(query)
        raw_results = get_tiger_memory().search(embedding, hybrid=True, query_text=query, top_k=limit)

        results = [
            {
                "text": r.get("text", ""),
                "score": float(r.get("score", 0.0)),
                "metadata": r.get("metadata", {}),
            }
            for r in raw_results
        ]
        return {"results": results, "count": len(results)}

    except Exception as exc:
        # Log but don't raise — tool failure should degrade gracefully
        logger.warning(
            "search_similar_findings: Qdrant search failed, returning empty results. "
            "Error: %s",
            exc,
        )
        return {"results": [], "count": 0, "degraded": True, "error": str(exc)}


_TOOL_SEARCH_SIMILAR = ToolDefinition(
    schema=ToolSchema(
        name="search_similar_findings",
        description=(
            "Search the codebase knowledge base for code chunks or findings similar "
            "to the provided query string. Uses vector similarity (Qdrant). Useful "
            "for detecting systemic issues (same pattern in multiple files), "
            "referencing related tests, or finding how similar code is documented. "
            "Returns empty results gracefully if the knowledge base is unavailable. "
            "READ-ONLY."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language or code snippet to search for. "
                        "e.g. 'SQL injection in authentication code' or "
                        "the actual suspicious code fragment."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of results to return (default: 5, max: 20).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        output_description=(
            '{"results": [{"text": str, "score": float, "metadata": dict}], '
            '"count": int, "degraded"?: bool}'
        ),
    ),
    handler=_search_similar_findings_handler,
)


# ---------------------------------------------------------------------------
# Tool 4: get_dependency_advisory
#
# WHY THIS TOOL EXISTS:
#   SecurityAgent needs to flag new dependencies that have known CVEs.
#   Full OSV.dev / GitHub Advisory API integration is Phase 14 (Data Engineering).
#   For Phase 7 we register a stub that returns a clear "not yet implemented"
#   result — this means SecurityAgent can start calling it and the Phase 14
#   team can drop the real implementation in without changing any agent code.
#
# WHY A STUB (not just nothing):
#   Wiki (Tool-Use-Pattern): "Specific tools beat generic tools — the LLM has
#   unambiguous intent signals." Registering a real schema for a stubbed tool
#   means the schema is defined and stable. Phase 14 replaces only the handler.
# ---------------------------------------------------------------------------

def _get_dependency_advisory_handler(args: dict[str, Any]) -> dict[str, Any]:
    """
    Stub: Phase 14 will replace this with a real OSV.dev / GitHub Advisory lookup.

    Returns:
        {"package": str, "version": str, "advisories": [], "stub": true}
    """
    package: str = args["package"]
    version: str = args.get("version", "unknown")

    logger.info(
        "get_dependency_advisory called for %s@%s — stub, no real lookup yet. "
        "Phase 14 will implement OSV.dev integration.",
        package,
        version,
    )

    return {
        "package": package,
        "version": version,
        "advisories": [],
        "stub": True,
        "message": (
            "Advisory lookup not yet implemented. "
            "Phase 14 will integrate OSV.dev and GitHub Advisory Database."
        ),
    }


_TOOL_DEPENDENCY_ADVISORY = ToolDefinition(
    schema=ToolSchema(
        name="get_dependency_advisory",
        description=(
            "Look up known security advisories (CVEs) for a package and version. "
            "Currently a stub — always returns empty advisories. "
            "Phase 14 will implement real OSV.dev / GitHub Advisory Database lookups. "
            "READ-ONLY."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "Package name, e.g. 'requests', 'lodash'.",
                },
                "version": {
                    "type": "string",
                    "description": "Package version string, e.g. '2.28.0'.",
                },
            },
            "required": ["package"],
        },
        output_description=(
            '{"package": str, "version": str, "advisories": list, '
            '"stub": bool, "message"?: str}'
        ),
    ),
    handler=_get_dependency_advisory_handler,
)


# ---------------------------------------------------------------------------
# Module-level singleton — registered at import time
#
# WHY singleton (not a class method or factory):
#   Agents import tool_registry and call tool_registry.call(...).
#   If we used a factory pattern, agents would need to create or inject the
#   registry, which is unnecessary coupling for a system-wide utility.
#   A module-level singleton is idiomatic Python for registries.
#
# WHY register at import time (not lazily):
#   Fail fast. If a tool definition is broken (bad schema, missing handler),
#   the error surfaces at server startup — not on the first PR review at 2 AM.
# ---------------------------------------------------------------------------

tool_registry = ToolRegistry()
tool_registry.register(_TOOL_CHECK_SECRETS)
tool_registry.register(_TOOL_RUN_SYNTAX_CHECK)
tool_registry.register(_TOOL_SEARCH_SIMILAR)
tool_registry.register(_TOOL_DEPENDENCY_ADVISORY)

logger.debug(
    "ToolRegistry initialised with %d tools: %s",
    len(tool_registry.list_names()),
    tool_registry.list_names(),
)
