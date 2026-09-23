from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
from src.management_api.dependencies import (
    ManagementRuntime,
    get_management_control_owner,
)
from src.management_api.routers import (
    _observability,
    apikey,
    auxiliary_support,
    channels,
    load_balancing,
    mapping,
    model_metadata,
    oauth_support,
    proxy,
    system_support,
)
from src.management_control import (
    BoundedAuditSink,
    ManagementError,
    ManagementErrorCode,
    OperationRegistry,
    OperationStore,
)
from src.telegram import bot as tgbot


ROOT = Path(__file__).resolve().parents[2]


class _StateStore:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def _runtime() -> ManagementRuntime:
    audit = BoundedAuditSink()
    operations = OperationStore(audit_sink=audit)
    return ManagementRuntime(
        sessions=SimpleNamespace(),
        approvals=SimpleNamespace(),
        operations=operations,
        operation_registry=OperationRegistry(operations),
        audit_sink=audit,  # type: ignore[arg-type]
        state_store=_StateStore(),  # type: ignore[arg-type]
        allowed_origins=frozenset(),
        application_version="test",
        documentation_url="/docs",
    )


def _request(runtime: ManagementRuntime):
    state = SimpleNamespace(management_runtime=runtime)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_all_api_dependency_paths_reuse_one_runtime_control_owner():
    runtime = _runtime()
    request = _request(runtime)
    try:
        owner = get_management_control_owner(runtime)
        assert get_management_control_owner(runtime) is owner

        assert mapping.get_mapping_control(runtime) is owner.mapping
        assert mapping.get_mapping_control(runtime) is owner.mapping
        assert model_metadata.get_metadata_control(runtime) is owner.mapping
        assert load_balancing.get_load_balancing_control(runtime) is owner.load_balancing
        assert proxy.get_proxy_control(runtime) is owner.proxy
        assert oauth_support.get_oauth_control_dependency(runtime) is owner.oauth

        assert channels.get_channel_control(request, runtime) is owner.channels
        assert channels.get_channel_control(request, runtime) is owner.channels
        assert apikey.get_api_key_control(request, runtime) is owner.api_keys
        assert apikey.get_api_key_control(request, runtime) is owner.api_keys
        assert auxiliary_support.get_bound_auxiliary_controls(request, runtime) is owner.auxiliary
        assert _observability.controls(request) is owner.observability
        assert system_support.get_bound_system_network_controls(request) is owner.system
    finally:
        runtime.close()


def test_control_graph_is_bound_to_the_runtime_audit_and_operation_owner():
    runtime = _runtime()
    try:
        owner = runtime.control_owner()
        assert owner.audit_sink is runtime.audit_sink
        assert owner.operations is runtime.operations
        assert owner.operation_registry is runtime.operation_registry

        assert owner.oauth._audit_sink is runtime.audit_sink
        assert owner.channels._audit_sink is runtime.audit_sink
        assert owner.channels._operation_store is runtime.operations
        assert owner.channels._operation_registry is runtime.operation_registry
        assert owner.api_keys._audit is runtime.audit_sink
        assert owner.mapping._audit_sink is runtime.audit_sink
        assert owner.mapping._operation_store is runtime.operations
        assert owner.load_balancing._audit_sink is runtime.audit_sink
        assert owner.proxy._audit_sink is runtime.audit_sink
        assert owner.proxy._operation_store is runtime.operations
        assert owner.system.settings._audit_sink is runtime.audit_sink
        assert owner.system.blacklist._audit_sink is runtime.audit_sink
        assert owner.system.network._audit_sink is runtime.audit_sink
        assert owner.system.network.operations is runtime.operations
        assert owner.system_runtime._audit_sink is runtime.audit_sink
        assert owner.observability.stats.audit_sink is runtime.audit_sink
        assert owner.observability.retention.audit_sink is runtime.audit_sink

        for control in (
            owner.auxiliary.translation,
            owner.auxiliary.status_alerts,
            owner.auxiliary.updates,
        ):
            assert control._audit_sink is runtime.audit_sink
            assert control._operation_store is runtime.operations
            assert control._operation_registry is runtime.operation_registry
        assert owner.auxiliary.images._audit_sink is runtime.audit_sink
        assert owner.auxiliary.xai_media._audit_sink is runtime.audit_sink
    finally:
        runtime.close()


