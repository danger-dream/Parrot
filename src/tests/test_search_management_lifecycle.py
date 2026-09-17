"""Direct regressions for search TG lifecycle, navigation and source deletion."""
from __future__ import annotations

import asyncio
import copy
import threading

import pytest

from src import search_service
from src.management_api.dependencies import get_management_context
from src.management_auth import Capability
from src.telegram import bot, states, ui
from src.telegram.menus import search_menu
from src.tests.test_search_management import (
    api, memory, control, ctx, tg, search_workers,  # reusable isolated fixtures
    buttons, callback, latest, row,
)


def bot_callback(data, message_id=100):
    bot._handle_callback({"id": "cb", "message": {"chat": {"id": 42}, "message_id": message_id}, "data": data})


def bot_text(text):
    bot._handle_message({"chat": {"id": 42}, "message_id": 101, "text": text})


@pytest.mark.parametrize("navigate_progress", [False, True])
def test_blocked_probe_returns_immediately_and_owns_only_its_progress(tg, monkeypatch, search_workers, navigate_progress):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    calls = []
    async def blocked(arguments, *, backend_id, request_id, origin="managed_round", round_no=0):
        calls.append(backend_id)
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        return {"results": [], "attempts": [{}]}
    monkeypatch.setattr(search_service, "search", blocked)
    bot_callback("srch:test:" + search_menu._code("tavily"))
    original = states.get_state(42)["data"]
    def submit():
        bot_text("deliberate paid test")
        returned.set()
    handler = threading.Thread(target=submit, daemon=True)
    try:
        handler.start()
        assert entered.wait(1), "fake backend never started"
        assert returned.wait(0.5), "bot handler blocked on unfinished backend"
        assert not release.is_set() and search_workers[0].is_alive()
        assert states.get_state(42) is None
        progress_index, progress = next((i, data) for i, (method, data) in enumerate(tg)
                                        if method == "sendMessage" and "正在测试" in data["text"])
        progress_id = 201 + progress_index
        # Replay the already-consumed input data while the call is still blocked.
        search_menu._on_test_input(42, original, "deliberate paid test")
        assert len(calls) == 1
        # Actual dispatcher still services an unrelated menu while search is blocked.
        bot_callback("menu:settings", progress_id if navigate_progress else 100)
        assert "系统设置" in latest(tg)["text"]
        bot_callback("srch:edit:maxResults", 100)
        newer_state = states.get_state(42)["data"]
        checkpoint = len(tg)
        release.set()
        handler.join(1)
        for worker in search_workers:
            worker.join(2)
            assert not worker.is_alive()
        assert states.get_state(42)["data"] is newer_state
        result_edits = [data for method, data in tg[checkpoint:] if method == "editMessageText"]
        if navigate_progress:
            assert result_edits == [], "late result replaced the page opened on its old prompt"
        else:
            assert len(result_edits) == 1
            assert result_edits[0]["message_id"] == progress_id
            assert "此来源测试成功" in result_edits[0]["text"]
        assert calls == ["tavily"]
    finally:
        release.set()
        handler.join(2)
        for worker in search_workers:
            worker.join(2)


def test_missing_progress_message_does_not_dispatch_paid_probe(tg, monkeypatch, search_workers):
    calls = []
    async def unexpected(*args, **kwargs):
        calls.append(True)
        return {"results": [], "attempts": []}
    monkeypatch.setattr(search_service, "search", unexpected)
    bot_callback("srch:test:" + search_menu._code("tavily"))
    monkeypatch.setattr(ui, "send", lambda *args, **kwargs: {"ok": False})
    bot_text("query")
    assert states.get_state(42) is None
    assert not calls and not search_workers


def test_cancel_command_returns_exact_parent_and_settings_command_leaves(tg, memory):
    code = search_menu._code("tavily", 2)
    bot_callback("srch:apiKeys:" + code)
    updates = memory.updates
    bot_text("/cancel")
    result = latest(tg, "sendMessage")
    assert "已取消，未保存" in result["text"]
    assert buttons(result)[0]["callback_data"] == "srch:keys:" + code
    assert states.get_state(42) is None and memory.updates == updates
    assert "未识别输入" not in result["text"]
    bot_callback("srch:edit:maxResults:2")
    bot_text("/settings")
    assert states.get_state(42) is None
    assert "系统设置" in latest(tg, "sendMessage")["text"]
    assert memory.updates == updates


