from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from src import config, model_metadata, model_state
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.management_control.models import (
    ModelCenterControl,
    ModelFilters,
    ModelKind,
    ModelSelection,
    ModelSelectionMode,
    ModelSourceRef,
    ModelSourceType,
    ModelStateField,
    ModelStateTarget,
    ModelStatus,
)


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    config.update(lambda current: (current.clear(), current.update(copy.deepcopy(config.DEFAULT_CONFIG))))
    monkeypatch.setattr(model_metadata, "resolve_binding", lambda *_args, **_kwargs: None)


def _install_catalog() -> None:
    def mutate(cfg):
        cfg["channels"] = [
            {
                "name": "alpha", "type": "api", "enabled": True,
                "protocol": "openai-chat", "providerId": "openai",
                "baseUrl": "https://alpha.invalid", "apiKey": "test",
                "models": [
                    {"real": "upstream-alpha", "alias": "shared"},
                    {"real": "only-alpha", "alias": "only-alpha"},
                ],
            },
            {
                "name": "beta", "type": "api", "enabled": True,
                "protocol": "openai-chat", "providerId": "openai",
                "baseUrl": "https://beta.invalid", "apiKey": "test",
                "models": [{"real": "upstream-beta", "alias": "shared"}],
            },
        ]
        cfg["oauthAccounts"] = [{
            "provider": "claude", "email": "lkg@example.test", "enabled": True,
            "models": ["oauth-live", "oauth-disabled"],
            "disabledModels": ["oauth-disabled"],
        }]
        cfg["modelMapping"] = {"global": {"assistant": "shared"}}
        cfg["xaiOAuth"]["imageModels"] = ["same-media"]
        cfg["xaiOAuth"]["videoModels"] = ["same-media"]
        cfg["antigravityOAuth"]["imageModels"] = ["same-media"]
        cfg["oauthAccounts"].append({
            "provider": "antigravity", "email": "ag@example.test",
            "project_id": "project-test", "enabled": True,
            "models": ["ag-chat"], "imageModels": ["same-media"],
        })
    config.update(mutate)


def test_source_effective_metadata_is_not_promoted_across_aggregated_sources(monkeypatch):
    _install_catalog()

    def resolve(model_id, *, scope_key=None, outbound_model=None):
        if model_id != "shared":
            return None
        value = 100 if scope_key == "api:alpha" else 200 if scope_key == "api:beta" else 999
        return SimpleNamespace(
            metadata={"contextWindow": value},
            value_source={"contextWindow": "source-override"},
            constrained_by={},
            source=f"binding:{scope_key}",
        )

    monkeypatch.setattr(model_metadata, "resolve_binding", resolve)
    item = next(row for row in ModelCenterControl().list_models().items if row.model_id == "shared")
    assert item.common_metadata == {}
    assert {
        source.id: source.effective_metadata["contextWindow"] for source in item.sources
    } == {"api:alpha": 100, "api:beta": 200}
    assert all("contextWindow" in source.value_source for source in item.sources)


def test_query_aggregates_chat_retains_oauth_disabled_and_disambiguates_media():
    _install_catalog()
    control = ModelCenterControl()
    page = control.list_models()

    shared = next(item for item in page.items if item.model_id == "shared" and item.identity.kind is ModelKind.CHAT)
    assert len(shared.sources) == 2
    assert {source.id for source in shared.sources} == {"api:alpha", "api:beta"}
    assert shared.aliases == ("assistant",)
    disabled = next(item for item in page.items if item.model_id == "oauth-disabled")
    assert disabled.sources[0].source_enabled is False
    assert disabled.sources[0].effective_routable is False

    media = [item for item in page.items if item.model_id == "same-media"]
    assert {item.identity.kind for item in media} == {ModelKind.IMAGE, ModelKind.VIDEO}
    assert len({item.resource_key for item in media}) == 2
    assert all(item.identity.provider != "antigravity" for item in media)
    assert next(item for item in media if item.identity.kind is ModelKind.IMAGE).global_enabled is True
    assert control.get_model(None, media[0].resource_key) == media[0]

    by_source = control.list_models(filters=ModelFilters(
        source=ModelSourceRef(ModelSourceType.API, "api:alpha"),
        statuses=(ModelStatus.ENABLED,),
    ))
    assert {item.model_id for item in by_source.items} == {"shared", "only-alpha"}
    text = control.list_models(filters=ModelFilters(text="UPSTREAM-BETA"))
    assert [item.model_id for item in text.items] == ["shared"]
    with pytest.raises(ManagementError) as exc:
        control.list_models(filters=ModelFilters(
            source=ModelSourceRef(ModelSourceType.API, "api:missing")
        ))
    assert exc.value.code is ManagementErrorCode.RESOURCE_NOT_FOUND


