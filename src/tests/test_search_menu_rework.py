"""TG search-tool pages added by the rework: sort, stats and the search log.

Contract: the model is chosen from the account's own catalog (never typed); the
sort page commits every source order in one CAS write; statistics and the log
read the dedicated search log and never a request parent.
"""
from __future__ import annotations

import copy
import json

import pytest

from src import log_db, search_service
from src.telegram import ui
from src.telegram.menus import search_menu
from src.tests.test_search_management import (  # noqa: F401  (fixtures)
    api, buttons, callback, control, ctx, latest, memory, search_workers, tg,
)


def _render(cb):
    callback(cb)
    return latest(tg)


def test_model_button_is_gone_and_picker_is_a_list(tg, memory, control, ctx):
    memory.value["oauthAccounts"] = [{"provider": "xai", "email": "c@example.test",
                                      "access_token": "t", "enabled": True,
                                      "models": ["grok-4.6", "grok-4.5"]}]
    code = search_menu._code("xai")
    callback("srch:backend:" + code)
    detail = latest(tg)
    assert "🧠 模型: " in detail["text"]
    assert any(v["callback_data"] == "srch:models:" + code for v in buttons(detail))
    # The legacy free-text entry point is refused, not silently rendered; the
    # refusal is a result message rather than a page edit.
    callback("srch:model:" + code)
    refusal = [d["text"] for m, d in tg if m in ("sendMessage", "editMessageText")][-1]
    assert "请在「选择模型」中从账户模型目录选择" in refusal
    callback("srch:models:" + code)
    page = latest(tg)
    assert "搜索模型" in page["text"]
    assert "自动（默认 grok-4.6）" in page["text"]
    offered = [v for v in buttons(page) if str(v["callback_data"]).startswith("srch:setmodel:" + code + ":")
               and str(v["callback_data"]) != "srch:setmodel:" + code + ":"]
    assert len(offered) == 2
    callback(offered[0]["callback_data"])
    stored = control.get(ctx)["backends"]
    chosen = next(r for r in stored if r["id"] == "xai")["model"]
    assert chosen in ("grok-4.6", "grok-4.5")
    # The picker marks the stored value.
    callback("srch:models:" + code)
    assert any(v["text"] == f"✅ {chosen}" for v in buttons(latest(tg)))
    callback("srch:setmodel:" + code + ":")
    assert next(r for r in control.get(ctx)["backends"] if r["id"] == "xai")["model"] == ""


def test_sort_page_lists_configured_sources_and_reorders_them(tg, memory, control, ctx):
    """排序页与主页一致只列已配置来源；提交时仍补全完整集合。"""
    for kind in ("exa", "tavily"):
        code = search_menu._code(kind)
        callback("srch:addApiKeys:" + code)
        search_menu.handle_text_state(42, "search_input", "k1")
    before = [row["id"] for row in control.get(ctx)["backends"]]
    callback("srch:sort")
    page = latest(tg)
    assert "搜索来源排序" in page["text"]
    # 只列已配置的两个来源，而不是全部 7 个模板。
    assert "Exa" in page["text"] and "Tavily" in page["text"]
    assert "Anthropic" not in page["text"]
    numbered = [v for v in buttons(page) if v["callback_data"].startswith("srch:sortpick:")]
    assert len(numbered) == 2
    # 把第 2 个移到最前。
    # 列表中第 2 位是 Exa；置顶后 Exa 应排在 Tavily 之前。
    callback("srch:sortpick:0:2")
    callback("srch:sortmove:0:top")
    first_line = [ln for ln in latest(tg)["text"].splitlines() if ln.startswith("1. ")][0]
    assert "Exa" in first_line
    callback("srch:sortsave:0")
    after = [row["id"] for row in control.get(ctx)["backends"]]
    # 完整集合不丢：已配置的两个被重排，未配置的仍在。
    assert sorted(after) == sorted(before)
    assert after.index("exa") < after.index("tavily")
    assert "anthropic" in after and "openai" in after


def test_sort_cancel_leaves_order_untouched(tg, control, ctx):
    before = [row["id"] for row in control.get(ctx)["backends"]]
    callback("srch:sort")
    callback(f"srch:sortpick:0:{len(before)}")
    callback("srch:sortmove:0:bottom")
    callback("srch:show")
    assert [row["id"] for row in control.get(ctx)["backends"]] == before


