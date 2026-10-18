"""Retained resident parents: credentials stay in memory, intent stays on disk.

The helper controls parents through attempt-bound local mailboxes. Only a parent
with the actual Popen handle can acknowledge startup. After parent loss, a fresh
matching invocation may reclaim a proven quiescent role using its own credentials.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path

from bubo.update_admission import UpdateBusy, _file_lock, environment_lock
from bubo.update_identity import atomic_json
from bubo.update_installers import _start_identity
from bubo.update_readiness import ResidentReadiness, StartupRejected
from bubo.updater import ResidentRestartRequired

COMMAND_ENV = "BUBO_RESIDENT_COMMAND"
CHILD_ENV = "BUBO_RESIDENT_CHILD"
KINDS = {"service", "mcp-http", "mcp-stdio"}
_fallback_leases = ExitStack()
atexit.register(_fallback_leases.close)


class UnreapedProcess(subprocess.Popen):
    """Observe exit without releasing the PID while group termination is pending."""

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        result = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is None:
            return None
        return result.si_status if result.si_code == os.CLD_EXITED else -result.si_status


def _processes() -> dict[int, tuple[int, int, str]]:
    """PID -> (parent, process group, state), without reading argv or environment."""
    if sys.platform.startswith("linux"):
        result = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                result[int(path.parent.name)] = (int(fields[1]), int(fields[2]), fields[0])
            except FileNotFoundError:
                continue
        return result
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        int(pid): (int(parent), int(group), state)
        for pid, parent, group, state in (line.split() for line in result.stdout.splitlines())
    }


def _escaped(pid: int, table: dict) -> bool:
    descendants = {pid}
    while True:
        found = {child for child, (parent, _, _) in table.items() if parent in descendants}
        if found <= descendants:
            break
        descendants |= found
    return any(
        group != pid and not state.startswith("Z")
        for child, (_, group, state) in table.items()
        if child in descendants
    )


def _read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid resident record")
    return value


def _alive(record: dict) -> bool:
    return bool(record.get("identity")) and _start_identity(record["pid"]) == record["identity"]


def _orphan_quiescent(record: dict) -> bool:
    if _alive(record):
        return False
    child = record.get("child")
    if record.get("process_backend") == "windows-job-v1":
        from bubo.update_windows_process import _name, recover_job

        if child is None:
            return record.get("child_state") in {"not_started", "stopped"}
        if not isinstance(child, dict) or type(child.get("created")) is not bool:
            return False
        name = _name(child.get("job"))
        if child["created"]:
            recover_job(name)
            return True
        # No child may be created before the exclusive-Job callback commits.
        # This receipt never authorizes touching a possibly colliding Job name.
        return record.get("child_state") == "launching"
    if child is None:
        return record.get("child_state") in {"not_started", "stopped"}
    if not isinstance(child, dict) or not child.get("identity") or _alive(child):
        return False
    pid = child.get("pid")
    return type(pid) is int and not any(group == pid for _, group, _ in _processes().values())


def _replacement_matches(owner: dict, entry: dict, journal: dict) -> bool:
    return owner.get("replacement") == {
        "pid": entry["pid"],
        "identity": entry["identity"],
        "attempt_id": journal["attempt_id"],
        "token": journal["token"],
    }


def _pin_windows_legacy() -> None:
    """Exclude replacement for a client-owned Windows process until it exits."""
    from bubo.update_admission import _check_generation, environment_lock
    from bubo.update_helper import installation_for_environment

    environment = Path(sys.prefix)
    installation = installation_for_environment(environment)
    metadata = installation / "installation.json"
    # Registration uses the bridge until canonical admission is published.
    # Recheck after acquiring it to cover registration racing this launch.
    with ExitStack() as lifetime:
        bridge = ExitStack()
        lifetime.callback(bridge.close)
        if not metadata.exists():
            bridge.enter_context(environment_lock(environment, exclusive=False))
        if metadata.exists():
            lifetime.enter_context(_file_lock(installation / "activity.lock", exclusive=False))
            record = _read(metadata)
            if Path(record["environment"]).resolve() != environment.resolve():
                raise OSError("Installation registration changed during startup")
            _check_generation(installation)
            bridge.close()
        _fallback_leases.enter_context(lifetime.pop_all())


def delegate(kind: str) -> None:
    """Replace a registered resident entrypoint with its retained helper parent."""
    if os.environ.get(CHILD_ENV) or os.environ.get("BUBO_UPDATE_START_REQUEST"):
        return
    if os.name == "nt":
        try:
            _pin_windows_legacy()
        except Exception as exc:
            raise SystemExit(75) from exc
        return
    if os.name != "posix":
        return
    from bubo import paths
    from bubo.update_helper import _command, ensure_registered
    from bubo.update_helper import _read as read_installation
    from bubo.update_registration import load_registration

    installation = None
    registered = {}
    try:
        ensure_registered(paths.ROOT, state=paths.DB.parent)
        registered = load_registration(paths.DB.parent)
        if registered.get("external") == "docker":
            return
        installation = Path(registered["installation"])
        record = read_installation(installation)
        if not Path(record.get("archive", str(installation / "helper.pyz"))).is_file():
            raise OSError("Retained helper unavailable")
        env = dict(os.environ)
        env[COMMAND_ENV] = json.dumps([sys.executable, *sys.orig_argv[1:]])
        command = [
            *_command(installation, record),
            "--state",
            str(paths.DB.parent),
            "supervise",
            kind,
        ]
        os.execve(command[0], command, env)
    except OSError, ValueError, KeyError:
        if registered.get("ready"):
            from bubo.update_helper import _raw_config, analytics_config_from_dict
            from bubo.update_reporting import FailureStore
            from bubo.version import installed_version

            with suppress(Exception):
                FailureStore(paths.DB.parent).record(
                    attempt_id=str(uuid.uuid4()),
                    phase="bootstrap",
                    error_code="controller_unavailable",
                    producer_version=installed_version(),
                    cfg=analytics_config_from_dict(_raw_config(paths.DB.parent, root=paths.ROOT)),
                )
        # Setup failure must not prevent ordinary Bubo use. Keep this unmanaged
        # runtime pinned for its lifetime so a later registration cannot replace it.
        try:
            _fallback_leases.enter_context(environment_lock(Path(sys.prefix)))
            if installation and (installation / "activity.lock").is_file():
                _fallback_leases.enter_context(
                    _file_lock(installation / "activity.lock", exclusive=False)
                )
        except UpdateBusy:
            raise SystemExit(75) from None
        return


class ResidentSupervisor:
    def __init__(self, installation: Path, state: Path, kind: str, command: list[str]):
        if kind not in KINDS or os.name not in {"posix", "nt"}:
            raise ValueError("Unsupported resident supervisor")
        self.installation = installation.resolve()
        self.state = state.resolve()
        self.kind = kind
        self.windows = os.name == "nt"
        self.command = command
        self.cwd = os.getcwd()
        self.env = dict(os.environ)
        self.env.pop(COMMAND_ENV, None)
        self.env.pop("BUBO_UPDATE_START_REQUEST", None)
        self.env.pop("BUBO_MAINTENANCE_TOKEN", None)
        self.env[CHILD_ENV] = "1"
        self.directory = installation / "supervisors" / str(uuid.uuid4())
        self.record = {
            "protocol_version": 1,
            "pid": os.getpid(),
            "identity": _start_identity(os.getpid()),
            "state": str(self.state),
            "kind": kind,
            "closed": False,
            "child_state": "not_started",
            "launch_digest": hashlib.sha256(
                json.dumps(
                    [self.command, self.cwd, kind, str(self.state)],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
        if not self.record["identity"]:
            raise OSError("Resident parent identity unavailable")
        if self.windows:
            self.record["process_backend"] = "windows-job-v1"
        self.child: subprocess.Popen | None = None
        self.receipt: ResidentReadiness | None = None
        self.worker_broker = None
        self.maintenance = False
        self.cancelled = False
        self.last_request = None

    def _publish(self) -> None:
        atomic_json(self.directory / "owner.json", self.record)

    def _launch(self, *, outcome: str | None = None) -> None:
        if self.child is not None:
            raise OSError("Previous resident has not been reaped")
        self.receipt = (
            ResidentReadiness(self.state, self.kind, outcome=outcome) if outcome else None
        )
        env = self.receipt.environment(self.env) if self.receipt else self.env
        # An initialized stdio client cannot transfer its session to another server.
        # Probe the new server on private pipes; close the old connection after commit.
        probe = outcome is not None and self.kind == "mcp-stdio"
        self.record["child_state"] = "launching"
        if self.windows:
            from bubo.update_windows_process import new_job_name

            self.record["child"] = {"job": new_job_name(), "created": False}
        self._publish()
        if self.windows:
            from bubo.update_windows_broker import WorkerBroker
            from bubo.update_windows_resident import WindowsResidentProcess

            self.worker_broker = WorkerBroker(self.installation, self.state)
            env = {**env, **self.worker_broker.environment()}

            def created():
                self.record["child"]["created"] = True
                self._publish()

            self.child = WindowsResidentProcess(
                self.command,
                name=self.record["child"]["job"],
                cwd=self.cwd,
                env=env,
                probe=probe,
                control_input=self.kind == "mcp-http",
                on_job_created=created,
            )
            self.worker_broker.bind(self.child.job)
            self.record["child"].update(pid=self.child.pid, identity=self.child.identity())
        else:
            self.child = UnreapedProcess(
                self.command,
                cwd=self.cwd,
                env=env,
                start_new_session=True,
                stdin=subprocess.PIPE if probe else None,
                stdout=subprocess.DEVNULL if probe else None,
            )
            self.record["child"] = {
                "pid": self.child.pid,
                "identity": _start_identity(self.child.pid),
            }
        self.record["child_state"] = "running"
        self._publish()
        if self.receipt:
            self.receipt.bind(self.child)
            if probe:
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "bubo-update", "version": "1"},
                    },
                }
                self.child.stdin.write((json.dumps(request) + "\n").encode())
                self.child.stdin.flush()
            self.receipt.wait()

    def _stop(self) -> bool:
        if self.windows:
            return self._stop_windows()
        child = self.child
        if child is None:
            return True
        # Never signal a numeric group after its leader has been reaped. The
        # unreaped leader pins its PID even when readiness detects premature exit.
        owner = self.record.get("child", {})
        if (
            child.returncode is not None
            or not owner.get("identity")
            or _start_identity(child.pid) != owner["identity"]
        ):
            return False
        if self._quiescent():
            # macOS refuses group signals when only the zombie leader remains.
            # No live group member can fork, and the unreaped PID remains pinned.
            return self._reap_child()
        if not self.maintenance and child.poll() is None:
            # User-requested shutdown retains the resident's ordinary graceful
            # signal handling. Maintenance shutdown happens only under idle admission.
            os.killpg(child.pid, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
        try:
            os.killpg(child.pid, signal.SIGSTOP)
        except PermissionError, ProcessLookupError:
            # The last live member may have exited between observation and STOP.
            return self._reap_child() if self._quiescent() else False
        killed = False
        try:
            # Resident subprocesses may own separate sessions. Those normally
            # hold admission leases; if any still exist, defer instead of making
            # an unsupported claim that killing this group would stop them.
            if not self._frozen_group():
                return False
            # Keep the group frozen until killed: resuming it would permit a
            # new detached subprocess between the census and termination.
            os.killpg(child.pid, signal.SIGKILL)
            killed = True
        finally:
            if not killed:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGCONT)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            table = _processes()
            live = any(
                group == child.pid and not state.startswith("Z")
                for _, group, state in table.values()
            )
            if not live and child.poll() is not None:
                return self._reap_child()
            time.sleep(0.05)
        return False

    def _reap_child(self) -> bool:
        self.child.wait(timeout=3)
        if self.windows:
            self._close_worker_broker()
            self.child.close()
        elif self.child.stdin:
            self.child.stdin.close()
        return self._mark_stopped()

    def _close_worker_broker(self):
        if self.worker_broker is not None:
            self.worker_broker.close()
            self.worker_broker = None

    def _mark_stopped(self) -> bool:
        self._close_worker_broker()
        self.child = None
        self.receipt = None
        self.record.pop("child", None)
        self.record["child_state"] = "stopped"
        self._publish()
        return True

    def _stop_windows(self) -> bool:
        from bubo.update_windows_process import _name, recover_job

        owner = self.record.get("child")
        if owner is None:
            self._close_worker_broker()
            return self.child is None and self.record.get("child_state") in {
                "not_started",
                "stopped",
            }
        if not isinstance(owner, dict) or type(owner.get("created")) is not bool:
            return False
        name = _name(owner.get("job"))
        if self.child is not None:
            if self.child.name != name or not owner["created"]:
                return False
            self.child.retain_members()
            requested = False
            if self.kind == "service" and self.child.poll() is None:
                from bubo.update_windows_resident import request_service_stop

                with suppress(OSError):
                    requested = request_service_stop(self.state, self.child)
            elif self.kind == "mcp-http" and self.child.poll() is None:
                with suppress(OSError):
                    requested = self.child.request_stop()
            self._close_worker_broker()
            if requested:
                deadline = time.monotonic() + 3
                while not self.child.quiescent() and time.monotonic() < deadline:
                    time.sleep(0.05)
            self.child.close()
        elif owner["created"]:
            self._close_worker_broker()
            # Construction/publication may have failed after exclusive Job creation.
            # The current parent still owns that launch and must prove it stopped.
            recover_job(name)
        elif self.record.get("child_state") != "launching":
            return False
        self._close_worker_broker()
        return self._mark_stopped()

    def _quiescent(self) -> bool:
        if self.windows:
            return self.child.quiescent()
        return self.child.poll() is not None and not any(
            group == self.child.pid and not state.startswith("Z")
            for _, group, state in _processes().values()
        )

    def _frozen_group(self) -> bool:
        """A successful STOP send is not proof that every member processed it."""
        deadline = time.monotonic() + 3
        previous = None
        while time.monotonic() < deadline:
            table = _processes()
            if _escaped(self.child.pid, table):
                return False
            live = {
                pid: state
                for pid, (_, group, state) in table.items()
                if group == self.child.pid and not state.startswith("Z")
            }
            if all(state.startswith(("T", "t")) for state in live.values()):
                if set(live) == previous:
                    return True
                previous = set(live)
            else:
                previous = None
                os.killpg(self.child.pid, signal.SIGSTOP)
            time.sleep(0.02)
        return False

    def _handle(self, request: dict) -> bool:
        if request.get("parent") != {key: self.record[key] for key in ("pid", "identity")}:
            raise StartupRejected("Resident control parent changed")
        journal = _read(self.installation / "attempt.json")
        fence = _read(self.installation / "maintenance.json")
        if any(
            request.get(key) != item.get(key) or not request.get(key)
            for item in (journal, fence)
            for key in ("attempt_id", "token")
        ) or str(self.state) not in journal.get("states", []):
            raise StartupRejected("Resident control attempt changed")
        action = request.get("action")
        if action == "stop":
            self.maintenance = True
            return self._stop()
        if not self.maintenance:
            raise StartupRejected("Resident was not quiesced")
        if action == "start":
            self._launch(outcome=request["outcome"])
            return True
        if action == "verify":
            return bool(self.receipt and self.receipt.verify())
        raise ValueError("Unknown resident control operation")

    def _reattach(self) -> bool:
        """Claim an exact pending role using a fresh invocation's in-memory credentials."""
        journal = _read(self.installation / "attempt.json")
        binding = {key: journal[key] for key in ("attempt_id", "token")}
        if _read(self.installation / "maintenance.json") != binding:
            return False
        matches = []
        for entry in journal.get("resident_supervisors", []):
            if any(
                entry.get(key) != self.record.get(key) for key in ("state", "kind", "launch_digest")
            ):
                continue
            directory = Path(entry["directory"])
            if directory.parent != self.installation / "supervisors" or directory.is_symlink():
                return False
            owner = _read(directory / "owner.json")
            if any(owner.get(key) != entry.get(key) for key in ("state", "kind", "launch_digest")):
                return False
            if any(owner.get(key) != entry.get(key) for key in ("pid", "identity")) and not (
                _replacement_matches(owner, entry, journal)
            ):
                return False
            matches.append((entry, owner, directory))
        if len(matches) != 1:
            return False
        entry, owner, directory = matches[0]
        with (
            _file_lock(self.installation / "activity.lock", exclusive=True),
            _file_lock(self.state / "update-activity.lock", exclusive=True),
        ):
            if not _orphan_quiescent(owner):
                return False
            if _read(self.installation / "maintenance.json") != binding:
                return False
            self.directory = directory
            mailbox = directory / "command.json"
            self.last_request = _read(mailbox).get("request_id") if mailbox.exists() else None
            self.record["replacement"] = {
                "pid": entry["pid"],
                "identity": entry["identity"],
                **binding,
            }
            self.maintenance = True
            self._publish()
        return True

    def run(self) -> int:
        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: setattr(self, "cancelled", True))
        try:
            with _file_lock(self.installation / "registry.lock", exclusive=True):
                if (self.installation / "maintenance.json").exists():
                    if not self._reattach():
                        return 75
                else:
                    # Publish parent intent before any child exists.
                    self._publish()
                    self._launch()
            last = self.last_request
            while not self.cancelled:
                mailbox = self.directory / "command.json"
                if mailbox.exists():
                    request = _read(mailbox)
                    nonce = request.get("request_id")
                    if nonce != last:
                        last = nonce
                        try:
                            ok = self._handle(request)
                        except Exception:
                            ok = False
                        atomic_json(
                            self.directory / "response.json", {"request_id": nonce, "ok": ok}
                        )
                if (
                    self.maintenance
                    and self.receipt
                    and not (self.installation / "maintenance.json").exists()
                ):
                    if self.kind == "mcp-stdio":
                        return 75
                    self.maintenance = False
                    self.receipt = None
                if not self.maintenance and self.child:
                    result = self.child.poll()
                    if result is not None:
                        return result
                time.sleep(0.05)
            return 128 + signal.SIGTERM
        finally:
            if self._stop():
                self.record["closed"] = True
                self._publish()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


