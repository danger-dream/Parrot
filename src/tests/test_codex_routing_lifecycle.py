"""Regression coverage for reviewed Codex routing and credential lifecycle bugs."""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from src import config, oauth_manager, channel_state, failover
from src.channel.openai_oauth_channel import OpenAIOAuthChannel
from src.oauth import openai
from src.openai import codex_constants as c, responses_ws, realtime
from src.tests.test_codex_upgrade_regressions import isolated_upgrade_state
from src.transports.ws_runtime import RejectRedirectConnect


ROUTE = {"workspace_backend_origin": "https://gov.chatgpt.com", "account_routing_override": "us_cr"}
URL = "https://chatgpt.com/backend-api/codex/responses"


def add_account(**extra):
    config.update(lambda cfg: cfg.update(oauthAccounts=[], oauth={"mockMode": True}))
    entry = {
        "provider": "openai", "email": "route@example.test",
        "chatgpt_account_id": "review-route-ws", "access_token": "old-access",
        "refresh_token": "old-refresh", "expired": "2999-01-01T00:00:00Z",
        "models": ["gpt-5.5"], **ROUTE, **extra,
    }
    oauth_manager.add_account(entry)
    return "openai:route@example.test:review-route-ws"


@pytest.mark.parametrize("origin", ["https://[bad", "https://gov.chatgpt.com:bad", "https://gov.chatgpt.com:99999", "https://gov.chatgpt.com:0", "https://user:pass@gov.chatgpt.com", "https://gov.chatgpt.com\n", "http://gov.chatgpt.com"])
def test_invalid_origin_does_not_throw_or_route(origin):
    # Whitespace surrounding a URL is intentionally normalized; embedded controls aren't.
    if origin.endswith("\n"):
        origin = "https://gov.chat\ngpt.com"
    account = {**ROUTE, "workspace_backend_origin": origin}
    assert c.codex_workspace_routing(account) is None
    assert c.codex_workspace_routing_patch(account) == {}
    assert c.apply_codex_workspace_routing(URL, {}, account) == (URL, {})


def test_no_constraint_origin_keeps_default_and_independent_relays_win():
    route = {**ROUTE, "workspace_backend_origin": "NO_CONSTRAINT"}
    url, headers = c.apply_codex_workspace_routing(URL, {}, route)
    assert url == URL and headers[c.CODEX_ACCOUNT_ROUTING_OVERRIDE_HEADER] == "us_cr"
    for selected in (ROUTE, route):
        relay = "https://relay.example/custom/responses?q=1"
        assert c.apply_codex_workspace_routing(relay, {}, selected) == (relay, {})
        assert not c.codex_workspace_route_applies(relay, selected)
    assert c.codex_workspace_route_applies(URL, {**ROUTE, "account_routing_override": "NO_CONSTRAINT"})


@pytest.mark.parametrize("routing, expected", [
    ({}, {}),
    ({"workspace_backend_origin": None}, {}),
    ({"workspace_backend_origin": None, "account_routing_override": None}, {"workspace_backend_origin": "", "account_routing_override": ""}),
    ({**ROUTE, "workspace_backend_origin": "https://[bad"}, {}),
    (ROUTE, ROUTE),
])
def test_route_update_is_atomic_and_distinguishes_absence_clear_invalid(routing, expected):
    assert c.codex_workspace_routing_patch(routing) == expected
    assert oauth_manager._openai_metadata_new_fields(ROUTE, routing) == expected


@pytest.mark.parametrize("metadata", [
    {"accounts": [{"id": "review-route-ws", **ROUTE, "workspace_backend_origin": "https://[bad"}]},
    {"accounts": [{"id": "review-route-ws", "account": "invalid"}]},
    ["not an accounts document"],
])
def test_successful_token_rotation_survives_bad_metadata(monkeypatch, metadata):
    monkeypatch.setattr(openai, "_mock_mode_enabled", lambda: False)
    tokens = {"access_token": "new-access", "refresh_token": "new-refresh"}
    monkeypatch.setattr(openai, "_post_token_json", lambda *a, **k: tokens)
    monkeypatch.setattr(openai, "_fetch_accounts_check_payload_sync", lambda *a, **k: metadata)
    result = openai.refresh_sync("old-refresh", workspace_id="review-route-ws")
    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "new-refresh"
    assert "workspace_backend_origin" not in result


