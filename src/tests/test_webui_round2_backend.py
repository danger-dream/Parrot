"""WebUI round 2 backend regressions (items 0-6, 13, 14, 16-19).

Each test names the item it locks. Network is never used: providers,
monitors and update fetches are monkeypatched.
"""
from __future__ import annotations

import ast
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.management_control import ManagementError, ManagementErrorCode
from src.management_control import stats_cache
from src.management_control.stats_cache import CachingStatsSource, StatsCache, snapshot_key


ROUTERS = Path(__file__).resolve().parents[1] / "management_api" / "routers"


# ── item 0: nothing slow runs on the event loop ─────────────────────────────
# Async routes may only touch controls through ``run_in_threadpool`` (or the
# upstream wrapper passed to it). The listed exceptions only enqueue an
# in-memory Operation on the running loop (their worker runs elsewhere).
_ASYNC_ENQUEUE_ONLY = {
    ("channels.py", "discover_channel_models"), ("channels.py", "probe_channel_draft"),
    ("channels.py", "probe_existing_channel"), ("channels.py", "refresh_channel_provider_usage"),
}
_CONTROL_NAMES = {"control", "controls", "runtime"}


def _owner(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def test_item0_async_routes_never_call_controls_on_the_event_loop():
    violations = []
    for path in sorted(ROUTERS.glob("*.py")):
        if path.name == "live_events.py":
            continue  # WebSocket: verified separately below
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]:
            if (path.name, fn.name) in _ASYNC_ENQUEUE_ONLY:
                continue
            offloaded = set()
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_in_threadpool":
                    offloaded.update(id(arg) for arg in ast.walk(node))
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and _owner(node.func) in _CONTROL_NAMES and id(node) not in offloaded
                        and id(node.func) not in offloaded):
                    violations.append((path.name, fn.name, node.lineno))
    assert violations == []


def test_item0_session_dependency_and_sync_routes_run_in_threadpool():
    from src.management_api import dependencies
    from src.management_api.routers import foundation

    # A plain def dependency/route is executed by FastAPI on the worker pool.
    assert not asyncio.iscoroutinefunction(dependencies.get_authenticated_session)
    for name in ("create_management_session", "get_current_management_session",
                 "revoke_current_management_session", "get_management_operation"):
        assert not asyncio.iscoroutinefunction(getattr(foundation, name)), name
    source = (ROUTERS / "live_events.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread" not in source and "run_in_threadpool(runtime.sessions.verify" in source


def _cache(**kwargs) -> StatsCache:
    cache = StatsCache(**kwargs)
    cache.enabled = True
    return cache


def test_item0_stats_cache_fresh_stale_and_single_flight():
    now = [0.0]
    cache = _cache(fresh_seconds=10, max_stale_seconds=100, clock=lambda: now[0])
    calls = []

    def loader():
        calls.append(1)
        return {"n": len(calls)}

    assert cache.get("k", loader) == {"n": 1}
    first = cache.get("k", loader)
    first["n"] = 99  # callers get copies
    assert cache.get("k", loader) == {"n": 1} and len(calls) == 1
    now[0] = 50  # stale: old value immediately, one background refresh
    assert cache.get("k", loader) == {"n": 1}
    for _ in range(100):
        if len(calls) == 2 and not cache._flights:
            break
        time.sleep(0.01)
    assert cache.get("k", loader) == {"n": 2}
    now[0] = 500  # too old: recompute synchronously
    assert cache.get("k", loader) == {"n": 3}

    gate = threading.Event()
    slow_calls = []

    def slow():
        slow_calls.append(1)
        gate.wait(2)
        return "v"

    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.get("slow", slow))) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join()
    assert results == ["v"] * 5 and len(slow_calls) == 1


def test_item0_stats_cache_errors_disabled_and_keys():
    cache = _cache()
    with pytest.raises(ValueError):
        cache.get("bad", lambda: (_ for _ in ()).throw(ValueError("x")))
    assert cache.get("bad", lambda: 1) == 1  # a failure is not cached
    cache.enabled = False
    seen = []
    cache.get("x", lambda: seen.append(1))
    cache.get("x", lambda: seen.append(1))
    assert len(seen) == 2
    assert snapshot_key("month", 100.0) != snapshot_key("month", 200.0)  # month rollover
    assert snapshot_key("7d", 1.0) == snapshot_key("7d", 2.0)  # rolling window keeps SWR


