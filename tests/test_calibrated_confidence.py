"""Tests for the calibrated per-class confidence lever (①c).

Covers the three pieces:

* the per-category floor **mechanism** in ``filter_findings_by_policy`` — a
  finding must clear ``max(min_confidence, category_floor)``, with a distinct
  ``confidence_below_category_floor`` drop reason, and floors that only ever
  *raise* the bar;
* the **populators** — ``dispute_stats_by_canonical`` (fold raw dispute stats
  onto the canonical taxonomy so synonym variants stop fragmenting the signal)
  and ``calibrated_category_floors`` (dispute-rate → floor, sample-gated); and
* config parsing/validation for ``[review.category_min_confidence]`` +
  ``calibrate_confidence`` / ``calibrate_max_confidence``.

Default (no floors, calibration off) is byte-for-byte the pre-①c behavior.
"""

from __future__ import annotations

import pytest

from bubo.config_values import ConfigError
from bubo.findings import (
    calibrated_category_floors,
    dispute_stats_by_canonical,
    filter_findings_by_policy,
    normalize_finding_categories,
)
from bubo.review_config import DEFAULT_CALIBRATE_MAX_CONFIDENCE, review_config_from_dict


# --------------------------------------------------------------------------- #
# dispute_stats_by_canonical — synonym variants fold into one canonical bucket
# --------------------------------------------------------------------------- #
def test_dispute_stats_fold_combines_synonyms() -> None:
    raw = [
        {"category": "test", "total": 3, "rejected": 2},
        {"category": "testing", "total": 4, "rejected": 3},
        {"category": "test-coverage", "total": 1, "rejected": 1},
        {"category": "correctness", "total": 10, "rejected": 1},
    ]
    folded = {r["category"]: r for r in dispute_stats_by_canonical(raw)}
    # test + testing + test-coverage -> one "test" bucket: 8 total / 6 rejected
    assert folded["test"]["total"] == 8
    assert folded["test"]["rejected"] == 6
    assert folded["test"]["dispute_rate"] == 6 / 8
    assert folded["correctness"]["total"] == 10


def test_dispute_stats_fold_unknown_labels_go_to_other() -> None:
    folded = {
        r["category"]: r
        for r in dispute_stats_by_canonical(
            [
                {"category": "ci", "total": 2, "rejected": 2},
                {"category": "frobnicate", "total": 1, "rejected": 0},
            ]
        )
    }
    assert folded["other"]["total"] == 3
    assert folded["other"]["rejected"] == 2


def test_dispute_stats_fold_empty() -> None:
    assert dispute_stats_by_canonical([]) == []


# --------------------------------------------------------------------------- #
# calibrated_category_floors — dispute rate -> floor, gated and bounded
# --------------------------------------------------------------------------- #
def test_calibrated_floors_interpolate_on_dispute_rate() -> None:
    stats = [
        {"category": "test", "total": 8, "rejected": 6},  # rate 0.75
        {"category": "correctness", "total": 10, "rejected": 1},  # rate 0.10
    ]
    floors = calibrated_category_floors(stats, base=0.85, max_floor=0.97, min_samples=5)
    # 0.85 + 0.75*(0.97-0.85) = 0.94 ; 0.85 + 0.10*0.12 = 0.862
    assert floors["test"] == pytest.approx(0.94)
    assert floors["correctness"] == pytest.approx(0.862)


def test_calibrated_floors_skip_thin_samples() -> None:
    # 4 samples < min_samples 5 -> not calibrated even at 100% dispute
    assert (
        calibrated_category_floors(
            [{"category": "docs", "total": 4, "rejected": 4}],
            base=0.85,
            max_floor=0.97,
            min_samples=5,
        )
        == {}
    )


def test_calibrated_floors_zero_rate_yields_no_entry() -> None:
    # rate 0 keeps the floor at base, so no entry is emitted (no-op category)
    assert (
        calibrated_category_floors(
            [{"category": "security", "total": 9, "rejected": 0}],
            base=0.85,
            max_floor=0.97,
            min_samples=5,
        )
        == {}
    )


