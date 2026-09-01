from __future__ import annotations

import json
import uuid

import pytest

from src import config, oauth_manager
from src.channel.api_channel import ApiChannel
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.openai.channel.api_channel import OpenAIApiChannel
from src.openai.responses_ws_runtime import prepare_oauth_responses_ws_request_parts


DEVICE_A = "123e4567-e89b-42d3-a456-426614174000"
DEVICE_B = "123e4567-e89b-42d3-b456-426614174001"


def _entry(email: str, workspace: str, **extra):
    return {
        "email": email,
        "provider": "openai",
        "access_token": "access",
        "refresh_token": "refresh",
        "workspace_id": workspace,
        "chatgpt_account_id": workspace,
        **extra,
    }


def _assert_uuid4(value: str) -> None:
    assert str(uuid.UUID(value)) == value
    assert uuid.UUID(value).version == 4


def test_import_defaults_on_preserves_identity_and_explicit_off_on_is_reversible():
    oauth_manager.add_account(_entry("device-preserve@example.test", "ws-preserve"))
    account = oauth_manager.get_account("openai:device-preserve@example.test:ws-preserve")
    first_id = account["codexDeviceInstallationId"]
    _assert_uuid4(first_id)

    oauth_manager.add_account(_entry("device-preserve@example.test", "ws-preserve"))
    account = oauth_manager.get_account("openai:device-preserve@example.test:ws-preserve")
    assert account["codexDeviceInstallationId"] == first_id

    oauth_manager.add_account(_entry(
        "device-preserve@example.test", "ws-preserve",
        codexDeviceConvergenceEnabled=False,
    ))
    account = oauth_manager.get_account("openai:device-preserve@example.test:ws-preserve")
    assert account["codexDeviceConvergenceEnabled"] is False
    assert account["codexDeviceInstallationId"] == first_id
    assert OpenAIOAuthChannel(account).codex_device_installation_id == ""

    oauth_manager.add_account(_entry(
        "device-preserve@example.test", "ws-preserve",
        codexDeviceConvergenceEnabled=True,
    ))
    account = oauth_manager.get_account("openai:device-preserve@example.test:ws-preserve")
    assert account["codexDeviceConvergenceEnabled"] is True
    assert account["codexDeviceInstallationId"] == first_id


def test_missing_workspace_stays_out_until_refresh_establishes_identity():
    from src import state_db
    state_db.init()
    oauth_manager.add_account(_entry("device-late-workspace@example.test", ""))
    account = oauth_manager.get_account("openai:device-late-workspace@example.test")
    assert account is not None
    assert "codexDeviceInstallationId" not in account

    assert oauth_manager._save_token_fields(
        "openai:device-late-workspace@example.test",
        {"workspace_id": "ws-established", "chatgpt_account_id": "ws-established"},
    ) is True
    account = oauth_manager.get_account(
        "openai:device-late-workspace@example.test:ws-established"
    )
    _assert_uuid4(account["codexDeviceInstallationId"])


@pytest.mark.asyncio
async def test_first_request_uses_workspace_and_device_created_during_refresh(monkeypatch):
    from src import state_db

    state_db.init()
    oauth_manager.add_account(_entry("device-first-refresh@example.test", ""))
    before = oauth_manager.get_account("openai:device-first-refresh@example.test")
    channel = OpenAIOAuthChannel(before)
    assert channel.codex_device_installation_id == ""

    async def refresh_during_request(account_key):
        assert oauth_manager._save_token_fields(
            account_key,
            {
                "workspace_id": "ws-first-refresh",
                "chatgpt_account_id": "ws-first-refresh",
            },
        ) is True
        return "access"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", refresh_during_request)
    deferred = await channel.build_upstream_request(
        {"model": "gpt-test", "input": "hello"},
        "gpt-test", ingress_protocol="responses", defer_device_fingerprint=True,
    )
    persisted = oauth_manager.get_account(
        "openai:device-first-refresh@example.test:ws-first-refresh"
    )
    installation_id = persisted["codexDeviceInstallationId"]
    _assert_uuid4(installation_id)
    assert deferred.headers["chatgpt-account-id"] == "ws-first-refresh"
    assert "x-codex-installation-id" not in deferred.headers
    assert channel.codex_device_installation_id == installation_id

    _, headers, frame, _ = prepare_oauth_responses_ws_request_parts(
        deferred, {"model": "gpt-test"}, "gpt-test", channel=channel,
    )
    assert headers["x-codex-installation-id"] == installation_id
    assert json.loads(frame)["client_metadata"][
        "x-codex-installation-id"
    ] == installation_id


