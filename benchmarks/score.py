"""Pure scoring for the bubo review benchmark — no I/O, no third-party deps.

Compares a reviewer's findings against labeled ground-truth issues per case and
computes the standard code-review-eval metrics — precision / recall / F1 and a
false-positive rate — plus the efficiency axes (tokens / seconds / cost).

Methodology follows the now-common "a finding *hits* a ground-truth issue when it
**localizes** it (right file + line) and **describes** it" rubric used by
real-world AI-code-review benchmarks. The default matcher is **deterministic**
(same file, line within a tolerance, optional category match) so a run is
reproducible and dependency-free; swap in an LLM-as-judge matcher for fuzzy
description matching (see benchmarks/README.md). Matching is greedy 1:1 — each
ground-truth issue is credited to at most one finding and vice-versa, so piling
on duplicate comments can't inflate true positives.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Issue:
    """One issue — either a labeled ground-truth defect or a reviewer finding."""

    file: str
    line: int
    category: str = ""
    severity: str = ""
    summary: str = ""


@dataclass(frozen=True, slots=True)
class Efficiency:
    """Cost of producing the findings for one case (the apples-to-apples axes)."""

    tokens_total: int = 0
    seconds: float = 0.0
    cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_id: str
    reviewer: str
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    fp_rate: float
    efficiency: Efficiency = field(default_factory=Efficiency)


def _norm_path(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./").lstrip("/")


def localized_match(
    finding: Issue, truth: Issue, *, line_tolerance: int, require_category: bool
) -> bool:
    """Deterministic hit test: same file, line within tolerance, optional category.

    ``require_category`` only constrains when *both* sides name a category, so a
    reviewer that omits categories is not penalized on localization alone.
    """
    if _norm_path(finding.file) != _norm_path(truth.file):
        return False
    if abs(finding.line - truth.line) > line_tolerance:
        return False
    if require_category and finding.category and truth.category:
        return finding.category.strip().lower() == truth.category.strip().lower()
    return True


Matcher = Callable[[Issue, Issue], bool]


def score_case(
    case_id: str,
    reviewer: str,
    findings: Sequence[Issue],
    ground_truth: Sequence[Issue],
    *,
    line_tolerance: int = 2,
    require_category: bool = False,
    matcher: Matcher | None = None,
    efficiency: Efficiency | None = None,
) -> CaseScore:
    """Score one case. ``matcher`` overrides the default localized match (e.g. an
    LLM-judge); when given, ``line_tolerance``/``require_category`` are ignored."""
    hit = matcher or (
        lambda f, t: localized_match(
            f, t, line_tolerance=line_tolerance, require_category=require_category
        )
    )
    matched_truth: set[int] = set()
    matched_finding: set[int] = set()
    for fi, finding in enumerate(findings):
        for ti, truth in enumerate(ground_truth):
            if ti in matched_truth:
                continue
            if hit(finding, truth):
                matched_truth.add(ti)
                matched_finding.add(fi)
                break
    tp = len(matched_truth)
    fp = len(findings) - len(matched_finding)
    fn = len(ground_truth) - len(matched_truth)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fp_rate = fp / (tp + fp) if (tp + fp) else 0.0
    return CaseScore(
        case_id=case_id,
        reviewer=reviewer,
        tp=tp,
        fp=fp,
        fn=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        fp_rate=round(fp_rate, 4),
        efficiency=efficiency or Efficiency(),
    )


def aggregate(case_scores: Sequence[CaseScore]) -> dict[str, object]:
    """Micro-average over cases (pooled TP/FP/FN) + summed efficiency.

    Micro-average (not mean-of-per-case) so larger cases weigh proportionally and
    the precision/recall stay internally consistent with the pooled counts.
    """
    tp = sum(c.tp for c in case_scores)
    fp = sum(c.fp for c in case_scores)
    fn = sum(c.fn for c in case_scores)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    tokens = sum(c.efficiency.tokens_total for c in case_scores)
    seconds = sum(c.efficiency.seconds for c in case_scores)
    cost = sum(c.efficiency.cost_usd for c in case_scores)
    return {
        "cases": len(case_scores),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "fp_rate": round(fp / (tp + fp), 4) if (tp + fp) else 0.0,
        "tokens_total": tokens,
        "seconds": round(seconds, 2),
        "cost_usd": round(cost, 4),
        "tokens_per_true_positive": round(tokens / tp, 1) if tp else None,
        "cost_per_true_positive_usd": round(cost / tp, 4) if tp else None,
    }
