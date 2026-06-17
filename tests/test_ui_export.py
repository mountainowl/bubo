"""Tests for ``bubo ui-export`` and the :mod:`bubo.ui_export` data builder.

Covers the three contracts the static-UI export must hold:

* **Shape.** ``data.json`` always carries the same fixed top-level keys,
  whether the DB is missing, empty-but-initialized, or populated — the SPA
  relies on the shape.
* **Empty-DB safety.** A missing DB and an init'd-empty DB both produce a
  valid, well-formed file, and the missing-DB path must NOT create the
  operator's database on disk.
* **CLI.** ``cmd_ui_export`` respects ``--out``, writes ``data.json`` +
  ``data.js`` (the ``file://`` fallback), and copies the SPA when present.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bubo import cli, db, paths, ui_export

# The contract the SPA depends on: these keys are always present, in any DB state.
_TOP_LEVEL_KEYS = {
    "meta",
    "version",
    "health",
    "inflight",
    "dashboard",
    "reviews",
    "reports",
    "config",
}


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-target paths so the readers/export point under tmp_path, not $HOME."""
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(paths, "CONFIG", tmp_path / "config" / "env.toml")
    state = tmp_path / "var"
    monkeypatch.setattr(paths, "DB", state / "state" / "reviewer.sqlite")
    monkeypatch.setattr(paths, "WORK", state / "work")
    monkeypatch.setattr(paths, "REPORTS", state / "reports")
    monkeypatch.setattr(paths, "JOBS", state / "jobs")
    monkeypatch.setattr(paths, "LOGS", state / "log")
    monkeypatch.setattr(paths, "RENDERED_PROMPTS", state / "rendered-prompts")
    return tmp_path


def _seed_one_review() -> None:
    """Insert a minimal populated window: one run, one finding, one outcome."""
    when = "2026-06-16T12:00:00+00:00"
    with sqlite3.connect(paths.DB) as con:
        con.execute(
            "insert into reviewed_mrs(project,iid,sha,status,updated_at) values(?,?,?,?,?)",
            ("g/r", 1, "sha1", "success", when),
        )
        con.execute(
            """insert into review_runs(run_id,project,iid,sha,status,dry_run,
               started_at,finished_at,tokens_total,cost_usd)
               values(?,?,?,?,?,?,?,?,?,?)""",
            ("run1", "g/r", 1, "sha1", "success", 1, when, when, 1000, 0.5),
        )
        con.execute(
            """insert into review_findings(project,iid,sha,fingerprint,file,line,
               status,body,updated_at,severity,category)
               values(?,?,?,?,?,?,?,?,?,?,?)""",
            ("g/r", 1, "sha1", "fp0", "f.py", 1, "posted", "**Issue**: bug", when,
             "blocking", "correctness"),
        )
        con.execute(
            """insert into finding_outcomes(finding_id,project,iid,sha,fingerprint,
               resolved,last_checked_at) values(?,?,?,?,?,?,?)""",
            ("g/r:1:sha1:fp0", "g/r", 1, "sha1", "fp0", 1, when),
        )


# ---------------------------------------------------------------------------
# build_data shape — same keys in every DB state
# ---------------------------------------------------------------------------


def test_build_data_missing_db_has_full_shape(isolated_root: Path) -> None:
    # No init — DB file does not exist.
    assert not paths.DB.exists()

    data = ui_export.build_data()

    assert set(data.keys()) == _TOP_LEVEL_KEYS
    assert data["meta"]["db_present"] is False
    assert data["reviews"] == []
    assert data["health"]["status"] == "empty"
    # Config schema still renders defaults so a fresh install previews settings.
    assert any(row["name"] == "min_confidence" for row in data["config"])


def test_build_data_missing_db_does_not_create_db(isolated_root: Path) -> None:
    # The single most important read-only invariant: exporting against a
    # never-initialized install must not write the operator's database.
    ui_export.build_data()
    assert not paths.DB.exists()


