"""WebUI round 3: TG-parity view data (secrets, logs, OAuth, models, stats, channels, keys)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.management_auth import AuthMethod
from src.management_auth.principal import Capability, ManagementPrincipal
from src.management_control import ManagementContext, ManagementError, ManagementErrorCode
from src.management_control.oauth.menu_bridge import telegram_context
from src.tests.management_oauth_fakes import build_control

OPENAI_ID = "openai:admin@example.test:workspace-1"


def _api(*capabilities):
    principal = ManagementPrincipal.administrator(subject_id="web", auth_method=AuthMethod.MANAGEMENT_KEY)
    if capabilities:
        principal = ManagementPrincipal(subject_id="web", auth_method=AuthMethod.MANAGEMENT_KEY,
                                        roles=principal.roles, capabilities=frozenset(capabilities),
                                        issued_at=principal.issued_at)
    return ManagementContext(request_id="r3", actor=principal)


# ── Part 1: API key secret in list/get only ─────────────────────────────────
def test_secret_readable_requires_secrets_capability():
    from src.management_api.routers.apikey import _secret_readable

    assert _secret_readable(_api()) is True
    assert _secret_readable(_api(Capability.READ)) is False


def test_secret_is_requested_only_by_list_and_get_routes():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "management_api/routers/apikey.py").read_text()
    tree = ast.parse(source)
    callers = set()
    for fn in [node for node in tree.body if isinstance(node, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if isinstance(node, ast.keyword) and node.arg == "include_secret":
                callers.add(fn.name)
    assert callers == {"list_api_keys", "get_api_key"}
    # Events, operations and logs carry no ApiKeyData at all.
    for name in ("live_events.py", "logs.py", "foundation.py"):
        text = (Path(__file__).resolve().parents[1] / "management_api/routers" / name).read_text()
        assert "include_secret" not in text and "ApiKeyData" not in text


def test_api_key_list_summary_counts_like_tg():
    from src.management_control.apikey.list_extras import list_summary

    usage = lambda total, inp=0, out=0: SimpleNamespace(total=total, input_tokens=inp, cache_creation_tokens=1 if total else 0,
                                                        cache_read_tokens=0, output_tokens=out)
    views = [SimpleNamespace(enabled=True, month_stats=usage(3, 10, 5)),
             SimpleNamespace(enabled=False, month_stats=usage(0)),
             SimpleNamespace(enabled=True, month_stats=usage(0))]
    assert list_summary(views) == {"total": 3, "active": 1, "disabled": 1, "idle": 2, "month_requests": 3,
                                   "month_prompt_tokens": 11, "month_output_tokens": 5}


def test_history_totals_skip_telegram_and_tolerate_missing_source():
    from src.management_control.apikey.list_extras import history_totals

    assert history_totals(telegram_context(1)) == ({}, False)
    assert history_totals(_api(), SimpleNamespace(request_totals_by_apikey=lambda: {"a": 4})) == ({"a": 4}, False)
    assert history_totals(_api(), SimpleNamespace()) == ({}, True)


# ── Request logs ───────────────────────────────────────────────────────────
def _row(**extra):
    row = {"request_id": "r1", "status": "success", "created_at": 1.0, "api_key_name": "k",
           "requested_model": "m", "final_model": "m", "final_channel_key": "api:chan",
           "final_channel_type": "api", "http_status": 200, "is_stream": 1, "input_tokens": 100,
           "output_tokens": 50, "cache_creation_tokens": 20, "cache_read_tokens": 80,
           "connect_time_ms": 10, "first_token_time_ms": 100, "total_time_ms": 1100,
           "affinity_hit": 1, "ingress_protocol": "responses", "upstream_transport": "ws",
           "reasoning_effort": "high", "actual_service_tier": "priority", "proxy_name": "p1",
           "local_web_count": 2, "upstream_actual_model": "m-2",
           "safety_review": json.dumps({"use_cases": ["x"]})}
    row.update(extra)
    return row


def test_log_list_fields_match_tg_formulas():
    from src.management_control.observability import log_fields

    fields = log_fields.list_fields(_row(), channel=("chan", "kimi"))
    assert fields["promptTokens"] == 200 and fields["cacheReadRatio"] == 0.4
    assert fields["tokensPerSecond"] == 50.0  # 50 tokens / (1100 - 100) ms
    assert fields["speedMode"] == "fast" and fields["websocket"] == "upstream"
    assert fields["affinityHit"] is True and fields["localSearchCount"] == 2
    assert fields["channelName"] == "chan" and fields["upstreamActualModel"] == "m-2"
    assert fields["safetyReview"]["flagged"] is True
    assert log_fields.speed_mode({"actual_service_tier": "ultrafast"}) == "ultrafast"
    assert log_fields.speed_mode({"actual_service_tier": "flex"}) == "tier"
    assert log_fields.speed_mode({"fast_mode": 1}) == "fast"
    assert log_fields.tokens_per_second({"status": "success", "output_tokens": 10, "total_time_ms": 500}) == 20.0


def test_log_error_summary_and_detail_fields():
    from src.management_control.observability import log_fields

    raw = 'HTTP 429: {"error": {"type": "rate_limit", "message": "slow down"}}'
    assert log_fields.error_summary(raw) == "HTTP 429 — rate_limit: slow down"
    assert log_fields.error_summary("plain") == "plain"
    detail = log_fields.detail_fields({"client_ip": "1.2.3.4", "tls_ms": 5, "target_tls_ms": 7, "dns_ms": 1})
    assert detail["clientIp"] == "1.2.3.4" and detail["stageMilliseconds"] == {"dns": 1.0, "targetTls": 7.0}


def test_log_channel_names_resolve_oauth_and_api():
    from src.management_control.observability.log_fields import ChannelNames

    oauth = SimpleNamespace(
        get_account=lambda key: {"email": "a@x", "provider": "openai", "workspace_name": "WS"},
        provider_of=lambda acc: "openai",
        list_accounts=lambda: [{"email": "a@x"}, {"email": "a@x"}],
        account_key_to_email=lambda key: None,
    )
    registry = SimpleNamespace(get_channel=lambda key: SimpleNamespace(provider_id="kimi"))
    names = ChannelNames(oauth, registry)
    assert names("oauth:openai:a@x:ws") == ("a@x · WS", "openai")
    assert names("api:Kimi") == ("Kimi", "kimi")
    assert names(None) == (None, None)


def test_request_log_schema_accepts_round3_fields():
    from src.management_api.schemas.observability import RequestLogData
    from src.management_control.observability import log_fields

    base = {"id": "r1", "status": "success", "retryCount": 0, "inputTokens": 1, "outputTokens": 1,
            "costTicks": 0, "billing": {"costTicks": 0, "actualCostTicks": 0, "estimatedCostTicks": 0,
                                         "actualCostedSuccess": 0, "estimatedCostedSuccess": 0,
                                         "costedSuccess": 0, "unpricedSuccess": 0}, "revision": "x"}
    data = RequestLogData.model_validate({**base, **log_fields.list_fields(_row())})
    assert data.safetyReview.flagged and data.speedMode == "fast"


# ── OAuth list card / detail ──────────────────────────────────────────────
def test_oauth_list_items_carry_tg_card():
    from src.management_api.routers.oauth_support import summary

    control, backend = build_control()
    items = {item.account_id: item for item in control.list_accounts(_api()).items}
    card = summary(items[OPENAI_ID]).card
    assert card.localPeriod.label == "本地自然月" and card.localPeriod.stats is not None
    assert [section.id for section in card.usageSections][:1] == ["windows"]
    assert card.cooldown.permanentModels == 1  # fake gpt-beta permanent cooldown
    invalid = summary(items["claude:invalid@example.test"]).card
    assert invalid.statusTag == "authFailed"
    # Telegram reads keep their own caches: no card computed.
    assert all(item.card is None for item in control.list_accounts(telegram_context(1)).items)


def test_oauth_local_period_matches_tg_rules():
    from datetime import datetime, timezone
    from src.management_control.oauth.account_card import local_period, window_since

    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    claude = local_period("claude", {"subscription_created_at": "2026-01-25T00:00:00Z"}, None, now)
    assert claude["label"] == "套餐本期" and datetime.fromtimestamp(claude["since"], timezone.utc).day == 25
    assert datetime.fromtimestamp(claude["since"], timezone.utc).month == 9
    openai = local_period("openai", {"subscription_expires_at": "2026-11-05T00:00:00Z"}, None, now)
    assert openai["source"] == "openai_inferred_subscription"
    assert local_period("workbuddy", {}, None, now)["source"] == "natural_month"
    assert window_since(None, 3600, 10_000.0) == 6400.0
    assert window_since("1970-01-01T03:00:00Z", 3600, 10_000.0) == 7200.0


def test_oauth_weekly_projection_formula():
    from src.management_control.oauth.account_card import weekly_projection

    stats = {"prompt_tokens": 900, "output_tokens": 100, "cost_ticks": 50, "costed_success": 1}
    assert weekly_projection("claude", {"seven_day_util": 25}, stats) == {"tokens": 4000, "cost_ticks": 200}
    assert weekly_projection("cursor", {"seven_day_util": 25}, stats) is None
    assert weekly_projection("claude", {"seven_day_util": 0}, stats) is None


def test_oauth_detail_has_plan_period_stats_and_typed_errors():
    from src.management_api.routers.oauth_support import detail

    control, _backend = build_control()
    data = detail(control.get_account(_api(), OPENAI_ID))
    assert data.localStats.periodLabel == "本地自然月" and data.localStats.total == data.localStats.requestCount
    assert data.plan is not None
    error = data.runtimeErrors[0]
    assert error.kind == "fault" and error.errorCount == 0
    windows = next(section for section in data.usageSections if section.id == "windows")
    assert all(hasattr(item, "local") for item in windows.items)


def test_oauth_runtime_error_kinds():
    from src.cooldown import ACCOUNT_SCOPE
    from src.management_control.oauth.account_card import runtime_errors

    backend = SimpleNamespace(cursor_disabled_models=lambda account: {"off"})
    pool = json.dumps({"error": {"type": "cursor_pool_quota", "pool": "cursor_models"}})
    entries = [{"channel_key": "oauth:c", "model": ACCOUNT_SCOPE, "error_count": 2},
               {"channel_key": "oauth:c", "model": "m1", "last_error_message": "boom", "error_count": 3},
               {"channel_key": "oauth:other", "model": "x"}]
    facts = runtime_errors("claude", "c", {"models": ["m1"]}, entries, backend)
    assert [(f["kind"], f["error_count"]) for f in facts] == [("accountRestriction", 2), ("fault", 3)]
    from src import quota_errors
    if quota_errors.is_cursor_pool_quota_message(pool):
        facts = runtime_errors("cursor", "c", {"models": ["m1"]},
                               [{"channel_key": "oauth:c", "model": "m1", "last_error_message": pool}], backend)
        assert facts[0]["kind"] == "quotaPause" and facts[0]["pool"] == "cursor_models"


def test_usage_sections_cover_workbuddy_and_qoder():
    from src.management_control.oauth.usage_sections import build_usage_sections

    wb = build_usage_sections("workbuddy", {}, None, provider_snapshot={
        "credits": {"capacity": 100, "remaining": 40, "scope": "enterprise"}, "complete": True,
        "personal_credits": {"capacity": 10, "remaining": 5}, "packages": [{"expires_at": "2026-12-01T00:00:00Z"}]})
    items = wb[0]["items"]
    assert items[0]["label"] == "企业积分" and items[0]["used"] == 60 and items[0]["used_percent"] == 60
    assert items[2]["remaining"] == 1
    qd = build_usage_sections("qoder", {}, None, provider_snapshot={
        "usage": {"plan": "Pro", "until": "2026-12-01T00:00:00Z", "pools": [{"remaining": 3, "total": 10, "used_percent": 70}]}})
    assert [item["label"] for item in qd[0]["items"]] == ["套餐", "积分"]


# ── Models ─────────────────────────────────────────────────────────────────
def _source(**kw):
    base = dict(type=None, id="s", label="s", provider="claude", outbound_model="m", source_enabled=True,
                container_enabled=True, effective_routable=True, effective_metadata={},
                unavailable_reason=None, enabled_override=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_model_shared_metadata_and_display_fields():
    from src.management_control.models.presentation import display_metadata, shared_metadata

    meta = {"contextWindow": 200000, "contextWindowMaxMode": 1000000, "maxOutputTokens": 64000,
            "compactTriggerTokens": 120000, "cost": {"input": 3, "cache_read": 0.3}}
    shown = display_metadata(meta)
    assert shown["contextWindowMaxMode"] == 1000000 and shown["cost"] == {"input": 3, "cacheRead": 0.3}
    a = _source(effective_metadata=meta)
    b = _source(effective_metadata={**meta, "maxOutputTokens": 32000})
    off = _source(effective_routable=False, effective_metadata={"contextWindow": 1})
    shared = shared_metadata([a, b, off])
    assert "maxOutputTokens" not in shared and shared["compactTriggerTokens"] == 120000


def test_model_source_order_and_reasons_like_tg():
    from src.management_control.models.common import ModelKind, ModelSourceType
    from src.management_control.models.presentation import list_state, not_scheduled_reason, ordered_sources

    a = _source(id="api:a", type=ModelSourceType.API)
    b = _source(id="api:b", type=ModelSourceType.API)
    c = _source(id="api:c", type=ModelSourceType.API, effective_routable=False, source_enabled=False)
    view = SimpleNamespace(sources=(c, a, b), global_enabled=True, model_id="m",
                           identity=SimpleNamespace(kind=ModelKind.CHAT),
                           available_in=lambda source=None: True)
    ordered = ordered_sources(view, ["api:b", "api:a", "api:c"])
    assert [(item.id, rank, order) for item, rank, order in ordered] == [("api:b", 1, 0), ("api:a", 2, 1), ("api:c", None, 2)]
    assert not_scheduled_reason(view, c) == "sourceDisabled" and not_scheduled_reason(view, a) is None
    assert list_state(view) is None
    smart = ordered_sources(view, [])
    assert [item.id for item, _rank, _order in smart] == ["api:a", "api:b", "api:c"]


def test_api_source_primary_balance():
    from src.management_control.models.presentation import primary_balance

    usage = SimpleNamespace(snapshot={"balances": [{"id": "total", "label": "总余额", "value": "9"},
                                                   {"id": "available", "label": "可用余额", "value": "5", "currency": "CNY"}]},
                            fetched_at=123)
    assert primary_balance(usage) == {"label": "可用余额", "amount": "5", "currency": "CNY", "fetched_at": 123}
    assert primary_balance(SimpleNamespace(snapshot=None, fetched_at=None)) is None


# ── Stats summary / breakdown ─────────────────────────────────────────────
def test_stats_rates_and_summary_tops():
    from src.management_control.observability.stats import StatsControl
    from src.management_control.observability.stats_view import summary_tops

    metrics = StatsControl._normalize_metrics({"total": 4, "success_count": 2, "input": 10, "cache_creation": 5,
                                               "cache_read": 5, "success_with_cache_hit": 1, "retried_requests": 1,
                                               "affinity_hits": 2})
    assert metrics["promptTokens"] == 20 and metrics["cacheReadRatio"] == 0.25
    assert metrics["successRate"] == 0.5 and metrics["cacheHitRate"] == 0.5 and metrics["affinityHitRate"] == 0.5
    assert StatsControl._normalize_metrics({})["successRate"] is None
    snapshot = {"model_channels": {"m1": [{"key": "api:a", "type": "api", "count": 3}]},
                "summary": {"by_apikey": [{"key": "k1", "metrics": {"total": 5}}, {"key": "?", "metrics": {}}]}}
    families = {"anthropic": {"by_channel": [{"key": "api:a", "metrics": {"total": 3}}],
                              "by_model": [{"key": "m1", "metrics": {"total": 3}}],
                              "by_apikey": [{"key": "k1", "metrics": {"total": 3}}]},
                "openai": {"by_apikey": [{"key": "k1", "metrics": {"total": 2}}]}}
    tops = summary_tops(snapshot, families, StatsControl._normalize_metrics)
    assert tops["familyTops"]["anthropic"]["models"][0]["channels"][0]["count"] == 3
    assert tops["topApiKeys"] == [{"key": "k1", "metrics": StatsControl._normalize_metrics({"total": 5}),
                                   "familyTotals": {"anthropic": 3, "openai": 2}}]


def test_breakdown_family_and_channel_parameters(monkeypatch):
    from src.management_control.observability.stats import (
        StatsBreakdownQuery, StatsControl, StatsDimension, StatsPeriod,
    )

    control = StatsControl()
    snapshot = {"summary": {"by_model": [{"key": "all", "metrics": {"total": 9}}]},
                "families": {"openai": {"by_model": [{"key": "gpt", "metrics": {"total": 2}}]}},
                "model_channels": {"gpt": [{"key": "api:a", "type": "api", "count": 2}]}}
    monkeypatch.setattr(control, "period_snapshot", lambda context, period: snapshot)
    calls = []
    monkeypatch.setattr(control.log_db, "channel_model_stats",
                        lambda key, since_ts: calls.append(key) or [{"final_model": "x", "total": 4}])
    query = lambda **kw: StatsBreakdownQuery(dimension=StatsDimension.MODEL, period=StatsPeriod.TODAY, **kw)
    rows = control.breakdown(telegram_context(1), query(family="openai")).items
    assert [row["key"] for row in rows] == ["gpt"] and rows[0]["channels"][0]["key"] == "api:a"
    rows = control.breakdown(telegram_context(1), query(channel="api:chan")).items
    assert [(row["key"], row["metrics"]["total"]) for row in rows] == [("x", 4)] and calls == ["api:chan"]
    with pytest.raises(ManagementError) as bad:
        control.breakdown(telegram_context(1), query(family="nope"))
    assert bad.value.code is ManagementErrorCode.VALIDATION_FAILED
    with pytest.raises(ManagementError):
        control.breakdown(telegram_context(1), StatsBreakdownQuery(
            dimension=StatsDimension.CHANNEL, period=StatsPeriod.TODAY, channel="api:x"))


# ── Channels ───────────────────────────────────────────────────────────────
def test_channel_month_stats_dto_and_loading(monkeypatch):
    from src.management_api.schemas.channels import month_stats_data
    from src.management_control.channels import list_stats

    data = month_stats_data({"total": 4, "success_count": 3, "input": 10, "cache_read": 10, "output": 2,
                             "cost_ticks": 10_000_000_000, "costed_success": 3})
    assert data.promptTokens == 20 and data.successRate == 0.75 and data.cacheReadRatio == 0.5
    assert data.cost == "1" and data.costTicks == 10_000_000_000
    assert list_stats.month_stats_by_channel(telegram_context(1)) == ({}, False)


# ── Live events / compression ─────────────────────────────────────────────
def test_request_updated_event_carries_proxy_name():
    from src.management_api.schemas.webui import LiveRequestUpdatedData

    assert LiveRequestUpdatedData(id="r", proxyName="p").proxyName == "p"


def test_clear_compression_model_removes_legacy_flag():
    from src import config, model_metadata

    def seed(cfg):
        cfg["compressionModel"] = ""
        cfg.setdefault("modelMetadata", {})["legacy-model"] = {"compressionModel": True}
    config.update(seed)
    try:
        assert model_metadata.get_compression_model() == "legacy-model"
        assert model_metadata.clear_compression_model() is True
        assert model_metadata.get_compression_model() is None
    finally:
        config.update(lambda cfg: (cfg.get("modelMetadata") or {}).pop("legacy-model", None))


def test_stats_cache_peek_or_refresh_is_non_blocking():
    import threading
    import time
    from src.management_control.stats_cache import StatsCache

    cache = StatsCache()
    cache.enabled = True
    gate = threading.Event()
    value, loading = cache.peek_or_refresh("k", lambda: gate.wait(2) and 7)
    assert (value, loading) == (None, True)
    gate.set()
    for _ in range(100):
        value, loading = cache.peek_or_refresh("k", lambda: 7)
        if value is not None:
            break
        time.sleep(0.01)
    assert (value, loading) == (True, False) or (value, loading) == (7, False)


def test_log_detail_counts_local_searches_like_list(monkeypatch):
    from src.management_control.observability.logs import LogsControl

    control = LogsControl()
    monkeypatch.setattr(control.log_db, "management_log_detail", lambda log_id: {
        "log": _row(local_web_count=None, request_id=log_id), "detail": {},
        "local_web_log": [{"tool_name": "search"}, {"tool_name": "fetch"}]})
    monkeypatch.setattr(control.log_db, "cost_for_log", lambda row: {})
    data = control.detail(telegram_context(1), "r9")
    assert data["log"]["localSearchCount"] == 2 and data["log"]["clientIp"] is None


def test_stats_cache_keeps_serving_old_value_within_custom_stale_window():
    from src.management_control.stats_cache import StatsCache

    clock = [0.0]
    cache = StatsCache(fresh_seconds=15, max_stale_seconds=600, clock=lambda: clock[0])
    cache.enabled = True
    cache._store("k", 1)
    clock[0] = 3600.0
    started: list = []
    cache._start_background = lambda key, loader: started.append(key)
    assert cache.peek_or_refresh("k", lambda: 2) == (None, True)
    assert cache.peek_or_refresh("k", lambda: 2, max_stale_seconds=6 * 3600) == (1, False)
    assert started == ["k", "k"]


def test_oauth_card_warmup_computes_every_account():
    control, backend = build_control()
    seen: list = []
    control._account_card = lambda account_id, provider, account, cooldowns, *, light=False, blocking=False: seen.append(
        (account_id, provider, blocking))
    control.warm_card_stats()
    ids = {backend.account_id(account) for account in backend.list_accounts()}
    assert {item[0] for item in seen} == ids and all(item[2] for item in seen)


def test_rolling_window_card_stats_keep_one_cache_key():
    from src.management_control.oauth.account_card import card_stats_key, window_since

    now = 1_800_000_000.0
    # No reset time (unused Claude 5h window): the window rolls, the key must not.
    first = window_since(None, 5 * 3600, now)
    later = window_since(None, 5 * 3600, now + 600)
    assert card_stats_key("claude:a", first, 5 * 3600) == card_stats_key("claude:a", later, 5 * 3600)
    # Anchored windows still change key with their start.
    assert card_stats_key("claude:a", 1000.0) != card_stats_key("claude:a", 1000.0 + 3600)


def test_antigravity_windows_are_ordered_and_use_seven_day_label():
    from src.management_control.oauth.usage_sections import _antigravity

    usage = {"antigravity": {"quota_groups": [{"display_name": "Gemini Models", "buckets": [
        {"window": "weekly", "remaining_fraction": 0.5},
        {"window": "5h", "remaining_fraction": 1.0},
    ]}]}}
    group = _antigravity(usage)[0]
    assert [item["label"] for item in group["items"]] == ["5 小时", "7 天"]


def test_claude_fable_window_counts_only_fable_models():
    from datetime import datetime, timezone
    from src.management_control.oauth.account_card import OAuthAccountCardMixin, card_stats_key

    calls: list = []

    class Backend:
        def fable_display_from_quota_row(self, row):
            return 2.0, "2026-10-27T00:00:00Z"

        def claude_fable_models(self, account):
            return ["claude-fable-5", "claude-fable-5-1"]

        def tokens_for_channel_models(self, channel, models, since):
            calls.append((channel, models, since))
            return {"total": 4, "success_count": 4, "output": 10}

    card = OAuthAccountCardMixin()
    card.backend = Backend()
    now = datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()
    window = card._fable_window("claude:a@x", {}, {}, now, False)["fable"]
    assert window["stats"]["total"] == 4 and window["stats_loading"] is False
    assert calls == [("oauth:claude:a@x", ["claude-fable-5", "claude-fable-5-1"],
                      datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp())]
    assert card_stats_key("claude:a@x", 1.0, None, "fable") != card_stats_key("claude:a@x", 1.0)


@pytest.mark.parametrize("name", ["Credits", "Add-on credits", "Shared credits", "Dedicated credits"])
def test_qoder_credit_pools_are_labelled_jifen(name):
    from src.management_control.oauth.usage_sections import _qoder

    section = _qoder({"usage": {"pools": [{"name": name, "total": 100, "remaining": 40, "used_percent": 60}]}})
    assert [i["label"] for i in section["items"]] == ["积分"]
