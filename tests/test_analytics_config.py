from __future__ import annotations

import pytest

from bubo.analytics_config import (
    DEFAULT_ANALYTICS_API_KEY,
    DEFAULT_ANALYTICS_ENDPOINT,
    AnalyticsConfig,
    analytics_config_from_dict,
)
from bubo.config_values import ConfigError
from bubo.review_config import review_config_from_dict


def test_analytics_defaults_to_enabled() -> None:
    cfg = analytics_config_from_dict({})

    assert cfg == AnalyticsConfig()
    assert cfg.enabled is True
    assert cfg.endpoint == "https://us.i.posthog.com/i/v1/logs"
    assert cfg.endpoint == DEFAULT_ANALYTICS_ENDPOINT
    assert cfg.api_key == DEFAULT_ANALYTICS_API_KEY


def test_analytics_opt_out_parses() -> None:
    cfg = analytics_config_from_dict({"analytics": {"enabled": False}})
    assert cfg.enabled is False


def test_analytics_endpoint_and_key_overridable() -> None:
    cfg = analytics_config_from_dict(
        {"analytics": {"endpoint": "https://self.host/otlp/v1/logs", "api_key": "phc_other"}}
    )
    assert cfg.endpoint == "https://self.host/otlp/v1/logs"
    assert cfg.api_key == "phc_other"


def test_analytics_rejects_quoted_boolean() -> None:
    # Same footgun as telemetry: bare bool("false") is True. A quoted "false"
    # must be rejected rather than silently keeping analytics ON.
    with pytest.raises(ConfigError, match=r"analytics\.enabled"):
        analytics_config_from_dict({"analytics": {"enabled": "false"}})


def test_analytics_rejects_non_table() -> None:
    with pytest.raises(ConfigError, match="analytics must be a table"):
        analytics_config_from_dict({"analytics": "on"})


def test_review_config_carries_analytics_on_by_default() -> None:
    cfg = review_config_from_dict({"gitlab": {"url": "https://gitlab.example"}})
    assert cfg.analytics_config.enabled is True


def test_review_config_threads_analytics_opt_out() -> None:
    cfg = review_config_from_dict({"analytics": {"enabled": False}})
    assert cfg.analytics_config.enabled is False
