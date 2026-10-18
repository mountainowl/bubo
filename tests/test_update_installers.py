from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

from bubo.update_installers import PythonInstaller, UnsafeInstallation, latest_release
from bubo.update_platform import environment_python
from bubo.updater import Release


def test_failed_guardian_publication_never_executes_command(tmp_path, monkeypatch):
    adapter = PythonInstaller(tmp_path / "environment", tmp_path)
    spawned = []
    original = subprocess.Popen
    marker = tmp_path / "executed"

    def spawn(args, **kwargs):
        if kwargs.get("start_new_session"):
            args = list(args)
            args[4] = args[4].replace(
                "os.replace(temporary, writer)", "raise OSError('publication failed')"
            )
        process = original(args, **kwargs)
        if kwargs.get("start_new_session"):
            spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(OSError, match="subprocess failed"):
        adapter._run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
                str(marker),
            ],
            mutation=True,
        )
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert not marker.exists()
    assert adapter.writers_quiescent()


def wheel(directory, version, launchers=("bubo",), requires_python=None):
    path = directory / f"bubo-{version}-py3-none-any.whl"
    metadata = f"bubo-{version}.dist-info"
    files = {
        "bubo/__init__.py": f"__version__ = '{version}'\ndef main(): print(__version__)\n",
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: bubo\nVersion: {version}\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{metadata}/entry_points.txt": "[console_scripts]\n"
        + "".join(f"{name} = bubo:main\n" for name in launchers),
    }
    files[f"{metadata}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{metadata}/RECORD"])
    if requires_python:
        files[f"{metadata}/METADATA"] += f"Requires-Python: {requires_python}\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


class Download(io.BytesIO):
    def geturl(self):
        return "https://files.pythonhosted.org/packages/bubo.whl"


@pytest.fixture
def python_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    initial = wheel(tmp_path, "0.26.0")
    subprocess.run(
        [str(environment_python(environment)), "-m", "pip", "install", str(initial)],
        check=True,
        capture_output=True,
    )
    installation = tmp_path / "controller"
    installation.mkdir()
    return environment, installation


def test_real_pip_environment_upgrade_and_full_restore(python_environment, tmp_path):
    environment, controller = python_environment
    artifact = wheel(tmp_path, "0.27.0")
    content = artifact.read_bytes()
    adapter = PythonInstaller(environment, controller, opener=lambda *a, **k: Download(content))
    work = controller / "work"
    work.mkdir()
    release = Release(
        "0.27.0",
        "https://files.pythonhosted.org/packages/bubo.whl",
        hashlib.sha256(content).hexdigest(),
    )
    adapter.stage(release, work)
    adapter.snapshot(work)
    adapter.activate(work)
    command = [str(environment / "bin/bubo")]
    assert subprocess.check_output(command, text=True).strip() == "0.27.0"
    assert adapter.writers_quiescent()
    adapter.restore(work)
    assert subprocess.check_output(command, text=True).strip() == "0.26.0"
    # The saved entrypoint still runs from the original path after full restoration.
    assert (environment / "bin/pip").is_file()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_real_uv_upgrade_restores_removed_and_added_launchers(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    tool_root = tmp_path / "tools"
    bin_directory = tmp_path / "bin"
    monkeypatch.setenv("UV_TOOL_DIR", str(tool_root))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_directory))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("UV_NO_CONFIG", "true")
    monkeypatch.setenv("UV_OFFLINE", "true")
    initial = wheel(tmp_path, "0.26.0", ("bubo", "bubo-old"))
    subprocess.run(
        [
            shutil.which("uv"),
            "tool",
            "install",
            "--python",
            sys.executable,
            "--no-index",
            str(initial),
        ],
        check=True,
        capture_output=True,
    )
    environment = tool_root / "bubo"
    controller = tmp_path / "controller"
    controller.mkdir()
    content = wheel(tmp_path, "0.27.0", ("bubo", "bubo-new")).read_bytes()
    adapter = PythonInstaller(
        environment,
        controller,
        kind="uv",
        bin_directory=bin_directory,
        opener=lambda *a, **kw: Download(content),
    )
    old_launcher = bin_directory / ("bubo-old.exe" if sys.platform == "win32" else "bubo-old")
    old_link = os.readlink(old_launcher) if old_launcher.is_symlink() else old_launcher.read_bytes()
    work = controller / "work"
    work.mkdir()
    adapter.stage(
        Release(
            "0.27.0",
            "https://files.pythonhosted.org/packages/bubo.whl",
            hashlib.sha256(content).hexdigest(),
        ),
        work,
    )
    adapter.snapshot(work)
    monkeypatch.setenv("PATH", os.defpath)
    adapter.activate(work)
    assert (
        subprocess.check_output(
            [str(bin_directory / ("bubo.exe" if sys.platform == "win32" else "bubo"))], text=True
        ).strip()
        == "0.27.0"
    )
    assert (bin_directory / ("bubo-new.exe" if sys.platform == "win32" else "bubo-new")).is_file()
    assert not (
        bin_directory / ("bubo-old.exe" if sys.platform == "win32" else "bubo-old")
    ).exists()
    adapter.restore(work)
    assert (
        subprocess.check_output(
            [str(bin_directory / ("bubo.exe" if sys.platform == "win32" else "bubo"))], text=True
        ).strip()
        == "0.26.0"
    )
    assert (
        os.readlink(old_launcher) if old_launcher.is_symlink() else old_launcher.read_bytes()
    ) == old_link
    assert not (
        bin_directory / ("bubo-new.exe" if sys.platform == "win32" else "bubo-new")
    ).exists()


