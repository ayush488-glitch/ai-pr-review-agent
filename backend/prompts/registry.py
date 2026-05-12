# backend/prompts/registry.py
#
# PromptRegistry — versioned prompt loader for all specialist agents.
#
# WHAT THIS FILE IS:
# The single source of truth for prompt content. Instead of prompts being
# inline strings inside agent classes (hardcoded, un-versioned, un-diff-able),
# they live in .txt files on disk at:
#   backend/prompts/templates/{agent_type}/v{N}.txt
#
# This file provides the load/resolve/cache API that agents call.
#
# WHY FILE-BASED (NOT DATABASE)?
# Prompts are code. They should live in git, be code-reviewed, and be diff-able.
# A prompt stored in a database is a change that bypasses code review.
# (Pragmatic Programmer: "Version everything. Treat prompts like code.")
# File-based also means no network call, no DB migration, no startup dependency.
#
# WHY VERSIONED?
# LLMOps-Essentials.md: "Run evals every time you change the system."
# You can't run evals on a prompt that has no version. v1, v2, ... gives you:
#   - A stable identifier to reference in eval results
#   - The ability to pin a specific version for regression testing
#   - A clear history of what changed and when (via git log)
#   - A/B testing: two agent instances running v1 vs v2 in shadow mode
#
# CACHING:
# Template files are read from disk once per process lifetime and cached
# in a module-level dict. This means:
#   - No repeated filesystem I/O during a review (LLM calls dominate latency anyway)
#   - Cache is intentionally NOT invalidated at runtime (restart to reload)
#   - In tests, you can call _clear_cache() to reset between test cases
#
# FALLBACK POLICY:
# If a template file is missing: raises PromptNotFoundError immediately.
# We do NOT silently fall back to an empty string or a default prompt.
# A missing template is a deployment error and should fail loudly.
# The agent's inline _system_prompt() method serves as the fallback at a higher
# level (see BaseAgent.analyze()) but registry.py itself never swallows errors.

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from backend.core.exceptions import PromptNotFoundError
from backend.models.enums import AgentType

logger = logging.getLogger(__name__)

# Root directory for all prompt templates.
# Resolved relative to this file's location so the registry works regardless
# of the working directory the server is started from.
_TEMPLATES_ROOT: Path = Path(__file__).parent / "templates"

# Version string pattern: exactly "v" followed by one or more digits.
# Valid: "v1", "v2", "v10". Invalid: "latest", "v1.2", "v1-beta".
_VERSION_RE = re.compile(r"^v(\d+)$")


