"""Regression coverage for partial Codex quota observations and TG display."""
from __future__ import annotations

import json
import time

import pytest

from src.tests.test_openai_oauth_quota import (
    _import_modules, _setup, _add_openai, _MockResp, _UiRecorder,
    _preheat_oauth_menu_windows, _low_wham,
)


def _account(m, name):
    _setup(m)
    email = f"{name}@quota.test"
    _add_openai(m, email)
    key = f"openai:{email}:acct-{email}"
    m["registry"].rebuild_from_config()
    return key, m["registry"].get_channel(f"oauth:{key}")


def _save_headers(m, key, headers, clock_ms):
    snap = m["openai_provider"].parse_rate_limit_headers(headers)
    snap["fetched_at"] = clock_ms
    m["state_db"].quota_save_openai_snapshot(key, snap)
    return snap


@pytest.mark.parametrize("fragment", ["credits", "additional", "secondary"])
@pytest.mark.parametrize("absolute", [False, True])
def test_partial_snapshot_does_not_extend_expired_window(m, monkeypatch, fragment, absolute):
    key, _ = _account(m, f"expire-{fragment}-{absolute}")
    now = int(time.time())
    clock = [now - 90]
    monkeypatch.setattr(m["openai_provider"].time, "time", lambda: clock[0])
    reset_header = "x-codex-primary-reset-at" if absolute else "x-codex-primary-reset-after-seconds"
    snap = _save_headers(m, key, {
        "x-codex-primary-used-percent": "99",
        "x-codex-primary-window-minutes": "300",
        reset_header: str(now - 30 if absolute else 60),
    }, clock[0] * 1000)
    original = m["state_db"].quota_load(key)
    clock[0] = now
    headers = {
        "credits": {"x-codex-credits-balance": "9"},
        "additional": {
            "x-gpt-reserve-primary-used-percent": "20",
            "x-gpt-reserve-primary-window-minutes": "60",
        },
        "secondary": {
            "x-codex-secondary-used-percent": "10",
            "x-codex-secondary-window-minutes": "10080",
        },
    }[fragment]
    _save_headers(m, key, headers, now * 1000)
    row = m["state_db"].quota_load(key)
    observations = json.loads(row["codex_window_observations"])
    assert observations["five_hour"]["observed_at"] == snap["fetched_at"]
    assert row["five_hour_reset"] == original["five_hour_reset"]
    candidate = m["oauth_manager"]._codex_window_candidates(key, row)["five_hour"][0]
    assert candidate["observed_ms"] == (now - 90) * 1000
    assert candidate["reset_ms"] == (now - 30) * 1000
    assert not m["oauth_manager"]._cached_openai_codex_quota_hit(key, 95)["any_over"]
    m["oauth_manager"].set_disabled_by_quota(key, original["five_hour_reset"])
    result = m["oauth_manager"].evaluate_and_toggle_by_usage(key, _low_wham(), threshold=95, fresh=True)
    assert result["action"] == "resumed", result


