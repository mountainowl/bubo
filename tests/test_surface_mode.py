"""Tests for canonical category normalization and the [review].mode preset.

Covers two pieces of the operator-configurable precision lever:

* :func:`normalize_category` / :func:`normalize_finding_categories` — the
  reviewer's free-form ``category`` is mapped onto a fixed canonical taxonomy in
  a *separate* ``category_canonical`` field, leaving the original label (and
  therefore the body, fingerprint, and audit row) untouched.
* the ``gate`` surface preset — a conjunction (assertion type + blocking-tier
  severity + canonical defect category) that the OR-based ``allowed_kinds``
  whitelist cannot express, wired through ``filter_findings_by_policy`` so its
  drops flow through the same logged-reason path. ``collaborate`` (the default)
  is a no-op: byte-for-byte the pre-mode behavior.
"""

from __future__ import annotations

import pytest

from bubo.config_values import ConfigError
from bubo.findings import (
    CANONICAL_CATEGORIES,
    DEFECT_CATEGORIES,
    NON_DEFECT_CATEGORIES,
    filter_findings_by_policy,
    finding_body,
    finding_canonical_category,
    gate_surfaces,
    normalize_category,
    normalize_finding_categories,
    surface_predicate_for_mode,
)
from bubo.review_config import DEFAULT_MODE, VALID_MODES, review_config_from_dict


# --------------------------------------------------------------------------- #
# normalize_category
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        # defects — synonyms collapse to one bucket
        ("correctness", "correctness"),
        ("logic", "correctness"),
        ("code-logic", "correctness"),
        ("compatibility", "correctness"),  # a compat break is a correctness defect
        ("security", "security"),
        ("vulnerability", "security"),
        ("race-condition", "concurrency"),
        ("memory-leak", "resource"),
        ("failure", "error_handling"),  # the contract's own term
        ("error-handling", "error_handling"),
        ("perf", "performance"),
        # non-defects
        ("code-style", "style"),
        ("readability", "style"),
        ("documentation", "docs"),  # contract term
        ("testing", "test"),
        ("test-coverage", "test"),
        ("maintainability", "design"),  # contract term
        ("code-quality", "design"),
        ("naming", "naming"),
        # unknown / non-defect catch-alls → other (never promoted into the gate)
        ("usability", "other"),
        ("ci", "other"),
        ("observability", "other"),
        ("frobnicate", "other"),
    ],
)
def test_normalize_category_maps_synonyms(raw: str, canonical: str) -> None:
    assert normalize_category(raw) == canonical


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Error Handling", "error_handling"),  # spaces folded
        ("error_handling", "error_handling"),  # underscores folded
        ("CODE_STYLE", "style"),  # case + underscore
        ("  Logic  ", "correctness"),  # surrounding whitespace
    ],
)
def test_normalize_category_is_case_and_separator_insensitive(raw: str, canonical: str) -> None:
    assert normalize_category(raw) == canonical


@pytest.mark.parametrize("bad", [None, 123, 4.5, ["correctness"], {}, ""])
def test_normalize_category_unknown_or_malformed_is_other(bad: object) -> None:
    assert normalize_category(bad) == "other"


def test_normalize_category_is_total_and_canonical() -> None:
    # Every input lands in exactly one canonical bucket; the two halves are disjoint.
    samples = ["logic", "security", "ci", "weird", None, "", "STYLE", "perf"]
    assert all(normalize_category(s) in CANONICAL_CATEGORIES for s in samples)
    assert DEFECT_CATEGORIES.isdisjoint(NON_DEFECT_CATEGORIES)
    assert "other" in NON_DEFECT_CATEGORIES


# --------------------------------------------------------------------------- #
# normalize_finding_categories — annotate without mutating the original label
# --------------------------------------------------------------------------- #
def test_normalize_finding_categories_annotates_and_preserves_original() -> None:
    findings = [
        {"category": "Logic", "title": "a"},
        {"category": "documentation", "title": "b"},
        {"title": "no category"},
    ]

    result = normalize_finding_categories(findings)

    assert [f["category_canonical"] for f in result] == ["correctness", "docs", "other"]
    # The reviewer's own label is untouched (drives body/fingerprint/audit).
    assert findings[0]["category"] == "Logic"
    assert findings[1]["category"] == "documentation"
    assert "category" not in findings[2]


