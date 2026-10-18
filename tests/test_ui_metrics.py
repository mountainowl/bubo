"""Known-answer dashboard math and local live transport checks."""

import json
import sqlite3
from datetime import UTC, datetime
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from test_ui_export import isolated_root  # noqa: F401

from bubo import db, paths, ui_metrics, ui_server


@pytest.mark.usefixtures("isolated_root")
def test_known_answer_cohorts_and_missing_values():
    db.init_db()
    stamp = "2026-10-17T12:00:00+00:00"
    with sqlite3.connect(paths.DB) as con:
        for index, end, cost, model in [
            (1, "12:01:00", 2, "gpt-5"),
            (2, "12:03:00", None, "claude-sonnet"),
            (3, None, None, None),
        ]:
            con.execute(
                """insert into review_runs(run_id,project,iid,sha,status,dry_run,
                started_at,finished_at,cost_usd,model,tokens_total)
                values(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(index),
                    "p",
                    index,
                    "s",
                    "running" if end is None else "success",
                    0,
                    stamp,
                    f"2026-10-17T{end}+00:00" if end else None,
                    cost,
                    model,
                    100 if end else None,
                ),
            )
        for index, category in [(1, "auth"), (2, "correctness")]:
            con.execute(
                """insert into review_findings(project,iid,sha,fingerprint,file,status,
                body,updated_at,category,severity) values(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "p",
                    index,
                    "s",
                    str(index),
                    "secret.py",
                    "posted",
                    "PRIVATE CONTENT",
                    stamp,
                    category,
                    "blocking",
                ),
            )
        con.execute("""insert into finding_outcomes(finding_id,project,iid,sha,fingerprint,
            resolved,last_checked_at) values('1','p',1,'s','1',1,'2027-01-01')""")
        con.execute(
            """insert into reviewed_mrs(project,iid,sha,status,updated_at)
            values('p',3,'s','running',?)""",
            (stamp,),
        )
    result = ui_metrics.build_metrics(paths.DB, generated_at=datetime(2026, 10, 17, 13, tzinfo=UTC))
    window = result["windows"][0]
    assert window["usage"]["runs"] == 3
    assert window["usage"]["completed"] == 2
    assert window["usage"]["latency"]["p50"] == 120
    assert window["usage"]["latency"]["p95"] == 174
    assert window["usage"]["latency"]["stddev"] == 60
    assert window["usage"]["cost_usd"] == dict(total=2, recorded=1, missing=2)
    assert window["outcomes"]["observed"] == 1
    assert window["outcomes"]["unobserved"] == 1
    assert window["outcomes"]["rates"]["resolved"] == 1
    assert window["findings"]["security"] == 1
    assert {m["family"] for m in window["models"]} == {"GPT", "Claude", "Unknown"}
    assert result["active"][0]["iid"] == 3
    assert result["inventory"]["review_runs"] == 3
    assert "PRIVATE CONTENT" not in json.dumps(result)
    assert "secret.py" not in json.dumps(result)


def test_empty_statistics_and_invalid_duration():
    assert ui_metrics.stats([])["p95"] is None
    assert ui_metrics.stats([5])["stddev"] == 0
    assert ui_metrics.ratio(0, 0) is None
    assert ui_metrics._duration(dict(started_at="bad", finished_at="bad")) is None
    assert ui_metrics._duration(dict(started_at="2026-01-02", finished_at="2026-01-01")) is None


def test_live_transport_is_loopback_token_protected_and_fresh(monkeypatch):
    count = 0

    def snapshot():
        nonlocal count
        count += 1
        return {"meta": {}, "count": count}

    monkeypatch.setattr(ui_server, "build_data", snapshot)
    server = ui_server.make_server(b"<h1>Bubo</h1>")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(server.dashboard_url + "data.json", timeout=2) as response:
            assert json.load(response) == {"meta": {"live": True}, "count": 1}
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers.get("Access-Control-Allow-Origin") is None
        with urlopen(server.dashboard_url + "data.json", timeout=2) as response:
            assert json.load(response)["count"] == 2
        with pytest.raises(HTTPError) as exc:
            urlopen(
                server.dashboard_url.replace(server.dashboard_url.split("/")[-2], "wrong"),
                timeout=2,
            )
        assert exc.value.code == 404
        request = Request(server.dashboard_url, headers={"Host": "attacker.example"})
        with pytest.raises(HTTPError) as exc:
            urlopen(request, timeout=2)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.usefixtures("isolated_root")
