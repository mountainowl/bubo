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

import json
import math
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from bubo import paths
from bubo.events import now
from bubo.governance_policy import GovernanceDecision
from bubo.hash_utils import stable_digest, stable_hash
from bubo.provenance import ProvenanceSignal
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
        ro = sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=30)
        ro.execute("pragma busy_timeout=5000")
        return ro
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


def record_provenance(run_id: str, signal: ProvenanceSignal) -> None:
    """Persist a change's provenance onto its ``review_runs`` row — write-once.

    Governance/audit integrity: provenance is computed once per run and must
    never be retroactively rewritten, so this UPDATEs **only** when
    ``provenance_band`` is still null. A no-op when the run row doesn't exist
    yet or already carries provenance. The signal's list fields are stored as
    JSON text for the audit trail.
    """
    with connect_db() as db:
        db.execute(
            """
            update review_runs set
              provenance_band=?,
              provenance_source=?,
              provenance_confidence=?,
              provenance_signals=?,
              sensitive_paths=?
            where run_id=? and provenance_band is null
            """,
            (
                signal.band,
                signal.source,
                signal.confidence,
                json.dumps(signal.ai_signals),
                json.dumps(signal.sensitive_paths),
                run_id,
            ),
        )


def provenance_for(run_id: str) -> JsonObject | None:
    """Return the persisted provenance for ``run_id``, or ``None`` if absent.

    The inverse of :func:`record_provenance`; the JSON list fields are decoded
    back to lists. Used by tests now and by Phase 3 governance reporting later.
    """
    with connect_db() as db:
        row = db.execute(
            """
            select provenance_band, provenance_source, provenance_confidence,
                   provenance_signals, sensitive_paths
            from review_runs where run_id=?
            """,
            (run_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return {
        "band": row[0],
        "source": row[1],
        "confidence": row[2],
        "ai_signals": json.loads(row[3]) if row[3] else [],
        "sensitive_paths": json.loads(row[4]) if row[4] else [],
    }


def _governance_decision_row(
    project: str, iid: int, sha: str, row: tuple[Any, ...]
) -> JsonObject:
    """Shape a ``governance_decisions`` row tuple into the public JSON dict."""
    return {
        "run_id": row[0],
        "project": project,
        "iid": iid,
        "sha": sha,
        "mode": row[1],
        "action": row[2],
        "triggered": bool(row[3]),
        "matched_rule": row[4],
        "rigor_injected": bool(row[5]),
        "band": row[6],
        "sensitive_paths": json.loads(row[7]) if row[7] else [],
        "reason": row[8],
        "created_at": row[9],
    }


def record_governance_decision(
    run_id: str,
    *,
    project: str,
    iid: int,
    sha: str,
    decision: GovernanceDecision,
) -> None:
    """Persist an advisory governance decision — write-once (audit integrity).

    ``insert ... on conflict(run_id) do nothing`` so a retried worker never
    rewrites an existing decision; the first decision for a run is the record
    of truth. ``sensitive_paths`` is stored as JSON text.
    """
    with connect_db() as db:
        db.execute(
            """
            insert into governance_decisions(
              run_id,project,iid,sha,mode,action,triggered,matched_rule,
              rigor_injected,band,sensitive_paths,reason,created_at
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?)
            on conflict(run_id) do nothing
            """,
            (
                run_id,
                project,
                iid,
                sha,
                decision.mode,
                decision.action,
                int(decision.triggered),
                decision.matched_rule,
                int(decision.rigor_injected),
                decision.band,
                json.dumps(decision.sensitive_paths),
                decision.reason,
                now(),
            ),
        )


def governance_decision_for(run_id: str) -> JsonObject | None:
    """Return the governance decision for ``run_id``, or ``None`` if absent."""
    with connect_db() as db:
        row = db.execute(
            """
            select run_id,mode,action,triggered,matched_rule,rigor_injected,
                   band,sensitive_paths,reason,created_at
            from governance_decisions where run_id=?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        meta = db.execute(
            "select project,iid,sha from governance_decisions where run_id=?",
            (run_id,),
        ).fetchone()
    return _governance_decision_row(str(meta[0]), int(meta[1]), str(meta[2]), row)


def governance_decisions_for(
    project: str, iid: int, sha: str | None = None
) -> list[JsonObject]:
    """Return governance decisions for an MR/PR (keyed like findings_for/outcomes_for).

    When ``sha`` is ``None`` the most-recent reviewed SHA is resolved via
    :func:`_resolve_sha` so this reader agrees with the others on "current".
    """
    with connect_db() as db:
        target_sha = sha if sha is not None else _resolve_sha(db, project, iid)
        if target_sha is None:
            return []
        rows = db.execute(
            """
            select run_id,mode,action,triggered,matched_rule,rigor_injected,
                   band,sensitive_paths,reason,created_at
            from governance_decisions
            where project=? and iid=? and sha=?
            order by created_at asc
            """,
            (project, iid, target_sha),
        ).fetchall()
    return [_governance_decision_row(project, iid, target_sha, row) for row in rows]


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
    verified: bool | None = None,
    verify_votes: str | None = None,
) -> None:
    """Upsert one ``review_findings`` row.

    ``body`` is the rendered comment body (computed by the caller via
    :func:`bubo.findings.finding_body` so this module does not
    have to depend on findings.py). Passing it in keeps the DB layer
    free of finding-formatting logic.

    ``verified`` / ``verify_votes`` carry the opt-in verification verdict
    (off by default; ``None`` when the pass did not run). They are written
    *write-once*: the on-conflict branch ``COALESCE``s a non-NULL prior
    verdict, so a later verify-off re-record at the same SHA cannot null out
    an audit trail an earlier verified run wrote.
    """
    file_path = str(finding.get("file") or finding.get("path") or "")
    line = finding.get("line") or finding.get("new_line")
    line = int(line) if line is not None else None
    confidence = finding.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None
    verified_int = None if verified is None else int(verified)
    with connect_db() as db:
        db.execute(
            """
            insert into review_findings(
              project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at,
              run_id,type,severity,category,confidence,note_id,verified,verify_votes
            )
            values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
              verified=coalesce(excluded.verified, review_findings.verified),
              verify_votes=coalesce(excluded.verify_votes, review_findings.verify_votes),
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
                verified_int,
                verify_votes,
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


# Shared dispute-class aggregation. Both the suppression-set reader
# (:func:`disputed_finding_classes`, poller path, writable connection) and the
# read-only stats reader (:func:`disputed_class_stats`, report/MCP path) run the
# EXACT same join + normalization through here so the two can never drift on what
# "a category's dispute rate" means. The helper takes an OPEN connection rather
# than opening its own, so each caller picks its own read/write mode.
_DISPUTE_CLASS_SQL = """
    select lower(trim(rf.category)) as category,
           count(*) as total,
           sum(case when fo.disputed = 1 or fo.false_positive = 1
                    then 1 else 0 end) as rejected
    from finding_outcomes fo
    join review_findings rf
      on rf.project || ':' || rf.iid || ':' || rf.sha || ':' || rf.fingerprint
         = fo.finding_id
    where rf.project = ?
      and rf.category is not null
      and trim(rf.category) != ''
    group by category
"""


def _dispute_class_rows(db: sqlite3.Connection, project: str) -> list[tuple[str, int, int]]:
    """Run the shared dispute-class aggregation against an open connection.

    Returns ``(category, total, rejected)`` triples — the raw,
    config-independent counts. ``total`` is *all* outcome rows for the
    category (including the diluting sync-attempt rows); ``rejected`` is
    ``count(disputed OR false_positive)``. Rate/threshold semantics live in
    the callers so the SQL stays a single source of truth.
    """
    rows = db.execute(_DISPUTE_CLASS_SQL, (project,)).fetchall()
    return [(str(category), int(total), int(rejected)) for category, total, rejected in rows]


def disputed_finding_classes(
    project: str,
    *,
    min_samples: int,
    threshold: float,
) -> set[str]:
    """Return the set of finding categories this repo repeatedly rejects.

    Powers the opt-in dispute-driven suppression filter
    (``[review].suppress_disputed_classes``). For ``project``, it joins
    ``finding_outcomes`` to ``review_findings`` on the composite finding id,
    groups by normalized ``category``, and returns every category whose
    dispute rate clears ``threshold`` once at least ``min_samples`` outcomes
    have accrued.

    Dispute rate is ``count(disputed OR false_positive) / count(outcomes)``
    for the category. The denominator is *all* outcome rows for the
    category, including ones written by
    :func:`record_finding_outcome_sync_attempt` on a sync failure (which
    carry ``disputed=0, false_positive=0``). That deliberately dilutes the
    rate — the bias is toward **under**-suppressing, so a real finding class
    is never silenced off a thin or noisy signal.

    Categories are normalized with ``lower(trim(...))`` here and must be
    matched the same way at the call site
    (:func:`bubo.findings.filter_findings_by_policy`).

    Note: suppression is self-reinforcing — a suppressed category stops
    producing new ``review_findings`` / ``finding_outcomes`` rows, so its
    rate is frozen at the pre-suppression snapshot. The escape hatches are
    operator-side: raise ``threshold`` / ``min_samples`` or disable the
    flag. This is documented as a known limitation in
    ``docs/configuration.md``.
    """
    with connect_db() as db:
        rows = _dispute_class_rows(db, project)
    return {
        category
        for category, total, rejected in rows
        if total >= min_samples and (rejected / total) >= threshold
    }


def disputed_class_stats(project: str, *, min_samples: int) -> list[JsonObject]:
    """Return raw per-category dispute stats for ``project`` (read-only).

    The config-independent *truth* behind
    :func:`disputed_finding_classes`: every normalized category with at least
    ``min_samples`` outcome rows, as ``{category, total, rejected,
    dispute_rate}`` where ``dispute_rate = rejected / total``. Ordered by
    ``dispute_rate`` descending then ``category`` ascending for deterministic
    output.

    Shares the EXACT join + normalization + dilution semantics of
    :func:`disputed_finding_classes` via :func:`_dispute_class_rows`, so the
    two readers cannot drift. Unlike that reader this one carries no
    ``threshold`` and no ``suppressed`` flag: whether a class *would* be
    suppressed depends on the operator's real ``[review]`` thresholds, which
    only the caller knows. The report layer (:func:`bubo.report.build_report`)
    derives a truthful ``would_suppress`` flag from those when given them.

    Read-only: opens a non-mutating connection and never calls ``init_db``.
    """
    with connect_db(readonly=True) as db:
        rows = _dispute_class_rows(db, project)
    # Sort the raw triples (rate desc, category asc) BEFORE building dicts so the
    # sort key is concretely typed (mypy can't see into a dict[str, Any] value).
    ranked = sorted(
        (
            (category, total, rejected)
            for category, total, rejected in rows
            if total >= min_samples
        ),
        key=lambda r: (-(r[2] / r[1]), r[0]),
    )
    stats: list[JsonObject] = [
        {
            "category": category,
            "total": total,
            "rejected": rejected,
            "dispute_rate": rejected / total,
        }
        for category, total, rejected in ranked
    ]
    return stats


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
                   run_id,type,severity,category,confidence,note_id,verified
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
            "verified": row[13],
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


def metrics_summary(
    since_hours: int = 24,
    project: str | None = None,
    *,
    since: str | None = None,
    until: str | None = None,
    readonly: bool = False,
) -> JsonObject:
    """Aggregate counts and totals over a window of history.

    Powers :func:`bubo.mcp_server.get_metrics` and the ``reviews`` section of
    the governance report. Three queries (reviews/by-status, findings count,
    token+cost sum) run against the same connection — sqlite-cheap.

    The ``(? is null or column = ?)`` predicate folds the optional project
    filter into one SQL per metric.

    Window: when ``since``/``until`` are given (the report path), the shared
    :func:`_report_window` resolves them (so this section covers the SAME window
    as the rest of the report); otherwise the legacy ``since_hours`` path
    applies, clamped to ``[1, 720]`` so a misconfigured client cannot scan the
    whole table. ``readonly=True`` uses a non-mutating connection (report path).
    ``by_status`` is ordered for deterministic output.
    """
    if since is not None or until is not None:
        start, end = _report_window(since_hours, since, until)
    else:
        since_hours = max(1, min(720, int(since_hours)))
        start = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
        end = _OPEN_END
    args = (start, end, project, project)
    with connect_db(readonly=readonly) as db:
        status_rows = db.execute(
            """
            select status, count(*) from reviewed_mrs
            where updated_at >= ? and updated_at <= ? and (? is null or project = ?)
            group by status order by status
            """,
            args,
        ).fetchall()
        findings_row = db.execute(
            """
            select count(*) from review_findings
            where updated_at >= ? and updated_at <= ? and (? is null or project = ?)
            """,
            args,
        ).fetchone()
        token_row = db.execute(
            """
            select coalesce(sum(tokens_total),0), coalesce(sum(cost_usd),0.0)
            from review_runs
            where started_at >= ? and started_at <= ? and (? is null or project = ?)
            """,
            args,
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


# ---------------------------------------------------------------------------
# Governance reporting readers (Phase 3 / Rec ③).
#
# All read-only and deterministic (explicit ORDER BY with tie-breaker). They do
# NOT call init_db — reporting must never mutate state, so callers run them
# against an already-initialized DB. Raw counts are returned here; rates/ratios
# are derived once in bubo.report (single rounding boundary).
# ---------------------------------------------------------------------------

# ~366 days — a generous audit window (vs metrics_summary's 30-day operational
# clamp); regulated reports run quarterly/annually.
_REPORT_MAX_HOURS = 8784
_OPEN_END = "9999-12-31T23:59:59+00:00"


def _parse_bound(value: str, *, end: bool) -> str:
    """Normalize a user ISO date/datetime to the stored timestamp format.

    Stored timestamps are ``datetime.now(UTC).isoformat(timespec="seconds")``
    (a ``+00:00`` offset). The window is compared as STRINGS, so a raw user
    bound like ``2026-06-16`` or an offset-less ``...T23:59:59`` would
    mis-compare against the stored ``+00:00`` strings. Parse to a UTC datetime
    and re-serialize in the stored format so the comparison is exact:

    * a date-only bound is widened to the start (``end=False``) or **end**
      (``end=True``) of that day, so ``--until 2026-06-16`` includes all of the
      16th;
    * a naive datetime is assumed UTC; an offset-aware one is converted to UTC.

    Raises :class:`ValueError` on an unparseable value (caller surfaces it).
    """
    text = value.strip()
    # A date-only bound (no time component) widens to the start/end of that day.
    # Check this FIRST: datetime.fromisoformat("2026-06-16") would otherwise
    # succeed at midnight and silently drop the end-of-day widening.
    if "T" not in text and " " not in text:
        day = date.fromisoformat(text)  # raises ValueError if not a bare date
        parsed = datetime.combine(day, time(23, 59, 59) if end else time(0, 0, 0))
    else:
        parsed = datetime.fromisoformat(text)
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return parsed.isoformat(timespec="seconds")


def _report_window(since_hours: int, since: str | None, until: str | None) -> tuple[str, str]:
    """Resolve a report window into ``(start_iso, end_iso)``, both UTC strings.

    An explicit ``since``/``until`` bound wins (fixed audit periods) and is
    normalized via :func:`_parse_bound` so string comparison against the stored
    ``+00:00`` timestamps is exact; otherwise the window is the last
    ``since_hours`` (clamped) up to an open end so clock skew never drops a
    just-written row.
    """
    end = _parse_bound(until, end=True) if until else _OPEN_END
    if since:
        return _parse_bound(since, end=False), end
    hours = max(1, min(_REPORT_MAX_HOURS, int(since_hours)))
    start = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    return start, end


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "select 1 from sqlite_master where type='table' and name=?", (name,)
    ).fetchone()
    return row is not None


def provenance_summary(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
) -> JsonObject:
    """Counts of review runs by provenance band and source within the window."""
    start, end = _report_window(since_hours, since, until)
    where = "started_at >= ? and started_at <= ? and (? is null or project = ?)"
    args = (start, end, project, project)
    with connect_db(readonly=True) as db:
        runs_total = db.execute(
            f"select count(*) from review_runs where {where}", args
        ).fetchone()[0]
        band_rows = db.execute(
            f"select provenance_band, count(*) from review_runs "
            f"where {where} and provenance_band is not null group by provenance_band "
            f"order by provenance_band",
            args,
        ).fetchall()
        source_rows = db.execute(
            f"select provenance_source, count(*) from review_runs "
            f"where {where} and provenance_source is not null group by provenance_source "
            f"order by provenance_source",
            args,
        ).fetchall()
        sensitive_runs = db.execute(
            f"select count(*) from review_runs "
            f"where {where} and sensitive_paths is not null "
            f"and sensitive_paths not in ('', '[]')",
            args,
        ).fetchone()[0]
    return {
        "runs_total": int(runs_total),
        "by_band": {str(r[0]): int(r[1]) for r in band_rows},
        "by_source": {str(r[0]): int(r[1]) for r in source_rows},
        "sensitive_path_runs": int(sensitive_runs),
    }


def outcomes_summary(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
) -> JsonObject:
    """Raw finding-outcome counts within the window (rates derived in report)."""
    start, end = _report_window(since_hours, since, until)
    where = "last_checked_at >= ? and last_checked_at <= ? and (? is null or project = ?)"
    args = (start, end, project, project)
    with connect_db(readonly=True) as db:
        row = db.execute(
            f"""
            select count(*),
                   coalesce(sum(resolved),0), coalesce(sum(disputed),0),
                   coalesce(sum(false_positive),0), coalesce(sum(duplicate),0),
                   coalesce(sum(developer_replied),0), coalesce(sum(merged_unresolved),0),
                   coalesce(sum(deleted),0)
            from finding_outcomes where {where}
            """,
            args,
        ).fetchone()
    return {
        "total": int(row[0]),
        "resolved": int(row[1]),
        "disputed": int(row[2]),
        "false_positive": int(row[3]),
        "duplicate": int(row[4]),
        "developer_replied": int(row[5]),
        "merged_unresolved": int(row[6]),
        "deleted": int(row[7]),
    }


def noise_trend(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
) -> list[JsonObject]:
    """Per-day finding/false-positive/dispute counts (ascending by day)."""
    start, end = _report_window(since_hours, since, until)
    where = "last_checked_at >= ? and last_checked_at <= ? and (? is null or project = ?)"
    with connect_db(readonly=True) as db:
        rows = db.execute(
            f"""
            select date(last_checked_at) as day, count(*),
                   coalesce(sum(false_positive),0), coalesce(sum(disputed),0)
            from finding_outcomes where {where}
            group by day order by day asc
            """,
            (start, end, project, project),
        ).fetchall()
    return [
        {
            "day": str(r[0]),
            "findings": int(r[1]),
            "false_positive": int(r[2]),
            "disputed": int(r[3]),
        }
        for r in rows
    ]


def roi_proxy(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
) -> JsonObject:
    """Bug-catch ROI proxy: accepted findings + cost over the window.

    ``accepted`` = findings whose outcome is resolved and neither disputed nor
    false-positive. Joins findings to outcomes on the composite finding id
    (same join used elsewhere). ``cost_usd_sum`` comes from ``review_runs``.
    """
    start, end = _report_window(since_hours, since, until)
    fwhere = "rf.updated_at >= ? and rf.updated_at <= ? and (? is null or rf.project = ?)"
    args = (start, end, project, project)
    with connect_db(readonly=True) as db:
        findings_total = db.execute(
            f"select count(*) from review_findings rf where {fwhere}", args
        ).fetchone()[0]
        accepted_row = db.execute(
            f"""
            select count(*),
                   coalesce(sum(case when rf.severity = 'blocking' then 1 else 0 end), 0)
            from review_findings rf
            join finding_outcomes fo
              on fo.finding_id = rf.project || ':' || rf.iid || ':' || rf.sha
                 || ':' || rf.fingerprint
            where {fwhere} and fo.resolved = 1 and fo.disputed = 0 and fo.false_positive = 0
            """,
            args,
        ).fetchone()
        cost_row = db.execute(
            "select coalesce(sum(cost_usd),0.0) from review_runs "
            "where started_at >= ? and started_at <= ? and (? is null or project = ?)",
            args,
        ).fetchone()
    return {
        "findings_total": int(findings_total),
        "accepted": int(accepted_row[0]),
        "blocking_accepted": int(accepted_row[1]),
        "cost_usd_sum": float(cost_row[0]),
    }


def policy_decisions_summary(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
) -> JsonObject:
    """Governance-decision counts by action/mode/band within the window.

    Degrades gracefully (``available: False``) if the ``governance_decisions``
    table is absent — e.g. a DB created before Phase 2 — so reporting never
    hard-fails on a partially-migrated install.
    """
    start, end = _report_window(since_hours, since, until)
    where = "created_at >= ? and created_at <= ? and (? is null or project = ?)"
    args = (start, end, project, project)
    empty = {"available": False, "total": 0, "by_action": {}, "by_mode": {}, "by_band": {}}
    with connect_db(readonly=True) as db:
        if not _table_exists(db, "governance_decisions"):
            return empty
        total = db.execute(
            f"select count(*) from governance_decisions where {where}", args
        ).fetchone()[0]
        action_rows = db.execute(
            f"select action, count(*) from governance_decisions where {where} "
            f"group by action order by action",
            args,
        ).fetchall()
        mode_rows = db.execute(
            f"select mode, count(*) from governance_decisions where {where} "
            f"group by mode order by mode",
            args,
        ).fetchall()
        band_rows = db.execute(
            f"select band, count(*) from governance_decisions where {where} "
            f"and band is not null group by band order by band",
            args,
        ).fetchall()
    return {
        "available": True,
        "total": int(total),
        "by_action": {str(r[0]): int(r[1]) for r in action_rows},
        "by_mode": {str(r[0]): int(r[1]) for r in mode_rows},
        "by_band": {str(r[0]): int(r[1]) for r in band_rows},
    }


def audit_rows(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    limit: int | None = None,
) -> list[JsonObject]:
    """One enriched audit row per review run (the write-once trail).

    Finding/outcome counts are computed in correlated subqueries keyed on
    ``(project, iid, sha)`` so the 1-row-per-run grain is preserved — a naive
    join to ``review_findings`` would multiply rows and double-count tokens/cost.
    Governance decision fields come from a LEFT JOIN (NULL when absent).
    Ordered **newest-first** by ``(started_at, run_id)`` so a ``limit`` keeps
    the most recent activity (the relevant part of an audit) rather than the
    oldest; ordering is fully deterministic for a diff-clean report.
    """
    start, end = _report_window(since_hours, since, until)
    limit_sql = " limit ?" if limit is not None else ""
    # Only the main WHERE has placeholders; the correlated subqueries and the
    # LEFT JOIN reference columns, not binds.
    args: tuple[Any, ...] = (start, end, project, project)
    if limit is not None:
        args = (*args, int(limit))
    with connect_db(readonly=True) as db:
        has_gov = _table_exists(db, "governance_decisions")
        gov_select = (
            "g.action, g.mode" if has_gov else "null as action, null as mode"
        )
        gov_join = (
            "left join governance_decisions g on g.run_id = r.run_id" if has_gov else ""
        )
        rows = db.execute(
            f"""
            select r.run_id, r.project, r.iid, r.sha, r.started_at, r.finished_at,
                   r.status, r.model, r.review_mode, r.dry_run,
                   r.provenance_band, r.provenance_source, r.provenance_confidence,
                   r.sensitive_paths, r.tokens_total, r.cost_usd,
                   (select count(*) from review_findings f
                      where f.project=r.project and f.iid=r.iid and f.sha=r.sha),
                   (select count(*) from review_findings f
                      where f.project=r.project and f.iid=r.iid and f.sha=r.sha
                      and f.status='posted'),
                   (select count(*) from finding_outcomes o
                      where o.project=r.project and o.iid=r.iid and o.sha=r.sha
                      and o.resolved=1),
                   (select count(*) from finding_outcomes o
                      where o.project=r.project and o.iid=r.iid and o.sha=r.sha
                      and o.disputed=1),
                   (select count(*) from finding_outcomes o
                      where o.project=r.project and o.iid=r.iid and o.sha=r.sha
                      and o.false_positive=1),
                   {gov_select}
            from review_runs r
            {gov_join}
            where r.started_at >= ? and r.started_at <= ? and (? is null or r.project = ?)
            order by r.started_at desc, r.run_id desc{limit_sql}
            """,
            args,
        ).fetchall()
    out: list[JsonObject] = []
    for r in rows:
        sensitive = json.loads(r[13]) if r[13] else []
        out.append(
            {
                "run_id": r[0],
                "project": r[1],
                "iid": int(r[2]),
                "sha": r[3],
                "started_at": r[4],
                "finished_at": r[5],
                "status": r[6],
                "model": r[7],
                "review_mode": r[8],
                "dry_run": bool(r[9]),
                "provenance_band": r[10],
                "provenance_source": r[11],
                "provenance_confidence": r[12],
                "sensitive_paths_count": len(sensitive),
                "tokens_total": int(r[14]) if r[14] is not None else 0,
                "cost_usd": float(r[15]) if r[15] is not None else 0.0,
                "findings_total": int(r[16]),
                "findings_posted": int(r[17]),
                "outcomes_resolved": int(r[18]),
                "outcomes_disputed": int(r[19]),
                "outcomes_false_positive": int(r[20]),
                "policy_action": r[21],
                "policy_mode": r[22],
            }
        )
    return out


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over a non-empty, pre-sorted list.

    Deterministic and interpolation-free: the rank is
    ``ceil(fraction * n)`` clamped to ``[1, n]`` (1-based), so a fixture's
    p50/p95 land on an actual observed value rather than a float blend.
    Callers guarantee ``sorted_values`` is non-empty.
    """
    n = len(sorted_values)
    rank = max(1, min(n, math.ceil(fraction * n)))
    return sorted_values[rank - 1]


def latency_summary(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
) -> JsonObject:
    """Review-run wall-clock latency over the window (read-only).

    Considers ``review_runs`` rows with a non-null ``finished_at`` (completed
    runs); duration is ``finished_at - started_at``, both stored ISO-8601
    UTC (``+00:00``) timestamps. Raw durations are pulled via SQL and the
    percentiles computed in Python (nearest-rank, deterministic). Window is
    resolved by :func:`_report_window` over ``started_at`` to match the other
    ``review_runs`` readers.

    Returns ``{count, p50_seconds, p95_seconds, max_seconds, avg_seconds}``;
    every field is ``0`` / ``0.0`` for an empty window. Seconds are raw
    floats here — :mod:`bubo.report` rounds them at the report boundary.
    """
    start, end = _report_window(since_hours, since, until)
    where = (
        "started_at >= ? and started_at <= ? and (? is null or project = ?) "
        "and finished_at is not null"
    )
    args = (start, end, project, project)
    with connect_db(readonly=True) as db:
        rows = db.execute(
            f"select started_at, finished_at from review_runs where {where}", args
        ).fetchall()
    durations = sorted(
        (
            datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
        ).total_seconds()
        for started_at, finished_at in rows
    )
    if not durations:
        return {
            "count": 0,
            "p50_seconds": 0.0,
            "p95_seconds": 0.0,
            "max_seconds": 0.0,
            "avg_seconds": 0.0,
        }
    return {
        "count": len(durations),
        "p50_seconds": _percentile(durations, 0.50),
        "p95_seconds": _percentile(durations, 0.95),
        "max_seconds": durations[-1],
        "avg_seconds": sum(durations) / len(durations),
    }


__all__ = [
    "already_seen",
    "audit_rows",
    "connect_db",
    "count_inflight_workers",
    "disputed_class_stats",
    "disputed_finding_classes",
    "ensure_column",
    "finding_seen",
    "findings_for",
    "get_review_row",
    "governance_decision_for",
    "governance_decisions_for",
    "init_db",
    "init_dirs",
    "latency_summary",
    "latest_reviewed_row",
    "list_recent_reviews",
    "metrics_summary",
    "noise_trend",
    "outcomes_for",
    "outcomes_summary",
    "policy_decisions_summary",
    "posted_findings_for_outcome_sync",
    "prompt_version",
    "provenance_for",
    "record",
    "record_finding",
    "record_finding_outcome",
    "record_finding_outcome_sync_attempt",
    "record_governance_decision",
    "record_provenance",
    "record_review_run_finish",
    "record_review_run_start",
    "review_run_id",
    "status_age_seconds",
]
