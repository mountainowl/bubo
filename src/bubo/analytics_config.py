"""Configuration for pseudonymous usage analytics ("help improve Bubo").

Bubo is free and open source; the only way the project learns what to
improve is usage signal from real installs. This block controls that signal.
It is **on by default** and sends counts, durations, fixed category labels,
and random installation/project IDs. Verified organization namespaces may be
sent for public or private projects; repository names require verified public
visibility and a recognized open-source license. Code, paths, personal account
names, review text and credentials are excluded (see :mod:`bubo.analytics`).

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

from dataclasses import dataclass, field
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
    profiles: dict[str, dict[str, str]] = field(default_factory=dict)


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
    from bubo.project_analytics import DOMAINS, PROJECT_TYPES

    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("analytics.profiles must be a table")
    for key, profile in profiles.items():
        if (
            not isinstance(key, str)
            or not key.startswith(("github:", "gitlab:"))
            or not isinstance(profile, dict)
        ):
            raise ConfigError("analytics.profiles entries must be tables keyed by provider:project")
        if (
            not isinstance(profile.get("domain", "unknown"), str)
            or profile.get("domain", "unknown") not in DOMAINS
        ):
            raise ConfigError("analytics profile domain must be a documented category")
        if (
            not isinstance(profile.get("project_type", "unknown"), str)
            or profile.get("project_type", "unknown") not in PROJECT_TYPES
        ):
            raise ConfigError("analytics profile project_type must be a documented category")
    return AnalyticsConfig(
        # bool_value rejects the quoted-"false" footgun (truthy to bare bool()).
        enabled=bool_value(raw.get("enabled"), "analytics.enabled", default=True),
        endpoint=text_value(
            raw.get("endpoint"), "analytics.endpoint", default=DEFAULT_ANALYTICS_ENDPOINT
        ),
        api_key=text_value(
            raw.get("api_key"), "analytics.api_key", default=DEFAULT_ANALYTICS_API_KEY
        ),
        profiles=profiles,
    )


__all__ = [
    "DEFAULT_ANALYTICS_API_KEY",
    "DEFAULT_ANALYTICS_ENDPOINT",
    "AnalyticsConfig",
    "analytics_config_from_dict",
]
