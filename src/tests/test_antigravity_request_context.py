from __future__ import annotations

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.responses import JSONResponse

from src.antigravity import request_context as ctx
from src.antigravity.generation_policy import SHORT_IDENTITY
from src.openai.transform.guard import GuardError
from src.tests.test_antigravity_oauth import _import_modules, _setup, _add_ag_account
from src.tests.test_antigravity_package_a import _patch_failover_runtime, _google

MODEL = "gemini-4-carbon-medium"
ENUM = "MODEL_PLACEHOLDER_M437"


@pytest.fixture(autouse=True)
def clean_context():
    ctx.clear_for_tests()
    yield
    ctx.clear_for_tests()


def attempt(context=None, **kwargs):
    defaults = dict(principal="key-a", account="account-a", base_url="https://daily.example",
                    model=MODEL, anchor="session-a", model_enum=ENUM)
    return ctx.next_attempt(context or ctx.RequestContext(), **{**defaults, **kwargs})


def test_round_and_retry_have_separate_counters(monkeypatch):
    clock = [1.0]
    monkeypatch.setattr(ctx.time, "time", lambda: clock[0])
    logical = ctx.context_of(ctx.ensure_context({}))
    first = attempt(logical)
    clock[0] = 2.0
    retry = attempt(logical)
    following = attempt()
    assert [(x.round, x.step) for x in (first, retry, following)] == [(0, 1), (0, 2), (1, 4)]
    assert first.cascade == retry.cascade == following.cascade
    assert first.trajectory == following.trajectory
    assert first.labels()["request_id"] == retry.labels()["request_id"]
    assert first.epoch_ms == 1000 and retry.epoch_ms == 2000
    for identity in (first, retry, following):
        labels = identity.labels()
        assert len(labels) == 9 and all(isinstance(value, str) for value in labels.values())
        assert labels["cascade_id"] == labels["root_cascade_id"] == identity.cascade
        assert int(labels["last_step_index"]) == identity.step - 1
        assert identity.request_id == f"agent/{identity.cascade}/{identity.epoch_ms}/{identity.trajectory}/{identity.step}"


@pytest.mark.parametrize("change", [
    {"principal": "key-b"}, {"account": "account-b"}, {"base_url": "https://prod.example"},
    {"anchor": "session-b"}, {"model": "gemini-4-carbon-low"},
])
def test_scope_and_model_changes_start_new_top_level_session(change):
    first = attempt()
    second = attempt(**change)
    assert first.cascade != second.cascade and second.round == 0 and second.step == 1
    if "model" in change:
        assert attempt().cascade != first.cascade


def test_missing_anchor_or_tenant_never_merges_unrelated_requests():
    assert attempt(anchor="").cascade != attempt(anchor="").cascade
    assert attempt(principal="").cascade != attempt(principal="").cascade
    logical = ctx.RequestContext()
    assert attempt(logical, anchor="").cascade == attempt(logical, anchor="").cascade


def test_untrusted_context_is_ignored_and_deepcopy_keeps_trusted_context():
    body = {"_parrot_antigravity_context": {"cascade": "injected"}, "input": "same text"}
    prepared = ctx.ensure_context(body)
    assert prepared is not body and body["_parrot_antigravity_context"] == {"cascade": "injected"}
    assert ctx.context_of(copy.deepcopy(prepared)) is ctx.context_of(prepared)
    assert attempt(ctx.context_of(prepared)).cascade != "injected"


def test_parallel_round_allocation_is_unique():
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: attempt(), range(64)))
    assert len({i.cascade for i in identities}) == 1
    assert sorted(i.round for i in identities) == list(range(64))
    assert sorted(i.step for i in identities) == list(range(1, 128, 2))


def test_expiry_bounds_and_unavailable_enum(monkeypatch):
    monkeypatch.setattr(ctx, "MAX_SESSIONS", 2)
    monkeypatch.setattr(ctx, "MAX_RESPONSES", 2)
    now = [10.0]
    monkeypatch.setattr(ctx.time, "monotonic", lambda: now[0])
    first = attempt(model_enum="")
    assert first.labels() == {}
    for n in range(4):
        identity = attempt(anchor=str(n))
        ctx.remember_response(str(n), identity)
    assert len(ctx._sessions) <= 2 and len(ctx._responses) <= 2
    current = attempt(anchor="3")
    now[0] += ctx.TTL_SECONDS + 1
    assert attempt(anchor="3").cascade != current.cascade
    assert not ctx._responses


