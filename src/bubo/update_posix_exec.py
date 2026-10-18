"""Durable launch gate for POSIX installer processes.

A guardian publishes its own identity before exec. A controller may cancel an
unpublished launch under the same lock, including before Popen ever ran.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from bubo.update_identity import atomic_json

PROTOCOL = "posix-exec-v1"


def gate_path(writer: Path) -> Path:
    return writer.with_name(writer.name + ".gate")


@contextmanager
def launch_gate(writer: Path):
    import fcntl

    path = gate_path(writer)
    # Never recreate a lock that cleanup has removed.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opened, current = os.fstat(fd), path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise OSError("Launch gate identity changed")
        yield
    finally:
        os.close(fd)


def cancel_unstarted(writer: Path, expected: dict) -> bool:
    if expected.get("protocol") != PROTOCOL or expected.get("state") != "starting":
        return False
    try:
        with launch_gate(writer):
            if json.loads(writer.read_text()) != expected:
                return False
            atomic_json(writer, {**expected, "state": "quiescent"})
        return True
    except OSError:
        return False


# Isolated stdlib-only child: it must survive replacement of the Bubo package.
# Keep this record and start-identity format identical to update_installers.
GUARDIAN = r"""
import fcntl, json, os, pathlib, stat, subprocess, sys, tempfile
writer, nonce = pathlib.Path(sys.argv[1]), sys.argv[2]
command = sys.argv[3:]
gate = writer.with_name(writer.name + '.gate')
fd = os.open(gate, os.O_RDONLY | os.O_NOFOLLOW)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
opened, current = os.fstat(fd), gate.lstat()
if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
    raise RuntimeError('Launch gate identity changed')
expected = {'state': 'starting', 'protocol': 'posix-exec-v1', 'nonce': nonce}
if json.loads(writer.read_text()) != expected:
    raise RuntimeError('Launch cancelled')
pid = os.getpid()
if os.getsid(0) != pid or os.getpgrp() != pid:
    raise RuntimeError('Guardian does not own process group')
if sys.platform.startswith('linux'):
    fields = pathlib.Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    start = f'proc:{boot}:{fields[19]}'
else:
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                            capture_output=True, text=True, check=True)
    start = result.stdout.strip()
if not start:
    raise RuntimeError('Process identity unavailable')
record = {**expected, 'state': 'running', 'pid': pid, 'start': start}
temporary = None
try:
    with tempfile.NamedTemporaryFile('w', dir=writer.parent, delete=False) as stream:
        temporary = stream.name
        os.chmod(temporary, 0o600)
        json.dump(record, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, writer)
    directory = os.open(writer.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    if temporary and os.path.exists(temporary):
        os.unlink(temporary)
# The close-on-exec gate remains held until durable publication and exec.
os.execvpe(command[0], command, os.environ)
"""
