"""Account restriction appears consistently and clears through existing controls."""
import time
from types import SimpleNamespace

import pytest

from src import cooldown, oauth_manager
from src.management_control.oauth import OAuthAccountFilter
from src.providers.workbuddy_errors import ACCOUNT_RESTRICTED_MESSAGE
from src.telegram import ui
from src.telegram.menus import oauth_menu as menu
from src.telegram.menus import model_center_menu as models, model_center_catalog as views
from src.management_control.models import ModelCenterControl, ModelFilters, ModelKind, ModelSourceRef, ModelSourceType
from src.tests.test_workbuddy_actions import action_env
from src.tests.test_workbuddy_lifecycle import account
from src.tests.test_workbuddy_telegram import tg, buttons


def test_restriction_list_detail_filter_and_clear_receipt(tg, monkeypatch):
    key, ctl, ledger, output, snap, save, clock = tg
    monkeypatch.setattr(ui, "is_admin", lambda chat: True)
    before = oauth_manager.get_account(key)
    until = int(time.time() * 1000) + 7200000
    cooldown.record_account_pause("oauth:" + key, ACCOUNT_RESTRICTED_MESSAGE, cooldown_until=until)
    text, kb = menu._list_text_and_kb()
    assert "供应商限制" in text and "暂停全部模型" in text
    assert "冷却 1 个模型" not in text and "__account__" not in text
    assert not menu._filter_account(before, "available")
    detail, kb = menu._detail_text_and_kb(key, refresh_quota=False, actor_chat_id=42)
    assert "供应商限制账户（11140）" in detail and "不是官方解封时间" in detail and "申诉" in detail
    assert "__account__" not in detail and "重新登录" not in str(kb)
    button = next(b for b in buttons(kb) if "限制已解除" in b["text"])
    context = menu._management_context(42)
    result = ctl.get_account(context, key)
    assert not result.account.available and not result.account.invalid
    assert result.account.disabled_reason == "供应商限制"
    assert result.runtime_errors[0].model_id is None
    assert result.runtime_errors[0].message == ACCOUNT_RESTRICTED_MESSAGE
    assert all(item.account_id != key for item in ctl.list_accounts(context, account_filter=OAuthAccountFilter.AVAILABLE).items)
    assert menu.handle_callback(42, 900, "cb", button["callback_data"])
    assert "不代表供应商已解除限制" in ui.answer_cb.call_args.args[1]
    assert not cooldown.is_blocked("oauth:" + key, "new-model")
    assert ctl.get_account(context, key).account.available
    assert menu._filter_account(before, "available")
    assert oauth_manager.get_account(key) == before and ledger["calls"] == 0


def test_model_center_distinguishes_account_pause_from_model_switch(tg, monkeypatch, request):
    from src.channel import registry
    key, ctl, ledger, *_ = tg
    registry.rebuild_from_config()
    monkeypatch.setattr(ui, "is_admin", lambda chat: True)
    monkeypatch.setattr(models, "_CONTROL", ModelCenterControl(oauth=ctl))
    monkeypatch.setattr(views.usage, "peek", lambda *a, **k: SimpleNamespace(value={}, error=None))
    models.reset_for_tests()
    request.addfinalizer(models.reset_for_tests)
    source = ModelSourceRef(ModelSourceType.OAUTH, key)
    session = models._session(42)
    session.source, session.source_label, session.source_provider = source, "研发号 A", "workbuddy"
    context = models._ctx(42)
    filters = ModelFilters(kinds=(ModelKind.CHAT,), source=source)
    cooldown.record_account_pause("oauth:" + key, ACCOUNT_RESTRICTED_MESSAGE, cooldown_until=int(time.time() * 1000) + 7200000)
    page = models._CONTROL.list_models(context, filters=filters, page=1, page_size=20)
    view = next(row for row in page.items if row.model_id == "glm-fixture")
    selected = next(row for row in view.sources if row.id == key)
    assert selected.source_enabled and selected.container_enabled
    assert not selected.effective_routable and not view.available_in(source)
    assert selected.unavailable_reason == "account_restricted"
    text, kb = models._detail_render(42, view.resource_key)
    assert "供应商限制" in text and "当前可用：<code>否" in text
    clear = next(button for button in buttons(kb) if "清除此模型故障" in button["text"])
    assert models.handle_callback(42, 900, "cb-model", clear["callback_data"])
    assert "账户仍受限" in ui.answer_cb.call_args.args[1]
    assert cooldown.is_blocked("oauth:" + key, view.model_id)
    ctl.clear_errors(menu._management_context(42), key)
    view = models._CONTROL.get_model(context, view.resource_key)
    assert view.available_in(source) and ledger["calls"] == 0


@pytest.mark.parametrize("state", ["user", "auth_error", "quota"])
def test_restriction_clear_does_not_enable_otherwise_disabled_account(tg, monkeypatch, state):
    from src import config
    key, ctl, *_ = tg
    monkeypatch.setattr(ui, "is_admin", lambda chat: True)
    config.update(lambda c: c["oauthAccounts"][0].update(enabled=False, disabled_reason=state))
    cooldown.record_account_pause("oauth:" + key, ACCOUNT_RESTRICTED_MESSAGE, cooldown_until=int(time.time() * 1000) + 7200000)
    before = oauth_manager.get_account(key)
    assert not ctl.get_account(menu._management_context(42), key).account.available
    ctl.clear_errors(menu._management_context(42), key)
    assert oauth_manager.get_account(key) == before
    assert not ctl.get_account(menu._management_context(42), key).account.available
