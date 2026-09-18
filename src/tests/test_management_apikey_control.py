from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.management_auth import AuthMethod, Capability, ManagementPrincipal
from src.management_control import BoundedAuditSink, ManagementContext, ManagementError
from src.management_control.apikey import (
    ApiKeyControl,
    ApiKeyEnabledFilter,
    ApiKeyProvenance,
    ApiKeySort,
    ApiKeySource,
)


class FakeConfig:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.update_calls = 0

    def get(self):
        return self.value

    def update(self, mutator):
        candidate = deepcopy(self.value)
        mutator(candidate)
        self.value = candidate
        self.update_calls += 1
        return self.value


class FakeLimiter:
    def __init__(self):
        self.forgot = []
        self.snapshots = {}

    def key_snapshot(self, name):
        return {
            "enabled": True,
            "in_flight": 1 if name == "busy" else 0,
            "max_concurrent": 3,
            "max_queue": 7,
            "queue_wait_seconds": 60,
            "waiting": 2 if name == "busy" else 0,
            "oldest_wait_seconds": 4,
            "unlimited": False,
            "enabled_source": "global",
            "max_concurrent_source": "global",
            "max_queue_source": "global",
            "queue_wait_source": "global",
            **self.snapshots.get(name, {}),
        }

    def forget_key(self, name):
        self.forgot.append(name)


class FakeStats:
    def stats_period_snapshot(self, since):
        return {
            "by_apikey": {
                "alpha": {"total": 5, "success_count": 4, "error_count": 1},
                "busy": {"total": 9, "success_count": 9},
            }
        }

    def tokens_for_apikey(self, name, since):
        return {
            "total": 2,
            "success_count": 1,
            "error_count": 1,
            "input": 10,
            "output": 4,
            "cache_creation": 2,
            "cache_read": 3,
            "cost_ticks": 12,
        }

    def apikey_model_stats(self, name, since_ts):
        return [{
            "final_model": "model-a",
            "total": 2,
            "success_count": 1,
            "error_count": 1,
            "input": 10,
            "output": 4,
            "cache_creation": 2,
            "cache_read": 3,
        }]


class FakeModels:
    @staticmethod
    def available_models():
        return ["model-a", "model-b"]


class Sequence:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self, *args):
        return next(self.values)


