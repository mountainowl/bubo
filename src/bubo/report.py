"""Governance report assembly + formatting (Phase 3 / Rec ③).

This is the read-only reporting layer that turns the raw governance
aggregates persisted by :mod:`bubo.db` into a single, deterministic report
document plus its JSON/CSV renderings.

Design notes:

* **SQL-free.** Nothing here touches the database directly. The module
  calls only the :mod:`bubo.db` *reader* functions (``metrics_summary``,
  ``provenance_summary``, ``outcomes_summary``, ``noise_trend``,
  ``roi_proxy``, ``policy_decisions_summary``, ``audit_rows``) and the
  stdlib. In particular it MUST NOT call :func:`bubo.db.init_db` or open a
  connection: reporting is strictly read-only, and ``init_db`` would run
  DDL.
* **Deterministic.** Given the same database, the only non-deterministic
  field in the output is ``meta.generated_at`` (defaulted from
  :func:`bubo.events.now`). Every derived rate is rounded at one place
  (:func:`_rate`) so the JSON/CSV bytes are reproducible.
* **Pure.** Functions assemble and return data; they never write files.
  The CLI decides where the rendered strings go (stdout, a file, …).
* **Single rounding boundary.** Rates are computed *here*, not in the DB
  readers, so the readers stay raw-count-only and there is exactly one
  place that defines what "accept rate" means.
"""

from __future__ import annotations

import csv
import io
import json

from bubo import db
from bubo.events import now
from bubo.types import JsonObject

SCHEMA_VERSION = 1

# Fixed CSV column orders. These double as the single source of truth for
# *which* sections are tabular: ``to_csv`` rejects any section not present in
# ``_CSV_COLUMNS`` (scalar rollups are JSON-only). ``AUDIT_COLUMNS`` mirrors
# the field order of ``db.audit_rows`` rows exactly.
AUDIT_COLUMNS = (
    "run_id",
    "project",
    "iid",
    "sha",
    "started_at",
    "finished_at",
    "status",
    "model",
    "review_mode",
    "dry_run",
    "provenance_band",
    "provenance_source",
    "provenance_confidence",
    "sensitive_paths_count",
    "tokens_total",
    "cost_usd",
    "findings_total",
    "findings_posted",
    "outcomes_resolved",
    "outcomes_disputed",
    "outcomes_false_positive",
    "policy_action",
    "policy_mode",
)

NOISE_COLUMNS = (
    "day",
    "findings",
    "false_positive",
    "disputed",
    "false_positive_rate",
)

# Map of CSV-renderable section name -> its fixed column order. Membership in
# this map is what makes a section tabular; everything else is JSON-only.
_CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "audit": AUDIT_COLUMNS,
    "noise_trend": NOISE_COLUMNS,
}


def _rate(numerator: float, denominator: float) -> float:
    """Return ``numerator / denominator`` rounded to 4 dp, ``0.0`` if zero.

    This is the module's single rounding boundary: every derived rate in the
    report flows through here, so "what is a rate" is defined in exactly one
    place and the output stays byte-reproducible.
    """
    if not denominator:
        return 0.0
    return round(numerator / denominator, 4)


