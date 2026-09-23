"""Read-only aggregate reporting queries for Bubo's SQLite state."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from bubo.db_schema import connect_db, read_db
from bubo.types import JsonObject

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
    with connect_db(readonly=True) as db:
        rows = _dispute_class_rows(db, project)
    return {
        category
        for category, total, rejected in rows
        if total >= min_samples and (rejected / total) >= threshold
    }


def disputed_class_stats(
    project: str,
    *,
    min_samples: int,
    connection: sqlite3.Connection | None = None,
) -> list[JsonObject]:
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
    with read_db(connection) as db:
        rows = _dispute_class_rows(db, project)
    # Sort the raw triples (rate desc, category asc) BEFORE building dicts so the
    # sort key is concretely typed (mypy can't see into a dict[str, Any] value).
    ranked = sorted(
        ((category, total, rejected) for category, total, rejected in rows if total >= min_samples),
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
    *,
    connection: sqlite3.Connection | None = None,
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
    with read_db(connection) as db:
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


def get_review_row(
    project: str,
    iid: int,
    sha: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> JsonObject | None:
    """Return one ``reviewed_mrs`` row, or ``None`` if no match.

    When ``sha`` is ``None``, the row with the freshest ``updated_at`` for
    ``(project, iid)`` is returned — useful for "what's the current state
    of MR <iid>" without first looking up the SHA.
    """
    with read_db(connection) as db:
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


def findings_for(
    project: str,
    iid: int,
    sha: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> list[JsonObject]:
    """Return one ``review_findings`` row per finding for an MR/PR.

    See :func:`bubo.mcp_server.get_findings` for the public
    contract. When ``sha`` is ``None`` we resolve to the most recent
    reviewed SHA via :func:`_resolve_sha`.
    """
    with read_db(connection) as db:
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


def outcomes_for(
    project: str,
    iid: int,
    sha: str | None = None,
    *,
    connection: sqlite3.Connection | None = None,
) -> list[JsonObject]:
    """Return one ``finding_outcomes`` row per finding for an MR/PR.

    Empty list when ``--sync-outcomes`` has not yet run for the target —
    that is not an error condition.
    """
    with read_db(connection) as db:
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


def review_details_for(
    reviews: Sequence[tuple[str, int, str]],
    *,
    connection: sqlite3.Connection,
) -> dict[tuple[str, int, str], JsonObject]:
    """Load findings, outcomes, and governance for many exact review SHAs."""
    unique = list(dict.fromkeys(reviews))
    details: dict[tuple[str, int, str], JsonObject] = {
        key: {"findings": [], "governance": []} for key in unique
    }
    if not unique:
        return details

    placeholders = ",".join("(?,?,?)" for _ in unique)
    params = tuple(value for key in unique for value in key)
    with read_db(connection) as db:
        finding_rows = db.execute(
            f"""
            select project,iid,sha,fingerprint,file,line,status,discussion_id,
                   body,updated_at,run_id,type,severity,category,confidence,
                   note_id,verified
            from review_findings
            where (project,iid,sha) in ({placeholders})
            order by project, iid, sha, updated_at asc
            """,
            params,
        ).fetchall()
        outcome_rows = db.execute(
            f"""
            select project,iid,sha,fingerprint,discussion_id,resolved,deleted,
                   developer_replied,disputed,false_positive,duplicate,
                   resolved_at,merged_unresolved,reply_classified,last_checked_at
            from finding_outcomes
            where (project,iid,sha) in ({placeholders})
            order by project, iid, sha, last_checked_at desc
            """,
            params,
        ).fetchall()
        governance_rows = db.execute(
            f"""
            select project,iid,sha,run_id,mode,action,triggered,matched_rule,
                   rigor_injected,band,sensitive_paths,reason,created_at
            from governance_decisions
            where (project,iid,sha) in ({placeholders})
            order by project, iid, sha, created_at asc
            """,
            params,
        ).fetchall()

    outcome_by_fingerprint: dict[tuple[str, int, str, str], JsonObject] = {}
    for row in outcome_rows:
        key = (str(row[0]), int(row[1]), str(row[2]))
        outcome_by_fingerprint[(*key, str(row[3]))] = {
            "project": key[0],
            "iid": key[1],
            "sha": key[2],
            "fingerprint": row[3],
            "discussion_id": row[4],
            "resolved": bool(row[5]),
            "deleted": bool(row[6]),
            "developer_replied": bool(row[7]),
            "disputed": bool(row[8]),
            "false_positive": bool(row[9]),
            "duplicate": bool(row[10]),
            "resolved_at": row[11],
            "merged_unresolved": bool(row[12]),
            "reply_classified": bool(row[13]),
            "last_checked_at": row[14],
        }
    for row in finding_rows:
        key = (str(row[0]), int(row[1]), str(row[2]))
        finding: JsonObject = {
            "project": key[0],
            "iid": key[1],
            "sha": key[2],
            "fingerprint": row[3],
            "file": row[4],
            "line": row[5],
            "status": row[6],
            "discussion_id": row[7],
            "body": row[8],
            "updated_at": row[9],
            "run_id": row[10],
            "type": row[11],
            "severity": row[12],
            "category": row[13],
            "confidence": row[14],
            "note_id": row[15],
            "verified": row[16],
        }
        finding["outcome"] = outcome_by_fingerprint.get((*key, str(row[3])))
        details[key]["findings"].append(finding)
    for row in governance_rows:
        key = (str(row[0]), int(row[1]), str(row[2]))
        details[key]["governance"].append(
            {
                "run_id": row[3],
                "project": key[0],
                "iid": key[1],
                "sha": key[2],
                "mode": row[4],
                "action": row[5],
                "triggered": bool(row[6]),
                "matched_rule": row[7],
                "rigor_injected": bool(row[8]),
                "band": row[9],
                "sensitive_paths": json.loads(row[10]) if row[10] else [],
                "reason": row[11],
                "created_at": row[12],
            }
        )
    return details


def metrics_summary(
    since_hours: int = 24,
    project: str | None = None,
    *,
    since: str | None = None,
    until: str | None = None,
    readonly: bool = True,
    connection: sqlite3.Connection | None = None,
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
    context = read_db(connection) if connection is not None else connect_db(readonly=readonly)
    with context as db:
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
# Internal batching only: callers still receive one complete ordered audit list.
# This stays small enough that SQLite materializes a page rather than a whole
# history in one cursor fetch.
_AUDIT_PAGE_SIZE = 500


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
    connection: sqlite3.Connection | None = None,
) -> JsonObject:
    """Counts of review runs by provenance band and source within the window."""
    start, end = _report_window(since_hours, since, until)
    where = "started_at >= ? and started_at <= ? and (? is null or project = ?)"
    args = (start, end, project, project)
    with read_db(connection) as db:
        runs_total = db.execute(f"select count(*) from review_runs where {where}", args).fetchone()[
            0
        ]
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
    connection: sqlite3.Connection | None = None,
) -> JsonObject:
    """Raw finding-outcome counts within the window (rates derived in report)."""
    start, end = _report_window(since_hours, since, until)
    with read_db(connection) as db:
        if project is None:
            row = db.execute(
                """
            select count(*),
                   coalesce(sum(resolved),0), coalesce(sum(disputed),0),
                   coalesce(sum(false_positive),0), coalesce(sum(duplicate),0),
                   coalesce(sum(developer_replied),0), coalesce(sum(merged_unresolved),0),
                   coalesce(sum(deleted),0)
            from finding_outcomes where last_checked_at >= ? and last_checked_at <= ?
            """,
                (start, end),
            ).fetchone()
        else:
            row = db.execute(
                """
            select count(*),
                   coalesce(sum(resolved),0), coalesce(sum(disputed),0),
                   coalesce(sum(false_positive),0), coalesce(sum(duplicate),0),
                   coalesce(sum(developer_replied),0), coalesce(sum(merged_unresolved),0),
                   coalesce(sum(deleted),0)
            from finding_outcomes
            where project=? and last_checked_at >= ? and last_checked_at <= ?
            """,
                (project, start, end),
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
    connection: sqlite3.Connection | None = None,
) -> list[JsonObject]:
    """Per-day finding/false-positive/dispute counts (ascending by day)."""
    start, end = _report_window(since_hours, since, until)
    where = "last_checked_at >= ? and last_checked_at <= ? and (? is null or project = ?)"
    with read_db(connection) as db:
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
    connection: sqlite3.Connection | None = None,
) -> JsonObject:
    """Bug-catch ROI proxy: accepted findings + cost over the window.

    ``accepted`` = findings whose outcome is resolved and neither disputed nor
    false-positive. Joins findings to outcomes on the composite finding id
    (same join used elsewhere). ``cost_usd_sum`` comes from ``review_runs``.
    """
    start, end = _report_window(since_hours, since, until)
    fwhere = "rf.updated_at >= ? and rf.updated_at <= ? and (? is null or rf.project = ?)"
    args = (start, end, project, project)
    with read_db(connection) as db:
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
    connection: sqlite3.Connection | None = None,
) -> JsonObject:
    """Governance-decision counts by action/mode/band within the window.

    Degrades gracefully (``available: False``) if the ``governance_decisions``
    table is absent — e.g. a DB created before Phase 2 — so reporting never
    hard-fails on a partially-migrated install.
    """
    start, end = _report_window(since_hours, since, until)
    if project is None:
        where = "created_at >= ? and created_at <= ?"
        args: tuple[object, ...] = (start, end)
    else:
        where = "project = ? and created_at >= ? and created_at <= ?"
        args = (project, start, end)
    empty = {"available": False, "total": 0, "by_action": {}, "by_mode": {}, "by_band": {}}
    with read_db(connection) as db:
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
    connection: sqlite3.Connection | None = None,
) -> list[JsonObject]:
    """One enriched audit row per review run (the write-once trail).

    Finding/outcome counts are aggregated by ``(project, iid, sha)`` before
    joining the current page, so the 1-row-per-run grain is preserved — a naive
    join to ``review_findings`` would multiply rows and double-count tokens/cost.
    Governance decision fields come from a LEFT JOIN (NULL when absent).
    Ordered **newest-first** by ``(started_at, run_id)`` so a ``limit`` keeps
    the most recent activity (the relevant part of an audit) rather than the
    oldest; ordering is fully deterministic for a diff-clean report.
    """
    start, end = _report_window(since_hours, since, until)
    remaining = None if limit is None else max(0, int(limit))
    if remaining == 0:
        return []
    out: list[JsonObject] = []
    cursor: tuple[str, str] | None = None
    with read_db(connection) as db:
        has_gov = _table_exists(db, "governance_decisions")
        gov_select = "g.action, g.mode" if has_gov else "null as action, null as mode"
        gov_join = "left join governance_decisions g on g.run_id = r.run_id" if has_gov else ""
        while remaining is None or remaining > 0:
            page_size = _AUDIT_PAGE_SIZE if remaining is None else min(_AUDIT_PAGE_SIZE, remaining)
            cursor_clause = ""
            cursor_args: tuple[Any, ...] = ()
            if cursor is not None:
                cursor_clause = " and (r.started_at < ? or (r.started_at = ? and r.run_id < ?))"
                cursor_args = (cursor[0], cursor[0], cursor[1])
            rows = db.execute(
                f"""
            with selected_runs as (
              select r.*
              from review_runs r
              where r.started_at >= ? and r.started_at <= ?
                and (? is null or r.project = ?){cursor_clause}
              order by r.started_at desc, r.run_id desc
              limit ?
            ), finding_counts as (
              select project, iid, sha,
                     count(*) as findings_total,
                     sum(status = 'posted') as findings_posted
              from review_findings
              where (project, iid, sha) in (
                select project, iid, sha from selected_runs
              )
              group by project, iid, sha
            ), outcome_counts as (
              select project, iid, sha,
                     sum(resolved = 1) as outcomes_resolved,
                     sum(disputed = 1) as outcomes_disputed,
                     sum(false_positive = 1) as outcomes_false_positive
              from finding_outcomes
              where (project, iid, sha) in (
                select project, iid, sha from selected_runs
              )
              group by project, iid, sha
            )
            select r.run_id, r.project, r.iid, r.sha, r.started_at, r.finished_at,
                   r.status, r.model, r.review_mode, r.dry_run,
                   r.provenance_band, r.provenance_source, r.provenance_confidence,
                   r.sensitive_paths, r.tokens_total, r.cost_usd,
                   coalesce(f.findings_total, 0), coalesce(f.findings_posted, 0),
                   coalesce(o.outcomes_resolved, 0), coalesce(o.outcomes_disputed, 0),
                   coalesce(o.outcomes_false_positive, 0),
                   {gov_select},
                   r.tone
            from selected_runs r
            {gov_join}
            left join finding_counts f
              on f.project=r.project and f.iid=r.iid and f.sha=r.sha
            left join outcome_counts o
              on o.project=r.project and o.iid=r.iid and o.sha=r.sha
            order by r.started_at desc, r.run_id desc
            """,
                (start, end, project, project, *cursor_args, page_size),
            ).fetchall()
            if not rows:
                break
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
                        "tone": r[23] or "terse",
                    }
                )
            cursor = (str(rows[-1][4]), str(rows[-1][0]))
            if remaining is not None:
                remaining -= len(rows)
            if len(rows) < page_size:
                break
    return out


def audit_rows_count(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Return the full audit-run count for the same window as :func:`audit_rows`.

    Kept separate from the paged enrichment query so a bounded report can say
    how much history it omitted without materializing the omitted rows.
    """
    start, end = _report_window(since_hours, since, until)
    with read_db(connection) as db:
        row = db.execute(
            "select count(*) from review_runs "
            "where started_at >= ? and started_at <= ? "
            "and (? is null or project = ?)",
            (start, end, project, project),
        ).fetchone()
    return int(row[0]) if row is not None else 0


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
    connection: sqlite3.Connection | None = None,
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
    with read_db(connection) as db:
        rows = db.execute(
            f"select started_at, finished_at from review_runs where {where}", args
        ).fetchall()
    durations = sorted(
        (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
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
