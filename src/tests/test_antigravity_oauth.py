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
from types import SimpleNamespace
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

    live_unknown = ag.parse_credits({
        "currentTier": {"id": "standard-tier", "name": "Antigravity"},
        "paidTier": {
            "id": "g1-pro-tier",
            "name": "Google AI Pro",
            "description": "Google AI Pro",
            "availableCredits": [{
                "creditType": "GOOGLE_ONE_AI",
                "minimumCreditAmountForUsage": "50",
            }],
        },
    })
    assert live_unknown["known"] is False
    assert live_unknown["tier"] == "g1-pro-tier"
    assert live_unknown["tier_name"] == "Google AI Pro"
    assert live_unknown["minimum_credit_amount"] == 50.0
    assert live_unknown.get("credit_amount") is None

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
    # mock 模式下 quota summary 返回模拟分组，填充标准 5h/7d 窗口
    assert usage["five_hour"]["utilization"] == 25.0
    assert usage["seven_day"]["utilization"] == 10.0
    assert usage["antigravity"].get("quota_groups")

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
    assert gemini["contents"][2]["parts"][0]["functionCall"]["id"] == "call_1"
    assert gemini["contents"][3]["parts"][0]["functionResponse"]["name"] == "lookup"
    assert gemini["contents"][3]["parts"][0]["functionResponse"]["id"] == "call_1"
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


