"""Search contract acceptance: isolated model/service transports, no live OAuth."""
import asyncio
import copy
import json

import pytest
from fastapi.responses import JSONResponse

from src import local_web_tools as web, search_service, search_tool_policy as policy, search_tool_wire as wire
from src.openai.transform.guard import GuardError
from src.tests.search_stream_fixtures import wire as stream_wire, decode, text_delta


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    cfg = {"functionMode": "managed", "hostedMode": "managed", "maxToolRounds": 4,
           "maxResults": 8, "maxFetchUrlChars": 2048, "maxFetchChars": 50000,
           "requireKnownUrlForFetch": True, "maxConcurrentToolCalls": 0}
    monkeypatch.setattr(search_service, "settings", lambda: dict(cfg))
    policy._REPLAY.clear()
    return cfg


def request(protocol="responses", hosted=False, mixed=False):
    tool = {"type": "web_search", "filters": {"allowed_domains": ["docs.python.org"]}, "external_web_access": False} if hosted else {"type": "function", "name": "web_search", "parameters": {"type": "object"}}
    other = {"type": "function", "name": "lookup", "parameters": {"type": "object"}}
    if protocol == "anthropic":
        tool = {"type": "web_search_20250305", "name": "web_search", "allowed_domains": ["docs.python.org"], "external_web_access": False} if hosted else {"name": "WebSearch", "input_schema": {"type": "object"}}
        other = {"name": "lookup", "input_schema": {"type": "object"}}
    elif protocol == "chat":
        if not hosted:
            tool = {"type": "function", "function": {"name": "WebSearch", "parameters": {"type": "object"}}}
        other = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    history = {"input": "search Python"} if protocol == "responses" else {"messages": [{"role": "user", "content": "search Python"}]}
    return {"model": "test-model", **history, "tools": [tool, other] if mixed else [tool], "prompt_cache_key": "session", "stream": False}


def reply(protocol, name=None, mixed=False):
    calls = [("local-1", name)] if name else []
    if mixed:
        calls.append(("client-1", "lookup"))
    if protocol == "responses":
        output = [{"type": "function_call", "id": "fc_" + cid, "call_id": cid, "name": n, "arguments": '{"query":"Python docs","external_web_access":true}', "status": "completed"} for cid, n in calls]
        if not calls:
            output = [{"type": "message", "id": "msg_1", "role": "assistant", "content": [{"type": "output_text", "text": "Found Python docs"}], "status": "completed"}]
        return {"object": "response", "id": "resp_calls" if calls else "resp_final", "model": "test-model", "status": "completed", "output": output, "usage": {"input_tokens": 12, "output_tokens": 5}}
    if protocol == "anthropic":
        return {"type": "message", "id": "msg_calls" if calls else "msg_final", "role": "assistant", "model": "test-model", "content": [{"type": "tool_use", "id": cid, "name": n, "input": {"query": "Python docs", "external_web_access": True}} for cid, n in calls] or [{"type": "text", "text": "Found Python docs"}], "stop_reason": "tool_use" if calls else "end_turn", "usage": {"input_tokens": 12, "output_tokens": 5}}
    return {"object": "chat.completion", "id": "chat_1", "model": "test-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": None if calls else "Found Python docs", "tool_calls": [{"id": cid, "type": "function", "function": {"name": n, "arguments": '{"query":"Python docs","external_web_access":true}'}} for cid, n in calls]}, "finish_reason": "tool_calls" if calls else "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 5}}


