"""Small validated config-value helpers.

These helpers parse and validate raw TOML values into the strict Python types
the rest of the codebase expects. They raise :class:`ConfigError` (which
inherits from :class:`ValueError`) so calling code can convert configuration
problems into a non-zero CLI exit at the boundary without conflating them with
runtime exceptions like ``RuntimeError`` or :class:`SystemExit`.

All public helpers in this module are pure: they read their inputs and either
return a normalized value or raise. They never read environment variables, the
filesystem, or globals.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bubo.errors import describe


class ConfigError(ValueError):
    """Raised when a configuration value is missing, malformed, or out of range.

    Inherits from :class:`ValueError` so callers that catch ``ValueError``
    (for example, the broad ``except`` in :func:`review_config_from_dict`)
    will still catch configuration problems, but the dedicated class makes it
    easy to convert configuration failures into a clean CLI exit at the
    entry-point boundary.
    """


def section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """Return ``cfg[name]`` as a dict, or an empty dict if the key is missing
    or its value is not a table.

    TOML loaders produce nested dicts for ``[section]`` tables; this helper
    centralizes the "absent section is just empty" pattern so callers do not
    have to repeat ``cfg.get("section") or {}`` everywhere.
    """
    value = cfg.get(name) or {}
    return value if isinstance(value, dict) else {}


def positive_int(value: object, name: str) -> int:
    """Parse ``value`` as an integer ``>= 1``.

    Raises :class:`ConfigError` with the field ``name`` if ``value`` cannot be
    parsed as an integer or is less than ``1``. ``name`` is included in the
    error message so the operator can locate the offending TOML key.
    """
    try:
        parsed = int(str(value))
    except ValueError:
        raise ConfigError(
            describe(
                f"{name} must be a positive integer",
                reason=f"the value {value!r} could not be parsed as an integer",
                fix=f"set {name} in config/env.toml to a whole number >= 1.",
            )
        ) from None
    if parsed < 1:
        raise ConfigError(
            describe(
                f"{name} must be a positive integer",
                reason=f"the value {parsed} is less than 1",
                fix=f"set {name} in config/env.toml to a whole number >= 1.",
            )
        )
    return parsed


def non_negative_float(value: object, name: str) -> float:
    """Parse ``value`` as a float ``>= 0.0``.

    Used for cost/rate config (e.g. telemetry pricing) where a negative value
    is meaningless. Raises :class:`ConfigError` with the field ``name`` if
    ``value`` is non-numeric or negative, so the operator can locate the
    offending TOML key.
    """
    try:
        parsed = float(str(value))
    except ValueError:
        raise ConfigError(
            describe(
                f"{name} must be a non-negative number",
                reason=f"the value {value!r} could not be parsed as a number",
                fix=f"set {name} in config/env.toml to a number >= 0.",
            )
        ) from None
    if parsed < 0:
        raise ConfigError(
            describe(
                f"{name} must be a non-negative number",
                reason=f"the value {parsed} is negative",
                fix=f"set {name} in config/env.toml to a number >= 0.",
            )
        )
    return parsed


def confidence_threshold(value: object, name: str) -> float:
    """Parse ``value`` as a confidence threshold in the inclusive range
    ``[0.0, 1.0]``.

    Confidence values come from the LLM as a number between 0 and 1 (zero =
    "no confidence", one = "fully confident"). The threshold filter compares
    findings' confidence to this value, so it must live in the same range.

    Raises :class:`ConfigError` if the value is non-numeric or out of range.
    """
    try:
        parsed = float(str(value))
    except ValueError:
        raise ConfigError(
            describe(
                f"{name} must be a number between 0.0 and 1.0",
                reason=f"the value {value!r} could not be parsed as a number",
                fix=f"set {name} in config/env.toml to a number in [0.0, 1.0].",
            )
        ) from None
    if parsed < 0.0 or parsed > 1.0:
        raise ConfigError(
            describe(
                f"{name} must be a number between 0.0 and 1.0",
                reason=f"the value {parsed} is outside the inclusive range [0.0, 1.0]",
                fix=f"set {name} in config/env.toml to a number in [0.0, 1.0].",
            )
        )
    return parsed


def confidence_map(value: object, name: str, *, allowed: Iterable[str]) -> dict[str, float]:
    """Parse a ``{category = confidence}`` TOML table into a lowercase dict.

    Each value is validated as a confidence in ``[0.0, 1.0]`` via
    :func:`confidence_threshold`; each key is stripped/lowercased and must be
    one of ``allowed`` (the canonical category taxonomy), so an operator typo
    like ``performnce`` fails loudly instead of silently never matching a
    finding. ``None`` (the key is absent) returns ``{}``.

    Raises :class:`ConfigError` if ``value`` is not a table, a key is not in
    ``allowed``, or a value is out of range.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table of category = confidence")
    allowed_set = {str(item).strip().lower() for item in allowed}
    out: dict[str, float] = {}
    for key, raw in value.items():
        category = str(key).strip().lower()
        if category not in allowed_set:
            raise ConfigError(
                f"{name} has unknown category {key!r}; allowed: {sorted(allowed_set)}"
            )
        out[category] = confidence_threshold(raw, f"{name}.{category}")
    return out