def test_cached_content_tokens_map_to_responses_usage(m):
    codec = m["codec"]
    out = codec.gemini_to_responses({
        "response": {
            "candidates": [{
                "content": {"parts": [{"text": "ok"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 5000,
                "candidatesTokenCount": 8,
                "totalTokenCount": 5008,
                "cachedContentTokenCount": 4500,
            },
        }
    }, model="gemini-3-flash")
    assert out["usage"]["input_tokens"] == 5000
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 4500

    from src.protocols.usage import select_openai_responses_usage
    legacy = select_openai_responses_usage(out).legacy_dict()
    assert legacy["cache_read"] == 4500
    assert legacy["input_tokens"] == 500
    assert legacy["input_tokens"] + legacy["cache_read"] == 5000

    gemini = codec.responses_to_gemini({
        "instructions": "stable prefix",
        "input": [{"type": "message", "role": "user", "content": "same first turn"}],
    })
    first = codec.wrap_cloud_code(gemini, model="gemini-3-flash", project_id="proj-x")
    second = codec.wrap_cloud_code(gemini, model="gemini-3-flash", project_id="proj-x")
    assert first["request"]["sessionId"] == second["request"]["sessionId"]
    other = codec.wrap_cloud_code(
        codec.responses_to_gemini({
            "instructions": "stable prefix",
            "input": [{"type": "message", "role": "user", "content": "different first turn"}],
        }),
        model="gemini-3-flash",
        project_id="proj-x",
    )
    assert other["request"]["sessionId"] != first["request"]["sessionId"]

    pinned = codec.wrap_cloud_code(
        codec.responses_to_gemini({
            "instructions": "stable prefix",
            "input": [{"type": "message", "role": "user", "content": "same first turn"}],
        }),
        model="gemini-3-flash",
        project_id="proj-x",
        session_id="client-session-a",
    )
    pinned_again = codec.wrap_cloud_code(
        codec.responses_to_gemini({
            "instructions": "stable prefix",
            "input": [{"type": "message", "role": "user", "content": "different first turn"}],
        }),
        model="gemini-3-flash",
        project_id="proj-x",
        session_id="client-session-a",
    )
    other_session = codec.wrap_cloud_code(
        codec.responses_to_gemini({
            "instructions": "stable prefix",
            "input": [{"type": "message", "role": "user", "content": "same first turn"}],
        }),
        model="gemini-3-flash",
        project_id="proj-x",
        session_id="client-session-b",
    )
    assert pinned["request"]["sessionId"] == pinned_again["request"]["sessionId"]
    assert pinned["request"]["sessionId"] != first["request"]["sessionId"]
    assert pinned["request"]["sessionId"] != other_session["request"]["sessionId"]
    assert pinned["request"]["sessionId"].startswith("-")


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

    same_key_a = asyncio.run(ch.build_upstream_request({
        "model": "gemini-3.7-flash-high",
        "prompt_cache_key": "conv-1",
        "input": [{"type": "message", "role": "user", "content": "first opening"}],
        "stream": False,
    }, "gemini-3.7-flash-high", ingress_protocol="responses"))
    same_key_b = asyncio.run(ch.build_upstream_request({
        "model": "gemini-3.7-flash-high",
        "prompt_cache_key": "conv-1",
        "input": [{"type": "message", "role": "user", "content": "different opening"}],
        "stream": False,
    }, "gemini-3.7-flash-high", ingress_protocol="responses"))
    other_key = asyncio.run(ch.build_upstream_request({
        "model": "gemini-3.7-flash-high",
        "prompt_cache_key": "conv-2",
        "input": [{"type": "message", "role": "user", "content": "first opening"}],
        "stream": False,
    }, "gemini-3.7-flash-high", ingress_protocol="responses"))
    sid_a = json.loads(same_key_a.body)["request"]["sessionId"]
    sid_b = json.loads(same_key_b.body)["request"]["sessionId"]
    sid_c = json.loads(other_key.body)["request"]["sessionId"]
    assert sid_a == sid_b
    assert sid_a != sid_c


def test_anthropic_thinking_maps_to_gemini_thinking_level(m):
    _setup(m)
    from src.openai.transform import anthropic_to_responses
    from src.openai.transform.guard import GuardError

    thinking_body = {
        "model": "gemini-3-flash",
        "max_tokens": 32,
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(GuardError, match="does not support reasoning.effort"):
        anthropic_to_responses.translate_request(
            thinking_body, target_model="gemini-3-flash",
        )
    mapped = anthropic_to_responses.translate_request(
        thinking_body, target_model="gemini-3-flash", allow_reasoning_effort=True,
    )
    assert mapped["reasoning"]["effort"] == "low"

    om = m["oauth_manager"]
    om.add_account({
        "provider": "antigravity",
        "email": "think@gmail.com",
        "project_id": "proj-think",
        "access_token": "at-think",
        "refresh_token": "rt-think",
        "expired": _future_expired(),
        "models": ["gemini-3-flash", "claude-sonnet-4-6"],
    })
    ch = m["AntigravityOAuthChannel"](om.get_account("antigravity:think@gmail.com:proj-think"))
    import asyncio
    low = asyncio.run(ch.build_upstream_request(thinking_body, "gemini-3-flash", ingress_protocol="anthropic"))
    assert json.loads(low.body)["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
    high = asyncio.run(ch.build_upstream_request({
        **thinking_body,
        "thinking": {"type": "enabled", "budget_tokens": 20000},
    }, "gemini-3-flash", ingress_protocol="anthropic"))
    assert json.loads(high.body)["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"

    claude = asyncio.run(ch.build_upstream_request({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "messages": [{"role": "user", "content": "hi"}],
    }, "claude-sonnet-4-6", ingress_protocol="anthropic"))
    claude_thinking = json.loads(claude.body)["request"]["generationConfig"]["thinkingConfig"]
    assert claude_thinking["thinkingBudget"] == 2048
    assert "thinkingLevel" not in claude_thinking


def test_tool_history_preserves_id_and_thought_signature(m):
    codec = m["codec"]
    restored = codec.gemini_to_responses({
        "candidates": [{
            "content": {"parts": [{
                "thoughtSignature": "native-sig-1234567890ab",
                "functionCall": {"id": "tool-77", "name": "lookup", "args": {"q": "x"}},
            }]},
            "finishReason": "STOP",
        }]
    }, model="gemini-3-flash")
    item = restored["output"][0]
    assert item["call_id"] == "tool-77"
    assert item["encrypted_content"] == "native-sig-1234567890ab"

    replay = codec.responses_to_gemini({
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            item,
            {"type": "function_call_output", "call_id": item["call_id"], "output": "{\"ok\":true}"},
        ]
    })
    call_part = replay["contents"][1]["parts"][0]
    resp_part = replay["contents"][2]["parts"][0]
    assert call_part["functionCall"]["id"] == "tool-77"
    assert call_part["thoughtSignature"] == "native-sig-1234567890ab"
    assert resp_part["functionResponse"]["id"] == "tool-77"

    unsigned = codec.wrap_cloud_code({
        "contents": [{
            "role": "model",
            "parts": [
                {"functionCall": {"id": "a", "name": "first", "args": {}}},
                {"functionCall": {"id": "b", "name": "second", "args": {}}},
            ],
        }]
    }, model="gemini-3-flash", project_id="proj-x")
    parts = unsigned["request"]["contents"][0]["parts"]
    assert parts[0]["thoughtSignature"] == codec.SKIP_THOUGHT_SIGNATURE
    assert "thoughtSignature" not in parts[1]


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
                "tier": "g1-pro-tier",
                "tier_name": "Google AI Pro",
                "credit_amount": 12,
                "minimum_credit_amount": 1,
                "available": True,
            }
        }),
    }, email="cred@gmail.com")
    text = m["oauth_menu"]._format_antigravity_credits_block(ak, detail=True)
    assert "🪙 Credits: 12（最低 1） · 可用" in text
    assert "5h" not in text
    assert "$" not in text
    listing = m["oauth_menu"]._format_account_block(m["oauth_manager"].get_account(ak))
    assert "🏷️ 套餐: <code>Google AI Pro</code>" in listing
    assert "🪙 Credits: 12（最低 1） · 可用" in listing
    assert "💎 月度: <i>暂无本地请求</i>" in listing
    used = m["oauth_menu"]._format_account_block(
        m["oauth_manager"].get_account(ak),
        month_snapshot={
            "by_channel": {
                f"oauth:{ak}": {
                    "total": 3,
                    "success_count": 3,
                    "error_count": 0,
                    "input": 1000,
                    "output": 200,
                    "cache_creation": 0,
                    "cache_read": 400,
                    "avg_tps": 12.5,
                    "max_tps": 30.0,
                    "min_tps": 2.0,
                    "costed_success": 1,
                    "unpriced_success": 0,
                }
            }
        },
    )
    assert "💎 月度:" in used
    assert "⚡ TPS:" in used
    assert "💵" in used
    assert "暂无本地请求" not in used
    detail, _kb = m["oauth_menu"]._detail_text_and_kb(ak, refresh_quota=False)
    assert detail is not None
    assert "🏷️ 套餐: <code>Google AI Pro</code>" in detail
    assert "Project: <code>proj-cred</code>" in detail
    assert "🧬 模型目录:" in detail
    assert "个文本" in detail
    assert "⚡ 本月使用统计" in detail
    assert "暂无本地请求" in detail


def test_refresh_notice_uses_antigravity_credits(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account_if_identity_absent({
        "provider": "antigravity",
        "email": "notice@gmail.com",
        "project_id": "proj-notice",
        "access_token": "at",
        "refresh_token": "rt",
        "expired": _future_expired(),
        "enabled": True,
    })
    ak = "antigravity:notice@gmail.com:proj-notice"

    unknown = om._build_refresh_notice(ak, None, usage={
        "antigravity": {
            "known": False,
            "quota_supported": True,
            "tier": "g1-pro-tier",
            "tier_name": "Google AI Pro",
            "minimum_credit_amount": 50,
        }
    })
    assert "📊" not in unknown
    assert "Credits" not in unknown
    assert "套餐:" not in unknown
    assert "g1-pro-tier" not in unknown
    assert "本次未拉取到" not in unknown
    assert "5h" not in unknown
    assert "7d" not in unknown

    fallback = om._build_refresh_notice(ak, None, usage={
        "antigravity": {
            "known": False,
            "quota_supported": True,
            "tier": "g1-pro-tier",
        }
    })
    assert "📊" not in fallback
    assert "Credits" not in fallback

    known = om._build_refresh_notice(ak, None, usage={
        "antigravity": {
            "known": True,
            "quota_supported": True,
            "tier": "g1-pro-tier",
            "tier_name": "Google AI Pro",
            "credit_amount": 12,
            "minimum_credit_amount": 1,
            "available": True,
        }
    })
    assert "📊 Credits: 12（最低 1） · 可用" in known
    assert "套餐:" not in known
    assert "本次未拉取到" not in known

    failed = om._build_refresh_notice(ak, None)
    assert "📊" not in failed
    assert "获取失败" not in failed
    assert "本次未拉取到" not in failed


def test_cpa_type_field_imports_as_antigravity(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "type": "antigravity",
        "email": "cpa@gmail.com",
        "project_id": "proj-cpa",
        "access_token": "at-cpa",
        "refresh_token": "rt-cpa",
        "expired": _future_expired(),
        "disabled": False,
    })
    acc = om.get_account("antigravity:cpa@gmail.com:proj-cpa")
    assert acc is not None
    assert acc["provider"] == "antigravity"
    assert acc["project_id"] == "proj-cpa"
    assert om.provider_of(acc) == "antigravity"


def test_image_inline_data_restores_as_output_image(m):
    codec = m["codec"]
    restored = codec.gemini_to_responses({
        "candidates": [{
            "content": {"parts": [
                {"text": "here"},
                {"inlineData": {"mimeType": "image/png", "data": "AAA"}},
            ]},
            "finishReason": "STOP",
        }],
    }, model="gemini-3.1-flash-image")
    kinds = []
    urls = []
    for item in restored["output"]:
        for part in item.get("content") or []:
            kinds.append(part.get("type"))
            if part.get("type") == "output_image":
                urls.append(part.get("image_url"))
    assert kinds == ["output_text", "output_image"]
    assert urls == ["data:image/png;base64,AAA"]
    assert restored["output_text"] == "here"


def test_claude_tool_schema_is_sanitized(m):
    from src.providers import antigravity_schema

    cleaned = antigravity_schema.clean_tool_schema({
        "type": "object",
        "title": "Lookup",
        "$schema": "https://json-schema.org/draft/07/schema#",
        "additionalProperties": False,
        "properties": {
            "q": {"type": "string", "format": "email", "minLength": 2, "enum": ["a", "b"]},
            "flag": {"const": True},
        },
        "anyOf": [{"type": "object"}, {"type": "null"}],
    }, require_placeholder=True)
    assert "$schema" not in cleaned
    assert "additionalProperties" not in cleaned
    assert "enum" not in cleaned["properties"]["q"]
    assert "format" not in cleaned["properties"]["q"]
    assert "const" not in cleaned["properties"]["flag"]
    assert "Allowed:" in cleaned["properties"]["q"]["description"]

    empty = antigravity_schema.clean_tool_schema({"type": "object"}, require_placeholder=True)
    assert empty["properties"]["reason"]["type"] == "string"
    assert empty["required"] == ["reason"]

    env = m["codec"].wrap_cloud_code({
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": [{
            "functionDeclarations": [{
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string", "format": "uuid"}},
                    "additionalProperties": False,
                },
            }],
        }],
    }, model="claude-sonnet-4-6", project_id="proj-x")
    params = env["request"]["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in params
    assert "format" not in params["properties"]["q"]


def test_anthropic_thinking_history_replays_as_thought(m):
    from src.openai.transform import anthropic_to_responses
    from src.openai.transform.guard import GuardError

    history = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096,
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "plan", "signature": "sig-history-1234567890"},
                {"type": "text", "text": "ok"},
            ]},
            {"role": "user", "content": "again"},
        ],
    }
    with pytest.raises(GuardError, match="thinking/redacted_thinking"):
        anthropic_to_responses.translate_request(history, target_model="claude-sonnet-4-6")
    mapped = anthropic_to_responses.translate_request(
        history, target_model="claude-sonnet-4-6", allow_reasoning_effort=True,
    )
    assert mapped["reasoning"]["budget_tokens"] == 2048
    kinds = [item.get("type") for item in mapped["input"]]
    assert "reasoning" in kinds
    thought = next(item for item in mapped["input"] if item["type"] == "reasoning")
    assert thought["encrypted_content"] == "sig-history-1234567890"

    gemini = m["codec"].responses_to_gemini({**mapped, "model": "claude-sonnet-4-6"})
    thought_part = gemini["contents"][1]["parts"][0]
    assert thought_part["thought"] is True
    assert thought_part["thoughtSignature"] == "sig-history-1234567890"
    assert thought_part["text"] == "plan"