def test_item0_api_key_reads_are_cached_for_api_actors_only(monkeypatch):
    from src import log_db
    from src.management_auth import AuthMethod
    from src.management_control.apikey.control import ApiKeyControl

    cache = _cache()
    monkeypatch.setattr(stats_cache, "STATS_CACHE", cache)
    calls = []

    class Source:
        def __getattr__(self, name):
            return getattr(log_db, name)

        def tokens_for_apikey(self, key, since):
            calls.append(("tokens", key))
            return {"total": 3}

    source = Source()
    monkeypatch.setattr(stats_cache, "cached", lambda key, loader, source=None: cache.get(key, loader))
    proxy = CachingStatsSource(source)
    assert proxy.tokens_for_apikey("a", 1.0) == {"total": 3}
    assert proxy.tokens_for_apikey("a", 1.0) == {"total": 3}
    assert calls == [("tokens", "a")]
    control = ApiKeyControl(statistics=source)
    tg = SimpleNamespace(actor=SimpleNamespace(auth_method=AuthMethod.TELEGRAM_ADMIN))
    api = SimpleNamespace(actor=SimpleNamespace(auth_method=AuthMethod.MANAGEMENT_KEY))
    assert control._stats_source(tg) is source
    assert isinstance(control._stats_source(api), CachingStatsSource)


def test_item0_stats_period_snapshot_is_cached_and_shared(monkeypatch):
    from src.management_control.observability import stats as stats_module
    from src.management_control.observability.stats import StatsControl

    cache = _cache()
    monkeypatch.setattr(stats_cache, "STATS_CACHE", cache)
    calls = []

    def snapshot(since):
        calls.append(since)
        return {"summary": {}, "by_apikey": {}, "model_channels": {}}

    monkeypatch.setattr(stats_module.log_db_module, "stats_period_snapshot", snapshot)
    control = StatsControl()
    context = _admin()
    control.period_snapshot(context, "month")
    control.period_snapshot(context, "month")
    assert len(calls) == 1


# ── item 1: oflow_ poll / cancel ────────────────────────────────────────────
def _admin():
    from src.management_control.oauth.menu_bridge import telegram_context
    return telegram_context(42)


@pytest.mark.parametrize("provider", ["claude", "openai", "xai", "cursor", "antigravity"])
def test_item1_browser_flows_poll_and_cancel(provider):
    from src.management_control.oauth import OAuthProvider
    from src.tests.management_oauth_fakes import build_control

    control, _backend = build_control()
    context = _admin()
    flow = control.start_login_flow(context, OAuthProvider(provider))
    assert flow.flow_id.startswith("oflow_")
    assert control.poll_login_flow(context, flow.flow_id, flow.flow_secret).status == "pending"
    control.cancel_login_flow(context, flow.flow_id, flow.flow_secret)
    assert control.poll_login_flow(context, flow.flow_id, flow.flow_secret).status == "cancelled"
    control.cancel_login_flow(context, flow.flow_id, flow.flow_secret)  # idempotent
    with pytest.raises(ManagementError) as wrong:
        control.poll_login_flow(context, flow.flow_id, "not-the-secret")
    assert wrong.value.code is ManagementErrorCode.INVALID_OPERATION_STATE


def test_item1_completed_browser_flow_reports_account():
    from urllib.parse import parse_qs, urlparse

    from src.management_control.oauth import CompleteOAuthLoginCommand, OAuthProvider
    from src.tests.management_oauth_fakes import build_control

    control, _backend = build_control()
    context = _admin()
    flow = control.start_login_flow(context, OAuthProvider.CLAUDE)
    state = parse_qs(urlparse(flow.auth_url or "").query)["state"][0]
    result = control.complete_login_flow(context, flow.flow_id, flow.flow_secret,
                                         CompleteOAuthLoginCommand(code="provider-code", state=state))
    poll = control.poll_login_flow(context, flow.flow_id, flow.flow_secret)
    assert poll.status == "completed" and poll.account_id == result.account_id
    with pytest.raises(ManagementError):
        control.cancel_login_flow(context, flow.flow_id, flow.flow_secret)


def test_item1_device_for_routes_by_prefix():
    from src.tests.management_oauth_fakes import build_control

    control, _ = build_control()
    flows = control._flows
    assert flows.device_for("wbflow_x") is flows.workbuddy
    assert flows.device_for("zhflow_x") is flows.zhipu
    assert flows.device_for("qdflow_x") is flows.qoder
    assert flows.device_for("oflow_x") is flows.browser
    with pytest.raises(ManagementError):
        flows.device_for("unknown_x")


