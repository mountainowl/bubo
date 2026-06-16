"""Poller-side gate for opt-in verification (Gap B, Story 2.2/2.3).

Mirrors ``tests/test_dispute_suppression.py``: a faked provider drives
``post_or_plan_findings`` and the verifier subprocess is replaced by
monkeypatching ``poller.run_verification`` so no agent is ever spawned.

Covers the load-bearing guarantees:
- OFF by default: the verifier seam is never called and the post path is
  unchanged.
- ON + a faked refute: the finding is dropped, recorded ``REFUTED``, and
  never posted.
- ON + a faked real verdict: the finding posts and the verdict columns are
  persisted.
- The cost guard: findings past ``verify_max_findings`` post unverified.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from bubo import db, paths, poller
from bubo.config_values import ConfigError
from bubo.review_config import DEFAULT_VERIFY_LENSES, ReviewConfig, review_config_from_dict
from bubo.statuses import FindingStatus
from bubo.verification import Verdict


@contextmanager
def _temp_db() -> Iterator[None]:
    original = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            yield
    finally:
        paths.DB = original


def _finding(line: int = 1) -> str:
    return (
        f'{{"category": "correctness", "file": "f.py", "line": {line}, '
        '"confidence": 0.99, "title": "real bug", "type": "issue", '
        '"severity": "blocking", "impact": "x", "evidence": "y", "fix": "z"}'
    )


def _raw(*findings: str) -> str:
    return "[" + ",".join(findings) + "]"


class _PlanProvider:
    """Maps every finding to a placeable position (dry-run → PLANNED)."""

    name = "gitlab"

    def change_number(self, mr):
        return mr["iid"]

    def get_change(self, *a, **k):
        return {}

    def changed_lines(self, *a, **k):
        return {}

    def build_position(self, change, changed, finding):
        return {"new_path": "f.py", "new_line": finding.get("line")}


class _PostProvider(_PlanProvider):
    """Like _PlanProvider but also records inline posts (non-dry-run path)."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    def post_inline_comment(self, cfg, token, project, number, body, position):
        self.posted.append(body)
        return f"disc-{len(self.posted)}"


def _real_verdicts() -> list[Verdict]:
    return [
        Verdict(lens="correctness", real=True, confidence=0.9, reason="ok"),
        Verdict(lens="in_diff", real=True, confidence=0.8, reason="ok"),
    ]


def _refute_verdicts() -> list[Verdict]:
    return [
        Verdict(lens="correctness", real=False, confidence=0.9, reason="no"),
        Verdict(lens="in_diff", real=False, confidence=0.8, reason="no"),
    ]


# --- OFF by default -------------------------------------------------------


def test_verify_off_never_calls_seam_and_plans_normally() -> None:
    cfg = ReviewConfig(dry_run=True, verify_findings=False)
    with _temp_db(), patch.object(
        poller, "run_verification", side_effect=AssertionError("verifier must not run")
    ):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=_raw(_finding()),
            provider=_PlanProvider(),
        )
        rows = db.findings_for("g/r", 1, "sha")
    assert (posted, planned, skipped) == (0, 1, 0)
    assert rows[0]["status"] == FindingStatus.PLANNED
    # Verdict columns stay NULL when the pass did not run.
    assert rows[0]["verified"] is None


# --- ON: refute drops the finding -----------------------------------------


def test_verify_refute_drops_and_records_refuted() -> None:
    cfg = ReviewConfig(dry_run=True, verify_findings=True, verify_min_votes=2)
    events: list[tuple[str, dict[str, object]]] = []

    with _temp_db(), patch.object(
        poller, "run_verification", return_value=_refute_verdicts()
    ), patch.object(poller, "log", lambda e, **f: events.append((e, f))):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=_raw(_finding()),
            provider=_PlanProvider(),
        )
        rows = db.findings_for("g/r", 1, "sha")
    # Refuted folds into skipped (tuple arity unchanged) — NOT planned/posted.
    assert (posted, planned, skipped) == (0, 0, 1)
    assert rows[0]["status"] == FindingStatus.REFUTED
    assert rows[0]["verified"] == 0
    refuted = [f for e, f in events if e == "finding_refuted"]
    assert len(refuted) == 1
    assert refuted[0]["votes"] == "0/2"


def test_verify_refute_does_not_post() -> None:
    cfg = ReviewConfig(dry_run=False, verify_findings=True, verify_min_votes=2)
    provider = _PostProvider()
    with _temp_db(), patch.object(poller, "run_verification", return_value=_refute_verdicts()):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=_raw(_finding()),
            provider=provider,
        )
    assert (posted, planned, skipped) == (0, 0, 1)
    assert provider.posted == []  # never posted


# --- ON: verified finding posts -------------------------------------------


def test_verify_real_posts_and_persists_verdict() -> None:
    cfg = ReviewConfig(dry_run=False, verify_findings=True, verify_min_votes=2)
    provider = _PostProvider()
    with _temp_db(), patch.object(poller, "run_verification", return_value=_real_verdicts()):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=_raw(_finding()),
            provider=provider,
        )
        rows = db.findings_for("g/r", 1, "sha")
    assert (posted, planned, skipped) == (1, 0, 0)
    assert len(provider.posted) == 1
    assert rows[0]["status"] == FindingStatus.POSTED
    assert rows[0]["verified"] == 1


# --- ON: cost guard -------------------------------------------------------


