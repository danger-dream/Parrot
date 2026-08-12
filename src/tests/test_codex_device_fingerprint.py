from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.openai.codex_device_fingerprint import (
    apply_device_fingerprint,
    canonical_uuid4,
)
from src.openai.codex_identity_confuse import ConfuseState, expose_response_payload
from src.openai.responses_ws_runtime import prepare_oauth_responses_ws_request_parts


DEVICE_A = "123e4567-e89b-42d3-a456-426614174000"
DEVICE_B = "123e4567-e89b-42d3-b456-426614174001"


def test_uuid4_validation_is_canonical_and_fail_closed():
    assert canonical_uuid4(None) == ""
    assert canonical_uuid4("") == ""
    assert canonical_uuid4(DEVICE_A) == DEVICE_A
    for bad in ("123E4567-E89B-42D3-A456-426614174000", "not-a-uuid", 123,
                "123e4567-e89b-52d3-a456-426614174000"):
        with pytest.raises(ValueError):
            canonical_uuid4(bad)


def test_http_rewrites_only_installation_carriers_and_preserves_sentinels():
    headers = {
        "X-Codex-Installation-Id": "downstream",
        "x-codex-turn-metadata": json.dumps({"installation_id": "old", "turn_id": "DEEP"}),
        "session-id": "SESSION",
        "thread-id": "THREAD",
        "x-client-request-id": "REQUEST",
    }
    body = {
        "prompt_cache_key": "CACHE",
        "reasoning": {"encrypted_content": "REPLAY"},
        "client_metadata": {
            "x-codex-installation-id": "old",
            "x-codex-turn-metadata": json.dumps({"installation_id": "old", "window_id": "WINDOW"}),
            "other": {"deep": "SENTINEL"},
        },
    }
    out_h, out_b = apply_device_fingerprint(headers, body, DEVICE_A, create_client_metadata=False)
    assert out_h["x-codex-installation-id"] == DEVICE_A
    assert not any(k == "X-Codex-Installation-Id" for k in out_h)
    assert json.loads(out_h["x-codex-turn-metadata"]) == {
        "installation_id": DEVICE_A, "turn_id": "DEEP",
    }
    assert json.loads(out_b["client_metadata"]["x-codex-turn-metadata"]) == {
        "installation_id": DEVICE_A, "window_id": "WINDOW",
    }
    assert out_b["client_metadata"]["other"] == {"deep": "SENTINEL"}
    assert (out_h["session-id"], out_h["thread-id"], out_h["x-client-request-id"]) == (
        "SESSION", "THREAD", "REQUEST",
    )
    assert out_b["prompt_cache_key"] == "CACHE"
    assert out_b["reasoning"] == {"encrypted_content": "REPLAY"}


def test_missing_metadata_http_not_created_ws_created_and_invalid_fails():
    _, http = apply_device_fingerprint({}, {"input": "x"}, DEVICE_A, create_client_metadata=False)
    assert "client_metadata" not in http
    _, ws = apply_device_fingerprint({}, {"input": "x"}, DEVICE_A, create_client_metadata=True)
    assert ws["client_metadata"] == {"x-codex-installation-id": DEVICE_A}
    with pytest.raises(ValueError, match="client_metadata must be an object"):
        apply_device_fingerprint({}, {"client_metadata": "bad"}, DEVICE_A, create_client_metadata=True)


def test_invalid_or_nonobject_turn_metadata_remains_unchanged():
    for raw in ("not-json", "[]", "null"):
        headers, body = apply_device_fingerprint(
            {"x-codex-turn-metadata": raw},
            {"client_metadata": {"x-codex-turn-metadata": raw}},
            DEVICE_A, create_client_metadata=False,
        )
        assert headers["x-codex-turn-metadata"] == raw
        assert body["client_metadata"]["x-codex-turn-metadata"] == raw


def test_default_off_is_identity_preserving():
    headers = {"x-codex-installation-id": "original"}
    body = {"client_metadata": "wrong-type", "deep": {"sentinel": [1, 2]}}
    out_h, out_b = apply_device_fingerprint(headers, body, "", create_client_metadata=True)
    assert out_h is headers
    assert out_b is body


def test_http_to_ws_finalizes_after_confuse_and_exposes_only_real_original():
    channel = SimpleNamespace(codex_device_installation_id=DEVICE_A)
    request = SimpleNamespace(
        url="https://chatgpt.com/backend-api/codex/responses",
        headers={"session-id": "SID", "x-codex-installation-id": "pre"},
        body=json.dumps({
            "model": "gpt-test", "input": [],
            "client_metadata": {"x-codex-installation-id": "downstream"},
        }).encode(),
    )
    _, headers, frame, state = prepare_oauth_responses_ws_request_parts(
        request,
        {"_api_key_name": "key", "prompt_cache_key": "raw"},
        "gpt-test", channel=channel,
    )
    frame_obj = json.loads(frame)
    assert headers["x-codex-installation-id"] == DEVICE_A
    assert frame_obj["client_metadata"]["x-codex-installation-id"] == DEVICE_A
    assert state.original_installation_id == "downstream"
    assert state.confused_installation_id == DEVICE_A
    assert expose_response_payload(DEVICE_A.encode(), state) == b"downstream"

    no_original = ConfuseState(enabled=True, auth_id="key")
    no_original.override_installation_for_upstream(DEVICE_A)
    assert expose_response_payload(DEVICE_A.encode(), no_original) == DEVICE_A.encode()


def test_different_account_channels_keep_distinct_configured_devices():
    for device in (DEVICE_A, DEVICE_B):
        channel = SimpleNamespace(codex_device_installation_id=device)
        req = SimpleNamespace(
            url="https://chatgpt.com/backend-api/codex/responses",
            headers={}, body=json.dumps({"model": "m", "input": []}).encode(),
        )
        _, headers, frame, _ = prepare_oauth_responses_ws_request_parts(
            req, {}, "m", channel=channel,
        )
        assert headers["x-codex-installation-id"] == device
        assert json.loads(frame)["client_metadata"]["x-codex-installation-id"] == device
