"""Isolated fake transports + real catalog parsers, CAS and config persistence."""
from __future__ import annotations

import copy
import json
import time
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from src import config, model_pricing, model_state, network, oauth_manager, state_db
from src.channel import registry
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.management_control.models import ModelCenterControl, ModelSourceRef, ModelSourceType
from src.management_control.operations import OperationStatus, OperationStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Keep the real normalizer/atomic writes; never patch config.update/get.
    path = tmp_path / "config.json"
    initial = copy.deepcopy(config.get())
    initial.update(channels=[], oauthAccounts=[], stateDbPath=str(tmp_path / "state.db"),
                   runtimeStatePath=str(tmp_path / "runtime.json"), durableStatePath=str(tmp_path / "durable.json"))
    initial["oauth"]["mockMode"] = False
    path.write_text(json.dumps(initial))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0)
    monkeypatch.setattr(config, "_rejected_rewrite_version", None)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    state_db.init()
    registry.install_config_reload_hook()
    monkeypatch.setattr(registry, "_channels", {})
    requests = []
    responses = {}

    def handle(request):
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        requests.append(key)
        action = responses[key]
        return action(request) if callable(action) else httpx.Response(200, json=action)

    transport = httpx.MockTransport(handle)

    def async_client(**kwargs):
        for key in ("proxy_purpose", "proxy_provider", "proxy_channel"):
            kwargs.pop(key, None)
        return httpx.AsyncClient(transport=transport, **kwargs)

    def get_sync(url, **kwargs):
        for key in ("proxy_purpose", "proxy_channel"):
            kwargs.pop(key, None)
        with httpx.Client(transport=transport) as client:
            return client.get(url, **kwargs)

    async def valid_token(_key):
        return None

    monkeypatch.setattr(network, "async_client", async_client)
    monkeypatch.setattr(network, "get_sync", get_sync)
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", valid_token)
    async def metadata():
        return {"status": "succeeded", "catalog": "updated"}
    monkeypatch.setattr(model_pricing, "refresh_metadata_after_model_sync", metadata)
    store = OperationStore(max_workers=1)
    control = ModelCenterControl(operations=store)
    context = control.current_context()
    value = SimpleNamespace(
        path=path, requests=requests, responses=responses, store=store,
        control=control, context=context,
    )
    try:
        yield value
    finally:
        store.close()
        state_db.close()


def _api(name="alpha", **extra):
    return {
        "name": name, "generationId": "generation-" + name,
        "baseUrl": "https://fake.invalid/" + name,
        "apiKey": "api-secret-" + name, "protocol": "anthropic", "enabled": True,
        "models": [{"real": "old-real", "alias": "manual-alias", "metadata": {"note": "preserve"}},
                   {"real": "missing-today", "alias": "disabled-alias"}],
        **extra,
    }


def _oauth(name="empty", **extra):
    return {
        "provider": "claude", "email": name + "@example.test", "label": name,
        "access_token": "oauth-secret-" + name, "generationId": "oauth-generation-" + name,
        "enabled": False, "disabled_reason": "user", "models": [],
        "disabledModels": ["oauth-disabled"], **extra,
    }


def _install(apis=(), oauth=()):
    config.update(lambda cfg: cfg.update(channels=list(apis), oauthAccounts=list(oauth)))


def _payload(*models):
    return {"data": [{"id": model} for model in models]}


def _wait(env, operation):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        current = env.store.get(env.context, operation.id)
        if current.status not in {OperationStatus.QUEUED, OperationStatus.RUNNING}:
            assert current.finished_at is not None
            return current
        time.sleep(0.005)
    pytest.fail("operation never reached a terminal state")


def _start(env, source=None):
    return env.control.start_upstream_sync(env.context, source)


def _api_ref(name="alpha"):
    return ModelSourceRef(ModelSourceType.API, "api:" + name)


def _oauth_ref(account):
    return ModelSourceRef(ModelSourceType.OAUTH, oauth_manager.get_account_key(account))


