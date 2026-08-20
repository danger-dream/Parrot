"""API Provider usage adapter/cache/coordinator/UI contract tests."""
from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest


def _import_modules():
    from src import config, provider_usage, state_db
    from src.channel import registry
    from src.providers.catalog import get_preset
    from src.telegram.menus import channel_menu
    return {"config": config, "provider_usage": provider_usage, "state_db": state_db,
            "registry": registry, "catalog_get": get_preset, "channel_menu": channel_menu}


def _ch(provider="deepseek", preset="standard", key="secret-key", name="one"):
    return SimpleNamespace(provider_id=provider, provider_preset_id=preset, api_key=key,
                           type="api", key=f"api:{name}", display_name=name, models=[])


@pytest.fixture(autouse=True)
def setup(m):
    m["state_db"].init()
    m["state_db"]._get_conn().execute("DELETE FROM api_provider_usage_cache")
    m["state_db"]._get_conn().commit()
    with m["provider_usage"]._GUARD:
        m["provider_usage"]._INFLIGHT.clear()
        m["provider_usage"]._RUNTIME.clear()


def test_exact_catalog_mapping_is_6_providers_13_presets(m):
    pu = m["provider_usage"]
    assert len(pu.SPECS) == 13
    assert {p for p, _ in pu.SPECS} == {"zhipu", "kimi", "deepseek", "openrouter", "minimax", "siliconflow"}
    for pair in pu.SPECS:
        assert m["catalog_get"](*pair) is not None
    assert pu.spec_for(_ch("zhipu", "api-cn")) is None
    assert pu.spec_for(_ch(None, None)) is None
    assert pu.spec_for(_ch("custom", "standard")) is None


def test_account_identity_is_hmac_and_scoped(m):
    pu = m["provider_usage"]
    a = pu.account_id(_ch("deepseek", "standard", "same"))
    b = pu.account_id(_ch("deepseek", "standard", "same", "two"))
    c = pu.account_id(_ch("openrouter", "standard", "same"))
    assert a == b and a != c and "same" not in a
    assert len(a) == 68 and a.startswith("pu1:")
    secret = m["state_db"].schema_meta_get("provider_usage_hmac_secret_v1")
    assert secret and "same" not in secret


def test_balance_adapters_preserve_decimals_and_drop_pii(m):
    pu = m["provider_usage"]
    cases = [
        (_ch("kimi", "api-cn"), {"data": {"available_balance": "12.3400", "voucher_balance": "2", "cash_balance": "10.34", "currency": "CNY"}}, 3),
        (_ch("deepseek"), {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "9.90", "granted_balance": "1.20", "topped_up_balance": "8.70"}]}, 3),
        (_ch("openrouter"), {"data": {"usage_daily": 1.25, "usage_weekly": "2.5", "usage_monthly": 3, "usage": "10", "byok_usage": "0.5", "limit": "20", "limit_remaining": "10", "is_free_tier": False, "rate_limit": {"requests": 1}}}, 7),
        (_ch("minimax", "api-cn"), {"available_balance": "5.00", "owed_amount": "0.10"}, 2),
        (_ch("siliconflow", "api-cn"), {"data": {"totalBalance": "8.00", "chargeBalance": "3", "balance": "5", "status": "active", "id": "PII", "name": "Alice", "email": "a@b.c", "image": "x"}}, 3),
    ]
    for ch, payload, count in cases:
        snap = pu.parse_payload(pu.spec_for(ch), payload)
        assert len(snap["balances"]) == count
        encoded = json.dumps(snap)
        assert "rate_limit" not in encoded and "PII" not in encoded and "Alice" not in encoded and "a@b.c" not in encoded
    assert cases[0][1]["data"]["available_balance"] == "12.3400"
    assert pu.parse_payload(pu.spec_for(cases[0][0]), cases[0][1])["balances"][0]["value"] == "12.3400"


def test_plan_parsers_do_not_invent_units_or_unlimited_exhaustion(m):
    pu = m["provider_usage"]
    kimi = pu.parse_payload(pu.spec_for(_ch("kimi", "code")), {"usage": {"used": 7}, "limits": [{"name": "rolling", "used": 7, "limit": 10, "resetAt": "tomorrow"}]})
    assert kimi["windows"][0]["label"] == "rolling" and "token" not in json.dumps(kimi).lower()
    mini = pu.parse_payload(pu.spec_for(_ch("minimax", "token-cn")), {"model_remains": [
        {"model_name": "m1", "current_interval": {"status": "unlimited", "total": 0, "usage": 99},
         "weekly": {"total": 100, "usage": 25, "remaining": 75, "reset_time": "soon"}},
        {"model_name": "m2", "current_interval": {"total": 10, "usage": 2}},
    ]})
    assert len(mini["windows"]) == 3
    assert "used_percent" not in mini["windows"][0]
    assert mini["windows"][1]["used_percent"] == 25
    assert not any(w["label"] == "合计" for w in mini["windows"])


