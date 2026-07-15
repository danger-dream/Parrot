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
    assert estimate.input_ticks == 10 * model_pricing.TICKS_PER_USD
    assert estimate.output_ticks == 45 * model_pricing.TICKS_PER_USD
    assert estimate.cache_write_ticks == int(12.5 * model_pricing.TICKS_PER_USD)
    assert estimate.cache_read_ticks == 1 * model_pricing.TICKS_PER_USD
    assert estimate.total_ticks == int(68.5 * model_pricing.TICKS_PER_USD)


def test_priority_prices_are_used_for_fast_mode():
    standard = model_pricing.estimate_cost(
        "gpt-5.6-luna",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        long_context=False,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    priority = model_pricing.estimate_cost(
        "gpt-5.6-luna",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        priority=True,
        long_context=False,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    assert standard is not None and priority is not None
    assert standard.total_ticks == 7 * model_pricing.TICKS_PER_USD
    assert priority.total_ticks == 14 * model_pricing.TICKS_PER_USD


def test_priority_long_context_prefers_tier_specific_above_price_then_standard():
    settings = model_pricing.settings({"pricing": {"enabled": True}})
    # gpt-5.4 has standard above-272k prices but no combined priority+above
    # fields, so LiteLLM falls back to the standard above-threshold tariff.
    gpt = model_pricing.estimate_cost(
        "gpt-5.4",
        input_tokens=300_000,
        output_tokens=10_000,
        priority=True,
        pricing_settings=settings,
    )
    # Gemini publishes explicit priority+above prices; those must win.
    gemini = model_pricing.estimate_cost(
        "gemini-3-pro-preview",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        priority=True,
        pricing_settings=settings,
    )
    assert gpt is not None and gemini is not None
    assert gpt.total_ticks == int(1.725 * model_pricing.TICKS_PER_USD)
    assert gemini.total_ticks == int(40.32 * model_pricing.TICKS_PER_USD)


def test_long_context_threshold_uses_all_prompt_tokens_and_priority_prices():
    settings = model_pricing.settings({"pricing": {"enabled": True}})
    at_threshold = model_pricing.estimate_cost(
        "gpt-5.6-sol",
        input_tokens=100_000,
        cache_creation_tokens=100_000,
        cache_read_tokens=72_000,
        output_tokens=10_000,
        pricing_settings=settings,
    )
    above_threshold = model_pricing.estimate_cost(
        "gpt-5.6-sol",
        input_tokens=100_001,
        cache_creation_tokens=100_000,
        cache_read_tokens=72_000,
        output_tokens=10_000,
        pricing_settings=settings,
    )
    priority = model_pricing.estimate_cost(
        "gpt-5.6-sol",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        priority=True,
        pricing_settings=settings,
    )
    assert at_threshold is not None and above_threshold is not None and priority is not None
    assert above_threshold.input_ticks > at_threshold.input_ticks
    assert above_threshold.cache_write_ticks == at_threshold.cache_write_ticks * 2
    assert above_threshold.cache_read_ticks == at_threshold.cache_read_ticks * 2
    assert above_threshold.output_ticks * 2 == at_threshold.output_ticks * 3
    # LiteLLM publishes explicit above-272k prices but no combined
    # priority+above fields, so the explicit standard long-context tier wins.
    assert priority.total_ticks == 55 * model_pricing.TICKS_PER_USD


def test_explicit_above_200k_and_anthropic_fast_tariffs():
    settings = model_pricing.settings({"pricing": {"enabled": True}})
    long_claude = model_pricing.estimate_cost(
        "claude-sonnet-4-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        pricing_settings=settings,
    )
    standard = model_pricing.estimate_cost(
        "claude-opus-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing_settings=settings,
    )
    fast = model_pricing.estimate_cost(
        "claude-opus-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        priority=True,
        pricing_settings=settings,
    )
    ambiguous_cache_write = model_pricing.estimate_cost(
        "claude-opus-4-6",
        input_tokens=1_000,
        cache_creation_tokens=1_000,
        pricing_settings=settings,
    )
    assert long_claude is not None and standard is not None and fast is not None
    assert long_claude.total_ticks == int(29.1 * model_pricing.TICKS_PER_USD)
    assert fast.total_ticks == standard.total_ticks * 6
    assert ambiguous_cache_write is None
    assert model_pricing.has_ambiguous_cache_write_ttl(
        "claude-opus-4-6", pricing_settings=settings
    )


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


def test_parse_catalog_fails_closed_for_multiple_context_tiers():
    parsed = model_pricing._parse_catalog(
        {
            "multi-tier": {
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
                "input_cost_per_token_above_200k_tokens": 0.000002,
                "input_cost_per_token_above_1m_tokens": 0.000004,
            },
            "valid": {
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
            },
        }
    )
    assert "multi-tier" not in parsed
    assert "valid" in parsed


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
    # A truncated/arbitrary metadata fragment is not a trusted JSON/SSE path.
    truncated_json_tail = (
        ('{"output":"' + ("x" * 300_000) + '","usage":{"cost_in_usd_ticks":')
        + str(ticks)
        + "}}"
    )[-262_144:]
    assert model_pricing.extract_actual_cost_ticks(truncated_json_tail) is None
    assert model_pricing.extract_actual_cost_ticks(
        'metadata: {"usage":{"cost_in_usd_ticks":123456789}}'
    ) is None