def test_all_sources_includes_empty_disabled_oauth_and_preserves_configuration(env):
    api = _api()
    empty = _oauth()
    bad = _oauth("bad", models=["last-known-good"])
    _install([api], [empty, bad])
    config.update(lambda cfg: (
        model_state.set_api_source_enabled_in_config(cfg, "api:alpha", ["disabled-alias"], False),
        cfg.update(modelMapping={"global": {"independent": "manual-alias"}},
                   modelMetadataOverrides={"manual-alias": {"contextWindow": 12345}}),
    ))
    def purposes(cfg):
        state_key = oauth_manager.account_state_key(empty)
        cfg["images"].update(enabled=False, disabledSources=[state_key])
        cfg.setdefault("videos", {}).update(enabled=True, independentAccounts=[state_key])
        cfg["oauth"]["enabled"] = False
    config.update(purposes)
    before = copy.deepcopy(config.get())
    env.responses.update({
        "api-secret-alpha": _payload("old-real", "brand-new", "brand-new"),
        "oauth-secret-empty": _payload("oauth-new", "oauth-disabled"),
        "oauth-secret-bad": lambda request: httpx.Response(500, text="token=do-not-publish https://private.invalid"),
    })
    operation = _wait(env, _start(env))
    assert operation.status is OperationStatus.SUCCEEDED  # completed report, not all sources successful
    assert operation.result["status"] == "partial_failed"
    assert (operation.result["total"], operation.result["succeeded"], operation.result["failed"]) == (3, 2, 1)
    assert operation.progress.current == operation.progress.total == 3
    assert operation.progress.message_code.endswith("partial_failed")
    assert env.requests == ["api-secret-alpha", "oauth-secret-empty", "oauth-secret-bad"]
    assert {row["sourceId"] for row in operation.result["items"]} == {"api:alpha", _oauth_ref(empty).id, _oauth_ref(bad).id}
    assert all(set(row) >= {"status", "sourceType", "sourceId", "label", "count", "errorCode"} for row in operation.result["items"])
    saved = json.loads(env.path.read_text())
    assert saved["channels"][0] == {**api, "models": api["models"] + [{"real": "brand-new", "alias": "brand-new"}]}
    for key in ("modelCenter", "modelMapping", "modelMetadataOverrides", "images", "videos", "oauth"):
        assert saved[key] == before[key]
    assert saved["oauthAccounts"][0]["models"] == ["oauth-new", "oauth-disabled"]
    assert saved["oauthAccounts"][1]["models"] == ["last-known-good"]
    for index in range(2):
        for key in ("enabled", "disabled_reason", "disabledModels"):
            assert saved["oauthAccounts"][index][key] == before["oauthAccounts"][index][key]
    assert saved["oauthAccounts"][1]["last_model_sync_error"]
    assert "brand-new" in registry.available_models()
    assert "disabled-alias" not in registry.available_models()
    assert registry.get_channel("api:alpha").supports_model("manual-alias") == "old-real"
    assert "brand-new" in {row.model_id for row in env.control.list_models().items}
    public = json.dumps(operation.result)
    for forbidden in ("api-secret", "oauth-secret", "private.invalid", "https://", "do-not-publish"):
        assert forbidden not in public


def test_single_source_legacy_entrypoint_persists_and_does_not_query_others(env):
    _install([_api(), _api("beta")], [_oauth()])
    env.responses["api-secret-alpha"] = _payload("first", "second")
    operation = env.control.sync_source_models(env.context, _api_ref())
    final = _wait(env, operation)
    assert final.result["status"] == "succeeded"
    assert final.result["items"][0]["addedCount"] == 2
    assert env.requests == ["api-secret-alpha"]
    assert config.get()["channels"][1]["models"] == _api("beta")["models"]
    assert config.get()["channels"][0]["models"][-2:] == [{"real": name, "alias": name} for name in ("first", "second")]


@pytest.mark.parametrize("payload", [_payload(), {"unexpected": []}])
def test_api_empty_or_invalid_keeps_all_existing_models(env, payload):
    _install([_api()])
    before = copy.deepcopy(config.get()["channels"])
    env.responses["api-secret-alpha"] = payload
    final = _wait(env, _start(env, _api_ref()))
    assert final.result["status"] == "failed"
    assert final.result["items"][0]["errorCode"] == "UPSTREAM_ERROR"
    assert config.get()["channels"] == before


