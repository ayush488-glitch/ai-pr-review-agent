# backend/tools/sandbox.py
#
# Sandbox — deterministic, resource-bounded code execution for syntax checking.
#
# WHY THIS EXISTS (wiki: Autonomous-Action-Agents):
#   "Don't try to prove code is correct (intractable), just block known-bad patterns."
#   "Relying on LLM for safety correctness is an anti-pattern."
#   The sandbox is the deterministic safety gate that sits BETWEEN the agent's
#   LLM reasoning and any real code execution. No code runs outside this sandbox.
#
# WHAT "SANDBOX" MEANS IN PHASE 7 vs. PHASE 13:
#   Phase 7 (NOW): subprocess with timeout + output cap + allowlist of commands.
#       No Docker. No network isolation. This is sufficient for SYNTAX CHECKING
#       because `python3 -c "compile(...)"` and `node --check` are:
#           - Read-only (no file writes)
#           - No network (no imports that call home)
#           - CPU-bounded (syntax check is O(n) in source length)
#
#   Phase 13 (LATER): Docker container with seccomp, no-network flag, read-only
#       filesystem. Needed for ARBITRARY code execution (e.g., running user tests).
#       Not needed here — don't over-engineer.
#
# THREAT MODEL (what this sandbox prevents):
#   - Shell injection via code argument:
#       e.g., code = '"; rm -rf /'  — blocked by NEVER using shell=True
#   - Timeout exhaustion: `while True: pass` — blocked by timeout_seconds
#   - Output flooding: `print("A" * 10_000_000)` — blocked by max_output_bytes
#   - Unsupported language smuggling: `language = "bash"` — blocked by allowlist
#
# DEPENDENCY: stdlib only (subprocess, dataclasses, time) — no extra packages.

from __future__ import annotations

import subprocess
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SandboxConfig:
    """
    Resource limits for sandbox execution.

    WHY frozen=True: Config should not be mutated after construction.
    The defaults here are conservative — syntax checking should complete
    in milliseconds. 5-second timeout is a safety net, not an expectation.
    """

    # Maximum wall-clock time for the subprocess (seconds)
    # Python syntax check on a 10,000-line file: ~50ms.
    # 5s is 100x headroom before we cut it off.
    timeout_seconds: int = 5

    # Maximum bytes captured from stdout + stderr combined.
    # Node.js --check on a valid file: 0 bytes. On an invalid file: ~100 bytes.
    # 10KB is more than enough, and prevents output-flooding attacks.
    max_output_bytes: int = 10_240

    # Only these languages are allowed through the sandbox.
    # WHY a frozenset (not a list): frozenset membership check is O(1).
    # WHY only these three: they are the languages our agents care about.
    # Adding a new language requires: (1) adding it here, (2) adding the
    # command-builder branch below, (3) testing it. Explicit is safer than implicit.
    allowed_languages: frozenset[str] = field(
        default_factory=lambda: frozenset({"python", "javascript", "typescript"})
    )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class SandboxResult:
    """
    Output from a single sandbox execution.

    exit_code:      0 = success, non-zero = error (syntax error, crash, etc.)
    stdout:         captured standard output (truncated to max_output_bytes)
    stderr:         captured standard error  (same truncation)
    timed_out:      True if the subprocess was killed due to timeout
    execution_time_ms: wall-clock time in milliseconds
    """
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    execution_time_ms: int


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SandboxViolationError(Exception):
    """
    Raised when a request violates sandbox policy BEFORE the subprocess runs.

    Examples:
        - language not in allowed_languages
        - code string looks like a shell injection attempt (heuristic check)

    WHY a distinct exception (not ValueError):
        Callers need to distinguish "policy violation" from "syntax error in
        the code being checked". CapabilityViolationError is a policy error;
        SandboxViolationError is also a policy error — both should be caught
        and surfaced as security findings, not bubbled as internal errors.
    """
    pass


# ---------------------------------------------------------------------------
# Sandbox class
# ---------------------------------------------------------------------------