def test_zhipu_unit_mapping_mcp_total_bjt_and_whitelist(m):
    pu = m["provider_usage"]
    spec = pu.spec_for(_ch("zhipu", "coding-cn"))
    snap = pu.parse_payload(spec, {"data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 74, "currentValue": 740, "usage": 1000, "nextResetTime": 1700000000000},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 23, "currentValue": 230, "usage": 1000, "nextResetTime": 1700604800000},
        {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 5, "currentValue": 233, "usage": 4000, "remaining": 3767, "nextResetTime": 1701209600000, "usageDetails": [{"identity": "discard"}]},
    ], "unknown": {"huge": "raw"}}}, kind="quota")
    assert [w["id"] for w in snap["windows"]] == ["tokens_5h", "tokens_7d", "mcp_month"]
    assert snap["windows"][2]["used"] == "233" and snap["windows"][2]["total"] == "4000"
    assert snap["windows"][0]["reset_at"] == "2023-11-15T06:13:20+08:00"
    encoded = json.dumps(snap)
    assert "discard" not in encoded and "huge" not in encoded and "raw" not in encoded


def test_zhipu_live_model_and_tool_shapes_are_distinct_bounded_and_ui_visible(m, monkeypatch):
    pu, menu = m["provider_usage"], m["channel_menu"]
    ch = _ch("zhipu", "coding-global")
    spec = pu.spec_for(ch)
    models = [{"modelName": f"glm-{i}", "totalTokens": i, "sortOrder": i,
               "tokensUsage": [0], "identity": "discard"} for i in range(10)]
    model = pu.parse_payload(spec, {"data": {"totalUsage": {
        "totalModelCallCount": 12, "totalTokensUsage": 345,
        "modelSummaryList": models}, "x_time": ["discard"]}}, kind="model")
    tools = [{"toolCode": f"tool-{i}", "toolName": f"Tool {i}",
              "totalUsageCount": i, "usageCount": [0]} for i in range(10)]
    tool = pu.parse_payload(spec, {"data": {"totalUsage": {
        "totalNetworkSearchCount": 9, "totalSearchMcpCount": 8,
        "totalWebReadMcpCount": 7, "totalZreadMcpCount": 6,
        "toolSummaryList": tools}, "toolDetails": [{"identity": "discard"}]}}, kind="tool")
    assert [x["id"] for x in model["counters"][:2]] == ["model_calls", "trend_tokens"]
    assert len([x for x in model["counters"] if x.get("kind") == "distribution"]) == 8
    assert [x["id"] for x in tool["counters"]] == ["mcp_total", "network_search", "web_read", "zread"]
    assert all(Decimal(x["value"]) != 0 for x in tool["counters"])
    snapshot = pu._merge(spec, [model, tool], [])
    assert "discard" not in json.dumps(snapshot) and "model_scope_mismatch" in snapshot["notices"]
    monkeypatch.setattr(pu, "cached", lambda channel: {"status": "fresh", "snapshot": snapshot})
    detail = "\n".join(menu._usage_detail_lines(ch))
    assert "近 24 小时模型用量" in detail and "模型分布" in detail
    assert "不同统计口径" in detail and "近 24 小时工具调用" in detail
    assert "MCP 总调用" in detail and "Tool 0" not in detail


def test_kimi_duration_and_absolute_reset_fields_remain_distinct_in_parser_and_ui(m, monkeypatch):
    pu, menu = m["provider_usage"], m["channel_menu"]
    ch = _ch("kimi", "code")
    snap = pu.parse_payload(pu.spec_for(ch), {"limits": [
        {"detail": {"used": 4, "total": 10, "window": {"label": "rolling", "reset_in": 3600}}},
        {"detail": {"usage": 2, "limit": 8}, "window": {"name": "weekly", "resetTime": 1700000000}},
        {"detail": {"current": 1, "remaining": 9, "ttl": 7200}},
    ]})
    rolling, weekly, unknown = snap["windows"]
    assert rolling["reset_in_seconds"] == 3600 and "reset_at" not in rolling
    assert weekly["reset_at"] == "2023-11-15T06:13:20+08:00"
    assert "reset_at" not in unknown and "reset_in_seconds" not in unknown
    monkeypatch.setattr(pu, "cached", lambda channel: {"status": "fresh", "snapshot": snap})
    rendered = menu._usage_summary(ch) + "\n" + "\n".join(menu._usage_detail_lines(ch))
    assert "1970" not in rendered and "重置 未知" not in rendered and "3600" not in rendered
    assert all(x["kind"] == "window" and x["unit"] == "count" for x in snap["windows"])
    assert "token" not in json.dumps(snap).lower() and "request" not in json.dumps(snap).lower()


