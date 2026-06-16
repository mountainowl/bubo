"""Unit tests for the bubo MCP server.

We exercise the tool functions directly rather than spawning the stdio
process — the FastMCP wrapper is just a decorator over the same callables,
and the contract we care about is the JSON-shaped return value. Spawning
the actual ``bubo-mcp`` subprocess belongs in an integration test.

Each test seeds a fresh on-disk SQLite (via ``paths.DB`` monkey-patching)
so the schema matches production exactly — we do not stub the DB layer.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from bubo import db, mcp_server, paths
from bubo.review_config import ReviewConfig
from bubo.statuses import FindingStatus, ReviewStatus


def _seed_two_reviews(tmp_db: Path) -> None:
    """Populate a clean DB with two MR rows + one finding + one outcome.

    Uses raw SQL with explicit ``updated_at`` values rather than the
    second-precision ``db.record()`` path, so the "newest-first" ordering
    test does not depend on wall-clock spacing between writes.
    """
    paths.DB = tmp_db
    db.init_db()
    with sqlite3.connect(paths.DB) as conn:
        conn.execute(
            "insert into reviewed_mrs(project,iid,sha,status,report,error,updated_at)"
            " values(?,?,?,?,?,?,?)",
            (
                "group/proj",
                7,
                "deadbeef",
                ReviewStatus.SUCCESS,
                "findings emitted",
                None,
                "2026-05-30T10:00:00+00:00",
            ),
        )
        conn.execute(
            "insert into reviewed_mrs(project,iid,sha,status,report,error,updated_at)"
            " values(?,?,?,?,?,?,?)",
            (
                "group/proj",
                8,
                "cafef00d",
                ReviewStatus.NO_FINDINGS,
                None,
                None,
                "2026-05-30T10:05:00+00:00",
            ),
        )
    db.record_finding(
        project="group/proj",
        iid=7,
        sha="deadbeef",
        fingerprint="fp-1",
        finding={
            "file": "src/foo.py",
            "line": 42,
            "type": "issue",
            "severity": "blocking",
            "category": "correctness",
            "confidence": 0.95,
        },
        status=FindingStatus.POSTED,
        body="rendered comment body",
        discussion_id="d-1",
        run_id="r-1",
        note_id="n-1",
    )
    db.record_finding_outcome(
        project="group/proj",
        iid=7,
        sha="deadbeef",
        fingerprint="fp-1",
        discussion_id="d-1",
        outcome={
            "resolved": True,
            "deleted": False,
            "developer_replied": True,
            "disputed": False,
            "false_positive": False,
            "duplicate": False,
            "resolved_at": "2026-05-30T10:00:00+00:00",
            "merged_unresolved": False,
        },
    )


def test_health_reports_empty_when_no_reviews_recorded() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            result = mcp_server.health()
            assert result["status"] == "empty"
            assert "message" in result
    finally:
        paths.DB = original_db


def test_health_reports_ok_with_age_when_state_present() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_two_reviews(paths.DB)
            result = mcp_server.health()
            assert result["status"] == "ok"
            # The most recent row wins — that is project=group/proj iid=8
            # (recorded after iid=7), with status no_findings.
            assert result["last_status"] == ReviewStatus.NO_FINDINGS
            assert isinstance(result["age_seconds"], float)
            assert result["age_seconds"] >= 0.0
    finally:
        paths.DB = original_db


def test_list_recent_reviews_returns_rows_newest_first() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_two_reviews(paths.DB)
            rows = mcp_server.list_recent_reviews()
            assert len(rows) == 2
            # The second seed (iid=8) was written last → newest first.
            assert rows[0]["iid"] == 8
            assert rows[1]["iid"] == 7
            # status filter
            successes = mcp_server.list_recent_reviews(status=ReviewStatus.SUCCESS)
            assert len(successes) == 1
            assert successes[0]["iid"] == 7
            # project filter — non-matching → empty
            assert mcp_server.list_recent_reviews(project="someone/else") == []
    finally:
        paths.DB = original_db


def test_list_recent_reviews_clamps_limit_to_safe_bounds() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_two_reviews(paths.DB)
            # limit=0 must not return zero rows silently; clamped to 1.
            assert len(mcp_server.list_recent_reviews(limit=0)) == 1
            # limit > 200 must not blow up memory; clamped to 200.
            assert len(mcp_server.list_recent_reviews(limit=10_000)) == 2
    finally:
        paths.DB = original_db


def test_get_review_resolves_latest_sha_when_unspecified() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_two_reviews(paths.DB)
            result = mcp_server.get_review("group/proj", 7)
            assert result["found"] is True
            assert result["sha"] == "deadbeef"
            assert result["status"] == ReviewStatus.SUCCESS
            assert result["report"] == "findings emitted"
    finally:
        paths.DB = original_db


def test_get_review_returns_not_found_marker_when_missing() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            result = mcp_server.get_review("nobody/nothing", 1)
            assert result == {
                "found": False,
                "project": "nobody/nothing",
                "iid": 1,
                "sha": None,
            }
    finally:
        paths.DB = original_db


def test_get_findings_returns_seeded_row_for_latest_sha() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_two_reviews(paths.DB)
            findings = mcp_server.get_findings("group/proj", 7)
            assert len(findings) == 1
            row = findings[0]
            assert row["fingerprint"] == "fp-1"
            assert row["file"] == "src/foo.py"
            assert row["line"] == 42
            assert row["severity"] == "blocking"
            assert row["category"] == "correctness"
            assert row["confidence"] == 0.95
            assert row["status"] == FindingStatus.POSTED
            assert row["discussion_id"] == "d-1"
            assert row["note_id"] == "n-1"
    finally:
        paths.DB = original_db


def test_get_findings_empty_for_unknown_mr() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            assert mcp_server.get_findings("nope", 1) == []
    finally:
        paths.DB = original_db


def test_get_finding_outcomes_coerces_int_flags_to_bool() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_two_reviews(paths.DB)
            outcomes = mcp_server.get_finding_outcomes("group/proj", 7)
            assert len(outcomes) == 1
            o = outcomes[0]
            assert o["resolved"] is True
            assert o["developer_replied"] is True
            assert o["deleted"] is False
            assert o["merged_unresolved"] is False
            assert o["resolved_at"] == "2026-05-30T10:00:00+00:00"
    finally:
        paths.DB = original_db


def test_get_finding_outcomes_empty_when_no_sync_yet() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            db.record("p", 1, "s", ReviewStatus.SUCCESS)
            assert mcp_server.get_finding_outcomes("p", 1) == []
    finally:
        paths.DB = original_db


def test_server_exposes_expected_tool_names() -> None:
    """FastMCP registers tools under the decorated function name.

    Guard rail: if someone reorganizes the module we want a test failure
    here before a downstream client breaks on a missing tool.
    """
    # Newer FastMCP exposes tools through an async list_tools() coroutine;
    # we reach into the internal registry instead so this test stays sync
    # and does not depend on the public API stabilizing.
    registry = getattr(mcp_server.mcp, "_tool_manager", None)
    assert registry is not None, "FastMCP internal tool manager moved"
    names = set(registry._tools.keys())  # type: ignore[attr-defined]
    assert {
        "health",
        "list_recent_reviews",
        "get_review",
        "get_findings",
        "get_finding_outcomes",
        "get_metrics",
        "get_governance_report",
        "get_dispute_classes",
        "review_change",
    }.issubset(names)


def test_seeded_db_row_count_matches_writer_path() -> None:
    """Anchor test: verifies the seed helper actually wrote what we expect.

    Catches the class of bug where a schema migration changes column
    counts and a tuple-based fixture silently shifts under everyone.
    """
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_two_reviews(paths.DB)
            with sqlite3.connect(paths.DB) as conn:
                mr_count = conn.execute("select count(*) from reviewed_mrs").fetchone()[0]
                f_count = conn.execute("select count(*) from review_findings").fetchone()[0]
                o_count = conn.execute("select count(*) from finding_outcomes").fetchone()[0]
            assert (mr_count, f_count, o_count) == (2, 1, 1)
    finally:
        paths.DB = original_db


# ---------------------------------------------------------------------------
# URL parsing — covers the four cases the trigger tool can encounter.
# ---------------------------------------------------------------------------


def test_parse_github_pr_url_yields_owner_repo_and_number() -> None:
    provider, project, number = mcp_server._parse_change_url(
        "https://github.com/mountainowl/bubo/pull/42"
    )
    assert (provider, project, number) == ("github", "mountainowl/bubo", 42)


def test_parse_gitlab_mr_url_handles_nested_groups_and_selfhosted_host() -> None:
    provider, project, number = mcp_server._parse_change_url(
        "https://gitlab.example.com/group/sub/proj/-/merge_requests/137"
    )
    assert (provider, project, number) == ("gitlab", "group/sub/proj", 137)


def test_parse_change_url_rejects_unknown_shapes() -> None:
    with pytest.raises(ValueError, match="unrecognized MR/PR URL"):
        mcp_server._parse_change_url("https://example.com/something/else")


def test_parse_change_url_accepts_trailing_path_segments() -> None:
    # GitHub appends /files, /commits etc. to PR URLs — we should tolerate
    # them rather than reject as "unrecognized".
    provider, project, number = mcp_server._parse_change_url(
        "https://github.com/owner/repo/pull/7/files"
    )
    assert (provider, project, number) == ("github", "owner/repo", 7)


# ---------------------------------------------------------------------------
# Provider resolution — `auto` semantics depend on whether URL was given.
# ---------------------------------------------------------------------------


def test_resolve_provider_accepts_explicit_supported_values() -> None:
    # Explicit values pass through unchanged; this is the path the
    # `review_change` tool takes when no URL was supplied.
    assert mcp_server._resolve_provider("github") == "github"
    assert mcp_server._resolve_provider("gitlab") == "gitlab"


def test_resolve_provider_falls_back_to_default_when_auto() -> None:
    # `provider="auto"` reads `[scm].provider` from `config/env.toml` or
    # returns `DEFAULT_PROVIDER` when the config is missing/unreadable.
    # In either case the result must be a supported provider.
    assert mcp_server._resolve_provider("auto") in mcp_server.SUPPORTED_PROVIDERS


def test_resolve_provider_rejects_unknown_explicit_provider() -> None:
    with pytest.raises(ValueError, match="provider must be one of"):
        mcp_server._resolve_provider("bitbucket")


# ---------------------------------------------------------------------------
# Task string — small but load-bearing (the agent parses this).
# ---------------------------------------------------------------------------


class _FakeReviewProvider:
    """Stand-in SCM provider for review_change tests.

    review_change now builds the contract-carrying prompt via
    ``provider.review_prompt`` (like the poller) and runs the configured
    ``reviewer_command``; these tests mock the provider + the subprocess.
    """

    def __init__(self, prompt: str = "REVIEW PROMPT") -> None:
        self._prompt = prompt

    def token(self) -> str:
        return "tok"

    def get_change(self, cfg, token, project, number):
        return {"web_url": f"https://example/{project}/{number}"}

    def review_prompt(self, project, change, cfg) -> str:
        return self._prompt


# ---------------------------------------------------------------------------
# Findings extractor — tolerant of preambles, strict on shape.
# ---------------------------------------------------------------------------


def test_extract_findings_parses_json_array_after_preamble() -> None:
    raw = 'Some log line\nAnother log\n[{"title":"x","confidence":0.9}]'
    findings = mcp_server._extract_findings(raw)
    assert findings == [{"title": "x", "confidence": 0.9}]


def test_extract_findings_returns_none_on_unparseable_output() -> None:
    assert mcp_server._extract_findings("not json at all") is None
    assert mcp_server._extract_findings("[unterminated") is None


def test_extract_findings_rejects_non_array_json() -> None:
    # The output contract is a JSON *array*; an object is not findings.
    assert mcp_server._extract_findings('{"title": "x"}') is None


# ---------------------------------------------------------------------------
# review_change — subprocess fully mocked so the test exercises only the
# tool's wiring (input parsing, task building, response shape).
# ---------------------------------------------------------------------------


def test_review_change_dispatches_with_parsed_url() -> None:
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args, 0, '[{"title": "ok"}]', None)

    cfg = ReviewConfig(reviewer_command=["codex", "exec"], timeout_seconds=1800)
    with (
        patch("bubo.mcp_server.load_review_config", return_value=cfg),
        patch("bubo.mcp_server.get_provider", return_value=_FakeReviewProvider()),
        patch("bubo.mcp_server.run_bounded", side_effect=fake_run),
    ):
        result = mcp_server.review_change(
            url="https://github.com/owner/repo/pull/5", timeout_seconds=10
        )

    assert result["provider"] == "github"
    assert result["project"] == "owner/repo"
    assert result["number"] == 5
    assert result["exit_code"] == 0
    assert result["findings"] == [{"title": "ok"}]
    assert "raw_output" in result
    # review_change runs the configured reviewer_command with the
    # contract-carrying review prompt (provider.review_prompt), not a
    # bundled review wrapper.
    assert captured["args"][:2] == ["codex", "exec"]
    assert captured["args"][-1] == "REVIEW PROMPT"
    assert captured["timeout"] == 10


def test_review_change_requires_url_or_project_plus_number() -> None:
    with pytest.raises(ValueError, match="either `url` or"):
        mcp_server.review_change(number=42, provider="github")  # project missing


def test_review_change_returns_raw_output_when_findings_unparseable() -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, "Codex crashed mid-stream", None)

    cfg = ReviewConfig(reviewer_command=["codex", "exec"], timeout_seconds=1800)
    with (
        patch("bubo.mcp_server.load_review_config", return_value=cfg),
        patch("bubo.mcp_server.get_provider", return_value=_FakeReviewProvider()),
        patch("bubo.mcp_server.run_bounded", side_effect=fake_run),
    ):
        result = mcp_server.review_change(
            provider="gitlab", project="g/p", number=1, timeout_seconds=10
        )

    assert result["findings"] is None
    assert result["raw_output"] == "Codex crashed mid-stream"
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# Metrics tool — aggregation across review_runs / review_findings.
# ---------------------------------------------------------------------------


def _seed_metrics_data(tmp_db: Path) -> None:
    """Seed two ``review_runs`` rows + the two MRs + finding from the main fixture."""
    _seed_two_reviews(tmp_db)
    # Recent run within the 24h window.
    recent = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    # Stale run outside the 24h window.
    stale = (datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
    with sqlite3.connect(tmp_db) as conn:
        for run_id, started_at, tokens, cost in (
            ("recent-1", recent, 1000, 0.10),
            ("recent-2", recent, 2500, 0.25),
            ("stale-1", stale, 99999, 9.99),
        ):
            conn.execute(
                """
                insert into review_runs(
                  run_id,project,iid,sha,status,model,prompt_version,review_mode,dry_run,
                  started_at,finished_at,tokens_input,tokens_output,tokens_cached,
                  tokens_total,cost_usd,error
                )
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    "group/proj",
                    7,
                    "deadbeef",
                    ReviewStatus.SUCCESS,
                    "gpt-test",
                    "v1",
                    "diff",
                    0,
                    started_at,
                    started_at,
                    0,
                    0,
                    0,
                    tokens,
                    cost,
                    None,
                ),
            )


