"""Discovery -> persisted account -> selection -> HTTP/WS regressions (offline)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src import config, oauth_manager, oauth_model_discovery, state_db
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.openai import codex_constants as constants, codex_identity
from src.openai.transform.guard import GuardError
from src.openai.responses_ws_runtime import build_oauth_responses_ws_frame, map_ws_create_frame_for_upstream
from src.state_store import StateStore

KEY = "openai:models@example.test:workspace-test"
VERSION = "0.157.0-alpha.10"


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"),
                       manifest_path=str(tmp_path / "manifest.json"))
    store.start()
    monkeypatch.setattr(state_db, "_store", store)
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    Path(config.CONFIG_PATH).write_text(json.dumps({
        "oauth": {"mockMode": True}, "channels": [], "oauthAccounts": [{
            "provider": "openai", "email": "models@example.test", "workspace_id": "workspace-test",
            "chatgpt_account_id": "workspace-test", "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh", "expired": "2999-01-01T00:00:00Z",
            "enabled": True, "models": [],
        }],
    }))
    config.get()
    async def token(*args, **kwargs): return "synthetic-access"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    wire = {"status": 200, "models": [], "calls": []}
    class Response:
        headers = {"etag": "review-etag"}
        @property
        def status_code(self): return wire["status"]
        def raise_for_status(self): pass
        def json(self): return {"models": copy.deepcopy(wire["models"])}
    def get(url, **kwargs):
        wire["calls"].append((url, kwargs))
        return Response()
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync", get)
    codex_identity.clear_turn_mappings_for_tests()
    yield wire
    codex_identity.clear_turn_mappings_for_tests()
    store.close()


async def sync(env, records):
    env["models"] = records
    result = await oauth_manager.refresh_account_models(KEY)
    assert result["action"] == "updated", result
    # Exercise disk round-trip, not an already-normalized handcrafted record.
    config.reload()
    saved = oauth_manager.get_account(KEY)
    assert saved["account_model_catalog"] == json.loads(Path(config.CONFIG_PATH).read_text())["oauthAccounts"][0]["account_model_catalog"]
    return OpenAIOAuthChannel(saved)


def record(model="gpt-6-sol", **fields):
    return {"slug": model, "visibility": "list", "use_responses_lite": True, **fields}


async def request(channel, model, body=None, transport="http"):
    req = await channel.build_upstream_request(
        {"model": model, "input": "hello", **(body or {})}, model,
        ingress_protocol="responses", responses_transport=transport,
    )
    payload = json.loads(req.body)
    if transport == "websocket":
        payload = build_oauth_responses_ws_frame(payload, model, channel=channel)
    return req, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_hidden_models_are_addressable_not_picker_entries(env, monkeypatch, transport):
    channel = await sync(env, [record(), record("hidden-account-model", visibility="hide")])
    account = oauth_manager.get_account(KEY)
    assert account["models"] == ["gpt-6-sol", "hidden-account-model"]
    assert channel.supports_model("hidden-account-model") == "hidden-account-model"
    assert channel.supports_model("gpt-6-luna") is None  # static profile is not permission
    assert channel.list_client_models() == ["gpt-6-sol"]
    selection = oauth_manager.account_model_selection(account)
    assert selection["models"] == ["gpt-6-sol"]
    assert [item["id"] for item in selection["records"]] == ["gpt-6-sol"]
    from src.management_control.models import ModelCenterControl, ModelSourceType
    from src.management_control.models.upstream_sync import UpstreamSync, _Source
    control = ModelCenterControl()
    preview = UpstreamSync(control)._source_model_names(_Source(ModelSourceType.OAUTH, KEY, "test", ""))
    assert preview == ["gpt-6-sol"]
    from src import scheduler
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: [channel])
    available, _, _, _ = scheduler._filter_candidates("hidden-account-model", "responses", {"input": "hello"})
    assert available == [(channel, "hidden-account-model")]
    _, payload = await request(channel, "hidden-account-model", transport=transport)
    assert payload["model"] == "hidden-account-model"
    account["disabledModels"] = ["hidden-account-model"]
    assert OpenAIOAuthChannel(account).supports_model("hidden-account-model") is None


@pytest.mark.asyncio
async def test_hidden_only_catalog_is_a_valid_authenticated_catalog(env):
    channel = await sync(env, [record("only-hidden", visibility="hide")])
    assert channel.supports_model("only-hidden") == "only-hidden"
    assert channel.list_client_models() == []
    assert oauth_manager.account_model_selection(KEY)["models"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("instructions", ["  Account-owned\n literal instructions.\n", ""])
@pytest.mark.parametrize("lite", [False, True])
async def test_remote_instructions_survive_discovery_disk_and_wire(env, transport, instructions, lite):
    channel = await sync(env, [record(use_responses_lite=lite, model_messages={"instructions_template": instructions})])
    config.update(lambda cfg: cfg["openaiOAuth"].update(defaultInstructions="fallback must not override empty"))
    _, payload = await request(channel, "gpt-6-sol", transport=transport)
    developers = [item for item in payload["input"] if item.get("type") == "message" and item.get("role") == "developer"]
    if lite:
        assert ([item["content"][0]["text"] for item in developers]) == ([instructions] if instructions else [])
    else:
        assert payload.get("instructions", "") == instructions
    _, explicit = await request(channel, "gpt-6-sol", {"instructions": "request wins"}, transport)
    assert (explicit["input"][1]["content"][0]["text"] if lite else explicit["instructions"]) == "request wins"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_null_defaults_empty_levels_and_legacy_instructions(env, transport):
    channel = await sync(env, [record("gpt-6-astra", default_reasoning_level=None,
        default_verbosity=None, multi_agent_reasoning_effort=None, supported_reasoning_levels=[],
        model_messages={"instructions_template": None}, base_instructions="legacy remote")])
    policy = channel.codex_model_policy("gpt-6-astra")
    assert (policy.default_reasoning_effort, policy.default_verbosity, policy.multi_agent_reasoning_effort, policy.reasoning_efforts) == (None, None, None, ())
    _, payload = await request(channel, "gpt-6-astra", transport=transport)
    assert payload["input"][1]["content"][0]["text"] == "legacy remote"
    assert payload["reasoning"] == {"context": "all_turns"}
    assert "text" not in payload
    # Explicit caller values must not be replaced, nor max mapped via stale metadata.
    _, explicit = await request(channel, "gpt-6-astra", {"reasoning": {"effort": "max"}, "text": {"verbosity": "high"}}, transport)
    assert explicit["reasoning"]["effort"] == "max"
    assert explicit["text"]["verbosity"] == "high"


@pytest.mark.asyncio
async def test_null_remote_instructions_do_not_revive_profile(env):
    channel = await sync(env, [record(model_messages={"instructions_template": None})])
    assert "baseInstructions" in oauth_manager.get_account(KEY)["account_model_catalog"]["models"][0]
    assert channel.codex_model_policy("gpt-6-sol").base_instructions is None
    _, payload = await request(channel, "gpt-6-sol")
    assert len(payload["input"]) == 2  # empty tools + user, no bundled developer


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("choice,wire_choice,names", [
    ("none", "none", []), ("required", "required", ["lookup", "write"]),
    ({"type": "function", "name": "lookup"}, "required", ["lookup"]),
    ("auto", "auto", ["lookup", "write"]),
])
async def test_lite_tool_constraints_are_not_broadened(env, transport, choice, wire_choice, names):
    channel = await sync(env, [record()])
    tools = [{"type": "function", "name": name, "parameters": {"type": "object", "properties": {}}} for name in ["lookup", "write"]]
    _, payload = await request(channel, "gpt-6-sol", {"tools": tools, "tool_choice": choice}, transport)
    assert payload["tool_choice"] == wire_choice
    assert [tool["name"] for tool in payload["input"][0]["tools"]] == names
    assert payload["parallel_tool_calls"] is False
    assert "tools" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,names", [("none", []), ({"type": "function", "name": "lookup"}, ["lookup"])])
async def test_existing_lite_prefix_and_ws_delta_constraints(env, choice, names):
    channel = await sync(env, [record()])
    tools = [{"type": "function", "name": name, "parameters": {"type": "object"}} for name in ["lookup", "write"]]
    _, prefixed = await request(channel, "gpt-6-sol", {"instructions": "caller", "tools": tools}, "websocket")
    original_prefix_id = prefixed["input"][0]["id"]
    prefixed["tool_choice"] = choice
    constrained = map_ws_create_frame_for_upstream(prefixed, "gpt-6-sol", channel=channel)
    assert constrained["input"][0]["id"] != original_prefix_id
    assert [tool["name"] for tool in constrained["input"][0]["tools"]] == names
    assert constrained["input"][1] == prefixed["input"][1]
    delta = {"type": "response.create", "previous_response_id": "resp-prev", "model": "gpt-6-sol",
             "prompt_cache_key": prefixed["prompt_cache_key"], "input": [], "tools": tools, "tool_choice": choice, "generate": False}
    if isinstance(choice, dict):
        with pytest.raises(GuardError, match="requires a full request"):
            map_ws_create_frame_for_upstream(delta, "gpt-6-sol", channel=channel)
        return
    out = map_ws_create_frame_for_upstream(delta, "gpt-6-sol", channel=channel)
    assert out["previous_response_id"] == "resp-prev" and out["generate"] is False
    assert out["tool_choice"] == "none"
    assert out["input"] == []  # no synthetic context/default instructions in delta


@pytest.mark.asyncio
async def test_named_ws_delta_without_definitions_is_not_silently_auto(env):
    channel = await sync(env, [record()])
    with pytest.raises(GuardError, match="requires a full request") as caught:
        map_ws_create_frame_for_upstream({"type": "response.create", "model": "gpt-6-sol", "input": [],
            "previous_response_id": "resp-prev", "tool_choice": {"type": "function", "name": "lookup"}}, "gpt-6-sol", channel=channel)
    assert (caught.value.status, caught.value.param, caught.value.scope) == (400, "tool_choice", "candidate")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("tiers,status", [
    (None, "permitted"), ([], "permitted"),
    ([{"id": "priority", "name": "Fast"}], "permitted"),
    ([{"id": "flex", "name": "Flex"}], "advertised"),
])
async def test_flex_and_existing_compatibility_policy(env, transport, tiers, status):
    fields = {"service_tiers": tiers} if tiers is not None else {}
    channel = await sync(env, [record(**fields)])
    original_catalog = copy.deepcopy(oauth_manager.get_account(KEY)["account_model_catalog"])
    assert channel.service_tier_catalog_status("gpt-6-sol", "flex") == status
    _, payload = await request(channel, "gpt-6-sol", {"service_tier": "flex", "max_output_tokens": 1, "temperature": 0}, transport)
    assert payload["service_tier"] == "flex"
    assert "max_output_tokens" not in payload and "temperature" not in payload
    assert payload["reasoning"]["effort"] == "medium"  # missing defaults use profile
    if tiers is not None:
        with pytest.raises(GuardError, match="does not advertise"):
            await request(channel, "gpt-6-sol", {"service_tier": "unsupported"}, transport)
        assert original_catalog["models"][0]["serviceTiers"] == tiers
    else:
        assert channel.service_tier_catalog_status("gpt-6-sol", "unsupported") == "unknown"
        assert "serviceTiers" not in original_catalog["models"][0]
    assert oauth_manager.get_account(KEY)["account_model_catalog"] == original_catalog
    assert json.loads(Path(config.CONFIG_PATH).read_text())["oauthAccounts"][0]["account_model_catalog"] == original_catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", [False, True])
async def test_whole_query_full_identity_cache_and_semver_are_separate(env, pin):
    version = "0.153.4" if pin else VERSION
    if pin:
        config.update(lambda cfg: cfg["openaiOAuth"].update(codexProfileAutoUpdate=False,
            codexCliVersion=version, codexProtocolProfile=f"rust-v{version}"))
    channel = await sync(env, [record(minimal_client_version="0.155.0")])
    url, kwargs = env["calls"][-1]
    assert url.endswith("client_version=" + ("0.153.4" if pin else "0.157.0"))
    assert kwargs["headers"]["version"] == version
    assert kwargs["headers"]["user-agent"].startswith(f"codex_cli_rs/{version} ")
    assert not oauth_manager._model_sync_due(oauth_manager.get_account(KEY))
    env["status"] = 304
    assert (await oauth_manager.refresh_account_models(KEY))["action"] == "not_modified"
    assert env["calls"][-1][1]["headers"]["If-None-Match"] == "review-etag"
    # Same release, old parser/query cache: force a full 200 to recover hidden
    # entries, instructions and nulls rather than retaining an incomplete LKG.
    config.update(lambda cfg: cfg["oauthAccounts"][0]["account_model_catalog"].update(schema=1))
    assert oauth_manager._model_sync_due(oauth_manager.get_account(KEY))
    env["status"] = 200
    await sync(env, [record(minimal_client_version="0.155.0")])
    assert "If-None-Match" not in env["calls"][-1][1]["headers"]
    if pin:
        with pytest.raises(Exception, match="below model"):
            await request(channel, "gpt-6-sol")
    else:
        assert (await request(channel, "gpt-6-sol"))[1]["model"] == "gpt-6-sol"
        assert constants.codex_version_meets_minimum(VERSION, "0.157.0") is False
        channel = await sync(env, [record(minimal_client_version="0.157.0")])
        with pytest.raises(GuardError, match="below model"):
            await request(channel, "gpt-6-sol")


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", [False, True])
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_default_instructions_remains_profile_first_fallback(env, pin, transport):
    if pin:
        config.update(lambda cfg: cfg["openaiOAuth"].update(codexProfileAutoUpdate=False,
            codexCliVersion="0.153.4", codexProtocolProfile="rust-v0.153.4"))
    config.update(lambda cfg: cfg["openaiOAuth"].update(defaultInstructions="deployment fallback"))
    channel = await sync(env, [record("gpt-5.5", use_responses_lite=False)])
    _, payload = await request(channel, "gpt-5.5", transport=transport)
    expected = "deployment fallback" if pin else constants.codex_protocol_profile().model_policy("gpt-5.5").base_instructions
    assert payload["instructions"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_caller_mcp_attribution_is_preserved_not_invented(env, transport):
    channel = await sync(env, [record()])
    attribution = '{"status":"caller_owned"}'
    _, payload = await request(channel, "gpt-6-sol", {"client_metadata": {"mcp_attribution": attribution}}, transport)
    assert payload["client_metadata"]["mcp_attribution"] == attribution
    _, plain = await request(channel, "gpt-6-sol", transport=transport)
    assert "mcp_attribution" not in plain["client_metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["none", "required"])
async def test_ws_delta_string_constraint_needs_no_synthetic_prefix(env, choice):
    channel = await sync(env, [record()])
    delta = {"type": "response.create", "model": "gpt-6-sol", "previous_response_id": "resp-prev",
             "input": [{"type": "function_call_output", "call_id": "call-1", "output": "ok"}],
             "tool_choice": choice}
    out = map_ws_create_frame_for_upstream(delta, "gpt-6-sol", channel=channel)
    assert out["tool_choice"] == choice
    assert out["input"] == delta["input"]


@pytest.mark.asyncio
async def test_named_function_and_unknown_tool_are_not_relaxed(env):
    channel = await sync(env, [record()])
    with pytest.raises(GuardError, match="matching explicit tool"):
        await request(channel, "gpt-6-sol", {"tool_choice": {"type": "function", "name": "missing"}})
    with pytest.raises(GuardError, match="needs available tools"):
        await request(channel, "gpt-6-sol", {"tool_choice": "required"})
    _, payload = await request(channel, "gpt-6-sol", {
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    })
    assert payload["tool_choice"] == "required"
    assert payload["input"][0]["tools"][0]["name"] == "lookup"