@pytest.mark.parametrize("fallback", [False, True])
def test_static_preset_never_counts_as_live_or_replaces_catalog(env, monkeypatch, fallback):
    from src.management_control.channels import discovery
    _install([_api(providerId="fake-provider", providerPresetId="fake-preset")])
    before = env.path.read_text()
    preset = SimpleNamespace(
        models_url="https://fake.invalid/models" if fallback else None,
        models_auth="bearer", models_parser="openai-data-id", static_models=("static-fallback",),
    )
    monkeypatch.setattr(discovery, "get_preset", lambda *args: preset)
    env.responses["api-secret-alpha"] = lambda request: httpx.Response(500, text="secret failure https://private.invalid")
    final = _wait(env, _start(env))
    assert final.result["status"] == "failed"
    assert final.result["items"][0]["errorCode"] == "UNSUPPORTED_VALUE"
    assert env.path.read_text() == before
    assert env.requests == (["api-secret-alpha"] if fallback else [])


def test_alias_collision_retains_manual_route_and_reports_partial_not_false_success(env):
    _install([_api()])
    env.responses["api-secret-alpha"] = _payload("manual-alias", "new")
    final = _wait(env, _start(env))
    assert final.result["status"] == "partial_failed"
    assert final.result["items"][0]["errorCode"] == "ALIAS_CONFLICT"
    assert final.result["items"][0]["skippedCount"] == 1
    assert config.get()["channels"][0]["models"] == _api()["models"] + [{"real": "new", "alias": "new"}]


@pytest.mark.parametrize("change", ["delete", "edit"])
def test_api_change_during_network_is_not_overwritten(env, change):
    _install([_api()])

    def response(request):
        if change == "delete":
            config.update(lambda cfg: cfg.update(channels=[]))
        else:
            config.update(lambda cfg: cfg["channels"][0].update(models=[{"real": "edited", "alias": "edited"}], apiKey="new-key"))
        return httpx.Response(200, json=_payload("late-model"))

    env.responses["api-secret-alpha"] = response
    final = _wait(env, _start(env))
    expected = "RESOURCE_NOT_FOUND" if change == "delete" else "REVISION_CONFLICT"
    assert final.result["items"][0]["errorCode"] == expected
    assert "late-model" not in env.path.read_text()
    if change == "edit":
        assert config.get()["channels"][0]["models"] == [{"real": "edited", "alias": "edited"}]


@pytest.mark.parametrize("change", ["delete", "edit"])
def test_oauth_formal_generation_cas_protects_inflight_results(env, change):
    account = _oauth(models=["lkg"])
    _install(oauth=[account])

    def response(request):
        if change == "delete":
            config.update(lambda cfg: cfg.update(oauthAccounts=[]))
        else:
            config.update(lambda cfg: cfg["oauthAccounts"][0].update(access_token="rotated-token", models=["edited"]))
        return httpx.Response(200, json=_payload("late-oauth"))

    env.responses["oauth-secret-empty"] = response
    final = _wait(env, _start(env, _oauth_ref(account)))
    assert final.result["status"] == "failed"
    assert final.result["items"][0]["errorCode"] == "REVISION_CONFLICT"
    assert "late-oauth" not in env.path.read_text()
    if change == "edit":
        assert config.get()["oauthAccounts"][0]["models"] == ["edited"]


@pytest.mark.parametrize("kind", ["api", "oauth"])
@pytest.mark.parametrize("change", ["delete", "edit"])
def test_queued_source_revalidated_before_network(env, kind, change):
    account = _oauth()
    _install([_api()], [account])
    entered, release = Event(), Event()
    blocker = env.store.create(env.context, kind="test.block", cancellable=False)

    def block():
        env.store.mark_running(blocker.id)
        entered.set()
        assert release.wait(5)
        env.store.succeed(blocker.id)

    env.store.submit(blocker.id, block)
    assert entered.wait(2)
    source = _api_ref() if kind == "api" else _oauth_ref(account)
    operation = _start(env, source)
    try:
        field = "channels" if kind == "api" else "oauthAccounts"
        if change == "delete":
            config.update(lambda cfg: cfg.update({field: []}))
        else:
            config.update(lambda cfg: cfg[field][0].update(enabled=True, label="new-label"))
    finally:
        release.set()
    final = _wait(env, operation)
    assert final.result["items"][0]["errorCode"] == ("RESOURCE_NOT_FOUND" if change == "delete" else "REVISION_CONFLICT")
    assert env.requests == []