def one_of(value: object, name: str, choices: tuple[str, ...], *, default: str) -> str:
    """Parse ``value`` as a string restricted to ``choices`` (case-insensitive).

    Returns ``default`` when ``value`` is ``None`` (key absent). Otherwise the
    value is stripped and lowercased and must be one of ``choices`` (which are
    compared lowercase); anything else raises :class:`ConfigError` naming the
    field and the allowed set — mirrors the inline ``[scm].provider`` check.
    """
    if value is None:
        return default
    parsed = str(value).strip().lower()
    if parsed not in choices:
        raise ConfigError(
            describe(
                f"{name} must be one of {choices}, got {parsed!r}",
                reason="the configured value is not one of the allowed choices",
                fix=f"set {name} in config/env.toml to one of {choices}.",
            )
        )
    return parsed


def bool_value(value: object, name: str, *, default: bool) -> bool:
    """Parse ``value`` as a strict boolean.

    Accepts only Python ``bool`` (which TOML's ``true`` / ``false`` parses
    to). Raises :class:`ConfigError` for string ``"true"`` / ``"false"``,
    integers, or anything else — operators routinely write ``= "false"``
    expecting it to disable a feature, and Python's ``bool()`` silently
    treats a non-empty string as truthy, inverting the intent.

    ``default`` is returned when ``value`` is ``None`` (key absent from
    the TOML mapping).
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(
            describe(
                f"{name} must be a boolean (true/false, no quotes)",
                reason=(
                    f"the value {value!r} is not a TOML boolean "
                    "(quoted strings like \"false\" and numbers are rejected)"
                ),
                fix=f"set {name} in config/env.toml to bare true or false (no quotes).",
            )
        )
    return value


def text_value(value: object, name: str, *, default: str) -> str:
    """Parse ``value`` as a string.

    Accepts only Python ``str``. Raises :class:`ConfigError` if the TOML
    value is a list, table, or non-string scalar — operators sometimes
    write arrays or numbers by mistake, and ``str()`` silently coerces
    them to a misleading representation (``str([1, 2]) == "[1, 2]"``,
    ``str(False) == "False"``) that would then be posted verbatim as a
    comment body or similar payload.

    ``default`` is returned when ``value`` is ``None`` (key absent from
    the TOML mapping). Whitespace-only strings ARE accepted — callers
    treat that as a documented "disabled" signal.
    """
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(
            describe(
                f"{name} must be a string",
                reason=f"the value {value!r} is a {type(value).__name__}, not a TOML string",
                fix=f"set {name} in config/env.toml to a quoted string value.",
            )
        )
    return value


def string_list(value: object, name: str) -> list[str]:
    """Parse ``value`` as a list of strings, **preserving case**.

    Like :func:`lower_string_list` but does not lowercase — for values where
    case is significant: regex patterns and case-sensitive path globs (paths
    are case-sensitive on Linux, so lowercasing a glob would silently break
    matching). Each entry is stripped; empty entries and ``None`` are dropped.
    An empty or absent list returns ``[]``.

    Raises :class:`ConfigError` if ``value`` is not a list of scalars.
    """
    if value is None:
        return []
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise ConfigError(
            describe(
                f"{name} must be a list of strings",
                reason=f"the value {value!r} is not a TOML array (a bare string is not a list)",
                fix=f'set {name} in config/env.toml to a TOML array, e.g. {name} = ["a", "b"].',
            )
        )
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def lower_string_list(value: object, name: str) -> list[str]:
    """Parse ``value`` as a list of lowercase strings.

    Accepts TOML arrays of strings. Each entry is stripped of surrounding
    whitespace and lowercased so callers can do case-insensitive set
    membership checks (``"BLOCKING"`` in TOML matches ``"blocking"`` in a
    finding payload).

    An empty or absent list is returned as ``[]`` — callers interpret that as
    "no filter" rather than "filter out everything".

    Raises :class:`ConfigError` if ``value`` is not a list of scalars.
    """
    if value is None:
        return []
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise ConfigError(
            describe(
                f"{name} must be a list of strings",
                reason=f"the value {value!r} is not a TOML array (a bare string is not a list)",
                fix=f'set {name} in config/env.toml to a TOML array, e.g. {name} = ["a", "b"].',
            )
        )
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip().lower()
        if text:
            out.append(text)
    return out
