"""Verification stops running lenses once the outcome cannot change.

The poller turns a finding's verdicts into one of three outcomes via
:func:`decide`: verified (``survives``), refuted (``refuted``) or unresolved
(neither, the finding is held). These tests pin that stopping early never
changes that outcome:

* a pure, exhaustive check of :func:`outcome_settled` over every lens answer
  sequence for 1-5 lenses and every ``min_votes`` from 1 to 5; and
* an end-to-end run of the real ``post_or_plan_findings`` ->
  ``run_verification`` path, with only the verifier agent call faked, for
  every combination of three lens answers, comparing the early-stop build
  against running every lens.
"""

from __future__ import annotations

import itertools
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from review_output import review_output

from bubo import db, paths, poller
from bubo.review_config import ReviewConfig
from bubo.verification import Verdict, decide, outcome_settled

FLOOR = 0.6
# Every kind of lens answer: failed to run, confirmed above and below the
# floor, refuted above and below the floor, and insufficient evidence.
KINDS = {
    "fail": Verdict(lens="", real=None, confidence=0.0, reason="", ok=False),
    "confirm": Verdict(lens="", real=True, confidence=0.9, reason="r"),
    "weak_confirm": Verdict(lens="", real=True, confidence=0.3, reason="r"),
    "refute": Verdict(lens="", real=False, confidence=0.9, reason="r"),
    "weak_refute": Verdict(lens="", real=False, confidence=0.3, reason="r"),
    "unsure": Verdict(lens="", real=None, confidence=0.9, reason="r"),
}


def _outcome(verdicts: list[Verdict], min_votes: int) -> str:
    """The poller's three-way decision in post_or_plan_findings."""
    result = decide(verdicts, min_votes=min_votes, confidence_floor=FLOOR)
    if result.survives:
        return "verified"
    if result.refuted:
        return "refuted"
    return "unresolved"


def _early_stopped(verdicts: list[Verdict], min_votes: int) -> list[Verdict]:
    """Replay run_verification's loop: the prefix of lenses that would run."""
    ran: list[Verdict] = []
    for index, verdict in enumerate(verdicts):
        if outcome_settled(
            ran, remaining=len(verdicts) - index, min_votes=min_votes, confidence_floor=FLOOR
        ):
            break
        ran.append(verdict)
    return ran


@pytest.mark.parametrize("lenses", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("min_votes", [1, 2, 3, 4, 5])
def test_early_stop_never_changes_the_outcome(lenses: int, min_votes: int) -> None:
    for combo in itertools.product(KINDS.values(), repeat=lenses):
        full = list(combo)
        stopped = _early_stopped(full, min_votes)
        assert stopped == full[: len(stopped)]
        assert _outcome(stopped, min_votes) == _outcome(full, min_votes), (combo, stopped)


def test_default_config_skips_the_third_lens_when_the_first_two_agree() -> None:
    for first, second, third in itertools.product(KINDS, repeat=3):
        stopped = _early_stopped([KINDS[first], KINDS[second], KINDS[third]], min_votes=2)
        # Settled after two: both confirm, both refute, or neither counts
        # (one more vote can never reach the two-vote quorum).
        neutral = {"fail", "weak_confirm", "weak_refute", "unsure"}
        agree = (first, second) in {("confirm", "confirm"), ("refute", "refute")} or {
            first,
            second,
        } <= neutral
        assert len(stopped) == (2 if agree else 3), (first, second, third)


# --- end to end: real post_or_plan_findings -> run_verification ------------

AGENT_ANSWERS = {
    "fail": None,
    "confirm": {"verdict": "confirmed", "confidence": 0.9, "reason": "reachable"},
    "weak_confirm": {"verdict": "confirmed", "confidence": 0.3, "reason": "maybe"},
    "refute": {"verdict": "refuted", "confidence": 0.9, "reason": "guarded"},
    "unsure": {"verdict": "insufficient_evidence", "confidence": 0.5, "reason": "unclear"},
}


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


class _PlanProvider:
    name = "gitlab"

    def change_number(self, mr):
        return mr["iid"]

    def get_change(self, *a, **k):
        return {}

    def head_sha(self, change):
        return "sha"

    def changed_lines(self, *a, **k):
        return {}

    def build_position(self, change, changed, finding):
        return {"new_path": "f.py", "new_line": finding.get("line")}


def _review(answers: tuple[str, ...], *, early_stop: bool) -> tuple[object, int, int]:
    """Run the real posting path; only the verifier agent call is faked."""
    calls: list[str] = []

    def agent(argv, **_kwargs):
        calls.append(argv[-1])
        answer = AGENT_ANSWERS[answers[len(calls) - 1]]
        if answer is None:
            return SimpleNamespace(stdout="boom", stderr="", returncode=1)
        return SimpleNamespace(stdout=json.dumps(answer), stderr="", returncode=0)

    cfg = ReviewConfig(
        dry_run=True,
        verify_findings=True,
        reviewer_command=["codex", "exec"],
        verify_min_votes=2,
        verify_confidence_floor=FLOOR,
    )
    settled = poller.outcome_settled if early_stop else (lambda *a, **k: False)
    with (
        _temp_db(),
        patch.object(poller, "run", side_effect=agent),
        patch.object(poller, "outcome_settled", settled),
    ):
        _posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=review_output([{"file": "f.py", "line": 1}]),
            provider=_PlanProvider(),
        )
        rows = db.findings_for("g/r", 1, "sha")
    status = rows[0]["status"] if rows else None
    return (status, planned, skipped), len(calls), len(rows)


def test_real_posting_path_same_result_with_fewer_agent_runs() -> None:
    before = after = 0
    statuses = set()
    for answers in itertools.product(AGENT_ANSWERS, repeat=3):
        old, old_runs, _ = _review(answers, early_stop=False)
        new, new_runs, _ = _review(answers, early_stop=True)
        assert new == old, answers
        assert old_runs == 3
        statuses.add(old[0])
        before += old_runs
        after += new_runs
    # All three outcomes were exercised.
    assert {"planned", "refuted", "verify_capped"} <= {str(s) for s in statuses}
    # 11 of the 25 (first, second) pairs settle the vote (confirm+confirm,
    # refute+refute, or any two of fail/weak_confirm/unsure), so 11 * 5 = 55
    # of 125 cases skip the third run.
    assert (before, after) == (375, 320)
