from __future__ import annotations

import json

from src import model_pricing


def test_bundled_gpt56_prices_and_cache_components():
    model_pricing.initialize()
    estimate = model_pricing.estimate_cost(
        "gpt-5.6-sol",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    assert estimate is not None
    assert estimate.input_ticks == 5 * model_pricing.TICKS_PER_USD
    assert estimate.output_ticks == 30 * model_pricing.TICKS_PER_USD
    assert estimate.cache_write_ticks == int(6.25 * model_pricing.TICKS_PER_USD)
    assert estimate.cache_read_ticks == int(0.5 * model_pricing.TICKS_PER_USD)
    assert estimate.total_ticks == int(41.75 * model_pricing.TICKS_PER_USD)


def test_priority_prices_are_used_for_fast_mode():
    standard = model_pricing.estimate_cost(
        "gpt-5.6-luna",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    priority = model_pricing.estimate_cost(
        "gpt-5.6-luna",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        priority=True,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    assert standard is not None and priority is not None
    assert standard.total_ticks == 7 * model_pricing.TICKS_PER_USD
    assert priority.total_ticks == 14 * model_pricing.TICKS_PER_USD


def test_alias_override_and_provider_date_fallback():
    cfg = {
        "pricing": {
            "enabled": True,
            "aliases": {"private-sol": "private-price"},
            "overrides": {
                "private-price": {
                    "inputPerMillion": 2,
                    "outputPerMillion": 8,
                    "cacheWritePerMillion": 3,
                    "cacheReadPerMillion": 0.2,
                }
            },
        }
    }
    custom = model_pricing.estimate_cost(
        "private-sol",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        pricing_settings=model_pricing.settings(cfg),
    )
    assert custom is not None
    assert custom.total_ticks == int(13.2 * model_pricing.TICKS_PER_USD)

    dated = model_pricing.estimate_cost(
        "openai/gpt-5.6-sol-2026-07-01",
        input_tokens=1_000_000,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    assert dated is not None
    assert dated.pricing_model == "gpt-5.6-sol"


def test_unknown_or_disabled_model_is_not_silently_zero_priced():
    assert model_pricing.estimate_cost(
        "definitely-unknown-model",
        input_tokens=123,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    ) is None
    assert model_pricing.estimate_cost(
        "gpt-5.6-sol",
        input_tokens=123,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": False}}),
    ) is None


def test_parse_catalog_rejects_image_only_entry_for_token_billing():
    parsed = model_pricing._parse_catalog(
        {
            "image-only": {"output_cost_per_image": 0.04, "mode": "image_generation"},
            "text": {"input_cost_per_token": 0.000001, "output_cost_per_token": 0.000002},
        }
    )
    assert "image-only" not in parsed
    assert "text" in parsed


def test_extract_actual_xai_cost_from_json_and_sse():
    ticks = 123456789
    assert model_pricing.extract_actual_cost_ticks(
        json.dumps({"usage": {"cost_in_usd_ticks": ticks}})
    ) == ticks
    sse = "\n".join(
        [
            'event: response.completed',
            'data: {"response":{"usage":{"cost_in_usd_ticks":"123456789"}}}',
            "",
            "data: [DONE]",
        ]
    )
    assert model_pricing.extract_actual_cost_ticks(sse) == ticks
