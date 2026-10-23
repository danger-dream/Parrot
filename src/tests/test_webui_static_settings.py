"""WebUI switch, static hosting under /ui/, and the overview status-bar fields."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import config, webui_static
from src.management_control.observability.status import StatusControl
from src.tests.webui_support import API, bearer, build_app, key_session


@pytest.fixture
def build_dir(tmp_path):
    root = tmp_path / "webui"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Parrot</title>", encoding="utf-8")
    (root / "assets" / "app-1234.js").write_text("console.log(1)", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    return root


def _host_app(root, state):
    app = FastAPI()
    webui_static.install_webui(app, enabled=lambda: state["on"], root=root)
    return app


def test_disabled_ui_is_plain_404_and_root_unchanged(build_dir):
    state = {"on": False}
    with TestClient(_host_app(build_dir, state), follow_redirects=False) as client:
        for path in ("/ui", "/ui/", "/ui/index.html", "/ui/assets/app-1234.js", "/ui/deep/link", "/"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json() == {"detail": "Not Found"}
        assert client.post("/").status_code == 404
        assert client.head("/ui/").status_code == 404


def test_enabled_ui_redirects_spa_fallback_and_cache_headers(build_dir):
    state = {"on": True}
    with TestClient(_host_app(build_dir, state), follow_redirects=False) as client:
        for path in ("/", "/ui"):
            response = client.get(path)
            assert response.status_code == 307
            assert response.headers["location"] == "/ui/"
        index = client.get("/ui/")
        assert index.status_code == 200
        assert index.headers["content-type"].startswith("text/html")
        assert index.headers["cache-control"] == "no-cache"
        assert "Parrot" in index.text
        asset = client.get("/ui/assets/app-1234.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert asset.text == "console.log(1)"
        assert client.get("/ui/assets/missing-0000.js").status_code == 404
        for spa_path in ("/ui/logs", "/ui/settings/web-ui", "/ui/channels/api:alpha"):
            fallback = client.get(spa_path)
            assert fallback.status_code == 200, spa_path
            assert fallback.headers["cache-control"] == "no-cache"
            assert fallback.text == index.text
        favicon = client.get("/ui/favicon.svg")
        assert favicon.status_code == 200 and favicon.text == "<svg/>"
        # Path traversal never escapes the build directory.
        escaped = client.get("/ui/%2e%2e/secret.txt")
        assert "outside" not in escaped.text
        assert client.head("/ui/").status_code == 200
        assert client.post("/ui/").status_code == 404
    # Switching off takes effect immediately, no restart.
    state["on"] = False
    with TestClient(_host_app(build_dir, state), follow_redirects=False) as client:
        assert client.get("/ui/").status_code == 404


def test_enabled_without_build_explains_in_plain_text(tmp_path):
    state = {"on": True}
    with TestClient(_host_app(tmp_path / "missing", state)) as client:
        response = client.get("/ui/")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/plain")
        assert "src/webui" in response.text
        assert client.get("/ui/anything").status_code == 404


def test_real_server_stack_does_not_intercept_ui(build_dir, monkeypatch):
    import server

    monkeypatch.setattr(server.WEBUI_HOST, "root", build_dir)
    monkeypatch.setattr(config, "web_ui_enabled", lambda cfg=None: True)
    client = TestClient(server.app, follow_redirects=False)
    try:
        # No inference API key and a foreign Origin: neither inference auth,
        # CORS, drain nor the management Origin policy may block static files.
        headers = {"Origin": "https://elsewhere.example"}
        assert client.get("/", headers=headers).status_code == 307
        assert client.get("/ui", headers=headers).headers["location"] == "/ui/"
        index = client.get("/ui/", headers=headers)
        assert index.status_code == 200 and "Parrot" in index.text
        assert client.get("/ui/assets/app-1234.js").headers["cache-control"].endswith("immutable")
        assert client.get("/ui/some/route").text == index.text
        monkeypatch.setattr(config, "web_ui_enabled", lambda cfg=None: False)
        assert client.get("/ui/").status_code == 404
        assert client.get("/").status_code == 404
        # Unrelated routes keep their behavior.
        assert client.get("/v1/models").status_code == 401
    finally:
        client.close()


@pytest.fixture
def reset_web_ui():
    yield
    def mutate(cfg):
        cfg.setdefault("management", {})["webUi"] = {"enabled": False}
    config.update(mutate)


def test_web_ui_settings_get_patch_revision_and_validation(tmp_path, reset_web_ui):
    app, runtime, _ = build_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(key_session(client).json()["data"]["credential"])
        current = client.get(f"{API}/settings/web-ui", headers=headers)
        assert current.status_code == 200, current.text
        data = current.json()["data"]
        # On by default.
        assert data["enabled"] is True and data["publicUrl"] == "" and data["revision"].startswith("rev_")

        stale = client.patch(
            f"{API}/settings/web-ui", json={"enabled": False},
            headers={**headers, "If-Match": "rev_stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

        turned_off = client.patch(
            f"{API}/settings/web-ui", json={"enabled": False},
            headers={**headers, "If-Match": data["revision"]},
        )
        assert turned_off.status_code == 200, turned_off.text
        assert turned_off.json()["data"]["enabled"] is False
        assert turned_off.json()["data"]["revision"] != data["revision"]
        assert config.web_ui_enabled() is False
        assert client.get(f"{API}/auth/login-options").json()["data"]["webUiEnabled"] is False

        updated = client.patch(
            f"{API}/settings/web-ui", json={"enabled": True},
            headers={**headers, "If-Match": turned_off.json()["data"]["revision"]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["enabled"] is True
        assert config.get()["management"]["webUi"]["enabled"] is True
        assert config.web_ui_enabled() is True
        assert client.get(f"{API}/auth/login-options").json()["data"]["webUiEnabled"] is True

        addressed = client.patch(f"{API}/settings/web-ui", json={"publicUrl": " https://console.example.com/ui/ "}, headers=headers)
        assert addressed.status_code == 200, addressed.text
        assert addressed.json()["data"]["publicUrl"] == "https://console.example.com/ui/"
        assert config.web_ui_address(config.get(), "203.0.113.9") == "https://console.example.com/ui/"
        cleared = client.patch(f"{API}/settings/web-ui", json={"publicUrl": ""}, headers=headers)
        assert cleared.json()["data"]["publicUrl"] == ""

        for body in ({}, {"enabled": "yes"}, {"enabled": True, "other": 1},
                     {"publicUrl": "ftp://x.example/"}, {"publicUrl": "https://a.example/?q=1"}, {"publicUrl": 3}):
            bad = client.patch(f"{API}/settings/web-ui", json=body, headers=headers)
            assert bad.status_code == 422, body
            assert bad.json()["error"]["code"] == "VALIDATION_FAILED"
        assert client.get(f"{API}/settings/web-ui?x=1", headers=headers).status_code == 422
        assert client.get(f"{API}/settings/web-ui").status_code == 401

        disabled = client.patch(f"{API}/settings/web-ui", json={"enabled": False}, headers=headers)
        assert disabled.json()["data"]["enabled"] is False
    actions = [row["action"] for row in runtime.state_store.audit_snapshot()]
    assert actions.count("settings.web-ui.update") >= 3


def test_telegram_and_http_share_one_web_ui_use_case(reset_web_ui):
    from src.management_control.observability.common import telegram_context
    from src.management_control.system import DEFAULT_SETTINGS_CONTROL

    DEFAULT_SETTINGS_CONTROL.update_web_ui(telegram_context("webui-test"), {"enabled": True})
    assert DEFAULT_SETTINGS_CONTROL.get(telegram_context("webui-test"), "web-ui").enabled is True
    assert DEFAULT_SETTINGS_CONTROL.web_ui_enabled() is True


class _Strict:
    """Fail loudly if overview touches anything beyond cached state."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def __getattr__(self, name):
        raise AssertionError(f"unexpected access: {name}")


