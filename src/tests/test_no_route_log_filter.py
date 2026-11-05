from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from starlette.requests import Request

import server
from src import auth, config, failover, log_db, scheduler
from src.channel import registry
from src.openai import handler, responses_ws
from src.openai.channel.registration import register_factories
from src.tests import test_openai_responses_ws as ws_tests


@pytest.fixture
def route_env(monkeypatch):
    modules = ws_tests._import_modules()
    ws_tests._setup(modules)
    register_factories()
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg.update({
        "apiKeys": {"client": {"key": "test-key", "enabled": True}},
        "oauthAccounts": [],
        "channels": [
            {"name": name, "type": "api", "enabled": True, "protocol": "openai-responses",
             "baseUrl": "https://example.invalid", "apiKey": "test",
             "models": [{"real": "test-model", "alias": "test-model"}]}
            for name in ("A", "B")
        ],
        "modelRegistry": {"enabled": True},
        "modelCenter": {"disabledModels": [], "hiddenModels": [],
                        "sourceEnabledModels": {}, "apiSourceDisabledModels": {}},
        "modelMapping": {"global": {"friendly": "test-model"}},
    })
    monkeypatch.setattr(config, "_cache", cfg)
    monkeypatch.setattr(config, "_mtime", config._current_mtime())
    monkeypatch.setattr(auth, "validate", lambda headers: ("client", None, None))
    notices = []
    monkeypatch.setattr(responses_ws.notifications, "routing_unavailable", lambda **kw: notices.append(kw))
    registry.rebuild_from_config()
    return cfg, notices


def _request(body):
    data = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": data, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/v1/test",
                    "headers": [], "client": ("127.0.0.1", 1234)}, receive)


def _counts():
    conn = log_db._get_conn()
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                 for table in ("request_log", "request_detail"))


def _disable_sources(cfg):
    cfg["modelCenter"]["apiSourceDisabledModels"] = {
        "api:A": ["test-model"], "api:B": ["test-model"],
    }


async def _invoke(ingress, model):
    if ingress == "anthropic":
        response = await server.proxy_messages(_request({"model": model, "messages": [], "max_tokens": 10}))
        return response.status_code
    if ingress == "responses_ws":
        ws = ws_tests.FakeWebSocket({"type": "response.create", "model": model, "input": "hi"})
        await responses_ws.handle_responses_ws(ws)
        return ws.close_calls[-1][0]
    body = {"model": model, "messages": []} if ingress == "chat" else {"model": model, "input": "hi"}
    response = await handler.handle(_request(body), ingress_protocol=ingress)
    return response.status_code


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses", "responses_ws"])
@pytest.mark.parametrize("scenario", ["unknown", "disabled", "mixed-disabled", "channel-disabled"])
@pytest.mark.parametrize("registry_enabled", [True, False])
async def test_unknown_and_explicitly_disabled_skip_new_log(route_env, ingress, scenario, registry_enabled):
    cfg, notices = route_env
    cfg["modelRegistry"]["enabled"] = registry_enabled
    if scenario == "disabled":
        _disable_sources(cfg)
    elif scenario == "mixed-disabled":
        cfg["modelCenter"]["disabledModels"] = ["test-model"]
        cfg["modelCenter"]["sourceEnabledModels"] = {"api:B": ["test-model"]}
        cfg["modelCenter"]["apiSourceDisabledModels"] = {"api:B": ["test-model"]}
    elif scenario == "channel-disabled":
        for channel in cfg["channels"]:
            channel["enabled"] = False
    registry.rebuild_from_config()
    before = _counts()
    status = await _invoke(ingress, "missing-model" if scenario == "unknown" else "friendly")
    expected = 404 if scenario == "unknown" else 503
    assert status == (expected + 4000 if ingress == "responses_ws" else expected)
    assert _counts() == before
    assert notices[-1]["request_id"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses", "responses_ws"])
@pytest.mark.parametrize("scenario", ["cooldown", "denied", "temporary-disable"])
@pytest.mark.parametrize("registry_enabled", [True, False])
async def test_non_disabled_failures_still_write_log(route_env, monkeypatch, ingress, scenario, registry_enabled):
    cfg, notices = route_env
    cfg["modelRegistry"]["enabled"] = registry_enabled
    cfg["modelCenter"]["apiSourceDisabledModels"] = {"api:A": ["test-model"]}
    if scenario == "cooldown":
        monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda key, model: key == "api:B")
        monkeypatch.setattr(scheduler.cooldown, "get_state", lambda key, model: {})
    elif scenario == "denied":
        monkeypatch.setattr(auth, "channel_allowed", lambda key, channel: channel != "api:B")
    else:
        cfg["channels"][1]["disabled_reason"] = "temporary provider failure"
    registry.rebuild_from_config()
    before = _counts()
    status = await _invoke(ingress, "test-model")
    assert status == (4503 if ingress == "responses_ws" else 503)
    assert _counts() == (before[0] + 1, before[1] + 1)
    row = log_db._get_conn().execute(
        "SELECT status,http_status FROM request_log WHERE request_id=?", (notices[-1]["request_id"],)
    ).fetchone()
    assert tuple(row) == ("error", 503)


