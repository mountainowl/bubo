"""Per-project ``[[projects]] auto_review`` switch.

An onboarded repository is written with ``auto_review = false``: it stays a
known, listed project, but Bubo never starts a review, never runs an agent and
never posts anything for it until the switch is turned on. A missing key means
``true``, so existing configs keep today's behaviour.

The poll tests drive the real ``poll()`` and ``worker()`` with the real
``GitHubProvider``; only the GitHub REST edge (:class:`test_rereview.FakeGitHub`)
and the forked worker are faked; any comment Bubo posted would land in the fake.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from test_outcome_sync import _FakeProvider, _seed_posted_finding, _SilentTelemetry
from test_rereview import PROJECT, REPO_ROOT, S1, FakeGitHub, status_of

from bubo import db, github, paths, poller
from bubo.config_values import ConfigError
from bubo.review_config import ReviewConfig, review_config_from_dict
from bubo.statuses import ReviewStatus


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    gh = FakeGitHub()
    for name in (
        "open_prs",
        "get_pr",
        "get_pr_files",
        "get_pr_commits",
        "find_review_comment_by_body",
        "create_pr_review_comment",
        "get_pr_review_threads",
        "find_issue_comment_by_body",
        "create_issue_comment",
    ):
        monkeypatch.setattr(github, name, getattr(gh, name))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")
    monkeypatch.setenv("BUBO_PROMPT_SOURCE", str(REPO_ROOT / "prompts" / "00-meta.md"))
    db.init_db()
    return gh


@pytest.fixture
def forked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    def fork(job: Path) -> int:
        started.append(json.loads(job.read_text())["mr"]["head"]["sha"])
        return 0

    monkeypatch.setattr(poller, "fork_worker", fork)
    return started


def parse(*projects: dict[str, object]) -> ReviewConfig:
    return review_config_from_dict(
        {
            "scm": {"provider": "github"},
            "review": {"dry_run": False},
            "analytics": {"enabled": False},
            "projects": list(projects),
        }
    )


def use_parsed(monkeypatch: pytest.MonkeyPatch, *projects: dict[str, object]) -> ReviewConfig:
    cfg = parse(*projects)
    monkeypatch.setattr(poller, "read_config", lambda: cfg)
    return cfg


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_auto_review_defaults_to_true_when_missing() -> None:
    cfg = parse({"path": "g/a"}, {"path": "g/b", "enabled": True})
    assert cfg.projects == ["g/a", "g/b"]
    assert cfg.project_auto_review == {"g/a": True, "g/b": True}
    assert cfg.auto_review_enabled("g/a") is True
    assert cfg.auto_review_projects() == ["g/a", "g/b"]


def test_auto_review_explicit_true() -> None:
    cfg = parse({"path": "g/a", "auto_review": True})
    assert cfg.project_auto_review == {"g/a": True}
    assert cfg.auto_review_enabled("g/a") is True


def test_auto_review_false_keeps_project_listed() -> None:
    cfg = parse({"path": "g/a", "auto_review": False}, {"path": "g/b"})
    # Still a known project (Enterprise lists it), but not auto-reviewed.
    assert cfg.projects == ["g/a", "g/b"]
    assert cfg.project_auto_review == {"g/a": False, "g/b": True}
    assert cfg.auto_review_enabled("g/a") is False
    assert cfg.auto_review_projects() == ["g/b"]


def test_disabled_project_is_not_listed_whatever_auto_review_says() -> None:
    cfg = parse({"path": "g/a", "enabled": False, "auto_review": True}, {"path": "g/b"})
    assert cfg.projects == ["g/b"]
    assert cfg.project_auto_review == {"g/b": True}


@pytest.mark.parametrize("value", ["false", "true", 0, 1, 1.5, [], {}])
def test_auto_review_rejects_non_bool(value: object) -> None:
    with pytest.raises(ConfigError, match="auto_review"):
        parse({"path": "g/a", "auto_review": value})


def test_duplicate_entry_with_auto_review_off_wins() -> None:
    cfg = parse({"path": "g/a"}, {"path": "g/a", "auto_review": False})
    assert cfg.auto_review_enabled("g/a") is False


def test_unlisted_project_and_bare_config_keep_auto_review_on() -> None:
    assert ReviewConfig().auto_review_enabled("any/repo") is True
    assert ReviewConfig(projects=["g/a"]).auto_review_projects() == ["g/a"]


def test_shipped_example_projects_keep_auto_review_on() -> None:
    from bubo.review_config import load_review_config

    root = Path(__file__).resolve().parents[1]
    cfg = load_review_config(root / "config" / "env.example.toml")
    assert cfg.project_auto_review == {"example/enabled-repo": True}


# ---------------------------------------------------------------------------
# Poller: no automatic review of an auto_review = false project
# ---------------------------------------------------------------------------


def test_poll_does_not_queue_auto_review_off_project(monkeypatch, fake, forked) -> None:
    use_parsed(monkeypatch, {"path": PROJECT, "auto_review": False})
    listed: list[str] = []
    real_open_prs = fake.open_prs

    def open_prs(cfg, project, token):
        listed.append(project)
        return real_open_prs(cfg, project, token)

    monkeypatch.setattr(poller.github, "open_prs", open_prs)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(poller, "log", lambda event, **fields: events.append((event, fields)))

    assert poller.poll() == 0
    assert forked == []
    assert status_of(S1) is None
    assert fake.comments == []
    assert fake.issue_comments == []
    # Skipped before any SCM call for the project.
    assert listed == []
    skips = [fields for event, fields in events if event == "poll_project_skipped"]
    assert skips
    assert skips[0]["project"] == PROJECT
    assert skips[0]["reason"] == "auto_review_off"


def test_poll_inline_run_once_does_not_review_auto_review_off(monkeypatch, fake) -> None:
    use_parsed(monkeypatch, {"path": PROJECT, "auto_review": False})
    monkeypatch.setattr(poller, "worker", lambda job: pytest.fail("worker must not run"))
    assert poller.run_once() == 0
    assert status_of(S1) is None


@pytest.mark.parametrize("entry", [{"path": PROJECT}, {"path": PROJECT, "auto_review": True}])
def test_poll_reviews_auto_review_on_project(monkeypatch, fake, forked, entry) -> None:
    use_parsed(monkeypatch, entry)
    assert poller.poll() == 1
    assert forked == [S1]
    assert status_of(S1) == ReviewStatus.QUEUED


def test_switching_auto_review_on_reviews_on_next_poll(monkeypatch, fake, forked) -> None:
    use_parsed(monkeypatch, {"path": PROJECT, "auto_review": False})
    poller.poll()
    assert forked == []
    use_parsed(monkeypatch, {"path": PROJECT, "auto_review": True})
    poller.poll()
    assert forked == [S1]


def test_worker_defers_job_queued_before_auto_review_turned_off(monkeypatch, fake) -> None:
    use_parsed(monkeypatch, {"path": PROJECT})
    poller.record(PROJECT, 1, S1, ReviewStatus.QUEUED)
    job = poller.write_job(PROJECT, fake.pr())
    use_parsed(monkeypatch, {"path": PROJECT, "auto_review": False})

    def no_agent(*_args, **_kwargs):
        pytest.fail("agent must not run for an auto_review = false project")

    monkeypatch.setattr(poller, "run", no_agent)
    assert poller.worker(job) == 0
    assert status_of(S1) == ReviewStatus.DEFERRED
    assert fake.comments == []
    assert fake.issue_comments == []
    assert json.loads(job.read_text())["project"] == PROJECT


# ---------------------------------------------------------------------------
# Outcome sync: no LLM reply classification for an auto_review = false project
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_state(tmp_path: Path) -> Iterator[None]:
    old = paths.DB
    paths.DB = tmp_path / "reviewer.sqlite"
    poller.init_db()
    _seed_posted_finding(paths.DB)
    try:
        yield
    finally:
        paths.DB = old


@pytest.mark.parametrize(("auto_review", "classified"), [(True, 1), (False, 0)])
def test_outcome_sync_skips_llm_classifier_when_auto_review_off(
    outcome_state: None, auto_review: bool, classified: int
) -> None:
    telemetry = _SilentTelemetry()
    cfg = ReviewConfig(
        telemetry_config=telemetry.config,
        projects=["group/repo"],
        project_auto_review={"group/repo": auto_review},
    )
    outcome = {
        "resolved": False,
        "deleted": False,
        "developer_replied": True,
        "disputed": False,
        "false_positive": False,
        "duplicate": False,
        "resolved_at": None,
        "merged_unresolved": False,
        "_finding_text": "finding",
        "_reply_text": "I am not sure about this.",
    }
    calls: list[str] = []

    def classify(_cfg: object, _finding: str, text: str) -> dict[str, object]:
        calls.append(text)
        return {"verdict": "accepted", "false_positive": False}

    with (
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.get_provider", return_value=_FakeProvider(outcome=outcome)),
        patch("bubo.poller.ReviewTelemetry.from_config", return_value=telemetry),
        patch("bubo.poller.classify_developer_reply", side_effect=classify),
    ):
        assert poller.sync_outcomes(limit=1) == 1
    assert len(calls) == classified
    with sqlite3.connect(paths.DB) as conn:
        [row] = conn.execute("select reply_classified from finding_outcomes").fetchall()
    assert row[0] == classified
