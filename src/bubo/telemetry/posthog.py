"""Local OTel providers exporting bounded measurements and safe dimensions."""

from __future__ import annotations

import json
import math
import platform
import re
import threading
from dataclasses import asdict

from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    MetricExporter,
    MetricExportResult,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from bubo import analytics
from bubo.analytics import analytics_enabled

SPAN_NAMES = frozenset(
    {
        "llm_review.run",
        "llm_review.checkout",
        "llm_review.provenance",
        "llm_review.agent",
        "llm_review.post",
        "llm_review.subscription_circuit",
    }
)
METRIC_NAMES = frozenset(
    {
        "llm_review.runs",
        "llm_review.findings",
        "llm_review.tokens",
        "llm_review.cost.usd",
        "llm_review.failures",
        "llm_review.provenance",
        "llm_review.governance",
        "llm_review.verifications",
        "llm_review.subscription_circuit",
        "llm_review.latency.review_seconds",
        "llm_review.latency.queue_seconds",
        "llm_review.lines_reviewed",
    }
)


_LANGUAGES = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "java",
        "kotlin",
        "go",
        "rust",
        "c",
        "cpp",
        "csharp",
        "ruby",
        "php",
        "swift",
        "scala",
        "shell",
        "sql",
        "html",
        "css",
        "vue",
        "svelte",
        "dart",
        "r",
        "elixir",
        "lua",
    }
)
_PROVIDERS_ALLOWED = frozenset(
    {
        "openai",
        "anthropic",
        "google",
        "azure",
        "aws",
        "mistral",
        "ollama",
        "openrouter",
        "xai",
        "deepseek",
    }
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


def safe_context(attrs):
    """Only explicit bounded product dimensions; never identifiers or resource metadata."""
    out = {}
    attrs = attrs or {}
    model = attrs.get("model")
    if isinstance(model, str) and _MODEL.fullmatch(model) and ".." not in model:
        out["model"] = model
        name = re.sub(r"^(?:openai|anthropic|google|meta)[_:-]", "", model.lower())
        family = "other"
        for prefix, label in [
            ("gpt", "gpt"),
            ("chatgpt", "gpt"),
            ("o1", "gpt"),
            ("o3", "gpt"),
            ("o4", "gpt"),
            ("claude", "claude"),
            ("gemini", "gemini"),
            ("llama", "llama"),
            ("deepseek", "deepseek"),
        ]:
            if name.startswith(prefix):
                family = label
                break
        out["model_family"] = family
    dimensions = {
        "status": {
            "queued",
            "running",
            "success",
            "no_findings",
            "failed",
            "deferred",
            "superseded",
            # llm_review.findings lifecycle (FindingStatus) and outcome-sync
            # transitions; without these every finding point merges into one.
            "posted",
            "planned",
            "skipped",
            "refuted",
            "pending_external_id",
            "resolved",
            "deleted",
            "developer_replied",
            "disputed",
            "false_positive",
            "duplicate",
        },
        "review_mode": {"diff", "repo", "repo_review"},
        "outcome": {"verified", "refuted", "opened", "recovered"},
        "finding_type": {"issue", "suggestion", "question", "unknown"},
        "severity": {"blocking", "non-blocking", "unknown"},
        "category": {
            "security",
            "correctness",
            "performance",
            "maintainability",
            "style",
            "testing",
            "documentation",
            "reliability",
            "other",
            "unknown",
        },
        "governance_mode": {"off", "report-only", "soft", "unknown"},
        "governance_action": {"clear", "flag", "unknown"},
        "provenance_band": {"unknown", "likely_ai", "collaborative", "human"},
        "provenance_source": {"trailer", "detection", "both", "none"},
    }
    for key, allowed in dimensions.items():
        value = attrs.get(key)
        if isinstance(value, str) and value in allowed:
            out[key] = value
    if isinstance(attrs.get("dry_run"), bool):
        out["dry_run"] = attrs["dry_run"]
    provider = attrs.get("llm_provider", attrs.get("provider"))
    if isinstance(provider, str) and provider.lower() in _PROVIDERS_ALLOWED:
        out["llm_provider"] = provider.lower()
    language = attrs.get("language")
    if isinstance(language, str) and language.lower() in _LANGUAGES:
        out["language"] = language.lower()
    languages = attrs.get("languages")
    if isinstance(languages, (list, tuple, set, frozenset)):
        known = sorted(
            {
                value.lower()
                for value in languages
                if isinstance(value, str) and value.lower() in _LANGUAGES
            }
        )
        if known:
            out["languages"] = "+".join(known[:8])
    elif isinstance(languages, str):
        known = sorted(set(languages.split("+")))
        if known and len(known) <= 8 and all(value in _LANGUAGES for value in known):
            out["languages"] = "+".join(known)
    operation = attrs.get("operation")
    if operation in ("input", "output", "cached", "total"):
        out["operation"] = operation
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    if system:
        out["os"] = system
    return out


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Consent:
    def __init__(self, config):
        self.config = config
        self.active = True
        self.lock = threading.RLock()

    def revoke(self):
        with self.lock:
            self.active = False

    def track(self, event, **properties):
        with self.lock:
            if self.active and analytics_enabled(self.config):
                analytics.track(self.config, event, **properties)


class PostHogSpanExporter(SpanExporter):
    def __init__(self, config, consent=None):
        self.config = config
        self.consent = consent or _Consent(config)

    def export(self, spans):
        if analytics_enabled(self.config):
            for span in spans:
                if span.name not in SPAN_NAMES or span.end_time is None or span.start_time is None:
                    continue
                duration = (span.end_time - span.start_time) / 1_000_000_000
                if _number(duration) and duration >= 0:
                    self.consent.track(
                        "otel_span",
                        otel_name=span.name,
                        duration_seconds=duration,
                        **safe_context(getattr(span, "attributes", None)),
                    )
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class PostHogMetricExporter(MetricExporter):
    def __init__(self, config, consent=None):
        super().__init__(
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            }
        )
        self.config = config
        self.consent = consent or _Consent(config)

    def export(self, metrics_data, timeout_millis=10000, **kwargs):
        if not analytics_enabled(self.config):
            return MetricExportResult.SUCCESS
        for resource in metrics_data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name not in METRIC_NAMES:
                        continue
                    groups = {}
                    for point in metric.data.data_points:
                        value = getattr(point, "value", getattr(point, "sum", None))
                        if _number(value):
                            context = safe_context(getattr(point, "attributes", None))
                            key = tuple(sorted(context.items()))
                            total, count = groups.get(key, (0, 0))
                            groups[key] = (total + value, count + getattr(point, "count", 1))
                    for key, (total, count) in groups.items():
                        if count and _number(total):
                            self.consent.track(
                                "otel_metric",
                                otel_name=metric.name,
                                metric_value=total,
                                metric_count=count,
                                **dict(key),
                            )

        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis=10000):
        return True

    def shutdown(self, timeout_millis=30000, **kwargs):
        pass


