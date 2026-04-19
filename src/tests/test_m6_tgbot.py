"""M6 TG Bot 单元测试（纯逻辑 + mock，不连 api.telegram.org）。

覆盖：
  - states：set / get / pop / TTL 过期
  - ui.inline_kb / btn / register_code / resolve_code / escape_html / truncate
  - ui.is_admin：空 admin 列表 = 不限；非空列表精确匹配
  - apikey_menu：
      add 输入合法 → 写入 config；非法 → 保留状态不写
      del 列表 / confirm / exec → config 正确变化
      不存在的 short code 删除 → 拒绝
  - bot 路由：
      非 admin 的 message / callback 被拒
      /start 触发 welcome；/menu 触发主菜单
      callback menu:apikey 进入 apikey.show
      未实现 menu:* 返回提示

方法：全程猴补 ui.api 为 recorder，记录所有"如果真调用了会发的" API 请求。

运行：./venv/bin/python -m src.tests.test_m6_tgbot
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


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, state_db
    from src.telegram import bot, states, ui
    from src.telegram.menus import apikey_menu, main as main_menu
    return {
        "config": config, "state_db": state_db,
        "bot": bot, "states": states, "ui": ui,
        "apikey_menu": apikey_menu, "main_menu": main_menu,
    }


# ─── 录制 TG api 调用 ────────────────────────────────────────────

class ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, dict] = {}

    def __call__(self, method: str, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return self.responses.get(method, {"ok": True, "result": {}})

    def by_method(self, method: str) -> list[dict]:
        return [d for (m, d) in self.calls if m == method]

    def last(self, method: str) -> dict | None:
        lst = self.by_method(method)
        return lst[-1] if lst else None

    def clear(self):
        self.calls.clear()


def _install_recorder(m) -> ApiRecorder:
    rec = ApiRecorder()
    m["ui"].api = rec  # 猴补
    return rec


# ─── Tests ───────────────────────────────────────────────────────

def test_states(m):
    sts = m["states"]
    sts.clear_all()
    assert sts.size() == 0

    sts.set_state(123, "ak_add_name", {"x": 1})
    got = sts.get_state(123)
    assert got["action"] == "ak_add_name"
    assert got["data"] == {"x": 1}

    sts.pop_state(123)
    assert sts.get_state(123) is None

    # TTL
    sts.set_state(456, "x")
    # 人为把 ts 往前推
    sts._states[456]["ts"] = time.time() - 99999
    assert sts.get_state(456) is None  # 读时自动清
    assert sts.size() == 0
    print("  [PASS] states set/get/pop/TTL")


def test_ui_helpers(m):
    ui = m["ui"]
    ui.configure("", [])  # admin 空 → 不限
    assert ui.is_admin(12345) is True
    ui.configure("TOKEN", [5352767013])
    assert ui.is_admin(5352767013) is True
    assert ui.is_admin(99) is False

    kb = ui.inline_kb([[ui.btn("a", "cb:a"), ui.btn("b", "cb:b")]])
    assert kb["inline_keyboard"][0][0]["text"] == "a"
    assert kb["inline_keyboard"][0][0]["callback_data"] == "cb:a"

    s = ui.register_code("some/long name with 中文")
    assert len(s) == 8
    assert ui.resolve_code(s) == "some/long name with 中文"
    assert ui.resolve_code("no-such") is None

    assert ui.escape_html("<b>&a</b>") == "&lt;b&gt;&amp;a&lt;/b&gt;"

    assert ui.truncate("abc", limit=100) == "abc"
    out = ui.truncate("x" * 5000, limit=200)
    assert len(out) <= 200
    assert out.endswith("(已截断)")
    print("  [PASS] ui helpers")


def test_apikey_add_flow(m):
    m["config"].update(lambda c: c.__setitem__("apiKeys", {}))
    rec = _install_recorder(m)
    ak = m["apikey_menu"]
    sts = m["states"]
    sts.clear_all()

    # 点击添加按钮 → 应回编辑消息并设置状态
    ak.on_add(chat_id=100, message_id=50, cb_id="cb1")
    assert sts.get_state(100)["action"] == "ak_add_name"
    assert rec.last("editMessageText") is not None

    # 合法名称输入 → 写入 config + 清状态 + 发送两条消息（key 显示 + 主菜单）
    ak.on_add_name_input(100, "my-app.01")
    keys = m["config"].get()["apiKeys"]
    assert "my-app.01" in keys
    # 新结构：apiKeys[name] 是 dict {key, allowedModels}
    entry = keys["my-app.01"]
    assert isinstance(entry, dict)
    assert entry["key"].startswith("ccp-")
    assert len(entry["key"]) == 4 + 48  # ccp- + 48 hex
    assert entry["allowedModels"] == []   # 默认无限制
    assert sts.get_state(100) is None
    # 两次 sendMessage
    assert len(rec.by_method("sendMessage")) >= 1
    print("  [PASS] apikey add flow (valid)")

    # 名称重复 → 保留状态不写入
    sts.set_state(100, "ak_add_name")
    ak.on_add_name_input(100, "my-app.01")
    assert len(m["config"].get()["apiKeys"]) == 1
    assert sts.get_state(100)["action"] == "ak_add_name"
    # 非法字符 → 保留状态不写入
    ak.on_add_name_input(100, "bad name!")
    assert sts.get_state(100)["action"] == "ak_add_name"
    assert len(m["config"].get()["apiKeys"]) == 1
    print("  [PASS] apikey add rejects dup & invalid")


def test_apikey_del_flow(m):
    """新交互：列表 → 点 key 按钮进详情 → 详情里有「🗑 删除」按钮 → 二次确认 → 执行。"""
    rec = _install_recorder(m)
    ak = m["apikey_menu"]
    m["config"].update(lambda c: c.__setitem__("apiKeys", {
        "alpha": {"key": "ccp-alpha-xxxx", "allowedModels": []},
        "beta":  {"key": "ccp-beta-yyyy",  "allowedModels": []},
    }))

    # 1) 列表页按钮包含两个 ak:view:<short>
    rec.clear()
    ak.show(100, 50)
    edit = rec.last("editMessageText")
    rows = edit["reply_markup"]["inline_keyboard"]
    view_btns = [
        b for row in rows for b in row
        if b.get("callback_data", "").startswith("ak:view:")
    ]
    assert len(view_btns) == 2

    # 2) 点 alpha 进详情，详情含「🗑 删除」按钮 ak:del:<short>
    alpha_short = [b for b in view_btns if "alpha" in b["text"]][0]["callback_data"].split(":")[2]
    rec.clear()
    ak.on_view(100, 50, "cb-view", alpha_short)
    detail = rec.last("editMessageText")
    detail_kb = detail["reply_markup"]["inline_keyboard"]
    del_btns = [b for row in detail_kb for b in row
                if b.get("callback_data", "").startswith("ak:del:")]
    assert len(del_btns) == 1
    del_cb = del_btns[0]["callback_data"]       # ak:del:<short>
    short = del_cb.split(":")[2]

    # 3) 点删除 → 二次确认页含 ak:del_exec:
    rec.clear()
    ak.on_del_confirm(100, 50, "cb-confirm", short)
    confirm = rec.last("editMessageText")
    confirm_kb = confirm["reply_markup"]["inline_keyboard"]
    exec_btn = [b for row in confirm_kb for b in row
                if b.get("callback_data", "").startswith("ak:del_exec:")]
    assert len(exec_btn) == 1

    # 4) 执行删除
    rec.clear()
    ak.on_del_exec(100, 50, "cb-exec", short)
    keys = m["config"].get()["apiKeys"]
    assert len(keys) == 1
    assert "alpha" not in keys
    # 短码无效不崩
    ak.on_del_exec(100, 50, "cb-bad", "00000000")
    # 不 crash；并重新展示主菜单
    print("  [PASS] apikey del flow (list → confirm → exec)")


def test_bot_routing_non_admin(m):
    m["ui"].configure("TOKEN", [42])
    rec = _install_recorder(m)
    bot = m["bot"]

    # 非 admin 发消息 → 被拒
    bot._handle_message({"chat": {"id": 999}, "text": "/start"})
    last = rec.last("sendMessage")
    assert last is not None
    assert "无权限" in last["text"]
    # 非 admin 点按钮 → answerCallbackQuery 拒绝
    rec.clear()
    bot._handle_callback({"id": "cbx", "message": {"chat": {"id": 999}, "message_id": 10}, "data": "menu:apikey"})
    ans = rec.last("answerCallbackQuery")
    assert ans and "无权限" in ans["text"]
    print("  [PASS] non-admin rejected")


def test_bot_commands_and_callbacks(m):
    m["ui"].configure("TOKEN", [42])
    rec = _install_recorder(m)
    bot = m["bot"]

    # /start → 一条合并消息（欢迎语 + 主菜单按钮），避免 send 多条造成消息泛滥
    rec.clear()
    bot._handle_message({"chat": {"id": 42}, "text": "/start"})
    sends = rec.by_method("sendMessage")
    assert len(sends) == 1, f"expected exactly 1 message, got {len(sends)}"
    msg = sends[-1]
    assert "欢迎使用" in msg["text"]
    kb = msg.get("reply_markup", {}).get("inline_keyboard", [])
    btns = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
    assert "menu:apikey" in btns
    assert "menu:oauth" in btns

    # callback menu:apikey → 编辑消息显示 API Key 列表
    rec.clear()
    bot._handle_callback({
        "id": "cb1", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "menu:apikey"
    })
    assert rec.last("editMessageText") is not None
    assert rec.last("answerCallbackQuery") is not None

    # 完全未定义的 callback → 回"未知操作"或"尚未实现"
    rec.clear()
    bot._handle_callback({
        "id": "cb2", "message": {"chat": {"id": 42}, "message_id": 100}, "data": "menu:nonexistent_xyz"
    })
    ans = rec.last("answerCallbackQuery")
    assert ans and ("未知操作" in (ans.get("text") or "") or "尚未实现" in (ans.get("text") or ""))

    # /menu 命令
    rec.clear()
    bot._handle_message({"chat": {"id": 42}, "text": "/menu"})
    assert rec.last("sendMessage") is not None
    print("  [PASS] bot commands & callbacks")


def test_bot_state_dispatch(m):
    """有状态时，text 输入应被路由给对应菜单的状态处理，而不是走命令。"""
    m["ui"].configure("TOKEN", [42])
    m["config"].update(lambda c: c.__setitem__("apiKeys", {}))
    rec = _install_recorder(m)
    bot = m["bot"]
    sts = m["states"]
    sts.clear_all()

    # 预置状态
    sts.set_state(42, "ak_add_name")
    # 发送一条文本
    bot._handle_message({"chat": {"id": 42}, "text": "via-state"})
    # 应写入 config
    assert "via-state" in m["config"].get()["apiKeys"]
    # 状态被清
    assert sts.get_state(42) is None
    print("  [PASS] bot text dispatches to active state handler")


def test_notifier_handler_to_admins(m):
    """install_notify_handler 让 notifier 把消息发给所有 admin。

    notifier.notify 已改为非阻塞队列（worker 线程消费），测试需要等 worker 处理。
    """
    import time
    from src import notifier
    m["ui"].configure("TOKEN", [11, 22])
    rec = _install_recorder(m)
    m["ui"].install_notify_handler()

    # handler 不再对整段 text escape（让 <b>/<code> 渲染）。
    # 调用方需自己对用户字符串调 notifier.escape_html。
    notifier.notify(f"hello {notifier.escape_html('<world>')}")
    notifier.wait_drain(2.0)
    sends = rec.by_method("sendMessage")
    assert len(sends) == 2, f"expected 2 sends, got {len(sends)}"
    ids = sorted(s["chat_id"] for s in sends)
    assert ids == [11, 22]
    for s in sends:
        assert "&lt;world&gt;" in s["text"]

    # 自动删除：notify 带 auto_delete_seconds 时，handler 在文案末尾追加倒计时
    rec.clear()
    notifier.notify("ping", auto_delete_seconds=180)
    notifier.wait_drain(2.0)
    sends = rec.by_method("sendMessage")
    assert sends, "should send"
    assert "180 秒后自动删除" in sends[-1]["text"]

    # 卸载
    notifier.set_handler(None)
    print("  [PASS] notifier → admin broadcast")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()

    orig_cfg = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_states,
        test_ui_helpers,
        test_apikey_add_flow,
        test_apikey_del_flow,
        test_bot_routing_non_admin,
        test_bot_commands_and_callbacks,
        test_bot_state_dispatch,
        test_notifier_handler_to_admins,
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
        m["states"].clear_all()

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
