"""saturated-only 场景测试：candidates 为空但 saturated 非空时，
不能提前 503，应进入 failover 排队，等 slot 释放后继续。

覆盖：
  - Anthropic 入口 /v1/messages 主流程（用修复后的判断 if not result）
  - OpenAI 入口 /v1/chat/completions 真 handler
  - 完全无候选（既无 candidates 也无 saturated）时仍然 503  ← 回归
  - 排队超时返回 503（queueWaitSeconds 极小）             ← 回归

运行：./venv/bin/python -m src.tests.test_saturated_only
"""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import itertools
import json
import sys
import time

import httpx
import pytest

# 复用 test_m4_failover 的工具
from src.tests.test_m4_failover import (
    MockRouter,
    json_ok_response,
    _import_modules as _import_anth,
    _setup as _setup_anth,
    _install_channels,
)

# 复用 test_openai_m4 的工具
from src.tests.test_openai_m4 import (
    _import_modules as _import_oa,
    _setup as _setup_oa,
    _install_keys,
    _call_openai_handler,
    MockRouter as OAMockRouter,
)




def _import_modules():
    return _import_anth()


@pytest.fixture
def m_oa():
    return _import_oa()

_REQUEST_SEQ = itertools.count()


def _set_concurrency_cfg(cfg_mod, *, enabled=True, queue_wait_s=10, default_max=0):
    def _mutate(c):
        c["concurrency"] = {
            "enabled": enabled,
            "queueWaitSeconds": queue_wait_s,
            "defaultMaxConcurrent": default_max,
        }
    cfg_mod.update(_mutate)


def _reset_slots(c_mod):
    c_mod._slots.clear()


def _make_anth_channel_with_max(m, name, base_url, max_concurrent):
    return m["api_channel"].ApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "models": [{"real": "glm-5", "alias": "glm-5"}],
        "cc_mimicry": False, "enabled": True,
        "maxConcurrent": max_concurrent,
    })


def _make_oa_channel(name, base_url, protocol="openai-chat",
                     real="gpt-5.5", alias="gpt-5.5"):
    from src.openai.channel.api_channel import OpenAIApiChannel
    return OpenAIApiChannel({
        "name": name, "type": "api",
        "baseUrl": base_url, "apiKey": "sk-x",
        "models": [{"real": real, "alias": alias}],
        "protocol": protocol, "enabled": True,
    })


async def _call_proxy_fixed(m, router: MockRouter, body: dict, api_key="k1",
                            client_ip="1.1.1.1", ingress_protocol="anthropic"):
    """模拟修复后的 server.py /v1/messages：判断改为 `if not result`，
    saturated-only 也进入 failover。返回 (resp, request_id, sched_result, mock_client)。"""
    transport = httpx.MockTransport(router.handle)
    mock_client = httpx.AsyncClient(transport=transport, timeout=10.0)
    m["upstream"].set_client(mock_client)

    request_id = f"req-{int(time.time()*1000)}-{next(_REQUEST_SEQ)}"
    start = time.time()

    msg_items = body.get("messages") if ingress_protocol == "anthropic" else body.get("input")
    await asyncio.to_thread(
        m["log_db"].insert_pending,
        request_id, client_ip, api_key, body.get("model"), bool(body.get("stream", True)),
        len(msg_items or []), len(body.get("tools") or []),
        {}, body, ingress_protocol=ingress_protocol,
    )

    sched_result = m["scheduler"].schedule(
        body, api_key_name=api_key, client_ip=client_ip,
        ingress_protocol=ingress_protocol,
    )
    if not sched_result:  # ← 修复后的判断（覆盖 candidates + saturated）
        from src import errors as er
        resp = er.json_error_response(503, er.ErrType.API, "no candidates")
        await mock_client.aclose()
        return resp, request_id, sched_result, mock_client

    resp = await m["failover"].run_failover(
        sched_result, body, request_id, api_key, client_ip,
        is_stream=bool(body.get("stream", True)), start_time=start,
        ingress_protocol=ingress_protocol,
    )
    return resp, request_id, sched_result, mock_client


