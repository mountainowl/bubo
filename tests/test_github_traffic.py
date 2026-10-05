"""Acquisition snapshots must never retain partial or credential-bearing responses."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


def test_traffic_snapshot_is_complete_or_absent(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "traffic", Path(__file__).parents[1] / "scripts" / "snapshot_github_traffic.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "snapshot.jsonl"
    monkeypatch.setattr(sys, "argv", ["traffic", "--output", str(output)])
    clones = Mock(stdout=json.dumps({"count": 3, "uniques": 2, "clones": []}))
    runner = Mock(side_effect=[clones, subprocess.CalledProcessError(1, "gh")])
    monkeypatch.setattr(module.subprocess, "run", runner)
    with pytest.raises(SystemExit, match="1"):
        module.main()
    assert not output.exists()
    runner.side_effect = [
        clones,
        Mock(stdout=json.dumps({"count": 4, "uniques": 3, "views": [], "unapproved": "secret"})),
    ]
    module.main()
    snapshot = json.loads(output.read_text())
    assert snapshot["clones"]["uniques"] == 2
    assert snapshot["views"]["count"] == 4
    assert "secret" not in output.read_text()