def test_get_metrics_aggregates_within_window_excluding_stale_rows() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_metrics_data(paths.DB)
            metrics = mcp_server.get_metrics(since_hours=24)
            # Token sum should only include the two recent runs
            # (1000 + 2500 = 3500), not the 99999 stale one.
            assert metrics["tokens_total_sum"] == 3500
            assert metrics["cost_usd_sum"] == pytest.approx(0.35)
            assert metrics["window_hours"] == 24
            assert metrics["project"] is None
    finally:
        paths.DB = original_db


def test_get_metrics_includes_stale_rows_when_window_widens() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_metrics_data(paths.DB)
            metrics = mcp_server.get_metrics(since_hours=720)
            # With a one-month window, the stale run lands in scope too.
            assert metrics["tokens_total_sum"] == 3500 + 99999
    finally:
        paths.DB = original_db


def test_get_metrics_clamps_extreme_since_hours_inputs() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            # since_hours=0 must not silently match nothing; clamped to 1h.
            assert mcp_server.get_metrics(since_hours=0)["window_hours"] == 1
            # Far-future input clamped to 720h (one month).
            assert mcp_server.get_metrics(since_hours=10_000_000)["window_hours"] == 720
    finally:
        paths.DB = original_db


def test_get_metrics_project_filter_excludes_other_projects() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            _seed_metrics_data(paths.DB)
            other = mcp_server.get_metrics(since_hours=720, project="someone/else")
            assert other["reviews_total"] == 0
            assert other["tokens_total_sum"] == 0
    finally:
        paths.DB = original_db


