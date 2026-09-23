"""Build the static ``data.json`` snapshot the operator UI renders.

This is the read-only data layer for ``bubo ui-export`` (see
:func:`bubo.cli.cmd_ui_export`). It assembles a single self-contained JSON
document from the existing :mod:`bubo.report` + :mod:`bubo.db` *reader*
functions plus the installed package version. The static Svelte SPA
``fetch()``es this file (or reads it from an inlined ``window.__BUBO_DATA__``
global on ``file://``), so **everything the UI needs must be precomputed
here** — there is no server to re-query.

Design rules, mirroring :mod:`bubo.report`:

* **Read-only and non-mutating.** The builder calls only reader functions
  and never :func:`bubo.db.init_db`. On a *missing* DB it short-circuits to
  a valid empty skeleton **without opening any connection** (the writer-mode
  readers would otherwise create an empty DB file on the operator's disk).
  On an *existing* DB it reads from a throwaway read-only snapshot copy (see
  :func:`_readonly_db_snapshot`), so the operator's file is never opened
  read-write and a read-only mount works. On a missing ``env.toml`` it falls
  back to :class:`ReviewConfig` defaults for the read-only config display.
* **No network.** ``version.installed`` comes from
  :func:`importlib.metadata.version`; ``version.update`` is always ``None``
  (the design's PyPI update check is a later, online phase).
* **Deterministic shape.** The top-level keys are always present and in a
  fixed order regardless of whether the DB exists, is empty, or is
  populated, so the SPA can rely on the shape.

The document's top-level sections, in fixed emission order: ``meta``,
``version``, ``health``, ``inflight``, ``dashboard``, ``reviews``,
``reports``, ``config``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import fields, is_dataclass
from importlib import metadata
from typing import Any

from bubo import db, paths, report
from bubo.config_values import ConfigError
from bubo.events import now
from bubo.review_config import ReviewConfig, load_review_config
from bubo.types import JsonObject

SCHEMA_VERSION = 1

# Windows the Reports view offers, precomputed because a static page cannot
# re-query. ``(label, since_hours)`` — "today" is the trailing 24h window.
REPORT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("today", 24),
    ("7d", 24 * 7),
    ("30d", 24 * 30),
)

# How many recent reviews to embed (with their full finding/outcome detail)
# so the Reviews detail view works fully offline from the snapshot.
RECENT_REVIEWS_LIMIT = 50


def _installed_version() -> str:
    """Return the installed ``bubo`` version, or ``"unknown"`` if unresolvable.

    An editable/source checkout that was never ``pip install``ed has no
    distribution metadata; degrade to a string rather than crash the export.
    """
    try:
        return metadata.version("bubo")
    except metadata.PackageNotFoundError:
        return "unknown"


def _health(timeout_seconds: int, connection: sqlite3.Connection) -> JsonObject:
    """Return the shared poller/UI health calculation without mutating state."""
    return db.review_health(timeout_seconds, connection=connection)


def _recent_reviews(
    connection: sqlite3.Connection, audit_history: list[JsonObject]
) -> list[JsonObject]:
    """Recent ``reviewed_mrs`` rows, each enriched with embedded detail.

    The audit trail (tokens/cost/provenance/run-span per run) is pulled ONCE
    over a wide window and indexed by ``(project, iid, sha)`` so each review's
    detail can carry its run summary without a per-review query.
    """
    rows = db.list_recent_reviews(limit=RECENT_REVIEWS_LIMIT, connection=connection)
    review_keys = [(row["project"], row["iid"], row["sha"]) for row in rows]
    details = db.review_details_for(review_keys, connection=connection)
    # One audit pass keyed by (project, iid, sha); newest-first means the first
    # row wins for a re-run SHA (the freshest run summary).
    runs: dict[tuple[str, int, str], JsonObject] = {}
    for audit in audit_history:
        key = (audit["project"], audit["iid"], audit["sha"])
        runs.setdefault(key, audit)
    return [
        {
            **row,
            "detail": {
                **details[(row["project"], row["iid"], row["sha"])],
                "run": runs.get((row["project"], row["iid"], row["sha"])),
            },
        }
        for row in rows
    ]


def _reports(connection: sqlite3.Connection, audit_history: list[JsonObject]) -> list[JsonObject]:
    """Precompute the full report for each fixed reporting window.

    Each entry is ``{label, since_hours, report}`` where ``report`` is the
    full :func:`bubo.report.build_report` document for that window (all
    projects). The exec-rollup preset in the UI is a view over these.
    """
    out: list[JsonObject] = []
    for label, since_hours in REPORT_WINDOWS:
        out.append(
            {
                "label": label,
                "since_hours": since_hours,
                "report": report.build_report(
                    since_hours=since_hours,
                    connection=connection,
                    audit_history=audit_history,
                    limit=1000,
                ),
            }
        )
    return out


# Per-field rendering for the config display. ``ReviewConfig`` fields whose
# value is a nested config dataclass are summarized rather than dumped, so the
# read-only config view stays a flat, scannable name/value/description table.
_NESTED_CONFIG_FIELDS = {"telemetry_config", "governance_config"}


def _config_value(value: Any) -> Any:
    """Coerce a config value into a JSON-serializable display form.

    Scalars/lists/dicts pass through; a nested dataclass (the telemetry /
    governance blocks) is flattened to its public attrs (recursing, so a
    dataclass-of-dataclass still serializes). Anything else falls back to
    ``str()`` so the export never raises on an unexpected field type.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_config_value(item) for item in value]
    if isinstance(value, dict):
        # Keys are config names (always str); values may be nested dataclasses
        # (e.g. telemetry pricing -> ModelPricing), so recurse on them.
        return {str(k): _config_value(v) for k, v in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _config_value(getattr(value, f.name))
            for f in fields(value)
            if not f.name.startswith("_")
        }
    return str(value)


