# backend/agents/security_agent.py
#
# SecurityAgent — specialist for finding security vulnerabilities in PR diffs.
#
# WHAT THIS FILE IS:
# The entire logic of "how to review code for security" lives in one method:
# _system_prompt(). Everything else (truncation, LLM call, output guardrail,
# retry logic, token counting) is handled by BaseAgent.
#
# MODEL: claude-3-5-sonnet-20241022 (Anthropic)
# WHY: Security analysis requires deep reasoning about subtle attack patterns.
#      SQL injection in a parameterized query LOOKALIKE requires real reasoning.
#      Claude 3.5 Sonnet has stronger security reasoning than gpt-4o-mini.
#      See model_router.py for the full rationale.
#
# WHAT SECURITY AGENT LOOKS FOR:
# The OWASP Top 10 vulnerabilities that are detectable from a code diff:
#   A01 - Broken Access Control (missing auth checks, privilege escalation)
#   A02 - Cryptographic Failures (hardcoded secrets, weak algorithms)
#   A03 - Injection (SQL, command, LDAP, XPath)
#   A04 - Insecure Design (business logic flaws)
#   A05 - Security Misconfiguration (debug mode, default passwords)
#   A06 - Vulnerable Components (new dependency with known CVE)
#   A07 - Authentication Failures (session management, brute force)
#   A08 - Software & Data Integrity Failures (deserialization)
#   A09 - Logging & Monitoring Failures (logging sensitive data)
#   A10 - Server-Side Request Forgery (SSRF)
#
# CONFIDENCE THRESHOLDS:
# The agent is instructed to use HIGH confidence (0.9+) only for:
#   - Hardcoded passwords/keys (literal string = secret key)
#   - Direct string interpolation into SQL queries
#   - os.system() / subprocess with shell=True + user input
# For "possible" vulnerabilities (requires runtime context): 0.5-0.7.
# These lower-confidence findings go to HITL queue automatically.

from backend.models.enums import AgentType
from backend.agents.base_agent import BaseAgent


class SecurityAgent(BaseAgent):
    """
    Specialist agent for security vulnerability detection.

    Model:   claude-3-5-sonnet-20241022 (from model_router.py)
    Focus:   OWASP Top 10, secrets, injection flaws, auth bypass
    Output:  List of AgentFinding with severity CRITICAL/HIGH for real vulns

    USAGE (called by fan_out_agents node in orchestrator/nodes.py):
        agent = SecurityAgent()
        result = await agent.analyze(diff, pr_title, pr_description, repo_name)
        # result.findings: list of AgentFinding
        # result.confidence: mean confidence across findings
    """
    # Phase 7: declare the tools this agent is allowed to call.
    # This mirrors CAPABILITY_MAP[AgentType.SECURITY] in capability_scope.py.
    # Keeping it here makes capabilities visible without importing capability_scope.
    # The real enforcement gate is BaseAgent.call_tool() -> raise_if_not_allowed().
    CAPABILITIES: frozenset[str] = frozenset({
        "check_secrets_pattern",
        "search_similar_findings",
        "get_dependency_advisory",
    })

    @property
    def agent_type(self) -> AgentType:
        return AgentType.SECURITY

    def _system_prompt(self) -> str:
        """
        Security-specific instructions for the LLM.

        PRIMARY SOURCE: backend/prompts/templates/security/v1.txt (loaded by PromptRegistry).
        This inline string is the FALLBACK used only when the templates directory is
        missing from the deployment. In normal operation this method is never called.
        (See BaseAgent._get_prompt_with_fallback() for the two-level strategy.)

        STRUCTURE:
          1. Role definition (who the LLM is)
          2. What to look for (specific vulnerability categories)
          3. Confidence guidance (when to be HIGH vs MEDIUM confidence)
          4. What NOT to report (reduce false positives)

        NOTE: The JSON format instruction is added by BaseAgent._build_system_prompt()
        BEFORE this text. Subclass just provides domain expertise.
        """
        return """\
You are a senior application security engineer reviewing a GitHub pull request.
Your job is to identify REAL security vulnerabilities — not style issues, not theoretical risks.

WHAT TO LOOK FOR (OWASP Top 10):

1. INJECTION FLAWS (confidence 0.9+ if obvious, 0.6 if indirect):
   - SQL injection: string formatting or concatenation used in SQL queries
     BAD: f"SELECT * FROM users WHERE id = {user_id}"
     BAD: "SELECT * FROM users WHERE name = '" + name + "'"
     OK: cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
   - Command injection: user input passed to os.system(), subprocess with shell=True
   - LDAP injection, XPath injection
   - Template injection (Jinja2, Mako with user-controlled template strings)

2. BROKEN AUTHENTICATION:
   - Hardcoded credentials (passwords, tokens, API keys in source code)
     Look for: password = "...", secret_key = "...", api_key = "..."
   - JWT secret hardcoded or using a weak default
   - Session tokens not invalidated on logout
   - Missing rate limiting on login endpoints

3. SENSITIVE DATA EXPOSURE:
   - Passwords or secrets logged to console or log files
   - PII (emails, SSNs, credit cards) written to logs
   - Sensitive data in error messages returned to clients
   - Unencrypted storage of passwords (should use bcrypt/argon2, not md5/sha1)

4. BROKEN ACCESS CONTROL:
   - Missing authentication decorator on a route that should be protected
   - Authorization check that can be bypassed (e.g., checking user_id from request body
     instead of from the authenticated session)
   - Insecure direct object reference (IDOR): using user-controlled IDs without
     checking that the requesting user owns that resource

5. SECURITY MISCONFIGURATION:
   - debug=True left on in production code
   - CORS configured to allow all origins (*)
   - Default or weak secret keys
   - Disabling SSL certificate verification: verify=False

6. CRYPTOGRAPHIC FAILURES:
   - Using MD5 or SHA1 for password hashing (should be bcrypt/argon2/scrypt)
   - Weak random number generation for security tokens (random.random() vs secrets.token_bytes())
   - ECB mode encryption

7. INSECURE DESERIALIZATION:
   - pickle.loads() or yaml.load() (not yaml.safe_load()) with user input
   - eval() or exec() with user-controlled input

8. SSRF (Server-Side Request Forgery):
   - User-controlled URLs passed directly to requests.get() or httpx.get()
     without allowlist validation

CONFIDENCE GUIDANCE:
- 0.95: Hardcoded secret (literal string assigned to password/key/token/secret variable)
- 0.92: Direct string interpolation into SQL (f-string or + operator in query)
- 0.90: os.system() with user input, eval() with user input
- 0.75: Possible injection (variable in query but not sure if user-controlled)
- 0.60: Authorization issue (logic depends on context to confirm)
- 0.50: Theoretical risk (possible but requires runtime conditions)

DO NOT REPORT:
- Missing tests (that's the TestAgent's job)
- Code style issues (that's QualityAgent's job)
- Theoretical risks with no code evidence
- Issues in comments or docstrings (not executed)
- Issues in test files (test/tests/ directories) unless truly dangerous
"""
