from types import SimpleNamespace as NS

from bubo.analytics_config import AnalyticsConfig
from bubo.telemetry import posthog


def test_metric_context_preserves_model_language_and_token_kind(monkeypatch):
    events = []
    monkeypatch.setattr(posthog, "analytics_enabled", lambda cfg: True)
    monkeypatch.setattr(posthog.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        posthog.analytics, "track", lambda cfg, event, **props: events.append(props)
    )
    points = [
        NS(
            value=value,
            attributes={
                "model": model,
                "llm_provider": "openai",
                "languages": ("Python", "TypeScript"),
                "operation": operation,
                "repo": "/private/SECRET",
            },
        )
        for value, model, operation in [
            (5, "gpt-5", "input"),
            (3, "gpt-5", "output"),
            (9, "gpt-4", "input"),
        ]
    ]
    data = NS(
        resource_metrics=[
            NS(
                scope_metrics=[
                    NS(metrics=[NS(name="llm_review.tokens", data=NS(data_points=points))])
                ]
            )
        ]
    )
    posthog.PostHogMetricExporter(AnalyticsConfig()).export(data)
    assert {(event["model"], event["operation"], event["metric_value"]) for event in events} == {
        ("gpt-5", "input", 5),
        ("gpt-5", "output", 3),
        ("gpt-4", "input", 9),
    }
    assert all(
        event["os"] == "macos"
        and event["languages"] == "python+typescript"
        and event["llm_provider"] == "openai"
        for event in events
    )
    assert "SECRET" not in repr(events)


def test_untrusted_context_is_not_exported(monkeypatch):
    monkeypatch.setattr(posthog.platform, "system", lambda: "Windows")
    assert posthog.safe_context(
        {
            "model": "/private/SECRET",
            "provider": "secret-endpoint",
            "language": "SECRET",
            "languages": ["Python", "/private/SECRET"],
            "operation": "SECRET",
        }
    ) == {"languages": "python", "os": "windows"}


def test_enabled_config_records_spans_and_metrics(monkeypatch):
    from dataclasses import replace

    from bubo.telemetry.config import TelemetryConfig

    events = []
    monkeypatch.setattr(
        posthog.analytics, "track", lambda cfg, event, **props: events.append((event, props))
    )
    cfg = TelemetryConfig(analytics=AnalyticsConfig(enabled=True), export_interval_seconds=3600)
    tracer, meter = posthog.configure_otel(cfg)
    with tracer.start_as_current_span("llm_review.run", attributes={"model": "gpt-5"}):
        pass
    meter.create_counter("llm_review.runs").add(1, {"model": "gpt-5"})
    posthog.flush_otel()
    assert {event for event, props in events} == {"otel_span", "otel_metric"}
    assert all(props["model"] == "gpt-5" for event, props in events)
    posthog.configure_otel(replace(cfg, analytics=replace(cfg.analytics, enabled=False)))


def test_safe_context_preserves_model_family_and_classification():
    attrs = posthog.safe_context(
        {
            "model": "claude-opus-4-7",
            "status": "success",
            "severity": "blocking",
            "category": "security",
            "governance_action": "flag",
            "dry_run": False,
            "repo": "/private/SECRET",
            "error_type": "SECRET",
        }
    )
    assert attrs["model_family"] == "claude"
    assert attrs["category"] == "security"
    assert attrs["governance_action"] == "flag"
    assert "repo" not in attrs
    assert "error_type" not in attrs


def test_export_envelope_uses_shared_identity_version_and_runtime(monkeypatch):
    from bubo import analytics

    posthog.PostHogSpanExporter(AnalyticsConfig(enabled=True)).export(
        [
            NS(
                name="llm_review.run",
                start_time=0,
                end_time=1_000_000,
                attributes={"model": "gpt-5", "languages": ["python", "java"]},
            ),
        ]
    )
    event = analytics._pending_events[-1][2]
    props = event["properties"]
    assert props["model_family"] == "gpt"
    assert props["languages"] == "java+python"
    assert props["distinct_id"] == analytics.install_id()
    assert props["bubo_version"] == analytics.installed_version()
    assert props["python_version"]
    assert props["arch"]
    assert props["os"]
    assert props["os_family"] in {"macos", "linux", "windows", "unknown"}
