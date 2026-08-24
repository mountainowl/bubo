"""Configuration for anonymous usage analytics ("help improve Bubo").

Bubo is free and open source; the only way the project learns what to
improve is anonymous usage signal from real installs. This block controls
that signal. It is **on by default** and sends *numbers only* — counts,
durations, lines-of-code reviewed, token totals, SCM provider, and model
name. It never sends code, file paths, repository names, review text,
credentials, or any identifying content (see :mod:`bubo.analytics` for the
default-deny allowlist that enforces this).

Three independent ways to opt out, checked in :func:`bubo.analytics`:

* ``[analytics] enabled = false`` in ``config/env.toml`` (this block);
* ``BUBO_ANALYTICS=0`` (or ``false``/``no``/``off``) in the environment;
* the cross-tool ``DO_NOT_TRACK=1`` convention (https://consoledonottrack.com).

The destination is a PostHog project ingestion key. PostHog ``phc_`` keys
are *public write-only* keys designed to be embedded in client software —
they can ingest events but cannot read any data back — so shipping the
default in the repo is the intended model. Operators may override the
endpoint/key, or blank either one to disable sending entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bubo.config_values import ConfigError, bool_value, text_value
from bubo.errors import describe

# PostHog Product Analytics batch endpoint and the public project key. A blank
# endpoint or key disables sending (treated as a soft opt-out by the client).
DEFAULT_ANALYTICS_ENDPOINT = "https://us.i.posthog.com/batch/"
DEFAULT_ANALYTICS_API_KEY = "phc_uhKucyWAFGAQTSyQDcH2NqJ2gto3TThBW5mvc8Phf5vq"


@dataclass(frozen=True, slots=True)
class AnalyticsConfig:
    """Parsed ``[analytics]`` block — anonymous usage analytics settings.

    ``enabled`` defaults to ``True``: the signal is opt-out, not opt-in.
    The environment kill-switches (``BUBO_ANALYTICS`` / ``DO_NOT_TRACK``)
    are applied on top of this in :func:`bubo.analytics.analytics_enabled`,
    so a Docker/CI operator who cannot edit ``env.toml`` can still opt out.
    """

    enabled: bool = True
    endpoint: str = DEFAULT_ANALYTICS_ENDPOINT
    api_key: str = DEFAULT_ANALYTICS_API_KEY


def analytics_config_from_dict(data: dict[str, Any]) -> AnalyticsConfig:
    """Parse the ``[analytics]`` table into an :class:`AnalyticsConfig`.

    A missing block yields the default (enabled) config. A malformed block
    (``analytics`` parsed as a non-table) is a hard :class:`ConfigError`,
    matching how :mod:`bubo.telemetry.config` treats ``[telemetry]``.
    """
    raw = data.get("analytics") or {}
    if not isinstance(raw, dict):
        raise ConfigError(
            describe(
                "analytics must be a table",
                reason=f"[analytics] parsed as a {type(raw).__name__}, not a TOML table",
                fix="declare analytics as an [analytics] table in config/env.toml.",
            )
        )
    return AnalyticsConfig(
        # bool_value rejects the quoted-"false" footgun (truthy to bare bool()).
        enabled=bool_value(raw.get("enabled"), "analytics.enabled", default=True),
        endpoint=text_value(
            raw.get("endpoint"), "analytics.endpoint", default=DEFAULT_ANALYTICS_ENDPOINT
        ),
        api_key=text_value(
            raw.get("api_key"), "analytics.api_key", default=DEFAULT_ANALYTICS_API_KEY
        ),
    )


__all__ = [
    "DEFAULT_ANALYTICS_API_KEY",
    "DEFAULT_ANALYTICS_ENDPOINT",
    "AnalyticsConfig",
    "analytics_config_from_dict",
]
