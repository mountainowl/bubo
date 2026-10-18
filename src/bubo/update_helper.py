"""Retained update control plane, independent of the environment being replaced."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import random
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
import zipapp
import zipfile
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from bubo.analytics import analytics_enabled
from bubo.analytics_config import AnalyticsConfig, analytics_config_from_dict
from bubo.update_admission import UpdateBusy, _file_lock, environment_lock
from bubo.update_identity import atomic_json, sync_directory
from bubo.update_installers import (
    PythonInstaller,
    SourceInstaller,
    UnsafeInstallation,
    latest_release,
)
from bubo.update_platform import current_owner
from bubo.update_reporting import FailureStore
from bubo.updater import Installer, UpdateEngine, UpdateResult
from bubo.version import installed_version

_SETUP_NOTICES: set[tuple[str, str]] = set()


def _setup_notice(root: Path, reason: str, message: str) -> None:
    key = (str(root), reason)
    if key not in _SETUP_NOTICES:
        _SETUP_NOTICES.add(key)
        print(message, file=sys.stderr)


_BUNDLE = (
    "__init__",
    "update_helper",
    "update_installers",
    "update_docker",
    "update_docker_source",
    "update_docker_readiness",
    "update_docker_job",
    "update_docker_migration",
    "update_docker_installer",
    "update_docker_runtime",
    "update_docker_setup",
    "update_retention",
    "update_locking",
    "update_platform",
    "update_windows_schedule",
    "update_windows_process",
    "update_windows_legacy",
    "update_posix_exec",
    "update_windows_files",
    "update_windows_jobs",
    "update_windows_worker",
    "update_windows_resident",
    "service_control",
    "update_windows_broker",
    "updater",
    "update_admission",
    "update_identity",
    "update_reporting",
    "update_readiness",
    "update_supervisor",
    "update_registration",
    "version",
    "analytics",
    "analytics_config",
    "paths",
    "env_config",
    "config_values",
    "errors",
    "secrets",
)


def _read(installation: Path) -> dict:
    record = json.loads((installation / "installation.json").read_text())
    if record.get("protocol_version") != 1 or record.get("owner") != current_owner():
        raise UnsafeInstallation("Unknown update registration owner or protocol")
    return record


def _state_for_root(root: Path) -> Path:
    """Resolve runtime storage without creating it or importing cached paths."""
    from bubo.env_config import expand_env_placeholders, section

    root = root.resolve()
    override = os.environ.get("BUBO_BASE_DIR")
    if override:
        base = Path(override)
        if not base.is_absolute():
            base = root / base
    else:
        config = root / "config" / "env.toml"
        cfg = tomllib.loads(config.read_text()) if config.exists() else {}
        value = section(cfg, "poller").get("state_dir", "var")
        base = Path(expand_env_placeholders(str(value), os.environ))
        if not base.is_absolute():
            base = root / base
    return base.resolve() / "state"


def _raw_config(state: Path, *, root: Path | None = None) -> dict:
    if (state / "update-registration-conflict.json").exists():
        raise ValueError("State directory has conflicting registration owners")
    if root is None:
        from bubo.update_registration import load_registration

        registration = load_registration(state)
        value = registration.get("root")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError("Registered configuration root is unavailable")
        root = Path(value)
    return tomllib.loads((root / "config" / "env.toml").read_text())


def _check_registration(state: Path, root: Path, installation: Path, *, writable: bool) -> dict:
    marker = state / "update-registration.json"
    if not marker.exists():
        return {}
    from bubo.update_registration import load_registration

    record = load_registration(state)
    if record.get("root") != str(root.resolve()) or record.get("installation") != str(installation):
        if writable:
            atomic_json(state / "update-registration-conflict.json", {"code": "shared_state"})
        raise UnsafeInstallation(
            "Separate state directories are required for different configurations"
        )
    return record


def auto_enabled(state: Path, *, root: Path | None = None) -> bool:
    try:
        value = _raw_config(state, root=root).get("auto_update", True)
        return isinstance(value, bool) and value
    except OSError, ValueError:
        return False


def analytics_policy(installation: Path, state: Path) -> tuple[AnalyticsConfig, bool | None]:
    """Read the same persisted consent as the app, including after detachment."""
    try:
        cfg = analytics_config_from_dict(_raw_config(state))
        if "${" in cfg.api_key or "${" in cfg.endpoint:
            return cfg, None
        return cfg, analytics_enabled(cfg)
    except OSError, ValueError, KeyError, TypeError:
        return AnalyticsConfig(enabled=False), None


def _bundle(installation: Path, python: Path) -> str:
    digest = hashlib.sha256()
    contents = {}
    for module in _BUNDLE:
        contents["bubo/" + module + ".py"] = Path(__file__).with_name(module + ".py").read_bytes()
        digest.update(contents["bubo/" + module + ".py"])
    contents["__main__.py"] = b"from bubo.update_helper import main\nraise SystemExit(main())\n"
    archive = installation / ("helper-" + digest.hexdigest()[:16] + ".pyz")
    if archive.exists():
        if archive.is_symlink() or getattr(archive.lstat(), "st_file_attributes", 0) & 0x400:
            raise UnsafeInstallation("Retained helper archive is redirected")
        with zipfile.ZipFile(archive) as existing:
            names = [name for name in existing.namelist() if not name.endswith("/")]
            if sorted(names) != sorted(contents) or any(
                existing.read(name) != content for name, content in contents.items()
            ):
                raise UnsafeInstallation("Retained helper archive does not match its version")
        # Windows prevents replacing an archive opened by a running helper.
        # The content-addressed archive is immutable and can be reused as-is.
    else:
        with tempfile.TemporaryDirectory(dir=installation) as directory:
            root = Path(directory)
            (root / "bubo").mkdir()
            for name, content in contents.items():
                (root / name).write_bytes(content)
            temporary = installation / "helper.pyz.new"
            zipapp.create_archive(root, target=temporary)
            os.replace(temporary, archive)
    if sys.platform != "win32":
        wrapper = installation / "bubo-update-helper"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([str(python), str(archive)]) + ' "$@"\n')
        wrapper.chmod(0o700)
    return str(archive)


def _command(installation: Path, record: dict) -> list[str]:
    return [
        record["python"],
        record.get("archive", str(installation / "helper.pyz")),
        "--installation",
        str(installation),
    ]


def _owned_schedule_command(installation: Path, record: dict, arguments: list[str]) -> bool:
    command = [*_command(installation, record), "run"]
    return (
        len(arguments) == len(command)
        and arguments[0] == command[0]
        and arguments[2:] == command[2:]
        and Path(arguments[1]).parent == installation
    )


def _owned_systemd_service(installation: Path, record: dict, content: str) -> bool:
    starts = [
        line.removeprefix("ExecStart=")
        for line in content.splitlines()
        if line.startswith("ExecStart=")
    ]
    return (
        content.startswith("# Bubo-owned updater " + installation.name + "\n")
        and len(starts) == 1
        and _owned_schedule_command(installation, record, shlex.split(starts[0].replace("%%", "%")))
    )


def _schedule_label(installation: Path, record: dict) -> str:
    if record.get("kind") == "docker":
        identity = hashlib.sha256(str(installation.resolve()).encode()).hexdigest()[:24]
        return "com.bubo.update.docker-" + identity
    return "com.bubo.update." + installation.name


def register_schedule(
    installation: Path, record: dict, *, home: Path, runner=subprocess.run
) -> str:
    """One native user service; no privileged installation or generic scheduler."""
    if sys.platform == "win32":
        from bubo.update_windows_schedule import schedule

        return schedule(installation, record, runner=runner)
    label = _schedule_label(installation, record)
    command = [*_command(installation, record), "run"]
    if sys.platform == "darwin":
        directory = home / "Library" / "LaunchAgents"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (label + ".plist")
        content = {
            "Label": label,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardErrorPath": str(installation / "helper.log"),
        }
        changed = False
        if path.exists():
            previous = plistlib.loads(path.read_bytes())
            arguments = previous.get("ProgramArguments", [])
            if previous.get("Label") != label or not _owned_schedule_command(
                installation, record, arguments
            ):
                raise UnsafeInstallation("Refusing to overwrite an unrelated launch agent")
            changed = arguments != command
        path.write_bytes(plistlib.dumps(content))
        domain = f"gui/{os.getuid()}"
        existing = runner(
            ["launchctl", "print", f"{domain}/{label}"], capture_output=True, check=False
        )
        if changed and not existing.returncode:
            runner(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=True)
        if existing.returncode or changed:
            runner(["launchctl", "bootstrap", domain, str(path)], capture_output=True, check=True)
        return str(path)
    if sys.platform.startswith("linux"):
        directory = home / ".config" / "systemd" / "user"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (label + ".service")
        text = (
            "# Bubo-owned updater " + installation.name + "\n[Unit]\nDescription=Bubo updates\n"
            "[Service]\nType=simple\nExecStart="
            + shlex.join(command).replace("%", "%%")
            + "\nRestart=on-failure\n[Install]\nWantedBy=default.target\n"
        )
        if path.exists() and not _owned_systemd_service(installation, record, path.read_text()):
            raise UnsafeInstallation("Refusing to overwrite an unrelated service")
        changed = path.exists() and path.read_text() != text
        path.write_text(text)
        runner(["systemctl", "--user", "daemon-reload"], capture_output=True, check=True)
        runner(
            ["systemctl", "--user", "enable", "--now", path.name], capture_output=True, check=True
        )
        if changed:
            runner(["systemctl", "--user", "restart", path.name], capture_output=True, check=True)
        return str(path)
    raise UnsafeInstallation("Native scheduling is not available for this platform")


def remove_schedule(installation: Path, record: dict, *, home: Path, runner=subprocess.run) -> None:
    """Remove only a verified Bubo-owned native registration, retaining recovery data."""
    if sys.platform == "win32":
        from bubo.update_windows_schedule import schedule

        schedule(installation, record, remove=True, runner=runner)
        return
    label = _schedule_label(installation, record)
    if sys.platform == "darwin":
        path = home / "Library" / "LaunchAgents" / (label + ".plist")
        if not path.exists():
            return
        previous = plistlib.loads(path.read_bytes())
        if previous.get("Label") != label or not _owned_schedule_command(
            installation, record, previous.get("ProgramArguments", [])
        ):
            raise UnsafeInstallation("Refusing to remove an unrelated launch agent")
        domain = f"gui/{os.getuid()}"
        loaded = runner(
            ["launchctl", "print", f"{domain}/{label}"], capture_output=True, check=False
        )
        if loaded.returncode == 0:
            runner(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=True)
        path.unlink()
        sync_directory(path.parent)
        return
    if sys.platform.startswith("linux"):
        path = home / ".config" / "systemd" / "user" / (label + ".service")
        if not path.exists():
            return
        content = path.read_text()
        if not _owned_systemd_service(installation, record, content):
            raise UnsafeInstallation("Refusing to remove an unrelated service")
        runner(
            ["systemctl", "--user", "disable", "--now", path.name], capture_output=True, check=True
        )
        path.unlink()
        sync_directory(path.parent)
        runner(["systemctl", "--user", "daemon-reload"], capture_output=True, check=True)
        return
    raise UnsafeInstallation("Native scheduling is not available for this platform")


def uninstall_helper(
    installation: Path, *, home: Path | None = None, remove=remove_schedule
) -> None:
    with _file_lock(installation / "attempt.lock", exclusive=True):
        if (installation / "maintenance.json").exists():
            raise UnsafeInstallation("Complete pending recovery before removing the update helper")
        record = _read(installation)
        remove(installation, record, home=home or Path.home())
        atomic_json(installation / "disabled.json", {"disabled": True})


def _check_legacy_processes(environment: Path) -> None:
    """Old residents do not know our locks. Defer adoption while any may exist."""
    if sys.platform == "win32":
        from bubo.update_windows_legacy import check_legacy_processes

        check_legacy_processes(environment)
        return
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5, check=True
    )
    environment = environment.resolve()
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) == os.getpid():
            continue
        try:
            arguments = shlex.split(parts[1])
        except ValueError:
            # A malformed command belonging to this environment is still unsafe.
            if str(environment) in parts[1]:
                raise UnsafeInstallation(
                    "Existing process requires restart before update setup"
                ) from None
            continue
        belongs = any(
            argument.startswith("/")
            and (
                environment in Path(argument).absolute().parents
                or environment in Path(argument).resolve().parents
            )
            for argument in arguments
        )
        bubo = any(
            argument.startswith("bubo.")
            or Path(argument).name == "bubo"
            or Path(argument).name.startswith("bubo-")
            or "bubo" in Path(argument).parts
            for argument in arguments
        )
        if belongs and bubo:
            raise UnsafeInstallation("Existing Bubo process requires restart before update setup")


def installation_for_environment(environment: Path, *, home: Path | None = None) -> Path:
    identity = hashlib.sha256(str(environment.resolve()).encode()).hexdigest()[:16]
    return (home or Path.home()) / ".local" / "share" / "bubo-updater" / identity


def bootstrap(
    root: Path,
    *,
    refresh: bool = False,
    environment: Path | None = None,
    home: Path | None = None,
    schedule=None,
    state: Path | None = None,
) -> Path:
    if schedule is None:
        schedule = register_schedule
    root = root.resolve()
    state = state.resolve() if state is not None else _state_for_root(root)
    environment = (environment or Path(sys.prefix)).absolute()
    home = home or Path.home()
    installation = installation_for_environment(environment, home=home)
    kind = (
        "uv"
        if (environment / "uv-receipt.toml").exists()
        else ("pipx" if (environment / "pipx_metadata.json").exists() else "pip")
    )
    bin_directory = None
    if kind == "uv":
        output = subprocess.check_output(["uv", "tool", "dir", "--bin"], text=True)
        bin_directory = Path(output.strip())
    elif kind == "pipx":
        output = subprocess.check_output(
            ["pipx", "environment", "--value", "PIPX_BIN_DIR"], text=True
        )
        bin_directory = Path(output.strip())
    installer = PythonInstaller(environment, installation, kind=kind, bin_directory=bin_directory)
    direct = json.loads(
        installer._run(
            [
                str(installer.python),
                "-c",
                "from importlib.metadata import distribution; "
                "print(distribution('bubo').read_text('direct_url.json') or '{}')",
            ]
        )
    )
    source = None
    if direct.get("dir_info", {}).get("editable"):
        location = urlparse(direct.get("url", ""))
        if kind != "pip" or location.scheme != "file" or location.netloc:
            raise UnsafeInstallation("Editable installation ownership is unsupported")
        source = Path(url2pathname(location.path)).resolve()
        kind = "editable"
        installer = SourceInstaller(environment, installation, source)
    installer.preflight()  # no host files/schedules are touched for unsafe installations
    python = Path(sys._base_executable).resolve()
    if environment.resolve() in python.parents:
        raise UnsafeInstallation(
            "A retained Python interpreter outside the installation is required"
        )
    installation.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.mkdir(parents=True, exist_ok=True)
    for name in ("registry.lock", "activity.lock", "attempt.lock", "sender.lock"):
        (installation / name).touch(mode=0o600, exist_ok=True)
    (state / "update-activity.lock").touch(mode=0o600, exist_ok=True)
    with (
        _file_lock(installation / "attempt.lock", exclusive=True),
        environment_lock(environment, exclusive=True),
    ):
        if not (installation / "installation.json").exists():
            _check_legacy_processes(environment)
        with _file_lock(installation / "registry.lock", exclusive=True):
            _check_registration(state, root, installation, writable=True)
            if (installation / "maintenance.json").exists():
                raise UnsafeInstallation("Finish update recovery before changing registration")
            if (installation / "installation.json").exists():
                record = _read(installation)
            else:
                version = installer._run(
                    [
                        str(installer.python),
                        "-c",
                        "from importlib.metadata import version; print(version('bubo'))",
                    ]
                ).strip()
                record = {
                    "protocol_version": 1,
                    "owner": current_owner(),
                    "environment": str(environment),
                    "python": str(python),
                    "kind": kind,
                    "source": str(source) if source else None,
                    "bin_directory": str(bin_directory) if bin_directory else None,
                    "states": [],
                    "source_version": version,
                    "helper_version": installed_version(),
                    "next_check": time.time() + random.uniform(0, 900),
                }
            if str(state) not in record["states"]:
                record["states"].append(str(state))
                record["states"].sort()
            record["archive"] = _bundle(installation, python)
            record["executables"] = installer.executables
            atomic_json(installation / "installation.json", record)
            atomic_json(
                state / "update-registration.json",
                {
                    "installation": str(installation),
                    "ready": False,
                    "root": str(root),
                    "base_dir": str(state.parent),
                    "state_dir": str(state),
                },
            )
        schedule(installation, record, home=home)
        (installation / "disabled.json").unlink(missing_ok=True)
        atomic_json(
            state / "update-registration.json",
            {
                "installation": str(installation),
                "ready": True,
                "root": str(root),
                "base_dir": str(state.parent),
                "state_dir": str(state),
            },
        )
    return installation


def _installer(installation: Path, record: dict) -> Installer:
    if record["kind"] == "docker":
        from bubo.update_docker import DockerAPI
        from bubo.update_docker_installer import DockerInstaller

        if len(record["states"]) != 1:
            raise UnsafeInstallation("Docker requires one mapped persistent state")
        api = DockerAPI(Path(record["docker"]["socket"]))
        if record["docker"].get("daemon_id") and (
            api.request("GET", "/info").get("ID") != record["docker"]["daemon_id"]
        ):
            raise UnsafeInstallation("Docker controller daemon changed")
        return DockerInstaller(
            api,
            Path(record["states"][0]),
            record["docker"]["container_id"],
        )
    if record["kind"] == "editable":
        return SourceInstaller(
            Path(record["environment"]),
            installation,
            Path(record["source"]),
            executables=record.get("executables"),
        )
    return PythonInstaller(
        Path(record["environment"]),
        installation,
        kind=record["kind"],
        bin_directory=Path(record["bin_directory"]) if record["bin_directory"] else None,
        executables=record.get("executables"),
    )


def _local_command(
    store: FailureStore,
    action: str,
    attempt_id: str | None,
    *,
    installation: Path | None = None,
    root: Path | None = None,
) -> int:
    if action == "report":
        print(store.report(attempt_id), end="")
    else:
        status = {
            "auto_update": auto_enabled(store.state, root=root),
            "helper_configured": False,
            "helper_disabled": False,
            "update": {"status": "setup_required"},
            "failures": store.status(attempt_id),
        }
        if installation is not None:
            marker = store.state / "update-registration.json"
            status["helper_configured"] = marker.exists() and json.loads(marker.read_text()).get(
                "ready", True
            )
            status["helper_configured"] = status["helper_configured"] and _read(installation).get(
                "ready", True
            )
            status["helper_disabled"] = (installation / "disabled.json").exists()
            current = installation / "status.json"
            status["update"] = (
                json.loads(current.read_text())
                if current.exists()
                else {"status": "ready" if status["helper_configured"] else "setup_required"}
            )
            if (installation / "maintenance.json").exists():
                status["update"] = {"status": "recovery_required"}
        print(json.dumps(status, indent=2))
    return 0


def ensure_registered(root: Path, *, state: Path | None = None) -> None:
    """Best-effort writable startup; diagnostics never prevent ordinary work."""
    root = root.resolve()
    try:
        state = state.resolve() if state is not None else _state_for_root(root)
    except OSError, ValueError, KeyError, TypeError:
        _setup_notice(
            root,
            "configuration",
            "Bubo update setup unavailable: check runtime state configuration.",
        )
        return
    installation = installation_for_environment(Path(sys.prefix))
    try:
        from bubo.update_registration import load_registration

        if (state / "update-registration.json").exists():
            external = load_registration(state)
            if external.get("external") == "docker":
                _check_registration(state, root, Path(external["installation"]), writable=True)
                return
        record = _check_registration(state, root, installation, writable=True)
        if record.get("ready", False):
            return
    except OSError, ValueError:
        _setup_notice(
            root, "ownership", "Bubo update deferred: state registration ownership needs repair."
        )
        return
    if (installation / "disabled.json").exists():
        return
    if not auto_enabled(state, root=root) and not (installation / "installation.json").exists():
        return
    notice = state / "update-bootstrap.json"
    try:
        previous = json.loads(notice.read_text()) if notice.exists() else {}
        if previous.get("retry_after", 0) > time.time():
            return
        bootstrap(root, state=state)
        notice.unlink(missing_ok=True)
    except Exception:
        attempt = str(uuid.uuid4())
        try:
            cfg = analytics_config_from_dict(_raw_config(state, root=root))
            FailureStore(state).record(
                attempt_id=attempt,
                phase="bootstrap",
                error_code="controller_unavailable",
                cfg=cfg,
                producer_version=installed_version(),
            )
            previous = json.loads(notice.read_text()) if notice.exists() else {}
            atomic_json(notice, {"retry_after": time.time() + 86400, "code": "setup_required"})
            if previous.get("code") != "setup_required":
                print(
                    "Bubo automatic updates need setup: run `bubo update configure`; "
                    f"details: `bubo update report {attempt}`.",
                    file=sys.stderr,
                )
        except Exception:
            # A read-only/full state directory must not break existing work.
            _setup_notice(
                root, "storage", "Bubo update setup unavailable; run `bubo update configure`."
            )


def cli_command(
    root: Path,
    action: str,
    attempt_id: str | None = None,
    *,
    container: str | None = None,
    docker_socket: Path | None = None,
) -> int:
    """Control dispatch never holds an activity lease while awaiting the helper."""
    if container is not None or docker_socket is not None:
        if action != "configure" or not container or docker_socket is None:
            print("--container and --docker-socket are required together for update configure.")
            return 1
        try:
            from bubo.update_docker_setup import configure

            configure(root, container, docker_socket)
            print("Bubo Docker update controller configured.")
            return 0
        except Exception as exc:
            print(f"Docker update setup deferred ({type(exc).__name__}).")
            if getattr(exc, "bubo_report", None):
                print(f"Report: {exc.bubo_report}")
            return 1
    try:
        state = _state_for_root(root)
    except OSError, ValueError, KeyError, TypeError:
        print("Bubo update deferred: check runtime state configuration.", file=sys.stderr)
        return 75
    registration = state / "update-registration.json"
    if action == "uninstall-helper" and not registration.exists():
        print("Bubo update helper is not registered.")
        return 0
    if action in {"status", "report"} and not registration.exists():
        return _local_command(FailureStore(state), action, attempt_id, root=root)
    try:
        from bubo.update_registration import load_registration

        existing = load_registration(state) if registration.exists() else {}
        if existing.get("external") == "docker":
            external_installation = Path(existing["installation"])
            _check_registration(state, root, external_installation, writable=False)
            if action in {"status", "report"}:
                return _local_command(
                    FailureStore(state),
                    action,
                    attempt_id,
                    installation=external_installation,
                    root=root,
                )
            if existing["view"] != "host" or action == "configure":
                print(
                    "Docker updates are managed by the external host controller.", file=sys.stderr
                )
                return 75
        if action == "configure" or not registration.exists():
            if not (root / "config" / "env.toml").exists():
                print(
                    "Bubo update deferred: run bubo init for this installation first.",
                    file=sys.stderr,
                )
                return 75
            installation = bootstrap(root, refresh=action == "configure")
            if action == "configure":
                print("Bubo update helper configured.")
                if sys.platform == "win32":
                    print("Use this MCP client configuration for idle updates:")
                    print(
                        json.dumps(
                            {
                                "mcpServers": {
                                    "bubo": resident_launch(installation, state, "mcp-stdio")
                                }
                            },
                            indent=2,
                        )
                    )
                return 0
        else:
            installation = Path(load_registration(state)["installation"])
            _check_registration(
                state, root, installation, writable=action not in {"status", "report"}
            )
        record = _read(installation)
        command = [*_command(installation, record), "--state", str(state), action]
        if attempt_id:
            command.append(attempt_id)
        return subprocess.run(command, check=False).returncode
    except OSError, ValueError, KeyError, subprocess.SubprocessError:
        if action in {"status", "report"}:
            try:
                if registration.exists():
                    registered = load_registration(state)
                    _check_registration(
                        state, root, Path(registered["installation"]), writable=False
                    )
            except OSError, ValueError, KeyError:
                print(
                    "Bubo update deferred: state registration ownership needs repair.",
                    file=sys.stderr,
                )
                return 75
            return _local_command(FailureStore(state), action, attempt_id, root=root)
        attempt = str(uuid.uuid4())
        try:
            cfg = analytics_config_from_dict(_raw_config(state, root=root))
            FailureStore(state).record(
                attempt_id=attempt,
                phase="bootstrap",
                error_code="controller_unavailable",
                cfg=cfg,
                producer_version=installed_version(),
            )
        except Exception:
            pass
        print(
            "Bubo update deferred: a safe retained helper could not be configured. "
            f"Inspect bubo update report {attempt}.",
            file=sys.stderr,
        )
        return 75


def tick(
    installation: Path, *, trigger: str = "automatic", state: Path | None = None
) -> UpdateResult:
    record = _read(installation)
    if trigger == "automatic" and (installation / "disabled.json").exists():
        return UpdateResult("deferred", code="helper_disabled")
    states = [Path(value) for value in record["states"]]
    state = state or states[0]
    cfg, allowed = analytics_policy(installation, state)
    for item in states:
        config, _ = analytics_policy(installation, item)
        # Broken diagnostics must not stop a pending recovery transaction.
        with suppress(Exception):
            FailureStore(item).drain(
                config, limit=1, consent=lambda item=item: analytics_policy(installation, item)[1]
            )
    installer = _installer(installation, record)
    generation = installation / "generation.json"
    if generation.exists():
        record["source_version"] = json.loads(generation.read_text())["version"]
    from bubo.update_supervisor import ResidentController

    residents = ResidentController(installation)
    if record["kind"] == "docker":
        from bubo.update_docker_runtime import DockerResidents

        residents = DockerResidents(installer)
    engine = UpdateEngine(
        installation,
        states,
        installer,
        source_version=record["source_version"],
        producer_version=record["helper_version"],
        analytics=replace(cfg, enabled=allowed is True),
        enabled=auto_enabled,
        consent=lambda: analytics_policy(installation, state)[1],
        reporting_policy=lambda reporting_state: analytics_policy(installation, reporting_state),
        residents=residents,
    )
    if (installation / "maintenance.json").exists():
        result = engine.recover()
        if generation.exists():
            record["source_version"] = json.loads(generation.read_text())["version"]
        _cleanup_installer(installer, installation, state)
        return _publish_result(installation, record, result)
    if record.get("ready") is False:
        return UpdateResult("deferred", code="setup_required")
    if trigger == "automatic" and (
        not all(auto_enabled(item) for item in states) or time.time() < record["next_check"]
    ):
        return UpdateResult("deferred", code="not_due")
    attempt = str(uuid.uuid4())
    _cleanup_installer(installer, installation, state)
    try:
        release = latest_release(record["source_version"])
        if release and record["kind"] == "docker":
            from bubo.update_docker import resolve_release

            container = installer.api.inspect_container(installer.container_id)
            image = installer.api.inspect_image(container["Image"])
            release = resolve_release(installer.api, release, architecture=image["Architecture"])
        result = (
            engine.run(release, trigger=trigger, initiating_state=state)
            if release
            else UpdateResult("current")
        )
        if result.status == "updated":
            record["source_version"] = release.version
    except Exception:
        FailureStore(state).record(
            attempt_id=attempt,
            phase="discovery",
            error_code="unexpected_error",
            cfg=cfg,
            consent=allowed is True,
            install_type=record["kind"],
            from_version=record["source_version"],
            producer_version=record["helper_version"],
        )
        result = UpdateResult("failed", attempt, "discovery_failed")
    if result.status == "updated" or result.code == "restored":
        _cleanup_installer(installer, installation, state)
    return _publish_result(installation, record, result)


def _cleanup_installer(installer: Installer, installation: Path, state: Path) -> None:
    cleanup = getattr(installer, "cleanup", None)
    try:
        from bubo.update_retention import prune

        if cleanup is not None:
            cleanup()
        prune(installation)
    except UpdateBusy:
        return
    except Exception:
        try:
            cfg, allowed = analytics_policy(installation, state)
            FailureStore(state).record(
                attempt_id=str(uuid.uuid4()),
                phase="cleanup",
                error_code="cleanup_failed",
                cfg=cfg,
                consent=allowed is True,
                install_type=installer.kind,
            )
        except Exception:
            _setup_notice(
                installation,
                "cleanup_failed",
                "Bubo update cleanup deferred; local diagnostic storage unavailable.",
            )


def _publish_result(installation: Path, record: dict, result: UpdateResult) -> UpdateResult:
    record["next_check"] = time.time() + (60 if result.code == "busy" else 86400)
    with _file_lock(installation / "registry.lock", exclusive=True):
        current = _read(installation)
        current.update(next_check=record["next_check"], source_version=record["source_version"])
        atomic_json(installation / "installation.json", current)
    atomic_json(
        installation / "status.json",
        {"status": result.status, "code": result.code, "attempt_id": result.attempt_id},
    )
    return result


def resident_launch(installation: Path, state: Path, kind: str) -> dict:
    """Describe the stable process a Windows client should launch directly."""
    from bubo.update_registration import load_registration

    if kind not in {"mcp-stdio", "mcp-http", "service"}:
        raise ValueError("Unsupported resident supervisor")
    record = _read(installation)
    registration = load_registration(state)
    root = Path(registration["root"])
    _check_registration(state, root, installation, writable=False)
    if (
        str(state) not in record["states"]
        or registration.get("external")
        or not registration.get("ready", True)
        or not record.get("ready", True)
    ):
        raise UnsafeInstallation("Resident update registration is not ready")
    command = _command(installation, record)
    return {
        "command": command[0],
        "args": [*command[1:], "--state", str(state), "supervise", kind],
        "env": {
            "BUBO_ROOT": str(root),
            "BUBO_BASE_DIR": str(state.parent),
            **(
                {"BUBO_MCP_TRANSPORT": "stdio" if kind == "mcp-stdio" else "http"}
                if kind != "service"
                else {}
            ),
        },
    }


def _direct_resident_command(installation: Path, state: Path, kind: str) -> list[str]:
    from bubo.update_platform import environment_python

    launch = resident_launch(installation, state, kind)
    record = _read(installation)
    environment = Path(record["environment"])
    python = environment_python(environment)
    if not python.is_file():
        raise UnsafeInstallation("Registered resident interpreter is unavailable")
    os.environ.update(launch["env"])
    if kind == "service":
        return [str(python), "-m", "bubo.poller", "service", "start", "--foreground"]
    return [str(python), "-c", "from bubo.mcp_server import main; main()"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bubo-update-helper")
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument(
        "action",
        choices=[
            "run",
            "update",
            "status",
            "report",
            "retry-failures",
            "uninstall-helper",
            "supervise",
        ],
    )
    parser.add_argument("attempt_id", nargs="?")
    args = parser.parse_args(argv)
    record = _read(args.installation)
    state = args.state or Path(record["states"][0])
    if str(state) not in record["states"]:
        parser.error("State root is not registered")
    if args.action == "supervise":
        from bubo.update_supervisor import COMMAND_ENV, ResidentSupervisor

        invocation = os.environ.pop(COMMAND_ENV, None)
        command = (
            json.loads(invocation)
            if invocation is not None
            else _direct_resident_command(args.installation, state, args.attempt_id)
        )
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
        ):
            raise ValueError("Invalid resident invocation")
        return ResidentSupervisor(args.installation, state, args.attempt_id, command).run()
    store = FailureStore(state)
    if args.action == "uninstall-helper":
        try:
            uninstall_helper(args.installation)
        except OSError, subprocess.SubprocessError:
            print(
                "Bubo update helper removal deferred: check recovery and native scheduler status.",
                file=sys.stderr,
            )
            return 75
        print("Bubo update helper removed. Manual updates and local reports remain available.")
        return 0
    if args.action in {"status", "report"}:
        return _local_command(store, args.action, args.attempt_id, installation=args.installation)
    if args.action == "retry-failures":
        if not args.attempt_id:
            parser.error("retry-failures requires an attempt ID")
        cfg, allowed = analytics_policy(args.installation, state)
        if allowed is True:
            store.retry(args.attempt_id, replace(cfg, enabled=True))
            store.drain(cfg, consent=lambda: analytics_policy(args.installation, state)[1])
        elif allowed is False:
            store.suppress()
        return 0
    if args.action == "update":
        result = tick(args.installation, trigger="manual", state=state)
        print(f"Bubo update: {result.status}" + (f" ({result.code})" if result.code else ""))
        if result.attempt_id:
            print(f"Report: bubo update report {result.attempt_id}")
        return result.exit_code
    with _file_lock(args.installation / "sender.lock", exclusive=True):
        while True:
            if (args.installation / "disabled.json").exists():
                return 0
            try:
                tick(args.installation)
            except Exception:
                print("Bubo update helper deferred: inspect local update status.", file=sys.stderr)
            time.sleep(60)