def test_input_file_becomes_inline_data(m):
    gemini = m["codec"].responses_to_gemini({
        "input": [{
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "read this"},
                {"type": "input_file", "filename": "note.pdf", "file_data": "JVBERi0x"},
            ],
        }],
    })
    parts = gemini["contents"][0]["parts"]
    assert parts[0]["text"] == "read this"
    assert parts[1]["inlineData"]["mimeType"] == "application/pdf"
    assert parts[1]["inlineData"]["data"] == "JVBERi0x"


def test_antigravity_429_reads_retry_delay_and_quota_reason(m):
    from src.failover import _attach_retry_after_from_response
    from src.protocols.runtime import AttemptResult, bounded_account_quota_error
    from src.providers.antigravity_errors import parse_antigravity_429

    rate = parse_antigravity_429({
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "1.5s"},
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"},
            ],
        }
    })
    assert rate["retry_after"] == 1.5
    assert rate["quota_exhausted"] is False

    quota = parse_antigravity_429({
        "error": {
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "QUOTA_EXHAUSTED"},
            ],
        }
    })
    assert quota["quota_exhausted"] is True

    result = AttemptResult(
        outcome="http_error",
        http_status=429,
        full_response_text=json.dumps({
            "error": {
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"},
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"},
                ],
            }
        }),
    )
    attached = _attach_retry_after_from_response(
        result, None, SimpleNamespace(provider="antigravity"),
    )
    assert attached.retry_after_seconds == 12
    assert attached.cooldown_until is not None

    exhausted = AttemptResult(
        outcome="http_error",
        http_status=429,
        error_code="QUOTA_EXHAUSTED",
        error_detail="INSUFFICIENT_G1_CREDITS_BALANCE",
    )
    classified = bounded_account_quota_error(exhausted)
    assert classified is not None
    assert classified["classification"] == "quota_exhausted"


