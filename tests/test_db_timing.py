"""Deterministic coverage for the shared SQLite query-timing boundary."""

from __future__ import annotations

import sqlite3

import pytest

from bubo import db_schema


def test_timed_connection_observes_direct_cursor_and_batch_paths() -> None:
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    try:
        with db_schema.observe_queries() as collector:
            connection.execute("create table sample (value text)")
            connection.execute("insert into sample(value) values(?)", ("private",))
            assert connection.cursor().execute("select value from sample").fetchone() == (
                "private",
            )
            connection.executemany("insert into sample(value) values(?)", [("a",), ("b",)])
    finally:
        connection.close()

    assert [timing.label for timing in collector.records] == [
        db_schema.query_label("create table sample (value text)"),
        db_schema.query_label("insert into sample(value) values(?)"),
        db_schema.query_label("select value from sample"),
        db_schema.query_label("insert into sample(value) values(?)"),
    ]


def test_timed_connection_observes_lazy_result_materialization() -> None:
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    try:
        connection.execute("create table sample (value text)")
        connection.executemany("insert into sample(value) values(?)", [("a",), ("b",)])
        with db_schema.observe_queries() as collector:
            cursor = connection.execute("select value from sample order by value")
            assert cursor.fetchone() == ("a",)
            assert cursor.fetchmany(1) == [("b",)]
            assert cursor.fetchall() == []
    finally:
        connection.close()

    assert len(collector.records) == 1
    assert collector.records[0].label == db_schema.query_label(
        "select value from sample order by value"
    )


def test_incremental_fetches_report_cumulative_execute_through_fetch_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    connection.execute("create table sample (value text)")
    connection.executemany("insert into sample(value) values(?)", [("a",), ("b",)])
    ticks = iter((1.0, 1.1, 1.2, 1.3, 1.4))
    monkeypatch.setattr(db_schema, "perf_counter", lambda: next(ticks))
    try:
        with db_schema.observe_queries() as collector:
            cursor = connection.execute("select value from sample order by value")
            cursor.fetchone()
            cursor.fetchmany(1)
            cursor.fetchall()
    finally:
        connection.close()
    assert len(collector.records) == 1
    assert collector.records[0].duration_ms == pytest.approx(400.0)


def test_unfetched_result_is_recorded_at_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    ticks = iter((1.0, 1.025))
    monkeypatch.setattr(db_schema, "perf_counter", lambda: next(ticks))
    try:
        with db_schema.observe_queries() as collector:
            connection.execute("select ? as value", ("private",))
    finally:
        connection.close()

    assert len(collector.records) == 1
    assert collector.records[0].label == db_schema.query_label("select ? as value")
    assert collector.records[0].duration_ms == pytest.approx(25.0)


def test_failed_execute_records_one_safe_timing_and_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    ticks = iter((1.0, 1.025))
    monkeypatch.setattr(db_schema, "perf_counter", lambda: next(ticks))
    try:
        with db_schema.observe_queries() as collector:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("select ? from", ("do-not-log-me",))
    finally:
        connection.close()

    assert len(collector.records) == 1
    timing = collector.records[0]
    assert timing.label == db_schema.query_label("select ? from")
    assert timing.duration_ms == pytest.approx(25.0)
    assert "do-not-log-me" not in repr(timing)


def test_failing_slow_query_logger_never_masks_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    ticks = iter((1.0, 1.031))
    monkeypatch.setattr(db_schema, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(
        db_schema, "log", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("log"))
    )
    try:
        with db_schema.observe_queries() as collector:
            with pytest.raises(sqlite3.OperationalError):
                connection.execute("select ? from", ("do-not-log-me",))
    finally:
        connection.close()

    assert len(collector.records) == 1
    assert collector.records[0].duration_ms == pytest.approx(31.0)
    assert collector.records[0].slow_logged is True
    assert "do-not-log-me" not in repr(collector.records[0])


def test_failing_slow_query_logger_never_breaks_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    ticks = iter((1.0, 1.031, 1.032))
    monkeypatch.setattr(db_schema, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(
        db_schema, "log", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("log"))
    )
    try:
        with db_schema.observe_queries() as collector:
            assert connection.execute("select 1").fetchone() == (1,)
    finally:
        connection.close()

    assert len(collector.records) == 1
    assert collector.records[0].duration_ms == pytest.approx(32.0)


def test_legacy_observer_receives_one_final_record_per_statement() -> None:
    timings: list[tuple[str, float]] = []
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    try:
        with db_schema.observe_queries(
            lambda label, elapsed_ms: timings.append((label, elapsed_ms))
        ):
            cursor = connection.execute("select 1 union all select 2")
            assert cursor.fetchone() == (1,)
            assert cursor.fetchall() == [(2,)]
    finally:
        connection.close()

    assert [label for label, _ in timings] == [db_schema.query_label("select 1 union all select 2")]


def test_slow_query_event_has_label_and_no_parameter_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    ticks = iter((1.0, 1.0, 1.031))
    monkeypatch.setattr(db_schema, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(db_schema, "log", lambda event, **fields: events.append((event, fields)))
    connection = sqlite3.connect(":memory:", factory=db_schema.TimedConnection)
    try:
        connection.execute("select ? as secret", ("do-not-log-me",)).fetchone()
    finally:
        connection.close()

    assert events == [
        (
            "db_query_slow",
            {
                "query_label": db_schema.query_label("select ? as secret"),
                "duration_ms": 31.0,
            },
        )
    ]
