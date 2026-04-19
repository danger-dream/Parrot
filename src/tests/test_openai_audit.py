"""审计后补的回归用例。

针对系统性审视发现的 4 个问题的修复验证：
  P1 - stream_c2r 空流也要发 response.created 序列
  P1 - chat_to_responses 历史 assistant.reasoning_content 应转为 reasoning item
  P2 - handler pending log 不应含 _api_key_name 等下划线前缀内部字段
  P2 - guard 对 conversation:null 应放行（只有实际非空值才拒绝）

运行：./venv/bin/python -m src.tests.test_openai_audit
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import os
import sys

import httpx


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, auth, config, cooldown, errors, failover, log_db,
        scheduler, scorer, state_db, upstream,
    )
    from src.channel import registry, api_channel
    from src.openai import handler as openai_handler, store as openai_store
    from src.openai.channel.registration import register_factories
    from src.openai.transform import (
        chat_to_responses, responses_to_chat, stream_c2r, guard,
    )
    register_factories()
    return {
        "affinity": affinity, "auth": auth, "config": config, "cooldown": cooldown,
        "errors": errors, "failover": failover, "log_db": log_db,
        "scheduler": scheduler, "scorer": scorer, "state_db": state_db,
        "upstream": upstream, "registry": registry, "api_channel": api_channel,
        "openai_handler": openai_handler, "openai_store": openai_store,
        "chat_to_responses": chat_to_responses,
        "responses_to_chat": responses_to_chat,
        "stream_c2r": stream_c2r, "guard": guard,
    }


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["openai_store"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    m["openai_store"]._reset_for_test()
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"].init()
    m["cooldown"].init()
    m["scorer"].init()


def _parse_responses_events(frames):
    text = b"".join(frames).decode("utf-8", errors="replace")
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev, data_str = "", ""
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if not data_str:
            continue
        try:
            out.append((ev, json.loads(data_str)))
        except Exception:
            pass
    return out


# ─── P1.1：stream_c2r 空流也要 emit 完整序列 ────────────────────


def test_c2r_empty_stream_still_has_created(m):
    """上游只发 [DONE] 就关闭：下游序列必须有 response.created → in_progress → completed。"""
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="x")
    frames = list(tr.feed(b"data: [DONE]\n\n")) + list(tr.close())
    events = _parse_responses_events(frames)
    names = [n for n, _ in events]
    assert names[0] == "response.created", f"首事件应是 response.created：{names}"
    assert "response.in_progress" in names
    assert names[-1] == "response.completed", f"末事件应是 response.completed：{names}"
    print("  [PASS] c2r: 空流仍 emit created/in_progress/completed")


def test_c2r_close_without_any_feed(m):
    """直接 close() 不 feed：依然要发 created → completed 合法序列。"""
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="x")
    frames = list(tr.close())
    events = _parse_responses_events(frames)
    names = [n for n, _ in events]
    assert names[0] == "response.created"
    assert names[-1] == "response.completed"
    print("  [PASS] c2r: 直接 close() 不 feed 也合法")


def test_c2r_immediate_error_still_has_created(m):
    """上游第一包就 error chunk：下游仍需 created → failed 合法。"""
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="x")
    chunks = [
        # 一个 data: 行后立即给 error，但不 [DONE]；然后 close
        b'data: {"error":{"message":"bad","type":"server_error"}}\n\n',
    ]
    frames: list[bytes] = []
    for c in chunks:
        frames.extend(tr.feed(c))
    frames.extend(tr.close())
    events = _parse_responses_events(frames)
    names = [n for n, _ in events]
    assert names[0] == "response.created", f"error 流首事件也应是 created：{names}"
    assert names[-1] == "response.failed", f"末事件应是 failed：{names}"
    failed = [p for n, p in events if n == "response.failed"][0]
    assert failed["response"]["status"] == "failed"
    assert "bad" in failed["response"]["error"]["message"]
    print("  [PASS] c2r: 首包 error 也先 emit created，再 failed")


def test_c2r_only_close_after_error_before_done(m):
    """上游一个 data chunk 都没发就直接断开（没有 [DONE]、没有 error）：close() 应合法收尾。"""
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="x")
    frames = list(tr.close())
    events = _parse_responses_events(frames)
    types = [n for n, _ in events]
    assert types == ["response.created", "response.in_progress", "response.completed"]
    print("  [PASS] c2r: 无数据直接关闭 → 最小合法 3 事件序列")


# ─── P1.2：chat_to_responses 保留历史 reasoning_content ──────────


def _set_bridge(m, mode: str):
    def _mut(c): c.setdefault("openai", {})["reasoningBridge"] = mode
    m["config"].update(_mut)


def test_historic_reasoning_passthrough(m):
    """passthrough 模式：chat messages 里 assistant.reasoning_content 要转成 reasoning input item。"""
    _set_bridge(m, "passthrough")
    c2r = m["chat_to_responses"]
    body = {
        "model": "gpt-5",
        "messages": [
            {"role": "user", "content": "Q1"},
            {"role": "assistant",
             "content": "A1",
             "reasoning_content": "step-1 then step-2"},
            {"role": "user", "content": "Q2"},
        ],
    }
    out = c2r.translate_request(body)
    items = out["input"]
    types = [it["type"] for it in items]
    # 期望：user-msg, reasoning, assistant-msg, user-msg
    assert types.count("reasoning") == 1, f"passthrough 应生成 1 个 reasoning item：{types}"
    # reasoning 出现在 assistant message 之前
    reasoning_idx = types.index("reasoning")
    assistant_msg_positions = [i for i, it in enumerate(items)
                                if it["type"] == "message" and it.get("role") == "assistant"]
    assert assistant_msg_positions and reasoning_idx < assistant_msg_positions[0]
    # reasoning item 内容正确
    r_item = items[reasoning_idx]
    assert r_item["summary"][0]["text"] == "step-1 then step-2"
    print("  [PASS] c2r 历史 reasoning_content 保留为 reasoning input item（passthrough）")


def test_historic_reasoning_drop(m):
    """drop 模式：reasoning_content 完全丢弃。"""
    _set_bridge(m, "drop")
    try:
        c2r = m["chat_to_responses"]
        body = {
            "model": "gpt-5",
            "messages": [
                {"role": "user", "content": "Q1"},
                {"role": "assistant",
                 "content": "A1",
                 "reasoning_content": "step-1 then step-2"},
                {"role": "user", "content": "Q2"},
            ],
        }
        out = c2r.translate_request(body)
        types = [it["type"] for it in out["input"]]
        assert "reasoning" not in types, f"drop 模式不应有 reasoning item：{types}"
    finally:
        _set_bridge(m, "passthrough")
    print("  [PASS] c2r 历史 reasoning_content drop 模式丢弃")


# ─── P2.1：handler pending log 不应含 _api_key_name ─────────────


class FakeHeaders:
    def __init__(self, data):
        self._d = {k.lower(): v for k, v in data.items()}
    def get(self, k, default=None): return self._d.get(k.lower(), default)
    def items(self): return self._d.items()
    def keys(self): return self._d.keys()
    def __getitem__(self, k): return self._d[k.lower()]
    def __iter__(self): return iter(self._d.keys())
    def __len__(self): return len(self._d)


class FakeClient:
    def __init__(self, host="1.2.3.4"): self.host = host


class FakeRequest:
    def __init__(self, headers, body_bytes):
        self.headers = FakeHeaders(headers)
        self._body = body_bytes
        self.client = FakeClient()
    async def body(self): return self._body


class MockRouter:
    def __init__(self): self.handlers = {}
    def register(self, base, h): self.handlers[base.rstrip("/")] = h
    def handle(self, req):
        url = str(req.url)
        for base, h in self.handlers.items():
            if url.startswith(base):
                return h(req)
        return httpx.Response(404, text="no mock")


def _make_openai_channel(m, name, base_url, protocol="openai-chat"):
    from src.openai.channel.api_channel import OpenAIApiChannel
    return OpenAIApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "protocol": protocol,
        "models": [{"real": "gpt-5", "alias": "gpt-5"}],
        "enabled": True,
    })


def _install_channels(m, chs):
    with m["registry"]._lock:
        m["registry"]._channels = {c.key: c for c in chs}


async def test_handler_sanitizes_internal_body_fields(m):
    _setup(m)

    def _k(c):
        c["apiKeys"] = {"alice": {"key": "ccp-alice"}}
    m["config"].update(_k)

    router = MockRouter()
    router.register("https://a.example", lambda r: httpx.Response(200, json={
        "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-5",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }))
    ch = _make_openai_channel(m, "oaiA", "https://a.example", protocol="openai-chat")
    _install_channels(m, [ch])

    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)

    req = FakeRequest({"Authorization": "Bearer ccp-alice"},
                      json.dumps({"model": "gpt-5", "stream": False,
                                   "messages": [{"role": "user", "content": "hi"}]}).encode())
    resp = await m["openai_handler"].handle(req, ingress_protocol="chat")
    assert resp.status_code == 200
    await mock_client.aclose()

    # 找最新的 pending log 记录（body 存在 request_detail 表；用 log_detail 取）
    import sqlite3
    log_dir = m["log_db"]._log_dir
    month_db = [f for f in os.listdir(log_dir) if f.endswith(".db")]
    assert month_db, "log_db 应已写入"
    conn = sqlite3.connect(os.path.join(log_dir, month_db[0]))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT request_id FROM request_log ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    detail = m["log_db"].log_detail(row["request_id"])
    conn.close()
    raw_body = (detail.get("detail") or {}).get("request_body")
    assert raw_body, f"log_detail 应含 request_body；得到 {detail}"
    body_json = json.loads(raw_body)
    assert "_api_key_name" not in body_json, (
        f"pending log 不应包含内部下划线字段：{list(body_json.keys())}"
    )
    # 但正常请求字段还在
    assert body_json["model"] == "gpt-5"
    assert "messages" in body_json
    print("  [PASS] handler：log body 不含 _api_key_name 等内部字段")


# ─── P2.2：guard conversation null 应放行 ──────────────────────


def test_reasoning_content_read_from_reasoning_text(m):
    """reasoning item 的 content[].reasoning_text 也应被读取，不只是 summary_text。"""
    c2r = m["chat_to_responses"]
    resp = {
        "id": "resp_1", "status": "completed", "created_at": 1, "model": "x",
        "output": [
            {"type": "reasoning", "id": "rs_1",
             # 只给 content 不给 summary，模拟部分模型的输出
             "content": [{"type": "reasoning_text", "text": "raw step-1 raw step-2"}]},
            {"type": "message", "id": "m", "role": "assistant",
             "content": [{"type": "output_text", "text": "ans", "annotations": []}]},
        ],
        "output_text": "ans",
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    }
    out = c2r.translate_response(resp, model="x")
    msg = out["choices"][0]["message"]
    assert msg.get("reasoning_content") == "raw step-1 raw step-2", (
        f"content[].reasoning_text 应被收集：{msg}"
    )
    print("  [PASS] c2r: reasoning.content[].reasoning_text 也能被 gather")


def test_legacy_function_call_passthrough(m):
    """Chat 请求白名单保留 legacy `functions` / `function_call` 字段（SDK 仍接受）。"""
    from src.openai.transform.common import filter_chat_passthrough, CHAT_REQ_ALLOWED
    assert "functions" in CHAT_REQ_ALLOWED
    assert "function_call" in CHAT_REQ_ALLOWED
    body = {
        "model": "gpt-5",
        "messages": [{"role": "user", "content": "hi"}],
        "functions": [{"name": "f", "parameters": {}}],
        "function_call": "auto",
        "some_unknown": "should drop",
    }
    filtered = filter_chat_passthrough(body)
    assert "functions" in filtered
    assert "function_call" in filtered
    assert "some_unknown" not in filtered
    print("  [PASS] chat 请求白名单：legacy functions/function_call 透传")


def test_cancelled_status_maps_to_stop(m):
    """Response.status=cancelled/queued → chat finish_reason=stop（保守）。"""
    c2r = m["chat_to_responses"]
    for status in ("cancelled", "queued", "in_progress"):
        resp = {
            "id": "r", "status": status, "created_at": 1, "model": "x",
            "output": [], "output_text": "",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
        out = c2r.translate_response(resp, model="x")
        fr = out["choices"][0]["finish_reason"]
        assert fr == "stop", f"status={status} 期望 finish_reason=stop，得到 {fr}"
    print("  [PASS] c2r: cancelled/queued/in_progress status → finish_reason=stop")


def test_guard_conversation_null_allowed(m):
    """conversation=null / 空 dict：不应触发 guard 拒绝；只有非空 conv id 才拒。"""
    g = m["guard"]

    # ingress guard：null 不拒
    g.guard_responses_ingress({"conversation": None}, store_enabled=True)  # no raise
    g.guard_responses_ingress({"conversation": {}}, store_enabled=True)    # no raise
    g.guard_responses_ingress({"conversation": ""}, store_enabled=True)    # no raise
    # 显式给值时拒
    try:
        g.guard_responses_ingress({"conversation": "conv_xyz"}, store_enabled=True)
        assert False, "非空 conversation 应被拒"
    except g.GuardError as e:
        assert e.status == 400

    # cross-variant guard 同理
    g.guard_responses_to_chat({"conversation": None}, store_enabled=True)  # no raise
    try:
        g.guard_responses_to_chat({"conversation": {"id": "x"}}, store_enabled=True)
        assert False
    except g.GuardError as e:
        assert e.status == 400

    print("  [PASS] guard：conversation=null/空 → 放行；非空 → 400")


# ─── 驱动 ────────────────────────────────────────────────────────


def _async(fn):
    def _w(m): asyncio.run(fn(m))
    _w.__name__ = fn.__name__
    return _w


def main() -> int:
    m = _import_modules()
    m["state_db"].init()
    m["log_db"].init()
    m["openai_store"].init()
    orig_cfg = m["config"].get().copy()

    tests = [
        test_c2r_empty_stream_still_has_created,
        test_c2r_close_without_any_feed,
        test_c2r_immediate_error_still_has_created,
        test_c2r_only_close_after_error_before_done,
        test_historic_reasoning_passthrough,
        test_historic_reasoning_drop,
        test_reasoning_content_read_from_reasoning_text,
        test_legacy_function_call_passthrough,
        test_cancelled_status_maps_to_stop,
        _async(test_handler_sanitizes_internal_body_fields),
        test_guard_conversation_null_allowed,
    ]
    passed = 0
    try:
        print("── OpenAI 审计后修复回归 ─────────────────────")
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
        def _r(c): c.clear(); c.update(orig_cfg)
        m["config"].update(_r)

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