# ---------------------------------------------------------------------------
# get_dispute_classes — read-only stats + truthful would_suppress from config.
# ---------------------------------------------------------------------------


def _seed_dispute_classes(tmp_db: Path) -> None:
    """documentation: 3/5 disputed (0.6); security: 2/5 disputed (0.4)."""
    paths.DB = tmp_db
    db.init_db()
    for category, n_disputed in [("documentation", 3), ("security", 2)]:
        for i in range(5):
            fp = f"{category}-{i}"
            db.record_finding(
                project="g/r",
                iid=1,
                sha="sha",
                fingerprint=fp,
                finding={"category": category, "file": "f.py", "line": 1, "confidence": 0.9},
                status=FindingStatus.POSTED,
                body="b",
                discussion_id=f"d-{fp}",
            )
            db.record_finding_outcome(
                project="g/r",
                iid=1,
                sha="sha",
                fingerprint=fp,
                discussion_id=f"d-{fp}",
                outcome={
                    "resolved": True,
                    "deleted": False,
                    "developer_replied": True,
                    "disputed": i < n_disputed,
                    "false_positive": False,
                    "duplicate": False,
                    "merged_unresolved": False,
                },
            )


def test_get_dispute_classes_flags_would_suppress_from_config() -> None:
    cfg = ReviewConfig(dispute_suppress_threshold=0.5, dispute_suppress_min_samples=5)
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _seed_dispute_classes(Path(tmp) / "reviewer.sqlite")
            with patch("bubo.mcp_server.load_review_config", return_value=cfg):
                out = mcp_server.get_dispute_classes(project="g/r")
    finally:
        paths.DB = original_db
    classes = {c["category"]: c for c in out["classes"]}
    assert classes["documentation"]["dispute_rate"] == 0.6
    assert classes["documentation"]["would_suppress"] is True
    assert classes["security"]["would_suppress"] is False  # 0.4 < 0.5


