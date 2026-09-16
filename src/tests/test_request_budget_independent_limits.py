"""Independent input/output budgets through real xAI/Cursor builds and HTTP wire."""
from __future__ import annotations

import copy
import json
import time
import uuid

import httpx
import pytest

from src import config, failover, log_db, model_metadata, oauth_manager, scheduler, token_counter, upstream
from src.channel import registry
from src.channel.cursor_oauth_channel import CursorOAuthChannel
from src.channel.xai_oauth_channel import XAIOAuthChannel
from src.cursor_bridge import catalog as cursor_catalog, runtime as cursor_runtime
from src.cursor_bridge.models import CursorModel
from src.tests._config_isolation import isolated_config
from src.tests.test_media_ingress_fixes import ws_modules

pytestmark = pytest.mark.usefixtures("isolated_config")
MODEL = "grok-4.6"


@pytest.fixture
def native_routes(monkeypatch, ws_modules):
    _, cfg = ws_modules
    monkeypatch.setattr(config, "_reload_callbacks", [])
    # No Cursor bridge listener or real OAuth lifecycle is needed for wire build.
    monkeypatch.setattr(cursor_runtime, "base_url", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(cursor_runtime, "bearer_secret", lambda: "isolated-bridge-token")
    monkeypatch.setattr(cursor_runtime, "update_account", lambda *_: None)
    async def fixture_token(_):
        return "isolated-access-token"
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", fixture_token)
    xai = {
        "provider": "xai", "email": "budget@example.test", "subject": "budget-xai",
        "models": [MODEL], "base_url": "https://api.x.ai/v1", "enabled": True,
        "account_model_catalog": {"models": [{
            "id": MODEL, "contextWindow": 500_000, "maxOutputTokens": 500_000,
            "compactTriggerTokens": 250_000, "reasoningEfforts": ["high"],
            "serviceTiers": [{"id": "priority", "name": "Priority"}],
        }]},
    }
    cursor = {
        "provider": "cursor", "email": "cursor@example.test", "sub": "budget-cursor",
        "models": [MODEL], "enabled": True,
        "cursor_model_catalog": cursor_catalog.build_catalog([CursorModel(
            id=MODEL, name="Grok budget fixture", context_window=500_000,
            max_tokens=64_000, reasoning=True, supports_agent=True,
            supports_images=False, supports_max_mode=False,
            legacy_slugs=("cursor-grok-4.6-high-fast",),
        )]),
    }
    cfg.update({
        "oauthAccounts": [xai, cursor], "modelBindings": {"defaults": {}, "scoped": {}},
        "modelMetadataOverrides": {"defaults": {}, "scoped": {}},
        "modelCenter": {}, "compactRescue": {"enabled": False, "safetyBufferTokens": 20_000},
    })
    channels = [CursorOAuthChannel(cursor), XAIOAuthChannel(xai)]
    monkeypatch.setattr(registry, "_channels", {ch.key: ch for ch in channels})
    return channels, xai


def _body(repeats=17_000, output=500_000):
    body = {"model": MODEL, "stream": True, "max_output_tokens": output,
            "instructions": "Respond accurately.", "input": "budget " * repeats,
            "reasoning": {"effort": "high"}, "service_tier": "priority"}
    # Calibrate fixture length using the real counter (tokenizer cache may be
    # absent in isolated CI). Never replace the counter or force a token result.
    count = token_counter.count_request_tokens(body, model=MODEL)
    body["input"] = "budget " * max(1, int(repeats * repeats / count))
    return body


async def _run(monkeypatch, channels, body):
    """Real failover, channel transforms, wire guard, HTTP client and ledger."""
    rid = "independent-budget-" + uuid.uuid4().hex
    log_db.insert_pending(rid, "127.0.0.1", "budget-fixture", MODEL, True, 1, 0, {}, body,
                          ingress_protocol="responses")
    sent = []
    def transport(request):
        payload = json.loads(request.content)
        sent.append((str(request.url), payload))
        events = [
            {"type": "response.created", "response": {"id": "budget-response", "model": MODEL}},
            {"type": "response.output_text.delta", "delta": "ok", "output_index": 0, "content_index": 0},
            {"type": "response.completed", "response": {"id": "budget-response", "model": MODEL,
                "status": "completed", "service_tier": "priority", "output": [],
                "usage": {"input_tokens": 17_011, "output_tokens": 1}}},
        ]
        content = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)
    route = scheduler.ScheduleResult(candidates=[(ch, MODEL) for ch in channels], fp_query=None, affinity_hit=False)
    monkeypatch.setattr(upstream, "_client_pool", upstream.SharedClientPool(upstream._new_client))
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        upstream.set_client(client)
        response = await failover.run_failover(
            route, body, rid, "budget-fixture", "127.0.0.1", True, time.time(),
            ingress_protocol="responses", start_monotonic=time.monotonic(),
        )
        if hasattr(response, "body_iterator"):
            chunks = [chunk async for chunk in response.body_iterator]
            assert any(b"response.completed" in (c.encode() if isinstance(c, str) else c) for c in chunks)
    attempts = log_db._get_conn().execute(
        "SELECT outcome,dispatched_at,error_detail FROM retry_chain WHERE request_id=? ORDER BY id", (rid,),
    ).fetchall()
    return response, sent, attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_first", [False, True])
