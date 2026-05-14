# tests/eval_phase9.py
#
# Phase 9 Smoke Tests: Evaluation Systems
#
# 20 tests across 4 groups. No real LLM calls.
# The judge is always given a mock LLMClient that returns scripted JSON.
# The regression gate's run_fn is always a lambda.
#
# Run: python3 -m pytest tests/eval_phase9.py -v
#
# Group A (5): Golden Dataset
# Group B (5): LLM-as-Judge
# Group C (5): Regression Gate (threshold logic)
# Group D (5): Integration (suite, slices, baseline round-trip)

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

# ── make project root importable ───────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.evaluation.golden_dataset import (
    GoldenPR,
    ExpectedFinding,
    load_golden_dataset,
    get_slice,
)
from backend.evaluation.judge import PRReviewJudge, JudgeScore, PASS_THRESHOLD
from backend.evaluation.regression_gate import (
    RegressionGate,
    EvalResult,
    PASS_THRESHOLD as GATE_PASS,
)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _make_mock_llm(json_payload: dict) -> MagicMock:
    """
    Return a MagicMock LLMClient whose .complete() returns a scripted response.
    The judge calls llm.complete(...) and reads response.content.
    """
    response = MagicMock()
    response.content = json.dumps(json_payload)
    mock_llm = MagicMock()
    mock_llm.complete.return_value = response
    return mock_llm


def _make_eval_result(
    example_id: str = "x",
    score: float = 0.80,
    difficulty: str = "easy",
    category: str = "quality",
    expected_verdict: str = "approve",
    actual_verdict: str = "approve",
    verdict_correct: bool = True,
) -> EvalResult:
    return EvalResult(
        example_id=example_id,
        difficulty=difficulty,
        category=category,
        expected_verdict=expected_verdict,
        actual_verdict=actual_verdict,
        verdict_correct=verdict_correct,
        finding_coverage=1.0,
        severity_accuracy=1.0,
        score=score,
        reasoning="test",
    )


# ─────────────────────────────────────────────────────────────
# GROUP A: Golden Dataset (5 tests)
# ─────────────────────────────────────────────────────────────

class TestGoldenDataset:

    def test_load_returns_12_examples(self):
        """
        Golden dataset must have exactly 12 entries as planned.
        Wiki: "Keep golden datasets small but representative."
        """
        examples = load_golden_dataset()
        assert len(examples) == 12, (
            f"Expected 12 golden examples, got {len(examples)}. "
            "Add or remove examples to match the planned distribution."
        )

    def test_all_examples_have_required_fields(self):
        """
        Every GoldenPR must have all required fields populated.
        id, pr_title, diff_snippet can be empty strings on edge cases
        but must be present. expected_verdict must be one of 3 valid values.
        """
        valid_verdicts = {"approve", "request_changes", "needs_human_review"}
        valid_difficulties = {"easy", "medium", "hard"}
        valid_categories = {"security", "quality", "test_coverage", "docs", "mixed"}

        for ex in load_golden_dataset():
            assert ex.id, f"Example missing id: {ex}"
            assert ex.pr_title is not None, f"Example {ex.id} missing pr_title"
            assert ex.diff_snippet is not None, f"Example {ex.id} missing diff_snippet"
            assert ex.expected_verdict in valid_verdicts, (
                f"Example {ex.id} has invalid expected_verdict '{ex.expected_verdict}'"
            )
            assert ex.difficulty in valid_difficulties, (
                f"Example {ex.id} has invalid difficulty '{ex.difficulty}'"
            )
            assert ex.category in valid_categories, (
                f"Example {ex.id} has invalid category '{ex.category}'"
            )

    def test_edge_case_examples_present(self):
        """
        Dataset must include: empty diff, injection in title, large diff.
        Wiki (EvaluatingOnlyHappyPath): "Explicitly add empty queries,
        extremely long inputs, injection attempts."
        """
        ids = {ex.id for ex in load_golden_dataset()}
        assert "edge_empty_diff" in ids, "Missing edge case: empty diff"
        assert "edge_injection_in_title" in ids, "Missing edge case: injection in title"
        assert "edge_large_diff" in ids, "Missing edge case: large diff"

    def test_get_slice_filters_by_difficulty(self):
        """get_slice(difficulty='hard') must return only hard examples."""
        hard = get_slice(difficulty="hard")
        assert len(hard) > 0, "No hard examples found"
        for ex in hard:
            assert ex.difficulty == "hard", (
                f"get_slice(difficulty='hard') returned example {ex.id} "
                f"with difficulty={ex.difficulty}"
            )

    def test_all_three_verdicts_represented(self):
        """
        Golden dataset must contain examples for all 3 verdict types.
        Slice evaluation by verdict requires at least one example per verdict.
        """
        verdicts = {ex.expected_verdict for ex in load_golden_dataset()}
        assert "approve" in verdicts, "No APPROVE examples in golden dataset"
        assert "request_changes" in verdicts, "No REQUEST_CHANGES examples"
        assert "needs_human_review" in verdicts, "No NEEDS_HUMAN_REVIEW examples"