def _add_ag_account(om, email="state@gmail.com", project="proj-state", **extra):
    account = {
        "provider": "antigravity", "email": email, "project_id": project,
        "access_token": "at", "refresh_token": "rt", "expired": _future_expired(),
        "enabled": True,
    }
    account.update(extra)
    om.add_account(account)
    return f"antigravity:{email}:{project}"


def test_antigravity_windows_and_credits_share_quota_state_machine(m):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om)
    exhausted_window = {
        "antigravity": {"known": False, "quota_supported": True},
        "five_hour": {"utilization": 96, "resets_at": "2099-01-01T00:00:00Z"},
        "seven_day": {"utilization": 20, "resets_at": "2099-01-07T00:00:00Z"},
    }
    first = om.evaluate_and_toggle_by_usage(ak, exhausted_window, threshold=95, fresh=True)
    assert first["action"] == "disabled"
    assert first["hit_windows"] == ["5h"]
    assert first["disabled_until"] == "2099-01-01T00:00:00Z"
    assert om.get_account(ak)["disabled_reason"] == "quota"
    assert om.evaluate_and_toggle_by_usage(ak, exhausted_window, threshold=95)["action"] == "still_over_quota"

    # Credits success is not proof that an independently exhausted 5h/weekly
    # window recovered when retrieveUserQuotaSummary failed in this refresh.
    partial = {
        "antigravity": {
            "known": True,
            "available": True,
            "quota_error": {"kind": "server_error", "http_status": 503},
        },
    }
    assert om.evaluate_and_toggle_by_usage(
        ak, partial, threshold=95, fresh=True,
    )["action"] == "quota_partial_keep_disabled"
    assert om.get_account(ak)["enabled"] is False

    recovered = {
        "antigravity": {"known": False, "quota_supported": True},
        "five_hour": {"utilization": 5}, "seven_day": {"utilization": 10},
    }
    assert om.evaluate_and_toggle_by_usage(ak, recovered, threshold=95, fresh=False)["action"] == "quota_stale_keep_disabled"
    assert om.evaluate_and_toggle_by_usage(ak, recovered, threshold=95, fresh=True)["action"] == "resumed"
    assert om.get_account(ak)["enabled"] is True

    # Either reliable source can gate routing; an available Credits pool cannot
    # override an exhausted window, and low windows cannot override exhausted Credits.
    over_with_credits = {**exhausted_window, "antigravity": {"known": True, "available": True}}
    assert om.evaluate_and_toggle_by_usage(ak, over_with_credits, threshold=95)["action"] == "disabled"
    low_exhausted_credits = {**recovered, "antigravity": {"known": True, "available": False}}
    result = om.evaluate_and_toggle_by_usage(ak, low_exhausted_credits, threshold=95)
    assert result["action"] == "still_over_quota"
    assert result["hit_windows"] == ["Credits"]
    assert result["disabled_until"] == "2099-01-01T00:00:00Z"  # existing target is not moved


