"""CC321 channel-wire contracts. No services, real credentials or live catalogs."""
import asyncio
import copy
import json
from pathlib import Path
import re
import uuid

import pytest

from src import oauth_model_discovery as discovery
from src.channel.oauth_channel import OAuthChannel
from src.tests.test_cc_model_profiles import channel, decoded, fake_credentials
from src.transform import cc_mimicry as cc

FIXTURES = Path(__file__).parent / "fixtures" / "cc321"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth"])
@pytest.mark.parametrize("name", sorted(MANIFEST))
async def test_captured_body_is_resigned_after_final_channel_rewrite(auth, name):
    source = json.loads((FIXTURES / name).read_bytes())
    original = copy.deepcopy(source)
    req = await channel(auth, source["model"]).build_upstream_request(source, source["model"])
    wire = decoded(req)  # validates final serialized outgoing CCH, not raw fixture
    assert wire != source
    assert source == original
    source_billing = cc._generated_billing_block(source)["text"]
    wire_billing = cc._generated_billing_block(wire)["text"]
    assert re.sub(r"cch=[0-9a-f]{5}", "cch=00000", wire_billing) == re.sub(r"cch=[0-9a-f]{5}", "cch=00000", source_billing)
    assert req.headers["x-claude-code-prompt-id"] == cc._billing_prompt_id(wire)
    assert cc.MID_CONVERSATION_SYSTEM_BETA not in req.headers["anthropic-beta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth", "compatible"])
@pytest.mark.parametrize("model,effort", [
    ("claude-fable-5-2", "medium"), ("claude-fable-5.2", "medium"),
    ("claude-haiku-5-5", "medium"), ("claude-haiku-5.5", "medium"),
    ("claude-mythos-5-2", "medium"), ("claude-mythos-5.2", "medium"),
    ("claude-opus-5-5", "medium"), ("claude-sonnet-5-5", "medium"),
    ("claude-opus-4-7", "xhigh"), ("claude-fable-5-1", "high"),
])
async def test_model_default_and_explicit_contract(auth, model, effort):
    base = {"messages": [{"role": "user", "content": "hello"}]}
    ch = channel(auth, model)
    req = await ch.build_upstream_request(base, model)
    wire = decoded(req)
    assert wire["model"] == model
    assert wire["output_config"] == {"effort": effort}
    assert wire["max_tokens"] == (64000 if model in {"claude-opus-4-7", "claude-fable-5-1"} else 128000)
    assert wire["thinking"]["display"] == "omitted"
    assert cc.THINKING_DISPLAY_UPDATES_BETA not in req.headers["anthropic-beta"]
    explicit = {"max_tokens": 2048, "thinking": {"type": "adaptive", "display": "updates"}, "output_config": {"effort": "high"}}
    req = await ch.build_upstream_request({**base, **explicit}, model)
    wire = decoded(req)
    assert all(wire[k] == v for k, v in explicit.items())
    assert cc.THINKING_DISPLAY_UPDATES_BETA in req.headers["anthropic-beta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth", "compatible"])
async def test_bridges_new_model(auth):
    for ingress, data in [("chat", {"messages": [{"role": "user", "content": "hi"}]}), ("responses", {"input": "hi"})]:
        req = await channel(auth, "claude-fable-5-2").build_upstream_request({"model": "claude-fable-5-2", **data}, "claude-fable-5-2", ingress_protocol=ingress)
        wire = decoded(req)
        assert wire["max_tokens"] == 128000
        assert wire["output_config"]["effort"] == "medium"
        assert req.headers["x-claude-code-prompt-id"] == cc._billing_prompt_id(wire)


