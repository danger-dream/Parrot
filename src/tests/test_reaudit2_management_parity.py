from __future__ import annotations

import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server
from src import config
from src.management_api import install_management_error_handlers
from src.management_api.origin import ManagementOriginMiddleware
from src.management_api.routers import apikey, auxiliary_support, channels


FAKE_MANAGEMENT_KEY = "pmk_" + "K" * 64


def _normalized_default_config() -> dict:
    value = copy.deepcopy(config.DEFAULT_CONFIG)
    config._normalize_openai_oauth_config(value, value)
    return value


def _legacy_without_management() -> dict:
    value = _normalized_default_config()
    value.pop("management")
    return value


def test_p01_missing_management_key_is_generated_once_and_persisted(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_legacy_without_management()), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    generated = []

    def token_urlsafe(size: int) -> str:
        generated.append(size)
        return "G" * 64

    monkeypatch.setattr(config.secrets, "token_urlsafe", token_urlsafe)
    loaded = config._load_from_disk()

    assert generated == [48]
    assert loaded["management"]["managementKey"] == "pmk_" + "G" * 64
    assert json.loads(path.read_text(encoding="utf-8"))["management"] == loaded["management"]


def test_p01_existing_management_key_is_unchanged_without_write(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    existing = _normalized_default_config()
    existing["management"]["managementKey"] = FAKE_MANAGEMENT_KEY
    path.write_text(json.dumps(existing), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(
        config.secrets,
        "token_urlsafe",
        lambda _size: pytest.fail("an existing key must not be regenerated"),
    )
    monkeypatch.setattr(
        config,
        "_write_atomic",
        lambda _value: pytest.fail("an unchanged config must not be rewritten"),
    )

    loaded = config._load_from_disk()

    assert loaded["management"]["managementKey"] == FAKE_MANAGEMENT_KEY


def test_p01_management_write_failure_keeps_inference_readable_and_management_closed(
    tmp_path, monkeypatch, capsys,
):
    path = tmp_path / "config.json"
    legacy = _legacy_without_management()
    path.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config.secrets, "token_urlsafe", lambda _size: "N" * 64)
    writes = []

    def deny_write(value: dict) -> None:
        writes.append(copy.deepcopy(value))
        raise PermissionError("read-only config directory")

    monkeypatch.setattr(config, "_write_atomic", deny_write)
    loaded = config._load_from_disk()

    assert len(writes) == 1
    assert loaded["listen"] == legacy["listen"]
    assert loaded["apiKeys"] == legacy["apiKeys"]
    assert "management" not in loaded
    assert json.loads(path.read_text(encoding="utf-8")) == legacy
    with pytest.raises(ValueError, match="management configuration must be an object"):
        config.management_settings(loaded)
    output = capsys.readouterr().out
    assert "management disabled (PermissionError)" in output
    assert "pmk_" + "N" * 64 not in output


def _patch_successful_lifespan(monkeypatch, events: list[str]) -> None:
    monkeypatch.setenv("PARROT_NO_REFRESH", "1")
    server._background_tasks.clear()

    monkeypatch.setattr(server.state_db, "init", lambda: None)
    monkeypatch.setattr(server.network, "init", lambda: None)
    monkeypatch.setattr(server.network, "bootstrap_system_dns_once", lambda: None)
    monkeypatch.setattr(server.log_db, "init", lambda: None)
    monkeypatch.setattr(server.log_db, "cleanup_stale_pending", lambda _age: 0)
    monkeypatch.setattr(server.log_db, "maybe_cleanup_retention", lambda: {"ok": True, "skipped": True})
    monkeypatch.setattr(server.image_db, "init", lambda: None)
    monkeypatch.setattr(server.translation, "init", lambda: None)

    def initialize(app: FastAPI):
        events.append("management-init")
        app.state.management_runtime = object()
        return app.state.management_runtime

    async def close(app: FastAPI):
        events.append("management-close")
        app.state.management_runtime = None

    monkeypatch.setattr(server, "_initialize_management_runtime", initialize)
    monkeypatch.setattr(server, "_close_management_runtime", close)
    monkeypatch.setattr(server.oauth_manager, "migrate_provider_field", lambda: 0)
    monkeypatch.setattr(
        server.oauth_manager,
        "bootstrap_composite_key_migration",
        lambda: {"skipped": True, "reason": "test"},
    )
    monkeypatch.setattr(
        server.oauth_manager,
        "bootstrap_openai_workspace_key_migration",
        lambda: {"state": {"skipped": True, "reason": "test"}},
    )
    monkeypatch.setattr(server.affinity, "init", lambda: None)
    monkeypatch.setattr(server.affinity, "client_init", lambda: None)
    monkeypatch.setattr(server.cooldown, "init", lambda: None)
    monkeypatch.setattr(server.scorer, "init", lambda: None)

    from src.openai.channel import registration
    from src.openai import store as openai_store
    from src.cursor_bridge import runtime as cursor_bridge_runtime

    monkeypatch.setattr(registration, "register_factories", lambda: None)
    monkeypatch.setattr(openai_store, "init", lambda: None)
    monkeypatch.setattr(cursor_bridge_runtime, "ensure_started", lambda: None)
    monkeypatch.setattr(cursor_bridge_runtime, "stop", lambda: None)
    monkeypatch.setattr(server.registry, "rebuild_from_config", lambda: None)
    monkeypatch.setattr(server.registry, "install_config_reload_hook", lambda: None)
    monkeypatch.setattr(server.registry, "channel_count", lambda: 0)
    monkeypatch.setattr(server.provider_usage, "is_enabled", lambda: False)
    monkeypatch.setattr(server.upstream, "create_client", lambda: None)
    monkeypatch.setattr(server.model_pricing, "initialize", lambda: None)
    monkeypatch.setattr(
        server.model_metadata,
        "migrate_legacy_config",
        lambda: {"bindings": 0, "compression": 0},
    )
    monkeypatch.setattr(server.public_ip, "fetch_async", lambda: None)
    monkeypatch.setattr(
        server.config,
        "get",
        lambda: {
            "listen": {"host": "127.0.0.1", "port": 0},
            "apiKeys": {},
            "oauthAccounts": [],
            "channels": [],
            "telegram": {"botToken": "", "adminIds": []},
            "channelSelection": "smart",
            "oauth": {"mockMode": False},
            "timeouts": {},
            "cchMode": "disabled",
        },
    )
    monkeypatch.setattr(server, "codex_cli_version", lambda: "test")
    monkeypatch.setattr(server.updater, "resume_after_restart", lambda: None)

    async def wait_forever():
        await asyncio.Event().wait()

    for owner, name in (
        (server, "_wal_checkpoint_loop"),
        (server, "_stale_pending_loop"),
        (server, "_affinity_cleanup_loop"),
        (server.oauth_manager, "proactive_refresh_loop"),
        (server.oauth_manager, "quota_monitor_loop"),
        (server.oauth_manager, "oauth_model_sync_loop"),
        (server.probe, "recovery_loop"),
        (server.status_monitor, "monitor_loop"),
        (server.network_monitor, "monitor_loop"),
        (server.update_checker, "update_loop"),
        (server.model_pricing, "refresh_loop"),
        (openai_store, "cleanup_loop"),
        (server.translation, "cleanup_loop"),
    ):
        monkeypatch.setattr(owner, name, wait_forever)

    monkeypatch.setattr(server.drain, "begin", lambda _reason: None)
    monkeypatch.setattr(server.drain, "shutdown_timeout_seconds", lambda: 0.01)

    async def drained(_timeout):
        return True

    async def no_op():
        return None

    monkeypatch.setattr(server.drain, "wait_for_zero", drained)
    monkeypatch.setattr(server.apikey_limiter, "shutdown_spooling", no_op)
    monkeypatch.setattr(server.tgbot, "stop", lambda: None)
    monkeypatch.setattr(server.provider_usage, "stop", no_op)
    monkeypatch.setattr(server.upstream, "close_client", no_op)
    monkeypatch.setattr(server, "_finalize_state_store", lambda: True)


def test_p02_lifespan_closes_management_on_first_post_init_startup_failure(monkeypatch):
    events = []
    app = FastAPI()
    monkeypatch.setattr(server.state_db, "init", lambda: None)
    monkeypatch.setattr(server.network, "init", lambda: None)
    monkeypatch.setattr(server.network, "bootstrap_system_dns_once", lambda: None)
    monkeypatch.setattr(server.log_db, "init", lambda: None)
    monkeypatch.setattr(server.image_db, "init", lambda: None)
    monkeypatch.setattr(server.translation, "init", lambda: None)

    class Operations:
        async def aclose(self):
            events.append("operations-close")

    class Store:
        def close(self):
            events.append("store-close")

    def initialize(target: FastAPI):
        events.append("management-init")
        target.state.management_runtime = server.ManagementRuntime(
            sessions=None,
            approvals=None,
            operations=Operations(),
            operation_registry=None,
            audit_sink=None,
            state_store=Store(),
            allowed_origins=frozenset(),
            application_version="test",
            documentation_url="/docs",
        )
        server.tgbot.configure_management_approval_handler(lambda *_args: "approved")

    def fail(_age):
        events.append("startup-failure")
        raise RuntimeError("post-management startup failed")

    monkeypatch.setattr(server, "_initialize_management_runtime", initialize)
    monkeypatch.setattr(server.log_db, "cleanup_stale_pending", fail)

    async def run():
        with pytest.raises(RuntimeError, match="post-management startup failed"):
            async with server.lifespan(app):
                pytest.fail("lifespan must not yield after startup failure")

    asyncio.run(run())
    assert events == [
        "management-init", "startup-failure", "operations-close", "store-close",
    ]
    assert app.state.management_runtime is None
    assert server.tgbot._management_approval_handler is None


def test_p02_lifespan_normal_and_cancel_paths_close_management_once(monkeypatch):
    async def normal_case():
        events: list[str] = []
        _patch_successful_lifespan(monkeypatch, events)
        async with server.lifespan(FastAPI()):
            events.append("yield")
        server._background_tasks.clear()
        assert events.count("management-close") == 1

    asyncio.run(normal_case())


def test_p02_lifespan_cancellation_closes_management_once(monkeypatch):
    async def cancel_case():
        events: list[str] = []
        _patch_successful_lifespan(monkeypatch, events)
        entered = asyncio.Event()

        async def serve():
            async with server.lifespan(FastAPI()):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(serve())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server._background_tasks.clear()
        assert events.count("management-close") == 1

    asyncio.run(cancel_case())


class _BarrierLock:
    def __init__(self) -> None:
        self._barrier = threading.Barrier(2)
        self._lock = threading.RLock()

    def __enter__(self):
        self._barrier.wait(timeout=5)
        self._lock.acquire()
        return self

    def __exit__(self, *_args):
        self._lock.release()


class _Registry:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def register(self, kind, _starter) -> None:
        if kind in self.kinds:
            raise ValueError(f"duplicate operation: {kind}")
        self.kinds.append(kind)


def _concurrent_calls(call):
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call) for _ in range(2)]
        return [future.result(timeout=10) for future in futures]


