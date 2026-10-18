"""SCM API call budget for the worker and outcome sync (issue #255).

These tests run the real ``worker()`` / ``sync_outcomes()`` and the real
GitHub/GitLab providers. Only the HTTP boundary (``bubo._http.request_json``),
the git checkout and the reviewer agent subprocess are faked, so each counted
request is one real SCM round-trip.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from review_output import review_output

from bubo import db, paths, poller
from bubo.analytics_config import AnalyticsConfig
from bubo.governance_config import GovernanceConfig
from bubo.review_config import ReviewConfig
from bubo.scm.github import GitHubProvider
from bubo.scm.gitlab import GitLabProvider

SHA = "b" * 40
PATCH = "@@ -0,0 +1,3 @@\n+a\n+b\n+c"


@contextmanager
def _temp_db() -> Iterator[Path]:
    old = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            yield Path(tmp)
    finally:
        paths.DB = old


class _FakeScmHttp:
    """Records every SCM HTTP request and answers it from a router."""

    def __init__(self, route: Callable[[str, str, Any], Any]) -> None:
        self.route = route
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(
        self, url: str, *, method: str, headers: dict[str, str], body: Any = None, **_: Any
    ) -> tuple[Any, dict[str, str]]:
        self.calls.append((method, url, body))
        return self.route(method, url, body), {}

    def count(self, predicate: Callable[[str, str, Any], bool]) -> int:
        return sum(1 for call in self.calls if predicate(*call))


# --- worker(): one pre-review diff fetch ------------------------------------


def _is_files(method: str, url: str, _body: Any) -> bool:
    return method == "GET" and urlparse(url).path.endswith("/pulls/1/files")


def _github_worker_route(fail_first_files: list[bool] | None = None):
    def route(method: str, url: str, body: Any) -> Any:
        path = urlparse(url).path
        if path.endswith("/pulls/1/files"):
            if fail_first_files:
                fail_first_files.pop()
                raise RuntimeError("diff endpoint down")
            return [{"filename": "a.py", "patch": PATCH}]
        if path.endswith("/pulls/1/commits"):
            return [{"sha": "c1", "commit": {"message": "feat: x", "author": {"name": "J"}}}]
        if path.endswith("/pulls/1"):
            return {"number": 1, "state": "open", "head": {"sha": SHA}}
        return [] if method == "GET" else {}

    return route


def _run_worker(
    tmp: Path, cfg: ReviewConfig, http: _FakeScmHttp
) -> tuple[int, list[str], MagicMock]:
    events: list[str] = []
    finding = {
        "file": "a.py",
        "line": 2,
        "title": "t",
        "body": "b",
        "severity": "blocking",
        "category": "correctness",
        "confidence": 0.95,
        "kind": "bug",
        "fix": "f",
    }
    real_http = http.__call__

    def recording_http(url: str, **kwargs: Any) -> tuple[Any, dict[str, str]]:
        if _is_files(kwargs["method"], url, None):
            events.append("changed_lines")
        return real_http(url, **kwargs)

    def agent(*_a: Any, **_k: Any) -> SimpleNamespace:
        events.append("agent")
        return SimpleNamespace(stdout=review_output([finding]), stderr="", returncode=0)

    completed = MagicMock()
    job = tmp / "job.json"
    job.write_text(json.dumps({"project": "o/r", "mr": {"number": 1, "head": {"sha": SHA}}}))
    with (
        patch("bubo._http.request_json", side_effect=recording_http),
        patch.object(GitHubProvider, "token", return_value="tok"),
        patch.object(GitHubProvider, "checkout", return_value=None),
        patch.object(poller.paths, "REPORTS", tmp / "reports"),
        patch.object(poller.paths, "WORK", tmp / "work"),
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.write_rendered_meta_prompt", return_value=tmp / "prompt"),
        patch("bubo.poller.run", side_effect=agent),
        patch("bubo.poller.cleanup_worktree"),
        patch("bubo.poller.analytics.flush"),
        patch("bubo.poller.analytics.record_review_completed", completed),
        patch("bubo.project_analytics.collect_profile", return_value={}),
    ):
        rc = poller.worker(job)
    return rc, events, completed


def _all_consumers_cfg() -> ReviewConfig:
    return ReviewConfig(
        provider="github",
        dry_run=True,
        governance_config=GovernanceConfig(capture_provenance=True),
        analytics_config=AnalyticsConfig(enabled=True, endpoint="http://x", api_key="k"),
    )


def test_worker_fetches_diff_once_before_agent_and_once_at_posting() -> None:
    http = _FakeScmHttp(_github_worker_route())
    with _temp_db() as tmp:
        rc, events, completed = _run_worker(tmp, _all_consumers_cfg(), http)
        provenance = db.provenance_for(poller.review_run_id("o/r", 1, SHA))
        with sqlite3.connect(paths.DB) as conn:
            lines = conn.execute("select lines_reviewed from review_runs").fetchone()[0]
    assert rc == 0
    agent_at = events.index("agent")
    assert events[:agent_at].count("changed_lines") == 1, events
    assert events[agent_at:].count("changed_lines") == 1, events
    # Every consumer still got the diff: lines_reviewed, analytics LoC,
    # language mix and provenance.
    assert lines == 3
    kwargs = completed.call_args.kwargs
    assert (kwargs["files_changed"], kwargs["lines_changed"]) == (1, 3)
    assert kwargs["languages"] == {"python": 1}
    assert provenance is not None


def test_worker_opted_out_still_fetches_diff_once() -> None:
    http = _FakeScmHttp(_github_worker_route())
    cfg = ReviewConfig(provider="github", dry_run=True)
    with _temp_db() as tmp:
        rc, events, _ = _run_worker(tmp, cfg, http)
    assert rc == 0
    agent_at = events.index("agent")
    assert events[:agent_at].count("changed_lines") == 1
    # Governance off: no commit-list round-trip either.
    assert http.count(lambda m, u, b: urlparse(u).path.endswith("/commits")) == 0


def test_worker_diff_fetch_failure_soft_fails_all_consumers() -> None:
    http = _FakeScmHttp(_github_worker_route(fail_first_files=[True]))
    with _temp_db() as tmp:
        rc, events, completed = _run_worker(tmp, _all_consumers_cfg(), http)
        provenance = db.provenance_for(poller.review_run_id("o/r", 1, SHA))
        with sqlite3.connect(paths.DB) as conn:
            lines = conn.execute("select lines_reviewed from review_runs").fetchone()[0]
    assert rc == 0
    agent_at = events.index("agent")
    kwargs = completed.call_args.kwargs
    assert (kwargs["files_changed"], kwargs["lines_changed"]) == (None, None)
    # One failed attempt before the agent; no retry by analytics/governance.
    assert events[:agent_at].count("changed_lines") == 1
    assert lines == 0
    assert provenance is None
    # Posting still re-fetches the diff.
    assert events[agent_at:].count("changed_lines") == 1


# --- sync_outcomes(): one change + thread fetch per PR ----------------------

PRS, PER_PR = 3, 5


def _github_sync_route(method: str, url: str, body: Any) -> Any:
    path = urlparse(url).path
    if method == "POST" and path.endswith("/graphql"):
        number = body["variables"]["number"]
        nodes = []
        for i in range(PER_PR):
            comments = [
                {
                    "databaseId": int(f"{number}0{i}"),
                    "id": f"n{number}-{i}",
                    "author": {"login": "bubo", "__typename": "Bot"},
                    "body": "finding",
                }
            ]
            if i % 2:
                comments.append(
                    {
                        "databaseId": int(f"{number}9{i}"),
                        "id": f"r{number}-{i}",
                        "author": {"login": "dev", "__typename": "User"},
                        "body": "[llm-review:disputed] no",
                    }
                )
            nodes.append(
                {
                    "id": f"T{number}-{i}",
                    "isResolved": i == 0,
                    "comments": {"nodes": comments, "pageInfo": {"hasNextPage": False}},
                }
            )
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
                    }
                }
            }
        }
    number = int(path.rsplit("/", 1)[-1])
    return {"number": number, "state": "closed", "merged": number == 2, "head": {"sha": "s"}}


def _seed_findings(discussion_id: Callable[[int, int], str]) -> None:
    with sqlite3.connect(paths.DB) as conn:
        for n in range(1, PRS + 1):
            for i in range(PER_PR):
                conn.execute(
                    "insert into review_findings(project,iid,sha,fingerprint,file,line,status,"
                    "discussion_id,body,updated_at) values(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "o/r",
                        n,
                        "s",
                        f"fp{n}{i}",
                        "a.py",
                        1,
                        "posted",
                        discussion_id(n, i),
                        "b",
                        poller.now(),
                    ),
                )


def _outcome_rows() -> list[tuple[Any, ...]]:
    with sqlite3.connect(paths.DB) as conn:
        return conn.execute(
            "select finding_id,resolved,deleted,developer_replied,disputed,false_positive,"
            "duplicate,merged_unresolved,developer_disposition from finding_outcomes "
            "order by finding_id"
        ).fetchall()


def _expected_rows(merged: Callable[[int], bool]) -> list[tuple[Any, ...]]:
    rows = []
    for n in range(1, PRS + 1):
        for i in range(PER_PR):
            resolved = i == 0
            disputed = bool(i % 2)
            rows.append(
                (
                    f"o/r:{n}:s:fp{n}{i}",
                    int(resolved),
                    0,
                    int(disputed),
                    int(disputed),
                    0,
                    0,
                    int(merged(n) and not resolved),
                    "disagrees" if disputed else "unknown",
                )
            )
    return sorted(rows)


def test_github_sync_outcomes_fetches_pr_and_threads_once_per_pr() -> None:
    http = _FakeScmHttp(_github_sync_route)
    with _temp_db():
        _seed_findings(lambda n, i: f"{n}0{i}")
        with (
            patch("bubo._http.request_json", side_effect=http),
            patch.object(GitHubProvider, "token", return_value="tok"),
            patch.object(poller, "read_config", return_value=ReviewConfig(provider="github")),
            patch.object(poller, "classify_developer_reply", side_effect=AssertionError("no LLM")),
        ):
            synced = poller.sync_outcomes(limit=200)
        rows = _outcome_rows()
    assert synced == PRS * PER_PR
    get_pr = http.count(lambda m, u, b: m == "GET" and "/pulls/" in u)
    threads = http.count(lambda m, u, b: m == "POST" and u.endswith("/graphql"))
    assert (get_pr, threads) == (PRS, PRS)
    assert rows == _expected_rows(lambda n: n == 2)


def _gitlab_sync_route(method: str, url: str, body: Any) -> Any:
    path = urlparse(url).path
    if "/discussions/" in path:
        disc = path.rsplit("/", 1)[-1]
        i = int(disc.rsplit("-", 1)[-1])
        notes = [{"author": {"username": "bubo"}, "body": "finding"}]
        if i % 2:
            notes.append({"author": {"username": "dev"}, "body": "[llm-review:disputed] no"})
        return {"id": disc, "resolved": i == 0, "notes": notes}
    iid = int(path.rsplit("/", 1)[-1])
    return {"iid": iid, "state": "merged" if iid == 2 else "closed", "sha": "s"}


def test_gitlab_sync_outcomes_fetches_mr_once_per_mr() -> None:
    http = _FakeScmHttp(_gitlab_sync_route)
    with _temp_db():
        _seed_findings(lambda n, i: f"d{n}-{i}")
        with (
            patch("bubo._http.request_json", side_effect=http),
            patch.object(GitLabProvider, "token", return_value="tok"),
            patch.object(poller, "read_config", return_value=ReviewConfig(provider="gitlab")),
            patch.object(poller, "classify_developer_reply", side_effect=AssertionError("no LLM")),
        ):
            synced = poller.sync_outcomes(limit=200)
        rows = _outcome_rows()
    assert synced == PRS * PER_PR
    get_mr = http.count(lambda m, u, b: "/discussions/" not in u)
    discussions = http.count(lambda m, u, b: "/discussions/" in u)
    assert (get_mr, discussions) == (PRS, PRS * PER_PR)
    assert rows == _expected_rows(lambda n: n == 2)


# --- posted_findings_for_outcome_sync: deleted rows are terminal ------------


def test_deleted_finding_is_not_reselected_for_outcome_sync() -> None:
    with _temp_db():
        with sqlite3.connect(paths.DB) as conn:
            for fp, disc in (("gone", "d-gone"), ("live", "d-live")):
                conn.execute(
                    "insert into review_findings(project,iid,sha,fingerprint,file,line,status,"
                    "discussion_id,body,updated_at) values('o/r',1,'s',?,'a.py',1,'posted',?,"
                    "'b',?)",
                    (fp, disc, poller.now()),
                )
        db.record_finding_outcome(
            project="o/r",
            iid=1,
            sha="s",
            fingerprint="gone",
            discussion_id="d-gone",
            outcome={
                "resolved": False,
                "deleted": True,
                "developer_replied": False,
                "disputed": False,
                "false_positive": False,
                "duplicate": False,
                "resolved_at": None,
                "merged_unresolved": False,
            },
        )
        selected = db.posted_findings_for_outcome_sync(200)
    assert [row["fingerprint"] for row in selected] == ["live"]


def test_github_graphql_failure_is_not_memoized_and_rest_fallback_is_shared() -> None:
    """A failed thread fetch is retried per finding; the REST fallback list is per PR."""

    def route(method: str, url: str, body: Any) -> Any:
        path = urlparse(url).path
        if method == "POST" and path.endswith("/graphql"):
            raise RuntimeError("graphql down")
        if path.endswith("/pulls/1/comments"):
            return []
        if "/pulls/comments/" in path:
            return {"id": int(path.rsplit("/", 1)[-1]), "user": {"login": "bubo"}}
        return {"number": 1, "state": "open", "head": {"sha": "s"}}

    http = _FakeScmHttp(route)
    with _temp_db():
        with sqlite3.connect(paths.DB) as conn:
            for i in range(3):
                conn.execute(
                    "insert into review_findings(project,iid,sha,fingerprint,file,line,status,"
                    "discussion_id,body,updated_at) values('o/r',1,'s',?,'a.py',1,'posted',?,"
                    "'b',?)",
                    (f"fp{i}", f"10{i}", poller.now()),
                )
        with (
            patch("bubo._http.request_json", side_effect=http),
            patch.object(GitHubProvider, "token", return_value="tok"),
            patch.object(poller, "read_config", return_value=ReviewConfig(provider="github")),
        ):
            synced = poller.sync_outcomes(limit=200)
    assert synced == 3
    assert http.count(lambda m, u, b: u.endswith("/graphql")) == 3
    assert http.count(lambda m, u, b: urlparse(u).path.endswith("/pulls/1")) == 1
    assert http.count(lambda m, u, b: urlparse(u).path.endswith("/pulls/1/comments")) == 1
    assert http.count(lambda m, u, b: "/pulls/comments/" in u) == 3