def test_get_dispute_classes_falls_back_to_raw_when_config_unreadable() -> None:
    from bubo.config_values import ConfigError

    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            _seed_dispute_classes(Path(tmp) / "reviewer.sqlite")
            with patch(
                "bubo.mcp_server.load_review_config",
                side_effect=ConfigError("no config"),
            ):
                out = mcp_server.get_dispute_classes(project="g/r")
    finally:
        paths.DB = original_db
    # No config → raw stats only, no would_suppress flag anywhere.
    assert all("would_suppress" not in c for c in out["classes"])
    doc = next(c for c in out["classes"] if c["category"] == "documentation")
    assert doc["dispute_rate"] == 0.6


def test_get_dispute_classes_does_not_init_db() -> None:
    # Mirrors get_governance_report: read-only, must not create the DB file.
    original_db = paths.DB
    try:
        paths.DB = Path(paths.WORK) / "missing-dispute" / "reviewer.sqlite"
        with (
            patch(
                "bubo.mcp_server.load_review_config",
                return_value=ReviewConfig(),
            ),
            pytest.raises(sqlite3.OperationalError),
        ):
            mcp_server.get_dispute_classes(project="g/r")
        assert not paths.DB.exists()  # mode=ro never creates the file
    finally:
        paths.DB = original_db


