from __future__ import annotations

from src.tests import _isolation

_isolation.isolate()

from src import oauth_manager
from src.telegram.menus import oauth_menu


def test_refresh_keeps_curated_team_workspace_name_when_accounts_check_says_personal(monkeypatch):
    acc = {
        "email": "team@example.com",
        "provider": "openai",
        "type": "openai",
        "access_token": "old-access",
        "refresh_token": "refresh-token",
        "expired": "2000-01-01T00:00:00Z",
        "workspace_id": "workspace-team",
        "chatgpt_account_id": "workspace-team",
        "workspace_name": "AU2",
        "workspace_type": "team",
        "plan_type": "team",
        "enabled": True,
    }
    oauth_manager.add_account(acc)

    monkeypatch.setattr(oauth_manager.openai_provider, "refresh_sync", lambda *args, **kwargs: {
        "access_token": "new-access",
        "expires_in": 3600,
        "workspace_id": "workspace-team",
        "workspace_name": "Personal",
        "workspace_type": "team",
        "plan_type": "team",
    })

    oauth_manager._refresh_sync_locked("openai:team@example.com:workspace-team", True)
    refreshed = oauth_manager.get_account("openai:team@example.com:workspace-team")

    assert refreshed["workspace_name"] == "AU2"
    assert refreshed["workspace_type"] == "team"
    assert refreshed["plan_type"] == "team"


def test_openai_workspace_label_does_not_show_personal_for_team_workspace():
    label = oauth_menu._openai_workspace_label({
        "provider": "openai",
        "email": "team@example.com",
        "workspace_name": "Personal",
        "workspace_type": "team",
        "plan_type": "team",
    }, force=True, html=False)

    assert label == "team"