def test_build_data_empty_initialized_db_matches_missing_shape(isolated_root: Path) -> None:
    db.init_db()  # creates an empty-but-valid DB
    assert paths.DB.exists()

    data = ui_export.build_data()

    # Same top-level keys as the missing-DB path (the advisor's identical-shape
    # invariant); only db_present flips.
    assert set(data.keys()) == _TOP_LEVEL_KEYS
    assert data["meta"]["db_present"] is True
    assert data["reviews"] == []
    assert data["inflight"] == 0
    # Reports windows are always present, even when empty.
    assert {r["label"] for r in data["reports"]} == {"today", "7d", "30d"}


def test_build_data_populated_db_carries_review_detail(isolated_root: Path) -> None:
    db.init_db()
    _seed_one_review()

    data = ui_export.build_data()

    assert set(data.keys()) == _TOP_LEVEL_KEYS
    assert data["meta"]["db_present"] is True
    assert len(data["reviews"]) == 1
    review = data["reviews"][0]
    assert review["project"] == "g/r"
    # Embedded detail so the Reviews detail view works offline.
    finding = review["detail"]["findings"][0]
    assert finding["category"] == "correctness"
    # Outcome folded onto the finding by fingerprint.
    assert finding["outcome"]["resolved"] is True


def test_build_data_config_descriptions_populated(isolated_root: Path) -> None:
    data = ui_export.build_data()
    by_name = {row["name"]: row for row in data["config"]}
    # min_confidence is documented in the ReviewConfig docstring; the schema
    # must surface that description for the read-only config view.
    assert "confidence" in by_name["min_confidence"]["description"].lower()
    assert by_name["min_confidence"]["default"] == 0.85


# ---------------------------------------------------------------------------
# CLI — --out respected, file:// fallback written, JSON valid
# ---------------------------------------------------------------------------


