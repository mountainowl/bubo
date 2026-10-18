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
