from __future__ import annotations

import copy
import json

import pytest

from src.antigravity import generation_policy
from src.tests.test_antigravity_oauth import _import_modules, _setup, _add_ag_account
from src.tests.test_antigravity_fixed_thinking import _catalog_wire


@pytest.fixture(autouse=True)
def clean(m):
    _setup(m)


def body_for(ingress, model, limit=32768, system="You are OpenBear.\nKeep this text unchanged.\n"):
    if ingress == "responses":
        body = {"model": model, "instructions": system, "input": "original user", "reasoning": {"effort": "medium"}}
        if limit is not None:
            body["max_output_tokens"] = limit
    elif ingress == "chat":
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": "original user"}], "reasoning_effort": "medium"}
        if limit is not None:
            body["max_completion_tokens"] = limit
    else:
        body = {"model": model, "system": system, "messages": [{"role": "user", "content": "original user"}], "output_config": {"effort": "medium"}}
        if limit is not None:
            body["max_tokens"] = limit
    return body


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("model,maximum", [
    ("gemini-4-carbon-medium", 262144),
    ("gemini-4-hydrogen-medium", 262144),
    ("gemini-4-argon-medium", 65536),
    ("claude-opus-5-5-medium", 128000),
])
@pytest.mark.parametrize("limit", [None, 32768, 999999])
@pytest.mark.asyncio
async def test_text_channel_forces_catalog_limit_and_preserves_original_system(m, monkeypatch, ingress, model, maximum, limit):
    system = "You are OpenBear.\nKeep this text unchanged.\n"
    body = body_for(ingress, model, limit, system)
    before = copy.deepcopy(body)
    wire = await _catalog_wire(m, monkeypatch, body, ingress, 2, maximum)
    request = wire["request"]
    assert request["generationConfig"]["maxOutputTokens"] == maximum
    assert request["systemInstruction"]["parts"][0]["text"] == generation_policy.SHORT_IDENTITY + "\n\n" + system
    assert request["contents"] == [{"role": "user", "parts": [{"text": "original user"}]}]
    assert body == before


@pytest.mark.parametrize("value", [None, 0, -1, True, "262144", 262144.0])
def test_invalid_or_missing_limit_uses_official_default(value):
    assert generation_policy.max_output_tokens({"maxOutputTokens": value}) == 16384
    assert generation_policy.max_output_tokens({}) == 16384


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_missing_active_endpoint_limit_does_not_use_side_endpoint(m, monkeypatch, ingress):
    wire = await _catalog_wire(m, monkeypatch, body_for(ingress, "gemini-4-carbon-medium", 999999), ingress, 2, None)
    assert wire["request"]["generationConfig"]["maxOutputTokens"] == 16384


@pytest.mark.asyncio
async def test_output_limit_is_account_scoped(m, monkeypatch):
    om, ag = m["oauth_manager"], m["ag"]
    async def token(*args, **kwargs): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    model = "gemini-4-carbon-medium"
    for email, limit in [("first@example.test", 123456), ("second@example.test", 65432)]:
        base = ag.daily_api_base_url()
        account = _add_ag_account(om, email=email, models=[model], account_model_catalog={
            "antigravityGenerationBaseUrl": base,
            "models": [{"id": model, "antigravitySources": [{"baseUrl": base, "thinkingLevel": 2, "maxOutputTokens": limit}]}],
        })
        ch = m["AntigravityOAuthChannel"](om.get_account(account))
        request = await ch.build_upstream_request(body_for("responses", model), model)
        assert json.loads(request.body)["request"]["generationConfig"]["maxOutputTokens"] == limit


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_claude_thinking_uses_forced_limit_not_client_limit(m, monkeypatch, ingress):
    body = body_for(ingress, "claude-opus-5-5-high", 100)
    if ingress == "responses":
        body["reasoning"] = {"effort": "high"}
    elif ingress == "chat":
        body["reasoning_effort"] = "high"
    else:
        body["output_config"] = {"effort": "high"}
    wire = await _catalog_wire(m, monkeypatch, body, ingress, 3, 128000)
    gen = wire["request"]["generationConfig"]
    assert gen["maxOutputTokens"] == 128000
    assert gen["thinkingConfig"]["thinkingBudget"] == 16384