async def channel(m, monkeypatch):
    _setup(m)
    ag, om = m["ag"], m["oauth_manager"]
    ak = _add_ag_account(om, models=[MODEL], account_model_catalog={
        "antigravityGenerationBaseUrl": ag.daily_api_base_url(),
        "models": [{"id": MODEL, "antigravitySources": [{"baseUrl": ag.daily_api_base_url(), "modelEnum": ENUM, "thinkingLevel": 2}]}],
    })
    async def token(*args, **kwargs): return "synthetic-token"
    monkeypatch.setattr(om, "ensure_channel_token", token)
    return m["AntigravityOAuthChannel"](om.get_account(ak))


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_channel_labels_preserve_prompts_and_client_data(m, monkeypatch, ingress):
    ch = await channel(m, monkeypatch)
    body = {"model": MODEL, "_api_key_name": "tenant", "metadata": {"session_id": "client-session"}}
    if ingress == "responses":
        body.update(input="original text", instructions="You are a custom client")
    elif ingress == "chat":
        body["messages"] = [{"role": "system", "content": "You are a custom client"}, {"role": "user", "content": "original text"}]
    else:
        body.update(system="You are a custom client", max_tokens=100,
                    messages=[{"role": "user", "content": "original text"}])
    before = copy.deepcopy(body)
    requests = [await ch.build_upstream_request(body, MODEL, ingress_protocol=ingress) for _ in range(2)]
    wires = [json.loads(request.body) for request in requests]
    a, b = [wire["request"]["labels"] for wire in wires]
    assert a["model_enum"] == ENUM and a["cascade_id"] == b["cascade_id"]
    assert a["request_id"].endswith("-0") and b["request_id"].endswith("-1")
    assert wires[0]["request"]["sessionId"] == wires[1]["request"]["sessionId"]
    assert wires[0]["request"]["sessionId"].startswith("-")
    assert wires[0]["request"]["systemInstruction"]["parts"][0]["text"] == SHORT_IDENTITY + "\n\nYou are a custom client"
    assert wires[0]["request"]["contents"][0]["parts"][0]["text"] == "original text"
    assert "_parrot_antigravity_context" not in requests[0].body.decode()
    assert body == before


@pytest.mark.asyncio
async def test_previous_response_keeps_session_after_tenant_checked_store(m, monkeypatch):
    from src.openai import store
    from src.providers import registry
    ch = await channel(m, monkeypatch)
    store.init()
    body = {"model": MODEL, "_api_key_name": "context-tenant", "input": "hello"}
    first = await ch.build_upstream_request(body, MODEL)
    raw = json.dumps({"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": "hello back"}]}, "finishReason": "STOP"}]}}).encode()
    result = json.loads(await registry.restore_response_bytes(ch, raw, translator_ctx=first.translator_ctx))
    assert result["store"] is True
    followup = {**body, "input": "continue", "previous_response_id": result["id"]}
    second = await ch.build_upstream_request(followup, MODEL)
    a, b = [json.loads(req.body)["request"]["labels"] for req in (first, second)]
    assert a["cascade_id"] == b["cascade_id"] and b["request_id"].endswith("-1")
    with pytest.raises(GuardError) as error:
        await ch.build_upstream_request({**followup, "_api_key_name": "other-tenant"}, MODEL)
    assert error.value.status == 403