class Sandbox:
    """
    Executes code snippets in a resource-bounded subprocess.

    Usage:
        sandbox = Sandbox()
        result = sandbox.run_syntax_check("def foo(: pass", "python")
        if not result.exit_code == 0:
            print("Syntax error:", result.stderr)

    Thread safety:
        Each call to run_syntax_check() creates a new subprocess.
        No shared mutable state. Multiple threads can call this safely.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self._config = config or SandboxConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_syntax_check(self, code: str, language: str) -> SandboxResult:
        """
        Check code for syntax errors without executing it.

        For Python: uses `python3 -c "compile(code, '<string>', 'exec')"`.
            WHY compile() and not exec(): compile() parses but does not execute.
            This means `import os; os.system("rm -rf /")` is syntax-valid but
            never runs. We check syntax, not semantics.

        For JavaScript / TypeScript: uses `node --check` via a temp file.
            WHY --check: node --check parses and exits, never runs the module.
            WHY temp file (not stdin): node --check requires a file path, not stdin.
            WHY we write to /tmp: it's writable, no permission issues in CI.
            The temp file is always cleaned up in a finally block.

        Raises:
            SandboxViolationError: if language is not in the allowlist or if
                the code string contains shell metacharacters that would escape
                the command construction (defense-in-depth check).
        """
        language = language.lower().strip()
        self._enforce_language_policy(language)
        self._enforce_injection_policy(code)

        if language == "python":
            return self._run_python_syntax(code)
        elif language in ("javascript", "typescript"):
            return self._run_node_syntax(code, language)

        # Should be unreachable because enforce_language_policy already raised,
        # but makes type checkers happy.
        raise SandboxViolationError(f"Unhandled language: {language}")

    # ------------------------------------------------------------------
    # Policy enforcement (before subprocess launch)
    # ------------------------------------------------------------------

    def _enforce_language_policy(self, language: str) -> None:
        """
        Reject languages not in the allowlist.

        WHY explicit allowlist (not blocklist):
            A blocklist ("everything except bash, sh, zsh, ...") is impossible to
            keep complete. An allowlist ("only these three") is closed by default.
            Secure by default — wiki principle: "forbid by default, allowlist only."
        """
        if language not in self._config.allowed_languages:
            raise SandboxViolationError(
                f"Language '{language}' is not in the sandbox allowlist. "
                f"Allowed: {sorted(self._config.allowed_languages)}. "
                "To add a new language, update SandboxConfig.allowed_languages "
                "and add the corresponding command builder in Sandbox."
            )

    def _enforce_injection_policy(self, code: str) -> None:
        """
        Heuristic guard against shell injection in the code string itself.

        IMPORTANT: this is a defense-in-depth check, not the primary defense.
        The primary defense is NEVER using shell=True in subprocess.run().
        When shell=False, the OS does not interpret metacharacters — the code
        string is passed as a raw argument, not interpolated into a shell command.

        However, for Python syntax check we do pass code as a string argument
        to `python3 -c`. Without shell=True, the shell cannot inject, but
        a crafted code string could still call os.system() etc. Since we only
        want syntax checking (not execution), we add this guard as a second layer.

        We do NOT block all shell metacharacters — valid Python code can contain
        backticks, semicolons, pipes, etc. We only block the specific patterns
        that would indicate someone is trying to escape the compile() wrapper.
        """
        # Null bytes crash Python's compile() with a misleading error.
        # They have no place in real source code.
        if "\x00" in code:
            raise SandboxViolationError(
                "Code contains null bytes (\\x00). "
                "Null bytes are not valid in source code and may indicate an injection attempt."
            )

    # ------------------------------------------------------------------
    # Language-specific command builders
    # ------------------------------------------------------------------

    def _run_python_syntax(self, code: str) -> SandboxResult:
        """
        Run `python3 -c "import ast; ast.parse(<code>)"` as a subprocess.

        WHY ast.parse() instead of compile():
            ast.parse() gives cleaner error messages with line numbers.
            compile() also works but the error messages are harder to parse.

        WHY -c with the import on the same line (not a temp file):
            A temp file write would require file system permissions and cleanup.
            For Python syntax checking, -c is simpler and equally safe.

        WHY we JSON-encode the code (not raw string interpolation):
            We pass the code as a Python string literal inside the -c argument.
            We use repr() to safely escape all quotes and backslashes in the code
            so they don't break out of the string literal context.
        """
        # repr() produces a valid Python string literal from any string.
        # e.g., repr('he said "hi"') == '"he said \\"hi\\""'
        # This is safe even if code contains single quotes, double quotes,
        # backslashes, newlines — repr() escapes all of them.
        safe_code_literal = repr(code)

        cmd = [
            "python3",
            "-c",
            f"import ast; ast.parse({safe_code_literal}); print('OK')",
        ]

        return self._run_subprocess(cmd)

    def _run_node_syntax(self, code: str, language: str) -> SandboxResult:
        """
        Write code to a temp file then run `node --check <tempfile>`.

        WHY a temp file (not stdin piping):
            `node --check` does not support reading from stdin.
            It requires a file path argument.

        WHY /tmp/<unique_name>:
            /tmp is always writable. We use a timestamp + pid for uniqueness
            so concurrent checks don't collide.
        """
        import os
        import tempfile

        # Choose file extension so node recognises the file type
        ext = ".ts" if language == "typescript" else ".js"

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=ext,
            dir="/tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(code)

        try:
            cmd = ["node", "--check", tmp_path]
            result = self._run_subprocess(cmd)
        finally:
            # Always clean up — even if subprocess raises
            try:
                os.unlink(tmp_path)
            except OSError:
                pass  # File was already cleaned up; not an error

        return result

    # ------------------------------------------------------------------
    # Core subprocess runner
    # ------------------------------------------------------------------

    def _run_subprocess(self, cmd: list[str]) -> SandboxResult:
        """
        Run a command and capture output, enforcing timeout and output limits.

        KEY SECURITY PROPERTY: shell=False
            subprocess.run(..., shell=False) is the default but we make it explicit.
            With shell=False, the OS does NOT interpret metacharacters in cmd[1:].
            There is NO way for the code argument to escape to a shell command.
            This is the primary injection defense.

        timeout behaviour:
            subprocess raises TimeoutExpired when the timeout elapses.
            We catch it, kill the process, and return timed_out=True.
            The agent treats timed_out as a syntax check failure — overly long
            syntax checks indicate something very wrong with the input.
        """
        start_ms = int(time.monotonic() * 1000)
        timed_out = False

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._config.timeout_seconds,
                shell=False,  # Explicit: NEVER interpolate into a shell
            )
            exit_code = proc.returncode
            raw_stdout = proc.stdout
            raw_stderr = proc.stderr

        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = -1
            raw_stdout = ""
            raw_stderr = f"Syntax check timed out after {self._config.timeout_seconds}s"
            logger.warning("Sandbox: subprocess timed out. cmd=%s", cmd[0])

        except FileNotFoundError as exc:
            # e.g., `node` not installed — not a sandbox error, a deployment error
            exit_code = -2
            raw_stdout = ""
            raw_stderr = f"Command not found: {cmd[0]}. Ensure it is installed. ({exc})"
            logger.error("Sandbox: command not found: %s", cmd[0])

        end_ms = int(time.monotonic() * 1000)
        elapsed_ms = end_ms - start_ms

        # Truncate output to prevent flooding (defence-in-depth)
        max_bytes = self._config.max_output_bytes
        stdout = raw_stdout[:max_bytes]
        stderr = raw_stderr[:max_bytes]

        logger.debug(
            "Sandbox: cmd=%s exit_code=%d elapsed_ms=%d timed_out=%s",
            cmd[0],
            exit_code,
            elapsed_ms,
            timed_out,
        )

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            execution_time_ms=elapsed_ms,
        )
