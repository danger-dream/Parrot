"""Real failover/translation/HTTP and Responses WS, gated fake provider I/O."""
import asyncio
import copy
import json
import time

import httpx
import pytest

from src import local_web_tools as web, search_tool_policy as policy
from src.tests import test_protocol_fake_upstreams as fx
from src.tests.test_search_tool_policy import request, reply, settings  # noqa: F401
from src.tests.test_search_true_stream import with_text, collect
from src.tests.search_stream_fixtures import events, encode, text_delta


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, obj, protocol, number, marks, gate):
        self.obj, self.protocol, self.number = obj, protocol, number
        self.marks, self.gate = marks, gate
        self.closed = False

    async def __aiter__(self):
        count = 0
        for event, data in events(self.obj, self.protocol):
            if event == "response.completed" or event == "message_stop" or data is None:
                self.marks.append((self.number, "upstream-terminal", time.monotonic()))
            yield encode(event, data)
            if data and text_delta(data, self.protocol):
                count += 1
                if count == 2:
                    # Causality proof: EOF is impossible until the client has
                    # received two text deltas through the production pipeline.
                    await asyncio.wait_for(self.gate.wait(), 2)
                await asyncio.sleep(0.001)

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol", ["responses", "chat", "anthropic", "ws"])
@pytest.mark.parametrize("upstream_protocol", ["responses", "chat", "anthropic"])
@pytest.mark.parametrize("search", [False, True])
async def test_production_ingress_delivers_text_before_upstream_end(protocol, upstream_protocol, search, monkeypatch):
    m = fx._import_modules()
    fx._setup(m)
    fx._install_keys(m, fx._default_key(key="sk-ws" if protocol == "ws" else "ccp-test"))
    if upstream_protocol == "anthropic":
        channel = fx._make_anthropic_channel(m, "gated", "https://gated.example", alias="test-model", real="test-model")
    else:
        channel = fx._make_openai_channel("gated", "https://gated.example", protocol="openai-" + upstream_protocol,
            alias="test-model", real="test-model", extra={"responsesWsUpstreamTransport": "sse"})
    fx._install_channels(m, [channel])
    ingress = "responses" if protocol == "ws" else protocol
    body = request(ingress, hosted=search)
    if not search:
        # Reproduce the reported 51-tool declaration case, without executing
        # any tool: the presence of WebSearch must not buffer ordinary text.
        if ingress == "responses":
            body["tools"][0]["name"] = "WebSearch"
        for index in range(50):
            fn = {"name": "client_tool_" + str(index), "parameters": {"type": "object"}}
            tool = ({"type": "function", "function": fn} if ingress == "chat" else
                    {"name": fn["name"], "input_schema": fn["parameters"]} if ingress == "anthropic" else
                    {"type": "function", **fn})
            body["tools"].append(tool)
        assert len(body["tools"]) == 51
    body["stream"] = True
    captured, streams, gates, marks, seen, executed = [], [], [], [], [], []
    async def execute(calls, **kw):
        executed.extend(calls)
        return [web.LocalToolResult(c.id, "private executed result") for c in calls]
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    def model(req):
        payload = json.loads(req.content)
        assert payload["stream"] is True
        number = len(captured)
        marks.append((number, "upstream-start", time.monotonic()))
        captured.append(copy.deepcopy(payload))
        name = (payload["tools"][0].get("function") or payload["tools"][0])["name"]
        obj = reply(upstream_protocol, name if search and number == 0 else None)
        if search and number == 0:
            obj = with_text(obj, upstream_protocol, "我查一下")
        gate = asyncio.Event()
        gates.append(gate)
        stream = GatedStream(obj, upstream_protocol, number, marks, gate)
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})
    def receive(data):
        text = text_delta(data, ingress)
        if text:
            number = len(captured) - 1
            seen.append((number, text))
            marks.append((number, "downstream-text", time.monotonic()))
            if len([x for x in seen if x[0] == number]) >= 2:
                assert not streams[number].closed
                gates[number].set()
    router = fx.MockRouter()
    router.register("https://gated.example", model)
    if protocol == "ws":
        from src.tests.test_openai_responses_ws import FakeWebSocket
        class Socket(FakeWebSocket):
            async def send_text(self, text):
                receive(json.loads(text))
                await super().send_text(text)
        ws = Socket({**body, "type": "response.create"})
        client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle))
        m["upstream"].set_client(client)
        try:
            await asyncio.wait_for(m["responses_ws"].handle_responses_ws(ws), 6)
            raw = "\n".join(ws.sent_texts).encode()
        finally:
            await client.aclose()
    else:
        if protocol == "anthropic":
            response, client, _ = await fx._call_anthropic_core(m, router, body)
        else:
            response, client = await fx._call_openai_handler(m, router, protocol, body)
        try:
            raw, _ = await asyncio.wait_for(collect(response, receive), 6)
        finally:
            await client.aclose()
    assert len(captured) == (2 if search else 1)
    assert len(executed) == int(search)
    assert "".join(x[1] for x in seen) == ("我查一下" if search else "") + "Found Python docs"
    assert all(s.closed for s in streams)
    assert b"parrot_hosted_" not in raw and b"private executed result" not in raw
    timing = []
    for number in range(len(captured)):
        arrivals = [stamp for n, kind, stamp in marks if n == number and kind == "downstream-text"]
        terminal = next(stamp for n, kind, stamp in marks if n == number and kind == "upstream-terminal")
        start = next(stamp for n, kind, stamp in marks if n == number and kind == "upstream-start")
        assert gates[number].is_set() and len(arrivals) >= 2
        assert arrivals[0] < arrivals[1] < terminal
        timing.append({'round': number, 'deltas': len(arrivals), 'first_ms': round((arrivals[0]-start)*1000, 2),
                       'second_ms': round((arrivals[1]-start)*1000, 2), 'terminal_ms': round((terminal-start)*1000, 2)})
    print("ARRIVAL", protocol, upstream_protocol, search, timing)


@pytest.mark.parametrize("cancel_phase", ["model", "search"])
async def test_ws_cancel_owns_model_search_and_lease(cancel_phase, monkeypatch):
    from src.openai import responses_ws
    from src import failover
    from types import SimpleNamespace
    from fastapi.responses import StreamingResponse
    body = request(hosted=True)
    name = next(iter(policy.compile_request(body, "responses")[1].values())).name
    entered, closed, released = asyncio.Event(), asyncio.Event(), []
    async def execute(calls, **kw):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    monkeypatch.setattr(web, "execute_local_tool_calls", execute)
    async def invoke(*args, **kwargs):
        async def source():
            try:
                for event, data in events(reply("responses", name), "responses"):
                    yield encode(event, data)
                    if cancel_phase == "model":
                        entered.set()
                        await asyncio.Event().wait()
            finally:
                if cancel_phase == "model":
                    closed.set()
        return StreamingResponse(source())
    monkeypatch.setattr(failover, "run_failover", invoke)
    class Socket:
        application_state = None

        async def receive(self):
            await entered.wait()
            return {"type": "websocket.receive", "text": '{"type":"response.cancel"}'}
        async def send_text(self, text):
            assert json.loads(text).get("type") != "response.completed"
        async def close(self, **kw):
            pass
    class Lease:
        async def release(self):
            released.append(True)
    await asyncio.wait_for(responses_ws._run_search_ws_session(Socket(), body=body, schedule_result=SimpleNamespace(),
        request_id=None, api_key_name="k", client_ip="ip", start_time=time.time(), start_monotonic=time.monotonic(),
        allowed_models=None, api_key_lease=Lease()), 3)
    assert closed.is_set() and released == [True]
