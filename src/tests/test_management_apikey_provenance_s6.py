from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.management_auth import (
    AuthMethod,
    Capability,
    ManagementPrincipal,
    ManagementStateStore,
)
from src.management_control import ManagementContext
from src.management_control.apikey import ApiKeyControl, ApiKeyProvenance, ApiKeySource
from src.tests.test_management_apikey_api import build_app, session_headers
from src.tests.test_management_apikey_control import (
    FakeModels,
    FakeStats,
    Sequence,
    make_control,
)


def telegram_context() -> ManagementContext:
    return ManagementContext(
        request_id="telegram-apikey:42",
        actor=ManagementPrincipal.administrator(
            subject_id="telegram:42",
            auth_method=AuthMethod.TELEGRAM_ADMIN,
            issued_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        ),
    )


def read_context() -> ManagementContext:
    return ManagementContext(
        request_id="management-read",
        actor=ManagementPrincipal.with_capabilities(
            subject_id="administrator",
            auth_method=AuthMethod.MANAGEMENT_KEY,
            capabilities=(Capability.READ,),
            issued_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        ),
    )


def entry(secret: str) -> dict:
    return {
        "key": secret,
        "enabled": True,
        "allowedModels": [],
        "allowImages": False,
        "allowVideos": False,
        # MCP access is off unless explicitly granted, so every created key
        # carries these two fields at their default values.
        "allowMcp": False,
        "mcpTools": [],
    }