@pytest.mark.parametrize("first_all", [True, False])
def test_background_singleflight_rejects_overlapping_all_and_single(env, first_all):
    _install([_api(), _api("beta")])
    entered, release = Event(), Event()

    def hold(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=_payload("new"))

    env.responses.update({"api-secret-alpha": hold, "api-secret-beta": _payload("beta-new")})
    started = time.monotonic()
    operation = _start(env, None if first_all else _api_ref())
    assert time.monotonic() - started < 0.5
    assert entered.wait(2)  # HTTP has not completed; callback already returned.
    try:
        for source in (None, _api_ref()):
            with pytest.raises(ManagementError) as error:
                _start(env, source)
            assert error.value.code is ManagementErrorCode.OPERATION_ALREADY_RUNNING
        assert env.requests == ["api-secret-alpha"]
        assert env.store.get(env.context, operation.id).status is OperationStatus.RUNNING
    finally:
        release.set()
    assert _wait(env, operation).result["status"] == "succeeded"
    env.responses["api-secret-alpha"] = _payload("new", "next")
    assert _wait(env, _start(env, _api_ref())).result["status"] == "succeeded"
    assert env.requests.count("api-secret-alpha") == 2


def test_partial_failure_does_not_block_later_source(env):
    _install([_api(), _api("beta")])
    env.responses.update({
        "api-secret-alpha": lambda request: httpx.Response(500, text="private-token"),
        "api-secret-beta": _payload("after-failure"),
    })
    final = _wait(env, _start(env))
    assert final.result["status"] == "partial_failed"
    assert [row["status"] for row in final.result["items"]] == ["failed", "succeeded"]
    assert "after-failure" in registry.available_models()


def test_empty_scope_and_missing_source_have_explicit_outcomes(env):
    _install()
    final = _wait(env, _start(env))
    assert final.result == {"status": "succeeded", "total": 0, "succeeded": 0, "failed": 0, "items": []}
    assert final.progress.current == final.progress.total == 0
    with pytest.raises(ManagementError) as error:
        _start(env, _api_ref("deleted"))
    assert error.value.code is ManagementErrorCode.RESOURCE_NOT_FOUND


def test_submission_failure_releases_scope_and_reaches_terminal(env, monkeypatch):
    _install([_api()])
    submit = env.store._executor.submit

    def reject(*args, **kwargs):
        raise RuntimeError("private-url-or-token")

    monkeypatch.setattr(env.store._executor, "submit", reject)
    with pytest.raises(ManagementError) as error:
        _start(env)
    assert env.store.get(env.context, error.value.operation_id).status is OperationStatus.FAILED
    monkeypatch.setattr(env.store._executor, "submit", submit)
    env.responses["api-secret-alpha"] = _payload("new")
    assert _wait(env, _start(env)).result["status"] == "succeeded"


@pytest.mark.parametrize("account, expected", [
    (_oauth(label=""), "Claude · empty@example.test"),
    (_oauth(label="Human"), "Claude · Human · empty@example.test"),
    (_oauth(provider="workbuddy", email="", label="", uid="opaque-uuid", realm="cn", phone="13800000000"), "WorkBuddy · 13800000000"),
    (_oauth(provider="workbuddy", email="", label="opaque-uuid", uid="opaque-uuid", realm="cn", nickname="Human WB"), "WorkBuddy · Human WB"),
    (_oauth(provider="workbuddy", email="", label="opaque-uuid", uid="opaque-uuid", realm="cn"), "WorkBuddy · 未命名账户"),
])
def test_oauth_result_label_uses_human_dto_identity_not_opaque_id(env, account, expected):
    _install(oauth=[account])
    source = env.control._upstream_sync._sources(_oauth_ref(account))[0]
    assert source.label == expected
    assert source.id == oauth_manager.get_account_key(account)
    assert "opaque-uuid" not in source.label


def test_oauth_models_edited_during_sync_must_not_be_overwritten(env):
    account = _oauth(models=["lkg"])
    _install(oauth=[account])

    def response(request):
        config.update(lambda cfg: cfg["oauthAccounts"][0].update(models=["user-edited-catalog"]))
        return httpx.Response(200, json=_payload("late-oauth"))

    env.responses["oauth-secret-empty"] = response
    final = _wait(env, _start(env, _oauth_ref(account)))
    assert config.get()["oauthAccounts"][0]["models"] == ["user-edited-catalog"]
    assert final.result["items"][0]["errorCode"] == "REVISION_CONFLICT"


