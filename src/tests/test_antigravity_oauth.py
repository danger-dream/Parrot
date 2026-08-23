"""Antigravity OAuth identity, login helpers, manager dispatch and credits."""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, oauth_manager, state_db
    from src import network_monitor
    from src.oauth import VALID_PROVIDERS, normalize_provider
    from src.oauth import antigravity as ag
    from src import oauth_ids
    from src.channel import registry
    from src.channel.antigravity_oauth_channel import AntigravityOAuthChannel
    from src.providers import registry as provider_registry
    from src.providers import antigravity_codec as codec
    from src.telegram import states, ui
    from src.telegram.menus import oauth_defaults_menu, oauth_menu
    return {
        "config": config,
        "oauth_manager": oauth_manager,
        "state_db": state_db,
        "network_monitor": network_monitor,
        "ag": ag,
        "oauth_ids": oauth_ids,
        "VALID_PROVIDERS": VALID_PROVIDERS,
        "normalize_provider": normalize_provider,
        "registry": registry,
        "AntigravityOAuthChannel": AntigravityOAuthChannel,
        "provider_registry": provider_registry,
        "codec": codec,
        "states": states,
        "ui": ui,
        "oauth_defaults_menu": oauth_defaults_menu,
        "oauth_menu": oauth_menu,
    }


def _setup(m):
    m["state_db"].init()

    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
        c["channels"] = []
        c["antigravityOAuth"] = dict(m["config"].DEFAULT_CONFIG.get("antigravityOAuth") or {})

    m["config"].update(_reset)


def _future_expired(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_antigravity_is_registered_provider(m):
    assert "antigravity" in m["VALID_PROVIDERS"]
    assert m["normalize_provider"]("antigravity") == "antigravity"
    assert m["normalize_provider"]("unknown-provider") == "claude"


def test_login_url_is_google_auth_code_without_pkce(m):
    _setup(m)
    url = m["ag"].build_login_url("STATE-1")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    assert parsed.path == "/o/oauth2/v2/auth"
    q = parse_qs(parsed.query)
    assert q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["state"] == ["STATE-1"]
    assert q["redirect_uri"] == ["http://localhost:51121/oauth-callback"]
    assert "code_challenge" not in q
    assert "code_challenge_method" not in q
    assert "cloud-platform" in q["scope"][0]
    assert q["client_id"][0].endswith(".apps.googleusercontent.com")


def test_parse_callback_url_and_mock_complete_login(m):
    _setup(m)
    ag = m["ag"]
    parsed = ag.parse_callback_url(
        "http://localhost:51121/oauth-callback?state=STATE-1&code=AUTH-CODE"
    )
    assert parsed["code"] == "AUTH-CODE"
    assert parsed["state"] == "STATE-1"

    with pytest.raises(ValueError, match="missing code"):
        ag.parse_callback_url("http://localhost:51121/oauth-callback?state=x")
    with pytest.raises(ValueError, match="denied"):
        ag.parse_callback_url(
            "http://localhost:51121/oauth-callback?error=access_denied&error_description=nope"
        )

    tok = ag.complete_login_sync("AUTH-CODE")
    assert tok["access_token"].startswith("mock-antigravity-access-")
    assert tok["refresh_token"].startswith("mock-antigravity-refresh-")
    assert "@" in tok["email"]
    assert tok["project_id"]
    assert tok["base_url"] == "https://cloudcode-pa.googleapis.com"


def test_account_key_requires_project_and_keeps_projects_separate(m):
    _setup(m)
    om = m["oauth_manager"]
    ids = m["oauth_ids"]

    with pytest.raises(ValueError, match="project_id"):
        om.add_account({
            "provider": "antigravity",
            "email": "a@gmail.com",
            "access_token": "at",
            "refresh_token": "rt",
            "expired": _future_expired(),
        })

    om.add_account({
        "provider": "antigravity",
        "email": "a@gmail.com",
        "project_id": "proj-1",
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expired": _future_expired(),
    })
    om.add_account({
        "provider": "antigravity",
        "email": "a@gmail.com",
        "project_id": "proj-2",
        "access_token": "at-2",
        "refresh_token": "rt-2",
        "expired": _future_expired(),
    })
    om.add_account({
        "provider": "claude",
        "email": "a@gmail.com",
        "access_token": "at-c",
        "refresh_token": "rt-c",
        "expired": _future_expired(),
    })

    accounts = om.list_accounts()
    keys = sorted(ids.account_key(acc) for acc in accounts)
    assert keys == [
        "antigravity:a@gmail.com:proj-1",
        "antigravity:a@gmail.com:proj-2",
        "claude:a@gmail.com",
    ]
    assert ids.is_account_key("antigravity:a@gmail.com:proj-1")
    assert om.provider_of("antigravity:a@gmail.com:proj-1") == "antigravity"
    assert om.account_key_to_email("antigravity:a@gmail.com:proj-1") == "a@gmail.com"

    om.add_account({
        "provider": "antigravity",
        "email": "a@gmail.com",
        "project_id": "proj-1",
        "access_token": "at-1-new",
        "refresh_token": "rt-1-new",
        "expired": _future_expired(),
    })
    accounts = om.list_accounts()
    assert len(accounts) == 3
    updated = next(a for a in accounts if a.get("project_id") == "proj-1")
    assert updated["access_token"] == "at-1-new"
    assert updated["project_id"] == "proj-1"


def test_refresh_does_not_rewrite_project_id(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "provider": "antigravity",
        "email": "keep@gmail.com",
        "project_id": "proj-keep",
        "access_token": "at-old",
        "refresh_token": "rt-old",
        "expired": _future_expired(60),
    })
    ak = "antigravity:keep@gmail.com:proj-keep"
    token = om._refresh_sync_locked(ak, True)
    assert token.startswith("mock-antigravity-access-")
    acc = om.get_account(ak)
    assert acc["project_id"] == "proj-keep"
    assert acc["email"] == "keep@gmail.com"
    assert om._canonical_key(acc) == ak


