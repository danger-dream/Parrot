"""Notification source regressions using isolated state and completed local futures."""
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from src import failover, oauth_manager
from src.notifications import store
from src.tests.test_notifications import env


@pytest.mark.parametrize("primary,secondary,primary_label,secondary_label", [
    (10080, 300, "7", "5"),
    (43800, 10080, "月", "7"),
    (None, None, None, None),
])
def test_codex_notice_uses_observed_window_lengths(env, monkeypatch, primary, secondary, primary_label, secondary_label):
    cfg = env[0]
    account = cfg["oauthAccounts"][0]
    key = oauth_manager.get_account_key(account)
    monkeypatch.setattr(oauth_manager, "openai_credits_usable", lambda *a, **kw: False)

    def disable(*args, **kwargs):
        account.update(enabled=False, disabled_reason="quota")
        return {"state": "disabled"}

    monkeypatch.setattr(oauth_manager, "set_disabled_by_quota", disable)
    snapshot = {"primary_used_pct": 99, "secondary_used_pct": 96}
    if primary is not None:
        snapshot["primary_window_min"] = primary
        snapshot["secondary_window_min"] = secondary
    failover._maybe_auto_disable_by_codex_snapshot(key, account["email"], snapshot)
    records = list(store.items("events").values())
    assert len(records) == 1
    metrics = records[0]["facts"]["metrics"]
    labels = {item["metric_id"]: item["display_name"] for item in metrics}
    if primary is None:
        assert "5小时" not in labels["primary"]
        assert "7天" not in labels["secondary"]
    else:
        assert primary_label in labels["primary"]
        assert secondary_label in labels["secondary"]


@pytest.mark.parametrize("model_result,expected_model,expected_metadata", [
    ({"action": "error", "error": "discovery failed"}, "failed", None),
    ({"action": "updated", "models": 1, "metadata_sync": {"status": "failed"}}, "succeeded", "failed"),
    ({"action": "updated", "models": 1, "metadata_sync": {"status": "partial_failed"}}, "succeeded", "partial_failed"),
])
def test_zhipu_receipt_preserves_actual_stage_results(env, monkeypatch, model_result, expected_model, expected_metadata):
    from src.notifications import operations
    from src.telegram import states, ui
    from src.telegram.menus import zhipu_oauth_menu as menu

    cfg = env[0]
    account = {"provider": "zhipu", "site": "bigmodel", "credential_mode": "oauth",
               "subject": "source-review", "generationId": "source-review-zhipu",
               "enabled": True, "model_key": "fixture-model-key", "models": ["old-model"]}
    cfg["oauthAccounts"] = [account]
    key = oauth_manager.get_account_key(account)
    future = Future()
    future.set_result(model_result)
    saved = SimpleNamespace(account_id=key, post_save={"model_sync_future": future,
        "usage": {}, "reset": {}})
    received = []
    monkeypatch.setattr(ui, "edit", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(menu.control, "account_snapshot", lambda _key: account)
    monkeypatch.setattr(menu, "_run_remote", lambda chat, data, work, done, failed: done(work()))
    monkeypatch.setattr(operations, "present", lambda *a, **kw: received.append(kw))
    states.clear_all()
    menu._initialize_account(11, 101, key, result=saved)
    assert len(received) == 1
    stages = {stage["name"]: stage["status"] for stage in received[0]["stages"]}
    assert stages["模型目录"] == expected_model
    if expected_metadata:
        assert stages.get("元数据") == expected_metadata
    states.clear_all()


def test_workbuddy_auto_result_does_not_rebind_to_readded_account(env, monkeypatch):
    from datetime import datetime
    from src.notifications import subjects
    from src.oauth.workbuddy import actions

    cfg = env[0]
    old = {"provider": "workbuddy", "realm": "cn", "uid": "source-review",
           "generationId": "source-review-wb-old", "enabled": True,
           "workbuddy_auto_checkin": True}
    cfg["oauthAccounts"] = [old]
    key = oauth_manager.get_account_key(old)
    old_subject = subjects.account(key)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 17, 10, 0, tzinfo=tz)

    def execute(*args, **kwargs):
        cfg["oauthAccounts"] = [{**old, "generationId": "source-review-wb-new"}]
        return {"status": "unknown", "code": "persistence_or_runtime_failure"}

    monkeypatch.setattr(actions, "datetime", Clock)
    monkeypatch.setattr(actions, "execute", execute)
    monkeypatch.setattr(actions, "_auto_attempts", {})
    monkeypatch.setattr(actions, "_next_auto_at", {})
    monkeypatch.setattr(actions, "_cleanup_after", float("inf"))
    actions.auto_checkin_once()
    records = list(store.items("events").values())
    assert all(record["subject"]["generation"] == old_subject.generation for record in records)