def _status_control(*, cfg, public_ip, cached, banner):
    return StatusControl(
        config=SimpleNamespace(get=lambda: cfg),
        registry=SimpleNamespace(all_channels=lambda: []),
        oauth_manager=SimpleNamespace(list_accounts=lambda: []),
        log_db=SimpleNamespace(
            stats_summary=lambda **kwargs: {"overall": {"total": 1}},
            stats_lifetime=lambda: {"overall": {"total": 2}},
        ),
        state_db=SimpleNamespace(quota_load_all=lambda: []),
        status_monitor=SimpleNamespace(snapshot_active=lambda: {}),
        public_ip=_Strict(get=lambda: public_ip),
        update_checker=_Strict(get_cached=lambda: cached, get_update_banner=lambda: banner),
    )


def test_overview_adds_public_ip_telegram_and_cached_update_state():
    from src.management_control.observability.common import telegram_context

    context = telegram_context("overview-test")
    configured = {"telegram": {"botToken": "fake-token", "adminIds": [42]}, "listen": {}}
    value = _status_control(
        cfg=configured, public_ip="203.0.113.9",
        cached={"latest_version": "v9.9.9"}, banner="🆕 new",
    ).overview(context)
    assert value["publicIp"] == "203.0.113.9"
    assert value["telegramBot"] == {"configured": True}
    assert value["update"] == {"available": True, "latestVersion": "v9.9.9"}
    for key in ("version", "uptimeSeconds", "listeners", "counts", "today", "lifetime", "activeAlerts", "revision"):
        assert key in value

    empty = _status_control(
        cfg={"telegram": {"botToken": "fake-token", "adminIds": []}},
        public_ip=None, cached=None, banner=None,
    ).overview(context)
    assert empty["publicIp"] is None
    assert empty["telegramBot"] == {"configured": False}
    assert empty["update"] == {"available": False, "latestVersion": None}

    ignored = _status_control(
        cfg={"telegram": {"adminIds": [42]}}, public_ip="198.51.100.1",
        cached={"latest_version": "v9.9.9"}, banner=None,
    ).overview(context)
    assert ignored["telegramBot"] == {"configured": False}  # no bot token
    assert ignored["update"] == {"available": False, "latestVersion": "v9.9.9"}


def test_overview_api_serializes_new_fields(tmp_path, monkeypatch):
    from src.management_api.schemas.observability import OverviewData

    app, runtime, _ = build_app(tmp_path)
    status = runtime.control_owner().observability.status
    monkeypatch.setattr(status, "public_ip_service", SimpleNamespace(get=lambda: "203.0.113.7"))
    monkeypatch.setattr(status, "update_checker", SimpleNamespace(
        get_cached=lambda: {"latest_version": "v1.2.3"}, get_update_banner=lambda: None,
    ))
    with TestClient(app) as client:
        headers = bearer(key_session(client).json()["data"]["credential"])
        response = client.get(f"{API}/overview", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["publicIp"] == "203.0.113.7"
    assert data["update"] == {"available": False, "latestVersion": "v1.2.3"}
    assert set(data["telegramBot"]) == {"configured"}
    # Fields stay optional so older control fakes still validate.
    legacy = OverviewData.model_validate({
        "version": "0", "uptimeSeconds": 0, "listeners": {"host": "", "port": 0},
        "counts": {"channels": 0, "oauthAccounts": 0, "apiKeys": 0, "quotaHot": 0},
        "today": {}, "lifetime": {}, "activeAlerts": {}, "revision": "rev_x",
    })
    assert legacy.publicIp is None and legacy.update.available is False