def test_window_boundaries_governance_and_unknown_outcomes():
    db.init_db()
    with sqlite3.connect(paths.DB) as con:
        # At the same instant, differing timezone spellings must agree.
        for index, stamp in [
            (1, "2026-10-16T13:00:00+00:00"),
            (2, "2026-10-16T05:59:59-07:00"),
            (3, "2026-10-18T13:00:00+00:00"),
        ]:
            con.execute(
                """insert into review_runs(run_id,project,iid,sha,status,dry_run,
                started_at,provenance_band) values(?,?,?,?,?,?,?,?)""",
                (str(index), "p", index, "s", "running", 0, stamp, "mixed"),
            )
        con.execute("""insert into governance_decisions(run_id,project,iid,sha,mode,action,
            triggered,matched_rule,created_at) values('1','p',1,'s','advisory','warn',1,
            'sensitive','2026-10-17T10:00:00+00:00')""")
    metric = ui_metrics.build_metrics(paths.DB, generated_at=datetime(2026, 10, 17, 13, tzinfo=UTC))
    today = metric["windows"][0]
    assert today["usage"]["runs"] == 1
    assert today["usage"]["latency"]["count"] == 0
    assert today["outcomes"]["rates"]["resolved"] is None
    assert today["governance"]["rules"] == {"sensitive": 1}
    assert today["governance"]["triggered"] == 1
    assert metric["windows"][1]["usage"]["runs"] == 2


@pytest.mark.parametrize("explicit_root", [False, True])
def test_live_cli_owns_activity_until_server_closes(monkeypatch, tmp_path, explicit_root):
    from argparse import Namespace
    from contextlib import contextmanager

    from bubo import cli

    transitions = []

    @contextmanager
    def lease():
        transitions.append("entered")
        try:
            yield
        finally:
            transitions.append("exited")

    (tmp_path / "index.html").write_bytes(b"UI")
    monkeypatch.setattr(cli, "activity", lease)
    monkeypatch.setattr(cli, "_asset", lambda _: tmp_path)
    retargeted = []
    monkeypatch.setattr(cli, "_retarget_paths", retargeted.append)

    def serve(index, port):
        assert transitions == ["entered"]
        assert index == b"UI"
        assert port == 0
        return 0

    monkeypatch.setattr(ui_server, "serve", serve)
    assert cli.cmd_ui_serve(Namespace(root=tmp_path if explicit_root else None, port=0)) == 0
    assert transitions == ["entered", "exited"]
    assert retargeted == ([tmp_path] if explicit_root else [])


@pytest.mark.usefixtures("isolated_root")
def test_metrics_never_select_unused_large_finding_body(monkeypatch):
    db.init_db()
    with sqlite3.connect(paths.DB) as con:
        con.execute(
            """insert into review_findings(project,iid,sha,fingerprint,file,status,
            body,updated_at) values('p',1,'s','f','a.py','posted',?,'2026-10-17T12:00:00Z')""",
            ("UNUSED SECRET " * 100_000,),
        )
    original = sqlite3.connect
    reads = []

    def guarded_connect(*args, **kwargs):
        con = original(*args, **kwargs)

        def authorize(action, table, column, _database, _source):
            if action == sqlite3.SQLITE_READ:
                reads.append((table, column))
                if column in {"body", "sensitive_paths", "provenance_signals", "evidence"}:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        con.set_authorizer(authorize)
        return con

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    result = ui_metrics.build_metrics(paths.DB, generated_at=datetime(2026, 10, 17, 13, tzinfo=UTC))
    assert result["windows"][0]["findings"]["total"] == 1
    assert ("review_findings", "body") not in reads
    assert "UNUSED SECRET" not in json.dumps(result)


