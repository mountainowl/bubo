"""Identity isolation/concurrency and privacy regressions for product analytics."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from bubo import analytics, poller
from bubo.analytics_config import AnalyticsConfig


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    monkeypatch.setattr(analytics, "_install_id", None)
    monkeypatch.setattr(analytics, "_install_path", None)
    monkeypatch.setattr(analytics, "_pending_events", [])
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("BUBO_ANALYTICS", raising=False)


def test_distinct_state_directories_and_restart(tmp_path, monkeypatch):
    first = analytics.install_id()
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "second" / "reviewer.sqlite")
    second = analytics.install_id()
    assert first != second
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    assert analytics.install_id() == first


@pytest.mark.parametrize("content", ["", "customer-secret", "00000000000000000000000000000000"])
def test_invalid_identity_is_repaired(content):
    path = analytics.paths.DB.parent / "install_id"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    identity = analytics.install_id()
    assert uuid.UUID(identity).version == 4
    assert path.read_text().strip() == identity


def test_concurrent_first_run_uses_one_persisted_identity(tmp_path):
    # A losing nonblocking lock may drop an event, but must never invent an ID.
    program = """
import json, time
from bubo.analytics import install_id
identity = None
for _ in range(100):
    identity = install_id()
    if identity is not None:
        break
    time.sleep(0.01)
print(json.dumps(identity))
"""
    env = {
        **os.environ,
        "BUBO_BASE_DIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", program], env=env, stdout=subprocess.PIPE, text=True
        )
        for _ in range(8)
    ]
    identities = [json.loads(worker.communicate(timeout=10)[0]) for worker in workers]
    assert all(worker.returncode == 0 for worker in workers)
    assert len(set(identities)) == 1
    assert identities[0] == (tmp_path / "state" / "install_id").read_text().strip()


def test_identity_cannot_be_overridden():
    analytics._emit(
        AnalyticsConfig(), "session_start", {"install_id": "secret", "distinct_id": "secret"}
    )
    attrs = analytics._pending_events[0][2]["properties"]
    assert attrs["install_id"] == attrs["distinct_id"] == analytics.install_id()


def test_language_categories_and_unknown_paths_never_escape():
    files = ["secret/main.py", "customer/a.c", "customer/Test.java", "x.tsx", "private.xyz"]
    assert analytics.language_counts(files) == {
        "python": 1,
        "c": 1,
        "java": 1,
        "typescript": 1,
        "other": 1,
    }


def test_changed_loc_reuses_one_provider_request():
    provider = Mock()
    provider.changed_lines.return_value = {
        "secret/a.py": {"new_lines": [1, 2]},
        "secret/b.py": {"new_lines": [3]},
        "secret/C.java": {"new_lines": []},
    }
    languages = {}
    assert poller.changed_loc(provider, Mock(), "token", "repo", 1, languages) == (3, 3)
    assert languages == {"python": 2, "java": 1}
    provider.changed_lines.assert_called_once()


def test_review_language_events_are_bounded_and_share_identity():
    analytics.record_review_completed(
        AnalyticsConfig(),
        scm_provider="github",
        agent="codex",
        model=None,
        status="success",
        dry_run=False,
        review_mode="diff",
        tone=None,
        duration_seconds=1,
        tokens_input=None,
        tokens_output=None,
        tokens_cached=None,
        tokens_total=None,
        cost_usd=0,
        findings_posted=0,
        findings_planned=0,
        findings_skipped=0,
        files_changed=3,
        lines_changed=4,
        queue_seconds=2,
        languages={"python": 2, "java": 1, "secret/customer": 1},
    )
    events = [item[2] for item in analytics._pending_events]
    assert [event["event"] for event in events] == [
        "review_completed",
        "review_language",
        "review_language",
    ]
    assert events[0]["properties"]["queue_seconds"] == 2
    assert "secret" not in json.dumps(events)
    assert {event["properties"]["distinct_id"] for event in events} == {analytics.install_id()}


def test_opt_out_does_not_create_identity():
    analytics.record_session_start(
        AnalyticsConfig(enabled=False), scm_provider="github", projects_count=1
    )
    assert not analytics.paths.DB.parent.exists()


def test_otel_resource_uses_same_identity(monkeypatch):
    from bubo.telemetry import metrics
    from bubo.telemetry.config import TelemetryConfig

    monkeypatch.setattr(metrics, "_CONFIGURED", False)
    resource = Mock(wraps=metrics.Resource.create)
    monkeypatch.setattr(metrics.Resource, "create", resource)
    monkeypatch.setattr(metrics.trace, "set_tracer_provider", Mock())
    metrics.configure_otel(TelemetryConfig(enabled=True))
    attrs = resource.call_args.args[0]
    assert attrs["service.instance.id"] == attrs["install_id"]
    assert attrs["distinct_id"] == analytics.install_id()
