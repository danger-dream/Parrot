"""Exhausted usage windows cool the account/model until the upstream reset.

WorkBuddy 6004 (per-model usage limit) and Zhipu 1308 (5-hour window) are only
recognized when the code, the limit wording and a reset time all match.
"""

from __future__ import annotations

import json
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import cooldown, quota_errors
from src.tests.test_zhipu_provider import account_env, credential, quota, window  # noqa: F401

BJT = timezone(timedelta(hours=8))
KEY = ("oauth:workbuddy:cn:7009a407-312c-464e-8e69-67bafd2f7e36:p", "deepseek-v4.1-flash")
ZHIPU = ("oauth:zhipu:bigmodel:oauth:36021726279647635:org-c775:proj_cf88", "glm-5.3")


def _reset_in(hours: float) -> tuple[str, int]:
    reset = (datetime.now(BJT) + timedelta(hours=hours)).replace(microsecond=0)
    return reset.strftime("%Y-%m-%d %H:%M:%S"), int(reset.timestamp() * 1000)


def _usage_limit(reset_text: str, code: object = 6004) -> str:
    body = {
        "code": code,
        "msg": f"您的使用量已超出频率限制，将在 {reset_text} UTC+8 重置，您也可以切换其他模型继续使用。",
        "requestId": "01a1aab33e9971b1a4ce28f24ef85efb",
    }
    return "HTTP 429: " + json.dumps(body, ensure_ascii=False)


def _zhipu_5h(reset_text: str, code: str = "1308", wording: str = "已达到 5 小时的使用上限。",
              message: str | None = None) -> str:
    body = {
        "type": "error",
        "error": {
            "type": "rate_limit_error", "code": code,
            "message": message or f"[{code}][{wording}您的限额将在 {reset_text} 重置。][20261105163552dfeb1c424913421c]",
        },
        "request_id": "20261105163552dfeb1c424913421c",
    }
    return "HTTP 429: " + json.dumps(body, ensure_ascii=False)


@pytest.fixture
def isolated_cooldown(monkeypatch):
    entries: dict[tuple[str, str], dict] = {}
    saved: list[tuple] = []
    monkeypatch.setattr(cooldown, "_entries", entries)
    monkeypatch.setattr(cooldown.channel_state, "resolve", lambda key: key)
    monkeypatch.setattr(cooldown.channel_state, "is_deleted", lambda _key: False)
    monkeypatch.setattr(cooldown.state_db, "optional_write_timeout", lambda: nullcontext())
    monkeypatch.setattr(cooldown.state_db, "error_save", lambda *args, **_kwargs: saved.append(args))
    monkeypatch.setattr(cooldown, "_grace_count", lambda _key: 3)
    return entries, saved


def test_parses_reset_only_for_workbuddy_6004():
    text, reset_ms = _reset_in(16)
    detail = _usage_limit(text)

    assert quota_errors.workbuddy_usage_limit_reset_ms(KEY[0], detail) == reset_ms
    assert quota_errors.is_workbuddy_usage_limit_message(detail)
    assert quota_errors.workbuddy_usage_limit_reset_ms("oauth:zhipu:acct", detail) is None
    assert quota_errors.workbuddy_usage_limit_reset_ms(KEY[0], _usage_limit(text, code=6005)) is None
    stale, _ = _reset_in(-1)
    assert quota_errors.workbuddy_usage_limit_reset_ms(KEY[0], _usage_limit(stale)) is None
    assert quota_errors.workbuddy_usage_limit_reset_ms(
        KEY[0], 'HTTP 429: {"code":6004,"msg":"您的使用量已超出频率限制"}') is None
    assert quota_errors.workbuddy_usage_limit_reset_ms(
        KEY[0], detail.replace("使用量已超出", "请求过于频繁")) is None


def test_parses_zhipu_1308_only_with_code_and_wording():
    text, reset_ms = _reset_in(3)
    zhipu = SimpleNamespace(provider="zhipu", base_url="")
    detail = _zhipu_5h(text)

    assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=429, error_detail=detail) == reset_ms
    assert quota_errors.is_zhipu_usage_window_message(detail)
    # The documented template is `${number} ${unit}`, not only five hours.
    for wording in ("已达到 7 天的使用上限。", "已达到 1 周的使用上限。"):
        assert quota_errors.zhipu_usage_window_reset_ms(
            zhipu, http_status=429, error_detail=_zhipu_5h(text, wording=wording)) == reset_ms
    for window in ("5 hour", "7 days"):
        english = _zhipu_5h(text, message=f"Usage limit reached for {window}. Your limit will reset at {text}")
        assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=429, error_detail=english) == reset_ms
    # 1316-1321: the window is used up and extra pay-as-you-go usage is unavailable.
    no_extra = _zhipu_5h(text, code="1316", message=(
        f"已达到 5 小时使用上限。主账号余额不足，无法使用超额按量付费。您的限额将在 {text} 重置。"))
    assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=429, error_detail=no_extra) == reset_ms
    no_extra_en = _zhipu_5h(text, code="1321", message=(
        f"Usage limit reached for the past 7 days. Extra usage is not available due to monthly spend limit. Resets at {text}."))
    assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=429, error_detail=no_extra_en) == reset_ms
    assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=429, error_detail=_zhipu_5h(
        text, code="1316", message="您的账户已达到速率限制，请您控制请求频率")) is None

    assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=429, error_detail=_zhipu_5h(text, code="1302")) is None
    assert quota_errors.zhipu_usage_window_reset_ms(
        zhipu, http_status=429, error_detail=_zhipu_5h(text, wording="并发数过高。")) is None
    assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=400, error_detail=detail) is None
    assert quota_errors.zhipu_usage_window_reset_ms(
        SimpleNamespace(provider="openai", base_url=""), http_status=429, error_detail=detail) is None
    stale, _ = _reset_in(-1)
    assert quota_errors.zhipu_usage_window_reset_ms(zhipu, http_status=429, error_detail=_zhipu_5h(stale)) is None