def test_refresh_can_clear_saved_route_and_preserves_rotated_credentials(monkeypatch):
    key = add_account()
    monkeypatch.setattr(openai, "refresh_sync", lambda *a, **k: {
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
        "workspace_backend_origin": "", "account_routing_override": "",
    })
    assert asyncio.run(oauth_manager.force_refresh(key)) == "new-access"
    saved = oauth_manager.get_account(key)
    assert saved["refresh_token"] == "new-refresh"
    assert saved["workspace_backend_origin"] == saved["account_routing_override"] == ""


def test_enrichment_propagates_explicit_route_removal(monkeypatch):
    monkeypatch.setattr(openai, "fetch_accounts_check_sync", lambda *a, **k: {
        "workspace_id": "review-route-ws", "workspace_backend_origin": "", "account_routing_override": "",
    })
    result = openai.enrich_token_response_sync({"access_token": "new-access"})
    assert result["workspace_backend_origin"] == result["account_routing_override"] == ""


def test_metadata_failure_keeps_route_but_explicit_empty_clears(monkeypatch):
    key = add_account()
    monkeypatch.setattr(openai, "fetch_accounts_check_sync", lambda *a, **k: None)
    assert oauth_manager.refresh_openai_metadata_sync(key, force=True)["action"] == "fetch_no_metadata"
    assert oauth_manager.get_account(key)["workspace_backend_origin"] == ROUTE["workspace_backend_origin"]
    monkeypatch.setattr(openai, "fetch_accounts_check_sync", lambda *a, **k: {
        "workspace_backend_origin": "", "account_routing_override": "",
    })
    assert oauth_manager.refresh_openai_metadata_sync(key, force=True)["action"] == "updated"
    assert oauth_manager.get_account(key)["workspace_backend_origin"] == ""


def test_metadata_cannot_write_to_deleted_and_readded_generation(monkeypatch):
    key = add_account()
    def fetch(*args, **kwargs):
        oauth_manager.delete_account(key)
        add_account(access_token="replacement", workspace_backend_origin="", account_routing_override="")
        return dict(ROUTE)
    monkeypatch.setattr(openai, "fetch_accounts_check_sync", fetch)
    result = oauth_manager.refresh_openai_metadata_sync(key, force=True)
    assert result["action"] == "skipped:deleted_generation"
    assert oauth_manager.get_account(key)["access_token"] == "replacement"
    assert oauth_manager.get_account(key)["workspace_backend_origin"] == ""


def test_metadata_follows_same_generation_rename(monkeypatch):
    key = add_account()
    new_key = "openai:renamed@example.test:review-route-ws"
    def fetch(*args, **kwargs):
        assert oauth_manager._save_token_fields(key, {"email": "renamed@example.test"})
        return {"workspace_backend_origin": "NO_CONSTRAINT", "account_routing_override": "us"}
    monkeypatch.setattr(openai, "fetch_accounts_check_sync", fetch)
    result = oauth_manager.refresh_openai_metadata_sync(key, force=True)
    assert result["action"] == "updated" and result["account_key"] == new_key
    assert oauth_manager.get_account(new_key)["workspace_backend_origin"] == "NO_CONSTRAINT"


def test_metadata_does_not_overwrite_newer_token_rotation(monkeypatch):
    key = add_account()
    def fetch(*args, **kwargs):
        oauth_manager._save_token_fields(key, {"access_token": "newer-access", "workspace_backend_origin": "", "account_routing_override": ""})
        return dict(ROUTE)
    monkeypatch.setattr(openai, "fetch_accounts_check_sync", fetch)
    assert oauth_manager.refresh_openai_metadata_sync(key, force=True)["action"] == "skipped:credentials_changed"
    assert oauth_manager.get_account(key)["workspace_backend_origin"] == ""


