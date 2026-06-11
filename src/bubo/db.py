"""SQLite state layer for the review pipeline.

Owns the schema (``reviewed_mrs``, ``review_runs``, ``review_findings``,
``finding_outcomes``) and every reader/writer that touches it. The rest of
the codebase calls functions in this module — nothing else opens the
database directly.

Design choices, on purpose:

* **One connection per call.** Every helper opens its own
  :func:`connect_db` context. Wasteful at scale but obvious to reason
  about; SQLite WAL mode plus the 5s busy timeout makes contention rare
  at this workload (≤8 MRs/cycle, single host).
* **``INSERT … ON CONFLICT DO UPDATE``** for every writer. Idempotent at
  the SQL layer so a retried worker cannot corrupt state.
* **No ORM.** The schema is small and stable; raw SQL keeps the layer
  inspectable. Use :func:`ensure_column` for additive migrations rather
  than a versioned migration framework — appropriate for the size.

Schema-altering changes belong in :func:`init_db`. The function is called
unconditionally at the start of every poll/worker/sync run so new columns
land on the first invocation after deploy.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bubo import paths
from bubo.events import now
from bubo.hash_utils import stable_digest, stable_hash
from bubo.statuses import FindingStatus, ReviewMode, ReviewStatus
from bubo.telemetry import TokenUsage
from bubo.types import JsonObject

# Path attributes are read via the ``paths`` module at call time (rather
# than imported by name once at module load) so tests can monkey-patch
# ``paths.DB`` / ``paths.WORK`` and have the new value reach the writers
# below.


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


def connect_db() -> sqlite3.Connection:
    """Open a connection to the state database with sensible defaults.

    Sets WAL journaling (so readers don't block the single writer) and a
    5-second busy timeout (so a concurrent worker waits rather than
    raising ``OperationalError``). Caller is responsible for closing —
    use as a context manager.
    """
    db = sqlite3.connect(paths.DB, timeout=30)
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


def ensure_column(db: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    """Idempotent ``ALTER TABLE ADD COLUMN`` — additive schema migrations.

    No-op when the column already exists. SQLite cannot parameterize
    identifiers, so ``table``, ``name``, and ``definition`` are
    interpolated; the call sites pass only hardcoded literals.
    """
    columns = {row[1] for row in db.execute(f"pragma table_info({table})").fetchall()}
    if name not in columns:
        db.execute(f"alter table {table} add column {name} {definition}")


def review_run_id(project: str, iid: int, sha: str) -> str:
    """Deterministic ID for a review run, used as the ``review_runs`` PK.

    Same (project, iid, sha) → same run_id, across processes and across
    parent/forked-worker boundaries. SHA-256 over the canonical JSON form
    of the tuple — see :func:`bubo.hash_utils.stable_hash`.
    """
    return stable_hash({"project": project, "iid": iid, "sha": sha})


def prompt_version(prompt: Path) -> str:
    """Short hash of the rendered meta prompt — used as a metric label.

    Returns the literal string ``"unknown"`` if the file cannot be read,
    so a missing prompt does not break the recorded run entirely.
    """
    try:
        return stable_digest(prompt.read_bytes(), length=12)
    except OSError:
        return "unknown"


def record_review_run_start(
    *,
    run_id: str,
    project: str,
    iid: int,
    sha: str,
    model: str,
    prompt_version: str,
    review_mode: ReviewMode | str,
    dry_run: bool,
) -> None:
    """Insert (or reset) the ``review_runs`` row at the start of a worker.

    Idempotent: a retried worker with the same ``run_id`` clears
    ``finished_at`` and ``error`` and updates the start metadata.
    """
    with connect_db() as db:
        db.execute(
            """
            insert into review_runs(
              run_id,project,iid,sha,status,model,prompt_version,review_mode,dry_run,started_at
            )
            values(?,?,?,?,?,?,?,?,?,?)
            on conflict(run_id) do update set
              status=excluded.status,
              model=excluded.model,
              prompt_version=excluded.prompt_version,
              review_mode=excluded.review_mode,
              dry_run=excluded.dry_run,
              started_at=excluded.started_at,
              finished_at=null,
              error=null
            """,
            (
                run_id,
                project,
                iid,
                sha,
                ReviewStatus.RUNNING,
                model,
                prompt_version,
                review_mode,
                int(dry_run),
                now(),
            ),
        )


def record_review_run_finish(
    *,
    run_id: str,
    status: ReviewStatus | str,
    tokens: TokenUsage,
    cost_usd: float,
    error: str | None,
) -> None:
    """Finalize a ``review_runs`` row at the end of a worker.

    Updates token counts, cost, and the terminal status. If no row exists
    for ``run_id`` (because the worker failed before
    :func:`record_review_run_start`), this is a silent no-op — the row
    simply never appears in telemetry rather than carrying partial data.
    """
    with connect_db() as db:
        db.execute(
            """
            update review_runs set
              status=?,
              finished_at=?,
              tokens_input=?,
              tokens_output=?,
              tokens_cached=?,
              tokens_total=?,
              cost_usd=?,
              error=?
            where run_id=?
            """,
            (
                status,
                now(),
                tokens.input,
                tokens.output,
                tokens.cached,
                tokens.total,
                cost_usd,
                error,
                run_id,
            ),
        )


def record(
    project: str,
    iid: int,
    sha: str,
    status: ReviewStatus,
    report: str | None = None,
    error: str | None = None,
) -> None:
    """Upsert one ``reviewed_mrs`` row with the latest status.

    The primary index keys on ``(project, iid, sha)`` so transient
    statuses (``queued`` → ``running`` → terminal) all flow into the
    same row.
    """
    with connect_db() as db:
        db.execute(
            """
            insert into reviewed_mrs(project,iid,sha,status,report,error,updated_at)
            values(?,?,?,?,?,?,?)
            on conflict(project,iid,sha) do update set
              status=excluded.status,
              report=excluded.report,
              error=excluded.error,
              updated_at=excluded.updated_at
            """,
            (project, iid, sha, status, report, error, now()),
        )


def already_seen(
    project: str,
    iid: int,
    sha: str,
    queued_ttl_seconds: int | None = None,
    failed_ttl_seconds: int | None = None,
) -> bool:
    """Return ``True`` if the poll loop should skip this (project, iid, sha).

    Terminal statuses (``running``, ``success``, ``no_findings``) always
    skip. ``queued`` and ``failed`` rows get a TTL — older rows are
    treated as eligible for re-queue (the worker died or a transient
    failure has aged out).
    """
    with connect_db() as db:
        row = db.execute(
            """
            select status,updated_at from reviewed_mrs
            where project=? and iid=? and sha=?
            """,
            (project, iid, sha),
        ).fetchone()
    if row is None:
        return False
    status, updated_at = row
    if status == ReviewStatus.QUEUED and queued_ttl_seconds is not None:
        return status_age_seconds(updated_at) <= queued_ttl_seconds
    if status == ReviewStatus.FAILED:
        return (
            failed_ttl_seconds is not None and status_age_seconds(updated_at) <= failed_ttl_seconds
        )
    return status in {ReviewStatus.RUNNING, ReviewStatus.SUCCESS, ReviewStatus.NO_FINDINGS}


def status_age_seconds(updated_at: object) -> float:
    """Seconds since the ISO-8601 ``updated_at`` string.

    Returns ``+inf`` for an unparseable timestamp so callers treating
    "very old" as "expired" do the safe thing on garbage input.
    """
    try:
        updated = datetime.fromisoformat(str(updated_at))
    except ValueError:
        return float("inf")
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return (datetime.now(UTC) - updated).total_seconds()


def count_inflight_workers() -> int:
    """Count MRs currently in ``running`` or ``queued`` status.

    See :func:`bubo.poller.poll` for how the result is used as a
    backpressure signal. Over-reports during the TTL-reap gap; that is
    the safe direction.
    """
    with connect_db() as db:
        row = db.execute(
            """
            select count(*) from reviewed_mrs
            where status in (?, ?)
            """,
            (ReviewStatus.RUNNING, ReviewStatus.QUEUED),
        ).fetchone()
    return int(row[0]) if row else 0


def latest_reviewed_row() -> tuple[str, str] | None:
    """Return ``(status, updated_at)`` of the most recently touched MR row.

    Used by :func:`bubo.poller.check_health`. ``None`` when the
    table is empty (fresh install).
    """
    with connect_db() as db:
        row = db.execute(
            "select status, updated_at from reviewed_mrs order by updated_at desc limit 1"
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def finding_seen(project: str, iid: int, sha: str, fingerprint: str) -> bool:
    """Return ``True`` if a finding with this fingerprint was already posted.

    Used to short-circuit re-extraction across retried worker runs at the
    same SHA.
    """
    with connect_db() as db:
        row = db.execute(
            """
            select 1 from review_findings
            where project=? and iid=? and sha=? and fingerprint=?
              and status = ?
            """,
            (project, iid, sha, fingerprint, FindingStatus.POSTED),
        ).fetchone()
    return row is not None


def record_finding(
    *,
    project: str,
    iid: int,
    sha: str,
    fingerprint: str,
    finding: JsonObject,
    status: FindingStatus,
    body: str,
    discussion_id: str | None = None,
    run_id: str | None = None,
    note_id: str | None = None,
) -> None:
    """Upsert one ``review_findings`` row.

    ``body`` is the rendered comment body (computed by the caller via
    :func:`bubo.findings.finding_body` so this module does not
    have to depend on findings.py). Passing it in keeps the DB layer
    free of finding-formatting logic.
    """
    file_path = str(finding.get("file") or finding.get("path") or "")
    line = finding.get("line") or finding.get("new_line")
    line = int(line) if line is not None else None
    confidence = finding.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except TypeError, ValueError:
        confidence = None
    with connect_db() as db:
        db.execute(
            """
            insert into review_findings(
              project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at,
              run_id,type,severity,category,confidence,note_id
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(project,iid,sha,fingerprint) do update set
              status=excluded.status,
              discussion_id=excluded.discussion_id,
              body=excluded.body,
              run_id=excluded.run_id,
              type=excluded.type,
              severity=excluded.severity,
              category=excluded.category,
              confidence=excluded.confidence,
              note_id=excluded.note_id,
              updated_at=excluded.updated_at
            """,
            (
                project,
                iid,
                sha,
                fingerprint,
                file_path,
                line,
                status,
                discussion_id,
                body,
                now(),
                run_id,
                finding.get("type"),
                finding.get("severity"),
                finding.get("category"),
                confidence,
                note_id,
            ),
        )


def record_finding_outcome(
    *,
    project: str,
    iid: int,
    sha: str,
    fingerprint: str,
    discussion_id: str,
    outcome: JsonObject,
) -> None:
    """Upsert a ``finding_outcomes`` row from a classify_discussion_outcome dict."""
    finding_id = f"{project}:{iid}:{sha}:{fingerprint}"
    with connect_db() as db:
        db.execute(
            """
            insert into finding_outcomes(
              finding_id,project,iid,sha,fingerprint,discussion_id,
              resolved,deleted,developer_replied,disputed,false_positive,duplicate,
              resolved_at,merged_unresolved,reply_classified,last_checked_at
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(finding_id) do update set
              discussion_id=excluded.discussion_id,
              resolved=excluded.resolved,
              deleted=excluded.deleted,
              developer_replied=excluded.developer_replied,
              disputed=excluded.disputed,
              false_positive=excluded.false_positive,
              duplicate=excluded.duplicate,
              resolved_at=excluded.resolved_at,
              merged_unresolved=excluded.merged_unresolved,
              reply_classified=excluded.reply_classified,
              last_checked_at=excluded.last_checked_at
            """,
            (
                finding_id,
                project,
                iid,
                sha,
                fingerprint,
                discussion_id,
                int(bool(outcome["resolved"])),
                int(bool(outcome["deleted"])),
                int(bool(outcome["developer_replied"])),
                int(bool(outcome["disputed"])),
                int(bool(outcome["false_positive"])),
                int(bool(outcome["duplicate"])),
                outcome.get("resolved_at"),
                int(bool(outcome["merged_unresolved"])),
                int(bool(outcome.get("reply_classified", False))),
                now(),
            ),
        )


def record_finding_outcome_sync_attempt(
    *,
    project: str,
    iid: int,
    sha: str,
    fingerprint: str,
    discussion_id: str,
) -> None:
    """Record only the timestamp of a sync attempt — used after sync failures.

    Without this, a persistently-failing GitLab fetch (404 deleted
    discussion, permission revoked) keeps the same row at the head of
    the outcome-sync query forever. Touching ``last_checked_at`` lets
    the loop move past it.
    """
    finding_id = f"{project}:{iid}:{sha}:{fingerprint}"
    with connect_db() as db:
        db.execute(
            """
            insert into finding_outcomes(
              finding_id,project,iid,sha,fingerprint,discussion_id,last_checked_at
            )
            values(?,?,?,?,?,?,?)
            on conflict(finding_id) do update set
              discussion_id=excluded.discussion_id,
              last_checked_at=excluded.last_checked_at
            """,
            (finding_id, project, iid, sha, fingerprint, discussion_id, now()),
        )


def posted_findings_for_outcome_sync(limit: int = 200) -> list[JsonObject]:
    """Return up to ``limit`` posted findings ordered by sync staleness.

    Never-synced findings come first; then oldest ``last_checked_at``.
    The poller uses this list to drive ``--sync-outcomes``.
    """
    with connect_db() as db:
        rows = db.execute(
            """
            select rf.project,rf.iid,rf.sha,rf.fingerprint,rf.discussion_id,
                   fo.reply_classified
            from review_findings rf
            left join finding_outcomes fo
              on fo.finding_id = rf.project || ':' || rf.iid || ':' || rf.sha || ':' ||
                rf.fingerprint
            where rf.status=? and rf.discussion_id is not null and rf.discussion_id != ''
            order by
              case when fo.last_checked_at is null then 0 else 1 end,
              fo.last_checked_at asc,
              rf.updated_at asc
            limit ?
            """,
            (FindingStatus.POSTED, limit),
        ).fetchall()
    return [
        {
            "project": row[0],
            "iid": int(row[1]),
            "sha": row[2],
            "fingerprint": row[3],
            "discussion_id": row[4],
            "reply_classified": bool(row[5]),
        }
        for row in rows
    ]


def list_recent_reviews(
    limit: int = 20,
    status: str | None = None,
    project: str | None = None,
) -> list[JsonObject]:
    """Return ``reviewed_mrs`` rows newest-first, with optional filters.

    Caller-friendly reader powering :func:`bubo.mcp_server.list_recent_reviews`
    — keeps SQL in this module so MCP server code stays free of cursor
    handling.

    ``limit`` is clamped to ``[1, 200]`` so a misconfigured client cannot
    accidentally request the whole table.
    """
    limit = max(1, min(200, int(limit)))
    sql = "select project,iid,sha,status,error,updated_at from reviewed_mrs"
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if project is not None:
        clauses.append("project=?")
        params.append(project)
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by updated_at desc limit ?"
    params.append(limit)
    with connect_db() as db:
        rows = db.execute(sql, params).fetchall()
    return [
        {
            "project": row[0],
            "iid": int(row[1]),
            "sha": row[2],
            "status": row[3],
            "error": row[4],
            "updated_at": row[5],
        }
        for row in rows
    ]


def get_review_row(project: str, iid: int, sha: str | None = None) -> JsonObject | None:
    """Return one ``reviewed_mrs`` row, or ``None`` if no match.

    When ``sha`` is ``None``, the row with the freshest ``updated_at`` for
    ``(project, iid)`` is returned — useful for "what's the current state
    of MR <iid>" without first looking up the SHA.
    """
    with connect_db() as db:
        if sha is None:
            row = db.execute(
                """
                select project,iid,sha,status,report,error,updated_at
                from reviewed_mrs
                where project=? and iid=?
                order by updated_at desc
                limit 1
                """,
                (project, iid),
            ).fetchone()
        else:
            row = db.execute(
                """
                select project,iid,sha,status,report,error,updated_at
                from reviewed_mrs
                where project=? and iid=? and sha=?
                """,
                (project, iid, sha),
            ).fetchone()
    if row is None:
        return None
    return {
        "project": row[0],
        "iid": int(row[1]),
        "sha": row[2],
        "status": row[3],
        "report": row[4],
        "error": row[5],
        "updated_at": row[6],
    }


def _resolve_sha(db: sqlite3.Connection, project: str, iid: int) -> str | None:
    """Return the most-recently-updated SHA for ``(project, iid)`` or None.

    Internal helper. Used by :func:`findings_for` and :func:`outcomes_for`
    when the caller did not pin a SHA — we resolve to the same SHA
    :func:`get_review_row` would pick, so the three readers agree on
    "current".
    """
    row = db.execute(
        """
        select sha from reviewed_mrs
        where project=? and iid=?
        order by updated_at desc
        limit 1
        """,
        (project, iid),
    ).fetchone()
    return None if row is None else str(row[0])


def findings_for(project: str, iid: int, sha: str | None = None) -> list[JsonObject]:
    """Return one ``review_findings`` row per finding for an MR/PR.

    See :func:`bubo.mcp_server.get_findings` for the public
    contract. When ``sha`` is ``None`` we resolve to the most recent
    reviewed SHA via :func:`_resolve_sha`.
    """
    with connect_db() as db:
        target_sha = sha if sha is not None else _resolve_sha(db, project, iid)
        if target_sha is None:
            return []
        rows = db.execute(
            """
            select fingerprint,file,line,status,discussion_id,body,updated_at,
                   run_id,type,severity,category,confidence,note_id
            from review_findings
            where project=? and iid=? and sha=?
            order by updated_at asc
            """,
            (project, iid, target_sha),
        ).fetchall()
    return [
        {
            "project": project,
            "iid": iid,
            "sha": target_sha,
            "fingerprint": row[0],
            "file": row[1],
            "line": row[2],
            "status": row[3],
            "discussion_id": row[4],
            "body": row[5],
            "updated_at": row[6],
            "run_id": row[7],
            "type": row[8],
            "severity": row[9],
            "category": row[10],
            "confidence": row[11],
            "note_id": row[12],
        }
        for row in rows
    ]


def outcomes_for(project: str, iid: int, sha: str | None = None) -> list[JsonObject]:
    """Return one ``finding_outcomes`` row per finding for an MR/PR.

    Empty list when ``--sync-outcomes`` has not yet run for the target —
    that is not an error condition.
    """
    with connect_db() as db:
        target_sha = sha if sha is not None else _resolve_sha(db, project, iid)
        if target_sha is None:
            return []
        rows = db.execute(
            """
            select fingerprint,discussion_id,resolved,deleted,
                   developer_replied,disputed,false_positive,duplicate,
                   resolved_at,merged_unresolved,reply_classified,last_checked_at
            from finding_outcomes
            where project=? and iid=? and sha=?
            order by last_checked_at desc
            """,
            (project, iid, target_sha),
        ).fetchall()
    return [
        {
            "project": project,
            "iid": iid,
            "sha": target_sha,
            "fingerprint": row[0],
            "discussion_id": row[1],
            "resolved": bool(row[2]),
            "deleted": bool(row[3]),
            "developer_replied": bool(row[4]),
            "disputed": bool(row[5]),
            "false_positive": bool(row[6]),
            "duplicate": bool(row[7]),
            "resolved_at": row[8],
            "merged_unresolved": bool(row[9]),
            "reply_classified": bool(row[10]),
            "last_checked_at": row[11],
        }
        for row in rows
    ]


def metrics_summary(since_hours: int = 24, project: str | None = None) -> JsonObject:
    """Aggregate counts and totals over ``since_hours`` of history.

    Powers :func:`bubo.mcp_server.get_metrics`. Three queries
    (reviews/by-status, findings count, token+cost sum) run against the
    same connection — sqlite-cheap, no need to optimize.

    The ``(? is null or column = ?)`` predicate folds the optional
    project filter into one SQL per metric instead of doubling them;
    each parameter pair is the same ``project`` value twice.

    ``since_hours`` is clamped to ``[1, 720]`` so a misconfigured client
    cannot accidentally scan the entire table; one month is a generous
    operational window.
    """
    since_hours = max(1, min(720, int(since_hours)))
    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    with connect_db() as db:
        status_rows = db.execute(
            """
            select status, count(*) from reviewed_mrs
            where updated_at >= ? and (? is null or project = ?)
            group by status
            """,
            (cutoff, project, project),
        ).fetchall()
        findings_row = db.execute(
            """
            select count(*) from review_findings
            where updated_at >= ? and (? is null or project = ?)
            """,
            (cutoff, project, project),
        ).fetchone()
        token_row = db.execute(
            """
            select coalesce(sum(tokens_total),0), coalesce(sum(cost_usd),0.0)
            from review_runs
            where started_at >= ? and (? is null or project = ?)
            """,
            (cutoff, project, project),
        ).fetchone()
    by_status = {str(row[0]): int(row[1]) for row in status_rows}
    return {
        "window_hours": since_hours,
        "project": project,
        "reviews_total": sum(by_status.values()),
        "by_status": by_status,
        "findings_total": int(findings_row[0]) if findings_row else 0,
        "tokens_total_sum": int(token_row[0]) if token_row else 0,
        "cost_usd_sum": float(token_row[1]) if token_row else 0.0,
    }


__all__ = [
    "already_seen",
    "connect_db",
    "count_inflight_workers",
    "ensure_column",
    "finding_seen",
    "findings_for",
    "get_review_row",
    "init_db",
    "init_dirs",
    "latest_reviewed_row",
    "list_recent_reviews",
    "metrics_summary",
    "outcomes_for",
    "posted_findings_for_outcome_sync",
    "prompt_version",
    "record",
    "record_finding",
    "record_finding_outcome",
    "record_finding_outcome_sync_attempt",
    "record_review_run_finish",
    "record_review_run_start",
    "review_run_id",
    "status_age_seconds",
]
