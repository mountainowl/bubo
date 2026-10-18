"""Reviewers are told to check config errors swallowed as domain results (#128).

A human reviewer asked for an invalid-configuration parse error to throw to the
caller (which marks the request failed and logs it) instead of being recorded
as a price/fare failure code; Bubo missed it. The rule now reaches the agent,
and the benchmark corpus carries a labelled case of that shape.
"""

from __future__ import annotations

import json
from pathlib import Path

from bubo.review_config import ReviewConfig
from bubo.scm.github import GitHubProvider

CASE = Path(__file__).resolve().parents[1] / "benchmarks" / "cases" / "java-config-error-001.json"


def test_review_prompt_asks_about_config_errors_turned_into_ordinary_results() -> None:
    change = {"number": 1, "title": "t", "head": {"ref": "f", "sha": "h"}, "base": {"ref": "main"}}
    prompt = GitHubProvider().review_prompt("o/r", change, ReviewConfig(provider="github"))
    assert "catches a configuration, schema, or contract error" in prompt
    assert "ask whether it should propagate there" in prompt


def test_benchmark_case_labels_the_swallowed_config_error() -> None:
    case = json.loads(CASE.read_text(encoding="utf-8"))
    [truth] = case["ground_truth"]
    assert truth["file"].endswith("PriceCheck.java")
    assert truth["category"] == "failure"
    added = [line[1:] for line in case["diff"].splitlines() if line.startswith("+")]
    assert any("catch (IllegalArgumentException e)" in line for line in added)
    assert any("throw new IllegalArgumentException" in line for line in added)
