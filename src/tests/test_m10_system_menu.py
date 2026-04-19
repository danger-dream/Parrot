"""M10 系统设置菜单测试。

覆盖：
  - 主设置页渲染（7 大项）
  - 超时 / 错误阶梯 输入：合法 / 非法
  - 评分参数：4 字段各自修改（范围校验）
  - 亲和参数：2 字段
  - CCH：disabled/dynamic/static 切换 + static 值编辑（hex 校验）
  - channelSelection：smart/order 切换
  - 首包黑名单：加/删默认 + 加渠道专属
  - 整路径路由 + /settings 命令
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
    from src import config, state_db
    from src.telegram import bot, states, ui
    from src.telegram.menus import system_menu
    return {
        "config": config, "state_db": state_db,
        "bot": bot, "states": states, "ui": ui,
        "system_menu": system_menu,
    }


class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {}}
    def by(self, m): return [d for mm, d in self.calls if mm == m]
    def last(self, m):
        l = self.by(m); return l[-1] if l else None
    def clear(self): self.calls.clear()


def _reset(m):
    m["state_db"].init()
    m["states"].clear_all()


def _install(m):
    rec = ApiRecorder()
    m["ui"].api = rec
    return rec


# ─── Tests ───────────────────────────────────────────────────────

def test_main_page(m):
    _reset(m)
    rec = _install(m)
    m["system_menu"].show(42, 100, "cb")
    edit = rec.last("editMessageText")
    text = edit["text"]
    # 主页应含所有项
    for s in ("超时", "错误阶梯", "评分", "亲和", "CCH", "渠道选择", "黑名单"):
        assert s in text, s
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"]
            for b in row if "callback_data" in b]
    expected = {"sys:show:timeouts", "sys:show:errwin", "sys:show:scoring",
                "sys:show:affinity", "sys:show:cch", "sys:show:chsel",
                "sys:show:blacklist", "menu:main"}
    for e in expected:
        assert e in btns, f"missing btn {e}"
    print("  [PASS] main settings page")


def test_timeouts_edit(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._show_timeouts(42, 100, "cb")
    assert rec.last("editMessageText") is not None

    sm._edit_timeouts(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "sys_timeouts"

    # 非法输入
    sm._on_timeouts_input(42, "10,30,30")
    assert m["states"].get_state(42) is not None
    sm._on_timeouts_input(42, "a,b,c,d")
    assert m["states"].get_state(42) is not None
    sm._on_timeouts_input(42, "-1,30,30,600")
    assert m["states"].get_state(42) is not None

    # 合法输入
    sm._on_timeouts_input(42, "11, 31, 32, 650")
    assert m["states"].get_state(42) is None
    t = m["config"].get()["timeouts"]
    assert t["connect"] == 11 and t["firstByte"] == 31 and t["idle"] == 32 and t["total"] == 650
    print("  [PASS] timeouts edit")


def test_errwin_edit(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._show_errwin(42, 100, "cb")
    sm._edit_errwin(42, 100, "cb")
    # 非法
    sm._on_errwin_input(42, "")
    assert m["states"].get_state(42) is not None
    sm._on_errwin_input(42, "a,b")
    assert m["states"].get_state(42) is not None
    sm._on_errwin_input(42, "1,-1")
    assert m["states"].get_state(42) is not None
    # 合法
    sm._on_errwin_input(42, "2, 5, 10, 30, 0")
    assert m["config"].get()["errorWindows"] == [2, 5, 10, 30, 0]
    print("  [PASS] errwin edit")


def test_scoring_fields(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    sm._show_scoring(42, 100, "cb")

    # emaAlpha float
    sm._edit_scoring(42, 100, "cb", "emaAlpha")
    assert m["states"].get_state(42)["action"] == "sys_scoring:emaAlpha"
    sm._on_scoring_input(42, "sys_scoring:emaAlpha", "0.33")
    assert m["config"].get()["scoring"]["emaAlpha"] == 0.33

    # recentWindow int 范围校验
    sm._edit_scoring(42, 100, "cb", "recentWindow")
    sm._on_scoring_input(42, "sys_scoring:recentWindow", "-1")
    assert m["states"].get_state(42) is not None  # 未通过，状态仍在
    sm._on_scoring_input(42, "sys_scoring:recentWindow", "42")
    assert m["config"].get()["scoring"]["recentWindow"] == 42

    # errorPenaltyFactor int
    sm._edit_scoring(42, 100, "cb", "errorPenaltyFactor")
    sm._on_scoring_input(42, "sys_scoring:errorPenaltyFactor", "10")
    assert m["config"].get()["scoring"]["errorPenaltyFactor"] == 10

    # explorationRate float 范围
    sm._edit_scoring(42, 100, "cb", "explorationRate")
    sm._on_scoring_input(42, "sys_scoring:explorationRate", "0.1")
    assert m["config"].get()["scoring"]["explorationRate"] == 0.1
    print("  [PASS] scoring 4 fields + range checks")


def test_affinity_fields(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]
    sm._show_affinity(42, 100, "cb")

    sm._edit_affinity(42, 100, "cb", "ttlMinutes")
    sm._on_affinity_input(42, "sys_affinity:ttlMinutes", "0")
    assert m["states"].get_state(42) is not None
    sm._on_affinity_input(42, "sys_affinity:ttlMinutes", "45")
    assert m["config"].get()["affinity"]["ttlMinutes"] == 45

    sm._edit_affinity(42, 100, "cb", "threshold")
    sm._on_affinity_input(42, "sys_affinity:threshold", "2.5")
    assert m["config"].get()["affinity"]["threshold"] == 2.5
    print("  [PASS] affinity fields")


def test_cch_mode_switch_and_static(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._show_cch(42, 100, "cb")

    sm._on_cch_set(42, 100, "cb", "dynamic")
    assert m["config"].get()["cchMode"] == "dynamic"

    sm._on_cch_set(42, 100, "cb", "static")
    assert m["config"].get()["cchMode"] == "static"

    # 编辑静态值
    sm._edit_cch_static(42, 100, "cb")
    sm._on_cch_static_input(42, "bad")  # 太短
    assert m["states"].get_state(42) is not None
    sm._on_cch_static_input(42, "abcde1")  # 太长
    assert m["states"].get_state(42) is not None
    sm._on_cch_static_input(42, "ghhij")  # 非 hex
    assert m["states"].get_state(42) is not None
    sm._on_cch_static_input(42, "a1b2c")
    assert m["config"].get()["cchStaticValue"] == "a1b2c"

    sm._on_cch_set(42, 100, "cb", "disabled")
    assert m["config"].get()["cchMode"] == "disabled"

    # 无效模式
    sm._on_cch_set(42, 100, "cb", "bogus")
    assert m["config"].get()["cchMode"] == "disabled"
    print("  [PASS] CCH mode + static value")


def test_chsel_switch(m):
    _reset(m)
    rec = _install(m)
    sm = m["system_menu"]

    sm._on_chsel_set(42, 100, "cb", "order")
    assert m["config"].get()["channelSelection"] == "order"

    sm._on_chsel_set(42, 100, "cb", "smart")
    assert m["config"].get()["channelSelection"] == "smart"

    sm._on_chsel_set(42, 100, "cb", "bogus")
    assert m["config"].get()["channelSelection"] == "smart"
    print("  [PASS] channelSelection switch")


def test_blacklist_default_add_and_remove(m):
    _reset(m)
    # 清空黑名单
    m["config"].update(lambda c: c.__setitem__("contentBlacklist", {"default": [], "byChannel": {}}))
    rec = _install(m)
    sm = m["system_menu"]

    sm._bl_add_default(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "sys_bl_add_default"
    sm._on_bl_add_default_input(42, "policy_violation")
    assert "policy_violation" in m["config"].get()["contentBlacklist"]["default"]
    assert m["states"].get_state(42) is None

    # 添加第二个
    sm._bl_add_default(42, 100, "cb")
    sm._on_bl_add_default_input(42, "content_filter")
    defaults = m["config"].get()["contentBlacklist"]["default"]
    assert defaults == ["policy_violation", "content_filter"]

    # 空输入被拒
    sm._bl_add_default(42, 100, "cb")
    sm._on_bl_add_default_input(42, "   ")
    assert m["states"].get_state(42) is not None

    # 删除：取列表 → 删其中一个
    rec.clear()
    sm._bl_del_default(42, 100, "cb")
    edit = rec.last("editMessageText")
    btns = [b["callback_data"] for row in edit["reply_markup"]["inline_keyboard"]
            for b in row if "callback_data" in b]
    exec_cbs = [b for b in btns if b.startswith("sys:bl_del_exec:")]
    assert len(exec_cbs) == 2
    # 执行删除第一个
    short = exec_cbs[0].split(":", 2)[2]
    sm._bl_del_exec(42, 100, "cb", short)
    # 列表缩减
    remaining = m["config"].get()["contentBlacklist"]["default"]
    assert len(remaining) == 1
    print("  [PASS] blacklist default add+remove")


def test_blacklist_by_channel(m):
    _reset(m)
    m["config"].update(lambda c: c.__setitem__("contentBlacklist", {"default": [], "byChannel": {}}))
    rec = _install(m)
    sm = m["system_menu"]

    sm._bl_add_ch(42, 100, "cb")
    assert m["states"].get_state(42)["action"] == "sys_bl_add_ch"

    # 非法格式
    sm._on_bl_add_ch_input(42, "no-equal-sign")
    assert m["states"].get_state(42) is not None

    # 合法
    sm._on_bl_add_ch_input(42, "智谱Coding Plan Max=dangerous_term")
    by_ch = m["config"].get()["contentBlacklist"]["byChannel"]
    assert "智谱Coding Plan Max" in by_ch
    assert by_ch["智谱Coding Plan Max"] == ["dangerous_term"]
    print("  [PASS] blacklist byChannel add")


def test_router_dispatch(m):
    _reset(m)
    rec = _install(m)
    m["ui"].configure("TOKEN", [42])

    m["bot"]._handle_callback({
        "id": "cb1", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "menu:settings",
    })
    assert rec.last("editMessageText") is not None

    rec.clear()
    m["bot"]._handle_callback({
        "id": "cb2", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "sys:show:cch",
    })
    assert rec.last("editMessageText") is not None

    rec.clear()
    m["bot"]._handle_message({"chat": {"id": 42}, "text": "/settings"})
    assert rec.last("sendMessage") is not None
    print("  [PASS] router + /settings")


def test_text_state_dispatch_to_system(m):
    _reset(m)
    rec = _install(m)
    m["ui"].configure("TOKEN", [42])
    m["states"].set_state(42, "sys_timeouts")

    m["bot"]._handle_message({"chat": {"id": 42}, "text": "15,40,45,700"})
    assert m["states"].get_state(42) is None
    assert m["config"].get()["timeouts"]["connect"] == 15
    print("  [PASS] bot text → system state handler")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()
    orig_cfg = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_main_page,
        test_timeouts_edit,
        test_errwin_edit,
        test_scoring_fields,
        test_affinity_fields,
        test_cch_mode_switch_and_static,
        test_chsel_switch,
        test_blacklist_default_add_and_remove,
        test_blacklist_by_channel,
        test_router_dispatch,
        test_text_state_dispatch_to_system,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m); passed += 1
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
        m["states"].clear_all()

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
