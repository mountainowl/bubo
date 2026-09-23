from __future__ import annotations

import json
import sqlite3
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace
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


def test_worker_fetches_changed_lines_once_in_normal_mode(monkeypatch, tmp_path: Path) -> None:
    """The worker shares one diff map across LoC and finding-position phases."""

    class Provider:
        name = "fake"

        def __init__(self) -> None:
            self.changed_lines_calls = 0

        def token(self) -> str:
            return "token"

        def checkout(self, cfg, project, mr, repo) -> None:
            repo.mkdir(parents=True, exist_ok=True)

        def changed_lines(self, cfg, token, project, number):
            self.changed_lines_calls += 1
            return {}

        def review_prompt(self, project, mr, cfg, *, extra_directive="") -> str:
            return "review"

        def change_number(self, mr) -> int:
            return int(mr["iid"])

        def bot_username(self) -> str:
            return "bubo"

    class Span:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

    class Telemetry:
        def record_queue_latency(self, **kwargs) -> None:
            return None

        def span(self, *args, **kwargs) -> Span:
            return Span()

        def set_span_attrs(self, *args, **kwargs) -> None:
            return None

        def record_review_done(self, **kwargs) -> None:
            return None

        def record_failure(self, **kwargs) -> None:
            return None

    provider = Provider()
    cfg = ReviewConfig(dry_run=True, post_no_findings_comment=False, reviewer_command=("fake",))
    job = tmp_path / "job.json"
    job.write_text(json.dumps({"project": "group/repo", "mr": {"iid": 7, "sha": "abc"}}))
    monkeypatch.setattr(poller.paths, "WORK", tmp_path / "work")
    monkeypatch.setattr(poller.paths, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(poller, "init_db", lambda: None)
    monkeypatch.setattr(poller, "read_config", lambda: cfg)
    monkeypatch.setattr(poller, "get_provider", lambda _cfg: provider)
    monkeypatch.setattr(poller, "write_rendered_meta_prompt", lambda _cfg: tmp_path / "prompt.md")
    monkeypatch.setattr(poller, "analytics_identity", lambda *_args: None)
    monkeypatch.setattr(poller.analytics, "analytics_enabled", lambda _cfg: False)
    monkeypatch.setattr(poller.analytics, "record_review_completed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(poller.analytics, "flush", lambda: None)
    monkeypatch.setattr(poller, "record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(poller, "record_review_run_start", lambda **_kwargs: None)
    monkeypatch.setattr(poller, "record_review_run_finish", lambda **_kwargs: None)
    monkeypatch.setattr(poller.ReviewTelemetry, "from_config", lambda _cfg: Telemetry())
    monkeypatch.setattr(
        poller, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout="[]", returncode=0)
    )

    assert poller.worker(job) == 0
    assert provider.changed_lines_calls == 1


def test_init_db_creates_review_telemetry_state_tables(initialized_db: Path) -> None:
    with sqlite3.connect(initialized_db) as db:
        tables = {
            row[0]
            for row in db.execute("select name from sqlite_master where type='table'").fetchall()
        }
        finding_columns = {
            row[1] for row in db.execute("pragma table_info(review_findings)").fetchall()
        }

    assert {"review_runs", "finding_outcomes"}.issubset(tables)
    assert {"run_id", "type", "severity", "category", "confidence", "note_id"}.issubset(
        finding_columns
    )


def test_head_change_after_review_skips_outdated_inline_posts() -> None:
    """A fresh head must supersede the agent's completed checkout."""

    class Provider:
        def change_number(self, change):
            return int(change["iid"])

        def get_change(self, *args):
            return {"iid": 1, "sha": "new-head"}

        def changed_lines(self, *args):
            raise AssertionError("stale review must not map a fresh diff")

    cfg = ReviewConfig(dry_run=False)
    raw = (
        '[{"type":"issue","severity":"blocking","category":"correctness",'
        '"title":"bad","file":"x.py","line":1,"impact":"i",'
        '"evidence":"e","fix":"f","confidence":1}]'
    )
    events: list[tuple[str, dict[str, object]]] = []
    with patch.object(poller, "log", lambda event, **fields: events.append((event, fields))):
        result = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "old-head"},
            raw_review=raw,
            provider=Provider(),
        )
    assert result == (0, 0, 1)
    assert events == [
        (
            "finding_posting_superseded",
            {
                "project": "g/r",
                "iid": 1,
                "reviewed_sha": "old-head",
                "current_sha": "new-head",
            },
        )
    ]