def test_canonical_field_does_not_leak_into_posted_body() -> None:
    finding = {
        "type": "issue",
        "severity": "blocking",
        "category": "Logic",  # original, free-form
        "title": "offset is now required",
    }
    normalize_finding_categories([finding])

    body = finding_body(finding)
    # Body shows the ORIGINAL label, never the canonical annotation.
    assert "**Issue (blocking, Logic):** offset is now required" in body
    assert "category_canonical" not in body
    assert "correctness" not in body


def test_finding_canonical_category_prefers_annotation_then_falls_back() -> None:
    assert finding_canonical_category({"category_canonical": "security"}) == "security"
    # Falls back to normalizing `category` when not pre-annotated.
    assert finding_canonical_category({"category": "logic"}) == "correctness"
    assert finding_canonical_category({}) == "other"


# --------------------------------------------------------------------------- #
# gate_surfaces — the conjunction (and its deliberate edge cases)
# --------------------------------------------------------------------------- #
def test_gate_keeps_blocking_defect_issue() -> None:
    assert gate_surfaces({"type": "issue", "severity": "blocking", "category": "logic"}) is True


def test_gate_keeps_high_and_critical_severity_defects() -> None:
    # Severity is brittle: models emit high/critical, not just the contract's
    # "blocking". A literal == "blocking" check would silently drop these.
    assert gate_surfaces({"type": "issue", "severity": "high", "category": "security"}) is True
    assert gate_surfaces({"type": "issue", "severity": "critical", "category": "resource"}) is True


def test_gate_drops_suggestions_and_questions_even_in_defect_categories() -> None:
    # The whole point of the gate lane: a suggestion/question cannot be a merge
    # blocker, regardless of how it is categorized.
    assert (
        gate_surfaces({"type": "suggestion", "severity": "blocking", "category": "correctness"})
        is False
    )
    assert (
        gate_surfaces({"type": "question", "severity": "blocking", "category": "security"}) is False
    )


def test_gate_drops_non_defect_categories() -> None:
    assert (
        gate_surfaces({"type": "issue", "severity": "blocking", "category": "documentation"})
        is False
    )
    assert (
        gate_surfaces({"type": "issue", "severity": "blocking", "category": "code-style"}) is False
    )
    assert gate_surfaces({"type": "issue", "severity": "blocking", "category": "ci"}) is False


def test_gate_requires_explicit_blocking_severity() -> None:
    assert (
        gate_surfaces({"type": "issue", "severity": "non-blocking", "category": "correctness"})
        is False
    )
    assert gate_surfaces({"type": "issue", "severity": "low", "category": "correctness"}) is False
    # Missing/blank severity is not gated through — a merge gate blocks only on
    # an explicit high-severity signal.
    assert gate_surfaces({"type": "issue", "category": "correctness"}) is False


def test_gate_treats_missing_type_as_an_assertion() -> None:
    # No `type` field defaults to an assertion (matches finding_body's default),
    # so it can still surface if it is a blocking defect.
    assert gate_surfaces({"severity": "blocking", "category": "correctness"}) is True


# --------------------------------------------------------------------------- #
# surface_predicate_for_mode
# --------------------------------------------------------------------------- #
def test_surface_predicate_for_mode_resolution() -> None:
    assert surface_predicate_for_mode("gate") is gate_surfaces
    assert surface_predicate_for_mode("GATE") is gate_surfaces  # case-insensitive
    assert surface_predicate_for_mode("collaborate") is None
    assert surface_predicate_for_mode("anything-else") is None


