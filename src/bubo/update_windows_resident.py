"""Windows process/stdio boundary for the existing resident supervisor.

Resident Job ownership belongs in the supervisor record, not the ordinary work
ledger: update admission drains ordinary work before asking residents to stop.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import ExitStack

from .update_windows_process import JobProcess

CONTROL_INPUT = "BUBO_RESIDENT_CONTROL_STDIN"


class WindowsResidentProcess:
    """Own one resident tree; provide the process interface used by readiness."""

    def __init__(
        self, args, *, name, cwd, env, probe=False, control_input=False, on_job_created=None
    ):
        import msvcrt

        self.stdin = None
        self.control_input = control_input
        env = {key: value for key, value in env.items() if key != CONTROL_INPUT}
        if control_input:
            env[CONTROL_INPUT] = "1"
        self._process = None
        try:
            with ExitStack() as temporary:
                source, output = sys.stdin, sys.stdout
                if probe or control_input:
                    reader, writer = os.pipe()
                    try:
                        source = os.fdopen(reader, "rb", buffering=0)
                    except BaseException:
                        os.close(reader)
                        os.close(writer)
                        raise
                    temporary.enter_context(source)
                    try:
                        self.stdin = os.fdopen(writer, "wb", buffering=0)
                    except BaseException:
                        os.close(writer)
                        raise
                if probe:
                    output = temporary.enter_context(open(os.devnull, "wb"))
                streams = []
                for stream, mode in ((source, "rb"), (output, "wb"), (sys.stderr, "wb")):
                    if stream is None:
                        stream = temporary.enter_context(open(os.devnull, mode))
                    streams.append(stream)
                self._process = JobProcess(
                    args,
                    name=name,
                    cwd=cwd,
                    env=env,
                    handles=tuple(msvcrt.get_osfhandle(stream.fileno()) for stream in streams),
                    on_job_created=on_job_created,
                )
        except BaseException:
            if self.stdin is not None:
                self.stdin.close()
            raise

    def retain_members(self):
        self._process.retain_members()

    def request_stop(self):
        if not self.control_input:
            return False
        self.stdin.close()
        return True

    @property
    def job(self):
        return self._process.job

    @property
    def pid(self):
        return self._process.pid

    @property
    def name(self):
        return self._process.name

    @property
    def returncode(self):
        return self._process.returncode

    def owns_member(self, pid, identity):
        from .update_windows_process import verified_job_member

        try:
            with verified_job_member(pid, identity, self.job):
                return True
        except OSError, ValueError:
            return False

    def identity(self):
        return self._process.identity()

    def poll(self):
        return self._process.poll()

    def wait(self, timeout=None):
        return self._process.wait(timeout=timeout)

    def quiescent(self):
        return self._process.quiescent()

    def close(self):
        self._process.close()  # Whole-Job quiescence before releasing probe stdin.
        if self.stdin is not None:
            self.stdin.close()


def request_service_stop(state, process) -> bool:
    """Reuse the service's cooperative token protocol for our exact live child."""
    from .service_control import publish_stop
    from .update_platform import windows_process_identity

    pid_path = state / "bubo-service.pid"
    try:
        record = json.loads(pid_path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or type(record.get("pid")) is not int
            or not isinstance(record.get("token"), str)
            or not 0 < len(record["token"]) <= 256
        ):
            return False
        identity = process.identity()
        if not isinstance(identity, str) or windows_process_identity(process.pid) != identity:
            return False
        from .update_windows_process import verified_job_member

        member_identity = windows_process_identity(record["pid"])
        if not member_identity:
            return False
        with verified_job_member(record["pid"], member_identity, process.job):
            publish_stop(pid_path, record["token"])
        return True
    except OSError, ValueError:
        return False


def http_stop_event():
    """Monitor the supervisor-owned HTTP input pipe; EOF requests normal exit.

    HTTP does not use stdin for client traffic. A private pipe avoids console
    signal dependencies and also closes when the retained parent dies. Stdio MCP
    keeps its real client input and never activates this channel.
    """
    if os.environ.pop(CONTROL_INPUT, None) != "1":
        return None
    event = threading.Event()
    stream = sys.stdin

    def monitor():
        try:
            descriptor = stream.fileno()
            while os.read(descriptor, 1):
                pass
        except OSError, ValueError, AttributeError:
            # A closed or missing pipe is treated the same as EOF: request exit.
            pass
        finally:
            event.set()

    threading.Thread(target=monitor, name="bubo-http-stop", daemon=True).start()
    return event
