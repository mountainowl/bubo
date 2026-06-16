"""Tests for the [review].tone "mood" knob.

Covers: config parsing/validation, that the voice directive is injected into the
review prompt for non-default tones (and absent for terse), that the posted body
honors the tone while falling back safely, and — the load-bearing invariant —
that the dedup fingerprint is mood-neutral so switching tone never re-posts or
splits a finding's outcome history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bubo import db, paths
from bubo.config_values import ConfigError
from bubo.findings import finding_body, finding_comment_body, finding_fingerprint
from bubo.review_config import VALID_TONES, ReviewConfig, review_config_from_dict
from bubo.scm.base import build_review_contract, comment_voice_directive
from bubo.scm.github import GitHubProvider

# A complete structured finding (no comment field) — the terse baseline.
FINDING = {
    "type": "issue",
    "severity": "non-blocking",
    "category": "correctness",
    "title": "popitem removes duplicate-name cookies",
    "file": "src/requests/cookies.py",
    "line": 313,
    "impact": "Removes more cookies than the single pair it returns.",
    "evidence": "del self[name] routes through remove_cookie_by_name across domains.",
    "fix": "Clear the selected Cookie by its exact (domain, path, name).",
    "confidence": 0.99,
}

# The distinctive phrase only the voice directive carries.
VOICE_SENTINEL = 'add a "comment" field'


# --- config -------------------------------------------------------------


def test_tone_defaults_to_terse() -> None:
    assert ReviewConfig().tone == "terse"
    assert review_config_from_dict({}).tone == "terse"


def test_tone_parses_every_valid_value() -> None:
    for tone in VALID_TONES:
        assert review_config_from_dict({"review": {"tone": tone}}).tone == tone


def test_tone_is_normalized() -> None:
    assert review_config_from_dict({"review": {"tone": " Collaborative "}}).tone == "collaborative"


def test_invalid_tone_raises_configerror() -> None:
    with pytest.raises(ConfigError):
        review_config_from_dict({"review": {"tone": "snarky"}})


# --- prompt injection ---------------------------------------------------


def test_terse_omits_voice_directive() -> None:
    assert comment_voice_directive("terse") == ""
    contract = build_review_contract(ReviewConfig(tone="terse", max_findings_per_merge_request=8))
    assert VOICE_SENTINEL not in contract
    assert "Return [] when there are no actionable findings." in contract


def test_each_mood_injects_its_voice_and_keeps_the_cap() -> None:
    for tone in ("collaborative", "socratic", "formal", "casual"):
        contract = build_review_contract(
            ReviewConfig(tone=tone, max_findings_per_merge_request=8)
        )
        assert VOICE_SENTINEL in contract
        assert "at most 8 findings" in contract  # cap survives the voice append


def test_provider_prompt_injects_voice_only_for_mood() -> None:
    change = {
        "html_url": "https://github.com/o/r/pull/5",
        "number": 5,
        "head": {"ref": "feature", "sha": "deadbeef"},
        "base": {"ref": "main"},
        "title": "t",
    }
    provider = GitHubProvider()
    terse = provider.review_prompt("o/r", change, ReviewConfig(provider="github", tone="terse"))
    mood = provider.review_prompt(
        "o/r", change, ReviewConfig(provider="github", tone="collaborative")
    )
    assert VOICE_SENTINEL not in terse
    assert VOICE_SENTINEL in mood


# --- posted body --------------------------------------------------------


def test_terse_posts_structured_body() -> None:
    assert finding_comment_body(FINDING, "terse") == finding_body(FINDING)


def test_mood_posts_the_comment_field() -> None:
    f = {**FINDING, "comment": "Heads up — this can over-delete duplicate-name cookies."}
    assert (
        finding_comment_body(f, "collaborative")
        == "Heads up — this can over-delete duplicate-name cookies."
    )


def test_mood_falls_back_to_structured_without_comment() -> None:
    assert finding_comment_body(FINDING, "formal") == finding_body(FINDING)


def test_mood_ignores_blank_comment() -> None:
    assert finding_comment_body({**FINDING, "comment": "   "}, "casual") == finding_body(FINDING)


# --- the load-bearing invariant -----------------------------------------


def test_fingerprint_is_mood_invariant() -> None:
    # Adding an in-voice comment must NOT change the dedup fingerprint, or
    # switching tone would re-post findings and split their outcome history.
    with_comment = {**FINDING, "comment": "A totally different sounding human note."}
    assert finding_fingerprint("o/r", 5, "sha", with_comment) == finding_fingerprint(
        "o/r", 5, "sha", FINDING
    )


# --- per-run tone tracking (Gap D: measure mood effectiveness) ----------


def test_run_start_persists_tone_into_audit_trail(tmp_path: Path) -> None:
    # The active [review].tone is recorded on the review_runs row so accept/
    # dispute rates can later be A/B'd by tone. It surfaces in the audit trail.
    original = paths.DB
    try:
        paths.DB = tmp_path / "reviewer.sqlite"
        db.init_db()
        run_id = db.review_run_id("o/r", 5, "deadbeef")
        db.record_review_run_start(
            run_id=run_id,
            project="o/r",
            iid=5,
            sha="deadbeef",
            model="gpt-5.5",
            prompt_version="v1",
            review_mode="diff",
            dry_run=True,
            tone="collaborative",
        )
        rows = db.audit_rows(since_hours=720, project="o/r")
        assert len(rows) == 1
        assert rows[0]["tone"] == "collaborative"
    finally:
        paths.DB = original


def test_run_start_tone_defaults_to_terse(tmp_path: Path) -> None:
    # A caller that omits tone (and legacy rows) read back as the terse default.
    original = paths.DB
    try:
        paths.DB = tmp_path / "reviewer.sqlite"
        db.init_db()
        run_id = db.review_run_id("o/r", 6, "cafe")
        db.record_review_run_start(
            run_id=run_id,
            project="o/r",
            iid=6,
            sha="cafe",
            model="m",
            prompt_version="v",
            review_mode="diff",
            dry_run=True,
        )
        rows = db.audit_rows(since_hours=720, project="o/r")
        assert rows[0]["tone"] == "terse"
    finally:
        paths.DB = original
