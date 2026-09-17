"""Search Control/API/TG behavior. Run only via isolated_pytest.py (no network)."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src import config, search_service
from src.management_auth import AuthMethod, Capability, ManagementPrincipal
from src.management_control import BoundedAuditSink, ManagementContext, ManagementError
from src.management_control.search import SearchControl
from src.management_api.routers.search import router
from src.tests.test_management_api_foundation import bearer, build_app, create_session
from src.telegram import states, ui
from src.telegram.menus import search_menu


class MemoryConfig:
    def __init__(self):
        self.value = {"search": {}, "anysearch": {}, "oauthAccounts": [], "unrelated": {"keep": True}}
        self.updates = 0
        self.lock = threading.RLock()

    def get(self):
        return self.value

    def update(self, mutate):
        with self.lock:
            candidate = copy.deepcopy(self.value)
            mutate(candidate)
            self.value = candidate
            self.updates += 1
            return self.value


@pytest.fixture
def memory(monkeypatch):
    cfg = MemoryConfig()
    monkeypatch.setattr(config, "get", cfg.get)
    monkeypatch.setattr(config, "update", cfg.update)
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    return cfg


@pytest.fixture
def ctx():
    return ManagementContext(request_id="search-test", actor=ManagementPrincipal.administrator(
        subject_id="tester", auth_method=AuthMethod.MANAGEMENT_KEY))


@pytest.fixture
def control():
    return SearchControl(audit_sink=BoundedAuditSink())


def row(value, backend_id):
    return next(v for v in value["backends"] if v["id"] == backend_id)


def assert_no_keys(value, *secrets):
    rendered = json.dumps(value, default=str)
    assert '"apiKeys"' not in rendered
    assert '"access_token"' not in rendered
    for secret in secrets:
        assert secret not in rendered


def test_defaults_read_only_legacy_fallback_and_secret_preserving_patch(memory, control, ctx):
    memory.value["anysearch"] = {"apiKey": "legacy-search-secret", "enabled": False, "maxResults": 6,
                                 "endpoint": "https://api.anysearch.com/mcp"}
    original = copy.deepcopy(memory.value)
    public = control.get(ctx)
    assert public["functionMode"] == public["hostedMode"] == "passthrough"
    assert public["maxAttempts"] == 3 and public["timeoutSeconds"] == 10
    assert public["maxResults"] == 6
    assert row(public, "anysearch")["keyCount"] == 1
    assert row(public, "anthropic")["verified"] is False
    assert memory.value == original and memory.updates == 0
    result = control.patch(ctx, {"functionMode": "managed", "language": "zh", "country": "CN", "freshness": "week"})
    assert result["hostedMode"] == "passthrough"
    assert search_service.settings()["backends"][0]["apiKeys"] == ["legacy-search-secret"]
    assert memory.value["anysearch"] == original["anysearch"]
    assert memory.value["unrelated"] == original["unrelated"]
    control.patch_backend(ctx, "tavily", {"apiKeys": ["key-one", "key-two"]})
    control.patch(ctx, {"hostedMode": "disabled", "maxToolRounds": 70, "maxFetchChars": 100000})
    assert search_service.settings()["backends"][1]["apiKeys"] == ["key-one", "key-two"]
    assert_no_keys(control.get(ctx), "legacy-search-secret", "key-one", "key-two")
    assert_no_keys(control._audit_sink.snapshot(), "legacy-search-secret", "key-one", "key-two")


def test_multi_key_write_only_cas_and_priority_preserve_identity(memory, control, ctx):
    before = control.get(ctx)
    public = control.patch_backend(ctx, "tavily", {"apiKeys": ["alpha", "beta", "alpha"]}, expected_revision=before["revision"])
    assert row(public, "tavily")["keyCount"] == 2
    with pytest.raises(ManagementError, match="REVISION_CONFLICT"):
        control.patch_backend(ctx, "tavily", {"apiKeys": []}, expected_revision=before["revision"])
    control.patch_backend(ctx, "tavily", {"addApiKeys": ["gamma"]})
    control.patch_backend(ctx, "tavily", {"removeKeyIndices": [1]})
    assert search_service.settings()["backends"][1]["apiKeys"] == ["alpha", "gamma"]
    snapshot = copy.deepcopy(search_service.settings()["backends"])
    ids = [v["id"] for v in reversed(snapshot)]
    reordered = control.priority(ctx, ids)
    assert [v["id"] for v in reordered["backends"]] == ids
    assert {v["id"]: v for v in search_service.settings()["backends"]} == {v["id"]: v for v in snapshot}
    with pytest.raises(ManagementError):
        control.priority(ctx, ids[:-1] + [ids[0]])
    with pytest.raises(ManagementError):
        control.patch_backend(ctx, "tavily", {"id": "other"})
    control.patch_backend(ctx, "tavily", {"apiKeys": []})
    assert row(control.get(ctx), "tavily")["keyCount"] == 0


def test_full_oauth_identity_disabled_account_optin_only_changes_search(memory, control, ctx):
    memory.value["oauthAccounts"] = [
        {"provider": "openai", "email": "same@example.test", "workspace_id": "workspace-one", "access_token": "oauth-a", "enabled": False},
        {"provider": "openai", "email": "same@example.test", "workspace_id": "workspace-two", "access_token": "oauth-b", "enabled": True},
    ]
    accounts = copy.deepcopy(memory.value["oauthAccounts"])
    public = control.get(ctx)
    assert row(public, "openai")["allowDisabledAccounts"] is False
    assert row(public, "openai")["accountCount"] == 1
    choices = control.accounts(ctx, "openai")
    assert [v["id"] for v in choices] == ["openai:same@example.test:workspace-one", "openai:same@example.test:workspace-two"]
    with pytest.raises(ManagementError):
        control.patch_backend(ctx, "openai", {"accountIds": ["openai:same@example.test"]})
    selected = control.patch_backend(ctx, "openai", {"accountIds": [choices[0]["id"]]})
    assert row(selected, "openai")["available"] is False
    selected = control.patch_backend(ctx, "openai", {"allowDisabledAccounts": True})
    assert row(selected, "openai")["available"] is True
    assert memory.value["oauthAccounts"] == accounts
    assert_no_keys(choices, "oauth-a", "oauth-b")


@pytest.mark.parametrize("patch", [{"maxAttempts": 11}, {"maxResults": True}, {"timeoutSeconds": float("nan")},
                                      {"functionMode": None}, {"backends": []}, {"maxToolRounds": 0}, {"freshness": "forever"}])
def test_invalid_setting_never_writes(memory, control, ctx, patch):
    with pytest.raises(ManagementError):
        control.patch(ctx, patch)
    assert memory.updates == 0


def test_control_authorization_and_safe_failures(memory, control, ctx, monkeypatch):
    read_only = replace(ctx, actor=ManagementPrincipal.with_capabilities(subject_id="reader", auth_method=AuthMethod.MANAGEMENT_KEY,
                        capabilities=[Capability.READ], issued_at=datetime.now(timezone.utc)))
    with pytest.raises(ManagementError):
        control.patch(read_only, {"maxResults": 4})
    writer = replace(read_only, actor=replace(read_only.actor, capabilities=frozenset({Capability.READ, Capability.WRITE})))
    with pytest.raises(ManagementError):
        control.patch_backend(writer, "tavily", {"apiKeys": ["must-not-save"]})
    assert memory.updates == 0
    def broken(_):
        raise RuntimeError("private-token-write-failure")
    monkeypatch.setattr(config, "update", broken)
    with pytest.raises(ManagementError) as error:
        control.patch(ctx, {"maxResults": 4})
    assert "private-token" not in str(error.value)


def test_real_service_probe_is_single_source_and_does_not_failover(memory, control, ctx, monkeypatch):
    control.patch_backend(ctx, "anysearch", {"apiKeys": ["a"]})
    control.patch_backend(ctx, "tavily", {"apiKeys": ["t1", "t2"]})
    called = []
    async def adapter(backend, credential, args, operation, cfg):
        called.append((backend["id"], credential, operation))
        raise search_service.SearchError("safe failure")
    monkeypatch.setattr(search_service, "_http_adapter", adapter)
    with pytest.raises(ManagementError, match="safe failure"):
        asyncio.run(control.test(ctx, "tavily", query="test query"))
    assert called == [("tavily", "t1", "search"), ("tavily", "t2", "search"), ("tavily", "t1", "search")]
    called.clear()
    async def success(backend, credential, args, operation, cfg):
        called.append((backend["id"], operation))
        return {"content": "example extracted text"}
    monkeypatch.setattr(search_service, "_http_adapter", success)
    summary = asyncio.run(control.test(ctx, "tavily", operation="extract", url="https://example.com/"))
    assert called == [("tavily", "extract")]
    assert summary["contentChars"] == 22 and summary["attemptCount"] == 1
    assert memory.updates == 2  # tests never change readiness/credentials/settings


def test_real_config_atomic_save_and_reload_preserves_legacy(tmp_path, monkeypatch, ctx):
    # Uses the production config.update writer, but exclusively an isolated temp file.
    path = tmp_path / "search-config.json"
    initial = copy.deepcopy(config.get())
    initial.update(search={}, anysearch={"apiKey": "disk-legacy-secret", "enabled": True})
    initial["oauthAccounts"] = []
    path.write_text(json.dumps(initial))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", copy.deepcopy(initial))
    monkeypatch.setattr(config, "_mtime", os.path.getmtime(path))
    monkeypatch.setattr(config, "_reload_callbacks", [])
    control = SearchControl()
    control.patch_backend(ctx, "exa", {"apiKeys": ["disk-key-1", "disk-key-2"]})
    control.patch(ctx, {"maxResults": 5})
    persisted = json.loads(path.read_text())
    assert persisted["anysearch"] == initial["anysearch"]
    assert persisted["search"]["backends"][0]["apiKeys"] == ["disk-legacy-secret"]
    assert persisted["search"]["backends"][2]["apiKeys"] == ["disk-key-1", "disk-key-2"]
    config.reload()
    result = control.get(ctx)
    assert result["maxResults"] == 5 and row(result, "exa")["keyCount"] == 2
    assert_no_keys(result, "disk-key-1", "disk-key-2", "disk-legacy-secret")


@pytest.fixture
def api(tmp_path, memory, control):
    app, runtime, _ = build_app(tmp_path)
    app.include_router(router, prefix="/api/management/v1")
    control.operations = runtime.operations
    app.state.management_search_control = control
    try:
        with TestClient(app) as client:
            credential = create_session(client)
            yield client, bearer(credential), runtime, app
    finally:
        runtime.close()


def test_api_auth_origin_validation_envelope_and_save_readback(api, memory, control):
    client, headers, _, _ = api
    base = "/api/management/v1/search"
    assert client.get(base).status_code == 401
    assert client.patch(base, json={"maxResults": 6}, headers={**headers, "Origin": "https://evil.invalid"}).status_code == 403
    response = client.get(base, headers={**headers, "X-Request-Id": "search-request"})
    assert response.status_code == 200 and response.json()["meta"]["requestId"] == "search-request"
    assert response.headers["cache-control"] == "no-store"
    revision = response.json()["data"]["revision"]
    response = client.patch(base + "/backends/tavily", json={"apiKeys": ["api-secret-one", "api-secret-two"]}, headers={**headers, "If-Match": revision})
    assert response.status_code == 200
    assert row(response.json()["data"], "tavily")["keyCount"] == 2
    assert_no_keys(response.json(), "api-secret-one", "api-secret-two")
    stale = client.patch(base, json={"maxResults": 6}, headers={**headers, "If-Match": revision})
    assert stale.status_code == 409
    assert client.patch(base, json={"maxResults": "6"}, headers=headers).status_code == 422
    assert client.patch(base + "/backends/tavily", json={"apiKeys": [123]}, headers=headers).status_code == 422
    assert client.get(base + "?apiKey=leak", headers=headers).status_code == 422
    assert client.post(base + "/backends", json={"type": "searxng"}, headers=headers).status_code == 422
    response = client.post(base + "/backends", json={"type": "exa", "id": "extra-exa", "name": "Second Exa", "apiKeys": ["extra-secret"]}, headers=headers)
    assert response.status_code == 201
    ids = [v["id"] for v in response.json()["data"]["backends"]][::-1]
    assert client.put(base + "/priority", json={"backendIds": ids}, headers=headers).status_code == 200
    updated = client.patch(base, json={"functionMode": "disabled", "hostedMode": "passthrough", "language": "en", "country": "US", "freshness": "day"}, headers=headers)
    assert updated.status_code == 200
    saved = client.get(base, headers=headers).json()["data"]
    assert saved["functionMode"] == "disabled" and saved["hostedMode"] == "passthrough"
    assert [v["id"] for v in saved["backends"]] == ids
    assert row(saved, "extra-exa")["keyCount"] == 1
    assert_no_keys(saved, "api-secret-one", "api-secret-two", "extra-secret")
    assert_no_keys(control._audit_sink.snapshot(), "api-secret-one", "api-secret-two", "extra-secret")


def test_api_test_operation_and_idempotency(api, monkeypatch, memory):
    client, headers, _, _ = api
    calls = []
    async def search(args, *, request_id, backend_id, origin="managed_round", round_no=0):
        calls.append((args, request_id, backend_id))
        return {"results": [{"title": "safe", "url": "https://example.test"}], "attempts": [{}]}
    monkeypatch.setattr(search_service, "search", search)
    base = "/api/management/v1"
    request = {"backendId": "tavily", "query": "explicit test"}
    headers = {**headers, "Idempotency-Key": "once-only"}
    response = client.post(base + "/search/test", json=request, headers=headers)
    assert response.status_code == 202, response.text
    operation_id = response.json()["data"]["id"]
    for _ in range(100):
        operation = client.get(base + "/operations/" + operation_id, headers=headers).json()["data"]
        if operation["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.01)
    assert operation["status"] == "succeeded"
    assert operation["result"]["backendId"] == "tavily" and operation["result"]["resultCount"] == 1
    replay = client.post(base + "/search/test", json=request, headers=headers)
    assert replay.json()["data"]["id"] == operation_id
    conflict = client.post(base + "/search/test", json={**request, "query": "changed"}, headers=headers)
    assert conflict.status_code == 409
    assert len(calls) == 1 and calls[0][2] == "tavily"
    assert client.post(base + "/search/test", json={"backendId": "tavily"}, headers=headers).status_code == 422
    assert memory.updates == 0


@pytest.fixture
def search_workers(monkeypatch):
    workers = []
    real_spawn = search_menu._spawn_async_task
    def spawn(factory):
        worker = real_spawn(factory)
        workers.append(worker)
        return worker
    monkeypatch.setattr(search_menu, "_spawn_async_task", spawn)
    yield workers
    for worker in workers:
        worker.join(2)
        assert not worker.is_alive(), "search worker leaked past test"


@pytest.fixture
def tg(monkeypatch, memory, control, search_workers):
    states.clear_all()
    records = []
    def fake_api(method, data=None):
        records.append((method, copy.deepcopy(data or {})))
        return {"ok": True, "result": {"message_id": 200 + len(records)}}
    monkeypatch.setattr(search_menu, "_CONTROL", control)
    monkeypatch.setattr(ui, "api", fake_api)
    monkeypatch.setattr(ui, "is_admin", lambda chat_id: chat_id == 42)
    yield records
    states.clear_all()


def latest(records, method="editMessageText"):
    return [data for name, data in records if name == method][-1]


def buttons(message):
    return [item for values in message["reply_markup"]["inline_keyboard"] for item in values]


def callback(data):
    assert search_menu.handle_callback(42, 100, "cb", data)


def _last_text(records):
    """Latest rendered text, whichever Telegram method carried it."""
    for method, data in reversed(records):
        if method in ("editMessageText", "sendMessage"):
            return data["text"]
    raise AssertionError("no message rendered")


def test_tg_root_modes_defaults_brand_status_and_navigation(tg, memory, control, ctx):
    from src.telegram.menus import system_menu
    _, keyboard = system_menu._main_text_and_kb()
    assert any(v["text"] == "🔎 搜索工具" and v["callback_data"] == "srch:show" for group in keyboard["inline_keyboard"] for v in group)
    callback("srch:show")
    page = latest(tg)
    assert "共 7 个来源" in page["text"] and "待配置" in page["text"]
    assert "本轮" not in page["text"] and "14.3" not in page["text"]
    assert buttons(page)[-1]["callback_data"] == "menu:settings"
    openai_button = next(v for v in buttons(page) if "OpenAI OAuth" in v["text"])
    assert openai_button["icon_custom_emoji_id"] == ui.provider_custom_emoji_id("openai")
    # Both ownership switches live on one page and apply in a single tap.
    callback("srch:modes")
    assert "搜索归属策略" in latest(tg)["text"]
    callback("srch:setmode:functionMode:disabled")
    callback("srch:setmode:hostedMode:passthrough")
    assert control.get(ctx)["functionMode"] == "disabled"
    assert control.get(ctx)["hostedMode"] == "passthrough"
    callback("srch:defaults")
    defaults = latest(tg)["text"]
    # Values are grouped and each button carries its current value.
    assert "总尝试次数（含首次）：<code>3</code>" in defaults
    assert "单次超时（秒）：<code>10s</code>" in defaults
    assert any(v["text"] == "✏ 单次超时（秒）：10s" for v in buttons(latest(tg)))
    assert "14.3" not in defaults and "本轮" not in defaults
    callback("srch:backend:" + search_menu._code("anthropic"))
    detail = latest(tg)["text"]
    # Implementation-status prose is replaced by a normal status line.
    assert "状态: " in detail and "原生实现尚未完成账户实测" not in detail
    assert "本轮" not in detail and "无 Anthropic 账户" not in detail
    for field, value in (("maxAttempts", "4"), ("timeoutSeconds", "20"), ("maxResults", "9"), ("language", "zh")):
        callback("srch:edit:" + field)
        search_menu.handle_text_state(42, "search_input", value)
        assert latest(tg, "sendMessage")["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == "srch:defaults"
    assert control.get(ctx)["timeoutSeconds"] == 20


def test_tg_key_edits_sort_stable_id_cancel_and_direct_parent(tg, memory, control, ctx):
    code = search_menu._code("tavily")
    callback("srch:keys:" + code)
    assert "已设置 0 个" in latest(tg)["text"]
    callback("srch:apiKeys:" + code)
    search_menu.handle_text_state(42, "search_input", "tg-secret-one\ntg-secret-two")
    assert control.get(ctx)["backends"][1]["keyCount"] == 2
    assert latest(tg, "sendMessage")["reply_markup"]["inline_keyboard"][-1][0]["callback_data"] == "srch:keys:" + code
    callback("srch:up:" + code)
    assert control.get(ctx)["backends"][0]["id"] == "tavily"
    assert "Tavily" in latest(tg)["text"]
    callback("srch:addApiKeys:" + code)
    search_menu.handle_text_state(42, "search_input", "tg-secret-three")
    callback("srch:removeKeyIndices:" + code)
    search_menu.handle_text_state(42, "search_input", "2")
    assert search_service.settings()["backends"][0]["apiKeys"] == ["tg-secret-one", "tg-secret-three"]
    callback("srch:apiKeys:" + code)
    callback("srch:keys:" + code)  # cancel does not clear keys, merely abandons input
    assert states.get_state(42) is None
    assert control.get(ctx)["backends"][0]["keyCount"] == 2
    callback("srch:backend:" + code)
    assert buttons(latest(tg))[-1]["callback_data"] == "srch:show"
    assert_no_keys(tg, "tg-secret-one", "tg-secret-two", "tg-secret-three")


@pytest.mark.parametrize("disabled_reason", [None, "user", "auth", "quota"])
def test_oauth_ineligible_status_and_manual_optin_semantics(tg, api, memory, disabled_reason):
    client, headers, _, _ = api
    account = {"provider": "openai", "email": "disabled@example.test", "workspace_id": "stable-workspace",
               "access_token": "private-oauth", "enabled": False}
    if disabled_reason is not None:
        account["disabled_reason"] = disabled_reason
    memory.value["oauthAccounts"] = [account]
    before = copy.deepcopy(account)
    data = client.get("/api/management/v1/search", headers=headers).json()["data"]
    assert row(data, "openai")["reason"] == "no_eligible_accounts"
    assert row(data, "openai")["allowDisabledAccounts"] is False
    assert row(data, "tavily")["reason"] == "missing_credentials"
    callback("srch:show")
    labels = [v["text"] for v in buttons(latest(tg))]
    assert any("OpenAI OAuth" in label for label in labels)
    assert any("Tavily" in label for label in labels)
    assert any("🔕" in label for label in labels)
    assert "待配置" in latest(tg)["text"]
    code = search_menu._code("openai")
    callback("srch:backend:" + code)
    page = latest(tg)
    assert "来源无可用账户" in page["text"] and "缺少凭据" not in page["text"]
    assert "允许使用手动停用账户: 关" in page["text"]
    assert "不改变普通对话状态" in page["text"]
    assert any(v["text"] == "⏸ 允许停用账户：关" for v in buttons(page))
    callback("srch:allow:" + code)
    data = client.get("/api/management/v1/search", headers=headers).json()["data"]
    eligible = disabled_reason in (None, "user")
    assert row(data, "openai")["available"] is eligible
    assert row(data, "openai")["reason"] == ("configured" if eligible else "no_eligible_accounts")
    assert ("来源可用" if eligible else "来源无可用账户") in latest(tg)["text"]
    assert memory.value["oauthAccounts"] == [before]


def test_tg_accounts_optin_and_single_source_test(tg, memory, control, ctx, monkeypatch, search_workers):
    memory.value["oauthAccounts"] = [{"provider": "openai", "email": "user@example.test", "workspace_id": "stable-workspace", "access_token": "private-oauth", "enabled": False}]
    code = search_menu._code("openai")
    callback("srch:allow:" + code)
    assert memory.value["oauthAccounts"][0]["enabled"] is False
    callback("srch:accounts:" + code)
    select = next(v["callback_data"] for v in buttons(latest(tg)) if v["callback_data"].startswith("srch:account:"))
    callback(select)
    assert row(control.get(ctx), "openai")["accountIds"] == ["openai:user@example.test:stable-workspace"]
    assert buttons(latest(tg))[-1]["callback_data"] == "srch:backend:" + code
    calls = []
    async def search(arguments, *, request_id, backend_id, origin="managed_round", round_no=0):
        calls.append(backend_id)
        return {"results": [], "attempts": [{"backend_id": backend_id}]}
    monkeypatch.setattr(search_service, "search", search)
    callback("srch:test:" + code)
    assert "失败不会切换其它来源" in latest(tg)["text"]
    search_menu.handle_text_state(42, "search_input", "user requested query")
    for worker in search_workers:
        worker.join(2)
    assert calls == ["openai"]
    assert "此来源测试成功" in latest(tg)["text"]
    assert_no_keys(tg, "private-oauth")


def test_bot_dispatch_admin_gate_search_input_and_leaving_clears_state(tg, memory):
    from src.telegram import bot
    def cb(chat_id, data):
        bot._handle_callback({"id": "cb", "message": {"chat": {"id": chat_id}, "message_id": 100}, "data": data})
    cb(43, "srch:show")
    assert "无权限" in latest(tg, "answerCallbackQuery")["text"]
    cb(42, "srch:show")
    assert "搜索工具" in latest(tg)["text"]
    cb(42, "srch:edit:maxResults")
    bot._handle_message({"chat": {"id": 42}, "text": "7", "message_id": 101})
    assert search_service.settings()["maxResults"] == 7
    cb(42, "srch:edit:maxResults")
    cb(42, "menu:settings")
    assert states.get_state(42) is None
    assert "系统设置" in latest(tg)["text"]


def test_api_capabilities_and_runtime_binding(api, ctx):
    from src.management_api.dependencies import get_management_context
    client, headers, runtime, app = api
    del app.state.management_search_control  # exercise normal runtime-owned binding
    assert client.get("/api/management/v1/search", headers=headers).status_code == 200
    bound = app.state.management_search_control
    assert bound is not search_menu.DEFAULT_SEARCH_CONTROL
    assert bound._audit_sink is runtime.audit_sink and bound.operations is runtime.operations
    read_only = replace(ctx, actor=replace(ctx.actor, capabilities=frozenset({Capability.READ})))
    app.dependency_overrides[get_management_context] = lambda: read_only
    assert client.patch("/api/management/v1/search", json={"maxResults": 7}, headers=headers).status_code == 403
    writer = replace(ctx, actor=replace(ctx.actor, capabilities=frozenset({Capability.READ, Capability.WRITE})))
    app.dependency_overrides[get_management_context] = lambda: writer
    assert client.patch("/api/management/v1/search", json={"maxResults": 7}, headers=headers).status_code == 200
    response = client.patch("/api/management/v1/search/backends/tavily", json={"apiKeys": ["secret-denied"]}, headers=headers)
    assert response.status_code == 403 and "secret-denied" not in response.text


def test_probe_failure_operation_does_not_expose_exception(api, monkeypatch):
    client, headers, _, _ = api
    async def fail(*args, **kwargs):
        raise RuntimeError("credential=raw-secret-never-public")
    monkeypatch.setattr(search_service, "search", fail)
    response = client.post("/api/management/v1/search/test", json={"backendId": "tavily", "query": "explicit test"}, headers=headers)
    operation_id = response.json()["data"]["id"]
    for _ in range(100):
        result = client.get("/api/management/v1/operations/" + operation_id, headers=headers)
        if result.json()["data"]["status"] == "failed":
            break
        time.sleep(0.01)
    assert result.json()["data"]["status"] == "failed"
    assert "raw-secret" not in result.text


def test_registration_and_openapi_write_only_request_keys():
    from src.management_api.router import create_management_router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(create_management_router())
    document = app.openapi()
    assert "/api/management/v1/search" in document["paths"]
    schemas = document["components"]["schemas"]
    assert "apiKeys" not in schemas["SearchBackendData"]["properties"]
    assert schemas["SearchBackendPatch"]["properties"]["apiKeys"]["writeOnly"] is True


def test_legacy_url_credentials_are_not_returned_or_rewritten(memory, control, ctx):
    memory.value["anysearch"] = {"endpoint": "https://user:private-url-secret@example.test/mcp"}
    original = copy.deepcopy(memory.value)
    assert_no_keys(control.get(ctx), "private-url-secret")
    assert memory.value == original


def test_tg_stale_key_editor_rejected_without_clearing_keys(tg, control, ctx):
    code = search_menu._code("tavily")
    control.patch_backend(ctx, "tavily", {"apiKeys": ["existing-one"]})
    callback("srch:apiKeys:" + code)
    control.patch_backend(ctx, "tavily", {"addApiKeys": ["concurrent-two"]})
    search_menu.handle_text_state(42, "search_input", "replace-stale")
    assert row(control.get(ctx), "tavily")["keyCount"] == 2
    assert "REVISION_CONFLICT" in latest(tg, "sendMessage")["text"]
    assert_no_keys(tg, "existing-one", "concurrent-two", "replace-stale")


@pytest.mark.parametrize("kind", search_service.BACKEND_TYPES)
def test_model_setting_is_oauth_only_and_chosen_from_catalog(api, tg, memory, control, ctx, kind):
    client, headers, _, _ = api
    is_oauth = kind in ("openai", "xai", "anthropic")
    code = search_menu._code(kind)
    callback("srch:backend:" + code)
    detail = latest(tg)
    assert ("模型: " in detail["text"]) is is_oauth
    # OAuth sources open a list picker; API-key sources have no model dimension.
    assert any(v["callback_data"] == "srch:models:" + code for v in buttons(detail)) is is_oauth
    assert not any(v["callback_data"] == "srch:model:" + code for v in buttons(detail))
    # Read compatibility: legacy/default empty model remains a valid DTO value.
    assert row(client.get("/api/management/v1/search", headers=headers).json()["data"], kind)["model"] == ""
    before = memory.updates
    response = client.patch("/api/management/v1/search/backends/" + kind, json={"model": "requested-model"}, headers=headers)
    assert response.status_code == (200 if is_oauth else 422)
    assert memory.updates == before + int(is_oauth)
    created = client.post("/api/management/v1/search/backends", json={"type": kind, "id": kind + "-model-test", "model": "requested-model"}, headers=headers)
    assert created.status_code == (201 if is_oauth else 422)
    if is_oauth:
        # A forged free-text editor can no longer open an input state at all:
        # the action is unknown, so the button is rejected rather than silently
        # accepting typed model names.
        callback("srch:model:" + code)
        assert states.get_state(42) is None
        assert "从账户模型目录选择" in _last_text(tg)
        callback("srch:models:" + code)
        assert "搜索模型" in latest(tg)["text"]
        # No eligible account here, so the list is empty and only "automatic" is offered.
        assert not any(str(v["callback_data"]).startswith("srch:setmodel:" + code + ":")
                       and len(str(v["callback_data"])) > len("srch:setmodel:" + code + ":")
                       for v in buttons(latest(tg)))
        callback("srch:setmodel:" + code + ":")
        assert row(control.get(ctx), kind)["model"] == ""
    else:
        # An API-key source has no model dimension; both the list picker and a
        # legacy free-text button are refused instead of silently rendering.
        callback("srch:models:" + code)
        assert "只有 OAuth 搜索来源支持模型设置" in _last_text(tg)
        callback("srch:model:" + code)
        assert "从账户模型目录选择" in _last_text(tg)
        assert states.get_state(42) is None
        assert row(control.get(ctx), kind)["model"] == ""
        cleared = client.patch("/api/management/v1/search/backends/" + kind, json={"model": ""}, headers=headers)
        assert cleared.status_code == 200
        assert row(cleared.json()["data"], kind)["model"] == ""


@pytest.mark.parametrize("kind", ["xai", "openai"])
def test_model_picker_lists_only_eligible_account_catalog(tg, memory, control, ctx, kind):
    """The picker offers the account's own catalog and writes back the chosen ID."""
    account = {"provider": kind, "email": "catalog@example.test",
               "access_token": "private-oauth", "enabled": True,
               "models": ["model-b", "model-a"]}
    memory.value["oauthAccounts"] = [account]
    code = search_menu._code(kind)
    callback("srch:models:" + code)
    page = latest(tg)
    offered = [str(v["callback_data"]) for v in buttons(page) if str(v["callback_data"]).startswith("srch:setmodel:" + code + ":")]
    assert len(offered) == 2
    chosen = offered[0]
    callback(chosen)
    assert row(control.get(ctx), kind)["model"] in ("model-a", "model-b")
    assert any(v["text"].startswith("✅ ") for v in buttons(latest(tg)))
    # Restoring automatic clears the stored override.
    callback("srch:setmodel:" + code + ":")
    assert row(control.get(ctx), kind)["model"] == ""


