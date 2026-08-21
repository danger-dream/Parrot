"""M8 渠道菜单测试（不连 TG，不发真实 probe）。

覆盖：
  - 空列表 / 有渠道列表（含健康图标）
  - 详情展示（URL/Key 掩码/CC 伪装开关/模型列表/性能/亲和绑定数）
  - 启停切换（registry.update → config 变化）
  - 清错误（cooldown.clear + UI 刷新）
  - 清亲和（affinity.delete_by_channel）
  - 全局清错误 / 全局清亲和
  - 删除：二次确认 → 执行 + 级联清理
  - 添加向导：4 步输入 → 进入测试面板；跳过测试保存；正常测试保存
  - 测试面板：单模型 / 全部模型 probe 结果拼接 + 按钮状态
  - 编辑：名称 / URL / Key / 模型 / CC 伪装 逐项更新

所有 TG API 调用被 ApiRecorder 拦截。probe 被猴补为固定返回。
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import json
import os
import sys
import time
import pytest
from datetime import datetime, timedelta, timezone


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, channel_state, config, cooldown, log_db, probe, scorer, state_db,
    )
    from src.channel import registry, api_channel
    from src.telegram import bot, menu_cache, states, ui
    from src.telegram.menus import channel_menu, channel_wizard, main as main_menu, status_menu
    return {
        "affinity": affinity, "channel_state": channel_state,
        "config": config, "cooldown": cooldown,
        "log_db": log_db, "probe": probe, "scorer": scorer, "state_db": state_db,
        "registry": registry, "api_channel": api_channel,
        "bot": bot, "menu_cache": menu_cache, "states": states, "ui": ui,
        "channel_menu": channel_menu, "channel_wizard": channel_wizard,
        "main_menu": main_menu, "status_menu": status_menu,
    }


@pytest.fixture
def m():
    return _import_modules()


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        # 为 sendMessage 返回一个可用 message_id
        self._send_id = 1000

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        if method == "sendMessage":
            self._send_id += 1
            return {"ok": True, "result": {"message_id": self._send_id}}
        return {"ok": True, "result": {}}

    def by(self, method):
        return [d for m, d in self.calls if m == method]

    def last(self, method):
        deadline = time.time() + 5
        while True:
            calls = self.by(method)
            item = calls[-1] if calls else None
            text = str((item or {}).get("text") or "")
            loading = any(marker in text for marker in (
                "正在加载，完成后", "统计正在加载", "统计加载中", "历史统计加载中",
            ))
            if not loading or time.time() >= deadline:
                return item
            time.sleep(0.01)

    def clear(self):
        self.calls.clear()


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    m["state_db"].client_affinity_delete()
    conn = m["log_db"]._get_conn()
    conn.execute("DELETE FROM request_log")
    conn.execute("DELETE FROM request_detail")
    conn.execute("DELETE FROM retry_chain")
    conn.commit()
    for mod_name in ("cooldown", "scorer", "affinity"):
        m[mod_name]._initialized = False
    m["cooldown"].init()
    m["scorer"].init()
    m["affinity"].init()
    m["affinity"].client_init()

    def _r(c):
        c["channels"] = []
        c.setdefault("scoring", {})["explorationRate"] = 0.0
    m["config"].update(_r)
    m["states"].clear_all()
    # 测试场景下让"后台 worker 任务"立即同步执行，便于断言后续状态
    m["channel_menu"]._SYNC_SPAWN = True
    async def _discovery_unavailable(*args, **kwargs):
        raise m["channel_wizard"].ModelsDiscoveryError("测试中禁用外部网络")
    m["channel_wizard"].discover_models = _discovery_unavailable
    m["registry"].rebuild_from_config()


def _seed_stats_snapshots(m) -> None:
    """测试显式模拟中央调度器与详情队列已生成快照。"""
    cache = m["menu_cache"]
    since = cache.month_start_ts()
    cache.PERIOD_STATS.store(
        ("period", int(since)), m["log_db"].stats_period_snapshot(since),
    )
    for channel in m["registry"].all_channels():
        if channel.type == "api":
            cache.DETAIL_STATS.store(
                ("channel-model", channel.key, int(since)),
                m["log_db"].channel_model_stats(channel.key, since_ts=since),
            )


def _install_recorder(m):
    _seed_stats_snapshots(m)
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec




def _insert_channel_success(m, channel_name: str, request_id: str = "ch-r1", *, model: str = "glm-5") -> None:
    ld = m["log_db"]
    ld.insert_pending(request_id, "1.1.1.1", "k1", model, True,
                      msg_count=3, tool_count=0, request_headers={}, request_body={})
    ld.finish_success(
        request_id, f"api:{channel_name}", "api", model,
        input_tokens=100, output_tokens=20, cache_creation_tokens=10, cache_read_tokens=50,
        connect_ms=100, first_token_ms=300, total_ms=1500,
        retry_count=0, affinity_hit=1, response_body='{}', http_status=200,
    )


def _add_channel(m, name, url="https://example.com/v", models=None):
    models = models or [{"real": "glm-5", "alias": "glm-5"}]
    m["registry"].add_api_channel({
        "name": name, "baseUrl": url, "apiKey": "sk-testkey12345",
        "models": models, "cc_mimicry": True, "enabled": True,
    })


def test_existing_zhipu_1310_is_upgraded_to_stored_reset_on_startup(m):
    _setup(m)
    _add_channel(
        m,
        "智谱 Max",
        url="https://open.bigmodel.cn/api/anthropic",
        models=[{"real": "glm-5.2", "alias": "glm-5.2"}],
    )
    bjt = timezone(timedelta(hours=8))
    reset_dt = (datetime.now(bjt) + timedelta(days=2)).replace(microsecond=0)
    reset_ms = int(reset_dt.timestamp() * 1000)
    reset_text = reset_dt.strftime("%Y-%m-%d %H:%M:%S")
    detail = (
        'HTTP 429: {"type":"error","error":{"type":"rate_limit_error",'
        f'"code":"1310","message":"限额将在 {reset_text} 重置"}}}}'
    )
    m["state_db"].error_save(
        "api:智谱 Max", "glm-5.2", 1, int(time.time() * 1000) - 1, detail,
    )
    m["cooldown"]._initialized = False
    m["cooldown"].init()
    state = m["cooldown"].get_state("api:智谱 Max", "glm-5.2")
    assert state["cooldown_until"] == reset_ms
    assert m["cooldown"].is_blocked("api:智谱 Max", "glm-5.2")
    print("  [PASS] existing 1310 startup quota cooldown upgrade")


def test_quota_cooldown_is_explicit_in_channel_detail_and_status(m):
    _setup(m)
    _add_channel(
        m,
        "智谱 Max",
        url="https://open.bigmodel.cn/api/anthropic",
        models=[{"real": "glm-5.2", "alias": "glm-5.2"}],
    )
    ch = m["registry"].get_channel("api:智谱 Max")
    reset_ms = int(time.time() * 1000) + 2 * 24 * 60 * 60 * 1000
    detail = (
        'HTTP 429: {"type":"error","error":{"type":"rate_limit_error",'
        '"code":"1310","message":"[1310][您已达到每周/每月使用上限]"}}'
    )
    m["cooldown"].record_error(
        ch.key, "glm-5.2", detail, cooldown_until=reset_ms,
    )

    icon, health = m["channel_menu"]._channel_health(ch)
    assert icon == "🟠" and "配额冷却" in health
    lines = "\n".join(m["channel_menu"]._channel_model_lines(ch))
    assert "🟠 <b>配额冷却</b>" in lines
    assert "周/月额度已用尽（1310）" in lines
    assert "恢复前自动跳过本渠道模型" in lines
    assert "北京时间" in lines

    overview = m["status_menu"]._channel_overview()
    assert overview["anthropic"]["quota_cooling"] == 1
    problems = "\n".join(m["status_menu"]._problem_channels())
    assert "配额冷却" in problems
    assert "周/月额度耗尽（1310）" in problems
    print("  [PASS] quota cooldown channel/status UI")


# ─── Probe mock ─────────────────────────────────────────────────

def _set_probe_result(m, fn):
    """注入 probe.probe_with_progress 的实现。

    fn(channel, model) → (ok, elapsed, reason)
    """
    async def _fake(ch, model, progress_cb=None, timeout_s=None, progress_interval=10):
        if progress_cb is not None:
            try:
                await progress_cb(f"调用时长超过 10s...")
            except Exception:
                pass
        return fn(ch, model)
    m["probe"].probe_with_progress = _fake


# ─── Tests ───────────────────────────────────────────────────────

def test_list_empty_and_populated(m):
    _setup(m)
    rec = _install_recorder(m)
    m["channel_menu"].show(chat_id=42, message_id=100)
    last = rec.last("editMessageText")
    assert last and "共 0 个" in last["text"]
    assert "暂无渠道" in last["text"]

    _add_channel(m, "chA")
    _add_channel(m, "chB", models=[{"real": "gpt-4", "alias": "gpt-4"}])
    _insert_channel_success(m, "chA")
    _seed_stats_snapshots(m)
    rec.clear()
    m["channel_menu"].show(42, 100)
    last = rec.last("editMessageText")
    assert "共 2 个" in last["text"]
    assert "chA" in last["text"]
    assert "chB" in last["text"]
    assert "🏷️ 模型：<code>1</code> 个" in last["text"]
    assert "💎 Parrot 月度：↑ 160 · ↓ 20 · 缓存 50 (31.2%)" in last["text"]
    assert "📨 请求：1 次 · 成功率 100.0%" in last["text"]
    assert "⚡ TPS：平均" in last["text"]
    assert "💵 费用：$0.000" in last["text"]
    assert "💎 Parrot 月度：<i>暂无调用</i>" in last["text"]
    assert "≈" not in last["text"]
    print("  [PASS] list empty + populated")


def test_list_pagination_and_detail_return_page(m):
    _setup(m)
    for i in range(8):
        _add_channel(m, f"chan-{i:02d}")
    rec = _install_recorder(m)
    cm = m["channel_menu"]

    cm.show(42, 100, page=1)
    page1 = rec.last("editMessageText")
    assert page1 and "共 8 个 | 第 1/2 页" in page1["text"]
    assert "chan-00" in page1["text"] and "chan-03" in page1["text"]
    assert "chan-04" not in page1["text"]
    page1_btns = [
        b["callback_data"]
        for row in page1["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "ch:page:2" in page1_btns
    assert "ch:sort:1" in page1_btns
    view_payloads = [cb.split(":", 2)[2] for cb in page1_btns if cb.startswith("ch:view:")]
    assert view_payloads and all(payload.endswith(":1") for payload in view_payloads)

    rec.clear()
    cm.show(42, 100, page=2)
    page2 = rec.last("editMessageText")
    assert page2 and "共 8 个 | 第 2/2 页" in page2["text"]
    assert "chan-04" in page2["text"] and "chan-07" in page2["text"]
    assert "chan-00" not in page2["text"]
    page2_btns = [
        b["callback_data"]
        for row in page2["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "ch:page:1" in page2_btns
    assert "ch:sort:2" in page2_btns
    detail_payload = next(cb.split(":", 2)[2] for cb in page2_btns if cb.startswith("ch:view:"))
    assert detail_payload.endswith(":2")

    rec.clear()
    cm.on_view(42, 100, "cb", detail_payload)
    detail = rec.last("editMessageText")
    assert detail and "chan-04" in detail["text"]
    detail_btns = [
        b["callback_data"]
        for row in detail["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "ch:page:2" in detail_btns
    assert any(cb.startswith("ch:toggle:") and cb.endswith(":2") for cb in detail_btns)

    rec.clear()
    assert cm.handle_callback(42, 100, "cb", "ch:page:2") is True
    routed = rec.last("editMessageText")
    assert routed and "第 2/2 页" in routed["text"]
    print("  [PASS] list pagination + detail return page")


def test_channel_sort_reorders_config(m):
    _setup(m)
    for i in range(8):
        _add_channel(m, f"sort-{i:02d}")
    rec = _install_recorder(m)
    cm = m["channel_menu"]

    assert cm.handle_callback(42, 100, "cb", "ch:sort:2") is True
    sort_page = rec.last("editMessageText")
    assert sort_page and "渠道排序" in sort_page["text"]
    assert "sort-00" in sort_page["text"] and "sort-07" in sort_page["text"]

    rec.clear()
    assert cm.handle_callback(42, 100, "cb", "ch:sort_sel:8") is True
    selected = rec.last("editMessageText")
    btns = [
        b["text"]
        for row in selected["reply_markup"]["inline_keyboard"]
        for b in row
    ]
    assert "8 ✅" in btns

    rec.clear()
    assert cm.handle_callback(42, 100, "cb", "ch:sort_mv:top") is True
    moved = rec.last("editMessageText")
    assert moved and "sort-07" in moved["text"]
    first_order_line = next(line for line in moved["text"].splitlines() if "sort-" in line)
    assert "sort-07" in first_order_line, first_order_line

    rec.clear()
    assert cm.handle_callback(42, 100, "cb", "ch:sort_save") is True
    names = [c["name"] for c in m["config"].get()["channels"]]
    assert names[0] == "sort-07", names
    assert names[1:4] == ["sort-00", "sort-01", "sort-02"], names[:4]
    api_names = [ch.display_name for ch in m["registry"].all_channels() if ch.type == "api"]
    assert api_names[0] == "sort-07"
    saved = rec.last("editMessageText")
    assert saved and "已保存渠道排序" in saved["text"]
    saved_btns = [
        b["callback_data"]
        for row in saved["reply_markup"]["inline_keyboard"]
        for b in row if "callback_data" in b
    ]
    assert "ch:sort:2" in saved_btns and "ch:page:2" in saved_btns
    print("  [PASS] channel sort reorders config")


def test_channel_sort_keeps_name_first_with_compact_protocol(m):
    _setup(m)
    _add_channel(m, "智谱 Max")
    from src.openai.channel.api_channel import OpenAIApiChannel
    m["registry"].register_channel_factory("openai-chat", OpenAIApiChannel)
    m["registry"].register_channel_factory("openai-responses", OpenAIApiChannel)
    m["registry"].add_api_channel({
        "name": "CPA",
        "baseUrl": "https://example.com/v1",
        "apiKey": "sk-testkey12345",
        "protocol": "openai-responses",
        "models": [{"real": "gpt-5", "alias": "gpt-5"}],
        "enabled": True,
    })
    m["registry"].add_api_channel({
        "name": "Kimi",
        "baseUrl": "https://example.com/v1",
        "apiKey": "sk-testkey12345",
        "protocol": "openai-chat",
        "models": [{"real": "kimi", "alias": "kimi"}],
        "enabled": True,
    })
    rec = _install_recorder(m)
    cm = m["channel_menu"]

    assert cm.handle_callback(42, 100, "cb", "ch:sort:1") is True
    sort_page = rec.last("editMessageText")
    assert sort_page
    text = sort_page["text"]
    ui = m["ui"]

    assert ui.family_tag("openai") not in text
    assert "/v1/messages" not in text
    assert "/v1/chat/completions" not in text
    assert "/v1/responses" not in text

    zhipu = next(line for line in text.splitlines() if "智谱 Max" in line)
    cpa = next(line for line in text.splitlines() if ">CPA<" in line or "CPA" in line)
    kimi = next(line for line in text.splitlines() if "Kimi" in line)
    assert zhipu.index("智谱 Max") < zhipu.index("Anthropic"), zhipu
    assert cpa.index("CPA") < cpa.index("Responses"), cpa
    assert kimi.index("Kimi") < kimi.index("Chat"), kimi
    assert "Grok" not in text and "Cursor" not in text
    assert "🤖" not in text
    print("  [PASS] channel sort compact protocol, name first")


def test_detail_renders(m):
    _setup(m)
    _add_channel(m, "chA", models=[
        {"real": "GLM-5", "alias": "glm-5"},
        {"real": "GLM-Turbo", "alias": "glm-turbo"},
    ])
    _insert_channel_success(m, "chA", model="GLM-5")
    rec = _install_recorder(m)
    short = m["ui"].register_code("chA")
    m["channel_menu"].on_view(42, 100, "cb", short)
    last = rec.last("editMessageText")
    assert last
    text = last["text"]
    assert "chA" in text
    assert "GLM-5" in text and "glm-5" in text
    assert "缓存 50 (31.2%)" in text
    assert "\n    💵 $0.000" in text
    assert "缓存 50 (31.2%) · 💵" not in text
    assert "≈" not in text
    # API Key 掩码
    assert "sk-tes" in text and "***" in text
    # 按钮
    btns = [b["callback_data"] for row in last["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert any(x.startswith("ch:test:") for x in btns)
    assert any(x.startswith("ch:edit:") for x in btns)
    assert any(x.startswith("ch:clear_errors:") for x in btns)
    assert any(x.startswith("ch:clear_affinity:") for x in btns)
    assert any(x.startswith("ch:del:") for x in btns)
    print("  [PASS] detail renders")


def test_toggle_clear_errors_clear_affinity(m):
    _setup(m)
    _add_channel(m, "chA")
    rec = _install_recorder(m)
    short = m["ui"].register_code("chA")

    # toggle → 禁用
    m["channel_menu"].on_toggle(42, 100, "cb", short)
    assert any(c["name"] == "chA" and c["enabled"] is False for c in m["config"].get()["channels"])

    # 再 toggle → 启用
    m["channel_menu"].on_toggle(42, 100, "cb", short)
    assert any(c["name"] == "chA" and c["enabled"] is True for c in m["config"].get()["channels"])

    # 清错误（先注入）
    m["cooldown"].record_error("api:chA", "glm-5", "oops")
    assert m["cooldown"].is_blocked("api:chA", "glm-5")
    m["channel_menu"].on_clear_errors(42, 100, "cb", short)
    assert not m["cooldown"].is_blocked("api:chA", "glm-5")

    # 清亲和
    m["affinity"].upsert("fp-xx", "api:chA", "glm-5")
    assert m["affinity"].get("fp-xx") is not None
    m["channel_menu"].on_clear_affinity(42, 100, "cb", short)
    assert m["affinity"].get("fp-xx") is None
    print("  [PASS] toggle / clear errors / clear affinity")


def test_global_clear(m):
    _setup(m)
    _add_channel(m, "chA")
    _add_channel(m, "chB")
    rec = _install_recorder(m)
    m["cooldown"].record_error("api:chA", "glm-5", "x")
    m["cooldown"].record_error("api:chB", "glm-5", "x")
    m["affinity"].upsert("fp1", "api:chA", "glm-5")
    m["affinity"].upsert("fp2", "api:chB", "glm-5")

    m["channel_menu"].on_clear_errors_all(42, 100, "cb")
    assert not m["cooldown"].is_blocked("api:chA", "glm-5")
    assert not m["cooldown"].is_blocked("api:chB", "glm-5")

    m["channel_menu"].on_clear_affinity_all(42, 100, "cb")
    assert m["affinity"].count() == 0
    print("  [PASS] global clear errors + affinity")


def test_delete_channel_cascades(m):
    _setup(m)
    _add_channel(m, "chA")
    rec = _install_recorder(m)
    # 人为写入一些状态
    m["scorer"].record_success("api:chA", "glm-5", 100, 200, 1000)
    m["cooldown"].record_error("api:chA", "glm-5", "x")
    m["affinity"].upsert("fp1", "api:chA", "glm-5")
    short = m["ui"].register_code("chA")

    # 确认
    m["channel_menu"].on_delete_ask(42, 100, "cb", short)
    # 执行
    rec.clear()
    m["channel_menu"].on_delete_exec(42, 100, "cb", short)
    # 渠道应消失
    assert not any(c["name"] == "chA" for c in m["config"].get()["channels"])
    # state.db 全部清
    assert m["scorer"].get_stats("api:chA", "glm-5") is None
    assert not m["cooldown"].is_blocked("api:chA", "glm-5")
    assert m["affinity"].get("fp1") is None
    # state.db 持久层也空
    assert m["state_db"].perf_load("api:chA", "glm-5") is None
    print("  [PASS] delete cascades across state.db")


def test_wizard_save_recreates_deleted_same_name(m):
    """用户实测场景：删除后，TG 向导可立即保存同显示名称的新 generation。"""
    _setup(m)
    _install_recorder(m)
    _add_channel(m, "智谱 Max")
    old_channel = m["registry"].get_channel("api:智谱 Max")
    old_generation = m["channel_state"].effect_key(old_channel)
    _insert_channel_success(m, "智谱 Max", "before-recreate")
    assert m["log_db"].tokens_for_channel("api:智谱 Max", 0)["total"] == 1
    assert m["registry"].delete_api_channel("智谱 Max")

    data = {
        "name": "智谱 Max",
        "baseUrl": "https://open.bigmodel.cn/api/anthropic",
        "apiPath": None,
        "apiKey": "sk-testkey-longenough",
        "protocol": "anthropic",
        "models": [{"real": "glm-5", "alias": "glm-5"}],
        "cc_mimicry": True,
        "test_results": {"glm-5": (True, 12, None)},
    }
    m["states"].set_state(42, "ch_wiz_test", data)
    m["channel_menu"].wiz_save(42, 100, "cb")

    new_channel = m["registry"].get_channel("api:智谱 Max")
    assert new_channel is not None
    new_generation = m["channel_state"].effect_key(new_channel)
    assert new_generation != old_generation
    _insert_channel_success(m, "智谱 Max", "after-recreate")
    totals = m["log_db"].tokens_for_channel("api:智谱 Max", 0)
    models = m["log_db"].channel_model_stats("api:智谱 Max", 0)
    assert totals["total"] == 2
    assert totals["input"] == 200
    assert len(models) == 1 and models[0]["total"] == 2
    # generation/state key is internal-only and must never become a public stats dimension.
    assert m["log_db"].tokens_for_channel(old_generation, 0)["total"] == 0
    assert m["log_db"].tokens_for_channel(new_generation, 0)["total"] == 0
    assert m["states"].get_state(42) is None
    print("  [PASS] TG wizard delete → same-name save keeps api:<name> historical stats")


def test_add_wizard_happy_path_save_ok(m):
    """完整向导：name → URL → Key → models → 测试（mock 成功）→ 保存。"""
    _setup(m)
    rec = _install_recorder(m)
    cm = m["channel_menu"]

    # 进入向导
    cm.wiz_start(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "ch_wiz_name"

    cm.wiz_on_name_input(42, "智谱 Coding Max")
    assert m["states"].get_state(42)["action"] == "ch_wiz_url"

    cm.wiz_on_url_input(42, "https://coding.zhipu.com/anthropic")
    assert m["states"].get_state(42)["action"] == "ch_wiz_protocol"

    # 新增：选择协议步骤（MS-1 引入），默认 anthropic 走原来的 CC 伪装路径
    cm.wiz_on_protocol_select(42, 100, "cb", "anthropic")
    assert m["states"].get_state(42)["action"] == "ch_wiz_key"

    cm.wiz_on_key_input(42, "sk-testkey-longenough")
    # 自动发现失败后直接进入可输入状态，不再要求额外点击“手动输入”。
    manual_state = m["states"].get_state(42)
    assert manual_state["action"] == "ch_wiz_models"
    assert manual_state["data"]["models_source"] == "manual"
    assert manual_state["data"]["discovery_retry_available"] is True

    cm.wiz_on_models_input(42, "GLM-5:glm-5, GLM-Turbo:glm-turbo")
    assert m["states"].get_state(42)["action"] == "ch_wiz_test"

    # 注入 probe 全部成功
    _set_probe_result(m, lambda ch, model: (True, 123, None))

    rec.clear()
    cm.wiz_test_all(42, 100, "cb")
    state = m["states"].get_state(42)
    assert state and state["action"] == "ch_wiz_test"
    results = state["data"]["test_results"]
    assert len(results) == 2
    assert all(r[0] for r in results.values())

    # 保存
    rec.clear()
    cm.wiz_save(42, 100, "cb")
    assert m["states"].get_state(42) is None
    cfg = m["config"].get()
    assert any(c["name"] == "智谱 Coding Max" for c in cfg["channels"])
    added = next(c for c in cfg["channels"] if c["name"] == "智谱 Coding Max")
    assert added["baseUrl"] == "https://coding.zhipu.com/anthropic"
    assert len(added["models"]) == 2
    print("  [PASS] wizard add (all tests ok) → save")


def test_add_wizard_partial_ok_saves_and_marks_failed_as_cooldown(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["channel_menu"]

    cm.wiz_start(42, 100, "cb")
    cm.wiz_on_name_input(42, "mixed")
    cm.wiz_on_url_input(42, "https://m.example.com/v")
    cm.wiz_on_protocol_select(42, 100, "cb", "anthropic")
    cm.wiz_on_key_input(42, "sk-long-enough")
    m["channel_wizard"].wiz_manual(42, 100, "cb")
    cm.wiz_on_models_input(42, "A:a, B:b")

    # a 成功 b 失败
    def _probe(ch, model):
        return (True, 100, None) if model == "A" else (False, 50, "connect refused")
    _set_probe_result(m, _probe)

    cm.wiz_test_all(42, 100, "cb")
    cm.wiz_save(42, 100, "cb")

    assert any(c["name"] == "mixed" for c in m["config"].get()["channels"])
    # B 应进入永久冷却
    assert m["cooldown"].is_blocked("api:mixed", "B")
    # A 不应在冷却
    assert not m["cooldown"].is_blocked("api:mixed", "A")
    print("  [PASS] wizard save: failed models marked cooldown")


def test_add_wizard_all_fail_cannot_save(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["channel_menu"]

    cm.wiz_start(42, 100, "cb")
    cm.wiz_on_name_input(42, "bad")
    cm.wiz_on_url_input(42, "https://b.example.com/v")
    cm.wiz_on_protocol_select(42, 100, "cb", "anthropic")
    cm.wiz_on_key_input(42, "sk-long-enough")
    m["channel_wizard"].wiz_manual(42, 100, "cb")
    cm.wiz_on_models_input(42, "X")
    _set_probe_result(m, lambda c, mdl: (False, 50, "down"))

    cm.wiz_test_all(42, 100, "cb")
    rec.clear()
    cm.wiz_save(42, 100, "cb")
    # 应弹出告警（answerCallbackQuery show_alert）
    ans = rec.last("answerCallbackQuery")
    assert ans and "至少一个" in ans.get("text", "")
    # 渠道未入 config
    assert not any(c["name"] == "bad" for c in m["config"].get()["channels"])
    print("  [PASS] wizard cannot save when all tests fail")


def test_add_wizard_skip_test(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["channel_menu"]
    cm.wiz_start(42, 100, "cb")
    cm.wiz_on_name_input(42, "skipme")
    cm.wiz_on_url_input(42, "https://s.example.com/v")
    cm.wiz_on_protocol_select(42, 100, "cb", "anthropic")
    cm.wiz_on_key_input(42, "sk-long-enough")
    m["channel_wizard"].wiz_manual(42, 100, "cb")
    cm.wiz_on_models_input(42, "p1, p2:alias2")

    cm.wiz_skip_test(42, 100, "cb")
    assert m["states"].get_state(42) is None
    added = next(c for c in m["config"].get()["channels"] if c["name"] == "skipme")
    assert len(added["models"]) == 2
    # 没有冷却
    assert not m["cooldown"].is_blocked("api:skipme", "p1")
    print("  [PASS] wizard skip_test")


def test_add_wizard_cancel(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["channel_menu"]
    cm.wiz_start(42, 100, "cb")
    cm.wiz_on_name_input(42, "willcancel")
    cm.wiz_cancel(42, 100, "cb")
    assert m["states"].get_state(42) is None
    assert not any(c["name"] == "willcancel" for c in m["config"].get()["channels"])
    print("  [PASS] wizard cancel")


def test_add_wizard_input_validation(m):
    _setup(m)
    rec = _install_recorder(m)
    cm = m["channel_menu"]
    cm.wiz_start(42, 100, "cb")

    # 空名
    cm.wiz_on_name_input(42, "")
    assert m["states"].get_state(42)["action"] == "ch_wiz_name"
    # 重名
    _add_channel(m, "dup")
    cm.wiz_on_name_input(42, "dup")
    assert m["states"].get_state(42)["action"] == "ch_wiz_name"
    # 合法
    cm.wiz_on_name_input(42, "new-one")
    # URL 校验
    cm.wiz_on_url_input(42, "ftp://bad")
    assert m["states"].get_state(42)["action"] == "ch_wiz_url"
    cm.wiz_on_url_input(42, "https://ok.example.com")
    # 协议选择（MS-1 引入）：默认 anthropic
    cm.wiz_on_protocol_select(42, 100, "cb", "anthropic")
    # Key 校验
    cm.wiz_on_key_input(42, "x")
    assert m["states"].get_state(42)["action"] == "ch_wiz_key"
    cm.wiz_on_key_input(42, "sk-long-enough")
    m["channel_wizard"].wiz_manual(42, 100, "cb")
    # Models 校验
    cm.wiz_on_models_input(42, "a:x, b:x")  # 重复别名
    assert m["states"].get_state(42)["action"] == "ch_wiz_models"
    cm.wiz_on_models_input(42, "a,b")
    assert m["states"].get_state(42)["action"] == "ch_wiz_test"
    print("  [PASS] wizard input validation")


def test_edit_fields(m):
    _setup(m)
    _add_channel(m, "oldname", url="https://old.example.com/v",
                 models=[{"real": "GLM-5", "alias": "glm-5"}])
    rec = _install_recorder(m)
    cm = m["channel_menu"]
    short = m["ui"].register_code("oldname")

    # 修改名称
    m["states"].set_state(42, "ch_edit_name", {"short": short})
    cm.handle_edit_text(42, "ch_edit_name", "newname")
    assert any(c["name"] == "newname" for c in m["config"].get()["channels"])

    # 改名后短码也要重新找
    short2 = m["ui"].register_code("newname")
    # URL
    m["states"].set_state(42, "ch_edit_url", {"short": short2})
    cm.handle_edit_text(42, "ch_edit_url", "https://new.example.com/v")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "newname")
    assert entry["baseUrl"] == "https://new.example.com/v"

    # Key
    m["states"].set_state(42, "ch_edit_key", {"short": short2})
    cm.handle_edit_text(42, "ch_edit_key", "sk-newkey-longer")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "newname")
    assert entry["apiKey"] == "sk-newkey-longer"

    # Models
    m["states"].set_state(42, "ch_edit_models", {"short": short2})
    cm.handle_edit_text(42, "ch_edit_models", "ModelA, ModelB:mb, ModelC:mc")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "newname")
    assert len(entry["models"]) == 3
    assert entry["models"][1] == {"real": "ModelB", "alias": "mb"}

    # CC 伪装切换
    cm.on_edit_cc_toggle(42, 100, "cb", short2)
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "newname")
    assert entry["cc_mimicry"] is False
    print("  [PASS] edit name/url/key/models/cc_mimicry")


def test_channel_compatibility_menu_and_model_scope(m):
    _setup(m)
    _add_channel(
        m,
        "compat",
        models=[
            {"real": "claude-fable-5", "alias": "fable"},
            {"real": "claude-haiku-4-5-20251001", "alias": "haiku"},
        ],
    )
    rec = _install_recorder(m)
    cm = m["channel_menu"]
    short = m["ui"].register_code("compat")

    cm.on_edit_menu(42, 100, "cb", short)
    edit = rec.last("editMessageText")
    rows = edit["reply_markup"]["inline_keyboard"]
    assert any(
        len(row) == 2
        and row[0]["callback_data"].startswith("ch:emax:")
        and row[1]["callback_data"].startswith("ch:cmp:")
        for row in rows
    )
    assert not any(
        button["callback_data"].startswith(("ch:eomit:", "ch:ethink:"))
        for row in rows for button in row if "callback_data" in button
    )

    rec.clear()
    cm.on_compat_menu(42, 100, "cb", short)
    compat = rec.last("editMessageText")
    assert "渠道兼容配置" in compat["text"]
    callbacks = [
        button["callback_data"]
        for row in compat["reply_markup"]["inline_keyboard"]
        for button in row if "callback_data" in button
    ]
    assert f"ch:cf:{short}:1m" in callbacks
    assert f"ch:cf:{short}:fast" in callbacks

    cm.on_compat_feature_mode(42, 100, "cb", short, "1m", "force")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "compat")
    assert entry["context1mMode"] == "force"
    assert entry["context1mModels"] == []

    # “全部模型”下点一个模型，转成只强制该真实上游模型。
    cm.on_compat_feature_toggle_model(42, 100, "cb", short, "1m", "0")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "compat")
    assert entry["context1mModels"] == ["claude-fable-5"]
    cm.on_compat_feature_toggle_model(42, 100, "cb", short, "1m", "1")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "compat")
    assert entry["context1mModels"] == ["claude-fable-5", "claude-haiku-4-5-20251001"]
    cm.on_compat_feature_all_models(42, 100, "cb", short, "1m")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "compat")
    assert entry["context1mModels"] == []

    cm.on_compat_feature_mode(42, 100, "cb", short, "fast", "force")
    cm.on_compat_feature_toggle_model(42, 100, "cb", short, "fast", "1")
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "compat")
    assert entry["fastMode"] == "force"
    assert entry["fastModels"] == ["claude-haiku-4-5-20251001"]

    cm.on_edit_omit_temperature_toggle(42, 100, "cb", short)
    cm.on_edit_omit_thinking_toggle(42, 100, "cb", short)
    entry = next(c for c in m["config"].get()["channels"] if c["name"] == "compat")
    assert entry["omitTemperature"] is True
    assert entry["omitThinking"] is True

    # OpenAI 渠道也有 Fast 和通用剔除，但不显示 1M 入口。
    from src.openai.channel.api_channel import OpenAIApiChannel
    m["registry"].register_channel_factory("openai-chat", OpenAIApiChannel)
    m["registry"].add_api_channel({
        "name": "openai-compat",
        "baseUrl": "https://example.com/v1",
        "apiKey": "sk-testkey12345",
        "protocol": "openai-chat",
        "models": [{"real": "gpt-5", "alias": "gpt-5"}],
        "enabled": True,
    })
    openai_short = m["ui"].register_code("openai-compat")
    rec.clear()
    cm.on_compat_menu(42, 100, "cb", openai_short)
    openai_menu = rec.last("editMessageText")
    callbacks = [
        button["callback_data"]
        for row in openai_menu["reply_markup"]["inline_keyboard"]
        for button in row if "callback_data" in button
    ]
    assert f"ch:cf:{openai_short}:fast" in callbacks
    assert f"ch:cf:{openai_short}:1m" not in callbacks
    print("  [PASS] channel compatibility menu/model scope")


def test_router_dispatch(m):
    _setup(m)
    _add_channel(m, "routed")
    rec = _install_recorder(m)
    m["ui"].configure("TOKEN", [42])

    # menu:channel
    m["bot"]._handle_callback({
        "id": "cb1", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "menu:channel",
    })
    assert rec.last("editMessageText") is not None

    # ch:view:<short>
    short = m["ui"].register_code("routed")
    rec.clear()
    m["bot"]._handle_callback({
        "id": "cb2", "message": {"chat": {"id": 42}, "message_id": 100},
        "data": f"ch:view:{short}",
    })
    last = rec.last("editMessageText")
    assert last and "routed" in last["text"]

    # /channels 命令
    rec.clear()
    m["bot"]._handle_message({"chat": {"id": 42}, "text": "/channels"})
    assert rec.last("sendMessage") is not None
    print("  [PASS] router dispatch: menu:channel / ch:view / /channels")


def test_test_panel_single(m):
    _setup(m)
    _add_channel(m, "tchan",
                 models=[{"real": "X1", "alias": "x"}, {"real": "Y2", "alias": "y"}])
    rec = _install_recorder(m)
    short = m["ui"].register_code("tchan")
    cm = m["channel_menu"]

    # 测试面板（按钮列表）
    cm.on_test_panel(42, 100, "cb", short)
    edit = rec.last("editMessageText")
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"] for b in row if "callback_data" in b]
    assert any(x.startswith("ch:t1:") for x in btns)
    assert any(x.startswith("ch:tall:") for x in btns)

    # 测试单模型
    _set_probe_result(m, lambda ch, mdl: (True, 42, None))
    rec.clear()
    cm.on_test_single(42, 100, "cb", short, "0")
    # 应 sendMessage 一条，editMessage 一条（进度）+ 一条（结果）
    assert len(rec.by("sendMessage")) == 1
    assert len(rec.by("editMessageText")) >= 2
    print("  [PASS] test panel single")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()
    m["log_db"].init()

    orig_cfg = json.loads(json.dumps(m["config"].get()))
    orig_probe = m["probe"].probe_with_progress

    tests = [
        test_list_empty_and_populated,
        test_list_pagination_and_detail_return_page,
        test_channel_sort_reorders_config,
        test_detail_renders,
        test_existing_zhipu_1310_is_upgraded_to_stored_reset_on_startup,
        test_quota_cooldown_is_explicit_in_channel_detail_and_status,
        test_toggle_clear_errors_clear_affinity,
        test_global_clear,
        test_delete_channel_cascades,
        test_wizard_save_recreates_deleted_same_name,
        test_add_wizard_happy_path_save_ok,
        test_add_wizard_partial_ok_saves_and_marks_failed_as_cooldown,
        test_add_wizard_all_fail_cannot_save,
        test_add_wizard_skip_test,
        test_add_wizard_cancel,
        test_add_wizard_input_validation,
        test_edit_fields,
        test_channel_compatibility_menu_and_model_scope,
        test_router_dispatch,
        test_test_panel_single,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m)
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig_cfg)
        m["config"].update(_restore)
        m["probe"].probe_with_progress = orig_probe
        m["states"].clear_all()

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())


def test_supported_provider_usage_list_detail_are_cache_only(m, monkeypatch):
    _setup(m)
    m["registry"].add_api_channel({
        "name": "DeepSeek quota", "baseUrl": "https://api.deepseek.com",
        "apiKey": "sk-live-not-used", "providerId": "deepseek",
        "providerPresetId": "standard", "models": [{"real": "deepseek-chat", "alias": "deepseek-chat"}],
        "cc_mimicry": False, "enabled": True,
    })
    ch = m["registry"].get_channel("api:DeepSeek quota")
    pu = m["channel_menu"].provider_usage
    monkeypatch.setattr(pu, "cached", lambda channel: {"status": "fresh", "fetched_at": 1000,
        "snapshot": {"source": "deepseek", "balances": [{"label": "总余额", "value": "8.80", "currency": "CNY"}],
                     "windows": [], "counters": [], "notices": ["账户可用"], "partial": False}})
    called = []
    monkeypatch.setattr(pu, "schedule_refresh", lambda channel, **kwargs: called.append((channel.key, kwargs)) or True)
    text, _ = m["channel_menu"]._list_text_and_kb(snapshot={"by_channel": {}})
    assert "💰 总余额：<b>8.8 CNY</b>" in text
    detail, kb = m["channel_menu"]._detail_text_and_kb(ch.display_name, model_stats=[])
    assert "上游账户额度" in detail and "Parrot 本地统计" in detail
    assert detail.index("上游账户额度") < detail.index("Parrot 本地统计")
    callbacks = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    usage_cb = next(x for x in callbacks if x.startswith("ch:usage:"))
    assert len(usage_cb.encode()) <= 64 and "deepseek" not in usage_cb and "sk-live" not in usage_cb
    assert called == []  # pure render path never performs or schedules network itself
