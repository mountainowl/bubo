import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from bubo import update_helper
from bubo import update_windows_schedule as scheduler


class Tasks:
    def __init__(self):
        self.xml = None
        self.calls = []
        self.fail_create = False
        self.state = 3
        self.ignore_stop = False

    def __call__(self, args, **kwargs):
        assert kwargs["timeout"] == 15
        self.calls.append(args)
        if args[0] == "powershell.exe":
            if "Export-ScheduledTask" in args[-1]:
                import base64

                return SimpleNamespace(
                    returncode=0 if self.xml is not None else 1,
                    stdout=base64.b64encode((self.xml or "").encode()),
                )
            return SimpleNamespace(returncode=0, stdout=str(self.state).encode())
        assert args[0] == "schtasks"
        if args[1] == "/Query":
            return SimpleNamespace(returncode=0 if self.xml else 1, stdout=self.xml or "")
        if args[1] == "/Create":
            if self.fail_create:
                raise subprocess.CalledProcessError(1, args)
            self.xml = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        if args[1] == "/Delete":
            self.xml = None
        if args[1] == "/End" and not self.ignore_stop:
            self.state = 3
        if args[1] == "/Run":
            self.state = 4
        return SimpleNamespace(returncode=0, stdout="")


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "current_owner", lambda: "S-1-5-21-123")
    root = tmp_path / 'a space & quoted " path'
    root.mkdir()
    return root, {"python": str(root / "python.exe"), "archive": str(root / "helper-first.pyz")}


def test_create_repeat_refresh_and_remove_owned_task(configuration):
    root, record = configuration
    tasks = Tasks()
    name = scheduler.schedule(root, record, runner=tasks)
    assert name.startswith("Bubo-Update-")
    assert "/F" not in tasks.calls[1]
    assert tasks.calls[2][1] == "/Run"
    assert scheduler._owned(tasks.xml, root, record)
    assert not list(root.glob("scheduler-*.xml"))
    # Windows exports pretty XML; whitespace must not cause a restart on every CLI run.
    tree = ET.fromstring(tasks.xml)
    ET.indent(tree)
    tasks.xml = ET.tostring(tree, encoding="unicode")
    tasks.calls.clear()
    assert scheduler.schedule(root, record, runner=tasks) == name
    assert [call[1] for call in tasks.calls] == ["-NoProfile", "-NoProfile"]
    record["archive"] = str(root / "helper-second.pyz")
    tasks.calls.clear()
    scheduler.schedule(root, record, runner=tasks)
    assert [call[1] for call in tasks.calls] == [
        "-NoProfile",
        "/End",
        "-NoProfile",
        "/Create",
        "/Run",
    ]
    assert "/F" in tasks.calls[3]
    tasks.calls.clear()
    scheduler.schedule(root, record, remove=True, runner=tasks)
    assert [call[1] for call in tasks.calls] == ["-NoProfile", "/End", "-NoProfile", "/Delete"]
    assert tasks.xml is None


@pytest.mark.parametrize("mutation", ["owner", "action", "elevated", "extra_action", "malformed"])
def test_unknown_or_modified_task_is_never_changed(configuration, mutation):
    root, record = configuration
    tasks = Tasks()
    tasks.xml = scheduler._xml(root, record)
    if mutation == "owner":
        tasks.xml = tasks.xml.replace("S-1-5-21-123", "S-1-5-21-456")
    elif mutation == "action":
        tasks.xml = tasks.xml.replace("--installation", "--unrelated")
    elif mutation == "elevated":
        tasks.xml = tasks.xml.replace("LeastPrivilege", "HighestAvailable")
    elif mutation == "extra_action":
        tree = ET.fromstring(tasks.xml)
        actions = tree.find(f"{{{scheduler._NS}}}Actions")
        actions.append(ET.Element(f"{{{scheduler._NS}}}Exec"))
        tasks.xml = ET.tostring(tree, encoding="unicode")
    else:
        tasks.xml = "not XML"
    with pytest.raises(OSError, match="unowned"):
        scheduler.schedule(root, record, runner=tasks)
    assert [call[1] for call in tasks.calls] == ["-NoProfile"]
    with pytest.raises(OSError, match="unowned"):
        scheduler.schedule(root, record, remove=True, runner=tasks)
    assert all(call[1] == "-NoProfile" for call in tasks.calls)