def test_minimax_absolute_reset_uses_epoch_ms_not_duration_and_keeps_unlimited(m):
    pu = m["provider_usage"]
    snap = pu.parse_payload(pu.spec_for(_ch("minimax", "token-cn")), {"model_remains": [{
        "model_name": "m1", "current_interval_total_count": 0,
        "current_interval_usage_count": 99, "current_interval_status": "unlimited",
        "start_time": 1699996400000, "end_time": 1700000000000, "remains_time": 123456,
        "current_weekly_total_count": 100, "current_weekly_usage_count": 25,
        "weekly_start_time": 1700000000000, "weekly_end_time": 1700604800000,
        "weekly_remains_time": 654321,
    }]})
    current, weekly = snap["windows"]
    assert current["status"] == "unlimited" and "used_percent" not in current
    assert current["reset_at"] == current["end_at"] == "2023-11-15T06:13:20+08:00"
    assert weekly["reset_at"] == weekly["end_at"] == "2023-11-22T06:13:20+08:00"
    assert "123456" not in json.dumps(snap) and "654321" not in json.dumps(snap)


def test_failure_retains_success_cache_and_is_secret_free(m):
    pu, db = m["provider_usage"], m["state_db"]
    ch = _ch(key="TOP-SECRET")
    aid = pu.account_id(ch)
    old = {"source": "deepseek", "balances": [{"label": "总余额", "value": "9.9"}], "windows": [], "counters": [], "notices": [], "partial": False}
    db.provider_usage_save_success(aid, "deepseek", old)
    db.provider_usage_save_error(aid, "deepseek", "上游拒绝当前 Key", int(time.time() * 1000) + 60000)
    row = db.provider_usage_load(aid)
    assert json.loads(row["snapshot_json"]) == old
    assert row["last_error"] == "上游拒绝当前 Key"
    assert "TOP-SECRET" not in json.dumps(row)
    assert pu.cached(ch)["status"] in {"fresh", "stale_error"}


@pytest.mark.asyncio
async def test_zhipu_all_failed_preserves_429_retry_after_delta_and_http_date(m, monkeypatch):
    pu = m["provider_usage"]
    spec = pu.spec_for(_ch("zhipu", "coding-cn"))
    now = 1_700_000_000
    monkeypatch.setattr(pu.time, "time", lambda: now)
    headers = [
        {"Retry-After": "120"},
        {"Retry-After": "Tue, 14 Nov 2023 22:18:20 GMT"},  # now + 300s
        {},
    ]
    calls = 0
    async def fail_get(client, url, key, *, raw_auth=False):
        nonlocal calls
        response = __import__("httpx").Response(
            429, headers=headers[calls], content=b"SECRET RESPONSE BODY",
            request=__import__("httpx").Request("GET", url),
        )
        calls += 1
        raise __import__("httpx").HTTPStatusError("limited", request=response.request, response=response)
    monkeypatch.setattr(pu, "_get", fail_get)
    with pytest.raises(pu.ProviderUsageError) as caught:
        await pu.fetch(spec, "TOP-SECRET-KEY")
    assert caught.value.failure.message == "上游请求频率受限"
    assert caught.value.failure.retry_after == 300
    assert "SECRET" not in str(caught.value)


