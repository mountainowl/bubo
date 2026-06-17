"""Governance report assembly + formatting (Phase 3 / Rec ③).

This is the read-only reporting layer that turns the raw governance
aggregates persisted by :mod:`bubo.db` into a single, deterministic report
document plus its JSON/CSV renderings.

Design notes:

* **SQL-free.** Nothing here touches the database directly. The module
  calls only the :mod:`bubo.db` *reader* functions (``metrics_summary``,
  ``provenance_summary``, ``outcomes_summary``, ``noise_trend``,
  ``roi_proxy``, ``latency_summary``, ``disputed_class_stats``,
  ``policy_decisions_summary``, ``audit_rows``) and the stdlib. In
  particular it MUST NOT call :func:`bubo.db.init_db` or open a
  connection: reporting is strictly read-only, and ``init_db`` would run
  DDL.

The report's top-level sections, in fixed emission order: ``meta``,
``reviews`` (with a nested ``acknowledgements`` rollup), ``provenance``,
``outcomes``, ``noise_trend``, ``roi``, ``latency``, ``dispute_classes``,
``policy_decisions``, ``audit``.
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
from bubo.errors import describe
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
    "tone",
)

NOISE_COLUMNS = (
    "day",
    "findings",
    "false_positive",
    "disputed",
    "false_positive_rate",
)

# ``would_suppress`` is always listed even though it is only populated when the
# caller supplies the operator's real thresholds: DictWriter renders a missing
# key as an empty cell, so the column order is stable for both the with-flag
# (MCP) and raw-stats (CLI) renderings.
DISPUTE_CLASS_COLUMNS = (
    "category",
    "total",
    "rejected",
    "dispute_rate",
    "would_suppress",
)

# Map of CSV-renderable section name -> its fixed column order. Membership in
# this map is what makes a section tabular; everything else is JSON-only.
_CSV_COLUMNS: dict[str, tuple[str, ...]] = {
    "audit": AUDIT_COLUMNS,
    "noise_trend": NOISE_COLUMNS,
    "dispute_classes": DISPUTE_CLASS_COLUMNS,
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


def _seconds(value: float) -> float:
    """Round a duration in seconds to 2 dp.

    Latency is a duration, not a ratio, so it does *not* go through
    :func:`_rate` (4 dp, with the divide-by-zero guard). Kept tiny and
    separate so the two rounding rules never get conflated.
    """
    return round(value, 2)


def build_report(
    *,
    since_hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    project: str | None = None,
    limit: int | None = None,
    generated_at: str | None = None,
    suppress_threshold: float | None = None,
    suppress_min_samples: int | None = None,
) -> JsonObject:
    """Assemble the full governance report from the :mod:`bubo.db` readers.

    Calls each reader once and stitches the results into a nested dict whose
    top-level sections are emitted in a fixed order: ``meta``, ``reviews``,
    ``provenance``, ``outcomes``, ``noise_trend``, ``roi``, ``latency``,
    ``dispute_classes``, ``policy_decisions``, ``audit``.

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
    * ``latency`` — :func:`bubo.db.latency_summary` over the window, seconds
      rounded to 2 dp via :func:`_seconds` (a duration, not a ratio).
    * ``dispute_classes`` — ``{classes: [...]}`` from
      :func:`bubo.db.disputed_class_stats` (per-project; empty when
      ``project is None``), each class carrying ``dispute_rate`` via
      :func:`_rate`. **Audit-integrity rule:** a bare ``suppressed: bool``
      against hardcoded thresholds would misreport reality, so the flag is
      only emitted when the caller passes the operator's real
      ``suppress_threshold``/``suppress_min_samples`` (the MCP tool reads
      them from ``[review]`` config); then each class gets a truthful
      ``would_suppress``. The flag is gated on the *raw* ``rejected/total``
      (not the rounded ``dispute_rate``) so it can never flip at a rounding
      boundary, replicating :func:`bubo.db.disputed_finding_classes` exactly.
      When thresholds are absent the section is raw stats only (no flag).

    The ``reviews`` section also carries an ``acknowledgements`` rollup
    (``{no_findings, success, failed}``) mirroring its ``by_status`` so the
    "reviewer ran and was happy" counts are first-class.

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
        Optional project filter; ``None`` means "all projects". The
        per-project ``dispute_classes`` section is empty when ``None``.
    limit:
        Optional cap on the number of ``audit`` rows (newest-window-first by
        ``started_at``); ``None`` means no cap. Only the audit trail is
        capped — the rollup sections always cover the full window.
    generated_at:
        Optional fixed timestamp for ``meta.generated_at``.
    suppress_threshold, suppress_min_samples:
        Optional operator dispute-suppression thresholds. When BOTH are
        given, each ``dispute_classes`` entry gains a truthful
        ``would_suppress`` flag; when absent the section is raw stats only.
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
    latency_raw = db.latency_summary(
        since_hours=since_hours, since=since, until=until, project=project
    )
    # Per-project only: suppression is a per-repo signal, and the reader takes a
    # required `project: str`. With no project filter there is no meaningful
    # cross-repo dispute set, so the section is empty.
    dispute_stats = (
        []
        if project is None
        else db.disputed_class_stats(project, min_samples=1)
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

    latency = {
        "count": latency_raw["count"],
        "p50_seconds": _seconds(latency_raw["p50_seconds"]),
        "p95_seconds": _seconds(latency_raw["p95_seconds"]),
        "max_seconds": _seconds(latency_raw["max_seconds"]),
        "avg_seconds": _seconds(latency_raw["avg_seconds"]),
    }

    # AUDIT-INTEGRITY: `would_suppress` is only emitted when BOTH operator
    # thresholds are supplied, and it is gated on the RAW rejected/total (the
    # exact predicate in db.disputed_finding_classes) — never on the rounded
    # `dispute_rate` — so it cannot flip at a rounding boundary.
    dispute_classes_rows: list[JsonObject] = []
    for stat in dispute_stats:
        row: JsonObject = {
            "category": stat["category"],
            "total": stat["total"],
            "rejected": stat["rejected"],
            "dispute_rate": _rate(stat["rejected"], stat["total"]),
        }
        if suppress_threshold is not None and suppress_min_samples is not None:
            row["would_suppress"] = (
                stat["total"] >= suppress_min_samples
                and stat["dispute_rate"] >= suppress_threshold
            )
        dispute_classes_rows.append(row)

    # Acknowledgements: a first-class rollup mirroring reviews.by_status. Use
    # .get(..., 0) because metrics_summary only emits statuses that occurred.
    by_status = metrics["by_status"]
    acknowledgements = {
        "no_findings": by_status.get("no_findings", 0),
        "success": by_status.get("success", 0),
        "failed": by_status.get("failed", 0),
    }
    reviews = {**metrics, "acknowledgements": acknowledgements}

    return {
        "meta": {
            "generated_at": generated_at or now(),
            "window": window,
            "project": project,
            "schema_version": SCHEMA_VERSION,
        },
        "reviews": reviews,
        "provenance": provenance,
        "outcomes": outcomes,
        "noise_trend": noise_trend,
        "roi": roi,
        "latency": latency,
        "dispute_classes": dispute_classes_rows,
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

    Only sections backed by a list-of-dicts — ``"audit"``, ``"noise_trend"``
    and ``"dispute_classes"`` — are CSV-renderable; their fixed column orders
    live in :data:`AUDIT_COLUMNS` / :data:`NOISE_COLUMNS` /
    :data:`DISPUTE_CLASS_COLUMNS`. Requesting any other section (the scalar
    rollups, which are JSON-only) raises :class:`ValueError`.

    The header is always written (even for an empty section), columns follow
    the fixed order, and extra keys are dropped (``extrasaction="ignore"``).
    Rows are written in the order the readers returned them, so the output is
    deterministic. Cell values are passed through :func:`_csv_safe` to neutralize
    spreadsheet formula injection (this is an audit export destined for Excel /
    Sheets).
    """
    columns = _CSV_COLUMNS.get(section)
    if columns is None:
        raise ValueError(
            describe(
                f"section {section!r} is not CSV-renderable",
                reason="the requested report section has no flat CSV form",
                fix="request a CSV-renderable section, or use JSON output.",
            )
        )
    rows = report[section]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: _csv_safe(value) for key, value in row.items()} for row in rows)
    return buffer.getvalue()


__all__ = [
    "AUDIT_COLUMNS",
    "DISPUTE_CLASS_COLUMNS",
    "NOISE_COLUMNS",
    "SCHEMA_VERSION",
    "build_report",
    "to_csv",
    "to_json",
]
