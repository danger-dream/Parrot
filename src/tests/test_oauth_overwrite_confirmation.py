"""Focused regressions for explicit OAuth same-identity overwrite confirmation."""
from __future__ import annotations

import copy
import uuid
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import pytest

from src import config, log_db, oauth_manager
from src.telegram import states
from src.telegram.menus import oauth_menu


@pytest.fixture(autouse=True)
def reset_accounts():
    config.update(lambda c: c.update({
        "oauthAccounts": [],
        "loadBalancing": {
            "priorityOrders": {"anthropic": [], "openai": []},
            "modelPriorityOrders": {},
        },
    }))
    states.clear_all()


_OPENAI_INSTALLATION = str(uuid.uuid4())


def _entry(provider: str) -> dict:
    entry = {
        "provider": provider,
        "email": f"{provider}@example.test",
        "access_token": "access-old",
        "refresh_token": "refresh-old",
        "models": ["manual-old"],
        "enabled": False,
        "disabled_reason": "user",
        "disabled_until": 123,
        "maxConcurrent": 7,
        "extension": {"keep": True},
    }
    if provider == "openai":
        entry.update(workspace_id="ws-1", chatgpt_account_id="ws-1",
                     codexDeviceInstallationId=_OPENAI_INSTALLATION,
                     codexDeviceConvergenceEnabled=False)
    elif provider in ("xai", "cursor"):
        entry.update(subject="subject-1", sub="subject-1")
    if provider == "cursor":
        entry.update(label="Verified Cursor", cursor_profile_id="profile-old",
                     cursor_profile_name="Old Name", cursor_email_verified=True,
                     cursor_disabled_models=["disabled-local"],
                     cursor_max_context_disabled_models=["max-local"])
    return entry


@pytest.mark.parametrize("provider", ["claude", "openai", "xai", "cursor"])
def test_exact_replace_preserves_key_position_and_settings(provider):
    before = _entry(provider)
    prefix = _entry("claude") | {"email": "prefix@example.test"}
    config.update(lambda c: c["oauthAccounts"].extend([prefix, copy.deepcopy(before)]))
    key = oauth_manager.get_account_key(before)
    config.update(lambda c: c["loadBalancing"]["priorityOrders"]["openai"].append(f"oauth:{key}"))

    incoming = copy.deepcopy(before)
    incoming.update(access_token="access-new", refresh_token="refresh-new",
                    enabled=True, disabled_reason=None, disabled_until=None,
                    maxConcurrent=1, models=["fresh-model"])
    incoming.pop("extension")
    if provider == "xai":
        incoming["email"] = "changed-display@example.test"  # subject remains identity
    if provider == "cursor":
        incoming.update(email="fallback-label", label="fallback-label", cursor_profile_id="")
        incoming.pop("cursor_disabled_models")
        incoming.pop("cursor_max_context_disabled_models")
    if provider == "openai":
        incoming.pop("codexDeviceInstallationId")
        incoming.pop("codexDeviceConvergenceEnabled")

    result = oauth_manager.replace_exact_identity(key, incoming)
    assert result["status"] == "replaced"
    accounts = oauth_manager.list_accounts()
    assert oauth_manager.get_account_key(accounts[1]) == key
    assert accounts[1]["access_token"] == "access-new"
    assert accounts[1]["refresh_token"] == "refresh-new"
    assert accounts[1]["enabled"] is False
    assert accounts[1]["disabled_reason"] == "user"
    assert accounts[1]["disabled_until"] == 123
    assert accounts[1]["maxConcurrent"] == 7
    assert accounts[1]["extension"] == {"keep": True}
    assert config.get()["loadBalancing"]["priorityOrders"]["openai"] == [f"oauth:{key}"]
    if provider != "cursor":
        assert accounts[1]["models"] == ["manual-old"]
    else:
        assert accounts[1]["models"] == ["fresh-model"]
        assert accounts[1]["label"] == "Verified Cursor"
        assert accounts[1]["cursor_disabled_models"] == ["disabled-local"]
        assert accounts[1]["cursor_max_context_disabled_models"] == ["max-local"]
    if provider == "openai":
        assert accounts[1]["codexDeviceInstallationId"] == _OPENAI_INSTALLATION
        assert "codexDeviceConvergenceEnabled" not in accounts[1]


