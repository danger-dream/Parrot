from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ._isolation import isolate
from ._config_isolation import isolated_config

isolate()

from src import compact_rescue, config, failover, model_metadata, model_pricing  # noqa: E402
from src.channel.base import UpstreamRequest, build_dispatch_metadata  # noqa: E402
from src.openai.transform.guard import GuardError  # noqa: E402
from src.protocols.runtime import AttemptResult  # noqa: E402


def _tariff(input_per_million: float) -> dict:
    return asdict(model_pricing.PricingEntry(
        input_per_token=input_per_million / 1_000_000,
        output_per_token=2.0 / 1_000_000,
        cache_write_per_token=input_per_million / 1_000_000,
        cache_read_per_token=input_per_million / 1_000_000,
    ))


def _snapshot(
    revision: str,
    *,
    context: int,
    max_output: int,
    trigger: int,
    input_price: float,
) -> dict:
    return {
        "catalogRevision": revision,
        "catalogSource": "budget-fixture",
        "metadata": {
            "contextWindow": context,
            "maxInputTokens": context,
            "maxOutputTokens": max_output,
            "compactTriggerTokens": trigger,
            "cost": {"input": input_price, "output": 2.0},
        },
        "tariff": _tariff(input_price),
    }


def _entry(target: str, outbound: str, snapshot: dict) -> dict:
    return {
        "target": target,
        "outboundModel": outbound,
        "source": "auto",
        "autoSnapshot": copy.deepcopy(snapshot),
    }


def _install_budget_config(*, b_max_output: int = 20_000) -> None:
    one_million_a = _snapshot(
        "A", context=1_000_000, max_output=50_000,
        trigger=800_000, input_price=1.0,
    )
    one_million_b = _snapshot(
        "B", context=1_000_000, max_output=b_max_output,
        trigger=800_000, input_price=9.0,
    )
    config.update(lambda cfg: cfg.update({
        "modelBindings": {
            "defaults": {},
            "scoped": {
                "api:A": {"demo": _entry("demo/a", "real-A", one_million_a)},
                "api:B": {"demo": _entry("demo/b", "real-B", one_million_b)},
            },
        },
        "modelMetadataOverrides": {"defaults": {}, "scoped": {}},
    }))
    assert model_metadata.patch_override_fields(
        "demo", scope_key="api:B", outbound_model="real-B",
        set_fields={
            "contextWindow": 300_000,
            "maxInputTokens": 300_000,
            "compactTriggerTokens": 250_000,
        },
    )


class _WireChannel:
    type = "api"
    protocol = "anthropic"
    cc_mimicry = False
    upstream_stream_only = False

    def __init__(self, key: str):
        self.key = key

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str, *, ingress_protocol: str = "anthropic",
    ) -> UpstreamRequest:
        payload = {
            key: copy.deepcopy(value)
            for key, value in requested_body.items()
            if not key.startswith("_parrot_") and key != "_client_visible_model"
        }
        payload["model"] = resolved_model
        return UpstreamRequest(
            url="https://fixture.invalid/v1/messages",
            headers={"content-type": "application/json"},
            body=json.dumps(payload).encode("utf-8"),
            dispatch_metadata=build_dispatch_metadata(payload, self.protocol),
        )


async def _attempt(channel: _WireChannel, outbound: str, body: dict) -> AttemptResult:
    now = time.time()
    return await failover._try_channel(
        channel, outbound, body,
        False, now + 60, now, None, body.get("messages") or [],
        None, "127.0.0.1", "budget-fixture", 0, 0,
        ingress_protocol="anthropic",
        start_monotonic=time.monotonic(),
        attempt_start_monotonic=time.monotonic(),
    )


