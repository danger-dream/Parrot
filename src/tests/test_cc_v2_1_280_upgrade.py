"""Claude Code v2.1.280/Fable wire-model regression tests.

Real fixtures are local, read-only captures.  They are never sent upstream; when
that corpus is not mounted, only corpus-specific tests skip.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)
from src.tests import _isolation

_isolation.isolate()

from src import oauth_manager
from src.channel.api_channel import ApiChannel
from src.providers import registry as provider_registry
from src.transform import cc_mimicry as m
from src.transports import http_runtime


BODIES = Path("/opt/workspace/claude-code-cch/v2.1.280/bodies_representative")
BODY_FILES = sorted(BODIES.glob("*.bin")) if BODIES.is_dir() else []
CORPUS_SKIP = pytest.mark.skipif(not BODY_FILES, reason=f"v280 fixtures not present: {BODIES}")

MAIN_BETAS = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,"
    "thinking-token-count-2026-05-13,context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,mid-conversation-system-2026-04-07,"
    "mid-conversation-tool-changes-2026-07-01,advanced-tool-use-2025-11-20,"
    "effort-2025-11-24,fallback-credit-2026-06-01,"
    "thinking-binding-controls-2026-08-01,cache-diagnosis-2026-04-07"
)
FABLE_BETAS = MAIN_BETAS.replace(
    "fallback-credit-2026-06-01",
    "server-side-fallback-2026-06-01,fallback-credit-2026-06-01",
)
OPUS_5_BETAS = MAIN_BETAS
HAIKU_MAIN_BETAS = (
    "interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
    "claude-code-20250219"
)
SIDE_BETAS = (
    "interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
    "structured-outputs-2025-12-15,cache-diagnosis-2026-04-07"
)
OAUTH_SIDE_BETAS = "oauth-2025-04-20," + SIDE_BETAS


@pytest.fixture
def dynamic_cch(monkeypatch):
    monkeypatch.setattr(
        m, "load_config",
        lambda: {"cch_mode": "dynamic", "cch_static_value": "00000"},
    )


def _messages(text: str = "say hi") -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def _request(model: str, text: str = "say hi", **updates) -> dict:
    body = {"model": model, "messages": _messages(text), "stream": True}
    body.update(updates)
    return m.ensure_request_context(body)


def _transform(model: str, text: str = "say hi", *, auth_mode="api_key", **updates):
    body = _request(model, text, **updates)
    payload, _ = m.transform_request(
        body,
        session_id=body[m.PARROT_CC_SESSION_ID_KEY],
        auth_mode=auth_mode,
    )
    return body, payload


def _billing(payload: dict) -> str:
    return payload["system"][0]["text"]


def _fixture_billing(body: dict) -> str:
    return next(
        block["text"]
        for block in body.get("system", [])
        if isinstance(block, dict)
        and str(block.get("text", "")).startswith("x-anthropic-billing-header:")
    )


def test_v280_constants_and_stainless_versions():
    assert m.CC_VERSION == "2.1.280"
    assert m.FINGERPRINT_SALT == "59cf53e54c78"
    assert m.FINGERPRINT_INDICES == (4, 7, 20)
    assert m.CCH_SEED == 0x4D659218E32A3268
    assert m.CLI_USER_AGENT == "claude-cli/2.1.280 (external, sdk-cli)"
    assert m._STAINLESS_HEADERS["X-Stainless-Package-Version"] == "0.112.1"
    assert m._STAINLESS_HEADERS["X-Stainless-Runtime-Version"] == "v26.3.0"


def test_fingerprint_fixed_vectors_and_utf16_emoji():
    assert m.compute_fingerprint(_messages("say hi")) == "31f"
    assert m.compute_fingerprint(_messages("what is 2+2")) == "853"
    emoji = "ab🚀d🚀f🚀hijklmnopqr🚀t"
    assert m.compute_fingerprint(_messages(emoji)) == "bc3"


def test_fingerprint_selected_lone_surrogate_hashes_as_replacement_character():
    text = "abcd\ud800"
    expected = hashlib.sha256(
        f"{m.FINGERPRINT_SALT}\ufffd00{m.CC_VERSION}".encode("utf-8")
    ).hexdigest()[:3]
    assert m.compute_fingerprint(_messages(text)) == expected


def test_fingerprint_selects_first_valid_non_meta_text_before_injection():
    messages = [
        {"role": "user", "isMeta": True, "content": "ignored explicit meta"},
        {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>\nignored wire meta\n</system-reminder>"},
            {"type": "text", "text": "what is 2+2"},
            {"type": "text", "text": "say hi"},
        ]},
    ]
    assert m.select_fingerprint_prompt(messages) == "what is 2+2"
    assert m.compute_fingerprint(messages) == "853"
    side = _messages("<session>\nwhat is 2+2\n</session>")
    assert m.select_fingerprint_prompt(side).startswith("<session>")


@CORPUS_SKIP
def test_fingerprint_matches_all_v280_representative_bodies():
    assert len(BODY_FILES) == 8
    for path in BODY_FILES:
        body = json.loads(path.read_bytes())
        expected = re.search(
            r"cc_version=2\.1\.280\.([0-9a-f]{3})",
            _fixture_billing(body),
        ).group(1)
        assert m.compute_fingerprint(body["messages"]) == expected, path.name


@CORPUS_SKIP
def test_cch_matches_every_signed_v280_representative_body():
    signed = 0
    for path in BODY_FILES:
        body = json.loads(path.read_bytes())
        match = re.search(r"cch=([0-9a-f]{5});", _fixture_billing(body))
        if match is None:
            continue
        signed += 1
        assert m.compute_cch(body) == match.group(1), path.name
    assert signed == 6


@CORPUS_SKIP
def test_fable_220k_fixture_is_signed_without_truncation():
    path = BODIES / "body_fable51_bigctx_220k.bin"
    raw = path.read_bytes()
    assert len(raw) > 220_000
    body = json.loads(raw)
    expected = re.search(r"cch=([0-9a-f]{5});", _fixture_billing(body)).group(1)
    assert m.compute_cch(body) == expected


def _cch_matrix_base() -> dict:
    messages = _messages("wire cch=00000 remains user text")
    return {
        "model": "claude-fable-5",
        "messages": [
            *messages,
            {"role": "assistant", "content": [{
                "type": "tool_use", "id": "toolu_1", "name": "probe",
                "input": {"max_tokens": 17, "model": "input-model", "keep": True},
            }]},
        ],
        "system": m.build_system_blocks(
            messages,
            inject_cache=False,
            prompt_id="11111111-1111-4111-8111-111111111111",
        ),
        "max_tokens": 64000,
        "nested": {"max_tokens": 7, "model": "nested-model"},
        "schema": {"properties": {"model": {"type": "string"}}},
        "fallbacks": "default",
        "tool_input": {"fallback_credit_token": "credit-a"},
        "stream": True,
    }


def test_cch_v280_double_body_matrix(dynamic_cch):
    base = _cch_matrix_base()
    top_max = {**base, "max_tokens": 1}
    assert m.cch_hash_view(top_max) == m.cch_hash_view(base)

    nested_max = json.loads(json.dumps(base))
    nested_max["nested"]["max_tokens"] = 8
    assert m.cch_hash_view(nested_max) != m.cch_hash_view(base)

    nested_model = json.loads(json.dumps(base))
    nested_model["nested"]["model"] = "another-string-model"
    assert m.cch_hash_view(nested_model) == m.cch_hash_view(base)

    top_model = {**base, "model": "claude-opus-5"}
    assert m.cch_hash_view(top_model) == m.cch_hash_view(base)

    schema_model = json.loads(json.dumps(base))
    schema_model["schema"]["properties"]["model"]["description"] = "changed"
    assert m.cch_hash_view(schema_model) != m.cch_hash_view(base)

    fallbacks = {**base, "fallbacks": "not-default"}
    assert m.cch_hash_view(fallbacks) == m.cch_hash_view(base)

    tool_input_max = json.loads(json.dumps(base))
    tool_input_max["messages"][1]["content"][0]["input"]["max_tokens"] = 999
    assert m.cch_hash_view(tool_input_max) == m.cch_hash_view(base)

    tool_input_other = json.loads(json.dumps(base))
    tool_input_other["messages"][1]["content"][0]["input"]["keep"] = False
    assert m.cch_hash_view(tool_input_other) != m.cch_hash_view(base)

    fallback_credit = json.loads(json.dumps(base))
    fallback_credit["tool_input"]["fallback_credit_token"] = "credit-b"
    assert m.cch_hash_view(fallback_credit) != m.cch_hash_view(base)


def test_sign_body_patches_only_generated_billing_and_preserves_wire(dynamic_cch):
    payload = _cch_matrix_base()
    signed = m.sign_body(payload)
    wire = json.loads(signed)
    assert wire["messages"][0]["content"][0]["text"] == "wire cch=00000 remains user text"
    assert wire["nested"]["model"] == "nested-model"
    assert wire["model"] == "claude-fable-5"
    assert "cch=00000" not in wire["system"][0]["text"]
    assert re.search(r"cch=[0-9a-f]{5};", wire["system"][0]["text"])


def test_cch_static_and_disabled_modes_do_not_dynamic_resign(monkeypatch):
    messages = _messages()
    monkeypatch.setattr(
        m, "load_config",
        lambda: {"cch_mode": "static", "cch_static_value": "abcde"},
    )
    payload = {"system": m.build_system_blocks(messages), "messages": messages}
    assert b"cch=abcde" in m.sign_body(payload)
    monkeypatch.setattr(
        m, "load_config",
        lambda: {"cch_mode": "disabled", "cch_static_value": "00000"},
    )
    payload = {"system": m.build_system_blocks(messages), "messages": messages}
    assert all("x-anthropic-billing-header" not in b.get("text", "") for b in payload["system"])


def test_fable_api_key_main_profile_and_wire_order(dynamic_cch):
    request, payload = _transform("claude-fable-5.1", "what is 2+2")
    assert list(payload) == [
        "model", "messages", "system", "metadata", "max_tokens", "thinking",
        "context_management", "output_config", "diagnostics", "stream",
    ]
    assert payload["model"] == "claude-fable-5.1"
    assert "fallbacks" not in payload
    assert payload["diagnostics"] == {"previous_message_id": None}
    assert payload["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert payload["context_management"] == {
        "edits": [{"type": "clear_thinking_20251015", "keep": "all"}],
    }
    assert payload["output_config"] == {"effort": "high"}
    assert payload["max_tokens"] == 64000
    assert re.search(r"cc_prompt_id=[0-9a-f-]{36}; cc_turn_origin=sdk;$", _billing(payload))
    assert request[m.PARROT_CC_PROMPT_ID_KEY] in _billing(payload)


def test_main_profiles_have_exact_observed_betas(dynamic_cch):
    _, fable = _transform("claude-fable-5.1", "what is 2+2", fallbacks=[{"model": "claude-opus-5"}])
    fable_h = m.build_upstream_headers(
        "key", auth_scheme="api_key", auth_mode="api_key",
        model="claude-fable-5.1", payload=fable,
    )
    assert fable_h["anthropic-beta"] == FABLE_BETAS
    assert fable_h["x-claude-code-request-class"] == "main"
    assert m.ADVISOR_TOOL_BETA not in fable_h["anthropic-beta"]

    _, opus48 = _transform("claude-opus-4-8")
    opus48_h = m.build_upstream_headers(
        "key", auth_scheme="api_key", auth_mode="api_key",
        model="claude-opus-4-8", payload=opus48,
    )
    assert opus48_h["anthropic-beta"] == MAIN_BETAS
    assert m.CONTEXT_1M_BETA not in opus48_h["anthropic-beta"]

    _, opus5 = _transform("claude-opus-5")
    opus5_h = m.build_upstream_headers(
        "key", auth_scheme="api_key", auth_mode="api_key",
        model="claude-opus-5", payload=opus5,
    )
    assert opus5_h["anthropic-beta"] == OPUS_5_BETAS
    assert m.CONTEXT_1M_BETA not in opus5_h["anthropic-beta"]

    _, haiku = _transform("claude-haiku-4-5", "ordinary main prompt")
    haiku_h = m.build_upstream_headers(
        "key", auth_scheme="api_key", auth_mode="api_key",
        model="claude-haiku-4-5", payload=haiku,
    )
    assert haiku_h["anthropic-beta"] == HAIKU_MAIN_BETAS
    assert "x-claude-code-request-class" not in haiku_h


def test_side_query_api_and_oauth_profiles(dynamic_cch):
    text = "<session>\nwhat is 2+2\n</session>\n\nWrite the title"
    request, payload = _transform("claude-haiku-4-5-20251001", text)
    assert list(payload) == [
        "model", "messages", "system", "metadata", "max_tokens", "thinking",
        "temperature", "output_config", "stream",
    ]
    assert payload["max_tokens"] == 32000
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["temperature"] == 1
    assert payload["output_config"]["format"]["type"] == "json_schema"
    assert all(field not in payload for field in ("fallbacks", "diagnostics"))
    assert "cc_prompt_id" not in _billing(payload)

    api_h = m.build_upstream_headers(
        "key", session_id=request[m.PARROT_CC_SESSION_ID_KEY],
        auth_scheme="api_key", auth_mode="api_key",
        model="claude-haiku-4-5-20251001", payload=payload,
    )
    assert api_h["anthropic-beta"] == SIDE_BETAS

    oauth_h = m.build_upstream_headers(
        "oat", session_id=request[m.PARROT_CC_SESSION_ID_KEY],
        auth_scheme="bearer", auth_mode="oauth",
        model="claude-haiku-4-5-20251001", payload=payload,
    )
    assert oauth_h["anthropic-beta"] == OAUTH_SIDE_BETAS
    assert oauth_h["Authorization"] == "Bearer oat"
    assert "x-api-key" not in oauth_h


def test_explicit_user_semantics_are_preserved(dynamic_cch):
    _, payload = _transform(
        "claude-fable-5",
        thinking={"type": "disabled"},
        output_config={"effort": "max"},
        temperature=0.25,
        stream=False,
    )
    assert payload["thinking"] == {"type": "disabled"}
    assert "context_management" not in payload
    assert payload["output_config"] == {"effort": "max"}
    assert payload["temperature"] == 0.25
    assert payload["stream"] is False


def test_oauth_fable_main_uses_captured_v280_profile_and_betas(dynamic_cch):
    _, payload = _transform(
        "claude-fable-5.1",
        auth_mode="oauth",
        thinking={"type": "disabled"},
        fallbacks="explicit-oauth-value",
        diagnostics={"previous_message_id": "msg_explicit"},
        output_config={"effort": "low"},
    )
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["fallbacks"] == "explicit-oauth-value"
    assert payload["diagnostics"] == {"previous_message_id": "msg_explicit"}
    assert payload["output_config"] == {"effort": "low"}

    _, captured = _transform("claude-fable-5.1", auth_mode="oauth")
    assert captured["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert "fallbacks" not in captured
    assert captured["output_config"] == {"effort": "high"}
    assert captured["diagnostics"] == {"previous_message_id": None}
    captured["messages"][0]["content"][0]["cache_control"] = {
        "type": "ephemeral", "ttl": "1h",
    }
    headers = m.build_upstream_headers(
        "token", auth_scheme="bearer", auth_mode="oauth",
        model="claude-fable-5.1", payload=captured,
    )
    betas = headers["anthropic-beta"].split(",")
    assert betas[:2] == ["claude-code-20250219", m.OAUTH_BETA]
    assert m.SERVER_SIDE_FALLBACK_BETA not in betas
    assert m.FALLBACK_CREDIT_BETA in betas
    assert m.EXTENDED_CACHE_TTL_BETA in betas
    assert m.ADVISOR_TOOL_BETA not in betas
    assert headers["x-claude-code-request-class"] == "main"


def test_metadata_session_and_billing_order_are_stable(dynamic_cch):
    body = _request("claude-opus-4-8")
    payload, _ = m.transform_request(
        body, email="account@example.com",
        session_id=body[m.PARROT_CC_SESSION_ID_KEY],
    )
    user_id = json.loads(payload["metadata"]["user_id"])
    assert user_id == {
        "device_id": m.DEVICE_ID,
        "account_uuid": "",
        "session_id": body[m.PARROT_CC_SESSION_ID_KEY],
    }
    billing = _billing(payload)
    assert billing.index("cc_version=") < billing.index("cc_entrypoint=")
    assert billing.index("cc_entrypoint=") < billing.index("cch=")
    assert billing.index("cch=") < billing.index("cc_prompt_id=")

    full = m.build_system_blocks(
        _messages(),
        inject_cache=False,
        workload="sdk",
        is_subagent=True,
        prev_req="req_previous",
        prompt_id="11111111-1111-4111-8111-111111111111",
    )[0]["text"]
    fields = [
        "cc_version=", "cc_entrypoint=", "cch=", "cc_workload=",
        "cc_is_subagent=", "cc_prev_req=", "cc_prompt_id=", "cc_turn_origin=",
    ]
    assert [full.index(field) for field in fields] == sorted(full.index(field) for field in fields)


def test_cc_provider_allowlist_retains_v280_fields_but_standard_does_not():
    payload = {
        "model": "claude-fable-5", "messages": [], "max_tokens": 1,
        "fallbacks": "default", "diagnostics": {"previous_message_id": None},
        m.PARROT_CC_SESSION_ID_KEY: "sid", m.PARROT_CC_PROMPT_ID_KEY: str(uuid.uuid4()),
    }
    cc_channel = SimpleNamespace(protocol="anthropic", type="api", cc_mimicry=True)
    standard_channel = SimpleNamespace(protocol="anthropic", type="api", cc_mimicry=False)
    cc = provider_registry.filter_request_payload(cc_channel, payload, protocol="anthropic")
    standard = provider_registry.filter_request_payload(standard_channel, payload, protocol="anthropic")
    assert payload.keys() <= cc.keys()
    assert "fallbacks" not in standard and "diagnostics" not in standard
    assert m.PARROT_CC_SESSION_ID_KEY not in standard


@pytest.mark.asyncio
async def test_official_api_key_and_third_party_auth_shapes_and_private_wire_strip(dynamic_cch):
    common = {
        "type": "api", "apiKey": "secret", "protocol": "anthropic",
        "models": [{"real": "claude-fable-5", "alias": "fable"}],
        "cc_mimicry": True, "enabled": True,
    }
    official = ApiChannel({
        **common, "name": "official", "baseUrl": "https://api.anthropic.com",
        "providerId": "anthropic",
    })
    compatible = ApiChannel({
        **common, "name": "compatible", "baseUrl": "https://third-party.example",
    })
    body = m.ensure_request_context({
        "model": "fable", "messages": _messages(), "fallbacks": "default",
        "diagnostics": {"previous_message_id": None}, "stream": False,
    })
    official_req = await official.build_upstream_request(body, "claude-fable-5")
    compatible_req = await compatible.build_upstream_request(body, "claude-fable-5")
    assert official_req.headers["x-api-key"] == "secret"
    assert "Authorization" not in official_req.headers
    assert compatible_req.headers["Authorization"] == "Bearer secret"
    assert "x-api-key" not in compatible_req.headers
    for wire in (json.loads(official_req.body), json.loads(compatible_req.body)):
        assert not any(str(key).startswith("_parrot_") for key in wire)
        assert wire["fallbacks"] == "default"
        assert wire["diagnostics"] == {"previous_message_id": None}


@pytest.mark.asyncio
async def test_openai_ingress_bridge_reuses_cc_request_context(dynamic_cch):
    channel = ApiChannel({
        "name": "openai-to-cc",
        "type": "api",
        "baseUrl": "https://api.anthropic.com",
        "apiKey": "secret",
        "providerId": "anthropic",
        "protocol": "anthropic",
        "models": [{"real": "claude-opus-4-8", "alias": "opus"}],
        "cc_mimicry": True,
        "enabled": True,
    })
    context = m.ensure_request_context({
        "model": "opus",
        "messages": [{"role": "user", "content": "say hi"}],
        "stream": False,
    })
    request = await channel.build_upstream_request(
        context, "claude-opus-4-8", ingress_protocol="chat",
    )
    wire = json.loads(request.body)
    metadata = json.loads(wire["metadata"]["user_id"])
    assert request.headers["X-Claude-Code-Session-Id"] == context[m.PARROT_CC_SESSION_ID_KEY]
    assert metadata["session_id"] == context[m.PARROT_CC_SESSION_ID_KEY]
    assert context[m.PARROT_CC_PROMPT_ID_KEY] in _billing(wire)


class _JSONResponse:
    def __init__(self, payload=None):
        self._payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_oauth_scope_login_and_v280_refresh_compatibility(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    scope = parse_qs(urlparse(oauth_manager.build_login_url("challenge", "state")).query)["scope"][0]
    assert scope == oauth_manager.OAUTH_SCOPES
    assert scope.endswith("user:file_upload user:plugins")

    calls = []

    def refresh_candidate(url, refresh_token, *, scope, proxy_channel=""):
        calls.append((url, refresh_token, scope, proxy_channel))
        if len(calls) == 1:
            raise RuntimeError("invalid_scope")
        return {"access_token": "new-token"}

    monkeypatch.setattr(oauth_manager, "_post_refresh_candidate", refresh_candidate)
    result = oauth_manager._do_refresh_http("refresh-token", account_key="claude:acct")
    assert result == {"access_token": "new-token"}
    assert calls[0][0] == calls[1][0] == oauth_manager.OAUTH_TOKEN_URL
    assert calls[0][2] == oauth_manager.OAUTH_SCOPES
    assert "user:plugins" not in calls[1][2]
    assert calls[0][3] == "oauth:claude:acct"


def test_oauth_refresh_request_always_carries_supplied_scope_without_custom_ua(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _JSONResponse({"access_token": "new-token"})

    monkeypatch.setattr(oauth_manager.network, "post_sync", post)
    oauth_manager._post_refresh_candidate(
        oauth_manager.OAUTH_TOKEN_URL, "refresh-token",
        scope=oauth_manager.OAUTH_SCOPES,
    )
    assert calls[0][1]["json"]["scope"] == oauth_manager.OAUTH_SCOPES
    assert calls[0][1]["headers"] == {"Content-Type": "application/json"}


def test_oauth_usage_profile_and_bootstrap_v280_headers(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return _JSONResponse()

    monkeypatch.setattr(oauth_manager, "mock_mode_enabled", lambda: False)
    monkeypatch.setattr(oauth_manager.network, "get_sync", get)
    oauth_manager._usage_sync("access", account_key="claude:acct")
    oauth_manager._profile_sync("access", account_key="claude:acct")
    oauth_manager._bootstrap_sync("access")

    usage_headers = calls[0][1]["headers"]
    profile_headers = calls[1][1]["headers"]
    bootstrap_headers = calls[2][1]["headers"]
    assert usage_headers["User-Agent"] == m.CLI_USER_AGENT
    assert usage_headers["anthropic-beta"] == m.OAUTH_BETA
    assert profile_headers["User-Agent"] == m.CLI_USER_AGENT
    assert profile_headers["Cache-Control"] == "no-cache"
    assert "anthropic-beta" not in profile_headers
    assert bootstrap_headers["User-Agent"] == f"claude-code/{m.CC_VERSION}"


def test_application_and_httpx_raw_header_order_and_encoding(dynamic_cch):
    _, payload = _transform("claude-fable-5")
    headers = m.build_upstream_headers(
        "key", auth_scheme="api_key", auth_mode="api_key",
        model="claude-fable-5", payload=payload,
    )
    expected_application = [
        "Accept", "Content-Type", "User-Agent", "X-Claude-Code-Session-Id",
        "X-Stainless-Arch", "X-Stainless-Lang", "X-Stainless-OS",
        "X-Stainless-Package-Version", "X-Stainless-Retry-Count",
        "X-Stainless-Runtime", "X-Stainless-Runtime-Version", "X-Stainless-Timeout",
        "anthropic-beta", "anthropic-dangerous-direct-browser-access",
        "anthropic-version", "x-api-key", "x-app", "x-claude-code-request-class",
        "x-client-request-id",
    ]
    assert list(headers)[:len(expected_application)] == expected_application
    assert headers["Accept-Encoding"] == "gzip, deflate"
    assert "br" not in headers["Accept-Encoding"] and "zstd" not in headers["Accept-Encoding"]
    uuid.UUID(headers["x-client-request-id"])

    raw = httpx.Request(
        "POST", "https://api.anthropic.com/v1/messages",
        headers=headers, content=b"{}",
    ).headers.raw
    names = [name.decode("ascii").lower() for name, _value in raw]
    assert names == [
        "host",
        *(name.lower() for name in expected_application),
        "accept-encoding",
        "content-length",
    ]


def test_physical_dispatch_refreshes_request_id_without_mutating_builder_headers(dynamic_cch):
    headers = m.build_upstream_headers("token", session_id="sid")
    original = dict(headers)
    first = http_runtime._headers_for_physical_dispatch(headers)
    second = http_runtime._headers_for_physical_dispatch(headers)
    assert headers == original
    assert first is not headers and second is not headers
    assert first["x-client-request-id"] != second["x-client-request-id"]
    assert first["x-client-request-id"] != headers["x-client-request-id"]
    assert first["X-Stainless-Retry-Count"] == second["X-Stainless-Retry-Count"] == "0"


def test_non_cc_payload_filter_and_request_id_absence_are_unchanged():
    standard_channel = SimpleNamespace(protocol="anthropic", type="api", cc_mimicry=False)
    payload = provider_registry.filter_request_payload(
        standard_channel,
        {"model": "m", "messages": [], "fallbacks": "must-drop"},
        protocol="anthropic",
    )
    assert "fallbacks" not in payload
    plain = {"Authorization": "Bearer existing"}
    dispatched = http_runtime._headers_for_physical_dispatch(plain)
    assert dispatched == plain and "x-client-request-id" not in dispatched


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