def test_head_change_after_verification_skips_post_before_side_effect(monkeypatch) -> None:
    """A push during verification cannot race the following comment post."""

    class Provider(_FakeProvider):
        def __init__(self) -> None:
            mr = {"iid": 1, "sha": "old-head"}
            super().__init__(
                mr, {"new_path": "x.py", "old_path": "x.py", "diff": "@@ -0,0 +1 @@\n+x\n"}
            )
            self.current_sha = "old-head"
            self.posted = False

        def get_change(self, *args):
            return {"iid": 1, "sha": self.current_sha}

        def post_inline_comment(self, *args):
            self.posted = True
            raise AssertionError("superseded finding must not post")

    provider = Provider()
    cfg = ReviewConfig(dry_run=False, verify_findings=True)
    raw = (
        '[{"type":"issue","severity":"blocking","category":"correctness",'
        '"title":"bad","file":"x.py","line":1,"impact":"i",'
        '"evidence":"e","fix":"f","confidence":1}]'
    )

    def verify_then_push(*args):
        provider.current_sha = "new-head"
        return []

    monkeypatch.setattr(poller, "run_verification", verify_then_push)
    monkeypatch.setattr(poller, "finding_seen", lambda *args: False)
    monkeypatch.setattr(poller, "record_finding", lambda **kwargs: None)

    assert poller.post_or_plan_findings(
        cfg=cfg,
        token="t",
        project="g/r",
        mr={"iid": 1, "sha": "old-head"},
        raw_review=raw,
        provider=provider,
    ) == (0, 0, 1)
    assert provider.posted is False


def test_finalize_worker_records_original_exception_type() -> None:
    class Telemetry:
        def __init__(self) -> None:
            self.failures: list[dict[str, object]] = []

        def record_failure(self, **kwargs) -> None:
            self.failures.append(kwargs)

        def record_review_done(self, **kwargs) -> None:
            return None

    telemetry = Telemetry()
    poller._finalize_worker(
        cfg=None,
        telemetry=telemetry,
        run_id="run",
        project="g/r",
        model="model",
        status=poller.ReviewStatus.FAILED,
        started=0,
        tokens=TokenUsage(),
        cost_usd=0,
        lines_reviewed=0,
        files_changed=None,
        lines_changed=None,
        identity=None,
        error="redacted error",
        error_type="OSError",
        posted=0,
        planned=0,
        skipped=0,
    )

    assert telemetry.failures == [{"repo": "g/r", "error_type": "OSError", "operation": "review"}]


def test_empty_gitlab_discussion_id_is_not_marked_posted(initialized_db: Path) -> None:
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
    diff = {"new_path": "src/A.java", "old_path": "src/A.java", "diff": "@@ -10,1 +12,1 @@\n+new\n"}

    posted, planned, skipped = poller.post_or_plan_findings(
        cfg=cfg,
        token="token",
        project="group/repo",
        mr=mr,
        raw_review=raw,
        run_id="run1",
        provider=_FakeProvider(mr, diff, post_id=""),
    )

    with sqlite3.connect(initialized_db) as db:
        row = db.execute("select status,discussion_id from review_findings").fetchone()

    assert (posted, planned, skipped) == (0, 0, 1)
    assert row == ("pending_external_id", None)


def test_finding_metric_flag_suppresses_finding_emission(initialized_db: Path) -> None:
    class FakeTelemetry:
        config = TelemetryConfig(enabled=True, emit_finding_events=False)

        def __init__(self) -> None:
            self.count = 0

        def record_finding(self, **_: object) -> None:
            self.count += 1

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
    diff = {"new_path": "src/A.java", "old_path": "src/A.java", "diff": "@@ -10,1 +12,1 @@\n+new\n"}

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


def test_outcome_sync_prefers_never_checked_then_oldest_checked(initialized_db: Path) -> None:
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


def test_record_review_run_start_and_finish(initialized_db: Path) -> None:
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
        lines_reviewed=42,
    )

    with sqlite3.connect(initialized_db) as db:
        row = db.execute(
            "select status,tokens_input,tokens_output,tokens_cached,tokens_total,cost_usd,lines_reviewed from review_runs where run_id=?",
            (run_id,),
        ).fetchone()

    assert row == ("success", 10, 2, 1, 13, 0.25, 42)


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


def test_stale_queued_review_is_not_treated_as_already_seen(initialized_db: Path) -> None:
    with poller.connect_db() as db:
        db.execute(
            """
            insert into reviewed_mrs(project,iid,sha,status,updated_at)
            values(?,?,?,?,?)
            """,
            ("group/repo", 7, "abc", "queued", "2026-01-01T00:00:00+00:00"),
        )

    assert not poller.already_seen("group/repo", 7, "abc", queued_ttl_seconds=1)


def test_recent_failed_review_is_backed_off_but_stale_failed_retries(initialized_db: Path) -> None:
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