@pytest.mark.usefixtures("isolated_root")
def test_metrics_budget_discards_previously_completed_window(monkeypatch):
    db.init_db()
    with sqlite3.connect(paths.DB) as con:
        con.execute("""insert into review_runs(run_id,project,iid,sha,status,dry_run,
            started_at) values('1','p',1,'s','running',0,'2026-10-17T12:00:00Z')""")
    instant = datetime(2026, 10, 17, 13, tzinfo=UTC)
    monkeypatch.setattr(ui_metrics, "MAX_METRIC_ROWS", 3)
    assert len(ui_metrics.build_metrics(paths.DB, generated_at=instant)["windows"]) == 3
    monkeypatch.setattr(ui_metrics, "MAX_METRIC_ROWS", 2)
    unavailable = ui_metrics.build_metrics(paths.DB, generated_at=instant)
    assert "budget was exceeded" in unavailable["error"]
    assert unavailable["windows"] == []
    assert unavailable["inventory"] == {}
    assert unavailable["active"] == []


def test_production_rates_use_finished_terminal_non_dry_runs():
    rows = [
        dict(
            status=status,
            finished_at=finish,
            started_at="2026-10-17T12:00:00Z",
            dry_run=dry,
            cost_usd=cost,
        )
        for status, finish, dry, cost in [
            ("success", "2026-10-17T12:01:00Z", 0, 2),
            ("no_findings", "2026-10-17T12:01:00Z", 0, None),
            ("failed", "2026-10-17T12:01:00Z", 0, 1),
            ("failed", "2026-10-17T12:01:00Z", 1, 10),
            ("success", None, 0, 9),
            ("other", "2026-10-17T12:01:00Z", 0, 8),
        ]
    ]
    result = ui_metrics._production(rows)
    assert result["terminal"] == 3
    assert result["successful"] == 2
    assert result["failed"] == 1
    assert result["failed_usage"]["cost_usd"] == dict(total=1, recorded=1, missing=0)
    assert result["success_rate"] == 2 / 3
    assert result["failure_rate"] == 1 / 3
    assert result["successful_usage"]["cost_per_recorded_run"] == 2
    assert result["successful_usage"]["cost_usd"]["recorded"] == 1
    assert result["cost_usd"]["total"] == 20
    assert ui_metrics._production([])["success_rate"] is None
    validation = ui_metrics._production(rows, dry_run=True)
    assert validation["runs"] == validation["failed"] == 1
    assert validation["cost_usd"]["total"] == 10
    assert validation["successful"] == 0


@pytest.mark.usefixtures("isolated_root")
def test_security_attention_separates_unobserved_and_excludes_non_actionable():
    db.init_db()
    with sqlite3.connect(paths.DB) as con:
        for i, severity, status, verified, outcome in [
            (1, "high", "posted", None, None),
            (2, "blocking", "posted", 1, "open"),
            (3, "critical", "posted", 1, "resolved"),
            (4, "high", "posted", 0, "open"),
            (5, "high", "posted", None, "disputed"),
            (6, "high", "planned", None, "open"),
        ]:
            con.execute(
                """insert into review_findings(project,iid,sha,fingerprint,file,status,
                body,updated_at,category,severity,verified) values('p',?,'s',?,'a.py',?,'',
                '2026-10-17T12:00:00Z','security',?,?)""",
                (i, str(i), status, severity, verified),
            )
            if outcome:
                con.execute(
                    """insert into finding_outcomes(finding_id,project,iid,sha,
                    fingerprint,resolved,disputed,last_checked_at) values(?,'p',?,'s',?,?,?,
                    '2026-10-17T12:30:00Z')""",
                    (str(i), i, str(i), int(outcome == "resolved"), int(outcome == "disputed")),
                )
    result = ui_metrics.build_metrics(paths.DB, generated_at=datetime(2026, 10, 17, 13, tzinfo=UTC))
    assert result["windows"][0]["security_attention"] == dict(
        total=4, observed=3, unobserved=1, open=1
    )


