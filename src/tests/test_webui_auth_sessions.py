"""WebUI auth: cookie transport, same-origin policy, confirm code, devices."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src import config
from src.management_auth import ApprovalNotification, ManagementStateStore
from src.management_auth.approvals import CONFIRM_CODE_ALPHABET
from src.telegram import bot, ui
from src.tests.webui_support import API, FAKE_KEY, bearer, build_app, key_session


COOKIE = "parrot_management_session"
SAME_ORIGIN = {"Origin": "http://testserver"}


def _set_cookie(response) -> str:
    values = response.headers.get_list("set-cookie")
    assert len(values) == 1, values
    return values[0]


def test_cookie_transport_hides_credential_and_sets_strict_cookie(tmp_path):
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        response = key_session(client, transport="cookie", headers=SAME_ORIGIN)
        data = response.json()["data"]
        assert data["credential"] is None
        assert data["session"]["authMethod"] == "managementKey"
        header = _set_cookie(response)
        attributes = [part.strip().lower() for part in header.split(";")]
        assert attributes[0].startswith(COOKIE + "=pms_")
        assert "httponly" in attributes
        assert "samesite=strict" in attributes
        assert "path=/api/management" in attributes
        assert "secure" not in attributes
        max_age = int(next(a for a in attributes if a.startswith("max-age=")).split("=")[1])
        assert 30 * 86400 - 5 <= max_age <= 30 * 86400
        assert "pms_" not in response.text  # never echoed in the body

        # The browser cookie authenticates reads without any Authorization header.
        current = client.get(f"{API}/auth/session")
        assert current.status_code == 200, current.text
        assert current.json()["data"]["sessionId"] == data["session"]["sessionId"]


def test_cookie_secure_flag_follows_https_and_forwarded_proto(tmp_path):
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        forwarded = key_session(
            client, transport="cookie", headers={"X-Forwarded-Proto": "https"},
        )
        assert "secure" in [p.strip().lower() for p in _set_cookie(forwarded).split(";")]
    with TestClient(app, base_url="https://testserver") as client:
        secure = key_session(client, transport="cookie")
        assert "secure" in [p.strip().lower() for p in _set_cookie(secure).split(";")]


def test_bearer_default_is_unchanged_and_never_sets_cookie(tmp_path):
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        response = key_session(client)
        assert response.json()["data"]["credential"].startswith("pms_")
        assert "set-cookie" not in response.headers
        explicit = key_session(client, transport="bearer")
        assert explicit.json()["data"]["credential"].startswith("pms_")
        revoke = client.delete(
            f"{API}/auth/session", headers=bearer(response.json()["data"]["credential"]),
        )
        assert revoke.status_code == 204
        assert "set-cookie" not in revoke.headers


def test_session_transport_and_device_fields_are_validated(tmp_path):
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        for body in (
            {"grantType": "managementKey", "managementKey": FAKE_KEY, "sessionTransport": "jwt"},
            {"grantType": "managementKey", "managementKey": FAKE_KEY, "clientName": ""},
            {"grantType": "managementKey", "managementKey": FAKE_KEY, "deviceSummary": "x" * 161},
            {"grantType": "telegramApproval", "approvalId": "map_12345678",
             "exchangeSecret": "s", "clientName": "not-allowed"},
        ):
            response = client.post(f"{API}/auth/sessions", json=body)
            assert response.status_code == 422, body
            assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_cookie_writes_require_allowed_origin(tmp_path):
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        key_session(client, transport="cookie", headers=SAME_ORIGIN)
        # Missing Origin on a cookie-authenticated write: CSRF defense rejects.
        missing = client.delete(f"{API}/auth/session")
        assert missing.status_code == 403
        assert missing.json()["error"]["code"] == "ORIGIN_DENIED"
        # Cross-site Origin: rejected by the path-scoped middleware.
        cross = client.delete(f"{API}/auth/session", headers={"Origin": "https://evil.example"})
        assert cross.status_code == 403
        assert cross.json()["error"]["code"] == "ORIGIN_DENIED"
        # The session is still alive after both rejected writes.
        assert client.get(f"{API}/auth/session").status_code == 200
        # Same-origin write succeeds and clears the cookie.
        ok = client.delete(f"{API}/auth/session", headers=SAME_ORIGIN)
        assert ok.status_code == 204
        cleared = [p.strip().lower() for p in _set_cookie(ok).split(";")]
        assert cleared[0] in {COOKIE + '=""', COOKIE + "="}
        assert "max-age=0" in cleared and "path=/api/management" in cleared
        assert client.get(f"{API}/auth/session").status_code == 401


def test_same_origin_matches_host_or_forwarded_host_and_allowlist_still_works(tmp_path):
    app, _runtime, _ = build_app(tmp_path, allowed_origins=("https://admin.example.test",))
    with TestClient(app) as client:
        credential = key_session(client).json()["data"]["credential"]
        cases = [
            ({"Origin": "http://testserver"}, 200),
            ({"Origin": "http://testserver:80"}, 200),
            ({"Origin": "https://admin.example.test"}, 200),
            ({"Origin": "https://parrot.example", "X-Forwarded-Host": "parrot.example, proxy"}, 200),
            ({"Origin": "http://testserver:8080"}, 403),
            ({"Origin": "https://evil.example"}, 403),
            ({"Origin": "null"}, 403),
        ]
        for headers, expected in cases:
            response = client.get(
                f"{API}/auth/session", headers={**bearer(credential), **headers},
            )
            assert response.status_code == expected, headers
        # Bearer without Origin (CLI) keeps working for writes.
        assert client.delete(f"{API}/auth/session", headers=bearer(credential)).status_code == 204


def test_login_options_are_public_and_reflect_telegram_availability(tmp_path):
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.get(f"{API}/auth/login-options")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()["data"]
        assert data["telegramApproval"] == {"available": True, "ttlSeconds": 180}
        assert data["managementKey"] == {"available": True}
        assert isinstance(data["webUiEnabled"], bool)
        assert client.get(f"{API}/auth/login-options?x=1").status_code == 422
    no_tg, _runtime, _ = build_app(tmp_path / "no-tg", telegram_admins=())
    with TestClient(no_tg) as client:
        data = client.get(f"{API}/auth/login-options").json()["data"]
        assert data["telegramApproval"]["available"] is False
    unconfigured, _runtime, _ = build_app(tmp_path / "no-bot", telegram_configured=False)
    with TestClient(unconfigured) as client:
        assert client.get(f"{API}/auth/login-options").json()["data"]["telegramApproval"]["available"] is False


def test_login_options_report_web_ui_switch(tmp_path, monkeypatch):
    app, runtime, _ = build_app(tmp_path)
    settings = runtime.control_owner().system.settings
    state = {"management": {"webUi": {"enabled": False}}}
    monkeypatch.setattr(settings.config, "get", lambda: state)
    with TestClient(app) as client:
        assert client.get(f"{API}/auth/login-options").json()["data"]["webUiEnabled"] is False
        state["management"]["webUi"]["enabled"] = True
        assert client.get(f"{API}/auth/login-options").json()["data"]["webUiEnabled"] is True


def test_confirm_code_in_create_response_and_telegram_message(tmp_path, monkeypatch):
    app, _runtime, notifier = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            f"{API}/auth/telegram-approvals",
            json={"clientName": "Parrot WebUI", "deviceSummary": "Firefox on Linux"},
        )
        assert response.status_code == 201, response.text
        code = response.json()["data"]["confirmCode"]
    assert re.fullmatch(f"[{CONFIRM_CODE_ALPHABET}]{{4}}", code)
    (_admins, notification), = notifier.notifications
    assert notification.confirm_code == code

    calls = []
    monkeypatch.setattr(ui, "api", lambda method, data=None: calls.append((method, data)) or {"ok": True})
    ui.configure("fake-telegram-token", [42])
    assert bot.send_management_approval((42,), notification) is True
    text = calls[0][1]["text"]
    assert f"确认码：<b><code>{code}</code></b>" in text
    assert "请确认与网页上显示的一致" in text
    assert "max_" not in text


def test_confirm_codes_use_unambiguous_alphabet_and_are_stored(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    assert set(CONFIRM_CODE_ALPHABET).isdisjoint("01OI")
    with TestClient(app) as client:
        codes = set()
        for _ in range(20):
            data = client.post(
                f"{API}/auth/telegram-approvals", json={"clientName": "Parrot WebUI"},
            ).json()["data"]
            codes.add(data["confirmCode"])
            stored = runtime.state_store.get_approval(data["approvalId"])
            assert stored["confirm_code"] == data["confirmCode"]
    assert len(codes) > 1


def test_approval_exchange_with_cookie_copies_device_from_approval(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            f"{API}/auth/telegram-approvals",
            json={"clientName": "Parrot WebUI", "deviceSummary": "Safari on iOS"},
        ).json()["data"]
        runtime.approvals.decide(created["approvalId"], telegram_user_id=42, approved=True)
        exchanged = client.post(
            f"{API}/auth/sessions",
            headers=SAME_ORIGIN,
            json={
                "grantType": "telegramApproval",
                "approvalId": created["approvalId"],
                "exchangeSecret": created["exchangeSecret"],
                "sessionTransport": "cookie",
            },
        )
        assert exchanged.status_code == 201, exchanged.text
        assert exchanged.json()["data"]["credential"] is None
        devices = client.get(f"{API}/auth/sessions").json()["data"]
    assert len(devices) == 1
    device = devices[0]
    assert device["authMethod"] == "telegramApproval"
    assert device["clientName"] == "Parrot WebUI"
    assert device["deviceSummary"] == "Safari on iOS"
    assert device["sourceAddress"] == "testclient"
    assert device["current"] is True


def test_device_list_revoke_one_and_revoke_others(tmp_path):
    app, runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        mine = key_session(
            client, clientName="Laptop", deviceSummary="Chrome on macOS",
        ).json()["data"]
        other_a = key_session(client, clientName="Phone").json()["data"]
        other_b = key_session(client).json()["data"]
        headers = bearer(mine["credential"])

        listed = client.get(f"{API}/auth/sessions", headers=headers)
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        items = {item["sessionId"]: item for item in listed.json()["data"]}
        assert set(items) == {s["session"]["sessionId"] for s in (mine, other_a, other_b)}
        current = items[mine["session"]["sessionId"]]
        assert current["current"] is True
        assert current["clientName"] == "Laptop"
        assert current["deviceSummary"] == "Chrome on macOS"
        assert current["sourceAddress"] == "testclient"
        assert set(current) == {
            "sessionId", "authMethod", "clientName", "deviceSummary", "sourceAddress",
            "issuedAt", "lastSeenAt", "expiresAt", "idleExpiresAt", "current",
        }
        assert sum(item["current"] for item in items.values()) == 1
        assert items[other_b["session"]["sessionId"]]["clientName"] is None

        revoked = client.delete(
            f"{API}/auth/sessions/{other_a['session']['sessionId']}", headers=headers,
        )
        assert revoked.status_code == 204
        assert client.get(f"{API}/auth/session", headers=bearer(other_a["credential"])).status_code == 401
        again = client.delete(
            f"{API}/auth/sessions/{other_a['session']['sessionId']}", headers=headers,
        )
        assert again.status_code == 404
        assert again.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        others = client.post(f"{API}/auth/sessions/actions/revoke-others", headers=headers)
        assert others.status_code == 200
        assert others.json()["data"] == {"revoked": 1}
        assert client.get(f"{API}/auth/session", headers=bearer(other_b["credential"])).status_code == 401
        remaining = client.get(f"{API}/auth/sessions", headers=headers).json()["data"]
        assert [item["sessionId"] for item in remaining] == [mine["session"]["sessionId"]]

        for method, path in (
            ("GET", "/auth/sessions"),
            ("DELETE", f"/auth/sessions/{mine['session']['sessionId']}"),
            ("POST", "/auth/sessions/actions/revoke-others"),
        ):
            assert client.request(method, API + path).status_code == 401

    actions = [row["action"] for row in runtime.state_store.audit_snapshot()]
    assert "session.list" in actions
    assert actions.count("session.revoke") == 2  # one success, one not-found
    assert "session.revoke-others" in actions


def test_revoking_own_cookie_session_by_id_clears_cookie(tmp_path):
    app, _runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        session_id = key_session(
            client, transport="cookie", headers=SAME_ORIGIN,
        ).json()["data"]["session"]["sessionId"]
        no_origin = client.delete(f"{API}/auth/sessions/{session_id}")
        assert no_origin.status_code == 403
        response = client.delete(f"{API}/auth/sessions/{session_id}", headers=SAME_ORIGIN)
        assert response.status_code == 204
        assert "max-age=0" in _set_cookie(response).lower()


def test_device_list_excludes_expired_and_revoked_sessions(tmp_path):
    now = [time.time()]
    app, runtime, _ = build_app(tmp_path, clock=lambda: now[0], idle_timeout_seconds=3600)
    with TestClient(app) as client:
        stale = key_session(client).json()["data"]
        now[0] += 1800
        fresh = key_session(client).json()["data"]
        now[0] += 1900  # stale is idle-expired, fresh is not
        listed = client.get(
            f"{API}/auth/sessions", headers=bearer(fresh["credential"]),
        ).json()["data"]
    assert [item["sessionId"] for item in listed] == [fresh["session"]["sessionId"]]
    assert stale["session"]["sessionId"] not in {item["sessionId"] for item in listed}


_LEGACY_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, selector TEXT NOT NULL UNIQUE, verifier BLOB NOT NULL,
    subject_id TEXT NOT NULL, auth_method TEXT NOT NULL, roles_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL, issued_at REAL NOT NULL, last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL, generation INTEGER NOT NULL, revoked_at REAL
);
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY, secret_verifier BLOB NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, client_name TEXT NOT NULL,
    source_address TEXT NOT NULL, device_summary TEXT, request_id TEXT NOT NULL,
    decided_by TEXT, decided_at REAL, consumed_at REAL
);
CREATE TABLE audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
    target TEXT NOT NULL, result TEXT NOT NULL, request_id TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
"""