@pytest.mark.asyncio
async def test_zhipu_partial_429_saves_snapshot_and_persists_retry_deadline(m, monkeypatch):
    pu, db = m["provider_usage"], m["state_db"]
    ch = _ch("zhipu", "coding-cn", key="partial")
    aid, spec = pu.account_id(ch), pu.spec_for(ch)
    snap = pu._merge(spec, [pu.parse_payload(spec, {"data": {"limits": []}}, kind="quota")],
                     [pu.UpstreamFailure("上游请求频率受限", 600)])
    monkeypatch.setattr(pu, "fetch", lambda spec, key: asyncio.sleep(0, result=dict(snap)))
    monkeypatch.setattr(pu, "_still_live", lambda account: account == aid)
    await pu.start()
    try:
        assert pu.schedule_refresh(ch)
        for _ in range(100):
            if aid not in pu._INFLIGHT: break
            await asyncio.sleep(.01)
        row, runtime = db.provider_usage_load(aid), pu._RUNTIME[aid]
        saved = json.loads(row["snapshot_json"])
        assert saved["partial"] is True and "_retry_after_seconds" not in saved
        assert row["retry_after"] == runtime["retry_after"]
        assert runtime["next_refresh_at"] == runtime["retry_after"]
        assert runtime["retry_after"] >= int(time.time() * 1000) + 598_000
    finally:
        await pu.stop()


def test_registry_key_change_and_delete_last_account_clean_cache(m):
    pu, db, registry, config = m["provider_usage"], m["state_db"], m["registry"], m["config"]
    original = json.loads(json.dumps(config.get()))
    try:
        def add(name, key):
            registry.add_api_channel({
                "name": name, "baseUrl": "https://api.deepseek.com", "apiKey": key,
                "protocol": "anthropic", "providerId": "deepseek",
                "providerPresetId": "standard", "models": [], "enabled": True,
            })
        add("usage-life-a", "old-shared")
        add("usage-life-b", "old-shared")
        first = registry.get_channel("api:usage-life-a")
        old_aid = pu.account_id(first)
        db.provider_usage_save_success(old_aid, "deepseek", {"source": "deepseek"})
        assert registry.delete_api_channel("usage-life-a")
        assert db.provider_usage_load(old_aid) is not None  # still shared
        registry.update_api_channel("usage-life-b", {"apiKey": "replacement"})
        assert db.provider_usage_load(old_aid) is None
        replacement = registry.get_channel("api:usage-life-b")
        new_aid = pu.account_id(replacement)
        db.provider_usage_save_success(new_aid, "deepseek", {"source": "deepseek"})
        assert registry.delete_api_channel("usage-life-b")
        assert db.provider_usage_load(new_aid) is None
    finally:
        config.update(lambda cfg: (cfg.clear(), cfg.update(original)))
        registry.rebuild_from_config()


def test_account_cleanup_keeps_shared_then_deletes_last_cache_and_runtime(m, monkeypatch):
    pu, db, registry = m["provider_usage"], m["state_db"], m["registry"]
    first, second = _ch(key="shared-cleanup", name="first"), _ch(key="shared-cleanup", name="second")
    aid = pu.account_id(first)
    db.provider_usage_save_success(aid, "deepseek", {"source": "deepseek"})
    pu._RUNTIME[aid] = {"last_attempt": 1, "next_refresh_at": 2, "retry_after": 0}
    live = [second]
    monkeypatch.setattr(registry, "all_channels", lambda: list(live))
    assert not pu.cleanup_account_if_orphaned(aid)
    assert db.provider_usage_load(aid) is not None and aid in pu._RUNTIME
    live.clear()
    assert pu.cleanup_account_if_orphaned(aid)
    assert db.provider_usage_load(aid) is None and aid not in pu._RUNTIME


@pytest.mark.asyncio
async def test_singleflight_same_key_and_lifecycle_late_result(m, monkeypatch):
    pu, db = m["provider_usage"], m["state_db"]
    ch1, ch2 = _ch(name="one"), _ch(name="two")
    calls = []
    gate = asyncio.Event()
    async def fake_fetch(spec, key):
        calls.append((spec.adapter, key))
        await gate.wait()
        return {"source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", fake_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda aid: True)
    await pu.start()
    try:
        assert pu.schedule_refresh(ch1)
        assert not pu.schedule_refresh(ch2)
        await asyncio.sleep(0)
        gate.set()
        aid = pu.account_id(ch1)
        for _ in range(100):
            if db.provider_usage_load(aid) is not None: break
            await asyncio.sleep(.01)
        assert len(calls) == 1 and db.provider_usage_load(aid)["snapshot_json"]

        # 渠道被删除/换 Key 后，迟到成功不得写入。
        ch3 = _ch(key="late", name="late")
        late_aid = pu.account_id(ch3)
        monkeypatch.setattr(pu, "_still_live", lambda aid: False)
        assert pu.schedule_refresh(ch3)
        for _ in range(100):
            if late_aid not in pu._INFLIGHT: break
            await asyncio.sleep(.01)
        assert db.provider_usage_load(late_aid) is None

        # 迟到失败同样不得创建 error-only 行。
        failed = _ch(key="late-error", name="late-error")
        failed_aid = pu.account_id(failed)
        async def fail_fetch(spec, key):
            raise RuntimeError("上游服务暂时不可用")
        monkeypatch.setattr(pu, "fetch", fail_fetch)
        assert pu.schedule_refresh(failed)
        for _ in range(100):
            if failed_aid not in pu._INFLIGHT: break
            await asyncio.sleep(.01)
        assert db.provider_usage_load(failed_aid) is None
    finally:
        await pu.stop()


