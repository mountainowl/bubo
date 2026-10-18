from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from bubo import update_helper
from bubo.update_helper import analytics_policy, auto_enabled
from bubo.update_identity import atomic_json


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    root = tmp_path / "root"
    state = root / "var" / "state"
    state.mkdir(parents=True)
    (root / "config").mkdir()
    config = root / "config" / "env.toml"
    config.write_text("auto_update = false\n[analytics]\nenabled = true\n")
    installation = tmp_path / "controller"
    installation.mkdir()
    monkeypatch.setattr(
        update_helper, "installation_for_environment", lambda environment: installation
    )
    from bubo import paths

    monkeypatch.setattr(paths, "ROOT", root)
    (installation / "activity.lock").touch()
    (state / "update-activity.lock").touch()
    record = {
        "protocol_version": 1,
        "owner": update_helper.current_owner(),
        "states": [str(state)],
        "python": sys._base_executable,
    }
    atomic_json(installation / "installation.json", record)
    atomic_json(
        state / "update-registration.json",
        {"installation": str(installation), "root": str(root), "ready": False},
    )
    return installation, state, record


def test_auto_update_false_does_not_disable_analytics(registered):
    installation, state, _ = registered
    assert not auto_enabled(state)
    assert analytics_policy(installation, state)[1] is True


def test_windows_worker_accepts_real_bootstrap_registration(tmp_path, monkeypatch):
    import inspect
    from types import SimpleNamespace

    from bubo import update_admission, update_windows_jobs, update_windows_worker

    root, environment, installation = [tmp_path / name for name in ("root", "venv", "installation")]
    (root / "config").mkdir(parents=True)
    (root / "config" / "env.toml").write_text("auto_update = false\n")
    environment.mkdir()
    (environment / "pyvenv.cfg").touch()
    adapter = SimpleNamespace(
        python=environment / "bin" / "python",
        preflight=lambda: None,
        executables=[],
        _run=lambda args: (
            "{}" if "direct_url.json" in args[-1] else update_admission._PROCESS_VERSION
        ),
    )
    monkeypatch.setattr(update_helper, "PythonInstaller", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(
        update_helper, "installation_for_environment", lambda *a, **kw: installation
    )
    monkeypatch.setattr(update_helper, "_check_legacy_processes", lambda path: None)
    assert (
        update_helper.bootstrap(
            root,
            environment=environment,
            home=tmp_path,
            schedule=lambda *a, **kw: None,
        )
        == installation
    )
    state = root / "var" / "state"
    record = json.loads((installation / "installation.json").read_text())
    signature = inspect.signature(update_windows_worker.JobProcess)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd))
    monkeypatch.setattr(update_windows_jobs, "windows_process_identity", lambda pid: "parent-start")
    observed = []

    def native_boundary(args, **kwargs):
        signature.bind(args, **kwargs)
        assert args[0] == record["python"]
        assert args[-2] == record["archive"]
        payload = json.loads(kwargs["env"][args[-1]])
        assert payload["environment"] == record["environment"]
        assert payload["installation"] == str(installation)
        observed.append(True)
        raise RuntimeError("verified native launch boundary")

    monkeypatch.setattr(update_windows_worker, "JobProcess", native_boundary)
    with open(os.devnull, "wb") as output:
        with pytest.raises(RuntimeError, match="verified native launch boundary"):
            update_windows_worker.spawn(["worker"], installation, state, stdout=output)
    assert observed == [True]
    assert list((installation / "windows-jobs").iterdir()) == []


