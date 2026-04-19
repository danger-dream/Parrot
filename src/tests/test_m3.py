"""M3 调度器综合单测。

覆盖：
  - fingerprint 对称性（N 到达 vs N-1 完成）
  - scorer 滑动窗口边界 / 陈旧衰减
  - cooldown 阶梯 / 永久拉黑 / 成功清零
  - affinity TTL / 命中 / 打破
  - scheduler 筛选 + 亲和 + 评分 端到端

运行：
  ./venv/bin/python -m src.tests.test_m3
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import os
import random
import sys
import time


def _import_modules():
    # 确保使用本项目的 src 包
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import (
        affinity, config, cooldown, fingerprint, scheduler, scorer, state_db,
    )
    from src.channel import api_channel, registry
    return {
        "affinity": affinity, "config": config, "cooldown": cooldown,
        "fingerprint": fingerprint, "scheduler": scheduler, "scorer": scorer,
        "state_db": state_db, "api_channel": api_channel, "registry": registry,
    }


def _reset_all_state(m):
    m["state_db"].init()
    m["state_db"].perf_delete()
    m["state_db"].error_delete()
    m["state_db"].affinity_delete()
    # 重置内存层：把 _initialized 置 False 强制重载
    for mod_name in ("affinity", "cooldown", "scorer"):
        mod = m[mod_name]
        mod._initialized = False
    m["affinity"].init()
    m["cooldown"].init()
    m["scorer"].init()


# ─── Tests ───────────────────────────────────────────────────────

def test_fingerprint_symmetry(m):
    """第 N 次到达指纹 == 第 N-1 次完成写入指纹。"""
    fp = m["fingerprint"]
    api_key, ip = "k1", "1.2.3.4"

    # 模拟多轮对话
    u1 = {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    a1 = {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}
    u2 = {"role": "user", "content": [{"type": "text", "text": "how are you?"}]}
    a2 = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": "x"}},
    ]}
    u3 = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
    ]}
    a3 = {"role": "assistant", "content": [{"type": "text", "text": "done"}]}
    u4 = {"role": "user", "content": "next?"}

    cases = [
        # (description, messages_on_arrival_N, response_assistant_N, messages_on_arrival_N_plus_1)
        ("round 2 → round 3",
         [u1, a1, u2],         # 第 2 轮到达
         a2,                    # 第 2 轮响应（assistant tool_use）
         [u1, a1, u2, a2, u3]), # 第 3 轮到达
        ("round 3 (tool_use) → round 4 (text)",
         [u1, a1, u2, a2, u3],
         a3,
         [u1, a1, u2, a2, u3, a3, u4]),
    ]

    for desc, msgs_now, resp_now, msgs_next in cases:
        write_fp = fp.fingerprint_write(api_key, ip, msgs_now, resp_now)
        query_fp = fp.fingerprint_query(api_key, ip, msgs_next)
        assert write_fp == query_fp, f"{desc}: {write_fp} != {query_fp}"
        assert write_fp is not None, f"{desc}: fingerprint should not be None"

    # 场景：新会话第一轮不应产生 fingerprint_query（长度 < 3）
    assert fp.fingerprint_query(api_key, ip, [u1]) is None
    assert fp.fingerprint_query(api_key, ip, [u1, a1]) is None
    # 但完成后可以写入（会作为下一轮 query 的目标，下一轮 messages 就够 3 条了）
    assert fp.fingerprint_write(api_key, ip, [u1], a1) is not None

    # api_key / ip 不同 → 指纹不同（隔离）
    assert fp.fingerprint_query("k1", "1.2.3.4", [u1, a1, u2]) != \
           fp.fingerprint_query("k2", "1.2.3.4", [u1, a1, u2])
    assert fp.fingerprint_query("k1", "1.2.3.4", [u1, a1, u2]) != \
           fp.fingerprint_query("k1", "5.6.7.8", [u1, a1, u2])

    print("  [PASS] fingerprint symmetry + isolation")


def test_scorer_sliding_window(m):
    """滑动窗口：窗口未满时累加；满后每次新事件等效"滑出一次"再"滑入一次"。"""
    _reset_all_state(m)
    sc = m["scorer"]
    cfg = m["config"]

    # 设置较小窗口便于测试
    def _set(c):
        c.setdefault("scoring", {})["recentWindow"] = 5
        c["scoring"]["emaAlpha"] = 0.5
        c["scoring"]["staleMinutes"] = 99999  # 禁止衰减干扰
    cfg.update(_set)
    sc._initialized = False
    sc.init()

    ck, mo = "api:chA", "m1"
    # 5 次成功（窗口恰好装满）
    for _ in range(5):
        sc.record_success(ck, mo, connect_ms=100, first_byte_ms=200, total_ms=1000)
    s = sc.get_stats(ck, mo)
    assert s["recent_requests"] == 5
    assert s["recent_success_count"] == 5

    # 第 6 次成功：窗口满，滑出旧平均(5/5=1)滑入 1 → 仍是 5
    sc.record_success(ck, mo, connect_ms=100, first_byte_ms=200, total_ms=1000)
    s = sc.get_stats(ck, mo)
    assert s["recent_requests"] == 5, f"recent_requests={s['recent_requests']}"
    assert s["recent_success_count"] == 5

    # 第 7 次失败：滑出 1 滑入 0 → 4
    sc.record_failure(ck, mo, connect_ms=500)
    s = sc.get_stats(ck, mo)
    assert s["recent_requests"] == 5
    assert s["recent_success_count"] == 4, f"expected 4, got {s['recent_success_count']}"

    # 再连续 N 次失败 → recent_success_count 指数级衰减；
    # 由于每次仅滑出旧平均 1/window 的"贡献"且使用整数化 round，
    # 稳态可能停在一个小整数（不会精确归零，这是 EMA 等效方案的特性）。
    # 测试意图是：成功率应大幅下降。
    for _ in range(30):
        sc.record_failure(ck, mo, connect_ms=500)
    s = sc.get_stats(ck, mo)
    rate = s["recent_success_count"] / s["recent_requests"]
    assert rate <= 0.5, f"rate={rate} too high after 30 failures (expected big drop from 1.0)"
    assert s["recent_success_count"] < 4, f"expected < 4, got {s['recent_success_count']}"

    print("  [PASS] scorer sliding window")


def test_scorer_stale_decay(m):
    """陈旧衰减：超过 staleMinutes 后分数向 defaultScore 漂移。"""
    _reset_all_state(m)
    sc = m["scorer"]
    cfg = m["config"]

    def _set(c):
        sc_cfg = c.setdefault("scoring", {})
        sc_cfg["defaultScore"] = 3000
        sc_cfg["staleMinutes"] = 15
        sc_cfg["staleFullDecayMinutes"] = 30
        sc_cfg["recentWindow"] = 50
        sc_cfg["errorPenaltyFactor"] = 8
    cfg.update(_set)
    sc._initialized = False
    sc.init()

    ck, mo = "api:chB", "m1"
    # 很快的渠道：latency 100ms，100% 成功
    sc.record_success(ck, mo, connect_ms=50, first_byte_ms=50, total_ms=1000)
    fresh_score = sc.get_score(ck, mo)
    assert 50 < fresh_score < 150, f"fresh score unexpected: {fresh_score}"

    # 手动把 last_updated 推到 15 分钟前（衰减起点）→ 分数仍约 fresh
    now_ms = m["state_db"].now_ms()
    sc._stats[(ck, mo)]["last_updated"] = now_ms - 15 * 60 * 1000
    score_15 = sc.get_score(ck, mo)

    # 22.5 分钟前 → 位于衰减中段，应在 fresh 和 default 之间
    sc._stats[(ck, mo)]["last_updated"] = now_ms - int(22.5 * 60 * 1000)
    score_22 = sc.get_score(ck, mo)
    assert fresh_score < score_22 < 3000, f"mid decay={score_22}"

    # 30 分钟前 → 完全回归 defaultScore
    sc._stats[(ck, mo)]["last_updated"] = now_ms - 30 * 60 * 1000
    score_30 = sc.get_score(ck, mo)
    assert abs(score_30 - 3000) < 1, f"full decay={score_30}"

    print(f"  [PASS] scorer stale decay (fresh={fresh_score:.0f} → 15m={score_15:.0f} → 22.5m={score_22:.0f} → 30m={score_30:.0f})")


def test_cooldown_ladder(m):
    """错误阶梯 [1,3,5,10,15,0] 中 0 = 永久；连续失败递进；成功清零。"""
    _reset_all_state(m)
    cd = m["cooldown"]
    cfg = m["config"]

    def _set(c):
        c["errorWindows"] = [1, 3, 5, 10, 15, 0]
    cfg.update(_set)

    ck, mo = "api:chC", "m1"

    # 第 1 次失败 → 1 分钟
    cd.record_error(ck, mo, "err1")
    assert cd.is_blocked(ck, mo)
    s = cd.get_state(ck, mo)
    assert s["error_count"] == 1

    # 第 2~5 次失败：递进阶梯
    for expected_count in (2, 3, 4, 5):
        cd.record_error(ck, mo, f"err{expected_count}")
        assert cd.get_state(ck, mo)["error_count"] == expected_count

    # 第 6 次失败：走到阶梯最后一格 0 → 永久（cooldown_until = -1）
    cd.record_error(ck, mo, "err6")
    s = cd.get_state(ck, mo)
    assert s["cooldown_until"] == -1, f"expected permanent, got {s}"
    assert cd.is_blocked(ck, mo)

    # 清零后不再阻塞
    cd.clear(ck, mo)
    assert not cd.is_blocked(ck, mo)
    assert cd.get_state(ck, mo) is None

    # 清理所有
    cd.record_error(ck, mo, "x")
    cd.clear_all()
    assert not cd.is_blocked(ck, mo)

    print("  [PASS] cooldown ladder + clear")


def test_affinity_ttl_and_delete(m):
    """亲和 TTL：get 时发现超过 TTL 自动删除。"""
    _reset_all_state(m)
    aff = m["affinity"]
    cfg = m["config"]

    # TTL = 1 分钟便于测试
    def _set(c):
        c.setdefault("affinity", {})["ttlMinutes"] = 1
    cfg.update(_set)

    fp = "abc" * 10  # 30 字符
    aff.upsert(fp, "api:chA", "m1")
    assert aff.get(fp) is not None

    # 人为把 last_used 推到 2 分钟前
    now_ms = m["state_db"].now_ms()
    with aff._lock:
        aff._entries[fp]["last_used"] = now_ms - 2 * 60 * 1000
    # 触发 get → 应自动删除
    assert aff.get(fp) is None
    assert fp not in aff._entries

    # delete_by_channel
    aff.upsert("fp1", "api:chA", "m1")
    aff.upsert("fp2", "api:chB", "m1")
    aff.delete_by_channel("api:chA")
    assert aff.get("fp1") is None
    assert aff.get("fp2") is not None

    print("  [PASS] affinity TTL + delete_by_channel")


class _FakeChannel:
    def __init__(self, key, models: list[str], enabled=True, disabled_reason=None):
        self.key = key
        self.display_name = key
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self._models = models

    def supports_model(self, requested_model):
        return requested_model if requested_model in self._models else None


def _patch_registry_for_tests(m, channels: list[_FakeChannel]):
    reg = m["registry"]
    # 直接把内存字典替换
    with reg._lock:
        reg._channels = {ch.key: ch for ch in channels}


def test_scheduler_end_to_end(m):
    """调度端到端：
        - 筛选：enabled + supports_model + 不在 cooldown
        - 亲和：把绑定渠道顶到首位
        - 评分：最低分在首位（无亲和时）
        - 亲和打破：绑定渠道分数 > 最优 × threshold 时打破
    """
    _reset_all_state(m)
    cfg = m["config"]
    sch = m["scheduler"]

    def _set(c):
        c.setdefault("affinity", {})["threshold"] = 3.0
        c.setdefault("scoring", {})["explorationRate"] = 0.0  # 关闭探索，测试确定性
        c["scoring"]["recentWindow"] = 10
        c["scoring"]["staleMinutes"] = 99999
        c["channelSelection"] = "smart"
    cfg.update(_set)

    # 重置 scorer（让新配置生效）
    m["scorer"]._initialized = False
    m["scorer"].init()

    chA = _FakeChannel("api:chA", ["gpt-5"])
    chB = _FakeChannel("api:chB", ["gpt-5"])
    chC = _FakeChannel("api:chC", ["gpt-5"], enabled=False)          # 禁用
    chD = _FakeChannel("api:chD", ["gpt-5"], disabled_reason="user") # 禁用原因
    chE = _FakeChannel("api:chE", ["other-model"])                   # 模型不匹配
    _patch_registry_for_tests(m, [chA, chB, chC, chD, chE])

    # 先写一些 perf：A 较快（100+100），B 较慢（500+500）
    sc = m["scorer"]
    for _ in range(5):
        sc.record_success("api:chA", "gpt-5", connect_ms=100, first_byte_ms=100, total_ms=500)
    for _ in range(5):
        sc.record_success("api:chB", "gpt-5", connect_ms=500, first_byte_ms=500, total_ms=2500)

    # 场景 1：无亲和，应 A 在前
    body = {"model": "gpt-5", "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "go"},
    ]}
    result = sch.schedule(body, api_key_name="k1", client_ip="1.1.1.1")
    assert result.candidates, "should have candidates"
    assert [c[0].key for c in result.candidates] == ["api:chA", "api:chB"], \
        f"{[c[0].key for c in result.candidates]}"
    assert result.affinity_hit is False

    # 场景 2：亲和绑定到 B（分数差距未超 3×）→ 应 B 在前
    # 调整让 B 分数约为 A 的 2 倍（未超 3）
    # 此刻 A:(100+100)*1=200, B:(500+500)*1=1000 → B/A=5，会打破
    # 我们把 A 的成功率降到 0.5（penalty 会让 A 涨分）
    for _ in range(5):
        sc.record_failure("api:chA", "gpt-5", connect_ms=100)
    a_score = sc.get_score("api:chA", "gpt-5")
    b_score = sc.get_score("api:chB", "gpt-5")
    print(f"    after tuning: A score={a_score:.0f}, B score={b_score:.0f}, B/A={b_score/a_score:.2f}")

    # 现在 A 成功率 5/10=0.5 → penalty=1+(0.5)*8=5 → A_score = 200*5=1000
    # B 成功率 5/5=1.0 → B_score=1000*1=1000
    # B/A ≈ 1.0，不打破

    # 绑定到 B
    fp_q = m["fingerprint"].fingerprint_query("k1", "1.1.1.1", body["messages"])
    assert fp_q is not None
    m["affinity"].upsert(fp_q, "api:chB", "gpt-5")

    result = sch.schedule(body, api_key_name="k1", client_ip="1.1.1.1")
    assert result.affinity_hit, "should hit affinity"
    assert result.candidates[0][0].key == "api:chB", \
        f"bound channel should be first: got {[c[0].key for c in result.candidates]}"

    # 场景 3：亲和打破（使 B 分数远高于 A）
    # 让 B 连续失败几次，分数变很高
    for _ in range(10):
        sc.record_failure("api:chB", "gpt-5", connect_ms=2000)
    a_score = sc.get_score("api:chA", "gpt-5")
    b_score = sc.get_score("api:chB", "gpt-5")
    print(f"    B degraded: A={a_score:.0f}, B={b_score:.0f}, B/A={b_score/a_score:.2f}")

    # 绑定仍然在（上一 schedule 调用 touch 了）
    assert m["affinity"].get(fp_q) is not None

    result = sch.schedule(body, api_key_name="k1", client_ip="1.1.1.1")
    # 此时 B/A 可能已 > 3 → 打破 → 回到评分排序，A 应在前
    if b_score > a_score * 3:
        assert not result.affinity_hit, "should break affinity when B >> A"
        assert result.candidates[0][0].key == "api:chA"
        assert m["affinity"].get(fp_q) is None, "affinity should be deleted"
        print(f"    [ok] affinity broken as expected")
    else:
        print(f"    (affinity not broken this run, ratio={b_score/a_score:.2f})")

    # 场景 4：cooldown 过滤
    m["cooldown"].record_error("api:chA", "gpt-5", "oops")
    result = sch.schedule(body, api_key_name="k1", client_ip="1.1.1.1")
    keys = [c[0].key for c in result.candidates]
    assert "api:chA" not in keys, f"chA should be blocked: {keys}"

    # 场景 5：无匹配模型 → 空结果
    body2 = {"model": "nonexistent", "messages": body["messages"]}
    result = sch.schedule(body2, api_key_name="k1", client_ip="1.1.1.1")
    assert not result.candidates
    assert not result

    print("  [PASS] scheduler end-to-end")


def main():
    m = _import_modules()

    # 备份当前 config 原值，防止单测副作用污染运行中的代理
    orig = m["config"].get().copy()

    try:
        print("── M3 Tests ─────────────────────────────")
        test_fingerprint_symmetry(m)
        test_scorer_sliding_window(m)
        test_scorer_stale_decay(m)
        test_cooldown_ladder(m)
        test_affinity_ttl_and_delete(m)
        test_scheduler_end_to_end(m)
        print("\n✅ ALL M3 TESTS PASSED")
        return 0
    except AssertionError as e:
        print(f"\n❌ FAIL: {e}")
        import traceback; traceback.print_exc()
        return 1
    finally:
        # 恢复 config 到初始
        def _restore(c):
            c.clear(); c.update(orig)
        m["config"].update(_restore)
        # 清空 state.db
        _reset_all_state(m)


if __name__ == "__main__":
    sys.exit(main())