# ---------------------------------------------------------------------------
# HTTP transport — bearer-auth ASGI middleware and env-driven settings.
# We exercise the ASGI wrapper directly with a stub inner app rather than
# binding a port; uvicorn integration is a future integration-test concern.
# ---------------------------------------------------------------------------


def _drive_asgi(asgi: object, headers: list[tuple[bytes, bytes]]) -> tuple[int, bytes]:
    """Synchronously drive one HTTP request through an ASGI app.

    Returns ``(status, body)``. The implementation only handles the
    request/response pair used by these tests — enough to verify the
    bearer-auth wrapper without dragging in httpx or a test client.
    """
    import asyncio

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "query_string": b"",
    }
    body_chunks: list[bytes] = []
    status: list[int] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            status.append(int(message["status"]))  # type: ignore[arg-type]
        elif message["type"] == "http.response.body":
            body_chunks.append(message.get("body", b""))  # type: ignore[arg-type]

    asyncio.run(asgi(scope, receive, send))  # type: ignore[operator]
    return status[0], b"".join(body_chunks)


def test_bearer_auth_rejects_request_without_authorization_header() -> None:
    async def stub_inner(scope: object, receive: object, send: object) -> None:
        raise AssertionError("inner app must not be reached without auth")

    wrapped = mcp_server._BearerAuthASGI(stub_inner, expected_token="secret")
    status, body = _drive_asgi(wrapped, headers=[])
    assert status == 401
    assert b"unauthorized" in body


