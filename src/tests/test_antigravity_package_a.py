from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src import config
from src.antigravity import images
from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
from src.openai import images_openai_compat as images_compat
from src.providers import antigravity_codec, remote_image
from src.protocols.runtime import (
    AttemptResult, apply_non_stream_response_translator, make_stream_translator,
    retryable_transient_error_kind,
)


def _import_modules():
    from src import failover, scheduler
    from src.protocols import runtime
    return {"failover": failover, "scheduler": scheduler, "runtime": runtime}


class FailoverChannel:
    def __init__(self, key, provider="antigravity", account_key=None):
        self.key, self.type, self.protocol = key, "oauth", "openai-responses"
        self.provider, self.account_key = provider, account_key or key
        self.upstream_stream_only = False


class Parsed(SimpleNamespace):
    def __init__(self, **kw):
        defaults = dict(prompt="draw a blue square", model="gemini-3.1-flash-image",
                        requested_n=1, size=None, response_format="b64_json",
                        native_options={}, input_images=[], mask_url=None)
        defaults.update(kw)
        super().__init__(**defaults)


def test_antigravity_images_parameter_matrix_and_envelope():
    req = images._build_request(Parsed(requested_n=2, size="1536x1024",
                                       native_options={"quality": "hd"}))
    cfg = req["generationConfig"]
    assert cfg == {"responseModalities": ["IMAGE"], "candidateCount": 2,
                   "imageConfig": {"aspectRatio": "3:2", "imageSize": "2K"}}
    for field in ("style", "background"):
        with pytest.raises(ValueError, match="unsupported parameter"):
            images._build_request(Parsed(native_options={field: "x"}))
    for fmt in ("png", "webp"):
        with pytest.raises(ValueError, match="output_format"):
            images._build_request(Parsed(native_options={"output_format": fmt}))
    with pytest.raises(ValueError, match="n must"):
        images._build_request(Parsed(requested_n=5))


def test_antigravity_images_decode_base64_url_usage_and_missing():
    wrapped = {"response": {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}
    ]}}], "usageMetadata": {"promptTokenCount": 3}}}
    b64 = images._decode(wrapped, model="m", response_format="b64_json")
    assert b64["data"] == [{"b64_json": "aGVsbG8="}]
    assert b64["usage"]["promptTokenCount"] == 3
    url = images._decode(wrapped, model="m", response_format="url")
    assert url["data"][0]["url"] == "data:image/png;base64,aGVsbG8="
    with pytest.raises(ValueError, match="no decodable image"):
        images._decode({"response": {"candidates": []}}, model="m", response_format="b64_json")


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def test_antigravity_reasoning_bridge_nonstream_flag_order_and_replay():
    upstream = {"id": "r", "status": "completed", "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "inspect"}], "encrypted_content": "sig-a"},
        {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}", "encrypted_content": "sig-b"},
    ]}
    base = {"response_translator": "anthropic_to_responses", "model_for_response": "m"}
    legacy = apply_non_stream_response_translator(upstream, base)
    assert [b["type"] for b in legacy["content"]] == ["tool_use"]
    enabled = apply_non_stream_response_translator(upstream, {
        **base, "antigravity_anthropic_reasoning_bridge": True,
    })
    assert enabled["content"] == [
        {"type": "thinking", "thinking": "inspect", "signature": "sig-a"},
        {"type": "redacted_thinking", "data": "sig-b"},
        {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
    ]
    replay = antigravity_codec.responses_to_gemini({"input": [
        {"type": "reasoning", "summary": [], "encrypted_content": enabled["content"][1]["data"]},
        {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
    ]})
    assert replay["contents"][0]["parts"][0]["thoughtSignature"] == "sig-b"


@pytest.mark.asyncio
async def test_antigravity_channel_enables_reasoning_bridge_only_for_anthropic(monkeypatch):
    ch = AntigravityOAuthChannel({"email": "bridge@example.com", "project_id": "p", "models": ["m"]})
    import src.oauth_manager as om
    async def token(key): return "token"
    monkeypatch.setattr(om, "ensure_valid_token", token)
    anthropic = await ch.build_upstream_request({
        "model": "m", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}],
    }, "m", ingress_protocol="anthropic")
    assert anthropic.translator_ctx["antigravity_anthropic_reasoning_bridge"] is True
    responses = await ch.build_upstream_request({"model": "m", "input": "hi"}, "m", ingress_protocol="responses")
    assert "antigravity_anthropic_reasoning_bridge" not in responses.translator_ctx


