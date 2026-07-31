from __future__ import annotations

import json

import pytest

from src import model_pricing


def test_bundled_gpt56_prices_and_cache_components():
    model_pricing.initialize()
    estimate = model_pricing.estimate_cost(
        "openai/gpt-5.6-sol",
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
        "openai/gpt-5.6-luna",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        long_context=False,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    priority = model_pricing.estimate_cost(
        "openai/gpt-5.6-luna",
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
    # models.dev fast mode is an exact replacement tariff and must not be
    # overwritten by the standard long-context tier.
    gpt = model_pricing.estimate_cost(
        "openai/gpt-5.4",
        input_tokens=300_000,
        output_tokens=10_000,
        priority=True,
        pricing_settings=settings,
    )
    # Gemini has no fast tariff in models.dev; a proven priority request cannot
    # silently fall back to the standard tariff.
    gemini = model_pricing.estimate_cost(
        "google/gemini-3-pro-preview",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        priority=True,
        pricing_settings=settings,
    )
    assert gpt is not None and gemini is None
    assert gpt.total_ticks == int(1.8 * model_pricing.TICKS_PER_USD)


def test_long_context_threshold_uses_all_prompt_tokens_and_priority_prices():
    settings = model_pricing.settings({"pricing": {"enabled": True}})
    at_threshold = model_pricing.estimate_cost(
        "openai/gpt-5.6-sol",
        input_tokens=100_000,
        cache_creation_tokens=100_000,
        cache_read_tokens=72_000,
        output_tokens=10_000,
        pricing_settings=settings,
    )
    above_threshold = model_pricing.estimate_cost(
        "openai/gpt-5.6-sol",
        input_tokens=100_001,
        cache_creation_tokens=100_000,
        cache_read_tokens=72_000,
        output_tokens=10_000,
        pricing_settings=settings,
    )
    priority = model_pricing.estimate_cost(
        "openai/gpt-5.6-sol",
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
    # models.dev fast mode replaces the standard tariff at every context size.
    assert priority.total_ticks == 70 * model_pricing.TICKS_PER_USD


def test_explicit_above_200k_and_anthropic_fast_tariffs():
    settings = model_pricing.settings({"pricing": {"enabled": True}})
    long_claude = model_pricing.estimate_cost(
        "anthropic/claude-sonnet-4-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        pricing_settings=settings,
    )
    standard = model_pricing.estimate_cost(
        "anthropic/claude-opus-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing_settings=settings,
    )
    fast = model_pricing.estimate_cost(
        "anthropic/claude-opus-4-6",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        priority=True,
        pricing_settings=settings,
    )
    ambiguous_cache_write = model_pricing.estimate_cost(
        "anthropic/claude-opus-4-6",
        input_tokens=1_000,
        cache_creation_tokens=1_000,
        pricing_settings=settings,
    )
    assert long_claude is not None and standard is not None and fast is not None
    assert long_claude.total_ticks == int(18.3 * model_pricing.TICKS_PER_USD)
    assert fast.total_ticks == standard.total_ticks * 6
    assert ambiguous_cache_write is None
    assert model_pricing.has_ambiguous_cache_write_ttl(
        "anthropic/claude-opus-4-6", pricing_settings=settings
    )


def test_alias_override_requires_explicit_mapping_for_dated_variants():
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

    dated_without_alias = model_pricing.estimate_cost(
        "openai/gpt-5.6-sol-2026-07-01",
        input_tokens=1_000_000,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    assert dated_without_alias is None
    dated = model_pricing.estimate_cost(
        "openai/gpt-5.6-sol-2026-07-01",
        input_tokens=1_000_000,
        pricing_settings=model_pricing.settings({
            "pricing": {
                "enabled": True,
                "aliases": {
                    "openai/gpt-5.6-sol-2026-07-01": "openai/gpt-5.6-sol",
                },
            }
        }),
    )
    assert dated is not None
    assert dated.pricing_model == "openai/gpt-5.6-sol"


def test_unknown_or_disabled_model_is_not_silently_zero_priced():
    assert model_pricing.estimate_cost(
        "definitely-unknown-model",
        input_tokens=123,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    ) is None


def test_channel_provider_mapping_qualifies_models_dev_provider_model_id():
    pricing_settings = model_pricing.settings({
        "pricing": {
            "enabled": True,
            "channelProviders": {"api:Router": "openrouter"},
        }
    })
    assert model_pricing.provider_pricing_model(
        "openai/gpt-5.6-luna", "api:Router",
        pricing_settings=pricing_settings,
    ) == "openrouter/openai/gpt-5.6-luna"
    assert model_pricing.provider_pricing_model(
        "openai/gpt-5.6-luna", "api:Unknown",
        pricing_settings=pricing_settings,
    ) == "openai/gpt-5.6-luna"
    assert model_pricing.provider_pricing_model(
        "gpt-5.6-luna", "oauth:openai:user@example.com",
        pricing_settings=pricing_settings,
    ) == "openai/gpt-5.6-luna"

    aliased = model_pricing.settings({
        "pricing": {
            "enabled": True,
            "aliases": {"my-luna": "openai/gpt-5.6-luna"},
        }
    })
    assert model_pricing.provider_pricing_model(
        "my-luna", "oauth:openai:user@example.com", pricing_settings=aliased,
    ) == "openai/gpt-5.6-luna"
    assert model_pricing.estimate_cost(
        "openai/gpt-5.6-sol",
        input_tokens=123,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": False}}),
    ) is None


def test_explicit_override_precedes_channel_provider_and_catalog_availability(monkeypatch):
    pricing_settings = model_pricing.settings({
        "pricing": {
            "enabled": True,
            "channelProviders": {"api:Router": "openrouter"},
            "overrides": {
                "private-model": {
                    "inputPerMillion": 2,
                    "outputPerMillion": 8,
                }
            },
        }
    })
    assert model_pricing.provider_pricing_model(
        "private-model", "api:Router", pricing_settings=pricing_settings,
    ) == "private-model"

    monkeypatch.setattr(model_pricing, "_initialized", False)

    def unavailable():
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(model_pricing, "initialize", unavailable)
    estimate = model_pricing.estimate_cost(
        "private-model",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing_settings=pricing_settings,
    )
    assert estimate is not None
    assert estimate.total_ticks == 10 * model_pricing.TICKS_PER_USD
    assert model_pricing.resolve_price(
        "openai/gpt-5.6-sol",
        pricing_settings=model_pricing.settings({"pricing": {"enabled": False}}),
    ) is None


def test_parse_catalog_rejects_image_only_entry_for_token_billing():
    parsed = model_pricing._parse_catalog(
        {
            "provider": {
                "id": "provider",
                "models": {
                    "image-only": {"cost": {"output": 0.04}},
                    "text": {"cost": {"input": 1, "output": 2}},
                },
            }
        },
        {"provider/image-only": {}, "provider/text": {}},
    )
    assert "provider/image-only" not in parsed
    assert "provider/text" in parsed


def test_parse_catalog_fails_closed_for_multiple_context_tiers():
    parsed = model_pricing._parse_catalog(
        {
            "provider": {
                "models": {
                    "multi-tier": {
                        "cost": {
                            "input": 1,
                            "output": 2,
                            "tiers": [
                                {"input": 2, "output": 4,
                                 "tier": {"type": "context", "size": 200_000}},
                                {"input": 4, "output": 8,
                                 "tier": {"type": "context", "size": 1_000_000}},
                            ],
                        }
                    },
                    "valid": {"cost": {"input": 1, "output": 2}},
                }
            }
        },
        {"provider/multi-tier": {}, "provider/valid": {}},
    )
    assert "provider/multi-tier" not in parsed
    assert "provider/valid" in parsed


def test_models_dev_metadata_only_builds_unambiguous_canonical_aliases():
    api = {
        "official": {
            "models": {"shared": {"cost": {"input": 1, "output": 2}}}
        },
        "gateway": {
            "models": {"shared": {"cost": {"input": 9, "output": 10}}}
        },
    }
    models = {"official/shared": {"name": "Shared", "cost": {"input": 999}}}
    parsed, aliases, _ = model_pricing._parse_models_dev_catalog(api, models)
    assert parsed["official/shared"].input_per_token == 0.000001
    assert parsed["gateway/shared"].input_per_token == 0.000009
    assert "shared" not in aliases


def test_models_dev_metadata_builds_bare_alias_for_one_provider_target_only():
    parsed, aliases, _ = model_pricing._parse_models_dev_catalog(
        {
            "official": {
                "models": {"unique": {"cost": {"input": 1, "output": 2}}}
            },
            "gateway": {
                "models": {
                    "official/unique": {"cost": {"input": 9, "output": 10}}
                }
            },
        },
        {"official/unique": {"name": "Unique"}},
    )
    assert "official/unique" in parsed
    assert "gateway/official/unique" in parsed
    assert aliases["unique"] == "official/unique"


@pytest.mark.parametrize(
    "cost",
    [
        {"input": "1", "output": 2},
        {"input": 1, "output": True},
        {"input": 1, "output": 2, "reasoning": 3},
        {"input": 1, "output": 2, "input_audio": 4},
        {"input": 1, "output": 2, "output_audio": 4},
    ],
)
def test_models_dev_parser_rejects_wrong_types_and_untracked_token_tariffs(cost):
    parsed = model_pricing._parse_catalog(
        {
            "provider": {
                "models": {
                    "unsafe": {"cost": cost},
                    "safe": {"cost": {"input": 1, "output": 2}},
                }
            }
        },
        {"provider/unsafe": {}, "provider/safe": {}},
    )
    assert "provider/unsafe" not in parsed


def test_models_dev_parser_accepts_equal_specialist_and_aggregate_tariffs():
    parsed = model_pricing._parse_catalog(
        {
            "provider": {
                "models": {
                    "safe": {
                        "cost": {
                            "input": 1,
                            "output": 2,
                            "reasoning": 2,
                            "input_audio": 1,
                            "output_audio": 2,
                        }
                    }
                }
            }
        },
        {"provider/safe": {}},
    )
    assert "provider/safe" in parsed


def test_fast_zero_input_tariff_is_not_replaced_by_standard_input_price():
    entry = model_pricing._entry_from_models_dev({
        "cost": {"input": 1, "output": 2},
        "experimental": {
            "modes": {"fast": {"cost": {"input": 0, "output": 3}}}
        },
    })
    assert entry is not None
    assert entry.priority_input_per_token == 0
    assert entry.priority_cache_write_per_token == 0
    assert entry.priority_cache_read_per_token == 0


@pytest.mark.parametrize("bad", [-1, True, 1.5, float("inf"), 1 << 63])
def test_estimate_rejects_invalid_or_oversized_token_counts(bad):
    assert model_pricing.estimate_cost(
        "openai/gpt-5.6-luna",
        input_tokens=bad,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    ) is None


def test_estimate_rejects_costs_outside_sqlite_integer_range():
    pricing_settings = model_pricing.settings({
        "pricing": {
            "enabled": True,
            "overrides": {
                "too-expensive": {
                    "inputPerMillion": 1e300,
                    "outputPerMillion": 1e300,
                }
            },
        }
    })
    assert model_pricing.estimate_cost(
        "too-expensive", input_tokens=1_000_000,
        pricing_settings=pricing_settings,
    ) is None


@pytest.mark.asyncio
async def test_refresh_fetches_both_models_dev_sources_and_writes_one_atomic_bundle(
    tmp_path, monkeypatch,
):
    from src import config, upstream

    api = {
        "provider": {
            "models": {
                f"m{i}": {"id": f"m{i}", "cost": {"input": 1, "output": 2}}
                for i in range(500)
            }
        }
    }
    models = {f"provider/m{i}": {"id": f"provider/m{i}"} for i in range(100)}
    payloads = {
        "https://models.dev/api.json": json.dumps(api).encode(),
        "https://models.dev/models.json": json.dumps(models).encode(),
    }

    class Response:
        def __init__(self, content):
            self.content = content
            self.headers = {"content-length": str(len(content))}
            self.produced = 0
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            for offset in range(0, len(self.content), 257):
                chunk = self.content[offset:offset + 257]
                self.produced += len(chunk)
                yield chunk

    class Client:
        def __init__(self):
            self.calls = []
            self.responses = []

        def stream(self, method, url, timeout):
            self.calls.append((method, url, timeout))
            response = Response(payloads[url])
            self.responses.append(response)
            return response

    client = Client()
    pricing_cfg = {
        "enabled": True,
        "autoUpdate": True,
        "sourceUrl": "https://models.dev/api.json",
        "modelsUrl": "https://models.dev/models.json",
    }
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "get", lambda: {"pricing": pricing_cfg})
    monkeypatch.setattr(upstream, "get_client", lambda: client)

    monkeypatch.setattr(
        model_pricing, "_MAX_REMOTE_CATALOG_BYTES",
        len(payloads["https://models.dev/api.json"])
        + len(payloads["https://models.dev/models.json"]),
    )
    model_pricing.reset_for_tests()
    assert await model_pricing.refresh_once() is True
    cache = tmp_path / "models_dev_catalog.json.gz"
    assert cache.is_file() and cache.stat().st_size > 0
    assert {url for _, url, _ in client.calls} == {
        "https://models.dev/api.json",
        "https://models.dev/models.json",
    }
    before = cache.read_bytes()

    # The two files share one budget. Stop the companion stream at its first
    # crossing chunk and preserve the existing last-known-good cache.
    api_size = len(payloads["https://models.dev/api.json"])
    models_size = len(payloads["https://models.dev/models.json"])
    monkeypatch.setattr(
        model_pricing, "_MAX_REMOTE_CATALOG_BYTES", api_size + models_size // 2,
    )
    with pytest.raises(ValueError):
        await model_pricing.refresh_once()
    crossing = client.responses[-1]
    assert crossing.produced < models_size
    assert crossing.produced <= models_size // 2 + 257
    assert crossing.closed is True
    assert cache.read_bytes() == before

    model_pricing.reset_for_tests()
    assert model_pricing.reload_local_catalog() is True
    assert model_pricing.catalog_status()["source"] == "cache"
    estimate = model_pricing.estimate_cost(
        "provider/m0", input_tokens=1_000_000,
        pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
    )
    assert estimate is not None and estimate.input_ticks == model_pricing.TICKS_PER_USD

    # A malformed companion catalog must not replace the last good bundle.
    monkeypatch.setattr(model_pricing, "_MAX_REMOTE_CATALOG_BYTES", 16 * 1024 * 1024)
    payloads["https://models.dev/models.json"] = b"[]"
    with pytest.raises(ValueError):
        await model_pricing.refresh_once()
    assert cache.read_bytes() == before
    model_pricing.reset_for_tests()


@pytest.mark.asyncio
async def test_catalog_download_content_length_rejects_before_body_and_chunked_stops_at_crossing():
    class Response:
        def __init__(self, chunks, headers):
            self.chunks = chunks
            self.headers = headers
            self.produced = 0
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            for chunk in self.chunks:
                self.produced += len(chunk)
                yield chunk

    class Client:
        def __init__(self, response):
            self.response = response

        def stream(self, method, url, timeout):
            assert method == "GET" and timeout == 20.0
            return self.response

    length_response = Response([b"body-must-not-be-read"], {"content-length": "9"})
    with pytest.raises(ValueError):
        await model_pricing._download_catalog_bounded(
            Client(length_response), "https://models.dev/api.json", 8,
        )
    assert length_response.produced == 0
    assert length_response.closed is True

    malformed = Response([b"body-must-not-be-read"], {"content-length": "8, 9"})
    with pytest.raises(ValueError):
        await model_pricing._download_catalog_bounded(
            Client(malformed), "https://models.dev/api.json", 100,
        )
    assert malformed.produced == 0
    assert malformed.closed is True

    chunked = Response([b"1234", b"5678", b"9", b"not-read"], {})
    with pytest.raises(ValueError):
        await model_pricing._download_catalog_bounded(
            Client(chunked), "https://models.dev/api.json", 8,
        )
    assert chunked.produced == 9
    assert chunked.closed is True


def test_corrupt_runtime_catalog_falls_back_to_bundled_snapshot(tmp_path, monkeypatch):
    from src import config

    (tmp_path / "models_dev_catalog.json.gz").write_bytes(b"not-a-gzip-catalog")
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    model_pricing.reset_for_tests()
    model_pricing.initialize()
    status = model_pricing.catalog_status()
    assert status["source"] == "bundled"
    assert status["models"] >= 500
    assert model_pricing.resolve_price("openai/gpt-5.6-sol") is not None
    model_pricing.reset_for_tests()


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


def test_malformed_service_tier_fail_closes_otherwise_valid_usage():
    normalized = model_pricing.normalize_response_billing({
        "service_tier": {"unexpected": "object"},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    })
    assert normalized.usage_invalid is True
    assert normalized.usage_observed is False


def test_service_tier_classifier_rejects_unknown_tariffs():
    assert model_pricing.priority_from_service_tier(None) is False
    assert model_pricing.priority_from_service_tier("default") is False
    assert model_pricing.priority_from_service_tier("priority") is True
    assert model_pricing.priority_from_service_tier("fast") is True
    assert model_pricing.priority_from_service_tier("flex") is None


def test_models_dev_missing_cache_tariffs_are_explicit_zero_in_all_contexts():
    entry = model_pricing._entry_from_models_dev({
        "cost": {
            "input": 3,
            "output": 15,
            "tiers": [{
                "tier": {"type": "context", "size": 200_000},
                "input": 6,
                "output": 22.5,
            }],
        }
    })
    assert entry is not None
    assert entry.cache_write_per_token == 0
    assert entry.cache_read_per_token == 0
    assert entry.above_cache_write_per_token == 0
    assert entry.above_cache_read_per_token == 0


def test_dispatch_binding_provider_facts_do_not_come_from_channel_names_or_bare_catalog_aliases(monkeypatch):
    overrides = {
        "provider-a/shared/model": {"inputPerMillion": 1, "outputPerMillion": 2},
        "provider-b/shared/model": {"inputPerMillion": 9, "outputPerMillion": 18},
    }
    settings = model_pricing.settings({
        "pricing": {
            "enabled": True,
            "channelProviders": {
                "api:a": "provider-a",
                "api:b": "provider-b",
            },
            "overrides": overrides,
        }
    })
    a = model_pricing.build_pricing_binding(
        channel_key="api:a", channel_type="api",
        upstream_protocol="openai-responses", outbound_model_id="shared/model",
        pricing_settings=settings,
    )
    b = model_pricing.build_pricing_binding(
        channel_key="api:b", channel_type="api",
        upstream_protocol="openai-responses", outbound_model_id="shared/model",
        pricing_settings=settings,
    )
    assert a.model_id == b.model_id == "shared/model"
    # pricing.channelProviders/overrides are no longer metadata bindings.
    assert (a.provider_id, a.pricing_key, a.tariff) == (None, None, None)
    assert (b.provider_id, b.pricing_key, b.tariff) == (None, None, None)
    assert a.binding_source == b.binding_source == "unbound"
    assert a.binding_version != b.binding_version

    monkeypatch.setattr(model_pricing, "_initialized", True)
    monkeypatch.setattr(model_pricing, "_catalog", {
        "openai/unique": model_pricing.PricingEntry(1e-6, 2e-6, 0, 0),
    })
    monkeypatch.setattr(model_pricing, "_catalog_aliases", {"unique": "openai/unique"})
    for channel_name in ("api:xai-looking", "api:openai-looking"):
        unproven = model_pricing.build_pricing_binding(
            channel_key=channel_name,
            channel_type="api",
            upstream_protocol="openai-responses",
            outbound_model_id="unique",
            pricing_settings=model_pricing.settings({"pricing": {"enabled": True}}),
        )
        assert unproven.provider_id is None
        assert unproven.pricing_key is None
        assert unproven.tariff is None


def test_dispatch_binding_preserves_exact_model_id_with_slash():
    binding = model_pricing.build_pricing_binding(
        channel_key="api:router",
        channel_type="api",
        upstream_protocol="openai-responses",
        outbound_model_id="lab/model/version",
        pricing_settings=model_pricing.settings({
            "pricing": {
                "enabled": True,
                "channelProviders": {"api:router": "provider"},
                "overrides": {
                    "provider/lab/model/version": {
                        "inputPerMillion": 2,
                        "outputPerMillion": 8,
                    }
                },
            }
        }),
    )
    assert binding.model_id == "lab/model/version"
    assert binding.provider_id is None
    assert binding.pricing_key is None
    assert binding.tariff is None
    assert binding.binding_json
    assert binding.binding_version.startswith("binding-v1:")