_LOCK = threading.Lock()
_PROVIDERS = {}
_CONSENTS = {}


def _effective_config(config):
    return config.analytics


def _cache_key(config):
    return (
        json.dumps(asdict(_effective_config(config)), sort_keys=True),
        config.export_interval_seconds,
    )


def configure_otel(config):
    effective = _effective_config(config)
    enabled = analytics_enabled(effective) and analytics.installed_version() is not None
    key = _cache_key(config)
    with _LOCK:
        # A reload replaces this process's policy. Revoke before SDK shutdown,
        # which itself collects pending metrics, and before constructing replacements.
        for old_key in list(_PROVIDERS):
            if not enabled or old_key != key:
                old_consent = _CONSENTS.pop(old_key)
                old_consent.revoke()
                analytics.discard_pending(old_consent.config)
                old_tracer, old_meter = _PROVIDERS.pop(old_key)
                old_tracer.shutdown()
                old_meter.shutdown(timeout_millis=1000)
        if not enabled:
            return None
        if key not in _PROVIDERS:
            consent = _Consent(effective)
            resource = Resource({})
            tracer = TracerProvider(resource=resource, sampler=ALWAYS_ON)
            # Bubo owns these providers and controls their lifecycle through config.
            tracer._disabled = False
            tracer.add_span_processor(SimpleSpanProcessor(PostHogSpanExporter(effective, consent)))
            reader = PeriodicExportingMetricReader(
                PostHogMetricExporter(effective, consent),
                export_interval_millis=config.export_interval_seconds * 1000,
            )
            try:
                meter = MeterProvider(resource=resource, metric_readers=[reader])
                meter._disabled = False
            except Exception:
                consent.revoke()
                reader.shutdown(timeout_millis=1000)
                tracer.shutdown()
                raise
            _CONSENTS[key] = consent
            _PROVIDERS[key] = (tracer, meter)
        tracer, meter = _PROVIDERS[key]
    return tracer.get_tracer("bubo"), meter.get_meter("bubo")


_FLUSHING = threading.local()


def flush_otel():
    if getattr(_FLUSHING, "active", False):
        return
    _FLUSHING.active = True
    try:
        with _LOCK:
            providers = list(_PROVIDERS.values())
        for tracer, meter in providers:
            tracer.force_flush(timeout_millis=1000)
            meter.force_flush(timeout_millis=1000)
    finally:
        _FLUSHING.active = False