@dataclass(frozen=True)
class PromptVersion:
    """
    A loaded, immutable prompt version.

    Stored in the module-level cache after the first disk read.
    frozen=True prevents accidental mutation in agent code.

    Fields:
        agent_type:   Which specialist this prompt belongs to.
        version_str:  Version identifier, e.g. "v1", "v2".
        content:      The full prompt text (not including the JSON format block,
                      which BaseAgent._build_system_prompt() prepends at call time).
        loaded_at:    UTC timestamp of when this version was read from disk.
                      Useful for debugging "which prompt was active when this
                      review ran" (Phase 10 Observability will log this).
    """
    agent_type: AgentType
    version_str: str
    content: str
    loaded_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class PromptRegistry:
    """
    Versioned prompt registry for all specialist agents.

    This is a singleton-style class — there is exactly one instance per process,
    stored as the module-level `registry` object at the bottom of this file.
    Agents import and call that instance; they do not instantiate PromptRegistry
    directly.

    RESOLUTION ORDER for load_prompt(agent_type, "latest"):
      1. Check cache for "<agent_type.value>:latest" — return if found
      2. Scan templates/{agent_type.value}/ for v{N}.txt files
      3. Sort by integer N, pick the highest
      4. Load that file's content
      5. Cache under both "<agent>:latest" AND "<agent>:v{N}" (deduplicated)
      6. Return content

    THREAD SAFETY:
    The cache dict is read/written from async coroutines that are all on the
    same event loop thread. No locking is needed. If this ever moves to a
    multi-threaded executor, add a threading.Lock around _cache writes.
    """

    # Module-level cache. Key: "{agent_type.value}:{version_str}".
    # Value: PromptVersion dataclass.
    # ClassVar so it's shared across all instances (though we only have one).
    _cache: ClassVar[dict[str, PromptVersion]] = {}

    def load_prompt(
        self,
        agent_type: AgentType,
        version: str = "latest",
    ) -> str:
        """
        Returns the prompt text for the given agent type and version.

        Args:
            agent_type: The specialist agent (AgentType.SECURITY, etc.)
            version:    Version string. "latest" resolves to the highest vN.txt
                        found on disk. Explicit versions: "v1", "v2", etc.

        Returns:
            The prompt text as a plain string (no JSON format block — BaseAgent
            adds that via _build_system_prompt()).

        Raises:
            PromptNotFoundError: If the template directory or version file
                                 does not exist. Deployment misconfiguration
                                 should fail loudly, not silently degrade.
        """
        # Resolve "latest" to a concrete version string first.
        # We cache the resolved concrete version, not "latest", so that
        # during a single process run the "latest" pointer is stable.
        resolved_version = version
        if version == "latest":
            resolved_version = self._resolve_latest(agent_type)

        cache_key = f"{agent_type.value}:{resolved_version}"

        # Cache hit — return immediately without disk I/O.
        if cache_key in self._cache:
            logger.debug("prompt_cache_hit | agent=%s version=%s", agent_type.value, resolved_version)
            return self._cache[cache_key].content

        # Cache miss — load from disk.
        content = self._load_from_disk(agent_type, resolved_version)

        prompt_version = PromptVersion(
            agent_type=agent_type,
            version_str=resolved_version,
            content=content,
        )
        self._cache[cache_key] = prompt_version

        # Also cache under the "latest" key if that's what was requested,
        # so subsequent load_prompt(..., "latest") calls are O(1) lookups.
        if version == "latest":
            latest_key = f"{agent_type.value}:latest"
            self._cache[latest_key] = prompt_version

        logger.info(
            "prompt_loaded | agent=%s version=%s chars=%d",
            agent_type.value, resolved_version, len(content),
        )
        return content

    def list_versions(self, agent_type: AgentType) -> list[str]:
        """
        Returns all available version strings for the given agent, sorted ascending.

        Example: ["v1", "v2", "v3"]

        Raises:
            PromptNotFoundError: If the template directory for this agent doesn't exist.
        """
        agent_dir = _TEMPLATES_ROOT / agent_type.value
        if not agent_dir.is_dir():
            raise PromptNotFoundError(
                agent_type=agent_type.value,
                version="*",
                message=f"Template directory not found: {agent_dir}",
            )

        versions = self._scan_versions(agent_dir)
        if not versions:
            raise PromptNotFoundError(
                agent_type=agent_type.value,
                version="*",
                message=f"No v{{N}}.txt files found in {agent_dir}",
            )
        return [f"v{n}" for n in sorted(versions)]

    def _resolve_latest(self, agent_type: AgentType) -> str:
        """
        Finds the highest version integer N in templates/{agent_type}/ and
        returns the string "v{N}".

        Raises PromptNotFoundError if no template files exist.
        """
        agent_dir = _TEMPLATES_ROOT / agent_type.value
        if not agent_dir.is_dir():
            raise PromptNotFoundError(
                agent_type=agent_type.value,
                version="latest",
                message=f"Template directory not found: {agent_dir}",
            )

        version_ints = self._scan_versions(agent_dir)
        if not version_ints:
            raise PromptNotFoundError(
                agent_type=agent_type.value,
                version="latest",
                message=f"No v{{N}}.txt files found in {agent_dir}",
            )

        latest_n = max(version_ints)
        return f"v{latest_n}"

    def _scan_versions(self, agent_dir: Path) -> list[int]:
        """
        Returns a list of integer version numbers found in the given directory.
        Only files named v{N}.txt (where N is a positive integer) are included.
        """
        version_ints: list[int] = []
        for f in agent_dir.iterdir():
            if f.is_file() and f.suffix == ".txt":
                m = _VERSION_RE.match(f.stem)
                if m:
                    version_ints.append(int(m.group(1)))
        return version_ints

    def _load_from_disk(self, agent_type: AgentType, version: str) -> str:
        """
        Reads the template file at templates/{agent_type}/{version}.txt.

        Raises PromptNotFoundError if the file doesn't exist.
        """
        template_path = _TEMPLATES_ROOT / agent_type.value / f"{version}.txt"
        if not template_path.is_file():
            raise PromptNotFoundError(
                agent_type=agent_type.value,
                version=version,
                message=f"Template file not found: {template_path}",
            )

        content = template_path.read_text(encoding="utf-8").strip()
        if not content:
            raise PromptNotFoundError(
                agent_type=agent_type.value,
                version=version,
                message=f"Template file is empty: {template_path}",
            )
        return content

    @classmethod
    def _clear_cache(cls) -> None:
        """
        Clears the in-process prompt cache.

        NOT called in normal operation — the cache is intentionally permanent
        for the lifetime of the process (no file watching, no hot-reload).

        Called in tests to reset state between test cases, and can be called
        manually during development if you want to force a reload without
        restarting the server.
        """
        cls._cache.clear()
        logger.debug("prompt_cache_cleared")


# Module-level singleton. Import this in agents and call registry.load_prompt().
# Consistent with how llm_client and get_settings() are used throughout.
registry = PromptRegistry()
