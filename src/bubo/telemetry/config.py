from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bubo.config_values import positive_int


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
        raise ValueError("telemetry must be a table")
    protocol = str(raw.get("otlp_protocol", "grpc"))
    if protocol != "grpc":
        raise ValueError("otlp_protocol must be grpc")

    pricing = {"default": _default_pricing_from_telemetry(raw)}
    return TelemetryConfig(
        enabled=bool(raw.get("enabled", False)),
        service_name=str(raw.get("service_name", "bubo")),
        environment=str(raw.get("environment", "dev")),
        otlp_endpoint=str(raw.get("otlp_endpoint", "")),
        export_interval_seconds=positive_int(
            raw.get("export_interval_seconds", 30), "export_interval_seconds"
        ),
        emit_finding_events=bool(raw.get("emit_finding_events", True)),
        emit_outcome_sync=bool(raw.get("emit_outcome_sync", True)),
        pricing=pricing,
    )


def _default_pricing_from_telemetry(raw: dict[str, Any]) -> ModelPricing:
    return ModelPricing(
        input_per_1m=_non_negative_float(raw.get("input_per_1m", 0.0), "telemetry.input_per_1m"),
        output_per_1m=_non_negative_float(raw.get("output_per_1m", 0.0), "telemetry.output_per_1m"),
        cached_input_per_1m=_non_negative_float(
            raw.get("cached_input_per_1m", 0.0),
            "telemetry.cached_input_per_1m",
        ),
    )


def _non_negative_float(value: object, name: str) -> float:
    try:
        parsed = float(str(value))
    except ValueError:
        raise ValueError(f"{name} must be a non-negative number") from None
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return parsed
