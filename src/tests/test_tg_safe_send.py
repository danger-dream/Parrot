"""验证 ui.api 对 TG 解析错误的防御：
  - HTML parse 失败 → 自动用纯文本重发
  - message is not modified → 吞掉（视为成功）
  - 其他错误 → 返回原始响应
  - 注入未 escape 的渠道名/上游错误到 HTML 内不应让整条消息丢失
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import os
import sys


def _import():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src.telegram import ui
    return ui


class FakeResp:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p


class FakeSession:
    """记录每次 post 参数，按预设依次返回。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
    def post(self, url, json=None):
        self.requests.append({"url": url, "json": json})
        return FakeResp(self.responses.pop(0) if self.responses else {"ok": True, "result": {}})
    def get(self, url):
        self.requests.append({"url": url})
        return FakeResp(self.responses.pop(0) if self.responses else {"ok": True, "result": {}})
    def close(self): pass


def _install_session(ui, session):
    import src.telegram.ui as mod
    mod._session = session


def test_parse_error_auto_fallback_to_plaintext():
    ui = _import()
    ui.configure("TOKEN", [42])
    session = FakeSession([
        {"ok": False, "error_code": 400,
         "description": "Bad Request: can't parse entities: Unsupported start tag \"bad\" at byte offset 12"},
        {"ok": True, "result": {"message_id": 999}},
    ])
    _install_session(ui, session)

    text = "<b>badly</b> <bad>not valid</bad> <code>x</code>"
    r = ui.api("sendMessage", {"chat_id": 42, "text": text, "parse_mode": "HTML"})
    assert r and r.get("ok") is True, f"expected ok from retry, got {r}"
    assert len(session.requests) == 2
    # 第二次重发应无 parse_mode、text 是剥离标签后
    second = session.requests[1]["json"]
    assert "parse_mode" not in second
    assert "badly not valid x" in second["text"]
    print("  [PASS] parse error → plain-text retry")


def test_message_not_modified_swallowed():
    ui = _import()
    ui.configure("TOKEN", [42])
    session = FakeSession([
        {"ok": False, "error_code": 400,
         "description": "Bad Request: message is not modified"},
    ])
    _install_session(ui, session)
    r = ui.api("editMessageText",
               {"chat_id": 42, "message_id": 10, "text": "same", "parse_mode": "HTML"})
    assert r and r.get("ok") is True
    assert r["result"].get("not_modified") is True
    assert len(session.requests) == 1  # 不重试
    print("  [PASS] message is not modified → swallowed")


def test_other_error_returns_raw():
    ui = _import()
    ui.configure("TOKEN", [42])
    session = FakeSession([
        {"ok": False, "error_code": 400, "description": "chat not found"},
    ])
    _install_session(ui, session)
    r = ui.api("sendMessage", {"chat_id": 42, "text": "x"})
    assert r and r.get("ok") is False
    assert "chat not found" in r.get("description", "")
    assert len(session.requests) == 1
    print("  [PASS] other error → raw returned (no retry)")


def test_success_passthrough():
    ui = _import()
    ui.configure("TOKEN", [42])
    session = FakeSession([
        {"ok": True, "result": {"message_id": 1234}},
    ])
    _install_session(ui, session)
    r = ui.api("sendMessage", {"chat_id": 42, "text": "hi"})
    assert r and r.get("ok") is True
    assert r["result"]["message_id"] == 1234
    print("  [PASS] success passthrough")


def test_strip_html_tags():
    ui = _import()
    raw = "<b>Bold</b> &amp; <code>code &lt;x&gt;</code> <a href=\"x\">link</a>"
    plain = ui._strip_html_tags(raw)
    assert plain == "Bold & code <x> link"
    print("  [PASS] _strip_html_tags roundtrip")


def test_escape_html_covers_basics():
    ui = _import()
    # 基本三项
    assert ui.escape_html("<b>hi & you</b>") == "&lt;b&gt;hi &amp; you&lt;/b&gt;"
    # None/数字
    assert ui.escape_html(None) == ""
    assert ui.escape_html(42) == "42"
    print("  [PASS] escape_html basic")


def main():
    ui = _import()
    # 保存初始状态
    import src.telegram.ui as mod
    orig = mod._session
    try:
        tests = [
            test_parse_error_auto_fallback_to_plaintext,
            test_message_not_modified_swallowed,
            test_other_error_returns_raw,
            test_success_passthrough,
            test_strip_html_tags,
            test_escape_html_covers_basics,
        ]
        passed = 0
        for t in tests:
            try:
                t(); passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        mod._session = orig
    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
