"""Release regressions for declared dependencies and frozen OAuth save preflight."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from src import config, oauth_manager
from src.management_control import ManagementError, ManagementErrorCode
from src.telegram import states
from src.telegram.menus import oauth_menu


def test_management_schemas_declare_the_supported_pydantic_major():
    requirements = {
        requirement.name.lower(): requirement
        for line in (Path(__file__).parents[2] / "requirements.txt").read_text().splitlines()
        if (spec := line.split("#", 1)[0].strip())
        if spec.split(";", 1)[0].strip() != "./vendor/parrot-go-tls-client"
        for requirement in [Requirement(spec)]
    }
    assert "pydantic" in requirements, "Management schemas directly require Pydantic 2"
    versions = requirements["pydantic"].specifier
    assert "1.10.22" not in versions
    assert "2.10.6" in versions  # The separately exercised compatibility floor.
    assert "2.13.5" in versions
    assert "3.0.0" not in versions


@pytest.fixture
def oauth_save_state():
    previous = copy.deepcopy(config.get().get("oauthAccounts", []))
    states.clear_all()
    try:
        yield
    finally:
        config.update(lambda root: root.update({"oauthAccounts": previous}))
        states.clear_all()


def _mixed_accounts(provider: str) -> tuple[dict, dict]:
    legacy = {
        "provider": provider,
        "email": "release-identity@example.test",
        "access_token": "release-old-access-not-real",
        "refresh_token": "release-old-refresh-not-real",
        "enabled": True,
        "models": [],
    }
    canonical = {**legacy, "subject": "release-subject", "sub": "release-subject"}
    return legacy, canonical


@pytest.mark.parametrize("provider", ["xai", "cursor"])
@pytest.mark.parametrize("incoming_has_subject", [False, True])
def test_mixed_legacy_identity_is_rejected_before_tg_overwrite_state(
    oauth_save_state, monkeypatch, provider, incoming_has_subject,
):
    legacy, canonical = _mixed_accounts(provider)
    incoming = copy.deepcopy(canonical if incoming_has_subject else legacy)
    incoming["access_token"] = "release-new-access-not-real"
    config.update(lambda root: root.update({"oauthAccounts": [legacy, canonical]}))
    before = copy.deepcopy(config.get())
    before_disk = Path(config.path()).read_bytes()
    states.set_state(42, "oa_login_code", {"source": "unchanged"})
    before_state = copy.deepcopy(states.get_state(42))
    ui_calls = []
    for name in ("edit", "send_result", "answer_cb"):
        monkeypatch.setattr(oauth_menu.ui, name, lambda *a, **kw: ui_calls.append((a, kw)))

    with pytest.raises(ManagementError) as caught:
        oauth_menu._persist_new_or_stage_overwrite(
            42, incoming, source="release-regression", message_id=100,
        )

    assert caught.value.code is ManagementErrorCode.IDENTITY_CONFLICT
    assert caught.value.fields[0].code == "LEGACY_IDENTITY_MIGRATION"
    assert str(caught.value) == (
        f"{provider} legacy email fallback 会改变 canonical identity，请先移除或迁移旧账户"
    )
    assert ui_calls == []
    assert states.get_state(42) == before_state
    assert config.get() == before
    assert Path(config.path()).read_bytes() == before_disk


@pytest.mark.parametrize("provider", ["xai", "cursor"])
def test_commit_rechecks_identity_after_successful_save_preflight(
    oauth_save_state, provider,
):
    legacy, canonical = _mixed_accounts(provider)
    config.update(lambda root: root.update({"oauthAccounts": [canonical]}))
    incoming = {**canonical, "access_token": "release-new-access-not-real"}
    context = oauth_menu._management_context(42)
    control = oauth_menu.oauth_control
    key = oauth_manager.get_account_key(canonical)
    assert control.prepare_account_save(context, incoming)[0] == key

    # A second writer can change the account set while Telegram waits for consent.
    config.update(lambda root: root["oauthAccounts"].append(legacy))
    before = copy.deepcopy(config.get())
    with pytest.raises(ManagementError) as caught:
        control.replace_account_entry(context, key, incoming, defer_post_save=True)
    assert caught.value.code is ManagementErrorCode.IDENTITY_CONFLICT
    assert config.get() == before