@pytest.mark.parametrize(
    ("status", "version"), [("updated", "0.28.0"), ("restored", "0.27.0"), ("failed", "0.27.0")]
)
def test_recovery_publishes_status_and_actual_generation(registered, monkeypatch, status, version):
    from types import SimpleNamespace

    from bubo.updater import UpdateResult

    installation, _state, record = registered
    record.update(kind="pip", source_version="0.27.0", helper_version="0.27.0", next_check=0)
    (installation / "registry.lock").touch()
    atomic_json(installation / "installation.json", record)
    atomic_json(installation / "maintenance.json", {})
    attempt = "6b3ba572-057d-4361-80c2-9e59d2adfe96"
    result = UpdateResult(status, attempt, "recovery_blocked" if status == "failed" else None)

    def recover():
        atomic_json(installation / "generation.json", {"version": version})
        return result

    monkeypatch.setattr(update_helper, "_installer", lambda *args: object())
    monkeypatch.setattr(
        update_helper, "UpdateEngine", lambda *args, **kwargs: SimpleNamespace(recover=recover)
    )
    # Pending recovery runs even when ordinary automatic checks are disabled.
    assert update_helper.tick(installation) == result
    assert json.loads((installation / "status.json").read_text()) == {
        "status": status,
        "attempt_id": attempt,
        "code": result.code,
    }
    current = json.loads((installation / "installation.json").read_text())
    assert current["source_version"] == version
    assert current["next_check"] > 0


def test_config_changes_take_effect_without_reregistering(registered):
    installation, state, _ = registered
    assert analytics_policy(installation, state)[1] is True
    (state.parent.parent / "config" / "env.toml").write_text("[analytics]\nenabled = false\n")
    assert analytics_policy(installation, state)[1] is False
    (state.parent.parent / "config" / "env.toml").write_text("[analytics]\nenabled = true\n")
    assert analytics_policy(installation, state)[1] is True


