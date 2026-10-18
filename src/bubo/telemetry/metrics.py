from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import Span

from bubo.analytics import installed_version
from bubo.telemetry.config import TelemetryConfig
from bubo.telemetry.cost import TokenUsage

METRIC_ATTRIBUTE_KEYS = frozenset(
    {
        "repo",
        "model",
        "llm_provider",
        "language",
        "languages",
        "prompt_version",
        "review_mode",
        "status",
        "dry_run",
        "finding_type",
        "severity",
        "category",
        "skip_reason",
        "error_type",
        "component",
        "operation",
        "outcome",
        "reviewer",
        "provenance_band",
        "provenance_source",
        "governance_mode",
        "governance_action",
        "tone",
    }
)

_CONFIGURED = False
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_ENUM_VALUES = {
    "finding_type": {"issue", "suggestion", "question", "unknown"},
    "severity": {"blocking", "non-blocking", "unknown"},
    "governance_mode": {"off", "report-only", "soft", "unknown"},
    "governance_action": {"clear", "flag", "unknown"},
}
SpanAttribute = (
    str | bool | int | float | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
)


def metric_attrs(**attrs: object) -> dict[str, str | bool | int | float]:
    out: dict[str, str | bool | int | float] = {}
    for key, value in attrs.items():
        if key not in METRIC_ATTRIBUTE_KEYS or value is None:
            continue
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str):
            out[key] = _safe_label_value(key, value)
    out["bubo_version"] = installed_version()
    return out


def _safe_label_value(key: str, value: str) -> str:
    normalized = value.strip()
    allowed = _ENUM_VALUES.get(key)
    if allowed is not None:
        lowered = normalized.lower()
        return lowered if lowered in allowed else "unknown"
    if not _SAFE_LABEL.match(normalized):
        return "unknown"
    return normalized