@pytest.mark.parametrize("passive_newer", [False, True])
def test_family_and_credit_clocks_survive_unrelated_fragments(m, passive_newer):
    key, _ = _account(m, f"source-order-{passive_newer}")
    base = int(time.time() * 1000) - 10000
    passive_at, active_at = (base + 1000, base) if passive_newer else (base, base + 1000)

    def passive():
        _save_headers(m, key, {
            "x-codex-primary-used-percent": "90",
            "x-codex-primary-window-minutes": "300",
            "x-codex-credits-has-credits": "true",
            "x-codex-credits-unlimited": "false",
            "x-codex-credits-balance": "1",
            "x-gpt-reserve-primary-used-percent": "70",
            "x-gpt-reserve-primary-window-minutes": "60",
            "x-gpt-reserve-secondary-used-percent": "80",
            "x-gpt-reserve-secondary-window-minutes": "10080",
            "x-gpt-reserve-secondary-reset-after-seconds": "120",
        }, passive_at)

    def active():
        usage = m["openai_provider"].normalize_wham_usage({
            "rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 18000}},
            "credits": {"has_credits": True, "unlimited": False, "balance": "9"},
            "additional_rate_limits": [{
                "metered_feature": "gpt_reserve", "limit_name": "Reserve",
                "rate_limit": {"primary_window": {"used_percent": 20, "limit_window_seconds": 3600}},
            }],
        })
        flat = m["oauth_manager"].flatten_usage(usage)
        m["state_db"].quota_save(key, {**flat, "raw_data": json.dumps(usage), "fetched_at": active_at})

    for write in ([active, passive] if passive_newer else [passive, active]):
        write()
    _save_headers(m, key, {
        "x-gpt-reserve-primary-used-percent": "40",
        "x-gpt-reserve-primary-window-minutes": "60",
    }, base + 2000)
    # A partial credits observation must not refresh the untouched balance.
    _save_headers(m, key, {"x-codex-credits-unlimited": "true"}, base + 3000)
    row = m["state_db"].quota_load(key)
    usage = m["oauth_manager"].usage_from_quota_row(row)
    families = {item["limit_id"]: item for item in usage["openai"]["rate_limits"]}
    assert families["codex"]["primary"]["used_percent"] == (90 if passive_newer else 10)
    assert usage["five_hour"]["utilization"] == (90 if passive_newer else 10)
    assert usage["openai"]["credits"]["balance"] == ("1" if passive_newer else "9")
    assert usage["openai"]["credits"]["unlimited"] is True
    reserve = families["gpt_reserve"]
    assert reserve["primary"]["used_percent"] == 40
    assert reserve["primary"]["observed_at"] == base + 2000
    assert reserve["secondary"]["used_percent"] == 80
    assert reserve["secondary"]["observed_at"] == passive_at
    assert reserve["secondary"]["reset_at"] == passive_at // 1000 + 120
    assert row["codex_active_observed_at"] == active_at
    assert json.loads(row["codex_credits_observed_at"])["balance"] == passive_at


def test_legacy_row_clocks_are_frozen_before_partial_update(m):
    key, _ = _account(m, "legacy-clocks")
    old = int(time.time() * 1000) - 90000
    # Simulate a persisted pre-clock row at the storage mutation boundary.
    legacy = {
        "account_key": key, "fetched_at": old + 1000, "last_passive_update_at": old,
        "codex_primary_used_pct": 99, "codex_primary_reset_sec": 60,
        "codex_primary_window_min": 300, "codex_credits_balance": "1",
        "codex_rate_limits": json.dumps([{
            "limit_id": "codex", "primary": {"used_percent": 90, "reset_after_seconds": 60},
        }]),
        "raw_data": json.dumps({"openai": {
            "credits": {"balance": "9"},
            "rate_limits": [{"limit_id": "codex", "primary": {"used_percent": 10}}],
        }}),
    }
    m["state_db"]._quota_write(key, lambda rows, target: rows.__setitem__(target, legacy))
    for offset in (90000, 91000):
        _save_headers(m, key, {"x-gpt-reserve-primary-used-percent": "7"}, old + offset)
    row = m["state_db"].quota_load(key)
    candidate = m["oauth_manager"]._codex_window_candidates(key, row)["five_hour"][0]
    assert candidate["observed_ms"] == old
    assert candidate["reset_ms"] == old + 60000
    usage = m["oauth_manager"].usage_from_quota_row(row)["openai"]
    assert usage["credits"]["balance"] == "9"
    assert usage["rate_limits"][0]["primary"]["used_percent"] == 10


def test_credits_do_not_make_old_wham_supersede_codex(m):
    key, _ = _account(m, "wham-clock")
    now = int(time.time() * 1000)
    old = now - 16 * 60 * 1000
    _save_headers(m, key, {
        "x-codex-primary-used-percent": "99",
        "x-codex-primary-window-minutes": "300",
        "x-codex-primary-reset-after-seconds": "3600",
    }, old)
    usage = _low_wham()
    m["state_db"].quota_save(key, {
        **m["oauth_manager"].flatten_usage(usage),
        "raw_data": json.dumps(usage), "fetched_at": old + 1000,
    })
    _save_headers(m, key, {"x-codex-credits-balance": "9"}, now)
    hit = m["oauth_manager"]._cached_openai_codex_quota_hit(key, 95, usage=usage)
    assert hit["any_over"] is True
    assert hit["hit_windows"] == ["codex primary 99%"]


def _event(name, used, reset):
    return {
        "type": "codex.rate_limits", "metered_limit_name": name,
        "rate_limits": {"primary": {"used_percent": used, "window_minutes": 300, "reset_at": reset}},
    }