def test_exact_replace_recovers_previous_auth_error_disable():
    before = _entry("claude") | {
        "enabled": False,
        "disabled_reason": "auth_error",
        "disabled_until": "2099-01-01T00:00:00Z",
    }
    config.update(lambda c: c["oauthAccounts"].append(copy.deepcopy(before)))
    key = oauth_manager.get_account_key(before)
    incoming = copy.deepcopy(before) | {
        "access_token": "access-new",
        "refresh_token": "refresh-new",
    }

    assert oauth_manager.replace_exact_identity(key, incoming)["status"] == "replaced"
    saved = oauth_manager.get_account(key)
    assert saved["enabled"] is True
    assert saved["disabled_reason"] is None
    assert saved["disabled_until"] is None


def test_stage_cancel_double_click_expiry_and_conflict_are_noop(monkeypatch):
    old = _entry("xai")
    config.update(lambda c: c["oauthAccounts"].append(copy.deepcopy(old)))
    incoming = copy.deepcopy(old) | {"access_token": "access-new", "refresh_token": "refresh-new"}
    baseline = copy.deepcopy(config.get())
    sent = []
    monkeypatch.setattr(oauth_menu.ui, "send_result", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(oauth_menu.ui, "edit", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(oauth_menu.ui, "answer_cb", lambda *a, **kw: None)

    assert oauth_menu._persist_new_or_stage_overwrite(42, incoming, source="test") == "staged"
    state = states.get_state(42); nonce = state["data"]["nonce"]
    callback_text = repr(sent)
    assert "access-new" not in callback_text and "refresh-new" not in callback_text
    assert config.get() == baseline
    oauth_menu.on_oauth_overwrite_cancel(42, 1, "cb", nonce)
    assert config.get() == baseline
    oauth_menu.on_oauth_overwrite_cancel(42, 1, "cb", nonce)  # old button
    assert config.get() == baseline

    assert oauth_menu._persist_new_or_stage_overwrite(42, incoming, source="test") == "staged"
    nonce = states.get_state(42)["data"]["nonce"]
    config.update(lambda c: c["oauthAccounts"].clear())
    oauth_menu.on_oauth_overwrite_confirm(42, 1, "cb", nonce)
    assert oauth_manager.list_accounts() == []  # stale target must not become add
    oauth_menu.on_oauth_overwrite_confirm(42, 1, "cb", nonce)  # double click
    assert oauth_manager.list_accounts() == []


def test_replace_keeps_oauth_historical_stats_dimension():
    log_db.init()
    conn = log_db._get_conn()
    conn.execute("DELETE FROM request_log"); conn.execute("DELETE FROM request_detail"); conn.commit()
    old = _entry("claude")
    oauth_manager.add_account(old)
    key = oauth_manager.get_account_key(old)

    def finish(request_id: str):
        log_db.insert_pending(request_id, "127.0.0.1", "test", "claude-test", True,
                              msg_count=1, tool_count=0, request_headers={}, request_body={})
        log_db.finish_success(request_id, f"oauth:{key}", "oauth", "claude-test",
                              input_tokens=10, output_tokens=5, cache_creation_tokens=0,
                              cache_read_tokens=0, connect_ms=1, first_token_ms=2,
                              total_ms=3, retry_count=0, affinity_hit=0,
                              response_body="{}", http_status=200)

    finish("before-overwrite")
    incoming = copy.deepcopy(old) | {"access_token": "new", "refresh_token": "new-refresh"}
    assert oauth_manager.replace_exact_identity(key, incoming)["status"] == "replaced"
    finish("after-overwrite")
    assert log_db.tokens_for_channel(f"oauth:{key}", 0)["total"] == 2
    rows = log_db.channel_model_stats(f"oauth:{key}", 0)
    assert len(rows) == 1 and rows[0]["total"] == 2


def test_subject_and_workspace_identity_boundaries():
    x1 = _entry("xai")
    x2 = copy.deepcopy(x1) | {"subject": "subject-2", "sub": "subject-2"}
    oauth_manager.add_account(x1)
    assert oauth_manager.find_exact_identity(x2) is None

    personal = _entry("openai")
    team = copy.deepcopy(personal) | {"workspace_id": "ws-team", "chatgpt_account_id": "ws-team"}
    oauth_manager.add_account(personal)
    assert oauth_manager.find_exact_identity(team) is None
    assert oauth_manager.find_exact_identity(copy.deepcopy(personal)) is not None
