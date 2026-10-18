"""Opt-in native user-systemd registration, execution, and removal acceptance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bubo.update_helper import register_schedule, remove_schedule

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("BUBO_NATIVE_SYSTEMD_TEST") != "1",
    reason="requires explicit native user-systemd acceptance opt-in",
)


def test_native_user_schedule_executes_and_removes(tmp_path):
    # A unique service exercises the actual platform runner without invoking
    # release discovery, a package manager, or telemetry.
    installation = tmp_path / ("bubo-native-" + tmp_path.name)
    installation.mkdir()
    receipt = installation / "receipt.json"
    archive = installation / "helper.py"
    archive.write_text(
        "import json, pathlib, sys, time\n"
        f"pathlib.Path({str(receipt)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "time.sleep(120)\n"
    )
    record = {"python": sys.executable, "archive": str(archive)}
    home = Path(os.path.expanduser("~"))
    label = "com.bubo.update." + installation.name
    service = home / ".config/systemd/user" / (label + ".service")
    assert not service.exists()
    try:
        assert register_schedule(installation, record, home=home) == str(service)
        deadline = time.monotonic() + 15
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert json.loads(receipt.read_text()) == ["--installation", str(installation), "run"]
        assert (
            subprocess.run(
                ["systemctl", "--user", "is-active", label + ".service"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    finally:
        remove_schedule(installation, record, home=home)
    assert not service.exists()
    assert (
        subprocess.run(
            ["systemctl", "--user", "is-active", label + ".service"],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )
