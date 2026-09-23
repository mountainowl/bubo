"""SQLite connection and schema lifecycle for Bubo state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from typing import Any, Literal, Self, TypeVar, overload

from bubo import paths
from bubo.events import log

SLOW_QUERY_MS = 30.0
LegacyQueryObserver = Callable[[str, float], None]
CursorT = TypeVar("CursorT", bound=sqlite3.Cursor)
_query_collector: ContextVar[QueryCollector | None] = ContextVar(
    "bubo_query_collector", default=None
)


@dataclass
class QueryTiming:
    """One SQL statement's cumulative execute-through-fetch duration.

    The object is deliberately updated in place as a result cursor is fetched.
    Collectors therefore retain exactly one record for every ``execute`` or
    ``executemany`` call, including statements whose result is never fetched.
    """

    label: str
    duration_ms: float
    slow_logged: bool = False


class QueryCollector:
    """Collect mutable :class:`QueryTiming` records for one scoped operation."""

    def __init__(self) -> None:
        self.records: list[QueryTiming] = []

    def append(self, timing: QueryTiming) -> None:
        self.records.append(timing)


def query_label(statement: str) -> str:
    """Return a low-cardinality SQL label without exposing query text or values."""
    tokens = statement.strip().lower().replace("\n", " ").split()
    if not tokens:
        return "empty"
    operation = tokens[0]
    target = operation
    anchors = {"select": "from", "insert": "into", "update": None, "delete": "from"}
    anchor = anchors.get(operation)
    if operation == "update" and len(tokens) > 1:
        target = tokens[1]
    elif anchor and anchor in tokens:
        index = tokens.index(anchor)
        if index + 1 < len(tokens):
            target = tokens[index + 1].strip("[]")
    shape = " ".join(tokens).encode()
    return f"{operation}:{target}:{sha256(shape).hexdigest()[:10]}"


def _record_query(statement: str, elapsed_ms: float) -> QueryTiming:
    """Create one timing record and attach it to the active collector."""
    timing = QueryTiming(query_label(statement), elapsed_ms)
    collector = _query_collector.get()
    if collector is not None:
        collector.append(timing)
    _log_slow_query(timing)
    return timing


def _update_query(timing: QueryTiming, elapsed_ms: float) -> None:
    """Update a statement's single record after incremental materialization."""
    timing.duration_ms = elapsed_ms
    _log_slow_query(timing)


def _log_slow_query(timing: QueryTiming) -> None:
    if timing.duration_ms > SLOW_QUERY_MS and not timing.slow_logged:
        timing.slow_logged = True
        # Observability must not change the database operation's result.
        # In particular, this runs from ``execute``'s ``finally`` path.
        with suppress(Exception):
            log(
                "db_query_slow",
                query_label=timing.label,
                duration_ms=round(timing.duration_ms, 3),
            )


class TimedCursor(sqlite3.Cursor):
    """Cursor that measures execution and result materialization safely."""

    _timed_query: QueryTiming | None = None
    _timed_started: float | None = None

    def execute(self, statement: str, parameters: Any = ()) -> Self:
        started = perf_counter()
        succeeded = False
        try:
            cursor = sqlite3.Cursor.execute(self, statement, parameters)
            succeeded = True
        finally:
            elapsed_ms = (perf_counter() - started) * 1000
            # This must stay in ``finally``: SQLite errors are production
            # statements too, and the original exception continues unchanged
            # after this block. Never inspect stale cursor metadata after a
            # failed execute.
            timing = _record_query(statement, elapsed_ms)
            if succeeded and self.description is not None:
                self._timed_query = timing
                self._timed_started = started
            else:
                self._timed_query = None
                self._timed_started = None
        return cursor

    def executemany(self, statement: str, parameters: Any) -> Self:
        started = perf_counter()
        try:
            cursor = sqlite3.Cursor.executemany(self, statement, parameters)
        finally:
            _record_query(statement, (perf_counter() - started) * 1000)
        return cursor

    def _fetch(self, operation: Callable[[], Any], *, exhausted: Callable[[Any], bool]) -> Any:
        """Record one end-to-end execute-through-fetch query observation."""
        timing = self._timed_query
        started = self._timed_started
        if timing is None or started is None:
            return operation()
        try:
            result = operation()
        except BaseException:
            _update_query(timing, (perf_counter() - started) * 1000)
            raise
        _update_query(timing, (perf_counter() - started) * 1000)
        if exhausted(result):
            self._timed_query = None
            self._timed_started = None
        return result

    def fetchone(self) -> Any:
        return self._fetch(
            sqlite3.Cursor.fetchone.__get__(self), exhausted=lambda value: value is None
        )

    def fetchmany(self, size: int | None = None) -> Any:
        actual_size = self.arraysize if size is None else size
        return self._fetch(
            lambda: sqlite3.Cursor.fetchmany(self, actual_size),
            exhausted=lambda value: len(value) < actual_size,
        )

    def fetchall(self) -> Any:
        return self._fetch(sqlite3.Cursor.fetchall.__get__(self), exhausted=lambda _: True)