class ReviewTelemetry:
    def __init__(self, config: TelemetryConfig, *, providers=None):
        self.config = config
        self.enabled = config.enabled
        self.tracer = providers[0] if providers else trace.NoOpTracerProvider().get_tracer("bubo")
        self.meter = providers[1] if providers else metrics.NoOpMeterProvider().get_meter("bubo")
        self._runs = _safe_instrument(self.meter.create_counter, "llm_review.runs")
        self._findings = _safe_instrument(self.meter.create_counter, "llm_review.findings")
        self._tokens = _safe_instrument(self.meter.create_counter, "llm_review.tokens")
        self._cost = _safe_instrument(self.meter.create_counter, "llm_review.cost.usd")
        self._failures = _safe_instrument(self.meter.create_counter, "llm_review.failures")
        self._provenance = _safe_instrument(self.meter.create_counter, "llm_review.provenance")
        self._governance = _safe_instrument(self.meter.create_counter, "llm_review.governance")
        self._verifications = _safe_instrument(
            self.meter.create_counter, "llm_review.verifications"
        )
        self._subscription_circuits = _safe_instrument(
            self.meter.create_counter, "llm_review.subscription_circuit"
        )
        self._review_duration = _safe_instrument(
            self.meter.create_histogram,
            "llm_review.latency.review_seconds",
        )
        self._queue_latency = _safe_instrument(
            self.meter.create_histogram,
            "llm_review.latency.queue_seconds",
        )
        self._lines_reviewed = _safe_instrument(
            self.meter.create_histogram,
            "llm_review.lines_reviewed",
        )

    @classmethod
    def from_config(cls, config: TelemetryConfig) -> ReviewTelemetry:
        providers = configure_otel(config)
        instance = cls(config, providers=providers)
        return instance

    @contextmanager
    def span(self, name: str, **attrs: object) -> Iterator[Span | None]:
        """Open an OTel span as the *current* span, so nested ``span`` calls
        become children — a review run's stages (checkout, agent, post) form a
        proper trace tree. No-op when telemetry is disabled. Body exceptions are
        recorded on the span and re-raised (never swallowed).
        """
        if not self.enabled:
            yield None
            return
        try:
            cm = self.tracer.start_as_current_span(name)
        except Exception:
            yield None
            return
        with cm as span:
            self.set_span_attrs(span, **attrs)
            yield span

    def set_span_attrs(self, span: Span | None, **attrs: object) -> None:
        """Attach scalar attributes to ``span``. No-op when disabled or ``None``."""
        if not self.enabled or span is None:
            return
        with suppress(Exception):
            for key, value in span_attrs(attrs).items():
                span.set_attribute(key, value)

    def add_event(self, span: Span | None, name: str, **attrs: object) -> None:
        if not self.enabled or span is None:
            return
        try:
            span.add_event(name, span_attrs(attrs))
        except Exception:
            return

    def record_review_done(
        self,
        *,
        repo: str,
        model: str,
        status: str,
        review_mode: str,
        dry_run: bool,
        duration_seconds: float,
        tokens: TokenUsage,
        cost_usd: float,
        tone: str | None = None,
        lines_reviewed: int = 0,
        llm_provider: str | None = None,
        agent: str | None = None,
        scm_provider: str | None = None,
        language: str | None = None,
        languages: Sequence[str] = (),
        prompt_version: str | None = None,
    ) -> None:
        from bubo.telemetry.posthog import safe_context

        context = safe_context(
            dict(
                model=model,
                llm_provider=llm_provider,
                language=language,
                languages=languages,
                agent=agent,
                scm_provider=scm_provider,
            )
        )
        attrs = metric_attrs(
            repo=repo,
            model=model,
            status=status,
            review_mode=review_mode,
            dry_run=dry_run,
            tone=tone,
            prompt_version=prompt_version,
        )
        attrs.update(context)
        self._add(self._runs, 1, attrs)
        self._record(self._review_duration, duration_seconds, attrs)
        self._record(self._lines_reviewed, lines_reviewed, attrs)
        self._add_token("input", tokens.input, attrs)
        self._add_token("output", tokens.output, attrs)
        self._add_token("cached", tokens.cached, attrs)
        self._add_token("total", tokens.total, attrs)
        self._add(self._cost, cost_usd, attrs)

    def record_finding(
        self, *, repo: str, status: str, finding: dict[str, Any], dry_run: bool
    ) -> None:
        attrs = metric_attrs(
            repo=repo,
            status=status,
            dry_run=dry_run,
            finding_type=finding.get("type"),
            severity=finding.get("severity"),
            category=finding.get("category"),
        )
        self._add(self._findings, 1, attrs)

    def record_provenance(self, *, repo: str, band: str, source: str) -> None:
        self._add(
            self._provenance,
            1,
            metric_attrs(repo=repo, provenance_band=band, provenance_source=source),
        )

    def record_governance(self, *, repo: str, mode: str, action: str) -> None:
        self._add(
            self._governance,
            1,
            metric_attrs(repo=repo, governance_mode=mode, governance_action=action),
        )

    def record_verification(self, *, repo: str, outcome: str) -> None:
        """Count one verified/refuted finding from the opt-in verify pass.

        ``outcome`` is ``"verified"`` (survived the lenses) or ``"refuted"``
        (a majority dropped it). Low cardinality by design — the vote tally
        and reason text are persisted to the DB/log, never as OTel attributes.
        No-op when telemetry is disabled.
        """
        self._add(self._verifications, 1, metric_attrs(repo=repo, outcome=outcome))

    def record_subscription_circuit(self, *, event: str) -> None:
        """Count a circuit transition without repository or error attributes."""
        outcome = "opened" if event == "subscription_circuit_opened" else "recovered"
        self._add(
            self._subscription_circuits,
            1,
            metric_attrs(component="subscription_circuit", outcome=outcome),
        )

    def record_failure(self, *, repo: str, error_type: str, operation: str) -> None:
        self._add(
            self._failures, 1, metric_attrs(repo=repo, error_type=error_type, operation=operation)
        )

    def record_queue_latency(self, *, repo: str, seconds: float) -> None:
        self._record(self._queue_latency, seconds, metric_attrs(repo=repo))

    def _add_token(
        self, kind: str, value: int | None, attrs: dict[str, str | bool | int | float]
    ) -> None:
        if value is None:
            return
        enriched = dict(attrs)
        enriched["operation"] = kind
        self._add(self._tokens, value, enriched)

    def _add(
        self, counter: Any, value: int | float, attrs: dict[str, str | bool | int | float]
    ) -> None:
        if not self.enabled or counter is None:
            return
        try:
            counter.add(value, attrs)
        except Exception:
            return

    def _record(
        self, histogram: Any, value: int | float, attrs: dict[str, str | bool | int | float]
    ) -> None:
        if not self.enabled or histogram is None:
            return
        try:
            histogram.record(value, attrs)
        except Exception:
            return


def configure_otel(config: TelemetryConfig):
    """Use Bubo-owned OTel providers with the shared PostHog consent/transport."""
    from bubo.telemetry.posthog import configure_otel as configure

    try:
        return configure(config)
    except Exception:
        return None


def _safe_instrument(factory: Any, *args: object) -> Any:
    try:
        return factory(*args)
    except Exception:
        return None


def span_attrs(attrs: dict[str, object]) -> dict[str, SpanAttribute]:
    out: dict[str, SpanAttribute] = {}
    for key, value in attrs.items():
        if isinstance(value, str | bool | int | float):
            out[key] = value
    out["bubo_version"] = installed_version()
    return out
