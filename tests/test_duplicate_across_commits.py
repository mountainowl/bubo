"""A still-present finding is not re-posted on every new commit (#135).

Runs the real worker and GitHub provider twice on one pull request; only the
HTTP boundary, the checkout, the reviewer agent and the live thread state are
faked. The reviewer rewords its finding between runs, as models do.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from review_output import review_output

from bubo import db, paths, poller
from bubo.review_config import ReviewConfig
from bubo.scm.base import FindingThread, FindingThreadState
from bubo.scm.github import GitHubProvider

PATCH = "@@ -0,0 +1,3 @@\n+a\n+b\n+c"
SECRET = {
    "file": "gradle.properties",
    "line": 2,
    "body": "token in VCS",
    "severity": "blocking",
    "category": "security",
    "confidence": 0.97,
    "kind": "bug",
    "fix": "rotate it",
}


@pytest.fixture
def posted(tmp_path: Path) -> Iterator[list[dict[str, Any]]]:
    old = paths.DB
    paths.DB = tmp_path / "state" / "reviewer.sqlite"
    paths.DB.parent.mkdir()
    db.init_db()
    try:
        yield []
    finally:
        paths.DB = old


def _review(
    tmp: Path,
    posted: list[dict[str, Any]],
    sha: str,
    findings: list[dict[str, Any]],
    thread: FindingThreadState = FindingThreadState.OPEN,
) -> None:
    def http(url: str, *, method: str, body: Any = None, **_: Any) -> tuple[Any, dict[str, str]]:
        path = urlparse(url).path
        if path.endswith("/pulls/1/files"):
            return [{"filename": "gradle.properties", "patch": PATCH}], {}
        if path.endswith("/pulls/1/comments"):
            if method == "POST":
                posted.append(body)
                return {"id": len(posted), "node_id": f"N{len(posted)}"}, {}
            # Like the real API: every comment already on the pull request.
            return [
                {"id": i + 1, "body": c["body"], "user": {"login": "bubo"}}
                for i, c in enumerate(posted)
            ], {}
        if path.endswith("/pulls/1"):
            return {"number": 1, "state": "open", "head": {"sha": sha}}, {}
        return ([] if method == "GET" else {}), {}

    prompt = tmp / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    job = tmp / f"job-{sha[:4]}.json"
    job.write_text(json.dumps({"project": "o/r", "mr": {"number": 1, "head": {"sha": sha}}}))
    with (
        patch("bubo._http.request_json", side_effect=http),
        patch.object(GitHubProvider, "token", return_value="tok"),
        patch.object(GitHubProvider, "bot_username", return_value="bubo"),
        patch.object(GitHubProvider, "checkout", return_value=None),
        patch.object(GitHubProvider, "finding_thread", return_value=FindingThread(thread)),
        patch.object(poller.paths, "REPORTS", tmp / "reports"),
        patch.object(poller.paths, "WORK", tmp / "work"),
        patch(
            "bubo.poller.read_config", return_value=ReviewConfig(provider="github", dry_run=False)
        ),
        patch("bubo.poller.write_rendered_meta_prompt", return_value=prompt),
        patch(
            "bubo.poller.run",
            return_value=SimpleNamespace(stdout=review_output(findings), stderr="", returncode=0),
        ),
        patch("bubo.poller.cleanup_worktree"),
        patch("bubo.poller.analytics.flush"),
        patch("bubo.project_analytics.collect_profile", return_value={}),
    ):
        assert poller.worker(job) == 0


def _titles(posted: list[dict[str, Any]]) -> list[str]:
    return [p["body"].splitlines()[0] for p in posted]


def test_reworded_finding_on_a_new_commit_is_not_reposted(
    tmp_path: Path, posted: list[dict[str, Any]]
) -> None:
    _review(tmp_path, posted, "a" * 40, [{**SECRET, "title": "private token is committed"}])
    _review(tmp_path, posted, "b" * 40, [{**SECRET, "title": "GitLab token is committed"}])
    assert _titles(posted) == ["**Issue (blocking, security):** private token is committed"]


def test_resolved_thread_does_not_block_a_new_post(
    tmp_path: Path, posted: list[dict[str, Any]]
) -> None:
    _review(tmp_path, posted, "a" * 40, [{**SECRET, "title": "private token is committed"}])
    _review(
        tmp_path,
        posted,
        "b" * 40,
        [{**SECRET, "title": "GitLab token is committed"}],
        thread=FindingThreadState.RESOLVED,
    )
    assert len(posted) == 2


def test_a_different_finding_at_the_same_line_still_posts(
    tmp_path: Path, posted: list[dict[str, Any]]
) -> None:
    _review(tmp_path, posted, "a" * 40, [{**SECRET, "title": "private token is committed"}])
    other = {**SECRET, "title": "debug flag left on", "category": "correctness"}
    _review(tmp_path, posted, "b" * 40, [{**SECRET, "title": "GitLab token is committed"}, other])
    assert _titles(posted) == [
        "**Issue (blocking, security):** private token is committed",
        "**Issue (blocking, correctness):** debug flag left on",
    ]


def test_two_findings_on_one_line_in_one_review_both_post(
    tmp_path: Path, posted: list[dict[str, Any]]
) -> None:
    first = {**SECRET, "title": "private token is committed"}
    second = {**SECRET, "title": "token also logged", "body": "printed at startup"}
    _review(tmp_path, posted, "a" * 40, [first, second])
    assert len(posted) == 2
