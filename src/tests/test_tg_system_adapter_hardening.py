from __future__ import annotations

import copy
import threading
from types import SimpleNamespace

import pytest

from src.management_control.observability import RetentionControl
from src.management_control.observability.common import telegram_context
from src.management_control.system.telegram_retention import TelegramRetentionAdapter
from src.telegram import states
from src.telegram.menus import system_menu as sm
from src.tests.test_tg_contract_system_helpers import SystemEnv, cases_for


SYSTEM_CASES = cases_for("TG-SYS-02", "TG-SYS-05")


def _case(case_id: str):
    return copy.deepcopy(next(case for case in SYSTEM_CASES if case["caseId"] == case_id))


def test_tg_retry_write_failure_preserves_raw_exception_and_state(monkeypatch):
    env = SystemEnv(_case("TG-SYS-02.002-retry-inputs"), monkeypatch)
    states.set_state(42, "sys_retry_attempts")

    def fail_write(_mutator):
        raise RuntimeError("retry-write-failed")

    monkeypatch.setattr(sm.config, "update", fail_write)
    with pytest.raises(RuntimeError, match="^retry-write-failed$"):
        sm._on_retry_attempts_input(42, "3")
    assert states.get_state(42)["action"] == "sys_retry_attempts"
    assert env.capture.calls == []


def test_tg_blacklist_success_has_no_post_write_registry_dependency(monkeypatch):
    env = SystemEnv(_case("TG-SYS-02.001-retry-toggles"), monkeypatch)
    states.set_state(42, "sys_bl_add_default")

    def unavailable_registry():
        raise RuntimeError("registry-after-write-failed")

    monkeypatch.setattr(sm.registry, "all_channels", unavailable_registry)
    sm._on_bl_add_default_input(42, "policy/violation")
    assert env.cfg["contentBlacklist"]["default"][-1] == "policy/violation"
    assert states.get_state(42) is None
    assert any(
        call["method"] == "sendMessage" and "已添加默认黑名单关键词" in call["payload"]["text"]
        for call in env.capture.calls
    )


def test_tg_retention_missing_body_setting_is_off_and_first_toggle_enables(monkeypatch):
    env = SystemEnv(_case("TG-SYS-05.001-menu-toggle-busy"), monkeypatch)
    env.cfg.pop("logStoreBodies")
    text, kb = sm._retention_menu_text_kb()
    assert "保存完整请求：<code>关闭</code>" in text
    assert any(button["text"] == "☐ 保存完整请求" for row in kb["inline_keyboard"] for button in row)
    control = sm._retention_control.control
    context = sm._control_context(42)
    assert control.sync_settings(context)["logStoreBodies"] is False
    assert control.settings(context)["logStoreBodies"] is False
    sm._toggle_log_store_bodies(42, 100, "cb-toggle")
    assert env.cfg["logStoreBodies"] is True
    assert "保存完整请求：<code>开启</code>" in sm._retention_menu_text_kb()[0]


def test_tg_retention_pending_has_no_256_limit_and_exact_600_second_ttl(monkeypatch):
    env = SystemEnv(_case("TG-SYS-05.014-ttl"), monkeypatch)
    codes = [sm._register_retention_pending(42, "first", days=index + 1) for index in range(257)]
    assert len(set(codes)) == 257
    assert len(sm._retention_pending) == 257
    env.now += 599
    assert sm._get_retention_pending(codes[0], 42, "first") is not None
    env.now += 1
    assert sm._get_retention_pending(codes[0], 42, "first") is None
    assert sm._retention_pending == {}


