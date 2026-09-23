"""Actual WS/readers, reset-credit state, login DTO and WHAM gate regressions."""
from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

import pytest

from src.tests.test_openai_oauth_quota import _import_modules, _setup, _add_openai, _low_wham


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DISABLE_OAUTH_NETWORK_CALLS", "1")
    m = _import_modules()
    _setup(m)
    _add_openai(m, "state-path@example.test")
    key = "openai:state-path@example.test:acct-state-path@example.test"
    m["registry"].rebuild_from_config()
    monkeypatch.setattr(m["oauth_manager"].notifier, "notify_event", lambda *a, **k: None)
    return m, key, m["registry"].get_channel("oauth:" + key)


def high(m, ch, *, secondary=False, observed_ms=None):
    headers = {
        "x-codex-primary-used-percent": "99",
        "x-codex-primary-window-minutes": "300",
        "x-codex-primary-reset-after-seconds": "3600",
    }
    if secondary:
        headers.update({"x-codex-secondary-used-percent": "98",
                        "x-codex-secondary-window-minutes": "10080",
                        "x-codex-secondary-reset-after-seconds": "7200"})
    snap = m["openai_provider"].parse_rate_limit_headers(headers)
    snap["fetched_at"] = observed_ms if observed_ms is not None else int(time.time() * 1000) - 1000
    m["failover"]._maybe_record_codex_snapshot(ch, None, snapshot=snap)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["reset", "alreadyRedeemed", "new_observation", "missing_window", "fetch_failed", "recreated"])
async def test_reset_receipt_invalidates_only_obsolete_same_generation_evidence(env, monkeypatch, case):
    m, key, ch = env
    om = m["oauth_manager"]
    high(m, ch, secondary=case == "missing_window")
    original = copy.deepcopy(om.get_account(key))
    async def token(*args, **kwargs):
        return "fake-token"
    async def consume(*args, **kwargs):
        assert kwargs["credit_id"] == "card"
        return {"outcome": case if case == "alreadyRedeemed" else "reset", "windows_reset": 1}
    async def usage(*args, **kwargs):
        if case == "fetch_failed":
            raise RuntimeError("offline failure")
        if case == "recreated":
            om.delete_account(key)
            _add_openai(m, "state-path@example.test")
            return _low_wham()
        if case == "new_observation":
            # New quota evidence delivered during the awaited upstream work.
            high(m, ch, observed_ms=int(time.time() * 1000) + 1)
        result = _low_wham()
        if case == "missing_window":
            result["seven_day"] = {}
        return result
    monkeypatch.setattr(om, "ensure_valid_token", token)
    monkeypatch.setattr(m["openai_provider"], "consume_rate_limit_reset_credit", consume)
    monkeypatch.setattr(om, "fetch_usage_snapshot", usage)
    result = await om.redeem_openai_rate_limit_reset_credit(key, idempotency_key="idem", credit_id="card")
    action = result["quota_action"]["action"]
    acc = om.get_account(key)
    if case in {"reset", "alreadyRedeemed"}:
        assert action == "resumed", result
        assert acc["enabled"] is True
        assert not acc.get("quota_observation")
        row = m["state_db"].quota_load(key)
        assert row["five_hour_util"] == 1
        assert row["codex_primary_used_pct"] is None
        assert not om._cached_openai_codex_quota_hit(key, 95)["any_over"]
    elif case == "recreated":
        assert action == "noop_missing"
        assert acc["enabled"] is True
        assert m["state_db"].quota_load(key) is None
    elif case == "fetch_failed":
        assert action == "refresh_failed_keep_disabled"
        assert acc["quota_observation"] == original["quota_observation"]
        assert acc["enabled"] is False
    else:
        assert action == "still_over_quota", result
        assert acc["enabled"] is False
        windows = om._codex_windows_from_observation(acc["quota_observation"])
        if case == "missing_window":
            assert set(windows) == {"seven_day"}
        else:
            assert windows["five_hour"]["observed_at"] > original["quota_observation"]["observed_at"]