# ── item 2: no "no running event loop" 500 ─────────────────────────────────
def test_item2_provider_failure_is_not_chained_to_loop_probe(monkeypatch):
    from src.oauth.zhipu import common

    async def fail():
        raise common.ZhipuError("reset_status", "network")

    def run(coro):
        coro.close()
        raise common.ZhipuError("reset_status", "network")

    try:
        asyncio.get_running_loop()
        pytest.skip("must run outside a loop")
    except RuntimeError:
        pass
    monkeypatch.setattr(common.asyncio, "run", run)
    with pytest.raises(common.ZhipuError) as caught:
        common._request("https://example.invalid", headers={}, method="GET", body=None,
                        account_key="k", timeout=1, envelope=False, read_attempts=1, stage="reset_status")
    assert caught.value.__context__ is None or not isinstance(caught.value.__context__, RuntimeError)


@pytest.mark.parametrize("exc, code", [
    (TimeoutError(), ManagementErrorCode.UPSTREAM_TIMEOUT),
    (RuntimeError("boom"), ManagementErrorCode.UPSTREAM_ERROR),
])
def test_item2_call_upstream_maps_to_management_errors(exc, code):
    from src.management_control.oauth.upstream import call_upstream

    def action():
        raise exc

    with pytest.raises(ManagementError) as caught:
        call_upstream(action)
    assert caught.value.code is code


def test_item2_zhipu_qoder_routes_wrap_every_upstream_call():
    for name in ("oauth_zhipu.py", "oauth_qoder.py"):
        source = (ROUTERS / name).read_text(encoding="utf-8")
        assert "run_in_threadpool(control." not in source, name
        assert source.count("call_upstream") >= 4, name


# ── item 3: invalid list == delete-all plan ────────────────────────────────
def test_item3_invalid_deletion_all_matches_invalid_list():
    from src.management_control.oauth import PageSpec
    from src.tests.management_oauth_fakes import build_control

    control, backend = build_control()
    backend.accounts.append({"_id": "workbuddy:wb-1", "provider": "workbuddy", "type": "workbuddy",
                             "enabled": False, "disabled_reason": "auth_error", "models": []})
    backend.accounts.append({"_id": "xai:no-email", "provider": "xai", "type": "xai",
                             "enabled": False, "disabled_reason": "auth_error", "models": []})
    context = _admin()
    listed = {item.account_id for item in control.list_invalid_accounts(context, page=PageSpec(1, 200)).items}
    plan = control.plan_invalid_deletion(context, None)
    assert set(plan.account_ids) == listed
    assert "workbuddy:wb-1" in listed and "xai:no-email" not in listed


# ── item 4: OpenAI API channel max concurrency ─────────────────────────────
@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses"])
def test_item4_openai_api_channel_exposes_max_concurrent(protocol):
    from src.openai.channel.api_channel import OpenAIApiChannel

    entry = {"name": "o", "type": "api", "baseUrl": "https://example.test", "apiKey": "sk-x",
             "protocol": protocol, "models": [{"real": "m", "alias": "m"}], "maxConcurrent": 3}
    assert OpenAIApiChannel(entry).max_concurrent == 3
    assert OpenAIApiChannel({**entry, "maxConcurrent": "bad"}).max_concurrent == 0
    assert OpenAIApiChannel({k: v for k, v in entry.items() if k != "maxConcurrent"}).max_concurrent == 0


# ── item 5: manual update check ignores the auto-check switch ──────────────
def test_item5_manual_update_check_fetches_when_auto_check_disabled(monkeypatch):
    from src import update_checker

    fetched = []
    monkeypatch.setattr(update_checker, "_cfg", lambda: {
        "enabled": False, "repo": "owner/repo", "apiBase": "https://api.example.test",
        "includePrerelease": False, "ignoredVersions": [], "autoUpdate": False,
    })
    monkeypatch.setattr(update_checker, "_http_get_json", lambda url: fetched.append(url) or [])
    monkeypatch.setattr(update_checker, "_save_state", lambda *a, **k: None)
    monkeypatch.setattr(update_checker, "_load_state", lambda repo: {})
    monkeypatch.setattr(update_checker, "_set_cache", lambda value: None)
    update_checker._check_once(push=False, manual=False)
    assert fetched == []  # background loop stays off
    update_checker._check_once(push=False, raise_on_error=True, manual=True)
    assert len(fetched) == 1


