"""Structured JSON-line event logging.

Two primitives:

* :func:`now` — canonical UTC timestamp string (ISO-8601 to second precision).
* :func:`log` — emit one JSON object per line on stdout.

Every consumer in the codebase logs through :func:`log` so the output
stream is a single newline-delimited JSON sequence — easy to `jq`, easy
to ship to a log aggregator. Stdout is flushed per call because the
poller is forked and SIGKILLed during host shutdown; an unflushed buffer
loses events.

This module deliberately does NOT use the stdlib :mod:`logging` package.
The application's needs (structured fields, one shape per line, no
filtering, no levels) do not map cleanly to logger/handler/formatter
plumbing, and routing through it would dilute the "one event = one
JSON line" guarantee the rest of the codebase depends on.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime


def now() -> str:
    """Return the current UTC time as an ISO-8601 string truncated to seconds."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(event: str, **fields: object) -> None:
    """Emit one structured JSON line on stdout.

    Every event carries a UTC timestamp (``ts``) and an event name (``event``);
    additional keyword arguments become top-level fields.

    Callers SHOULD include ``run_id`` (worker scope) or ``poll_run_id``
    (poll-cycle scope) when known so events from the same logical run can
    be correlated across files and processes.
    """
    print(
        json.dumps({"ts": now(), "event": event, **fields}, sort_keys=True),
        flush=True,
        file=sys.stdout,
    )


__all__ = ["log", "now"]
