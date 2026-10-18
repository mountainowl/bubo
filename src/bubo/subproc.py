"""Bounded subprocess execution shared by the poller and the codex runner.

Both call sites need the same shape: spawn a child, capture combined
stdout+stderr as text, enforce a wall-clock timeout, and kill the entire
process group on expiry so MCP servers and other grandchildren do not
leak. This module is the one canonical implementation.

Why a new session group:

    The agent CLIs (Codex, Claude) spawn their own helpers — MCP servers,
    `git`, network probes. If the parent times out and only the direct
    child is killed, those grandchildren keep consuming the API key and
    network sockets until the OS reaps them. `start_new_session=True`
    puts every descendant into one process group; ``os.killpg`` then
    cleanly terminates the lot.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

from bubo.errors import describe

DEFAULT_TIMEOUT_SECONDS = 600


def run_bounded(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``args`` with a wall-clock timeout and process-group cleanup.

    Capture mode is fixed: ``stdin=DEVNULL``, ``stdout=PIPE``,
    ``stderr=STDOUT``, ``text=True``, UTF-8. Python child pipes are also
    configured as UTF-8 without mutating the caller environment. This matches how the rest of the
    codebase consumes subprocess output (one combined transcript that goes
    to the report file and the LLM-output parser).

    On :class:`subprocess.TimeoutExpired` the entire process group is
    killed and the partial stdout collected so far is attached to the
    raised exception's ``output`` attribute — callers that want to record
    a failed transcript can pull it from there.
    """
    # Reviewer pipes have a UTF-8 contract, independent of the console locale.
    env = {**(os.environ if env is None else env), "PYTHONIOENCODING": "utf-8"}
    if sys.platform == "win32":
        from bubo import paths
        from bubo.update_admission import MaintenanceInProgress, activity
        from bubo.update_registration import load_registration

        registration = (
            load_registration(paths.DB.parent)
            if (paths.DB.parent / "update-registration.json").exists()
            else None
        )
        if registration is not None and "external" not in registration:
            from bubo.update_windows_bounded import CommandNotFoundError, run_bounded_windows

            with activity(paths.DB.parent, writable=False):
                if load_registration(paths.DB.parent) != registration:
                    raise MaintenanceInProgress("Bubo update registration changed; retry shortly")
                try:
                    return run_bounded_windows(
                        args,
                        cwd=cwd,
                        env=env,
                        timeout=timeout,
                        installation=Path(registration["installation"]),
                    )
                except CommandNotFoundError as exc:
                    raise _missing_command(args) from exc

    from bubo.update_admission import handoff_fds

    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            pass_fds=handoff_fds(),
        )
    except FileNotFoundError as exc:
        raise _missing_command(args) from exc
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_process_group(proc)
        try:
            stdout, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout = exc.output if isinstance(exc.output, str) else ""
        raise subprocess.TimeoutExpired(args, timeout, output=stdout) from exc
    return subprocess.CompletedProcess(args, proc.returncode, stdout or "", None)


def _missing_command(args):
    program = args[0] if args else "<empty command>"
    return FileNotFoundError(
        describe(
            f"could not launch the command {program!r}",
            reason="the executable was not found on PATH",
            fix=(
                f"install {program!r} (the reviewer is 'codex' by default) or correct "
                "[agents].reviewer_command in config/env.toml to a runnable path."
            ),
        )
    )


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the entire process group owning ``proc``.

    Falls back to ``proc.kill()`` (single-process kill) if the process
    group lookup fails — the child has already exited, or the OS does not
    grant access. Suppresses every exception because this is a
    cleanup-only path; the caller has already decided the subprocess must
    die and there is no recovery from "I tried to kill it and it complained".
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        with suppress(Exception):
            proc.kill()


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "kill_process_group", "run_bounded"]