class TimedConnection(sqlite3.Connection):
    """Connection whose direct and cursor execution paths share timing logic."""

    @overload
    def cursor(self, factory: None = None) -> TimedCursor: ...

    @overload
    def cursor(self, factory: Callable[[sqlite3.Connection], CursorT]) -> CursorT: ...

    def cursor(
        self, factory: Callable[[sqlite3.Connection], sqlite3.Cursor] | None = None
    ) -> sqlite3.Cursor:
        return super().cursor(factory=factory or TimedCursor)

    def execute(self, statement: str, parameters: Any = ()) -> TimedCursor:
        return self.cursor().execute(statement, parameters)

    def executemany(self, statement: str, parameters: Any) -> TimedCursor:
        return self.cursor().executemany(statement, parameters)

    def commit(self) -> None:
        started = perf_counter()
        try:
            return super().commit()
        finally:
            _record_query("commit", (perf_counter() - started) * 1000)

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Literal[False]:
        if exc_type is not None:
            return super().__exit__(exc_type, exc_value, traceback)
        started = perf_counter()
        try:
            # sqlite3.Connection.__exit__ rolls back if its implicit commit
            # fails; delegating preserves that lock-release guarantee.
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            _record_query("commit", (perf_counter() - started) * 1000)


@contextmanager
def observe_queries(
    observer: LegacyQueryObserver | None = None,
) -> Iterator[QueryCollector]:
    """Collect one mutable timing record per SQL statement in this scope.

    The yielded collector is the primary API. The optional callback preserves
    the previous ``(label, duration_ms)`` API and is called once per final
    record when the scope exits.
    """
    collector = QueryCollector()
    token = _query_collector.set(collector)
    try:
        yield collector
    finally:
        _query_collector.reset(token)
        if observer is not None:
            for timing in collector.records:
                observer(timing.label, timing.duration_ms)


