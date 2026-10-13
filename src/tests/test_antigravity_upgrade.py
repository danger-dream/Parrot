from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.oauth import antigravity as ag
from src.management_control.errors import ManagementError
from src.management_control.oauth.flows import OAuthFlowService
from src.management_control.oauth.models import CompleteOAuthLoginCommand, OAuthProvider
from src.tests.management_oauth_fakes import InMemoryOAuthBackend


def _flow(monkeypatch, *, official=False):
    backend = InMemoryOAuthBackend()
    if official:
        monkeypatch.setattr(backend, "antigravity_redirect_uri", lambda: ag.OFFICIAL_REDIRECT_URI)
    calls = []
    original = backend.antigravity_complete_login

    def complete(code, **kwargs):
        calls.append((code, kwargs))
        return original(code, **kwargs)

    monkeypatch.setattr(backend, "antigravity_complete_login", complete)
    return OAuthFlowService(backend), calls


def test_pkce_challenge_and_version_identity(monkeypatch):
    monkeypatch.setattr(ag, "_ag_cfg", lambda: {})
    verifier, challenge = ag.pkce_generate()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = parse_qs(urlparse(ag.build_login_url("state", code_challenge=challenge)).query)
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert "https://www.googleapis.com/auth/aicode" in query["scope"][0].split()
    assert query["redirect_uri"] == [ag.REDIRECT_URI]
    assert ag.DEFAULT_HUB_VERSION in ag.request_user_agent()
    assert "cl=995734886" in ag.request_user_agent()


