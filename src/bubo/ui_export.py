"""Build the read-only offline or live operator dashboard snapshot.

Legacy report readers and dashboard aggregates run against a coherent private
SQLite backup. No schema migration, external telemetry, update registration or
source DB writes are performed. Missing databases return an empty skeleton;
unreadable databases surface an error, and invalid schemas carry a visible
error message. Local review detail may contain repository information.
"""

from __future__ import annotations

import re
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import fields, is_dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from bubo import db, paths, report
from bubo.config_values import ConfigError
from bubo.events import now
from bubo.review_config import ReviewConfig, load_review_config
from bubo.types import JsonObject
from bubo.ui_metrics import build_metrics

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


def _health(timeout_seconds: int) -> JsonObject:
    """Derive the dashboard health pill without mutating state.

    Reimplements :func:`bubo.poller.check_health`'s verdict logic against
    :func:`bubo.db.latest_reviewed_row` so we never call the MCP
    ``health()`` (which runs ``init_db``). ``stale`` when the freshest row is
    older than ``timeout_seconds * 3`` (one cycle plus jitter), ``ok`` when
    fresh, ``empty`` on a fresh install with no rows yet.
    """
    threshold = timeout_seconds * 3
    latest = db.latest_reviewed_row()
    if latest is None:
        return {"status": "empty", "threshold_seconds": threshold}
    status, updated_at = latest
    age = db.status_age_seconds(updated_at)
    return {
        "status": "ok" if age <= threshold else "stale",
        "last_status": status,
        "last_updated_at": updated_at,
        "age_seconds": age,
        "threshold_seconds": threshold,
    }


def _review_detail(project: str, iid: int, sha: str, run: JsonObject | None) -> JsonObject:
    """Embed one review's findings + outcomes + governance + run for offline detail.

    Keyed by ``(project, iid, sha)`` so the detail matches the exact row in
    the recent-reviews list (never resolving to a different "current" SHA).
    Outcomes are folded onto each finding by ``fingerprint`` so the SPA does
    not have to join client-side. ``run`` is the matching audit row (tokens,
    cost, provenance band/source, started_at/finished_at for the timeline), or
    ``None`` if no run was recorded for this exact SHA. The readers here SELECT
    only; some use the writer connection, so :func:`build_data` runs them
    against a read-only snapshot copy of the DB (never the operator's file).
    """
    findings = db.findings_for(project, iid, sha)
    outcomes = db.outcomes_for(project, iid, sha)
    by_fp = {o["fingerprint"]: o for o in outcomes}
    enriched = [{**f, "outcome": by_fp.get(f["fingerprint"])} for f in findings]
    return {
        "findings": enriched,
        "governance": db.governance_decisions_for(project, iid, sha),
        "run": run,
    }


def _recent_reviews() -> list[JsonObject]:
    """Recent ``reviewed_mrs`` rows, each enriched with embedded detail.

    The audit trail (tokens/cost/provenance/run-span per run) is pulled ONCE
    over a wide window and indexed by ``(project, iid, sha)`` so each review's
    detail can carry its run summary without a per-review query.
    """
    rows = db.list_recent_reviews(limit=RECENT_REVIEWS_LIMIT)
    # One audit pass keyed by (project, iid, sha); newest-first means the first
    # row wins for a re-run SHA (the freshest run summary).
    runs: dict[tuple[str, int, str], JsonObject] = {}
    for audit in db.audit_rows(since_hours=24 * 366):
        key = (audit["project"], audit["iid"], audit["sha"])
        runs.setdefault(key, audit)
    return [
        {
            **row,
            "detail": _review_detail(
                row["project"],
                row["iid"],
                row["sha"],
                runs.get((row["project"], row["iid"], row["sha"])),
            ),
        }
        for row in rows
    ]


def _reports() -> list[JsonObject]:
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
                "report": report.build_report(since_hours=since_hours),
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
        "dashboard": {
            "recent": [],
            "reports": [],
            "metrics": {"inventory": {}, "windows": [], "active": []},
        },
        "reviews": [],
        "reports": [],
        "config": _config_schema(cfg),
    }


@contextmanager
def _readonly_db_snapshot() -> Iterator[None]:
    """Take a coherent SQLite online backup through a read-only connection.

    All legacy readers then operate on a private copy. A deadline bounds
    repeated busy retries while a writer is active; no source migration runs.
    """
    original = paths.DB
    with original.open("rb"):
        pass  # Preserve a clear filesystem permission error before SQLite opens it.
    with tempfile.TemporaryDirectory(prefix="bubo-ui-") as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite"
        deadline = time.monotonic() + 5

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() > deadline:
                raise OSError("Dashboard snapshot timed out; retry after the current write")

        with (
            closing(sqlite3.connect(f"{original.resolve().as_uri()}?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(snapshot)) as target,
        ):
            source.backup(target, pages=256, progress=progress, sleep=0.05)
        paths.DB = snapshot
        try:
            yield
        finally:
            paths.DB = original


def build_data() -> JsonObject:
    """Assemble the full ``data.json`` document — read-only, never mutating.

    Guards on ``paths.DB.exists()`` first: a missing DB returns the empty
    skeleton without opening any connection (so the export never creates the
    operator's database). When the DB exists it is snapshotted read-only (see
    :func:`_readonly_db_snapshot`) so the readers — including the ones that use
    the writer connection — run against a throwaway copy; this keeps the export
    genuinely non-mutating and works against a read-only mount.

    A DB that exists but is empty yields the same shape with empty aggregates.
    A DB that exists but is *unreadable as SQLite* (truncated / not a database)
    degrades to the empty skeleton; a DB file that cannot be *read at all*
    (permission denied) raises ``OSError`` before backup so the CLI
    surfaces it rather than silently emptying a populated DB.
    """
    version = _installed_version()
    if not paths.DB.exists():
        return _empty_skeleton(version)

    cfg = _load_config()
    try:
        with _readonly_db_snapshot():
            reviews = _recent_reviews()
            reports = _reports()
            health = _health(cfg.timeout_seconds)
            inflight = db.count_inflight_workers()
            metrics = build_metrics(paths.DB)
    except sqlite3.DatabaseError:
        # Preserve an explicit error for corrupt/incompatible schemas; never
        # disguise an existing unreadable database as a fresh installation.
        empty = _empty_skeleton(version)
        empty["meta"]["db_present"] = True
        empty["meta"]["error"] = (
            "The state database could not be read. Check its schema and integrity."
        )
        return empty

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
            "reports": reports,
            "metrics": metrics,
        },
        "reviews": reviews,
        "reports": reports,
        "config": _config_schema(cfg),
    }


__all__ = ["SCHEMA_VERSION", "build_data"]
