"""Upstream error handling rules: engine, failover integration and Management API."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from src.tests import _isolation

_isolation.isolate()

from src import config as config_module  # noqa: E402
from src import error_rule_effects, error_rules  # noqa: E402
from src.error_rule_catalog import cond, group, notify, pause, retry  # noqa: E402
from src.tests.test_protocol_fake_upstreams import (  # noqa: E402
    MockRouter, _call_openai_handler, _chat_response, _default_key, _import_modules,  # noqa: F401
    _install_channels, _install_keys, _make_openai_channel, _setup,
)

BJT = timezone(timedelta(hours=8))
MINIMAX_2067 = ('HTTP 500: {"error":{"message":"当前已达到 Token Plan 用量上限。为避免调用中断，请升级 Token Plan 套餐，'
                '或购买积分补充用量并开启积分自动消耗。 (2067)","code":"server_error"},"base_resp":{"status_code":1002}}')


def _now_ms() -> int:
    return int(time.time() * 1000)


def _reset_text(minutes: int) -> tuple[str, int]:
    when = (datetime.now(BJT) + timedelta(minutes=minutes)).replace(microsecond=0)
    return when.strftime("%Y-%m-%d %H:%M:%S"), int(when.timestamp() * 1000)


def _obs(body: str, *, provider: str = "", status: int | None = None, model: str = "m",
         channel_key: str = "api:x", channel_type: str = "api", phase: str = "http"):
    if status is None and body.startswith("HTTP "):
        status = int(body[5:8])
    return error_rules.observation_from_parts(
        phase=phase, status=status, outcome="http_error", provider=provider, channel_key=channel_key,
        model=model, body=body, channel_type=channel_type)


def _rule(*items, mode="all", **overrides):
    rule = {"id": "rule-test", "name": "测试规则", "category": "quota_exhausted",
            "when": group(mode, *(items or (cond("status", "in", values=["429"]),)))}
    rule.update(overrides)
    return rule


def _set_rules(rules=(), presets=None, enabled=True):
    def mutate(cfg):
        cfg["errorRules"] = {"enabled": enabled, "rules": list(rules), "presets": presets or {}}
    config_module.update(mutate)


def _settings():
    return error_rules.normalize_settings(config_module.get()["errorRules"])


def _first(trace_rule, obs):
    row = error_rules.explain(obs, {"enabled": True, "rules": [error_rules.normalize_rule(trace_rule)],
                                    "presets": {k: {"enabled": False} for k in error_rules.PRESET_IDS}})["trace"][0]
    return row["status"], row["reason"]


@pytest.fixture(autouse=True)
def _clean_rules():
    _set_rules()
    error_rule_effects._notified.clear()
    error_rule_effects._counters.clear()
    yield
    _set_rules()


# ── engine: rules and conditions ─────────────────────────────────────


def test_rule_needs_a_condition_and_takes_category_handling():
    with pytest.raises(error_rules.ErrorRuleValidationError, match="至少设置一个条件"):
        error_rules.normalize_rule({"id": "x", "name": "x", "category": "observe", "when": group("all")})
    rule = error_rules.normalize_rule(_rule())
    assert [s["type"] for s in rule["steps"]] == ["pause", "notify"]
    assert rule["steps"][0]["mode"] == "message_time" and rule["final"] == "next"
    assert rule["response"] == {"status": 429, "message": "", "retryAfter": True}
    busy = error_rules.normalize_rule(_rule(category="upstream_busy"))
    assert [s["type"] for s in busy["steps"]] == ["retry", "pause", "deprioritize"]
    with pytest.raises(error_rules.ErrorRuleValidationError, match="preset."):
        error_rules.normalize_rule(_rule(id="preset.mine"))
    with pytest.raises(error_rules.ErrorRuleValidationError, match="嵌套重复"):
        error_rules.normalize_rule(_rule(cond("message", "regex", values=["(a+)+$"])))
    with pytest.raises(error_rules.ErrorRuleValidationError, match="以 \\$ 开头"):
        error_rules.normalize_rule(_rule(cond("path", "eq", path="error.code", values=["1"])))
    with pytest.raises(error_rules.ErrorRuleValidationError, match="还没有填值"):
        error_rules.normalize_rule(_rule(cond("message", "contains", values=[])))
    with pytest.raises(error_rules.ErrorRuleValidationError, match="时间窗口"):
        error_rules.normalize_rule(_rule(thresholds=[{"count": 3, "mode": "consecutive", "scope": "rule",
                                                      "steps": [notify()]}]))
    with pytest.raises(error_rules.ErrorRuleValidationError, match="处理步骤的类型无效"):
        error_rules.normalize_rule(_rule(thresholds=[{"count": 3, "steps": [retry()]}]))


def test_codes_are_found_in_fields_and_in_bracketed_text():
    obs = _obs(MINIMAX_2067, provider="minimax")
    assert "2067" in obs.codes and "server_error" in obs.codes
    assert obs.message.startswith("当前已达到 Token Plan 用量上限")
    zhipu = _obs('HTTP 429: {"type":"error","error":{"type":"rate_limit_error","code":"1308",'
                 '"message":"[1308][已达到 5 小时的使用上限。您的限额将在 2026-11-20 19:52:32 重置。][x]"}}')
    assert zhipu.codes[:2] == ("1308", "rate_limit_error")


def test_json_paths_operators_and_nested_groups():
    obs = _obs(MINIMAX_2067, provider="minimax", model="MiniMax-M3.1")
    # $.base_resp.status_code ≠ 0 且 ($.error.message 包含 x 或 $..message 包含 Token Plan)
    rule = _rule(cond("path", "ne", path="$.base_resp.status_code", values=["0"]),
                 group("any", cond("path", "contains", path="$.error.message", values=["nothing"]),
                       cond("path", "contains", path="$..message", values=["token plan"])))
    assert _first(rule, obs) == ("matched", "")
    assert _first(_rule(cond("path", "eq", path="$.base_resp.status_code", values=["0"])), obs) == (
        "missed", "「$.base_resp.status_code 等于 0」不成立（实际是 1002）")
    assert _first(_rule(cond("path", "gte", path="$['base_resp'].status_code", values=["1000"])), obs)[0] == "matched"
    assert _first(_rule(cond("path", "exists", path="$.error.code")), obs)[0] == "matched"
    assert _first(_rule(cond("path", "not_exists", path="$.error.param")), obs)[0] == "matched"
    # A path that reaches nothing makes every test but 不存在 false, including 不等于.
    assert _first(_rule(cond("path", "ne", path="$.nope", values=["x"])), obs)[1] == "「$.nope 不等于 “x”」不成立（取不到这个值）"
    assert _first(_rule(cond("model", "in", values=["minimax-m3*"])), obs)[0] == "matched"
    assert _first(_rule(cond("model", "in", values=["glm-*"])), obs)[0] == "missed"
    assert _first(_rule(cond("status", "in", values=["5xx"])), obs)[0] == "matched"
    assert _first(_rule(cond("status", "in", values=["400-499"])), obs)[0] == "missed"
    assert _first(_rule(cond("code", "in", values=["2060-2070"])), obs)[0] == "matched"
    assert _first(_rule(cond("provider", "in", values=["zhipu"])), obs)[1] == "「提供商 是其中之一 智谱」不成立（实际是 MiniMax）"
    either = _rule(cond("message", "contains", values=["foo"]), cond("body", "contains", values=["bar"]), mode="any")
    assert _first(either, obs)[1].startswith("以下条件都不成立：")
    list_obs = _obs('HTTP 400: {"errors":[{"code":"a"},{"code":"ctx_too_long"}]}')
    assert _first(_rule(cond("path", "in", path="$.errors[*].code", values=["ctx_too_long"])), list_obs)[0] == "matched"
    assert _first(_rule(cond("path", "eq", path="$.errors[0].code", values=["ctx_too_long"])), list_obs)[0] == "missed"


def test_channel_and_account_type_conditions():
    obs = _obs('HTTP 429: {"error":{"code":"1302"}}', provider="zhipu", channel_key="oauth:zhipu:a", channel_type="oauth")
    assert _first(_rule(cond("channelType", "eq", values=["oauth"])), obs)[0] == "matched"
    assert _first(_rule(cond("channelType", "eq", values=["api"])), obs)[0] == "missed"
    assert _first(_rule(cond("channel", "in", values=["oauth:zhipu:a"])), obs)[0] == "matched"
    assert _first(_rule(cond("channel", "not_in", values=["oauth:zhipu:a"])), obs)[0] == "missed"


def test_user_rule_wins_over_preset_and_pass_rules_hand_over():
    obs = _obs(MINIMAX_2067, provider="minimax")
    assert error_rules.match(obs).id == "preset.minimax-token-plan"

    _set_rules([_rule(cond("code", "in", values=["2067"]), id="mine", category="upstream_busy")])
    assert error_rules.match(obs).id == "mine"

    _set_rules([_rule(cond("code", "in", values=["2067"]), id="watch", category="observe")])
    result = error_rules.explain(obs, _settings())
    assert [(r["id"], r["status"]) for r in result["trace"] if r["status"] == "matched"] == [
        ("watch", "matched"), ("preset.minimax-token-plan", "matched")]
    assert result["matched"]["id"] == "preset.minimax-token-plan"
    assert [m.id for m in error_rules.match_chain(obs)] == ["watch", "preset.minimax-token-plan"]

    _set_rules(presets={"preset.minimax-token-plan": {"enabled": False}})
    assert error_rules.match(obs) is None
    _set_rules([_rule(cond("code", "in", values=["2067"]), id="mine")], enabled=False)
    assert error_rules.match(obs) is None


def test_provider_of_every_channel_kind():
    claude = SimpleNamespace(type="oauth", key="oauth:claude:someone@example.com")
    openai = SimpleNamespace(type="oauth", key="oauth:openai:someone@example.com:ws1")
    zhipu = SimpleNamespace(type="oauth", key="oauth:zhipu:bigmodel:x", provider="zhipu")
    kimi = SimpleNamespace(type="api", key="api:Kimi", provider_id="kimi")
    minimax = SimpleNamespace(type="api", key="api:MM", base_url="https://api.minimaxi.com/anthropic")
    assert [error_rules.provider_of(c) for c in (claude, openai, zhipu, kimi, minimax)] == [
        "claude", "openai", "zhipu", "kimi", "minimax"]


def test_recovery_time_sources():
    now = _now_ms()
    text, reset_ms = _reset_text(90)
    obs = _obs('HTTP 429: {"code":6004,"msg":"您的使用量已超出频率限制，将在 ' + text + ' UTC+8 重置，您也可以切换其他模型继续使用。"}')
    assert error_rules.resolve_until(pause("message_time"), obs, now_ms=now) == (reset_ms, "message_time")

    no_time = _obs('HTTP 429: {"code":"x","message":"quota exhausted"}')
    assert error_rules.resolve_until(pause("message_time"), no_time, now_ms=now) == (now + 900_000, "fallback")

    with_header = error_rules.observation_from_parts(
        phase="http", status=429, outcome="http_error", provider="", channel_key="", model="m",
        body="{}", retry_after=42)
    assert error_rules.resolve_until(pause("retry_after", fallback=60), with_header, now_ms=now) == (
        now + 42_000, "retry_after")

    step = pause("boundary", boundary=5)
    until, source = error_rules.resolve_until(step, no_time, now_ms=now)
    hour = datetime.fromtimestamp(until / 1000, BJT)
    assert source == "boundary" and hour.hour % 5 == 0 and hour.minute == 0 and 0 < until - now <= 5 * 3600_000
    late = int(datetime(2026, 11, 20, 21, 30, tzinfo=BJT).timestamp() * 1000)
    assert error_rules.resolve_until(step, no_time, now_ms=late)[0] == int(
        datetime(2026, 11, 21, 0, 0, tzinfo=BJT).timestamp() * 1000)

    preset = error_rules.find_rule("preset.minimax-token-plan")
    assert error_rules.resolve_until(preset.pause_steps()[0], _obs(MINIMAX_2067), now_ms=now)[1] == "boundary"
    past = _obs('HTTP 429: {"message":"将在 2020-01-01 00:00:00 重置"}')
    assert error_rules.resolve_until(pause("message_time"), past, now_ms=now)[1] == "fallback"


def test_client_message_template_with_paths():
    rule = error_rules.normalize_rule(_rule(response={
        "status": 429, "retryAfter": True,
        "message": "{model} 额度用尽（{$.error.message}，码 {$.base_resp.status_code}{$.nope}），{reset_in}后恢复 {unknown}"}))
    now = _now_ms()
    obs = _obs(MINIMAX_2067)
    response = error_rules.client_response(rule, obs=obs, model="glm-5.3", until_ms=now + 7_200_000, now_ms=now)
    assert response["status"] == 429 and response["retryAfter"] == 7200
    assert response["message"].startswith("glm-5.3 额度用尽（当前已达到 Token Plan 用量上限")
    assert "码 1002）" in response["message"] and response["message"].endswith("2 h后恢复 {unknown}")


# ── failover ─────────────────────────────────────────────────────────


def _channels(m, router, first_handler):
    channels = [
        _make_openai_channel("first", "https://first.example", protocol="openai-chat", alias="m", real="m"),
        _make_openai_channel("second", "https://second.example", protocol="openai-chat", alias="m", real="m"),
    ]
    router.register("https://first.example", first_handler)
    router.register("https://second.example", lambda req: _chat_response("second ok"))
    _install_channels(m, channels)
    for ch in channels:
        m["cooldown"].clear(ch.key, "m", notify_recovered=False)
    return channels


async def _chat(m, router, text="hi"):
    resp, client = await _call_openai_handler(m, router, "chat", {
        "model": "m", "stream": False, "messages": [{"role": "user", "content": text}]})
    await client.aclose()
    return resp


def _hosts(router):
    return [str(r.url).split("/")[2].split(".")[0] for r in router.requests]


def _retry_rows(m, channel_key):
    conn = m["log_db"]._get_conn()
    return conn.execute(
        "SELECT error_rule_id, error_category FROM retry_chain WHERE channel_key=? ORDER BY id DESC LIMIT 1",
        (channel_key,)).fetchone()


def _quota_error(text):
    return lambda req: httpx.Response(429, json={"error": {"code": "quota", "message": f"额度用尽，将在 {text} 重置"}})


async def test_quota_rule_pauses_until_reset_and_answers_429_while_paused(m):
    _setup(m)
    _install_keys(m, _default_key())
    text, reset_ms = _reset_text(120)
    _set_rules([_rule(cond("status", "in", values=["429"]), cond("message", "contains", values=["额度用尽"]), id="mine")])
    router = MockRouter()
    first, second = _channels(m, router, _quota_error(text))

    resp = await _chat(m, router)
    assert resp.status_code == 200 and b"second ok" in resp.body
    state = m["cooldown"].get_state(first.key, "m")
    assert state["cooldown_until"] == reset_ms and state["rule_id"] == "mine" and state["error_count"] == 0
    assert tuple(_retry_rows(m, first.key)) == ("mine", "quota_exhausted")

    # Both channels now exhausted: the rule's answer replaces the generic 503.
    router.register("https://second.example", _quota_error(text))
    m["cooldown"].clear(first.key, "m", notify_recovered=False)
    resp = await _chat(m, router)
    assert resp.status_code == 429
    assert 7000 < int(resp.headers["Retry-After"]) <= 7200
    assert "Expected to recover at" in json.loads(resp.body)["error"]["message"]

    sent = len(router.requests)
    resp = await _chat(m, router)
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) > 7000
    assert len(router.requests) == sent  # paused channels are not contacted again


async def test_respond_final_returns_upstream_error_without_failover(m):
    _setup(m)
    _install_keys(m, _default_key())
    _set_rules([_rule(cond("path", "eq", path="$.error.code", values=["context_too_long"]),
                      id="bad", category="request_invalid")])
    router = MockRouter()
    first, _second = _channels(m, router, lambda req: httpx.Response(
        400, json={"error": {"code": "context_too_long", "message": "prompt is too long"}}))
    resp = await _chat(m, router)
    assert resp.status_code == 400 and b"prompt is too long" in resp.body
    assert _hosts(router) == ["first"]
    assert m["cooldown"].get_state(first.key, "m") is None


async def test_steps_run_in_order_retry_is_a_checkpoint(m, monkeypatch):
    _setup(m)
    _install_keys(m, _default_key())
    paused = []
    real_run = error_rule_effects.run_steps

    def spy(channel, model, found, obs, steps, **kw):
        paused.append([s["type"] for s in steps])
        return real_run(channel, model, found, obs, steps, **kw)

    monkeypatch.setattr(error_rule_effects, "run_steps", spy)
    _set_rules([_rule(cond("status", "in", values=["500"]), id="busy", category="upstream_busy",
                      steps=[retry(0, 2), pause("fixed", seconds=60), retry(0, 1), notify(1)])])
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": "busy"}})

    router = MockRouter()
    first, _second = _channels(m, router, flaky)
    resp = await _chat(m, router)
    assert resp.status_code == 200
    # 1 attempt + 2 retries, pause, 1 more retry, notify, then the next channel.
    assert _hosts(router) == ["first"] * 4 + ["second"]
    assert paused == [["pause"], ["notify"]]
    assert m["cooldown"].get_state(first.key, "m")["rule_id"] == "busy"


async def test_retry_success_stops_the_steps(m):
    _setup(m)
    _install_keys(m, _default_key())
    _set_rules([_rule(cond("status", "in", values=["503"]), id="blip", category="upstream_busy",
                      steps=[retry(0, 1), pause("fixed", seconds=600)])])
    replies = iter([httpx.Response(503, json={"error": {"message": "blip"}}), _chat_response("first ok")])
    router = MockRouter()
    first, _second = _channels(m, router, lambda req: next(replies))
    resp = await _chat(m, router)
    assert resp.status_code == 200 and b"first ok" in resp.body
    assert _hosts(router) == ["first", "first"]
    assert m["cooldown"].get_state(first.key, "m") is None


async def test_consecutive_threshold_fires_and_success_resets_it(m, monkeypatch):
    _setup(m)
    _install_keys(m, _default_key())
    notified = []
    monkeypatch.setattr(error_rule_effects, "_notify",
                        lambda ch, model, found, obs, **kw: notified.append(kw.get("trigger")))
    _set_rules([_rule(cond("status", "in", values=["429"]), id="limit", category="rate_limit", steps=[],
                      thresholds=[{"count": 3, "mode": "consecutive", "scope": "model",
                                   "steps": [pause("fixed", seconds=1800), notify(1)]}])])
    state = {"fail": True}
    router = MockRouter()
    first, _second = _channels(m, router, lambda req: httpx.Response(429, json={"error": {"message": "slow down"}})
                               if state["fail"] else _chat_response("first ok"))
    prior = config_module.get().get("channelSelection")
    monkeypatch.setattr(m["affinity"], "client_get", lambda *a, **k: None)
    config_module.update(lambda c: c.__setitem__("channelSelection", "order"))
    try:
        await _run_threshold_rounds(m, router, first, state, notified)
    finally:
        config_module.update(lambda c: c.__setitem__("channelSelection", prior or "smart"))


async def _run_threshold_rounds(m, router, first, state, notified):

    # Distinct conversations, so session affinity never pins them to the second channel.
    for i in range(2):
        assert (await _chat(m, router, f"a{i}")).status_code == 200
    assert m["cooldown"].get_state(first.key, "m") is None and notified == []

    # A success in between restarts the count.
    state["fail"] = False
    assert b"first ok" in (await _chat(m, router, "ok")).body
    state["fail"] = True
    for i in range(2):
        await _chat(m, router, f"b{i}")
    assert m["cooldown"].get_state(first.key, "m") is None

    before = _now_ms()
    await _chat(m, router, "c")
    cd = m["cooldown"].get_state(first.key, "m")
    assert cd["rule_id"] == "limit" and 1790_000 <= cd["cooldown_until"] - before <= 1810_000
    assert notified == ["连续 3 次"]


async def test_pass_rule_marks_then_later_rule_handles(m, monkeypatch):
    _setup(m)
    _install_keys(m, _default_key())
    notified = []
    monkeypatch.setattr(error_rule_effects, "_notify", lambda ch, model, found, *a, **k: notified.append(found.id))
    text, reset_ms = _reset_text(60)
    _set_rules([
        _rule(cond("status", "in", values=["429"]), id="watch", category="observe", steps=[notify(1)]),
        _rule(cond("message", "contains", values=["额度用尽"]), id="quota"),
    ])
    router = MockRouter()
    first, _second = _channels(m, router, _quota_error(text))
    resp = await _chat(m, router)
    assert resp.status_code == 200
    assert notified == ["watch", "quota"]
    assert m["cooldown"].get_state(first.key, "m")["cooldown_until"] == reset_ms
    assert tuple(_retry_rows(m, first.key)) == ("quota", "quota_exhausted")


def test_window_threshold_counts_within_minutes(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(error_rule_effects.time, "monotonic", lambda: clock[0])
    ran = []
    monkeypatch.setattr(error_rule_effects, "run_steps",
                        lambda *a, **kw: ran.append(kw["trigger"]) or {"until": None, "paused": False})
    found = error_rules.RuleMatch(error_rules.normalize_rule(_rule(thresholds=[
        {"count": 3, "mode": "window", "windowMinutes": 10, "scope": "rule", "steps": [notify(1)]}])), "user")
    ch = SimpleNamespace(key="api:a", type="api", name="a")
    obs = _obs('HTTP 429: {}')
    for step in (0, 300, 700):  # the first hit falls out of the 10-minute window
        clock[0] = 1000.0 + step
        error_rule_effects.record_hit(ch, "m", found, obs, message="")
    assert ran == []
    clock[0] = 1000.0 + 800
    error_rule_effects.record_hit(ch, "m", found, obs, message="")
    assert ran == ["10 分钟内 3 次"]
    assert error_rule_effects.progress(found, "api:a", "m") == [0]


async def test_threshold_pause_is_not_shortened_and_stops_retries(m):
    _setup(m)
    _install_keys(m, _default_key())
    # Every hit: retry once, then pause 1 minute. The 2nd consecutive hit: pause 30 minutes.
    _set_rules([_rule(cond("status", "in", values=["429"]), id="limit", category="rate_limit",
                      steps=[retry(0, 1), pause("fixed", seconds=60)],
                      thresholds=[{"count": 2, "mode": "consecutive", "scope": "model",
                                   "steps": [pause("fixed", seconds=1800)]}])])
    router = MockRouter()
    first, _second = _channels(m, router, lambda req: httpx.Response(429, json={"error": {"message": "slow down"}}))
    before = _now_ms()
    resp = await _chat(m, router)
    assert resp.status_code == 200
    # The first hit asks for a retry; the retried hit reaches the threshold, which pauses the
    # model for 30 minutes: no further retry, and the 1-minute step pause does not shorten it.
    assert _hosts(router) == ["first", "first", "second"]
    until = m["cooldown"].get_state(first.key, "m")["cooldown_until"]
    assert 1790_000 <= until - before <= 1810_000


@pytest.mark.parametrize("credits", [True, False])
async def test_antigravity_credit_retry_runs_before_error_rules(monkeypatch, credits):
    from fastapi.responses import JSONResponse

    from src import failover, scheduler
    from src.antigravity import quota
    from src.protocols.runtime import AttemptResult
    from src.tests.test_antigravity_error_pipeline import decoded_result, payload
    from src.tests.test_antigravity_package_a import FailoverChannel, _patch_failover_runtime

    raw = payload("RESOURCE_EXHAUSTED", "QUOTA_EXHAUSTED", 429)
    raw["error"]["details"].append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1s"})
    exhausted = await decoded_result(raw, False)
    rule = _rule(cond("provider", "in", values=["antigravity"]), cond("status", "in", values=["429"]), id="ag")
    _patch_failover_runtime(monkeypatch, failover, cfg={
        "timeouts": {"total": 30}, "retry": {"transient": {"enabled": True, "maxExtraAttempts": 2}},
        "concurrency": {"queueWaitSeconds": 0}, "affinity": {},
        "errorRules": {"enabled": True, "rules": [rule], "presets": {}},
    })
    monkeypatch.setattr(failover.oauth_manager, "antigravity_credits_usable", lambda _key: credits)
    monkeypatch.setattr(quota, "_record_model_quota_exhausted_until", lambda *a: None)
    monkeypatch.setattr(failover.cooldown, "record_error", lambda *a, **k: None)
    ran = []
    monkeypatch.setattr(error_rule_effects, "run_steps", lambda ch, model, found, obs, steps, **kw:
                        ran.append(found.id) or {"until": None, "disabled": False, "paused": False})
    calls, bodies = [], []

    async def attempt(ch, model, body, *a, **k):
        calls.append(ch.key)
        bodies.append(body)
        if len(calls) == 1:
            return exhausted
        return AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True}))

    monkeypatch.setattr(failover, "_try_channel", attempt)
    route = scheduler.ScheduleResult([(FailoverChannel("ag:first"), "m"), (FailoverChannel("ag:second"), "m")], None, False)
    response = await failover.run_failover(route, {"model": "m"}, "r", "k", "ip", False, time.time(),
                                           ingress_protocol="responses")
    assert response.status_code == 200
    if credits:
        # The account may spend credits: that retry comes first and succeeds; the rule never runs.
        assert calls == ["ag:first", "ag:first"] and bodies[-1].get("_parrot_antigravity_use_credits") is True
        assert ran == []
    else:
        assert calls == ["ag:first", "ag:second"] and not bodies[-1].get("_parrot_antigravity_use_credits")
        assert ran == ["ag"]


def test_account_pause_needs_a_definite_end():
    with pytest.raises(error_rules.ErrorRuleValidationError, match="不能用冷却阶梯"):
        error_rules.normalize_rule(_rule(steps=[{**pause("ladder"), "scope": "account"}]))
    with pytest.raises(error_rules.ErrorRuleValidationError, match="保底时长"):
        error_rules.normalize_rule(_rule(steps=[{**pause("message_time", fallback=0), "scope": "account"}]))
    ok = error_rules.normalize_rule(_rule(steps=[{**pause("usage_window", fallback=0, boundary=5), "scope": "account"}]))
    assert ok["steps"][0]["scope"] == "account"


async def test_client_leaving_during_a_retry_wait_finalizes_the_request(m):
    import asyncio

    _setup(m)
    _install_keys(m, _default_key())
    _set_rules([_rule(cond("status", "in", values=["503"]), id="wait", category="upstream_busy",
                      steps=[retry(30, 1)])])
    router = MockRouter()
    _channels(m, router, lambda req: httpx.Response(503, json={"error": {"message": "busy"}}))
    task = asyncio.ensure_future(_chat(m, router))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if router.requests:
            break
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = m["log_db"]._get_conn().execute(
        "SELECT status, http_status FROM request_log ORDER BY rowid DESC LIMIT 1").fetchone()
    assert tuple(row) == ("cancelled", 499)


def test_probe_leaves_threshold_pauses_alone(m, monkeypatch):
    import asyncio

    from src import probe as probe_module
    _setup(m)
    channel = m["api_channel"].ApiChannel({
        "name": "probe-rule", "type": "api", "baseUrl": "https://probe.example", "apiKey": "k",
        "models": [{"real": "m", "alias": "m"}], "enabled": True,
    })
    _install_channels(m, [channel])
    _set_rules([_rule(cond("status", "in", values=["429"]), id="limit", category="rate_limit", steps=[],
                      thresholds=[{"count": 3, "steps": [pause("fixed", seconds=1800)]}])])
    calls = []

    async def fake_probe(ch, model, timeout_s=15):
        calls.append(model)
        return True, 0, ""

    monkeypatch.setattr(probe_module, "probe_channel_model", fake_probe)
    m["cooldown"].clear(channel.key, "m", notify_recovered=False)
    m["cooldown"].record_quota_pause(channel.key, "m", "slow down", cooldown_until=_now_ms() + 1_800_000, rule_id="limit")
    asyncio.run(probe_module.recovery_run_once())
    assert calls == []
    assert m["cooldown"].is_blocked(channel.key, "m")


def test_recent_errors_use_the_runtime_phase():
    assert error_rules.phase_of("stream_upstream_error", None) == "stream"
    assert error_rules.phase_of("http_error", 200) == "stream"
    assert error_rules.phase_of("connection_timeout", None) == "transport"
    assert error_rules.phase_of("http_error", 429) == "http"


# ── Management API ──────────────────────────────────────────────────


@pytest.fixture
def management_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server
    from src.management_api import install_management_routers
    from src.management_api.error_mapping import install_management_error_handlers

    key = "pmk_" + "E" * 64
    monkeypatch.setattr(server.config, "management_settings", lambda: {
        "managementKey": key, "stateDbPath": str(tmp_path / "mgmt.db"), "allowedOrigins": (),
        "sessionIdleTimeoutSeconds": 3600, "sessionAbsoluteTimeoutSeconds": 7200,
        "sessionTouchIntervalSeconds": 60, "telegramApprovalTtlSeconds": 180,
        "authRateLimitWindowSeconds": 60, "authRateLimitPerSource": 50, "authRateLimitGlobal": 200,
        "maxOperations": 100, "maxAuditRecords": 100,
    })
    from src import log_db
    log_db.init()
    app = FastAPI()
    server._initialize_management_runtime(app)
    install_management_error_handlers(app)
    install_management_routers(app)
    yield TestClient(app), key
    # Also unbinds the Telegram adapters, which would otherwise keep this runtime's controls.
    asyncio.run(server._close_management_runtime(app))


def test_management_api_crud_presets_and_dry_run(tmp_path, monkeypatch, management_client):
    client, key = management_client
    base = "/api/management/v1/error-rules"
    with client:
        sess = client.post("/api/management/v1/auth/sessions", json={"grantType": "managementKey", "managementKey": key})
        headers = {"Authorization": f"Bearer {sess.json()['data']['credential']}"}

        data = client.get(base, headers=headers).json()["data"]
        assert data["rules"] == [] and data["statsWindowDays"] == 7
        assert {p["id"] for p in data["presets"]} == set(error_rules.PRESET_IDS)
        assert [c["id"] for c in data["categories"]][:2] == ["rate_limit", "quota_exhausted"]
        assert data["categories"][1]["handling"]["steps"][0]["type"] == "pause"
        assert any(b["id"] == "default-ladder" for b in data["builtins"])

        bad = client.post(base + "/rules", headers={**headers, "If-Match": data["revision"]},
                          json={"name": "空条件", "category": "observe", "when": {"kind": "group", "items": []}})
        assert bad.status_code == 422 and "至少设置一个条件" in bad.text

        when = {"kind": "group", "mode": "all", "items": [
            {"kind": "cond", "field": "provider", "op": "in", "values": ["minimax"]},
            {"kind": "group", "mode": "any", "items": [
                {"kind": "cond", "field": "path", "op": "ne", "path": "$.base_resp.status_code", "values": ["0"]},
                {"kind": "cond", "field": "code", "op": "in", "values": ["2067"]},
            ]},
        ]}
        created = client.post(base + "/rules", headers={**headers, "If-Match": data["revision"]}, json={
            "name": "MiniMax 额度", "category": "quota_exhausted", "when": when,
            "steps": [{"type": "pause", "mode": "boundary", "boundaryHours": 5}, {"type": "notify", "repeatHours": 2}],
            "final": "next",
            "thresholds": [{"count": 3, "mode": "consecutive", "scope": "account",
                            "steps": [{"type": "pause", "scope": "account", "mode": "fixed", "seconds": 1800}]}],
            "response": {"status": 429, "message": "MiniMax：{$.error.message}", "retryAfter": True},
        })
        assert created.status_code == 201, created.text
        d1 = created.json()["data"]
        rule = d1["rules"][0]
        assert rule["id"].startswith("rule-") and rule["when"]["items"][1]["mode"] == "any"
        assert rule["steps"][0]["scope"] == "model" and rule["thresholds"][0]["steps"][0]["seconds"] == 1800

        stale = client.patch(base + "/settings", headers={**headers, "If-Match": data["revision"]}, json={"enabled": False})
        assert stale.status_code == 409

        off = client.patch(base + "/presets/preset.zhipu-plan-excluded", headers={**headers, "If-Match": d1["revision"]},
                           json={"enabled": False})
        assert off.status_code == 200
        d2 = off.json()["data"]
        assert next(p for p in d2["presets"] if p["id"] == "preset.zhipu-plan-excluded")["enabled"] is False
        assert client.patch(base + "/presets/preset.nope", headers={**headers, "If-Match": d2["revision"]},
                            json={"enabled": False}).status_code == 404

        tried = client.post(base + "/test", headers=headers, json={
            "provider": "minimax", "model": "MiniMax-M3.1", "status": 500, "body": MINIMAX_2067[10:],
            "draftRule": {"name": "草稿", "category": "upstream_busy",
                          "when": {"kind": "group", "items": [{"kind": "cond", "field": "code", "op": "eq", "values": ["9999"]}]}},
        })
        assert tried.status_code == 200, tried.text
        result = tried.json()["data"]
        assert result["trace"][0]["layer"] == "draft" and result["trace"][0]["status"] == "missed"
        assert result["matched"]["id"] == rule["id"]
        assert result["plan"]["steps"][0]["source"] == "boundary" and result["plan"]["steps"][0]["until"]
        assert result["plan"]["response"]["message"].startswith("MiniMax：当前已达到 Token Plan")
        assert result["observation"]["data"]["base_resp"]["status_code"] == 1002

        edited = {k: v for k, v in rule.items() if k not in ("id", "group")}
        edited["name"] = "MiniMax 额度（改）"
        upd = client.patch(f"{base}/rules/{rule['id']}", headers={**headers, "If-Match": d2["revision"]}, json=edited)
        assert upd.status_code == 200, upd.text
        assert upd.json()["data"]["rules"][0]["name"] == "MiniMax 额度（改）"

        recent = client.get(base + "/recent?hours=24", headers=headers)
        assert recent.status_code == 200 and recent.json()["data"]["hours"] == 24

        gone = client.delete(f"{base}/rules/{rule['id']}", headers={**headers, "If-Match": upd.json()["data"]["revision"]})
        assert gone.status_code == 200 and gone.json()["data"]["rules"] == []


def test_a_broken_rule_in_config_is_skipped_and_never_wiped(tmp_path, monkeypatch, management_client):
    good = error_rules.normalize_rule(_rule(id="good", name="好规则"))
    broken = {"id": "broken", "name": "坏规则", "category": "rate_limit", "when": group("all")}
    config_module.update(lambda c: c.__setitem__("errorRules", {"enabled": True, "rules": [good, broken], "presets": {}}))
    client, key = management_client
    base = "/api/management/v1/error-rules"
    with client:
        sess = client.post("/api/management/v1/auth/sessions", json={"grantType": "managementKey", "managementKey": key})
        headers = {"Authorization": f"Bearer {sess.json()['data']['credential']}"}
        data = client.get(base, headers=headers).json()["data"]
        assert [r["id"] for r in data["rules"]] == ["good"]
        refused = client.patch(base + "/settings", headers={**headers, "If-Match": data["revision"]}, json={"enabled": False})
        assert refused.status_code == 422 and "坏规则" in refused.text
    assert [r["id"] for r in config_module.get()["errorRules"]["rules"]] == ["good", "broken"]
    # The runtime keeps applying the good rule meanwhile.
    assert error_rules.match(_obs('HTTP 429: {}')).id == "good"


def test_preset_notification_is_effect_confirmed(monkeypatch):
    calls = []
    from src.tests.notification_helpers import capture
    capture(monkeypatch, lambda kind, text, record: calls.append((kind, text, record)))
    channel = SimpleNamespace(key="api:MiniMax", type="api", display_name="MiniMax")
    found = error_rules.find_rule("preset.minimax-token-plan")
    obs = _obs(MINIMAX_2067, provider="minimax", model="MiniMax-M3.1-Flash", channel_key=channel.key)
    until = _now_ms() + 3_600_000
    for _ in range(2):
        error_rule_effects._notify(channel, "MiniMax-M3.1-Flash", found, obs, until_ms=until, disabled=False,
                                   repeat_hours=6)
    assert len(calls) == 1
    kind, text, _record = calls[0]
    assert kind == "model.quota.blocked" and "Token Plan" in text and "2067" in text