def test_antigravity_manual_disable_is_never_overwritten(m):
    _setup(m)
    om = m["oauth_manager"]
    ak = _add_ag_account(om, email="manual@gmail.com", project="manual", enabled=False,
                         disabled_reason="user")
    available = {"antigravity": {"known": True, "available": True},
                 "five_hour": {"utilization": 0}, "seven_day": {"utilization": 0}}
    assert om.evaluate_and_toggle_by_usage(ak, available, fresh=True)["action"] == "noop_user"
    assert om.get_account(ak)["enabled"] is False
    assert om.get_account(ak)["disabled_reason"] == "user"


class _QuotaResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _QuotaHttpError(Exception):
    def __init__(self, status_code, payload):
        super().__init__(f"HTTP {status_code}")
        self.response = _QuotaResponse(status_code, payload)


@pytest.mark.parametrize("status,kind", [
    (401, "unauthorized"), (403, "forbidden"), (429, "rate_limited"), (503, "server_error"),
])
def test_quota_summary_http_errors_are_structured(m, status, kind):
    error = m["ag"]._safe_quota_error(_QuotaHttpError(status, {
        "error": {"code": status, "status": "PERMISSION_DENIED", "message": "safe message"}
    }))
    assert error["kind"] == kind
    assert error["http_status"] == status
    assert error["message"] == "safe message"