def test_model_picker_refuses_api_key_source(tg, memory, control, ctx):
    callback("srch:models:" + search_menu._code("tavily"))
    assert "只有 OAuth 搜索来源支持模型设置" in _last_text(tg)
    # A legacy free-text model button is also refused, never silently rendered.
    callback("srch:model:" + search_menu._code("tavily"))
    assert "从账户模型目录选择" in _last_text(tg)


@pytest.mark.parametrize("kind", ["tavily", "openai"])
def test_search_detail_action_labels_follow_enabled(tg, control, ctx, kind):
    code = search_menu._code(kind)
    for enabled in (True, False, True):
        callback("srch:backend:" + code)
        toggle = next(v for v in buttons(latest(tg)) if v["callback_data"] == "srch:toggle:" + code)
        assert toggle["text"] == ("❌ 停用来源" if enabled else "✅ 启用来源")
        callback(toggle["callback_data"])
        assert row(control.get(ctx), kind)["enabled"] is not enabled


@pytest.mark.parametrize("field", ["functionMode", "hostedMode"])
def test_search_mode_picker_marks_current_selection(tg, control, ctx, field):
    """One page carries both switches; the current choice is marked once."""
    for mode in ("managed", "passthrough", "disabled"):
        control.patch(ctx, {field: mode})
        callback("srch:modes:2")
        page = latest(tg)
        marked = [v for v in buttons(page) if v["text"].startswith("✅ ")]
        # Exactly one mark per switch, so two in total; the other switch keeps
        # whatever it currently holds rather than being reset by this page.
        assert len(marked) == 2
        assert any(v["callback_data"] == f"srch:setmode:{field}:{mode}:2" for v in marked)
        for other in ("functionMode", "hostedMode"):
            current = control.get(ctx)[other]
            assert any(v["callback_data"] == f"srch:setmode:{other}:{current}:2" for v in marked)
        # The mark is the only leading glyph, so the label never stacks icons.
        assert all(not v["text"].startswith("✅ ✅") for v in buttons(page))
        assert search_menu._MODES[mode] in page["text"]
        assert ("普通 function" if field == "functionMode" else "原生 hosted") in page["text"]
        assert buttons(page)[-1]["callback_data"] == "srch:show:2"


