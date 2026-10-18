"""The reviewer agent receives Bubo's review rules inline, rendered for this run.

Earlier prompts only said "Use the `code-reviewer` skill through Superpowers";
neither the plugin nor (on Claude) the skill ever loaded, so the rules in
``prompts/00-meta.md`` never reached the agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bubo import db, poller
from bubo.analytics_config import AnalyticsConfig
from bubo.review_config import ReviewConfig
from bubo.scm.github import GitHubProvider
from bubo.telemetry import TelemetryConfig
from tests.review_output import review_output

META_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "00-meta.md"


def test_worker_passes_rendered_meta_prompt_to_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUBO_PROMPT_SOURCE", str(META_PROMPT))
    db.init_dirs()
    db.init_db()
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"project": "o/r", "mr": {"iid": 1, "sha": "b" * 40}}))
    change = {
        "html_url": "https://github.com/o/r/pull/1",
        "number": 1,
        "title": "t",
        "head": {"ref": "f", "sha": "b" * 40},
        "base": {"ref": "main"},
    }
    provider = MagicMock()
    provider.token.return_value = "token"
    provider.get_change.return_value = change
    provider.changed_lines.return_value = {}
    provider.review_prompt.side_effect = GitHubProvider().review_prompt
    telemetry = MagicMock()
    telemetry.span.return_value.__enter__.return_value = MagicMock()
    agent = MagicMock(return_value=SimpleNamespace(stdout=review_output([]), returncode=0))
    cfg = ReviewConfig(
        provider="github",
        max_findings_per_merge_request=7,
        telemetry_config=TelemetryConfig(analytics=AnalyticsConfig(enabled=False)),
    )

    with (
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.get_provider", return_value=provider),
        patch("bubo.poller.ReviewTelemetry.from_config", return_value=telemetry),
        patch("bubo.poller.capture_provenance", return_value=None),
        patch("bubo.poller.run", agent),
        patch("bubo.poller.post_no_findings_comment", return_value=("disabled", "test")),
        patch("bubo.poller.cleanup_worktree"),
        patch("bubo.poller.analytics.flush"),
    ):
        assert poller.worker(job) == 0

    prompt = agent.call_args.args[0][-1]
    # The hardened runner loads no skills or plugins, so the rules must be inline.
    assert "Superpowers" not in prompt
    assert "skill" not in prompt.split("# Bubo review contract")[0]
    assert "# Bubo review contract" in prompt
    assert "Return at most 7 findings" in prompt
    assert "{{MAX_FINDINGS_PER_REVIEW}}" not in prompt


def test_reviewer_skill_no_findings_instruction_matches_the_contract() -> None:
    """The interactive skill once said to "return exactly" a sentence fragment
    (`a schema-version-1 result ...`), which is neither JSON nor the contract."""
    skill = Path(__file__).resolve().parents[1] / "skills" / "code-reviewer" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "return exactly" not in text
    assert "a schema-version-1 result with explicit status and coverage" not in text
    assert "empty `findings` array" in text
def test_contract_does_not_turn_standing_review_conditions_into_incomplete() -> None:
    """Every review is read-only and executes nothing. When the contract let models list
    that as a limitation, live Haiku/Sonnet/Opus runs marked 26 of 45 reviews incomplete
    (or "complete" with limitations, which fails parsing), and the worker records both as
    FAILED."""
    change = {"number": 1, "head": {"ref": "f", "sha": "b" * 40}, "base": {"ref": "main"}}
    prompt = GitHubProvider().review_prompt("o/r", change, ReviewConfig(provider="github"))
    assert "these are not limitations" in prompt
    assert "they are not limitations" in prompt
    assert "Questions,\nunreviewed scope, or limitations require incomplete" not in prompt