def test_legacy_management_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "legacy.db"
    now = time.time()
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
        ("ms_legacy", "sel", b"x", "administrator", "managementKey", '["administrator"]',
         '["management.read"]', now, now, now + 3600, 1),
    )
    conn.execute(
        "INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
        ("map_legacy", b"x", "pending", now, now + 60, "old", "1.2.3.4", None, "r"),
    )
    conn.commit()
    conn.close()

    store = ManagementStateStore(str(path), clock=time.time)
    try:
        columns = {row[1] for row in store._conn.execute("PRAGMA table_info(sessions)")}
        assert {"client_name", "source_address", "device_summary"} <= columns
        assert "confirm_code" in {row[1] for row in store._conn.execute("PRAGMA table_info(approvals)")}
        legacy = store.list_active_sessions(now=now, idle_timeout_seconds=3600)
        assert [row["session_id"] for row in legacy] == ["ms_legacy"]
        assert legacy[0]["client_name"] is None and legacy[0]["source_address"] is None
        assert store.get_approval("map_legacy")["confirm_code"] is None
    finally:
        store.close()
    # Re-opening an already migrated database is a no-op.
    ManagementStateStore(str(path), clock=time.time).close()


def test_management_settings_default_web_ui_and_validate_type():
    base = {"management": {"managementKey": FAKE_KEY, **{
        key: value for key, value in config.DEFAULT_CONFIG["management"].items()
        if key not in {"managementKey", "webUi"}
    }}}
    assert config.management_settings(base)["webUiEnabled"] is False
    assert config.web_ui_enabled(base) is False
    enabled = json.loads(json.dumps(base))
    enabled["management"]["webUi"] = {"enabled": True}
    assert config.management_settings(enabled)["webUiEnabled"] is True
    assert config.web_ui_enabled(enabled) is True
    broken = json.loads(json.dumps(base))
    broken["management"]["webUi"] = {"enabled": "yes"}
    with pytest.raises(ValueError, match="webUi"):
        config.management_settings(broken)
    assert config.web_ui_enabled(broken) is False
    assert config.DEFAULT_CONFIG["management"]["webUi"] == {"enabled": False}


def test_legacy_config_without_web_ui_loads_and_backfills_disabled(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    legacy = {
        "listen": {"host": "127.0.0.1", "port": 1},
        "management": {"managementKey": FAKE_KEY, "allowedOrigins": []},
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    loaded = config._load_from_disk()
    assert loaded["management"]["webUi"] == {"enabled": False}
    assert loaded["management"]["managementKey"] == FAKE_KEY
    settings = config.management_settings(loaded)
    assert settings["webUiEnabled"] is False
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["management"]["webUi"] == {"enabled": False}