def tool(name):
    return {"name": name, "input_schema": {"type": "object"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth"])
async def test_aliases_across_add_remove_reorder_and_protocol_fields(auth):
    names = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    ch = channel(auth, "claude-fable-5-2")
    maps = []
    for order in [names, ["new", *names], list(reversed(names)), names[:2]]:
        req = await ch.build_upstream_request({"messages": [{"role": "user", "content": "hi"}], "tools": [tool(n) for n in order]}, "claude-fable-5-2")
        wire = decoded(req)
        maps.append(dict(zip(order, [t["name"] for t in wire["tools"]])))
    for mapping in maps[1:]:
        for name in set(mapping) & set(maps[0]):
            assert mapping[name] == maps[0][name]
    ownership = {"verified": False, "owner": "mcp", "server_url": "https://mcp.invalid"}
    nested = {"type": "tool_use", "name": "alpha", "input": {"name": "alpha"}}
    additions = {"type": "tool_addition", "tool": {"definition": {**tool("alpha"), "ownership_metadata": ownership}}}
    body = {"tools": [{**tool("alpha"), "ownership_metadata": ownership}], "tool_choice": {"type": "tool", "name": "alpha"}, "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "u", "name": "alpha", "input": nested}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u", "content": [{"type": "tool_reference", "tool_name": "alpha"}]}, additions]},
    ]}
    req = await ch.build_upstream_request(body, "claude-fable-5-2")
    wire = decoded(req)
    alias = wire["tools"][0]["name"]
    assert alias == maps[0]["alpha"] == wire["tool_choice"]["name"]
    assert wire["tools"][0]["ownership_metadata"] == ownership
    assert cc.TOOL_PROVENANCE_BETA in req.headers["anthropic-beta"]
    assert wire["messages"][1]["content"][0]["name"] == alias
    assert wire["messages"][1]["content"][0]["input"] == nested
    assert wire["messages"][2]["content"][0]["content"][0]["tool_name"] == alias
    added = wire["messages"][2]["content"][1]
    assert added["tool"]["definition"]["name"] == alias
    assert added["tool"]["definition"]["ownership_metadata"] == ownership
    restored = cc._restore_tool_names_in_obj(added, req.dynamic_tool_map)
    assert restored["tool"]["definition"]["name"] == "alpha"


@pytest.mark.asyncio
async def test_concurrent_retry_identity_history_indices_and_diagnostics():
    model = "claude-fable-5-2"
    ch = channel("oauth", model)
    first = {cc.PARROT_CC_SESSION_ID_KEY: str(uuid.uuid4()), "messages": [{"role": "user", "content": "one"}]}
    continuation = {**first, "messages": [*first["messages"], {"role": "assistant", "id": "msg_one", "content": [{"type": "tool_use", "id": "u", "name": "probe", "input": {}}]}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u", "content": "done"}]}]}
    second = {**first, "messages": [*continuation["messages"], {"role": "assistant", "id": "msg_two", "content": "done"}, {"role": "user", "content": "two"}]}
    other = {**first, cc.PARROT_CC_SESSION_ID_KEY: str(uuid.uuid4())}
    reqs = await asyncio.gather(*(ch.build_upstream_request(b, model) for b in [first, continuation, second, first, other]))
    ids = [r.headers["x-claude-code-prompt-id"] for r in reqs]
    assert ids[0] == ids[1] == ids[3]
    assert len({ids[0], ids[2], ids[4]}) == 3
    for i, req in enumerate(reqs):
        wire = decoded(req)
        assert req.headers["x-claude-code-prompt-id"] == cc._billing_prompt_id(wire)
        billing = cc._generated_billing_block(wire)["text"]
        assert f"cc_turn_index={2 if i == 2 else 1};" in billing
        assert "cc_prompt_index=0;" in billing
    assert decoded(reqs[1])["diagnostics"]["previous_message_id"] == "msg_one"
    assert decoded(reqs[2])["diagnostics"]["previous_message_id"] == "msg_two"
    assert decoded(reqs[4])["diagnostics"]["previous_message_id"] is None
    assert cc._sdk_turn_position([{"role": "assistant", "content": "truncated"}, *first["messages"]]) is None
    explicit = await ch.build_upstream_request({**second, "diagnostics": {"previous_message_id": "msg_explicit"}}, model)
    assert decoded(explicit)["diagnostics"]["previous_message_id"] == "msg_explicit"


@pytest.mark.asyncio
async def test_supplied_tool_context_maps_without_inventing_execution():
    body = {"tools": [tool("alpha")], "messages": [{"role": "user", "content": "hi"}]}
    sid = str(uuid.uuid4())
    cc.capture_cc_headers(body, {"user-agent": cc.CLI_USER_AGENT, "x-claude-code-session-id": sid,
        "x-claude-code-sandy-plover": '[{"name":"alpha","mcp":true}]',
        "x-claude-code-prev-tool-durations": "alpha=123"})
    ch = channel("oauth", "claude-fable-5-2")
    req = await ch.build_upstream_request(body, "claude-fable-5-2")
    alias = decoded(req)["tools"][0]["name"]
    assert json.loads(req.headers["x-claude-code-sandy-plover"])[0]["name"] == alias
    assert req.headers["x-claude-code-prev-tool-durations"] == alias + "=123"
    cc.capture_cc_headers(body, {})  # no trusted ingress context, clear private claims
    req = await ch.build_upstream_request(body, "claude-fable-5-2")
    assert "x-claude-code-sandy-plover" not in req.headers
    assert "x-claude-code-prev-tool-durations" not in req.headers


def test_discovery_identity_directory_routes_and_no_profile_authorization(monkeypatch):
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "claude-fable-5-2", "display_name": "Fable"}], "has_more": False}
    def get(url, **kwargs):
        calls.append((url, kwargs)); return Response()
    monkeypatch.setattr(discovery.network, "get_sync", get)
    result = discovery.discover_claude({"access_token": "synthetic"})
    assert calls[0][1]["headers"]["user-agent"] == cc.CLI_USER_AGENT
    assert result.client_version == cc.CC_VERSION
    empty = OAuthChannel({"email": "local@test.invalid", "models": []})
    assert empty.supports_model("claude-fable-5-2") is None
    allowed = OAuthChannel({"email": "local@test.invalid", "models": result.models})
    assert allowed.supports_model("claude-fable-5-2") == "claude-fable-5-2"
    assert allowed.supports_model("claude-haiku-5-5") is None
    disabled = OAuthChannel({"email": "local@test.invalid", "models": result.models, "disabledModels": result.models})
    assert disabled.supports_model("claude-fable-5-2") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["dynamic", "static", "disabled"])