def test_p03_channel_binding_publishes_the_runtime_owned_control(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace())
    control = SimpleNamespace(plan_owner="runtime")
    owner = SimpleNamespace(channels=control)
    runtime = SimpleNamespace(control_owner=lambda: owner)

    monkeypatch.setattr(channels, "_BIND_LOCK", _BarrierLock())
    request = SimpleNamespace(app=app)
    results = _concurrent_calls(lambda: channels.get_channel_control(request, runtime))

    assert results[0] is results[1] is control
    assert app.state.management_channel_control is control
    assert app.state.management_channel_control_runtime is runtime


def test_p03_apikey_binding_preserves_the_runtime_plan_owner(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace())
    control = SimpleNamespace(plans={"owner": "runtime"})
    owner = SimpleNamespace(api_keys=control)
    runtime = SimpleNamespace(control_owner=lambda: owner)

    monkeypatch.setattr(apikey, "_BIND_LOCK", _BarrierLock())
    request = SimpleNamespace(app=app)
    results = _concurrent_calls(lambda: apikey.get_api_key_control(request, runtime))

    assert results[0] is results[1] is control
    assert app.state.management_apikey_control is control
    assert app.state.management_apikey_control_runtime is runtime


def test_p03_auxiliary_binding_publishes_the_runtime_owned_bundle():
    app = SimpleNamespace(state=SimpleNamespace())
    controls = SimpleNamespace(plan_owner="runtime")
    owner = SimpleNamespace(auxiliary=controls)
    runtime = SimpleNamespace(control_owner=lambda: owner)
    request = SimpleNamespace(app=app)

    results = _concurrent_calls(
        lambda: auxiliary_support.get_bound_auxiliary_controls(request, runtime)
    )

    assert results[0] is results[1] is controls
    assert app.state.management_auxiliary_controls is controls
    assert app.state.management_auxiliary_controls_runtime is runtime