@pytest.mark.parametrize("mixed_cc", [False, True])
@pytest.mark.asyncio
async def test_failover_same_account_retry_keeps_round_but_advances_attempt(m, monkeypatch, mixed_cc):
    from src import failover, scheduler
    from src.protocols.runtime import AttemptResult
    ch = await channel(m, monkeypatch)
    ch.cc_mimicry = mixed_cc
    get = m["config"].get
    _patch_failover_runtime(monkeypatch, failover)
    monkeypatch.setattr(m["config"], "get", get)
    async def no_wait(*args, **kwargs): pass
    monkeypatch.setattr(failover.asyncio, "sleep", no_wait)
    wires = []
    async def send(channel, resolved_model, body, *args, **kwargs):
        assert ctx.context_of(body).anchor == ""
        if mixed_cc:
            assert body.get("_parrot_claude_code_session_id")
        req = await channel.build_upstream_request(body, resolved_model)
        wires.append(json.loads(req.body))
        if len(wires) == 1:
            return AttemptResult(outcome="http_error", http_status=429, error_detail=_google("0.1s"), full_response_text=_google("0.1s"))
        return AttemptResult(outcome="success", success=True, http_status=200, response=JSONResponse({"ok": True}))
    monkeypatch.setattr(failover, "_try_channel", send)
    route = scheduler.ScheduleResult([(ch, MODEL)], None, False)
    response = await failover.run_failover(route, {"model": MODEL, "input": "hello", "_api_key_name": "tenant"},
                                           "logical-request", "tenant", "ip", False, time.time(), ingress_protocol="responses")
    assert response.status_code == 200 and len(wires) == 2
    a, b = [wire["request"]["labels"] for wire in wires]
    assert a["request_id"] == b["request_id"]
    assert a["last_step_index"] == "0" and b["last_step_index"] == "1"
    assert wires[0]["request"]["contents"] == wires[1]["request"]["contents"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_completed_parallel_tools_reserve_each_step(m, monkeypatch, stream, ingress):
    from src.providers import registry
    ch = await channel(m, monkeypatch)
    body = {"model": MODEL, "_api_key_name": "tenant", "metadata": {"session_id": "parallel"},
            "stream": stream, "store": False}
    if ingress == "responses":
        body["input"] = "read two files"
    else:
        body.update(messages=[{"role": "user", "content": "read two files"}], max_tokens=100)
    steps = []
    for tool_count in (2, 0, 2, 0):
        req = await ch.build_upstream_request(body, MODEL, ingress_protocol=ingress)
        steps.append(int(json.loads(req.body)["requestId"].rsplit("/", 1)[1]))
        parts = [{"functionCall": {"id": f"call-{n}", "name": "read_file", "args": {"path": str(n)}}}
                 for n in range(tool_count)] or [{"text": "done"}]
        payload = {"response": {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}}
        raw = (("data: " + json.dumps(payload) + "\n\n").encode() if stream else json.dumps(payload).encode())
        await registry.restore_response_bytes(ch, raw, translator_ctx=req.translator_ctx)
    assert steps == [1, 4, 6, 9]


def test_old_concurrent_round_cannot_overwrite_latest_tool_count():
    first = attempt()
    second = attempt()
    ctx.complete_response(second, {"status": "completed", "output": [{"type": "function_call"}] * 2})
    ctx.complete_response(first, {"status": "completed", "output": []})
    assert attempt().step == 6


def _cache_body(ingress):
    body = {"model": MODEL, "_api_key_name": "tenant", "_parrot_client_ip": "127.0.0.1"}
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
               {"role": "user", "content": "continue"}]
    if ingress == "responses":
        body.update(instructions="Helpful assistant", input=history)
    elif ingress == "chat":
        body["messages"] = [{"role": "system", "content": "Helpful assistant"}, *history]
    else:
        body.update(system="Helpful assistant", max_tokens=100, messages=history)
    return body


def _auto_cache(body, ingress):
    if ingress != "anthropic":
        from src.openai.handler import _maybe_apply_auto_prompt_cache_key
        _maybe_apply_auto_prompt_cache_key(body, fp_query=None, api_key_name="tenant",
                                          client_ip="127.0.0.1", model=MODEL, ingress_protocol=ingress)
        assert "prompt_cache_key" in body["_internal_injected_fields"]


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_generated_content_cache_never_identifies_independent_conversations(m, monkeypatch, ingress):
    ch = await channel(m, monkeypatch)
    m["config"].update(lambda c: c.setdefault("openai", {}).setdefault("autoPromptCacheKey", {}).update(enabled=True))
    wires = []
    for question in ("write a poem", "calculate 10*10"):
        body = _cache_body(ingress)
        if ingress == "anthropic":
            body["messages"] = [{"role": "user", "content": question}]
        _auto_cache(body, ingress)
        before = copy.deepcopy(body)
        req = await ch.build_upstream_request(body, MODEL, ingress_protocol=ingress)
        wires.append(json.loads(req.body)["request"])
        assert body == before
    assert wires[0]["labels"]["cascade_id"] != wires[1]["labels"]["cascade_id"]
    assert wires[0]["sessionId"] != wires[1]["sessionId"]
    assert all(w["labels"]["request_id"].endswith("-0") for w in wires)