def build_report(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    limit: int | None = None,
    generated_at: str | None = None,
) -> JsonObject:
    """Assemble the full governance report from the :mod:`bubo.db` readers.

    Calls each reader once and stitches the results into a nested dict whose
    top-level sections are emitted in a fixed order: ``meta``, ``reviews``,
    ``provenance``, ``outcomes``, ``noise_trend``, ``roi``,
    ``policy_decisions``, ``audit``.

    Most sections pass the reader output through verbatim. The exceptions are
    the sections that carry *derived rates*, all computed here through
    :func:`_rate` (the single rounding boundary):

    * ``outcomes`` — raw counts plus ``accept_rate`` (resolved/total),
      ``dispute_rate`` (disputed/total) and ``false_positive_rate``
      (false_positive/total).
    * ``roi`` — ``roi_proxy`` counts plus ``accepted_per_usd``
      (accepted/cost_usd_sum).
    * ``noise_trend`` — each bucket plus a per-bucket ``false_positive_rate``
      (false_positive/findings).

    ``meta.generated_at`` defaults to :func:`bubo.events.now`; passing
    ``generated_at`` makes the whole document deterministic (useful for
    tests). The window arguments are forwarded to the readers unchanged.

    Parameters
    ----------
    since_hours:
        Look-back window in hours when ``since``/``until`` are not given.
    since, until:
        Optional explicit ISO-8601 window bounds.
    project:
        Optional project filter; ``None`` means "all projects".
    limit:
        Optional cap on the number of ``audit`` rows (newest-window-first by
        ``started_at``); ``None`` means no cap. Only the audit trail is
        capped — the rollup sections always cover the full window.
    generated_at:
        Optional fixed timestamp for ``meta.generated_at``.
    """
    # Pass since/until + readonly so the `reviews` section covers the SAME
    # window as every other section (not metrics_summary's legacy 30-day path).
    metrics = db.metrics_summary(
        since_hours=since_hours, project=project, since=since, until=until, readonly=True
    )
    window = {"since_hours": since_hours, "since": since, "until": until}
    provenance = db.provenance_summary(
        since_hours=since_hours, since=since, until=until, project=project
    )
    outcomes_raw = db.outcomes_summary(
        since_hours=since_hours, since=since, until=until, project=project
    )
    noise = db.noise_trend(
        since_hours=since_hours, since=since, until=until, project=project
    )
    roi_raw = db.roi_proxy(
        since_hours=since_hours, since=since, until=until, project=project
    )
    policy = db.policy_decisions_summary(
        since_hours=since_hours, since=since, until=until, project=project
    )
    audit = db.audit_rows(
        since_hours=since_hours, since=since, until=until, project=project, limit=limit
    )

    outcomes_total = outcomes_raw["total"]
    outcomes = {
        **outcomes_raw,
        "accept_rate": _rate(outcomes_raw["resolved"], outcomes_total),
        "dispute_rate": _rate(outcomes_raw["disputed"], outcomes_total),
        "false_positive_rate": _rate(outcomes_raw["false_positive"], outcomes_total),
    }

    roi = {
        **roi_raw,
        "accepted_per_usd": _rate(roi_raw["accepted"], roi_raw["cost_usd_sum"]),
    }

    noise_trend = [
        {
            **bucket,
            "false_positive_rate": _rate(bucket["false_positive"], bucket["findings"]),
        }
        for bucket in noise
    ]

    return {
        "meta": {
            "generated_at": generated_at or now(),
            "window": window,
            "project": project,
            "schema_version": SCHEMA_VERSION,
        },
        "reviews": metrics,
        "provenance": provenance,
        "outcomes": outcomes,
        "noise_trend": noise_trend,
        "roi": roi,
        "policy_decisions": policy,
        "audit": audit,
    }


def to_json(report: JsonObject) -> str:
    """Render ``report`` as deterministic, pretty-printed JSON.

    Insertion order is preserved (``sort_keys=False``) so the section order
    set by :func:`build_report` survives into the output; a trailing newline
    is appended for clean file/stream concatenation.
    """
    return (
        json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    )


# Leading characters a spreadsheet (Excel/Sheets) treats as a formula start.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """Neutralize spreadsheet formula injection in a CSV cell.

    A string cell beginning with a formula trigger (``= + - @`` / tab / CR) is
    prefixed with a single quote so Excel/Sheets render it literally instead of
    executing it. Non-string cells (ints, floats, None) pass through unchanged.
    """
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def to_csv(report: JsonObject, section: str = "audit") -> str:
    """Render one tabular ``report`` section as CSV.

    Only sections backed by a list-of-dicts — ``"audit"`` and
    ``"noise_trend"`` — are CSV-renderable; their fixed column orders live in
    :data:`AUDIT_COLUMNS` / :data:`NOISE_COLUMNS`. Requesting any other
    section (the scalar rollups, which are JSON-only) raises
    :class:`ValueError`.

    The header is always written (even for an empty section), columns follow
    the fixed order, and extra keys are dropped (``extrasaction="ignore"``).
    Rows are written in the order the readers returned them, so the output is
    deterministic. Cell values are passed through :func:`_csv_safe` to neutralize
    spreadsheet formula injection (this is an audit export destined for Excel /
    Sheets).
    """
    columns = _CSV_COLUMNS.get(section)
    if columns is None:
        raise ValueError(f"section {section!r} is not CSV-renderable")
    rows = report[section]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: _csv_safe(value) for key, value in row.items()} for row in rows)
    return buffer.getvalue()


__all__ = [
    "AUDIT_COLUMNS",
    "NOISE_COLUMNS",
    "SCHEMA_VERSION",
    "build_report",
    "to_csv",
    "to_json",
]
