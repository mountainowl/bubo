from __future__ import annotations

import tempfile
from pathlib import Path

from bubo import poller
from bubo.telemetry.config import TelemetryConfig, telemetry_config_from_dict


def test_telemetry_defaults_to_enabled() -> None:
    cfg = telemetry_config_from_dict({})

    assert cfg == TelemetryConfig()
    assert cfg.enabled is True
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


def test_telemetry_config_rejects_quoted_boolean() -> None:
    # The footgun: bare bool("false") is True, so a quoted "false" used to
    # silently *enable* telemetry. bool_value now rejects the string outright.
    try:
        telemetry_config_from_dict({"analytics": {"enabled": "false"}})
    except ValueError as exc:
        assert "analytics.enabled" in str(exc)
    else:
        raise AssertionError("expected quoted boolean to be rejected")


def test_read_config_disables_quoted_boolean_telemetry_without_failing_reviews() -> None:
    # End-to-end: a quoted `enabled = "false"` is a ConfigError (ValueError
    # subclass) that flows through read_config's disable-on-error catch, so
    # telemetry ends up *off* rather than silently on. This is the
    # operator-visible behavior the bool_value hardening protects.
    original_config = poller.CONFIG
    try:
        with tempfile.TemporaryDirectory() as tmp:
            poller.CONFIG = Path(tmp) / "env.toml"
            poller.CONFIG.write_text(
                """
[gitlab]
url = "https://gitlab.com"

[analytics]
enabled = "false"

[[projects]]
path = "example/enabled-repo"
enabled = true
""",
                encoding="utf-8",
            )

            import pytest

            with pytest.raises(ValueError, match=r"analytics\.enabled"):
                poller.read_config()
    finally:
        poller.CONFIG = original_config


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
telemetry = "bad"
[gitlab]
url = "https://gitlab.com"

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


def test_analytics_setting_controls_both_transports():
    from bubo.analytics_config import analytics_config_from_dict

    for data, expected in [
        ({}, True),
        ({"analytics": {"enabled": False}}, False),
        ({"analytics": {"enabled": True}}, True),
    ]:
        analytics = analytics_config_from_dict(data)
        telemetry = telemetry_config_from_dict(data)
        assert analytics.enabled is expected
        assert telemetry.enabled is expected
        assert telemetry.analytics == analytics


def test_shipping_example_has_one_enabled_consent():
    import tomllib
    from pathlib import Path

    from bubo.analytics_config import analytics_config_from_dict

    data = tomllib.loads((Path(__file__).parents[1] / "config/env.example.toml").read_text())
    assert "enabled" not in data["telemetry"]
    assert analytics_config_from_dict(data).enabled is True
    assert telemetry_config_from_dict(data).enabled is True


def test_telemetry_config_live_pricing_defaults_on_and_can_be_turned_off() -> None:
    assert telemetry_config_from_dict({}).live_pricing is True
    cfg = telemetry_config_from_dict({"telemetry": {"live_pricing": False}})
    assert cfg.live_pricing is False
