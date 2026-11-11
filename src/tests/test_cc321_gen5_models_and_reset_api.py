"""Gen-5 Claude supplemental metadata/pricing and multi-card reset API regressions."""
from __future__ import annotations

from ._isolation import isolate

isolate()

import json
from decimal import Decimal

import pytest

from src import config, model_metadata, model_pricing
from src.tests.test_management_oauth_api import auth_client, request


@pytest.fixture(autouse=True)
def _reset_catalog_and_config():
    model_pricing.reset_for_tests()
    model_pricing.initialize()
    config.update(lambda c: c.pop("modelBindings", None) or c.pop("pricing", None))
    yield
    model_pricing.reset_for_tests()


def _usd(ticks: int) -> Decimal:
    return (Decimal(ticks) / Decimal(model_pricing.TICKS_PER_USD)).quantize(Decimal("0.00000001"))


def test_gen5_supplemental_models_pricing_tiers_and_dot_aliases():
    meta_fable = model_pricing.catalog_metadata("anthropic/claude-fable-5-2")
    assert meta_fable is not None
    assert meta_fable["contextWindow"] == 1_000_000
    assert meta_fable["maxOutputTokens"] == 128_000
    assert meta_fable["compactTriggerTokens"] == 697_600

    for spelling in (
        "claude-fable-5-2",
        "claude-fable-5.2",
        "anthropic/claude-fable-5-2",
        "anthropic/claude-fable-5.2",
        "claude/claude-fable-5.2",
    ):
        res = model_pricing.estimate_cost(
            spelling,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        assert res is not None, spelling
        assert _usd(res.input_ticks) == Decimal("8.00000000")
        assert _usd(res.output_ticks) == Decimal("40.00000000")
        assert _usd(res.cache_read_ticks) == Decimal("0.20000000")
        assert _usd(res.total_ticks) == Decimal("48.20000000")

        assert (
            model_pricing.estimate_cost(
                spelling,
                input_tokens=100,
                output_tokens=100,
                cache_creation_tokens=100,
            )
            is None
        )

    config.update(lambda c: c.update({
        "modelBindings": {
            "defaults": {
                "claude-fable-5-2": {"target": "anthropic/claude-fable-5-2", "source": "auto"},
                "claude-haiku-5-5": {"target": "anthropic/claude-haiku-5-5", "source": "auto"},
            },
            "scoped": {},
        }
    }))
    fable_binding = model_pricing.build_pricing_binding(
        channel_key="oauth:claude:acc1@example.com",
        channel_type="oauth",
        upstream_protocol="anthropic",
        outbound_model_id="claude-fable-5-2",
        client_visible_model="claude-fable-5.2",
    )
    fable_settled = model_pricing.estimate_cost_from_binding(
        fable_binding,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        cache_creation_5m_tokens=1_000_000,
        cache_creation_1h_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert fable_settled is not None
    assert _usd(fable_settled[0].cache_write_ticks) == Decimal("10.00000000")
    assert _usd(fable_settled[0].total_ticks) == Decimal("58.20000000")

    assert model_pricing.canonical_official_model("claude-fable-5-2") == "anthropic/claude-fable-5-2"
    assert model_pricing.canonical_official_model("claude-fable-5.2") == "anthropic/claude-fable-5-2"
    assert model_pricing.canonical_official_model("claude-haiku-5.5") == "anthropic/claude-haiku-5-5"

    assert _usd(
        model_pricing.estimate_cost(
            "claude-fable-5-1", input_tokens=1_000_000, output_tokens=1_000_000
        ).total_ticks
    ) == Decimal("60.00000000")
    assert _usd(
        model_pricing.estimate_cost(
            "claude-opus-5-5", input_tokens=1_000_000, output_tokens=1_000_000
        ).total_ticks
    ) == Decimal("24.00000000")
    assert _usd(
        model_pricing.estimate_cost(
            "claude-sonnet-5-5", input_tokens=1_000_000, output_tokens=1_000_000
        ).total_ticks
    ) == Decimal("12.00000000")

    meta_haiku = model_pricing.catalog_metadata("anthropic/claude-haiku-5-5")
    assert meta_haiku is not None
    assert meta_haiku["contextWindow"] == 1_000_000
    assert meta_haiku["maxOutputTokens"] == 128_000
    assert meta_haiku["compactTriggerTokens"] == 100_000
    assert model_pricing.long_context_threshold("claude-haiku-5-5") == 100_000
    assert model_pricing.long_context_threshold("claude-haiku-5.5") == 100_000

    haiku_binding = model_pricing.build_pricing_binding(
        channel_key="oauth:claude:acc1@example.com",
        channel_type="oauth",
        upstream_protocol="anthropic",
        outbound_model_id="claude-haiku-5-5",
        client_visible_model="claude-haiku-5.5",
    )
    haiku_base = model_pricing.estimate_cost_from_binding(
        haiku_binding,
        input_tokens=80_000,
        cache_creation_tokens=10_000,
        cache_creation_5m_tokens=10_000,
        cache_creation_1h_tokens=0,
        cache_read_tokens=10_000,
        output_tokens=10_000,
    )
    assert haiku_base is not None
    assert _usd(haiku_base[0].total_ticks) == Decimal("0.01435000")

    haiku_long = model_pricing.estimate_cost_from_binding(
        haiku_binding,
        input_tokens=80_001,
        cache_creation_tokens=10_000,
        cache_creation_5m_tokens=10_000,
        cache_creation_1h_tokens=0,
        cache_read_tokens=10_000,
        output_tokens=10_000,
    )
    assert haiku_long is not None
    expected_long = (
        Decimal(80_001) * Decimal("0.0000005")
        + Decimal(10_000) * Decimal("0.0000025")
        + Decimal(10_000) * Decimal("0.000000625")
        + Decimal(10_000) * Decimal("0.00000005")
    ).quantize(Decimal("0.00000001"))
    assert _usd(haiku_long[0].total_ticks) == expected_long


def test_gen5_supplement_respects_models_dev_authority_overrides_and_hides_mythos():
    assert model_pricing.catalog_model("anthropic/claude-mythos-5") is None
    assert model_pricing.estimate_cost(
        "claude-mythos-5", input_tokens=1000, output_tokens=1000
    ) is None
    assert model_pricing.estimate_cost(
        "claude-nonexistent-9-9", input_tokens=1000, output_tokens=1000
    ) is None
    catalog_keys = {item["key"] for item in model_pricing.catalog_models()}
    assert "anthropic/claude-mythos-5" not in catalog_keys

    api_payload = {
        "anthropic": {
            "name": "Anthropic",
            "models": {
                "claude-fable-5-2": {
                    "id": "claude-fable-5-2",
                    "name": "Published Fable 5.2",
                    "limit": {"context": 2_000_000, "output": 256_000},
                    "cost": {"input": 7, "output": 35, "cache_write": 8.75, "cache_read": 0.7},
                }
            },
        }
    }
    models_payload = {
        "anthropic/claude-fable-5-2": {
            "id": "anthropic/claude-fable-5-2",
            "limit": {"context": 2_000_000, "output": 256_000},
        }
    }
    eff_api, eff_models = model_pricing._supplement_official_catalog_payloads(
        api_payload, models_payload
    )
    assert eff_api["anthropic"]["models"]["claude-fable-5-2"]["cost"]["input"] == 7
    assert eff_api["anthropic"]["models"]["claude-fable-5-2"]["limit"]["context"] == 2_000_000
    assert eff_models["anthropic/claude-fable-5-2"]["limit"]["output"] == 256_000
    assert "claude-haiku-5-5" in eff_api["anthropic"]["models"]

    ps = model_pricing.settings({
        "pricing": {
            "overrides": {
                "claude-fable-5-2": {
                    "inputPerMillion": 3.0,
                    "outputPerMillion": 15.0,
                }
            }
        }
    })
    res_override = model_pricing.estimate_cost(
        "claude-fable-5-2",
        input_tokens=1_000_000,
        output_tokens=0,
        pricing_settings=ps,
    )
    assert res_override is not None
    assert _usd(res_override.total_ticks) == Decimal("3.00000000")

    config.update(lambda c: c.update({
        "modelBindings": {
            "defaults": {
                "claude-fable-5-2": {"target": "anthropic/claude-fable-5-2", "source": "auto"},
            },
            "scoped": {},
        }
    }))
    b_dot = model_metadata.resolve_binding("claude-fable-5.2")
    assert b_dot is not None
    assert b_dot.target == "anthropic/claude-fable-5-2"
    assert b_dot.metadata["contextWindow"] == 1_000_000


def test_claude_reset_plans_multi_card_grant_id_binding_and_one_shot_execution(tmp_path, monkeypatch):
    client, headers, _, _, backend = auth_client(tmp_path)
    try:
        claude_id = "claude:multi@example.test"
        backend.accounts.append({
            "_id": claude_id,
            "provider": "claude",
            "type": "claude",
            "email": "multi@example.test",
            "access_token": "claude-multi-at",
            "refresh_token": "claude-multi-rt",
            "enabled": True,
            "disabled_reason": None,
            "disabled_until": None,
            "maxConcurrent": 0,
            "models": ["claude-fable-5-2", "claude-opus-5-5"],
        })
        observed_status = {
            "organization_uuid": "org-multi",
            "generation": "gen-multi-1",
            "cedar_ember": {
                "eligible": True,
                "at_limit": True,
                "exhausted": ["seven_day_overage_included"],
                "next_grant_id": "g_weekly_blocked",
                "weekly_resets_at": "2030-01-07T00:00:00Z",
                "cooldown_until": None,
                "grants": [
                    {
                        "id": "g_weekly_blocked",
                        "label": "周卡",
                        "type": "weekly",
                        "gives_back_weekly": False,
                        "resets_left": 1,
                        "resets_total": 1,
                        "starts_at": "2020-01-01T00:00:00Z",
                        "ends_at": "2030-01-01T00:00:00Z",
                        "paused": False,
                        "usable_now": False,
                        "unavailable_reason": "cannot_clear",
                        "use_requires_limit": True,
                        "clears": ["seven_day"],
                        "blocking": [],
                    },
                    {
                        "id": "g_fable",
                        "label": "Fable 退回卡",
                        "type": "weekly",
                        "gives_back_weekly": True,
                        "resets_left": 2,
                        "resets_total": 3,
                        "starts_at": "2020-01-01T00:00:00Z",
                        "ends_at": "2030-01-01T00:00:00Z",
                        "paused": False,
                        "usable_now": True,
                        "unavailable_reason": None,
                        "use_requires_limit": True,
                        "clears": ["seven_day_overage_included"],
                        "blocking": [],
                    },
                    {
                        "id": "g_session",
                        "label": "5h 卡",
                        "type": "session",
                        "gives_back_weekly": False,
                        "resets_left": 1,
                        "resets_total": 1,
                        "starts_at": "2020-01-01T00:00:00Z",
                        "ends_at": "2030-01-01T00:00:00Z",
                        "paused": False,
                        "usable_now": True,
                        "unavailable_reason": None,
                        "use_requires_limit": True,
                        "clears": ["five_hour"],
                        "blocking": [],
                    },
                ],
            },
        }
        redeemed: list[dict] = []

        async def fake_status(account_id: str, program: str) -> dict:
            assert account_id == claude_id and program == "cedar_ember"
            return json.loads(json.dumps(observed_status))

        async def fake_redeem(account_id: str, program: str, **kwargs) -> dict:
            redeemed.append({"account_id": account_id, "program": program, **kwargs})
            return {
                "result": "reset",
                "program": program,
                "resets_left": 1,
                "cleared": ["seven_day_overage_included"],
                "weekly_resets_at": "2030-01-07T00:00:00Z",
                "quota_action": {"action": "resumed"},
            }

        monkeypatch.setattr(backend, "claude_reset_status", fake_status)
        monkeypatch.setattr(backend, "claude_reset_redeem", fake_redeem)

        default_resp = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/plans",
            {"program": "cedar_ember"}, headers,
        )
        assert default_resp.status_code == 200, default_resp.text
        default_data = default_resp.json()["data"]
        assert default_data["available"] is True
        assert default_data["grantId"] == "g_fable"
        assert default_data["status"]["cedarEmber"]["grants"][1]["givesBackWeekly"] is True

        session_resp = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/plans",
            {"program": "cedar_ember", "grantId": "g_session"}, headers,
        )
        assert session_resp.status_code == 200, session_resp.text
        session_data = session_resp.json()["data"]
        assert session_data["available"] is True
        assert session_data["grantId"] == "g_session"
        assert session_data["planToken"] != default_data["planToken"]

        blocked_resp = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/plans",
            {"program": "cedar_ember", "grantId": "g_weekly_blocked"}, headers,
        )
        assert blocked_resp.status_code == 200
        blocked_data = blocked_resp.json()["data"]
        assert blocked_data["available"] is False
        assert blocked_data["reason"] == "cannot_clear"
        assert blocked_data["planToken"] is None

        bad_fmt = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/plans",
            {"program": "cedar_ember", "grantId": "INVALID UPPER!"}, headers,
        )
        assert bad_fmt.status_code == 422
        bad_prog = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/plans",
            {"program": "juniper_tide", "grantId": "g_fable"}, headers,
        )
        assert bad_prog.status_code == 422
        assert bad_prog.json()["error"]["code"] == "VALIDATION_FAILED"

        exec_session = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/execute",
            {"planToken": session_data["planToken"]}, headers,
        )
        assert exec_session.status_code == 200, exec_session.text
        assert len(redeemed) == 1 and redeemed[0]["grant_id"] == "g_session"

        replay = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/execute",
            {"planToken": session_data["planToken"]}, headers,
        )
        assert replay.status_code == 400
        assert replay.json()["error"]["code"] == "INVALID_OPERATION_STATE"

        exec_fable = request(
            client, "POST", f"/oauth/accounts/{claude_id}/claude-reset/execute",
            {"planToken": default_data["planToken"]}, headers,
        )
        assert exec_fable.status_code == 200, exec_fable.text
        assert len(redeemed) == 2 and redeemed[1]["grant_id"] == "g_fable"
    finally:
        client.__exit__(None, None, None)