@pytest.mark.parametrize("field", ["tools", "functions", "namespace"])
@pytest.mark.parametrize("value", [1, False, {}, "not-an-array"])
def test_malformed_tool_collections_are_client_errors(field, value):
    body = {field: value} if field != "namespace" else {
        "tools": [{"type": "namespace", "name": "browser", "tools": value}],
    }
    with pytest.raises(GuardError, match="must be an array") as exc:
        policy.validate(body)
    assert exc.value.status == 400


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.parametrize("mode", ["managed", "passthrough", "disabled"])
def test_three_states_original_kind(protocol, hosted, mode, settings):
    settings["hostedMode" if hosted else "functionMode"] = mode
    settings["functionMode" if hosted else "hostedMode"] = "disabled"
    body = request(protocol, hosted)
    original = copy.deepcopy(body)
    if mode == "disabled":
        with pytest.raises(GuardError, match="disabled"):
            policy.compile_request(body, protocol)
    else:
        compiled, plan = policy.compile_request(body, protocol)
        assert bool(plan) is (mode == "managed")
        assert body == original
        if mode == "passthrough":
            assert compiled["tools"] == body["tools"]
        elif hosted:
            assert compiled["tools"][0].get("type") in (None, "function")
            assert next(iter(plan.values())).category == "hosted"


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_managed_call_result_final(protocol, hosted, stream, monkeypatch):
    seen, model_rounds = [], []
    async def search(args, **kwargs):
        seen.append((args, kwargs))
        return {"query": args["query"], "results": [{"title": "Python", "url": "https://docs.python.org/", "snippet": "docs"}], "provider": "fake", "backend_id": "isolated"}
    monkeypatch.setattr(search_service, "search", search)
    async def invoke(body):
        model_rounds.append(copy.deepcopy(body))
        _, plan = policy.compile_request(request(protocol, hosted), protocol)
        name = next(iter(plan.values())).name
        return JSONResponse(reply(protocol, name if len(model_rounds) == 1 else None))
    body = request(protocol, hosted)
    if stream:
        response = policy.stream(body, protocol, invoke, request_id="req-test", api_key_name="A")
        chunks = b"".join([c async for c in response.body_iterator])
        assert b"Found Python docs" in chunks
        assert b'"name":"parrot_hosted_' not in chunks
        assert (b"[DONE]" if protocol == "chat" else (b"response.completed" if protocol == "responses" else b"message_stop")) in chunks
    else:
        response = await policy.run(body, protocol, invoke, request_id="req-test", api_key_name="A")
        assert b"Found Python docs" in response.body
    assert len(seen) == 1 and len(model_rounds) == 2
    assert seen[0][1]["request_id"] == "req-test"
    if hosted:
        assert seen[0][0]["external_web_access"] is False
        assert seen[0][0]["allowed_domains"] == ["docs.python.org"]
    history = json.dumps(model_rounds[1])
    assert "local-1" in history and "https://docs.python.org/" in history
    assert model_rounds[1]["prompt_cache_key"] == "session"


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
async def test_mixed_tools_resume_exact_ids_without_reexecuting(protocol, monkeypatch):
    searches = []
    async def search(args, **kw):
        searches.append(args)
        return {"query": args["query"], "results": [], "answer": "real simulated result"}
    monkeypatch.setattr(search_service, "search", search)
    body = request(protocol, hosted=True, mixed=True)
    _, plan = policy.compile_request(body, protocol)
    name = next(iter(plan.values())).name
    async def first(_):
        return JSONResponse(reply(protocol, name, mixed=True))
    response = await policy.run(body, protocol, first, api_key_name="A")
    visible = json.loads(response.body)
    assert [c[1] for c in policy._calls(visible, protocol)] == ["client-1"]
    assert len(searches) == 1
    if protocol == "responses":
        continuation = {"model": "test-model", "input": [{"type": "function_call_output", "call_id": "client-1", "output": "client-result"}], "previous_response_id": "resp_calls"}
    elif protocol == "anthropic":
        continuation = {"model": "test-model", "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "client-1", "content": "client-result"}]}]}
    else:
        continuation = {"model": "test-model", "messages": [{"role": "tool", "tool_call_id": "client-1", "content": "client-result"}]}
    assert policy.restore_replay(continuation, protocol, "B") == continuation
    if protocol != "responses":
        # Chat/Anthropic are stateless protocols: a call ID alone is not a
        # conversation credential. Carry the actual visible history to resume.
        delta = continuation["messages"]
        continuation = copy.deepcopy(body)
        policy._append(continuation, visible, [], protocol)
        continuation["messages"].extend(delta)
    resumed = []
    async def final(b):
        resumed.append(copy.deepcopy(b))
        return JSONResponse(reply(protocol))
    await policy.run(continuation, protocol, final, api_key_name="A")
    assert len(searches) == 1
    text = json.dumps(resumed[0])
    assert "real simulated result" in text and "client-result" in text
    assert text.count("real simulated result") == 1
    assert "local-1" in text and "client-1" in text


def test_image_untouched_and_hosted_function_name_collision():
    body = request(hosted=True)
    body["tools"] += [{"type": "image_generation"}, {"type": "function", "name": "web_search", "parameters": {}}, {"type": "function", "name": "parrot_hosted_web_search", "parameters": {}}]
    compiled, plan = policy.compile_request(body, "responses")
    assert compiled["tools"][1] == {"type": "image_generation"}
    assert len({t.get("name") for t in compiled["tools"] if t.get("type") == "function"}) == 3
    assert len(plan) == 2


def test_xai_structural_aliases_choice_history_namespace_collision():
    body = request()
    body["tools"] += [{"type": "namespace", "name": "browser", "tools": [{"type": "function", "name": "WebFetch", "parameters": {}}]}]
    body["input"] = [{"type": "function_call", "name": "web_search", "call_id": "c", "arguments": '{"text":"web_search"}'}, {"type": "function_call_output", "call_id": "c", "output": {"type": "function_call", "name": "web_search"}}]
    body["tool_choice"] = {"type": "allowed_tools", "tools": [{"type": "function", "name": "web_search"}]}
    compiled, mapping = wire.compile_xai(body)
    alias = mapping["to_wire"]["web_search"]
    assert alias != "web_search"
    assert compiled["input"][0]["name"] == alias
    assert compiled["input"][0]["arguments"] == body["input"][0]["arguments"]
    assert compiled["input"][1] == body["input"][1]
    assert compiled["tool_choice"]["tools"][0]["name"] == alias
    assert compiled["tools"][1]["tools"][0]["name"] != "WebFetch"
    body["tools"].append({"type": "function", "name": alias, "parameters": {}})
    compiled2, mapping2 = wire.compile_xai(body)
    assert mapping2["to_wire"]["web_search"] != alias
    assert compiled2["tools"][-1]["name"] == alias
    assert compiled["prompt_cache_key"] == "session"


@pytest.mark.parametrize("name", ["web_search", "WebSearch", "WebFetch", "web_fetch"])
async def test_xai_fragmented_stream_all_events_and_concurrency(name):
    async def one(label):
        payload, mapping = wire.compile_xai({"tools": [{"type": "function", "name": name, "parameters": {}}]})
        alias = payload["tools"][0]["name"]
        call = {"type": "function_call", "id": "fc_" + label, "call_id": label, "name": alias, "arguments": '{"text":"' + alias + '中文"}'}
        events = [{"type": "response.output_item.added", "item": call}, {"type": "response.output_item.done", "item": call}, {"type": "response.completed", "response": {"output": [call]}}]
        raw = b"".join(web._sse(e["type"], e) for e in events)
        chunks = []
        for idx in range(0, len(raw), 7):
            await asyncio.sleep(0)
            chunks.append(wire.restore_bytes(raw[idx:idx+7], mapping))
        out = b"".join(chunks).decode()
        assert '"name":"' + name + '"' in out
        assert alias + "中文" in out
        assert mapping["buffer"] == b""
        return out
    a, b = await asyncio.gather(one("A"), one("B"))
    assert '"call_id":"B"' not in a and '"call_id":"A"' not in b


def test_unexpected_hosted_is_not_silent_stop():
    _, state = wire.compile_xai(request())
    with pytest.raises(ValueError, match="misclassified"):
        wire.restore_object({"type": "response.output_item.done", "item": {"type": "web_search_call"}}, state)


def test_cross_protocol_passthrough_stays_hosted(settings):
    from src.openai.transform import anthropic_to_responses, responses_to_anthropic
    settings.update(functionMode="managed", hostedMode="passthrough")
    original = request("anthropic", True)
    original["tool_choice"] = {"type": "tool", "name": "web_search"}
    # Anthropic cannot express the OpenAI offline field; preserving an unknown
    # field was not proof of interoperability. Native cross-bridge must reject.
    with pytest.raises(GuardError, match="external_web_access"):
        anthropic_to_responses.translate_request(original)
    original["tools"][0].pop("external_web_access")
    converted = anthropic_to_responses.translate_request(original)
    assert converted["tools"][0] == {"type": "web_search", "filters": {"allowed_domains": ["docs.python.org"]}}
    assert converted["tool_choice"] == {"type": "web_search"}
    assert not policy.needs_loop(converted)
    converted["model"] = original["model"]
    back = responses_to_anthropic.translate_request(converted)
    assert back["tools"][0]["type"].startswith("web_search_")
    assert back["tools"][0]["allowed_domains"] == ["docs.python.org"]
    converted["tools"][0]["external_web_access"] = False
    with pytest.raises(GuardError, match="offline/cached"):
        responses_to_anthropic.translate_request(converted)


async def test_cancel_stops_service_task():
    cancelled = asyncio.Event()
    entered = asyncio.Event()
    async def invoke(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    stream = policy.stream(request(), "responses", invoke)
    iterator = stream.body_iterator
    pull = asyncio.create_task(anext(iterator))
    await entered.wait()
    pull.cancel()
    await asyncio.gather(pull, return_exceptions=True)
    await iterator.aclose()
    assert cancelled.is_set()


async def test_service_error_is_structured_tool_result_and_offline_not_weakened(monkeypatch):
    async def search(args, **kw):
        assert args["external_web_access"] is False
        raise search_service.SearchError("No offline backend", code="offline_unavailable", status_code=400)
    monkeypatch.setattr(search_service, "search", search)
    result = await web.execute_local_tool_call(web.LocalToolCall("c", "web_search", {"query": "test", "external_web_access": False}))
    assert result.is_error and "offline_unavailable" in result.content


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("upstream_protocol", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
async def test_http_real_pipeline_managed_matrix(protocol, upstream_protocol, hosted, streaming, monkeypatch):
    import httpx
    from src.tests import test_protocol_fake_upstreams as fx
    m = fx._import_modules()
    fx._setup(m)
    fx._install_keys(m, fx._default_key())
    # Explicit fake-channel models avoid real OAuth or live channel discovery.
    if upstream_protocol == "anthropic":
        ch = fx._make_anthropic_channel(m, "search-matrix", "https://search-matrix.example", alias="test-model", real="test-model")
    else:
        ch = fx._make_openai_channel("search-matrix", "https://search-matrix.example", protocol="openai-" + upstream_protocol, alias="test-model", real="test-model")
    fx._install_channels(m, [ch])
    captured, searches = [], []
    async def search(args, **kw):
        searches.append(args)
        return {"query": args["query"], "results": [{"title": "Python", "url": "https://docs.python.org/", "snippet": "docs"}]}
    monkeypatch.setattr(search_service, "search", search)
    def model(req):
        payload = json.loads(req.content)
        captured.append(payload)
        fn = payload["tools"][0].get("function") or payload["tools"][0]
        name = fn["name"]
        assert payload["tools"][0].get("type") in (None, "function")
        result = reply(upstream_protocol, name if len(captured) == 1 else None)
        if payload.get("stream"):
            return httpx.Response(200, content=stream_wire(result, upstream_protocol), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=result)
    router = fx.MockRouter()
    router.register("https://search-matrix.example", model)
    body = request(protocol, hosted)
    body["stream"] = streaming
    if protocol == "anthropic":
        response, client, _ = await fx._call_anthropic_core(m, router, body)
    else:
        response, client = await fx._call_openai_handler(m, router, protocol, body)
    try:
        raw = b"".join([chunk async for chunk in response.body_iterator]) if streaming and hasattr(response, "body_iterator") else response.body
        assert response.status_code == 200, raw
        assert ("".join(text_delta(frame, protocol) for frame in decode(raw)) == "Found Python docs") if streaming else b"Found Python docs" in raw, raw
        assert len(captured) == 2 and len(searches) == 1
        assert "local-1" in json.dumps(captured[1])
        assert "docs.python.org" in json.dumps(captured[1])
    finally:
        await client.aclose()


@pytest.mark.parametrize("mode", ["managed", "passthrough", "disabled"])
async def test_ws_first_and_followup_share_policy(mode, settings, monkeypatch):
    import httpx
    from src.tests import test_protocol_fake_upstreams as fx
    m = fx._import_modules()
    fx._setup(m)
    settings["functionMode"] = mode
    fx._install_keys(m, fx._default_key(key="sk-ws"))
    ch = fx._make_openai_channel("ws-search", "https://ws-search.example", protocol="openai-responses", alias="test-model", real="test-model", extra={"responsesWsUpstreamTransport": "sse"})
    fx._install_channels(m, [ch])
    model_rounds, searches = [], []
    async def search(args, **kw):
        searches.append(args)
        return {"query": args["query"], "results": []}
    monkeypatch.setattr(search_service, "search", search)
    def model(req):
        payload = json.loads(req.content)
        model_rounds.append(payload)
        name = payload["tools"][0]["name"]
        obj = reply("responses", name if len(model_rounds) == 1 else None)
        if payload.get("stream"):
            frames = [{"type": "response.created", "response": {"id": obj["id"]}}, {"type": "response.completed", "response": obj}]
            return httpx.Response(200, content=b"".join(web._sse(e["type"], e) for e in frames), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=obj)
    router = fx.MockRouter()
    router.register("https://ws-search.example", model)
    client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle))
    m["upstream"].set_client(client)
    body = request()
    body["type"] = "response.create"
    from src.tests.test_openai_responses_ws import SequentialFakeWebSocket
    # A sequential client waits for the terminal response before its next create;
    # the active-turn reader now also observes disconnect/cancel frames.
    inp = [{"type": "function_call_output", "call_id": "local-1", "output": "client-result"}] if mode == "passthrough" else "continue"
    next_body = {"type": "response.create", "model": "test-model", "previous_response_id": "resp_calls", "input": inp}
    ws = SequentialFakeWebSocket(body, *([] if mode == "disabled" else [next_body]))
    try:
        await m["responses_ws"].handle_responses_ws(ws)
        text = "\n".join(ws.sent_texts)
        if mode == "disabled":
            assert "disabled" in text and not model_rounds and not searches
        else:
            assert text.count('"type":"response.completed"') == 2, text
            assert len(searches) == (1 if mode == "managed" else 0)
            assert len(model_rounds) == (3 if mode == "managed" else 2)
            if mode == "managed":
                assert '"type":"function_call"' not in text
            else:
                assert '"type":"function_call"' in text
                assert "client-result" in json.dumps(model_rounds[-1])
    finally:
        await client.aclose()


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "ws"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("mode", ["managed", "passthrough"])
async def test_xai_actual_pipeline_cache_key_function_output(protocol, streaming, mode, settings, monkeypatch):
    import httpx
    from src.tests import test_protocol_fake_upstreams as fx
    from src.channel.xai_oauth_channel import XAIOAuthChannel
    from src import oauth_manager
    m = fx._import_modules()
    fx._setup(m)
    settings["functionMode"] = mode
    fx._install_keys(m, fx._default_key(key="sk-ws" if protocol == "ws" else "ccp-test"))
    ch = XAIOAuthChannel({"provider": "xai", "email": "mock@xai.invalid", "accessToken": "isolated", "models": ["test-model"]})
    async def token(_):
        return "isolated-not-a-live-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    fx._install_channels(m, [ch])
    captured, searches = [], []
    async def search(args, **kw):
        searches.append(args)
        return {"query": args["query"], "results": []}
    monkeypatch.setattr(search_service, "search", search)
    def model(req):
        payload = json.loads(req.content)
        captured.append(payload)
        assert payload["prompt_cache_key"]
        if protocol != "anthropic":
            assert payload["prompt_cache_key"] == "session"
        assert req.headers.get("x-grok-conv-id")
        name = payload["tools"][0]["name"]
        assert name.startswith("parrot_fn_")
        result = reply("responses", name if len(captured) == 1 else None)
        raw = stream_wire(result, "responses")
        return httpx.Response(200, stream=fx.ChunkedByteStream([raw[i:i+37] for i in range(0, len(raw), 37)]), headers={"content-type": "text/event-stream"})
    router = fx.MockRouter()
    router.register("https://api.x.ai", model)
    body = request("responses" if protocol == "ws" else protocol)
    body["stream"] = streaming
    if protocol == "ws":
        body["type"] = "response.create"
        client = httpx.AsyncClient(transport=httpx.MockTransport(router.handle))
        m["upstream"].set_client(client)
        from src.tests.test_openai_responses_ws import FakeWebSocket
        # Stay connected while the private model/search rounds are running.
        ws = FakeWebSocket(body)
        await m["responses_ws"].handle_responses_ws(ws)
        raw = "\n".join(ws.sent_texts).encode()
    else:
        if protocol == "anthropic":
            response, client, _ = await fx._call_anthropic_core(m, router, body)
        else:
            response, client = await fx._call_openai_handler(m, router, protocol, body)
        raw = b"".join([c async for c in response.body_iterator]) if hasattr(response, "body_iterator") else response.body
    try:
        assert b"parrot_fn_" not in raw, raw
        if mode == "managed":
            if protocol != "ws" and streaming:
                assert "".join(text_delta(frame, protocol) for frame in decode(raw)) == "Found Python docs", raw
            else:
                assert b"Found Python docs" in raw, raw
            assert len(searches) == 1 and len(captured) == 2
        else:
            assert b"local-1" in raw and (b"WebSearch" in raw or b"web_search" in raw), raw
            assert len(searches) == 0 and len(captured) == 1
    finally:
        await client.aclose()


def test_native_hosted_response_history_round_trip_and_no_function():
    from src import search_hosted_codec as codec
    from src.openai.transform import anthropic_to_responses as a2r, responses_to_anthropic as r2a
    native = {"type": "web_search_call", "id": "ws_native", "status": "completed", "action": {"type": "search", "query": "Python", "sources": [{"type": "url", "url": "https://docs.python.org/", "title": "Python"}]}}
    blocks = codec.responses_to_anthropic(native)
    assert blocks[0]["type"] == "server_tool_use" and blocks[1]["tool_use_id"] == "ws_native"
    assert codec.anthropic_to_responses(blocks) == [native]
    converted = a2r.translate_response({"id": "r", "status": "completed", "output": [native]})
    assert converted["content"][0]["type"] == "server_tool_use"
    original = {"model": "m", "messages": [{"role": "assistant", "content": converted["content"]}]}
    assert a2r.translate_request(original)["input"][0] == native
    replay = r2a.translate_request({"model": "m", "input": [native, {"role": "user", "content": "continue"}]})
    assert replay["messages"][0]["content"][0]["type"] == "server_tool_use"
    assert replay["messages"][1]["role"] == "user"


async def test_native_hosted_two_way_streams_keep_result_ids():
    from src.openai.transform.stream_responses_to_anthropic import StreamTranslator as R2A
    from src.openai.transform.stream_anthropic_to_responses import StreamTranslator as A2R
    native = {"type": "web_search_call", "id": "ws_native", "status": "completed", "action": {"type": "search", "query": "Python", "sources": [{"type": "url", "url": "https://docs.python.org/"}]}}
    r2a = R2A(model="m")
    raw = web._sse("response.output_item.done", {"type": "response.output_item.done", "item": native}) + web._sse("response.completed", {"type": "response.completed", "response": {"id": "r", "status": "completed", "output": [native]}})
    downstream = b"".join(r2a.feed(raw)) + b"".join(r2a.close())
    assert b'"type":"server_tool_use"' in downstream
    assert b'"type":"tool_use"' not in downstream
    assert b'"tool_use_id":"ws_native"' in downstream
    a2r = A2R(model="m")
    reverse = b"".join(a2r.feed(downstream)) + b"".join(a2r.close())
    assert b'"type":"web_search_call"' in reverse
    assert b'"type":"function_call"' not in reverse
    assert a2r.get_downstream_responses_output() == [native]


def test_only_satisfied_choice_released_and_allowed_set_kept():
    body = request()
    body["tool_choice"] = {"type": "allowed_tools", "mode": "required", "tools": [{"type": "function", "name": "web_search"}]}
    policy._append(body, reply("responses", "web_search"), [web.LocalToolResult("local-1", "result")], "responses")
    assert body["tool_choice"] == {"type": "allowed_tools", "mode": "auto", "tools": [{"type": "function", "name": "web_search"}]}
    body["tool_choice"] = {"type": "function", "name": "lookup"}
    policy._append(body, reply("responses", "web_search"), [web.LocalToolResult("local-1", "result")], "responses")
    assert body["tool_choice"] == {"type": "function", "name": "lookup"}


async def test_candidate_failover_does_not_reuse_xai_alias_or_failed_predecessor(monkeypatch):
    import httpx
    from src.tests import test_protocol_fake_upstreams as fx
    from src.channel.xai_oauth_channel import XAIOAuthChannel
    from src import oauth_manager
    m = fx._import_modules()
    fx._setup(m)
    fx._install_keys(m, fx._default_key())
    first = XAIOAuthChannel({"provider": "xai", "email": "fail@invalid", "accessToken": "isolated", "models": ["test-model"]})
    second = fx._make_openai_channel("fallback", "https://fallback.example", protocol="openai-responses", alias="test-model", real="test-model")
    fx._install_channels(m, [first, second])
    async def token(_):
        return "isolated"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    original_schedule = m["scheduler"].schedule
    def ordered(*args, **kwargs):
        result = original_schedule(*args, **kwargs)
        result.candidates.sort(key=lambda pair: pair[0].key != first.key)
        return result
    monkeypatch.setattr(m["scheduler"], "schedule", ordered)
    calls, searches = [], []
    async def search(args, **kw):
        searches.append(args)
        return {"query": args["query"], "results": []}
    monkeypatch.setattr(search_service, "search", search)
    def xai(req):
        calls.append("xai")
        assert json.loads(req.content)["tools"][0]["name"].startswith("parrot_fn_")
        return httpx.Response(404, json={"error": {"message": "model unavailable"}})
    def fallback(req):
        calls.append("fallback")
        body = json.loads(req.content)
        assert body["tools"][0]["name"] == "web_search"
        assert "parrot_fn_" not in json.dumps(body)
        return httpx.Response(200, json=reply("responses", "web_search" if calls.count("fallback") == 1 else None))
    router = fx.MockRouter()
    router.register("https://api.x.ai", xai)
    router.register("https://fallback.example", fallback)
    response, client = await fx._call_openai_handler(m, router, "responses", request())
    try:
        assert response.status_code == 200 and b"Found Python docs" in response.body
        assert calls == ["xai", "fallback", "fallback"]
        assert len(searches) == 1
    finally:
        await client.aclose()


async def test_native_ws_turn_can_later_introduce_managed_search(monkeypatch):
    import httpx
    from src.tests import test_openai_responses_ws as wsfx
    from src.tests import test_protocol_fake_upstreams as fx
    m = wsfx._import_modules()
    wsfx._setup(m)
    wsfx._make_channel(m)
    downstream = wsfx.SequentialFakeWebSocket(
        {"type": "response.create", "model": "test-model", "input": "first", "stream": True},
        {"type": "response.create", "model": "test-model", "previous_response_id": "resp_first", "input": "search Python", "tools": [{"type": "web_search"}]},
    )
    prior_output = [{"type": "message", "role": "assistant", "id": "msg_prior", "status": "completed", "content": [{"type": "output_text", "text": "prior-context"}]}]
    upstream_ws = wsfx.FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "resp_first"}},
        {"type": "response.output_item.done", "output_index": 0, "item": prior_output[0]},
        {"type": "response.completed", "response": {"id": "resp_first", "output": prior_output, "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 2}}},
    ])
    async def connect(*args, **kwargs):
        return upstream_ws
    monkeypatch.setattr(m["responses_ws"], "_connect_upstream_ws", connect)
    searches, rounds = [], []
    async def search(args, **kwargs):
        searches.append(args)
        return {"query": args["query"], "results": []}
    monkeypatch.setattr(search_service, "search", search)
    def handler(req):
        body = json.loads(req.content)
        rounds.append(body)
        assert "previous_response_id" not in body
        assert "prior-context" in json.dumps(body)
        obj = reply("responses", body["tools"][0]["name"] if len(rounds) == 1 else None)
        return httpx.Response(200, content=stream_wire(obj, "responses"), headers={"content-type": "text/event-stream"})
    from src import upstream
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream.set_client(client)
    try:
        await m["responses_ws"].handle_responses_ws(downstream)
        text = "\n".join(downstream.sent_texts)
        assert len(upstream_ws.sent) == 1
        assert len(rounds) == 2 and len(searches) == 1
        assert '"type":"function_call"' not in text
        assert "Found Python docs" in text
    finally:
        await client.aclose()