def filtered_ids(client: TestClient, headers: dict[str, str], source: str) -> set[str]:
    response = client.get(
        f"/api/management/v1/api-keys?source={source}&pageSize=200",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return {item["keyId"] for item in response.json()["data"]["items"]}


def test_tg_sources_survive_real_store_control_rebuild_and_api_filters(tmp_path):
    initial = {
        "apiKeys": {
            "legacy-generated": entry("legacy-auto-secret"),
            "legacy-custom": entry("legacy-custom-secret"),
            "legacy-unknown": entry("ccp-prefix-is-not-proof"),
        },
        "xaiOAuth": {"imageModels": [], "videoModels": []},
    }
    generated_secrets = ["ccp-" + "g" * 48, "ccp-" + "r" * 48]
    app, runtime, control, config_store, limiter = build_app(
        tmp_path,
        initial_config=initial,
        generated=Sequence(generated_secrets),
    )
    client = TestClient(app)
    headers = session_headers(runtime)
    tg = telegram_context()
    read = read_context()
    try:
        assert control.get_api_key(read, "legacy-generated").source is ApiKeyProvenance.UNKNOWN
        assert control.get_api_key(read, "legacy-custom").source is ApiKeyProvenance.UNKNOWN

        generated = control.create_api_key(
            tg, name="tg-generated", mode=ApiKeySource.GENERATED,
        )
        custom = control.create_api_key(
            tg,
            name="tg-custom",
            mode=ApiKeySource.CUSTOM,
            custom_secret="tg-custom-secret",
        )
        regenerated = control.regenerate_api_key(
            tg,
            "legacy-generated",
            require_plan=False,
            reset_runtime=False,
        )
        replaced = control.replace_api_key_secret(
            tg,
            "legacy-custom",
            custom_secret="first-custom-replacement",
            if_match=None,
            require_revision=False,
            reset_runtime=False,
        )

        assert generated.api_key.source is ApiKeyProvenance.GENERATED
        assert custom.api_key.source is ApiKeyProvenance.CUSTOM
        assert regenerated.api_key.source is ApiKeyProvenance.GENERATED
        assert replaced.api_key.source is ApiKeyProvenance.CUSTOM
        assert control.get_api_key(read, "legacy-unknown").source is ApiKeyProvenance.UNKNOWN

        assert config_store.value["apiKeys"]["tg-generated"] == entry(generated_secrets[0])
        assert config_store.value["apiKeys"]["tg-custom"] == entry("tg-custom-secret")
        assert config_store.value["apiKeys"]["legacy-generated"] == entry(generated_secrets[1])
        assert config_store.value["apiKeys"]["legacy-custom"] == entry("first-custom-replacement")
        assert all(
            "source" not in current
            for current in config_store.value["apiKeys"].values()
        )

        assert filtered_ids(client, headers, "generated") == {
            "legacy-generated", "tg-generated",
        }
        assert filtered_ids(client, headers, "custom") == {
            "legacy-custom", "tg-custom",
        }
        assert filtered_ids(client, headers, "unknown") == {"legacy-unknown"}

        rebuilt = ApiKeyControl(
            config_store=config_store,
            provenance_store=runtime.state_store,
            limiter=limiter,
            statistics=FakeStats(),
            model_registry=FakeModels(),
        )
        app.state.management_apikey_control = rebuilt
        assert rebuilt.get_api_key(read, "tg-generated").source is ApiKeyProvenance.GENERATED
        assert rebuilt.get_api_key(read, "tg-custom").source is ApiKeyProvenance.CUSTOM
        assert filtered_ids(client, headers, "generated") == {
            "legacy-generated", "tg-generated",
        }
        assert filtered_ids(client, headers, "custom") == {
            "legacy-custom", "tg-custom",
        }

        with sqlite3.connect(runtime.state_store.path) as connection:
            rows = connection.execute(
                "SELECT key,value FROM metadata WHERE key LIKE 'apiKeyProvenance:%'"
            ).fetchall()
        serialized = repr(rows)
        assert len(rows) == 4
        for plaintext in (
            *generated_secrets,
            "tg-custom-secret",
            "first-custom-replacement",
            "tg-generated",
            "tg-custom",
            "legacy-generated",
            "legacy-custom",
        ):
            assert plaintext not in serialized
    finally:
        runtime.close()


def test_fingerprints_and_failed_writes_never_misattribute_a_secret(tmp_path, monkeypatch):
    state_store = ManagementStateStore(
        str(tmp_path / "management-apikey-provenance.db"),
        clock=lambda: 1_788_557_400.0,
    )
    control, config_store, limiter, _ = make_control(
        {
            "apiKeys": {},
            "xaiOAuth": {"imageModels": [], "videoModels": []},
        },
        generated=Sequence([
            "ccp-" + "a" * 48,
            "ccp-" + "b" * 48,
        ]),
        provenance_store=state_store,
    )
    tg = telegram_context()
    read = read_context()
    try:
        control.create_api_key(
            tg,
            name="external-change",
            mode=ApiKeySource.CUSTOM,
            custom_secret="original-custom-secret",
        )
        assert control.get_api_key(read, "external-change").source is ApiKeyProvenance.CUSTOM
        config_store.value["apiKeys"]["external-change"]["key"] = "different-external-secret"
        assert control.get_api_key(read, "external-change").source is ApiKeyProvenance.UNKNOWN
        assert control.list_api_keys(
            read,
            source=ApiKeyProvenance.CUSTOM,
            include_stats=False,
        ).items == ()

        control.create_api_key(
            tg,
            name="config-write-failure",
            mode=ApiKeySource.CUSTOM,
            custom_secret="unchanged-custom-secret",
        )
        before = deepcopy(config_store.value["apiKeys"]["config-write-failure"])

        def failed_config_update(mutator):
            candidate = deepcopy(config_store.value)
            mutator(candidate)
            raise OSError("simulated config publish failure")

        with monkeypatch.context() as scoped:
            scoped.setattr(config_store, "update", failed_config_update)
            with pytest.raises(OSError, match="config publish failure"):
                control.regenerate_api_key(
                    tg,
                    "config-write-failure",
                    require_plan=False,
                    reset_runtime=False,
                )
        assert config_store.value["apiKeys"]["config-write-failure"] == before
        assert control.get_api_key(
            read, "config-write-failure",
        ).source is ApiKeyProvenance.CUSTOM
        control.create_api_key(
            tg,
            name="commit-write-failure",
            mode=ApiKeySource.CUSTOM,
            custom_secret="commit-old-custom-secret",
        )
        with monkeypatch.context() as scoped:
            scoped.setattr(
                state_store,
                "set_api_key_provenance",
                lambda **_kwargs: (_ for _ in ()).throw(OSError("commit failed")),
            )
            changed = control.regenerate_api_key(
                tg,
                "commit-write-failure",
                require_plan=False,
                reset_runtime=False,
            )
        assert changed.secret == "ccp-" + "b" * 48
        assert changed.api_key.source is ApiKeyProvenance.UNKNOWN
        rebuilt = ApiKeyControl(
            config_store=config_store,
            provenance_store=state_store,
            limiter=limiter,
            statistics=FakeStats(),
            model_registry=FakeModels(),
        )
        assert rebuilt.get_api_key(
            read, "commit-write-failure",
        ).source is ApiKeyProvenance.UNKNOWN
    finally:
        state_store.close()


def test_auxiliary_source_store_failure_never_blocks_tg_key_writes(tmp_path, monkeypatch):
    store = ManagementStateStore(str(tmp_path / "unavailable-source.db"), clock=lambda: 1_788_557_400.0)
    control, config_store, _, _ = make_control(
        {"apiKeys": {}, "xaiOAuth": {"imageModels": [], "videoModels": []}},
        provenance_store=store,
    )

    def unavailable(**kwargs):
        raise OSError("auxiliary store unavailable")

    monkeypatch.setattr(store, "set_api_key_provenance", unavailable)
    monkeypatch.setattr(store, "forget_api_key_provenance", unavailable)
    try:
        created = control.create_api_key(
            telegram_context(), name="still-usable", mode=ApiKeySource.CUSTOM,
            custom_secret="first-custom-secret",
        )
        assert created.api_key.source is ApiKeyProvenance.UNKNOWN
        assert config_store.value["apiKeys"]["still-usable"] == entry("first-custom-secret")
        changed = control.replace_api_key_secret(
            telegram_context(), "still-usable", custom_secret="second-custom-secret",
            if_match=None, require_revision=False, reset_runtime=False,
        )
        assert changed.api_key.source is ApiKeyProvenance.UNKNOWN
        assert config_store.value["apiKeys"]["still-usable"] == entry("second-custom-secret")
        control.delete_api_key(
            telegram_context(), "still-usable", if_match=None, require_confirmation=False,
        )
        assert "still-usable" not in config_store.value["apiKeys"]
    finally:
        store.close()