def test_quota_summary_validation_timeout_and_network_are_safe(m):
    validation = m["ag"]._safe_quota_error(_QuotaHttpError(403, {"error": {
        "code": 403, "status": "PERMISSION_DENIED", "message": "VALIDATION_REQUIRED https://secret.example/token",
        "details": [{"reason": "VALIDATION_REQUIRED", "metadata": {"validation_url": "https://secret.example/token"}}],
    }}))
    assert validation["kind"] == "validation_required"
    assert validation["validation_required"] is True
    assert validation["hint"]
    assert "secret.example" not in json.dumps(validation)
    assert m["ag"]._safe_quota_error(TimeoutError("slow"))["kind"] == "timeout"
    assert m["ag"]._safe_quota_error(ConnectionError("offline"))["kind"] == "network"


def test_antigravity_partial_cache_and_error_ui(m):
    _setup(m)
    om, db, menu = m["oauth_manager"], m["state_db"], m["oauth_menu"]
    ak = _add_ag_account(om, email="partial@gmail.com", project="partial")
    groups = [{"display_name": "Gemini", "buckets": [{
        "window": "5h", "remaining_fraction": .5, "reset_time": "2099-01-01T00:00:00Z"
    }]}]
    old = {"antigravity": {"known": False, "quota_groups": groups}}
    db.quota_save(ak, om.flatten_usage(old), email="partial@gmail.com")
    partial = {"antigravity": {"known": True, "available": True,
                                "quota_error": {"kind": "rate_limited", "http_status": 429}}}
    merged = om.preserve_antigravity_cached_summary(ak, partial)
    assert merged["antigravity"]["quota_groups"] == groups
    assert merged["antigravity"]["quota_groups_stale"] is True
    db.quota_save(ak, om.flatten_usage(merged), email="partial@gmail.com")
    detail = menu._format_antigravity_credits_block(ak, detail=True)
    assert "旧数据已保留" in detail
    assert "429" in detail
    assert "403/校验" not in detail