def test_account_labels_and_human_workspace_disambiguation(tg, memory, control, ctx):
    memory.value["oauthAccounts"] = [
        {"provider": "openai", "email": "same@example.test", "workspace_id": "opaque-one", "workspace_name": "研发", "access_token": "token", "label": "主要账户", "name": "ignored-name"},
        {"provider": "openai", "email": "same@example.test", "workspace_id": "opaque-two", "workspace_name": "Personal", "workspace_type": "team", "plan_type": "team", "access_token": "token"},
        {"provider": "xai", "email": "mail@example.test", "subject": "opaque-subject", "label": "搜索账户", "access_token": "token"},
    ]
    rows = control.accounts(ctx, "openai")
    assert [v["name"] for v in rows] == ["主要账户 · 研发", "same@example.test · team"]
    assert [v["id"] for v in rows] == ["openai:same@example.test:opaque-one", "openai:same@example.test:opaque-two"]
    assert control.accounts(ctx, "xai")[0]["name"] == "搜索账户"
    callback("srch:accounts:" + search_menu._code("openai"))
    labels = " ".join(v["text"] for v in buttons(latest(tg)))
    assert "主要账户 · 研发" in labels and "same@example.test · team" in labels
    assert "opaque" not in labels and "ignored-name" not in labels
    selected = next(v["callback_data"] for v in buttons(latest(tg)) if "主要账户" in v["text"])
    callback(selected)
    assert row(control.get(ctx), "openai")["accountIds"] == ["openai:same@example.test:opaque-one"]


def populate_backends(control, ctx):
    for i in range(18):
        control.add_backend(ctx, {"type": "openai", "id": f"extra-{i}", "name": f"来源{i}" + "&<>" * 50})


def assert_bounded(page):
    assert len(page["text"]) < 4096
    assert len(page["reply_markup"]["inline_keyboard"]) <= 14
    assert all(len(v["callback_data"].encode()) <= 64 for v in buttons(page))


def test_large_backend_pages_preserve_edit_sort_and_parent_navigation(tg, control, ctx):
    populate_backends(control, ctx)  # 25 sources, not only the default seven
    seen = []
    # Six list rows per page: 25 sources span 6/6/6/6/1.
    for page_index, count in ((0, 6), (1, 6), (2, 6), (3, 6), (4, 1)):
        callback(search_menu._home(page_index))
        page = latest(tg)
        assert_bounded(page)
        entries = [v for v in buttons(page) if v["callback_data"].startswith("srch:backend:")]
        assert len(entries) == count
        seen.extend(search_menu._target(v["callback_data"].split(":")[2])[0] for v in entries)
        assert f"第 {page_index + 1}/5 页" in page["text"]
    assert len(seen) == len(set(seen)) == 25
    callback("srch:show:1")
    detail = next(v["callback_data"] for v in buttons(latest(tg)) if v["callback_data"].startswith("srch:backend:"))
    code = detail.split(":")[2]
    backend_id, original_page = search_menu._target(code)
    assert original_page == 1
    callback(detail)
    assert buttons(latest(tg))[-1]["callback_data"] == "srch:show:1"
    callback("srch:name:" + code)
    bot_text("Edited source")
    assert buttons(latest(tg, "sendMessage"))[0]["callback_data"] == detail
    callback(detail)
    callback("srch:up:" + code)  # crosses page boundary, same identity/parent page
    assert row(control.get(ctx), backend_id)["name"] == "Edited source"
    assert buttons(latest(tg))[-1]["callback_data"] == "srch:show:1"
    callback(buttons(latest(tg))[-1]["callback_data"])
    assert "第 2/5 页" in latest(tg)["text"]
    callback("srch:defaults:1")
    callback("srch:edit:language:1")
    bot_text("en")
    callback(buttons(latest(tg, "sendMessage"))[0]["callback_data"])
    assert buttons(latest(tg))[-1]["callback_data"] == "srch:show:1"


