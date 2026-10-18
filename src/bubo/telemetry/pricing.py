"""Live per-model token prices from LiteLLM's public price list.

The list is fetched at most once a day and cached next to the SQLite DB, so a
review never waits on it for long and keeps working offline from the cached
copy. ``[telemetry].live_pricing = false`` turns the fetch off and prices every
model with the ``[telemetry]`` ``*_per_1m`` values instead.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bubo import paths
from bubo.events import log
from bubo.telemetry.config import ModelPricing, TelemetryConfig

LITELLM_PRICES_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
CACHE_TTL_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT_SECONDS = 5


def model_pricing(model: str | None, cfg: TelemetryConfig) -> ModelPricing:
    """Price for ``model``: the live list first, then the configured prices."""
    if cfg.live_pricing:
        live = live_price(model)
        if live is not None:
            return live
    return cfg.price_for(model)


def live_price(
    model: str | None,
    *,
    cache: Path | None = None,
    fetch: Callable[[], bytes] | None = None,
    now: Callable[[], float] = time.time,
) -> ModelPricing | None:
    if not model:
        return None
    prices = _prices(cache or paths.DB.parent / "model-prices.json", fetch or _fetch, now)
    entry = prices.get(model) or prices.get(f"openai/{model}") or prices.get(f"anthropic/{model}")
    if not isinstance(entry, dict):
        return None
    input_cost = _per_1m(entry.get("input_cost_per_token"))
    output_cost = _per_1m(entry.get("output_cost_per_token"))
    if input_cost is None or output_cost is None:
        return None
    cached_cost = _per_1m(entry.get("cache_read_input_token_cost"))
    return ModelPricing(
        input_per_1m=input_cost,
        output_per_1m=output_cost,
        cached_input_per_1m=input_cost if cached_cost is None else cached_cost,
    )


def _prices(cache: Path, fetch: Callable[[], bytes], now: Callable[[], float]) -> dict[str, Any]:
    cached = _read(cache)
    fresh = cached is not None and now() - cache.stat().st_mtime < CACHE_TTL_SECONDS
    if fresh:
        return cached or {}
    try:
        raw = fetch()
        prices = json.loads(raw)
        if not isinstance(prices, dict):
            raise ValueError("price list is not a JSON object")
    except Exception as exc:
        log("model_prices_refresh_failed", error=type(exc).__name__, stale=cached is not None)
        return cached or {}
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
    tmp.write_bytes(raw)
    tmp.replace(cache)
    return prices


def _fetch() -> bytes:
    with urllib.request.urlopen(LITELLM_PRICES_URL, timeout=FETCH_TIMEOUT_SECONDS) as resp:
        return resp.read()


def _read(cache: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(cache.read_bytes())
    except OSError, ValueError:
        return None
    return value if isinstance(value, dict) else None


def _per_1m(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # Round off float noise from per-token prices (1e-07 * 1e6 != 0.1).
    return round(float(value) * 1_000_000, 9)
