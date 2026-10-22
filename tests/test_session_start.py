"""``session_start`` is a liveness signal, not a per-poll event.

The service calls ``poll()`` in one process every few minutes. Drives the
real ``poll()`` with an idle config and counts the queued PostHog events.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from bubo import analytics, db, paths, poller
from bubo.analytics_config import AnalyticsConfig
from bubo.review_config import ReviewConfig

CONSENT = AnalyticsConfig(enabled=True, endpoint="https://posthog.invalid/batch", api_key="k")


@contextmanager
def _state() -> Iterator[None]:
    old = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "state" / "reviewer.sqlite"
            paths.DB.parent.mkdir()
            db.init_db()
            yield
    finally:
        paths.DB = old


def _session_starts(cfg: ReviewConfig, polls: int, days: list[str] | None = None) -> int:
    provider = MagicMock()
    provider.name = "github"
    provider.token.return_value = "t"
    with (
        _state(),
        patch.object(poller, "read_config", return_value=cfg),
        patch.object(poller, "get_provider", return_value=provider),
        patch.object(analytics, "flush"),
        patch.object(analytics, "_utc_day", side_effect=days or ["2026-10-20"] * polls),
    ):
        for _ in range(polls):
            poller.poll(worker_mode="inline")
    with analytics._pending_lock:
        return sum(
            1 for *_, queued in analytics._pending_events if queued["event"] == "session_start"
        )


def _idle() -> ReviewConfig:
    return ReviewConfig(provider="github", projects=[], analytics_config=CONSENT)


def test_repeated_polls_in_one_process_send_one_session_start() -> None:
    assert _session_starts(_idle(), polls=3) == 1


def test_a_long_running_service_still_checks_in_once_a_day() -> None:
    days = ["2026-10-20", "2026-10-20", "2026-10-21", "2026-10-21"]
    assert _session_starts(_idle(), polls=4, days=days) == 2


def test_session_start_is_not_spent_while_analytics_is_off() -> None:
    off = ReviewConfig(
        provider="github", projects=[], analytics_config=AnalyticsConfig(enabled=False)
    )
    assert _session_starts(off, polls=1) == 0
    assert _session_starts(_idle(), polls=1) == 1