def test_key_page_replacement_character_free(tg):
    for kind in ("anysearch", "tavily", "exa", "brave"):
        callback("srch:keys:" + search_menu._code(kind))
        page = latest(tg)
        assert "序号从 1 开始" in page["text"]
        assert "保存不影响其它来源的 Key" in page["text"]
        assert "\ufffd" not in json.dumps(page, ensure_ascii=False)



def test_api_search_logs_and_stats_read_dedicated_log(api, memory, control, ctx, monkeypatch, tmp_path):
    """GET /search/logs and /search/stats read the search log, not request logs."""
    import threading
    from src import log_db
    client, headers, _, _ = api
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="tavily", source_type="tavily",
        source_name="Tavily", operation="search", credential_kind="api_key",
        credential_label="Key #1", query="api probe", origin="management_test",
    )
    log_db.finish_search_call(handle, status="success", elapsed_ms=120, result_count=3)

    logs = client.get("/api/management/v1/search/logs", headers=headers)
    assert logs.status_code == 200, logs.text
    rows = logs.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["sourceId"] == "tavily" and row["sourceName"] == "Tavily"
    assert row["origin"] == "management_test" and row["operation"] == "search"
    assert row["resultCount"] == 3 and row["elapsedMs"] == 120
    assert row["status"] == "success" and row["costSource"] in ("estimated", "unpriced")
    assert "costUsd" in row and isinstance(row["costUsd"], float)

    stats = client.get("/api/management/v1/search/stats", headers=headers)
    assert stats.status_code == 200, stats.text
    data = stats.json()["data"]
    assert len(data) == 1
    assert data[0]["sourceId"] == "tavily" and data[0]["attempts"] == 1
    assert data[0]["success"] == 1 and data[0]["averageMs"] == 120


