"""Total deadlines cancel slow headers/bodies, close IO, and keep retry ownership."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from src.oauth.zhipu import common as c, endpoint_routing as er, project_tokens as pt
from src.tests.test_zhipu_provider import credential
from src.tests.zhipu_pat_fixtures import envelope


@pytest.fixture
def slow_io(monkeypatch):
    state = {"body": False, "clients": [], "requests": [], "cancelled": [], "closed": []}
    payload = json.dumps(envelope()).encode()
    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                for _ in range(30):
                    await asyncio.sleep(.01)
                    yield b" "
                yield payload
            except asyncio.CancelledError:
                state["cancelled"].append(True)
                raise
        async def aclose(self):
            state["closed"].append(True)
    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(30):
                time.sleep(.01)
                yield b" "
            yield payload
        def close(self):
            state["closed"].append(True)
    async def async_handler(request):
        state["requests"].append(request)
        if state["body"]:
            return httpx.Response(200, stream=AsyncBody())
        try:
            await asyncio.sleep(.3)
        except asyncio.CancelledError:
            state["cancelled"].append(True)
            raise
        return httpx.Response(200, json=envelope())
    def sync_handler(request):
        state["requests"].append(request)
        if state["body"]:
            return httpx.Response(200, stream=SyncBody())
        time.sleep(.3)
        return httpx.Response(200, json=envelope())
    def async_client(**kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(async_handler), timeout=kwargs["timeout"])
        state["clients"].append(client)
        return client
    def sync_client(**kwargs):
        client = httpx.Client(transport=httpx.MockTransport(sync_handler), timeout=kwargs["timeout"])
        state["clients"].append(client)
        return client
    monkeypatch.setattr(c, "require_network", lambda: None)
    monkeypatch.setattr(c.network, "async_client", async_client)
    monkeypatch.setattr(c.network, "sync_client", sync_client)
    return state


@pytest.mark.parametrize("during_body", [False, True])
@pytest.mark.parametrize("in_loop", [False, True])
def test_pat_total_deadline_closes_each_attempt(slow_io, monkeypatch, during_body, in_loop):
    slow_io["body"] = during_body
    monkeypatch.setattr(pt, "PAT_REQUEST_TIMEOUT_S", .04)
    def run():
        return pt.business_request(credential("oauth"), "https://bigmodel.cn/access_tokens",
            header_kind="pat", method="POST", body={"clientType": "zcode", "clientVersion": c.VERSION},
            envelope=False, stage="project_token", pat_retry=True)
    async def in_event_loop():
        return run()
    with pytest.raises(c.ZhipuError) as failure:
        if in_loop:
            asyncio.run(in_event_loop())
        else:
            run()
    assert failure.value.kind == "timeout" and failure.value.timeout_phase == "total"
    assert failure.value.stage == "project_token"
    assert len(slow_io["requests"]) == 2
    assert len(slow_io["cancelled"]) == 2
    assert len(slow_io["closed"]) == (2 if during_body else 0)
    assert all(client.is_closed for client in slow_io["clients"])
    assert all(request.method == "POST" and json.loads(request.content)["clientVersion"] == c.VERSION
               for request in slow_io["requests"])


@pytest.mark.parametrize("during_body", [False, True])
@pytest.mark.parametrize("cached", [False, True])
async def test_routing_total_deadline_releases_waiters_and_keeps_last_mapping(slow_io, during_body, cached):
    slow_io["body"] = during_body
    router = er.EndpointRouter(account_key="fixture", platform_origin="https://zcode.z.ai",
                               app_version=c.VERSION, request_timeout_s=.04)
    url = "https://open.bigmodel.cn/api/anthropic/v1/messages"
    if cached:
        router.snapshot = (0, {"https://open.bigmodel.cn:443/api/anthropic/v1/messages":
                               (False, "https://zcode.z.ai/api/v1/ultra")})
    snapshot = router.snapshot
    first, second = await asyncio.gather(router.resolve(url), router.resolve(url))
    assert first.routed is cached and second == first
    assert router.snapshot is snapshot and router.retry_after > time.monotonic()
    assert router.flight is None
    assert len(slow_io["requests"]) == len(slow_io["cancelled"]) == 1
    assert len(slow_io["closed"]) == int(during_body)
    assert all(client.is_closed for client in slow_io["clients"])
    assert await router.resolve(url) == first
    assert len(slow_io["requests"]) == 1


@pytest.fixture
def drip_server(monkeypatch):
    state = {"requests": 0, "disconnected": 0}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args):
            pass
        def do_GET(self):
            self.respond({"code": 0, "data": {}})
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.respond(envelope())
        def respond(self, value):
            state["requests"] += 1
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                for byte in raw[:20]:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(.03)
                self.wfile.write(raw[20:])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                state["disconnected"] += 1
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    monkeypatch.setattr(c, "require_network", lambda: None)
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


async def test_routing_deadline_with_real_drip_response(drip_server):
    base, state = drip_server
    router = er.EndpointRouter(account_key="fixture", platform_origin=base, app_version=c.VERSION,
                               allow_insecure=True, request_timeout_s=.12)
    result = await router.resolve("https://open.bigmodel.cn/api/anthropic/v1/messages")
    assert not result.routed and router.snapshot is None
    assert router.retry_after > time.monotonic()
    assert state["requests"] == 1
    await asyncio.sleep(.12)
    assert state["disconnected"] == 1


def test_pat_deadline_with_real_drip_response(drip_server, monkeypatch):
    base, state = drip_server
    monkeypatch.setattr(pt, "PAT_REQUEST_TIMEOUT_S", .12)
    with pytest.raises(c.ZhipuError) as failure:
        pt.business_request(credential("oauth"), base + "/access_tokens", header_kind="pat",
                            method="POST", body={}, envelope=False, stage="project_token", pat_retry=True)
    assert failure.value.timeout_phase == "total"
    assert state["requests"] == 2
    time.sleep(.12)
    assert state["disconnected"] == 2
