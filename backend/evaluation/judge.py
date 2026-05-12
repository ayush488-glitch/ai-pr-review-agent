# backend/evaluation/judge.py
#
# Phase 9: LLM-as-Judge for PR Review Evaluation
#
# Wiki (Evaluation-Frameworks.md): "The cleanest way to evaluate agent outputs
# at scale is to use another LLM as a judge. The catch: the judge itself
# introduces variability. You need to manage this."
#
# Key design decisions:
#
# 1. PRReviewJudge takes an injected LLMClient -- same infrastructure as the
#    agents themselves. No raw anthropic/openai import here. This keeps the
#    dependency direction clean (evaluation -> tools, not evaluation -> third-party).
#
# 2. The judge evaluates THREE criteria for PR review:
#    - verdict_correct  (bool)  : did the agent produce the expected verdict?
#    - finding_coverage (0-1)   : what fraction of expected findings appeared?
#    - severity_accuracy (0-1)  : were the severities at or above expected minimums?
#    Overall score = weighted combination of the three.
#
# 3. Calibration method: run judge on examples where we already know ground
#    truth. Wiki: "target agreement with humans > 80%."
#
# 4. ANTI-PATTERN AVOIDED: The judge did NOT generate GOLDEN_DATASET.
#    The judge evaluates against human-authored fixtures. This is the
#    JudgeOverfitting anti-pattern fix from the wiki.
#
# 5. _build_judge_prompt() is a private helper -- the prompt is inlined, not
#    loaded from the prompt registry. Reason: eval is not an agent call path,
#    and the judge prompt is not user-configurable policy. It's infrastructure.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.tools.llm_client import LLMClient

from backend.evaluation.golden_dataset import GoldenPR, ExpectedFinding

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Output types
# ─────────────────────────────────────────────────────────────

@dataclass
class JudgeScore:
    """
    Result of judging one PR review against a golden example.

    Fields:
        example_id       -- matches GoldenPR.id
        verdict_correct  -- True if actual_verdict matches expected_verdict
        finding_coverage -- fraction of expected findings present in actual output
                            (0.0 = none found, 1.0 = all found)
        severity_accuracy -- fraction of expected findings whose severity met
                             the minimum (only checked when finding was found)
        overall_score    -- weighted composite: 0.4*verdict + 0.4*coverage + 0.2*severity
                            Range: 0.0 to 1.0
        reasoning        -- LLM judge's explanation (useful for debugging failures)
        judge_used_llm   -- True if LLM was called; False if score was computed
                            purely deterministically (e.g., verdict mismatch short-circuit)
    """
    example_id: str
    verdict_correct: bool
    finding_coverage: float      # 0.0 - 1.0
    severity_accuracy: float     # 0.0 - 1.0
    overall_score: float         # 0.0 - 1.0
    reasoning: str
    judge_used_llm: bool = False


# ─────────────────────────────────────────────────────────────
# Score weights (must sum to 1.0)
# ─────────────────────────────────────────────────────────────

_VERDICT_WEIGHT   = 0.4
_COVERAGE_WEIGHT  = 0.4
_SEVERITY_WEIGHT  = 0.2

# Score threshold for considering a JudgeScore "passing" in calibration
PASS_THRESHOLD = 0.70