def test_oauth_purpose_changes_during_sync_are_preserved(env):
    account = _oauth(models=["lkg"])
    _install(oauth=[account])

    def response(request):
        def mutate(cfg):
            cfg["oauthAccounts"][0].update(enabled=True, disabledModels=["late-oauth"])
            cfg["images"].update(enabled=False, disabledSources=["media-purpose-marker"])
            cfg.setdefault("videos", {}).update(independentAccounts=["media-purpose-marker"])
        config.update(mutate)
        return httpx.Response(200, json=_payload("late-oauth"))

    env.responses["oauth-secret-empty"] = response
    final = _wait(env, _start(env, _oauth_ref(account)))
    assert final.result["status"] == "succeeded"
    saved = json.loads(env.path.read_text())
    assert saved["oauthAccounts"][0]["models"] == ["late-oauth"]
    assert saved["oauthAccounts"][0]["enabled"] is True
    assert saved["oauthAccounts"][0]["disabledModels"] == ["late-oauth"]
    assert saved["images"]["enabled"] is False
    assert saved["images"]["disabledSources"] == ["media-purpose-marker"]
    assert saved["videos"]["independentAccounts"] == ["media-purpose-marker"]


def test_oauth_deleted_then_recreated_same_id_cannot_accept_old_result(env):
    account = _oauth(models=["lkg"])
    _install(oauth=[account])

    def response(request):
        config.update(lambda cfg: cfg.update(oauthAccounts=[]))
        config.update(lambda cfg: cfg.update(oauthAccounts=[{
            **account, "generationId": "new-incarnation", "models": ["new-incarnation-catalog"],
        }]))
        return httpx.Response(200, json=_payload("late-oauth"))

    env.responses["oauth-secret-empty"] = response
    final = _wait(env, _start(env, _oauth_ref(account)))
    assert final.result["items"][0]["errorCode"] == "REVISION_CONFLICT"
    assert config.get()["oauthAccounts"][0]["models"] == ["new-incarnation-catalog"]


def test_oauth_codex_not_modified_is_successful_live_validation(env):
    account = _oauth("codex", provider="openai", workspace_id="workspace-one")
    _install(oauth=[account])
    env.responses["oauth-secret-codex"] = lambda request: httpx.Response(
        200, headers={"etag": "catalog-v1"},
        json={"models": [{"slug": "gpt-live", "visibility": "list", "context_window": 32000}]},
    )
    first = _wait(env, _start(env, _oauth_ref(account)))
    assert first.result["status"] == "succeeded"
    before = copy.deepcopy(config.get()["oauthAccounts"][0])

    def not_modified(request):
        assert request.headers["If-None-Match"] == "catalog-v1"
        assert request.headers["ChatGPT-Account-ID"] == "workspace-one"
        return httpx.Response(304, headers={"etag": "catalog-v1"})

    env.responses["oauth-secret-codex"] = not_modified
    second = _wait(env, _start(env, _oauth_ref(account)))
    assert second.result["status"] == "succeeded"
    assert second.result["items"][0]["count"] == 1
    after = json.loads(env.path.read_text())["oauthAccounts"][0]
    assert after["models"] == before["models"] == ["gpt-live"]
    assert after["account_model_catalog"] == before["account_model_catalog"]
    assert after["enabled"] is False
    assert env.requests == ["oauth-secret-codex", "oauth-secret-codex"]


def test_shutdown_marks_owned_operation_terminal(env):
    _install([_api(), _api("beta")])
    entered, release = Event(), Event()

    def hold(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=_payload("new"))

    env.responses["api-secret-alpha"] = hold
    operation = _start(env)
    assert entered.wait(2)
    try:
        env.store.close(timeout_seconds=0)
        assert env.store.get(env.context, operation.id).status is OperationStatus.FAILED
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while env.control._upstream_sync._active and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not env.control._upstream_sync._active
    assert env.requests == ["api-secret-alpha"]
    assert env.store.get(env.context, operation.id).status is OperationStatus.FAILED


# ── 多来源选择、实时进度与取消 ─────────────────────────────────────────────