def test_ws_events_sample_families_independently_through_storage(m, monkeypatch):
    key, channel = _account(m, "event-families")
    base = int(time.time())
    clock = [base]
    monkeypatch.setattr(m["failover"].time, "time", lambda: clock[0])
    tracker = m["failover"]._WsResponsesTracker(channel)

    def emit(name, used):
        tracker.feed_text(json.dumps(_event(name, used, base + 3600)))

    emit("codex", 10)
    clock[0] += 1
    emit("gpt-reserve", 20)
    families = json.loads(m["state_db"].quota_load(key)["codex_rate_limits"])
    assert {f["limit_id"]: f["primary"]["used_percent"] for f in families} == {"codex": 10, "gpt_reserve": 20}
    # Same-family updates are still throttled; the boundary is inclusive at 30s.
    clock[0] = base + 29.999
    emit("codex", 30)
    assert m["state_db"].quota_load(key)["five_hour_util"] == 10
    clock[0] = base + 30
    emit("codex", 30)
    emit("gpt-reserve", 40)
    families = {f["limit_id"]: f for f in json.loads(m["state_db"].quota_load(key)["codex_rate_limits"])}
    assert families["codex"]["primary"]["used_percent"] == 30
    assert families["gpt_reserve"]["primary"]["used_percent"] == 20
    clock[0] = base + 31
    emit("gpt-reserve", 40)
    families = {f["limit_id"]: f for f in json.loads(m["state_db"].quota_load(key)["codex_rate_limits"])}
    assert families["gpt_reserve"]["primary"]["used_percent"] == 40
    assert tracker._frames == []  # Internal event, not client output.
    m["oauth_manager"].delete_account(key)
    assert key not in m["failover"]._codex_snapshot_family_last
    emit("gpt-reserve", 50)  # Late event cannot recreate the deleted row/bucket.
    assert m["state_db"].quota_load(key) is None
    assert key not in m["failover"]._codex_snapshot_family_last


def test_credits_only_event_does_not_consume_codex_sample(m, monkeypatch):
    key, channel = _account(m, "credits-event")
    base = int(time.time())
    clock = [base]
    monkeypatch.setattr(m["failover"].time, "time", lambda: clock[0])
    tracker = m["failover"]._WsResponsesTracker(channel)
    tracker.feed_text(json.dumps({"type": "codex.rate_limits", "credits": {"balance": "0"}}))
    clock[0] += 1
    tracker.feed_text(json.dumps(_event("codex", 10, base + 3600)))
    row = m["state_db"].quota_load(key)
    assert row["five_hour_util"] == 10
    assert row["codex_credits_balance"] == "0"
    assert json.loads(row["codex_credits_observed_at"])["balance"] == base * 1000


def test_new_header_family_does_not_resample_throttled_codex(m, monkeypatch):
    key, channel = _account(m, "mixed-headers")
    base = int(time.time())
    clock = [base]
    monkeypatch.setattr(m["failover"].time, "time", lambda: clock[0])
    headers = {"x-codex-primary-used-percent": "10", "x-codex-primary-window-minutes": "300"}
    m["failover"]._maybe_record_codex_snapshot(channel, _MockResp(headers))
    clock[0] += 1
    m["failover"]._maybe_record_codex_snapshot(channel, _MockResp({
        **headers, "x-codex-primary-used-percent": "20",
        "x-gpt-reserve-primary-used-percent": "30", "x-codex-credits-balance": "9",
    }))
    row = m["state_db"].quota_load(key)
    assert row["five_hour_util"] == 10
    assert row["codex_credits_balance"] == "9"
    assert json.loads(row["codex_window_observations"])["five_hour"]["observed_at"] == base * 1000
    assert {f["limit_id"] for f in json.loads(row["codex_rate_limits"])} == {"codex", "gpt_reserve"}


