from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from src.tests import _isolation

_isolation.isolate()

from src import config, oauth_manager, state_db
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.openai import codex_constants
from src.openai.transform import codex_oauth_transform


_PROFILE_CONFIG = {
    "codexCliVersion": "0.153.4",
    "codexProtocolProfile": "rust-v0.153.4",
}
_PROFILE_PATH = (
    Path(__file__).parents[1]
    / "openai"
    / "codex_profiles"
    / "rust-v0.153.4.json"
)
_BASE_PATH = (
    Path(__file__).parents[1]
    / "openai"
    / "codex_profiles"
    / "rust-v0.153.4"
    / "gpt-6-astra-base-instructions.txt"
)


@pytest.fixture(autouse=True)
def _restore_config():
    original = copy.deepcopy(config.get())
    try:
        yield
    finally:
        config.update(lambda current: (current.clear(), current.update(original)))


def _account(*, model: str, records: list[dict] | None = None) -> dict:
    account = {
        "email": "profile-test@openai.test",
        "provider": "openai",
        "access_token": "test-access-token",
        "refresh_token": "test-refresh-token",
        "chatgpt_account_id": "acct-profile-test",
        "plan_type": "plus",
        "models": [model],
    }
    if records is not None:
        account["account_model_catalog"] = {"schema": 1, "models": records}
    return account


def _channel(monkeypatch, *, model: str, records: list[dict] | None = None):
    state_db.init()
    account = _account(model=model, records=records)
    monkeypatch.setattr(oauth_manager, "list_accounts", lambda: [account])

    async def _token(_account_key):
        return "test-access-token"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", _token)
    monkeypatch.setattr(oauth_manager, "get_account", lambda _account_key: account)
    return OpenAIOAuthChannel(account)


def _payload(channel: OpenAIOAuthChannel, body: dict, model: str) -> tuple[dict, dict]:
    request = asyncio.run(channel.build_upstream_request(
        body,
        model,
        ingress_protocol="responses",
    ))
    return json.loads(request.body), {str(k).lower(): str(v) for k, v in request.headers.items()}


def test_profile_artifact_checksum_identity_and_astra_defaults():
    manifest = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
    record = manifest["models"]["gpt-6-astra"]
    raw_base = _BASE_PATH.read_bytes()
    assert hashlib.sha256(raw_base).hexdigest() == record["baseInstructionsSha256"]
    assert record["baseInstructionsSha256"] == (
        "152dfaeeb552876190962be1c12c93d426840ff12691f648261554a7675a6698"
    )

    profile = codex_constants.codex_protocol_profile(_PROFILE_CONFIG)
    astra = profile.model_policy("gpt-6-astra")
    assert astra is not None
    assert astra.base_instructions == raw_base.decode("utf-8")
    assert astra.use_responses_lite is True
    assert astra.default_reasoning_effort == "low"
    assert astra.default_verbosity == "low"
    assert astra.multi_agent_reasoning_effort == "xhigh"
    assert profile.user_agent == manifest["identity"]["userAgent"]
    assert profile.responses_websocket_beta == manifest["protocol"]["responsesWebsocketBeta"]


def test_codex_config_missing_empty_invalid_and_profile_mismatch_fail_closed():
    invalid = [
        {},
        {"codexCliVersion": "", "codexProtocolProfile": "rust-v0.153.4"},
        {"codexCliVersion": "not-semver", "codexProtocolProfile": "rust-v0.153.4"},
        {"codexCliVersion": "0.153.4", "codexProtocolProfile": ""},
        {"codexCliVersion": "0.153.4", "codexProtocolProfile": "../escape"},
    ]
    for candidate in invalid:
        with pytest.raises(codex_constants.CodexConfigurationError):
            codex_constants.codex_cli_version(candidate)

    with pytest.raises(
        codex_constants.CodexConfigurationError,
        match="requires client version '0.153.4'.*'0.144.0'",
    ):
        codex_constants.codex_cli_version({
            "codexCliVersion": "0.144.0",
            "codexProtocolProfile": "rust-v0.153.4",
        })


