from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bubo.config_values import (
    ConfigError,
    bool_value,
    non_negative_float,
    positive_int,
    text_value,
)
from bubo.errors import describe


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0
    cached_input_per_1m: float = 0.0


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    enabled: bool = False
    service_name: str = "bubo"
    environment: str = "dev"
    otlp_endpoint: str = ""
    export_interval_seconds: int = 30
    emit_finding_events: bool = True
    emit_outcome_sync: bool = True
    pricing: dict[str, ModelPricing] = field(default_factory=lambda: {"default": ModelPricing()})

    def price_for(self, model: str | None) -> ModelPricing:
        if model and model in self.pricing:
            return self.pricing[model]
        return self.pricing.get("default", ModelPricing())


def telemetry_config_from_dict(data: dict[str, Any]) -> TelemetryConfig:
    raw = data.get("telemetry") or {}
    if not isinstance(raw, dict):
        raise ConfigError(
            describe(
                "telemetry must be a table",
                reason=f"[telemetry] parsed as a {type(raw).__name__}, not a TOML table",
                fix="declare telemetry as a [telemetry] table in config/env.toml.",
            )
        )
    protocol = text_value(raw.get("otlp_protocol"), "telemetry.otlp_protocol", default="grpc")
    if protocol != "grpc":
        raise ConfigError(
            describe(
                "telemetry.otlp_protocol must be grpc",
                reason=f"the only supported OTLP protocol is 'grpc', got {protocol!r}",
                fix="set [telemetry].otlp_protocol to 'grpc' in config/env.toml (or omit it).",
            )
        )

    pricing = {"default": _default_pricing_from_telemetry(raw)}
    return TelemetryConfig(
        # bool_value/text_value reject the ``= "false"`` / ``= "0"`` footgun:
        # a quoted string is truthy to bare bool(), silently inverting intent.
        enabled=bool_value(raw.get("enabled"), "telemetry.enabled", default=False),
        service_name=text_value(raw.get("service_name"), "telemetry.service_name", default="bubo"),
        environment=text_value(raw.get("environment"), "telemetry.environment", default="dev"),
        otlp_endpoint=text_value(raw.get("otlp_endpoint"), "telemetry.otlp_endpoint", default=""),
        export_interval_seconds=positive_int(
            raw.get("export_interval_seconds", 30), "telemetry.export_interval_seconds"
        ),
        emit_finding_events=bool_value(
            raw.get("emit_finding_events"), "telemetry.emit_finding_events", default=True
        ),
        emit_outcome_sync=bool_value(
            raw.get("emit_outcome_sync"), "telemetry.emit_outcome_sync", default=True
        ),
        pricing=pricing,
    )


def _default_pricing_from_telemetry(raw: dict[str, Any]) -> ModelPricing:
    return ModelPricing(
        input_per_1m=non_negative_float(raw.get("input_per_1m", 0.0), "telemetry.input_per_1m"),
        output_per_1m=non_negative_float(raw.get("output_per_1m", 0.0), "telemetry.output_per_1m"),
        cached_input_per_1m=non_negative_float(
            raw.get("cached_input_per_1m", 0.0),
            "telemetry.cached_input_per_1m",
        ),
    )
