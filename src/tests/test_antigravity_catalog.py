from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.antigravity import DispatchGuardError
from src.oauth_model_discovery import _antigravity_is_text
from src.tests.test_antigravity_oauth import _add_ag_account, _setup, _import_modules
from src.tests.test_antigravity_quota_scope import _discover

MODEL = "gemini-4-carbon-medium"
ENUM = "MODEL_PLACEHOLDER_M437"


@pytest.mark.parametrize("record,accepted", [
    ({"id": MODEL, "disabled": True}, False),
    ({"id": "gemini-4-carbon-disabled"}, False),
    ({"id": MODEL, "disabled": False}, True),
    ({"id": MODEL}, True),
    ({"id": "gemini-4-hydrogen-xhigh"}, True),
])
def test_disabled_catalog_entries_are_not_requestable(record, accepted):
    assert _antigravity_is_text(record, set()) is accepted


@pytest.mark.parametrize("active", ["prod", "daily"])
@pytest.mark.parametrize("missing_enum", [False, True])
@pytest.mark.asyncio
async def test_enum_roundtrip_uses_only_generation_endpoint(m, monkeypatch, active, missing_enum):
    _setup(m)
    ag, om = m["ag"], m["oauth_manager"]
    base = ag.api_base_url() if active == "prod" else ag.daily_api_base_url()
    ak = _add_ag_account(om, request_base_url=base)
    payloads = {
        "prod": {"models": {MODEL: {"model": "MODEL_PLACEHOLDER_M100"}}},
        "daily": {"models": {MODEL: {"model": ENUM}}},
    }
    expected = payloads[active]["models"][MODEL]["model"]
    if missing_enum:
        payloads[active]["models"][MODEL].pop("model")
        expected = ""
    assert (await _discover(m, monkeypatch, ak, payloads))["action"] == "updated"
    saved = om.get_account(ak)
    assert ag.model_enum_at_generation_endpoint(saved, MODEL) == expected
    record = saved["account_model_catalog"]["models"][0]
    assert record.get("modelEnum", "") == expected
    disk = json.loads(Path(m["config"].CONFIG_PATH).read_text())["oauthAccounts"][0]
    assert disk["account_model_catalog"] == saved["account_model_catalog"]
    changed_route = copy.deepcopy(saved)
    changed_route["request_base_url"] = "https://other.example"
    assert not ag.model_at_generation_endpoint(changed_route, MODEL)
    assert ag.model_enum_at_generation_endpoint(changed_route, MODEL) == ""
    other_account = copy.deepcopy(saved)
    other_account["account_model_catalog"]["models"][0]["antigravitySources"] = [
        {"baseUrl": base, "modelEnum": "MODEL_PLACEHOLDER_M999"},
    ]
    assert ag.model_enum_at_generation_endpoint(other_account, MODEL) == "MODEL_PLACEHOLDER_M999"
    assert ag.model_enum_at_generation_endpoint(saved, MODEL) == expected


@pytest.mark.parametrize("active", ["prod", "daily"])
@pytest.mark.asyncio
async def test_active_disabled_overrides_available_side_and_keeps_preferences(m, monkeypatch, active):
    _setup(m)
    ag, om = m["ag"], m["oauth_manager"]
    base = ag.api_base_url() if active == "prod" else ag.daily_api_base_url()
    ak = _add_ag_account(om, models=[MODEL], request_base_url=base, disabledModels=["manual-choice"])
    old_channel = m["AntigravityOAuthChannel"](om.get_account(ak))
    payloads = {endpoint: {"models": {MODEL: {"model": ENUM}}} for endpoint in ("prod", "daily")}
    payloads[active]["models"][MODEL]["disabled"] = True
    payloads[active]["models"]["gemini-4-argon-disabled"] = {}
    assert (await _discover(m, monkeypatch, ak, payloads))["action"] == "updated"
    saved = om.get_account(ak)
    assert saved["models"] == []
    assert saved["disabledModels"] == ["manual-choice"]
    assert not ag.model_at_generation_endpoint(saved, MODEL)
    assert ag.model_enum_at_generation_endpoint(saved, MODEL) == ""
    assert not om._model_sync_due(saved)
    selection = om.account_model_selection(saved)
    assert selection["effective_models"] == []
    assert selection["source"] == "upstream:antigravity"
    assert selection["records"][0]["disabled"] is True
    assert m["AntigravityOAuthChannel"](saved).supports_model(MODEL) is None
    with pytest.raises(DispatchGuardError):
        old_channel.validate_dispatch(MODEL, (MODEL,))


