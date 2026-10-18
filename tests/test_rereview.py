"""What happens when a PR gets new commits after Bubo reviewed it (#254).

These drive the real ``poll()``, ``worker()`` and ``post_or_plan_findings()``
with the real ``GitHubProvider``. Only the edges are faked: the GitHub REST
functions in :mod:`bubo.github`, the forked worker process, and the agent
subprocess (``poller.run``). The incremental-review tests also run real
``git`` against a local repository through the provider's mirror checkout.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bubo import db, github, paths, poller
from bubo.review_config import ReviewConfig
from bubo.statuses import ReviewStatus
from bubo.telemetry.posthog import safe_context

PROJECT = "o/r"
CLONE_URL = "https://github.com/o/r.git"
S1, S2, S3 = "1" * 40, "2" * 40, "3" * 40
REPO_ROOT = Path(__file__).resolve().parents[1]


def _patch_for(lines: list[str]) -> str:
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines)


BASE_LINES = [f"line{i}" for i in range(1, 11)]
# S2/S3 insert five lines at the top: S1's line 7 is now line 12.
SHIFTED_LINES = [f"new{i}" for i in range(1, 6)] + BASE_LINES


class FakeGitHub:
    """In-memory GitHub REST edge: one open PR whose head can move."""

    def __init__(self) -> None:
        self.head = S1
        self.number = 1
        self.extra_prs: list[dict] = []
        self.patches: dict[str, str] = {
            S1: _patch_for(BASE_LINES),
            S2: _patch_for(SHIFTED_LINES),
            S3: _patch_for(SHIFTED_LINES),
        }
        self.comments: list[dict] = []
        self.issue_comments: list[str] = []

    def pr(self, number: int | None = None) -> dict:
        return {
            "number": number or self.number,
            "state": "open",
            "title": "t",
            "html_url": "https://github.com/o/r/pull/1",
            "head": {"sha": self.head, "ref": "feature"},
            "base": {"ref": "main"},
        }

    # -- bubo.github functions -------------------------------------------
    def open_prs(self, cfg, project, token):
        return [self.pr(), *self.extra_prs]

    def get_pr(self, cfg, token, project, number):
        return self.pr(number)

    def get_pr_files(self, cfg, token, project, number):
        return [{"filename": "a.py", "patch": self.patches.get(self.head, "")}]

    def get_pr_commits(self, cfg, token, project, number):
        return []

    def find_review_comment_by_body(self, cfg, token, project, number, body):
        return next((str(c["id"]) for c in self.comments if c["body"] == body), "")

    def create_pr_review_comment(self, cfg, token, project, number, body, position):
        comment = {"id": len(self.comments) + 1, "body": body, "resolved": False, **position}
        self.comments.append(comment)
        return {"id": comment["id"]}

    def get_pr_review_threads(self, cfg, token, project, number):
        return [
            {
                "is_resolved": c["resolved"],
                "comments": [{"database_id": c["id"], "login": "bubo", "body": c["body"]}],
            }
            for c in self.comments
        ]

    def find_issue_comment_by_body(self, cfg, token, project, number, body, bot_username=""):
        return ""

    def create_issue_comment(self, cfg, token, project, number, body):
        self.issue_comments.append(body)
        return {"id": len(self.issue_comments)}


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


def use_config(monkeypatch: pytest.MonkeyPatch, **overrides) -> ReviewConfig:
    cfg = ReviewConfig(provider="github", projects=[PROJECT], dry_run=False, **overrides)
    monkeypatch.setattr(poller, "read_config", lambda: cfg)
    return cfg


def status_of(sha: str, number: int = 1) -> str | None:
    with sqlite3.connect(paths.DB) as conn:
        row = conn.execute(
            "select status from reviewed_mrs where project=? and iid=? and sha=?",
            (PROJECT, number, sha),
        ).fetchone()
    return row[0] if row else None


def finding(line: int, comment: str = "", *, title: str = "xs[i+1] reads past the end") -> dict:
    item = {
        "type": "issue",
        "severity": "blocking",
        "category": "correctness",
        "title": title,
        "file": "a.py",
        "line": line,
        "impact": "IndexError on the last element.",
        "evidence": "loop runs to len(xs)",
        "fix": "Iterate to len(xs) - 1.",
        "confidence": 0.95,
    }
    if comment:
        item["comment"] = comment
    return item


# ---------------------------------------------------------------------------
# B. One review per PR at a time
# ---------------------------------------------------------------------------


def test_poll_during_running_review_starts_no_second_worker(monkeypatch, fake, forked) -> None:
    use_config(monkeypatch)
    poller.poll()
    assert forked == [S1]
    poller.record(PROJECT, 1, S1, ReviewStatus.RUNNING)

    fake.head = S2
    poller.poll()

    assert forked == [S1]
    # The new head is left unseen, so a later poll picks it up.
    assert status_of(S2) is None
    poller.record(PROJECT, 1, S1, ReviewStatus.SUCCESS)
    poller.poll()
    assert forked == [S1, S2]


def test_queued_review_of_same_pr_also_blocks(monkeypatch, fake, forked) -> None:
    use_config(monkeypatch)
    poller.poll()
    assert status_of(S1) == ReviewStatus.QUEUED
    fake.head = S2
    poller.poll()
    assert forked == [S1]


def test_stale_running_row_does_not_block_pr_forever(monkeypatch, fake, forked) -> None:
    cfg = use_config(monkeypatch)
    poller.record(PROJECT, 1, S1, ReviewStatus.RUNNING)
    stale = (datetime.now(UTC) - timedelta(seconds=cfg.timeout_seconds * 3)).isoformat()
    with sqlite3.connect(paths.DB) as conn:
        conn.execute("update reviewed_mrs set updated_at=? where sha=?", (stale, S1))
    fake.head = S2
    poller.poll()
    assert forked == [S2]


def _run_worker(monkeypatch, fake: FakeGitHub, stdout: str, *, push_to: str | None = None) -> int:
    """Run the real worker for the current head; optionally push mid-review."""

    def checkout(self, cfg, project, change, dest):
        dest.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("bubo.scm.github.GitHubProvider.checkout", checkout)
    prompts: list[str] = []

    def agent(argv, *, cwd, timeout, env):
        prompts.append(argv[-1])
        if push_to:
            fake.head = push_to
        return SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(poller, "run", agent)
    job = poller.write_job(PROJECT, fake.pr())
    code = poller.worker(job)
    fake.prompts = prompts  # type: ignore[attr-defined]
    return code


def test_push_during_review_marks_superseded_and_posts_nothing(monkeypatch, fake) -> None:
    use_config(monkeypatch)
    raw = json.dumps([finding(7)])

    assert _run_worker(monkeypatch, fake, raw, push_to=S2) == 0

    assert status_of(S1) == ReviewStatus.SUPERSEDED
    assert fake.comments == []
    assert fake.issue_comments == []
    with sqlite3.connect(paths.DB) as conn:
        run_status = conn.execute("select status from review_runs where sha=?", (S1,)).fetchone()
    assert run_status == (ReviewStatus.SUPERSEDED,)


def test_push_during_clean_review_posts_no_acknowledgement(monkeypatch, fake) -> None:
    use_config(monkeypatch)
    assert _run_worker(monkeypatch, fake, "[]", push_to=S2) == 0
    assert status_of(S1) == ReviewStatus.SUPERSEDED
    assert fake.issue_comments == []


def test_superseded_head_is_reviewed_by_next_poll(monkeypatch, fake, forked) -> None:
    use_config(monkeypatch)
    _run_worker(monkeypatch, fake, json.dumps([finding(7)]), push_to=S2)
    poller.poll()
    assert forked == [S2]


def test_never_posts_old_line_numbers_on_a_new_head(monkeypatch, fake) -> None:
    cfg = use_config(monkeypatch)
    fake.head = S2  # head moved after the agent reviewed S1
    with pytest.raises(poller.ReviewSuperseded):
        poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project=PROJECT,
            mr={"number": 1, "head": {"sha": S1}},
            raw_review=json.dumps([finding(7)]),
        )
    assert fake.comments == []


def test_unchanged_head_still_posts(monkeypatch, fake) -> None:
    use_config(monkeypatch)
    assert _run_worker(monkeypatch, fake, json.dumps([finding(7)])) == 0
    assert status_of(S1) == ReviewStatus.SUCCESS
    assert [(c["commit_id"], c["line"]) for c in fake.comments] == [(S1, 7)]


def test_superseded_status_is_a_known_reporting_dimension() -> None:
    assert safe_context({"status": "superseded"}).get("status") == "superseded"
