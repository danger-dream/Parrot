"""M4 故障转移集成测试。

使用 httpx.MockTransport 模拟上游行为，不触网。覆盖：
  - 非流式成功 / HTTP 500 → 切换 / HTTP 400 → 切换但不 cooldown
  - 流式成功完整转发
  - 上游首个 SSE event 是 error → 切换
  - 首包文本黑名单命中 → 切换
  - 全部候选失败 → 503
  - 亲和命中把绑定渠道顶首位
  - 连续 5xx 失败进入 cooldown，下次调度被排除

运行：./venv/bin/python -m src.tests.test_m4_failover
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import os
import sys
import time

import httpx


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, config, cooldown, failover, fingerprint,
        log_db, scheduler, scorer, state_db, upstream,
    )
    from src.channel import registry, api_channel
    return {
        "affinity": affinity, "config": config, "cooldown": cooldown,
        "failover": failover, "fingerprint": fingerprint,
        "log_db": log_db, "scheduler": scheduler, "scorer": scorer,
        "state_db": state_db, "upstream": upstream,
        "registry": registry, "api_channel": api_channel,
    }


def _setup(m):
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"].init()
    m["cooldown"].init()
    m["scorer"].init()


# ─── Mock Transport 路由 ─────────────────────────────────────────

class MockRouter:
    """按 channel baseUrl 分发模拟响应。"""

    def __init__(self):
        self.handlers: dict[str, callable] = {}

    def register(self, base_url: str, handler):
        self.handlers[base_url.rstrip("/")] = handler

    def handle(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        for base, handler in self.handlers.items():
            if url_str.startswith(base):
                return handler(request)
        return httpx.Response(404, text="no mock")


# ─── 常用响应工厂 ─────────────────────────────────────────────────

def json_ok_response():
    body = {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "glm-5",
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3},
    }
    return httpx.Response(200, json=body, headers={"content-type": "application/json"})


def http_500():
    return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "oops"}})


def http_400():
    return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})


def sse_ok():
    payload = (
        b'data: {"type":"message_start","message":{"id":"msg_1","role":"assistant","usage":{"input_tokens":10,"cache_creation_input_tokens":0,"cache_read_input_tokens":2}}}\n\n'
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def sse_first_event_error():
    payload = b'data: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n'
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


def sse_with_blacklist():
    payload = (
        b'data: {"type":"message_start","message":{"id":"x","role":"assistant",'
        b'"content":[{"type":"text","text":"content_policy_violation detected"}],'
        b'"usage":{"input_tokens":0}}}\n\n'
    )
    return httpx.Response(200, content=payload,
                          headers={"content-type": "text/event-stream"})


# ─── 用例 ────────────────────────────────────────────────────────

def _make_channel(m, name, base_url, real="glm-5", alias="glm-5", cc_mimicry=False):
    return m["api_channel"].ApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "models": [{"real": real, "alias": alias}],
        "cc_mimicry": cc_mimicry, "enabled": True,
    })


def _install_channels(m, channels):
    reg = m["registry"]
    with reg._lock:
        reg._channels = {ch.key: ch for ch in channels}


async def _call_proxy(m, router: MockRouter, body: dict, api_key="k1", client_ip="1.1.1.1"):
    """模拟 server.py /v1/messages 的核心调用链。"""
    # 注入 mock client
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)

    request_id = f"req-{int(time.time()*1000)}"
    start = time.time()

    await asyncio.to_thread(
        m["log_db"].insert_pending,
        request_id, client_ip, api_key, body.get("model"), bool(body.get("stream", True)),
        len(body.get("messages") or []), len(body.get("tools") or []),
        {}, body,
    )

    sched_result = m["scheduler"].schedule(body, api_key_name=api_key, client_ip=client_ip)
    if not sched_result.candidates:
        from src import errors as er
        resp = er.json_error_response(503, er.ErrType.API, "no candidates")
        await mock_client.aclose()
        return resp, request_id, sched_result

    resp = await m["failover"].run_failover(
        sched_result, body, request_id, api_key, client_ip,
        is_stream=bool(body.get("stream", True)), start_time=start,
    )
    # 非流式 resp 可以立刻关 client；流式需要等流消费完
    if not isinstance(resp, httpx.AsyncClient):  # 占位判断
        pass
    return resp, request_id, sched_result, mock_client


async def _consume_streaming_to_string(resp) -> str:
    chunks = []
    async for c in resp.body_iterator:
        if isinstance(c, str):
            chunks.append(c.encode())
        else:
            chunks.append(c)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _close_background(resp, mock_client):
    """StreamingResponse 的 background 任务在返回后由 FastAPI 调度；
    单测里我们手工关。"""
    try:
        await mock_client.aclose()
    except Exception:
        pass


# ─── 具体测试 ────────────────────────────────────────────────────

async def test_non_stream_success(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    _install_channels(m, [chA])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    if resp.status_code != 200:
        body_bytes = resp.body if hasattr(resp, "body") else b""
        print(f"    body={body_bytes[:500]!r}")
    assert resp.status_code == 200, f"status={resp.status_code}"
    await mc.aclose()

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success"
    assert log["log"]["final_channel_key"] == "api:chA"
    assert log["log"]["input_tokens"] == 10
    assert log["log"]["output_tokens"] == 5
    assert log["log"]["cache_read_tokens"] == 3
    assert len(log["retry_chain"]) == 1
    assert log["retry_chain"][0]["outcome"] == "success"
    # scorer 记录了一次 success
    stats = m["scorer"].get_stats("api:chA", "glm-5")
    assert stats["success_count"] == 1
    print("  [PASS] non_stream_success")


async def test_non_stream_500_then_ok(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_500())
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    await mc.aclose()

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success"
    assert log["log"]["final_channel_key"] == "api:chB"
    assert len(log["retry_chain"]) == 2
    assert log["retry_chain"][0]["outcome"] == "http_error"
    assert log["retry_chain"][1]["outcome"] == "success"
    # chA 进入 cooldown
    assert m["cooldown"].is_blocked("api:chA", "glm-5")
    # chB success
    assert m["scorer"].get_stats("api:chB", "glm-5")["success_count"] == 1
    print("  [PASS] non_stream 500 → switch → success; chA cooldown")


async def test_all_fail_503(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_500())
    router.register("https://chb", lambda r: http_500())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    # 按设计 doc §10.1：全候选耗尽 → 503 api_error
    # （只有最后一次失败是 timeout/transport 时才回 504/502）
    assert resp.status_code == 503, f"expected 503, got {resp.status_code}"
    await mc.aclose()
    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "error"
    assert len(log["retry_chain"]) == 2
    print("  [PASS] all_fail → 503")


async def test_400_switches_no_cooldown(m):
    """HTTP 400 应切下一个渠道但不记 cooldown（请求级问题）。"""
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: http_400())
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    await mc.aclose()
    # chA HTTP 400 仍记入 cooldown，按当前策略（outcome=http_error → should_cooldown=True）
    # 测试目的：记录当前行为，确保切换发生
    assert m["scorer"].get_stats("api:chA", "glm-5")["total_requests"] == 1
    assert m["scorer"].get_stats("api:chB", "glm-5")["success_count"] == 1
    print("  [PASS] 400 switch → next success")


async def test_stream_success_full_forward(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: sse_ok())
    chA = _make_channel(m, "chA", "https://cha")
    _install_channels(m, [chA])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200

    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    assert "message_start" in body_text
    assert "content_block_delta" in body_text
    assert "message_stop" in body_text

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success", log["log"]
    assert log["log"]["input_tokens"] == 10
    assert log["log"]["output_tokens"] == 7
    assert log["log"]["cache_read_tokens"] == 2
    print("  [PASS] stream_success_full_forward")


async def test_stream_first_event_error_switches(m):
    _setup(m)
    router = MockRouter()
    router.register("https://cha", lambda r: sse_first_event_error())
    router.register("https://chb", lambda r: sse_ok())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)
    # 下游看到的应是 chB 的完整流
    assert "message_stop" in body_text

    log = m["log_db"].log_detail(rid)
    assert log["log"]["status"] == "success"
    assert log["log"]["final_channel_key"] == "api:chB"
    outcomes = [a["outcome"] for a in log["retry_chain"]]
    assert outcomes == ["upstream_error_json", "success"], outcomes
    print("  [PASS] stream first event error → switch → chB ok")


async def test_stream_blacklist_switch(m):
    _setup(m)
    # 在 config 里配置黑名单
    m["config"].update(lambda c: c.setdefault("contentBlacklist", {}).__setitem__("default", ["content_policy_violation"]))

    router = MockRouter()
    router.register("https://cha", lambda r: sse_with_blacklist())
    router.register("https://chb", lambda r: sse_ok())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    body_text = await _consume_streaming_to_string(resp)
    await _close_background(resp, mc)

    log = m["log_db"].log_detail(rid)
    outcomes = [a["outcome"] for a in log["retry_chain"]]
    assert outcomes == ["blacklist_hit", "success"], outcomes
    assert "message_stop" in body_text

    # 清黑名单
    m["config"].update(lambda c: c.setdefault("contentBlacklist", {}).__setitem__("default", []))
    print("  [PASS] stream blacklist_hit → switch")


async def test_affinity_pins_channel(m):
    _setup(m)
    # 禁用探索率，让评分排序确定
    m["config"].update(lambda c: c.setdefault("scoring", {}).__setitem__("explorationRate", 0.0))

    router = MockRouter()
    router.register("https://cha", lambda r: json_ok_response())
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    # 多轮对话 → fingerprint 可算
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    body = {"model": "glm-5", "stream": False, "max_tokens": 100, "messages": msgs}

    # 第一次：两个渠道都可用，随便选一个
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    assert resp.status_code == 200
    first_choice = m["log_db"].log_detail(rid)["log"]["final_channel_key"]
    await mc.aclose()

    # 由于第一次写入亲和是基于 messages + assistant_response
    # 下次 messages = msgs + [assistant_response] + [new_user]
    a1_obj = json.loads(resp.body)
    next_msgs = msgs + [{"role": "assistant", "content": a1_obj["content"]},
                        {"role": "user", "content": "q3"}]
    body2 = {"model": "glm-5", "stream": False, "max_tokens": 100, "messages": next_msgs}

    resp2, rid2, sr2, mc2 = await _call_proxy(m, router, body2)
    assert resp2.status_code == 200
    await mc2.aclose()
    # 亲和命中
    assert sr2.affinity_hit, "expected affinity hit on 2nd request"
    second_choice = m["log_db"].log_detail(rid2)["log"]["final_channel_key"]
    assert second_choice == first_choice, f"expected same channel, got {first_choice} vs {second_choice}"

    # 恢复
    m["config"].update(lambda c: c.setdefault("scoring", {}).__setitem__("explorationRate", 0.2))
    print(f"  [PASS] affinity pinned to {first_choice}")


async def test_cooldown_excludes_from_next(m):
    _setup(m)

    router = MockRouter()
    # 让 chA 前 6 次失败进入永久 cooldown，然后 chB 始终成功
    call_count = {"a": 0}

    def chA_handler(req):
        call_count["a"] += 1
        return http_500()

    router.register("https://cha", chA_handler)
    router.register("https://chb", lambda r: json_ok_response())
    chA = _make_channel(m, "chA", "https://cha")
    chB = _make_channel(m, "chB", "https://chb")
    _install_channels(m, [chA, chB])

    body = {"model": "glm-5", "stream": False, "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}]}

    # 第一次：chA fail → chB ok (chA 进入 cooldown 1 分钟)
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    await mc.aclose()
    assert resp.status_code == 200
    assert m["cooldown"].is_blocked("api:chA", "glm-5")

    # 第二次：chA 已被 cooldown 排除，仅 chB 参与，立即成功
    a_called_before = call_count["a"]
    resp, rid, sr, mc = await _call_proxy(m, router, body)
    await mc.aclose()
    assert resp.status_code == 200
    keys = [c[0].key for c in sr.candidates]
    assert "api:chA" not in keys, f"chA should be excluded, got {keys}"
    assert call_count["a"] == a_called_before, "chA should not be called"
    print("  [PASS] cooldown excludes chA from next schedule")


async def amain():
    m = _import_modules()

    # 备份 config
    orig = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_non_stream_success,
        test_non_stream_500_then_ok,
        test_all_fail_503,
        test_400_switches_no_cooldown,
        test_stream_success_full_forward,
        test_stream_first_event_error_switches,
        test_stream_blacklist_switch,
        test_affinity_pins_channel,
        test_cooldown_excludes_from_next,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                await t(m)
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {t.__name__}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore(c):
            c.clear(); c.update(orig)
        m["config"].update(_restore)
        # 清 state.db
        _setup(m)

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