def test_startup_refresh_schedules_each_supported_account_once(m, monkeypatch):
    pu = m["provider_usage"]
    channels = [
        _ch("deepseek", "standard", key="shared", name="one"),
        _ch("deepseek", "standard", key="shared", name="two"),
        _ch("openrouter", "standard", key="shared", name="router"),
        _ch("custom", "standard", key="ignored", name="custom"),
    ]
    scheduled = []
    monkeypatch.setattr(
        pu, "schedule_refresh",
        lambda channel, *, force=False: scheduled.append((channel.display_name, force)) or True,
    )

    result = pu.schedule_startup_refresh(channels)

    assert result == {
        "supported_channels": 3,
        "supported_accounts": 2,
        "scheduled_accounts": 2,
    }
    assert scheduled == [("one", True), ("router", True)]


@pytest.mark.asyncio
async def test_schedule_returns_before_slow_provider(m, monkeypatch):
    pu = m["provider_usage"]
    gate = asyncio.Event()
    async def slow_fetch(spec, key):
        await gate.wait()
        return {"source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", slow_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda aid: False)
    await pu.start()
    try:
        started = time.monotonic()
        assert pu.schedule_refresh(_ch(key="slow-provider"))
        assert time.monotonic() - started < .1
        gate.set()
        await asyncio.sleep(.01)
    finally:
        await pu.stop()


def test_runtime_not_started_returns_false_without_inflight(m):
    pu = m["provider_usage"]
    ch = _ch(key="offline")
    assert not pu.schedule_refresh(ch)
    assert pu.account_id(ch) not in pu._INFLIGHT


def test_no_refresh_disables_server_mount_helper_and_lifecycle_order(m, monkeypatch):
    pu = m["provider_usage"]
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    assert not pu.is_enabled()
    monkeypatch.setenv("PARROT_NO_REFRESH", "0")
    assert pu.is_enabled()
    server_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "server.py")
    with open(server_path, encoding="utf-8") as handle:
        source = handle.read()
    assert source.index("registry.install_config_reload_hook()") < source.index("await provider_usage.start()")
    assert source.index("await provider_usage.start()") < source.index("provider_usage.schedule_startup_refresh()")
    assert source.index("provider_usage.schedule_startup_refresh()") < source.index("tgbot.start()")
    stop_pos = source.index("await provider_usage.stop()")
    assert stop_pos < source.index("_finalize_state_db()", stop_pos)


@pytest.mark.asyncio
async def test_single_queue_one_scheduler_three_workers_idempotent_and_threadsafe(m, monkeypatch):
    pu = m["provider_usage"]
    gate = asyncio.Event()
    calls = []
    async def fake_fetch(spec, key):
        calls.append(key)
        await gate.wait()
        return {"source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", fake_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda aid: False)
    await pu.start()
    try:
        queue = pu._QUEUE
        scheduler = pu._SCHEDULER_TASK
        workers = list(pu._WORKER_TASKS)
        assert isinstance(queue, asyncio.Queue)
        assert scheduler is not None and scheduler.get_name() == "provider-usage-scheduler"
        assert len(workers) == 3 and all(not task.done() for task in workers)
        await pu.start()
        assert pu._QUEUE is queue and pu._SCHEDULER_TASK is scheduler and pu._WORKER_TASKS == workers

        # schedule_refresh 自身不能创建 thread 或 per-refresh task。
        with monkeypatch.context() as mp:
            mp.setattr(pu.threading, "Thread", lambda *a, **k: pytest.fail("refresh created a thread"))
            before = set(asyncio.all_tasks())
            assert pu.schedule_refresh(_ch(key="main-loop"))
            await asyncio.sleep(0)
            assert set(asyncio.all_tasks()) == before

        # Telegram 等非 loop 线程可快速、安全预约到同一个 queue。
        result = []
        started = time.monotonic()
        thread = threading.Thread(target=lambda: result.append(pu.schedule_refresh(_ch(key="tg-thread"))))
        thread.start()
        thread.join(timeout=1)
        assert result == [True] and time.monotonic() - started < .2
        for _ in range(20):
            if len(calls) == 2: break
            await asyncio.sleep(.005)
        assert set(calls) == {"main-loop", "tg-thread"}
        gate.set()
    finally:
        await pu.stop()
    assert pu._QUEUE is None and pu._SCHEDULER_TASK is None and pu._WORKER_TASKS == []
    assert not pu._INFLIGHT and not pu._RUNTIME