def test_retained_helper_reports_without_application_environment(registered, monkeypatch):
    installation, state, record = registered
    archive = update_helper._bundle(installation, Path(sys._base_executable))
    record["archive"] = archive
    atomic_json(installation / "installation.json", record)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    command = [sys._base_executable, "-I", archive, "--installation", str(installation), "report"]
    result = subprocess.run(
        command, env=environment, cwd=installation, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "No update failures recorded.\n"
    assert not (state / "update-diagnostics").exists()


def test_retained_windows_guardian_imports_without_site_packages(registered):
    installation, _, _ = registered
    archive = update_helper._bundle(installation, Path(sys._base_executable))
    result = subprocess.run(
        [
            sys._base_executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from bubo.update_windows_worker import main",
            archive,
        ],
        cwd=installation,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_registration_is_scoped_to_owned_helper(registered, tmp_path, monkeypatch, platform):
    installation, _, record = registered
    monkeypatch.setattr(update_helper.sys, "platform", platform)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if "print" in command else 0)

    path = update_helper.register_schedule(installation, record, home=tmp_path / "home", runner=run)
    assert Path(path).is_file()
    assert all("sudo" not in command for command in calls)
    assert any("bootstrap" in command or "enable" in command for command in calls)
    assert str(installation) in Path(path).read_text()


def test_status_and_report_do_not_bootstrap(registered, capsys):
    installation, state, _ = registered
    assert update_helper.main(["--installation", str(installation), "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "auto_update": False,
        "helper_configured": False,
        "helper_disabled": False,
        "update": {"status": "setup_required"},
        "failures": [],
    }
    assert not (state / "update-diagnostics").exists()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_owned_schedule_refresh_restarts_old_helper(registered, tmp_path, monkeypatch, platform):
    installation, _, record = registered
    monkeypatch.setattr(update_helper.sys, "platform", platform)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    record["archive"] = str(installation / "helper-old.pyz")
    update_helper.register_schedule(installation, record, home=tmp_path, runner=run)
    calls.clear()
    record["archive"] = str(installation / "helper-new.pyz")
    update_helper.register_schedule(installation, record, home=tmp_path, runner=run)
    assert any("bootout" in command or "restart" in command for command in calls)


def test_automatic_bootstrap_is_disabled_without_touching_host(registered, monkeypatch):
    installation, state, _ = registered
    (installation / "installation.json").unlink()
    (state / "update-registration.json").unlink()
    calls = []
    monkeypatch.setattr(update_helper, "bootstrap", lambda root, **kwargs: calls.append(root))
    update_helper.ensure_registered(state.parent.parent)
    assert calls == []
    assert not (state / "update-bootstrap.json").exists()


def test_failed_bootstrap_retries_with_one_notice(registered, monkeypatch, capsys):
    _, state, _ = registered
    (state.parent.parent / "config/env.toml").write_text(
        "auto_update = true\n[analytics]\nenabled = false\n"
    )
    calls = []

    def unavailable(root, **kwargs):
        calls.append(root)
        raise OSError("private failure detail")

    monkeypatch.setattr(update_helper, "bootstrap", unavailable)
    monkeypatch.setattr(update_helper.time, "time", lambda: 1000)
    update_helper.ensure_registered(state.parent.parent)
    update_helper.ensure_registered(state.parent.parent)
    assert len(calls) == 1
    first = capsys.readouterr()
    assert "bubo update configure" in first.err
    assert "private failure detail" not in first.err
    monkeypatch.setattr(update_helper.time, "time", lambda: 100000)
    update_helper.ensure_registered(state.parent.parent)
    assert len(calls) == 2
    assert capsys.readouterr().err == ""


def test_only_writable_admission_bootstraps(registered, monkeypatch):
    from bubo.update_admission import activity

    _, state, _ = registered
    calls = []
    monkeypatch.setattr(
        update_helper, "ensure_registered", lambda root, **kwargs: calls.append(root)
    )
    with activity(state):
        pass
    assert calls == []
    with activity(state, writable=True):
        pass
    from bubo import paths

    assert calls == [paths.ROOT]


def test_bootstrap_detects_uncoordinated_legacy_resident(tmp_path, monkeypatch):
    from bubo.update_installers import UnsafeInstallation

    environment = tmp_path / "environment"
    command = f"999999 {environment}/bin/python -m bubo.mcp_server\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, command, "")
    )
    with pytest.raises(UnsafeInstallation, match="requires restart"):
        update_helper._check_legacy_processes(environment)


def test_bootstrap_ignores_unrelated_python_process(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    command = f"999999 {environment}/bin/python -m unrelated\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, command, "")
    )
    update_helper._check_legacy_processes(environment)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_uninstall_removes_only_owned_schedule_retaining_state(
    registered, tmp_path, monkeypatch, platform
):
    installation, state, record = registered
    monkeypatch.setattr(update_helper.sys, "platform", platform)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    schedule = Path(
        update_helper.register_schedule(installation, record, home=tmp_path, runner=run)
    )
    (installation / "attempt.lock").touch()
    identity = state / "install_id"
    identity.write_text("retain-identity")
    update_helper.uninstall_helper(
        installation,
        home=tmp_path,
        remove=lambda *a, **kw: update_helper.remove_schedule(*a, **kw, runner=run),
    )
    assert not schedule.exists()
    assert (installation / "disabled.json").exists()
    assert identity.read_text() == "retain-identity"
    assert (installation / "installation.json").exists()
    assert update_helper.tick(installation).code == "helper_disabled"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("operation", ["remove_schedule", "register_schedule"])
def test_uninstall_refuses_modified_unrelated_native_command(
    registered, tmp_path, monkeypatch, platform, operation
):
    from bubo.update_installers import UnsafeInstallation

    installation, _, record = registered
    monkeypatch.setattr(update_helper.sys, "platform", platform)

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0)

    path = Path(update_helper.register_schedule(installation, record, home=tmp_path, runner=runner))
    if platform == "darwin":
        data = plistlib.loads(path.read_bytes())
        data["ProgramArguments"][0] = "/unrelated/program"
        path.write_bytes(plistlib.dumps(data))
    else:
        content = path.read_text()
        path.write_text(content.replace("ExecStart=", "ExecStart=/unrelated/program "))
    before = path.read_bytes()
    with pytest.raises(UnsafeInstallation, match="unrelated"):
        getattr(update_helper, operation)(installation, record, home=tmp_path, runner=runner)
    assert path.read_bytes() == before


def test_uninstall_is_blocked_during_recovery(registered):
    from bubo.update_installers import UnsafeInstallation

    installation, _, _ = registered
    (installation / "attempt.lock").touch()
    (installation / "maintenance.json").write_text("{}")
    calls = []
    with pytest.raises(UnsafeInstallation, match="pending recovery"):
        update_helper.uninstall_helper(installation, remove=lambda *a, **kw: calls.append(a))
    assert not calls
    assert not (installation / "disabled.json").exists()