def test_multi_source_sync_only_touches_the_selected_ones(env):
    """多选来源：只同步选中的，未选中的不发请求。"""
    _install(apis=[_api("alpha"), _api("beta"), _api("gamma")])
    refs = (_api_ref("alpha"), _api_ref("gamma"))
    operation = env.control.start_upstream_sync(env.context, None, sources=refs)
    result = _wait(env, operation)
    assert result.status is OperationStatus.SUCCEEDED
    assert sorted(env.requests) == ["api-secret-alpha", "api-secret-gamma"]
    labels = [item["label"] for item in result.result["items"]]
    assert labels == ["alpha", "gamma"]      # 保持请求顺序


def test_multi_source_selection_rejects_unknown_and_empty(env):
    """未知来源与空集合都必须明确报错，而不是静默跳过。"""
    with pytest.raises(ManagementError) as unknown:
        env.control.start_upstream_sync(
            env.context, None, sources=(ModelSourceRef(ModelSourceType.API, "api:不存在"),))
    assert unknown.value.code is ManagementErrorCode.RESOURCE_NOT_FOUND
    with pytest.raises(ManagementError):
        env.control.start_upstream_sync(env.context, None, sources=())


def test_progress_sink_receives_start_and_done_per_source(env):
    """每一步都要回调：进入某项、完成某项各一次，且带标签与耗时。"""
    _install(apis=[_api("alpha")])
    env.responses["api-secret-alpha"] = _payload("m1", "m2")
    events = []
    operation = env.control.start_upstream_sync(
        env.context, None, sources=(_api_ref("alpha"),),
        progress_sink=lambda op_id, event: events.append(dict(event)),
    )
    _wait(env, operation)
    # 来源完成后统一同步元数据；finished 仍是整个任务落地后的重绘信号。
    phases = [e["phase"] for e in events]
    assert phases == ["start", "done", "metadata_start", "metadata_done", "finished"], phases
    done = next(e for e in events if e["phase"] == "done")
    assert done["label"] == "alpha"
    assert done["status"] == "succeeded"
    assert done["count"] >= 1
    assert done["elapsedMs"] >= 0
    assert isinstance(done["models"], list)


def test_progress_sink_failure_never_breaks_the_sync(env):
    """sink 只是展示增强；它抛异常不能影响同步本身。"""
    _install(apis=[_api("alpha")])
    env.responses["api-secret-alpha"] = _payload("m1")
    operation = env.control.start_upstream_sync(
        env.context, None, sources=(_api_ref("alpha"),),
        progress_sink=lambda op_id, event: (_ for _ in ()).throw(RuntimeError("sink boom")),
    )
    result = _wait(env, operation)
    assert result.status is OperationStatus.SUCCEEDED
    assert result.result["succeeded"] == 1


def test_queued_cancel_releases_sources_and_sink_before_worker_runs(env):
    _install([_api(), _api("beta")])
    entered, release = Event(), Event()
    blocker = env.store.create(env.context, kind="test.block", cancellable=False)

    def block():
        env.store.mark_running(blocker.id)
        entered.set()
        assert release.wait(5)
        env.store.succeed(blocker.id)

    env.store.submit(blocker.id, block)
    assert entered.wait(2)
    events = []
    try:
        # Repeat while the only worker remains occupied: cancellation must not
        # rely on the abandoned body eventually entering its finally block.
        for _ in range(2):
            operation = env.control.start_upstream_sync(
                env.context, progress_sink=lambda oid, event: events.append((
                    event["phase"], env.store.get(env.context, oid).status,
                )),
            )
            assert env.store.get(env.context, operation.id).status is OperationStatus.QUEUED
            future = env.store._futures[operation.id]
            env.store.cancel(env.context, operation.id)
            assert future.cancelled()
            assert not env.control._upstream_sync._active
            assert operation.id not in env.control._upstream_sync._sinks
        assert events == [("cancelled", OperationStatus.CANCELLED)] * 2
        assert env.requests == []
        env.responses.update({
            "api-secret-alpha": _payload("new-alpha"),
            "api-secret-beta": _payload("new-beta"),
        })
        retry = _start(env)
        assert len(env.control._upstream_sync._active) == 2
    finally:
        release.set()
    _wait(env, blocker)
    assert _wait(env, retry).result["status"] == "succeeded"
    assert env.requests == ["api-secret-alpha", "api-secret-beta"]