def test_calibrated_floors_capped_at_max_floor() -> None:
    floors = calibrated_category_floors(
        [{"category": "style", "total": 20, "rejected": 20}],
        base=0.85,
        max_floor=0.97,
        min_samples=5,
    )
    # 100% dispute -> reaches but never exceeds max_floor
    assert floors["style"] == pytest.approx(0.97)


# --------------------------------------------------------------------------- #
# filter mechanism — per-category floor + distinct reason
# --------------------------------------------------------------------------- #
def _batch():
    return normalize_finding_categories(
        [
            {"type": "issue", "category": "style", "confidence": 0.90, "title": "style-90"},
            {"type": "issue", "category": "correctness", "confidence": 0.90, "title": "corr-90"},
            {"type": "issue", "category": "performance", "confidence": 0.95, "title": "perf-95"},
            {"type": "issue", "category": "style", "confidence": 0.80, "title": "style-80"},
            {"type": "issue", "category": "docs", "title": "no-conf"},
        ]
    )


def test_category_floor_raises_bar_with_distinct_reason() -> None:
    kept, dropped = filter_findings_by_policy(
        _batch(), min_confidence=0.85, category_floors={"style": 0.95, "performance": 0.92}
    )
    reasons = {f["title"]: r for f, r in dropped}
    assert [f["title"] for f in kept] == ["corr-90", "perf-95"]
    # cleared global 0.85 but under the style floor 0.95 -> distinct reason
    assert reasons["style-90"] == "confidence_below_category_floor"
    # below the GLOBAL bar -> generic reason (the category floor is moot)
    assert reasons["style-80"] == "confidence_below_threshold"
    # missing confidence -> generic reason
    assert reasons["no-conf"] == "confidence_below_threshold"


def test_category_floor_never_lowers_the_bar() -> None:
    # A floor set below the global min_confidence is a no-op — it never admits a
    # finding the global bar would reject.
    kept, _ = filter_findings_by_policy(
        _batch(), min_confidence=0.85, category_floors={"style": 0.5}
    )
    titles = [f["title"] for f in kept]
    assert "style-90" in titles
    assert "style-80" not in titles  # still dropped by the global 0.85 bar


def test_no_category_floors_is_unchanged_behavior() -> None:
    kept, dropped = filter_findings_by_policy(_batch(), min_confidence=0.85)
    assert [f["title"] for f in kept] == ["style-90", "corr-90", "perf-95"]
    assert all(r == "confidence_below_threshold" for _, r in dropped)


# --------------------------------------------------------------------------- #
# config parsing
# --------------------------------------------------------------------------- #
def test_config_defaults_off() -> None:
    cfg = review_config_from_dict({})
    assert cfg.category_min_confidence == {}
    assert cfg.calibrate_confidence is False
    assert cfg.calibrate_max_confidence == DEFAULT_CALIBRATE_MAX_CONFIDENCE == 0.97


def test_config_parses_manual_floors_and_calibration() -> None:
    cfg = review_config_from_dict(
        {
            "review": {
                "category_min_confidence": {"style": 0.95, "Performance": 0.9},
                "calibrate_confidence": True,
                "calibrate_max_confidence": 0.99,
            }
        }
    )
    # keys lowercased to the canonical form
    assert cfg.category_min_confidence == {"style": 0.95, "performance": 0.9}
    assert cfg.calibrate_confidence is True
    assert cfg.calibrate_max_confidence == 0.99


def test_config_rejects_unknown_category_key() -> None:
    with pytest.raises(ConfigError) as exc:
        review_config_from_dict({"review": {"category_min_confidence": {"performnce": 0.9}}})
    assert "category_min_confidence" in str(exc.value)
    assert "performnce" in str(exc.value)


def test_config_rejects_out_of_range_floor() -> None:
    with pytest.raises(ConfigError):
        review_config_from_dict({"review": {"category_min_confidence": {"style": 1.5}}})


def test_config_rejects_non_table_floors() -> None:
    with pytest.raises(ConfigError):
        review_config_from_dict({"review": {"category_min_confidence": ["style", 0.9]}})
