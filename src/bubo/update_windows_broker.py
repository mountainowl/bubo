"""Transient authenticated worker launches from outside the resident Job.

Only the retained supervisor owns this endpoint. Requests and launch environment
stay in memory; output handles must belong to its exact resident Job. There is no
retry after sending a launch request: a lost reply can mean a committed worker.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import struct
import threading
import time
import uuid

ENDPOINT = "BUBO_WORKER_BROKER"
TOKEN = "BUBO_WORKER_BROKER_TOKEN"
_LIMIT = 1024 * 1024


class LaunchOutcomeUnknown(OSError):
    """A worker may already be running; never automatically resubmit this launch."""


def _read(connection, size, deadline):
    chunks = bytearray()
    while len(chunks) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Worker launch envelope timed out")
        connection.settimeout(remaining)
        data = connection.recv(size - len(chunks))
        if not data:
            raise EOFError("Worker launch connection closed")
        chunks.extend(data)
    return bytes(chunks)


def _receive(connection):
    deadline = time.monotonic() + (connection.gettimeout() or 2)
    (size,) = struct.unpack("!I", _read(connection, 4, deadline))
    if not 0 < size <= _LIMIT:
        raise ValueError("Invalid worker launch envelope size")
    return json.loads(_read(connection, size, deadline))


def _send(connection, message):
    data = json.dumps(message, separators=(",", ":")).encode()
    if len(data) > _LIMIT:
        raise ValueError("Worker launch envelope is too large")
    connection.sendall(struct.pack("!I", len(data)) + data)


class WorkerBroker:
    def __init__(self, installation, state):
        self.installation, self.state = installation, state
        self.token = secrets.token_hex(32)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.stopping = threading.Event()
        self.bound = threading.Event()
        self.job = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="bubo-worker-launch")
        try:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(4)
            self.listener.settimeout(0.1)
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise

    def environment(self):
        return {ENDPOINT: str(self.listener.getsockname()[1]), TOKEN: self.token}

    def bind(self, job):
        if not job:
            raise ValueError("Resident Job is unavailable")
        self.job = job
        self.bound.set()

    def close(self):
        self.stopping.set()
        self.bound.set()
        self.listener.close()
        self.thread.join(20)
        if self.thread.is_alive():
            raise TimeoutError("Worker broker has not retired its pending launch")

    def _run(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    request = _receive(connection)
                    if not isinstance(request, dict) or not isinstance(request.get("token"), str):
                        continue
                    if not hmac.compare_digest(request["token"], self.token):
                        continue
                    request_id = str(uuid.UUID(request["id"]))
                    if request_id != request["id"]:
                        continue
                    # The response contains no arguments, environment, or native errors.
                    try:
                        pid = self._launch(request)
                    except LaunchOutcomeUnknown:
                        response = {"id": request_id, "status": "unknown"}
                    except Exception:
                        response = {"id": request_id, "status": "failed"}
                    else:
                        response = {"id": request_id, "status": "started", "pid": pid}
                    _send(connection, response)
                except OSError, EOFError, ValueError, KeyError, TypeError, RecursionError:
                    # A committed worker survives a disconnected client. Never retry.
                    continue

    def _launch(self, request):
        from .update_windows_process import duplicate_output
        from .update_windows_worker import spawn

        args, env = request["args"], request["env"]
        if (
            not isinstance(args, list)
            or not args
            or not all(isinstance(x, str) for x in args)
            or not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            or not isinstance(request["cwd"], str)
            or not isinstance(request["version"], str)
            or not request["version"]
        ):
            raise ValueError("Invalid worker launch configuration")
        if not self.bound.wait(10) or self.stopping.is_set():
            raise OSError("Resident launch is unavailable")
        pid = None
        try:
            with duplicate_output(
                request["pid"], request["identity"], request["handle"], resident_job=self.job
            ) as output:
                environment = {k: v for k, v in env.items() if k not in {ENDPOINT, TOKEN}}
                try:
                    pid = spawn(
                        args,
                        self.installation,
                        self.state,
                        stdout=output,
                        env=environment,
                        cwd=request["cwd"],
                        process_version=request["version"],
                    )
                except Exception as error:
                    # A failed abort may leave a live guardian. Only the existing
                    # ownership recovery protocol can prove that launch stopped.
                    raise LaunchOutcomeUnknown("Guardian launch needs reconciliation") from error
        except Exception:
            if pid is None:
                raise
            # A failure closing our borrowed output cannot undo guardian handoff.
        return pid


def spawn_via_broker(args, *, stdout):
    import msvcrt

    from .update_admission import _PROCESS_VERSION
    from .update_platform import windows_process_identity

    request_id = str(uuid.uuid4())
    port = int(os.environ[ENDPOINT])
    if not 0 < port < 65536 or not os.environ.get(TOKEN):
        raise OSError("Resident worker broker is unavailable")
    request = {
        "id": request_id,
        "token": os.environ[TOKEN],
        "args": [os.fspath(arg) for arg in args],
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "identity": windows_process_identity(os.getpid()),
        "version": _PROCESS_VERSION,
        "handle": msvcrt.get_osfhandle(stdout.fileno()),
    }
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.settimeout(15)
        try:
            _send(connection, request)
            response = _receive(connection)
        except (OSError, EOFError, ValueError, RecursionError) as error:
            raise LaunchOutcomeUnknown(
                f"Worker launch outcome unknown ({request_id}); check worker status before retrying"
            ) from error
        if isinstance(response, dict) and response.get("id") == request_id:
            if (
                response.get("status") == "started"
                and type(response.get("pid")) is int
                and response["pid"] > 0
            ):
                return response["pid"]
            if response.get("status") == "failed":
                raise OSError(f"Worker launch failed ({request_id})")
        raise LaunchOutcomeUnknown(
            f"Worker launch outcome unknown ({request_id}); check worker status before retrying"
        )