@pytest.mark.asyncio
async def test_fixed_three_worker_concurrency_limit(m, monkeypatch):
    pu = m["provider_usage"]
    gate = asyncio.Event()
    active = maximum = completed = 0
    async def fake_fetch(spec, key):
        nonlocal active, maximum, completed
        active += 1
        maximum = max(maximum, active)
        try:
            await gate.wait()
        finally:
            active -= 1
            completed += 1
        return {"source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", fake_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda aid: False)
    await pu.start()
    try:
        for i in range(9):
            assert pu.schedule_refresh(_ch(key=f"key-{i}", name=str(i)))
        for _ in range(100):
            if active == 3: break
            await asyncio.sleep(.01)
        assert active == maximum == 3
        gate.set()
        for _ in range(100):
            if completed == 9: break
            await asyncio.sleep(.01)
        assert completed == 9 and maximum == 3
    finally:
        await pu.stop()


def test_adapter_ttls_are_deadlines_not_timers(m):
    pu, db = m["provider_usage"], m["state_db"]
    for ch in (_ch("openrouter", "standard", key="ttl-60"), _ch("deepseek", "standard", key="ttl-300")):
        aid, spec = pu.account_id(ch), pu.spec_for(ch)
        db.provider_usage_save_success(aid, spec.adapter, {"source": spec.adapter})
        runtime = pu._initial_runtime(aid, spec)
        assert runtime["next_refresh_at"] - runtime["last_attempt"] == spec.ttl * 1000
    assert pu.spec_for(_ch("openrouter", "standard")).ttl == 60
    assert pu.spec_for(_ch("deepseek", "standard")).ttl == 300


@pytest.mark.asyncio
async def test_startup_preheat_uses_shared_queue_dedupes_and_respects_retry(m, monkeypatch):
    pu, db = m["provider_usage"], m["state_db"]
    shared1 = _ch(key="shared-startup", name="one")
    shared2 = _ch(key="shared-startup", name="two")
    router = _ch("openrouter", "standard", key="router-startup", name="router")
    backed_off = _ch("deepseek", "standard", key="backoff", name="backoff")
    backed_aid = pu.account_id(backed_off)
    db.provider_usage_save_error(backed_aid, "deepseek", "上游请求频率受限", int(time.time() * 1000) + 120_000)
    calls = []
    async def fake_fetch(spec, key):
        calls.append(key)
        return {"source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", fake_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda aid: False)
    await pu.start()
    queue = pu._QUEUE
    try:
        result = pu.schedule_startup_refresh([shared1, shared2, router, backed_off])
        assert result == {"supported_channels": 4, "supported_accounts": 3, "scheduled_accounts": 2}
        assert pu._QUEUE is queue
        for _ in range(100):
            if len(calls) == 2: break
            await asyncio.sleep(.01)
        assert set(calls) == {"shared-startup", "router-startup"}
        assert "backoff" not in calls
    finally:
        await pu.stop()


@pytest.mark.asyncio
async def test_ttl_scan_is_memory_only_after_first_seen_and_due_only(m, monkeypatch):
    pu = m["provider_usage"]
    ch = _ch("openrouter", "standard", key="ttl")
    monkeypatch.setattr(pu, "_live_channels", lambda: [ch])
    gate = asyncio.Event()
    async def fake_fetch(spec, key):
        await gate.wait()
        return {"source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", fake_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda aid: False)
    await pu.start()
    try:
        aid = pu.account_id(ch)
        now_ms = int(time.time() * 1000)
        pu._RUNTIME[aid] = {"last_attempt": now_ms, "next_refresh_at": now_ms + 60_000, "retry_after": 0}
        monkeypatch.setattr(m["state_db"], "provider_usage_load", lambda aid: pytest.fail("TTL tick read SQLite"))
        assert pu._scan(force=False)["scheduled_accounts"] == 0
        pu._RUNTIME[aid]["next_refresh_at"] = now_ms - 1
        assert pu._scan(force=False)["scheduled_accounts"] == 1
        await asyncio.sleep(0)
        gate.set()
    finally:
        await pu.stop()


