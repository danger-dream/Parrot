"""Telegram OAuth adapter regressions for post-save effects and actor context."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tests import _isolation

_isolation.isolate()

from src.management_control import BoundedAuditSink  # noqa: E402
from src.management_control.oauth import OAuthControl  # noqa: E402
from src.management_control.oauth.menu_bridge import telegram_context  # noqa: E402
from src.telegram.menus import (  # noqa: E402
    oauth_account_models_menu,
    oauth_menu,
)
from src.tests.management_oauth_fakes import InMemoryOAuthBackend  # noqa: E402


def _control_with_audit():
    backend = InMemoryOAuthBackend()
    audit = BoundedAuditSink()
    return OAuthControl(backend, audit_sink=audit), backend, audit


def test_tg_import_restores_usage_quota_and_model_effects_once(monkeypatch):
    control, backend, audit = _control_with_audit()
    monkeypatch.setattr(oauth_menu, "oauth_control", control)
    existing_id = "openai:admin@example.test:workspace-1"
    new_id = "openai:tg-import@example.test:workspace-tg-import"
    replacement = {
        **backend.get_account(existing_id),
        "access_token": "replacement-access",
        "refresh_token": "replacement-refresh",
    }
    new_entry = {
        "provider": "openai",
        "type": "openai",
        "email": "tg-import@example.test",
        "workspace_id": "workspace-tg-import",
        "chatgpt_account_id": "workspace-tg-import",
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "models": [],
    }
    staged = {
        "new": [{"account_key": new_id, "entry": new_entry, "meta": {}}],
        "duplicate": [{
            "account_key": existing_id, "entry": replacement, "meta": {},
        }],
        "failed": [],
    }
    events: list[str] = []
    original_usage = backend.fetch_usage_snapshot
    original_model_sync = control.start_post_save_model_sync

    async def traced_usage(account_id):
        events.append(f"usage:{account_id}")
        return await original_usage(account_id)

    def traced_model_sync(context, account_id):
        assert context.actor.subject_id == "telegram:4242"
        events.append(f"model:{account_id}")
        return original_model_sync(context, account_id)

    monkeypatch.setattr(backend, "fetch_usage_snapshot", traced_usage)
    monkeypatch.setattr(control, "start_post_save_model_sync", traced_model_sync)
    monkeypatch.setattr(
        oauth_menu.ui, "edit",
        lambda _chat_id, _message_id, text, **_kwargs: events.append(
            "progress" if "正在同步模型，请稍候" in text else "result"
        ),
    )

    result = oauth_menu._commit_staged_openai_import(staged, chat_id=4242)

    assert result["added"] == ["tg-import@example.test"]
    assert result["replaced"] == ["admin@example.test"]
    assert events == [f"usage:{new_id}", f"usage:{existing_id}"]
    assert backend.model_sync_started == []
    oauth_menu._wait_import_model_sync(4242, 99, result)
    assert events == [
        f"usage:{new_id}", f"usage:{existing_id}", "progress",
        f"model:{new_id}", f"model:{existing_id}",
    ]
    assert backend.model_sync_started == [new_id, existing_id]
    assert backend.usage_fetches == [new_id, existing_id]
    assert backend.quota_evaluations == [new_id, existing_id]
    assert set(backend.quota) >= {new_id, existing_id}
    mutations = [
        row for row in audit.snapshot()
        if row.action in {"oauth.account.create", "oauth.account.replace"}
    ]
    assert [row.actor for row in mutations] == ["telegram:4242", "telegram:4242"]


def test_tg_import_model_launch_error_follows_progress_and_escapes(monkeypatch):
    failure = RuntimeError("fake model launch failure")
    events: list[str] = []

    class ControlProbe:
        def start_post_save_model_sync(self, context, account_id):
            assert context.actor.subject_id == "telegram:4242"
            events.append(f"model:{account_id}")
            return {"model_sync_future": None, "model_sync_error": failure}

    monkeypatch.setattr(oauth_menu, "oauth_control", ControlProbe())
    monkeypatch.setattr(
        oauth_menu.ui, "edit",
        lambda _chat_id, _message_id, _text, **_kwargs: events.append("progress"),
    )
    result = {"model_sync_keys": ["account-a"]}

    with pytest.raises(RuntimeError, match="fake model launch failure"):
        oauth_menu._wait_import_model_sync(4242, 99, result)

    assert events == ["progress", "model:account-a"]
    assert "model_sync" not in result


def test_tg_usage_defaults_and_model_sync_keep_real_chat_actor(monkeypatch):
    control, _backend, audit = _control_with_audit()
    monkeypatch.setattr(oauth_menu, "oauth_control", control)
    monkeypatch.setattr(oauth_account_models_menu, "oauth_control", control)
    account_id = "openai:admin@example.test:workspace-1"

    usage = oauth_menu._fetch_and_save_usage_result_sync(
        account_id, chat_id=4242, email="admin@example.test",
    )
    assert usage.get("error") is None
    result = asyncio.run(
        control.refresh_account_models_for_telegram(
            telegram_context(4242), account_id,
        )
    )
    assert result["action"] == "updated"

    by_action = {row.action: row for row in audit.snapshot()}
    for action in (
        "oauth.usage.refresh",
        "oauth.models.sync",
    ):
        assert by_action[action].actor == "telegram:4242"
        assert by_action[action].request_id == "telegram:4242"


def test_owned_tg_sources_have_no_zero_actor_or_raw_model_sync():
    root = Path("src/telegram/menus")
    sources = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in (
            "oauth_menu.py",
            "oauth_account_models_menu.py",
        )
    )
    assert "_management_context(0)" not in sources
    assert "refresh_account_models_raw(account_key)" not in sources