async def test_downstream_without_cch_and_disabled_identity(monkeypatch, mode):
    monkeypatch.setattr(cc, "load_config", lambda: {"cch_mode": mode, "cch_static_value": "abcde"})
    pid = str(uuid.uuid4())
    billing = f"x-anthropic-billing-header: cc_version=2.1.321.123; cc_entrypoint=cli; cc_prompt_id={pid}; cc_turn_origin=human;"
    req = await channel("oauth", "claude-fable-5-2").build_upstream_request({
        "system": [{"type": "text", "text": billing}],
        "messages": [{"role": "user", "content": "hello"}],
    }, "claude-fable-5-2")
    wire = json.loads(req.body)
    assert req.headers["x-claude-code-prompt-id"] == pid
    assert "cc_turn_origin=human;" in wire["system"][0]["text"]
    if mode == "dynamic":
        assert re.search(r"cch=([0-9a-f]{5})", wire["system"][0]["text"])[1] == cc.compute_cch(wire)
    else:
        assert wire["system"][0]["text"] == billing


@pytest.mark.asyncio
async def test_explicit_omit_updates_and_no_mimicry():
    model = "claude-fable-5-2"
    body = {"messages": [{"role": "user", "content": "hi"}], "thinking": {"type": "adaptive", "display": "updates"}}
    ch = channel("api_key", model)
    ch.omit_thinking = True
    req = await ch.build_upstream_request(body, model)
    assert "thinking" not in decoded(req)
    assert cc.THINKING_DISPLAY_UPDATES_BETA not in req.headers["anthropic-beta"]
    req = await channel("api_key", model, mimicry=False).build_upstream_request(body, model)
    assert "User-Agent" not in req.headers
    assert "x-claude-code-prompt-id" not in req.headers


def test_response_updates_are_existing_thinking_delta_not_rewritten():
    event = {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "Tool_name is plain text", "estimated_tokens": 5}}
    assert cc._restore_tool_names_in_obj(event, {"original": "Tool_name"}) == event