def test_tg_retention_unrepresentable_cutoff_reaches_frozen_renderer(monkeypatch):
    env = SystemEnv(_case("TG-SYS-05.018-plan-empty-cancel"), monkeypatch)

    def plan(days):
        return {
            "days": days,
            "cutoff": 10.0**100,
            "reference_ts": env.now,
            "base_policy": {"mode": "forever", "days": None},
            "items": [],
            "errors": [],
            "scanned_months": 0,
            "scanned_bytes": 0,
            "preflight": {"ok": True},
            "signature": "raw-plan",
        }

    monkeypatch.setattr(sm.log_db, "plan_retention", plan)
    first = sm._register_retention_pending(42, "first", days=7)
    sm._scan_retention(42, 100, "cb-scan", first)
    assert len(sm._retention_pending) == 1
    pending = next(iter(sm._retention_pending.values()))
    assert pending["kind"] == "plan"
    assert pending["plan"]["cutoff"] == 10.0**100
    edits = [call for call in env.capture.calls if call["method"] == "editMessageText"]
    assert "时间不可表示" in edits[-1]["payload"]["text"]
    assert isinstance(sm._retention_control.control, RetentionControl)


def test_tg_retention_extend_and_apply_exceptions_preserve_baseline_order(monkeypatch):
    body_env = SystemEnv(_case("TG-SYS-05.001-menu-toggle-busy"), monkeypatch)

    def fail_body_write(_mutator):
        raise RuntimeError("body-authority-failed")

    monkeypatch.setattr(sm.config, "update", fail_body_write)
    with pytest.raises(RuntimeError, match="^body-authority-failed$"):
        sm._toggle_log_store_bodies(42, 100, "cb-toggle")
    assert body_env.capture.calls == []

    extend_env = SystemEnv(_case("TG-SYS-05.004-extend"), monkeypatch)
    states.set_state(42, "sys_retention_days")

    def fail_extend(_days):
        raise RuntimeError("extend-authority-failed")

    monkeypatch.setattr(sm.log_db, "extend_retention_days", fail_extend)
    with pytest.raises(RuntimeError, match="^extend-authority-failed$"):
        sm._on_retention_days_input(42, "9")
    assert states.get_state(42) is None
    assert extend_env.capture.calls == []

    apply_env = SystemEnv(_case("TG-SYS-05.011-commit"), monkeypatch)
    plan = {"days": 7, "items": [], "preflight": {"ok": True}, "signature": "plan"}
    code = sm._register_retention_pending(42, "plan", plan=plan)

    def fail_apply(_plan, *, activate_policy=False, progress=None):
        raise RuntimeError("apply-authority-failed")

    monkeypatch.setattr(sm.log_db, "apply_retention_plan", fail_apply)
    with pytest.raises(RuntimeError, match="^apply-authority-failed$"):
        sm._commit_retention(42, 100, "cb-commit", code)
    assert code not in sm._retention_pending
    assert [call["method"] for call in apply_env.capture.calls] == [
        "answerCallbackQuery", "editMessageText",
    ]
    assert "正在验证清理计划并保存策略" in apply_env.capture.calls[-1]["payload"]["text"]


def test_tg_retention_progress_is_isolated_per_concurrent_call():
    barrier = threading.Barrier(2)

    class FakeLogDb:
        def apply_retention_plan(self, plan, *, activate_policy=False, progress=None):
            assert activate_policy is True
            barrier.wait(timeout=2)
            progress({"phase": "item_start", "item": {"month": plan["tag"]}})
            return {"ok": True, "tag": plan["tag"]}

    control = RetentionControl(log_db=FakeLogDb(), config=SimpleNamespace())
    adapter = TelegramRetentionAdapter(control)
    assert adapter.control is control
    received = {"A": [], "B": []}
    results = {}
    failures: list[BaseException] = []

    def invoke(tag: str) -> None:
        try:
            results[tag] = adapter.commit_plan(
                telegram_context("retention-" + tag), {"tag": tag},
                progress=lambda event: received[tag].append(event["item"]["month"]),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=invoke, args=(tag,)) for tag in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert received == {"A": ["A"], "B": ["B"]}
    assert results == {"A": {"ok": True, "tag": "A"}, "B": {"ok": True, "tag": "B"}}
