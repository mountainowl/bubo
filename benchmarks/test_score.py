"""Tests for the benchmark scoring core. Collected by the normal `uv run pytest`
(benchmarks/ has no __init__.py, so pytest puts it on the path → `import score`)."""

from __future__ import annotations

from score import Efficiency, Issue, aggregate, localized_match, score_case


def test_localized_match_file_and_line_tolerance() -> None:
    truth = Issue(file="a/calc.py", line=12, category="correctness")
    assert localized_match(Issue("./a/calc.py", 13), truth, line_tolerance=2, require_category=False)
    assert not localized_match(Issue("a/calc.py", 20), truth, line_tolerance=2, require_category=False)
    assert not localized_match(Issue("a/other.py", 12), truth, line_tolerance=2, require_category=False)


def test_require_category_only_binds_when_both_present() -> None:
    truth = Issue(file="a.py", line=5, category="security")
    # finding without a category still localizes
    assert localized_match(Issue("a.py", 5), truth, line_tolerance=0, require_category=True)
    # mismatched categories miss
    assert not localized_match(
        Issue("a.py", 5, category="style"), truth, line_tolerance=0, require_category=True
    )


def test_score_case_tp_fp_fn_and_rates() -> None:
    truth = [Issue("a.py", 10), Issue("a.py", 50), Issue("b.py", 3)]
    findings = [Issue("a.py", 10), Issue("a.py", 99)]  # 1 hit, 1 false positive
    s = score_case("c1", "bubo", findings, truth, line_tolerance=2)
    assert (s.tp, s.fp, s.fn) == (1, 1, 2)
    assert s.precision == 0.5  # 1 / (1+1)
    assert s.recall == round(1 / 3, 4)
    assert s.fp_rate == 0.5


def test_greedy_matching_no_double_count() -> None:
    # two findings on the same defect must not score as two true positives
    truth = [Issue("a.py", 10)]
    findings = [Issue("a.py", 10), Issue("a.py", 11)]
    s = score_case("c", "bubo", findings, truth, line_tolerance=2)
    assert (s.tp, s.fp, s.fn) == (1, 1, 0)


def test_clean_case_no_findings_is_perfect_no_division_error() -> None:
    s = score_case("clean", "bubo", [], [], line_tolerance=2)
    assert (s.tp, s.fp, s.fn, s.precision, s.recall, s.f1, s.fp_rate) == (0, 0, 0, 0.0, 0.0, 0.0, 0.0)


def test_clean_case_with_a_finding_is_a_false_positive() -> None:
    s = score_case("clean", "bubo", [Issue("a.py", 1)], [], line_tolerance=2)
    assert (s.tp, s.fp, s.fn, s.fp_rate) == (0, 1, 0, 1.0)


def test_aggregate_micro_averages_and_sums_efficiency() -> None:
    a = score_case("c1", "bubo", [Issue("a.py", 1)], [Issue("a.py", 1)],
                   efficiency=Efficiency(tokens_total=1000, seconds=10.0, cost_usd=0.2))
    b = score_case("c2", "bubo", [Issue("b.py", 1)], [Issue("b.py", 1), Issue("b.py", 9)],
                   efficiency=Efficiency(tokens_total=2000, seconds=20.0, cost_usd=0.4))
    agg = aggregate([a, b])
    assert agg["tp"] == 2 and agg["fp"] == 0 and agg["fn"] == 1
    assert agg["recall"] == round(2 / 3, 4)
    assert agg["precision"] == 1.0
    assert agg["tokens_total"] == 3000
    assert agg["cost_usd"] == 0.6
    assert agg["tokens_per_true_positive"] == 1500.0