@pytest.mark.asyncio
async def test_worker_success_error_and_retry_update_deadline_preserve_stale(m, monkeypatch):
    pu, db = m["provider_usage"], m["state_db"]
    success_ch = _ch("openrouter", "standard", key="success")
    error_ch = _ch("deepseek", "standard", key="error")
    stale = {"source": "deepseek", "balances": [{"label": "总余额", "value": "9"}], "windows": [], "counters": [], "notices": [], "partial": False}
    error_aid = pu.account_id(error_ch)
    db.provider_usage_save_success(error_aid, "deepseek", stale)
    response = __import__("httpx").Response(429, headers={"Retry-After": "120"}, request=__import__("httpx").Request("GET", "https://example.invalid"))
    async def fake_fetch(spec, key):
        if key == "error":
            raise __import__("httpx").HTTPStatusError("limited", request=response.request, response=response)
        return {"source": spec.adapter, "balances": [], "windows": [], "counters": [], "notices": [], "partial": False}
    monkeypatch.setattr(pu, "fetch", fake_fetch)
    monkeypatch.setattr(pu, "_still_live", lambda aid: True)
    await pu.start()
    try:
        # Existing success is force-refreshable after manual minimum, while stale survives error.
        db._get_conn().execute("UPDATE api_provider_usage_cache SET fetched_at=? WHERE account_id=?", (int(time.time() * 1000) - 9000, error_aid))
        db._get_conn().commit()
        assert pu.schedule_refresh(success_ch)
        assert pu.schedule_refresh(error_ch, force=True)
        for _ in range(100):
            if not pu._INFLIGHT: break
            await asyncio.sleep(.01)
        success_rt = pu._RUNTIME[pu.account_id(success_ch)]
        error_rt = pu._RUNTIME[error_aid]
        assert success_rt["next_refresh_at"] > success_rt["last_attempt"]
        assert success_rt["retry_after"] == 0
        assert error_rt["retry_after"] == error_rt["next_refresh_at"]
        assert error_rt["next_refresh_at"] >= int(time.time() * 1000) + 118_000
        row = db.provider_usage_load(error_aid)
        assert json.loads(row["snapshot_json"]) == stale and row["last_error"] == "上游请求频率受限"
    finally:
        await pu.stop()


def test_zhipu_oauth_style_list_order_detail_groups_and_bjt_cache(m, monkeypatch):
    pu, menu = m["provider_usage"], m["channel_menu"]
    ch = _ch("zhipu", "coding-cn")
    spec = pu.spec_for(ch)
    quota = pu.parse_payload(spec, {"data": {"limits": [
        {"type": "TIME_LIMIT", "unit": 5, "number": 1, "currentValue": 233, "usage": 4000, "percentage": 5, "nextResetTime": 1701209600000},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 23, "nextResetTime": 1700604800000},
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 74, "nextResetTime": 1700000000000},
    ]}}, kind="quota")
    model = pu.parse_payload(spec, {"data": {"totalUsage": {"totalModelCallCount": 805, "totalTokensUsage": 121607293},
        "modelSummaryList": [{"modelName": "glm-a", "totalTokens": 100}, {"modelName": "glm-b", "totalTokens": 300}]}}, kind="model")
    tool = pu.parse_payload(spec, {"data": {"totalUsage": {"totalSearchMcpCount": 203,
        "totalNetworkSearchCount": 201, "totalWebReadMcpCount": 2, "totalZreadMcpCount": 0},
        "toolSummaryList": [{"toolName": "重复项", "totalUsageCount": 203}]}}, kind="tool")
    snap = pu._merge(spec, [quota, model, tool], [])
    monkeypatch.setattr(pu, "cached", lambda channel: {"status": "fresh", "fetched_at": 1700000000000, "snapshot": snap})
    summary = menu._usage_summary(ch)
    assert summary.index("5h") < summary.index("7d") < summary.index("MCP 月度")
    assert "233 / 4,000" in summary and "11-15 06:13" in summary
    detail = "\n".join(menu._usage_detail_lines(ch))
    assert detail.index("上游账户额度") < detail.index("近 24 小时模型用量") < detail.index("近 24 小时工具调用")
    assert "121.6M" in detail and "glm-a 25.0%" in detail and "不同统计口径" in detail
    assert "MCP 总调用：<code>203 次</code>" in detail and "网页读取：<code>2 次</code>" in detail
    assert "重复项" not in detail and "Zread" not in detail and "更新于 06:13:20" in detail