def test_large_account_pages_select_by_full_identity_and_stay_on_page(tg, memory, control, ctx):
    memory.value["oauthAccounts"] = [
        {"provider": "openai", "email": "same@example.test", "workspace_id": f"opaque-{i}",
         "workspace_name": f"工作区{i}", "access_token": "token"} for i in range(63)
    ]
    code = search_menu._code("openai", 2)
    seen = []
    # Account rows follow the same six-per-page rule: 63 accounts span 11 pages.
    for p in range(11):
        callback(f"srch:accounts:{code}:{p}")
        page = latest(tg)
        assert_bounded(page)
        entries = [v for v in buttons(page) if v["callback_data"].startswith("srch:account:")]
        assert len(entries) == (3 if p == 10 else 6)
        seen.extend(v["text"] for v in entries)
    assert len(set(seen)) == 63 and "opaque" not in "".join(seen)
    callback(f"srch:accounts:{code}:9")
    selected = next(v["callback_data"] for v in buttons(latest(tg)) if "工作区54" in v["text"])
    callback(selected)
    assert row(control.get(ctx), "openai")["accountIds"] == ["openai:same@example.test:opaque-54"]
    assert buttons(latest(tg))[-1]["callback_data"] == "srch:backend:" + code
    callback(buttons(latest(tg))[-1]["callback_data"])
    assert buttons(latest(tg))[-1]["callback_data"] == "srch:show:2"


def test_delete_api_requires_write_and_cas_preserves_other_sources_and_legacy(api, memory, control, ctx):
    from dataclasses import replace
    client, headers, _, app = api
    memory.value["anysearch"] = {"apiKey": "shared-legacy-key"}
    memory.value["oauthAccounts"] = [{"provider": "openai", "email": "keep@example.test", "enabled": False, "access_token": "keep-token"}]
    control.add_backend(ctx, {"type": "anysearch", "id": "duplicate", "apiKeys": ["shared-legacy-key"]})
    before = copy.deepcopy(memory.value)
    revision = control.get(ctx)["revision"]
    path = "/api/management/v1/search/backends/anysearch"
    assert client.delete(path, headers=headers).status_code == 422
    assert client.delete(path, headers={**headers, "If-Match": "rev_stale"}).status_code == 409
    reader = replace(ctx, actor=replace(ctx.actor, capabilities=frozenset({Capability.READ})))
    app.dependency_overrides[get_management_context] = lambda: reader
    assert client.delete(path, headers={**headers, "If-Match": revision}).status_code == 403
    writer = replace(ctx, actor=replace(ctx.actor, capabilities=frozenset({Capability.WRITE})))
    app.dependency_overrides[get_management_context] = lambda: writer
    result = client.delete(path, headers={**headers, "If-Match": revision})
    assert result.status_code == 204 and not result.content
    assert result.headers["cache-control"] == "no-store"
    saved = search_service.settings()
    assert "anysearch" not in [v["id"] for v in saved["backends"]]
    assert row(saved, "duplicate")["apiKeys"] == ["shared-legacy-key"]
    assert memory.value["anysearch"] == before["anysearch"]
    assert memory.value["oauthAccounts"] == before["oauthAccounts"]
    # Explicitly deleting the final source must not revive legacy/default sources.
    for backend_id in [v["id"] for v in saved["backends"]]:
        revision = control.get(ctx)["revision"]
        assert client.delete("/api/management/v1/search/backends/" + backend_id, headers={**headers, "If-Match": revision}).status_code == 204
    assert search_service.settings()["backends"] == []
    assert memory.value["search"]["backends"] == []
    assert memory.value["anysearch"] == before["anysearch"]


def test_delete_tg_confirm_cancel_stale_and_page_return(tg, memory, control, ctx):
    populate_backends(control, ctx)
    code = search_menu._code("extra-15", 2)
    initial = copy.deepcopy(memory.value)
    callback("srch:delete:" + code)
    confirm = buttons(latest(tg))[0]["callback_data"]
    cancel = buttons(latest(tg))[1]["callback_data"]
    assert memory.value == initial
    callback(cancel)
    callback(confirm)
    assert memory.value == initial  # cancelled confirmation is no longer authorized
    callback("srch:delete:" + code)
    confirm = buttons(latest(tg))[0]["callback_data"]
    control.patch(ctx, {"maxResults": 9})
    callback(confirm)
    assert "REVISION_CONFLICT" in latest(tg, "sendMessage")["text"]
    assert row(control.get(ctx), "extra-15")
    callback("srch:delete:" + code)
    confirm = buttons(latest(tg))[0]["callback_data"]
    callback(confirm)
    assert "extra-15" not in [v["id"] for v in control.get(ctx)["backends"]]
    # 24 sources remain, spanning 4 pages; the action returns to its own page.
    assert "第 3/4 页" in latest(tg)["text"]
    saved = copy.deepcopy(memory.value)
    callback(confirm)
    assert memory.value == saved  # no second mutation/replay