@pytest.mark.asyncio
async def test_first_refresh_uses_exact_alias_with_same_email_other_workspace(monkeypatch):
    from src import state_db

    state_db.init()
    email = "device-alias@example.test"
    oauth_manager.add_account(_entry(email, "ws-existing"))
    oauth_manager.add_account(_entry(email, ""))
    channel = OpenAIOAuthChannel(
        oauth_manager.get_account(f"openai:{email}")
    )

    async def refresh_during_request(account_key):
        assert oauth_manager._save_token_fields(
            account_key,
            {"workspace_id": "ws-new", "chatgpt_account_id": "ws-new"},
        ) is True
        return "access"

    monkeypatch.setattr(oauth_manager, "ensure_valid_token", refresh_during_request)
    request = await channel.build_upstream_request(
        {"model": "gpt-test", "input": "hello"},
        "gpt-test", ingress_protocol="responses",
    )
    new_account = oauth_manager.get_account(f"openai:{email}:ws-new")
    existing_account = oauth_manager.get_account(f"openai:{email}:ws-existing")
    new_device = new_account["codexDeviceInstallationId"]
    assert request.headers["chatgpt-account-id"] == "ws-new"
    assert request.headers["x-codex-installation-id"] == new_device
    assert new_device != existing_account["codexDeviceInstallationId"]


def test_workspace_separation_never_inherits_device():
    oauth_manager.add_account(_entry(
        "device-workspaces@example.test", "ws-a",
        codexDeviceInstallationId=DEVICE_A,
    ))
    oauth_manager.add_account(_entry("device-workspaces@example.test", "ws-b"))
    first = oauth_manager.get_account("openai:device-workspaces@example.test:ws-a")
    second = oauth_manager.get_account("openai:device-workspaces@example.test:ws-b")
    assert first["codexDeviceInstallationId"] == DEVICE_A
    _assert_uuid4(second["codexDeviceInstallationId"])
    assert second["codexDeviceInstallationId"] != DEVICE_A


def test_input_validation_and_workspace_requirement_fail_closed():
    for bad in ("bad", "123E4567-E89B-42D3-A456-426614174000"):
        with pytest.raises(ValueError, match="canonical UUIDv4"):
            oauth_manager.add_account(_entry(
                "device-invalid@example.test", "ws-invalid",
                codexDeviceInstallationId=bad,
            ))
    with pytest.raises(ValueError, match="requires a nonempty"):
        oauth_manager.add_account(_entry(
            "device-no-workspace@example.test", "",
            codexDeviceInstallationId=DEVICE_A,
        ))
    with pytest.raises(ValueError, match="must be a boolean"):
        oauth_manager.add_account(_entry(
            "device-invalid-switch@example.test", "ws-invalid-switch",
            codexDeviceConvergenceEnabled="false",
        ))


def test_channel_snapshot_reads_persisted_id_and_explicit_off():
    off = OpenAIOAuthChannel(_entry(
        "snapshot-off@example.test", "ws-off",
        codexDeviceInstallationId=DEVICE_B,
        codexDeviceConvergenceEnabled=False,
    ))
    assert off.codex_device_installation_id == ""
    enabled = OpenAIOAuthChannel(_entry(
        "snapshot-on@example.test", "ws-on",
        codexDeviceInstallationId=DEVICE_A,
    ))
    assert enabled.codex_device_installation_id == DEVICE_A


