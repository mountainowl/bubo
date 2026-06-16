from __future__ import annotations

import json
import sqlite3
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch

from bubo import gitlab, paths, poller
from bubo.findings import build_position, changed_lines_from_diffs
from bubo.review_config import ReviewConfig
from bubo.telemetry.config import TelemetryConfig
from bubo.telemetry.cost import TokenUsage


class _FakeProvider:
    """Provider stand-in for post_or_plan_findings tests.

    Wraps the real GitLab finding-placement helpers but serves canned MR /
    diff payloads and a fixed post result, so the test does not touch the
    network or MCP.
    """

    name = "fake"

    def __init__(self, mr, diff, post_id=""):
        self._mr = mr
        self._diff = diff
        self._post_id = post_id

    def change_number(self, change):
        return int(change["iid"])

    def get_change(self, cfg, token, project, number):
        return self._mr

    def changed_lines(self, cfg, token, project, number):
        return changed_lines_from_diffs([self._diff])

    def build_position(self, change, changed, finding):
        return build_position(change, changed, finding)

    def post_inline_comment(self, cfg, token, project, number, body, position):
        return self._post_id


def test_init_db_creates_review_telemetry_state_tables() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()

            with sqlite3.connect(paths.DB) as db:
                tables = {
                    row[0]
                    for row in db.execute(
                        "select name from sqlite_master where type='table'"
                    ).fetchall()
                }
                finding_columns = {
                    row[1] for row in db.execute("pragma table_info(review_findings)").fetchall()
                }

            assert {"review_runs", "finding_outcomes"}.issubset(tables)
            assert {"run_id", "type", "severity", "category", "confidence", "note_id"}.issubset(
                finding_columns
            )
    finally:
        paths.DB = original_db


def test_empty_gitlab_discussion_id_is_not_marked_posted() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            cfg = ReviewConfig(
                gitlab_url="https://gitlab.com", dry_run=False, max_findings_per_merge_request=5
            )
            mr = {
                "iid": 9,
                "sha": "abc",
                "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
            }
            raw = """[{
              "type":"issue",
              "severity":"blocking",
              "category":"correctness",
              "title":"bad",
              "file":"src/A.java",
              "line":12,
              "impact":"i",
              "evidence":"e",
              "fix":"f",
              "confidence":1
            }]"""
            diff = {
                "new_path": "src/A.java",
                "old_path": "src/A.java",
                "diff": "@@ -10,1 +12,1 @@\n+new\n",
            }

            posted, planned, skipped = poller.post_or_plan_findings(
                cfg=cfg,
                token="token",
                project="group/repo",
                mr=mr,
                raw_review=raw,
                run_id="run1",
                provider=_FakeProvider(mr, diff, post_id=""),
            )

            with sqlite3.connect(paths.DB) as db:
                row = db.execute("select status,discussion_id from review_findings").fetchone()

            assert (posted, planned, skipped) == (0, 0, 1)
            assert row == ("pending_external_id", None)
    finally:
        paths.DB = original_db


def test_finding_metric_flag_suppresses_finding_emission() -> None:
    class FakeTelemetry:
        config = TelemetryConfig(enabled=True, emit_finding_events=False)

        def __init__(self) -> None:
            self.count = 0

        def record_finding(self, **_: object) -> None:
            self.count += 1

    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            telemetry = FakeTelemetry()
            cfg = ReviewConfig(
                gitlab_url="https://gitlab.com", dry_run=True, max_findings_per_merge_request=5
            )
            mr = {
                "iid": 9,
                "sha": "abc",
                "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
            }
            raw = '[{"type":"issue","severity":"blocking","category":"correctness","title":"bad","file":"src/A.java","line":12}]'
            diff = {
                "new_path": "src/A.java",
                "old_path": "src/A.java",
                "diff": "@@ -10,1 +12,1 @@\n+new\n",
            }

            poller.post_or_plan_findings(
                cfg=cfg,
                token="token",
                project="group/repo",
                mr=mr,
                raw_review=raw,
                telemetry=telemetry,
                provider=_FakeProvider(mr, diff),
            )

            assert telemetry.count == 0
    finally:
        paths.DB = original_db


