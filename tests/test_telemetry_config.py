from __future__ import annotations

import tempfile
from pathlib import Path

from bubo import poller
from bubo.telemetry.config import TelemetryConfig, telemetry_config_from_dict


def test_telemetry_defaults_to_disabled() -> None:
    cfg = telemetry_config_from_dict({})

    assert cfg == TelemetryConfig()
    assert cfg.enabled is False
    assert cfg.service_name == "bubo"
    assert not hasattr(cfg, "otlp_protocol")


def test_telemetry_config_parses_endpoint_and_pricing() -> None:
    cfg = telemetry_config_from_dict(
        {
            "telemetry": {
                "enabled": True,
                "service_name": "reviewer-prod",
                "environment": "prod",
                "otlp_endpoint": "http://127.0.0.1:4317",
                "otlp_protocol": "grpc",
                "export_interval_seconds": 15,
                "input_per_1m": 1.25,
                "output_per_1m": 10.0,
                "cached_input_per_1m": 0.125,
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.service_name == "reviewer-prod"
    assert cfg.environment == "prod"
    assert cfg.otlp_endpoint == "http://127.0.0.1:4317"
    assert cfg.export_interval_seconds == 15
    assert cfg.price_for("missing").input_per_1m == 1.25
    assert cfg.price_for("missing").output_per_1m == 10.0
    assert cfg.price_for("missing").cached_input_per_1m == 0.125


def test_telemetry_config_ignores_nested_pricing_alias() -> None:
    cfg = telemetry_config_from_dict(
        {
            "telemetry": {
                "pricing": {
                    "default": {
                        "input_per_1m": 1.25,
                        "output_per_1m": 10.0,
                        "cached_input_per_1m": 0.125,
                    },
                    "gpt-test": {
                        "input_per_1m": 2.0,
                        "output_per_1m": 12.0,
                        "cached_input_per_1m": 0.2,
                    },
                },
            }
        }
    )

    assert cfg.price_for("gpt-test").input_per_1m == 0.0
    assert cfg.price_for("missing").output_per_1m == 0.0


def test_telemetry_config_rejects_invalid_protocol() -> None:
    try:
        telemetry_config_from_dict(
            {"telemetry": {"enabled": True, "otlp_protocol": "http/protobuf"}}
        )
    except ValueError as exc:
        assert "otlp_protocol" in str(exc)
    else:
        raise AssertionError("expected invalid protocol to fail")


def test_read_config_disables_invalid_telemetry_without_failing_reviews() -> None:
    original_config = poller.CONFIG
    try:
        with tempfile.TemporaryDirectory() as tmp:
            poller.CONFIG = Path(tmp) / "env.toml"
            poller.CONFIG.write_text(
                """
[gitlab]
url = "https://gitlab.com"

[telemetry]
enabled = true
otlp_protocol = "http/protobuf"

[[projects]]
path = "example/enabled-repo"
enabled = true
""",
                encoding="utf-8",
            )

            cfg = poller.read_config()

            assert cfg.telemetry_config.enabled is False
    finally:
        poller.CONFIG = original_config


def test_read_config_disables_malformed_telemetry_shape_without_failing_reviews() -> None:
    original_config = poller.CONFIG
    try:
        with tempfile.TemporaryDirectory() as tmp:
            poller.CONFIG = Path(tmp) / "env.toml"
            poller.CONFIG.write_text(
                """
[gitlab]
url = "https://gitlab.com"
telemetry = "bad"

[[projects]]
path = "example/enabled-repo"
enabled = true
""",
                encoding="utf-8",
            )

            cfg = poller.read_config()

            assert cfg.telemetry_config.enabled is False
    finally:
        poller.CONFIG = original_config
