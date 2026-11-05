import json

import pytest

from src import failover, model_metadata
from src.channel.base import build_dispatch_metadata


@pytest.mark.parametrize("payload", [
    {"model": "m", "max_tokens": 900, "thinking": {"type": "enabled", "budget_tokens": 512}, "messages": []},
    {"model": "m", "max_output_tokens": 64000, "input": [{"type": "message", "content": "x" * 1000}]},
    {"model": "m", "max_completion_tokens": "128", "messages": []},
    {"type": "response.create", "response": {"model": "m", "max_output_tokens": 2048, "thinking": {"type": "enabled"}}},
    {"type": "response.create", "max_tokens": 10, "response": {"model": "m"}},
    {"model": "m", "messages": []},
])
def test_budget_shape_matches_parsed_wire_payload(payload):
    parsed = json.loads(json.dumps(payload))
    shape = build_dispatch_metadata(payload, "openai-responses").budget_shape
    assert failover._positive_output_limit(shape) == failover._positive_output_limit(parsed)
    assert model_metadata._requested_output_tokens(shape) == model_metadata._requested_output_tokens(parsed)
    key, _ = failover._positive_output_limit(parsed)
    for view in (shape, parsed):
        nested = view.get("response")
        final = nested if isinstance(nested, dict) and key in nested else view
        assert final.get("thinking") == (
            (parsed["response"] if isinstance(parsed.get("response"), dict) and key in parsed["response"] else parsed)
            .get("thinking")
        )


def test_budget_shape_is_independent_of_later_payload_mutation():
    payload = {"model": "m", "max_tokens": 100, "thinking": {"type": "enabled"}}
    shape = build_dispatch_metadata(payload, "anthropic").budget_shape
    payload["thinking"]["type"] = "disabled"
    assert shape["thinking"] == {"type": "enabled"}


def test_wire_payload_overrides_budget_source():
    payload = {"model": "m", "max_tokens": 100}
    wire = {"model": "m", "max_tokens": 7}
    metadata = build_dispatch_metadata(payload, "openai-chat", wire_payload=wire)
    assert metadata.budget_shape == {"max_tokens": 7}
    assert metadata.outbound_model_id == "m"