# ─── 用例 1：Anthropic 入口 saturated-only → 排队成功 200 ──────────
async def test_anth_saturated_only_queues_then_200(m):
    _setup_anth(m)
    from src import concurrency
    _reset_slots(concurrency)
    _set_concurrency_cfg(m["config"], enabled=True, queue_wait_s=10, default_max=0)

    router = MockRouter()
    router.register("https://cha", lambda r: json_ok_response())
    chA = _make_anth_channel_with_max(m, "chA", "https://cha", max_concurrent=1)
    _install_channels(m, [chA])

    assert await concurrency.try_acquire("api:chA") is True
    assert concurrency.is_saturated("api:chA") is True

    body = {"model": "glm-5", "stream": False, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}

    async def _free_after():
        await asyncio.sleep(0.3)
        concurrency.release("api:chA")

    free_task = asyncio.create_task(_free_after())
    resp, rid, sr, mc = await _call_proxy_fixed(m, router, body)
    await free_task

    assert sr.candidates == [], f"expected empty candidates, got {sr.candidates}"
    assert len(sr.saturated) == 1 and sr.saturated[0][0].key == "api:chA", \
        f"expected saturated [api:chA], got {sr.saturated}"
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    await mc.aclose()
    print("  [PASS] anthropic saturated-only → queue → 200")


