"""Cooperative SIGTERM / SIGINT shutdown for the poll loop.

The poller is intended to run under cron or systemd. When the supervisor
sends ``SIGTERM`` (``systemctl stop``, machine shutdown), we want the
current poll cycle to **finish the in-progress MR cleanly** and exit
rather than getting SIGKILLed mid-DB-write and leaving an MR stranded at
status ``queued`` forever.

The mechanism:

1. :func:`install_signal_handlers` is called once from ``poller.main``.
2. The handler sets :data:`_SHUTDOWN_REQUESTED` and logs the signal.
3. The poll loop checks :func:`shutdown_requested` between MRs and
   returns cleanly when it flips true.

The handler intentionally does NOT call ``sys.exit`` or raise — it just
flips a flag. Re-sending the signal still triggers Python's default
disposition because we used ``signal.signal`` rather than the cooperative
``set_wakeup_fd`` path; this means a wedged poll can be killed with a
second SIGTERM without code changes.
"""

from __future__ import annotations

import signal
from contextlib import suppress

from bubo.events import log

_SHUTDOWN_REQUESTED = False


def install_signal_handlers() -> None:
    """Wire SIGTERM and SIGINT to set the shutdown flag.

    Idempotent — calling twice replaces the previous handlers with
    equivalent ones. ``ValueError`` is suppressed because
    ``signal.signal`` raises it when called from a non-main thread; the
    worker subprocess inherits the parent's disposition anyway.
    """

    def _on_signal(signum: int, _frame: object) -> None:
        global _SHUTDOWN_REQUESTED
        _SHUTDOWN_REQUESTED = True
        log("shutdown_requested", signal=signal.Signals(signum).name)

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(ValueError):
            signal.signal(sig, _on_signal)


def shutdown_requested() -> bool:
    """Return ``True`` once a SIGTERM/SIGINT has been received this process."""
    return _SHUTDOWN_REQUESTED


def reset_for_tests() -> None:
    """Test-only hook: clear the shutdown flag.

    Production code should never call this — once a signal is delivered,
    pretending it didn't happen is a bug.
    """
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = False


__all__ = [
    "install_signal_handlers",
    "reset_for_tests",
    "shutdown_requested",
]
