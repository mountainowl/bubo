"""Descriptive runtime errors: every failure says *what* broke, *why*, and *how to fix it*.

Operators see bubo's failures as one-line JSON log events (see :mod:`bubo.events`)
and as the ``error`` field persisted on a review run. A bare ``"review exited 1"``
tells them nothing actionable. Every runtime error in bubo composes its message
through :func:`describe` so it carries up to three parts:

* **what** — the operation that failed, in plain language;
* **why** — the underlying reason (an OS error, a rejected sandbox, a bad value);
* **fix** — the concrete next step the operator can take.

They render on a single line as ``"<what> | why: <reason> | fix: <correction>"`` so
the "one event = one JSON line" guarantee in :mod:`bubo.events` is preserved and the
message stays greppable. (The ``what``/``why``/``fix`` scaffolding is always one line;
a few call sites embed already-multi-line subprocess output as the ``reason``, which
is unchanged by this helper.)

The helper only enriches *message text*; call sites keep their existing exception
types (``ValueError`` / :class:`bubo.config_values.ConfigError` / ``RuntimeError`` /
``FileNotFoundError`` / ``TimeoutError`` …), so callers that catch a specific type are
unaffected.
"""

from __future__ import annotations


def describe(what: str, *, reason: str | None = None, fix: str | None = None) -> str:
    """Compose a single-line, actionable error message.

    ``what`` is required; ``reason`` and ``fix`` are appended as ``why:`` / ``fix:``
    segments when present. Returns just ``what`` when neither is given, so the helper
    is safe to adopt incrementally at any ``raise`` site.
    """
    parts = [what.strip()]
    if reason:
        parts.append(f"why: {reason.strip()}")
    if fix:
        parts.append(f"fix: {fix.strip()}")
    return " | ".join(parts)


__all__ = ["describe"]
