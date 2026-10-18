"""Explicit native scheduler acceptance using only a disposable helper and job.

Run manually with PYTHONPATH=src python tests/fixtures/updates/native_schedule_smoke.py.
This registers and removes a real user job; it never runs an application updater.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import zipapp
from pathlib import Path

from bubo.update_helper import register_schedule, remove_schedule
from bubo.update_installers import _start_identity


def run(arguments, **kwargs):
    kwargs.setdefault("timeout", 15)
    return subprocess.run(arguments, **kwargs)


def wait_for(path):
    deadline = time.monotonic() + 15
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.exists():
        raise TimeoutError("Native scheduler did not start the disposable helper")
    return json.loads(path.read_text())


def main():
    if sys.platform not in {"darwin", "win32"}:
        raise SystemExit("This fixture requires macOS launchd or Windows Task Scheduler")
    with tempfile.TemporaryDirectory(prefix="bubo-scheduler-acceptance-") as directory:
        home = Path(directory)
        installation = home / ("acceptance-" + uuid.uuid4().hex)
        installation.mkdir()
        record = {"python": sys._base_executable}
        label = "com.bubo.update." + installation.name
        target = f"gui/{os.getuid()}/{label}" if sys.platform == "darwin" else None
        identities = []
        try:
            pids = []
            for version in (1, 2):
                archive = installation / f"helper-acceptance-{version}.pyz"
                receipt = installation / f"started-{version}.json"
                source = installation / f"source-{version}"
                source.mkdir()
                (source / "__main__.py").write_text(
                    "import json, os, sys, time\nfrom pathlib import Path\n"
                    f"assert sys.argv[1:] == ['--installation', {str(installation)!r}, 'run']\n"
                    f"Path({str(receipt)!r}).write_text(json.dumps({{'pid': os.getpid()}}))\n"
                    "time.sleep(300)\n"
                )
                zipapp.create_archive(source, archive)
                record["archive"] = str(archive)
                path = Path(register_schedule(installation, record, home=home, runner=run))
                pids.append(wait_for(receipt)["pid"])
                identity = _start_identity(pids[-1])
                if identity is None:
                    raise AssertionError("Native helper exited before identity verification")
                identities.append((pids[-1], identity))
                # Repeating registration must preserve the currently running helper.
                register_schedule(installation, record, home=home, runner=run)
                if _start_identity(pids[-1]) != identity:
                    raise AssertionError("Repeated registration replaced a running helper")
                if target:
                    result = run(["launchctl", "print", target], capture_output=True, check=True)
                    if f"pid = {pids[-1]}" not in result.stdout.decode():
                        raise AssertionError("Native helper identity did not match its receipt")
            if pids[0] == pids[1]:
                raise AssertionError("Changed helper was not restarted")
        finally:
            remove_schedule(installation, record, home=home, runner=run)
            for pid, identity in identities:
                deadline = time.monotonic() + 15
                while _start_identity(pid) == identity and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _start_identity(pid) == identity:
                    raise AssertionError("Disposable helper survived scheduler removal")
        query = (
            ["launchctl", "print", target]
            if target
            else ["schtasks", "/Query", "/TN", str(path), "/XML"]
        )
        if (target and path.exists()) or run(
            query, capture_output=True, check=False
        ).returncode == 0:
            raise AssertionError("Disposable scheduler registration was not removed")
    print(
        f"{sys.platform} scheduler: start, idempotent registration, replacement and removal passed"
    )


if __name__ == "__main__":
    main()
