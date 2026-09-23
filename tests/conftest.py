"""Shared opt-in fixtures for tests that need an isolated SQLite database."""

from __future__ import annotations

from pathlib import Path

import pytest

from bubo import db, paths


@pytest.fixture
def isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point Bubo's database path at a fresh per-test SQLite file."""
    database_path = tmp_path / "reviewer.sqlite"
    monkeypatch.setattr(paths, "DB", database_path)
    return database_path


@pytest.fixture
def initialized_db(isolated_db: Path) -> Path:
    """Return an isolated database after creating the current production schema."""
    db.init_db()
    return isolated_db