@pytest.mark.parametrize("expiry, days, refreshed", [("", 9, True), ("", 7, False), ("2999-01-01T00:00:00Z", 9, False)])
def test_background_refresh_eight_day_fallback(monkeypatch, expiry, days, refreshed):
    key = add_account(expired=expiry, last_refresh=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    calls = []
    async def noop(*a, **k):
        return {}
    async def force(*a, **k):
        calls.append(a[0]); return "new"
    monkeypatch.setattr(oauth_manager, "ensure_openai_metadata_fresh", noop)
    monkeypatch.setattr(oauth_manager, "fetch_usage_snapshot", noop)
    monkeypatch.setattr(oauth_manager, "force_refresh", force)
    monkeypatch.setattr(oauth_manager.notifier, "notify_event", lambda *a, **k: None)
    asyncio.run(oauth_manager.proactive_refresh_once())
    assert calls == ([key] if refreshed else [])


def test_access_jwt_expiry_takes_precedence_over_fallback():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).decode().rstrip("=")
    account = {"provider": "openai", "workspace_id": "ws", "access_token": f"e30.{payload}.sig", "expired": "2999-01-01T00:00:00Z", "last_refresh": datetime.now(timezone.utc).isoformat()}
    assert not oauth_manager._token_is_fresh(account)


@pytest.mark.parametrize("override", ["us_cr", "NO_CONSTRAINT"])
def test_route_marker_survives_native_ws_and_http_ws_builders(monkeypatch, override):
    key = add_account(account_routing_override=override)
    ch = OpenAIOAuthChannel(oauth_manager.get_account(key))
    req = asyncio.run(ch.build_upstream_request({"model": "gpt-5.5", "input": "hello"}, "gpt-5.5"))
    assert req.translator_ctx["codex_workspace_routed"] is True
    native = asyncio.run(responses_ws._build_ws_upstream_request(ch, {"model": "gpt-5.5", "input": "hello"}, "gpt-5.5", websocket=SimpleNamespace(headers={})))
    assert native.translator_ctx["codex_workspace_routed"] is True
    built = asyncio.run(failover._build_oauth_responses_ws_upstream_request(ch, {"model": "gpt-5.5", "input": "hello"}, "gpt-5.5"))
    assert built[3]["codex_workspace_routed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("module, name", [(failover, "_connect_oauth_responses_ws"), (responses_ws, "_connect_upstream_ws")])
async def test_both_ws_dialers_select_reject_redirect_policy(monkeypatch, module, name):
    seen = {}
    async def connect(url, **kwargs):
        seen.update(kwargs)
    monkeypatch.setattr(module, "connect_upstream_ws", connect)
    await getattr(module, name)("wss://gov.chatgpt.com/backend-api/codex/responses", headers={}, connector=None, proxy_bytes=None, open_timeout=2, timing=None, round_timeouts=None, reject_redirects=True)
    assert seen["connect_func"] is RejectRedirectConnect
    for location in ("wss://other.example/steal", "/same-origin-redirect"):
        error = InvalidStatus(Response(302, "Found", Headers({"Location": location})))
        client = RejectRedirectConnect("wss://gov.chatgpt.com/responses", additional_headers={"Authorization": "Bearer test"})
        assert client.process_redirect(error) is error


@pytest.mark.asyncio
async def test_real_ws_handshake_redirect_does_not_send_credentials_to_target():
    from src.transports.ws_runtime import connect_upstream_ws, WsProxyBytes
    requests = []
    redirected = []

    async def target(reader, writer):
        redirected.append(await reader.readuntil(b"\r\n\r\n"))
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(target, "127.0.0.1", 0) as destination:
        port = destination.sockets[0].getsockname()[1]
        async def origin(reader, writer):
            requests.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write((f"HTTP/1.1 302 Found\r\nLocation: ws://127.0.0.1:{port}/other\r\nContent-Length: 0\r\n\r\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        async with await asyncio.start_server(origin, "127.0.0.1", 0) as source:
            source_port = source.sockets[0].getsockname()[1]
            with pytest.raises(InvalidStatus):
                await connect_upstream_ws(
                    f"ws://127.0.0.1:{source_port}/responses",
                    headers={"Authorization": "Bearer isolated-test"}, connector=None,
                    proxy_bytes=WsProxyBytes(), open_timeout=2,
                    connect_func=RejectRedirectConnect,
                )
    assert len(requests) == 1
    assert b"Bearer isolated-test" in requests[0]
    assert redirected == []


def test_realtime_cannot_forward_client_owned_workspace_override():
    assert not realtime._should_forward_realtime_header("X-OpenAI-Account-Routing-Override")
    assert realtime._should_forward_realtime_header("x-openai-request-id")
