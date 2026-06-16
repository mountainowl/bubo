"""Tests for the pure governance-policy gate (governance Rec ②b, advisory only).

Covers the shared escalation predicate and the policy-evaluation decision in
:mod:`bubo.governance_policy`. The DB/provider/poller wiring lives in
``test_governance.py``.
"""

from __future__ import annotations

import pytest

from bubo.governance_policy import (
    ACTION_CLEAR,
    ACTION_FLAG,
    POLICY_OFF,
    POLICY_REPORT_ONLY,
    POLICY_SOFT,
    evaluate_policy,
    heightened_scrutiny_directive,
    is_escalated,
)
from bubo.provenance import (
    BAND_COLLABORATIVE,
    BAND_LIKELY_AI,
    BAND_UNKNOWN,
    ProvenanceSignal,
)

_ESCALATE = (BAND_LIKELY_AI, BAND_COLLABORATIVE)


def _signal(band=BAND_LIKELY_AI, sensitive=()):
    return ProvenanceSignal(band=band, sensitive_paths=list(sensitive))


# --- is_escalated truth matrix -------------------------------------------------
# band in/out of escalate_bands x sensitive present/absent x require_sensitive.


def test_escalated_band_in_set_sensitive_present_require_true() -> None:
    sig = _signal(BAND_LIKELY_AI, sensitive=["payments/charge.py"])
    assert is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=True)


def test_escalated_band_in_set_sensitive_present_require_false() -> None:
    sig = _signal(BAND_LIKELY_AI, sensitive=["payments/charge.py"])
    assert is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=False)


def test_not_escalated_band_in_set_no_sensitive_require_true() -> None:
    # The discriminating row: in-band but no sensitive path while sensitive is
    # required → NOT escalated.
    sig = _signal(BAND_LIKELY_AI, sensitive=[])
    assert not is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=True)


def test_escalated_band_in_set_no_sensitive_require_false() -> None:
    sig = _signal(BAND_LIKELY_AI, sensitive=[])
    assert is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=False)


def test_not_escalated_band_out_of_set_sensitive_present_require_true() -> None:
    sig = _signal(BAND_UNKNOWN, sensitive=["payments/charge.py"])
    assert not is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=True)


def test_not_escalated_band_out_of_set_sensitive_present_require_false() -> None:
    # Band gates first: out-of-band never escalates, even with sensitive paths.
    sig = _signal(BAND_UNKNOWN, sensitive=["payments/charge.py"])
    assert not is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=False)


def test_not_escalated_band_out_of_set_no_sensitive_require_true() -> None:
    sig = _signal(BAND_UNKNOWN, sensitive=[])
    assert not is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=True)


def test_not_escalated_band_out_of_set_no_sensitive_require_false() -> None:
    sig = _signal(BAND_UNKNOWN, sensitive=[])
    assert not is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=False)


def test_collaborative_band_also_escalates() -> None:
    sig = _signal(BAND_COLLABORATIVE, sensitive=["deploy/key.pem"])
    assert is_escalated(sig, escalate_bands=_ESCALATE, require_sensitive=True)


# --- evaluate_policy: mode x triggered ----------------------------------------

_MODES = [POLICY_OFF, POLICY_REPORT_ONLY, POLICY_SOFT]


@pytest.mark.parametrize("mode", _MODES)
def test_evaluate_triggered_with_sensitive_flags_band_plus_sensitive(mode) -> None:
    sig = _signal(BAND_LIKELY_AI, sensitive=["payments/charge.py"])
    decision = evaluate_policy(
        sig, mode=mode, escalate_bands=_ESCALATE, require_sensitive=True
    )
    assert decision.triggered is True
    assert decision.action == ACTION_FLAG
    assert decision.matched_rule == "band+sensitive"
    assert decision.mode == mode
    # band / sensitive_paths echoed from the signal.
    assert decision.band == BAND_LIKELY_AI
    assert decision.sensitive_paths == ["payments/charge.py"]


@pytest.mark.parametrize("mode", _MODES)
def test_evaluate_triggered_band_only_flags_band(mode) -> None:
    # In-band, no sensitive path, sensitive not required → triggered on band.
    sig = _signal(BAND_COLLABORATIVE, sensitive=[])
    decision = evaluate_policy(
        sig, mode=mode, escalate_bands=_ESCALATE, require_sensitive=False
    )
    assert decision.triggered is True
    assert decision.action == ACTION_FLAG
    assert decision.matched_rule == "band"
    assert decision.band == BAND_COLLABORATIVE
    assert decision.sensitive_paths == []


@pytest.mark.parametrize("mode", _MODES)
def test_evaluate_not_triggered_is_clear_empty_rule(mode) -> None:
    sig = _signal(BAND_UNKNOWN, sensitive=[])
    decision = evaluate_policy(
        sig, mode=mode, escalate_bands=_ESCALATE, require_sensitive=True
    )
    assert decision.triggered is False
    assert decision.action == ACTION_CLEAR
    assert decision.matched_rule == ""
    assert decision.mode == mode
    assert decision.band == BAND_UNKNOWN


def test_mode_off_still_flags_when_triggered() -> None:
    # The load-bearing rule: action depends ONLY on the predicate, never the
    # mode. evaluate_policy must not short-circuit on POLICY_OFF — the caller
    # decides whether to act based on mode.
    sig = _signal(BAND_LIKELY_AI, sensitive=["deploy/key.pem"])
    decision = evaluate_policy(
        sig, mode=POLICY_OFF, escalate_bands=_ESCALATE, require_sensitive=True
    )
    assert decision.triggered is True
    assert decision.action == ACTION_FLAG
    assert decision.mode == POLICY_OFF


def test_rigor_injected_passes_through() -> None:
    sig = _signal(BAND_LIKELY_AI, sensitive=["payments/charge.py"])
    assert (
        evaluate_policy(
            sig,
            mode=POLICY_SOFT,
            escalate_bands=_ESCALATE,
            require_sensitive=True,
            rigor_injected=True,
        ).rigor_injected
        is True
    )
    # Defaults to False when omitted.
    assert (
        evaluate_policy(
            sig, mode=POLICY_SOFT, escalate_bands=_ESCALATE, require_sensitive=True
        ).rigor_injected
        is False
    )


def test_decision_carries_a_reason_sentence() -> None:
    sig = _signal(BAND_LIKELY_AI, sensitive=["payments/charge.py"])
    decision = evaluate_policy(
        sig, mode=POLICY_SOFT, escalate_bands=_ESCALATE, require_sensitive=True
    )
    assert isinstance(decision.reason, str)
    assert decision.reason


def test_heightened_scrutiny_directive_mentions_security_lens() -> None:
    directive = heightened_scrutiny_directive()
    assert isinstance(directive, str)
    assert "security lens" in directive