def _config_schema(cfg: ReviewConfig) -> list[JsonObject]:
    """Build the read-only config schema: name, value, default, description.

    Descriptions come from the ``ReviewConfig`` dataclass attribute docstrings
    (the design's "every name/value pair shows what it does"). We read them
    from the class ``__doc__`` Attributes section so the UI never has to know
    about config internals. Editing is explicitly out of scope (a later server
    phase); this section is display-only.
    """
    docs = _attr_docs(ReviewConfig)
    defaults = ReviewConfig()
    rows: list[JsonObject] = []
    for f in fields(cfg):
        if f.name.startswith("_"):
            continue
        rows.append(
            {
                "name": f.name,
                "value": _config_value(getattr(cfg, f.name)),
                "default": _config_value(getattr(defaults, f.name)),
                "nested": f.name in _NESTED_CONFIG_FIELDS,
                "description": _plain_text(docs.get(f.name, "")),
            }
        )
    return rows


def _plain_text(text: str) -> str:
    """Strip RST inline markup (``code``, :func:`x`) from a docstring fragment.

    The ``ReviewConfig`` docstrings are written in RST; the read-only config
    view shows plain text, so collapse ``` ``literal`` ``` to ``literal`` and
    drop role prefixes like ``:func:`` / ``:class:`` / ``:data:`` / ``:mod:``.
    """
    if not text:
        return ""
    # :role:`target` -> target ; ``code`` -> code.
    text = re.sub(r":[a-z]+:`([^`]+)`", r"\1", text)
    text = re.sub(r"``([^`]+)``", r"\1", text)
    return text.replace("`", "").strip()


def _attr_docs(cls: type) -> dict[str, str]:
    """Parse ``name:`` / indented-body pairs from a class docstring's Attributes.

    ``ReviewConfig`` documents every field in a NumPy-style ``Attributes``
    block (``name:`` on its own line, description indented below). We extract a
    ``{field: description}`` map so the read-only config view can show what each
    setting does without duplicating the text. Best-effort: any field we cannot
    parse simply gets an empty description.
    """
    doc = cls.__doc__ or ""
    lines = doc.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Attributes")
    except StopIteration:
        return {}
    out: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    # Skip the "Attributes" header and its "----------" underline.
    for raw in lines[start + 2 :]:
        line = raw.strip()
        # A "name:" line (single token then a colon) starts a field; an indented
        # description follows on the lines below it.
        if line.endswith(":") and not line.startswith("-") and " " not in line[:-1]:
            if current is not None:
                out[current] = " ".join(body).strip()
            current = line[:-1]
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        out[current] = " ".join(body).strip()
    return out