def test_antigravity_reasoning_bridge_stream_events_and_indexes():
    tr = make_stream_translator({
        "response_translator": "anthropic_to_responses", "model_for_response": "m",
        "antigravity_anthropic_reasoning_bridge": True,
    })
    chunks = [
        _sse("response.output_item.added", {"output_index": 0, "item": {"type": "reasoning", "id": "rs"}}),
        _sse("response.reasoning_summary_text.delta", {"output_index": 0, "item_id": "rs", "delta": "inspect"}),
        _sse("response.output_item.done", {"output_index": 0, "item": {"type": "reasoning", "id": "rs", "summary": [{"type": "summary_text", "text": "inspect"}], "encrypted_content": "sig-a"}}),
        _sse("response.output_item.added", {"output_index": 1, "item": {"type": "function_call", "id": "fc", "call_id": "call", "name": "tool", "encrypted_content": "sig-b"}}),
        _sse("response.output_item.done", {"output_index": 1, "item": {"type": "function_call", "id": "fc", "call_id": "call", "name": "tool", "arguments": "{}", "encrypted_content": "sig-b"}}),
    ]
    raw = b"".join(piece for chunk in chunks for piece in tr.feed(chunk)) + b"".join(tr.close())
    text = raw.decode()
    assert '"type":"thinking_delta","thinking":"inspect"' in text
    assert '"type":"signature_delta","signature":"sig-a"' in text
    assert '"type":"redacted_thinking","data":"sig-b"' in text
    starts = [json.loads(block.split("data: ", 1)[1])["index"] for block in text.split("\n\n")
              if "event: content_block_start" in block]
    assert starts == [0, 1, 2]


class FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def aiter_bytes(self):
        for chunk in self._chunks: yield chunk


class FakeClient:
    def __init__(self, responses): self.responses = list(responses); self.requests = []
    def stream(self, method, url, headers=None, extensions=None):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "extensions": extensions or {}})
        return self.responses.pop(0)


def public_resolver(host, port, *args):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


@pytest.mark.asyncio
async def test_remote_https_image_download_redirect_mime_magic_and_size():
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 10
    client = FakeClient([
        FakeResponse(status=302, headers={"location": "https://cdn.example/b.png"}),
        FakeResponse(headers={"content-type": "image/png", "content-length": str(len(png))}, chunks=[png[:5], png[5:]]),
    ])
    raw, mime = await remote_image.download_https_image(
        "https://example/a", resolver=public_resolver, client=client,
    )
    assert raw == png and mime == "image/png"
    assert [r["url"] for r in client.requests] == [
        "https://93.184.216.34/a", "https://93.184.216.34/b.png",
    ]
    assert [r["headers"]["Host"] for r in client.requests] == ["example", "cdn.example"]
    assert [r["extensions"]["sni_hostname"] for r in client.requests] == ["example", "cdn.example"]

    with pytest.raises(remote_image.RemoteImageError, match="non-public"):
        await remote_image.download_https_image(
            "https://internal/x", resolver=lambda *a: [(2, 1, 6, "", ("127.0.0.1", 443))],
            client=FakeClient([]),
        )
    with pytest.raises(remote_image.RemoteImageError, match="exceeds"):
        await remote_image.download_https_image(
            "https://example/x", resolver=public_resolver,
            client=FakeClient([FakeResponse(headers={"content-type": "image/png", "content-length": "101"})]),
            max_bytes=100,
        )
    with pytest.raises(remote_image.RemoteImageError, match="does not match"):
        await remote_image.download_https_image(
            "https://example/x", resolver=public_resolver,
            client=FakeClient([FakeResponse(headers={"content-type": "image/png"}, chunks=[b"<html>"])]),
        )
    with pytest.raises(remote_image.RemoteImageError, match="exceeds"):
        await remote_image.download_https_image(
            "https://example/x", resolver=public_resolver,
            client=FakeClient([FakeResponse(headers={"content-type": "image/png"}, chunks=[
                b"\x89PNG\r\n\x1a\n" + b"x" * 60, b"y" * 60,
            ])]), max_bytes=100,
        )


