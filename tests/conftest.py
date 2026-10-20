"""Keep runtime configuration and automatic setup inside disposable test homes."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_external_telemetry(monkeypatch, request):
    from bubo import analytics
    from bubo.analytics_config import AnalyticsConfig
    from bubo.update_reporting import FailureStore

    # Only consent/capture tests exercise shipped defaults, behind fake transports.
    consent_tests = {
        "test_analytics",
        "test_analytics_config",
        "test_analytics_identity_languages",
        "test_project_analytics",
        "test_telemetry_config",
        "test_telemetry_metrics",
        "test_telemetry_posthog",
        "test_telemetry_context",
        "test_update_version",
        "test_update_reporting",
        "test_update_helper",
        "test_update_docker_setup",
        "test_poller_config",
        "test_no_findings_comment",
        "test_config_errors",
    }
    if request.node.path.stem not in consent_tests:
        defaults = AnalyticsConfig.__init__.__defaults__
        monkeypatch.setattr(AnalyticsConfig.__init__, "__defaults__", (False, *defaults[1:]))
    attempted = []

    def blocked(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Tests must use an in-memory telemetry transport")

    monkeypatch.setattr(analytics, "_pending_events", [])
    monkeypatch.setattr(analytics, "urlopen", blocked)
    monkeypatch.setattr(
        FailureStore.drain,
        "__kwdefaults__",
        {
            **FailureStore.drain.__kwdefaults__,
            "sender": blocked,
        },
    )
    # Tests run offline: the live model price list falls back to configured prices.
    from bubo.telemetry import pricing

    def offline():
        raise OSError("offline in tests")

    monkeypatch.setattr(pricing, "_fetch", offline)
    yield
    from bubo.telemetry import posthog
    from bubo.telemetry.config import TelemetryConfig

    posthog.configure_otel(TelemetryConfig(analytics=AnalyticsConfig(enabled=False)))
    with analytics._pending_lock:
        analytics._pending_events.clear()
    analytics._session_day = None
    assert not attempted, "A test attempted external telemetry without a fake transport"


@pytest.fixture(autouse=True)
def isolated_runtime_environment(tmp_path_factory, monkeypatch):
    # Runtime config exports environment variables for child workers. Restore
    # them after each test, including unittest cases that import the poller.
    # A writable entrypoint must never install a real host schedule during tests.
    from bubo import paths, update_helper

    # Entrypoint tests must opt into exec delegation explicitly; never replace
    # the pytest process with a retained parent as a side effect of a test.
    monkeypatch.setenv("BUBO_RESIDENT_CHILD", "1")

    home = tmp_path_factory.mktemp("test-home")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = home / ".local/share/bubo"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config/env.toml").write_text("[analytics]\nenabled = false\n")
    base = root / "var"
    for name, value in {
        "ROOT": root,
        "CONFIG": root / "config/env.toml",
        "_STATE": base,
        "DB": base / "state/reviewer.sqlite",
        "WORK": base / "work",
        "REPORTS": base / "reports",
        "JOBS": base / "jobs",
        "LOGS": base / "log",
        "RENDERED_PROMPTS": base / "rendered-prompts",
        "SERVICE_PID": base / "state/bubo-service.pid",
    }.items():
        monkeypatch.setattr(paths, name, value)

    def isolated_scheduler(original):
        def call(*args, **kwargs):
            if "runner" not in kwargs:
                raise RuntimeError("Tests must provide a fake scheduler runner")
            return original(*args, **kwargs)

        return call

    for name in ("register_schedule", "remove_schedule"):
        monkeypatch.setattr(update_helper, name, isolated_scheduler(getattr(update_helper, name)))
    with patch.dict(os.environ):
        yield