@pytest.mark.asyncio
async def test_http_all_codex_responses_ingresses_explicit_off_and_realtime_negative(monkeypatch):
    async def token(_key):
        return "access"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)

    enabled = OpenAIOAuthChannel(_entry(
        "http-on@example.test", "ws-http-on",
        codexDeviceInstallationId=DEVICE_A,
    ))
    request = await enabled.build_upstream_request(
        {
            "model": "gpt-test", "input": "hello",
            "client_metadata": {"sentinel": {"deep": "KEEP"}},
        },
        "gpt-test", ingress_protocol="responses",
    )
    payload = json.loads(request.body)
    assert request.headers["x-codex-installation-id"] == DEVICE_A
    assert payload["client_metadata"] == {
        "sentinel": {"deep": "KEEP"},
        "x-codex-installation-id": DEVICE_A,
    }
    request_without_metadata = await enabled.build_upstream_request(
        {"model": "gpt-test", "input": "hello"},
        "gpt-test", ingress_protocol="responses",
    )
    assert json.loads(request_without_metadata.body)["client_metadata"] == {
        "x-codex-installation-id": DEVICE_A,
    }

    off = OpenAIOAuthChannel(_entry(
        "http-off@example.test", "ws-http-off",
        codexDeviceInstallationId=DEVICE_B,
        codexDeviceConvergenceEnabled=False,
    ))
    request_off = await off.build_upstream_request(
        {"model": "gpt-test", "input": "hello"},
        "gpt-test", ingress_protocol="responses",
    )
    assert "x-codex-installation-id" not in request_off.headers
    assert "client_metadata" not in json.loads(request_off.body)

    realtime_headers = await enabled.build_realtime_headers()
    assert "x-codex-installation-id" not in realtime_headers

    chat_request = await enabled.build_upstream_request(
        {
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "prompt_cache_key": "chat-cache",
            "reasoning_effort": "high",
            "_api_key_name": "chat-client",
        },
        "gpt-test", ingress_protocol="chat",
    )
    chat_payload = json.loads(chat_request.body)
    assert chat_request.headers["x-codex-installation-id"] == DEVICE_A
    assert chat_payload["prompt_cache_key"] == "chat-cache"
    assert chat_payload["reasoning"] == {"effort": "high"}
    assert chat_request.headers.get("session_id")

    anthropic_body = {
        "model": "gpt-test",
        "system": [{
            "type": "text", "text": "stable system",
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [{"role": "user", "content": "hello"}],
        "output_config": {"effort": "high"},
        "metadata": {"user_id": '{"session_id":"anthropic-session"}'},
        "_parrot_api_key_name": "anthropic-client",
    }
    anthropic_request = await enabled.build_upstream_request(
        anthropic_body, "gpt-5.1", ingress_protocol="anthropic",
    )
    anthropic_payload = json.loads(anthropic_request.body)
    assert anthropic_request.headers["x-codex-installation-id"] == DEVICE_A
    assert anthropic_payload["reasoning"] == {"effort": "high"}
    assert anthropic_payload["prompt_cache_key"].startswith("parrot:cache:v1:a2o-session:")
    assert "prompt_cache_retention" not in anthropic_payload
    assert anthropic_request.headers.get("session_id")
    assert anthropic_body["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress_protocol", ["chat", "anthropic"])
async def test_translated_http_finalization_preserves_noninstallation_identity_fields(
    monkeypatch, ingress_protocol,
):
    async def token(_key):
        return "access"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)

    from src.channel import openai_oauth_channel as oauth_channel_module

    translated = {
        "model": "gpt-test",
        "input": [{"type": "message", "role": "user", "content": "hello"}],
        "prompt_cache_key": "CACHE",
        "reasoning": {"effort": "high"},
        "client_metadata": {
            "x-codex-installation-id": "downstream",
            "x-codex-turn-metadata": json.dumps({
                "installation_id": "downstream", "window_id": "WINDOW",
            }),
            "cache_hint": {"ttl": "1h"},
        },
    }

    if ingress_protocol == "chat":
        monkeypatch.setattr(
            oauth_channel_module.chat_to_responses, "translate_request",
            lambda _body: dict(translated),
        )
    else:
        monkeypatch.setattr(
            oauth_channel_module.anthropic_to_responses, "translate_request",
            lambda _body, *, target_model=None, codex_oauth=False: dict(translated),
        )
        monkeypatch.setattr(
            oauth_channel_module.cache_hints,
            "apply_anthropic_cache_to_openai_payload",
            lambda *_args, **_kwargs: None,
        )

    enabled = OpenAIOAuthChannel(_entry(
        f"identity-{ingress_protocol}@example.test", f"ws-{ingress_protocol}",
        codexDeviceInstallationId=DEVICE_A,
    ))
    monkeypatch.setattr(enabled, "_build_headers", lambda _token, **_kwargs: {
        "session-id": "SESSION",
        "thread-id": "THREAD",
        "x-codex-turn-metadata": json.dumps({
            "installation_id": "downstream", "turn_id": "TURN",
        }),
    })

    request = await enabled.build_upstream_request(
        {"model": "gpt-test", "messages": [{"role": "user", "content": "hello"}]},
        "gpt-test", ingress_protocol=ingress_protocol,
    )
    payload = json.loads(request.body)
    assert request.headers["x-codex-installation-id"] == DEVICE_A
    assert request.headers["session-id"] == "SESSION"
    assert request.headers["thread-id"] == "THREAD"
    assert json.loads(request.headers["x-codex-turn-metadata"]) == {
        "installation_id": DEVICE_A, "turn_id": "TURN",
    }
    assert payload["prompt_cache_key"] == "CACHE"
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["client_metadata"]["cache_hint"] == {"ttl": "1h"}
    assert json.loads(payload["client_metadata"]["x-codex-turn-metadata"]) == {
        "installation_id": DEVICE_A, "window_id": "WINDOW",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress_protocol", ["responses", "chat", "anthropic"])
async def test_http_defer_skips_device_finalization_for_every_accepted_ingress(
    monkeypatch, ingress_protocol,
):
    async def token(_key):
        return "access"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    enabled = OpenAIOAuthChannel(_entry(
        f"defer-{ingress_protocol}@example.test", f"ws-defer-{ingress_protocol}",
        codexDeviceInstallationId=DEVICE_A,
    ))
    body = (
        {"model": "gpt-test", "input": "hello"}
        if ingress_protocol == "responses"
        else {"model": "gpt-test", "messages": [{"role": "user", "content": "hello"}]}
    )
    request = await enabled.build_upstream_request(
        body, "gpt-test", ingress_protocol=ingress_protocol,
        defer_device_fingerprint=True,
    )
    assert "x-codex-installation-id" not in request.headers


@pytest.mark.asyncio
async def test_api_key_and_non_openai_channels_do_not_receive_codex_device_identity():
    openai_api = OpenAIApiChannel({
        "name": "openai-api-device-negative",
        "type": "api",
        "baseUrl": "https://api.example.test/v1",
        "apiKey": "sk-test",
        "protocol": "openai-responses",
        "models": [{"real": "gpt-test", "alias": "gpt-test"}],
        "codexDeviceInstallationId": DEVICE_A,
    })
    api_request = await openai_api.build_upstream_request(
        {"model": "gpt-test", "input": "hello"},
        "gpt-test", ingress_protocol="responses",
    )
    assert "x-codex-installation-id" not in api_request.headers
    assert "x-codex-installation-id" not in (
        json.loads(api_request.body).get("client_metadata") or {}
    )

    anthropic_api = ApiChannel({
        "name": "anthropic-api-device-negative",
        "type": "api",
        "baseUrl": "https://api.example.test",
        "apiKey": "sk-ant-test",
        "models": [{"real": "claude-test", "alias": "claude-test"}],
        "codexDeviceInstallationId": DEVICE_A,
    })
    anthropic_request = await anthropic_api.build_upstream_request(
        {
            "model": "claude-test",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 16,
        },
        "claude-test", ingress_protocol="anthropic",
    )
    assert "x-codex-installation-id" not in anthropic_request.headers


@pytest.mark.asyncio
async def test_http_wrong_type_metadata_fails_candidate_transformation(monkeypatch):
    async def token(_key):
        return "access"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    enabled = OpenAIOAuthChannel(_entry(
        "http-invalid@example.test", "ws-http-invalid",
        codexDeviceInstallationId=DEVICE_A,
    ))
    with pytest.raises(ValueError, match="client_metadata must be an object"):
        await enabled.build_upstream_request(
            {"model": "gpt-test", "input": "hello", "client_metadata": "bad"},
            "gpt-test", ingress_protocol="responses",
        )


def test_legacy_config_migration_is_atomic_idempotent_persistent_and_skips_unknown_workspace(
    tmp_path, monkeypatch,
):
    path = tmp_path / "device-config.json"
    raw = {
        "oauthAccounts": [
            _entry("legacy-default@example.test", "ws-legacy"),
            _entry("legacy-empty@example.test", "ws-empty", codexDeviceInstallationId=""),
            _entry("legacy-known@example.test", "ws-known", codexDeviceInstallationId=DEVICE_A),
            _entry("legacy-unknown@example.test", ""),
        ],
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    config._cache = None
    config._mtime = 0
    writes = []
    real_write = config._write_atomic

    def observed(candidate):
        writes.append(json.loads(json.dumps(candidate)))
        real_write(candidate)

    monkeypatch.setattr(config, "_write_atomic", observed)
    first = config.reload()
    accounts = {a["email"]: a for a in first["oauthAccounts"]}
    generated = accounts["legacy-default@example.test"]["codexDeviceInstallationId"]
    _assert_uuid4(generated)
    _assert_uuid4(accounts["legacy-empty@example.test"]["codexDeviceInstallationId"])
    assert "codexDeviceConvergenceEnabled" not in accounts["legacy-empty@example.test"]
    assert accounts["legacy-known@example.test"]["codexDeviceInstallationId"] == DEVICE_A
    assert "codexDeviceInstallationId" not in accounts["legacy-unknown@example.test"]
    assert len(writes) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["oauthAccounts"][0][
        "codexDeviceInstallationId"
    ] == generated
    assert (tmp_path / "device-config.json.bak.1").exists()

    second = config.reload()
    assert len(writes) == 1
    assert second["oauthAccounts"][0]["codexDeviceInstallationId"] == generated


def test_invalid_legacy_uuid_aborts_without_replacing_live_config(tmp_path, monkeypatch):
    path = tmp_path / "invalid-config.json"
    raw = {"oauthAccounts": [_entry(
        "legacy-invalid@example.test", "ws-invalid",
        codexDeviceInstallationId="invalid",
    )]}
    original = json.dumps(raw)
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    config._cache = None
    config._mtime = 0
    with pytest.raises(ValueError, match="canonical UUIDv4"):
        config.reload()
    assert path.read_text(encoding="utf-8") == original


def test_config_reload_preserves_manual_optional_field():
    oauth_manager.add_account(_entry(
        "device-reload@example.test", "ws-reload",
        codexDeviceInstallationId=DEVICE_A,
    ))
    config.reload()
    account = oauth_manager.get_account("openai:device-reload@example.test:ws-reload")
    assert account["codexDeviceInstallationId"] == DEVICE_A