@pytest.mark.asyncio
async def test_successful_empty_catalog_revokes_stale_models_without_erasing_manual_state(m, monkeypatch):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=[MODEL], disabledModels=[MODEL])
    assert (await _discover(m, monkeypatch, ak, {
        "prod": {"models": {MODEL: {"model": ENUM}}}, "daily": {"models": {}},
    }))["action"] == "updated"
    saved = om.get_account(ak)
    assert saved["models"] == [] and saved["disabledModels"] == [MODEL]
    assert not saved["last_model_sync_error"]
    assert not om._model_sync_due(saved)
    assert not m["ag"].model_at_generation_endpoint(saved, MODEL)


@pytest.mark.asyncio
async def test_disabled_side_does_not_override_enabled_active(m, monkeypatch):
    _setup(m)
    om, ag = m["oauth_manager"], m["ag"]
    ak = _add_ag_account(om)
    assert (await _discover(m, monkeypatch, ak, {
        "prod": {"models": {MODEL: {"disabled": True, "model": "MODEL_PLACEHOLDER_M1"}}},
        "daily": {"models": {MODEL: {"disabled": False, "model": ENUM}}},
    }))["action"] == "updated"
    saved = om.get_account(ak)
    assert saved["models"] == [MODEL]
    assert ag.model_at_generation_endpoint(saved, MODEL)
    assert ag.model_enum_at_generation_endpoint(saved, MODEL) == ENUM


@pytest.mark.parametrize("model,record", [
    ("gemini-4-carbon-disabled", {}),
    (MODEL, {"disabled": True}),
    (MODEL, {"antigravitySources": [{"baseUrl": "https://daily-cloudcode-pa.googleapis.com", "disabled": True}]}),
])
def test_old_catalog_cannot_resurrect_disabled_model(m, model, record):
    _setup(m)
    om, ag = m["oauth_manager"], m["ag"]
    ak = _add_ag_account(om, models=[model], account_model_catalog={
        "models": [{"id": model, **record}],
        "antigravityGenerationBaseUrl": ag.daily_api_base_url(),
    })
    account = om.get_account(ak)
    assert om.account_model_selection(account)["effective_models"] == []
    assert m["AntigravityOAuthChannel"](account).supports_model(model) is None


@pytest.mark.asyncio
async def test_disabled_during_token_await_is_rechecked_before_send(m, monkeypatch):
    _setup(m)
    om, cfg = m["oauth_manager"], m["config"]
    ak = _add_ag_account(om, models=[MODEL])
    channel = m["AntigravityOAuthChannel"](om.get_account(ak))
    async def token(*args, **kwargs):
        cfg.update(lambda c: c["oauthAccounts"][0].update(account_model_catalog={
            "models": [{"id": MODEL, "disabled": True}],
        }))
        return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    with pytest.raises(DispatchGuardError):
        await channel.build_upstream_request({"model": MODEL, "input": "hello"}, MODEL)


def test_manual_and_legacy_models_do_not_guess_enums(m):
    _setup(m)
    om, ag = m["oauth_manager"], m["ag"]
    ak = _add_ag_account(om, models=[MODEL])
    account = om.get_account(ak)
    assert ag.model_at_generation_endpoint(account, MODEL)
    assert ag.model_enum_at_generation_endpoint(account, MODEL) == ""
    assert om._model_sync_due(account)