def test_verify_cap_posts_extra_findings_unverified() -> None:
    # 3 findings, cap of 1: the first is verified, the rest post unverified.
    cfg = ReviewConfig(
        dry_run=True,
        verify_findings=True,
        verify_min_votes=2,
        verify_max_findings=1,
    )
    calls: list[int] = []

    def _fake(finding, repo, cfg):
        calls.append(finding["line"])
        return _real_verdicts()

    events: list[tuple[str, dict[str, object]]] = []
    with _temp_db(), patch.object(poller, "run_verification", side_effect=_fake), patch.object(
        poller, "log", lambda e, **f: events.append((e, f))
    ):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=_raw(_finding(1), _finding(2), _finding(3)),
            provider=_PlanProvider(),
        )
    # All 3 plan; only 1 was verified (the cap), the rest planned unverified.
    assert (posted, planned, skipped) == (0, 3, 0)
    assert calls == [1]  # verifier called once, then capped
    capped = [f for e, f in events if e == "finding_verify_capped"]
    assert len(capped) == 2
    summary = [f for e, f in events if e == "verification_summary"]
    assert len(summary) == 1
    assert summary[0]["verified"] == 1
    assert summary[0]["capped"] == 2


# --- ON: verifier outage posts unverified (not silently dropped) ----------


def test_verify_partial_outage_does_not_refute() -> None:
    # 1 lens ran and said real (0.9); 2 failed. With min_votes=2 the finding
    # does not "survive", but too few lenses RAN to call it a refutation — a
    # flaky verifier must never delete a real finding. It plans unverified.
    cfg = ReviewConfig(dry_run=True, verify_findings=True, verify_min_votes=2)
    mixed = [
        Verdict(lens="correctness", real=True, confidence=0.9, reason="ok"),
        Verdict(lens="in_diff", real=False, confidence=0.0, reason="", ok=False),
        Verdict(lens="reproduce", real=False, confidence=0.0, reason="", ok=False),
    ]
    with _temp_db(), patch.object(poller, "run_verification", return_value=mixed):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=_raw(_finding()),
            provider=_PlanProvider(),
        )
        rows = db.findings_for("g/r", 1, "sha")
    assert (posted, planned, skipped) == (0, 1, 0)
    assert rows[0]["status"] == FindingStatus.PLANNED
    assert rows[0]["verified"] is None


def test_verify_all_checks_failed_posts_unverified() -> None:
    cfg = ReviewConfig(dry_run=True, verify_findings=True, verify_min_votes=2)
    failed = [
        Verdict(lens="correctness", real=False, confidence=0.0, reason="", ok=False),
        Verdict(lens="in_diff", real=False, confidence=0.0, reason="", ok=False),
    ]
    with _temp_db(), patch.object(poller, "run_verification", return_value=failed):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=_raw(_finding()),
            provider=_PlanProvider(),
        )
        rows = db.findings_for("g/r", 1, "sha")
    # A verifier outage must NOT drop the finding — it plans unverified.
    assert (posted, planned, skipped) == (0, 1, 0)
    assert rows[0]["status"] == FindingStatus.PLANNED
    assert rows[0]["verified"] is None


# --- run_verification seam: no command → no verdicts ----------------------


def test_run_verification_empty_command_yields_no_verdicts() -> None:
    cfg = ReviewConfig(verify_findings=True, reviewer_command=[], verify_command=[])
    assert poller.run_verification({"file": "f.py", "line": 1}, None, cfg) == []


# --- config parse / validate ----------------------------------------------


def test_config_defaults_verification_off() -> None:
    cfg = review_config_from_dict({})
    assert cfg.verify_findings is False
    assert cfg.verify_lenses == DEFAULT_VERIFY_LENSES
    assert cfg.verify_min_votes == 2
    assert cfg.verify_confidence_floor == 0.6
    assert cfg.verify_max_findings == 5
    assert cfg.verify_timeout_seconds == 300
    # Empty by default — resolves to reviewer_command at the use-site.
    assert cfg.verify_command == []


def test_config_parses_verification_block() -> None:
    cfg = review_config_from_dict(
        {
            "review": {
                "verify_findings": True,
                "verify_lenses": ["Correctness", "REPRODUCE"],
                "verify_min_votes": 1,
                "verify_confidence_floor": 0.75,
                "verify_max_findings": 3,
                "verify_timeout_seconds": 120,
                "verify_command": ["claude", "-p"],
            }
        }
    )
    assert cfg.verify_findings is True
    # Lens names lowercased (case-insensitive matching against LENS_INSTRUCTIONS).
    assert cfg.verify_lenses == ["correctness", "reproduce"]
    assert cfg.verify_min_votes == 1
    assert cfg.verify_confidence_floor == 0.75
    assert cfg.verify_max_findings == 3
    assert cfg.verify_timeout_seconds == 120
    # Command preserves case (it's an argv, not a label).
    assert cfg.verify_command == ["claude", "-p"]


def test_config_empty_lens_list_falls_back_to_default() -> None:
    cfg = review_config_from_dict({"review": {"verify_lenses": []}})
    assert cfg.verify_lenses == DEFAULT_VERIFY_LENSES


def test_config_rejects_string_for_verify_findings() -> None:
    with pytest.raises(ConfigError, match="verify_findings"):
        review_config_from_dict({"review": {"verify_findings": "true"}})


def test_config_rejects_out_of_range_floor() -> None:
    with pytest.raises(ConfigError, match="verify_confidence_floor"):
        review_config_from_dict({"review": {"verify_confidence_floor": 1.5}})


def test_config_rejects_non_positive_min_votes() -> None:
    with pytest.raises(ConfigError, match="verify_min_votes"):
        review_config_from_dict({"review": {"verify_min_votes": 0}})