def test_p04_prefix_collisions_keep_plain_fastapi_origin_and_validation_behavior():
    app = FastAPI()
    app.state.management_runtime = None
    install_management_error_handlers(app)

    @app.get("/api/management/v10/ping")
    def collision_ping():
        return {"ok": True}

    @app.get("/api/management/v1-health/{value}")
    def collision_validation(value: int):
        return {"value": value}

    @app.get("/api/management/v1")
    def exact_management_root():
        return {"ok": True}

    @app.get("/api/management/v1/child")
    def nested_management_route():
        return {"ok": True}

    app.add_middleware(ManagementOriginMiddleware)
    client = TestClient(app)
    denied_origin = {"Origin": "https://not-allowed.invalid"}

    ping = client.get("/api/management/v10/ping", headers=denied_origin)
    assert ping.status_code == 200
    assert ping.json() == {"ok": True}
    assert "x-request-id" not in ping.headers

    invalid = client.get("/api/management/v1-health/not-an-int", headers=denied_origin)
    assert invalid.status_code == 422
    assert "detail" in invalid.json()
    assert "error" not in invalid.json()
    assert "x-request-id" not in invalid.headers

    for management_path in ("/api/management/v1", "/api/management/v1/child"):
        denied = client.get(management_path, headers=denied_origin)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ORIGIN_DENIED"