def test_credits_unknown_is_not_zero_and_does_not_resume(m):
    _setup(m)
    ag = m["ag"]
    parsed = ag.parse_credits({"paidTier": {"id": "pro"}})
    assert parsed["known"] is False
    assert parsed["tier"] == "pro"

    parsed = ag.parse_credits({
        "paidTier": {
            "id": "pro",
            "availableCredits": [{
                "creditType": "GOOGLE_ONE_AI",
                "creditAmount": "12.5",
                "minimumCreditAmountForUsage": "1",
            }],
        }
    })
    assert parsed["known"] is True
    assert parsed["available"] is True
    assert parsed["credit_amount"] == 12.5
    assert parsed["minimum_credit_amount"] == 1.0

    exhausted = ag.parse_credits({
        "paidTier": {
            "id": "pro",
            "availableCredits": [{
                "creditType": "GOOGLE_ONE_AI",
                "creditAmount": "0",
                "minimumCreditAmountForUsage": "1",
            }],
        }
    })
    assert exhausted["known"] is True
    assert exhausted["available"] is False

    om = m["oauth_manager"]
    om.add_account({
        "provider": "antigravity",
        "email": "q@gmail.com",
        "project_id": "proj-q",
        "access_token": "at",
        "refresh_token": "rt",
        "expired": _future_expired(),
        "enabled": False,
        "disabled_reason": "quota",
    })
    ak = "antigravity:q@gmail.com:proj-q"
    result = om.evaluate_and_toggle_by_usage(
        ak,
        {"antigravity": {"known": False, "quota_supported": True}},
        fresh=True,
    )
    assert result["action"] == "quota_unknown_keep_disabled"
    acc = om.get_account(ak)
    assert acc["disabled_reason"] == "quota"
    assert acc["enabled"] is False


def test_mock_usage_and_fetch_dispatch(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "provider": "antigravity",
        "email": "u@gmail.com",
        "project_id": "proj-u",
        "access_token": "at",
        "refresh_token": "rt",
        "expired": _future_expired(),
    })
    usage = m["ag"].fetch_usage_sync("at")
    assert usage["antigravity"]["known"] is True
    assert usage["antigravity"]["available"] is True
    assert usage["five_hour"] == {}

    import asyncio
    fetched = asyncio.run(om.fetch_usage("antigravity:u@gmail.com:proj-u"))
    assert fetched["antigravity"]["quota_supported"] is True


def test_responses_to_gemini_multiturn_and_tools(m):
    codec = m["codec"]
    gemini = codec.responses_to_gemini({
        "instructions": "be brief",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{\"q\":\"x\"}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "{\"ok\":true}"},
            {"type": "message", "role": "user", "content": "again"},
        ],
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}],
        "temperature": 0.2,
        "max_output_tokens": 128,
    })
    assert gemini["systemInstruction"]["parts"][0]["text"] == "be brief"
    roles = [c["role"] for c in gemini["contents"]]
    assert roles == ["user", "model", "model", "user", "user"]
    assert gemini["contents"][2]["parts"][0]["functionCall"]["name"] == "lookup"
    assert gemini["contents"][3]["parts"][0]["functionResponse"]["name"] == "lookup"
    assert gemini["tools"][0]["functionDeclarations"][0]["name"] == "lookup"
    assert gemini["generationConfig"]["temperature"] == 0.2
    assert gemini["generationConfig"]["maxOutputTokens"] == 128