def test_pkce_verifier_exchange_and_oauth_ua(monkeypatch):
    monkeypatch.setattr(ag, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(ag, "_ag_cfg", lambda: {})
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json={"access_token": "fake"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ag.network, "post_sync", post)
    ag.exchange_code_sync("code", code_verifier="verifier")
    assert calls[0]["data"]["code_verifier"] == "verifier"
    assert calls[0]["headers"]["user-agent"] == "Go-http-client/1.1"
    ag.refresh_sync("refresh")
    assert "code_verifier" not in calls[1]["data"]
    assert "scope" not in calls[1]["data"]


def test_loopback_flow_keeps_state_check_and_passes_verifier(monkeypatch):
    service, calls = _flow(monkeypatch)
    flow = service.start("owner", OAuthProvider.ANTIGRAVITY)
    query = parse_qs(urlparse(flow.auth_url).query)
    assert query["code_challenge"][0]
    with pytest.raises(ManagementError):
        service.complete("owner", flow.flow_id, flow.flow_secret, CompleteOAuthLoginCommand(
            callback_url=ag.REDIRECT_URI + "?code=code",
        ))
    assert calls == []
    service.complete("owner", flow.flow_id, flow.flow_secret, CompleteOAuthLoginCommand(
        callback_url=ag.REDIRECT_URI + "?code=code&state=" + query["state"][0],
    ))
    verifier = calls[0][1]["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == query["code_challenge"][0]


def test_official_code_flow_is_actor_bound_one_shot_and_pkce_bound(monkeypatch):
    service, calls = _flow(monkeypatch, official=True)
    flow = service.start("owner", OAuthProvider.ANTIGRAVITY)
    command = CompleteOAuthLoginCommand(code="4/valid-code")
    with pytest.raises(ManagementError):
        service.complete("other", flow.flow_id, flow.flow_secret, command)
    with pytest.raises(ManagementError):
        service.complete("owner", flow.flow_id, "wrong-secret", command)
    assert calls == []
    service.complete("owner", flow.flow_id, flow.flow_secret, command)
    assert calls[0][0] == "4/valid-code"
    assert calls[0][1]["redirect_uri"] == ag.OFFICIAL_REDIRECT_URI
    verifier = calls[0][1]["code_verifier"]
    challenge = parse_qs(urlparse(flow.auth_url).query)["code_challenge"][0]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == challenge
    with pytest.raises(ManagementError):
        service.complete("owner", flow.flow_id, flow.flow_secret, command)
    assert len(calls) == 1


@pytest.mark.parametrize("command", [
    CompleteOAuthLoginCommand(code="4/code", state="wrong-state"),
    CompleteOAuthLoginCommand(callback_url=ag.OFFICIAL_REDIRECT_URI + "?code=code"),
    CompleteOAuthLoginCommand(callback_url=ag.OFFICIAL_REDIRECT_URI + "?code=code&state=wrong"),
])
def test_official_flow_does_not_bypass_state_for_url_or_wrong_state(monkeypatch, command):
    service, calls = _flow(monkeypatch, official=True)
    flow = service.start("owner", OAuthProvider.ANTIGRAVITY)
    with pytest.raises(ManagementError):
        service.complete("owner", flow.flow_id, flow.flow_secret, command)
    assert calls == []


def test_loopback_flow_rejects_bare_code(monkeypatch):
    service, calls = _flow(monkeypatch)
    flow = service.start("owner", OAuthProvider.ANTIGRAVITY)
    with pytest.raises(ManagementError):
        service.complete("owner", flow.flow_id, flow.flow_secret, CompleteOAuthLoginCommand(code="4/code"))
    assert calls == []


def test_quota_uses_project_from_load_response(monkeypatch):
    monkeypatch.setattr(ag, "load_code_assist_sync", lambda *args, **kwargs: {
        "cloudaicompanionProject": "project-a", "paidTier": {},
    })
    calls = []
    monkeypatch.setattr(ag, "fetch_quota_summary_sync", lambda token, **kwargs: calls.append(kwargs) or [])
    ag.fetch_usage_sync("fake-token", account_key="account")
    assert calls == [{"account_key": "account", "project_id": "project-a"}]


def test_quota_body_carries_only_known_project(monkeypatch):
    monkeypatch.setattr(ag, "_mock_mode_enabled", lambda: False)
    monkeypatch.setattr(ag, "_ag_cfg", lambda: {})
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ag.network, "post_sync", post)
    ag.fetch_quota_summary_sync("token", project_id="project-a")
    ag.fetch_quota_summary_sync("token")
    assert [item["json"] for item in calls] == [{"project": "project-a"}, {}]


def test_control_plane_selects_go_only_for_explicit_profile(monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []
    monkeypatch.setitem(sys.modules, "src.transports.go_tls", SimpleNamespace(
        request_sync=lambda method, url, **kwargs: calls.append(("go", method, kwargs)) or "go-result",
    ))
    monkeypatch.setattr(ag.network, "post_sync", lambda url, **kwargs: calls.append(("old", "POST", kwargs)) or "old-result")
    for setting, expected in [
        ({"enabled": False, "profile": ag.DEFAULT_TLS_PROFILE}, "old-result"),
        ({"enabled": True, "profile": "chrome131"}, "old-result"),
        ({"enabled": True, "profile": ag.DEFAULT_TLS_PROFILE}, "go-result"),
        ({"enabled": True, "profile": " " + ag.DEFAULT_TLS_PROFILE + "\t"}, "go-result"),
        ({"enabled": False, "profile": " " + ag.DEFAULT_TLS_PROFILE + " "}, "old-result"),
        ({"enabled": True, "profile": " chrome131 "}, "old-result"),
    ]:
        monkeypatch.setattr(ag, "_ag_cfg", lambda: {"tlsFingerprint": setting})
        assert ag.request_sync("POST", "https://example.invalid", json={}, proxy_purpose="oauth_antigravity", proxy_channel="oauth:account") == expected
        assert calls[-1][2]["proxy_channel"] == "oauth:account"


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_padded_ls_profile_matches_generation_and_never_falls_back(monkeypatch, method):
    import sys
    from types import SimpleNamespace
    from src.channel import antigravity_oauth_channel as channel
    from src.transports.fingerprint import FingerprintBackendUnavailable

    setting = {"tlsFingerprint": {"enabled": True, "profile": " " + ag.DEFAULT_TLS_PROFILE + " "}}
    monkeypatch.setattr(ag, "_ag_cfg", lambda: setting)
    monkeypatch.setattr(channel, "_provider_cfg", lambda: setting)
    monkeypatch.setattr(channel.oauth_manager, "account_state_key", lambda account: "test-state")
    generated = channel.AntigravityOAuthChannel({"email": "test@example.invalid", "project_id": "test-project"})
    assert generated.tls_fingerprint == ag.DEFAULT_TLS_PROFILE
    calls = []

    def unavailable(*args, **kwargs):
        calls.append("go")
        raise FingerprintBackendUnavailable("synthetic missing backend")

    monkeypatch.setitem(sys.modules, "src.transports.go_tls", SimpleNamespace(request_sync=unavailable))
    monkeypatch.setattr(ag.network, "get_sync", lambda *args, **kwargs: calls.append("ordinary"))
    monkeypatch.setattr(ag.network, "post_sync", lambda *args, **kwargs: calls.append("ordinary"))
    with pytest.raises(FingerprintBackendUnavailable):
        ag.request_sync(method, "https://example.invalid/control")
    assert calls == ["go"]


@pytest.mark.parametrize("fingerprint, expected", [
    ({}, {"enabled": True, "profile": "chrome131"}),
    ({"profile": ag.DEFAULT_TLS_PROFILE}, {"enabled": True, "profile": ag.DEFAULT_TLS_PROFILE}),
    ({"enabled": True}, {"enabled": True, "profile": "chrome131"}),
    ({"enabled": False}, {"enabled": False, "profile": "chrome131"}),
    ({"enabled": True, "profile": "chrome131"}, {"enabled": True, "profile": "chrome131"}),
    ({"enabled": False, "profile": ag.DEFAULT_TLS_PROFILE}, {"enabled": False, "profile": ag.DEFAULT_TLS_PROFILE}),
    ({"enabled": True, "profile": " " + ag.DEFAULT_TLS_PROFILE + " "}, {"enabled": True, "profile": " " + ag.DEFAULT_TLS_PROFILE + " "}),
    ({"enabled": True, "profile": "operator-profile", "extra": "keep"}, {"enabled": True, "profile": "operator-profile", "extra": "keep"}),
])
def test_existing_disk_profile_migration_preserves_credentials_and_custom_settings(tmp_path, monkeypatch, fingerprint, expected):
    import json
    from src import config

    old_scope = ag.SCOPES.removesuffix(" https://www.googleapis.com/auth/aicode")
    account = {"provider": "antigravity", "email": "test@example.invalid", "project_id": "test-project",
               "access_token": "synthetic-at", "refresh_token": "synthetic-rt", "scopes": old_scope,
               "models": ["selected"], "account_model_catalog": {"models": [{"id": "selected"}]}}
    custom = {"userAgent": "operator-client", "scope": ["custom-scope"],
              "redirectUri": "https://example.invalid/callback", "tokenEndpoint": "https://example.invalid/token"}
    raw = {"oauth": {"mockMode": True}, "oauthAccounts": [account],
           "antigravityOAuth": {**custom, "tlsFingerprint": fingerprint}}
    path = tmp_path / "existing.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    loaded = config._load_from_disk()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["antigravityOAuth"]["tlsFingerprint"] == expected
    assert loaded["oauthAccounts"] == saved["oauthAccounts"] == [account]
    assert all(loaded["antigravityOAuth"][key] == value for key, value in custom.items())
    assert saved == loaded
    serialized = path.read_bytes()
    assert config._load_from_disk() == loaded
    assert path.read_bytes() == serialized


def test_minimal_enabled_legacy_file_get_keeps_control_plane_on_old_backend(tmp_path, monkeypatch):
    import json
    import sys
    from types import SimpleNamespace
    from src import config

    path = tmp_path / "legacy.json"
    path.write_text('{"antigravityOAuth":{"tlsFingerprint":{"enabled":true}}}', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0.0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    monkeypatch.setattr(config, "_rejected_rewrite_version", None)
    calls = []
    monkeypatch.setattr(ag.network, "post_sync", lambda *a, **kw: calls.append("ordinary"))
    monkeypatch.setitem(sys.modules, "src.transports.go_tls", SimpleNamespace(
        request_sync=lambda *a, **kw: pytest.fail("legacy file must not switch to Go"),
    ))
    assert config.get()["antigravityOAuth"]["tlsFingerprint"] == {"enabled": True, "profile": "chrome131"}
    ag.request_sync("POST", "https://example.invalid/control")
    assert calls == ["ordinary"]
    assert json.loads(path.read_text(encoding="utf-8"))["antigravityOAuth"]["tlsFingerprint"] == {
        "enabled": True, "profile": "chrome131",
    }
    assert config.reload()["antigravityOAuth"]["tlsFingerprint"]["profile"] == "chrome131"


@pytest.mark.parametrize("initial", [None, {}, {"antigravityOAuth": {}}])
def test_new_file_defaults_to_ls_but_existing_implicit_profile_stays_chrome(tmp_path, monkeypatch, initial):
    import json
    from src import config

    path = tmp_path / "config.json"
    if initial is not None:
        path.write_text(json.dumps(initial), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    loaded = config._load_from_disk()
    assert loaded["antigravityOAuth"]["tlsFingerprint"] == {
        "enabled": True, "profile": ag.DEFAULT_TLS_PROFILE if initial is None else "chrome131",
    }
    assert json.loads(path.read_text(encoding="utf-8")) == loaded
    assert config._load_from_disk() == loaded
    assert config.DEFAULT_CONFIG["antigravityOAuth"]["tlsFingerprint"] == {
        "enabled": True, "profile": ag.DEFAULT_TLS_PROFILE,
    }


def test_stock_identity_migration_preserves_accounts_and_operator_overrides():
    import copy
    from src import config
    old_scope = ag.SCOPES.removesuffix(" https://www.googleapis.com/auth/aicode")
    account = {"access_token": "stored", "refresh_token": "stored-refresh", "scopes": old_scope}
    cfg = {"antigravityOAuth": {
        "userAgent": "antigravity/hub/2.9.1 darwin/arm64", "scope": old_scope,
        "redirectUri": ag.REDIRECT_URI,
        "tlsFingerprint": {"enabled": False, "profile": "chrome131"},
    }, "oauthAccounts": [copy.deepcopy(account)]}
    assert config._migrate_antigravity_client_defaults(cfg)
    assert cfg["antigravityOAuth"]["userAgent"] == ag.DEFAULT_USER_AGENT
    assert cfg["antigravityOAuth"]["scope"] == ag.SCOPES
    assert cfg["oauthAccounts"] == [account]
    assert cfg["antigravityOAuth"]["tlsFingerprint"] == {"enabled": False, "profile": "chrome131"}
    assert not config._migrate_antigravity_client_defaults(cfg)
    cfg["antigravityOAuth"].update(userAgent="custom-client", scope="custom-scope")
    assert not config._migrate_antigravity_client_defaults(cfg)


def test_generation_preserves_existing_credit_and_id_policy():
    from src.providers.antigravity_codec import wrap_cloud_code
    result = wrap_cloud_code({"contents": []}, model="gemini-test", project_id="project")
    assert "enabledCreditTypes" not in result
    assert "userPromptId" not in result
    assert result["requestId"].startswith("agent-")
    assert result["request"]["sessionId"].startswith("-")