# ─────────────────────────────────────────────────────────────
# GROUP B: LLM-as-Judge (5 tests)
# ─────────────────────────────────────────────────────────────

class TestPRReviewJudge:

    def test_judge_score_fields_present_and_typed(self):
        """
        JudgeScore must have all expected fields with correct types.
        Tests the constructor contract -- catches field renames early.
        """
        js = JudgeScore(
            example_id="test_001",
            verdict_correct=True,
            finding_coverage=0.9,
            severity_accuracy=0.8,
            overall_score=0.85,
            reasoning="All good",
            judge_used_llm=False,
        )
        assert isinstance(js.example_id, str)
        assert isinstance(js.verdict_correct, bool)
        assert isinstance(js.finding_coverage, float)
        assert isinstance(js.severity_accuracy, float)
        assert isinstance(js.overall_score, float)
        assert isinstance(js.reasoning, str)
        assert isinstance(js.judge_used_llm, bool)

    def test_perfect_match_scores_above_threshold(self):
        """
        When actual_verdict matches expected and LLM returns perfect scores,
        overall_score should be >= PASS_THRESHOLD (0.70) and ideally near 1.0.
        """
        mock_llm = _make_mock_llm({
            "finding_coverage": 1.0,
            "severity_accuracy": 1.0,
            "reasoning": "Perfect match",
        })
        judge = PRReviewJudge(llm_client=mock_llm)

        golden = GoldenPR(
            id="test_001",
            pr_title="Test PR",
            pr_description="",
            diff_snippet="",
            expected_verdict="approve",
            expected_findings=(
                ExpectedFinding("security", "high", "injection"),
            ),
            difficulty="easy",
            category="security",
            notes="",
        )

        score = judge.score_review(
            golden=golden,
            actual_verdict="approve",
            actual_findings=[{
                "agent_type": "security",
                "severity": "high",
                "summary": "SQL injection detected",
            }],
        )
        assert score.verdict_correct is True
        assert score.overall_score >= PASS_THRESHOLD, (
            f"Perfect-match score {score.overall_score} below threshold {PASS_THRESHOLD}"
        )

    def test_wrong_verdict_sets_verdict_correct_false(self):
        """
        When actual_verdict does not match expected, verdict_correct must be False.
        The composite score should be capped because verdict weight is 0.
        """
        mock_llm = _make_mock_llm({
            "finding_coverage": 1.0,
            "severity_accuracy": 1.0,
            "reasoning": "Finding OK but wrong verdict",
        })
        judge = PRReviewJudge(llm_client=mock_llm)

        golden = GoldenPR(
            id="test_002",
            pr_title="",
            pr_description="",
            diff_snippet="",
            expected_verdict="needs_human_review",
            expected_findings=(
                ExpectedFinding("security", "critical", "secret"),
            ),
            difficulty="hard",
            category="security",
            notes="",
        )

        score = judge.score_review(
            golden=golden,
            actual_verdict="approve",   # wrong
            actual_findings=[{
                "agent_type": "security",
                "severity": "critical",
                "summary": "Hardcoded secret found",
            }],
        )
        assert score.verdict_correct is False
        # With verdict_weight=0.4, max possible score when verdict is wrong
        # is 0.4*0 + 0.4*1 + 0.2*1 = 0.60.
        assert score.overall_score <= 0.61, (
            f"Wrong-verdict score {score.overall_score} should be <= 0.60 "
            "(verdict weight=0.4 contributes 0)"
        )

    def test_missing_critical_finding_reduces_coverage(self):
        """
        When the agent's findings miss an expected keyword, finding_coverage
        should be < 1.0, which the LLM judge reflects.
        """
        mock_llm = _make_mock_llm({
            "finding_coverage": 0.0,   # agent missed the expected finding
            "severity_accuracy": 0.0,
            "reasoning": "Expected SQL injection finding not found",
        })
        judge = PRReviewJudge(llm_client=mock_llm)

        golden = GoldenPR(
            id="test_003",
            pr_title="",
            pr_description="",
            diff_snippet="",
            expected_verdict="request_changes",
            expected_findings=(
                ExpectedFinding("security", "high", "sql injection"),
            ),
            difficulty="medium",
            category="security",
            notes="",
        )

        score = judge.score_review(
            golden=golden,
            actual_verdict="request_changes",
            actual_findings=[],   # agent produced no findings
        )
        assert score.finding_coverage < 1.0, (
            "Expected finding_coverage < 1.0 when agent missed the finding"
        )

    def test_calibrate_returns_float_in_unit_interval(self):
        """
        calibrate() must return a float in [0.0, 1.0].
        Tests the method signature and return type.
        """
        mock_llm = _make_mock_llm({
            "finding_coverage": 1.0,
            "severity_accuracy": 1.0,
            "reasoning": "ok",
        })
        judge = PRReviewJudge(llm_client=mock_llm)

        golden = GoldenPR(
            id="cal_001",
            pr_title="",
            pr_description="",
            diff_snippet="",
            expected_verdict="approve",
            expected_findings=(),
            difficulty="easy",
            category="quality",
            notes="",
        )

        calibration_set = [
            {
                "golden": golden,
                "actual_verdict": "approve",
                "actual_findings": [],
                "human_passes": True,
            },
            {
                "golden": golden,
                "actual_verdict": "request_changes",
                "actual_findings": [],
                "human_passes": False,
            },
        ]

        rate = judge.calibrate(calibration_set)
        assert isinstance(rate, float), f"calibrate() returned {type(rate)}, expected float"
        assert 0.0 <= rate <= 1.0, f"calibrate() returned {rate}, expected [0, 1]"