def test_shutdown_releases_sources_of_never_started_sync(env):
    _install([_api()])
    entered, release = Event(), Event()
    blocker = env.store.create(env.context, kind="test.block", cancellable=False)

    def block():
        entered.set()
        assert release.wait(5)

    env.store.submit(blocker.id, block)
    assert entered.wait(2)
    try:
        operation = env.control.start_upstream_sync(env.context, progress_sink=lambda *args: None)
        env.store.close(timeout_seconds=0)
        assert env.store.get(env.context, operation.id).status is OperationStatus.FAILED
        assert not env.control._upstream_sync._active
        assert operation.id not in env.control._upstream_sync._sinks
        assert env.requests == []
    finally:
        release.set()


def test_cancel_stops_after_the_current_source(env):
    """取消是协作式的：当前项跑完，后续项不再发起。"""
    _install(apis=[_api("alpha"), _api("beta")])
    env.responses["api-secret-beta"] = _payload("m-beta")
    entered = Event()
    release = Event()

    def hold(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=_payload("m1"))

    env.responses["api-secret-alpha"] = hold
    operation = env.control.start_upstream_sync(
        env.context, None, sources=(_api_ref("alpha"), _api_ref("beta")))
    assert entered.wait(2)
    try:
        env.store.cancel(env.context, operation.id)
        assert env.control._upstream_sync._active
        with pytest.raises(ManagementError) as error:
            _start(env, _api_ref())
        assert error.value.code is ManagementErrorCode.OPERATION_ALREADY_RUNNING
    finally:
        release.set()
    # 取消会立刻把状态置为 terminal，但 worker 的 finally 还在收尾；
    # 等 _active 清空才说明它真的停了。
    deadline = time.monotonic() + 5
    while env.control._upstream_sync._active and time.monotonic() < deadline:
        time.sleep(0.005)
    # beta 没有开始
    assert env.requests == ["api-secret-alpha"]
    assert not env.control._upstream_sync._active


def test_sync_operation_is_cancellable(env):
    """同步任务必须标记为可取消，否则界面上的取消按钮无法生效。"""
    _install(apis=[_api("alpha")])
    operation = env.control.start_upstream_sync(env.context, None, sources=(_api_ref("alpha"),))
    assert env.store.get(env.context, operation.id).cancellable is True
    _wait(env, operation)


def test_cancel_emits_a_cancelled_event_for_the_page(env):
    """取消要发出事件，进度页才能从"正在同步"切到"已取消"。

    取消发生在某项执行期间时，循环是靠进度更新的异常中断的；若不在这里补发
    事件，页面会永远停在"正在同步"。
    """
    _install(apis=[_api("alpha")])
    entered = Event()
    release = Event()

    def hold(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=_payload("m1"))

    env.responses["api-secret-alpha"] = hold
    events = []
    operation = env.control.start_upstream_sync(
        env.context, None, sources=(_api_ref("alpha"),),
        progress_sink=lambda op_id, event: events.append(dict(event)),
    )
    assert entered.wait(2)
    try:
        env.store.cancel(env.context, operation.id)
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while "cancelled" not in [e["phase"] for e in events] and time.monotonic() < deadline:
        time.sleep(0.005)
    cancelled = [e for e in events if e["phase"] == "cancelled"]
    assert cancelled, f"未发出取消事件：{[e['phase'] for e in events]}"
    assert cancelled[-1]["label"] == "alpha"


def test_finished_event_arrives_after_the_operation_reaches_terminal(env):
    """最后一项完成后必须再发一次收尾事件。

    回归点：最后一项的 done 是在 _batch 里发的，那时任务仍是 RUNNING，进度页
    因此还画着"取消同步"；此后没有任何事件，按钮就永久卡住，点它只会得到
    INVALID_OPERATION_STATE（界面显示"操作失败，请稍后重试"）。
    """
    _install(apis=[_api("alpha")])
    env.responses["api-secret-alpha"] = _payload("m1")
    seen = []
    operation = env.control.start_upstream_sync(
        env.context, None, sources=(_api_ref("alpha"),),
        progress_sink=lambda op_id, event: seen.append(dict(event)),
    )
    _wait(env, operation)
    deadline = time.monotonic() + 5
    while "finished" not in [e["phase"] for e in seen] and time.monotonic() < deadline:
        time.sleep(0.005)
    assert "finished" in [e["phase"] for e in seen], [e["phase"] for e in seen]
    # 收到 finished 时任务已经是终态，页面据此才能隐藏取消按钮
    assert env.store.get(env.context, operation.id).status is OperationStatus.SUCCEEDED