def test_create_failure_cleans_xml_and_cannot_force_unknown_task(configuration):
    root, record = configuration
    tasks = Tasks()
    tasks.fail_create = True
    with pytest.raises(subprocess.CalledProcessError):
        scheduler.schedule(root, record, runner=tasks)
    assert "/F" not in tasks.calls[-1]
    assert not list(root.glob("scheduler-*.xml"))


def test_unverified_absence_does_not_claim_removal(configuration):
    root, record = configuration
    tasks = Tasks()
    with pytest.raises(OSError, match="Cannot verify"):
        scheduler.schedule(root, record, remove=True, runner=tasks)
    assert len(tasks.calls) == 1


def test_xml_uses_no_password_elevation_or_shell(configuration):
    root, record = configuration
    xml = scheduler._xml(root, record)
    tree = ET.fromstring(xml)
    ns = {"t": scheduler._NS}
    assert tree.find("t:Principals/t:Principal/t:LogonType", ns).text == "InteractiveToken"
    assert tree.find("t:Principals/t:Principal/t:RunLevel", ns).text == "LeastPrivilege"
    assert tree.find("t:Settings/t:MultipleInstancesPolicy", ns).text == "IgnoreNew"
    assert tree.find("t:Actions/t:Exec/t:Arguments", ns).text == subprocess.list2cmdline(
        [
            record["archive"],
            "--installation",
            str(root),
            "run",
        ]
    )
    assert tree.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text == "PT1M"


def test_helper_dispatches_windows_scheduler(configuration, monkeypatch):
    root, record = configuration
    tasks = Tasks()
    monkeypatch.setattr(sys, "platform", "win32")
    update_helper.register_schedule(root, record, home=root, runner=tasks)
    update_helper.remove_schedule(root, record, home=root, runner=tasks)
    assert tasks.xml is None


def test_identical_stopped_task_is_started(configuration):
    root, record = configuration
    tasks = Tasks()
    tasks.xml = scheduler._xml(root, record)
    scheduler.schedule(root, record, runner=tasks)
    assert [call[1] for call in tasks.calls] == ["-NoProfile", "-NoProfile", "/Run"]


def test_failed_stop_never_replaces_or_removes_task(configuration, monkeypatch):
    root, record = configuration
    tasks = Tasks()
    tasks.xml = scheduler._xml(root, record)
    tasks.state = 4
    tasks.ignore_stop = True
    times = iter([0, 6])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(times))
    with pytest.raises(OSError, match="did not stop"):
        scheduler.schedule(root, record, remove=True, runner=tasks)
    assert not any(call[1] in {"/Create", "/Delete"} for call in tasks.calls)
    assert tasks.xml


def test_exported_task_can_omit_default_least_privilege(configuration):
    root, record = configuration
    tasks = Tasks()
    tree = ET.fromstring(scheduler._xml(root, record))
    namespace = {"t": scheduler._NS}
    principal = tree.find("t:Principals/t:Principal", namespace)
    principal.remove(principal.find("t:RunLevel", namespace))
    tasks.xml = ET.tostring(tree, encoding="unicode")
    assert scheduler._owned(tasks.xml, root, record)
    scheduler.schedule(root, record, remove=True, runner=tasks)
    assert tasks.xml is None


def test_xml_query_preserves_unicode_and_ignores_misleading_declaration(configuration):
    root, record = configuration
    tasks = Tasks()
    tasks.xml = '<?xml version="1.0" encoding="UTF-16"?>' + scheduler._xml(root, record)
    tasks.xml = tasks.xml.replace("Bubo update helper", "Bubo update helper Ω")
    assert scheduler._query("Bubo-Update-test", tasks) == tasks.xml