def test_envelope_and_claude_tool_mode(m):
    codec = m["codec"]
    gemini = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "generationConfig": {"maxOutputTokens": 99}}
    env = codec.wrap_cloud_code(gemini, model="gemini-3.7-flash-high", project_id="proj-x")
    assert env["project"] == "proj-x"
    assert env["model"] == "gemini-3.7-flash-high"
    assert env["userAgent"] == "antigravity"
    assert env["requestType"] == "agent"
    assert env["requestId"].startswith("agent-")
    assert env["request"]["sessionId"].startswith("-")
    assert "maxOutputTokens" not in (env["request"].get("generationConfig") or {})

    image = codec.wrap_cloud_code(gemini, model="gemini-3.1-flash-image", project_id="proj-x")
    assert image["requestType"] == "image_gen"
    assert image["requestId"].startswith("image_gen/")

    claude = codec.wrap_cloud_code(
        {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "tools": [{"functionDeclarations": [{"name": "x"}]}]},
        model="claude-sonnet-4-6",
        project_id="proj-x",
    )
    assert claude["request"]["toolConfig"]["functionCallingConfig"]["mode"] == "VALIDATED"


def test_gemini_json_and_sse_restore(m):
    codec = m["codec"]
    payload = {
        "response": {
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "hello world"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
        }
    }
    out = codec.gemini_to_responses(payload, model="gemini-3.7-flash-high")
    assert out["status"] == "completed"
    assert out["output_text"] == "hello world"
    assert out["usage"]["input_tokens"] == 3
    assert out["output"][0]["type"] == "message"

    converter = codec.GeminiStreamToResponses(model="gemini-3.7-flash-high")
    first = converter.feed(b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"Hel"}]}}]}}\n\n')
    second = converter.feed(b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"Hello"}]},"finishReason":"STOP"}]}}\n\n')
    text = (first + second).decode()
    assert "event: response.created" in text
    assert '"delta":"Hel"' in text
    assert '"delta":"lo"' in text
    assert "event: response.completed" in text

    incremental = codec.GeminiStreamToResponses(model="gemini-3.7-flash-high")
    a = incremental.feed(b'data: {"candidates":[{"content":{"parts":[{"text":"A"}]}}]}\n\n')
    b = incremental.feed(b'data: {"candidates":[{"content":{"parts":[{"text":"B"}]},"finishReason":"STOP"}]}\n\n')
    both = (a + b).decode()
    assert '"delta":"A"' in both
    assert '"delta":"B"' in both


def test_function_call_roundtrip_and_channel_envelope(m):
    _setup(m)
    codec = m["codec"]
    om = m["oauth_manager"]
    om.add_account({
        "provider": "antigravity",
        "email": "ch@gmail.com",
        "project_id": "proj-ch",
        "access_token": "at-live",
        "refresh_token": "rt-live",
        "expired": _future_expired(),
        "models": ["gemini-3.7-flash-high"],
    })
    m["registry"].rebuild_from_config()
    channels = m["registry"].list_channels() if hasattr(m["registry"], "list_channels") else None
    ch = None
    if channels:
        for item in channels:
            if getattr(item, "provider", "") == "antigravity":
                ch = item
                break
    if ch is None:
        get_all = getattr(m["registry"], "all_channels", None) or getattr(m["registry"], "get_all", None)
        if callable(get_all):
            for item in get_all():
                if getattr(item, "provider", "") == "antigravity":
                    ch = item
                    break
    if ch is None:
        # Fallback: construct directly like xAI tests after add_account.
        acc = om.get_account("antigravity:ch@gmail.com:proj-ch")
        ch = m["AntigravityOAuthChannel"](acc)

    assert m["provider_registry"].adapter_for_channel(ch).name == "antigravity-oauth"
    import asyncio
    req = asyncio.run(ch.build_upstream_request({
        "model": "gemini-3.7-flash-high",
        "input": [{"type": "message", "role": "user", "content": "ping"}],
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        "stream": False,
    }, "gemini-3.7-flash-high", ingress_protocol="responses"))
    body = json.loads(req.body)
    assert body["project"] == "proj-ch"
    assert body["model"] == "gemini-3.7-flash-high"
    assert body["requestType"] == "agent"
    assert body["request"]["contents"][0]["parts"][0]["text"] == "ping"
    assert "generateContent" in req.url
    assert "streamGenerateContent" not in req.url
    assert req.url.startswith("https://daily-cloudcode-pa.googleapis.com/")
    assert req.headers["authorization"] == "Bearer at-live"
    assert req.translator_ctx["antigravity_stream"] is not None

    gemini_fc = {
        "candidates": [{
            "content": {"parts": [{"functionCall": {"name": "lookup", "args": {"q": "x"}}}]},
            "finishReason": "STOP",
        }]
    }
    restored = codec.gemini_to_responses(gemini_fc, model="gemini-3.7-flash-high")
    assert restored["output"][0]["type"] == "function_call"
    assert restored["output"][0]["name"] == "lookup"
    assert json.loads(restored["output"][0]["arguments"]) == {"q": "x"}


