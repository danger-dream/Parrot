"""Direct regression gates for parity re-audit findings P06--P13."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from src.management_control.auxiliary.common import telegram_context
from src.management_control.auxiliary.status_alerts import StatusAlertControl
from src.management_control.auxiliary.updates import STAGE_STAGED, UpdateControl
from src.management_control.observability import inspector
from src.management_control.observability.logs import LogsControl
from src.management_control.observability.media import MediaControl
from src.management_control.observability.stats import StatsControl
from src.telegram import menu_cache
from src.telegram.menus import stats_menu, status_alert_menu, status_menu, update_menu


_CONTEXT = telegram_context(42)


class _ConfigGateway:
    def __init__(self, value: dict[str, Any], *, fail_get_after: int | None = None) -> None:
        self.value = deepcopy(value)
        self.fail_get_after = fail_get_after
        self.get_count = 0
        self.update_count = 0

    def get(self) -> dict[str, Any]:
        self.get_count += 1
        if self.fail_get_after is not None and self.get_count > self.fail_get_after:
            raise RuntimeError("read-back must not happen")
        return deepcopy(self.value)

    def update(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        self.update_count += 1
        mutator(self.value)
        return deepcopy(self.value)


class _StatusGateway:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def forget_provider(self, provider: str) -> None:
        self.forgotten.append(provider)


class _UpdateGateway:
    current_version = "0.31.13"

    def __init__(self, events: list[Any] | None = None) -> None:
        self.events = events if events is not None else []
        self.state_calls = 0
        self.clear_calls = 0

    def state(self) -> dict[str, Any]:
        self.state_calls += 1
        self.events.append("state")
        # A second read deliberately observes a different state.  The frozen TG
        # path must consume only the first staged decision.
        return {"stage": STAGE_STAGED if self.state_calls == 1 else "idle"}

    def save_state(self, *, chat_id: int, notify_msg_id: int) -> None:
        self.events.append(("save_state", chat_id, notify_msg_id))

    def activate(self) -> tuple[bool, str]:
        self.events.append("confirm_restart")
        return False, "restart failed"

    def clear_ignored(self) -> None:
        self.clear_calls += 1


# P06 -----------------------------------------------------------------------


def test_p06_menu_cache_has_no_unconsumed_status_tps_job() -> None:
    names = [job.name for job in menu_cache.COORDINATOR._periodic]

    assert "status-tps" not in names
    assert not hasattr(menu_cache, "STATUS_TPS")
    assert not hasattr(menu_cache, "_refresh_status_tps")
    assert names == [
        "period-today-month",
        "oauth-windows",
        "lifetime",
        "apikey-history",
        "model-details",
    ]


# P07 -----------------------------------------------------------------------


def _patch_status_compose(monkeypatch, *, config: dict[str, Any], events: list[Any]):
    class Control:
        api_totals_calls = 0

        def config_snapshot(self, _context):
            events.append("config")
            return deepcopy(config)

        def selection_mode(self, _context, value):
            events.append("selection")
            return value

        def stats_summary(self, _context, *, since_ts, family=None):
            events.append(("stats", family))
            return {"overall": {"total": 0}}

        def tps_by_channel_model(self, _context, *, since_ts):
            events.append("tps")
            return {}

        def affinity_count(self, _context):
            events.append("affinity")
            return 0

        def apikey_concurrency_totals(self, _context):
            self.api_totals_calls += 1
            events.append(("api-totals", self.api_totals_calls))
            if self.api_totals_calls == 1:
                return {"in_flight": 0, "waiting": 0, "tracked_keys": 0}
            return {"in_flight": 7, "waiting": 0, "tracked_keys": 1}

        def apikey_concurrency_snapshot(self, _context):
            events.append("api-snapshot")
            return []

        def channel_concurrency_totals(self, _context):
            events.append("channel-totals")
            return {"in_flight": 0, "waiting": 0, "tracked_channels": 0}

        def channel_concurrency_snapshot(self, _context):
            events.append("channel-snapshot")
            return []

    control = Control()
    monkeypatch.setattr(status_menu, "_CONTROL", control)
    monkeypatch.setattr(status_menu, "_channel_overview", lambda: events.append("overview") or {})
    monkeypatch.setattr(
        status_menu,
        "_fastest_channels_by_family",
        lambda **_kwargs: events.append("fastest") or {
            "anthropic": [("api:a|m", {"avg_first_byte_ms": 1, "rate": 100, "recent_requests": 1})],
            "openai": [],
        },
    )
    monkeypatch.setattr(status_menu, "_problem_channels", lambda: events.append("problems") or [])
    monkeypatch.setattr(status_menu, "_quota_warnings", lambda *_args, **_kwargs: events.append("quota") or [])
    monkeypatch.setattr(status_menu, "_render_fastest_family", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(status_menu, "codex_cli_version", lambda: "0.0.0")
    return control


def test_p07_status_uses_frozen_staged_concurrency_order_and_two_api_totals(monkeypatch) -> None:
    events: list[Any] = []
    _patch_status_compose(
        monkeypatch,
        config={
            "channelSelection": "smart",
            "apiKeyConcurrency": {"enabled": True},
            "concurrency": {"enabled": True, "defaultMaxConcurrent": 1},
        },
        events=events,
    )

    text, _keyboard = status_menu._compose()

    assert events == [
        "config",
        "selection",
        "overview",
        ("stats", "anthropic"),
        ("stats", "openai"),
        ("stats", None),
        "fastest",
        "problems",
        "quota",
        "tps",
        "affinity",
        ("api-totals", 1),
        ("api-totals", 2),
        "api-snapshot",
        "channel-totals",
        "channel-snapshot",
    ]
    # The block consumes the second totals read, as the frozen code did.
    assert "在途 <b>7</b>" in text


def test_p07_status_skips_detail_and_channel_sources_when_blocks_are_disabled(monkeypatch) -> None:
    events: list[Any] = []
    control = _patch_status_compose(
        monkeypatch,
        config={
            "channelSelection": "smart",
            "apiKeyConcurrency": {"enabled": False},
            "concurrency": {"enabled": False},
        },
        events=events,
    )
    # No fastest channel means no on-demand TPS query in this condition.
    monkeypatch.setattr(
        status_menu,
        "_fastest_channels_by_family",
        lambda **_kwargs: events.append("fastest") or {"anthropic": [], "openai": []},
    )
    monkeypatch.setattr(
        control,
        "apikey_concurrency_snapshot",
        lambda _context: (_ for _ in ()).throw(AssertionError("hidden API detail queried")),
    )
    monkeypatch.setattr(
        control,
        "channel_concurrency_totals",
        lambda _context: (_ for _ in ()).throw(AssertionError("disabled channel totals queried")),
    )
    monkeypatch.setattr(
        control,
        "channel_concurrency_snapshot",
        lambda _context: (_ for _ in ()).throw(AssertionError("disabled channel snapshot queried")),
    )

    text, _keyboard = status_menu._compose()

    assert "API Key 队列" not in text
    assert "渠道并发队列" not in text
    assert events.count(("api-totals", 1)) == 1
    assert "api-snapshot" not in events
    assert "channel-totals" not in events
    assert "channel-snapshot" not in events
    assert "tps" not in events


def test_p07_status_propagates_failure_only_after_enabled_api_block_gate(monkeypatch) -> None:
    events: list[Any] = []
    control = _patch_status_compose(
        monkeypatch,
        config={
            "apiKeyConcurrency": {"enabled": True},
            "concurrency": {"enabled": False},
        },
        events=events,
    )

    def fail_snapshot(_context):
        events.append("api-snapshot-failed")
        raise RuntimeError("api detail unavailable")

    monkeypatch.setattr(control, "apikey_concurrency_snapshot", fail_snapshot)

    with pytest.raises(RuntimeError, match="api detail unavailable"):
        status_menu._compose()

    assert events[-3:] == [("api-totals", 1), ("api-totals", 2), "api-snapshot-failed"]


# P08 -----------------------------------------------------------------------


def test_p08_status_alert_accepts_86401_and_keeps_successful_visible_flow(monkeypatch) -> None:
    config = _ConfigGateway({"statusMonitor": {"intervalSeconds": 60}})
    control = StatusAlertControl(config_gateway=config, status_gateway=_StatusGateway())

    control.update_settings_direct(_CONTEXT, {"intervalSeconds": 86401})

    assert config.value["statusMonitor"]["intervalSeconds"] == 86401
    assert config.update_count == 1
    assert config.get_count == 0

    events: list[Any] = []
    fake = SimpleNamespace(
        update_settings_direct=lambda _ctx, patch: events.append(("update", patch["intervalSeconds"])),
    )
    monkeypatch.setattr(status_alert_menu, "_CONTROL", fake)
    monkeypatch.setattr(status_alert_menu.states, "pop_state", lambda chat_id: events.append(("pop", chat_id)))
    monkeypatch.setattr(status_alert_menu.ui, "send", lambda chat_id, text, **_kwargs: events.append(("send", chat_id, text)))
    monkeypatch.setattr(status_alert_menu, "send_new", lambda chat_id: events.append(("send_new", chat_id)))

    status_alert_menu._on_interval_input(42, "86401")

    assert events == [
        ("update", 86401),
        ("pop", 42),
        ("send", 42, "✅ 轮询间隔已更新为 <code>86401s</code>"),
        ("send_new", 42),
    ]


# P09 -----------------------------------------------------------------------


def test_p09_update_confirm_reuses_staged_read_and_preserves_save_edit_confirm_order(monkeypatch) -> None:
    events: list[Any] = []
    gateway = _UpdateGateway(events)
    control = UpdateControl(update_gateway=gateway)
    monkeypatch.setattr(update_menu, "_CONTROL", control)
    monkeypatch.setattr(
        update_menu.ui,
        "answer_cb",
        lambda cb_id, text=None, **_kwargs: events.append(("answer", cb_id, text)),
    )
    monkeypatch.setattr(
        update_menu.ui,
        "edit",
        lambda chat_id, message_id, text, **_kwargs: events.append(("edit", chat_id, message_id, text)),
    )
    monkeypatch.setattr(
        update_menu.ui,
        "send",
        lambda chat_id, text, **_kwargs: events.append(("send", chat_id, text)),
    )

    update_menu._confirm_restart(42, 77, "cb-update")

    assert gateway.state_calls == 1
    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "answer",
        "state",
        "save_state",
        "edit",
        "confirm_restart",
        "send",
    ]
    assert events[0] == ("answer", "cb-update", "正在重启…")
    assert events[2] == ("save_state", 42, 77)
    assert "正在重启生效" in events[3][3]
    assert "重启触发失败" in events[5][2]


# P10 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "oauth", "expected", "expected_events"),
    [
        (
            _ConfigGateway({"channels": [{"name": " A "}, {"name": "  "}]}),
            "fail",
            ["api:A"],
            ["config", "oauth"],
        ),
        (
            "fail",
            [{"key": "account-a"}],
            ["oauth:account-a"],
            ["config", "oauth", ("account-key", "account-a")],
        ),
    ],
)
def test_p10_logs_channel_sources_trim_and_fail_independently(config, oauth, expected, expected_events) -> None:
    events: list[Any] = []

    class Config:
        def get(self):
            events.append("config")
            if config == "fail":
                raise RuntimeError("config unavailable")
            return config.get()

    class OAuth:
        def list_accounts(self):
            events.append("oauth")
            if oauth == "fail":
                raise RuntimeError("oauth unavailable")
            return deepcopy(oauth)

        def _account_key(self, account):
            events.append(("account-key", account["key"]))
            return account["key"]

    values = LogsControl(config=Config(), oauth_manager=OAuth()).configured_channels(_CONTEXT)

    assert values == expected
    assert events == expected_events


# P11 -----------------------------------------------------------------------


def test_p11_tg_direct_writes_do_not_read_back_after_commit() -> None:
    stats_config = _ConfigGateway({}, fail_get_after=0)
    StatsControl(config=stats_config).update_preferences_direct(_CONTEXT, {"byChannel": False})
    assert stats_config.value["telegram"]["statsVisibility"]["byChannel"] is False
    assert (stats_config.update_count, stats_config.get_count) == (1, 0)

    alert_config = _ConfigGateway(
        {"statusMonitor": {"targets": ["claude", "openai"], "intervalSeconds": 60}},
        fail_get_after=0,
    )
    StatusAlertControl(
        config_gateway=alert_config,
        status_gateway=_StatusGateway(),
    ).update_settings_direct(_CONTEXT, {"enabled": False})
    assert alert_config.value["statusMonitor"]["enabled"] is False
    assert (alert_config.update_count, alert_config.get_count) == (1, 0)

    target_config = _ConfigGateway(
        {"statusMonitor": {"targets": ["claude", "openai"], "intervalSeconds": 60}},
        fail_get_after=1,
    )
    status_gateway = _StatusGateway()
    removed = StatusAlertControl(
        config_gateway=target_config,
        status_gateway=status_gateway,
    ).toggle_target_direct(_CONTEXT, "claude")
    assert removed is True
    assert target_config.value["statusMonitor"]["targets"] == ["openai"]
    assert status_gateway.forgotten == ["claude"]
    assert (target_config.update_count, target_config.get_count) == (1, 1)

    update_config = _ConfigGateway({}, fail_get_after=0)
    update_gateway = _UpdateGateway()
    update_control = UpdateControl(config_gateway=update_config, update_gateway=update_gateway)
    update_control.set_settings_direct(_CONTEXT, {"enabled": False})
    update_control.clear_ignored_direct(_CONTEXT)
    assert update_config.value["updateChecker"]["enabled"] is False
    assert (update_config.update_count, update_config.get_count) == (1, 0)
    assert update_gateway.clear_calls == 1


def test_p11_management_writes_still_return_read_back_dtos() -> None:
    stats_config = _ConfigGateway({})
    stats = StatsControl(config=stats_config)
    result = stats.update_preferences(_CONTEXT, {"byChannel": False})
    assert result["byChannel"] is False
    assert stats_config.get_count == 2

    alert_config = _ConfigGateway({"statusMonitor": {"intervalSeconds": 60}})
    alert = StatusAlertControl(config_gateway=alert_config, status_gateway=_StatusGateway())
    assert alert.update_settings(_CONTEXT, {"enabled": False}).enabled is False
    assert alert_config.get_count == 1


def test_p11_stats_callback_is_answered_before_redraw_read_failure(monkeypatch) -> None:
    events: list[Any] = []

    class Control:
        reads = 0

        def get_preferences(self, _context):
            self.reads += 1
            events.append(("read", self.reads))
            if self.reads > 1:
                raise RuntimeError("redraw read failed")
            return {"byChannel": True}

        def update_preferences_direct(self, _context, patch):
            events.append(("write", patch))

    monkeypatch.setattr(stats_menu, "_CONTROL", Control())
    monkeypatch.setattr(
        stats_menu.ui,
        "answer_cb",
        lambda cb_id, text=None, **_kwargs: events.append(("answer", cb_id, text)),
    )

    with pytest.raises(RuntimeError, match="redraw read failed"):
        stats_menu.toggle_visibility(42, 77, "cb-stats", "0", "byChannel")

    assert events == [
        ("read", 1),
        ("write", {"byChannel": False}),
        ("answer", "cb-stats", "已隐藏"),
        ("answer", "", None),
        ("read", 2),
    ]


def test_p11_status_alert_and_update_callbacks_answer_before_redraw_failure(monkeypatch) -> None:
    alert_events: list[Any] = []

    class AlertControl:
        reads = 0

        def get_settings(self, _context):
            self.reads += 1
            alert_events.append(("read", self.reads))
            if self.reads > 1:
                raise RuntimeError("alert redraw failed")
            return SimpleNamespace(
                enabled=True,
                interval_seconds=60,
                targets=("claude", "openai", "cloudflare"),
                min_impact="minor",
                notification_enabled=True,
            )

        def update_settings_direct(self, _context, patch):
            alert_events.append(("write", patch))

    monkeypatch.setattr(status_alert_menu, "_CONTROL", AlertControl())
    monkeypatch.setattr(
        status_alert_menu.ui,
        "answer_cb",
        lambda cb_id, text=None, **_kwargs: alert_events.append(("answer", cb_id, text)),
    )
    with pytest.raises(RuntimeError, match="alert redraw failed"):
        status_alert_menu._toggle_enabled(42, 77, "cb-alert")
    assert alert_events == [
        ("read", 1),
        ("write", {"enabled": False}),
        ("answer", "cb-alert", "已关闭"),
        ("read", 2),
    ]

    update_events: list[Any] = []

    class UpdatesControl:
        reads = 0

        def get_settings(self, _context):
            self.reads += 1
            update_events.append(("read", self.reads))
            if self.reads > 1:
                raise RuntimeError("update redraw failed")
            return SimpleNamespace(
                enabled=True,
                include_prerelease=True,
                auto_update=False,
                silent_update=False,
                keep_backups=1,
                interval_seconds=3600,
                ignored_versions=(),
            )

        def repository(self, _context):
            update_events.append("repository")
            return "danger-dream/Parrot"

        def set_settings_direct(self, _context, patch):
            update_events.append(("write", patch))

    monkeypatch.setattr(update_menu, "_CONTROL", UpdatesControl())
    monkeypatch.setattr(
        update_menu.ui,
        "answer_cb",
        lambda cb_id, text=None, **_kwargs: update_events.append(("answer", cb_id, text)),
    )
    with pytest.raises(RuntimeError, match="update redraw failed"):
        update_menu._toggle_enabled(42, 77, "cb-update")
    assert update_events == [
        ("read", 1),
        "repository",
        ("write", {"enabled": False}),
        ("answer", "cb-update", "已关闭"),
        ("read", 2),
    ]


# P12 -----------------------------------------------------------------------


def test_p12_response_dict_uses_frozen_raw_fallback_while_sqlite_text_stays_structured() -> None:
    payload = {
        "choices": [{
            "message": {"role": "assistant", "content": "structured answer"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
    }

    from_dict = inspector.parse_response_body(payload)
    from_text = inspector.parse_response_body(json.dumps(payload))

    assert len(from_dict) == 1
    assert from_dict[0]["kind"] == "raw_response"
    assert from_dict[0]["text"] == str(payload)
    assert [item["kind"] for item in from_text] == ["assistant", "finish", "usage"]
    assert from_text[0]["text"] == "structured answer"


# P13 -----------------------------------------------------------------------


@pytest.mark.parametrize("invalid_page", ["not-a-page", []])
def test_p13_media_invalid_page_falls_back_to_visible_page_one(invalid_page) -> None:
    class MediaDb:
        def __init__(self) -> None:
            self.recent_calls: list[tuple[int, int]] = []

        def count(self):
            return 12

        def recent(self, limit, *, offset):
            self.recent_calls.append((limit, offset))
            return [{"id": "first-page-row"}]

        def summary(self):
            return {"total": 12}

    db = MediaDb()
    rows, summary, page, pages = MediaControl(media_db=db).telegram_page(
        _CONTEXT,
        page=invalid_page,
        page_size=5,
    )

    assert (page, pages) == (1, 3)
    assert rows == [{"id": "first-page-row"}]
    assert summary == {"total": 12}
    assert db.recent_calls == [(5, 0)]