@pytest.mark.asyncio
async def test_remote_image_pins_single_dns_answer_and_revalidates_redirect():
    png = b"\x89PNG\r\n\x1a\n" + b"ok"
    calls = []
    answers = {
        "first.example": "93.184.216.34",
        "next.example": "1.1.1.1",
    }
    def resolver(host, port, *args):
        calls.append(host)
        # A second lookup of first.example would simulate a private rebind.  It
        # must never occur because the connector receives the first public IP.
        if calls.count(host) > 1 and host == "first.example":
            return [(2, 1, 6, "", ("127.0.0.1", port))]
        return [(2, 1, 6, "", (answers[host], port))]
    client = FakeClient([
        FakeResponse(status=302, headers={"location": "https://next.example/final"}),
        FakeResponse(headers={"content-type": "image/png"}, chunks=[png]),
    ])
    await remote_image.download_https_image("https://first.example/start", resolver=resolver, client=client)
    assert calls == ["first.example", "next.example"]
    assert [r["url"] for r in client.requests] == [
        "https://93.184.216.34/start", "https://1.1.1.1/final",
    ]


def _google(delay="1.5s", reason="RATE_LIMIT_EXCEEDED"):
    return json.dumps({"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason},
    ]}})


class ImageUpstreamResponse(FakeResponse):
    def __init__(self, status=200, body=None):
        payload = body if body is not None else {"response": {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}
        ]}}]}}
        super().__init__(status=status, headers={"content-type": "application/json"}, chunks=[json.dumps(payload).encode()])


class ImageNetworkClient:
    def __init__(self, response, capture): self.response, self.capture = response, capture
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    def stream(self, method, url, headers=None, content=None):
        self.capture.update(method=method, url=url, headers=headers, wire=json.loads(content))
        return self.response


def _images_app():
    app = FastAPI()
    async def route(request: Request): return await images_compat.handle_generations(request)
    app.add_api_route("/v1/images/generations", route, methods=["POST"])
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format,field", [("b64_json", "b64_json"), ("url", "url")])
async def test_images_real_route_antigravity_wire_and_slot(monkeypatch, response_format, field):
    model = "gemini-3.1-flash-image"
    ch = AntigravityOAuthChannel({"email": "fake@example.com", "project_id": "p", "imageModels": [model]})
    monkeypatch.setattr(images.registry, "all_channels", lambda: [ch])
    monkeypatch.setattr(images.cooldown, "is_blocked", lambda *a: False)
    monkeypatch.setattr(images.cooldown, "clear", lambda *a: None)
    acquired, released, capture = [], [], {}
    async def acquire(key): acquired.append(key); return True
    monkeypatch.setattr(images.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(images.concurrency, "release", lambda key: released.append(key))
    import src.oauth_manager as om
    async def token(key): return "token"
    monkeypatch.setattr(om, "ensure_valid_token", token)
    monkeypatch.setattr(images.network, "async_client", lambda **kw: ImageNetworkClient(ImageUpstreamResponse(), capture))
    cfg = config.get()
    monkeypatch.setitem(
        cfg, "apiKeys",
        {"test": {"key": "token", "allowImages": True, "allowedModels": []}},
    )
    monkeypatch.setitem(cfg.setdefault("images", {}), "enabled", True)
    transport = httpx.ASGITransport(app=_images_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/images/generations", headers={"Authorization": "Bearer token"}, json={
            "prompt": "blue square", "model": model, "n": 2, "size": "1536x1024",
            "quality": "hd", "response_format": response_format,
        })
    assert response.status_code == 200, response.text
    assert field in response.json()["data"][0]
    assert capture["wire"]["model"] == model
    generation = capture["wire"]["request"]["generationConfig"]
    assert generation == {"responseModalities": ["IMAGE"], "candidateCount": 2,
                          "imageConfig": {"aspectRatio": "3:2", "imageSize": "2K"}}
    assert acquired == released and len(acquired) == 1