class _ApiRecorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {"message_id": 123}}

    def by(self, method):
        return [d for m, d in self.calls if m == method]

    def last(self, method):
        items = self.by(method)
        return items[-1] if items else None


def test_antigravity_probe_url_is_not_codex(m):
    ch = type("Ch", (), {
        "type": "oauth",
        "provider": "antigravity",
        "protocol": "openai-responses",
    })()
    url = m["network_monitor"]._channel_probe_url(ch)
    assert url == "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"


def test_antigravity_defaults_menu_family(m):
    _setup(m)
    odm = m["oauth_defaults_menu"]
    models = odm._read_list("antigravity")
    assert "gemini-3.7-flash-high" in models
    assert "gemini-3.1-flash-image" not in models
    static = odm._static_models("antigravity")
    assert "claude-sonnet-4-6" in static
    assert "gpt-oss-120b-medium" in static
    odm._write_list("antigravity", ["gemini-3.7-flash-high"])
    assert odm._read_list("antigravity") == ["gemini-3.7-flash-high"]


def test_antigravity_image_model_is_media_only(m):
    _setup(m)
    ch = m["AntigravityOAuthChannel"]({
        "provider": "antigravity",
        "email": "img@gmail.com",
        "project_id": "proj-img",
        "access_token": "at",
        "refresh_token": "rt",
        "expired": _future_expired(),
        "models": [],
    })
    assert ch.supports_model("gemini-3.1-flash-image") is None
    assert ch.supports_media_model("image", "gemini-3.1-flash-image") is True
    assert ch.supports_model("gemini-3.7-flash-high") == "gemini-3.7-flash-high"


def test_antigravity_tg_login_persists_project_identity(m):
    _setup(m)
    m["states"].clear_all()
    rec = _ApiRecorder()
    m["ui"].api = rec
    menu = m["oauth_menu"]
    om = m["oauth_manager"]

    menu.on_add_menu(1, 10, "cb")
    rendered = json.dumps(rec.last("editMessageText") or {}, ensure_ascii=False)
    assert "Antigravity" in rendered

    menu.on_login_antigravity_start(1, 10, "cb")
    st = m["states"].get_state(1)
    assert st and st["action"] == "oa_antigravity_code"
    url_text = (rec.last("editMessageText") or {}).get("text", "")
    assert "accounts.google.com" in url_text
    state = (st.get("data") or {}).get("state")
    menu.on_login_antigravity_code_input(
        1, f"http://localhost:51121/oauth-callback?code=abc&state={state}",
    )
    accounts = [acc for acc in om.list_accounts() if om.provider_of(acc) == "antigravity"]
    assert len(accounts) == 1
    assert accounts[0]["project_id"]
    assert m["oauth_ids"].account_key(accounts[0]).startswith("antigravity:")
    assert "Antigravity OAuth 账户已" in (rec.last("sendMessage") or {}).get("text", "")


def test_antigravity_credits_block_does_not_invent_percent(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account_if_identity_absent({
        "provider": "antigravity",
        "email": "cred@gmail.com",
        "project_id": "proj-cred",
        "access_token": "at",
        "refresh_token": "rt",
        "expired": _future_expired(),
        "enabled": True,
    })
    ak = "antigravity:cred@gmail.com:proj-cred"
    m["state_db"].quota_save(ak, {
        "fetched_at": 1,
        "raw_data": json.dumps({
            "antigravity": {
                "known": True,
                "quota_supported": True,
                "tier": "GOOGLE_ONE_AI",
                "credit_amount": 12,
                "minimum_credit_amount": 1,
                "available": True,
            }
        }),
    }, email="cred@gmail.com")
    text = m["oauth_menu"]._format_antigravity_credits_block(ak, detail=True)
    assert "Credits: 12（最低 1）· 可用" in text
    assert "5h" not in text
    assert "$" not in text
    assert "Project: <code>proj-cred</code>" in text