def test_item5_api_gateway_and_tg_button_use_manual_check():
    source = Path(update_checker_path()).read_text(encoding="utf-8")
    assert "_check_once(push=True, manual=True)" in source  # TG 「立即检查」
    gateway = (Path(__file__).resolve().parents[1] / "management_control/auxiliary/updates.py").read_text()
    assert "_check_once(push=False, raise_on_error=True, manual=True)" in gateway


def update_checker_path() -> str:
    from src import update_checker
    return update_checker.__file__


# ── item 6: manual network monitor run ignores the master switch ───────────
def test_item6_manual_monitor_run_checks_without_saving(monkeypatch):
    from src import network_monitor as nm

    monkeypatch.setattr(nm, "cfg", lambda: {"enabled": False, "intervalSeconds": 60, "dns": True,
                                            "socks5": False, "timeoutSeconds": 1})
    monkeypatch.setattr(nm, "_dns_check", lambda timeout: nm.CheckResult("dns", "DNS", "dns", True, None))
    saved = []
    monkeypatch.setattr(nm, "_save_result", lambda res: saved.append(res))
    monkeypatch.setattr(nm.state_db, "network_check_delete_stale", lambda keys: None)
    assert asyncio.run(nm.run_once(save=True)) == []
    results = asyncio.run(nm.run_once(save=True, manual=True))
    assert [item.key for item in results] == ["dns"] and saved == []


def test_item6_gateway_and_tg_use_manual_run():
    gateway = (Path(__file__).resolve().parents[1] / "management_control/network/gateway.py").read_text()
    assert "run_once(save=True, manual=True)" in gateway


# ── item 13: background job telemetry ──────────────────────────────────────
def test_item13_background_jobs_report_last_and_next_run():
    from src import job_telemetry
    from src.management_control.observability.status import StatusControl

    job_telemetry._reset_for_tests()
    job_telemetry.scheduled("walCheckpoint", 300)
    job_telemetry.started("pendingCleanup")
    job_telemetry.finished("pendingCleanup", ok=False, error="OSError", next_delay_seconds=300)
    job_telemetry.started("affinityCleanup")
    control = StatusControl(job_telemetry=job_telemetry)
    rows = {row["id"]: row for row in control.background_jobs(_admin(), page=1, page_size=50).items}
    assert rows["walCheckpoint"]["status"] == "unknown" and rows["walCheckpoint"]["nextRunAt"] is not None
    assert rows["pendingCleanup"]["status"] == "failed" and rows["pendingCleanup"]["error"] == "OSError"
    assert rows["pendingCleanup"]["lastRunAt"] is not None
    assert rows["affinityCleanup"]["status"] == "running"
    job_telemetry._reset_for_tests()


# ── item 14: wizard probe results → cooldown on the real channel ──────────
@pytest.fixture
def channel_state():
    from src.tests.test_management_channels_control import _reset_channels
    _reset_channels()
    yield
    _reset_channels()


def test_item14_create_channel_cools_down_failed_initial_probes(channel_state):
    from src import cooldown
    from src.management_control.channels import (
        ChannelControl, ChannelCreateCommand, ChannelModel, ChannelProtocol, ProbeResult,
    )
    from src.tests.test_management_channels_control import ADMIN_CONTEXT

    control = ChannelControl()
    result = control.create_channel(ADMIN_CONTEXT, ChannelCreateCommand(
        name="R2Wizard", base_url="https://provider.example.test/v1/chat/completions",
        api_key="sk-fake-channel-key", protocol=ChannelProtocol.OPENAI_CHAT, max_concurrent=3,
        models=(ChannelModel(real="good", alias="good"), ChannelModel(real="bad", alias="bad")),
        initial_probe_results={"good": ProbeResult(True, 0, None, False, False),
                               "bad": ProbeResult(False, 0, "PROBE_FAILED", False, False)},
    ))
    state = cooldown.get_state(result.channel.id, "bad")
    assert state is not None and state["error_count"] == 1
    assert cooldown.get_state(result.channel.id, "good") is None
    assert cooldown.get_state("api:R2Wizard__wiz", "bad") is None
    # item 4: the OpenAI-protocol channel shows (and enforces) its saved limit.
    from src import concurrency
    assert control.get_channel(ADMIN_CONTEXT, result.channel.id).max_concurrent == 3
    assert concurrency._get_channel_max(result.channel.id) == 3


