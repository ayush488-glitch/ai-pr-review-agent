# backend/evaluation/regression_gate.py
#
# Phase 9: Regression Gate -- blocks deployment if eval scores drop
#
# Wiki (Evaluation-Frameworks.md):
#   "Every time you deploy a new agent version, you're making a bet.
#    The new version should be better or at least not worse.
#    Regression testing validates this."
#
# Wiki anti-pattern avoided (StaticEval):
#   "Running eval once, before deployment. But production is dynamic."
#   This gate saves a baseline to disk so CI can compare against it on
#   every future deploy -- not just today's manual run.
#
# Key design decisions:
#
# 1. run_fn is a Callable[[GoldenPR], tuple[str, list[dict]]] -- takes a
#    GoldenPR and returns (verdict, findings). This decouples the gate from
#    the LangGraph engine. Tests pass a lambda. Production wires in the real
#    engine. Same "dependency inversion" principle as BaseAgent.
#
# 2. Two threshold levels:
#    PASS_THRESHOLD (0.70)  -- minimum aggregate score to pass the gate
#    SLICE_THRESHOLD (0.60) -- minimum score for ANY individual slice
#    This prevents "passes on average but fails on hard examples" scenarios.
#    Wiki: "A model that averages 90% may fail catastrophically on edge cases."
#
# 3. Baseline is persisted as JSON to a path provided by caller.
#    CI scripts provide the path. Tests use tmp_path. Production wires in
#    a stable path like .hermes/eval_baseline.json.
#
# 4. Regression block criterion: > 5% absolute score drop from baseline.
#    Same threshold as the wiki's continuous eval example (baseline * 0.95).
#    Individual example regression: score drops by > 0.10 on a single example.

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from backend.evaluation.golden_dataset import GoldenPR
from backend.evaluation.judge import PRReviewJudge, JudgeScore

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────

@dataclass
class EvalResult:
    """
    Result of evaluating one golden example through the full pipeline.

    Maps 1:1 to a GoldenPR entry after run_suite() completes.
    """
    example_id: str
    difficulty: str
    category: str
    expected_verdict: str
    actual_verdict: str
    verdict_correct: bool
    finding_coverage: float
    severity_accuracy: float
    score: float            # JudgeScore.overall_score
    reasoning: str


@dataclass
class SliceMetrics:
    """
    Aggregated metrics for a named slice of the eval dataset.

    Wiki (MetricsWithoutContext): always report per-slice, not just aggregate.
    """
    slice_name: str
    count: int
    mean_score: float
    pass_rate: float        # fraction with score >= PASS_THRESHOLD
    min_score: float
    max_score: float


# ─────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────

PASS_THRESHOLD = 0.70      # aggregate and per-example minimum
SLICE_THRESHOLD = 0.60     # minimum mean score for any single slice
REGRESSION_DELTA = 0.05    # block if baseline drops by more than this (absolute)
PER_EXAMPLE_REGRESSION = 0.10  # an individual example "regresses" if score drops this much


# ─────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────

