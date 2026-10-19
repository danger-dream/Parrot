"""Provider-owned aliases through real routing/HTTP/SSE, fixture upstreams only."""
from __future__ import annotations

import copy
import gzip
import json
import uuid

import httpx
import pytest
from starlette.requests import Request

from src import auth, config, model_metadata, model_names, model_pricing, oauth_manager as om
from src.channel import registry
from src.channel.cursor_oauth_channel import CursorOAuthChannel
from src.channel.workbuddy_oauth_channel import WorkBuddyOAuthChannel
from src.cursor_bridge import catalog as cursor_catalog, runtime as cursor_runtime
from src.cursor_bridge.models import CursorModel
from src.management_control.oauth import OAuthControl
from src.management_control.oauth.models import PageSpec
from src.oauth.workbuddy import auth as wb_auth
from src.protocols.model_alias import ChatModelAliasStream, chat_response
from src.telegram import ui
from src.telegram.menus import oauth_account_models_menu as models_menu, oauth_menu
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_workbuddy_channel import ev, frames, request_body
from src.tests.test_workbuddy_lifecycle import context


def account(provider, realm="cn"):
    ids = ["auto", "default", "shared-fixture"]
    if provider == "workbuddy":
        entry = wb_auth.normalize_credential({"realm": realm, "uid": uuid.uuid4().hex,
            "access_token": "fixture-access", "refresh_token": "fixture-refresh",
            "workbuddy_client_profile": "ide" if realm == "global" else "cli"})
        entry.update(models=ids, account_model_catalog={"schema": 1, "models": [
            {"id": name, "name": name.title(), "contextWindow": 100000} for name in ids]})
        return entry
    catalog = cursor_catalog.build_catalog([CursorModel(id=name, name=name.title(),
        reasoning=False, supports_images=False,
        context_window=100000, context_window_max_mode=1000000, max_tokens=32000,
        supports_max_mode=True, supports_agent=True, legacy_slugs=()) for name in ids])
    return {"provider": "cursor", "type": "cursor", "email": "fixture@local", "subject": uuid.uuid4().hex,
        "access_token": "fixture-access", "refresh_token": "fixture-refresh", "expired": "2099-01-01T00:00:00Z",
        "enabled": True, "models": ids, "cursor_model_catalog": catalog}


def channel(entry):
    return (CursorOAuthChannel if entry["provider"] == "cursor" else WorkBuddyOAuthChannel)(entry)


@pytest.fixture
def env(monkeypatch):
    m = fake._import_modules()
    before = copy.deepcopy(config.get())
    fake._setup(m)
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    monkeypatch.setattr(cursor_runtime, "base_url", lambda: "http://cursor-alias.fixture")
    monkeypatch.setattr(cursor_runtime, "bearer_secret", lambda: "fixture-bridge")
    monkeypatch.setattr(cursor_runtime, "update_account", lambda *a: None)
    async def token(ch):
        assert om.get_account(ch.account_key)["access_token"] == "fixture-access"
        return "fixture-access"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    config.update(lambda c: c.update(oauthAccounts=[], channels=[], modelBindings={"defaults": {}, "scoped": {}},
        network={"routing": {"default": "direct"}}, protocolBridge={"enabled": True},
        modelMapping={}, timeouts={"connect": 2, "firstByte": 2, "idle": 2, "total": 5}))
    fake._install_keys(m, fake._default_key())
    yield m
    config.update(lambda c: (c.clear(), c.update(before)))
    fake._install_channels(m, [])


def install(env, *entries):
    config.update(lambda c: c.update(oauthAccounts=list(entries)))
    channels = [channel(entry) for entry in entries]
    fake._install_channels(env, channels)
    return channels


def objects(text):
    result = []
    for line in text.splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            result.append(json.loads(line[5:]))
    return result