def test_bearer_auth_rejects_request_with_wrong_token() -> None:
    async def stub_inner(scope: object, receive: object, send: object) -> None:
        raise AssertionError("inner app must not be reached with wrong token")

    wrapped = mcp_server._BearerAuthASGI(stub_inner, expected_token="secret")
    status, _ = _drive_asgi(wrapped, headers=[(b"authorization", b"Bearer not-the-right-token")])
    assert status == 401


def test_bearer_auth_passes_request_with_matching_token() -> None:
    called: dict[str, bool] = {"inner": False}

    async def stub_inner(scope: object, receive: object, send: object) -> None:
        called["inner"] = True
        await send(
            {  # type: ignore[operator]
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})  # type: ignore[operator]

    wrapped = mcp_server._BearerAuthASGI(stub_inner, expected_token="secret")
    status, body = _drive_asgi(wrapped, headers=[(b"authorization", b"Bearer secret")])
    assert called["inner"] is True
    assert status == 200
    assert body == b"ok"


def test_bearer_auth_does_not_short_circuit_lifespan_events() -> None:
    # Lifespan startup/shutdown carries no auth — the wrapper must
    # forward unchanged so the inner app can wire up SSE state.
    import asyncio

    captured: list[dict[str, object]] = []

    async def stub_inner(scope: dict[str, object], receive: object, send: object) -> None:
        captured.append(scope)

    wrapped = mcp_server._BearerAuthASGI(stub_inner, expected_token="secret")

    async def noop_receive() -> dict[str, object]:
        return {"type": "lifespan.startup"}

    async def noop_send(_: dict[str, object]) -> None:
        return None

    asyncio.run(wrapped({"type": "lifespan"}, noop_receive, noop_send))
    assert captured == [{"type": "lifespan"}]


def test_http_settings_reads_env_with_safe_defaults() -> None:
    env_keys = (
        "BUBO_MCP_HOST",
        "BUBO_MCP_PORT",
        "BUBO_MCP_BEARER_TOKEN",
    )
    saved = {key: os.environ.pop(key, None) for key in env_keys}
    try:
        os.environ["BUBO_MCP_BEARER_TOKEN"] = "tok-123"
        host, port, token = mcp_server._http_settings()
        assert (host, port, token) == ("127.0.0.1", 8765, "tok-123")

        os.environ["BUBO_MCP_HOST"] = "0.0.0.0"
        os.environ["BUBO_MCP_PORT"] = "9001"
        host, port, _ = mcp_server._http_settings()
        assert (host, port) == ("0.0.0.0", 9001)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_http_settings_refuses_to_start_without_bearer_token() -> None:
    saved = os.environ.pop("BUBO_MCP_BEARER_TOKEN", None)
    try:
        with pytest.raises(SystemExit, match="BUBO_MCP_BEARER_TOKEN must be set"):
            mcp_server._http_settings()
    finally:
        if saved is not None:
            os.environ["BUBO_MCP_BEARER_TOKEN"] = saved


