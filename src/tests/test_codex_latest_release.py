"""Released-source, real config migration, and latest Codex wire gates (offline)."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from src import config, oauth_manager, oauth_model_discovery, state_db
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.openai import codex_constants as constants, codex_identity
from src.openai.responses_ws_runtime import build_oauth_responses_ws_frame
from src.state_store import StateStore

VERSION = "0.157.0-alpha.10"
PROFILE = f"rust-v{VERSION}"
OLD = {"codexCliVersion": "0.153.4", "codexProtocolProfile": "rust-v0.153.4"}
ROOT = Path(__file__).parents[2]
PROFILES = ROOT / "src/openai/codex_profiles"


@pytest.fixture(autouse=True)
def isolated_release_state(tmp_path, monkeypatch):
    store = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"),
                       manifest_path=str(tmp_path / "manifest.json"))
    store.start()
    monkeypatch.setattr(state_db, "_store", store)
    monkeypatch.setattr(config, "CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setenv("DISABLE_OAUTH_NETWORK_CALLS", "1")
    codex_identity.clear_turn_mappings_for_tests()
    yield
    codex_identity.clear_turn_mappings_for_tests()
    store.close()


def account(model="gpt-6-sol", records=None):
    result = {
        "provider": "openai", "email": "release@example.test",
        "chatgpt_account_id": "release-workspace", "workspace_id": "release-workspace",
        "access_token": "test-access", "refresh_token": "test-refresh",
        "expired": "2999-01-01T00:00:00Z", "enabled": True, "models": [model],
    }
    if records is not None:
        result["account_model_catalog"] = {"schema": 1, "models": records}
    return result


def load(provider=None, accounts=None, *, legacy=False):
    raw = {"oauthAccounts": accounts or [], "channels": [], "oauth": {"mockMode": True}}
    if provider is not None:
        if legacy:
            raw["oauth"]["providers"] = {"openai": provider}
        else:
            raw["openaiOAuth"] = provider
    Path(config.CONFIG_PATH).write_text(json.dumps(raw))
    config._cache = None
    return config.get()


@pytest.mark.parametrize("auto", [None, True, False])
@pytest.mark.parametrize("legacy", [False, True])
def test_existing_config_upgrades_or_pins_without_rotating_identity(auto, legacy, monkeypatch):
    provider = dict(OLD)
    if auto is not None:
        provider["codexProfileAutoUpdate"] = auto
    now = datetime.now(timezone.utc)
    acc = account("gpt-5.5", records=[{"id": "gpt-5.5", "useResponsesLite": False}])
    acc.update({
        "last_model_sync": now.isoformat(),
        "last_model_sync_client_version": OLD["codexCliVersion"],
        "last_model_sync_profile": OLD["codexProtocolProfile"],
        "models_etag": "old-client-etag",
        "models_etag_client_version": OLD["codexCliVersion"],
        "models_etag_profile": OLD["codexProtocolProfile"],
    })
    codex_identity.normalize_account_identity(acc, protocol_profile=OLD["codexProtocolProfile"])
    original = copy.deepcopy(acc)
    loaded = load(provider, [acc], legacy=legacy)
    expected_version = "0.153.4" if auto is False else VERSION
    expected_profile = OLD["codexProtocolProfile"] if auto is False else PROFILE
    for cfg in (loaded, json.loads(Path(config.CONFIG_PATH).read_text()), config.reload()):
        selected = cfg["openaiOAuth"]
        assert selected["codexCliVersion"] == expected_version
        assert selected["codexProtocolProfile"] == expected_profile
        saved = cfg["oauthAccounts"][0]
        assert saved["codexDeviceInstallationId"] == original["codexDeviceInstallationId"]
        assert saved["access_token"] == original["access_token"]
        assert saved["refresh_token"] == original["refresh_token"]
    seen = {}
    class Response:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self):
            return {"models": [{"slug": "gpt-6-sol", "visibility": "list"}]}
    monkeypatch.setattr(oauth_model_discovery.network, "get_sync",
                        lambda url, **kw: seen.update(url=url, **kw) or Response())
    # The old schema lost hidden models/instructions/nulls even for an old pin.
    assert oauth_manager._model_sync_due(loaded["oauthAccounts"][0], now=now)
    result = oauth_model_discovery.discover_openai(loaded["oauthAccounts"][0])
    assert seen["headers"].get("If-None-Match") is None
    assert result.client_version == expected_version
    assert result.profile_id == expected_profile
    assert seen["url"].endswith(f"?client_version={expected_version.split('-', 1)[0]}")
    assert seen["headers"]["version"] == expected_version
    assert seen["headers"]["user-agent"].startswith(f"codex_cli_rs/{expected_version} ")


def test_latest_profile_packaged_hashes_and_baselines():
    profile = constants.current_codex_protocol_profile()
    assert (profile.profile_id, profile.client_version) == (PROFILE, VERSION)
    manifest = json.loads((PROFILES / f"{PROFILE}.json").read_text())
    assert manifest["source"]["codexCommit"] == "2170d8b3c77883dbe743078fb8bbb017f27caa9c"
    baseline_raw = (PROFILES / "rust-v0.153.4.json").read_bytes()
    assert hashlib.sha256(baseline_raw).hexdigest() == manifest["source"]["baselineProfilesSha256"]["rust-v0.153.4"]
    fresh = {model for model, row in manifest["models"].items() if row["sourceCodexTag"] == PROFILE}
    assert len(fresh) == 11
    assert {"gpt-6-astra", "gpt-6-sol", "gpt-6-luna"} <= fresh
    for model in fresh:
        row = manifest["models"][model]
        raw = (PROFILES / row["baseInstructionsFile"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == row["baseInstructionsSha256"]
        assert profile.model_policy(model).base_instructions == raw.decode()
    assert set(manifest["models"]) - fresh == {"gpt-5.2", "gpt-5.4-mini"}
    old = json.loads(baseline_raw)
    for model in set(manifest["models"]) - fresh:
        assert manifest["models"][model] == {**old["models"][model], "sourceCodexTag": "rust-v0.153.4"}
    # Astra's literal instructions are byte-identical across these tags; do not
    # manufacture a difference just to make an upgrade assertion pass.
    assert profile.model_policy("gpt-6-astra").base_instructions == constants.codex_protocol_profile(OLD).model_policy("gpt-6-astra").base_instructions


def test_latest_profile_matches_authoritative_release_source():
    source = os.environ.get("CODEX_RELEASE_SOURCE")
    if not source:
        pytest.skip("Set CODEX_RELEASE_SOURCE to the read-only released tag worktree")
    subprocess.run([sys.executable, str(ROOT / "scripts/codex_release_profile.py"), source, "--check"], check=True)
    source_models = json.loads((Path(source) / "codex-rs/models-manager/models.json").read_text())["models"]
    manifest = json.loads((PROFILES / f"{PROFILE}.json").read_text())
    for model in source_models:
        policy = constants.current_codex_protocol_profile().model_policy(model["slug"])
        row = manifest["models"][model["slug"]]
        assert policy.base_instructions == model["model_messages"]["instructions_template"]
        assert policy.minimal_client_version == model["minimal_client_version"]
        assert list(policy.reasoning_efforts) == [e["effort"] for e in model["supported_reasoning_levels"]]
        assert policy.use_responses_lite == model.get("use_responses_lite", False)
        assert policy.multi_agent_reasoning_effort == model.get("multi_agent_reasoning_effort")
        assert policy.support_verbosity == model["support_verbosity"]
        assert policy.supports_reasoning_summary_parameter == model.get("supports_reasoning_summary_parameter", True)
        assert row["sourceModelProfileSha256"] == hashlib.sha256(json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("current,minimum,expected", [
    (VERSION, "0.155.0", True), ("0.153.4", "0.155.0", False),
    (VERSION, "0.157.0", False), (VERSION, "0.157.0-alpha.9", True),
    (VERSION, "0.157.0-alpha.11", False), (VERSION, VERSION, True),
    ("0.157.0+build.1", VERSION, True), ("0.157.0-alpha.01", VERSION, None),
    ("0.157.0-alpha..1", VERSION, None), ("01.157.0", VERSION, None),
])
def test_semver_release_and_prerelease_gates(current, minimum, expected):
    assert constants.codex_version_meets_minimum(current, minimum) is expected


@pytest.mark.parametrize("version,profile", [(VERSION, OLD["codexProtocolProfile"]), ("0.153.4", PROFILE), ("0.156.0", PROFILE)])
def test_cross_release_pin_pairs_fail_closed(version, profile):
    loaded = load({"codexProfileAutoUpdate": False, "codexCliVersion": version, "codexProtocolProfile": profile})
    with pytest.raises(constants.CodexConfigurationError, match="requires client version"):
        constants.codex_protocol_profile(loaded["openaiOAuth"])


async def request(monkeypatch, model, body=None, *, pin=False, records=None, transport="http"):
    loaded = load({**OLD, "codexProfileAutoUpdate": not pin}, [account(model, records)])
    acc = loaded["oauthAccounts"][0]
    async def token(key): return "test-access"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    channel = OpenAIOAuthChannel(acc)
    req = await channel.build_upstream_request(
        {"model": model, "input": "hello", "prompt_cache_key": "release-thread", **(body or {})},
        model, ingress_protocol="responses", responses_transport=transport,
    )
    payload = json.loads(req.body)
    if transport == "websocket":
        payload = build_oauth_responses_ws_frame(payload, model, channel=channel)
    return req, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("model,lite,effort", [("gpt-6-sol", True, "medium"), ("gpt-6-luna", True, "medium"), ("gpt-6-astra", True, "low"), ("gpt-5.4", False, "medium")])
async def test_latest_source_models_real_http_ws_shape(monkeypatch, transport, model, lite, effort):
    req, body = await request(monkeypatch, model, {
        "temperature": 0.7, "max_output_tokens": 123,
        "tools": [{"type": "function", "name": "echo", "parameters": {"type": "object"}}],
    }, transport=transport)
    headers = {k.lower(): v for k, v in req.headers.items()}
    assert headers["version"] == VERSION
    assert headers["user-agent"].startswith(f"codex_cli_rs/{VERSION} ")
    assert body["reasoning"]["effort"] == effort
    assert body["text"]["verbosity"] == "low"
    assert body["store"] is False and body["stream"] is True
    assert "temperature" not in body and "max_output_tokens" not in body
    metadata = body["client_metadata"]
    assert metadata["thread_id"] == headers["thread-id"] == body["prompt_cache_key"]
    assert uuid.UUID(metadata["x-codex-installation-id"]).version == 4
    base = constants.current_codex_protocol_profile().model_policy(model).base_instructions
    if lite:
        assert "instructions" not in body and "tools" not in body
        assert body["input"][0]["type"] == "additional_tools"
        assert body["input"][1]["content"][0]["text"] == base
        assert body["input"][1]["internal_chat_message_metadata_passthrough"] == {"content_item_kinds": ["model.base_instructions"]}
        assert body["reasoning"]["context"] == "all_turns"
        assert body["parallel_tool_calls"] is False
        if transport == "websocket":
            assert metadata[constants.CODEX_RESPONSES_LITE_WS_METADATA_KEY] == "true"
        else:
            assert headers[constants.CODEX_RESPONSES_LITE_HEADER] == "true"
    else:
        assert body["instructions"] == base
        assert body["tools"][0]["name"] == "echo"
        assert "context" not in body["reasoning"]
        assert constants.CODEX_RESPONSES_LITE_WS_METADATA_KEY not in metadata
    assert "analytics_enabled" not in metadata and "mcp_attribution" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_new_model_minimum_passes_current_but_rejects_old_pin(monkeypatch, transport):
    records = [{"id": "gpt-6-sol", "useResponsesLite": True, "minimalClientVersion": "0.155.0"}]
    _, body = await request(monkeypatch, "gpt-6-sol", records=records, transport=transport)
    assert body["model"] == "gpt-6-sol"
    with pytest.raises(Exception, match="below model .* minimum") as error:
        await request(monkeypatch, "gpt-6-sol", records=records, pin=True, transport=transport)
    assert error.value.scope == "candidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_latest_ultra_mapping_and_capability_overrides(monkeypatch, transport):
    for model, target in [("gpt-6-sol", "max"), ("gpt-6-astra", "xhigh")]:
        _, body = await request(monkeypatch, model, {"reasoning": {"effort": "ultra", "summary": "none"}}, transport=transport)
        assert body["reasoning"] == {"effort": target, "context": "all_turns"}
    _, body = await request(monkeypatch, "gpt-6-sol", {
        "instructions": "caller instructions", "reasoning": {"summary": "auto"},
        "text": {"verbosity": "high", "format": {"type": "json_object"}},
    }, records=[{"id": "gpt-6-sol", "supportVerbosity": False, "supportsReasoningSummaryParameter": False}], transport=transport)
    assert body["text"] == {"format": {"type": "json_object"}}
    assert "summary" not in body["reasoning"]
    assert body["input"][1]["content"][0]["text"] == "caller instructions"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("levels,target,expected", [
    (["low", "max", "ultra"], None, "max"),
    (["low", "high", "ultra"], None, "high"),
    (["low", "max", "ultra"], "ultra", "max"),
    (["low", "high", "ultra"], "missing", "high"),
    (["low", "high", "ultra"], "low", "low"),
    (["ultra"], None, "medium"),
])
async def test_latest_ultra_uses_effective_catalog_not_static_profile(monkeypatch, transport, levels, target, expected):
    records = [{"id": "catalog-ultra", "useResponsesLite": False,
                "reasoningEfforts": levels, "multiAgentReasoningEffort": target}]
    _, body = await request(monkeypatch, "catalog-ultra", {"reasoning": {"effort": "ultra"}}, records=records, transport=transport)
    assert body["reasoning"]["effort"] == expected
    if target is None:
        with pytest.raises(ValueError, match="requires explicit model-scoped"):
            await request(monkeypatch, "catalog-ultra", {"reasoning": {"effort": "ultra"}}, records=records, pin=True, transport=transport)


@pytest.mark.parametrize("provider,ttl", [("openai", 300), ("claude", 21600), ("xai", 21600), ("cursor", 21600), ("antigravity", 21600), ("workbuddy", 21600)])
def test_each_provider_model_sync_ttl_only_openai_five_minutes(provider, ttl):
    load()
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    acc = {"provider": provider, "models": ["model"], "last_model_sync": now.isoformat(),
           "last_model_sync_client_version": VERSION, "last_model_sync_profile": PROFILE}
    key = "cursor_model_catalog" if provider == "cursor" else "account_model_catalog"
    acc[key] = {"schema": 2 if provider == "openai" else 1, "models": [{"id": "model"}]}
    assert not oauth_manager._model_sync_due(acc, now=now + timedelta(seconds=ttl - 1))
    assert oauth_manager._model_sync_due(acc, now=now + timedelta(seconds=ttl))
    acc["last_model_sync_error"] = "TimeoutError"
    acc["last_model_sync_attempt"] = now.isoformat()
    acc["last_model_sync_attempt_client_version"] = VERSION
    acc["last_model_sync_attempt_profile"] = PROFILE
    assert not oauth_manager._model_sync_due(acc, now=now + timedelta(seconds=899))
    assert oauth_manager._model_sync_due(acc, now=now + timedelta(seconds=900))
    acc["last_model_sync_error"] = ""
    acc[key] = {"models": []}
    assert oauth_manager._model_sync_due(acc, now=now)