def test_outcome_sync_prefers_never_checked_then_oldest_checked() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            findings = [
                (
                    "group/repo",
                    1,
                    "sha",
                    "old",
                    "src/A.py",
                    1,
                    "posted",
                    "disc-old",
                    "body",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "group/repo",
                    1,
                    "sha",
                    "new",
                    "src/B.py",
                    2,
                    "posted",
                    "disc-new",
                    "body",
                    "2026-01-02T00:00:00+00:00",
                ),
                (
                    "group/repo",
                    1,
                    "sha",
                    "never",
                    "src/C.py",
                    3,
                    "posted",
                    "disc-never",
                    "body",
                    "2026-01-03T00:00:00+00:00",
                ),
            ]
            with poller.connect_db() as db:
                db.executemany(
                    """
                    insert into review_findings(project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at)
                    values(?,?,?,?,?,?,?,?,?,?)
                    """,
                    findings,
                )
                db.execute(
                    """
                    insert into finding_outcomes(
                      finding_id,project,iid,sha,fingerprint,discussion_id,last_checked_at
                    )
                    values(?,?,?,?,?,?,?)
                    """,
                    (
                        "group/repo:1:sha:old",
                        "group/repo",
                        1,
                        "sha",
                        "old",
                        "disc-old",
                        "2026-01-01T01:00:00+00:00",
                    ),
                )

            rows = poller.posted_findings_for_outcome_sync(limit=3)

            assert [row["fingerprint"] for row in rows] == ["new", "never", "old"]
    finally:
        paths.DB = original_db


def test_record_review_run_start_and_finish() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()

            run_id = poller.review_run_id("group/repo", 7, "abc")
            poller.record_review_run_start(
                run_id=run_id,
                project="group/repo",
                iid=7,
                sha="abc",
                model="codex-cli",
                prompt_version="prompt1",
                review_mode="diff",
                dry_run=True,
            )
            poller.record_review_run_finish(
                run_id=run_id,
                status="success",
                tokens=TokenUsage(input=10, output=2, cached=1, total=13),
                cost_usd=0.25,
                error=None,
            )

            with sqlite3.connect(paths.DB) as db:
                row = db.execute(
                    "select status,tokens_input,tokens_output,tokens_cached,tokens_total,cost_usd from review_runs where run_id=?",
                    (run_id,),
                ).fetchone()

            assert row == ("success", 10, 2, 1, 13, 0.25)
    finally:
        paths.DB = original_db


def test_write_job_records_queue_timestamp() -> None:
    original_jobs = paths.JOBS
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.JOBS = Path(tmp)

            job = poller.write_job("group/repo", {"iid": 7, "sha": "abc"})

            payload = json.loads(job.read_text())
            assert payload["queued_at"].endswith("+00:00")
    finally:
        paths.JOBS = original_jobs


def test_stale_queued_review_is_not_treated_as_already_seen() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            with poller.connect_db() as db:
                db.execute(
                    """
                    insert into reviewed_mrs(project,iid,sha,status,updated_at)
                    values(?,?,?,?,?)
                    """,
                    ("group/repo", 7, "abc", "queued", "2026-01-01T00:00:00+00:00"),
                )

            assert not poller.already_seen("group/repo", 7, "abc", queued_ttl_seconds=1)
    finally:
        paths.DB = original_db


def test_recent_failed_review_is_backed_off_but_stale_failed_retries() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            with poller.connect_db() as db:
                db.execute(
                    """
                    insert into reviewed_mrs(project,iid,sha,status,updated_at)
                    values(?,?,?,?,?)
                    """,
                    ("group/repo", 7, "recent", "failed", poller.now()),
                )
                db.execute(
                    """
                    insert into reviewed_mrs(project,iid,sha,status,updated_at)
                    values(?,?,?,?,?)
                    """,
                    ("group/repo", 7, "old", "failed", "2026-01-01T00:00:00+00:00"),
                )

            assert poller.already_seen("group/repo", 7, "recent", failed_ttl_seconds=60)
            assert not poller.already_seen("group/repo", 7, "old", failed_ttl_seconds=60)
            assert not poller.already_seen("group/repo", 7, "recent")
    finally:
        paths.DB = original_db


def test_gitlab_api_retries_retryable_errors() -> None:
    class FakeResponse:
        headers = {"X-Next-Page": ""}

        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    error = urllib.error.HTTPError(
        "https://gitlab.example/api/v4/projects",
        429,
        "rate limited",
        {"Retry-After": "0"},
        None,
    )
    calls = [error, FakeResponse()]

    def fake_urlopen(*_: object, **__: object):
        item = calls.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # The shared retry/backoff loop (urlopen + sleep) now lives in bubo._http;
    # gitlab.api delegates to it, so the transport seams are patched there.
    with patch("bubo._http.urllib.request.urlopen", side_effect=fake_urlopen):
        with patch("bubo._http.time.sleep") as sleep:
            data, _headers = gitlab.api("https://gitlab.example", "token", "GET", "/projects")

    assert data == {"ok": True}
    sleep.assert_called_once_with(0.0)


def test_cleanup_worktree_removes_only_managed_workdirs() -> None:
    original_work = paths.WORK
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.WORK = Path(tmp) / "work"
            managed = paths.WORK / "repo"
            unmanaged = Path(tmp) / "outside"
            managed.mkdir(parents=True)
            unmanaged.mkdir()

            poller.cleanup_worktree(managed)
            poller.cleanup_worktree(unmanaged)

            assert not managed.exists()
            assert unmanaged.exists()
    finally:
        paths.WORK = original_work