@pytest.mark.usefixtures("isolated_root")
def test_audit_coverage_joins_production_run_cohort_and_shares_budget(monkeypatch):
    db.init_db()
    with sqlite3.connect(paths.DB) as con:
        for rid, dry, band, stamp in [
            ("known", 0, "likely_ai", "2026-10-17"),
            ("unknown", 0, "unknown", "2026-10-17"),
            ("missing", 0, None, "2026-10-17"),
            ("dry", 1, "collaborative", "2026-10-17"),
            ("old", 0, "collaborative", "2025-01-01"),
        ]:
            con.execute(
                """insert into review_runs(run_id,project,iid,sha,status,dry_run,
                started_at,provenance_band) values(?,'p',1,'s','success',?,?,?)""",
                (rid, dry, stamp, band),
            )
        # A legacy table without the current uniqueness constraint exercises
        # defensive distinct counting for repeated decisions.
        con.execute("create table governance_copy as select * from governance_decisions")
        con.execute("drop table governance_decisions")
        con.execute("alter table governance_copy rename to governance_decisions")
        for rid in ["known", "known", "dry", "old"]:
            con.execute(
                """insert into governance_decisions(run_id,project,iid,sha,mode,
                action,triggered,created_at) values(?,'p',1,'s','advisory','warn',1,
                '2025-01-01')""",
                (rid,),
            )
    instant = datetime(2026, 10, 17, 13, tzinfo=UTC)
    result = ui_metrics.build_metrics(paths.DB, generated_at=instant)
    today = result["windows"][0]
    assert today["audit_evidence"] == dict(
        available=True,
        production_runs=3,
        policy_recorded=1,
        policy_missing=2,
        policy_coverage=1 / 3,
        provenance_known=1,
        provenance_unknown=2,
        provenance_coverage=1 / 3,
    )
    assert today["governance"]["total"] == 0
    assert today["production"]["cost_usd"] == dict(total=0, recorded=0, missing=3)
    # Four selected runs + one distinct policy join in each of three windows.
    monkeypatch.setattr(ui_metrics, "MAX_METRIC_ROWS", 15)
    assert len(ui_metrics.build_metrics(paths.DB, generated_at=instant)["windows"]) == 3
    monkeypatch.setattr(ui_metrics, "MAX_METRIC_ROWS", 14)
    assert ui_metrics.build_metrics(paths.DB, generated_at=instant)["windows"] == []


@pytest.mark.usefixtures("isolated_root")
def test_audit_coverage_missing_table_is_not_empty_evidence():
    db.init_db()
    instant = datetime(2026, 10, 17, 13, tzinfo=UTC)
    empty = ui_metrics.build_metrics(paths.DB, generated_at=instant)["windows"][0]["audit_evidence"]
    assert empty["available"] is True
    assert empty["policy_recorded"] == 0
    assert empty["policy_coverage"] is None
    with sqlite3.connect(paths.DB) as con:
        con.execute("drop table governance_decisions")
    missing = ui_metrics.build_metrics(paths.DB, generated_at=instant)["windows"][0][
        "audit_evidence"
    ]
    assert missing["available"] is False
    assert missing["policy_recorded"] is None
    assert missing["policy_missing"] is None
    assert missing["policy_coverage"] is None


@pytest.mark.usefixtures("isolated_root")
def test_resolved_proxy_uses_finding_cohort_and_excludes_rejection():
    db.init_db()
    with sqlite3.connect(paths.DB) as con:
        for i, resolved, disputed, false_positive, severity, observed, stamp in [
            (1, 1, 0, 0, "blocking", True, "2026-10-17"),
            (2, 1, 1, 0, "blocking", True, "2026-10-17"),
            (3, 1, 0, 1, "blocking", True, "2026-10-17"),
            (4, 0, 0, 0, "blocking", True, "2026-10-17"),
            (5, 1, 0, 0, "warning", True, "2026-10-17"),
            (6, 1, 0, 0, "blocking", False, "2026-10-17"),
            (7, 1, 0, 0, "blocking", True, "2025-01-01"),
        ]:
            con.execute(
                """insert into review_findings(project,iid,sha,fingerprint,file,
                status,body,updated_at,severity) values('p',?,'s',?,'a.py','posted','',?,?)""",
                (i, str(i), stamp, severity),
            )
            if observed:
                con.execute(
                    """insert into finding_outcomes(finding_id,project,iid,sha,
                    fingerprint,resolved,disputed,false_positive,last_checked_at)
                    values(?,'p',?,'s',?,?,?,?,'2027-01-01')""",
                    (str(i), i, str(i), resolved, disputed, false_positive),
                )
    today = ui_metrics.build_metrics(paths.DB, generated_at=datetime(2026, 10, 17, 13, tzinfo=UTC))[
        "windows"
    ][0]
    assert today["findings"]["total"] == 6
    assert today["outcomes"]["observed"] == 5
    assert today["outcomes"]["unobserved"] == 1
    assert today["outcomes"]["resolved_without_rejection"] == 2
    assert today["outcomes"]["blocking_resolved_without_rejection"] == 1
    assert today["outcomes"]["rates"]["resolved_without_rejection"] == 2 / 5