@pytest.mark.parametrize("mode", ["used", "remaining"])
@pytest.mark.parametrize("source", ["headers", "wham"])
def test_tg_detail_renders_credits_and_named_limits(m, monkeypatch, source, mode):
    key, channel = _account(m, f"tg-{source}-{mode}")
    menu = m["oauth_menu"]
    monkeypatch.setattr(menu, "_usage_display_mode", lambda: mode)
    if source == "headers":
        m["failover"]._maybe_record_codex_snapshot(channel, _MockResp({
            "x-codex-primary-used-percent": "10", "x-codex-primary-window-minutes": "300",
            "x-codex-credits-balance": "9.5", "x-codex-credits-unlimited": "true",
            "x-gpt-reserve-limit-name": "Reserve <&>",
            "x-gpt-reserve-primary-used-percent": "20", "x-gpt-reserve-primary-window-minutes": "60",
            "x-gpt-reserve-primary-reset-after-seconds": "3600",
        }))
    else:
        usage = m["openai_provider"].normalize_wham_usage({
            "rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 18000}},
            "credits": {"balance": "9.5", "unlimited": True},
            "additional_rate_limits": [{
                "metered_feature": "gpt-reserve", "limit_name": "Reserve <&>",
                "normal_model_slug": "gpt-5.4",
                "rate_limit": {"primary_window": {
                    "used_percent": 20, "limit_window_seconds": 3600, "reset_after_seconds": 3600,
                }},
            }],
        })
        m["state_db"].quota_save(key, {
            **m["oauth_manager"].flatten_usage(usage), "raw_data": json.dumps(usage),
        })
    _preheat_oauth_menu_windows(m)
    rec = _UiRecorder()
    monkeypatch.setattr(m["ui"], "api", rec)
    menu.on_view(42, 100, "cb", m["ui"].register_code(key))
    sent = rec.last("editMessageText")
    assert sent
    text = sent["text"]
    credit_line = next(line for line in text.splitlines() if "Credits:" in line)
    assert "不限量" in credit_line and "余额 9.5" in credit_line and "$" not in credit_line
    quota_line = next(line for line in text.splitlines() if "Reserve &lt;&amp;&gt;" in line)
    assert "1h" in quota_line and "重置:" in quota_line
    assert ("剩余 80%" if mode == "remaining" else "已用 20%") in quota_line
    assert "⏱ 5h" in text and "Codex 原始窗口" not in text


def test_codex_reset_at_accepts_seconds_but_not_milliseconds_or_bad_values(m):
    parse = m["openai_provider"].parse_codex_reset_at
    now = 1_800_000_000
    for delta in (5 * 3600, 7 * 86400, 30 * 86400, 45 * 86400):
        assert parse(str(now + delta), observed_at=now, window_minutes=None) == now + delta
    assert parse(now - 10 * 86400, observed_at=now) == now - 10 * 86400
    assert parse(float(now + 3600), observed_at=now + .5) == now + 3600
    assert parse(now + 3600, observed_at=float("inf")) is None
    for bad in (True, False, None, "", "NaN", float("inf"), float("nan"),
                0, -1, 3600, now + 45 * 86400 + 1, (now + 60) * 1000,
                4_070_908_800, now + .5):
        assert parse(bad, observed_at=now, window_minutes=43200) is None
    assert parse(now + 3600, observed_at=now * 1000) is None


def test_codex_header_active_limit_only_and_reset_fallback(m):
    p = m["openai_provider"]
    active_only = p.parse_rate_limit_headers({"X-Codex-Active-Limit": " GPT-Reserve "})
    assert active_only["active_limit"] == "gpt_reserve"
    assert active_only["rate_limits"] == []
    assert p.parse_rate_limit_headers({"X-Codex-Active-Limit": "   "}) is None
    now = int(time.time())
    snap = p.parse_rate_limit_headers({
        "X-Codex-Active-Limit": "CODEx",
        "X-Codex-Primary-Used-Percent": "95",
        "X-Codex-Primary-Reset-At": str((now + 90) * 1000),
        "X-Codex-Primary-Reset-After-Seconds": "90",
        "X-Codex-Secondary-Used-Percent": "10",
        "X-Codex-Secondary-Reset-At": "4070908800",
        "X-Codex-Secondary-Reset-After-Seconds": "9999999999",
    })
    assert snap["active_limit"] == "codex"
    assert snap["primary_reset_at"] is None
    assert snap["primary_reset_sec"] == 90
    assert snap["secondary_reset_at"] is None
    assert snap["secondary_reset_sec"] is None
    merged = p.merge_codex_rate_limits((snap["rate_limits"], now * 1000))
    assert merged[0]["primary"]["reset_at"] == now + 90
    assert merged[0]["secondary"].get("reset_at") is None