def test_corrupt_artifact_never_changes_environment(python_environment):
    environment, controller = python_environment
    adapter = PythonInstaller(environment, controller, opener=lambda *a, **k: Download(b"corrupt"))
    work = controller / "work"
    work.mkdir()
    with pytest.raises(UnsafeInstallation, match="integrity"):
        adapter.stage(
            Release("0.27.0", "https://files.pythonhosted.org/packages/bubo.whl", "0" * 64), work
        )
    assert subprocess.check_output([str(environment / "bin/bubo")], text=True).strip() == "0.26.0"


def test_incompatible_python_release_never_replaces_environment(python_environment, tmp_path):
    environment, controller = python_environment
    content = wheel(tmp_path, "0.27.0", requires_python=">=99.0").read_bytes()
    adapter = PythonInstaller(environment, controller, opener=lambda *a, **kw: Download(content))
    work = controller / "work"
    work.mkdir()
    with pytest.raises(OSError, match="Update subprocess failed"):
        adapter.stage(
            Release(
                "0.27.0",
                "https://files.pythonhosted.org/packages/bubo.whl",
                hashlib.sha256(content).hexdigest(),
            ),
            work,
        )
    assert subprocess.check_output([str(environment / "bin/bubo")], text=True).strip() == "0.26.0"
    assert not (work / "snapshot.json").exists()


def test_system_environment_is_not_eligible(tmp_path):
    adapter = PythonInstaller(Path(sys.base_prefix), tmp_path)
    with pytest.raises(UnsafeInstallation, match="dedicated"):
        adapter.preflight()


