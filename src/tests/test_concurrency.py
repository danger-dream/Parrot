"""并发限制 (src/concurrency.py) 单元测试。

覆盖：
  - try_acquire / release 基本语义
  - max_concurrent=0 时永远成功（不限）
  - config enabled=False 时旁路
  - acquire_from_candidates：快速路径 / 排队 / FIFO 顺序 / 超时返回 None
  - snapshot / totals
  - rename_channel / forget_channel
  - scheduler 层 saturated 分流
"""

from __future__ import annotations

# 先隔离
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import threading
import time
import os
import sys

import pytest


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, concurrency, config, cooldown, log_db, scheduler, scorer, state_db,
    )
    from src.channel import api_channel, registry
    return {
        "affinity": affinity,
        "concurrency": concurrency,
        "config": config,
        "cooldown": cooldown,
        "log_db": log_db,
        "scheduler": scheduler,
        "scorer": scorer,
        "state_db": state_db,
        "api_channel": api_channel,
        "registry": registry,
    }


def _setup(m):
    """与 test_m4_failover 一致：初始化 state_db / scorer / cooldown / affinity。"""
    m["state_db"].init()
    m["log_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    m["state_db"].client_affinity_delete()
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"]._client_initialized = False
    m["affinity"].init()
    m["affinity"].client_init()
    m["cooldown"].init()
    m["scorer"].init()


def _reset_slots(concurrency):
    """清空全局 slot 状态，避免测试相互污染。"""
    concurrency._slots.clear()
    concurrency._retired_keys.clear()
    concurrency._retired_limits.clear()
    concurrency._release_event = __import__("asyncio").Event()


def _set_cfg(config, concurrency_cfg=None, channels=None):
    def _mut(c):
        if concurrency_cfg is not None:
            c["concurrency"] = concurrency_cfg
        if channels is not None:
            c["channels"] = channels
    config.update(_mut)


# ─── 基本 try_acquire / release ─────────────────────────────────

def test_try_acquire_and_release(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "queueWaitSeconds": 30, "defaultMaxConcurrent": 0},
             channels=[{
                 "name": "t1", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 2,
             }])
    reg.rebuild_from_config()
    ch_key = "api:t1"

    async def run():
        assert await c.try_acquire(ch_key) is True
        assert await c.try_acquire(ch_key) is True
        # 第 3 次：已满
        assert await c.try_acquire(ch_key) is False
        # 释放一个
        c.release(ch_key)
        assert await c.try_acquire(ch_key) is True

    asyncio.run(run())


def test_unlimited_always_ok(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[{
                 "name": "t2", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 0,  # 不限
             }])
    reg.rebuild_from_config()

    async def run():
        # 不限时连续 acquire 都成功
        for _ in range(10):
            assert await c.try_acquire("api:t2") is True

    asyncio.run(run())


def test_global_disabled_bypass(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": False, "defaultMaxConcurrent": 1},
             channels=[{
                 "name": "td", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 1,
             }])
    reg.rebuild_from_config()

    async def run():
        # 总开关关了，即使 max=1 也永远 True
        for _ in range(5):
            assert await c.try_acquire("api:td") is True
        # is_saturated 也永远 False
        assert c.is_saturated("api:td") is False

    asyncio.run(run())


def test_default_max_concurrent(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    # 渠道 maxConcurrent=0 → 用 defaultMaxConcurrent=2
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 2},
             channels=[{
                 "name": "td2", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 0,
             }])
    reg.rebuild_from_config()

    async def run():
        assert await c.try_acquire("api:td2") is True
        assert await c.try_acquire("api:td2") is True
        # 第 3 次被全局默认挡住
        assert await c.try_acquire("api:td2") is False

    asyncio.run(run())


# ─── acquire_from_candidates ────────────────────────────────────

