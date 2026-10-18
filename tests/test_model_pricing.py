from __future__ import annotations

import json
import os

import pytest

from bubo.telemetry import model_pricing
from bubo.telemetry.config import ModelPricing, TelemetryConfig
from bubo.telemetry.pricing import CACHE_TTL_SECONDS, live_price

LITELLM = {
    "gpt-6.1-sol": {
        "input_cost_per_token": 2e-06,
        "cache_read_input_token_cost": 1e-07,
        "output_cost_per_token": 1e-05,
    },
    "openai/gpt-only-prefixed": {"input_cost_per_token": 1e-06, "output_cost_per_token": 4e-06},
    "no-price": {"max_tokens": 10},
}


def _fetcher(payload=LITELLM):
    calls = []

    def fetch() -> bytes:
        calls.append(1)
        return json.dumps(payload).encode()

    return fetch, calls


def test_live_price_reads_litellm_per_token_costs(tmp_path) -> None:
    fetch, _ = _fetcher()
    price = live_price("gpt-6.1-sol", cache=tmp_path / "p.json", fetch=fetch)
    assert price == ModelPricing(input_per_1m=2.0, output_per_1m=10.0, cached_input_per_1m=0.1)


def test_live_price_tries_provider_prefix_and_defaults_cached_to_input(tmp_path) -> None:
    fetch, _ = _fetcher()
    price = live_price("gpt-only-prefixed", cache=tmp_path / "p.json", fetch=fetch)
    assert price == ModelPricing(input_per_1m=1.0, output_per_1m=4.0, cached_input_per_1m=1.0)


def test_live_price_is_none_for_unknown_or_unpriced_models(tmp_path) -> None:
    fetch, _ = _fetcher()
    cache = tmp_path / "p.json"
    assert live_price("not-a-model", cache=cache, fetch=fetch) is None
    assert live_price("no-price", cache=cache, fetch=fetch) is None
    assert live_price(None, cache=cache, fetch=fetch) is None


def test_price_list_is_fetched_once_per_day(tmp_path) -> None:
    fetch, calls = _fetcher()
    cache = tmp_path / "p.json"
    live_price("gpt-6.1-sol", cache=cache, fetch=fetch)
    live_price("gpt-6.1-sol", cache=cache, fetch=fetch)
    assert len(calls) == 1
    stale = cache.stat().st_mtime - CACHE_TTL_SECONDS - 1
    os.utime(cache, (stale, stale))
    live_price("gpt-6.1-sol", cache=cache, fetch=fetch)
    assert len(calls) == 2


def test_failed_refresh_keeps_the_stale_copy(tmp_path) -> None:
    cache = tmp_path / "p.json"
    cache.write_text(json.dumps(LITELLM))
    os.utime(cache, (0, 0))

    def offline() -> bytes:
        raise OSError("no network")

    assert live_price("gpt-6.1-sol", cache=cache, fetch=offline).input_per_1m == 2.0


@pytest.mark.parametrize("payload", [b"not json", b"[]"])
def test_bad_download_does_not_replace_the_cache(tmp_path, payload) -> None:
    cache = tmp_path / "p.json"
    cache.write_text(json.dumps(LITELLM))
    os.utime(cache, (0, 0))
    assert live_price("gpt-6.1-sol", cache=cache, fetch=lambda: payload).output_per_1m == 10.0
    assert json.loads(cache.read_text()) == LITELLM


def test_model_pricing_prefers_live_then_configured(monkeypatch, tmp_path) -> None:
    from bubo import paths
    from bubo.telemetry import pricing

    monkeypatch.setattr(paths, "DB", tmp_path / "reviewer.sqlite")
    monkeypatch.setattr(pricing, "_fetch", lambda: json.dumps(LITELLM).encode())
    configured = ModelPricing(input_per_1m=5.0, output_per_1m=30.0, cached_input_per_1m=0.5)
    cfg = TelemetryConfig(pricing={"default": configured})

    assert model_pricing("gpt-6.1-sol", cfg).input_per_1m == 2.0
    assert model_pricing("unknown-model", cfg) == configured
    off = TelemetryConfig(pricing={"default": configured}, live_pricing=False)
    assert model_pricing("gpt-6.1-sol", off) == configured
