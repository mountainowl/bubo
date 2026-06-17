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
    ``stderr=STDOUT``, ``text=True``. That matches how the rest of the
    codebase consumes subprocess output (one combined transcript that goes
    to the report file and the LLM-output parser).

    On :class:`subprocess.TimeoutExpired` the entire process group is
    killed and the partial stdout collected so far is attached to the
    raised exception's ``output`` attribute — callers that want to record
    a failed transcript can pull it from there.
    """
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        program = args[0] if args else "<empty command>"
        raise FileNotFoundError(
            describe(
                f"could not launch the command {program!r}",
                reason="the executable was not found on PATH",
                fix=(
                    f"install {program!r} (the reviewer is 'codex' by default) or correct "
                    "[agents].reviewer_command in config/env.toml to a runnable path."
                ),
            )
        ) from exc
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
