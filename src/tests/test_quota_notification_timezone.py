"""Quota notifications localize display only, never the stored reset instant."""
from __future__ import annotations

from src.tests import _isolation
_isolation.isolate()

from datetime import datetime, timedelta, timezone
import pytest
from src.tests.test_openai_oauth_quota import _import_modules


@pytest.mark.parametrize("provider", ["openai", "claude"])
@pytest.mark.parametrize("reset", ["2026-10-06T10:23:05Z", "2026-10-06T20:23:05Z", None])
def test_realtime_quota_notice_bjt_preserves_utc_reset(m, monkeypatch, provider, reset):
    manager = m["oauth_manager"]
    failover = m["failover"]
    from src import notifier
    messages, disabled = [], []
    monkeypatch.setattr(manager, "get_account", lambda key: {})
    monkeypatch.setattr(manager, "openai_credits_usable", lambda *a, **k: False)
    monkeypatch.setattr(manager, "openai_plan_workspace_label", lambda acc: "OpenAI")
    monkeypatch.setattr(manager, "claude_plan_label", lambda acc: "")
    monkeypatch.setattr(failover, "_get_quota_disable_threshold_pct", lambda: 100)
    monkeypatch.setattr(notifier, "notify_event", lambda event, text: messages.append(text))

    def disable(key, until, **kwargs):
        disabled.append(until)
        return {"state": "disabled"}

    monkeypatch.setattr(manager, "set_disabled_by_quota", disable)
    if provider == "openai":
        # The production function imports datetime locally. Freeze its clock
        # so the generated UTC timestamp is deterministic.
        import datetime as dt_module
        instant = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
        monkeypatch.setattr(dt_module, "datetime", FixedDatetime)
        snap = {"primary_used_pct": 100}
        if reset:
            target = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            snap["primary_reset_sec"] = int((target - instant).total_seconds())
        failover._maybe_auto_disable_by_codex_snapshot("test-account", "user@example.test", snap)
    else:
        headers = {"anthropic-ratelimit-unified-5h-utilization": "1.0"}
        if reset:
            headers["anthropic-ratelimit-unified-5h-reset"] = str(int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()))
        failover._maybe_auto_disable_by_headers("test-account", "user@example.test", headers, provider="claude")

    assert disabled == [reset]  # Storage and recovery still receive UTC ISO.
    assert len(messages) == 1
    expected = "unknown" if reset is None else (
        datetime.fromisoformat(reset.replace("Z", "+00:00"))
        .astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    )
    assert f"恢复时间: <code>{expected}</code>" in messages[0]
    if reset:
        assert reset not in messages[0]
