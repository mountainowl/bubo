from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

import pytest

from bubo import analytics
from bubo.update_identity import guard_path, protect_identity, release_identity


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "reviewer.sqlite")
    monkeypatch.setattr(analytics, "_install_id", None)
    monkeypatch.setattr(analytics, "_install_path", None)
    return tmp_path


def test_guard_survives_process_death_without_rotating_identity(state):
    identity = analytics.install_id()
    script = """
import os, sys
from pathlib import Path
from bubo.update_identity import protect_identity
protect_identity(Path(sys.argv[1]))
os._exit(7)
"""
    result = subprocess.run([sys.executable, "-c", script, str(state)], check=False)
    assert result.returncode == 7
    assert guard_path(state).is_file()
    assert analytics.install_id() == identity
    original = (state / "install_id").read_bytes()
    (state / "install_id").unlink()
    assert analytics.install_id() is None  # including the cached identity path
    assert not (state / "install_id").exists()
    with pytest.raises(ValueError, match="changed"):
        release_identity(state)
    (state / "install_id").write_bytes(original)
    release_identity(state)
    assert analytics.install_id() == identity


@pytest.mark.parametrize("replacement", [None, "broken", uuid.uuid4().hex])
def test_corruption_never_regenerates_while_guarded(state, replacement):
    analytics.install_id()
    protect_identity(state)
    if replacement is None:
        (state / "install_id").unlink()
    else:
        (state / "install_id").write_text(replacement)
    assert analytics.install_id() is None
    assert guard_path(state).exists()
    if replacement is not None:
        assert (state / "install_id").read_text() == replacement


def test_absent_before_upgrade_remains_absent_until_guard_released(state):
    assert protect_identity(state) is None
    assert analytics.install_id() is None
    assert not (state / "install_id").exists()
    release_identity(state)
    assert analytics.install_id()


def test_guard_is_idempotent_and_cannot_replace_snapshot(state):
    identity = analytics.install_id()
    assert protect_identity(state) == identity
    original = guard_path(state).read_bytes()
    assert protect_identity(state) == identity
    assert guard_path(state).read_bytes() == original
    (state / "install_id").write_text(uuid.uuid4().hex)
    with pytest.raises(ValueError, match="changed"):
        protect_identity(state)
    assert guard_path(state).read_bytes() == original


@pytest.mark.parametrize("guard", ["{", "[]", '{"schema": 99}', '{"schema":1}'])
def test_unreadable_guard_suppresses_analytics(state, guard):
    analytics.install_id()
    guard_path(state).write_text(guard)
    assert analytics.install_id() is None


def test_parallel_initializers_observe_guard(state):
    protect_identity(state)
    script = """
import sys
from pathlib import Path
from bubo import analytics
analytics.paths.DB = Path(sys.argv[1]) / 'reviewer.sqlite'
assert analytics.install_id() is None
"""
    children = [subprocess.Popen([sys.executable, "-c", script, str(state)]) for _ in range(6)]
    assert [child.wait(timeout=10) for child in children] == [0] * 6
    assert not (state / "install_id").exists()


def test_identity_guard_is_owner_private(state):
    protect_identity(state)
    assert os.stat(guard_path(state)).st_mode & 0o077 == 0
    assert json.loads(guard_path(state).read_text()) == {"schema": 1, "identity": None}
