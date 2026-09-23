"""Runtime state writes and operational readers for Bubo's SQLite state."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bubo.db_reporting import _resolve_sha
from bubo.db_schema import connect_db, read_db
from bubo.events import now
from bubo.governance_policy import GovernanceDecision
from bubo.hash_utils import stable_digest, stable_hash
from bubo.provenance import ProvenanceSignal
from bubo.statuses import FindingStatus, ReviewMode, ReviewStatus
from bubo.telemetry import TokenUsage
from bubo.types import JsonObject


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
    tone: str = "terse",
) -> None:
    """Insert (or reset) the ``review_runs`` row at the start of a worker.

    Idempotent: a retried worker with the same ``run_id`` clears
    ``finished_at`` and ``error`` and updates the start metadata.

    ``tone`` records the active ``[review].tone`` so mood effectiveness can be
    analyzed against outcomes; it defaults to ``terse`` (the byte-identical
    house style) for callers that do not set it.
    """
    with connect_db() as db:
        db.execute(
            """
            insert into review_runs(
              run_id,project,iid,sha,status,model,prompt_version,review_mode,dry_run,
              started_at,tone
            )
            values(?,?,?,?,?,?,?,?,?,?,?)
            on conflict(run_id) do update set
              status=excluded.status,
              model=excluded.model,
              prompt_version=excluded.prompt_version,
              review_mode=excluded.review_mode,
              dry_run=excluded.dry_run,
              started_at=excluded.started_at,
              tone=excluded.tone,
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
                tone,
            ),
        )


def record_review_run_finish(
    *,
    run_id: str,
    status: ReviewStatus | str,
    tokens: TokenUsage,
    cost_usd: float,
    error: str | None,
    lines_reviewed: int = 0,
) -> None:
    """Finalize a ``review_runs`` row at the end of a worker.

    Updates token counts, cost, ``lines_reviewed`` (added lines across the
    change's diff), and the terminal status. If no row exists for ``run_id``
    (because the worker failed before :func:`record_review_run_start`), this
    is a silent no-op — the row simply never appears in telemetry rather than
    carrying partial data.
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
              lines_reviewed=?,
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
                lines_reviewed,
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


def provenance_for(
    run_id: str, *, connection: sqlite3.Connection | None = None
) -> JsonObject | None:
    """Return the persisted provenance for ``run_id``, or ``None`` if absent.

    The inverse of :func:`record_provenance`; the JSON list fields are decoded
    back to lists. Used by tests now and by Phase 3 governance reporting later.
    """
    with read_db(connection) as db:
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


def _governance_decision_row(project: str, iid: int, sha: str, row: tuple[Any, ...]) -> JsonObject:
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


def governance_decision_for(
    run_id: str, *, connection: sqlite3.Connection | None = None
) -> JsonObject | None:
    """Return the governance decision for ``run_id``, or ``None`` if absent."""
    with read_db(connection) as db:
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
    project: str,
    iid: int,
    sha: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> list[JsonObject]:
    """Return governance decisions for an MR/PR (keyed like findings_for/outcomes_for).

    When ``sha`` is ``None`` the most-recent reviewed SHA is resolved via
    :func:`_resolve_sha` so this reader agrees with the others on "current".
    """
    with read_db(connection) as db:
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
    with connect_db(readonly=True) as db:
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


def count_inflight_workers(*, connection: sqlite3.Connection | None = None) -> int:
    """Count MRs currently in ``running`` or ``queued`` status.

    See :func:`bubo.poller.poll` for how the result is used as a
    backpressure signal. Over-reports during the TTL-reap gap; that is
    the safe direction.
    """
    with read_db(connection) as db:
        row = db.execute(
            """
            select count(*) from reviewed_mrs
            where status in (?, ?)
            """,
            (ReviewStatus.RUNNING, ReviewStatus.QUEUED),
        ).fetchone()
    return int(row[0]) if row else 0


def latest_reviewed_row(*, connection: sqlite3.Connection | None = None) -> tuple[str, str] | None:
    """Return ``(status, updated_at)`` of the most recently touched MR row.

    Used by :func:`bubo.poller.check_health`. ``None`` when the
    table is empty (fresh install).
    """
    with read_db(connection) as db:
        row = db.execute(
            "select status, updated_at from reviewed_mrs order by updated_at desc limit 1"
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


def review_health(
    timeout_seconds: int, *, connection: sqlite3.Connection | None = None
) -> JsonObject:
    """Return the shared poller/UI health view without changing database state."""
    row = latest_reviewed_row(connection=connection)
    if row is None:
        return {
            "status": "empty",
            "message": "no reviews recorded yet",
            "threshold_seconds": timeout_seconds * 3,
        }
    status, updated_at = row
    age_seconds = status_age_seconds(updated_at)
    threshold_seconds = timeout_seconds * 3
    return {
        "status": "ok" if age_seconds <= threshold_seconds else "stale",
        "last_status": status,
        "last_updated_at": updated_at,
        "age_seconds": age_seconds,
        "threshold_seconds": threshold_seconds,
        "fresh": age_seconds <= threshold_seconds,
    }


def finding_seen(
    project: str,
    iid: int,
    sha: str,
    fingerprint: str,
    *,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """Return ``True`` if a finding with this fingerprint was already posted.

    Used to short-circuit re-extraction across retried worker runs at the
    same SHA.
    """
    with read_db(connection) as db:
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
    except TypeError, ValueError:
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


def posted_findings_for_outcome_sync(
    limit: int = 200, *, connection: sqlite3.Connection | None = None
) -> list[JsonObject]:
    """Return up to ``limit`` posted findings ordered by sync staleness.

    Never-synced findings come first; then oldest ``last_checked_at``.
    The poller uses this list to drive ``--sync-outcomes``.

    Each row carries ``prior_outcome`` — the currently-stored outcome flags
    (all ``False`` if never synced) — so the caller can detect the
    ``false -> true`` transition of each dimension and emit analytics exactly
    once per outcome rather than on every re-check.
    """
    with read_db(connection) as db:
        rows = db.execute(
            """
            select rf.project,rf.iid,rf.sha,rf.fingerprint,rf.discussion_id,
                   fo.reply_classified,
                   coalesce(fo.resolved,0), coalesce(fo.deleted,0),
                   coalesce(fo.developer_replied,0), coalesce(fo.disputed,0),
                   coalesce(fo.false_positive,0), coalesce(fo.duplicate,0)
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
            "prior_outcome": {
                "resolved": bool(row[6]),
                "deleted": bool(row[7]),
                "developer_replied": bool(row[8]),
                "disputed": bool(row[9]),
                "false_positive": bool(row[10]),
                "duplicate": bool(row[11]),
            },
        }
        for row in rows
    ]


# Shared dispute-class aggregation. Both the suppression-set reader
# (:func:`disputed_finding_classes`, poller path, writable connection) and the
# read-only stats reader (:func:`disputed_class_stats`, report/MCP path) run the
# EXACT same join + normalization through here so the two can never drift on what
# "a category's dispute rate" means. The helper takes an OPEN connection rather
# than opening its own, so each caller picks its own read/write mode.
