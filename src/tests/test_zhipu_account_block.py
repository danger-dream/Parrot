"""Zhipu refusals of the whole account (1113/1309/1313/1314/1315) disable it until enabled by hand."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import failover, oauth_manager as om, quota_errors
from src.tests.notification_helpers import capture
from src.tests.test_zhipu_provider import account_env, credential  # noqa: F401

ZHIPU = SimpleNamespace(provider="zhipu", base_url="")

DOCUMENTED = {
    "1113": ("arrears", "您的账户已欠费，请充值后重试"),
    "1309": ("plan_expired", "您的 GLM Coding Plan 套餐已到期，暂无法使用，前往官方续订后即可恢复 https://bigmodel.cn/claude-code"),
    "1313": ("fair_use_limited", "您的账户当前使用模式不符合公平使用策略，请求频率已受到限制。"),
    "1314": ("plan_expired", "您的企业套餐已失效，请联系企业管理员。"),
    "1315": ("key_not_allowed", "该 API Key 仅限企业编程套餐场景使用，请到官网更换对应产品类型的 API Key"),
}
DOCUMENTED_EN = {
    "1113": "Insufficient balance or no resource package. Please recharge.",
    "1309": "Your GLM Coding Plan package has expired and is temporarily unavailable.",
    "1313": "Your account's current usage pattern does not comply with the Fair Usage Policy.",
    "1314": "Your enterprise package has expired. Please contact your enterprise administrator.",
    "1315": "This API Key is limited to enterprise coding package scenarios.",
}


def _error(code: str, message: str) -> str:
    body = {"type": "error", "error": {"type": "rate_limit_error", "code": code,
                                       "message": f"[{code}][{message}][20261105163552dfeb1c424913421c]"}}
    return "HTTP 429: " + json.dumps(body, ensure_ascii=False)


@pytest.mark.parametrize("code", sorted(DOCUMENTED))
def test_documented_codes_need_code_wording_and_429(code):
    reason, message = DOCUMENTED[code]
    for text in (message, DOCUMENTED_EN[code]):
        assert quota_errors.zhipu_account_block(ZHIPU, http_status=429, error_detail=_error(code, text)) == (code, reason)
    assert reason in quota_errors.ACCOUNT_BLOCK_LABELS
    assert quota_errors.zhipu_account_block(ZHIPU, http_status=400, error_detail=_error(code, message)) is None
    assert quota_errors.zhipu_account_block(
        SimpleNamespace(provider="openai", base_url=""), http_status=429, error_detail=_error(code, message)) is None
    assert quota_errors.zhipu_account_block(
        ZHIPU, http_status=429, error_detail=_error(code, "您的账户已达到速率限制，请您控制请求频率")) is None


def test_other_codes_are_not_account_blocks():
    for code, message in (("1302", "您的账户已达到速率限制，请您控制请求频率"), ("1308", "已达到 5 小时的使用上限。"),
                          ("1310", "您已达到每周/每月使用上限"), ("1311", "当前订阅套餐暂未开放GLM-5.3权限")):
        assert quota_errors.zhipu_account_block(ZHIPU, http_status=429, error_detail=_error(code, message)) is None


def _channel(key):
    account = om.get_account(key)
    return SimpleNamespace(type="oauth", provider="zhipu", base_url="", account_key=key,
                           key="oauth:" + key, state_key=om.account_state_key(account))


def test_block_disables_account_once_and_notifies(account_env, monkeypatch):
    events = []
    capture(monkeypatch, lambda kind, text, record: events.append((kind, text)))
    account = credential(models=["GLM-5.3"])
    om.add_account(account)
    key = om.get_account_key(account)
    ch = _channel(key)
    result = SimpleNamespace(http_status=429, error_detail=_error("1309", DOCUMENTED["1309"][1]))

    assert failover._apply_zhipu_account_block(ch, result) is True
    stored = om.get_account(key)
    assert (stored["enabled"], stored["disabled_reason"]) == (False, "plan_expired")
    assert [kind for kind, _ in events] == ["account.access.blocked"]
    assert quota_errors.ACCOUNT_BLOCK_LABELS["plan_expired"] in events[0][1] and "1309" in events[0][1]

    # Already disabled: the attempt still moves on, without a second write or notice.
    assert failover._apply_zhipu_account_block(ch, result) is True
    assert len(events) == 1

    om.set_enabled(key, True)
    stored = om.get_account(key)
    assert (stored["enabled"], stored["disabled_reason"]) == (True, None)


def test_block_keeps_a_manual_disable_and_skips_api_channels(account_env, monkeypatch):
    events = []
    capture(monkeypatch, lambda kind, text, record: events.append((kind, text)))
    account = credential(models=["GLM-5.3"])
    om.add_account(account)
    key = om.get_account_key(account)
    ch = _channel(key)
    om.set_enabled(key, False, reason="user")
    result = SimpleNamespace(http_status=429, error_detail=_error("1113", DOCUMENTED["1113"][1]))

    assert failover._apply_zhipu_account_block(ch, result) is True
    assert om.get_account(key)["disabled_reason"] == "user"
    assert events == []

    api = SimpleNamespace(type="api", provider="zhipu", base_url="https://open.bigmodel.cn/api/anthropic",
                          key="api:glm", account_key=None, state_key=None)
    assert failover._apply_zhipu_account_block(api, result) is False