# --------------------------------------------------------------------------- #
# filter_findings_by_policy — surface predicate integration
# --------------------------------------------------------------------------- #
def _mixed_batch() -> list[dict]:
    # Mirrors the empirical shape: a few blocking defect issues plus a pile of
    # suggestions/questions and docs/style/CI nits.
    return [
        {
            "type": "issue",
            "severity": "blocking",
            "category": "logic",
            "confidence": 0.9,
            "title": "real-bug",
        },
        {
            "type": "issue",
            "severity": "high",
            "category": "security",
            "confidence": 0.9,
            "title": "real-vuln",
        },
        {
            "type": "suggestion",
            "severity": "blocking",
            "category": "correctness",
            "confidence": 0.9,
            "title": "sugg",
        },
        {
            "type": "question",
            "severity": "blocking",
            "category": "design",
            "confidence": 0.9,
            "title": "ques",
        },
        {
            "type": "issue",
            "severity": "blocking",
            "category": "documentation",
            "confidence": 0.9,
            "title": "doc-nit",
        },
        {
            "type": "issue",
            "severity": "non-blocking",
            "category": "style",
            "confidence": 0.9,
            "title": "style-nit",
        },
    ]


def test_gate_mode_keeps_only_blocking_defect_issues() -> None:
    findings = normalize_finding_categories(_mixed_batch())

    kept, dropped = filter_findings_by_policy(
        findings,
        min_confidence=0.85,
        surface_predicate=surface_predicate_for_mode("gate"),
    )

    assert [f["title"] for f in kept] == ["real-bug", "real-vuln"]
    assert {reason for _, reason in dropped} == {"surface_mode_excluded"}
    assert {f["title"] for f, _ in dropped} == {"sugg", "ques", "doc-nit", "style-nit"}


def test_collaborate_mode_is_a_no_op_backward_compatible() -> None:
    findings = normalize_finding_categories(_mixed_batch())

    kept, dropped = filter_findings_by_policy(
        findings,
        min_confidence=0.85,
        surface_predicate=surface_predicate_for_mode("collaborate"),
    )

    assert len(kept) == 6
    assert dropped == []


def test_default_no_surface_predicate_matches_collaborate() -> None:
    # Omitting surface_predicate entirely (the existing call shape) is identical
    # to collaborate — proves the change is additive.
    findings = normalize_finding_categories(_mixed_batch())
    kept, dropped = filter_findings_by_policy(findings, min_confidence=0.85)
    assert len(kept) == 6
    assert dropped == []


def test_confidence_filter_wins_before_surface_mode() -> None:
    findings = normalize_finding_categories(
        [
            {
                "type": "issue",
                "severity": "blocking",
                "category": "logic",
                "confidence": 0.4,
                "title": "low",
            },
        ]
    )
    _, dropped = filter_findings_by_policy(
        findings,
        min_confidence=0.85,
        surface_predicate=surface_predicate_for_mode("gate"),
    )
    # Earliest failing filter wins the reason.
    assert dropped[0][1] == "confidence_below_threshold"


def test_gate_and_allowed_kinds_both_apply_intersection() -> None:
    # A blocking correctness issue passes the gate but is rejected by an
    # allowed_kinds whitelist that lists only "docs" — both filters apply.
    findings = normalize_finding_categories(
        [
            {
                "type": "issue",
                "severity": "blocking",
                "category": "logic",
                "confidence": 0.9,
                "title": "x",
            }
        ]
    )
    kept, dropped = filter_findings_by_policy(
        findings,
        min_confidence=0.85,
        allowed_kinds=["docs"],
        surface_predicate=surface_predicate_for_mode("gate"),
    )
    assert kept == []
    assert dropped[0][1] == "kind_not_allowed"


# --------------------------------------------------------------------------- #
# [review].mode config parsing
# --------------------------------------------------------------------------- #
def test_review_config_mode_defaults_to_collaborate() -> None:
    assert DEFAULT_MODE == "collaborate"
    assert "gate" in VALID_MODES
    assert review_config_from_dict({}).mode == "collaborate"


def test_review_config_parses_mode_case_insensitively() -> None:
    assert review_config_from_dict({"review": {"mode": "GATE"}}).mode == "gate"


def test_review_config_rejects_unknown_mode() -> None:
    with pytest.raises(ConfigError) as exc:
        review_config_from_dict({"review": {"mode": "strict"}})
    assert "review.mode" in str(exc.value)