# ─────────────────────────────────────────────────────────────
# GROUP C: Regression Gate -- threshold logic (5 tests)
# ─────────────────────────────────────────────────────────────

class TestRegressionGate:

    def test_check_threshold_passes_when_all_above_minimum(self):
        """All examples >= 0.80 -> aggregate and all slices pass."""
        gate = RegressionGate()
        results = [
            _make_eval_result(example_id=f"ex_{i}", score=0.80)
            for i in range(5)
        ]
        passed, reason = gate.check_threshold(results)
        assert passed, f"Expected pass but got: {reason}"

    def test_check_threshold_blocks_when_aggregate_below_minimum(self):
        """
        Aggregate below PASS_THRESHOLD (0.70) -> gate blocks.
        Wiki (StaticEval anti-pattern): evaluation must catch quality drops.
        """
        gate = RegressionGate()
        results = [
            _make_eval_result(example_id=f"ex_{i}", score=0.50)
            for i in range(5)
        ]
        passed, reason = gate.check_threshold(results)
        assert not passed, "Expected gate to block but it passed"
        assert "below" in reason.lower() or "threshold" in reason.lower(), (
            f"Reason should mention threshold: {reason}"
        )

    def test_check_threshold_blocks_on_weak_slice_even_if_aggregate_ok(self):
        """
        One hard slice with low scores (0.45) should fail SLICE_THRESHOLD (0.60)
        even if the aggregate is fine (padded by easy examples at 0.90).
        Wiki: "A model that averages 90% may fail catastrophically on edge cases."
        """
        gate = RegressionGate()
        # 8 easy examples at 0.90 -> aggregate ~0.78
        easy = [
            _make_eval_result(
                example_id=f"easy_{i}", score=0.90, difficulty="easy"
            )
            for i in range(8)
        ]
        # 2 hard examples at 0.45 -> hard-slice mean = 0.45 < SLICE_THRESHOLD
        hard = [
            _make_eval_result(
                example_id=f"hard_{i}", score=0.45, difficulty="hard"
            )
            for i in range(2)
        ]
        results = easy + hard
        # Verify aggregate is fine
        agg = sum(r.score for r in results) / len(results)
        assert agg >= GATE_PASS, f"Setup error: aggregate {agg:.2f} should be >= {GATE_PASS}"

        passed, reason = gate.check_threshold(results)
        assert not passed, (
            f"Gate should block on weak 'hard' slice but passed. Reason: {reason}"
        )
        assert "hard" in reason.lower() or "slice" in reason.lower(), (
            f"Reason should mention slice: {reason}"
        )

    def test_compare_to_baseline_blocks_on_large_drop(self):
        """
        5%+ aggregate drop from baseline -> regression gate blocks.
        Wiki: "alert when recent_score < baseline_score * 0.95"
        """
        gate = RegressionGate()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            baseline_path = f.name
            json.dump({
                "aggregate": 0.85,
                "scores_by_id": {"ex_0": 0.85, "ex_1": 0.85},
                "n_examples": 2,
            }, f)

        # Current results: dropped to 0.70 from 0.85 (delta = -0.15 > -0.05 threshold)
        results = [
            _make_eval_result(example_id="ex_0", score=0.70),
            _make_eval_result(example_id="ex_1", score=0.70),
        ]
        passed, reason = gate.compare_to_baseline(results, baseline_path)
        assert not passed, f"Expected regression block but got: {reason}"
        assert "regression" in reason.lower() or "baseline" in reason.lower(), reason
        os.unlink(baseline_path)

    def test_compare_to_baseline_passes_when_stable(self):
        """Score within 5% of baseline -> gate passes."""
        gate = RegressionGate()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            baseline_path = f.name
            json.dump({
                "aggregate": 0.80,
                "scores_by_id": {"ex_0": 0.80, "ex_1": 0.80},
                "n_examples": 2,
            }, f)

        # Current: 0.78, delta = -0.02, within tolerance
        results = [
            _make_eval_result(example_id="ex_0", score=0.78),
            _make_eval_result(example_id="ex_1", score=0.78),
        ]
        passed, reason = gate.compare_to_baseline(results, baseline_path)
        assert passed, f"Expected pass (stable scores) but got: {reason}"
        os.unlink(baseline_path)


