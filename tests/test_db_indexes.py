"""Query-plan guards for the state DB's secondary indexes (#251).

Each test runs a real :mod:`bubo.db` reader against a freshly initialized DB,
captures the SQL it actually executes (with binds expanded) and asserts that
SQLite's ``EXPLAIN QUERY PLAN`` uses an index instead of scanning the table.
Without these indexes ``audit_rows`` runs three correlated full scans of
``finding_outcomes`` per review run, so report time grows with the square of
the history size.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bubo import db, paths


@pytest.fixture(autouse=True)
def _temp_db(tmp_path: Path) -> Iterator[None]:
    original = paths.DB
    paths.DB = tmp_path / "reviewer.sqlite"
    try:
        db.init_db()
        yield
    finally:
        paths.DB = original


class _RecordingConnection:
    """Delegating wrapper that records each ``execute(sql, params)`` call."""

    def __init__(self, conn: sqlite3.Connection, executed: list[tuple[str, Any]]) -> None:
        self._conn = conn
        self._executed = executed

    def __enter__(self) -> _RecordingConnection:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: object) -> Any:
        return self._conn.__exit__(*exc)

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        self._executed.append((sql, params))
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _plans_for(call: Callable[[], Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Run ``call`` and return ``{sql: [query plan details]}`` for each SELECT it ran.

    Plans are computed with the reader's own bound parameters (not inlined
    literals), so they match what SQLite does at runtime.
    """
    executed: list[tuple[str, Any]] = []
    real_connect = db.connect_db

    def recording_connect(*args: Any, **kwargs: Any) -> Any:
        return _RecordingConnection(real_connect(*args, **kwargs), executed)

    monkeypatch.setattr(db, "connect_db", recording_connect)
    call()
    monkeypatch.setattr(db, "connect_db", real_connect)

    plans: dict[str, list[str]] = {}
    with sqlite3.connect(paths.DB) as conn:
        for sql, params in executed:
            if not sql.lstrip().lower().startswith(("select", "with")):
                continue
            if "sqlite_master" in sql:
                continue
            plans[sql] = [row[-1] for row in conn.execute(f"explain query plan {sql}", params)]
    assert plans, "reader executed no SELECT statements"
    return plans


def _full_scans(plans: dict[str, list[str]], tables: tuple[str, ...]) -> list[str]:
    """Plan lines that scan one of ``tables`` (by name or alias) without any index."""
    names = {name for table in tables for name in (table, *_ALIASES.get(table, ()))}
    return [
        detail
        for details in plans.values()
        for detail in details
        if (words := detail.split())[:1] == ["SCAN"]
        and len(words) >= 2
        and words[1] in names
        and "USING" not in words
    ]


_ALIASES = {
    "finding_outcomes": ("o", "fo"),
    "review_findings": ("f", "rf"),
    "review_runs": ("r",),
    "reviewed_mrs": (),
    "governance_decisions": ("g",),
}
_BIG_TABLES = tuple(_ALIASES)


def test_init_db_creates_secondary_indexes() -> None:
    with sqlite3.connect(paths.DB) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type='index' and sql is not null"
            )
        }
    assert {
        "ix_fo_change",
        "ix_fo_checked",
        "ix_rm_updated",
        "ix_rm_status",
        "ix_rr_started",
        "ix_rf_updated",
        "ix_gd_created",
        "ix_rf_status",
    } <= names


def test_init_db_indexes_are_idempotent() -> None:
    db.init_db()
    db.init_db()


def test_audit_rows_outcome_subqueries_use_index(monkeypatch: pytest.MonkeyPatch) -> None:
    plans = _plans_for(lambda: db.audit_rows(since_hours=24), monkeypatch)
    details = [d for ds in plans.values() for d in ds]
    assert not _full_scans(plans, _BIG_TABLES), details
    assert any(d.startswith("SEARCH o USING INDEX ix_fo_change") for d in details), details


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(db.count_inflight_workers, id="count_inflight_workers"),
        pytest.param(db.latest_reviewed_row, id="latest_reviewed_row"),
        pytest.param(lambda: db.outcomes_for("g/p", 1, "abc"), id="outcomes_for"),
        pytest.param(db.posted_findings_for_outcome_sync, id="posted_findings_for_outcome_sync"),
        pytest.param(lambda: db.metrics_summary(24), id="metrics_summary"),
        pytest.param(lambda: db.provenance_summary(), id="provenance_summary"),
        pytest.param(lambda: db.outcomes_summary(), id="outcomes_summary"),
        pytest.param(lambda: db.noise_trend(), id="noise_trend"),
        pytest.param(lambda: db.roi_proxy(), id="roi_proxy"),
        pytest.param(lambda: db.latency_summary(), id="latency_summary"),
        pytest.param(lambda: db.policy_decisions_summary(), id="policy_decisions_summary"),
    ],
)
def test_hot_and_report_readers_do_not_full_scan(
    reader: Callable[[], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    plans = _plans_for(reader, monkeypatch)
    assert not _full_scans(plans, _BIG_TABLES), plans