def test_raw_new_config_key_wins_over_legacy_even_when_values_look_default():
    raw = {
        "openaiOAuth": {
            **_PROFILE_CONFIG,
            "forceCodexCLI": True,
        },
        "oauth": {
            "providers": {
                "openai": {
                    "codexCliVersion": "9.9.9",
                    "codexProtocolProfile": "legacy-profile",
                    "forceCodexCLI": False,
                }
            }
        },
    }
    merged = config._deep_merge_defaults(config.DEFAULT_CONFIG, raw)
    config._normalize_openai_oauth_config(merged, raw)
    assert merged["openaiOAuth"]["codexCliVersion"] == "0.153.4"
    assert merged["openaiOAuth"]["codexProtocolProfile"] == "rust-v0.153.4"
    assert merged["openaiOAuth"]["codexProfileAutoUpdate"] is True
    assert "forceCodexCLI" not in merged["openaiOAuth"]
    assert "forceCodexCLI" not in merged["oauth"]["providers"]["openai"]


def test_catalog_lite_overrides_profile_profile_fills_absent_and_unknown_rejects():
    account_false = codex_constants.resolve_codex_model_policy(
        "gpt-6-astra",
        {"useResponsesLite": False},
        _PROFILE_CONFIG,
    )
    assert account_false.use_responses_lite is False
    assert account_false.base_instructions == _BASE_PATH.read_text(encoding="utf-8")

    profile_lite = codex_constants.resolve_codex_model_policy(
        "gpt-6-astra", {}, _PROFILE_CONFIG
    )
    assert profile_lite.use_responses_lite is True

    catalog_only = codex_constants.resolve_codex_model_policy(
        "catalog-only-model",
        {"useResponsesLite": True},
        _PROFILE_CONFIG,
    )
    assert catalog_only.use_responses_lite is True
    assert catalog_only.from_profile is False

    with pytest.raises(
        codex_constants.CodexConfigurationError,
        match="No explicit Responses Lite policy",
    ):
        codex_constants.resolve_codex_model_policy(
            "unknown-codex-model", {}, _PROFILE_CONFIG
        )


def test_astra_base_defaults_lite_ids_metadata_and_wire_omission(monkeypatch):
    channel = _channel(monkeypatch, model="gpt-6-astra")
    body = {
        "model": "gpt-6-astra",
        "input": "hello",
        "prompt_cache_key": "thread-astra-1",
        "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
    }
    payload, headers = _payload(channel, copy.deepcopy(body), "gpt-6-astra")
    assert headers["version"] == "0.153.4"
    assert headers["x-openai-internal-codex-responses-lite"] == "true"
    assert "instructions" not in payload
    assert "tools" not in payload
    assert payload["reasoning"] == {"effort": "low", "context": "all_turns"}
    assert payload["text"]["verbosity"] == "low"

    # The raw downstream anchor is lookup-only; Lite synthetic IDs use the
    # account-scoped durable UUIDv7 prompt-cache/session projection.
    assert payload["prompt_cache_key"] != "thread-astra-1"
    assert uuid.UUID(payload["prompt_cache_key"]).version == 7
    prefix_namespace = uuid.uuid5(uuid.NAMESPACE_OID, payload["prompt_cache_key"])
    expected_tools_json = json.dumps(
        body["tools"], ensure_ascii=False, separators=(",", ":")
    )
    assert payload["input"][0]["id"] == f"at_{uuid.uuid5(prefix_namespace, expected_tools_json)}"
    assert payload["input"][0]["type"] == "additional_tools"
    base_item = payload["input"][1]
    base_text = _BASE_PATH.read_text(encoding="utf-8")
    assert base_item["id"] == f"msg_{uuid.uuid5(prefix_namespace, base_text)}"
    assert base_item["content"] == [{"type": "input_text", "text": base_text}]
    assert base_item["internal_chat_message_metadata_passthrough"] == {
        "content_item_kinds": ["model.base_instructions"],
    }

    repeated, _ = _payload(channel, copy.deepcopy(body), "gpt-6-astra")
    assert [item["id"] for item in repeated["input"][:2]] == [
        item["id"] for item in payload["input"][:2]
    ]
    changed = copy.deepcopy(body)
    changed["prompt_cache_key"] = "thread-astra-2"
    changed_payload, _ = _payload(channel, changed, "gpt-6-astra")
    assert [item["id"] for item in changed_payload["input"][:2]] != [
        item["id"] for item in payload["input"][:2]
    ]