def test_codex_ws_and_wham_reject_invalid_absolute_and_use_valid_relative(m):
    p = m["openai_provider"]
    now = int(time.time())
    snap = p.parse_rate_limit_event({
        "type": "codex.rate_limits",
        "rate_limits": {"primary": {"used_percent": 20, "window_minutes": 300,
                                    "reset_at": (now + 60) * 1000, "reset_after_seconds": 60},
                        "secondary": {"used_percent": 70, "reset_at": 4_070_908_800}},
    })
    assert snap["primary_reset_at"] is None
    assert snap["primary_reset_sec"] == 60
    assert snap["secondary_reset_at"] is None
    assert snap["secondary_reset_sec"] is None
    merged = p.merge_codex_rate_limits((snap["rate_limits"], now * 1000))
    assert merged[0]["primary"]["reset_at"] == now + 60
    usage = p.normalize_wham_usage({"rate_limit": {"primary_window": {
        "used_percent": 20, "limit_window_seconds": 18000,
        "reset_at": (now + 60) * 1000, "reset_after_seconds": 60,
    }, "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800,
                            "reset_at": 4_070_908_800}}})
    assert usage["openai"]["rate_limits"][0]["primary"]["reset_at"] == now + 60
    assert usage["openai"]["rate_limits"][0]["secondary"]["reset_at"] is None
    assert usage["seven_day"]["resets_at"] is None


def test_codex_same_window_partial_merge_and_cycle_boundary(m):
    p = m["openai_provider"]
    base = 1_800_000_000
    old = [{"limit_id": "codex", "primary": {
        "used_percent": 80, "window_minutes": 300, "reset_after_seconds": 120,
    }, "secondary": {"used_percent": 15, "window_minutes": 10080}},
           {"limit_id": "gpt_reserve", "primary": {"used_percent": 45}}]
    newer = [{"limit_id": "codex", "primary": {"used_percent": 90}}]
    a = p.merge_codex_rate_limits((old, base * 1000), (newer, (base + 30) * 1000))
    family = {item["limit_id"]: item for item in a}["codex"]
    assert family["primary"]["used_percent"] == 90
    assert family["primary"]["reset_at"] == base + 120
    assert family["primary"]["reset_after_seconds"] == 120  # not re-anchored
    assert family["primary"]["window_minutes"] == 300
    assert family["primary"]["observed_at"] == (base + 30) * 1000
    assert family["secondary"]["used_percent"] == 15
    assert {item["limit_id"] for item in a} == {"codex", "gpt_reserve"}
    for observation, stamp, expected_reset in (
        ({"used_percent": 10, "reset_at": base + 3600}, base + 40, base + 3600),
        ({"used_percent": 10, "window_minutes": 60}, base + 40, None),
        ({"used_percent": 10}, base + 121, None),
    ):
        merged = p.merge_codex_rate_limits((a, (base + 30) * 1000),
                                           ([{"limit_id": "codex", "primary": observation}], stamp * 1000))
        primary = merged[0]["primary"]
        assert primary.get("reset_at") == expected_reset
        assert primary.get("window_minutes") == (60 if "window_minutes" in observation else None)
        assert primary["used_percent"] == 10
    stale = p.merge_codex_rate_limits((a, (base + 30) * 1000),
                                      ([{"limit_id": "codex", "primary": {"used_percent": 1}}],
                                       (base + 20) * 1000))
    assert stale[0]["primary"]["used_percent"] == 90


def test_codex_same_window_partial_merge_through_isolated_storage(m):
    key, _ = _account(m, "partial-window-reset")
    now = int(time.time())
    _save_headers(m, key, {"x-codex-primary-used-percent": "90",
                           "x-codex-primary-window-minutes": "300",
                           "x-codex-primary-reset-after-seconds": "120"}, now * 1000)
    _save_headers(m, key, {"x-codex-primary-used-percent": "95"}, (now + 30) * 1000)
    row = m["state_db"].quota_load(key)
    window = json.loads(row["codex_rate_limits"])[0]["primary"]
    assert window["used_percent"] == 95
    assert window["reset_at"] == now + 120
    assert window["window_minutes"] == 300
    assert window["reset_after_seconds"] == 120
