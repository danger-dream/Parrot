"""Regression cases for the four unpublished Codex review findings.

Uses the existing isolated test fixtures; all upstream activity is mocked.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from src.tests import _isolation

_isolation.isolate()

from src.tests import test_codex_compaction_owner as compaction_tests
from src.tests import test_m4_failover as failover_tests
from src.tests import test_openai_oauth_channel as channel_tests
from src.tests import test_openai_responses_ws as ws_tests
from src.openai import codex_identity, compaction_owner
from src import failover, state_db


@pytest.fixture(autouse=True)
def _scoped_oauth_mock_mode(monkeypatch):
    # Reused channel helpers also set this process-wide marker directly. Own it
    # here so later token/usage tests can exercise their mocked network paths.
    monkeypatch.setenv("DISABLE_OAUTH_NETWORK_CALLS", "1")


def _setup_oauth():
    modules = channel_tests._import_modules()
    channel_tests._setup(modules)
    channel_tests._add_openai_acc(modules)
    account = modules["oauth_manager"].get_account("openai:o@openai.test:acct-123")
    return modules, modules["OpenAIOAuthChannel"](account)


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress,limit_field", [
    ("responses", "max_output_tokens"),
    ("chat", "max_completion_tokens"),
    ("anthropic", "max_tokens"),
])
async def test_codex_stripping_preserves_next_api_candidate(ingress, limit_field):
    _, channel = _setup_oauth()
    original = {"model": "gpt-5.1", limit_field: 123, "temperature": 0.2}
    if ingress == "responses":
        original["input"] = "hi"
    else:
        original["messages"] = [{"role": "user", "content": "hi"}]
    attempt = failover._attempt_body_for_channel(original, channel.key, None)
    request = await channel.build_upstream_request(
        attempt, "gpt-5.1", ingress_protocol=ingress,
    )
    wire = json.loads(request.body)
    assert "max_output_tokens" not in wire
    assert limit_field not in wire
    assert "temperature" not in wire
    next_attempt = failover._attempt_body_for_channel(original, "api:next", None)
    assert next_attempt[limit_field] == 123
    assert next_attempt["temperature"] == 0.2


@pytest.mark.parametrize("boundary_kind", ["workspace", "account"])
def test_published_compaction_owner_survives_upgrade(boundary_kind):
    state_db.init()
    channel = compaction_tests.OAuthChannel("upgrade@example.com", "upgrade-workspace")
    refs = compaction_owner.complete_refs(
        compaction_tests.body("cmp_upgrade_" + boundary_kind)
    )
    # Exact b8ba47c/v0.31.13 hash, including its pre-refresh account fallback.
    boundary = (
        "workspace:upgrade-workspace"
        if boundary_kind == "workspace"
        else "account:openai:upgrade@example.com"
    )
    old_owner = hashlib.sha256(json.dumps(
        {"provider": "openai", "boundary": boundary},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    state_db.compaction_owner_upsert(
        refs[0].compaction_id, refs[0].content_digest, channel.key, old_owner,
    )
    # A different account must not be allowed to adopt the old reference.
    other = compaction_tests.OAuthChannel("other@example.com", "other-workspace")
    with pytest.raises(compaction_owner.CompactionRouteError):
        compaction_owner.select_owner(refs, [other], live_channels=[other])
    assert compaction_owner.select_owner(
        refs, [channel, other], live_channels=[channel, other],
    ) is channel
    stored = state_db.compaction_owner_load(refs[0].compaction_id, refs[0].content_digest)
    assert stored["owner_identity"] == compaction_owner.owner_identity(channel)


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_first", [False, True])
@pytest.mark.parametrize("terminal", ["error", "success", "cancel"])
async def test_queued_http_attempt_locks_and_releases(monkeypatch, primary_first, terminal):
    modules, channel = _setup_oauth()
    channel_tests._add_openai_acc(
        modules, email="queue@openai.test", chatgpt_account_id="queue-acct",
    )
    queued_channel = modules["OpenAIOAuthChannel"](
        modules["oauth_manager"].get_account("openai:queue@openai.test:queue-acct")
    )
    failover_tests._setup(failover_tests._import_modules())
    seen = []
    body = {
        "model": "gpt-5.1", "input": "hi",
        "prompt_cache_key": f"queue-{primary_first}-{terminal}",
        "_api_key_name": "review", "max_output_tokens": 123,
    }

    async def acquire(*args, **kwargs):
        return True

    async def acquire_queued(*args, **kwargs):
        return queued_channel.key, (queued_channel, "gpt-5.1")

    async def attempt(candidate, model, request, *args, **kwargs):
        await candidate.build_upstream_request(request, model, ingress_protocol="responses")
        seen.append(codex_identity.active_thread_turn_queue_count())
        if candidate is queued_channel:
            if terminal == "cancel":
                raise asyncio.CancelledError
            if terminal == "success":
                return failover.AttemptResult(
                    outcome="success", success=True, http_status=200,
                    response=JSONResponse({"output": []}),
                )
        return failover.AttemptResult(
            outcome="http_error", http_status=500, error_detail="mock upstream failure",
        )

    monkeypatch.setattr(failover.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(failover.concurrency, "acquire_from_candidates", acquire_queued)
    monkeypatch.setattr(failover.concurrency, "release", lambda *_: None)
    monkeypatch.setattr(failover, "_try_channel", attempt)
    monkeypatch.setattr(failover, "_should_use_responses_upstream_ws", lambda *a, **k: False)
    monkeypatch.setattr(failover, "_transient_retry_limit", lambda cfg: 0)
    route = SimpleNamespace(
        candidates=[(channel, "gpt-5.1")] if primary_first else [],
        saturated=[(queued_channel, "gpt-5.1")], affinity_hit=False, fp_query=None,
    )
    try:
        result = failover.run_failover(
            route, body, f"queue-{primary_first}-{terminal}", "review", "127.0.0.1",
            False, time.time(), ingress_protocol="responses",
        )
        if terminal == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await result
        else:
            await result
        assert len(seen) == (2 if primary_first else 1)
        assert all(count == 1 for count in seen)
        assert codex_identity.active_thread_turn_queue_count() == 0
        assert body["max_output_tokens"] == 123
    finally:
        codex_identity.release_request_turn_serialization(body)


@pytest.mark.asyncio
async def test_native_ws_second_turn_compaction_advances_next_window(monkeypatch):
    modules = ws_tests._import_modules()
    ws_tests._setup(modules)
    ws_tests._make_oauth_channel_for_failover(modules, name="review-ws@example.com")

    async def token(_):
        return "tok"

    def create(turn, value, previous=None):
        frame = {
            "type": "response.create", "model": "test-model", "input": value,
            "client_metadata": {"session_id": "review-session", "turn_id": turn},
        }
        if previous:
            frame["previous_response_id"] = previous
        return frame

    websocket = ws_tests.SequentialFakeWebSocket(
        create("one", "first"),
        create("two", [{"type": "compaction_trigger"}], "r1"),
        create("three", "third", "r2"),
    )
    upstream = ws_tests.FakeUpstreamWebSocket([
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.completed", "response": {"id": "r1", "output": [], "usage": {}}},
        {"type": "response.created", "response": {"id": "r2"}},
        {"type": "response.completed", "response": {
            "id": "r2", "output": [{
                "type": "compaction", "id": "cmp_review_ws", "encrypted_content": "opaque-review",
            }], "usage": {},
        }},
        {"type": "response.created", "response": {"id": "r3"}},
        {"type": "response.completed", "response": {"id": "r3", "output": [], "usage": {}}},
    ])

    async def connect(*args, **kwargs):
        return upstream

    monkeypatch.setattr(modules["responses_ws"].oauth_manager, "ensure_valid_token", token)
    monkeypatch.setattr(modules["responses_ws"], "_connect_upstream_ws", connect)
    await modules["responses_ws"].handle_responses_ws(websocket)
    assert len(upstream.sent) == 3, repr(websocket.close_calls)
    wire = [json.loads(frame) for frame in upstream.sent]
    metadata = [json.loads(frame["client_metadata"]["x-codex-turn-metadata"]) for frame in wire]
    assert [item["window_number"] for item in metadata] == [0, 0, 1]
    assert metadata[1]["context_window_id"] != metadata[2]["context_window_id"]
    assert len({item["session_id"] for item in metadata}) == 1
    assert codex_identity.active_thread_turn_queue_count() == 0
