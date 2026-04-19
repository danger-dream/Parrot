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


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, config, cooldown, log_db, probe, scorer, state_db,
    )
    from src.channel import registry, api_channel
    from src.telegram import bot, states, ui
    from src.telegram.menus import channel_menu, main as main_menu
    return {
        "affinity": affinity, "config": config, "cooldown": cooldown,
        "log_db": log_db, "probe": probe, "scorer": scorer, "state_db": state_db,
        "registry": registry, "api_channel": api_channel,
        "bot": bot, "states": states, "ui": ui,
        "channel_menu": channel_menu, "main_menu": main_menu,
    }


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
        l = self.by(method)
        return l[-1] if l else None

    def clear(self):
        self.calls.clear()


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    for mod_name in ("cooldown", "scorer", "affinity"):
        m[mod_name]._initialized = False
    m["cooldown"].init()
    m["scorer"].init()
    m["affinity"].init()

    def _r(c):
        c["channels"] = []
        c.setdefault("scoring", {})["explorationRate"] = 0.0
    m["config"].update(_r)
    m["states"].clear_all()
    # 测试场景下让"后台 worker 任务"立即同步执行，便于断言后续状态
    m["channel_menu"]._SYNC_SPAWN = True
    m["registry"].rebuild_from_config()


def _install_recorder(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


def _add_channel(m, name, url="https://example.com/v", models=None):
    models = models or [{"real": "glm-5", "alias": "glm-5"}]
    m["registry"].add_api_channel({
        "name": name, "baseUrl": url, "apiKey": "sk-testkey12345",
        "models": models, "cc_mimicry": True, "enabled": True,
    })


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
    rec.clear()
    m["channel_menu"].show(42, 100)
    last = rec.last("editMessageText")
    assert "共 2 个" in last["text"]
    assert "chA" in last["text"]
    assert "chB" in last["text"]
    print("  [PASS] list empty + populated")


def test_detail_renders(m):
    _setup(m)
    _add_channel(m, "chA", models=[
        {"real": "GLM-5", "alias": "glm-5"},
        {"real": "GLM-Turbo", "alias": "glm-turbo"},
    ])
    rec = _install_recorder(m)
    short = m["ui"].register_code("chA")
    m["channel_menu"].on_view(42, 100, "cb", short)
    last = rec.last("editMessageText")
    assert last
    text = last["text"]
    assert "chA" in text
    assert "GLM-5" in text and "glm-5" in text
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
    assert m["states"].get_state(42)["action"] == "ch_wiz_key"

    cm.wiz_on_key_input(42, "sk-testkey-longenough")
    assert m["states"].get_state(42)["action"] == "ch_wiz_models"

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
    cm.wiz_on_key_input(42, "sk-long-enough")
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
    cm.wiz_on_key_input(42, "sk-long-enough")
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
    cm.wiz_on_key_input(42, "sk-long-enough")
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
    # Key 校验
    cm.wiz_on_key_input(42, "x")
    assert m["states"].get_state(42)["action"] == "ch_wiz_key"
    cm.wiz_on_key_input(42, "sk-long-enough")
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
        test_detail_renders,
        test_toggle_clear_errors_clear_affinity,
        test_global_clear,
        test_delete_channel_cascades,
        test_add_wizard_happy_path_save_ok,
        test_add_wizard_partial_ok_saves_and_marks_failed_as_cooldown,
        test_add_wizard_all_fail_cannot_save,
        test_add_wizard_skip_test,
        test_add_wizard_cancel,
        test_add_wizard_input_validation,
        test_edit_fields,
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