def test_status_shows_recovery_and_disable_without_writing(registered, capsys):
    from bubo.update_reporting import FailureStore

    installation, state, _ = registered
    atomic_json(
        state / "update-registration.json", {"installation": str(installation), "ready": True}
    )
    atomic_json(installation / "status.json", {"status": "updated"})
    atomic_json(installation / "disabled.json", {"disabled": True})
    atomic_json(installation / "maintenance.json", {"attempt_id": "fixture"})
    before = {str(path): path.read_bytes() for path in installation.rglob("*") if path.is_file()}
    update_helper._local_command(FailureStore(state), "status", None, installation=installation)
    status = json.loads(capsys.readouterr().out)
    assert status["helper_disabled"] is True
    assert status["helper_configured"] is True
    assert status["auto_update"] is False
    assert status["update"]["status"] == "recovery_required"
    assert not (state / "update-diagnostics").exists()
    assert before == {
        str(path): path.read_bytes() for path in installation.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("setting", [None, "custom", "/tmp/bubo-path-test-absolute"])
def test_state_resolution_matches_config_without_writing(tmp_path, monkeypatch, setting):
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    config = "auto_update = false\n"
    if setting is not None:
        config += f'[poller]\nstate_dir = "{setting}"\n'
    (root / "config/env.toml").write_text(config)
    base = Path(setting) if setting and Path(setting).is_absolute() else root / (setting or "var")
    assert update_helper._state_for_root(root) == base.resolve() / "state"
    assert not (root / "var").exists()
    assert not (root / "custom").exists()


@pytest.mark.parametrize("override", ["external", "/tmp/bubo-path-test-override"])
def test_state_environment_override_precedes_config(tmp_path, monkeypatch, override):
    root = tmp_path / "root"
    monkeypatch.setenv("BUBO_BASE_DIR", override)
    expected = Path(override) if Path(override).is_absolute() else root / override
    assert update_helper._state_for_root(root) == expected.resolve() / "state"
    assert not root.exists()


def test_external_state_uses_registered_configuration_root(tmp_path):
    root = tmp_path / "installation"
    state = tmp_path / "external-volume" / "state"
    (root / "config").mkdir(parents=True)
    (root / "config/env.toml").write_text("auto_update = true\n")
    atomic_json(state / "update-registration.json", {"root": str(root)})
    assert auto_enabled(state)
    (root / "config/env.toml").write_text("auto_update = false\n")
    assert not auto_enabled(state)


def test_unconfigured_status_uses_real_default_state_without_writing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    assert update_helper.cli_command(tmp_path, "status") == 0
    assert json.loads(capsys.readouterr().out)["update"]["status"] == "setup_required"
    assert list(tmp_path.iterdir()) == []


def test_second_configuration_cannot_claim_registered_state(registered, tmp_path, monkeypatch):
    installation, state, record = registered
    original = (state / "update-registration.json").read_bytes()
    other = tmp_path / "other-root"
    (other / "config").mkdir(parents=True)
    (other / "config/env.toml").write_text("auto_update = false\n")
    monkeypatch.setenv("BUBO_BASE_DIR", str(state.parent))
    update_helper.ensure_registered(other)
    assert (state / "update-registration.json").read_bytes() == original
    assert (state / "update-registration-conflict.json").is_file()
    assert auto_enabled(state) is False
    assert analytics_policy(installation, state)[1] is None
    assert json.loads((installation / "installation.json").read_text()) == record
    assert update_helper.cli_command(other, "update") == 75


def test_readonly_other_configuration_does_not_mutate_registration(
    registered, tmp_path, monkeypatch
):
    _, state, _ = registered
    monkeypatch.setenv("BUBO_BASE_DIR", str(state.parent))
    before = {file.name: file.read_bytes() for file in state.iterdir() if file.is_file()}
    assert update_helper.cli_command(tmp_path / "other", "status") == 75
    assert {file.name: file.read_bytes() for file in state.iterdir() if file.is_file()} == before


@pytest.mark.parametrize("text", ["[invalid", '[poller]\nstate_dir = "${MISSING_STATE_PATH}"'])
def test_bad_runtime_configuration_does_not_crash_or_guess_state(tmp_path, monkeypatch, text):
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    monkeypatch.delenv("MISSING_STATE_PATH", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/env.toml").write_text(text)
    update_helper.ensure_registered(tmp_path)
    assert update_helper.cli_command(tmp_path, "status") == 75
    assert list(tmp_path.iterdir()) == [tmp_path / "config"]


def test_non_table_poller_preserves_default_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/env.toml").write_text('poller = "legacy"\n')
    assert update_helper._state_for_root(tmp_path) == tmp_path / "var/state"


def test_bad_configuration_notice_is_once_per_process(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/env.toml").write_text("[broken")
    update_helper.ensure_registered(tmp_path)
    update_helper.ensure_registered(tmp_path)
    assert capsys.readouterr().err.count("check runtime state configuration") == 1


@pytest.mark.parametrize("kind", ["mcp-stdio", "mcp-http", "service"])
def test_direct_resident_launch_uses_registered_helper_and_interpreter(
    registered, monkeypatch, kind
):
    from bubo import update_helper, update_platform

    installation, state, record = registered
    record["ready"] = True
    record["environment"] = str(installation / "environment")
    atomic_json(installation / "installation.json", record)
    root = Path(json.loads((state / "update-registration.json").read_text())["root"])
    atomic_json(
        state / "update-registration.json",
        {
            "installation": str(installation),
            "root": str(root),
            "ready": True,
        },
    )
    interpreter = Path(record["environment"]) / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.touch()
    monkeypatch.setattr(update_platform, "environment_python", lambda environment: interpreter)
    monkeypatch.setenv("BUBO_SYNTHETIC_SECRET", "inherited-only")
    # Restore the environment after exercising the entrypoint's deliberate root selection.
    for key in ("BUBO_ROOT", "BUBO_BASE_DIR", "BUBO_MCP_TRANSPORT"):
        monkeypatch.setenv(key, "original")
    launch = update_helper.resident_launch(installation, state, kind)
    assert launch["command"] == record["python"]
    assert launch["args"][-4:] == ["--state", str(state), "supervise", kind]
    assert "inherited-only" not in json.dumps(launch)
    command = update_helper._direct_resident_command(installation, state, kind)
    assert command[0] == str(interpreter)
    assert os.environ["BUBO_ROOT"] == str(root)
    assert os.environ["BUBO_BASE_DIR"] == str(state.parent)
    assert os.environ["BUBO_SYNTHETIC_SECRET"] == "inherited-only"
    assert command[1:] == (
        ["-m", "bubo.poller", "service", "start", "--foreground"]
        if kind == "service"
        else ["-c", "from bubo.mcp_server import main; main()"]
    )


def test_direct_resident_launch_rejects_unready_registration(registered):
    from bubo.update_helper import UnsafeInstallation, resident_launch

    installation, state, _ = registered
    with pytest.raises(UnsafeInstallation, match="not ready"):
        resident_launch(installation, state, "mcp-stdio")


def test_bundle_reuses_verified_archive_without_replacing_it(tmp_path, monkeypatch):
    archive = update_helper._bundle(tmp_path, Path(sys._base_executable))
    original = Path(archive).stat().st_mtime_ns

    def no_replace(*args):
        pytest.fail("An identical retained archive must not be replaced")

    monkeypatch.setattr(update_helper.os, "replace", no_replace)
    assert update_helper._bundle(tmp_path, Path(sys._base_executable)) == archive
    assert Path(archive).stat().st_mtime_ns == original


def test_bundle_refuses_modified_archive(tmp_path):
    import zipfile

    from bubo.update_installers import UnsafeInstallation

    archive = update_helper._bundle(tmp_path, Path(sys._base_executable))
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr("unexpected.py", 'raise RuntimeError("fixture")')
    with pytest.raises(UnsafeInstallation, match="does not match"):
        update_helper._bundle(tmp_path, Path(sys._base_executable))


def test_config_opt_out_survives_detach(registered):
    installation, state, _record = registered
    config = state.parent.parent / "config/env.toml"
    config.write_text("[analytics]\nenabled = false\n")
    assert analytics_policy(installation, state)[1] is False