@pytest.mark.asyncio
async def test_images_real_route_unsupported_cooldown_error_and_slot_release(monkeypatch):
    model = "gemini-3.1-flash-image"
    ch = AntigravityOAuthChannel({"email": "fake2@example.com", "project_id": "p", "imageModels": [model]})
    monkeypatch.setattr(images.registry, "all_channels", lambda: [ch])
    cfg = config.get()
    monkeypatch.setitem(
        cfg, "apiKeys",
        {"test": {"key": "token", "allowImages": True, "allowedModels": []}},
    )
    monkeypatch.setitem(cfg.setdefault("images", {}), "enabled", True)
    transport = httpx.ASGITransport(app=_images_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bad = await client.post("/v1/images/generations", headers={"Authorization": "Bearer token"},
                                json={"prompt": "x", "model": model, "style": "vivid"})
        assert bad.status_code == 400
        ch.disabled_reason = "quota"
        disabled = await client.post("/v1/images/generations", headers={"Authorization": "Bearer token"},
                                     json={"prompt": "x", "model": model})
        assert disabled.status_code == 503
        ch.disabled_reason = None
        monkeypatch.setattr(images.cooldown, "is_blocked", lambda *a: True)
        blocked = await client.post("/v1/images/generations", headers={"Authorization": "Bearer token"},
                                    json={"prompt": "x", "model": model})
        assert blocked.status_code == 503
        monkeypatch.setattr(images.cooldown, "is_blocked", lambda *a: False)
        monkeypatch.setattr(images.cooldown, "record_error", lambda *a, **kw: None)
        async def acquire(key): return True
        released = []
        monkeypatch.setattr(images.concurrency, "try_acquire", acquire)
        monkeypatch.setattr(images.concurrency, "release", lambda key: released.append(key))
        import src.oauth_manager as om
        async def token(key): return "token"
        monkeypatch.setattr(om, "ensure_valid_token", token)
        monkeypatch.setattr(images.network, "async_client", lambda **kw: ImageNetworkClient(ImageUpstreamResponse(429, {"error": "limited"}), {}))
        failed = await client.post("/v1/images/generations", headers={"Authorization": "Bearer token"},
                                   json={"prompt": "x", "model": model})
        assert failed.status_code == 429
        assert len(released) == 1


def _patch_failover_runtime(monkeypatch, failover, *, cfg=None):
    async def acquire(_key): return True
    monkeypatch.setattr(failover.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(failover.concurrency, "release", lambda *_: None)
    monkeypatch.setattr(failover, "_pick_non_direct_proxy_name", lambda *_: None)
    monkeypatch.setattr(failover, "_should_use_responses_upstream_ws", lambda *_a, **_k: False)
    monkeypatch.setattr(failover.local_web_tools, "request_declares_supported_tools", lambda *_: False)
    monkeypatch.setattr(failover.local_web_tools, "openai_responses_local_web_active", lambda *_: False)
    monkeypatch.setattr(failover.log_db, "record_retry_attempt", lambda *a, **k: int(a[1]))
    for name in ("update_retry_attempt", "update_pending", "finish_error"):
        monkeypatch.setattr(failover.log_db, name, lambda *a, **k: None)
    monkeypatch.setattr(failover.quota_errors, "zhipu_1310_reset_ms", lambda *a, **k: None)
    monkeypatch.setattr(failover.config, "get", lambda: cfg or {
        "timeouts": {"total": 30}, "retry": {"transient": {"enabled": True, "maxExtraAttempts": 2}},
        "concurrency": {"queueWaitSeconds": 0}, "affinity": {},
    })


@pytest.mark.asyncio
async def test_run_failover_antigravity_short_429_waits_same_candidate(m, monkeypatch):
    failover = m["failover"]; _patch_failover_runtime(monkeypatch, failover)
    ch = FailoverChannel("ag:one")
    route = m["scheduler"].ScheduleResult([(ch, "m")], None, False)
    calls, sleeps = [], []
    results = [
        AttemptResult(outcome="http_error", http_status=429, error_detail=_google(),
                      full_response_text=_google(), retry_after_seconds=1.5),
        AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True})),
    ]
    async def attempt(*a, **k): calls.append(a[0].key); return results.pop(0)
    async def sleep(delay): sleeps.append(delay)
    monkeypatch.setattr(failover, "_try_channel", attempt)
    monkeypatch.setattr(failover.asyncio, "sleep", sleep)
    response = await failover.run_failover(route, {"model": "m"}, "r", "k", "ip", False,
                                           __import__("time").time(), ingress_protocol="responses")
    assert response.status_code == 200
    assert calls == ["ag:one", "ag:one"] and sleeps == [1.5]


@pytest.mark.asyncio
async def test_run_failover_antigravity_medium_cooldown_and_quota_disable(m, monkeypatch):
    failover = m["failover"]; _patch_failover_runtime(monkeypatch, failover)
    first, second = FailoverChannel("ag:first", account_key="acct"), FailoverChannel("ag:second")
    route = m["scheduler"].ScheduleResult([(first, "m"), (second, "m")], None, False)
    effects, disabled, calls = [], [], []
    monkeypatch.setattr(failover.finalize_policy, "apply_error_health_effects",
                        lambda *a, **kw: effects.append(kw))
    monkeypatch.setattr(failover.oauth_manager, "set_disabled_by_quota",
                        lambda account, reset: disabled.append((account, reset)))
    sequence = [
        AttemptResult(outcome="http_error", http_status=429, error_detail=_google("30s"),
                      full_response_text=_google("30s"), cooldown_until=123456),
        AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True})),
    ]
    async def attempt(ch, *a, **k): calls.append(ch.key); return sequence.pop(0)
    monkeypatch.setattr(failover, "_try_channel", attempt)
    response = await failover.run_failover(route, {"model": "m"}, "r2", "k", "ip", False,
                                           __import__("time").time(), ingress_protocol="responses")
    assert response.status_code == 200 and calls == ["ag:first", "ag:second"]
    assert effects[0]["cooldown_until"] == 123456 and disabled == []

    quota_route = m["scheduler"].ScheduleResult([(first, "m"), (second, "m")], None, False)
    quota = _google("300s", "QUOTA_EXHAUSTED")
    sequence[:] = [AttemptResult(outcome="http_error", http_status=429, error_detail=quota, full_response_text=quota),
                   AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True}))]
    response = await failover.run_failover(quota_route, {"model": "m"}, "r3", "k", "ip", False,
                                           __import__("time").time(), ingress_protocol="responses")
    assert response.status_code == 200 and disabled == [("acct", None)]