def test_global_source_and_visible_state_are_orthogonal_atomic_and_idempotent():
    _install_catalog()
    control = ModelCenterControl()
    revision = control.list_models().revision

    disabled_global = control.set_state(
        None, scope=None,
        selection=ModelSelection(ModelSelectionMode.IDS, ("shared",)),
        target=ModelStateTarget(ModelStateField.ENABLED, False),
        expected_revision=revision,
    )
    assert disabled_global.items[0].status == "updated"
    assert not model_state.is_global_enabled("shared")

    source_off = control.set_state(
        None, scope=ModelSourceRef(ModelSourceType.API, "api:alpha"),
        selection=ModelSelection(ModelSelectionMode.IDS, ("shared",)),
        target=ModelStateTarget(ModelStateField.ENABLED, False),
        expected_revision=disabled_global.revision,
    )
    assert not model_state.is_source_enabled("api:alpha", "shared")

    global_on = control.set_state(
        None, scope=None,
        selection=ModelSelection(ModelSelectionMode.IDS, ("shared",)),
        target=ModelStateTarget(ModelStateField.ENABLED, True),
        expected_revision=source_off.revision,
    )
    assert model_state.is_global_enabled("shared")
    assert not model_state.is_source_enabled("api:alpha", "shared")
    view = next(item for item in control.list_models().items if item.model_id == "shared")
    routes = {source.id: source.effective_routable for source in view.sources}
    assert routes == {"api:alpha": False, "api:beta": True}

    hidden = control.set_state(
        None, scope=None,
        selection=ModelSelection(ModelSelectionMode.FILTER, filters=ModelFilters(
            kinds=(ModelKind.CHAT,), text="shared",
        )),
        target=ModelStateTarget(ModelStateField.VISIBLE, False),
        expected_revision=global_on.revision,
    )
    assert not model_state.is_discovery_visible("shared")
    assert model_state.is_global_enabled("shared")
    repeated = control.set_state(
        None, scope=None,
        selection=ModelSelection(ModelSelectionMode.IDS, ("shared",)),
        target=ModelStateTarget(ModelStateField.VISIBLE, False),
        expected_revision=hidden.revision,
    )
    assert repeated.items[0].status == "unchanged"
    assert repeated.revision == hidden.revision

    before = copy.deepcopy(config.get())
    with pytest.raises(ManagementError) as stale:
        control.set_state(
            None, scope=None,
            selection=ModelSelection(ModelSelectionMode.IDS, ("shared",)),
            target=ModelStateTarget(ModelStateField.VISIBLE, True),
            expected_revision="rev_stale",
        )
    assert stale.value.code is ManagementErrorCode.REVISION_CONFLICT
    assert config.get() == before

    with pytest.raises(ManagementError) as invalid:
        control.set_state(
            None, scope=ModelSourceRef(ModelSourceType.API, "api:alpha"),
            selection=ModelSelection(ModelSelectionMode.IDS, ("shared",)),
            target=ModelStateTarget(ModelStateField.VISIBLE, True),
            expected_revision=repeated.revision,
        )
    assert invalid.value.code is ManagementErrorCode.UNSUPPORTED_VALUE
    assert config.get() == before


def test_oauth_source_state_updates_authoritative_account_disabled_set():
    _install_catalog()
    control = ModelCenterControl()
    page = control.list_models()
    disabled = next(item for item in page.items if item.model_id == "oauth-disabled")
    source = disabled.sources[0]
    assert source.type is ModelSourceType.OAUTH and source.source_enabled is False
    enabled = control.set_state(
        None,
        scope=ModelSourceRef(ModelSourceType.OAUTH, source.id),
        selection=ModelSelection(ModelSelectionMode.IDS, ("oauth-disabled",)),
        target=ModelStateTarget(ModelStateField.ENABLED, True),
        expected_revision=page.revision,
    )
    assert enabled.items[0].status == "updated"
    account = next(row for row in config.get()["oauthAccounts"] if row.get("email") == "lkg@example.test")
    assert "oauth-disabled" not in account["disabledModels"]
    refreshed = next(item for item in control.list_models().items if item.model_id == "oauth-disabled")
    assert refreshed.sources[0].source_enabled is True


def test_selection_validation_rejects_media_cross_scope_and_missing_revision_without_write():
    _install_catalog()
    control = ModelCenterControl()
    page = control.list_models()
    media = next(item for item in page.items if item.identity.kind is ModelKind.VIDEO)
    config.get()["xaiOAuth"]["videoModels"] = ["video-only"]
    page = control.list_models()
    media = next(item for item in page.items if item.model_id == "video-only")
    before = copy.deepcopy(config.get())
    with pytest.raises(ManagementError) as missing_revision:
        control.set_state(
            None, scope=None,
            selection=ModelSelection(ModelSelectionMode.IDS, ("shared",)),
            target=ModelStateTarget(ModelStateField.ENABLED, False),
            expected_revision=None,
        )
    assert missing_revision.value.code is ManagementErrorCode.CONFIRMATION_REQUIRED
    with pytest.raises(ManagementError) as media_error:
        control.set_state(
            None, scope=None,
            selection=ModelSelection(ModelSelectionMode.IDS, (media.model_id,)),
            target=ModelStateTarget(ModelStateField.ENABLED, False),
            expected_revision=page.revision,
        )
    assert media_error.value.code is ManagementErrorCode.UNSUPPORTED_VALUE
    with pytest.raises(ManagementError) as cross_scope:
        control.set_state(
            None, scope=ModelSourceRef(ModelSourceType.API, "api:alpha"),
            selection=ModelSelection(ModelSelectionMode.IDS, ("oauth-live",)),
            target=ModelStateTarget(ModelStateField.ENABLED, False),
            expected_revision=page.revision,
        )
    assert cross_scope.value.code is ManagementErrorCode.VALIDATION_FAILED
    assert config.get() == before
