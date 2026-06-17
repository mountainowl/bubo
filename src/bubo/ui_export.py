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
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from importlib import metadata
from pathlib import Path
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
    except (ConfigError, OSError, ValueError, TypeError):
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
        "dashboard": {"recent": [], "reports": []},
        "reviews": [],
        "reports": [],
        "config": _config_schema(cfg),
    }


@contextmanager
def _readonly_db_snapshot() -> Iterator[None]:
    """Retarget ``paths.DB`` at a private filesystem copy of the operator DB.

    Several readers we call (``list_recent_reviews``, ``findings_for``,
    ``count_inflight_workers``, …) open the *writer* connection, which runs
    ``pragma journal_mode=WAL`` — a write. Against a read-only mount that fails;
    against a writable one it would touch the operator's DB. Either way the
    failure path would be swallowed into an empty document, silently losing data
    in an auditable tool.

    So we ``copy`` the DB file (plus its ``-wal`` sidecar, which carries
    uncheckpointed rows) into a private temp dir with the filesystem, then point
    ``paths.DB`` at the copy for the build. Every reader — writer- or
    readonly-connection — then hits the throwaway copy in a *writable* temp dir,
    where it can checkpoint the WAL normally; the operator's DB is never opened
    at all. The ``-shm`` is deliberately NOT copied: SQLite rebuilds it from the
    copied ``-wal`` in the writable temp dir, and a stale copied ``-shm`` would
    be worse than none.

    A failure to *read* the source (permission denied, unreadable mount) raises
    ``OSError`` from :func:`shutil.copy2` and is left to surface — the export
    must not mask it. Concurrency note: this is a plain file copy, so a copy
    racing a live poller write can be torn; that is inherent to the
    "snapshot, not live" design and is acceptable for a read-only export.
    """
    original = paths.DB
    with tempfile.TemporaryDirectory(prefix="bubo-ui-") as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite"
        shutil.copy2(original, snapshot)
        wal = Path(f"{original}-wal")
        if wal.exists():
            shutil.copy2(wal, Path(f"{snapshot}-wal"))
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
    (permission denied) raises ``OSError`` from the snapshot copy so the CLI
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
    except sqlite3.DatabaseError:
        # The snapshot copy opened but is not a usable SQLite/bubo DB
        # (truncated, "file is not a database", pre-schema). Treat as "nothing
        # to show yet" rather than crash. NOTE: an unreadable *source* file
        # raises OSError from shutil.copy2 inside the context manager — that is
        # deliberately NOT caught here, so it propagates to the CLI (which exits
        # non-zero) instead of silently producing an empty, data-losing export.
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
            "reports": reports,
        },
        "reviews": reviews,
        "reports": reports,
        "config": _config_schema(cfg),
    }


__all__ = ["SCHEMA_VERSION", "build_data"]