def test_stats_and_log_render_from_the_dedicated_search_log(tg, control, ctx, monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="tavily", source_type="tavily",
        source_name="Tavily", operation="search", credential_kind="api_key",
        credential_label="Key #1", query="tg probe query",
    )
    log_db.finish_search_call(handle, status="success", elapsed_ms=250,
                              result_count=4, content_chars=100)
    callback("srch:stats:0:month")
    stats = latest(tg)["text"]
    assert "搜索来源统计" in stats
    assert "调用 1 次" in stats and "✅ 1" in stats and "平均 250 ms" in stats
    callback("srch:logs:0:month")
    logs = latest(tg)["text"]
    assert "搜索日志" in logs
    assert "tg probe query" in logs and "250 ms" in logs and "Key #1" in logs


def test_stats_hides_unknown_source_behind_its_recorded_name(tg, control, ctx, monkeypatch, tmp_path):
    import threading
    monkeypatch.setattr(log_db, "_log_dir", str(tmp_path))
    monkeypatch.setattr(log_db, "_local", threading.local())
    monkeypatch.setattr(log_db, "_write_conn_registry", {})
    monkeypatch.setattr(log_db, "_request_handles", {})
    handle = log_db.record_search_call(
        call_id="c", attempt_no=1, source_id="deleted-source", source_type="tavily",
        source_name="Removed Source", operation="search",
    )
    log_db.finish_search_call(handle, status="error", error_code="search_timeout", elapsed_ms=5)
    callback("srch:stats:0:month")
    assert "Removed Source" in latest(tg)["text"]


def test_sources_list_shows_only_configured_sources_with_icons(tg, memory, control, ctx):
    """未配置的模板来源不占位；配置后才出现，且四个 Key 来源图标可区分。"""
    callback("srch:show")
    assert "共 0 个来源" in latest(tg)["text"]
    assert "还没有已配置的搜索来源" in latest(tg)["text"]
    # 配置两个 Key 来源：只有它们出现。
    for kind, keys in (("anysearch", ["k1"]), ("tavily", ["k1", "k2"])):
        code = search_menu._code(kind)
        callback("srch:addApiKeys:" + code)
        search_menu.handle_text_state(42, "search_input", "\n".join(keys))
    callback("srch:show")
    page = latest(tg)
    assert "共 2 个来源" in page["text"]
    assert "✅ 可用 2" in page["text"]
    labels = [v["text"] for v in buttons(page)]
    assert any("AnySearch" in x for x in labels) and any("Tavily" in x for x in labels)
    assert not any("Brave" in x for x in labels)
    # Four key-based sources are visually distinguishable.
    assert len({ui.provider_btn_emoji(t) for t in ("anysearch", "tavily", "exa", "brave")}) == 4


def test_sources_list_keeps_provider_with_only_ineligible_accounts(tg, memory, control, ctx):
    """有账户但全部停用的来源仍须可见，否则无法再进入详情调整。"""
    memory.value["oauthAccounts"] = [
        {"provider": "openai", "email": "off@example.test", "workspace_id": "ws",
         "access_token": "t", "enabled": False}]
    callback("srch:show")
    page = latest(tg)
    assert "共 1 个来源" in page["text"]
    code = search_menu._code("openai")
    assert any(v["callback_data"] == "srch:backend:" + code for v in buttons(page))
    callback("srch:backend:" + code)
    assert "来源无可用账户" in latest(tg)["text"]


def test_two_ownership_switches_share_one_page(tg, control, ctx):
    callback("srch:modes")
    page = latest(tg)
    assert "普通 function" in page["text"] and "原生 hosted" in page["text"]
    marked = [v for v in buttons(page) if v["text"].startswith("✅ ")]
    assert len(marked) == 2
    assert any(v["callback_data"].startswith("srch:setmode:functionMode:") for v in buttons(page))
    assert any(v["callback_data"].startswith("srch:setmode:hostedMode:") for v in buttons(page))


def test_defaults_buttons_carry_current_values(tg, control, ctx):
    callback("srch:defaults")
    page = latest(tg)
    for key, expected in (("maxAttempts", "3"), ("timeoutSeconds", "10s"), ("maxResults", "8")):
        assert any(v["text"].endswith("：" + expected) for v in buttons(page)
                   if v["callback_data"] == "srch:edit:" + key)


def test_delete_confirmation_is_explicit_about_scope(tg, control, ctx):
    code = search_menu._code("tavily")
    callback("srch:delete:" + code)
    page = latest(tg)["text"]
    assert "确认删除搜索来源" in page
    assert "删除后不可恢复" in page
    assert "不影响 OAuth 账户或其它来源的 Key" in page