@pytest.mark.asyncio
async def test_missing_catalog_keeps_claude_budget_below_fallback(m, monkeypatch):
    from src.tests.test_antigravity_compatibility import _wire
    body = {"model": "claude-opus-5-5-high", "input": "hi", "reasoning": {"effort": "xhigh"}, "max_output_tokens": 262144}
    wire, _, _ = await _wire(m, monkeypatch, body)
    gen = wire["request"]["generationConfig"]
    assert gen["maxOutputTokens"] == 16384
    assert gen["thinkingConfig"]["thinkingBudget"] == 16383


@pytest.mark.parametrize("system", ["", generation_policy.SHORT_IDENTITY, generation_policy.SHORT_IDENTITY + "\n\nOriginal system"])
@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_identity_is_present_once_for_empty_or_prefixed_system(m, monkeypatch, ingress, system):
    body = body_for(ingress, "gemini-4-carbon-medium", system=system)
    wire = await _catalog_wire(m, monkeypatch, body, ingress, 2)
    text = wire["request"]["systemInstruction"]["parts"][0]["text"]
    assert text == (system or generation_policy.SHORT_IDENTITY)
    assert text.count(generation_policy.SHORT_IDENTITY) == 1


def test_identity_preserves_multiple_parts_and_is_idempotent():
    original = {"systemInstruction": {"role": "system", "parts": [{"text": " First\n"}, {"text": "Second\r\n"}, {"text": "Third"}]}}
    request = copy.deepcopy(original)
    generation_policy.prepend_identity(request)
    assert request["systemInstruction"]["parts"][0]["text"] == generation_policy.SHORT_IDENTITY + "\n\n First\n"
    assert request["systemInstruction"]["parts"][1:] == original["systemInstruction"]["parts"][1:]
    assert request["systemInstruction"]["role"] == "system"
    first = copy.deepcopy(request)
    generation_policy.prepend_identity(request)
    assert request == first


@pytest.mark.asyncio
async def test_responses_system_and_developer_order_tools_and_retry(m, monkeypatch):
    from src.tests.test_antigravity_compatibility import _wire
    body = body_for("responses", "gemini-4-carbon-medium", system="First")
    body["input"] = [{"role": "developer", "content": "Second"}, {"role": "system", "content": "Third"}, {"role": "user", "content": "Task"}]
    body["tools"] = [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}]
    before = copy.deepcopy(body)
    wire, _, ch = await _wire(m, monkeypatch, body)
    second = json.loads((await ch.build_upstream_request(body, body["model"])).body)
    request = wire["request"]
    assert request["systemInstruction"]["parts"] == [{"text": generation_policy.SHORT_IDENTITY + "\n\nFirst"}, {"text": "Second"}, {"text": "Third"}]
    assert second["request"]["systemInstruction"] == request["systemInstruction"]
    assert request["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == body["tools"][0]["parameters"]
    assert body == before


def test_image_model_remains_unroutable_on_text_entry(m):
    model = "gemini-3.1-flash-image"
    om = m["oauth_manager"]
    account = _add_ag_account(om, models=[model])
    channel = m["AntigravityOAuthChannel"](om.get_account(account))
    assert channel.supports_model(model) is None
    assert channel.resolve_request_model(model, {"model": model, "input": "blue"}, "responses") is None


def test_pure_converter_does_not_change_other_provider_callers(m):
    body = {"model": "gemini-4-carbon-medium", "instructions": "Original", "input": "hi", "max_output_tokens": 42}
    converted = m["codec"].responses_to_gemini(body)
    assert converted["systemInstruction"]["parts"] == [{"text": "Original"}]
    assert converted["generationConfig"]["maxOutputTokens"] == 42