def test_antigravity_telegram_list_and_detail_stay_within_limit(m):
    _setup(m)
    om, db, menu = m["oauth_manager"], m["state_db"], m["oauth_menu"]
    groups = []
    for gi in range(18):
        groups.append({
            "display_name": f"Upstream group {gi} " + ("模型" * 24),
            "buckets": [{
                "window": window, "remaining_fraction": .42,
                "reset_time": "2099-01-01T00:00:00Z",
            } for window in ("5h", "weekly", "daily")],
        })
    keys = []
    for idx in range(4):
        ak = _add_ag_account(om, email=f"boundary{idx}@gmail.com", project=f"p{idx}")
        keys.append(ak)
        usage = {"antigravity": {"known": True, "available": True,
                                  "tier_name": "Google AI Pro", "credit_amount": 10,
                                  "minimum_credit_amount": 1, "quota_groups": groups}}
        db.quota_save(ak, om.flatten_usage(usage), email=f"boundary{idx}@gmail.com")
    list_text, _ = menu._list_text_and_kb(page=1, month_snapshot={"by_channel": {}})
    detail_text, _ = menu._detail_text_and_kb(keys[0], refresh_quota=False,
                                               month_snapshot={"by_channel": {}})
    assert len(list_text) <= m["ui"].TG_MSG_LIMIT
    assert len(detail_text) <= m["ui"].TG_MSG_LIMIT
    assert "其余配额组已省略" in list_text
    assert "其余配额组已省略" in detail_text


@pytest.mark.parametrize("usage,expected,unexpected", [
    ({"antigravity": {"known": True, "available": True, "quota_groups": [
        {"display_name": "Gemini", "buckets": [{"window": "5h", "remaining_fraction": .8}]}
    ]}}, "均已更新", "部分成功"),
    ({"antigravity": {"known": True, "available": True,
                       "quota_error": {"kind": "server_error", "http_status": 503}}},
     "部分成功", "均已更新"),
    ({"antigravity": {"known": False,
                       "quota_error": {"kind": "timeout"}}},
     "未获取到有效 quota 数据", "✅"),
])
def test_antigravity_manual_refresh_feedback(m, monkeypatch, usage, expected, unexpected):
    _setup(m)
    om, menu = m["oauth_manager"], m["oauth_menu"]
    ak = _add_ag_account(om, email="refresh@gmail.com", project="refresh")
    recorder = _ApiRecorder()
    m["ui"].api = recorder
    monkeypatch.setattr(menu, "_account_key_from_short", lambda _short: ak)
    monkeypatch.setattr(menu, "_fetch_and_save_usage_result_sync",
                        lambda *_args, **_kwargs: {"usage": usage})
    monkeypatch.setattr(menu, "_detail_text_and_kb",
                        lambda *_args, **_kwargs: ("DETAIL", {"inline_keyboard": []}))
    menu.on_refresh_usage(1, 10, "cb", "short")
    rendered = (recorder.last("editMessageText") or {}).get("text", "")
    assert expected in rendered
    assert unexpected not in rendered
    assert "403/校验要求" not in rendered


def test_quota_summary_failure_does_not_break_credits_success(m, monkeypatch):
    ag = m["ag"]
    monkeypatch.setattr(ag, "load_code_assist_sync", lambda _token: {"paidTier": {
        "id": "pro", "availableCredits": [{"creditType": "GOOGLE_ONE_AI",
        "creditAmount": "12", "minimumCreditAmountForUsage": "1"}],
    }})
    monkeypatch.setattr(ag, "fetch_quota_summary_sync",
                        lambda _token: (_ for _ in ()).throw(TimeoutError("slow endpoint")))
    usage = ag.fetch_usage_sync("token-not-logged")
    assert usage["antigravity"]["known"] is True
    assert usage["antigravity"]["available"] is True
    assert usage["antigravity"]["quota_error"]["kind"] == "timeout"
    assert not usage["five_hour"]
    assert not usage["seven_day"]


def test_quota_error_ui_distinguishes_validation_and_http_classes(m):
    menu = m["oauth_menu"]
    assert "Google 账号验证" in menu._ag_quota_error_html({"kind": "validation_required"})
    assert "401" in menu._ag_quota_error_html({"kind": "unauthorized"})
    assert "403" in menu._ag_quota_error_html({"kind": "forbidden"})
    assert "429" in menu._ag_quota_error_html({"kind": "rate_limited"})
    assert "5xx" in menu._ag_quota_error_html({"kind": "server_error"})
    assert "超时" in menu._ag_quota_error_html({"kind": "timeout"})
    assert "网络" in menu._ag_quota_error_html({"kind": "network"})
