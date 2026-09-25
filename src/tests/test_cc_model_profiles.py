"""Production-channel regressions for the independent v280 alignment review.

All auth/network boundaries are fake. Bodies are the actual channel output,
not an assertion that a profile/helper exists or that an account is entitled.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from src import oauth_manager
from src.channel.api_channel import ApiChannel
from src.channel.oauth_channel import OAuthChannel
from src.transform import cc_mimicry as cc


@pytest.fixture(autouse=True)
def fake_credentials(monkeypatch):
    async def token(channel):
        return "fake-claude-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    monkeypatch.setattr(cc, "load_config", lambda: {"cch_mode": "dynamic"})


def channel(auth, model, *, mimicry=True):
    if auth == "oauth":
        return OAuthChannel({"email": "profile@example.test", "models": [model]})
    return ApiChannel({
        "name": "profile-" + auth,
        "baseUrl": "https://api.anthropic.com" if auth == "api_key" else "https://relay.invalid",
        "apiKey": "fake-api-key", "cc_mimicry": mimicry,
        "models": [{"real": model, "alias": model}],
    })


def decoded(request):
    wire = json.loads(request.body)
    assert not any(key.startswith("_parrot_") for key in wire)
    billing = wire["system"][0]["text"]
    assert re.search(r"cch=([0-9a-f]{5});", billing)[1] == cc.compute_cch(wire)
    return wire


# Defaults from v280's catalog plus Jyr/vCt/L_ and Haiku wire capture.
@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth", "compatible"])
@pytest.mark.parametrize("model,maximum,thinking,effort", [
    ("claude-haiku-4-5", 32000, "enabled", False),
    ("claude-haiku-4-5-20251001", 32000, "enabled", False),
    ("claude-3-5-haiku-20241022", 8192, None, False),
    ("claude-3-5-sonnet-20241022", 8192, None, False),
    ("claude-3-7-sonnet-20250219", 32000, None, False),
    ("claude-sonnet-4-20250514", 32000, "enabled", False),
    ("claude-sonnet-4-5-20250929", 32000, "enabled", False),
    ("claude-sonnet-4-6", 32000, "adaptive", True),
    ("claude-opus-4-1-20250805", 32000, "enabled", False),
    ("claude-opus-4-5", 32000, "enabled", True),
    ("claude-opus-4-8", 64000, "adaptive", True),
    ("claude-opus-5", 64000, "adaptive", True),
    ("claude-fable-5.1", 64000, "adaptive", True),
    ("claude-fable-5-1", 64000, "adaptive", True),
    ("GLM-5", 4096, None, False),
    ("claude-haiku-9", 4096, None, False),
])
async def test_channel_default_profiles(auth, model, maximum, thinking, effort):
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}]}
    before = copy.deepcopy(body)
    req = await channel(auth, model).build_upstream_request(body, model)
    wire = decoded(req)
    assert wire["model"] == model  # Lookup must never rename the wire model.
    assert wire["max_tokens"] == maximum
    assert wire.get("thinking", {}).get("type") == thinking
    if thinking == "enabled":
        assert wire["thinking"]["budget_tokens"] == maximum - 1
    assert ("effort" in wire.get("output_config", {})) is effort
    assert "fallbacks" not in wire
    assert cc.SERVER_SIDE_FALLBACK_BETA not in req.headers["anthropic-beta"]
    assert cc.FALLBACK_CREDIT_BETA not in req.headers["anthropic-beta"]
    assert cc.ADVISOR_TOOL_BETA not in req.headers["anthropic-beta"]
    assert "anthropic-dispatch-id" not in req.headers
    if cc.canonical_model(model) == "claude-haiku-4-5":
        assert req.headers["x-claude-code-request-class"] == "main"
        assert req.headers["anthropic-beta"].split(",") == [
            cc.INTERLEAVED_THINKING_BETA, cc.THINKING_TOKEN_COUNT_BETA,
            cc.CONTEXT_MANAGEMENT_BETA, cc.PROMPT_CACHING_SCOPE_BETA,
            "claude-code-20250219",
            *([cc.OAUTH_BETA] if auth == "oauth" else []),
            cc.ADVANCED_TOOL_USE_BETA, cc.THINKING_BINDING_CONTROLS_BETA,
            cc.CACHE_DIAGNOSIS_BETA,
        ]
    if not effort:
        assert cc.EFFORT_BETA not in req.headers["anthropic-beta"]
    if thinking is None:
        assert "context_management" not in wire and "diagnostics" not in wire
    assert body == before
    if auth == "api_key":
        assert req.headers["x-api-key"] == "fake-api-key"
        assert "Authorization" not in req.headers
    else:
        assert req.headers["Authorization"].startswith("Bearer ")
    assert (cc.OAUTH_BETA in req.headers["anthropic-beta"]) is (auth == "oauth")


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth", "compatible"])
@pytest.mark.parametrize("model,kind", [("claude-haiku-4-5", "enabled"), ("claude-opus-5", "adaptive"), ("GLM-5", None)])
@pytest.mark.parametrize("ingress", ["chat", "responses"])
async def test_bridge_defaults_and_explicit_controls(auth, model, kind, ingress):
    base = {"model": model, "stream": False}
    if ingress == "chat":
        base["messages"] = [{"role": "user", "content": "hello"}]
    else:
        base["input"] = "hello"
    req = await channel(auth, model).build_upstream_request(base, model, ingress_protocol=ingress)
    wire = decoded(req)
    assert wire.get("thinking", {}).get("type") == kind
    assert "fallbacks" not in wire
    explicit = {**base, "temperature": 0.25, "top_p": 0.8,
                "max_tokens" if ingress == "chat" else "max_output_tokens": 100}
    if ingress == "chat":
        explicit["stop"] = ["done"]
    req = await channel(auth, model).build_upstream_request(explicit, model, ingress_protocol=ingress)
    wire = decoded(req)
    assert wire["max_tokens"] == 100
    assert wire["temperature"] == 0.25 and wire["top_p"] == 0.8
    assert "thinking" not in wire
    if ingress == "chat":
        assert wire["stop_sequences"] == ["done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth", "compatible"])
@pytest.mark.parametrize("fallback", ["default", [], [{"model": "claude-opus-5"}]])
async def test_explicit_fallback_is_preserved_and_beta_is_opt_in(auth, fallback):
    model = "claude-fable-5.1"
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}], "fallbacks": fallback}
    req = await channel(auth, model).build_upstream_request(body, model)
    wire = decoded(req)
    assert wire["fallbacks"] == fallback
    assert cc.SERVER_SIDE_FALLBACK_BETA in req.headers["anthropic-beta"]
    assert cc.FALLBACK_CREDIT_BETA in req.headers["anthropic-beta"]
    betas = req.headers["anthropic-beta"].split(",")
    start = betas.index(cc.SERVER_SIDE_FALLBACK_BETA)
    assert betas[start:start + 3] == [
        cc.SERVER_SIDE_FALLBACK_BETA, cc.FALLBACK_CREDIT_BETA,
        cc.THINKING_BINDING_CONTROLS_BETA,
    ]
    without = {k: v for k, v in wire.items() if k != "fallbacks"}
    assert cc.cch_hash_view(without) == cc.cch_hash_view(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth", "compatible"])
@pytest.mark.parametrize("model,thinking,output", [
    ("claude-haiku-4-5", {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}, {}),
    ("claude-opus-5", {"type": "disabled"}, {"effort": "low"}),
    ("claude-fable-5.1", {"type": "adaptive", "display": "summarized"}, {"effort": "max"}),
    ("GLM-5", {"type": "enabled", "budget_tokens": 2048}, {"effort": "medium"}),
])
async def test_explicit_fields_are_not_replaced_by_defaults(auth, model, thinking, output):
    explicit = {"thinking": thinking, "output_config": output, "max_tokens": 8192,
                "context_management": {"edits": []}, "stop_sequences": ["END"], "stream": False}
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}], **explicit}
    req = await channel(auth, model).build_upstream_request(body, model)
    wire = decoded(req)
    for key, value in explicit.items():
        assert wire[key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth"])
@pytest.mark.parametrize("control", [
    {"max_tokens": 1024}, {"temperature": 0.5}, {"top_p": 0.8}, {"top_k": 10},
    {"tool_choice": {"type": "tool", "name": "lookup"},
     "tools": [{"name": "lookup", "input_schema": {"type": "object"}}]},
])
async def test_haiku_implicit_thinking_cannot_invalidate_explicit_controls(auth, control):
    model = "claude-haiku-4-5"
    req = await channel(auth, model).build_upstream_request({
        "model": model, "messages": [{"role": "user", "content": "hi"}], **control,
    }, model)
    wire = decoded(req)
    assert "thinking" not in wire and "output_config" not in wire
    for key in ("max_tokens", "temperature", "top_p", "top_k"):
        if key in control:
            assert wire[key] == control[key]


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth", "compatible"])
@pytest.mark.parametrize("text,system,side_query", [
    ("<session>title</session>", "context", True),
    ("ordinary prompt", "<session>system-only</session>", False),
])
async def test_haiku_classification_survives_system_injection(auth, text, system, side_query):
    model = "claude-haiku-4-5-20251001"
    body = {
        "model": "haiku-alias", "messages": [{"role": "user", "content": text}],
        "system": system, "output_config": {},
    }
    before = copy.deepcopy(body)
    req = await channel(auth, model).build_upstream_request(body, model)
    wire = decoded(req)
    assert body == before
    assert wire["model"] == model
    assert wire["output_config"] == {}
    assert wire["thinking"]["type"] == ("disabled" if side_query else "enabled")
    assert ("cc_prompt_id=" in wire["system"][0]["text"]) is not side_query
    assert ("x-claude-code-request-class" not in req.headers) is side_query
    common = [cc.INTERLEAVED_THINKING_BETA, cc.THINKING_TOKEN_COUNT_BETA,
              cc.CONTEXT_MANAGEMENT_BETA, cc.PROMPT_CACHING_SCOPE_BETA]
    if side_query:
        expected = ([cc.OAUTH_BETA] if auth == "oauth" else []) + common + [
            cc.STRUCTURED_OUTPUTS_BETA, cc.CACHE_DIAGNOSIS_BETA,
        ]
    else:
        expected = common + ["claude-code-20250219"] + (
            [cc.OAUTH_BETA] if auth == "oauth" else []
        ) + [cc.ADVANCED_TOOL_USE_BETA, cc.THINKING_BINDING_CONTROLS_BETA,
             cc.CACHE_DIAGNOSIS_BETA]
    assert req.headers["anthropic-beta"].split(",") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_fallback", [False, True])
async def test_haiku_omit_thinking_preserves_remaining_betas_and_explicit_fallback(explicit_fallback):
    model = "claude-haiku-4-5"
    ch = channel("compatible", model)
    ch.omit_thinking = True
    body = {"model": model, "messages": [{"role": "user", "content": "hello"}]}
    if explicit_fallback:
        body["fallbacks"] = []
    req = await ch.build_upstream_request(body, model)
    wire = decoded(req)
    assert "thinking" not in wire and "context_management" not in wire
    if explicit_fallback:
        assert wire["fallbacks"] == []
    else:
        assert "fallbacks" not in wire
    betas = req.headers["anthropic-beta"].split(",")
    assert not any("thinking" in beta for beta in betas)
    assert cc.ADVANCED_TOOL_USE_BETA in betas and cc.CACHE_DIAGNOSIS_BETA in betas
    assert (cc.SERVER_SIDE_FALLBACK_BETA in betas) is explicit_fallback
    assert (cc.FALLBACK_CREDIT_BETA in betas) is explicit_fallback
    assert cc.OAUTH_BETA not in betas and cc.ADVISOR_TOOL_BETA not in betas
    assert req.headers["x-claude-code-request-class"] == "main"
    assert "anthropic-dispatch-id" not in req.headers
    assert req.headers["Authorization"] == "Bearer fake-api-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-fable-5.1", "GLM-5"])
async def test_non_cc_channels_keep_standard_defaults(model):
    req = await channel("api_key", model, mimicry=False).build_upstream_request({
        "model": model, "messages": [{"role": "user", "content": "hi"}],
    }, model)
    wire = json.loads(req.body)
    assert wire["max_tokens"] == 4096
    assert "thinking" not in wire and "output_config" not in wire and "fallbacks" not in wire
    assert "User-Agent" not in req.headers