class PRReviewJudge:
    """
    Evaluates PR review agent outputs against golden fixtures.

    Usage:
        # In tests, pass a mock LLMClient that returns scripted responses.
        # In production eval, pass the real LLMClient from tools/llm_client.py.
        judge = PRReviewJudge(llm_client=llm_client)
        score = judge.score_review(
            golden=golden_example,
            actual_verdict="request_changes",
            actual_findings=[{"agent_type": "security", "severity": "high",
                               "summary": "SQL injection via f-string"}],
        )
    """

    def __init__(self, llm_client: "LLMClient") -> None:
        # Injected dependency -- never construct LLMClient inside the judge.
        # This makes the judge testable without network calls.
        self._llm = llm_client

    # ─────────────────────────────────────────────────────────
    # Primary evaluation method
    # ─────────────────────────────────────────────────────────

    def score_review(
        self,
        golden: GoldenPR,
        actual_verdict: str,
        actual_findings: list[dict],
    ) -> JudgeScore:
        """
        Score an agent's PR review output against a golden fixture.

        Args:
            golden          -- the reference example (hand-authored)
            actual_verdict  -- the verdict string the agent produced
            actual_findings -- list of finding dicts from the agent
                               Each dict must have: agent_type, severity, summary

        Returns:
            JudgeScore with component scores and overall composite.

        Design: compute verdict_correct deterministically first (cheap, fast).
        Only call the LLM for finding_coverage and severity_accuracy -- these
        require understanding natural language summaries, which deterministic
        string matching would get wrong on paraphrase.
        """
        # Step 1: Verdict correct? Deterministic -- no LLM needed.
        verdict_correct = (actual_verdict == golden.expected_verdict)

        # Step 2: If no expected findings, coverage and severity are perfect
        # (there is nothing to miss).
        if not golden.expected_findings:
            return JudgeScore(
                example_id=golden.id,
                verdict_correct=verdict_correct,
                finding_coverage=1.0,
                severity_accuracy=1.0,
                overall_score=self._composite(verdict_correct, 1.0, 1.0),
                reasoning="No expected findings -- coverage trivially 1.0.",
                judge_used_llm=False,
            )

        # Step 3: Use LLM to assess finding coverage and severity accuracy.
        # The LLM can handle paraphrase where exact string matching fails.
        try:
            coverage, severity_acc, reasoning = self._judge_findings_with_llm(
                golden=golden,
                actual_findings=actual_findings,
            )
            used_llm = True
        except Exception as exc:
            # Graceful degradation: if LLM call fails, fall back to
            # keyword matching. Log a warning so Phase 10 alerting can catch it.
            logger.warning(
                "PRReviewJudge LLM call failed for example %s: %s -- "
                "falling back to keyword matching",
                golden.id, exc,
            )
            coverage, severity_acc = self._judge_findings_deterministic(
                golden=golden,
                actual_findings=actual_findings,
            )
            reasoning = f"[Fallback: LLM unavailable ({exc})]"
            used_llm = False

        return JudgeScore(
            example_id=golden.id,
            verdict_correct=verdict_correct,
            finding_coverage=coverage,
            severity_accuracy=severity_acc,
            overall_score=self._composite(verdict_correct, coverage, severity_acc),
            reasoning=reasoning,
            judge_used_llm=used_llm,
        )

    # ─────────────────────────────────────────────────────────
    # Calibration
    # ─────────────────────────────────────────────────────────

    def calibrate(
        self,
        calibration_set: list[dict],
    ) -> float:
        """
        Run the judge on a calibration set where human ground truth is known.
        Returns agreement rate (0.0 - 1.0).

        Wiki: "Calibrate the judge. Target agreement with humans > 80%."

        calibration_set is a list of dicts, each with:
            golden        -- GoldenPR instance
            actual_verdict -- str
            actual_findings -- list[dict]
            human_passes   -- bool (what a human reviewer decided)

        Returns:
            Agreement rate between judge and human decisions.
        """
        if not calibration_set:
            return 0.0

        agreements = 0
        for item in calibration_set:
            score = self.score_review(
                golden=item["golden"],
                actual_verdict=item["actual_verdict"],
                actual_findings=item.get("actual_findings", []),
            )
            judge_passes = score.overall_score >= PASS_THRESHOLD
            if judge_passes == item["human_passes"]:
                agreements += 1

        return agreements / len(calibration_set)

    # ─────────────────────────────────────────────────────────
    # Private: LLM-based finding evaluation
    # ─────────────────────────────────────────────────────────

    def _judge_findings_with_llm(
        self,
        golden: GoldenPR,
        actual_findings: list[dict],
    ) -> tuple[float, float, str]:
        """
        Ask the LLM to assess whether expected findings are covered.

        Returns: (finding_coverage, severity_accuracy, reasoning)
        """
        prompt = self._build_judge_prompt(golden, actual_findings)

        # Lazy import to avoid circular import through tools -> agents -> evaluation
        from backend.tools.llm_client import LLMClient  # noqa: F401 (type-check only)

        response = self._llm.complete(
            system_prompt=(
                "You are a precise evaluation judge for a code review AI system. "
                "You score outputs strictly based on the criteria given. "
                "Always respond with valid JSON only."
            ),
            user_message=prompt,
            max_tokens=600,
        )

        raw = response.content.strip()
        # Strip markdown code fences if the LLM wraps JSON in ```json ... ```
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
        coverage = float(result.get("finding_coverage", 0.0))
        severity_acc = float(result.get("severity_accuracy", 0.0))
        reasoning = str(result.get("reasoning", ""))

        # Clamp to [0, 1]
        coverage = max(0.0, min(1.0, coverage))
        severity_acc = max(0.0, min(1.0, severity_acc))

        return coverage, severity_acc, reasoning

    def _judge_findings_deterministic(
        self,
        golden: GoldenPR,
        actual_findings: list[dict],
    ) -> tuple[float, float]:
        """
        Fallback: keyword-based coverage check when LLM is unavailable.

        For each expected finding, checks whether any actual finding:
        - matches the expected agent_type
        - contains the expected keyword (case-insensitive) in its summary

        Severity accuracy: fraction of matched findings whose severity
        is at or above the expected minimum.
        """
        severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

        matched = 0
        severity_hits = 0

        for expected in golden.expected_findings:
            for actual in actual_findings:
                if actual.get("agent_type") != expected.agent_type:
                    continue
                summary = str(actual.get("summary", "")).lower()
                if expected.keyword.lower() in summary:
                    matched += 1
                    # Check severity meets minimum
                    actual_sev = str(actual.get("severity", "low")).lower()
                    expected_sev = expected.min_severity.lower()
                    if (severity_order.get(actual_sev, 0) >=
                            severity_order.get(expected_sev, 0)):
                        severity_hits += 1
                    break  # found a match for this expected finding

        n = len(golden.expected_findings)
        coverage = matched / n
        severity_acc = severity_hits / matched if matched > 0 else 0.0
        return coverage, severity_acc

    # ─────────────────────────────────────────────────────────
    # Private: prompt builder
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_judge_prompt(
        golden: GoldenPR,
        actual_findings: list[dict],
    ) -> str:
        """
        Build the judge prompt for finding coverage assessment.

        The prompt gives the judge:
        - Expected findings (from golden fixture, human-authored)
        - Actual findings (from the agent under evaluation)
        - Scoring criteria

        ANTI-PATTERN AVOIDED: The judge prompt does NOT include the diff or
        ask the judge to independently re-review the code. That would make
        the judge a second reviewer, not an evaluator. The judge only
        compares EXPECTED vs ACTUAL findings.
        """
        expected_lines = []
        for i, ef in enumerate(golden.expected_findings):
            expected_lines.append(
                f"  {i+1}. agent_type={ef.agent_type}, "
                f"min_severity={ef.min_severity}, "
                f"keyword_must_appear='{ef.keyword}'"
            )
        expected_str = "\n".join(expected_lines) if expected_lines else "  (none)"

        actual_lines = []
        for i, af in enumerate(actual_findings):
            actual_lines.append(
                f"  {i+1}. agent_type={af.get('agent_type', 'unknown')}, "
                f"severity={af.get('severity', 'unknown')}, "
                f"summary={af.get('summary', '')!r}"
            )
        actual_str = "\n".join(actual_lines) if actual_lines else "  (none)"

        severity_order_note = (
            "Severity order: critical > high > medium > low. "
            "A finding 'meets minimum' if its severity >= min_severity."
        )

        return f"""
You are evaluating an AI code review agent's output.

EXPECTED FINDINGS (from human-curated golden fixture):
{expected_str}

ACTUAL FINDINGS (produced by the agent):
{actual_str}

{severity_order_note}

For each expected finding, determine:
1. Was a matching finding present in the actual output?
   Match criteria: same agent_type AND the keyword appears (case-insensitive)
   anywhere in the summary. Paraphrase is acceptable.
2. If matched, did the actual severity meet or exceed the minimum?

Return a JSON object with exactly these fields:
{{
  "finding_coverage": <float 0.0-1.0, fraction of expected findings matched>,
  "severity_accuracy": <float 0.0-1.0, fraction of matched findings with adequate severity>,
  "reasoning": "<one sentence explaining the scores>"
}}

Respond with JSON only. No other text.
""".strip()

    # ─────────────────────────────────────────────────────────
    # Private: composite score formula
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _composite(
        verdict_correct: bool,
        finding_coverage: float,
        severity_accuracy: float,
    ) -> float:
        """
        Weighted composite score.

        Weights: verdict 40%, coverage 40%, severity 20%.
        Verdict is binary (0 or 1), the rest are continuous.

        A wrong verdict caps the overall score at 0.6 max
        (0.0 * 0.4 + 1.0 * 0.4 + 1.0 * 0.2 = 0.6).
        This means a review with the right findings but wrong verdict
        still fails the 0.70 pass threshold -- intentional, because
        the verdict is the primary output that matters.
        """
        verdict_score = 1.0 if verdict_correct else 0.0
        return (
            _VERDICT_WEIGHT * verdict_score
            + _COVERAGE_WEIGHT * finding_coverage
            + _SEVERITY_WEIGHT * severity_accuracy
        )