@pytest.mark.usefixtures("isolated_config")
def test_effective_budget_separates_trigger_hard_fit_unknown_and_restore():
    _install_budget_config()
    shape = {"model": "demo", "max_tokens": 20_000, "messages": []}
    a = model_metadata.effective_request_budget(
        "demo", scope_key="api:A", outbound_model="real-A", request_shape=shape,
    )
    b = model_metadata.effective_request_budget(
        "demo", scope_key="api:B", outbound_model="real-B", request_shape=shape,
    )
    assert a.effective_input_budget == 1_000_000
    assert b.effective_input_budget == 300_000
    assert a.can_fit(310_000)
    assert not b.can_fit(310_000)
    assert model_metadata.should_compact(
        "demo", 250_000, scope_key="api:B", outbound_model="real-B",
        request_shape=shape,
    )
    assert b.compact_trigger_tokens == 250_000
    assert b.effective_input_budget > b.compact_trigger_tokens

    unknown = model_metadata.effective_request_budget(
        "unknown-no-binding", request_shape=shape,
    )
    assert unknown.budget_known is False
    assert unknown.effective_input_budget is None
    assert unknown.can_fit(10_000_000)

    assert model_metadata.delete_override_layer("demo", scope_key="api:B")
    restored = model_metadata.effective_request_budget(
        "demo", scope_key="api:B", outbound_model="real-B", request_shape=shape,
    )
    assert restored.effective_input_budget == 1_000_000
    assert restored.can_fit(310_000)


