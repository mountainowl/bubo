from types import SimpleNamespace as NS

import pytest

from bubo.analytics_config import AnalyticsConfig
from bubo.telemetry import posthog


@pytest.fixture(autouse=True)
def fixed_platform(monkeypatch):
    monkeypatch.setattr(posthog.platform, "system", lambda: "Linux")


def test_exporters_drop_private_fields_and_unknown_names(monkeypatch):
    events = []
    monkeypatch.setattr(posthog, "analytics_enabled", lambda config: True)
    monkeypatch.setattr(
        posthog.analytics, "track", lambda config, event, **props: events.append((event, props))
    )
    span = NS(
        name="llm_review.run",
        start_time=0,
        end_time=2_000_000_000,
        attributes={"repo": "SECRET"},
        events=["SECRET"],
        resource="SECRET",
    )
    posthog.PostHogSpanExporter(AnalyticsConfig()).export([span, NS(name="SECRET")])
    point = NS(value=3, attributes={"repo": "SECRET"}, exemplars=["SECRET"])
    metric = NS(name="llm_review.runs", data=NS(data_points=[point, point]))
    data = NS(
        resource_metrics=[
            NS(resource="SECRET", scope_metrics=[NS(metrics=[metric, NS(name="SECRET")])])
        ]
    )
    posthog.PostHogMetricExporter(AnalyticsConfig()).export(data)
    assert events == [
        ("otel_span", {"otel_name": "llm_review.run", "duration_seconds": 2.0, "os": "linux"}),
        (
            "otel_metric",
            {"otel_name": "llm_review.runs", "metric_value": 6, "metric_count": 2, "os": "linux"},
        ),
    ]


def test_export_time_opt_out(monkeypatch):
    monkeypatch.setattr(posthog, "analytics_enabled", lambda config: False)
    monkeypatch.setattr(
        posthog.analytics,
        "track",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("exported")),
    )
    posthog.PostHogSpanExporter(AnalyticsConfig()).export([NS(name="llm_review.run")])
    posthog.PostHogMetricExporter(AnalyticsConfig()).export(None)


def test_local_sdk_flush_delta_and_provider_reuse(monkeypatch):
    events = []
    monkeypatch.setattr(posthog, "analytics_enabled", lambda config: True)
    monkeypatch.setattr(
        posthog.analytics, "track", lambda config, event, **props: events.append((event, props))
    )
    cfg = NS(
        enabled=True,
        analytics=AnalyticsConfig(api_key="local-sdk-test"),
        export_interval_seconds=3600,
    )
    tracer, meter = posthog.configure_otel(cfg)
    assert posthog.configure_otel(cfg) is not None
    assert len([key for key in posthog._PROVIDERS if key == posthog._cache_key(cfg)]) == 1
    with tracer.start_as_current_span("llm_review.run", attributes={"repo": "SECRET"}):
        pass
    counter = meter.create_counter("llm_review.runs")
    counter.add(2, {"repo": "SECRET"})
    posthog.flush_otel()
    posthog.flush_otel()
    metrics = [props for event, props in events if event == "otel_metric"]
    assert metrics == [
        {"otel_name": "llm_review.runs", "metric_value": 2, "metric_count": 1, "os": "linux"}
    ]
    assert "SECRET" not in repr(events)
    with posthog._LOCK:
        trace_provider, meter_provider = posthog._PROVIDERS.pop(posthog._cache_key(cfg))
    trace_provider.shutdown()
    meter_provider.shutdown()


def test_reload_revokes_pending_metrics_and_open_span(monkeypatch):
    from dataclasses import replace

    from bubo.telemetry.config import TelemetryConfig

    events = []
    monkeypatch.setattr(
        posthog.analytics, "track", lambda config, event, **props: events.append((event, props))
    )
    cfg = TelemetryConfig(analytics=AnalyticsConfig(enabled=True), export_interval_seconds=3600)
    tracer, meter = posthog.configure_otel(cfg)
    span = tracer.start_span("llm_review.run")
    meter.create_counter("llm_review.runs").add(12)
    assert (
        posthog.configure_otel(replace(cfg, analytics=replace(cfg.analytics, enabled=False)))
        is None
    )
    span.end()
    posthog.flush_otel()
    assert events == []
    assert not posthog._PROVIDERS


def test_reload_discards_only_matching_queued_analytics(monkeypatch):
    import json
    from dataclasses import replace

    from bubo.telemetry.config import TelemetryConfig

    monkeypatch.setattr(posthog.analytics, "_pending_events", [])
    monkeypatch.setattr(posthog.analytics, "install_id", lambda: "test-install")
    cfg = TelemetryConfig(analytics=AnalyticsConfig(enabled=True), export_interval_seconds=3600)
    other = replace(cfg.analytics, profiles={"project": "other"})
    tracer, _ = posthog.configure_otel(cfg)
    with tracer.start_as_current_span("llm_review.run"):
        pass
    posthog.analytics.track(other, "session_start")
    assert len(posthog.analytics._pending_events) == 2
    assert "config" not in json.loads(json.dumps(posthog.analytics._pending_events[0][2]))
    posthog.configure_otel(replace(cfg, analytics=replace(cfg.analytics, enabled=False)))
    assert len(posthog.analytics._pending_events) == 1
    assert posthog.analytics._pending_events[0][2].config == other
    posthog.analytics._pending_events.clear()