@pytest.mark.parametrize("provider,realm", [("workbuddy", "cn"), ("workbuddy", "global"), ("cursor", "cn")])
@pytest.mark.parametrize("raw", ["auto", "default"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_alias_six_inference_paths_keep_raw_upstream_and_public_response(env, monkeypatch, provider, realm, raw, legacy, ingress, stream):
    entry = account(provider, realm)
    ch, = install(env, entry)
    public = f"{provider}-{raw}"
    body = request_body(ingress, stream, tool=True)
    body["model"] = raw if legacy else public
    snapshot = copy.deepcopy(entry)
    router = fake.MockRouter()
    def wire(req):
        encoding = req.headers.get("content-encoding", "")
        # CN CLI and the retained Global IDE profile use gzip on the real wire.
        assert encoding == ("gzip" if provider == "workbuddy" else "")
        payload = json.loads(gzip.decompress(req.content) if encoding == "gzip" else req.content)
        assert payload["model"] == raw
        assert "reasoning_effort" not in payload
        if provider == "workbuddy":
            assert req.url.host == ("www.codebuddy.ai" if realm == "global" else "copilot.tencent.com")
            assert req.headers["authorization"] == "Bearer fixture-access"
            if realm == "global":
                assert req.headers["x-ide-name"] == "CodeBuddyIDE"
        else:
            assert req.url.host == "cursor-alias.fixture"
        if payload["stream"]:
            return httpx.Response(200, stream=fake.ChunkedByteStream(frames(tool=True)), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "chatcmpl-alias", "object": "chat.completion", "created": 1700000000,
            "model": raw, "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant",
                "content": None, "tool_calls": [{"id": "call-fixture", "type": "function", "function": {
                    "name": "fixture_tool", "arguments": '{"city":"昆明"}'}}]}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}})
    router.register(ch.base_url, wire)
    # Cursor deliberately creates a separate direct client for its local bridge.
    # Supply fixture transports to that client too; keep the real loopback path.
    original_client = httpx.AsyncClient
    def fixture_client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(router.handle))
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", fixture_client)
    if ingress == "anthropic":
        response, client, route = await fake._call_anthropic_core(env, router, body)
        assert route.candidates[0][0].key == ch.key
    else:
        response, client = await fake._call_openai_handler(env, router, ingress, body)
    try:
        text = await fake._consume_streaming_to_string(response) if hasattr(response, "body_iterator") else response.body.decode()
    finally:
        await client.aclose()
    assert response.status_code == 200, text
    assert len(router.requests) == 1
    assert response.headers["content-type"].startswith("text/event-stream" if stream else "application/json")
    if stream:
        chunks = objects(text)
        models = [obj["model"] for obj in chunks if "model" in obj]
        models += [obj[key]["model"] for obj in chunks for key in ("message", "response")
                   if isinstance(obj.get(key), dict) and "model" in obj[key]]
        assert models and set(models) == {public}, text
        assert {"chat": "[DONE]", "responses": "response.completed", "anthropic": "message_stop"}[ingress] in text
    else:
        assert json.loads(text)["model"] == public
    assert "fixture_tool" in text and "call-fixture" in text
    assert om.get_account(ch.account_key) == snapshot
    row = env["log_db"]._get_conn().execute("SELECT final_model FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["final_model"] == raw  # Historical/upstream storage keys are not migrated.


async def test_public_directory_routing_and_legacy_permissions_are_provider_scoped(env):
    import server
    wb, cursor = install(env, account("workbuddy"), account("cursor"))
    names = registry.available_models()
    assert names == ["cursor-auto", "cursor-default", "shared-fixture", "workbuddy-auto", "workbuddy-default"]
    for ch, other in [(wb, cursor), (cursor, wb)]:
        for raw in ("auto", "default"):
            public = f"{ch.provider}-{raw}"
            assert ch.supports_model(raw) == ch.supports_model(public) == raw
            assert other.supports_model(public) is None
            route = env["scheduler"].schedule({"model": public, "messages": [{"role": "user", "content": "hi"}]}, api_key_name="k", client_ip="127.0.0.1", ingress_protocol="chat")
            assert route and all(candidate[0].provider == ch.provider for candidate in route.candidates)
            metadata = model_metadata.get_metadata(public, scope_key=ch.key, outbound_model=raw)
            assert metadata["contextWindow"] == 100000
    request = Request({"type": "http", "method": "GET", "path": "/v1/models", "query_string": b"",
        "headers": [(b"authorization", b"Bearer ccp-test")], "client": ("127.0.0.1", 1234)})
    assert [item["id"] for item in (await server.list_models(request))["data"]] == names
    for grant, expected in [("default", ["cursor-default", "workbuddy-default"]), ("cursor-default", ["cursor-default"])]:
        config.update(lambda c: c["apiKeys"]["k"].update(allowedModels=[grant]))
        assert [item["id"] for item in (await server.list_models(request))["data"]] == expected
        _, allowed, error = auth.validate({"authorization": "Bearer ccp-test"})
        assert error is None
        assert ("default" in allowed) is (grant == "default")
        assert config.get()["apiKeys"]["k"]["allowedModels"] == [grant]


@pytest.mark.parametrize("provider", ["workbuddy", "cursor"])
def test_management_and_tg_use_public_names_but_store_original_ids(env, monkeypatch, provider):
    entry = account(provider)
    ch, = install(env, entry)
    ctl = OAuthControl()
    ctx = context()
    monkeypatch.setattr(models_menu, "oauth_control", ctl)
    monkeypatch.setattr(oauth_menu, "oauth_control", ctl)
    page = ctl.list_models(ctx, ch.account_key, page=PageSpec())
    assert [item.model_id for item in page.items] == [f"{provider}-auto", f"{provider}-default", "shared-fixture"]
    assert page.items[0].name == f"{provider}-auto"
    ctl.update_models(ctx, ch.account_key, model_ids=[f"{provider}-auto"], disabled=True)
    saved = om.get_account(ch.account_key)
    field = "cursor_disabled_models" if provider == "cursor" else "disabledModels"
    assert saved[field] == ["auto"] and saved["models"] == entry["models"]
    fresh = channel(saved)
    assert fresh.supports_model("auto") is None and fresh.supports_model(f"{provider}-auto") is None
    ctl.update_models(ctx, ch.account_key, model_ids=["auto"], disabled=False)
    if provider == "cursor":
        ctl.update_model_settings(ctx, ch.account_key, model_id="cursor-default", max_context_default=True)
        assert om.cursor_max_context_default(om.get_account(ch.account_key), "default")
    text, _ = models_menu.render(ch.account_key)
    assert f"<code>{provider}-auto</code>" in text and "<code>auto</code>" not in text
    text, _ = models_menu._detail_render(ch.account_key, "auto", model_page=1, account_page=1, filter_key="all")
    assert f"<code>{provider}-auto</code>" in text and "<code>auto</code>" not in text
    stats = dict(total=1, success_count=1, error_count=0, input=12, output=4, cache_creation=0, cache_read=0,
        cost_ticks=0, costed_success=0, unpriced_success=1, final_model="auto")
    monkeypatch.setattr(oauth_menu, "_account_period_stats", lambda *a, **k: stats)
    text = oauth_menu._format_month_stats_block(ch.account_key, by_model=[stats])
    assert f"<code>{provider}-auto</code>" in text and stats["final_model"] == "auto"
    if provider == "cursor":
        rendered = []
        monkeypatch.setattr(ui, "edit", lambda chat, message, text, **kw: rendered.append(text))
        monkeypatch.setattr(ui, "answer_cb", lambda *a, **kw: None)
        short = ui.register_code(ch.account_key)
        oauth_menu.on_cursor_models(42, 900, "cb", f"{short}:1")
        ref = oauth_menu._cursor_model_ref(ch.account_key, "auto")
        oauth_menu.on_cursor_model_detail(42, 900, "cb", f"{ref}:1")
        bulk, _ = oauth_menu._cursor_disable_text_and_kb({"account_key": ch.account_key,
            "models": ["auto", "default"], "selected": []})
        for text in [*rendered, bulk]:
            assert "<code>cursor-auto</code>" in text and "<code>auto</code>" not in text


@pytest.mark.parametrize("provider", ["workbuddy", "cursor"])
@pytest.mark.parametrize("scoped", [False, True])
def test_existing_raw_model_bindings_keep_working_without_migration(env, provider, scoped):
    ch, = install(env, account(provider))
    public = f"{provider}-auto"
    model_metadata.set_binding("auto", "openai/gpt-5.4", scope_key=ch.key if scoped else None, outbound_model="auto", source="test")
    snapshot = copy.deepcopy(config.get()["modelBindings"])
    old = model_metadata.resolve_binding("auto", scope_key=ch.key, outbound_model="auto")
    new = model_metadata.resolve_binding(public, scope_key=ch.key, outbound_model="auto")
    assert new.target == old.target == "openai/gpt-5.4"
    assert new.client_visible_model == public and new.metadata == old.metadata
    priced = model_pricing.build_pricing_binding(channel_key=ch.key, channel_type="oauth",
        upstream_protocol="openai-chat", outbound_model_id="auto", client_visible_model=public)
    assert priced.pricing_key == "openai/gpt-5.4" and priced.tariff is not None
    assert config.get()["modelBindings"] == snapshot
    assert model_metadata.resolve_binding(public, scope_key="api:unrelated", outbound_model="auto") is None


async def test_exclusive_public_grant_does_not_authorize_legacy_or_other_provider(env):
    install(env, account("workbuddy"), account("cursor"))
    config.update(lambda c: c["apiKeys"]["k"].update(allowedModels=["cursor-default"]))
    for model in ("default", "workbuddy-default"):
        body = request_body("chat", False)
        body["model"] = model
        router = fake.MockRouter()
        response, client = await fake._call_openai_handler(env, router, "chat", body)
        await client.aclose()
        assert response.status_code == 403
        assert router.requests == []


def test_same_protocol_rewriter_preserves_content_errors_and_incomplete_tail():
    content = {"choices": [{"delta": {"content": "auto/default should stay literal"}}], "model": "auto"}
    error = {"error": {"message": "raw error", "model": "auto"}}
    assert chat_response(error, "workbuddy-auto") is error
    wire = b": heartbeat\r\n\r\n" + ev(content) + ev(error) + b"data: [DONE]\n\n" + b"data: unfinished"
    transformer = ChatModelAliasStream("workbuddy-auto")
    output = b"".join(chunk for index in range(0, len(wire), 7) for chunk in transformer.feed(wire[index:index+7]))
    output += b"".join(transformer.close())
    assert output.endswith(b"data: unfinished") and b"auto/default should stay literal" in output
    assert b'"model":"workbuddy-auto"' in output and b"[DONE]" in output
    assert b'"message": "raw error", "model": "auto"' in output


def test_alias_module_does_not_change_other_providers_or_model_names():
    for provider, model in [("openai", "auto"), ("anthropic", "default"), ("cursor", "gpt-auto"), ("workbuddy", "DEFAULT")]:
        assert model_names.public_id(provider, model) == model
        assert model_names.response_context(provider, model, None, ingress="chat") is None
    assert model_names.upstream_id("workbuddy", "cursor-default") == "cursor-default"