@pytest.mark.asyncio
async def test_mixed_oauth_disabled_sources_match_reported_case(route_env):
    cfg, notices = route_env
    cfg["channels"] = []
    cfg["oauthAccounts"] = [
        {"provider": "openai", "email": "openai@example.invalid", "enabled": True,
         "models": ["gpt-5.6-luna"]},
        {"provider": "cursor", "email": "cursor@example.invalid", "subject": "cursor-fixture", "enabled": True,
         "models": ["gpt-5.6-luna"], "cursor_disabled_models": ["gpt-5.6-luna"],
         "cursor_model_catalog": {"models": [{"id": "gpt-5.6-luna", "name": "gpt-5.6-luna"}]}},
        {"provider": "workbuddy", "uid": "workbuddy-fixture", "realm": "global",
         "workbuddy_client_profile": "workbuddy-cli", "enabled": True,
         "models": ["gpt-5.6-luna"], "disabledModels": ["gpt-5.6-luna"]},
    ]
    cfg["modelCenter"]["disabledModels"] = ["gpt-5.6-luna"]
    registry.rebuild_from_config()
    route = scheduler.schedule({"model": "gpt-5.6-luna", "input": "hi"}, "client", "127.0.0.1", ingress_protocol="responses")
    assert sorted(item["reason"] for item in route.exclusions) == [
        "global_model_disabled", "source_model_disabled", "source_model_disabled",
    ]
    before = _counts()
    assert await _invoke("responses", "gpt-5.6-luna") == 503
    assert _counts() == before
    assert notices[-1]["request_id"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("generate", [True, False])
@pytest.mark.parametrize("scenario", ["unknown", "disabled"])
async def test_followup_search_turn_skips_log_and_keeps_error(route_env, generate, scenario):
    cfg, _ = route_env
    if scenario == "disabled":
        _disable_sources(cfg)
        registry.rebuild_from_config()
    model = "missing-model" if scenario == "unknown" else "test-model"
    body = {"model": model, "input": "hi", "stream": True, "generate": generate}
    ws = ws_tests.FakeWebSocket({})
    ws._first_text = None
    await ws.accept()
    before = _counts()
    route, skip = await responses_ws._schedule_search_turn(ws, body, "search-turn", "client", "127.0.0.1", None)
    assert not route and skip
    assert _counts() == before

    class Lease:
        async def release(self):
            pass

    await asyncio.wait_for(responses_ws._run_search_ws_session(
        ws, body=body, schedule_result=route, request_id="search-turn", api_key_name="client",
        client_ip="127.0.0.1", start_time=time.time(), start_monotonic=time.monotonic(),
        allowed_models=None, api_key_lease=Lease(), skip_request_log=skip,
    ), timeout=3)
    assert _counts() == before
    if generate:
        event = json.loads(ws.sent_texts[-1])
        assert event["type"] == "error" and event["error"]
    else:
        assert ws.close_calls[-1][0] == responses_ws._ws_close_code_for_http(503)


@pytest.mark.asyncio
async def test_suppressed_search_error_response_matches_logged_response(route_env):
    route = scheduler.ScheduleResult([], None, False)
    body = {"model": "missing-model", "input": "hi", "stream": False}
    log_db.insert_pending("logged-search", "127.0.0.1", "client", "missing-model", False, 1, 0, {}, body)
    before = _counts()
    logged = await failover.run_failover(route, body, "logged-search", "client", "127.0.0.1", False,
                                        time.time(), ingress_protocol="responses")
    skipped = await failover.run_failover(route, body, "skipped-search", "client", "127.0.0.1", False,
                                         time.time(), ingress_protocol="responses", skip_request_log=True)
    assert logged.status_code == skipped.status_code == 503
    assert logged.body == skipped.body
    assert _counts() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["anthropic", "chat", "responses", "responses_ws"])
async def test_scheduler_exception_still_records_pending(route_env, monkeypatch, ingress):
    def broken(*args, **kwargs):
        raise RuntimeError("scheduler failed")
    monkeypatch.setattr(scheduler, "schedule", broken)
    before = _counts()
    if ingress == "responses_ws":
        ws = ws_tests.FakeWebSocket({"type": "response.create", "model": "test-model", "input": "hi"})
        with pytest.raises(RuntimeError, match="scheduler failed"):
            await responses_ws._handle_responses_ws_lane(ws)
    else:
        with pytest.raises(RuntimeError, match="scheduler failed"):
            await _invoke(ingress, "test-model")
    assert _counts() == (before[0] + 1, before[1] + 1)
    row = log_db._get_conn().execute("SELECT status FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "pending"


def test_uncertain_source_and_guard_fail_closed(route_env, monkeypatch):
    cfg, _ = route_env
    result = scheduler.ScheduleResult([], None, False, guard_errors=["cannot route safely"])
    assert not scheduler.skip_no_route_log("missing-model", result, model_never_supported=True)
    result = scheduler.ScheduleResult([], None, False, exclusions=[
        {"channel": "api:A", "reason": "source_model_disabled"},
        {"channel": "api:B", "reason": "source_model_disabled"},
    ])
    assert not scheduler.skip_no_route_log("test-model", result, model_never_supported=False)
    monkeypatch.setattr(scheduler.model_state, "is_source_enabled", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError()))
    assert not scheduler.skip_no_route_log("test-model", result, model_never_supported=False)
