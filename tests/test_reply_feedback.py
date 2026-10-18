"""The reasoning behind a finding's outcome is kept and used (#184).

Runs the real outcome sync, SQLite writers, report builder and review worker;
only the SCM provider/HTTP boundary, the checkout and the LLM are faked.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from review_output import review_output
from test_outcome_sync import _FakeProvider, _SilentTelemetry

from bubo import db, github, gitlab, paths, poller, report
from bubo.review_config import ReviewConfig
from bubo.scm.github import GitHubProvider

PROJECT = "o/r"
REBUTTAL = "Billing amounts are validated upstream, so this check is intentional here."


def _seed(rows: list[tuple[str, int, str]]) -> None:
    with sqlite3.connect(paths.DB) as conn:
        for index, (file, line, category) in enumerate(rows):
            conn.execute(
                """insert into review_findings(project,iid,sha,fingerprint,file,line,status,
                   discussion_id,body,category,updated_at) values(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    PROJECT,
                    1,
                    "s1",
                    f"fp{index}",
                    file,
                    line,
                    "posted",
                    f"d{index}",
                    "b",
                    category,
                    poller.now(),
                ),
            )


def _outcome(reply: str, **extra: object) -> dict[str, object]:
    return {
        "resolved": True,
        "deleted": False,
        "developer_replied": True,
        "disputed": False,
        "false_positive": False,
        "duplicate": False,
        "resolved_at": None,
        "merged_unresolved": False,
        "_finding_text": "finding",
        "_reply_text": reply,
        "_reply_authors": [{"login": "maint", "maintainer": True}],
        **extra,
    }


def _sync(outcome: dict[str, object], verdict: str = "rejected", limit: int = 50) -> None:
    fake = _SilentTelemetry()
    cfg = ReviewConfig(provider="github", telemetry_config=fake.config)
    with (
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.get_provider", return_value=_FakeProvider(outcome=outcome)),
        patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake),
        patch(
            "bubo.poller.classify_developer_reply",
            return_value={"verdict": verdict, "false_positive": False},
        ),
    ):
        poller.sync_outcomes(limit=limit)


def _feedback() -> list[tuple[Any, ...]]:
    with sqlite3.connect(paths.DB) as conn:
        return conn.execute(
            "select fingerprint, reply_text, responders, verdict, source from finding_feedback"
            " order by first_seen_at, reply_text"
        ).fetchall()


@pytest.fixture
def state(tmp_path: Path) -> Iterator[Path]:
    old = paths.DB
    paths.DB = tmp_path / "state" / "reviewer.sqlite"
    paths.DB.parent.mkdir()
    db.init_db()
    try:
        yield tmp_path
    finally:
        paths.DB = old


def test_rejecting_reply_is_kept_with_responder_and_verdict(state: Path) -> None:
    _seed([("src/billing/charge.py", 10, "correctness")])
    _sync(_outcome(REBUTTAL))
    [(fp, text, responders, verdict, source)] = _feedback()
    assert (fp, text, verdict, source) == ("fp0", REBUTTAL, "rejected", "reply_classifier")
    assert json.loads(responders) == [{"login": "maint", "maintainer": True}]


def test_a_new_reply_adds_to_the_record(state: Path) -> None:
    _seed([("src/billing/charge.py", 10, "correctness")])
    _sync(_outcome("Will look."), verdict="accepted")
    _sync(_outcome("Will look.\n\n" + REBUTTAL))
    rows = _feedback()
    assert [row[3] for row in rows] == ["accepted", "rejected"]
    assert rows[0][1] == "Will look."


def test_explicit_marker_reply_is_kept(state: Path) -> None:
    _seed([("src/billing/charge.py", 10, "correctness")])
    marker = "[llm-review:disputed] intentional"
    _sync(
        _outcome(
            marker,
            disputed=True,
            developer_disposition="disagrees",
            disposition_evidence="explicit_marker",
        )
    )
    [(_, text, _, verdict, source)] = _feedback()
    assert (text, verdict, source) == (marker, "disagrees", "explicit_marker")