@pytest.mark.asyncio
async def test_images_route_uses_google_429_short_retry_and_quota_disable(monkeypatch):
    model = "gemini-3.1-flash-image"
    ch = AntigravityOAuthChannel({
        "email": "image-429@example.com", "project_id": "p", "imageModels": [model],
    })
    monkeypatch.setattr(images.registry, "all_channels", lambda: [ch])
    monkeypatch.setattr(images.cooldown, "is_blocked", lambda *a: False)
    monkeypatch.setattr(images.cooldown, "clear", lambda *a: None)
    recorded_errors, sleeps, disabled = [], [], []
    monkeypatch.setattr(
        images.cooldown, "record_error",
        lambda *a, **kw: recorded_errors.append((a, kw)),
    )
    async def acquire(_key): return True
    monkeypatch.setattr(images.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(images.concurrency, "release", lambda _key: None)
    monkeypatch.setattr(images.asyncio, "sleep", lambda delay: _record_sleep(sleeps, delay))

    import src.oauth_manager as om
    async def token(_key): return "token"
    monkeypatch.setattr(om, "ensure_valid_token", token)
    monkeypatch.setattr(
        om, "set_disabled_by_quota",
        lambda account, reset: disabled.append((account, reset)),
    )

    cfg = config.get()
    monkeypatch.setitem(
        cfg, "apiKeys",
        {"test": {"key": "token", "allowImages": True, "allowedModels": []}},
    )
    monkeypatch.setitem(cfg.setdefault("images", {}), "enabled", True)
    monkeypatch.setitem(cfg, "retry", {
        "transient": {
            "enabled": True,
            "maxExtraAttempts": 2,
            "errors": {"antigravityRateLimit": True},
        },
    })

    responses = [
        ImageUpstreamResponse(429, json.loads(_google("1.5s"))),
        ImageUpstreamResponse(),
    ]
    monkeypatch.setattr(
        images.network, "async_client",
        lambda **kw: ImageNetworkClient(responses.pop(0), {}),
    )
    transport = httpx.ASGITransport(app=_images_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer token"},
            json={"prompt": "x", "model": model},
        )
        assert response.status_code == 200
        assert sleeps == [1.5]
        assert not recorded_errors and not disabled

        monkeypatch.setattr(images.time, "time", lambda: 1000.0)
        responses[:] = [ImageUpstreamResponse(
            429, json.loads(_google("30s", "RATE_LIMIT_EXCEEDED")),
        )]
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer token"},
            json={"prompt": "x", "model": model},
        )
        assert response.status_code == 429
        assert recorded_errors[-1][1]["cooldown_until"] == 1_030_000
        recorded_errors.clear()

        responses[:] = [ImageUpstreamResponse(
            429, json.loads(_google("300s", "QUOTA_EXHAUSTED")),
        )]
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer token"},
            json={"prompt": "x", "model": model},
        )
    assert response.status_code == 429
    assert disabled == [(ch.account_key, None)]
    assert not recorded_errors


async def _record_sleep(sleeps, delay):
    sleeps.append(delay)


def test_antigravity_short_429_is_provider_bounded_transient():
    result = AttemptResult(outcome="http_error", http_status=429,
                           error_detail=_google(), full_response_text=_google())
    ag = SimpleNamespace(provider="antigravity")
    other = SimpleNamespace(provider="openai")
    assert retryable_transient_error_kind(ag, result) == "antigravityRateLimit"
    assert retryable_transient_error_kind(other, result) is None
    long = AttemptResult(outcome="http_error", http_status=429,
                         error_detail=_google("3s"), full_response_text=_google("3s"))
    assert retryable_transient_error_kind(ag, long) is None
