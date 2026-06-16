"""Tests for the pure verification core (Gap B, Story 2.1).

Everything here is IO-free: prompt rendering, verdict salvage, and the
majority-vote decision math. The poller-side gate (subprocess seam, REFUTED
persistence, off-by-default) is exercised in
``tests/test_verification_gate.py``.
"""

from __future__ import annotations

import json

from bubo.verification import (
    DEFAULT_LENSES,
    LENS_INSTRUCTIONS,
    Verdict,
    build_verification_prompt,
    decide,
    parse_verdict,
    votes_summary,
)

_FINDING = {
    "title": "off-by-one in loop bound",
    "file": "src/app/loop.py",
    "line": 42,
    "type": "issue",
    "severity": "blocking",
    "category": "correctness",
    "evidence": "range(0, n) should be range(0, n + 1)",
}


# --- prompt builder -------------------------------------------------------


def test_prompt_includes_finding_lens_and_json_contract() -> None:
    prompt = build_verification_prompt(_FINDING, lens="correctness")
    # The finding's claim is present.
    assert "off-by-one in loop bound" in prompt
    assert "src/app/loop.py:42" in prompt
    assert "range(0, n) should be range(0, n + 1)" in prompt
    # The lens angle is present.
    assert "CORRECTNESS" in prompt
    # The strict JSON contract is spelled out.
    assert '"real"' in prompt
    assert '"confidence"' in prompt
    assert '"reason"' in prompt
    # It asks the verifier to refute and to be conservative.
    assert "REFUTE" in prompt
    assert "NOT real" in prompt


def test_prompt_each_lens_carries_its_own_angle() -> None:
    for lens in DEFAULT_LENSES:
        prompt = build_verification_prompt(_FINDING, lens=lens)
        marker = LENS_INSTRUCTIONS[lens].split(".")[0]  # e.g. "Lens: CORRECTNESS"
        assert marker in prompt


def test_prompt_unknown_lens_falls_back_to_correctness() -> None:
    prompt = build_verification_prompt(_FINDING, lens="nonsense")
    assert "CORRECTNESS" in prompt


def test_prompt_includes_optional_diff_excerpt() -> None:
    prompt = build_verification_prompt(_FINDING, lens="in_diff", diff_excerpt="+ added line here")
    assert "RELEVANT DIFF:" in prompt
    assert "+ added line here" in prompt


# --- parser salvage -------------------------------------------------------


def test_parse_plain_object() -> None:
    verdict = parse_verdict('{"real": true, "confidence": 0.9, "reason": "confirmed"}', lens="x")
    assert verdict is not None
    assert verdict.real is True
    assert verdict.confidence == 0.9
    assert verdict.reason == "confirmed"
    assert verdict.lens == "x"
    assert verdict.ok is True


def test_parse_last_object_wins_amid_transcript() -> None:
    raw = (
        'thinking... {"real": false, "confidence": 0.2, "reason": "maybe"}\n'
        'final:\n{"real": true, "confidence": 0.8, "reason": "yes"}\n'
    )
    verdict = parse_verdict(raw)
    assert verdict is not None
    assert verdict.real is True
    assert verdict.confidence == 0.8


def test_parse_garbage_returns_none() -> None:
    assert parse_verdict("no json here at all") is None


def test_parse_missing_real_key_returns_none() -> None:
    # An object without a "real" key is not a verdict — treated as no answer.
    assert parse_verdict('{"confidence": 0.9, "reason": "x"}') is None


def test_parse_conservative_real_coercion() -> None:
    # Only literal JSON true counts as real; truthy strings/ints do not.
    assert parse_verdict('{"real": "true", "confidence": 0.9}').real is False
    assert parse_verdict('{"real": 1, "confidence": 0.9}').real is False


def test_parse_missing_or_bad_confidence_is_zero() -> None:
    # "real, no confidence" cannot slip past the floor.
    assert parse_verdict('{"real": true}').confidence == 0.0
    assert parse_verdict('{"real": true, "confidence": "high"}').confidence == 0.0
    assert parse_verdict('{"real": true, "confidence": 1.5}').confidence == 0.0
    assert parse_verdict('{"real": true, "confidence": -0.1}').confidence == 0.0


# --- decide: majority math ------------------------------------------------


def _v(real: bool, confidence: float, *, ok: bool = True) -> Verdict:
    return Verdict(lens="l", real=real, confidence=confidence, reason="", ok=ok)


def test_decide_majority_survives() -> None:
    result = decide(
        [_v(True, 0.7), _v(True, 0.8), _v(False, 0.9)],
        min_votes=2,
        confidence_floor=0.6,
    )
    assert result.survives is True
    assert result.real_votes == 2
    assert result.total == 3


def test_decide_minority_refuted() -> None:
    result = decide(
        [_v(True, 0.9), _v(False, 0.9), _v(False, 0.9)],
        min_votes=2,
        confidence_floor=0.6,
    )
    assert result.survives is False
    assert result.real_votes == 1


def test_decide_confidence_floor_excludes_low_confidence_real_votes() -> None:
    # Two "real" votes, but one is below the floor → only one counts.
    result = decide(
        [_v(True, 0.9), _v(True, 0.5)],
        min_votes=2,
        confidence_floor=0.6,
    )
    assert result.survives is False
    assert result.real_votes == 1


def test_decide_floor_is_inclusive() -> None:
    # confidence exactly at the floor counts (matches min_confidence semantics).
    result = decide([_v(True, 0.6), _v(True, 0.6)], min_votes=2, confidence_floor=0.6)
    assert result.survives is True
    assert result.real_votes == 2


def test_decide_tie_below_min_votes_refuted() -> None:
    # 1 real / 1 not-real with min_votes=2 → does not survive.
    result = decide([_v(True, 0.9), _v(False, 0.9)], min_votes=2, confidence_floor=0.6)
    assert result.survives is False


def test_decide_failed_checks_never_count_as_real_votes() -> None:
    # ok=False verdicts are ignored even if real/high-confidence.
    result = decide(
        [_v(True, 1.0, ok=False), _v(True, 1.0, ok=False)],
        min_votes=2,
        confidence_floor=0.6,
    )
    assert result.survives is False
    assert result.real_votes == 0
    assert result.total == 2


def test_decide_empty_does_not_survive() -> None:
    result = decide([], min_votes=1, confidence_floor=0.6)
    assert result.survives is False
    assert result.real_votes == 0


def test_decide_min_votes_one_single_real_survives() -> None:
    result = decide([_v(True, 0.7)], min_votes=1, confidence_floor=0.6)
    assert result.survives is True


# --- votes_summary --------------------------------------------------------


def test_votes_summary_is_valid_json_with_per_lens_tally() -> None:
    verdicts = [
        Verdict(lens="correctness", real=True, confidence=0.9, reason="ok"),
        Verdict(lens="in_diff", real=False, confidence=0.3, reason="no", ok=False),
    ]
    parsed = json.loads(votes_summary(verdicts))
    assert parsed == [
        {"lens": "correctness", "real": True, "confidence": 0.9, "ok": True, "reason": "ok"},
        {"lens": "in_diff", "real": False, "confidence": 0.3, "ok": False, "reason": "no"},
    ]