def test_active_usage_limit_counts_as_quota_cooldown():
    text, reset_ms = _reset_in(5)
    for detail in (_usage_limit(text), _zhipu_5h(text)):
        entry = {"cooldown_until": reset_ms, "last_error_message": detail}
        assert quota_errors.active_quota_cooldown(entry)
        assert not quota_errors.active_quota_cooldown({**entry, "cooldown_until": int(time.time() * 1000) - 1})


def test_record_error_cools_zhipu_oauth_and_api_until_5h_reset(monkeypatch, isolated_cooldown):
    entries, _saved = isolated_cooldown
    monkeypatch.setattr(cooldown, "_now_ms", lambda: int(time.time() * 1000))
    monkeypatch.setattr(cooldown.config, "get", lambda: {"channels": [
        {"name": "glm", "baseUrl": "https://open.bigmodel.cn/api/anthropic"},
        {"name": "other", "baseUrl": "https://api.example.com"},
    ]})
    text, reset_ms = _reset_in(3)

    cooldown.record_error(*ZHIPU, _zhipu_5h(text))
    cooldown.record_error("api:glm", "glm-5.3", _zhipu_5h(text))
    cooldown.record_error("api:other", "glm-5.3", _zhipu_5h(text))

    assert entries[ZHIPU]["cooldown_until"] == reset_ms
    assert entries[("api:glm", "glm-5.3")]["cooldown_until"] == reset_ms
    assert entries[("api:other", "glm-5.3")]["cooldown_until"] is None


def test_record_error_cools_until_reset_bypassing_grace(monkeypatch, isolated_cooldown):
    entries, _saved = isolated_cooldown
    monkeypatch.setattr(cooldown, "_now_ms", lambda: int(time.time() * 1000))
    text, reset_ms = _reset_in(16)

    cooldown.record_error(*KEY, _usage_limit(text))

    assert entries[KEY]["cooldown_until"] == reset_ms
    assert cooldown.is_blocked(*KEY)


def test_record_error_extends_short_cooldown_and_keeps_permanent(monkeypatch, isolated_cooldown):
    entries, _saved = isolated_cooldown
    now = int(time.time() * 1000)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: now)
    text, reset_ms = _reset_in(16)

    cooldown.record_error(*KEY, _usage_limit(text), cooldown_until=now + 60_000)
    assert entries[KEY]["cooldown_until"] == reset_ms

    other = (KEY[0], "glm-5.2")
    cooldown.record_error(*other, _usage_limit(text), cooldown_until=cooldown._INF)
    assert entries[other]["cooldown_until"] == cooldown._INF


def test_ordinary_workbuddy_429_keeps_the_ladder(monkeypatch, isolated_cooldown):
    entries, _saved = isolated_cooldown
    monkeypatch.setattr(cooldown, "_now_ms", lambda: int(time.time() * 1000))

    cooldown.record_error(*KEY, 'HTTP 429: {"code":429,"msg":"too many requests"}')

    assert entries[KEY]["cooldown_until"] is None
    assert entries[KEY]["error_count"] == 1


def test_init_upgrades_stored_short_cooldown_to_reset(monkeypatch, isolated_cooldown):
    entries, saved = isolated_cooldown
    text, reset_ms = _reset_in(16)
    short = int(time.time() * 1000) + 5 * 60_000
    monkeypatch.setattr(cooldown, "_initialized", False)
    monkeypatch.setattr(cooldown.state_db, "error_load_all", lambda: [{
        "channel_key": KEY[0], "model": KEY[1], "error_count": 8,
        "cooldown_until": short, "last_error_message": _usage_limit(text),
    }])

    cooldown.init()

    assert entries[KEY]["cooldown_until"] == reset_ms
    assert saved and saved[-1][:4] == (KEY[0], KEY[1], 8, reset_ms)


def test_init_upgrades_stored_zhipu_1308_cooldown(monkeypatch, isolated_cooldown):
    entries, saved = isolated_cooldown
    text, reset_ms = _reset_in(3)
    monkeypatch.setattr(cooldown, "_initialized", False)
    monkeypatch.setattr(cooldown.state_db, "error_load_all", lambda: [{
        "channel_key": ZHIPU[0], "model": ZHIPU[1], "error_count": 4,
        "cooldown_until": int(time.time() * 1000) + 60_000, "last_error_message": _zhipu_5h(text),
    }])

    cooldown.init()

    assert entries[ZHIPU]["cooldown_until"] == reset_ms
    assert saved[-1][:4] == (ZHIPU[0], ZHIPU[1], 4, reset_ms)


async def test_zhipu_quota_recovery_clears_1308_cooldown(account_env, monkeypatch):
    from src import oauth_manager as om
    from src.oauth.zhipu import common

    account = credential(models=["GLM-5.3"])
    om.add_account(account)
    key = om.get_account_key(account)
    cooldown.init()
    text, reset_ms = _reset_in(3)
    cooldown.record_error("oauth:" + key, "GLM-5.3", _zhipu_5h(text))
    assert cooldown.get_state("oauth:" + key, "GLM-5.3")["cooldown_until"] == reset_ms

    data = [quota(window(100))]
    monkeypatch.setattr(common, "request", lambda *a, **k: data[0])
    om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))
    data[0] = quota(window(0))
    assert om.evaluate_and_toggle_by_usage(key, await om.fetch_usage(key))["action"] == "resumed"
    assert not cooldown.is_blocked("oauth:" + key, "GLM-5.3")
