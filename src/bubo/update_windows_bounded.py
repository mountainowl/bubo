"""Job-backed bounded execution with the ordinary subprocess output contract.

The stdlib-only guardian delegates command lookup and batch-file behavior to the
same subprocess implementation used by Bubo today. Its result file contains only
numeric status, never command arguments, output, credentials or environment data.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from bubo.update_windows_process import JobProcess, _wait_empty, new_job_name

_GUARDIAN = """
import json, os, subprocess, sys
from pathlib import Path
result_path = os.environ.pop(sys.argv[1])
try:
    result = {"returncode": subprocess.call(
        sys.argv[2:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, close_fds=True
    )}
except FileNotFoundError:
    result = {"missing": True}
except OSError as error:
    result = {"errno": error.errno, "winerror": getattr(error, "winerror", None)}
Path(result_path).write_text(json.dumps(result), encoding="utf-8")
"""


class CommandNotFoundError(FileNotFoundError):
    """The command lookup failed, as distinct from guardian/receipt storage."""


def _text(stream):
    stream.seek(0)
    reader = io.TextIOWrapper(stream, encoding="utf-8")
    try:
        return reader.read()
    finally:
        reader.detach()


def run_bounded_windows(args, *, cwd=None, env=None, timeout=600, installation=None):
    """Return combined text/status, or timeout with the partial combined transcript.

    Callers must retain admission until this function returns or raises. Windows
    native acceptance must pass before Windows update registration is enabled.
    Registered installations use this path through the ordinary subprocess wrapper.
    """
    import msvcrt

    if not args:
        raise ValueError("A command is required")
    environment = {**(os.environ if env is None else env), "PYTHONIOENCODING": "utf-8"}
    key = "_BUBO_BOUNDED_" + uuid.uuid4().hex
    while key in environment:
        key = "_BUBO_BOUNDED_" + uuid.uuid4().hex
    started = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix="bubo-bounded-") as directory,
        tempfile.TemporaryFile() as transcript,
        open(os.devnull, "rb") as stdin,
    ):
        receipt = Path(directory) / "result.json"
        environment[key] = str(receipt)
        ownership = None
        if installation is not None:
            from bubo.update_windows_jobs import JobOwnership

            ownership = JobOwnership(installation)
        child = None
        try:
            child = JobProcess(
                [sys.executable, "-I", "-S", "-c", _GUARDIAN, key, *args],
                name=ownership.name if ownership else new_job_name(),
                on_job_created=ownership.created if ownership else None,
                env=environment,
                cwd=str(cwd) if cwd is not None else None,
                handles=tuple(
                    msvcrt.get_osfhandle(stream.fileno())
                    for stream in (stdin, transcript, transcript)
                ),
            )
            try:
                status = child.wait(timeout=max(0, timeout - (time.monotonic() - started)))
                _wait_empty(child.api, child.job, max(0, timeout - (time.monotonic() - started)))
            except (subprocess.TimeoutExpired, TimeoutError) as exc:
                child.terminate()
                raise subprocess.TimeoutExpired(args, timeout, output=_text(transcript)) from exc
            stdout = _text(transcript)
            if status or not receipt.is_file():
                raise OSError("Windows command guardian did not report completion")
            result = json.loads(receipt.read_text(encoding="utf-8"))
            if result.get("missing") is True:
                raise CommandNotFoundError("Command executable was not found")
            if "errno" in result:
                raise OSError(result["errno"] or 0, "Windows command could not be launched")
            code = result.get("returncode")
            if type(code) is not int:
                raise OSError("Windows command guardian returned an invalid status")
            return subprocess.CompletedProcess(args, code, stdout, None)
        finally:
            if ownership:
                ownership.close(child)
            elif child is not None:
                child.close()
