from __future__ import annotations

import subprocess
from unittest.mock import patch

from bubo import outcome_classifier as oc
from bubo.review_config import ReviewConfig


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["agent"], returncode, stdout, None)


def test_classifier_command_substitutes_raw_codex_for_bundled_reviewer() -> None:
    # The default reviewer_command is the bundled bin/bubo-codex, which
    # hardwires the code-reviewer meta-prompt and cannot classify — so the
    # smart default falls back to the raw `codex exec` profile path.
    command = oc.classifier_command(ReviewConfig())
    assert command[0] == "codex"
    assert "exec" in command


def test_classifier_command_reuses_custom_reviewer_command() -> None:
    cfg = ReviewConfig(reviewer_command=["claude", "-p", "--allowedTools", "Read"])
    assert oc.classifier_command(cfg) == ["claude", "-p", "--allowedTools", "Read"]


def test_build_prompt_includes_finding_and_reply() -> None:
    prompt = oc.build_prompt("the finding text", "the developer reply")
    assert "the finding text" in prompt
    assert "the developer reply" in prompt
    assert "verdict" in prompt


def test_parse_verdict_reads_plain_object() -> None:
    assert oc.parse_verdict('{"verdict": "rejected", "false_positive": true}') == {
        "verdict": "rejected",
        "false_positive": True,
    }


def test_parse_verdict_last_object_wins_amid_transcript() -> None:
    raw = (
        'thinking... {"verdict": "unclear", "false_positive": false}\n'
        'final answer:\n{"verdict": "accepted", "false_positive": false}\n'
    )
    assert oc.parse_verdict(raw)["verdict"] == "accepted"


def test_parse_verdict_unknown_verdict_collapses_to_unclear() -> None:
    assert oc.parse_verdict('{"verdict": "maybe"}') == {
        "verdict": "unclear",
        "false_positive": False,
    }


def test_parse_verdict_garbage_is_unclear() -> None:
    assert oc.parse_verdict("no json here") == {"verdict": "unclear", "false_positive": False}


def test_classify_developer_reply_empty_reply_skips_agent() -> None:
    with patch("bubo.outcome_classifier.run_bounded") as mocked:
        out = oc.classify_developer_reply(ReviewConfig(), "finding", "   ")
    mocked.assert_not_called()
    assert out["verdict"] == "unclear"


def test_classify_developer_reply_maps_rejected_false_positive() -> None:
    cfg = ReviewConfig(reviewer_command=["claude", "-p"])
    completed = _completed('{"verdict": "rejected", "false_positive": true}')
    with patch("bubo.outcome_classifier.run_bounded", return_value=completed):
        out = oc.classify_developer_reply(cfg, "finding", "this is working as intended")
    assert out == {"verdict": "rejected", "false_positive": True}


def test_classify_developer_reply_signals_error_on_exception() -> None:
    # Transient failures return "error" (not "unclear") so the caller can
    # leave the finding unclassified and retry on a later sync.
    cfg = ReviewConfig(reviewer_command=["claude", "-p"])
    with patch("bubo.outcome_classifier.run_bounded", side_effect=RuntimeError("boom")):
        out = oc.classify_developer_reply(cfg, "finding", "some reply")
    assert out["verdict"] == "error"


def test_classify_developer_reply_signals_error_on_nonzero_exit() -> None:
    cfg = ReviewConfig(reviewer_command=["claude", "-p"])
    with patch("bubo.outcome_classifier.run_bounded", return_value=_completed("", returncode=1)):
        out = oc.classify_developer_reply(cfg, "finding", "some reply")
    assert out["verdict"] == "error"
