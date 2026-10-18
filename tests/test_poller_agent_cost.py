"""The worker records what each agent run cost, from the agent's own output."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bubo import db, paths, poller
from bubo.review_config import ReviewConfig
from bubo.telemetry import TokenUsage
from tests.test_agent_output import CLAUDE_JSON, CODEX_JSONL


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    paths.DB.parent.mkdir(parents=True)
    db.init_db()


def _review(tmp: Path, cfg: ReviewConfig, stdout: str) -> tuple[MagicMock, MagicMock, MagicMock]:
    job = tmp / "job.json"
    job.write_text(json.dumps({"project": "o/r", "mr": {"iid": 1, "sha": "b" * 40}}))
    (tmp / "prompt").write_text("rules")
    provider = MagicMock()
    provider.token.return_value = "token"
    provider.review_prompt.return_value = "prompt"
    provider.changed_lines.return_value = {}
    telemetry = MagicMock()
    telemetry.span.return_value.__enter__.return_value = MagicMock()
    agent = MagicMock(return_value=SimpleNamespace(stdout=stdout, returncode=0))
    post = MagicMock(return_value=(0, 0, 0))
    finish = MagicMock()
    with (
        patch.object(poller.paths, "REPORTS", tmp / "reports"),
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.get_provider", return_value=provider),
        patch("bubo.poller.ReviewTelemetry.from_config", return_value=telemetry),
        patch("bubo.poller.write_rendered_meta_prompt", return_value=tmp / "prompt"),
        patch("bubo.poller.capture_provenance", return_value=None),
        patch("bubo.poller.count_added_lines", return_value=0),
        patch("bubo.poller.run", agent),
        patch("bubo.poller.post_or_plan_findings", post),
        patch("bubo.poller.record_review_run_finish", finish),
        patch("bubo.poller.post_no_findings_comment", return_value=("disabled", "test")),
        patch("bubo.poller.cleanup_worktree"),
        patch("bubo.poller.analytics.flush"),
        patch("bubo.poller.analytics.record_review_completed"),
    ):
        assert poller.worker(job) == 0
    return agent, post, finish


def test_claude_review_records_the_cost_claude_reports(tmp_path, temp_db) -> None:
    cfg = ReviewConfig(dry_run=True, reviewer_command=["claude", "-p"])
    agent, post, finish = _review(tmp_path, cfg, CLAUDE_JSON)

    assert agent.call_args.args[0][:4] == ["claude", "-p", "--output-format", "json"]
    assert post.call_args.kwargs["raw_review"] == "[]"
    assert finish.call_args.kwargs["cost_usd"] == 0.0492628
    assert finish.call_args.kwargs["tokens"] == TokenUsage(
        input=16712, output=4, cached=10834, total=16716
    )


def test_codex_review_is_priced_from_the_live_price_list(tmp_path, temp_db, monkeypatch) -> None:
    from bubo.telemetry import pricing

    prices = {
        "gpt-6.1-sol": {
            "input_cost_per_token": 2e-06,
            "cache_read_input_token_cost": 1e-07,
            "output_cost_per_token": 1e-05,
        }
    }
    monkeypatch.setattr(pricing, "_fetch", lambda: json.dumps(prices).encode())
    cfg = ReviewConfig(dry_run=True, model="gpt-6.1-sol")
    agent, post, finish = _review(tmp_path, cfg, CODEX_JSONL)

    assert "--json" in agent.call_args.args[0]
    assert post.call_args.kwargs["raw_review"] == "[]"
    # 21,874 uncached x $2 + 49,280 cached x $0.10 + 220 output x $10, per 1M tokens
    assert finish.call_args.kwargs["cost_usd"] == pytest.approx(0.050876)


def _codex_jsonl(text: str) -> str:
    return "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1000,
                        "cached_input_tokens": 400,
                        "output_tokens": 50,
                    },
                }
            ),
        ]
    )


@pytest.fixture
def live_prices(monkeypatch):
    from bubo.telemetry import pricing

    prices = {"m": {"input_cost_per_token": 1e-06, "output_cost_per_token": 1e-05}}
    monkeypatch.setattr(pricing, "_fetch", lambda: json.dumps(prices).encode())


# 600 uncached x $1 + 400 cached x $1 (no cache price listed) + 50 x $10, per 1M tokens
ONE_CALL = 0.0015


def test_each_verification_lens_is_metered_and_still_parsed(tmp_path, temp_db, live_prices):
    from bubo.telemetry import start_metering, stop_metering

    verdict = json.dumps({"real": True, "confidence": 0.9, "reason": "r"})
    agent = MagicMock(return_value=SimpleNamespace(stdout=_codex_jsonl(verdict), returncode=0))
    cfg = ReviewConfig(model="m", verify_lenses=["correctness", "in_diff", "reproduce"])
    meter, token = start_metering()
    try:
        with patch("bubo.poller.run", agent):
            verdicts = poller.run_verification({"title": "t"}, tmp_path, cfg)
    finally:
        stop_metering(token)

    assert [v.ok and v.real for v in verdicts] == [True, True, True]
    assert all("--json" in call.args[0] for call in agent.call_args_list)
    assert meter.tokens == TokenUsage(input=3000, output=150, cached=1200, total=3150)
    assert meter.cost_usd == pytest.approx(3 * ONE_CALL)


def test_reconciliation_verifier_call_is_metered(tmp_path, temp_db, live_prices):
    from bubo.telemetry import start_metering, stop_metering

    answer = json.dumps({"verdict": "still_valid", "confidence": 0.9, "reason": "r"})
    agent = MagicMock(return_value=SimpleNamespace(stdout=_codex_jsonl(answer), returncode=0))
    meter, token = start_metering()
    try:
        with patch("bubo.poller.run", agent):
            result = poller.run_reconciliation_verification(
                {"title": "t"},
                original_sha="a",
                head_sha="b",
                repo=tmp_path,
                cfg=ReviewConfig(model="m"),
            )
    finally:
        stop_metering(token)

    assert result.verdict.value == "still_valid"
    assert meter.cost_usd == pytest.approx(ONE_CALL)


def test_reply_classifier_call_is_metered(live_prices):
    from bubo.outcome_classifier import classify_developer_reply
    from bubo.telemetry import start_metering, stop_metering

    answer = json.dumps({"verdict": "rejected", "false_positive": True})
    completed = SimpleNamespace(stdout=_codex_jsonl(answer), stderr="", returncode=0)
    meter, token = start_metering()
    try:
        with patch("bubo.outcome_classifier.run_bounded", return_value=completed) as agent:
            verdict = classify_developer_reply(ReviewConfig(model="m"), "finding", "no")
    finally:
        stop_metering(token)

    assert verdict == {"verdict": "rejected", "false_positive": True}
    assert "--json" in agent.call_args.args[0]
    assert meter.cost_usd == pytest.approx(ONE_CALL)


def test_later_agent_calls_are_added_to_a_finished_run(temp_db) -> None:
    from bubo.db import (
        add_review_run_usage,
        connect_db,
        record_review_run_finish,
        record_review_run_start,
    )
    from bubo.statuses import ReviewMode, ReviewStatus

    record_review_run_start(
        run_id="r1",
        project="o/r",
        iid=1,
        sha="s",
        model="m",
        prompt_version="v",
        review_mode=ReviewMode.DIFF,
        dry_run=False,
        tone="balanced",
    )
    record_review_run_finish(
        run_id="r1",
        status=ReviewStatus.SUCCESS,
        tokens=TokenUsage(input=10, output=2, cached=4, total=12),
        cost_usd=0.5,
        error=None,
    )
    add_review_run_usage(
        "r1", tokens=TokenUsage(input=5, output=1, cached=0, total=6), cost_usd=0.25
    )
    add_review_run_usage("missing", tokens=TokenUsage(input=1, total=1), cost_usd=1.0)

    with connect_db() as db:
        row = db.execute(
            "select tokens_input, tokens_output, tokens_cached, tokens_total, cost_usd "
            "from review_runs"
        ).fetchall()
    assert row == [(15, 3, 4, 18, 0.75)]