class RegressionGate:
    """
    Orchestrates the full evaluation suite and enforces quality gates.

    Usage (production):
        gate = RegressionGate()
        results = gate.run_suite(
            golden=load_golden_dataset(),
            judge=PRReviewJudge(llm_client=...),
            run_fn=lambda pr: engine.run_review(pr.diff_snippet, pr.pr_title),
        )
        passed, reason = gate.check_threshold(results)
        if not passed:
            raise SystemExit(f"GATE FAILED: {reason}")
        gate.save_baseline(results, ".hermes/eval_baseline.json")

    Usage (tests):
        gate = RegressionGate()
        # run_fn returns scripted responses -- no real LLM or engine
        results = gate.run_suite(
            golden=examples,
            judge=mock_judge,
            run_fn=lambda pr: ("approve", []),
        )
    """

    def run_suite(
        self,
        golden: list[GoldenPR],
        judge: PRReviewJudge,
        run_fn: Callable[[GoldenPR], tuple[str, list[dict]]],
    ) -> list[EvalResult]:
        """
        Run the full evaluation suite.

        For each golden example:
          1. Call run_fn(pr) to get the agent's (verdict, findings).
          2. Call judge.score_review() to get a JudgeScore.
          3. Wrap into an EvalResult.

        run_fn signature: (GoldenPR) -> (verdict_str, findings_list)
        findings_list items: {"agent_type": str, "severity": str, "summary": str}

        run_fn errors are caught per-example so one crash does not abort the
        whole suite. The failed example gets score=0.0 with error in reasoning.
        """
        results: list[EvalResult] = []

        for pr in golden:
            try:
                actual_verdict, actual_findings = run_fn(pr)
            except Exception as exc:
                logger.error("run_fn crashed on example %s: %s", pr.id, exc)
                # Score 0.0 for crashed examples -- do not skip silently.
                results.append(EvalResult(
                    example_id=pr.id,
                    difficulty=pr.difficulty,
                    category=pr.category,
                    expected_verdict=pr.expected_verdict,
                    actual_verdict="ERROR",
                    verdict_correct=False,
                    finding_coverage=0.0,
                    severity_accuracy=0.0,
                    score=0.0,
                    reasoning=f"run_fn raised: {exc}",
                ))
                continue

            try:
                js: JudgeScore = judge.score_review(
                    golden=pr,
                    actual_verdict=actual_verdict,
                    actual_findings=actual_findings,
                )
            except Exception as exc:
                logger.error("judge.score_review crashed on example %s: %s", pr.id, exc)
                results.append(EvalResult(
                    example_id=pr.id,
                    difficulty=pr.difficulty,
                    category=pr.category,
                    expected_verdict=pr.expected_verdict,
                    actual_verdict=actual_verdict,
                    verdict_correct=False,
                    finding_coverage=0.0,
                    severity_accuracy=0.0,
                    score=0.0,
                    reasoning=f"judge.score_review raised: {exc}",
                ))
                continue

            results.append(EvalResult(
                example_id=pr.id,
                difficulty=pr.difficulty,
                category=pr.category,
                expected_verdict=pr.expected_verdict,
                actual_verdict=actual_verdict,
                verdict_correct=js.verdict_correct,
                finding_coverage=js.finding_coverage,
                severity_accuracy=js.severity_accuracy,
                score=js.overall_score,
                reasoning=js.reasoning,
            ))

        return results

    # ─────────────────────────────────────────────────────────
    # Slice metrics
    # ─────────────────────────────────────────────────────────

    def compute_slice_metrics(
        self,
        results: list[EvalResult],
    ) -> dict[str, SliceMetrics]:
        """
        Break down results by difficulty and category slices.

        Wiki (MetricsWithoutContext anti-pattern): aggregate metrics hide
        where the agent fails. Always report per-slice.

        Returns a dict keyed by slice name, e.g.:
          "difficulty:easy", "difficulty:hard",
          "category:security", "category:quality", ...
          "verdict:approve", "verdict:needs_human_review", ...
        """
        # Group by each dimension
        buckets: dict[str, list[float]] = defaultdict(list)
        for r in results:
            buckets[f"difficulty:{r.difficulty}"].append(r.score)
            buckets[f"category:{r.category}"].append(r.score)
            buckets[f"verdict:{r.expected_verdict}"].append(r.score)

        slice_metrics: dict[str, SliceMetrics] = {}
        for slice_name, scores in buckets.items():
            if not scores:
                continue
            mean = sum(scores) / len(scores)
            pass_rate = sum(1 for s in scores if s >= PASS_THRESHOLD) / len(scores)
            slice_metrics[slice_name] = SliceMetrics(
                slice_name=slice_name,
                count=len(scores),
                mean_score=mean,
                pass_rate=pass_rate,
                min_score=min(scores),
                max_score=max(scores),
            )

        return slice_metrics

    # ─────────────────────────────────────────────────────────
    # Threshold check (absolute gate -- no baseline needed)
    # ─────────────────────────────────────────────────────────

    def check_threshold(
        self,
        results: list[EvalResult],
    ) -> tuple[bool, str]:
        """
        Enforce PASS_THRESHOLD and SLICE_THRESHOLD.

        Returns (passed: bool, reason: str).
        On failure, reason explains which gate failed.

        Gates (checked in order):
        1. Aggregate mean score >= PASS_THRESHOLD
        2. Every slice mean score >= SLICE_THRESHOLD
        """
        if not results:
            return False, "No eval results -- cannot determine pass/fail."

        scores = [r.score for r in results]
        aggregate = sum(scores) / len(scores)

        # Gate 1: aggregate
        if aggregate < PASS_THRESHOLD:
            return False, (
                f"Aggregate score {aggregate:.3f} is below "
                f"PASS_THRESHOLD {PASS_THRESHOLD}. "
                f"({len(results)} examples evaluated)"
            )

        # Gate 2: per-slice
        slice_metrics = self.compute_slice_metrics(results)
        for slice_name, sm in slice_metrics.items():
            if sm.mean_score < SLICE_THRESHOLD:
                return False, (
                    f"Slice '{slice_name}' mean score {sm.mean_score:.3f} is below "
                    f"SLICE_THRESHOLD {SLICE_THRESHOLD}. "
                    f"({sm.count} examples in slice)"
                )

        return True, (
            f"Passed. Aggregate={aggregate:.3f} ({len(results)} examples). "
            f"All {len(slice_metrics)} slices above {SLICE_THRESHOLD}."
        )

    # ─────────────────────────────────────────────────────────
    # Baseline comparison (deploy-time regression gate)
    # ─────────────────────────────────────────────────────────

    def compare_to_baseline(
        self,
        results: list[EvalResult],
        baseline_path: str,
    ) -> tuple[bool, str]:
        """
        Compare current scores against a stored baseline.

        Blocks if:
        - Aggregate score drops by > REGRESSION_DELTA (5 percentage points)
        - More than 5% of examples regress individually by > PER_EXAMPLE_REGRESSION

        Wiki: "The new version should be better or at least not worse."
        Wiki continuous eval: alert when recent_score < baseline_score * 0.95.
        We use absolute delta (not relative) to avoid floating-point edge cases
        near 0.

        Returns (passed: bool, reason: str).
        Returns (True, reason) if no baseline exists yet (first run).
        """
        baseline = self.load_baseline(baseline_path)
        if baseline is None:
            return True, "No baseline found -- first run. Accepting current scores."

        baseline_scores: dict[str, float] = baseline.get("scores_by_id", {})
        baseline_aggregate: float = baseline.get("aggregate", 0.0)

        # Current aggregate
        current_scores = {r.example_id: r.score for r in results}
        current_aggregate = (
            sum(current_scores.values()) / len(current_scores)
            if current_scores else 0.0
        )

        # Gate 1: aggregate regression
        delta = current_aggregate - baseline_aggregate
        if delta < -REGRESSION_DELTA:
            return False, (
                f"Aggregate regression detected. "
                f"Baseline={baseline_aggregate:.3f}, "
                f"Current={current_aggregate:.3f}, "
                f"Delta={delta:+.3f} (threshold=-{REGRESSION_DELTA})."
            )

        # Gate 2: per-example regression count
        regressions = []
        for example_id, current_score in current_scores.items():
            if example_id in baseline_scores:
                per_delta = current_score - baseline_scores[example_id]
                if per_delta < -PER_EXAMPLE_REGRESSION:
                    regressions.append((example_id, per_delta))

        regression_rate = len(regressions) / len(current_scores) if current_scores else 0.0
        if regression_rate > 0.05:
            reg_list = ", ".join(
                f"{eid}({d:+.2f})" for eid, d in regressions[:5]
            )
            return False, (
                f"Too many per-example regressions: {len(regressions)}/{len(current_scores)} "
                f"({regression_rate:.1%}) dropped by >{PER_EXAMPLE_REGRESSION}. "
                f"Examples: {reg_list}"
            )

        return True, (
            f"Regression gate passed. "
            f"Baseline={baseline_aggregate:.3f}, "
            f"Current={current_aggregate:.3f}, "
            f"Delta={delta:+.3f}. "
            f"Per-example regressions: {len(regressions)}/{len(current_scores)}."
        )

    # ─────────────────────────────────────────────────────────
    # Baseline persistence
    # ─────────────────────────────────────────────────────────

    def save_baseline(
        self,
        results: list[EvalResult],
        baseline_path: str,
    ) -> None:
        """
        Persist current eval results as the new baseline.

        Saves: aggregate score + per-example scores + metadata.
        Called after a successful gate pass to update the reference point.
        """
        scores_by_id = {r.example_id: r.score for r in results}
        aggregate = sum(scores_by_id.values()) / len(scores_by_id) if scores_by_id else 0.0

        payload = {
            "aggregate": aggregate,
            "scores_by_id": scores_by_id,
            "n_examples": len(results),
            "pass_threshold": PASS_THRESHOLD,
            "slice_threshold": SLICE_THRESHOLD,
        }

        path = Path(baseline_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))
        logger.info("Saved eval baseline to %s (aggregate=%.3f)", baseline_path, aggregate)

    def load_baseline(
        self,
        baseline_path: str,
    ) -> Optional[dict]:
        """
        Load a previously saved baseline. Returns None if file does not exist.

        Returns None gracefully on parse errors (corrupt file) with a warning,
        rather than crashing CI. The gate will treat this as a first run.
        """
        path = Path(baseline_path)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not load baseline from %s: %s -- treating as first run.",
                baseline_path, exc,
            )
            return None