def test_dispute_report_shows_the_reason_and_where_it_applied(state: Path) -> None:
    _seed([("src/billing/charge.py", 10, "correctness")])
    _sync(_outcome(REBUTTAL))
    [row] = report.build_report(project=PROJECT)["dispute_classes"]
    assert row["category"] == "correctness"
    [feedback] = row["feedback"]
    assert feedback["reason"] == REBUTTAL
    assert (feedback["file"], feedback["scope"]) == ("src/billing/charge.py", "src/billing")
    assert feedback["responders"] == [{"login": "maint", "maintainer": True}]


def _worker(tmp: Path, findings: list[dict[str, Any]], cfg: ReviewConfig) -> list[Any]:
    posted: list[Any] = []
    sha = "c" * 40

    def http(url: str, *, method: str, body: Any = None, **_: Any) -> tuple[Any, dict[str, str]]:
        path = urlparse(url).path
        if path.endswith("/pulls/1/files"):
            patch_text = "@@ -0,0 +1,3 @@\n+a\n+b\n+c"
            return [{"filename": f["file"], "patch": patch_text} for f in findings], {}
        if path.endswith("/pulls/1/comments") and method == "POST":
            posted.append(body)
            return {"id": len(posted), "node_id": f"N{len(posted)}"}, {}
        if path.endswith("/pulls/1"):
            return {"number": 1, "state": "open", "head": {"sha": sha}}, {}
        return ([] if method == "GET" else {}), {}

    prompt = tmp / "prompt.md"
    prompt.write_text("p", encoding="utf-8")
    job = tmp / "job.json"
    job.write_text(json.dumps({"project": PROJECT, "mr": {"number": 1, "head": {"sha": sha}}}))
    with (
        patch("bubo._http.request_json", side_effect=http),
        patch.object(GitHubProvider, "token", return_value="tok"),
        patch.object(GitHubProvider, "checkout", return_value=None),
        patch.object(poller.paths, "REPORTS", tmp / "reports"),
        patch.object(poller.paths, "WORK", tmp / "work"),
        patch("bubo.poller.read_config", return_value=cfg),
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
    return posted


def test_suppression_applies_only_where_the_team_rejected_the_category(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed([(f"src/billing/m{i}.py", i + 1, "correctness") for i in range(5)])
    _sync(_outcome(REBUTTAL))
    finding = {
        "line": 2,
        "title": "t",
        "body": "b",
        "severity": "blocking",
        "category": "correctness",
        "confidence": 0.95,
        "kind": "bug",
        "fix": "f",
    }
    billing = {**finding, "file": "src/billing/refund.py", "title": "billing"}
    auth = {**finding, "file": "src/auth/login.py", "title": "auth"}
    cfg = ReviewConfig(
        provider="github",
        dry_run=False,
        suppress_disputed_classes=True,
        dispute_suppress_min_samples=5,
        dispute_suppress_threshold=0.5,
    )
    capsys.readouterr()
    posted = _worker(state, [billing, auth], cfg)
    assert [p["path"] for p in posted] == ["src/auth/login.py"]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if "{" in line]
    [filtered] = [e for e in events if e.get("event") == "finding_filtered"]
    assert filtered["file"] == "src/billing/refund.py"
    assert filtered["reason"] == "disputed_class_suppressed"
    assert filtered["dispute_scope"] == "src/billing"
    assert filtered["dispute_reason"] == REBUTTAL


def test_gitlab_reply_authors_are_reported() -> None:
    discussion = {
        "notes": [
            {"author": {"username": "bubo"}, "body": "Issue"},
            {"author": {"username": "dev"}, "body": "no"},
        ]
    }
    out = gitlab.classify_discussion_outcome(discussion, "bubo", "opened")
    assert out["_reply_authors"] == [{"login": "dev", "maintainer": None}]


def test_github_reply_authors_carry_maintainer_status() -> None:
    thread = {
        "is_resolved": False,
        "comments": [
            {"login": "bubo", "body": "Issue", "in_reply_to_node_id": "", "node_id": "R"},
            {
                "login": "owner",
                "actor_type": "User",
                "author_association": "OWNER",
                "body": "no",
                "in_reply_to_node_id": "R",
            },
            {
                "login": "drive-by",
                "actor_type": "User",
                "author_association": "NONE",
                "body": "agree with owner",
                "in_reply_to_node_id": "R",
            },
        ],
    }
    out = github.classify_graphql_thread_outcome(thread, "bubo", "open")
    assert out["_reply_authors"] == [
        {"login": "owner", "maintainer": True},
        {"login": "drive-by", "maintainer": False},
    ]
