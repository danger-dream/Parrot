"""Policy is live per response.create, including persistent native/fallback WS."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src import config
from src.channel import registry
from src.management_auth import Capability
from src.management_control.apikey import ApiKeyControl
from src.tests import test_openai_responses_ws as helpers
from src.tests.test_openai_responses_ws import _isolate_ws_config
from src.tests.test_management_apikey_control import FakeLimiter, FakeStats, context
from src.tests.test_api_key_model_policy import rule


def done(response_id):
    return [{"type": "response.created", "response": {"id": response_id}},
            {"type": "response.completed", "response": {"id": response_id, "output": [],
             "usage": {"input_tokens": 2, "output_tokens": 1}}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["quota_exhausted", "insufficient_balance", "service_busy", "model_unavailable"])
async def test_ws_first_create_never_acquires_or_routes(monkeypatch, preset):
    m = helpers._import_modules()
    cfg = helpers._setup(m)
    cfg["apiKeys"]["ws-key"]["modelBlocks"] = [rule("test-model", preset=preset)]
    from src import scheduler
    fail = Mock(side_effect=AssertionError("local denial reached upstream machinery"))
    for module, name in [(m["responses_ws"], "_connect_upstream_ws"), (m["apikey_limiter"], "acquire"),
                         (scheduler, "schedule"), (m["log_db"], "insert_pending")]:
        monkeypatch.setattr(module, name, fail)
    ws = helpers.FakeWebSocket({"type": "response.create", "model": "test-model", "input": "first"})
    await m["responses_ws"].handle_responses_ws(ws)
    events = [json.loads(text) for text in ws.sent_texts]
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["error"]["code"] == preset
    assert events[0]["status"] in (402, 403)
    assert events[0]["retryable"] is False
    assert events[0]["headers"] == {"x-should-retry": "false"}
    fail.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["native-ws", "codex-http-fallback"])
@pytest.mark.parametrize("change_event,scenario", [
    ("response.created", "enable"), ("response.completed", "enable"),
    ("response.completed", "switch-model"), ("response.completed", "disabled"),
])
async def test_ws_live_policy_turns(monkeypatch, transport, change_event, scenario):
    m = helpers._import_modules()
    cfg = helpers._setup(m)
    cfg["channelSelection"] = "order"
    cfg["apiKeyConcurrency"] = {"enabled": False}
    cfg["concurrency"]["enabled"] = False
    entry = cfg["apiKeys"]["ws-key"]
    entry["allowedModels"] = []
    if transport == "native-ws":
        ch = helpers._make_channel(m, extra={"name": "a"})
    else:
        ch = helpers._make_oauth_channel_for_failover(m, name="a@example.test")
    monkeypatch.setattr(registry, "_channels", {ch.key: ch})
    control = ApiKeyControl(config_store=config, statistics=FakeStats(), limiter=FakeLimiter())
    ctx = context(Capability.READ, Capability.WRITE)
    changed = False
    if scenario == "switch-model":
        entry["modelBlocks"] = [rule("blocked-model")]
    elif scenario == "disabled":
        entry["modelBlocks"] = [rule("test-model", enabled=False)]

    class ReconfiguringWebSocket(helpers.SequentialFakeWebSocket):
        async def send_text(self, text):
            nonlocal changed
            await super().send_text(text)
            event = json.loads(text)
            if scenario == "enable" and not changed and event.get("type") == change_event:
                control.update_api_key(ctx, "ws-key", changes={"model_blocks": [rule("test-model")]})
                changed = True
            if event.get("type") == "error" and scenario == "enable":
                # A following request on the SAME socket can proceed after removal.
                control.update_api_key(ctx, "ws-key", changes={"model_blocks": []})

    ws = ReconfiguringWebSocket(
        {"type": "response.create", "model": "test-model", "input": "first"},
        {"type": "response.create", "model": "blocked-model" if scenario == "switch-model" else "test-model", "input": "second"},
        {"type": "response.create", "model": "test-model", "input": "third"},
    )
    connects, requests = [], []
    denied = scenario != "disabled"
    completed_ids = ["first", "third"] if denied else ["first", "second", "third"]
    if transport == "native-ws":
        upstream = helpers.FakeUpstreamWebSocket([event for rid in completed_ids for event in done(rid)])
        async def connect(url, **kwargs):
            connects.append(url)
            return upstream
        monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    else:
        from websockets.exceptions import InvalidStatus
        from src.transports.http_runtime import OpenedHttpResponse
        class Stream(httpx.AsyncByteStream):
            def __init__(self, events):
                self.events = events
            async def __aiter__(self):
                for event in self.events:
                    yield (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode()
        class Context:
            async def __aexit__(self, *args):
                return None
        async def connect(url, **kwargs):
            connects.append(url)
            raise InvalidStatus(SimpleNamespace(status_code=426, headers={}, body=b"upgrade unavailable"))
        async def opened(**kwargs):
            raw = kwargs["upstream_req"].body
            requests.append(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)
            response = httpx.Response(200, stream=Stream(done(completed_ids[len(requests) - 1])),
                                      request=httpx.Request("POST", "https://example.test/responses"))
            return OpenedHttpResponse(ctx=Context(), response=response, connect_ms=1)
        monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
        monkeypatch.setattr(m["responses_ws"], "open_response_with_proxy_chain", opened)

    conn = m["log_db"]._get_conn()
    before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM request_log").fetchone()[0]
    await m["responses_ws"].handle_responses_ws(ws)
    wire = [json.loads(raw) for raw in upstream.sent] if transport == "native-ws" else requests
    assert len(connects) == 1
    assert len(wire) == len(completed_ids)
    assert "first" in json.dumps(wire[0]["input"])
    assert "third" in json.dumps(wire[-1]["input"])
    rows = conn.execute("SELECT status, input_tokens, output_tokens FROM request_log WHERE id>? ORDER BY id", (before,)).fetchall()
    assert len(rows) == len(completed_ids)
    assert [(row["status"], row["input_tokens"], row["output_tokens"]) for row in rows] == [("success", 2, 1)] * len(completed_ids)
    events = [json.loads(text) for text in ws.sent_texts]
    assert [e["response"]["id"] for e in events if e.get("type") == "response.completed"] == completed_ids
    errors = [e for e in events if e.get("type") == "error"]
    assert len(errors) == int(denied)
    if errors:
        assert errors[0]["status"] == 402
        assert errors[0]["error"]["code"] == "quota_exhausted"
        assert errors[0]["retryable"] is False
    assert not ws.close_calls
