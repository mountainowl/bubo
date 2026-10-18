from __future__ import annotations

from importlib.metadata import PackageNotFoundError

import pytest

from bubo import analytics, version
from bubo.analytics_config import AnalyticsConfig
from bubo.telemetry import metrics as telemetry
from bubo.telemetry.config import TelemetryConfig


def test_metadata_changes_do_not_relabel_running_process(monkeypatch):
    original = version.installed_version()
    monkeypatch.setattr(version, "version", lambda _: "99.0.0")
    assert version.installed_version() == original
    assert analytics._bubo_version() == original


def test_missing_metadata_does_not_invent_release(monkeypatch):
    def missing(_):
        raise PackageNotFoundError

    monkeypatch.setattr(version, "version", missing)
    assert version._resolve() is None


def test_unknown_version_suppresses_posthog(monkeypatch, tmp_path):
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "reviewer.sqlite")
    monkeypatch.setattr(analytics, "_pending_events", [])
    monkeypatch.setattr(analytics, "installed_version", lambda: None)
    analytics._emit(AnalyticsConfig(), "session_start", {})
    assert analytics._pending_events == []


@pytest.mark.parametrize("analytics_on", [True, False])
@pytest.mark.parametrize("otel_on", [True, False])
def test_otel_uses_unified_consent(monkeypatch, analytics_on, otel_on):
    from bubo.telemetry import posthog

    monkeypatch.setattr(posthog, "analytics_enabled", lambda cfg: analytics_on and cfg.enabled)
    monkeypatch.setattr(posthog.analytics, "installed_version", lambda: "0.27.0")
    cfg = TelemetryConfig(analytics=AnalyticsConfig(enabled=otel_on, api_key="local-consent-test"))
    result = telemetry.configure_otel(cfg)
    assert (result is not None) == (analytics_on and otel_on)
    with posthog._LOCK:
        providers = posthog._PROVIDERS.pop(posthog._cache_key(cfg), None)
    if providers:
        for provider in providers:
            provider.shutdown()


def test_no_otel_provider_without_producer_version(monkeypatch):
    from bubo.telemetry import posthog

    monkeypatch.setattr(posthog, "analytics_enabled", lambda cfg: True)
    monkeypatch.setattr(analytics, "installed_version", lambda: None)
    assert (
        telemetry.configure_otel(TelemetryConfig(analytics=AnalyticsConfig(enabled=True))) is None
    )