@contextmanager
def read_db(connection: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Yield an existing connection or one non-mutating read-only connection."""
    if connection is not None:
        yield connection
        return
    with connect_db(readonly=True) as db:
        yield db


def init_dirs() -> None:
    """Create every runtime directory the poller writes to."""
    for path in (
        paths.DB.parent,
        paths.WORK,
        paths.REPORTS,
        paths.JOBS,
        paths.LOGS,
        paths.RENDERED_PROMPTS,
    ):
        path.mkdir(parents=True, exist_ok=True)


def connect_db(*, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection to the state database with sensible defaults.

    The default (writer) connection sets WAL journaling (so readers don't
    block the single writer) and a 5-second busy timeout. Caller is
    responsible for closing — use as a context manager.

    ``readonly=True`` opens the DB via the ``file:...?mode=ro`` URI: it does
    **not** create the file, does **not** run the WAL pragma (which would
    write the DB header), and rejects any write. This is what the governance
    *report* readers use so reporting is genuinely non-mutating and safe to run
    against a read-only mount; a missing DB raises ``OperationalError`` rather
    than being silently created.
    """
    if readonly:
        ro = sqlite3.connect(
            f"file:{paths.DB}?mode=ro", uri=True, timeout=30, factory=TimedConnection
        )
        ro.execute("pragma busy_timeout=5000")
        return ro
    db = sqlite3.connect(paths.DB, timeout=30, factory=TimedConnection)
    db.execute("pragma journal_mode=WAL")
    db.execute("pragma busy_timeout=5000")
    return db


def init_db() -> None:
    """Create or migrate every table. Safe to call repeatedly.

    Tables:

    * ``reviewed_mrs`` — one row per ``(project, iid, sha)`` review.
    * ``review_runs`` — one row per worker invocation; carries token /
      cost / latency telemetry-quality data.
    * ``review_findings`` — one row per finding, keyed by a stable
      fingerprint so retried workers cannot post duplicate comments.
    * ``finding_outcomes`` — per-finding state that ``--sync-outcomes``
      updates by re-checking GitLab.

    Additive migrations land via :func:`ensure_column`; never drop or
    rename a column without a separate dated migration.
    """
    init_dirs()
    with connect_db() as db:
        db.execute(
            """
            create table if not exists reviewed_mrs (
              project text not null,
              iid integer not null,
              sha text not null,
              status text not null,
              report text,
              error text,
              updated_at text not null,
              primary key(project, iid, sha)
            )
            """
        )
        db.execute(
            """
            create table if not exists review_runs (
              run_id text primary key,
              project text not null,
              iid integer not null,
              sha text not null,
              status text not null,
              model text,
              prompt_version text,
              review_mode text,
              dry_run integer not null,
              started_at text not null,
              finished_at text,
              tokens_input integer,
              tokens_output integer,
              tokens_cached integer,
              tokens_total integer,
              cost_usd real,
              error text
            )
            """
        )
        # Governance/provenance (opt-in, off by default) — one banded signal
        # per change, persisted write-once. Additive so existing DBs migrate
        # on the next run. See bubo.provenance / record_provenance.
        for name, definition in {
            "provenance_band": "text",
            "provenance_source": "text",
            "provenance_confidence": "text",
            "provenance_signals": "text",
            "sensitive_paths": "text",
        }.items():
            ensure_column(db, "review_runs", name, definition)
        # Review-comment voice (``[review].tone``) recorded per run so mood
        # effectiveness can be A/B'd against outcomes. Additive; legacy rows read
        # back as the default ``terse``. See bubo.review_config.VALID_TONES.
        ensure_column(db, "review_runs", "tone", "text")
        # Lines of code reviewed per run — the count of *added* lines across the
        # change's diff (the lines an inline comment can attach to), not total
        # diff churn. Additive; legacy rows read back as NULL.
        ensure_column(db, "review_runs", "lines_reviewed", "integer")
        db.execute(
            """
            create table if not exists review_findings (
              project text not null,
              iid integer not null,
              sha text not null,
              fingerprint text not null,
              file text not null,
              line integer,
              status text not null,
              discussion_id text,
              body text not null,
              updated_at text not null,
              primary key(project, iid, sha, fingerprint)
            )
            """
        )
        for name, definition in {
            "run_id": "text",
            "type": "text",
            "severity": "text",
            "category": "text",
            "confidence": "real",
            "note_id": "text",
            # Opt-in verification (off by default) — per-finding verdict from
            # the pre-post "is this real?" pass. `verified` is 1 (survived) /
            # 0 (refuted) / NULL (not verified); `verify_votes` is the JSON
            # per-lens tally. Additive so existing DBs migrate on next run.
            "verified": "integer",
            "verify_votes": "text",
        }.items():
            ensure_column(db, "review_findings", name, definition)
        db.execute(
            """
            create table if not exists finding_outcomes (
              finding_id text primary key,
              project text not null,
              iid integer not null,
              sha text not null,
              fingerprint text not null,
              discussion_id text,
              resolved integer not null default 0,
              deleted integer not null default 0,
              developer_replied integer not null default 0,
              disputed integer not null default 0,
              false_positive integer not null default 0,
              duplicate integer not null default 0,
              resolved_at text,
              merged_unresolved integer not null default 0,
              reply_classified integer not null default 0,
              last_checked_at text not null
            )
            """
        )
        # Additive migration for DBs created before reply_classified existed.
        ensure_column(db, "finding_outcomes", "reply_classified", "integer not null default 0")
        # Governance policy decisions (opt-in, off by default) — one advisory,
        # write-once decision per change. Separate table from review_runs: a
        # decision is a policy *artifact about* the run's provenance, with its
        # own lifecycle. See record_governance_decision / bubo.governance_policy.
        db.execute(
            """
            create table if not exists governance_decisions (
              run_id text primary key,
              project text not null,
              iid integer not null,
              sha text not null,
              mode text not null,
              action text not null,
              triggered integer not null,
              matched_rule text,
              rigor_injected integer not null default 0,
              band text,
              sensitive_paths text,
              reason text,
              created_at text not null
            )
            """
        )
        # The UI/report path orders or windows these columns repeatedly. These
        # additive indexes leave write semantics and table data unchanged.
        for statement in (
            "create index if not exists reviewed_mrs_updated_at_idx "
            "on reviewed_mrs(updated_at desc)",
            "create index if not exists reviewed_mrs_status_idx on reviewed_mrs(status)",
            "create index if not exists review_runs_started_at_run_id_idx "
            "on review_runs(started_at desc, run_id desc)",
            "create index if not exists review_runs_project_started_at_idx "
            "on review_runs(project, started_at desc)",
            "create index if not exists review_findings_updated_at_idx "
            "on review_findings(updated_at desc)",
            "create index if not exists review_findings_review_updated_at_idx "
            "on review_findings(project, iid, sha, updated_at)",
            "create index if not exists finding_outcomes_review_checked_at_idx "
            "on finding_outcomes(project, iid, sha, last_checked_at)",
            "create index if not exists finding_outcomes_checked_at_idx "
            "on finding_outcomes(last_checked_at desc)",
            "create index if not exists finding_outcomes_project_checked_at_idx "
            "on finding_outcomes(project, last_checked_at desc)",
            "create index if not exists governance_decisions_review_created_at_idx "
            "on governance_decisions(project, iid, sha, created_at)",
            "create index if not exists governance_decisions_created_at_idx "
            "on governance_decisions(created_at desc)",
            "create index if not exists governance_decisions_project_created_at_idx "
            "on governance_decisions(project, created_at desc)",
        ):
            db.execute(statement)
        db.execute("drop index if exists review_runs_started_at_idx")


def ensure_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    """Idempotent ``ALTER TABLE ADD COLUMN`` — additive schema migrations.

    No-op when the column already exists. SQLite cannot parameterize
    identifiers, so ``table``, ``name``, and ``definition`` are
    interpolated; the call sites pass only hardcoded literals.
    """
    columns = {row[1] for row in db.execute(f"pragma table_info({table})").fetchall()}
    if name not in columns:
        db.execute(f"alter table {table} add column {name} {definition}")
