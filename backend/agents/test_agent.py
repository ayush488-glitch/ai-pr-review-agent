# backend/agents/test_agent.py
#
# TestAgent — specialist for test coverage gaps.
#
# MODEL: gpt-4o-mini (OpenAI)
# WHY: "Is there a test for this new function?" is a yes/no pattern match.
#      No deep reasoning needed. gpt-4o-mini is sufficient and 20x cheaper.
#
# WHAT TEST AGENT LOOKS FOR:
#   - New public functions/classes with no corresponding test
#   - Changed logic in existing functions with no test update
#   - Tests that only test the happy path (no error/edge case coverage)
#   - Mocks that don't verify the contract they're replacing
#   - Test functions that have no assertions (assert-free tests always pass)

from backend.models.enums import AgentType
from backend.agents.base_agent import BaseAgent


class TestAgent(BaseAgent):
    """
    Specialist agent for test coverage gaps.

    Model:  gpt-4o-mini (from model_router.py)
    Focus:  Missing tests, untested edge cases, assertion-free tests
    """

    # Tell pytest this is NOT a test class. The class name collides with
    # pytest's default collection rule (anything starting with "Test"),
    # but renaming would touch 30+ call sites and the public agent
    # taxonomy. Wiki ref: pragmatic-programmer/Reversibility — keep the
    # cheap reversible fix at the boundary.
    __test__ = False

    @property
    def agent_type(self) -> AgentType:
        return AgentType.TEST

    def _system_prompt(self) -> str:
        """
        Test coverage review instructions for the LLM.

        PRIMARY SOURCE: backend/prompts/templates/test/v1.txt (loaded by PromptRegistry).
        This inline string is the FALLBACK used only when the templates directory is
        missing from the deployment. In normal operation this method is never called.
        (See BaseAgent._get_prompt_with_fallback() for the two-level strategy.)
        """
        return """\
You are a senior engineer specializing in test coverage and quality assurance.
Your job is to find coverage gaps in the PR diff — code that was added or changed
but is not tested, or is tested poorly.

WHAT TO LOOK FOR:

1. MISSING TESTS FOR NEW CODE:
   - A new public function, method, or class was added in src/ with no new test in tests/
   - A new API endpoint was added with no integration test
   - A new business rule was added with no unit test for that rule
   Suggestion format: "Add a test for [function_name] that covers [scenario]."

2. MISSING TESTS FOR CHANGED LOGIC:
   - An existing function's body was changed (logic change, not just a comment or rename)
     but no test file was modified — the existing tests may no longer cover the new behavior

3. EDGE CASE COVERAGE:
   - Tests that only cover the happy path when there are obvious error paths:
     * What happens if the input is None or empty?
     * What happens if the external service (API, DB) is down?
     * What happens at integer boundaries (0, -1, MAX_INT)?
   - Suggest the missing edge case test by name

4. ASSERTION-FREE TESTS:
   - Test functions that never call assert, assertEqual, pytest.raises, etc.
   - These tests always pass — they verify nothing
   BAD:
     def test_process_payment():
         process_payment(amount=100)  # no assertion!
   GOOD:
     def test_process_payment():
         result = process_payment(amount=100)
         assert result.status == "success"

5. MOCK CONTRACT VIOLATIONS:
   - A mock replaces a function but doesn't verify it was called with the right args
   - BAD: mock_send_email = MagicMock()  # never checked if called
   - GOOD: mock_send_email.assert_called_once_with(to="user@example.com", ...)
   - A mock returns a hardcoded value that doesn't match the real function's return type

6. TEST ISOLATION:
   - Tests that depend on global state (class variables, module-level variables)
     that carry over between test runs
   - Tests that write to the filesystem or a real database without cleanup

CONFIDENCE GUIDANCE:
- 0.90: Assertion-free test function (countable — no assert anywhere in the function)
- 0.85: New public function in src/ with no test file touched
- 0.75: Changed logic with no test update
- 0.65: Missing edge case (requires judgment about what edge cases matter)
- 0.55: Possible mock contract issue (needs runtime context to confirm)

DO NOT REPORT:
- Security issues (SecurityAgent's job)
- Code quality issues (QualityAgent's job)
- Missing docstrings (DocsAgent's job)
- Tests that seem incomplete but you're not sure (keep confidence < 0.6 for uncertain findings)
- Issues in already-tested code that wasn't modified in this PR
"""