def test_cmd_ui_export_writes_valid_json_to_out(isolated_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "custom-out"
    args = cli.build_parser().parse_args(
        ["ui-export", "--root", str(isolated_root), "--out", str(out)]
    )

    rc = cli.cmd_ui_export(args)

    assert rc == 0
    data_json = out / "data.json"
    assert data_json.is_file()
    parsed = json.loads(data_json.read_text())
    assert set(parsed.keys()) == _TOP_LEVEL_KEYS


def test_cmd_ui_export_writes_file_protocol_fallback(isolated_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    cli.cmd_ui_export(
        cli.build_parser().parse_args(["ui-export", "--root", str(isolated_root), "--out", str(out)])
    )
    # data.js inlines the same payload as a window global for file:// loads.
    data_js = (out / "data.js").read_text()
    assert data_js.startswith("window.__BUBO_DATA__ = ")
    # The inlined object parses back to the same top-level shape.
    inline = data_js[len("window.__BUBO_DATA__ = ") :].rstrip().rstrip(";")
    assert set(json.loads(inline).keys()) == _TOP_LEVEL_KEYS


def test_cmd_ui_export_copies_spa_assets(isolated_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    cli.cmd_ui_export(
        cli.build_parser().parse_args(["ui-export", "--root", str(isolated_root), "--out", str(out)])
    )
    # The built SPA ships committed under ui/dist (editable fallback) / the
    # wheel's bubo/_assets/ui — either way index.html lands next to data.json.
    assert (out / "index.html").is_file()


def test_cmd_ui_export_does_not_create_db_on_fresh_root(
    isolated_root: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    cli.cmd_ui_export(
        cli.build_parser().parse_args(["ui-export", "--root", str(isolated_root), "--out", str(out)])
    )
    # Read-only: exporting must never initialize the operator's DB.
    assert not paths.DB.exists()


# ---------------------------------------------------------------------------
# Read-only DB: a populated but read-only DB must export populated, not empty
# (the silent-data-loss regression), and the operator DB stays untouched.
# ---------------------------------------------------------------------------


def test_build_data_reads_readonly_db_with_hot_wal(isolated_root: Path) -> None:
    # Regression for silent data loss against a read-only mount. The trap: if
    # the export only copies the main .sqlite (not -wal), rows that live ONLY in
    # an uncheckpointed WAL vanish. So we force a HOT WAL — a writer connection
    # with autocheckpoint disabled, kept OPEN (closing it would checkpoint) —
    # then lock the DB read-only and assert the WAL rows survive the export.
    import stat

    db.init_db()
    keep_open = sqlite3.connect(paths.DB, timeout=30)
    keep_open.execute("pragma journal_mode=WAL")
    keep_open.execute("pragma wal_autocheckpoint=0")  # rows stay in -wal
    keep_open.execute(
        "insert into reviewed_mrs(project,iid,sha,status,updated_at) values(?,?,?,?,?)",
        ("wal/repo", 99, "walsha", "success", "2026-06-16T12:00:00+00:00"),
    )
    keep_open.commit()

    db_dir = paths.DB.parent
    db_before = (paths.DB.read_bytes(), paths.DB.stat().st_mtime_ns)
    # Lock the DB file + directory read-only, simulating a RO mount.
    paths.DB.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    db_dir.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
    try:
        data = ui_export.build_data()
        # Capture the main-DB state the moment the export returns, while
        # ``keep_open`` is STILL OPEN. This isolates the export's effect: closing
        # the last writer connection (in the finally below) checkpoints the hot
        # WAL back into the main file, rewriting its bytes and mtime — a mutation
        # by SQLite's close, NOT by the export. Asserting after close conflated
        # the two and was platform-dependent (passed on macOS, failed on Linux).
        db_after = (paths.DB.read_bytes(), paths.DB.stat().st_mtime_ns)
    finally:
        db_dir.chmod(0o755)
        paths.DB.chmod(0o644)
        keep_open.close()

    # The WAL-only row must appear (proves -wal was copied), not silently lost.
    assert data["meta"]["db_present"] is True
    assert any(r["project"] == "wal/repo" for r in data["reviews"])
    # The operator's main DB file is byte-for-byte unchanged BY THE EXPORT
    # (readers ran against a throwaway copy in a writable temp dir, never the
    # original). Compared at db_after — before close() can checkpoint.
    assert db_after == db_before


def test_build_data_empty_skeleton_on_corrupt_db(isolated_root: Path) -> None:
    # A file that exists but is not a SQLite DB degrades to the empty skeleton
    # (treated as "nothing to show"), not a crash.
    paths.DB.parent.mkdir(parents=True, exist_ok=True)
    paths.DB.write_text("this is not a sqlite database")

    data = ui_export.build_data()

    assert set(data.keys()) == _TOP_LEVEL_KEYS
    assert data["reviews"] == []


def test_build_data_surfaces_unreadable_db(isolated_root: Path) -> None:
    # A DB file that exists but cannot be READ (permission denied) must SURFACE
    # as an error (OSError), not be swallowed into an empty document — silently
    # emptying a populated, auditable DB is the bug we are guarding against.
    db.init_db()
    _seed_one_review()
    paths.DB.chmod(0o000)  # unreadable file; dir stays traversable so exists() holds
    try:
        with pytest.raises(PermissionError):
            ui_export.build_data()
    finally:
        paths.DB.chmod(0o644)


def test_cmd_ui_export_nonzero_and_no_write_on_unreadable_db(
    isolated_root: Path, tmp_path: Path
) -> None:
    # The CLI must exit non-zero and write NOTHING when the DB is unreadable.
    db.init_db()
    _seed_one_review()
    out = tmp_path / "out"
    paths.DB.chmod(0o000)
    try:
        rc = cli.cmd_ui_export(
            cli.build_parser().parse_args(
                ["ui-export", "--root", str(isolated_root), "--out", str(out)]
            )
        )
    finally:
        paths.DB.chmod(0o644)

    assert rc == 1
    assert not (out / "data.json").exists()
