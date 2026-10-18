from __future__ import annotations

import re
from dataclasses import dataclass

from bubo.telemetry.config import ModelPricing


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input: int | None = None
    output: int | None = None
    cached: int | None = None
    total: int | None = None


_INPUT_TOKENS = re.compile(r"(?:input|prompt)\s+tokens?\s*[:=]\s*([0-9][0-9,]*)", re.IGNORECASE)
_OUTPUT_TOKENS = re.compile(
    r"(?:output|completion)\s+tokens?\s*[:=]\s*([0-9][0-9,]*)", re.IGNORECASE
)
_CACHED_TOKENS = re.compile(r"cached\s+(?:input\s+)?tokens?\s*[:=]\s*([0-9][0-9,]*)", re.IGNORECASE)
_TOTAL_TOKENS = re.compile(r"total\s+tokens?\s*[:=]\s*([0-9][0-9,]*)", re.IGNORECASE)
_TOKENS_USED = re.compile(r"tokens\s+used\s*\n\s*([0-9][0-9,]*)", re.IGNORECASE)


def parse_codex_token_usage(text: str) -> TokenUsage:
    """Parse known Codex token counters without exporting transcript text."""
    lowered = text.lower()
    input_tokens = _first_int(lowered, _INPUT_TOKENS)
    output_tokens = _first_int(lowered, _OUTPUT_TOKENS)
    cached_tokens = _first_int(lowered, _CACHED_TOKENS)
    total_tokens = _first_int(lowered, _TOTAL_TOKENS)

    if total_tokens is None:
        total_tokens = _first_int(lowered, _TOKENS_USED)

    if total_tokens is None and (
        input_tokens is not None or output_tokens is not None or cached_tokens is not None
    ):
        total_tokens = sum(value or 0 for value in (input_tokens, output_tokens, cached_tokens))

    return TokenUsage(
        input=input_tokens, output=output_tokens, cached=cached_tokens, total=total_tokens
    )


def add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    """Sum two usages; a counter stays None only when neither side has it."""

    def plus(x: int | None, y: int | None) -> int | None:
        return None if x is None and y is None else (x or 0) + (y or 0)

    return TokenUsage(
        input=plus(a.input, b.input),
        output=plus(a.output, b.output),
        cached=plus(a.cached, b.cached),
        total=plus(a.total, b.total),
    )


def estimate_cost_usd(usage: TokenUsage, pricing: ModelPricing) -> float:
    if usage.total is not None and usage.input is None and usage.output is None:
        return round(usage.total * pricing.input_per_1m / 1_000_000, 8)
    input_tokens = max((usage.input or 0) - (usage.cached or 0), 0)
    output_tokens = usage.output or 0
    cached_tokens = usage.cached or 0
    cost = (
        input_tokens * pricing.input_per_1m
        + output_tokens * pricing.output_per_1m
        + cached_tokens * pricing.cached_input_per_1m
    ) / 1_000_000
    return round(cost, 8)


def _first_int(text: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(text)
    if not match:
        return None
    return _int_or_none(match.group(1))


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).replace(",", ""))
    except ValueError:
        return None