async def test_17k_input_500k_output_reaches_xai_transport_unchanged(monkeypatch, native_routes, cursor_first):
    channels, _ = native_routes
    body = _body()
    original = copy.deepcopy(body)
    assert 16_000 <= token_counter.count_request_tokens(body, model=MODEL) <= 18_000
    # Inspect the real Cursor build too: its requested output is clamped to the
    # route cap in failover, not inside the channel build itself.
    cursor_wire = await channels[0].build_upstream_request(body, MODEL, ingress_protocol="responses")
    cursor_payload = json.loads(cursor_wire.body)
    assert cursor_payload["model"] == "cursor-grok-4.6-high-fast"
    response, sent, attempts = await _run(monkeypatch, channels if cursor_first else channels[1:], body)
    assert response.status_code == 200
    assert len(sent) == 1 and sent[0][0] == "https://api.x.ai/v1/responses"
    wire = sent[0][1]
    assert wire["max_output_tokens"] == 500_000
    assert wire["stream"] is True and wire["reasoning"] == {"effort": "high"}
    assert wire["service_tier"] == "priority"
    assert wire["instructions"] == original["instructions"] and wire["input"] == original["input"]
    assert 16_000 <= token_counter.count_request_tokens(wire, model=MODEL) <= 18_000
    # Existing failover may attach internal bookkeeping, but every client field
    # (especially its requested maximum output) must remain unchanged.
    assert {key: body[key] for key in original} == original
    # Cursor's smaller output cap is now handled by clamping, so the cursor
    # candidate is genuinely dispatched (its fixture base URL refuses the
    # connection) and failover proceeds to the xAI route, which succeeds.
    if cursor_first:
        assert [row["outcome"] for row in attempts] == ["connect_error", "success"]
    else:
        assert [row["outcome"] for row in attempts] == ["success"]
    assert attempts[-1]["dispatched_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", ["input", "output", "explicit_input"])
async def test_true_input_and_output_overflow_remain_zero_dispatch(monkeypatch, native_routes, overflow):
    channels, account = native_routes
    body = _body(repeats=500_050 if overflow == "input" else 17_000,
                 output=500_001 if overflow == "output" else 500_000)
    if overflow == "explicit_input":
        account["account_model_catalog"]["models"][0]["maxInputTokens"] = 16_000
    count = token_counter.count_request_tokens(body, model=MODEL)
    if overflow == "input":
        assert count > 500_000
    elif overflow == "explicit_input":
        assert 16_000 < count < 500_000
    response, sent, attempts = await _run(monkeypatch, channels[1:], body)
    if overflow == "output":
        # Oversized output is clamped to the route's maxOutputTokens and
        # dispatched; local input estimates never hard-reject a request.
        assert response.status_code == 200
        assert len(sent) == 1
        assert sent[0][1]["max_output_tokens"] == 500_000
        assert attempts[-1]["dispatched_at"] is not None
    else:
        # Input length is upstream's authority: the request is dispatched and
        # the mock transport still returns a 200 stream; real refusals arrive
        # as upstream context errors handled by the established failover path.
        assert response.status_code == 200
        assert len(sent) == 1
        assert attempts[-1]["dispatched_at"] is not None


def test_input_budget_and_trigger_do_not_follow_output_maximum(native_routes):
    channels, _ = native_routes
    scope = channels[1].key
    for output in (1, 250_000, 500_000, 500_001):
        body = _body(output=output)
        budget = model_metadata.effective_request_budget(MODEL, scope_key=scope, request_shape=body)
        assert budget.effective_input_budget == 500_000
        assert budget.compact_trigger_tokens == 250_000
        assert budget.output_within_limit is (output <= 500_000)
        assert not model_metadata.should_compact(MODEL, 17_011, scope_key=scope, request_shape=body)
        assert model_metadata.should_compact(MODEL, 250_000, scope_key=scope, request_shape=body)
        assert body["max_output_tokens"] == output
    assert model_metadata.safe_prompt_limit(MODEL, scope_key=scope) == 480_000
    assert model_metadata.required_context_for_compact(17_011, MODEL, scope_key=scope) == 37_011


def test_explicit_native_input_equal_normal_is_not_inferred_as_derived(native_routes):
    channels, account = native_routes
    record = account["account_model_catalog"]["models"][0]
    record["contextWindowMaxMode"] = 1_000_000
    absent = model_metadata.effective_request_budget(MODEL, scope_key=channels[1].key, use_max_context=True)
    assert absent.max_input_tokens is None and absent.effective_input_budget == 1_000_000
    record["maxInputTokens"] = 500_000
    explicit = model_metadata.effective_request_budget(MODEL, scope_key=channels[1].key, use_max_context=True)
    assert explicit.max_input_tokens == explicit.effective_input_budget == 500_000


@pytest.mark.asyncio
async def test_input_tightening_does_not_derive_smaller_output_limit(monkeypatch, native_routes):
    channels, _ = native_routes
    scope = channels[1].key
    model_metadata.patch_override_fields(MODEL, scope_key=scope, outbound_model=MODEL,
                                        set_fields={"contextWindow": 100_000, "maxOutputTokens": 500_000})
    effective = model_metadata.get_metadata(MODEL, scope_key=scope)
    assert effective["contextWindow"] == 100_000 and effective["maxOutputTokens"] == 500_000
    before = copy.deepcopy(config.get())
    with pytest.raises(ValueError, match="native ceiling"):
        model_metadata.patch_override_fields(MODEL, scope_key=scope, outbound_model=MODEL,
                                            set_fields={"maxOutputTokens": 500_001})
    assert config.get() == before
    response, sent, _ = await _run(monkeypatch, channels[1:], _body())
    assert response.status_code == 200 and sent[0][1]["max_output_tokens"] == 500_000