def test_usage_time_uses_bjt_and_omits_date_for_today_in_list(m):
    menu = m["channel_menu"]
    now = datetime.now(timezone(timedelta(hours=8)))
    now_ms = int(now.timestamp() * 1000)
    assert menu._usage_time(now_ms, detail=False) == now.strftime("%H:%M")
    assert menu._usage_time(now_ms, detail=True) == "今天 " + now.strftime("%H:%M")


def test_provider_aware_balance_openrouter_and_token_plan_renderers(m, monkeypatch):
    pu, menu = m["provider_usage"], m["channel_menu"]
    fixtures = [
        (_ch("kimi", "api-cn"), pu.parse_payload(pu.spec_for(_ch("kimi", "api-cn")), {"data": {"available_balance": "12.34", "cash_balance": "10", "currency": "CNY"}}), "可用余额", "现金余额"),
        (_ch("openrouter", "standard"), pu.parse_payload(pu.spec_for(_ch("openrouter", "standard")), {"data": {"limit_remaining": 10, "limit": 20, "usage_daily": 1, "usage_weekly": 2, "usage_monthly": 3}}), "Key 剩余", "今日用量"),
        (_ch("minimax", "token-cn"), pu.parse_payload(pu.spec_for(_ch("minimax", "token-cn")), {"model_remains": [{"model_name": "MiniMax-M2", "current_interval_total_count": 100, "current_interval_usage_count": 25, "end_time": 1700000000000, "current_weekly_total_count": 1000, "current_weekly_usage_count": 200, "weekly_end_time": 1700604800000}]}), "MiniMax-M2 · 当前周期", "MiniMax-M2 · 每周"),
    ]
    for ch, snap, list_expected, detail_expected in fixtures:
        monkeypatch.setattr(pu, "cached", lambda channel, snap=snap: {"status": "fresh", "fetched_at": 1700000000000, "snapshot": snap})
        assert list_expected in menu._usage_summary(ch)
        assert detail_expected in "\n".join(menu._usage_detail_lines(ch))


def test_cache_implementation_status_is_hidden_but_useful_failure_remains(m, monkeypatch):
    pu, menu = m["provider_usage"], m["channel_menu"]
    ch = _ch("zhipu", "coding-cn")
    old = {"source": "zhipu-coding", "windows": [{"label": "5 小时额度 · 分桶 1", "used_percent": 1}], "balances": [], "counters": [], "notices": []}
    monkeypatch.setattr(pu, "cached", lambda channel: {"status": "stale", "snapshot": old})
    rendered = menu._usage_summary(ch) + "\n" + "\n".join(menu._usage_detail_lines(ch))
    assert "暂无法展示" in rendered
    assert "缓存" not in rendered and "后台" not in rendered and "刷新" not in rendered
    for status in ("not_fetched", "refreshing"):
        monkeypatch.setattr(pu, "cached", lambda channel, status=status: {"status": status})
        rendered = menu._usage_summary(ch) + "\n" + "\n".join(menu._usage_detail_lines(ch))
        assert "上游用量尚未获取" in rendered
        assert "缓存" not in rendered and "后台" not in rendered and "刷新" not in rendered
    snap = {"source": "zhipu-coding", "version": 2, "windows": [], "balances": [], "counters": [], "notices": []}
    monkeypatch.setattr(pu, "cached", lambda channel: {"status": "stale_error", "snapshot": snap})
    assert "最近一次更新失败" in menu._usage_summary(ch)


def test_ui_formats_cached_usage_and_callback_is_short(m, monkeypatch):
    pu, menu = m["provider_usage"], m["channel_menu"]
    ch = _ch("deepseek", "standard")
    monkeypatch.setattr(pu, "cached", lambda channel: {"status": "fresh", "fetched_at": 1,
        "snapshot": {"balances": [{"label": "总余额", "value": "12.34", "currency": "CNY"}], "windows": [], "counters": [], "notices": [], "partial": False}})
    summary = menu._usage_summary(ch)
    detail = "\n".join(menu._usage_detail_lines(ch))
    assert "💰 总余额：<b>12.34 CNY</b>" in summary
    assert "上游账户额度" in detail and "Parrot 本地统计" not in detail
    payload = "ch:usage:abcdefgh:999999"
    assert len(payload.encode()) <= 64 and "deepseek" not in payload and "secret" not in payload
