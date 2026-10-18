from __future__ import annotations

import ntpath
import os
from contextlib import contextmanager

import pytest

from bubo import update_windows_legacy as legacy


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    root = ntpath.normcase(str(environment.resolve()))
    caller = os.getpid()
    rows = {
        caller: {
            "pid": caller,
            "parent": 101,
            "created": 20,
            "name": "python.exe",
            "path": r"C:\base\python.exe",
            "command": "child",
        },
        101: {
            "pid": 101,
            "parent": 0,
            "created": 10,
            "name": "python.exe",
            "path": root + r"\Scripts\python.exe",
            "command": "wrapper",
        },
    }
    arguments = {
        "child": [r"C:\base\python.exe", "-m", "bubo", "update", "configure"],
        "wrapper": [rows[101]["path"], "-m", "bubo", "update", "configure"],
    }
    pinned = []

    @contextmanager
    def pin(row):
        pinned.append(row["pid"])
        try:
            yield
        finally:
            pinned.remove(row["pid"])

    monkeypatch.setattr(legacy, "_snapshot", lambda: rows)
    monkeypatch.setattr(legacy, "_arguments", lambda command: arguments[command])
    monkeypatch.setattr(legacy, "_pin", pin)
    return environment, rows, arguments, pinned


def test_exact_redirector_is_exempt_and_handles_close(snapshot):
    environment, _rows, _arguments, pinned = snapshot
    legacy.check_legacy_processes(environment)
    assert pinned == []


def test_ancestor_with_different_invocation_is_not_exempt(snapshot):
    environment, rows, arguments, pinned = snapshot
    arguments["wrapper"] = [rows[101]["path"], "-m", "bubo", "serve"]
    with pytest.raises(OSError, match="Close legacy"):
        legacy.check_legacy_processes(environment)
    assert pinned == []


def test_reused_ancestor_pid_is_rejected(snapshot):
    environment, rows, _arguments, pinned = snapshot
    rows[101]["created"] = 30
    with pytest.raises(OSError, match="ancestry changed"):
        legacy.check_legacy_processes(environment)
    assert pinned == []


def test_uninspectable_python_candidate_defers_registration(snapshot):
    environment, rows, _arguments, pinned = snapshot
    rows[102] = {"pid": 102, "name": "python.exe", "path": None, "command": None}
    with pytest.raises(OSError, match="cannot be inspected"):
        legacy.check_legacy_processes(environment)
    assert pinned == []


def test_console_script_redirector_matches_child_script_argument(snapshot):
    environment, rows, arguments, pinned = snapshot
    rows[101]["path"] = rows[101]["path"].replace("python.exe", "bubo.exe")
    arguments["wrapper"][0] = rows[101]["path"]
    arguments["child"].insert(1, rows[101]["path"])
    legacy.check_legacy_processes(environment)
    assert pinned == []