class ResidentController:
    """Engine adapter; all process ownership remains with retained parents."""

    def __init__(self, installation: Path, *, timeout: float = 40):
        self.installation = installation
        self.timeout = timeout

    def _request(self, entry: dict, journal: dict, action: str, outcome: str | None = None) -> bool:
        directory = Path(entry["directory"])
        owner = _read(directory / "owner.json")
        same_role = all(
            owner.get(key) == entry.get(key) for key in ("state", "kind", "launch_digest")
        )
        same_parent = all(owner.get(key) == entry.get(key) for key in ("pid", "identity"))
        if (
            same_role
            and not same_parent
            and _replacement_matches(owner, entry, journal)
            and _alive(owner)
        ):
            binding = {key: journal[key] for key in ("attempt_id", "token")}
            if _read(self.installation / "maintenance.json") != binding:
                raise OSError("Resident replacement recovery decision changed")
            entry.update(owner)
            atomic_json(self.installation / "attempt.json", journal)
        elif same_role and same_parent and _orphan_quiescent(owner):
            binding = {key: journal[key] for key in ("attempt_id", "token")}
            if _read(self.installation / "maintenance.json") != binding:
                raise OSError("Resident recovery decision changed")
            if action == "stop":
                return True
            raise ResidentRestartRequired(owner["kind"])
        if (
            owner.get("closed")
            or not same_role
            or any(owner.get(k) != entry.get(k) for k in ("pid", "identity", "state", "kind"))
            or not _alive(owner)
        ):
            raise OSError("Retained resident parent unavailable")
        nonce = str(uuid.uuid4())
        atomic_json(
            directory / "command.json",
            {
                "request_id": nonce,
                "parent": {key: owner[key] for key in ("pid", "identity")},
                "attempt_id": journal["attempt_id"],
                "token": journal["token"],
                "action": action,
                "outcome": outcome,
            },
        )
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if not _alive(owner):
                raise OSError("Retained resident parent exited")
            response = directory / "response.json"
            if response.exists():
                result = _read(response)
                if result.get("request_id") == nonce:
                    return result.get("ok") is True
            time.sleep(0.05)
        raise TimeoutError("Resident control acknowledgement timed out")

    def quiesce(self, journal: dict) -> None:
        entries = []
        # The engine holds the registry lock during this inventory and mutation.
        for path in sorted((self.installation / "supervisors").glob("*/owner.json")):
            record = _read(path)
            if not record.get("closed"):
                entries.append({**record, "directory": str(path.parent)})
        journal["resident_supervisors"] = entries
        atomic_json(self.installation / "attempt.json", journal)
        if not self.stop(journal):
            raise OSError("Resident quiescence unproven")

    def stop(self, journal: dict) -> bool:
        # No short-circuit: try stopping every known writer even if one fails.
        stopped = True
        for entry in journal.get("resident_supervisors", []):
            try:
                stopped = self._request(entry, journal, "stop") and stopped
            except Exception:
                stopped = False
        return stopped

    def start(self, journal: dict, *, outcome: str) -> None:
        for entry in journal.get("resident_supervisors", []):
            if not self._request(entry, journal, "start", outcome):
                raise OSError("Replacement resident failed startup")

    def verify(self, journal: dict, *, outcome: str) -> bool:
        return all(
            self._request(entry, journal, "verify", outcome)
            for entry in journal.get("resident_supervisors", [])
        )
