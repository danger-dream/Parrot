"""A TG failure receipt identifies the same credential-free stack in service logs."""
import json
import re
import threading

import httpx
import pytest
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.telegram import bot, error_reporting as errors, states, ui


def test_report_correlates_full_chain_without_raw_exception_or_input(capsys):
    secret = "private-oauth-token-in-exception"
    try:
        try:
            raise KeyError(secret)
        except KeyError as cause:
            raise RuntimeError("https://example.test/callback?code=" + secret) from cause
    except RuntimeError as exc:
        text = errors.report(exc, operation="智谱OAuth回调处理", update_id=789)
    output = capsys.readouterr().out
    report = json.loads(output.split("[tg-error] ", 1)[1])
    assert report["incident_id"] in text and re.fullmatch(r"TG-\d{8}-[A-F0-9]{10}", report["incident_id"])
    assert report["update_id"] == 789
    assert [e["type"] for e in report["exceptions"]] == ["RuntimeError", "KeyError"]
    assert all(e["frames"] and e["frames"][-1]["line"] > 0 for e in report["exceptions"])
    assert secret not in output + text and "https://example.test" not in output + text


@pytest.mark.parametrize("exc,expected", [
    (ManagementError(ManagementErrorCode.REVISION_CONFLICT, "secret-detail"), "账号或设置已变更"),
    (httpx.ReadTimeout("secret-url"), "上游响应超时"),
    (httpx.HTTPStatusError("secret-body", request=httpx.Request("GET", "https://upstream.test?secret=x"),
        response=httpx.Response(403)), "上游拒绝认证或权限"),
])
def test_known_failures_use_safe_reason(exc, expected, capsys):
    text = errors.report(exc, operation="测试操作")
    assert expected in text and "secret" not in text + capsys.readouterr().out


def test_real_poll_loop_reports_context_captured_before_handler_pops_state_and_continues(monkeypatch, capsys):
    running = [True]
    called, sent = [], []
    updates = [{"update_id": 901, "message": {"chat": {"id": 42}, "text": "private-callback-code"}},
               {"update_id": 902, "message": {"chat": {"id": 42}, "text": "/menu"}}]
    states.set_state(42, "oa_zh_callback", {"flow_secret": "private-flow-secret"})
    def api(*a, **kw):
        if called:
            running[0] = False
            return {"ok": True, "result": []}
        return {"ok": True, "result": updates}
    def handle(update):
        called.append(update["update_id"])
        if update["update_id"] == 901:
            states.pop_state(42)
            raise ValueError("private-token-value")
    monkeypatch.setattr(bot, "_poll_generation_active", lambda *a: running[0])
    monkeypatch.setattr(bot, "_handle_update", handle)
    monkeypatch.setattr(ui, "api", api)
    monkeypatch.setattr(ui, "send", lambda chat, text: sent.append(text))
    bot._poll_loop(123, threading.Event())
    assert called == [901, 902] and len(sent) == 1
    assert "智谱OAuth回调处理" in sent[0] and "故障编号" in sent[0]
    output = capsys.readouterr().out
    assert '"update_id": 901' in output and '"state_action": "oa_zh_callback"' in output
    assert "private-" not in output + sent[0]


@pytest.mark.parametrize("route,operation", [
    ("oa:zh:confirm", "智谱确认操作"),
    ("oa:refresh_token", "OAuth凭据刷新"),
    ("oa:refresh_usage", "OAuth额度查询"),
])
def test_callback_context_discards_secret_suffix(route, operation):
    context = errors.update_context({"update_id": 1, "callback_query": {"data": route + ":private-capability-token",
        "message": {"chat": {"id": 42}}}})
    assert context["route"] == route and context["operation"] == operation
    assert "private-" not in str(context)


def test_zhipu_project_error_keeps_business_code_and_stage(capsys):
    from src.oauth.zhipu.common import ZhipuError
    exc = ZhipuError("project_lookup", "business", status=200, code="1234")
    text = errors.report(exc, operation="智谱组织/项目查询")
    report = json.loads(capsys.readouterr().out.split("[tg-error] ", 1)[1])
    assert report["error_code"] == 1234 and report["http_status"] == 200
    assert report["upstream_stage"] == "project_lookup" and report["upstream_kind"] == "business"
    assert "业务码 1234" in text and "HTTP 200" not in text


def test_zhipu_http_success_business_failure_keeps_both_codes(monkeypatch):
    from src.oauth.zhipu import common
    monkeypatch.setattr(common, "require_network", lambda: None)
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        json={"success": False, "code": "1234", "msg": "private-token-body"}))
    monkeypatch.setattr(common.network, "sync_client", lambda **kw: httpx.Client(transport=transport))
    with pytest.raises(common.ZhipuError) as caught:
        common.request("https://example.test/customer")
    assert caught.value.status_code == 200 and caught.value.code == 1234
    assert "private-token-body" not in str(caught.value)
