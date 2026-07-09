from __future__ import annotations

import io
import json
import os as _ap_os
import sys as _ap_sys
import zipfile

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()


def _import_modules():
    root = _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__))))
    if root not in _ap_sys.path:
        _ap_sys.path.insert(0, root)
    from src import config, oauth_manager
    from src.oauth import openai as openai_provider
    from src.oauth.openai_import import parse_openai_import_payload
    from src.telegram import states, ui
    from src.telegram.menus import oauth_menu
    return {
        "config": config,
        "oauth_manager": oauth_manager,
        "openai_provider": openai_provider,
        "parse_openai_import_payload": parse_openai_import_payload,
        "oauth_menu": oauth_menu,
        "states": states,
        "ui": ui,
    }


class ApiRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, method, data=None):
        self.calls.append((method, dict(data) if data else {}))
        return {"ok": True, "result": {"file_path": "imports/test.json"}}

    def by(self, method):
        return [d for m, d in self.calls if m == method]

    def last(self, method):
        items = self.by(method)
        return items[-1] if items else None


def _setup(m):
    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
    m["config"].update(_reset)
    m["states"].clear_all()
    m["ui"].api = ApiRecorder()


def _zip_bytes(files: dict[str, object]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for name, obj in files.items():
            zf.writestr(name, json.dumps(obj))
    return bio.getvalue()


def test_parse_cpa_zip_multiple_json(m):
    parse = m["parse_openai_import_payload"]
    payload = _zip_bytes({
        "a@example.com.json": {"email": "a@example.com", "refresh_token": "rt_a_" + "x" * 40},
        "nested/b@example.com.json": {"email": "b@example.com", "refresh_token": "rt_b_" + "y" * 40},
        "ignore.txt": {"email": "nope@example.com", "refresh_token": "rt_nope"},
    })

    items = parse("cpa", payload, filename="CPA.zip")

    assert [x.email for x in items] == ["a@example.com", "b@example.com"]
    assert all(x.refresh_token.startswith("rt_") for x in items)


def test_parse_sub2api_export_json(m):
    parse = m["parse_openai_import_payload"]
    payload = {
        "accounts": [
            {
                "platform": "openai",
                "type": "oauth",
                "credentials": {"email": "a@example.com", "refresh_token": "rt_a_" + "x" * 40},
            },
            {
                "platform": "anthropic",
                "type": "oauth",
                "credentials": {"email": "skip@example.com", "refresh_token": "rt_s_" + "x" * 40},
            },
            {
                "platform": "openai",
                "type": "apikey",
                "credentials": {"email": "skip2@example.com", "refresh_token": "rt_s2_" + "x" * 40},
            },
        ]
    }

    items = parse("sub2api", json.dumps(payload), filename="sub2api-import.json")

    assert len(items) == 1
    assert items[0].email == "a@example.com"


def test_import_duplicate_both_valid_keeps_existing(m):
    _setup(m)
    oauth_menu = m["oauth_menu"]
    provider = m["openai_provider"]
    om = m["oauth_manager"]

    old_tok = provider._mock_token_response("same@example.com", workspace_id="acct-same")
    old_tok["refresh_token"] = "old-valid-refresh-token-xxxxxxxx"
    old_entry, _ = oauth_menu._openai_token_to_entry(old_tok)
    om.add_account(old_entry)

    new_tok = provider._mock_token_response("same@example.com", workspace_id="acct-same")
    new_tok["refresh_token"] = "new-valid-refresh-token-yyyyyyyy"
    new_entry, _ = oauth_menu._openai_token_to_entry(new_tok)

    action, msg = oauth_menu._save_openai_entry_with_duplicate_policy(new_entry)

    assert action == "skipped"
    assert "现有 token 有效" in msg
    acc = om.get_account("openai:same@example.com:acct-same")
    assert acc is not None
    # 验证现有账号仍可用且没有被替换为导入 token。
    assert acc["refresh_token"] != "new-valid-refresh-token-yyyyyyyy"


def test_import_same_email_different_workspace_adds_new_account(m):
    _setup(m)
    oauth_menu = m["oauth_menu"]
    provider = m["openai_provider"]
    om = m["oauth_manager"]

    team_tok = provider._mock_token_response("same@example.com", workspace_id="acct-team")
    team_tok["workspace_type"] = "team"
    team_entry, _ = oauth_menu._openai_token_to_entry(team_tok)
    om.add_account(team_entry)

    personal_tok = provider._mock_token_response("same@example.com", workspace_id="acct-personal")
    personal_tok["workspace_type"] = "personal"
    personal_entry, _ = oauth_menu._openai_token_to_entry(personal_tok)

    action, msg = oauth_menu._save_openai_entry_with_duplicate_policy(personal_entry)

    assert action == "added"
    assert om.get_account("openai:same@example.com:acct-team") is not None
    assert om.get_account("openai:same@example.com:acct-personal") is not None
    accounts = [a for a in om.list_accounts() if a.get("email") == "same@example.com"]
    assert len(accounts) == 2


def test_import_duplicate_existing_invalid_replaces_with_imported(m, monkeypatch):
    _setup(m)
    oauth_menu = m["oauth_menu"]
    provider = m["openai_provider"]
    om = m["oauth_manager"]

    old_tok = provider._mock_token_response("same@example.com", workspace_id="acct-same")
    old_tok["refresh_token"] = "old-bad-refresh-token-xxxxxxxx"
    old_entry, _ = oauth_menu._openai_token_to_entry(old_tok)
    om.add_account(old_entry)

    def fake_refresh(rt, *, email=None, workspace_id=None, org_id=None):
        if rt == "old-bad-refresh-token-xxxxxxxx":
            raise RuntimeError("old token invalid")
        tok = provider._mock_token_response(
            email or "same@example.com", workspace_id=workspace_id or "acct-same",
        )
        tok.pop("refresh_token", None)
        return tok

    monkeypatch.setattr(provider, "refresh_sync", fake_refresh)

    new_entry, _, err = oauth_menu._refresh_openai_rt_to_entry(
        "new-good-refresh-token-yyyyyyyy", email_hint="same@example.com",
        workspace_id="acct-same",
    )
    assert err is None
    action, msg = oauth_menu._save_openai_entry_with_duplicate_policy(new_entry)

    assert action == "replaced"
    assert "现有 token 无效" in msg
    acc = om.get_account("openai:same@example.com:acct-same")
    assert acc is not None
    assert acc["refresh_token"] == "new-good-refresh-token-yyyyyyyy"


def test_import_candidate_invalid_new_keeps_valid_existing(m, monkeypatch):
    _setup(m)
    oauth_menu = m["oauth_menu"]
    provider = m["openai_provider"]
    om = m["oauth_manager"]

    old_tok = provider._mock_token_response("same@example.com", workspace_id="acct-same")
    old_tok["refresh_token"] = "old-good-refresh-token-xxxxxxxx"
    old_entry, _ = oauth_menu._openai_token_to_entry(old_tok)
    om.add_account(old_entry)

    def fake_refresh(rt, *, email=None, workspace_id=None, org_id=None):
        if rt == "new-bad-refresh-token-yyyyyyyy":
            raise RuntimeError("new token invalid")
        tok = provider._mock_token_response(
            email or "same@example.com", workspace_id=workspace_id or "acct-same",
        )
        tok.pop("refresh_token", None)
        return tok

    monkeypatch.setattr(provider, "refresh_sync", fake_refresh)

    status, email, msg = oauth_menu._import_candidate_with_policy({
        "email": "same@example.com",
        "refresh_token": "new-bad-refresh-token-yyyyyyyy",
    })

    assert status == "skipped"
    assert email == "same@example.com"
    assert "现有 token 有效" in msg
    acc = om.get_account("openai:same@example.com:acct-same")
    assert acc is not None
    assert acc["refresh_token"] == "old-good-refresh-token-xxxxxxxx"


def test_finish_openai_add_saves_only_current_workspace_identity(m):
    _setup(m)
    oauth_menu = m["oauth_menu"]
    provider = m["openai_provider"]
    om = m["oauth_manager"]
    rec = m["ui"].api

    tok = provider._mock_token_response("same@example.com", workspace_id="acct-team", org_id="org-team")
    tok["refresh_token"] = "rt-same-" + "x" * 40
    tok.update({
        "chatgpt_account_id": "acct-team",
        "workspace_id": "acct-team",
        "workspace_name": "Team Space",
        "workspace_type": "team",
        "organization_id": "org-team",
        "plan_type": "team",
    })

    oauth_menu._finish_openai_add(42, tok, source="login")

    acc = om.get_account("openai:same@example.com:acct-team")
    assert acc is not None
    assert acc["chatgpt_account_id"] == "acct-team"
    assert acc["organization_id"] == "org-team"
    assert acc["plan_type"] == "team"
    assert om.get_account("openai:same@example.com:acct-personal") is None
    sent = rec.last("sendMessage")
    assert sent and "OpenAI OAuth 账户已添加" in sent["text"]
    assert "Team Space" in sent["text"]


def test_same_email_different_workspace_adds_separate_openai_account(m):
    _setup(m)
    oauth_menu = m["oauth_menu"]
    provider = m["openai_provider"]
    om = m["oauth_manager"]

    personal_tok = provider._mock_token_response("same@example.com", workspace_id="acct-personal", org_id="org-personal")
    personal_tok["refresh_token"] = "rt-personal-" + "x" * 40
    personal_tok.update({"chatgpt_account_id": "acct-personal", "workspace_id": "acct-personal", "plan_type": "plus"})
    team_tok = provider._mock_token_response("same@example.com", workspace_id="acct-team", org_id="org-team")
    team_tok["refresh_token"] = "rt-team-" + "y" * 40
    team_tok.update({"chatgpt_account_id": "acct-team", "workspace_id": "acct-team", "plan_type": "team"})

    oauth_menu._finish_openai_add(42, personal_tok, source="login")
    oauth_menu._finish_openai_add(42, team_tok, source="login")

    assert om.get_account("openai:same@example.com:acct-personal") is not None
    assert om.get_account("openai:same@example.com:acct-team") is not None
    assert om.get_account("openai:same@example.com:acct-personal")["refresh_token"].startswith("rt-personal-")
    assert om.get_account("openai:same@example.com:acct-team")["refresh_token"].startswith("rt-team-")


def test_openai_import_text_preview_sets_confirm_state(m):
    _setup(m)
    oauth_menu = m["oauth_menu"]
    states = m["states"]
    ui = m["ui"]

    states.set_state(42, "oa_openai_import", {"kind": "sub2api"})
    oauth_menu.on_import_openai_text_input(42, json.dumps({
        "accounts": [{
            "platform": "openai",
            "type": "oauth",
            "credentials": {"email": "a@example.com", "refresh_token": "rt_a_" + "x" * 40},
        }]
    }))

    state = states.get_state(42)
    assert state and state["action"] == "oa_openai_import_confirm"
    assert state["data"]["items"][0]["email"] == "a@example.com"
    sent = ui.api.last("sendMessage")
    assert sent and "a@example.com" in sent["text"]
    assert "导入" in sent["text"]
    assert "Sub2API 账户" in sent["text"]
    assert "<tg-emoji" in sent["text"]
