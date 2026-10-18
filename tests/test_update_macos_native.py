"""Opt-in native LaunchAgent lifecycle without release or telemetry traffic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from bubo.update_helper import register_schedule, remove_schedule

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("BUBO_NATIVE_LAUNCHD_TEST") != "1",
    reason="requires explicit native LaunchAgent acceptance opt-in",
)


def test_native_launchagent_executes_and_removes(tmp_path):
    installation = tmp_path / ("bubo-native-" + uuid.uuid4().hex)
    installation.mkdir()
    receipt = installation / "receipt.json"
    archive = installation / "helper.py"
    archive.write_text(
        "import json, pathlib, sys, time\n"
        f"pathlib.Path({str(receipt)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "time.sleep(120)\n"
    )
    record = {"python": sys.executable, "archive": str(archive)}
    # launchctl accepts an explicit plist path; keep it inside the test sandbox.
    home = tmp_path / "home"
    label = "com.bubo.update." + installation.name
    target = f"gui/{os.getuid()}/{label}"
    path = home / "Library/LaunchAgents" / (label + ".plist")
    try:
        assert register_schedule(installation, record, home=home) == str(path)
        assert register_schedule(installation, record, home=home) == str(path)
        deadline = time.monotonic() + 15
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert json.loads(receipt.read_text()) == ["--installation", str(installation), "run"]
        assert (
            subprocess.run(
                ["launchctl", "print", target],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    finally:
        remove_schedule(installation, record, home=home)
    assert not path.exists()
    assert (
        subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