def _identity_boundary_history(cache_variant):
    prompt = {"role": "user", "content": [
        {"type": "text", "text": "first prompt"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "fixture"}},
    ]}
    if cache_variant == "message":
        prompt["cache_control"] = {"type": "ephemeral"}
    elif cache_variant in {"5m", "1h"}:
        prompt["content"][0]["cache_control"] = {"type": "ephemeral", "ttl": cache_variant}
    elif cache_variant == "moved":
        prompt["content"][1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    continuation = [
        {"role": "assistant", "id": "msg_real", "content": [
            {"type": "tool_use", "id": "toolu_one", "name": "probe", "input": {"cache_control": "user data"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_one", "content": "result"},
            {"type": "text", "text": "explanation accompanying the tool result"},
        ]},
    ]
    return prompt, continuation


@pytest.mark.parametrize("variant", ["none", "message", "5m", "1h", "moved"])
def test_context_derivation_ignores_mixed_tool_continuation_and_cache_hints(variant):
    sid = str(uuid.uuid4())
    prompt, continuation = _identity_boundary_history(variant)
    plain, _ = _identity_boundary_history("none")
    first = {cc.PARROT_CC_SESSION_ID_KEY: sid, "messages": [plain]}
    follow = {cc.PARROT_CC_SESSION_ID_KEY: sid, "messages": [prompt, *continuation]}
    before = copy.deepcopy(follow)
    initial = cc.ensure_request_context(first)
    after = cc.ensure_request_context(follow)
    assert initial[cc.PARROT_CC_PROMPT_ID_KEY] == after[cc.PARROT_CC_PROMPT_ID_KEY]
    assert cc._sdk_turn_position(follow["messages"]) == 1
    assert follow == before
    second = {**follow, "messages": [*follow["messages"], {"role": "user", "content": "second prompt"}]}
    assert cc._sdk_turn_position(second["messages"]) == 2
    assert cc.ensure_request_context(second)[cc.PARROT_CC_PROMPT_ID_KEY] != after[cc.PARROT_CC_PROMPT_ID_KEY]
    # A tool-only partial history, even with explanatory text, is not a prompt.
    partial = {cc.PARROT_CC_SESSION_ID_KEY: sid, "messages": continuation[1:]}
    assert cc._sdk_turn_position(partial["messages"]) is None
    assert uuid.UUID(cc.ensure_request_context(partial)[cc.PARROT_CC_PROMPT_ID_KEY]).version == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api_key", "oauth"])
@pytest.mark.parametrize("variant", ["none", "message", "5m", "1h", "moved"])
async def test_channel_mixed_tool_continuation_cache_hints_and_second_prompt(auth, variant):
    model = "claude-fable-5-2"
    sid = str(uuid.uuid4())
    plain, _ = _identity_boundary_history("none")
    prompt, continuation = _identity_boundary_history(variant)
    bodies = [
        {cc.PARROT_CC_SESSION_ID_KEY: sid, "messages": [plain]},
        {cc.PARROT_CC_SESSION_ID_KEY: sid, "messages": [prompt, *continuation]},
        {cc.PARROT_CC_SESSION_ID_KEY: sid, "messages": [prompt, *continuation, {"role": "user", "content": "second prompt"}]},
    ]
    before = copy.deepcopy(bodies)
    ch = channel(auth, model)
    requests = [await ch.build_upstream_request(body, model) for body in bodies]
    ids = [req.headers["x-claude-code-prompt-id"] for req in requests]
    assert ids[0] == ids[1] and ids[2] != ids[1]
    for req, turn in zip(requests, [1, 1, 2]):
        wire = decoded(req)
        assert cc._billing_prompt_id(wire) == req.headers["x-claude-code-prompt-id"]
        assert f"cc_turn_index={turn};" in cc._generated_billing_block(wire)["text"]
    assert bodies == before


def test_prompt_identity_view_does_not_strip_nested_user_payload():
    prompt = {"role": "user", "cache_control": {"type": "ephemeral"}, "content": [
        {"type": "document", "cache_control": {"type": "ephemeral"},
         "source": {"cache_control": "user-owned", "payload": {"cache_control": "nested"}}},
        {"type": "text", "text": '{"cache_control":"literal text"}'},
    ]}
    original = copy.deepcopy(prompt)
    view = cc._prompt_identity_view(prompt)
    assert "cache_control" not in view and "cache_control" not in view["content"][0]
    assert view["content"][0]["source"] == original["content"][0]["source"]
    assert view["content"][1] == original["content"][1]
    assert prompt == original
    changed = copy.deepcopy(prompt)
    changed["content"][0]["source"]["cache_control"] = "different user data"
    assert cc._prompt_identity_view(changed) != view


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", [
    {"model": "<synthetic>"}, {"isVirtual": True}, {"isSynthetic": True}, {"isApiErrorMessage": True},
])
async def test_diagnostics_excludes_exact_synthetic_model_and_flagged_records(excluded):
    body = {cc.PARROT_CC_SESSION_ID_KEY: str(uuid.uuid4()), "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "id": "msg_real", "content": "real"},
        {"role": "assistant", "id": "msg_excluded", "content": "placeholder", **excluded},
        {"role": "user", "content": "next"},
    ]}
    assert cc._history_previous_message_id(body) == "msg_real"
    req = await channel("oauth", "claude-fable-5-2").build_upstream_request(body, "claude-fable-5-2")
    assert decoded(req)["diagnostics"]["previous_message_id"] == "msg_real"
    body["messages"][2] = {"role": "assistant", "id": "msg_provider", "model": "synthetic-virtual-compatible-model", "content": "real provider reply"}
    assert cc._history_previous_message_id(body) == "msg_provider"