def test_downstream_instructions_win_over_astra_profile(monkeypatch):
    channel = _channel(monkeypatch, model="gpt-6-astra")
    payload, _ = _payload(channel, {
        "model": "gpt-6-astra",
        "instructions": "authoritative downstream instructions",
        "input": "hello",
        "prompt_cache_key": "downstream-wins",
    }, "gpt-6-astra")
    instruction_items = [
        item for item in payload["input"]
        if isinstance(item, dict)
        and item.get("internal_chat_message_metadata_passthrough") == {
            "content_item_kinds": ["model.base_instructions"],
        }
    ]
    assert len(instruction_items) == 1
    assert instruction_items[0]["content"][0]["text"] == (
        "authoritative downstream instructions"
    )
    assert _BASE_PATH.read_text(encoding="utf-8") not in json.dumps(payload)


def test_already_lite_prefix_is_authoritative_and_idempotent():
    official = {
        "model": "gpt-6-astra",
        "input": [{
            "id": "at_authoritative",
            "type": "additional_tools",
            "role": "developer",
            "tools": [],
        }, {
            "id": "msg_authoritative",
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "official base"}],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["model.base_instructions"],
            },
        }, {
            "type": "message",
            "role": "user",
            "content": "continue",
        }],
        "tools": [],
        "instructions": "",
        "reasoning": {"effort": "medium", "context": "all_turns"},
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
    }
    kwargs = {
        "resolved_model": "gpt-6-astra",
        "use_responses_lite": True,
        "base_instructions": _BASE_PATH.read_text(encoding="utf-8"),
    }
    once = codex_oauth_transform.apply_codex_oauth_transform(
        copy.deepcopy(official), **kwargs
    )
    assert "instructions" not in once
    assert "tools" not in once
    assert [item.get("id") for item in once["input"][:2]] == [
        "at_authoritative",
        "msg_authoritative",
    ]
    twice = codex_oauth_transform.apply_codex_oauth_transform(once, **kwargs)
    assert twice == once


def test_catalog_defaults_override_profile_and_explicit_request_values_win(monkeypatch):
    record = {
        "id": "gpt-6-astra",
        "useResponsesLite": False,
        "reasoningEfforts": ["low", "medium", "high", "xhigh", "ultra"],
        "defaultReasoningEffort": "high",
        "defaultVerbosity": "medium",
        "multiAgentReasoningEffort": "xhigh",
    }
    channel = _channel(monkeypatch, model="gpt-6-astra", records=[record])
    defaulted, _ = _payload(channel, {
        "model": "gpt-6-astra",
        "input": "hello",
    }, "gpt-6-astra")
    assert defaulted["reasoning"]["effort"] == "high"
    assert defaulted["text"]["verbosity"] == "medium"
    assert defaulted["instructions"] == _BASE_PATH.read_text(encoding="utf-8")

    explicit, _ = _payload(channel, {
        "model": "gpt-6-astra",
        "input": "hello",
        "reasoning": {"effort": "medium"},
        "text": {"verbosity": "high"},
    }, "gpt-6-astra")
    assert explicit["reasoning"]["effort"] == "medium"
    assert explicit["text"]["verbosity"] == "high"


def test_ultra_mapping_requires_model_scoped_catalog_policy(monkeypatch):
    supported_record = {
        "id": "catalog-ultra",
        "useResponsesLite": False,
        "reasoningEfforts": ["low", "xhigh", "ultra"],
        "multiAgentReasoningEffort": "xhigh",
    }
    supported = _channel(
        monkeypatch, model="catalog-ultra", records=[supported_record]
    )
    mapped, _ = _payload(supported, {
        "model": "catalog-ultra",
        "input": "hello",
        "reasoning": {"effort": "ultra"},
    }, "catalog-ultra")
    assert mapped["reasoning"]["effort"] == "xhigh"

    missing_record = {
        "id": "catalog-ultra",
        "useResponsesLite": False,
        "reasoningEfforts": ["low", "xhigh", "ultra"],
    }
    rejected = _channel(monkeypatch, model="catalog-ultra", records=[missing_record])
    with pytest.raises(ValueError, match="requires explicit model-scoped"):
        _payload(rejected, {
            "model": "catalog-ultra",
            "input": "hello",
            "reasoning": {"effort": "ultra"},
        }, "catalog-ultra")