@pytest.mark.asyncio
async def test_native_ws_before_and_after_visible_output_observes_all_families(env):
    from src.openai import responses_ws as ws
    m, key, ch = env
    def event(name, used):
        return json.dumps({"type": "codex.rate_limits", "metered_limit_name": name,
                           "rate_limits": {"primary": {"used_percent": used, "window_minutes": 300,
                                                       "reset_at": int(time.time()) + 3600}},
                           "credits": {"has_credits": True, "balance": "9"}})
    frames = [event("codex", 99), json.dumps({"type": "response.output_text.delta", "delta": "ok"}),
              event("gpt-reserve", 22)]
    class FakeWS:
        async def recv(self):
            return frames.pop(0)
    upstream = FakeWS()
    tracker = ws._WsTracker()
    timing = ws.WsAttemptTiming(route_type="direct")
    timing.mark_handshake_complete()
    pending = []
    visible = await ws._recv_until_first_visible_ws_event(
        upstream, tracker, pending, ch.key, 5, channel=ch, deadline_ts=time.time() + 20,
        idle_timeout=5, result=ws._WsAttemptResult(), proxy_bytes=ws._WsProxyBytes(),
        translator_ctx={}, timing=timing, round_timeouts=ws.RoundTimeouts(5, 5, 5, 20))
    assert "response.output_text.delta" in visible
    assert m["state_db"].quota_load(key)["codex_primary_used_pct"] == 99
    assert m["oauth_manager"].get_account(key)["enabled"] is False
    step = await ws.read_next_responses_ws_step(
        upstream, tracker, channel_key=ch.key, deadline_ts=time.time() + 20, idle_timeout=5,
        proxy_bytes=ws._WsProxyBytes(), skip_event_types=(),
        on_text_frame=lambda frame: ws._capture_codex_response_event(ch, {}, frame),
        timing=timing, round_timeouts=ws.RoundTimeouts(5, 5, 5, 20))
    row = m["state_db"].quota_load(key)
    assert {v["limit_id"] for v in json.loads(row["codex_rate_limits"])} == {"codex", "gpt_reserve"}
    assert any("gpt-reserve" in frame for frame in tracker._frames)
    # Retired channels cannot write into the re-created account.
    m["oauth_manager"].delete_account(key)
    _add_openai(m, "state-path@example.test")
    ws._capture_codex_response_event(ch, {}, event("codex", 99))
    assert m["state_db"].quota_load(key) is None
    assert m["oauth_manager"].get_account(key)["enabled"] is True


@pytest.mark.parametrize("entry_kind", ["management", "telegram"])
@pytest.mark.parametrize("routing", ["absent", "partial", "invalid", "clear", "new"])
def test_login_route_presence_and_atomic_pair_survive_dto_and_save(env, monkeypatch, entry_kind, routing):
    from src.management_control.oauth.flows import OAuthFlowService
    from src.openai.codex_constants import apply_codex_workspace_routing
    m, key, _ = env
    om = m["oauth_manager"]
    old = copy.deepcopy(om.get_account(key))
    old.update(workspace_backend_origin="https://gov.chatgpt.com", account_routing_override="us_cr")
    om.replace_exact_identity(key, old)
    token = {"access_token": "new-token", "refresh_token": "new-refresh", "id_token": "fake", "expires_in": 3600}
    changes = {
        "absent": {}, "partial": {"workspace_backend_origin": ""},
        "invalid": {"workspace_backend_origin": "https://[", "account_routing_override": "us"},
        "clear": {"workspace_backend_origin": "", "account_routing_override": ""},
        "new": {"workspace_backend_origin": "NO_CONSTRAINT", "account_routing_override": "us"},
    }
    token.update(changes[routing])
    info = {"email": old["email"], "workspace_id": old["workspace_id"], "chatgpt_account_id": old["chatgpt_account_id"]}
    if entry_kind == "management":
        backend = SimpleNamespace(openai_decode_id_token=lambda _: {}, openai_extract_user_info=lambda _: info)
        entry = OAuthFlowService(backend).openai_entry(token)
    else:
        control = m["oauth_menu"].oauth_control
        monkeypatch.setattr(control, "openai_decode_id_token", lambda _: {})
        monkeypatch.setattr(control, "openai_extract_user_info", lambda _: info)
        entry, _ = m["oauth_menu"]._openai_token_to_entry(token)
    assert om.replace_exact_identity(key, entry)["status"] == "replaced"
    acc = om.get_account(key)
    url, headers = apply_codex_workspace_routing("https://chatgpt.com/backend-api/codex/responses", {}, acc)
    if routing in {"absent", "partial", "invalid"}:
        assert url.startswith("https://gov.chatgpt.com/")
        assert headers["x-openai-account-routing-override"] == "us_cr"
    elif routing == "clear":
        assert url.startswith("https://chatgpt.com/")
        assert "x-openai-account-routing-override" not in headers
    else:
        assert url.startswith("https://chatgpt.com/")
        assert headers["x-openai-account-routing-override"] == "us"


def test_wham_spend_gate_roundtrips_cache_and_tg_without_percentage_windows(env):
    m, key, _ = env
    om = m["oauth_manager"]
    payload = {"rate_limit": None, "credits": None,
               "spend_control": {"reached": True, "individual_limit": None}}
    usage = m["openai_provider"].normalize_wham_usage(payload)
    m["state_db"].quota_save(key, om.flatten_usage(usage))
    row = m["state_db"].quota_load(key)
    assert om.usage_from_quota_row(row)["openai"]["spend_control"]["reached"] is True
    result = om.evaluate_and_toggle_by_cached_quota(key)
    assert result["action"] == "wham_limit_disabled"
    assert result["hit_windows"] == ["Workspace spend limit"]
    assert "工作区消费上限已达到" in m["oauth_menu"]._format_usage_block(key)
    assert om.evaluate_and_toggle_by_usage(key, _low_wham(spend_control={"reached": True}), fresh=True)["any_over"]
    payload["spend_control"]["reached"] = False
    recovered = m["openai_provider"].normalize_wham_usage(payload)
    assert om._usage_has_any_quota_signal(recovered)
    m["state_db"].quota_save(key, om.flatten_usage(recovered))
    assert om.evaluate_and_toggle_by_usage(key, recovered, fresh=False)["action"] == "quota_stale_keep_disabled"
    assert om.evaluate_and_toggle_by_usage(key, recovered, fresh=True)["action"] == "resumed"