def test_item14_initial_probe_results_must_name_channel_models():
    from src.management_api.routers.channels import _initial_probe_results
    from src.management_api.schemas.channels import ManualChannelCreateRequest

    body = ManualChannelCreateRequest(
        mode="manual", name="n", apiKey="sk-12345", protocol="openai-chat", baseUrl="https://e.test",
        models=[{"real": "m", "alias": "m"}],
        initialProbeResults=[{"model": "other", "ok": False}],
    )
    with pytest.raises(ManagementError) as caught:
        _initial_probe_results(body)
    assert caught.value.code is ManagementErrorCode.VALIDATION_FAILED


# ── item 16: listed metadata scope ─────────────────────────────────────────
def test_item16_listed_account_native_binding_reports_its_scope():
    from src.management_control.mapping.contracts import MetadataRecord
    from src.management_control.mapping.metadata_scope import listed_scope

    fields = {name: None for name in MetadataRecord.__dataclass_fields__}
    fields.update(model_id="m", scope="global", scope_id=None, source="auto", authority="x",
                  effective={}, raw={}, value_source={}, constrained_by={}, common_override={},
                  source_override={}, revision="r")
    record = MetadataRecord(**fields)
    listed = SimpleNamespace(scope_key="oauth:cursor:user@example.test", outbound_model="m-wire")
    scoped = listed_scope(record, listed)
    assert scoped.scope == "oauth" and scoped.scope_id and scoped.outbound_model == "m-wire"
    assert listed_scope(record, SimpleNamespace(scope_key=None)) is record
    api = listed_scope(record, SimpleNamespace(scope_key="api:chan", outbound_model=None))
    assert (api.scope, api.scope_id) == ("api", "api:chan")


# ── item 17: deleting a global binding restores auto matching ──────────────
def test_item17_delete_global_binding_rematches(monkeypatch):
    from src.management_control.mapping import control as mapping_control

    synced = []
    monkeypatch.setattr(mapping_control.model_metadata, "delete_binding", lambda model, scope_key=None: True)
    monkeypatch.setattr(mapping_control.model_metadata, "sync_auto_snapshots", lambda targets: synced.append(list(targets)))
    control = mapping_control.MappingControl()
    monkeypatch.setattr(control, "_check_revision", lambda *a, **k: None)
    monkeypatch.setattr(control, "_metadata_revision", lambda *a, **k: "r")
    control.delete_binding_control(_admin(), "model-x", scope="global", account_id=None, channel_id=None)
    assert synced == [[("model-x", None, None)]]


# ── item 18: batch order source / global effort ────────────────────────────
def test_item18_batch_model_orders_and_global_effort(monkeypatch):
    from src.management_control.load_balancing import control as lb

    control = lb.LoadBalancingControl()
    monkeypatch.setattr(control, "_client_models", lambda: ["b", "a"])
    monkeypatch.setattr(control, "_channels_for_model", lambda model: [])
    monkeypatch.setattr(lb.load_balancing, "has_model_priority", lambda model: model == "a")
    monkeypatch.setattr(lb.load_balancing, "effective_order_for_model", lambda model, channels: ["api:x"])
    records, revision = control.list_model_orders(_admin())
    assert [(r.model_id, r.source) for r in records] == [("a", "modelOverride"), ("b", "channelDefault")]
    only, _ = control.list_model_orders(_admin(), source="modelOverride")
    assert [r.model_id for r in only] == ["a"] and revision

    from src.management_control.models.control import ModelCenterControl
    from src.model_registry import runtime
    assert ModelCenterControl().global_default_effort(_admin()) == runtime.global_default_effort()


# ── item 19: search test preview / models ──────────────────────────────────
def test_item19_search_test_preview_redacts_url_credentials():
    from src.management_control.search import SearchControl

    preview = SearchControl._result_preview([
        {"title": "A", "url": "https://user:pass@example.test/p?q=1"},
        {"title": "B", "url": "https://example.test/b"}, "junk",
        *({"title": str(i), "url": f"https://e.test/{i}"} for i in range(10)),
    ])
    assert len(preview) == 5
    assert preview[0] == {"title": "A", "url": "https://example.test/p?q=1"}
    assert "pass" not in str(preview)


def test_item19_search_models_route_is_registered():
    source = (ROUTERS / "search.py").read_text(encoding="utf-8")
    assert '"/search/backends/{backendId}/models", operation_id="getSearchBackendModels"' in source
