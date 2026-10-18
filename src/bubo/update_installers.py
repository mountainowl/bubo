"""Pinned official releases and reversible dedicated Python environments."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from contextlib import suppress
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname, urlopen

from bubo.update_identity import atomic_json, sync_directory
from bubo.update_platform import environment_python, windows_process_identity
from bubo.updater import Release


class UnsafeInstallation(OSError):
    pass


SOURCE_ORIGIN = "https://github.com/mountainowl/bubo.git"

MIGRATION_SCRIPT = """
import os
import sqlite3
from pathlib import Path
from bubo import db, paths
if paths.DB.resolve() != Path(os.environ['BUBO_UPDATE_EXPECTED_DB']):
    raise RuntimeError('Migration database path mismatch')
db.init_db()
with sqlite3.connect(paths.DB) as connection:
    if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise RuntimeError('Migration integrity check failed')
    connection.execute('BEGIN IMMEDIATE')
    connection.execute('CREATE TABLE IF NOT EXISTS _bubo_update_probe (id INTEGER)')
    connection.rollback()
"""


def _check_windows_tree(root: Path) -> None:
    """Defer redirected Windows trees before copying or removing recovery data."""
    if sys.platform != "win32":
        return

    def check(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        if (
            stat.S_ISLNK(metadata.st_mode)
            or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise UnsafeInstallation("Windows update path contains a reparse point")
        return stat.S_ISDIR(metadata.st_mode)

    root = root.absolute()
    for parent in reversed(root.parents):
        check(parent)
    pending = [root]
    while pending:
        path = pending.pop()
        if check(path):
            pending.extend(path.iterdir())


def _sync_tree(root: Path) -> None:
    for directory, _, filenames in os.walk(root, topdown=False):
        for filename in filenames:
            path = Path(directory) / filename
            if not path.is_symlink():
                mode = stat.S_IMODE(path.stat().st_mode)
                readonly = sys.platform == "win32" and not mode & stat.S_IWRITE
                # Only staged copies reach this helper. Windows needs a writable
                # handle for FlushFileBuffers, including Git's read-only objects.
                if readonly:
                    path.chmod(mode | stat.S_IWRITE)
                try:
                    with path.open("r+b" if sys.platform == "win32" else "rb") as stream:
                        os.fsync(stream.fileno())
                finally:
                    if readonly:
                        path.chmod(mode)
        sync_directory(Path(directory))


def _source_manifest(root: Path) -> str:
    """Detect user file changes, including ignored files; omit Git and bytecode."""
    digest = hashlib.sha256()
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in {".git", "__pycache__"})
        for name in sorted([*folders, *files]):
            path = Path(directory) / name
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(str(path.lstat().st_mode).encode() + b"\0")
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                with path.open("rb") as stream:
                    digest.update(hashlib.file_digest(stream, "sha256").digest())
            digest.update(b"\0")
    return digest.hexdigest()


def stable_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+[A-Za-z0-9._-]+)?", value)
    if match is None:
        raise ValueError("Not a stable Bubo release")
    return tuple(int(part) for part in match.groups())


def latest_release(current: str, *, opener=urlopen) -> Release | None:
    with opener("https://pypi.org/pypi/bubo/json", timeout=10) as response:
        if urlparse(response.geturl()).hostname != "pypi.org":
            raise UnsafeInstallation("Unexpected release metadata origin")
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise UnsafeInstallation("Release metadata too large")
    metadata = json.loads(raw)
    version = metadata["info"]["version"]
    if stable_version(version) <= stable_version(current):
        return None
    expected = f"bubo-{version}-py3-none-any.whl"
    for artifact in metadata["urls"]:
        url = urlparse(artifact["url"])
        digest = artifact.get("digests", {}).get("sha256", "")
        if (
            artifact["filename"] == expected
            and not artifact.get("yanked")
            and url.scheme == "https"
            and url.hostname == "files.pythonhosted.org"
            and not url.username
            and not url.password
            and re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return Release(version, artifact["url"], digest)
    raise UnsafeInstallation("No verified compatible release wheel")


def _start_identity(pid: int) -> str | None:
    if sys.platform == "win32":
        return windows_process_identity(pid)
    # Minimal Linux images need no procps package to prove process identity.
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return f"proc:{boot}:{fields[19]}"
        except OSError, IndexError:
            return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )
    return result.stdout.strip() or None


class PythonInstaller:
    """Retain a complete environment, not just the old Bubo package version.

    Only dedicated environments are eligible. Editable/shared/system environments
    require their own adapter and are rejected here rather than silently converted.
    """

    def __init__(
        self,
        environment: Path,
        installation: Path,
        *,
        kind: str = "pip",
        bin_directory: Path | None = None,
        executables: dict[str, str] | None = None,
        opener=urlopen,
        timeout: float = 600,
    ):
        if kind not in {"pip", "pipx", "uv", "editable"}:
            raise UnsafeInstallation("Unsupported Python installation owner")
        self.environment = environment.absolute()
        self.installation = installation
        self.kind = kind
        self.bin_directory = bin_directory
        self.executables = (
            executables
            if executables is not None
            else {
                name: executable
                for name in ("uv", "pipx", "git")
                if (executable := shutil.which(name))
            }
        )
        self.opener = opener
        self.timeout = timeout
        self.python = environment_python(self.environment)
        self.writer = installation / "writer.json"
        self._staging = False

    def _run(self, args: list[str], *, mutation: bool = False, env=None) -> str:
        if self._staging:
            mutation = True
        if mutation and not self.writers_quiescent():
            raise UnsafeInstallation("Previous installer writer is not quiescent")
        env = dict(os.environ if env is None else env)
        env["PATH"] = os.pathsep.join(
            [
                *(str(Path(path).parent) for path in self.executables.values()),
                env.get("PATH", os.defpath),
            ]
        )
        # Health/migration must import the target distribution, not the caller's
        # checkout or an unrelated environment supplied through Python variables.
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        if sys.platform == "win32":
            return self._run_windows(args, mutation=mutation, env=env)
        if mutation:
            if not _start_identity(os.getpid()):
                raise UnsafeInstallation("Process ownership inspection is unavailable")
            from bubo.update_posix_exec import GUARDIAN, PROTOCOL, gate_path

            gate = os.open(gate_path(self.writer), os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
            os.close(gate)
            nonce = uuid.uuid4().hex
            atomic_json(self.writer, {"state": "starting", "protocol": PROTOCOL, "nonce": nonce})
            args = [sys.executable, "-I", "-S", "-c", GUARDIAN, str(self.writer), nonce, *args]
        # Never retain arbitrary installer output in issue/telemetry diagnostics.
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, env=env
        )
        try:
            output, _ = process.communicate(timeout=self.timeout)
        except BaseException as exc:
            # Ownership publication can fail after spawning, too. Reap the whole
            # process group before allowing any caller to attempt restoration.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            if isinstance(exc, subprocess.TimeoutExpired):
                raise OSError("Update subprocess timed out") from None
            raise
        finally:
            if mutation and process.poll() is not None:
                # Descendants may survive a completed parent. Do not assume the
                # package directory is safe merely because Popen.wait returned.
                group = subprocess.run(
                    ["pgrep", "-g", str(process.pid)], capture_output=True, check=False
                )
                if group.returncode == 1:
                    atomic_json(self.writer, {"state": "quiescent"})
        if process.returncode:
            raise OSError("Update subprocess failed")
        return output.decode("utf-8")

    def _run_windows(self, args: list[str], *, mutation: bool, env: dict[str, str]) -> str:
        from bubo.update_windows_process import new_job_name, run_capture

        name = new_job_name()
        owner = {"pid": os.getpid(), "start": _start_identity(os.getpid())}
        if not owner["start"]:
            raise UnsafeInstallation("Process ownership inspection is unavailable")
        record = {"state": "starting", "job": name, "owner": owner, "job_created": False}
        if mutation:
            # Publish ownership before the atomic job/child creation. A crash in either
            # direction is recoverable from the named job, without a guessed child PID.
            atomic_json(self.writer, record)

        def created():
            record["job_created"] = True
            if mutation:
                atomic_json(self.writer, record)

        def started(pid):
            if mutation:
                atomic_json(self.writer, {**record, "state": "running", "pid": pid})

        try:
            output = run_capture(
                args,
                name=name,
                env=env,
                timeout=self.timeout,
                on_started=started,
                on_job_created=created,
            )
        except subprocess.TimeoutExpired:
            raise OSError("Update subprocess timed out") from None
        finally:
            # Recovery verifies named-job destruction/membership before marking this
            # writer quiet, including failures during child ownership publication.
            if mutation and not self.writers_quiescent():
                raise UnsafeInstallation("Windows mutation writer is not proven quiescent")
        return output.decode("utf-8")

    def preflight(self) -> dict:
        _check_windows_tree(self.environment)
        if (
            not (self.environment / "pyvenv.cfg").is_file()
            or self.environment.is_symlink()
            or not self.python.exists()
            or not os.access(self.environment, os.W_OK)
        ):
            raise UnsafeInstallation("A dedicated writable Python environment is required")
        if self.environment in self.installation.resolve().parents:
            raise UnsafeInstallation("Recovery helper must be outside the replaced environment")
        script = """
