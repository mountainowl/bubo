"""Owned, per-user Windows Task Scheduler registration."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from bubo.update_platform import current_owner

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _xml(installation: Path, record: dict) -> str:
    ET.register_namespace("", _NS)
    task = ET.Element(f"{{{_NS}}}Task", version="1.2")

    def child(parent, name, value=None, **attributes):
        node = ET.SubElement(parent, f"{{{_NS}}}{name}", attributes)
        node.text = value
        return node

    sid = str(current_owner())
    registration = child(task, "RegistrationInfo")
    child(
        registration,
        "Description",
        json.dumps(
            {
                "owner": "Bubo update helper",
                "installation": str(installation),
                "archive": record["archive"],
            },
            sort_keys=True,
        ),
    )
    triggers = child(task, "Triggers")
    timer = child(triggers, "TimeTrigger")
    repetition = child(timer, "Repetition")
    child(repetition, "Interval", "PT1M")
    child(repetition, "StopAtDurationEnd", "false")
    child(timer, "StartBoundary", datetime.now().astimezone().isoformat(timespec="seconds"))
    child(timer, "Enabled", "true")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true")
    child(logon, "UserId", sid)
    principal = child(child(task, "Principals"), "Principal", id="BuboUser")
    child(principal, "UserId", sid)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    for key, value in {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "StartWhenAvailable": "true",
        "Enabled": "true",
        "Hidden": "true",
        "ExecutionTimeLimit": "PT0S",
    }.items():
        child(settings, key, value)
    action = child(child(task, "Actions", Context="BuboUser"), "Exec")
    child(action, "Command", record["python"])
    child(
        action,
        "Arguments",
        subprocess.list2cmdline(
            [
                record["archive"],
                "--installation",
                str(installation),
                "run",
            ]
        ),
    )
    child(action, "WorkingDirectory", str(installation))
    return ET.tostring(task, encoding="unicode")


def _owned(xml: bytes | str, installation: Path, record: dict) -> bool:
    try:
        task = ET.fromstring(xml)

        def find(path):
            return task.find(path, {"t": _NS})

        description = json.loads(find("t:RegistrationInfo/t:Description").text)
        archive = Path(description["archive"])
        if (
            description.get("owner") != "Bubo update helper"
            or description.get("installation") != str(installation)
            or archive.parent != installation
            or not archive.name.startswith("helper-")
            or archive.suffix != ".pyz"
        ):
            return False
        actions = find("t:Actions")
        principals = find("t:Principals")
        if (
            len(actions) != 1
            or len(principals) != 1
            or actions[0].tag != f"{{{_NS}}}Exec"
            or principals[0].tag != f"{{{_NS}}}Principal"
        ):
            return False
        expected = {
            "t:Principals/t:Principal/t:UserId": str(current_owner()),
            "t:Principals/t:Principal/t:LogonType": "InteractiveToken",
            "t:Principals/t:Principal/t:RunLevel": "LeastPrivilege",
            "t:Actions/t:Exec/t:Command": record["python"],
            "t:Actions/t:Exec/t:Arguments": subprocess.list2cmdline(
                [
                    str(archive),
                    "--installation",
                    str(installation),
                    "run",
                ]
            ),
            "t:Actions/t:Exec/t:WorkingDirectory": str(installation),
        }
        for path, value in expected.items():
            node = find(path)
            # Task Scheduler omits its documented LeastPrivilege default.
            actual = (
                node.text
                if node is not None
                else ("LeastPrivilege" if path.endswith("t:RunLevel") else None)
            )
            if actual != value:
                return False
        return True
    except ET.ParseError, ValueError, TypeError, KeyError, AttributeError, OSError:
        return False


def _matches(current, desired) -> bool:
    """Ignore XML formatting/default additions and the generated trigger start time."""
    if current is None or current.tag != desired.tag:
        return False
    if any(current.get(key) != value for key, value in desired.attrib.items()):
        return False
    if desired.text and not desired.tag.endswith("}StartBoundary") and current.text != desired.text:
        return False
    return all(_matches(current.find(child.tag), child) for child in desired)


def _query(task: str, runner) -> str | None:
    # schtasks /XML can emit OEM bytes with a UTF-16 declaration. Export through
    # .NET and transport base64 so Unicode paths and ownership compare exactly.
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$xml=Export-ScheduledTask -TaskPath '\\' -TaskName '{task}'; "
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($xml))"
    )
    result = runner(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        check=False,
        timeout=15,
    )
    if result.returncode:
        return None
    try:
        return base64.b64decode(result.stdout.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise OSError("Windows scheduler XML is unavailable") from error


def _state(task: str, runner) -> int:
    # task is generated locally from a hex digest, never user-provided script text.
    command = (
        "$ErrorActionPreference='Stop'; "
        f"[int](Get-ScheduledTask -TaskPath '\\' -TaskName '{task}').State"
    )
    result = runner(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        check=True,
        timeout=15,
    )
    try:
        value = int(result.stdout.strip())
    except (ValueError, TypeError) as exc:
        raise OSError("Windows scheduler state is unavailable") from exc
    if value not in {1, 2, 3, 4}:
        raise OSError("Windows scheduler state is unknown")
    return value


def _stop(task: str, runner) -> None:
    runner(["schtasks", "/End", "/TN", task], capture_output=True, check=False, timeout=15)
    deadline = time.monotonic() + 5
    while _state(task, runner) in {2, 4}:
        if time.monotonic() >= deadline:
            raise OSError("Windows scheduler did not stop the idle helper")
        time.sleep(0.1)


def schedule(
    installation: Path, record: dict, *, remove: bool = False, runner=subprocess.run
) -> str:
    """Caller holds attempt.lock, preventing replacement during an update or staging."""
    identity = hashlib.sha256(str(installation).encode()).hexdigest()[:24]
    task = "Bubo-Update-" + identity
    current = _query(task, runner)
    exists = current is not None
    if exists and not _owned(current, installation, record):
        raise OSError("Refusing to change an unowned Windows scheduled task")
    if remove:
        if not exists:
            # Query failure can mean absence or insufficient access; neither proves removal.
            raise OSError("Cannot verify Windows scheduled task before removal")
        _stop(task, runner)
        runner(
            ["schtasks", "/Delete", "/TN", task, "/F"], capture_output=True, check=True, timeout=15
        )
        return task
    desired = _xml(installation, record)
    if exists and _matches(ET.fromstring(current), ET.fromstring(desired)):
        if _state(task, runner) not in {2, 4}:
            runner(["schtasks", "/Run", "/TN", task], capture_output=True, check=True, timeout=15)
        return task
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-16",
        prefix="scheduler-",
        suffix=".xml",
        dir=installation,
        delete=False,
    ) as stream:
        path = Path(stream.name)
        stream.write('<?xml version="1.0" encoding="UTF-16"?>\n' + desired)
    try:
        if exists:
            _stop(task, runner)
        # Without a verified existing task, omit /F: query errors must never permit overwrite.
        runner(
            ["schtasks", "/Create", "/TN", task, "/XML", str(path), *(["/F"] if exists else [])],
            capture_output=True,
            check=True,
            timeout=15,
        )
        runner(["schtasks", "/Run", "/TN", task], capture_output=True, check=True, timeout=15)
    finally:
        path.unlink(missing_ok=True)
    return task
