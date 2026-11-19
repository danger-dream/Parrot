from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.antigravity import fixed_thinking
from src.openai.transform.guard import GuardError
from src.tests.test_antigravity_oauth import _import_modules, _setup, _add_ag_account
from src.tests.test_antigravity_quota_scope import _discover
from src.tests.test_antigravity_compatibility import _wire

VARIANTS = [f"{base}-{tier}" for base, tiers in fixed_thinking.FAMILIES.items() for tier in tiers]


def _body(model, ingress, effort=None):
    if ingress == "responses":
        return {"model": model, "input": "hello", "max_output_tokens": 321,
                **({"reasoning": {"effort": effort}} if effort else {})}
    if ingress == "chat":
        return {"model": model, "messages": [{"role": "user", "content": "hello"}],
                "max_completion_tokens": 321, **({"reasoning_effort": effort} if effort else {})}
    return {"model": model, "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 321, **({"output_config": {"effort": effort}} if effort else {})}


async def _catalog_wire(m, monkeypatch, body, ingress, level, max_out=262144):
    om = m["oauth_manager"]
    model = body["model"]
    ak = _add_ag_account(om, models=[model])
    await _discover(m, monkeypatch, ak, {
        "prod": {"models": {model: {"thinkingLevel": 1, "maxOutputTokens": 1000}}},
        "daily": {"models": {model: {"thinkingLevel": level, "maxOutputTokens": max_out}}},
    })
    async def token(*args, **kwargs): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    channel = m["AntigravityOAuthChannel"](om.get_account(ak))
    request = await channel.build_upstream_request(body, model, ingress_protocol=ingress)
    return json.loads(request.body)


@pytest.mark.parametrize("model", VARIANTS)
@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_directory_key_selects_thinking_without_level_conversion(m, monkeypatch, model, ingress):
    _setup(m)
    body = _body(model, ingress, model.rsplit("-", 1)[1])
    before = copy.deepcopy(body)
    tier = model.rsplit("-", 1)[1]
    level = {"low": 1, "medium": 2, "high": 3, "xhigh": 100}[tier]
    wire = await _catalog_wire(m, monkeypatch, body, ingress, level)
    assert wire["model"] == model
    gen = wire["request"]["generationConfig"]
    assert gen["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": 100 if tier == "xhigh" else tier.upper()}
    assert gen["maxOutputTokens"] == 262144
    assert body == before


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_bare_hydrogen_requires_explicit_available_tier(m, monkeypatch, ingress):
    _setup(m)
    m["config"].update(lambda cfg: cfg.update(modelRegistry={"enabled": True}))
    base = "gemini-4-hydrogen"
    wire, _, _ = await _wire(m, monkeypatch, _body(base, ingress, "xhigh"), ingress,
                              models=[base + "-medium", base + "-xhigh"])
    assert wire["model"] == base + "-xhigh"
    assert "thinkingLevel" not in wire["request"]["generationConfig"]["thinkingConfig"]
    for effort, selected in [(None, [base + "-xhigh"]), ("xhigh", [base + "-high"])]:
        with pytest.raises(GuardError):
            fixed_thinking.resolve(base, selected, _body(base, ingress, effort), ingress)


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("model,effort", [
    ("gemini-4-carbon-low", "high"),
    ("gemini-4-carbon-high", "xhigh"),
    ("gemini-4-hydrogen-xhigh", "high"),
    ("gemini-4-neon-xhigh", "high"),
])
def test_fixed_tier_rejects_conflicting_effort(ingress, model, effort):
    body = _body(model, ingress, effort)
    with pytest.raises(GuardError):
        fixed_thinking.resolve(model, [model], body, ingress)
    with pytest.raises(GuardError):
        fixed_thinking.thinking_config(model, body, ingress)


@pytest.mark.parametrize("ingress,fields", [
    ("responses", {"reasoning": {"budget_tokens": 1000}}),
    ("responses", {"reasoning": {"effort": "none"}}),
    ("anthropic", {"thinking": {"type": "enabled", "budget_tokens": 1000}}),
    ("anthropic", {"thinking": {"type": "disabled"}}),
])
def test_no_guessed_budget_or_disabled_thinking(ingress, fields):
    model = "gemini-4-carbon-medium"
    with pytest.raises(GuardError):
        fixed_thinking.thinking_config(model, {**_body(model, ingress), **fields}, ingress)


@pytest.mark.parametrize("ingress", ["responses", "chat"])
@pytest.mark.asyncio
async def test_suppress_summary_keeps_fixed_tier(m, monkeypatch, ingress):
    _setup(m)
    model = "gemini-4-hydrogen-xhigh"
    body = _body(model, ingress)
    if ingress == "responses":
        body["reasoning"] = {"summary": "none"}
    else:
        body["reasoning_summary"] = "none"
    wire, _, _ = await _wire(m, monkeypatch, body, ingress)
    assert wire["model"] == model
    assert wire["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": False}


@pytest.mark.parametrize("sample", json.loads(
    (Path(__file__).parent / "fixtures/antigravity_hub_2321_generation.json").read_text(),
)["samples"], ids=lambda sample: sample["model"])
@pytest.mark.asyncio
async def test_directory_defaults_match_official_construction(m, monkeypatch, sample):
    _setup(m)
    model = sample["model"]
    wire = await _catalog_wire(m, monkeypatch, {"model": model, "input": "hello"}, "responses",
                               sample["catalog"]["thinkingLevel"], sample["catalog"]["maxOutputTokens"])
    assert wire["request"]["generationConfig"] == sample["generationConfig"]


@pytest.mark.asyncio
async def test_changed_catalog_parameters_abort_prepared_request(m, monkeypatch):
    _setup(m)
    from src.antigravity import DispatchGuardError
    om, ag = m["oauth_manager"], m["ag"]
    model = "gemini-4-hydrogen-xhigh"
    ak = _add_ag_account(om, models=[model], account_model_catalog={
        "antigravityGenerationBaseUrl": ag.daily_api_base_url(),
        "models": [{"id": model, "antigravitySources": [{"baseUrl": ag.daily_api_base_url(), "thinkingLevel": 100}]}],
    })
    channel = m["AntigravityOAuthChannel"](om.get_account(ak))
    async def token(*args, **kwargs):
        m["config"].update(lambda c: c["oauthAccounts"][0]["account_model_catalog"]["models"][0]["antigravitySources"][0].update(thinkingLevel=3))
        return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    with pytest.raises(DispatchGuardError, match="metadata changed"):
        await channel.build_upstream_request({"model": model, "input": "hello"}, model)


def test_unknown_xhigh_mapping_is_not_silently_downgraded(m):
    with pytest.raises(GuardError, match="no verified xhigh"):
        m["codec"].responses_to_gemini({"model": "gemini-unknown", "input": "hello", "reasoning": {"effort": "xhigh"}})
    claude = m["codec"].responses_to_gemini({"model": "claude-opus-5-5-high", "input": "hello", "reasoning": {"effort": "xhigh"}})
    assert claude["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 32768


@pytest.mark.parametrize("base,tiers", [
    ("gemini-4-hydrogen-tiered", ("high", "xhigh")),
    ("gemini-4-krypton", ("low", "medium", "high", "xhigh")),
])
@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_catalog_driven_dynamic_family_resolves_xhigh_across_ingresses(m, monkeypatch, base, tiers, ingress):
    _setup(m)
    m["config"].update(lambda cfg: cfg.update(modelRegistry={"enabled": True}))
    om = m["oauth_manager"]
    models = [f"{base}-{tier}" for tier in tiers]
    level_map = {"low": 1, "medium": 2, "high": 3, "xhigh": 100}
    ak = _add_ag_account(om, models=models)
    await _discover(m, monkeypatch, ak, {
        "prod": {"models": {f"{base}-{tier}": {"thinkingLevel": 1, "maxOutputTokens": 1000} for tier in tiers}},
        "daily": {"models": {f"{base}-{tier}": {"thinkingLevel": level_map[tier], "maxOutputTokens": 262144} for tier in tiers}},
    })
    async def token(*args, **kwargs): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    channel = m["AntigravityOAuthChannel"](om.get_account(ak))

    target = f"{base}-xhigh"
    bare_body = _body(base, ingress, "xhigh")
    assert channel.resolve_request_model(base, bare_body, ingress) == target
    req = await channel.build_upstream_request(bare_body, target, ingress_protocol=ingress)
    wire = json.loads(req.body)
    assert wire["model"] == target
    assert wire["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": 100}
    assert wire["request"]["generationConfig"]["maxOutputTokens"] == 262144

    conflict = _body(target, ingress, "high")
    with pytest.raises(GuardError, match="conflicts") as exc:
        await channel.build_upstream_request(conflict, target, ingress_protocol=ingress)
    assert exc.value.code == "antigravity_thinking_unsupported"

    m["config"].update(lambda cfg: cfg.update(modelRegistry={"enabled": False}))
    assert channel.resolve_request_model(base, bare_body, ingress) is None
    exact_req = await channel.build_upstream_request(_body(target, ingress), target, ingress_protocol=ingress)
    exact_wire = json.loads(exact_req.body)
    assert exact_wire["model"] == target
    assert exact_wire["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": 100}


@pytest.mark.asyncio
async def test_catalog_driven_dynamic_family_respects_claude_argon_and_non_variant_boundaries(m, monkeypatch):
    _setup(m)
    om = m["oauth_manager"]
    models = ["claude-opus-5-5-high", "gemini-3-flash", "gemini-4-argon-high", "gemini-4-neon-xhigh"]
    ak = _add_ag_account(om, models=models)
    await _discover(m, monkeypatch, ak, {
        "prod": {"models": {}},
        "daily": {"models": {
            "claude-opus-5-5-high": {"thinkingLevel": 3, "maxOutputTokens": 128000},
            "gemini-3-flash": {"thinkingLevel": 3, "maxOutputTokens": 65536},
            "gemini-4-argon-high": {"thinkingLevel": 3, "maxOutputTokens": 65536},
            "gemini-4-neon-xhigh": {"thinkingLevel": 100, "maxOutputTokens": 262144},
        }},
    })
    async def token(*args, **kwargs): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    account = om.get_account(ak)
    catalog_models = m["ag"].generation_endpoint_models(account)
    discovered = fixed_thinking.catalog_families(catalog_models)
    assert discovered == {"gemini-4-neon": ("xhigh",)}

    channel = m["AntigravityOAuthChannel"](account)
    claude_req = await channel.build_upstream_request(
        _body("claude-opus-5-5-high", "responses", "xhigh"), "claude-opus-5-5-high", ingress_protocol="responses",
    )
    claude_thinking = json.loads(claude_req.body)["request"]["generationConfig"]["thinkingConfig"]
    assert claude_thinking == {"thinkingBudget": 32768}

    flash_req = await channel.build_upstream_request(
        _body("gemini-3-flash", "responses", "low"), "gemini-3-flash", ingress_protocol="responses",
    )
    flash_thinking = json.loads(flash_req.body)["request"]["generationConfig"]["thinkingConfig"]
    assert flash_thinking == {"thinkingLevel": "low", "includeThoughts": True}


@pytest.mark.asyncio
async def test_known_family_extends_tiers_only_from_active_endpoint_catalog(m, monkeypatch):
    _setup(m)
    m["config"].update(lambda cfg: cfg.update(modelRegistry={"enabled": True}))
    om = m["oauth_manager"]
    ak = _add_ag_account(om, models=["gemini-4-carbon-high", "gemini-4-carbon-xhigh"])
    await _discover(m, monkeypatch, ak, {
        "prod": {"models": {"gemini-4-carbon-xhigh": {"thinkingLevel": 100, "maxOutputTokens": 262144}}},
        "daily": {"models": {"gemini-4-carbon-high": {"thinkingLevel": 3, "maxOutputTokens": 262144}}},
    })
    channel = m["AntigravityOAuthChannel"](om.get_account(ak))
    with pytest.raises(GuardError, match="low/medium/high"):
        channel.resolve_request_model("gemini-4-carbon", _body("gemini-4-carbon", "responses", "xhigh"), "responses")

    await _discover(m, monkeypatch, ak, {
        "prod": {"models": {}},
        "daily": {"models": {
            "gemini-4-carbon-high": {"thinkingLevel": 3, "maxOutputTokens": 262144},
            "gemini-4-carbon-xhigh": {"thinkingLevel": 100, "maxOutputTokens": 262144},
        }},
    })
    async def token(*args, **kwargs): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    channel = m["AntigravityOAuthChannel"](om.get_account(ak))
    body = _body("gemini-4-carbon", "responses", "xhigh")
    assert channel.resolve_request_model("gemini-4-carbon", body, "responses") == "gemini-4-carbon-xhigh"
    req = await channel.build_upstream_request(body, "gemini-4-carbon-xhigh", ingress_protocol="responses")
    wire = json.loads(req.body)
    assert wire["request"]["generationConfig"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": 100}