def _load_config() -> ReviewConfig:
    """Load the operator config for display, falling back to defaults.

    A missing or invalid ``env.toml`` must not break a read-only export — the
    config view is informational. Any load error degrades to the dataclass
    defaults (which is exactly what the runtime would use for those fields).
    """
    try:
        return load_review_config(paths.CONFIG)
    except ConfigError, OSError, ValueError, TypeError:
        # Display-only: a missing/invalid env.toml must never fail a read-only
        # export. Degrade to the dataclass defaults (what the runtime uses for
        # those fields anyway).
        return ReviewConfig()


def _empty_skeleton(version: str) -> JsonObject:
    """A valid, well-formed data.json for a missing/uninitialized DB.

    Same top-level keys (and same fixed order) as the populated document so
    the SPA renders an honest "no data yet" state instead of crashing. The
    config section still shows defaults so a fresh install can preview the
    settings view. Critically this path opens **no** DB connection.
    """
    cfg = _load_config()
    return {
        "meta": {
            "generated_at": now(),
            "schema_version": SCHEMA_VERSION,
            "db_present": False,
        },
        "version": {"installed": version, "update": None},
        "health": {"status": "empty", "threshold_seconds": cfg.timeout_seconds * 3},
        "inflight": 0,
        "dashboard": {"recent": []},
        "reviews": [],
        "reports": [],
        "config": _config_schema(cfg),
    }


def build_data() -> JsonObject:
    """Assemble the full ``data.json`` document — read-only, never mutating.

    Guards on ``paths.DB.exists()`` first: a missing DB returns the empty
    skeleton without opening any connection (so the export never creates the
    operator's database). When the DB exists one SQLite read-only connection is
    shared by review/detail queries, so the export never copies database files
    or retargets global paths.

    A DB that exists but is empty yields the same shape with empty aggregates.
    A DB that is unreadable or malformed degrades to the empty skeleton.
    """
    version = _installed_version()
    if not paths.DB.exists():
        return _empty_skeleton(version)
    # SQLite may report permission denial as the generic DatabaseError. Surface
    # a clearly unreadable file instead of silently producing an empty export.
    if paths.DB.stat().st_mode & 0o444 == 0:
        raise PermissionError(paths.DB)

    cfg = _load_config()
    try:
        with db.connect_db(readonly=True) as connection:
            # Pin every reader below to one SQLite snapshot. Without an
            # explicit transaction, independent SELECTs can observe a writer
            # that commits midway through the static export.
            connection.execute("begin")
            audit_history = db.audit_rows(since_hours=24 * 366, connection=connection)
            reviews = _recent_reviews(connection, audit_history)
            reports = _reports(connection, audit_history)
            # Reports/review details retain only rows they expose; release the
            # preload container before assembling the export document.
            del audit_history
            health = _health(cfg.timeout_seconds, connection)
            inflight = db.count_inflight_workers(connection=connection)
    except sqlite3.DatabaseError:
        # The read-only DB was not a usable SQLite/bubo DB
        # (truncated, "file is not a database", pre-schema). Treat as "nothing
        # to show yet" rather than crash.
        return _empty_skeleton(version)

    return {
        "meta": {
            "generated_at": now(),
            "schema_version": SCHEMA_VERSION,
            "db_present": True,
        },
        "version": {"installed": version, "update": None},
        "health": health,
        "inflight": inflight,
        # The dashboard is a curated slice the SPA can render without scanning
        # the full lists: the most recent reviews + the report windows.
        "dashboard": {
            "recent": reviews[:10],
        },
        "reviews": reviews,
        "reports": reports,
        "config": _config_schema(cfg),
    }


__all__ = ["SCHEMA_VERSION", "build_data"]