import importlib.metadata as m, json, re
packages = list(m.distributions())
normalize = lambda s: re.sub(r'[-_.]+', '-', s).lower()
required = {normalize(re.match(r'[A-Za-z0-9_.-]+', r).group())
            for d in packages for r in (d.requires or [])}
roots = {normalize(d.metadata['Name']) for d in packages} - required
bubo = m.distribution('bubo')
direct = json.loads(bubo.read_text('direct_url.json') or '{}')
print(json.dumps({'roots': sorted(roots),
                  'editable': direct.get('dir_info',{}).get('editable',False),
                  'url': direct.get('url','')}))
"""
        metadata = json.loads(self._run([str(self.python), "-c", script]))
        if metadata["editable"] != (self.kind == "editable") or set(metadata["roots"]) - {
            "bubo",
            "pip",
            "setuptools",
            "wheel",
        }:
            raise UnsafeInstallation("Shared or editable environment requires its owning adapter")
        marker = {"uv": "uv-receipt.toml", "pipx": "pipx_metadata.json"}.get(self.kind)
        if marker and not (self.environment / marker).is_file():
            raise UnsafeInstallation("Installation ownership could not be verified")
        if self.kind in {"uv", "pipx"} and not os.access(
            self.executables.get(self.kind, ""), os.X_OK
        ):
            raise UnsafeInstallation("Owning installation manager is unavailable")
        if self.kind in {"uv", "pipx"} and self.bin_directory is None:
            raise UnsafeInstallation("Owning manager's launcher directory is not registered")
        return metadata

    def generation_version(self, version: str) -> str:
        return version

    def maintenance(self):
        from bubo.update_admission import environment_lock

        return environment_lock(self.environment, exclusive=True)

    def stage(self, release: Release, destination: Path) -> None:
        previous_writer = self.writer
        self.writer = destination / "stage-writer.json"
        self._staging = True
        try:
            if self.writer.exists():
                raise UnsafeInstallation("Staging ownership already exists")
            atomic_json(self.writer, {"state": "quiescent"})
            self._stage(release, destination)
            if not self.writers_quiescent():
                raise UnsafeInstallation("Staging writer is not quiescent")
        finally:
            self.writer = previous_writer
            self._staging = False

    def _stage(self, release: Release, destination: Path) -> None:
        self.preflight()
        stable_version(release.version)
        url = urlparse(release.artifact)
        if (
            url.scheme != "https"
            or url.hostname != "files.pythonhosted.org"
            or url.username
            or url.password
        ):
            raise UnsafeInstallation("Unexpected artifact origin")
        wheel = destination / f"bubo-{release.version}-py3-none-any.whl"
        digest = hashlib.sha256()
        with self.opener(release.artifact, timeout=30) as response, wheel.open("xb") as stream:
            redirected = urlparse(response.geturl())
            if redirected.scheme != "https" or redirected.hostname != "files.pythonhosted.org":
                raise UnsafeInstallation("Unexpected artifact redirect")
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > 256 * 1024 * 1024:
                    raise UnsafeInstallation("Update wheel too large")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != release.sha256:
            raise UnsafeInstallation("Update wheel integrity check failed")
        metadata_root = f"bubo-{release.version}.dist-info/"
        with zipfile.ZipFile(wheel) as archive:
            metadata = BytesParser().parsebytes(archive.read(metadata_root + "METADATA"))
            if metadata["Name"] != "bubo" or metadata["Version"] != release.version:
                raise UnsafeInstallation("Wheel metadata does not match the release")
            entrypoints = configparser.ConfigParser()
            entrypoints.optionxform = str
            if metadata_root + "entry_points.txt" in archive.namelist():
                entrypoints.read_string(archive.read(metadata_root + "entry_points.txt").decode())
            launchers = (
                list(entrypoints["console_scripts"])
                if entrypoints.has_section("console_scripts")
                else []
            )
            if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) for name in launchers):
                raise UnsafeInstallation("Unsafe package launcher name")
        # Let the owning resolver validate Python requirements and dependencies
        # without changing the live environment. Do not duplicate PEP 440 here.
        uses_uv = self.kind == "uv" or (
            self.kind == "pipx"
            and json.loads((self.environment / "pipx_metadata.json").read_text()).get("backend")
            == "uv"
        )
        if uses_uv:
            if not os.access(self.executables.get("uv", ""), os.X_OK):
                raise UnsafeInstallation("Recorded package resolver is unavailable")
            command = [
                self.executables["uv"],
                "pip",
                "install",
                "--dry-run",
                "--upgrade",
                "--python",
                str(self.python),
                str(wheel),
            ]
        else:
            command = [
                str(self.python),
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--upgrade",
                str(wheel),
            ]
        self._run(command)
        atomic_json(
            destination / "package.json",
            {
                "wheel": str(wheel),
                "version": release.version,
                "launchers": launchers,
            },
        )

    @staticmethod
    def _windows_launcher_digest(
        path: Path, environments: list[Path], known: set[str] | None = None
    ) -> str | None:
        """Hash a launcher only after proving its environment ownership."""
        if path.is_symlink():
            targets = {
                (environment / "Scripts" / path.name).resolve() for environment in environments
            }
            if path.resolve() not in targets:
                raise UnsafeInstallation("Launcher ownership changed; preserve local work")
            _check_windows_tree(path.resolve())
        else:
            _check_windows_tree(path)
        if not path.exists():
            return None
        if not path.is_file():
            raise UnsafeInstallation("Launcher is not a regular executable")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if known and digest in known:
            return digest
        for environment in environments:
            candidate = environment / "Scripts" / path.name
            _check_windows_tree(candidate)
            if candidate.is_file():
                with candidate.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() == digest:
                        return digest
        raise UnsafeInstallation("Launcher ownership changed; preserve local work")

    def snapshot(self, destination: Path) -> None:
        self.preflight()  # detect local environment changes since staging
        _check_windows_tree(destination)
        if self.kind in {"uv", "pipx"}:
            current = json.loads(
                self._run(
                    [
                        str(self.python),
                        "-c",
                        "from importlib.metadata import distribution; import json; "
                        "print(json.dumps([e.name for e in distribution('bubo').entry_points "
                        "if e.group == 'console_scripts']))",
                    ]
                )
            )
            target = json.loads((destination / "package.json").read_text())["launchers"]
            launchers = {}
            for name in sorted(set(current) | set(target)):
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
                    raise UnsafeInstallation("Unsafe installed launcher name")
                if sys.platform == "win32":
                    name += ".exe"
                    digest = self._windows_launcher_digest(
                        self.bin_directory / name, [self.environment]
                    )
                    path = self.bin_directory / name
                    launchers[name] = (
                        str(path.resolve())
                        if path.is_symlink()
                        else {"copy": digest}
                        if digest
                        else None
                    )
                    continue
                path = self.bin_directory / name
                if path.exists() or path.is_symlink():
                    if (
                        not path.is_symlink()
                        or self.environment.resolve() not in path.resolve().parents
                    ):
                        raise UnsafeInstallation("Launcher belongs to another installation")
                    launchers[name] = os.readlink(path)
                else:
                    launchers[name] = None
            atomic_json(destination / "launchers.json", launchers)
        shutil.copytree(self.environment, destination / "environment", symlinks=True)
        _sync_tree(destination / "environment")
        atomic_json(destination / "snapshot.json", {"environment": str(self.environment)})

    def activate(self, destination: Path) -> None:
        package = json.loads((destination / "package.json").read_text())
        wheel = package["wheel"]
        if self.kind == "pip":
            command = [str(self.python), "-m", "pip", "install", "--upgrade", wheel]
        elif self.kind == "pipx":
            command = [self.executables["pipx"], "install", "--force", wheel]
        else:
            command = [self.executables["uv"], "tool", "install", "--force", wheel]
        environment = dict(os.environ)
        if self.kind == "uv":
            environment.update(
                UV_TOOL_DIR=str(self.environment.parent), UV_TOOL_BIN_DIR=str(self.bin_directory)
            )
        elif self.kind == "pipx":
            if self.environment.parent.name != "venvs":
                raise UnsafeInstallation("Unknown pipx environment layout")
            environment.update(
                PIPX_HOME=str(self.environment.parent.parent), PIPX_BIN_DIR=str(self.bin_directory)
            )
        self._run(command, mutation=True, env=environment)
        actual = self._run(
            [
                str(self.python),
                "-c",
                "from importlib.metadata import version; print(version('bubo'))",
            ]
        )
        if actual.strip() != package["version"]:
            raise UnsafeInstallation("Owning manager did not activate the expected version")

    def restore(self, destination: Path) -> None:
        if not self.writers_quiescent():
            raise UnsafeInstallation("Mutation writer is not proven quiescent")
        for path in (
            destination,
            self.environment,
            self.environment.with_name(self.environment.name + ".bubo-recovery"),
            self.environment.with_name(self.environment.name + ".bubo-failed"),
        ):
            _check_windows_tree(path)
        snapshot = json.loads((destination / "snapshot.json").read_text())
        if snapshot["environment"] != str(self.environment):
            raise UnsafeInstallation("Snapshot environment mismatch")
        launchers = {}
        if self.kind in {"uv", "pipx"}:
            launchers = json.loads((destination / "launchers.json").read_text())
            receipt = destination / "launcher-recovery.json"
            recorded = json.loads(receipt.read_text()) if receipt.exists() else {}
            observed = {}
            for name in launchers:
                path = self.bin_directory / name
                if sys.platform == "win32":
                    target = launchers[name]
                    known = {recorded.get(name)} - {None}
                    observed[name] = self._windows_launcher_digest(
                        path, [self.environment, destination / "environment"], known
                    )
                    if isinstance(target, str):
                        resolved = (path.parent / target).resolve()
                        if resolved != (self.environment / "Scripts" / name).resolve():
                            raise UnsafeInstallation("Launcher snapshot target changed")
                    elif target is not None:
                        original = destination / "environment" / "Scripts" / name
                        if not isinstance(target, dict) or target != {
                            "copy": self._windows_launcher_digest(
                                original, [destination / "environment"]
                            )
                        }:
                            raise UnsafeInstallation("Launcher snapshot changed")
                    continue
                if (path.exists() or path.is_symlink()) and (
                    not path.is_symlink()
                    or self.environment.resolve() not in path.resolve().parents
                ):
                    raise UnsafeInstallation("Launcher changed during update; preserve local work")
            if sys.platform == "win32" and not receipt.exists():
                # Preserve evidence before replacing the candidate environment,
                # so recovery can resume after any external launcher was restored.
                atomic_json(receipt, observed)
        # Copy before removing anything; low disk must leave the failed runtime
        # and original snapshot available for a later recovery attempt.
        recovered = self.environment.with_name(self.environment.name + ".bubo-recovery")
        if recovered.exists():
            shutil.rmtree(recovered)
        shutil.copytree(destination / "environment", recovered, symlinks=True)
        _sync_tree(recovered)
        failed = self.environment.with_name(self.environment.name + ".bubo-failed")
        if failed.exists():
            shutil.rmtree(failed)
        if self.environment.exists():
            os.replace(self.environment, failed)
        os.replace(recovered, self.environment)
        sync_directory(self.environment.parent)
        for name, target in launchers.items():
            path = self.bin_directory / name
            if sys.platform == "win32" and isinstance(target, dict):
                original = self.environment / "Scripts" / name
                temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(original.read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                continue
            path.unlink(missing_ok=True)
            if target is not None:
                path.symlink_to(target)
        if launchers:
            sync_directory(self.bin_directory)

    def migrate_and_check(self, states: list[Path], token: str) -> None:
        for state in states:
            marker = state / "update-registration.json"
            registration = json.loads(marker.read_text()) if marker.exists() else {}
            env = dict(
                os.environ,
                BUBO_ROOT=str(registration.get("root", state.parent)),
                BUBO_BASE_DIR=str(state.parent.resolve()),
                BUBO_UPDATE_EXPECTED_DB=str((state / "reviewer.sqlite").resolve()),
                BUBO_MAINTENANCE_TOKEN=token,
            )
            self._run([str(self.python), "-c", MIGRATION_SCRIPT], mutation=True, env=env)

    def writers_quiescent(self) -> bool:
        return writer_quiescent(self.writer)


class SourceInstaller(PythonInstaller):
    """Keep an editable install editable; only clean upstream checkouts qualify."""

    def __init__(self, environment: Path, installation: Path, source: Path, **kwargs):
        super().__init__(environment, installation, kind="editable", **kwargs)
        self.source = source.resolve()

    def _git(self, *args: str, mutation: bool = False, repository: Path | None = None) -> str:
        return self._run(
            [self.executables["git"], "-C", str(repository or self.source), *args],
            mutation=mutation,
        ).strip()

    def generation_version(self, version: str) -> str:
        return version.split("+", 1)[0] + "+g" + self._git("rev-parse", "--short=12", "HEAD")

    def preflight(self) -> dict:
        _check_windows_tree(self.source)
        metadata = super().preflight()
        if not os.access(self.executables.get("git", ""), os.X_OK):
            raise UnsafeInstallation("Registered Git executable is unavailable")
        existing_parent = next(
            path for path in (self.installation, *self.installation.parents) if path.exists()
        )
        if (
            self.source in self.environment.resolve().parents
            or self.source in self.installation.resolve().parents
            or self.source.stat().st_dev != existing_parent.stat().st_dev
        ):
            raise UnsafeInstallation(
                "Source swap requires an external environment on the same filesystem"
            )
        url = urlparse(metadata["url"])
        if (
            url.scheme != "file"
            or url.netloc
            or Path(url2pathname(url.path)).resolve() != self.source
        ):
            raise UnsafeInstallation("Editable source registration changed")
        if (
            not (self.source / ".git").is_dir()
            or (self.source / ".gitmodules").exists()
            or (self.source / ".git" / "index.lock").exists()
            or Path(self._git("rev-parse", "--show-toplevel")).resolve() != self.source
        ):
            raise UnsafeInstallation("Source worktree or submodule layout is unsupported")
        if self._git("remote", "get-url", "origin") != SOURCE_ORIGIN:
            raise UnsafeInstallation("Source checkout is not the registered upstream")
        if self._git("status", "--porcelain", "--untracked-files=all"):
            raise UnsafeInstallation("Source checkout has local modifications")
        return metadata

    def _stage(self, release: Release, destination: Path) -> None:
        # Verify the same published package as binary installs before fetching its
        # release tag. Git staging stays outside the live checkout and its index.
        super()._stage(release, destination)
        staged = destination / "source.git"
        self._run([self.executables["git"], "init", "--bare", str(staged)])
        self._git(
            "fetch", "--no-tags", SOURCE_ORIGIN, f"refs/tags/v{release.version}", repository=staged
        )
        target = self._git("rev-parse", "FETCH_HEAD^{commit}", repository=staged)
        original = self._git("rev-parse", "HEAD")
        # Reject local commits/divergence even when the working tree is clean.
        self._git("merge-base", "--is-ancestor", original, target, repository=staged)
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        manifest = _source_manifest(self.source)
        prepared = destination / "source-new"
        shutil.copytree(self.source, prepared, symlinks=True)
        if _source_manifest(prepared) != manifest or _source_manifest(self.source) != manifest:
            raise UnsafeInstallation("Source changed while preparing the release")
        self._git("fetch", "--no-tags", str(staged), target, repository=prepared)
        self._git("checkout", "--no-overwrite-ignore", "--detach", target, repository=prepared)
        _sync_tree(prepared)
        atomic_json(
            destination / "source.json",
            {
                "source": str(self.source),
                "original": original,
                "branch": branch,
                "target": target,
                "original_manifest": manifest,
                "target_manifest": _source_manifest(prepared),
            },
        )

    def snapshot(self, destination: Path) -> None:
        _check_windows_tree(self.source)
        _check_windows_tree(destination)
        record = json.loads((destination / "source.json").read_text())
        if (
            self._git("rev-parse", "HEAD") != record["original"]
            or _source_manifest(self.source) != record["original_manifest"]
        ):
            raise UnsafeInstallation("Source checkout changed since staging")
        super().snapshot(destination)

    def activate(self, destination: Path) -> None:
        self.preflight()
        _check_windows_tree(destination)
        record = json.loads((destination / "source.json").read_text())
        if (
            self._git("rev-parse", "HEAD") != record["original"]
            or _source_manifest(self.source) != record["original_manifest"]
        ):
            raise UnsafeInstallation("Source checkout changed since snapshot")
        original = destination / "source-original"
        if original.exists():
            raise UnsafeInstallation("Source swap already started; recovery is required")
        os.rename(self.source, original)
        sync_directory(self.source.parent)
        sync_directory(destination)
        os.rename(destination / "source-new", self.source)
        sync_directory(self.source.parent)
        sync_directory(destination)
        self._run(
            [str(self.python), "-m", "pip", "install", "--upgrade", "--editable", str(self.source)],
            mutation=True,
        )
        version = self._run(
            [
                str(self.python),
                "-c",
                "from importlib.metadata import version; print(version('bubo'))",
            ]
        ).strip()
        package = json.loads((destination / "package.json").read_text())
        if version != package["version"]:
            raise UnsafeInstallation("Source release version does not match the published package")

    def restore(self, destination: Path) -> None:
        if not self.writers_quiescent():
            raise UnsafeInstallation("Mutation writer is not proven quiescent")
        _check_windows_tree(destination)
        _check_windows_tree(self.source)
        record = json.loads((destination / "source.json").read_text())
        if record["source"] != str(self.source):
            raise UnsafeInstallation("Snapshot source mismatch")
        original = destination / "source-original"
        if original.exists():
            if _source_manifest(original) != record["original_manifest"]:
                raise UnsafeInstallation("Original source changed; preserve local work")
            if self.source.exists():
                if (
                    self._git("rev-parse", "HEAD") != record["target"]
                    or _source_manifest(self.source) != record["target_manifest"]
                ):
                    raise UnsafeInstallation("Source changed during update; preserve local work")
                os.rename(self.source, destination / "source-failed")
                sync_directory(self.source.parent)
                sync_directory(destination)
            os.rename(original, self.source)
            sync_directory(self.source.parent)
            sync_directory(destination)
        elif (
            not self.source.exists()
            or self._git("rev-parse", "HEAD") != record["original"]
            or _source_manifest(self.source) != record["original_manifest"]
        ):
            raise UnsafeInstallation("Original source is unavailable; preserve local work")
        super().restore(destination)


def writer_quiescent(writer: Path) -> bool:
    if not writer.exists():
        return True
    record = json.loads(writer.read_text())
    if record["state"] == "quiescent":
        return True
    if sys.platform != "win32" and record["state"] == "starting":
        from bubo.update_posix_exec import cancel_unstarted

        return cancel_unstarted(writer, record)
    if sys.platform == "win32":
        from bubo.update_windows_process import recover_job

        owner = record.get("owner", {})
        if not owner.get("start") or type(owner.get("pid")) is not int:
            return False
        if owner["pid"] != os.getpid() and _start_identity(owner["pid"]) == owner["start"]:
            return False
        if record.get("job_created") is True:
            recover_job(record.get("job"))
        elif record.get("job_created") is not False or record.get("state") != "starting":
            return False
        # No child may be spawned before successful job ownership publication.
        atomic_json(writer, {"state": "quiescent"})
        return True
    if record["state"] != "running" or not record.get("start"):
        return False
    pid = record["pid"]
    if type(pid) is not int or pid <= 0:
        return False
    if _start_identity(pid) == record["start"]:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(50):
            if (
                subprocess.run(
                    ["pgrep", "-g", str(pid)], capture_output=True, check=False
                ).returncode
                == 1
            ):
                atomic_json(writer, {"state": "quiescent"})
                return True
            time.sleep(0.1)
        return False
    # A missing leader is not proof its descendants are gone; a reused PID
    # is never killed merely because an old journal contains its number.
    return (
        subprocess.run(["pgrep", "-g", str(pid)], capture_output=True, check=False).returncode == 1
    )
