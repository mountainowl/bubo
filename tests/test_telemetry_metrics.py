from __future__ import annotations

from bubo.telemetry.config import ModelPricing, TelemetryConfig
from bubo.telemetry.cost import TokenUsage, estimate_cost_usd, parse_codex_token_usage
from bubo.telemetry.metrics import METRIC_ATTRIBUTE_KEYS, ReviewTelemetry, metric_attrs


def test_metric_attrs_exclude_high_cardinality_fields() -> None:
    attrs = metric_attrs(
        repo="example/enabled-repo",
        status="success",
        mr_iid=269,
        sha="abc",
        file="src/A.java",
        line=12,
        fingerprint="fp",
        discussion_id="disc",
    )

    assert attrs == {"repo": "example/enabled-repo", "status": "success"}
    assert "mr_iid" not in METRIC_ATTRIBUTE_KEYS
    assert "file" not in METRIC_ATTRIBUTE_KEYS
    assert "fingerprint" not in METRIC_ATTRIBUTE_KEYS


def test_metric_attrs_coerce_untrusted_label_values() -> None:
    attrs = metric_attrs(
        finding_type="issue\nvery long untrusted text",
        severity="critical",
        category="correctness " * 50,
        status="posted",
    )

    assert attrs == {
        "finding_type": "unknown",
        "severity": "unknown",
        "category": "unknown",
        "status": "posted",
    }


def test_parse_codex_token_usage_from_total_only_transcript() -> None:
    usage = parse_codex_token_usage("tokens used\n63,272\n")

    assert usage == TokenUsage(total=63272)


def test_parse_codex_token_usage_from_split_transcript() -> None:
    usage = parse_codex_token_usage(
        """
input tokens: 12,000
cached tokens: 2,000
output tokens: 1,500
total tokens: 13,500
"""
    )

    assert usage.input == 12000
    assert usage.cached == 2000
    assert usage.output == 1500
    assert usage.total == 13500


def test_estimate_cost_uses_model_pricing() -> None:
    pricing = ModelPricing(input_per_1m=1.0, output_per_1m=10.0, cached_input_per_1m=0.1)
    usage = TokenUsage(input=1000, output=100, cached=500, total=1600)

    assert estimate_cost_usd(usage, pricing) == 0.00155


def test_estimate_cost_uses_total_as_input_floor_when_split_missing() -> None:
    pricing = ModelPricing(input_per_1m=5.0, output_per_1m=30.0, cached_input_per_1m=0.5)
    usage = TokenUsage(total=102_553)

    assert estimate_cost_usd(usage, pricing) == 0.512765


def test_disabled_review_telemetry_is_noop() -> None:
    telemetry = ReviewTelemetry(TelemetryConfig())

    with telemetry.span("llm_review.run", repo="r", mr_iid=1) as span:
        telemetry.add_event(span, "finding.generated", file="x")
        telemetry.record_review_done(
            repo="r",
            model="codex-cli",
            status="success",
            review_mode="diff",
            dry_run=False,
            duration_seconds=1.2,
            tokens=TokenUsage(total=100),
            cost_usd=0.0,
        )


def test_telemetry_span_does_not_swallow_body_exceptions() -> None:
    telemetry = ReviewTelemetry(TelemetryConfig(enabled=True))

    try:
        with telemetry.span("llm_review.run", repo="r"):
            raise RuntimeError("review failed")
    except RuntimeError as exc:
        assert str(exc) == "review failed"
    else:
        raise AssertionError("telemetry span swallowed the review exception")


def test_review_stage_spans_nest_under_run_with_attributes() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    telemetry = ReviewTelemetry(TelemetryConfig(enabled=True))
    telemetry.tracer = provider.get_tracer("bubo-test")  # bypass the global provider

    with telemetry.span("llm_review.run", repo="r"):
        with telemetry.span("llm_review.agent", repo="r", model="codex") as agent_span:
            telemetry.set_span_attrs(agent_span, tokens_total=123, cost_usd=0.4)

    by_name = {s.name: s for s in exporter.get_finished_spans()}
    assert {"llm_review.run", "llm_review.agent"} <= set(by_name)
    # The stage span is a child of the run span (real trace tree, not siblings).
    assert by_name["llm_review.agent"].parent is not None
    assert by_name["llm_review.agent"].parent.span_id == by_name["llm_review.run"].context.span_id
    # Stage attributes ride on the span for downstream tools to slice on.
    assert by_name["llm_review.agent"].attributes["tokens_total"] == 123
    assert by_name["llm_review.agent"].attributes["model"] == "codex"


def test_configure_otel_can_retry_after_init_failure(monkeypatch) -> None:
    from bubo.telemetry import metrics as metrics_module

    metrics_module._CONFIGURED = False

    def fail_create(*_: object, **__: object):
        raise RuntimeError("bad otel")

    monkeypatch.setattr(metrics_module.Resource, "create", fail_create)

    metrics_module.configure_otel(TelemetryConfig(enabled=True, otlp_endpoint="http://otel:4317"))

    assert metrics_module._CONFIGURED is False