def context(*capabilities, subject="actor"):
    principal = ManagementPrincipal.with_capabilities(
        subject_id=subject,
        auth_method=AuthMethod.MANAGEMENT_KEY,
        capabilities=capabilities,
        issued_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return ManagementContext(request_id="request-test", actor=principal)


def make_control(
    value=None, *, clock=None, generated=None, tokens=None, provenance_store=None,
):
    store = FakeConfig(value or {
        "apiKeys": {
            "alpha": {
                "key": "alpha-secret",
                "source": "custom",
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            },
            "busy": {
                "key": "ccp-busy-secret",
                "source": "generated",
                "enabled": False,
                "allowedModels": ["model-a"],
                "allowImages": True,
                "allowVideos": False,
            },
        },
        "xaiOAuth": {"imageModels": ["image-1"], "videoModels": ["video-1"]},
    })
    limiter = FakeLimiter()
    audit = BoundedAuditSink()
    control = ApiKeyControl(
        config_store=store,
        provenance_store=provenance_store,
        limiter=limiter,
        statistics=FakeStats(),
        model_registry=FakeModels(),
        audit_sink=audit,
        clock=clock or (lambda: 1_767_225_600.0),
        token_factory=tokens or Sequence(["plan-id-token", "plan-secret-token"]),
        generated_secret_factory=generated or Sequence(["ccp-" + "a" * 48]),
    )
    return control, store, limiter, audit


def assert_error(code, call):
    with pytest.raises(ManagementError) as caught:
        call()
    assert caught.value.code.value == code
    return caught.value


def test_permission_images_use_configured_sources_not_unsourced_defaults():
    control, store, _, _ = make_control({
        "apiKeys": {}, "oauthAccounts": [], "channels": [],
        "images": {"toolModel": "gpt-image-2"},
        "xaiOAuth": {"imageModels": ["image-z", "image-a"], "videoModels": []},
    })
    read = context(Capability.READ)
    assert control.configured_media_models(read) == (("image-z", "image-a"), ())
    assert control.available_permission_models(read) == ("model-a", "model-b", "image-z", "image-a")
    store.value["oauthAccounts"] = [{
        "provider": "openai", "email": "image@example.test", "workspace_id": "image-workspace",
        "access_token": "synthetic-test-token", "enabled": False, "disabled_reason": "user",
    }]
    store.value["channels"] = [{
        "name": "api-image", "protocol": "openai-responses", "enabled": False,
        "models": [{"alias": "image-api-alias", "real": "gpt-image-2", "kind": "image"}],
    }]
    images, _ = control.configured_media_models(read)
    # 断言关心的是"内置来源 + 配置来源都给出，且不混入无来源的默认值"，
    # 不是种子名单的完整元组——种子会随版本增删模型。
    assert set(images) >= {'gpt-image-2', 'gpt-image-2.5', 'image-z', 'image-a', 'image-api-alias'}
    assert images[-1] == 'image-api-alias'      # 配置的 API 来源排在最后
    assert control.available_permission_models(read) == control.available_permission_models_unchecked()
    assert set(images) <= set(control.available_permission_models(read))


def test_list_detail_filter_sort_page_and_no_secret_leak():
    control, _, _, _ = make_control()
    read = context(Capability.READ)

    page = control.list_api_keys(
        read,
        page=1,
        page_size=1,
        enabled=ApiKeyEnabledFilter.ALL,
        sort=ApiKeySort.MONTH_CALLS_DESC,
    )
    assert page.total == 2 and page.has_next is True
    assert page.items[0].key_id == "busy"
    assert page.items[0].month_stats.total == 9
    assert page.items[0].secret is None
    assert "busy-secret" not in page.items[0].masked_hint

    filtered = control.list_api_keys(
        read,
        enabled=ApiKeyEnabledFilter.ENABLED,
        source=ApiKeySource.CUSTOM,
        name_contains="ALP",
    )
    assert [item.name for item in filtered.items] == ["alpha"]

    detail = control.get_api_key(read, "alpha")
    assert detail.model_stats[0].model == "model-a"
    assert detail.secret is None
    assert_error("RESOURCE_NOT_FOUND", lambda: control.get_api_key(read, "missing"))
    error = assert_error(
        "VALIDATION_FAILED",
        lambda: control.list_api_keys(read, page_size=201),
    )
    assert error.fields[0].path == "page"


def test_create_update_replace_delete_revision_conflicts_and_side_effects():
    control, store, limiter, audit = make_control()
    secrets_write = context(Capability.SECRETS_WRITE)
    write = context(Capability.WRITE)
    destructive = context(Capability.DESTRUCTIVE)

    created = control.create_api_key(
        secrets_write,
        name="client.new",
        mode=ApiKeySource.CUSTOM,
        custom_secret="client-secret+/=",
    )
    assert created.secret == "client-secret+/="
    assert created.api_key.secret is None
    assert store.value["apiKeys"]["client.new"]["key"] == created.secret
    assert_error(
        "RESOURCE_CONFLICT",
        lambda: control.create_api_key(
            secrets_write,
            name="client.new",
            mode=ApiKeySource.CUSTOM,
            custom_secret="another-secret",
        ),
    )
    invalid = assert_error(
        "VALIDATION_FAILED",
        lambda: control.create_api_key(
            secrets_write,
            name="bad name",
            mode=ApiKeySource.GENERATED,
        ),
    )
    assert invalid.fields[0].path == "name"

    before = created.api_key.revision
    updated = control.update_api_key(
        write,
        "client.new",
        changes={
            "enabled": False,
            "allowed_models": ["model-a", "image-1"],
            "allow_images": True,
            "limit_override": {"max_concurrent": 0, "max_queue": 2},
        },
        if_match=before,
    )
    assert updated.enabled is False and updated.allowed_models == ("model-a", "image-1")
    assert updated.limit_override.max_concurrent == 0
    assert_error(
        "REVISION_CONFLICT",
        lambda: control.update_api_key(
            write, "client.new", changes={"enabled": True}, if_match=before,
        ),
    )

    assert_error(
        "CONFIRMATION_REQUIRED",
        lambda: control.replace_api_key_secret(
            secrets_write, "client.new", custom_secret="replacement-secret", if_match=None,
        ),
    )
    replaced = control.replace_api_key_secret(
        secrets_write,
        "client.new",
        custom_secret="replacement-secret",
        if_match=updated.revision,
    )
    assert replaced.secret == "replacement-secret"
    assert limiter.forgot == ["client.new"]

    assert_error(
        "CONFIRMATION_REQUIRED",
        lambda: control.delete_api_key(destructive, "client.new", if_match=None),
    )
    control.delete_api_key(
        destructive, "client.new", if_match=replaced.api_key.revision,
    )
    assert "client.new" not in store.value["apiKeys"]
    assert limiter.forgot == ["client.new", "client.new"]
    assert [row.action for row in audit.snapshot()] == [
        "apikey.create", "apikey.update", "apikey.secret.replace", "apikey.delete",
    ]


def test_source_uses_persisted_provenance_not_ccp_secret_prefix():
    control, store, _, _ = make_control({
        "apiKeys": {
            "legacy-custom": {
                "key": "ccp-user-chosen",
                "enabled": True,
                "allowedModels": [],
                "allowImages": False,
                "allowVideos": False,
            },
        },
    })
    read = context(Capability.READ)
    secrets_write = context(Capability.SECRETS_WRITE)

    legacy = control.get_api_key(read, "legacy-custom")
    assert legacy.source is ApiKeyProvenance.UNKNOWN
    assert [item.key_id for item in control.list_api_keys(
        read, source=ApiKeyProvenance.CUSTOM, include_stats=False,
    ).items] == []
    assert [item.key_id for item in control.list_api_keys(
        read, source=ApiKeyProvenance.UNKNOWN, include_stats=False,
    ).items] == ["legacy-custom"]

    custom = control.create_api_key(
        secrets_write,
        name="custom-prefix",
        mode=ApiKeySource.CUSTOM,
        custom_secret="ccp-custom-secret",
    )
    assert custom.api_key.source is ApiKeyProvenance.CUSTOM
    assert store.value["apiKeys"]["custom-prefix"]["source"] == "custom"

    generated = control.create_api_key(
        secrets_write, name="generated", mode=ApiKeySource.GENERATED,
    )
    assert generated.api_key.source is ApiKeyProvenance.GENERATED
    replaced = control.replace_api_key_secret(
        secrets_write,
        "generated",
        custom_secret="ccp-still-custom",
        if_match=generated.api_key.revision,
    )
    assert replaced.api_key.source is ApiKeyProvenance.CUSTOM
    assert store.value["apiKeys"]["generated"]["source"] == "custom"

    assert [item.key_id for item in control.list_api_keys(
        read, source=ApiKeyProvenance.CUSTOM, include_stats=False,
    ).items] == ["custom-prefix", "generated"]


def test_regeneration_plan_is_actor_bound_expiring_one_shot_and_resets_runtime():
    now = [1_767_225_600.0]
    control, store, limiter, _ = make_control(
        clock=lambda: now[0],
        generated=Sequence(["ccp-" + "b" * 48, "ccp-" + "c" * 48]),
        tokens=Sequence(["plan-one", "token-one", "plan-two", "token-two"]),
    )
    actor = context(Capability.SECRETS_WRITE, subject="one")
    other = context(Capability.SECRETS_WRITE, subject="two")

    plan = control.plan_regeneration(actor, "alpha")
    assert "alpha-secret" not in repr(plan)
    assert_error(
        "CAPABILITY_DENIED",
        lambda: control.regenerate_api_key(
            other,
            "alpha",
            plan_id=plan.plan_id,
            plan_token=plan.plan_token,
        ),
    )
    result = control.regenerate_api_key(
        actor,
        "alpha",
        plan_id=plan.plan_id,
        plan_token=plan.plan_token,
    )
    assert result.secret == "ccp-" + "b" * 48
    assert store.value["apiKeys"]["alpha"]["key"] == result.secret
    assert limiter.forgot == ["alpha"]
    assert_error(
        "INVALID_OPERATION_STATE",
        lambda: control.regenerate_api_key(
            actor,
            "alpha",
            plan_id=plan.plan_id,
            plan_token=plan.plan_token,
        ),
    )

    expired = control.plan_regeneration(actor, "alpha")
    now[0] += 301
    assert_error(
        "INVALID_OPERATION_STATE",
        lambda: control.regenerate_api_key(
            actor,
            "alpha",
            plan_id=expired.plan_id,
            plan_token=expired.plan_token,
        ),
    )


def test_reorder_reset_stats_authorization_and_complete_set_validation():
    control, store, limiter, _ = make_control()
    read = context(Capability.READ)
    write = context(Capability.WRITE)
    denied = context()

    collection = control.list_api_keys(read, include_stats=False).revision
    revision = control.reorder_api_keys(
        write, ["busy", "alpha"], if_match=collection,
    )
    assert list(store.value["apiKeys"]) == ["busy", "alpha"]
    assert revision != collection
    error = assert_error(
        "VALIDATION_FAILED",
        lambda: control.reorder_api_keys(
            write, ["busy"], if_match=revision,
        ),
    )
    assert error.fields[0].path == "keyIds"

    snapshot = control.reset_api_key_limiter(write, "busy")
    assert snapshot.in_flight == 1
    assert limiter.forgot == ["busy"]
    stats = control.get_api_key_stats(read, "alpha")
    assert stats.overall.total == 2 and stats.by_model[0].model == "model-a"
    assert_error(
        "CAPABILITY_DENIED",
        lambda: control.list_api_keys(denied),
    )