@pytest.mark.usefixtures("isolated_config")
def test_responses_ws_nested_final_frame_uses_candidate_budget(monkeypatch):
    _install_budget_config()
    channel = _WireChannel("api:B")
    frame = {
        "type": "response.create",
        "response": {
            "model": "real-B",
            "max_output_tokens": 20_000,
            "input": "controlled websocket input",
        },
    }
    metadata = build_dispatch_metadata(frame, "openai-responses")
    counted = {"tokens": 300_000}
    monkeypatch.setattr(
        failover.token_counter,
        "count_request_tokens",
        lambda payload, model=None: counted["tokens"],
    )
    budget = failover._validate_wire_payload_budget(
        channel,
        "real-B",
        {"model": "demo", "_client_visible_model": "demo"},
        json.dumps(frame),
        metadata,
    )
    assert budget is not None and budget.effective_input_budget == 300_000
    # Input-side enforcement is gone: even a prompt above the local estimate
    # of the input budget passes the pre-transport check.
    counted["tokens"] = 300_001
    budget = failover._validate_wire_payload_budget(
        channel,
        "real-B",
        {"model": "demo", "_client_visible_model": "demo"},
        json.dumps(frame),
        metadata,
    )
    assert budget is not None
    # The output safety net still rejects a final frame that overflows the
    # route's maxOutputTokens and was not clamped upstream of this check.
    oversized = copy.deepcopy(frame)
    oversized["response"]["max_output_tokens"] = 20_001
    with pytest.raises(GuardError) as overflow:
        failover._validate_wire_payload_budget(
            channel,
            "real-B",
            {"model": "demo", "_client_visible_model": "demo"},
            json.dumps(oversized),
            metadata,
        )
    assert getattr(overflow.value, "scope", None) == "candidate"
    assert getattr(overflow.value, "status", None) == 400


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_config")
async def test_candidate_final_wire_budget_recomputed_and_price_frozen(monkeypatch):
    _install_budget_config()
    sent: list[tuple[str, dict, model_pricing.PricingBinding]] = []

    counted = {"tokens": 310_000}
    monkeypatch.setattr(
        failover.token_counter,
        "count_request_tokens",
        lambda payload, model=None: counted["tokens"],
    )
    monkeypatch.setattr(
        failover.log_db,
        "update_pending_fast_mode_from_upstream",
        lambda *args, **kwargs: None,
    )

    async def fake_open_response_with_proxy_chain(**kwargs):
        channel = kwargs["channel"]
        req = kwargs["upstream_req"]
        payload = json.loads(req.body)
        metadata = req.dispatch_metadata
        binding = model_pricing.build_pricing_binding(
            channel_key=channel.key,
            channel_type=channel.type,
            upstream_protocol=channel.protocol,
            outbound_model_id=metadata.outbound_model_id,
            client_visible_model="demo",
        )
        sent.append((channel.key, payload, binding))
        return SimpleNamespace(error=AttemptResult(
            outcome="transport_error", error_detail="fixture stop before network",
        ))

    monkeypatch.setattr(
        failover, "open_response_with_proxy_chain", fake_open_response_with_proxy_chain,
    )
    body = {
        "model": "demo", "_client_visible_model": "demo", "max_tokens": 20_000,
        "messages": [{"role": "user", "content": "controlled 310k prompt"}],
    }

    a_result = await _attempt(_WireChannel("api:A"), "real-A", body)
    assert a_result.outcome == "transport_error"
    assert [item[0] for item in sent] == ["api:A"]

    b_result = await _attempt(_WireChannel("api:B"), "real-B", body)
    # Input budgets are no longer locally enforced (upstream is the authority),
    # so B is dispatched even with the 310k local estimate.
    assert b_result.outcome == "transport_error"
    assert "limit=300000" not in str(b_result.error_detail)
    assert [item[0] for item in sent] == ["api:A", "api:B"]

    # A per-source max-output cap is applied before the transport: oversized
    # output is clamped down to the route's maxOutputTokens before dispatch.
    too_much_output = {**body, "max_tokens": 20_001}
    output_result = await _attempt(_WireChannel("api:B"), "real-B", too_much_output)
    assert output_result.outcome == "transport_error"
    assert sent[2][1]["max_tokens"] == 20_000
    assert [item[0] for item in sent] == ["api:A", "api:B", "api:B"]

    # Once B's final payload fits, dispatch uses B's own outbound identity,
    # max-output cap and tariff rather than retaining A's route facts.
    counted["tokens"] = 200_000
    b_fit = await _attempt(_WireChannel("api:B"), "real-B", body)
    assert b_fit.outcome == "transport_error"
    assert [item[0] for item in sent] == ["api:A", "api:B", "api:B", "api:B"]
    assert sent[3][1]["model"] == "real-B"
    assert sent[3][1]["max_tokens"] == 20_000

    # An oversized output request that fits on input is clamped, not rejected.
    clamped = await _attempt(_WireChannel("api:B"), "real-B", too_much_output)
    assert clamped.outcome == "transport_error"
    assert [item[0] for item in sent] == ["api:A", "api:B", "api:B", "api:B", "api:B"]
    assert sent[4][1]["max_tokens"] == 20_000
    assert sent[0][2].tariff is not None
    assert sent[0][2].tariff.input_per_token == 1.0 / 1_000_000

    # The B dispatch-time binding remains immutable after a later source price
    # update, while a new dispatch observes the new effective tariff.
    frozen = sent[1][2]
    assert frozen.model_id == "b"
    assert frozen.tariff is not None
    assert frozen.tariff.input_per_token == 9.0 / 1_000_000
    assert model_metadata.patch_override_fields(
        "demo", scope_key="api:B", outbound_model="real-B",
        set_fields={"cost.input": 7.0},
    )
    refreshed = model_pricing.build_pricing_binding(
        channel_key="api:B", channel_type="api", upstream_protocol="anthropic",
        outbound_model_id="real-B", client_visible_model="demo",
    )
    assert refreshed.tariff is not None
    assert refreshed.tariff.input_per_token == 7.0 / 1_000_000
    assert frozen.tariff.input_per_token == 9.0 / 1_000_000


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_config")
async def test_run_failover_large_a_then_small_b_never_dispatches_b(monkeypatch):
    _install_budget_config()
    sent: list[str] = []

    async def acquire(_key):
        return True

    monkeypatch.setattr(failover.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(failover.concurrency, "release", lambda *_args: None)
    monkeypatch.setattr(failover, "_pick_non_direct_proxy_name", lambda *_args: None)
    monkeypatch.setattr(
        failover, "_should_use_responses_upstream_ws", lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        failover.local_web_tools, "request_declares_supported_tools", lambda *_args: False,
    )
    monkeypatch.setattr(
        failover.local_web_tools, "openai_responses_local_web_active", lambda *_args: False,
    )
    monkeypatch.setattr(
        failover.log_db, "record_retry_attempt", lambda *args, **kwargs: int(args[1]),
    )
    monkeypatch.setattr(failover.log_db, "update_retry_attempt", lambda *args, **kwargs: None)
    monkeypatch.setattr(failover.log_db, "update_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        failover.log_db, "update_pending_fast_mode_from_upstream",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(failover.log_db, "finish_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        failover.finalize_policy, "apply_error_health_effects",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        failover.quota_errors, "zhipu_1310_reset_ms", lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        failover.token_counter, "count_request_tokens",
        lambda payload, model=None: 310_000,
    )

    async def fake_open_response_with_proxy_chain(**kwargs):
        sent.append(kwargs["channel"].key)
        return SimpleNamespace(error=AttemptResult(
            outcome="transport_error", error_detail="force A to fail before response",
        ))

    monkeypatch.setattr(
        failover, "open_response_with_proxy_chain", fake_open_response_with_proxy_chain,
    )
    route = SimpleNamespace(
        candidates=[
            (_WireChannel("api:A"), "real-A"),
            (_WireChannel("api:B"), "real-B"),
        ],
        saturated=[],
        affinity_hit=False,
        fp_query=None,
        client_key=None,
        bound_channel_key=None,
        encrypted_content_count=0,
    )
    body = {
        "model": "demo", "_client_visible_model": "demo", "max_tokens": 20_000,
        "messages": [{"role": "user", "content": "controlled 310k prompt"}],
    }
    response = await failover.run_failover(
        route, body, "budget-failover", None, "127.0.0.1", False, time.time(),
        ingress_protocol="anthropic", start_monotonic=time.monotonic(),
    )
    # Input budgets are no longer locally enforced, so both candidates are
    # dispatched in order and the response reflects the upstream outcomes.
    assert sent == ["api:A", "api:B"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_config")
async def test_compact_direct_segment_reduce_each_fit_final_candidate(monkeypatch):
    _install_budget_config(b_max_output=10_000)
    sent: list[dict] = []
    token_counts = {
        "direct": 260_000,
        "segment": 265_000,
        "reduce": 280_000,
        "reduce-overflow": 280_001,
    }

    monkeypatch.setattr(
        failover.token_counter,
        "count_request_tokens",
        lambda payload, model=None: token_counts[payload["messages"][0]["content"]],
    )
    monkeypatch.setattr(
        failover.log_db,
        "update_pending_fast_mode_from_upstream",
        lambda *args, **kwargs: None,
    )

    async def fake_open_response_with_proxy_chain(**kwargs):
        payload = json.loads(kwargs["upstream_req"].body)
        sent.append(payload)
        return SimpleNamespace(error=AttemptResult(
            outcome="transport_error", error_detail="fixture stop before network",
        ))

    monkeypatch.setattr(
        failover, "open_response_with_proxy_chain", fake_open_response_with_proxy_chain,
    )
    channel = _WireChannel("api:B")

    for phase in ("direct", "segment", "reduce"):
        body = {
            "model": "demo",
            "_client_visible_model": "demo",
            compact_rescue.INTERNAL_FLAG: True,
            "max_tokens": 20_000,
            "messages": [{"role": "user", "content": phase}],
        }
        result = await _attempt(channel, "real-B", body)
        assert result.outcome == "transport_error"

    assert [payload["messages"][0]["content"] for payload in sent] == [
        "direct", "segment", "reduce",
    ]
    assert all(payload["max_tokens"] == 10_000 for payload in sent)
    assert all(
        token_counts[payload["messages"][0]["content"]]
        + model_metadata.compact_buffer_tokens()
        <= 300_000
        for payload in sent
    )

    overflow = {
        "model": "demo",
        "_client_visible_model": "demo",
        compact_rescue.INTERNAL_FLAG: True,
        "max_tokens": 20_000,
        "messages": [{"role": "user", "content": "reduce-overflow"}],
    }
    result = await _attempt(channel, "real-B", overflow)
    # Input-side local estimates no longer hard-reject Parrot-owned compact
    # traffic: an oversized compact attempt is dispatched (still output-
    # clamped) and the established rescue ladder handles upstream refusal.
    assert result.outcome == "transport_error"
    assert len(sent) == 4
    assert sent[3]["max_tokens"] == 10_000