def test_api_search_logs_filter_and_unknown_query(api, monkeypatch, tmp_path):
    import threading
    from src import log_db
    client, headers, _, _ = api
    (tmp_path / "logs2").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path / "logs2"))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    for source in ("tavily", "exa"):
        handle = log_db.record_search_call(
            call_id="c-" + source, attempt_no=1, source_id=source, source_type=source,
            operation="search",
        )
        log_db.finish_search_call(handle, status="success", elapsed_ms=1)
    only = client.get("/api/management/v1/search/logs?sourceId=exa", headers=headers)
    assert [r["sourceId"] for r in only.json()["data"]] == ["exa"]
    # Rejected unknown query parameters keep the strict read contract, and the
    # pre-existing routes still accept no query parameters at all.
    assert client.get("/api/management/v1/search/logs?bogus=1", headers=headers).status_code == 422
    assert client.get("/api/management/v1/search/stats?bogus=1", headers=headers).status_code == 422
    assert client.get("/api/management/v1/search/logs?period=nonsense", headers=headers).status_code == 422
    assert client.get("/api/management/v1/search?bogus=1", headers=headers).status_code == 422


def test_api_create_and_update_reject_api_key_model(api, memory, control, ctx):
    """Model overrides stay restricted to OAuth sources at the API boundary."""
    client, headers, _, _ = api
    base = "/api/management/v1/search/backends"
    assert client.post(base, json={"type": "tavily", "id": "k-model", "model": "x"},
                       headers=headers).status_code == 422
    created = client.post(base, json={"type": "xai", "id": "o-model"}, headers=headers)
    assert created.status_code == 201
    assert client.patch(base + "/o-model", json={"model": "grok-4.6"}, headers=headers).status_code == 200
