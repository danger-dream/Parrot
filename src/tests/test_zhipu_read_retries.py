"""Bounded retries for opted-in reads, never replaying remote writes."""
import asyncio
import time

import httpx
import pytest
from src.oauth.zhipu import auth, common
from src.tests.test_zhipu_provider import credential


@pytest.fixture
def wire(monkeypatch):
    calls, sleeps = [], []
    replies = []
    monkeypatch.setattr(common, "require_network", lambda: None)
    async def pause(delay):
        sleeps.append(delay)
    monkeypatch.setattr(common.asyncio, "sleep", pause)
    def handler(request):
        calls.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(common.network, "sync_client", lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), timeout=kw["timeout"]))
    monkeypatch.setattr(common.network, "async_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=kw["timeout"]))
    return replies, calls, sleeps


@pytest.mark.parametrize("first", [httpx.ConnectError("private-detail"), httpx.ReadTimeout("private-detail"),
    httpx.Response(408), httpx.Response(429), httpx.Response(500), httpx.Response(503)])
def test_transient_read_retries_then_succeeds(wire, first):
    replies, calls, sleeps = wire
    replies.extend([first, httpx.Response(200, json={"code": 200, "data": {"organizations": []}})])
    assert auth.project_choices(credential("oauth")) == []
    assert len(calls) == 2 and sleeps == [0.5]


def test_timeout_exhaustion_has_three_attempts_and_stage(wire, capsys):
    replies, calls, sleeps = wire
    replies.extend(httpx.ReadTimeout("private-token-detail") for _ in range(3))
    with pytest.raises(common.ZhipuError) as failed:
        auth.project_choices(credential("oauth"))
    assert failed.value.stage == "projects" and failed.value.kind == "timeout"
    assert failed.value.timeout_phase == "read"
    assert len(calls) == 3 and sleeps == [0.5, 1.0]
    assert "private-token-detail" not in capsys.readouterr().out


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_status_does_not_retry(wire, status):
    replies, calls, sleeps = wire
    replies.append(httpx.Response(status))
    with pytest.raises(common.ZhipuError):
        auth.entitlement(credential("oauth"))
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("path", ["/api/v1/oauth/token", "/api_keys", "/api/v1/usage/reset/opportunity", "/api/v1/usage/reset/use"])
def test_posts_never_replay_even_if_retry_requested(wire, path):
    replies, calls, sleeps = wire
    replies.append(httpx.ReadTimeout("lost-reply"))
    with pytest.raises(common.ZhipuError):
        common.request("https://zcode.z.ai" + path, method="POST", body={}, read_attempts=3)
    assert len(calls) == 1 and not sleeps


def test_retry_after_respected_and_excessive_delay_stops(wire):
    replies, calls, sleeps = wire
    replies.extend([httpx.Response(429, headers={"Retry-After": "2"}),
                    httpx.Response(503, headers={"Retry-After": "60"})])
    with pytest.raises(common.ZhipuError):
        auth.entitlement(credential("oauth"))
    assert len(calls) == 2 and sleeps == [2]


def test_key_list_and_copy_retry_independently_without_creation(wire):
    replies, calls, sleeps = wire
    replies.extend([httpx.Response(502), httpx.Response(200, json={"data": [{"apiKey": "id", "name": "zcode-api-key"}]}),
                    httpx.ConnectError("temporary"), httpx.Response(200, json={"data": {"secretKey": "secret"}})])
    assert auth.resolve_model_key(credential("oauth", organization_id="org", project_id="project")) == "id.secret"
    assert len(calls) == 4 and sleeps == [0.5, 0.5]
    assert all(request.method == "GET" for request in calls)


def test_read_timeouts_20_connect_60_read_preserve_explicit_and_write_limits(wire):
    replies, calls, _ = wire
    replies.extend(httpx.Response(200, json={"data": []}) for _ in range(3))
    common.request("https://bigmodel.cn/read", read_attempts=3)
    assert calls[-1].extensions["timeout"] == {"connect": 20, "read": 60, "write": 20, "pool": 20}
    common.request("https://bigmodel.cn/read", read_attempts=3, timeout=1.25)
    assert set(calls[-1].extensions["timeout"].values()) == {1.25}
    common.request("https://bigmodel.cn/write", method="POST", read_attempts=3)
    assert set(calls[-1].extensions["timeout"].values()) == {20}
    assert common.READ_TOTAL_TIMEOUT == 180


@pytest.mark.parametrize("cls,phase", [(httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
    (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool")])
def test_timeout_phase_reaches_display_and_logs(wire, capsys, cls, phase):
    from src import oauth_errors
    from src.telegram import error_reporting
    replies, _, _ = wire
    replies.extend(cls("private-credential") for _ in range(3))
    with pytest.raises(common.ZhipuError) as failure:
        auth.project_choices(credential("oauth"))
    assert failure.value.timeout_phase == phase
    shown = oauth_errors.describe_oauth_error(failure.value, provider="zhipu")
    assert "超时" in shown.title and "组织/项目查询" in shown.reason
    error_reporting.report(failure.value, operation="智谱组织/项目查询")
    logs = capsys.readouterr().out
    assert f'"timeout_phase": "{phase}"' in logs and '"upstream_stage": "projects"' in logs
    assert "private-credential" not in logs


@pytest.mark.parametrize("during_body", [False, True])
def test_total_deadline_cancels_active_io_and_closes_client(monkeypatch, during_body):
    cancelled, closed, clients, calls = [], [], [], []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                await asyncio.Event().wait()
                yield b"never"
            finally:
                cancelled.append(True)
        async def aclose(self):
            closed.append(True)
    async def handler(request):
        calls.append(request)
        if during_body:
            return httpx.Response(200, stream=Body())
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    def client(**kw):
        result = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=kw["timeout"])
        clients.append(result)
        return result
    monkeypatch.setattr(common, "require_network", lambda: None)
    monkeypatch.setattr(common, "READ_TOTAL_TIMEOUT", 0.04)
    monkeypatch.setattr(common.network, "async_client", client)
    started = time.monotonic()
    with pytest.raises(common.ZhipuError) as failure:
        common.request("https://bigmodel.cn/read", read_attempts=3, stage="projects")
    assert failure.value.timeout_phase == "total" and failure.value.stage == "projects"
    assert time.monotonic() - started < 1
    assert len(calls) == 1 and cancelled and all(c.is_closed for c in clients)
    assert bool(closed) == during_body


async def test_sync_read_remains_usable_under_existing_event_loop(wire):
    replies, calls, _ = wire
    replies.append(httpx.Response(200, json={"data": []}))
    assert common.request("https://bigmodel.cn/read", read_attempts=3) == []
    assert len(calls) == 1


def test_business_rejection_does_not_retry(wire):
    replies, calls, sleeps = wire
    replies.append(httpx.Response(200, json={"success": False, "code": 1001}))
    with pytest.raises(common.ZhipuError):
        auth.entitlement(credential("oauth"))
    assert len(calls) == 1 and not sleeps