def test_acquire_fast_path(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[
                 {"name": "a", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                  "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                  "cc_mimicry": True, "enabled": True, "maxConcurrent": 1},
                 {"name": "b", "type": "api", "baseUrl": "http://y", "apiKey": "k",
                  "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                  "cc_mimicry": True, "enabled": True, "maxConcurrent": 1},
             ])
    reg.rebuild_from_config()

    async def run():
        # 先占满 a
        assert await c.try_acquire("api:a") is True
        # acquire_from_candidates 应快速路径走 b
        got = await c.acquire_from_candidates([("api:a", "m1"), ("api:b", "m1")], 5.0)
        assert got is not None
        assert got[0] == "api:b"

    asyncio.run(run())


def test_acquire_queue_fifo(m):
    """FIFO 顺序：先排队的先被唤醒。"""
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[{
                 "name": "q1", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 1,
             }])
    reg.rebuild_from_config()

    async def run():
        # 占满
        assert await c.try_acquire("api:q1") is True

        order: list[str] = []

        async def waiter(name: str, delay: float):
            # 注册时间错开，保证 FIFO 顺序明确
            await asyncio.sleep(delay)
            got = await c.acquire_from_candidates([("api:q1", "m1")], 5.0)
            assert got is not None
            order.append(name)

        t1 = asyncio.create_task(waiter("first", 0.02))
        t2 = asyncio.create_task(waiter("second", 0.06))
        t3 = asyncio.create_task(waiter("third", 0.10))

        # 等所有 waiter 都挂上队列
        await asyncio.sleep(0.25)
        # 逐个释放，观察 order
        c.release("api:q1")
        await asyncio.sleep(0.15)
        c.release("api:q1")
        await asyncio.sleep(0.15)
        c.release("api:q1")
        await asyncio.wait_for(asyncio.gather(t1, t2, t3), timeout=3.0)
        assert order == ["first", "second", "third"], f"FIFO broken: {order}"

    asyncio.run(run())


def test_acquire_timeout_returns_none(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[{
                 "name": "to1", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 1,
             }])
    reg.rebuild_from_config()

    async def run():
        assert await c.try_acquire("api:to1") is True
        # 满了，没人释放 → 小超时期后返回 None
        got = await c.acquire_from_candidates([("api:to1", "m1")], 0.3)
        assert got is None

    asyncio.run(run())


def test_acquire_cross_channel_wakeup(m):
    """候选多个渠道时，任一释放就应唤醒排队方。"""
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[
                 {"name": "x1", "type": "api", "baseUrl": "http://a", "apiKey": "k",
                  "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                  "cc_mimicry": True, "enabled": True, "maxConcurrent": 1},
                 {"name": "x2", "type": "api", "baseUrl": "http://b", "apiKey": "k",
                  "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                  "cc_mimicry": True, "enabled": True, "maxConcurrent": 1},
             ])
    reg.rebuild_from_config()

    async def run():
        # 两个都占满
        assert await c.try_acquire("api:x1") is True
        assert await c.try_acquire("api:x2") is True

        async def waiter():
            return await c.acquire_from_candidates(
                [("api:x1", "m1"), ("api:x2", "m1")], 5.0,
            )

        t = asyncio.create_task(waiter())
        await asyncio.sleep(0.08)
        # 释放 x2 → waiter 应拿到 x2
        c.release("api:x2")
        got = await asyncio.wait_for(t, timeout=3.0)
        assert got is not None
        assert got[0] == "api:x2"

    asyncio.run(run())


# ─── snapshot / totals / rename / forget ────────────────────────

def test_snapshot_and_totals(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[{
                 "name": "sn1", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 3,
             }])
    reg.rebuild_from_config()

    async def run():
        await c.try_acquire("api:sn1")
        await c.try_acquire("api:sn1")
        snap = c.snapshot()
        assert any(r["channel_key"] == "api:sn1" and r["in_flight"] == 2
                   and r["max_concurrent"] == 3 for r in snap)
        totals = c.totals()
        assert totals["in_flight"] >= 2
        # 释放清零
        c.release("api:sn1")
        c.release("api:sn1")
        totals = c.totals()
        assert totals["in_flight"] == 0

    asyncio.run(run())


def test_rename_and_forget(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[{
                 "name": "rn1", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m1", "alias": "m1"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 5,
             }])
    reg.rebuild_from_config()

    async def run():
        await c.try_acquire("api:rn1")
        await c.try_acquire("api:rn2")
        assert "api:rn1" in c._slots
        destination = c._slots["api:rn2"]
        c.rename_channel("api:rn1", "api:rn2")
        # Active old work keeps releasing by old key; destination state is not
        # overwritten. The final old release drains and removes the retired slot.
        assert c._slots["api:rn1"].in_flight == 1
        assert c._slots["api:rn2"] is destination
        assert destination.in_flight == 1
        c.release("api:rn1")
        assert "api:rn1" not in c._slots
        assert c._slots["api:rn2"].in_flight == 1
        # 释放 + forget destination
        c.release("api:rn2")
        c.forget_channel("api:rn2")
        assert "api:rn2" not in c._slots
        # 在途时 forget 不应删除
        await c.try_acquire("api:rn2")
        c.forget_channel("api:rn2")
        assert "api:rn2" in c._slots

    asyncio.run(run())


def test_rename_drains_old_waiter_and_unlimited_slot_without_touching_destination(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 1},
             channels=[{
                 "name": "drain-old", "type": "api", "baseUrl": "http://x", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m", "alias": "m"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 1,
             }, {
                 "name": "drain-new", "type": "api", "baseUrl": "http://y", "apiKey": "k",
                 "protocol": "anthropic", "models": [{"real": "m", "alias": "m"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 1,
             }])
    reg.rebuild_from_config()

    async def run():
        assert await c.try_acquire("api:drain-old")
        assert await c.try_acquire("api:drain-new")
        destination = c._slots["api:drain-new"]
        waiter = asyncio.create_task(
            c.acquire_from_candidates([("api:drain-old", "payload")], 2)
        )
        for _ in range(100):
            if c._slots["api:drain-old"].waiters:
                break
            await asyncio.sleep(0.001)
        assert c._slots["api:drain-old"].waiters

        c.rename_channel("api:drain-old", "api:drain-new")
        c.release("api:drain-old")
        assert await waiter == ("api:drain-old", "payload")
        assert c._slots["api:drain-new"] is destination
        assert destination.in_flight == 1
        c.release("api:drain-old")
        assert "api:drain-old" not in c._slots
        c.release("api:drain-new")

        # Unlimited slots use an early release path and must drain too.
        cfg.update(lambda x: x.setdefault("concurrency", {}).__setitem__("defaultMaxConcurrent", 0))
        assert await c.try_acquire("api:unlimited-old")
        c.rename_channel("api:unlimited-old", "api:unlimited-new")
        c.release("api:unlimited-old")
        assert "api:unlimited-old" not in c._slots

        # A retired custom limit must not refresh to default unlimited after
        # the old key disappears from registry; only one waiter may pass.
        def freeze_cfg(x):
            x.setdefault("concurrency", {})["defaultMaxConcurrent"] = 0
            x["channels"] = [{
                "name": "freeze-old", "type": "api", "baseUrl": "http://f", "apiKey": "***",
                "protocol": "anthropic", "models": [{"real": "m", "alias": "m"}],
                "cc_mimicry": True, "enabled": True, "maxConcurrent": 1,
            }]
        cfg.update(freeze_cfg)
        reg.rebuild_from_config()
        assert await c.try_acquire("api:freeze-old")
        waiters = [
            asyncio.create_task(
                c.acquire_from_candidates([("api:freeze-old", index)], 2)
            )
            for index in (1, 2)
        ]
        for _ in range(100):
            if len(c._slots["api:freeze-old"].waiters) == 2:
                break
            await asyncio.sleep(0.001)
        assert len(c._slots["api:freeze-old"].waiters) == 2
        c.rename_channel("api:freeze-old", "api:freeze-new")
        c.release("api:freeze-old")
        await asyncio.sleep(0.02)
        assert sum(task.done() for task in waiters) == 1
        assert c._slots["api:freeze-old"].max_concurrent == 1
        first_done = next(task for task in waiters if task.done())
        assert (await first_done)[0] == "api:freeze-old"
        c.release("api:freeze-old")
        second_pending = next(task for task in waiters if task is not first_done)
        assert (await second_pending)[0] == "api:freeze-old"
        c.release("api:freeze-old")
        assert "api:freeze-old" not in c._slots

        # Cancellation immediately after wake must not strand a retired slot.
        cfg.update(lambda x: x.setdefault("concurrency", {}).__setitem__("defaultMaxConcurrent", 1))
        assert await c.try_acquire("api:cancel-old")
        cancelled_waiter = asyncio.create_task(
            c.acquire_from_candidates([("api:cancel-old", "payload")], 2)
        )
        for _ in range(100):
            if c._slots["api:cancel-old"].waiters:
                break
            await asyncio.sleep(0.001)
        c.rename_channel("api:cancel-old", "api:cancel-new")
        c.release("api:cancel-old")
        cancelled_waiter.cancel()
        try:
            await cancelled_waiter
        except asyncio.CancelledError:
            pass
        assert "api:cancel-old" not in c._slots

    asyncio.run(run())


def test_cross_thread_late_pre_acquire_cannot_detach_or_overwrite_frozen_limit(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    _setup(m); _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[{
                 "name": "thread-old", "type": "api", "baseUrl": "http://x", "apiKey": "***",
                 "protocol": "anthropic", "models": [{"real": "m", "alias": "m"}],
                 "cc_mimicry": True, "enabled": True, "maxConcurrent": 1,
             }])
    reg.rebuild_from_config()
    entered = threading.Event()
    allow = threading.Event()
    rename_done = threading.Event()
    errors = []
    real_get_max = c._get_channel_max

    def paused_get_max(key):
        if key == "api:thread-old":
            entered.set(); assert allow.wait(5)
        return real_get_max(key)

    c._get_channel_max = paused_get_max

    def acquire_worker():
        try:
            assert asyncio.run(c.try_acquire("api:thread-old"))
        except BaseException as exc:
            errors.append(exc)

    def rename_worker():
        try:
            c.rename_channel("api:thread-old", "api:thread-new", frozen_max=1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            rename_done.set()

    first = threading.Thread(target=acquire_worker)
    second = threading.Thread(target=rename_worker)
    try:
        first.start(); assert entered.wait(5)
        second.start(); time.sleep(0.05)
        assert not rename_done.is_set()
        allow.set(); first.join(5); second.join(5)
    finally:
        allow.set(); first.join(5); second.join(5)
        c._get_channel_max = real_get_max

    assert not errors
    assert c._slots["api:thread-old"].max_concurrent == 1
    assert c._slots["api:thread-old"].in_flight == 1
    c.release("api:thread-old")
    assert "api:thread-old" not in c._slots


# ─── scheduler 分流 ────────────────────────────────────────────

def test_scheduler_saturated_split(m):
    c = m["concurrency"]
    cfg = m["config"]
    reg = m["registry"]
    sch = m["scheduler"]
    _setup(m)
    _reset_slots(c)
    _set_cfg(cfg,
             concurrency_cfg={"enabled": True, "defaultMaxConcurrent": 0},
             channels=[
                 {"name": "sat1", "type": "api", "baseUrl": "http://a", "apiKey": "k",
                  "protocol": "anthropic", "models": [{"real": "mx", "alias": "mx"}],
                  "cc_mimicry": True, "enabled": True, "maxConcurrent": 1},
                 {"name": "sat2", "type": "api", "baseUrl": "http://b", "apiKey": "k",
                  "protocol": "anthropic", "models": [{"real": "mx", "alias": "mx"}],
                  "cc_mimicry": True, "enabled": True, "maxConcurrent": 0},
             ])
    # apiKeys 随便加一个以满足调度器要求
    def _setk(cc):
        cc.setdefault("apiKeys", {})["kx"] = {"key": "kkx", "allowedModels": []}
    cfg.update(_setk)
    reg.rebuild_from_config()

    async def run():
        # 占满 sat1 的当前 generation slot（public key 仍用于断言/UI）。
        sat1 = reg.get_channel("api:sat1")
        assert sat1 is not None
        assert await c.try_acquire(sat1.state_key) is True
        res = sch.schedule(
            {"model": "mx", "messages": []},
            api_key_name="kx",
            client_ip="1.2.3.4",
            ingress_protocol="anthropic",
        )
        cand_keys = [ch.key for ch, _ in res.candidates]
        sat_keys = [ch.key for ch, _ in res.saturated]
        assert "api:sat2" in cand_keys, cand_keys
        assert "api:sat1" in sat_keys, sat_keys
        assert "api:sat1" not in cand_keys

    asyncio.run(run())