def test_p05_openapi_keeps_counts_ids_and_uses_the_seven_camelcase_templates():
    document = server.app.openapi()
    management_paths = {
        path: item
        for path, item in document["paths"].items()
        if path == "/api/management/v1" or path.startswith("/api/management/v1/")
    }
    operations = [
        operation
        for item in management_paths.values()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    operation_ids = [operation["operationId"] for operation in operations]
    expected_ids = set(
        (Path(__file__).parent / "fixtures/management_api/production-operation-ids.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )

    expected = {
        "/api/management/v1/load-balancing/model-orders/{modelId}": {
            "getModelChannelOrder", "replaceModelChannelOrder", "deleteModelChannelOrder",
        },
        "/api/management/v1/model-metadata/{modelId}": {"getModelMetadata"},
        "/api/management/v1/model-metadata/{modelId}/binding": {
            "putModelMetadataBinding", "deleteModelMetadataBinding",
        },
        "/api/management/v1/proxies/{proxyId}": {"getProxy", "updateProxy", "deleteProxy"},
        "/api/management/v1/proxies/{proxyId}/actions/test": {"testProxy"},
        "/api/management/v1/proxy-groups/{groupId}": {
            "getProxyGroup", "updateProxyGroup", "deleteProxyGroup",
        },
        "/api/management/v1/proxy-groups/{groupId}/actions/test": {"testProxyGroup"},
    }

    # Search contributes six paths/eight operations; retired AG media removes
    # three paths/five operations; independent media adds three paths/four operations.
    # All seven pre-existing templates below stay exact.
    assert len(management_paths) == 175
    assert len(operations) == 237
    assert len(set(operation_ids)) == 237
    assert set(operation_ids) == expected_ids
    assert sum(len(ids) for ids in expected.values()) == 14
    for path, ids in expected.items():
        assert path in management_paths
        path_item = management_paths[path]
        actual = {
            operation["operationId"]
            for method, operation in path_item.items()
            if method in {"get", "post", "put", "patch", "delete"}
        }
        assert actual == ids
        parameter_name = path.rsplit("{", 1)[1].split("}", 1)[0]
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            path_parameters = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "path"
            }
            assert path_parameters == {parameter_name}

    selected_paths = "\n".join(expected)
    assert "{model_id}" not in selected_paths
    assert "{proxy_id}" not in selected_paths
    assert "{group_id}" not in selected_paths