# ─────────────────────────────────────────────────────────────
# GROUP D: Integration (5 tests)
# ─────────────────────────────────────────────────────────────

class TestIntegration:

    def _make_judge_all_perfect(self) -> PRReviewJudge:
        """Judge that returns perfect scores for any input."""
        mock_llm = _make_mock_llm({
            "finding_coverage": 1.0,
            "severity_accuracy": 1.0,
            "reasoning": "Perfect",
        })
        return PRReviewJudge(llm_client=mock_llm)

    def test_run_suite_returns_one_result_per_golden_example(self):
        """
        run_suite() must return exactly one EvalResult for every GoldenPR
        passed in, including edge cases with empty diffs.
        """
        gate = RegressionGate()
        judge = self._make_judge_all_perfect()
        golden = load_golden_dataset()

        # run_fn always returns the expected verdict so scores will be high
        def run_fn(pr: GoldenPR) -> tuple[str, list[dict]]:
            return pr.expected_verdict, []

        results = gate.run_suite(golden=golden, judge=judge, run_fn=run_fn)
        assert len(results) == len(golden), (
            f"Expected {len(golden)} results, got {len(results)}"
        )
        result_ids = {r.example_id for r in results}
        for pr in golden:
            assert pr.id in result_ids, (
                f"Golden example {pr.id} missing from run_suite() output"
            )

    def test_compute_slice_metrics_produces_correct_slices(self):
        """
        compute_slice_metrics() must produce slice keys for each unique
        difficulty, category, and expected_verdict in the results.
        """
        gate = RegressionGate()
        results = [
            _make_eval_result("a", score=0.80, difficulty="easy",   category="security",  expected_verdict="approve"),
            _make_eval_result("b", score=0.75, difficulty="hard",   category="quality",   expected_verdict="request_changes"),
            _make_eval_result("c", score=0.90, difficulty="medium", category="security",  expected_verdict="needs_human_review"),
        ]
        slices = gate.compute_slice_metrics(results)

        assert "difficulty:easy" in slices
        assert "difficulty:hard" in slices
        assert "category:security" in slices
        assert "verdict:approve" in slices
        # Verify counts
        assert slices["difficulty:easy"].count == 1
        assert slices["category:security"].count == 2

    def test_save_and_load_baseline_round_trip(self):
        """
        save_baseline() followed by load_baseline() must return the same
        aggregate and scores_by_id, with no data loss.
        """
        gate = RegressionGate()
        results = [
            _make_eval_result("r1", score=0.82),
            _make_eval_result("r2", score=0.91),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "baseline.json")
            gate.save_baseline(results, path)
            loaded = gate.load_baseline(path)

        assert loaded is not None, "load_baseline returned None after save"
        assert abs(loaded["aggregate"] - 0.865) < 0.001, (
            f"Expected aggregate 0.865, got {loaded['aggregate']}"
        )
        assert loaded["scores_by_id"]["r1"] == pytest.approx(0.82)
        assert loaded["scores_by_id"]["r2"] == pytest.approx(0.91)

    def test_verdict_correct_computed_for_all_verdict_types(self):
        """
        run_suite() correctly computes verdict_correct for all 3 verdict types.
        A run_fn that always returns 'approve' should produce:
          - verdict_correct=True  for APPROVE examples
          - verdict_correct=False for REQUEST_CHANGES examples
          - verdict_correct=False for NEEDS_HUMAN_REVIEW examples
        """
        gate = RegressionGate()
        judge = self._make_judge_all_perfect()

        golden = [
            GoldenPR(
                id="v_approve",
                pr_title="", pr_description="", diff_snippet="",
                expected_verdict="approve",
                expected_findings=(), difficulty="easy", category="quality", notes="",
            ),
            GoldenPR(
                id="v_changes",
                pr_title="", pr_description="", diff_snippet="",
                expected_verdict="request_changes",
                expected_findings=(), difficulty="medium", category="quality", notes="",
            ),
            GoldenPR(
                id="v_hitl",
                pr_title="", pr_description="", diff_snippet="",
                expected_verdict="needs_human_review",
                expected_findings=(), difficulty="hard", category="security", notes="",
            ),
        ]

        # run_fn always returns "approve"
        results = gate.run_suite(
            golden=golden,
            judge=judge,
            run_fn=lambda pr: ("approve", []),
        )

        by_id = {r.example_id: r for r in results}
        assert by_id["v_approve"].verdict_correct is True
        assert by_id["v_changes"].verdict_correct is False
        assert by_id["v_hitl"].verdict_correct is False

    def test_edge_case_empty_diff_does_not_crash_run_suite(self):
        """
        The edge_empty_diff example (diff_snippet="") must not crash run_suite().
        Wiki (EvaluatingOnlyHappyPath): edge inputs must be in the dataset.
        """
        gate = RegressionGate()
        judge = self._make_judge_all_perfect()

        # Pick only the empty-diff edge case
        edge_examples = [ex for ex in load_golden_dataset() if ex.id == "edge_empty_diff"]
        assert edge_examples, "edge_empty_diff not found in golden dataset"

        results = gate.run_suite(
            golden=edge_examples,
            judge=judge,
            run_fn=lambda pr: ("approve", []),
        )
        assert len(results) == 1, "Expected exactly 1 result for edge_empty_diff"
        assert results[0].example_id == "edge_empty_diff"
        # Should not have crashed -- score should be a valid float
        assert 0.0 <= results[0].score <= 1.0


# ─────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import subprocess
    result = subprocess.run(
        ["python3", "-m", "pytest", __file__, "-v", "--tb=short"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
    )
    sys.exit(result.returncode)