def test_production_telegram_bindings_are_the_same_instances_as_api_dependencies():
    runtime = _runtime()
    owner = runtime.control_owner()
    request = _request(runtime)
    server._bind_telegram_management_controls(owner)
    try:
        assert tgbot.main_menu._CONTROL is owner.observability.status
        assert tgbot.status_menu._CONTROL is owner.observability.status
        assert tgbot.stats_menu._CONTROL is owner.observability.stats
        assert tgbot.menu_cache._STATS_CONTROL is owner.observability.stats
        assert tgbot.logs_menu._CONTROL is owner.observability.logs
        assert tgbot.media_logs_menu._CONTROL is owner.observability.media
        assert tgbot.mapping_menu.mapping_control is mapping.get_mapping_control(runtime)
        assert tgbot.mapping_menu.compact_rescue is owner.mapping
        assert tgbot.load_balancing_menu.load_balancing_control is load_balancing.get_load_balancing_control(runtime)
        assert tgbot.proxy_menu.proxy_control is proxy.get_proxy_control(runtime)
        assert tgbot.channel_menu._CONTROL is channels.get_channel_control(request, runtime)
        assert tgbot.apikey_menu._CONTROL is apikey.get_api_key_control(request, runtime)
        assert tgbot.oauth_menu.oauth_control is oauth_support.get_oauth_control_dependency(runtime)
        assert tgbot.oauth_account_models_menu.oauth_control is owner.oauth
        assert not hasattr(tgbot, "oauth_defaults_menu")
        assert tgbot.translation_menu._CONTROL is owner.auxiliary.translation
        assert tgbot.status_alert_menu._CONTROL is owner.auxiliary.status_alerts
        assert tgbot.update_menu._CONTROL is owner.auxiliary.updates
        assert tgbot.image_menu._CONTROL is owner.auxiliary.images
        assert tgbot.xai_imagine_menu._CONTROL is owner.auxiliary.xai_media
        assert tgbot.system_menu._settings_control is owner.system.settings
        assert tgbot.system_menu._blacklist_control is owner.system.blacklist
        assert tgbot.system_menu._network_control is owner.system.network
        assert tgbot.system_menu._runtime_control is owner.system_runtime
        assert tgbot.system_menu._load_balancing_control is owner.load_balancing
        assert tgbot.system_menu._retention_control is owner.telegram_retention
        assert owner.telegram_retention.control is owner.observability.retention
        assert tgbot.system_menu._retention_control.control is owner.observability.retention
        assert system_support.get_bound_system_network_controls(request) is owner.system
    finally:
        server._unbind_telegram_management_controls(owner)
        runtime.close()


def test_closed_runtime_owner_is_not_reused_by_a_rebuilt_runtime():
    first = _runtime()
    first_owner = first.control_owner()
    first.close()
    assert first.controls is None
    with pytest.raises(ManagementError) as exc_info:
        first.control_owner()
    assert exc_info.value.code is ManagementErrorCode.SERVICE_NOT_READY

    second = _runtime()
    try:
        second_owner = second.control_owner()
        assert second_owner is not first_owner
        assert second_owner.mapping is not first_owner.mapping
        assert second_owner.operations is not first_owner.operations
        assert second_owner.audit_sink is not first_owner.audit_sink
    finally:
        second.close()


def test_system_menu_has_no_management_business_module_calls():
    path = ROOT / "src/telegram/menus/system_menu.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    banned_roots = {
        "apikey_limiter",
        "concurrency",
        "config",
        "log_db",
        "network",
        "network_monitor",
        "registry",
        "state_db",
    }
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if isinstance(node.func.value, ast.Name) and node.func.value.id in banned_roots:
            calls.append((node.func.value.id, node.func.attr, node.lineno))
    assert calls == []

    source = path.read_text(encoding="utf-8")
    assert 'f"sys:chsel_set:{m}"' in source
    assert "_load_balancing_control.set_mode(mode)" in source
    assert "load_balancing.set_mode(mode)" not in source


def test_contract_points_to_the_complete_production_operation_manifest():
    fixture_root = ROOT / "src/tests/fixtures/management_api"
    production = (fixture_root / "production-operation-ids.txt").read_text().splitlines()
    foundation = (fixture_root / "v1-operation-ids.txt").read_text().splitlines()
    document = (ROOT / "docs/13-management-control-api-refactor.md").read_text()
    section = document.split("## 15. Management API 完整性验收清单", 1)[1]

    assert len(production) == 243  # Includes saved-account Zhipu project selection
    assert len(foundation) == 9
    assert "production-operation-ids.txt" in section
    assert "P0/foundation 的 9-operation 基础清单" in section
