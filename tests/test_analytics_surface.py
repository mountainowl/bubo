"""Every analytics event says which Bubo surface produced it."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bubo import analytics, mcp_server, paths, poller
from bubo.analytics_config import AnalyticsConfig

CONSENT = AnalyticsConfig(enabled=True, endpoint="https://posthog.invalid/batch", api_key="k")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    monkeypatch.setattr(analytics, "_install_id", None)
    monkeypatch.setattr(analytics, "_surface", "unknown")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _surfaces() -> list[str]:
    return [event["properties"].get("surface") for _, _, event in analytics._pending_events]


def _main(argv: list[str]) -> None:
    with patch.object(sys, "argv", ["bubo-poller", *argv]):
        poller.main()


def test_events_carry_the_surface_and_it_cannot_be_overridden():
    analytics.set_surface("service")
    analytics.track(CONSENT, "session_start", surface="mcp")
    analytics.set_surface("not-a-surface")
    analytics.track(CONSENT, "session_start")
    assert _surfaces() == ["service", "unknown"]


@pytest.mark.parametrize(("actions", "surface"), [(None, "run_once"), ("true", "github_action")])
def test_run_once_is_tagged_and_github_action_is_told_apart(monkeypatch, actions, surface):
    if actions:
        monkeypatch.setenv("GITHUB_ACTIONS", actions)
    seen = []
    with patch("bubo.poller.run_once", lambda: seen.append(analytics.current_surface()) or 0):
        _main(["run-once"])
    assert seen == [surface]


def test_foreground_service_is_tagged(monkeypatch):
    seen = []
    with (
        patch("bubo.update_supervisor.delegate"),
        patch(
            "bubo.service.run_foreground",
            lambda token: seen.append(analytics.current_surface()) or 0,
        ),
    ):
        _main(["service", "start", "--foreground", "--service-token", "t"])
    assert seen == ["service"]


def test_detached_worker_inherits_the_pollers_surface(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "JOBS", tmp_path / "jobs")
    paths.JOBS.mkdir()
    analytics.set_surface("github_action")
    job = poller.write_job("o/r", {"number": 1, "head": {"sha": "b" * 40}})
    assert json.loads(Path(job).read_text())["surface"] == "github_action"

    analytics.set_surface("unknown")
    seen = []
    with patch("bubo.poller.worker", lambda path: seen.append(analytics.current_surface()) or 0):
        _main(["--worker", str(job)])
    assert seen == ["github_action"]


def test_mcp_server_is_tagged():
    seen = []
    with patch("bubo.mcp_server._main", lambda: seen.append(analytics.current_surface())):
        with patch("bubo.update_supervisor.delegate"):
            mcp_server.main()
    assert seen == ["mcp"]