@pytest.mark.skipif(
    not (Path(sys.executable).parent / "pipx").exists() and shutil.which("pipx") is None,
    reason="pipx is not installed",
)
@pytest.mark.parametrize("backend", ["pip", "uv"])
def test_real_pipx_upgrade_and_launcher_restore(tmp_path, monkeypatch, backend):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    tool_home = tmp_path / "pipx"
    bin_directory = tmp_path / "bin"
    monkeypatch.setenv("PIPX_HOME", str(tool_home))
    monkeypatch.setenv("PIPX_BIN_DIR", str(bin_directory))
    monkeypatch.setenv("PIPX_MAN_DIR", str(tmp_path / "man"))
    monkeypatch.setenv("PIPX_LOG_DIR", str(tmp_path / "pipx-logs"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    monkeypatch.setenv("PIPX_DEFAULT_BACKEND", backend)
    initial = wheel(tmp_path, "0.26.0", ("bubo", "bubo-old"))
    installed = subprocess.run(
        [
            shutil.which("pipx"),
            "install",
            "--python",
            sys.executable,
            str(initial),
        ],
        capture_output=True,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stderr.decode(errors="replace")
    environment = tool_home / "venvs" / "bubo"
    controller = tmp_path / "controller"
    controller.mkdir()
    content = wheel(tmp_path, "0.27.0", ("bubo", "bubo-new")).read_bytes()
    adapter = PythonInstaller(
        environment,
        controller,
        kind="pipx",
        bin_directory=bin_directory,
        opener=lambda *a, **kw: Download(content),
    )
    work = controller / "work"
    work.mkdir()
    adapter.stage(
        Release(
            "0.27.0",
            "https://files.pythonhosted.org/packages/bubo.whl",
            hashlib.sha256(content).hexdigest(),
        ),
        work,
    )
    adapter.snapshot(work)
    adapter.activate(work)
    assert (
        subprocess.check_output(
            [str(bin_directory / ("bubo.exe" if sys.platform == "win32" else "bubo"))], text=True
        ).strip()
        == "0.27.0"
    )
    assert (bin_directory / ("bubo-new.exe" if sys.platform == "win32" else "bubo-new")).is_file()
    adapter.restore(work)
    assert (
        subprocess.check_output(
            [str(bin_directory / ("bubo.exe" if sys.platform == "win32" else "bubo"))], text=True
        ).strip()
        == "0.26.0"
    )
    assert (bin_directory / ("bubo-old.exe" if sys.platform == "win32" else "bubo-old")).is_file()
    assert not (
        bin_directory / ("bubo-new.exe" if sys.platform == "win32" else "bubo-new")
    ).exists()


def test_shared_environment_is_not_eligible(python_environment):
    environment, controller = python_environment
    site = next((environment / "lib").glob("python*/site-packages"))
    other = site / "unrelated_app-1.0.dist-info"
    other.mkdir()
    (other / "METADATA").write_text("Metadata-Version: 2.1\nName: unrelated-app\nVersion: 1.0\n")
    with pytest.raises(UnsafeInstallation, match="Shared"):
        PythonInstaller(environment, controller).preflight()


def test_unknown_mutation_start_never_claims_quiescent(tmp_path):
    (tmp_path / "writer.json").write_text('{"state":"starting"}')
    adapter = PythonInstaller(tmp_path / "env", tmp_path)
    assert not adapter.writers_quiescent()


@pytest.mark.parametrize("version", ["0.25.0", "0.26.0"])
def test_release_discovery_never_downgrades(version):
    class Metadata(io.BytesIO):
        def geturl(self):
            return "https://pypi.org/pypi/bubo/json"

    assert (
        latest_release(
            "0.26.0",
            opener=lambda *a, **k: Metadata(json.dumps({"info": {"version": version}}).encode()),
        )
        is None
    )


def test_release_discovery_requires_official_origin():
    class Metadata(io.BytesIO):
        def geturl(self):
            return "https://example.com/pypi/bubo/json"

    with pytest.raises(UnsafeInstallation, match="origin"):
        latest_release("0.26.0", opener=lambda *a, **k: Metadata(b"{}"))


def test_real_bubo_migration_targets_each_registered_database(tmp_path, monkeypatch):
    import sqlite3
    import uuid

    from bubo.update_identity import atomic_json

    inherited = tmp_path / "unrelated-runtime"
    monkeypatch.setenv("BUBO_BASE_DIR", str(inherited))
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    controller = tmp_path / "controller"
    controller.mkdir()
    adapter = PythonInstaller(Path(sys.prefix), controller)
    states = []
    identities = []
    for index in range(2):
        root = tmp_path / f"root-{index}"
        state = (root / "var/state") if index == 0 else tmp_path / "external-volume/state"
        state.mkdir(parents=True)
        atomic_json(state / "update-registration.json", {"root": str(root)})
        identity = str(uuid.uuid4())
        (state / "install_id").write_text(identity)
        identities.append(identity)
        with sqlite3.connect(state / "reviewer.sqlite") as database:
            database.execute(
                "CREATE TABLE reviewed_mrs (project TEXT, iid INTEGER, sha TEXT, status TEXT, report TEXT, error TEXT, updated_at TEXT, PRIMARY KEY(project, iid, sha))"
            )
            database.execute(
                "INSERT INTO reviewed_mrs VALUES (?,1,'sha','done',NULL,NULL,'today')",
                (str(index),),
            )
        states.append(state)
    adapter.migrate_and_check(states, "test-maintenance")
    for index, state in enumerate(states):
        with sqlite3.connect(state / "reviewer.sqlite") as database:
            assert database.execute("SELECT project FROM reviewed_mrs").fetchall() == [
                (str(index),)
            ]
            assert database.execute(
                "SELECT name FROM sqlite_master WHERE name='review_runs'"
            ).fetchone()
            assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (state / "install_id").read_text() == identities[index]
        assert not (state.parent / "var").exists()
    assert not inherited.exists()


def test_linux_process_identity_works_without_ps(monkeypatch):
    from types import SimpleNamespace

    from bubo import update_installers

    reads = {
        "/proc/123/stat": "123 (command with ) parentheses) "
        + " ".join(["S"] + ["0"] * 18 + ["987654"]),
        "/proc/sys/kernel/random/boot_id": "boot-uuid\n",
    }
    original = Path.read_text
    monkeypatch.setattr(update_installers, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            reads[str(path)] if str(path) in reads else original(path, *args, **kwargs)
        ),
    )
    assert update_installers._start_identity(123) == "proc:boot-uuid:987654"


@pytest.mark.parametrize("fails", [False, True])
def test_staging_tracks_commands_and_restores_normal_writer(tmp_path, monkeypatch, fails):
    adapter = PythonInstaller(tmp_path / "environment", tmp_path)
    work = tmp_path / "stage"
    work.mkdir()
    normal_writer = adapter.writer
    normal_writer.write_text('{"state":"existing"}')

    def stage(_release, destination):
        assert adapter.writer == destination / "stage-writer.json"
        adapter._run([sys.executable, "-c", "raise SystemExit(1)" if fails else "print('ok')"])
        assert json.loads(adapter.writer.read_text())["state"] == "quiescent"

    monkeypatch.setattr(adapter, "_stage", stage)
    if fails:
        with pytest.raises(OSError, match="subprocess failed"):
            adapter.stage(None, work)
    else:
        adapter.stage(None, work)
    assert adapter.writer == normal_writer
    assert not adapter._staging
    assert json.loads(normal_writer.read_text()) == {"state": "existing"}
    assert json.loads((work / "stage-writer.json").read_text())["state"] == "quiescent"


def test_staging_does_not_overwrite_ambiguous_writer(tmp_path, monkeypatch):
    adapter = PythonInstaller(tmp_path / "environment", tmp_path)
    work = tmp_path / "stage"
    work.mkdir()

    def stage(_release, _destination):
        adapter.writer.write_text('{"state":"starting"}')
        adapter._run([sys.executable, "-c", "raise AssertionError('must not run')"])

    monkeypatch.setattr(adapter, "_stage", stage)
    with pytest.raises(UnsafeInstallation, match="not quiescent"):
        adapter.stage(None, work)
    assert adapter.writer == tmp_path / "writer.json"
    assert not adapter._staging
    assert json.loads((work / "stage-writer.json").read_text()) == {"state": "starting"}


@pytest.mark.parametrize("fail_flush", [False, True])
def test_windows_staged_readonly_file_keeps_permissions_after_flush(
    tmp_path, monkeypatch, fail_flush
):
    import stat

    import bubo.update_installers as installers

    staged = tmp_path / "git-object"
    staged.write_bytes(b"immutable object")
    staged.chmod(stat.S_IREAD)
    mode = stat.S_IMODE(staged.stat().st_mode)
    monkeypatch.setattr(installers.sys, "platform", "win32")
    monkeypatch.setattr(installers, "sync_directory", lambda *_: None)
    if fail_flush:

        def fail(_):
            raise OSError("flush failed")

        monkeypatch.setattr(installers.os, "fsync", fail)
        with pytest.raises(OSError, match="flush failed"):
            installers._sync_tree(tmp_path)
    else:
        installers._sync_tree(tmp_path)
    assert stat.S_IMODE(staged.stat().st_mode) == mode
    assert staged.read_bytes() == b"immutable object"


def test_copied_launcher_requires_exact_environment_bytes(tmp_path):
    environment = tmp_path / "environment"
    scripts = environment / "Scripts"
    scripts.mkdir(parents=True)
    original = scripts / "bubo.exe"
    original.write_bytes(b"owned executable")
    launcher = tmp_path / "bubo.exe"
    launcher.write_bytes(original.read_bytes())
    assert (
        PythonInstaller._windows_launcher_digest(launcher, [environment])
        == hashlib.sha256(original.read_bytes()).hexdigest()
    )
    launcher.write_bytes(b"operator modified executable")
    with pytest.raises(UnsafeInstallation, match="ownership changed"):
        PythonInstaller._windows_launcher_digest(launcher, [environment])
    assert launcher.read_bytes() == b"operator modified executable"
    assert PythonInstaller._windows_launcher_digest(tmp_path / "missing.exe", [environment]) is None


@pytest.mark.parametrize("layout", ["copy", "symlink"])
@pytest.mark.parametrize("scenario", ["normal", "interrupted", "modified"])
def test_copied_launcher_recovery_preserves_ownership_and_resumes(
    tmp_path, monkeypatch, scenario, layout
):
    from types import SimpleNamespace

    import bubo.update_installers as installers

    monkeypatch.setattr(installers, "sys", SimpleNamespace(platform="win32"))
    environment = tmp_path / "environment"
    scripts = environment / "Scripts"
    scripts.mkdir(parents=True)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for name in ("bubo.exe", "bubo-old.exe"):
        (scripts / name).write_bytes(b"old:" + name.encode())
        (bin_directory / name).unlink(missing_ok=True)
        if layout == "symlink":
            (bin_directory / name).symlink_to(scripts / name)
        else:
            (bin_directory / name).write_bytes((scripts / name).read_bytes())
    adapter = PythonInstaller(
        environment, tmp_path / "controller", kind="uv", bin_directory=bin_directory
    )
    monkeypatch.setattr(adapter, "preflight", lambda: {})
    monkeypatch.setattr(adapter, "_run", lambda *_: json.dumps(["bubo", "bubo-old"]))
    monkeypatch.setattr(adapter, "writers_quiescent", lambda: True)
    work = tmp_path / "work"
    work.mkdir()
    (work / "package.json").write_text(json.dumps({"launchers": ["bubo", "bubo-new"]}))
    adapter.snapshot(work)
    (scripts / "bubo-old.exe").unlink()
    (bin_directory / "bubo-old.exe").unlink()
    for name in ("bubo.exe", "bubo-new.exe"):
        (scripts / name).write_bytes(b"new:" + name.encode())
        (bin_directory / name).unlink(missing_ok=True)
        if layout == "symlink":
            (bin_directory / name).symlink_to(scripts / name)
        else:
            (bin_directory / name).write_bytes((scripts / name).read_bytes())
    if scenario == "modified":
        (bin_directory / "bubo.exe").unlink()
        (bin_directory / "bubo.exe").write_bytes(b"operator change")
        with pytest.raises(UnsafeInstallation, match="ownership changed"):
            adapter.restore(work)
        assert (bin_directory / "bubo.exe").read_bytes() == b"operator change"
        assert (scripts / "bubo.exe").read_bytes() == b"new:bubo.exe"
        return
    if scenario == "interrupted":
        replace = os.replace

        class ProcessDeath(BaseException):
            pass

        def crash_after_launcher(source, target):
            replace(source, target)
            if Path(target) == bin_directory / "bubo.exe":
                raise ProcessDeath()

        symlink = Path.symlink_to

        def crash_after_symlink(path, target, **kwargs):
            symlink(path, target, **kwargs)
            if path == bin_directory / "bubo.exe":
                raise ProcessDeath()

        monkeypatch.setattr(installers.os, "replace", crash_after_launcher)
        monkeypatch.setattr(Path, "symlink_to", crash_after_symlink)
        with pytest.raises(ProcessDeath):
            adapter.restore(work)
        assert (work / "launcher-recovery.json").exists()
        monkeypatch.setattr(installers.os, "replace", replace)
        monkeypatch.setattr(Path, "symlink_to", symlink)
    adapter.restore(work)
    assert sorted(path.name for path in bin_directory.iterdir()) == ["bubo-old.exe", "bubo.exe"]
    for name in ("bubo.exe", "bubo-old.exe"):
        assert (bin_directory / name).read_bytes() == b"old:" + name.encode()
        assert (bin_directory / name).is_symlink() is (layout == "symlink")
