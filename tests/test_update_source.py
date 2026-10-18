from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import venv
from pathlib import Path

import pytest
from test_update_installers import Download, wheel

from bubo.update_installers import SourceInstaller, UnsafeInstallation
from bubo.update_platform import environment_python
from bubo.updater import Release


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def source_install(tmp_path, monkeypatch):
    from bubo import update_installers

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init")
    git(upstream, "config", "user.email", "fixture@example.invalid")
    git(upstream, "config", "user.name", "Fixture")
    (upstream / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
    )
    (upstream / ".gitignore").write_text("__pycache__/\nrelease.txt\n")
    (upstream / "backend.py").write_text("""
from pathlib import Path
import zipfile

def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    root = Path(__file__).parent
    version = (root / "version.txt").read_text().strip()
    name = f"bubo-{version}-py3-none-any.whl"
    metadata = f"bubo-{version}.dist-info"
    files = {
        "bubo.pth": str(root) + "\\n",
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\\nName: bubo\\nVersion: {version}\\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
        f"{metadata}/entry_points.txt": "[console_scripts]\\nbubo = bubo:main\\n",
    }
    files[f"{metadata}/RECORD"] = "".join(f"{path},,\\n" for path in [*files, f"{metadata}/RECORD"])
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as archive:
        for path, value in files.items():
            archive.writestr(path, value)
    return name
""")
    (upstream / "bubo.py").write_text(
        'from pathlib import Path\ndef main(): print(Path(__file__).with_name("version.txt").read_text().strip())\n'
    )
    for version in ("0.26.0", "0.27.0"):
        (upstream / "version.txt").write_text(version)
        if version == "0.27.0":
            (upstream / "release.txt").write_text("upstream release data")
            git(upstream, "add", "-f", "release.txt")
        git(upstream, "add", ".")
        git(upstream, "commit", "-m", f"test: add release {version} fixture")
        git(upstream, "tag", "v" + version)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", str(upstream), str(source)], check=True, capture_output=True)
    git(source, "checkout", "-b", "installed", "v0.26.0")
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run(
        [str(environment_python(environment)), "-m", "pip", "install", "--editable", str(source)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(update_installers, "SOURCE_ORIGIN", str(upstream))
    installation = tmp_path / "controller"
    installation.mkdir()
    content = wheel(tmp_path, "0.27.0").read_bytes()
    adapter = SourceInstaller(
        environment, installation, source, opener=lambda *a, **kw: Download(content)
    )
    release = Release(
        "0.27.0",
        "https://files.pythonhosted.org/packages/bubo.whl",
        hashlib.sha256(content).hexdigest(),
    )
    work = installation / "work"
    work.mkdir()
    return adapter, release, work


def test_real_editable_upgrade_and_restore_preserves_branch(source_install):
    adapter, release, work = source_install
    original = git(adapter.source, "rev-parse", "HEAD")
    adapter.stage(release, work)
    assert git(adapter.source, "rev-parse", "HEAD") == original
    adapter.snapshot(work)
    adapter.activate(work)
    assert (
        subprocess.check_output(
            [
                str(
                    environment_python(adapter.environment).parent
                    / ("bubo.exe" if sys.platform == "win32" else "bubo")
                )
            ],
            text=True,
        ).strip()
        == "0.27.0"
    )
    assert adapter.preflight()["editable"] is True
    assert adapter.generation_version(release.version) == (
        "0.27.0+g" + git(adapter.source, "rev-parse", "--short=12", "HEAD")
    )
    adapter.restore(work)
    assert (
        subprocess.check_output(
            [
                str(
                    environment_python(adapter.environment).parent
                    / ("bubo.exe" if sys.platform == "win32" else "bubo")
                )
            ],
            text=True,
        ).strip()
        == "0.26.0"
    )
    assert git(adapter.source, "branch", "--show-current") == "installed"
    assert git(adapter.source, "rev-parse", "HEAD") == original


def test_dirty_editable_checkout_is_never_modified(source_install):
    adapter, release, work = source_install
    private_file = adapter.source / "untracked-user-work.txt"
    private_file.write_text("preserve me")
    with pytest.raises(UnsafeInstallation, match="local modifications"):
        adapter.stage(release, work)
    assert private_file.read_text() == "preserve me"
    assert (adapter.source / "version.txt").read_text() == "0.26.0"


def test_source_recovery_preserves_changes_made_during_update(source_install):
    adapter, release, work = source_install
    adapter.stage(release, work)
    adapter.snapshot(work)
    adapter.activate(work)
    changed = adapter.source / "version.txt"
    changed.write_text("user work")
    with pytest.raises(UnsafeInstallation, match="preserve local work"):
        adapter.restore(work)
    assert changed.read_text() == "user work"


def test_helper_registers_and_reopens_editable_adapter(source_install, tmp_path):
    from bubo.update_helper import _installer, bootstrap

    adapter, _, _ = source_install
    root = tmp_path / "root"
    scheduled = []
    installation = bootstrap(
        root,
        environment=adapter.environment,
        home=tmp_path / "home",
        schedule=lambda *args, **kwargs: scheduled.append(args),
    )
    record = json.loads((installation / "installation.json").read_text())
    assert record["kind"] == "editable"
    reopened = _installer(installation, record)
    assert isinstance(reopened, SourceInstaller)
    assert reopened.source == adapter.source
    assert reopened.preflight()["editable"] is True
    assert len(scheduled) == 1


def test_unknown_source_upstream_stays_untouched(source_install):
    adapter, release, work = source_install
    original = git(adapter.source, "rev-parse", "HEAD")
    git(adapter.source, "remote", "set-url", "origin", "https://example.invalid/fork.git")
    with pytest.raises(UnsafeInstallation, match="registered upstream"):
        adapter.stage(release, work)
    assert git(adapter.source, "rev-parse", "HEAD") == original


def test_failed_scheduler_registration_is_retryable(source_install, tmp_path):
    from bubo.update_helper import bootstrap

    adapter, _, _ = source_install
    root = tmp_path / "root"

    def fail(*args, **kwargs):
        raise OSError("scheduler unavailable")

    with pytest.raises(OSError, match="scheduler unavailable"):
        bootstrap(root, environment=adapter.environment, home=tmp_path, schedule=fail)
    marker = root / "var/state/update-registration.json"
    assert json.loads(marker.read_text())["ready"] is False
    bootstrap(root, environment=adapter.environment, home=tmp_path, schedule=lambda *a, **kw: None)
    assert json.loads(marker.read_text())["ready"] is True


def test_clean_local_commit_is_not_overwritten(source_install):
    adapter, release, work = source_install
    (adapter.source / "local.txt").write_text("local work")
    git(adapter.source, "add", "local.txt")
    git(
        adapter.source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "test: retain local source fixture",
    )
    original = git(adapter.source, "rev-parse", "HEAD")
    with pytest.raises(OSError, match="Update subprocess failed"):
        adapter.stage(release, work)
    assert git(adapter.source, "rev-parse", "HEAD") == original
    assert (adapter.source / "local.txt").read_text() == "local work"


def test_ignored_user_file_collision_is_not_overwritten(source_install):
    adapter, release, work = source_install
    collision = adapter.source / "release.txt"
    collision.write_text("private ignored work")
    with pytest.raises(OSError, match="Update subprocess failed"):
        adapter.stage(release, work)
    assert collision.read_text() == "private ignored work"
    assert (adapter.source / "version.txt").read_text() == "0.26.0"


@pytest.mark.parametrize("rename_number", [1, 2])
def test_process_death_between_source_swaps_is_recoverable(source_install, rename_number):
    adapter, release, work = source_install
    adapter.stage(release, work)
    adapter.snapshot(work)
    script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from bubo import update_installers
update_installers.SOURCE_ORIGIN = sys.argv[6]
adapter = update_installers.SourceInstaller(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
original = os.rename
count = 0
def interrupted(*args, **kwargs):
    global count
    original(*args, **kwargs)
    count += 1
    if count == int(sys.argv[7]):
        os._exit(42)
os.rename = interrupted
adapter.activate(Path(sys.argv[5]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).resolve().parents[1] / "src"),
            str(adapter.environment),
            str(adapter.installation),
            str(adapter.source),
            str(work),
            git(adapter.source, "remote", "get-url", "origin"),
            str(rename_number),
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 42
    adapter.restore(work)
    assert git(adapter.source, "branch", "--show-current") == "installed"
    assert (
        subprocess.check_output(
            [
                str(
                    environment_python(adapter.environment).parent
                    / ("bubo.exe" if sys.platform == "win32" else "bubo")
                )
            ],
            text=True,
        ).strip()
        == "0.26.0"
    )


def test_source_manifest_reads_readonly_files_without_write_access(tmp_path, monkeypatch):
    import stat

    import bubo.update_installers as installers

    source = tmp_path / "readonly.py"
    source.write_text("preserve = True\n")
    source.chmod(stat.S_IREAD)
    mode = source.stat().st_mode
    original = Path.open

    def read_only_open(path, mode="r", *args, **kwargs):
        if path == source:
            assert mode == "rb"
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_only_open)
    manifest = installers._source_manifest(tmp_path)
    assert len(manifest) == 64
    assert source.stat().st_mode == mode