def test_http_settings_rejects_non_integer_port() -> None:
    saved_port = os.environ.pop("BUBO_MCP_PORT", None)
    saved_token = os.environ.pop("BUBO_MCP_BEARER_TOKEN", None)
    try:
        os.environ["BUBO_MCP_PORT"] = "not-a-number"
        os.environ["BUBO_MCP_BEARER_TOKEN"] = "tok"
        with pytest.raises(SystemExit, match="must be an integer"):
            mcp_server._http_settings()
    finally:
        for key, value in (
            ("BUBO_MCP_PORT", saved_port),
            ("BUBO_MCP_BEARER_TOKEN", saved_token),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_main_rejects_unknown_transport() -> None:
    saved = os.environ.pop("BUBO_MCP_TRANSPORT", None)
    try:
        os.environ["BUBO_MCP_TRANSPORT"] = "websocket"
        with pytest.raises(SystemExit, match="must be 'stdio' or 'http'"):
            mcp_server.main()
    finally:
        if saved is None:
            os.environ.pop("BUBO_MCP_TRANSPORT", None)
        else:
            os.environ["BUBO_MCP_TRANSPORT"] = saved


def test_env_config_exports_mcp_server_section() -> None:
    from bubo.env_config import runtime_env

    exports = runtime_env(
        Path("/tmp/fake-root"),
        {
            "mcp_server": {
                "transport": "http",
                "host": "0.0.0.0",
                "port": 9000,
                "bearer_token": "tok-from-toml",
            },
        },
    )
    assert exports["BUBO_MCP_TRANSPORT"] == "http"
    assert exports["BUBO_MCP_HOST"] == "0.0.0.0"
    assert exports["BUBO_MCP_PORT"] == "9000"
    assert exports["BUBO_MCP_BEARER_TOKEN"] == "tok-from-toml"


def test_env_config_defaults_when_mcp_server_section_missing() -> None:
    from bubo.env_config import runtime_env

    exports = runtime_env(Path("/tmp/fake-root"), {})
    assert exports["BUBO_MCP_TRANSPORT"] == "stdio"
    assert exports["BUBO_MCP_HOST"] == "127.0.0.1"
    assert exports["BUBO_MCP_PORT"] == "8765"
    # No bearer token when none configured — stdio path doesn't need one,
    # http path fails loud at startup.
    assert "BUBO_MCP_BEARER_TOKEN" not in exports


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", False),
        ("::1", False),
        ("localhost", False),
        ("LocalHost", False),  # case-insensitive
        ("0.0.0.0", True),
        ("10.0.0.5", True),
        ("reviewer.example.com", True),
    ],
)
def test_bind_is_external_classifies_loopback_vs_routable(host: str, expected: bool) -> None:
    assert mcp_server._bind_is_external(host) is expected


def test_review_change_redacts_bearer_from_raw_output() -> None:
    # The codex subprocess inherits the env, including the bearer. If any
    # sub-tool prints `printenv` or a stack trace including the environ,
    # the bearer would leak through `raw_output` straight to the MCP
    # client. The redactor must catch the `BUBO_MCP_BEARER_TOKEN=`
    # form before we return.
    leaked = "Some output\nBUBO_MCP_BEARER_TOKEN=super-secret-xyz\nMore output\n[]"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, leaked, None)

    cfg = ReviewConfig(reviewer_command=["codex", "exec"], timeout_seconds=1800)
    with (
        patch("bubo.mcp_server.load_review_config", return_value=cfg),
        patch("bubo.mcp_server.get_provider", return_value=_FakeReviewProvider()),
        patch("bubo.mcp_server.run_bounded", side_effect=fake_run),
    ):
        result = mcp_server.review_change(
            provider="github", project="o/r", number=1, timeout_seconds=10
        )

    assert "super-secret-xyz" not in result["raw_output"]
    assert "<redacted>" in result["raw_output"]


def test_secrets_secret_env_names_includes_mcp_bearer() -> None:
    # Anchor: SECRET_ENV_NAMES is the canonical list; if someone deletes
    # the bearer entry the redactor silently stops protecting `raw_output`.
    from bubo.secrets import SECRET_ENV_NAMES

    assert "BUBO_MCP_BEARER_TOKEN" in SECRET_ENV_NAMES
