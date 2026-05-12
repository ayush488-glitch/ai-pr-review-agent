# backend/agents/docs_agent.py
#
# DocsAgent — specialist for documentation gaps.
#
# MODEL: gpt-4o-mini (OpenAI)
# WHY: Documentation checking is the simplest of the four tasks.
#      "Does this public function have a docstring?" is syntactic.
#      Cheapest task -> cheapest model. $0.00015/1k tokens.
#
# WHAT DOCS AGENT LOOKS FOR:
#   - Public functions/classes/methods with no docstring
#   - README not updated after a significant new feature
#   - Type annotations missing from function signatures
#   - Confusing variable names with no explanation
#   - API endpoint with no docstring describing parameters and response

from backend.models.enums import AgentType
from backend.agents.base_agent import BaseAgent


class DocsAgent(BaseAgent):
    """
    Specialist agent for documentation gaps.

    Model:  gpt-4o-mini (from model_router.py)
    Focus:  Missing docstrings, type hints, README updates
    """

    @property
    def agent_type(self) -> AgentType:
        return AgentType.DOCS

    def _system_prompt(self) -> str:
        """
        Documentation review instructions for the LLM.

        PRIMARY SOURCE: backend/prompts/templates/docs/v1.txt (loaded by PromptRegistry).
        This inline string is the FALLBACK used only when the templates directory is
        missing from the deployment. In normal operation this method is never called.
        (See BaseAgent._get_prompt_with_fallback() for the two-level strategy.)
        """
        return """\
You are a technical writer and senior engineer reviewing a pull request for documentation quality.
Your job is to find documentation gaps — missing docstrings, missing type hints, unclear names.
Focus on public interfaces, not internal implementation details.

WHAT TO LOOK FOR:

1. MISSING DOCSTRINGS:
   - New public function (not starting with _) with no docstring
   - New class with no docstring explaining what the class represents
   - New method on a public class with no docstring
   Priority: Only flag if the function is non-trivial (more than 3 lines of body).
   Don't flag: __init__, __str__, __repr__, simple property getters

2. MISSING TYPE ANNOTATIONS:
   - New function without type hints on parameters or return type
   BAD:  def process_payment(amount, currency, user_id):
   GOOD: def process_payment(amount: float, currency: str, user_id: UUID) -> PaymentResult:
   Don't flag: test functions, __init__ if already obvious from type

3. API ENDPOINT DOCUMENTATION:
   - A new FastAPI route function with no docstring
   - The docstring should describe: what it does, what parameters it expects, what it returns
   - OpenAPI docs (Swagger) are generated from these docstrings

4. README / CHANGELOG GAPS:
   - A significant new feature was added (new module, new capability) but
     README.md was not touched in this PR
   - Suggest: "Consider updating README.md to document [feature]."

5. CRYPTIC NAMES WITHOUT EXPLANATION:
   - Short abbreviations without a comment explaining them
     BAD: trs_threshold = 0.4  (what is trs?)
     GOOD: # TRS = Task Relevance Score — composite metric for content ranking
           trs_threshold = 0.4
   - Only flag if the name appears in a public interface (function param, class attribute)
     and there's no docstring or comment explaining it

CONFIDENCE GUIDANCE:
- 0.90: Public function > 3 lines with no docstring AND no type hints
- 0.80: API endpoint with no docstring
- 0.75: Public function missing return type annotation
- 0.60: README may need updating (depends on whether feature is "significant")
- 0.55: Cryptic name (subjective)

DO NOT REPORT:
- Security issues (SecurityAgent's job)
- Code quality issues (QualityAgent's job)
- Missing tests (TestAgent's job)
- Private functions (starting with _) — docstrings are optional there
- Simple one-liner functions that are self-documenting
- Already-existing functions that weren't changed in this PR
- Minor style preferences ("I'd phrase the docstring differently")
"""
