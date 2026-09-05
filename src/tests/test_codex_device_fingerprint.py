from __future__ import annotations

import json

import pytest

from src.openai.codex_device_fingerprint import (
    apply_device_fingerprint,
    canonical_uuid4,
)

DEVICE_A = "123e4567-e89b-42d3-a456-426614174000"


def test_uuid4_validation_is_canonical_and_fail_closed():
    assert canonical_uuid4(None) == ""
    assert canonical_uuid4("") == ""
    assert canonical_uuid4(DEVICE_A) == DEVICE_A
    for bad in (
        "123E4567-E89B-42D3-A456-426614174000",
        "not-a-uuid",
        123,
        "123e4567-e89b-52d3-a456-426614174000",
    ):
        with pytest.raises(ValueError):
            canonical_uuid4(bad)


def test_ordinary_responses_uses_metadata_not_direct_installation_header():
    headers = {
        "X-Codex-Installation-Id": "downstream",
        "x-codex-turn-metadata": json.dumps(
            {"installation_id": "old", "turn_id": "turn"}
        ),
    }
    body = {
        "client_metadata": {
            "x-codex-installation-id": "old",
            "x-codex-turn-metadata": json.dumps(
                {"installation_id": "old", "window_id": "window"}
            ),
        }
    }
    out_headers, out_body = apply_device_fingerprint(
        headers, body, DEVICE_A, create_client_metadata=True
    )
    assert not any(
        str(key).lower() == "x-codex-installation-id" for key in out_headers
    )
    assert out_body["client_metadata"]["x-codex-installation-id"] == DEVICE_A
    assert json.loads(out_headers["x-codex-turn-metadata"])["installation_id"] == DEVICE_A
    assert json.loads(
        out_body["client_metadata"]["x-codex-turn-metadata"]
    )["installation_id"] == DEVICE_A


def test_compact_profile_can_opt_into_direct_installation_header():
    headers, body = apply_device_fingerprint(
        {}, {"input": []}, DEVICE_A,
        create_client_metadata=True,
        direct_installation_header=True,
    )
    assert headers["x-codex-installation-id"] == DEVICE_A
    assert body["client_metadata"]["x-codex-installation-id"] == DEVICE_A


def test_invalid_client_metadata_fails_closed():
    with pytest.raises(ValueError, match="client_metadata must be an object"):
        apply_device_fingerprint(
            {}, {"client_metadata": "bad"}, DEVICE_A,
            create_client_metadata=True,
        )