@pytest.mark.parametrize("ingress", ["responses", "chat", "anthropic"])
@pytest.mark.asyncio
async def test_explicit_session_wins_over_auto_cache_and_stays_stable(m, monkeypatch, ingress):
    ch = await channel(m, monkeypatch)
    m["config"].update(lambda c: c.setdefault("openai", {}).setdefault("autoPromptCacheKey", {}).update(enabled=True))
    labels = []
    for session in ("session-a", "session-b", "session-a"):
        body = {**_cache_body(ingress), "metadata": {"session_id": session}}
        _auto_cache(body, ingress)
        req = await ch.build_upstream_request(body, MODEL, ingress_protocol=ingress)
        labels.append(json.loads(req.body)["request"]["labels"])
    a, b, again = labels
    assert a["cascade_id"] != b["cascade_id"]
    assert a["cascade_id"] == again["cascade_id"]
    assert again["request_id"].endswith("-1")


@pytest.mark.parametrize("session", [
    {"metadata": {"session_id": "client-session"}},
    {"metadata": {"user_id": '{"session_id":"client-session"}'}},
    {"_parrot_claude_code_session_id": "client-session"},
])
def test_session_snapshot_precedes_later_cache_or_other_provider_context(session):
    body = ctx.ensure_context({"prompt_cache_key": "shared-bucket", **session})
    from src.transform import cc_mimicry
    later = cc_mimicry.ensure_request_context(body)
    later["prompt_cache_key"] = "another-bucket"
    assert ctx.context_of(later).anchor == "client-session"


def test_explicit_cache_key_is_only_a_fallback():
    assert ctx.context_of({"prompt_cache_key": "client-key"}).anchor == "client-key"
    assert ctx.context_of({"prompt_cache_key": "client-key", "previous_response_id": "resp-parent"}).anchor == ""
    assert ctx.context_of({"prompt_cache_key": "auto-key", "_internal_injected_fields": ["prompt_cache_key"]}).anchor == ""


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_previous_response_survives_auto_cache_key_changes(m, monkeypatch, stream):
    from src.openai import store
    from src.providers import registry
    ch = await channel(m, monkeypatch)
    store.init()
    m["config"].update(lambda c: c.setdefault("openai", {}).setdefault("autoPromptCacheKey", {}).update(enabled=True))
    body = {"model": MODEL, "_api_key_name": "tenant", "input": "hello", "stream": stream}
    _auto_cache(body, "responses")
    first = await ch.build_upstream_request(body, MODEL)
    payload = {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": "hello back"}]}, "finishReason": "STOP"}]}}
    raw = ("data: " + json.dumps(payload) + "\n\n").encode() if stream else json.dumps(payload).encode()
    restored = await registry.restore_response_bytes(ch, raw, translator_ctx=first.translator_ctx)
    if stream:
        events = [json.loads(line[6:]) for line in restored.decode().splitlines() if line.startswith("data: ")]
        result = next(e["response"] for e in events if e["type"] == "response.completed")
    else:
        result = json.loads(restored)
    assert result["store"] is True
    followup = {"model": MODEL, "_api_key_name": "tenant", "input": "continue", "previous_response_id": result["id"]}
    _auto_cache(followup, "responses")
    assert body["prompt_cache_key"] != followup["prompt_cache_key"]
    second = await ch.build_upstream_request(followup, MODEL)
    a, b = [json.loads(req.body)["request"]["labels"] for req in (first, second)]
    assert a["cascade_id"] == b["cascade_id"] and b["request_id"].endswith("-1")
    with pytest.raises(GuardError) as error:
        await ch.build_upstream_request({**followup, "_api_key_name": "other-tenant"}, MODEL)
    assert error.value.status == 403


def test_image_envelope_never_inherits_text_labels(m):
    identity = attempt()
    wire = m["codec"].wrap_cloud_code({"contents": []}, model="gemini-3.1-flash-image", project_id="p", request_identity=identity)
    assert wire["requestId"].startswith("image_gen/")
    assert "labels" not in wire["request"] and "sessionId" not in wire["request"]