async def test_anth_saturated_failure_preserves_tracker_usage(m):
    """排队拿到渠道后的失败结算也必须使用 tracker 的累计 usage。"""
    _setup_anth(m)
    from src import concurrency
    _reset_slots(concurrency)
    _set_concurrency_cfg(m["config"], enabled=True, queue_wait_s=10, default_max=0)
    m["config"].update(
        lambda c: c.setdefault("contentBlacklist", {}).__setitem__(
            "default", ["content_policy_violation"]
        )
    )

    payload = (
        b'data: {"type":"message_start","message":{"id":"x","role":"assistant",'
        b'"usage":{"input_tokens":100,"cache_creation_input_tokens":0,'
        b'"cache_read_input_tokens":20}}}\n\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"input_tokens":0,"output_tokens":10,'
        b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}\n\n'
        b'data: {"type":"content_block_start","index":0,"content_block":'
        b'{"type":"text","text":"content_policy_violation detected"}}\n\n'
    )
    router = MockRouter()
    router.register(
        "https://cha",
        lambda r: httpx.Response(
            200, content=payload, headers={"content-type": "text/event-stream"}
        ),
    )
    chA = _make_anth_channel_with_max(m, "chA", "https://cha", max_concurrent=1)
    _install_channels(m, [chA])
    assert await concurrency.try_acquire("api:chA") is True

    async def _free_after():
        await asyncio.sleep(0.3)
        concurrency.release("api:chA")

    body = {
        "model": "glm-5", "stream": True, "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    }
    free_task = asyncio.create_task(_free_after())
    resp, rid, sr, mc = await _call_proxy_fixed(m, router, body)
    await free_task

    row = m["log_db"]._get_conn().execute(
        """SELECT usage_observed, input_tokens, output_tokens,
                  cache_creation_tokens, cache_read_tokens
             FROM upstream_attempt_usage WHERE root_request_id=?""",
        (rid,),
    ).fetchone()
    assert sr.candidates == [] and len(sr.saturated) == 1
    assert resp.status_code == 503
    assert row is not None
    assert tuple(row) == (1, 100, 10, 0, 20), tuple(row)

    m["config"].update(
        lambda c: c.setdefault("contentBlacklist", {}).__setitem__("default", [])
    )
    await mc.aclose()
    print("  [PASS] anthropic saturated failure preserves tracker usage")


# ─── 用例 2：OpenAI 入口 saturated-only → 真 handler 排队成功 ─────
async def test_oa_saturated_only_queues_then_200(m_oa):
    """OpenAI 入口走真 handler.handle，验证修复后 if not result 不再误判 503。"""
    _setup_oa(m_oa)
    from src import concurrency
    _reset_slots(concurrency)
    # 设置 defaultMaxConcurrent=1，让 OpenAI 渠道（不读 maxConcurrent）也能饱和
    _set_concurrency_cfg(m_oa["config"], enabled=True, queue_wait_s=10, default_max=1)
    _install_keys(m_oa, {"k": {"key": "ccp-test"}})

    chA = _make_oa_channel("chA", "https://chat.example", protocol="openai-chat")
    with m_oa["registry"]._lock:
        m_oa["registry"]._channels = {chA.key: chA}

    def _chat_ok(req):
        body = {
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1,
            "model": "gpt-5.5",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        return httpx.Response(200, json=body,
                              headers={"content-type": "application/json"})

    router = OAMockRouter()
    router.register("https://chat.example", _chat_ok)

    # 占满 slot（defaultMaxConcurrent=1）
    assert await concurrency.try_acquire(chA.key) is True
    assert concurrency.is_saturated(chA.key) is True

    body = {"model": "gpt-5.5", "stream": False,
            "messages": [{"role": "user", "content": "hi"}]}

    async def _free_after():
        await asyncio.sleep(0.3)
        concurrency.release(chA.key)

    free_task = asyncio.create_task(_free_after())
    resp, mc = await _call_openai_handler(m_oa, router, "chat", body)
    await free_task

    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    await mc.aclose()
    print("  [PASS] openai chat saturated-only → real handler → 200")


# ─── 用例 3：回归 - 完全无候选仍 503 ─────────────────────────────
async def test_anth_no_channels_still_503(m):
    _setup_anth(m)
    from src import concurrency
    _reset_slots(concurrency)
    _set_concurrency_cfg(m["config"], enabled=True, queue_wait_s=10, default_max=0)

    _install_channels(m, [])
    router = MockRouter()

    body = {"model": "glm-5", "stream": False, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    resp, rid, sr, mc = await _call_proxy_fixed(m, router, body)
    assert sr.candidates == [] and sr.saturated == []
    assert resp.status_code in (503, 404), \
        f"expected 503/404 when no channels at all, got {resp.status_code}"
    await mc.aclose()
    print(f"  [PASS] anthropic no-channels-at-all → {resp.status_code} (regression)")


# ─── 用例 4：回归 - 排队超时仍 503 ──────────────────────────────
async def test_anth_saturated_queue_timeout_503(m):
    _setup_anth(m)
    from src import concurrency
    _reset_slots(concurrency)
    _set_concurrency_cfg(m["config"], enabled=True, queue_wait_s=1, default_max=0)

    router = MockRouter()
    router.register("https://cha", lambda r: json_ok_response())
    chA = _make_anth_channel_with_max(m, "chA", "https://cha", max_concurrent=1)
    _install_channels(m, [chA])

    assert await concurrency.try_acquire("api:chA") is True

    body = {"model": "glm-5", "stream": False, "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}]}
    t0 = time.time()
    resp, rid, sr, mc = await _call_proxy_fixed(m, router, body)
    elapsed = time.time() - t0

    assert sr.candidates == [] and len(sr.saturated) == 1
    # failover 排队超时时返回 429 Too Many Requests（rate limited 语义），而不是 503
    assert resp.status_code in (429, 503), \
        f"expected 429/503 on queue timeout, got {resp.status_code}"
    assert elapsed >= 0.8, f"expected wait ≥ queueWaitSeconds, elapsed={elapsed:.2f}s"

    concurrency.release("api:chA")
    await mc.aclose()
    print(f"  [PASS] anthropic queue-timeout → {resp.status_code} after {elapsed:.2f}s (regression)")


async def amain() -> int:
    m_anth = _import_anth()
    m_oa = _import_oa()
    orig_anth = json.loads(json.dumps(m_anth["config"].get()))
    orig_oa = json.loads(json.dumps(m_oa["config"].get()))

    print("── saturated-only 修复回归套件 ─────────────────────")
    tests = [
        ("anth_saturated_only", lambda: test_anth_saturated_only_queues_then_200(m_anth)),
        ("anth_saturated_usage", lambda: test_anth_saturated_failure_preserves_tracker_usage(m_anth)),
        ("oa_saturated_only", lambda: test_oa_saturated_only_queues_then_200(m_oa)),
        ("anth_no_channels_still_503", lambda: test_anth_no_channels_still_503(m_anth)),
        ("anth_queue_timeout_503", lambda: test_anth_saturated_queue_timeout_503(m_anth)),
    ]
    passed = 0
    try:
        for name, fn in tests:
            try:
                await fn()
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {name}: {e}")
                import traceback; traceback.print_exc()
            except Exception as e:
                print(f"  [ERR ] {name}: {e}")
                import traceback; traceback.print_exc()
    finally:
        def _restore_anth(c):
            c.clear(); c.update(orig_anth)
        m_anth["config"].update(_restore_anth)
        def _restore_oa(c):
            c.clear(); c.update(orig_oa)
        m_oa["config"].update(_restore_oa)

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
