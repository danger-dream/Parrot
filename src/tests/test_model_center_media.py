from __future__ import annotations

import base64
import copy
import json
import os
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src import media_cache, media_db
from src.antigravity import images as ag_images
from src.management_auth import AuthMethod, Capability, ManagementPrincipal
from src.management_control import ManagementContext, ManagementError, ManagementErrorCode
from src.management_control.auxiliary.media import AntigravityMediaControl, XaiMediaControl
from src.management_control.models import ModelKind, ModelOwnerRef, ModelSourceType
from src.management_control.observability import MediaControl
from src.tests.management_auxiliary_support import bearer, build_auxiliary_app, create_session


BASE = "/api/management/v1"


class FakeConfig:
    def __init__(self, value):
        self.value = copy.deepcopy(value)
        self.updates = 0

    def get(self):
        return self.value

    def update(self, mutator):
        candidate = copy.deepcopy(self.value)
        mutator(candidate)
        self.value = candidate
        self.updates += 1
        return self.value


class FakeMediaGateway:
    xai_defaults = {
        "imageModels": ["img-a", "img-b"],
        "videoModels": ["vid-a", "vid-b"],
        "videoJobTtlSeconds": 10800,
        "mediaRequestTimeoutSeconds": 180,
    }


def admin_context() -> ManagementContext:
    return ManagementContext(
        request_id="model-center-media",
        actor=ManagementPrincipal.administrator(
            subject_id="administrator", auth_method=AuthMethod.MANAGEMENT_KEY,
        ),
    )


def test_media_controls_exact_order_revision_conflict_and_owner_isolation():
    cfg = FakeConfig({
        "xaiOAuth": {
            "imageModels": ["img-a", "img-b"],
            "videoModels": ["vid-a", "vid-b"],
            "videoJobTtlSeconds": 10800,
            "mediaRequestTimeoutSeconds": 180,
        },
        "antigravityOAuth": {"imageModels": ["global-a", "shared"]},
        "oauthAccounts": [{
            "provider": "antigravity", "email": "owner@example.test",
            "project_id": "project", "imageModels": ["shared", "owned-only"],
        }],
    })
    context = admin_context()
    xai = XaiMediaControl(config_gateway=cfg, media_gateway=FakeMediaGateway())
    initial = xai.get_settings(context)

    renamed = xai.rename_model(
        context, kind=ModelKind.IMAGE, old_model_id="img-a", new_model_id="img-c",
        expected_revision=initial.revision,
    )
    assert renamed.status == "renamed"
    assert renamed.models == ("img-c", "img-b")
    assert cfg.value["xaiOAuth"]["videoModels"] == ["vid-a", "vid-b"]
    account_before = copy.deepcopy(cfg.value["oauthAccounts"])

    with pytest.raises(ManagementError) as stale:
        xai.remove_model(
            context, kind="image", model_id="img-b",
            expected_revision=initial.revision,
        )
    assert stale.value.code is ManagementErrorCode.REVISION_CONFLICT
    assert cfg.value["xaiOAuth"]["imageModels"] == ["img-c", "img-b"]

    with pytest.raises(ManagementError) as collision:
        xai.rename_model(
            context, kind="image", old_model_id="img-c", new_model_id="img-b",
            expected_revision=renamed.revision,
        )
    assert collision.value.code is ManagementErrorCode.RESOURCE_CONFLICT
    assert cfg.value["xaiOAuth"]["imageModels"] == ["img-c", "img-b"]

    removed = xai.remove_model(
        context, kind=ModelKind.IMAGE, model_id="img-c",
        expected_revision=renamed.revision,
    )
    assert removed.models == ("img-b",)
    assert cfg.value["oauthAccounts"] == account_before

    ag = AntigravityMediaControl(config_gateway=cfg)
    settings = ag.get_settings(context)
    assert settings.image_models == ("global-a", "shared")
    assert settings.account_overrides[0][1] == ("shared", "owned-only")
    global_result = ag.add_model(
        context, owner=ModelOwnerRef(ModelSourceType.GLOBAL),
        model_id="owned-only", expected_revision=settings.revision,
    )
    assert global_result.models == ("global-a", "shared", "owned-only")
    assert cfg.value["oauthAccounts"] == account_before

    snapshot = copy.deepcopy(cfg.value)
    with pytest.raises(ManagementError) as read_only:
        ag.rename_model(
            context,
            owner=ModelOwnerRef(ModelSourceType.OAUTH, "antigravity:owner@example.test:project"),
            old_model_id="owned-only", new_model_id="owned-renamed",
            expected_revision=global_result.revision,
        )
    assert read_only.value.code is ManagementErrorCode.UNSUPPORTED_VALUE
    assert read_only.value.fields[0].code == "READ_ONLY_SCOPE"
    assert cfg.value == snapshot

    with pytest.raises(ManagementError) as missing_revision:
        ag.remove_model(
            context, owner=ModelOwnerRef(ModelSourceType.GLOBAL),
            model_id="global-a", expected_revision=None,
        )
    assert missing_revision.value.code is ManagementErrorCode.CONFIRMATION_REQUIRED
    assert cfg.value == snapshot


def test_media_item_management_api_roundtrip_errors_and_bulk_compatibility(tmp_path):
    app, runtime, fixture = build_auxiliary_app(tmp_path)
    fixture.config.value["antigravityOAuth"] = {"imageModels": ["ag-a"]}
    fixture.config.value["oauthAccounts"] = [{
        "provider": "antigravity", "email": "owner@example.test",
        "project_id": "project", "imageModels": ["ag-owned"],
    }]
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        xai = client.get(BASE + "/xai/media-settings", headers=headers)
        assert xai.status_code == 200
        revision = xai.json()["data"]["revision"]
        assert client.post(
            BASE + "/xai/media-models/image",
            json={"modelId": "denied"},
        ).status_code == 401
        restricted = runtime.sessions.issue_for_principal(
            subject_id="read-only", auth_method=AuthMethod.MANAGEMENT_KEY,
            roles=(), capabilities=(Capability.READ,),
        )
        denied = client.post(
            BASE + "/xai/media-models/image",
            json={"modelId": "denied"},
            headers={**bearer(restricted.credential), "If-Match": revision},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "CAPABILITY_DENIED"
        added = client.post(
            BASE + "/xai/media-models/image",
            json={"modelId": "grok-new"},
            headers={**headers, "If-Match": revision},
        )
        assert added.status_code == 200, added.text
        assert added.json()["data"]["status"] == "added"
        assert added.json()["data"]["owner"] == {"type": "global", "id": None}
        next_revision = added.json()["data"]["revision"]

        renamed = client.patch(
            BASE + "/xai/media-models/image/" + quote("grok-new", safe=""),
            json={"newModelId": "grok-renamed"},
            headers={**headers, "If-Match": next_revision},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["data"]["models"][-1] == "grok-renamed"
        stale = client.delete(
            BASE + "/xai/media-models/image/grok-renamed",
            headers={**headers, "If-Match": next_revision},
        )
        assert stale.status_code == 409
        missing_if_match = client.post(
            BASE + "/xai/media-models/video", json={"modelId": "video-new"},
            headers=headers,
        )
        assert missing_if_match.status_code == 400
        assert missing_if_match.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
        invalid_kind = client.post(
            BASE + "/xai/media-models/chat", json={"modelId": "bad"},
            headers={**headers, "If-Match": renamed.json()["data"]["revision"]},
        )
        assert invalid_kind.status_code == 422

        ag = client.get(BASE + "/antigravity/media-settings", headers=headers)
        assert ag.status_code == 200, ag.text
        ag_data = ag.json()["data"]
        assert ag_data["imageModels"] == ["ag-a"]
        assert ag_data["accountOverrides"] == [{
            "accountId": "antigravity:owner@example.test:project",
            "imageModels": ["ag-owned"], "editable": False,
        }]
        account_snapshot = copy.deepcopy(fixture.config.value["oauthAccounts"])
        ag_added = client.post(
            BASE + "/antigravity/media-models/image",
            json={"modelId": "ag-owned", "owner": {"type": "global"}},
            headers={**headers, "If-Match": ag_data["revision"]},
        )
        assert ag_added.status_code == 200, ag_added.text
        assert ag_added.json()["data"]["models"] == ["ag-a", "ag-owned"]
        assert fixture.config.value["oauthAccounts"] == account_snapshot

        account_write = client.patch(
            BASE + "/antigravity/media-models/image/ag-owned",
            json={
                "newModelId": "should-not-write",
                "owner": {
                    "type": "oauth",
                    "id": "antigravity:owner@example.test:project",
                },
            },
            headers={**headers, "If-Match": ag_added.json()["data"]["revision"]},
        )
        assert account_write.status_code == 422, account_write.text
        assert account_write.json()["error"]["fields"][0]["code"] == "READ_ONLY_SCOPE"
        assert fixture.config.value["oauthAccounts"] == account_snapshot

        bulk = client.patch(
            BASE + "/antigravity/media-settings",
            json={"imageModels": []},
            headers={**headers, "If-Match": ag_added.json()["data"]["revision"]},
        )
        assert bulk.status_code == 200, bulk.text
        assert bulk.json()["data"]["imageModels"] == []
        assert fixture.config.value["oauthAccounts"] == account_snapshot


def test_antigravity_model_limits_are_80_by_80_and_rejections_do_not_write():
    names_79 = [f"ag-{index}" for index in range(79)]
    name_80_chars = "m" * 80
    cfg = FakeConfig({"antigravityOAuth": {"imageModels": names_79}})
    context = admin_context()
    control = AntigravityMediaControl(config_gateway=cfg)

    initial = control.get_settings(context)
    assert len(initial.image_models) == 79
    accepted_79 = control.update_settings(
        context, image_models=names_79, expected_revision=initial.revision,
    )
    assert len(accepted_79.image_models) == 79
    accepted_80 = control.update_settings(
        context,
        image_models=[*names_79, name_80_chars],
        expected_revision=accepted_79.revision,
    )
    assert len(accepted_80.image_models) == 80
    assert accepted_80.image_models[-1] == name_80_chars

    snapshot = copy.deepcopy(cfg.value)
    writes = cfg.updates
    with pytest.raises(ManagementError):
        control.update_settings(
            context,
            image_models=[f"overflow-{index}" for index in range(81)],
            expected_revision=accepted_80.revision,
        )
    assert cfg.value == snapshot
    assert cfg.updates == writes

    with pytest.raises(ManagementError):
        control.update_settings(
            context,
            image_models=["n" * 81],
            expected_revision=accepted_80.revision,
        )
    assert cfg.value == snapshot
    assert cfg.updates == writes

    with pytest.raises(ManagementError):
        control.add_model(
            context, owner=ModelOwnerRef(ModelSourceType.GLOBAL),
            model_id="overflow", expected_revision=accepted_80.revision,
        )
    assert cfg.value == snapshot
    assert cfg.updates == writes

    with pytest.raises(ManagementError):
        control.rename_model(
            context, owner=ModelOwnerRef(ModelSourceType.GLOBAL),
            old_model_id=name_80_chars, new_model_id="n" * 81,
            expected_revision=accepted_80.revision,
        )
    assert cfg.value == snapshot
    assert cfg.updates == writes


def test_antigravity_http_limits_explicit_clear_and_owner_query_validation(tmp_path):
    app, _runtime, fixture = build_auxiliary_app(tmp_path)
    fixture.config.value["antigravityOAuth"] = {"imageModels": ["seed"]}
    names_79 = [f"ag-{index}" for index in range(79)]
    name_80_chars = "m" * 80

    with TestClient(app) as client:
        headers = bearer(create_session(client))
        initial = client.get(BASE + "/antigravity/media-settings", headers=headers).json()["data"]
        revision = initial["revision"]
        snapshot = copy.deepcopy(fixture.config.value)

        missing = client.patch(
            BASE + "/antigravity/media-settings", json={},
            headers={**headers, "If-Match": revision},
        )
        assert missing.status_code == 422, missing.text
        null = client.patch(
            BASE + "/antigravity/media-settings", json={"imageModels": None},
            headers={**headers, "If-Match": revision},
        )
        assert null.status_code == 422, null.text
        assert fixture.config.value == snapshot

        accepted_79 = client.patch(
            BASE + "/antigravity/media-settings", json={"imageModels": names_79},
            headers={**headers, "If-Match": revision},
        )
        assert accepted_79.status_code == 200, accepted_79.text
        assert len(accepted_79.json()["data"]["imageModels"]) == 79
        accepted_80 = client.patch(
            BASE + "/antigravity/media-settings",
            json={"imageModels": [*names_79, name_80_chars]},
            headers={**headers, "If-Match": accepted_79.json()["data"]["revision"]},
        )
        assert accepted_80.status_code == 200, accepted_80.text
        assert len(accepted_80.json()["data"]["imageModels"]) == 80
        revision_80 = accepted_80.json()["data"]["revision"]
        snapshot = copy.deepcopy(fixture.config.value)

        count_81 = client.patch(
            BASE + "/antigravity/media-settings",
            json={"imageModels": [f"overflow-{index}" for index in range(81)]},
            headers={**headers, "If-Match": revision_80},
        )
        assert count_81.status_code == 422, count_81.text
        name_81 = client.post(
            BASE + "/antigravity/media-models/image",
            json={"modelId": "n" * 81, "owner": {"type": "global"}},
            headers={**headers, "If-Match": revision_80},
        )
        assert name_81.status_code == 422, name_81.text
        path_81 = client.delete(
            BASE + "/antigravity/media-models/image/" + ("p" * 81),
            params={"ownerType": "global"},
            headers={**headers, "If-Match": revision_80},
        )
        assert path_81.status_code == 422, path_81.text
        global_with_id = client.delete(
            BASE + "/antigravity/media-models/image/ag-0",
            params={"ownerType": "global", "ownerId": "unexpected"},
            headers={**headers, "If-Match": revision_80},
        )
        assert global_with_id.status_code == 422, global_with_id.text
        oauth_without_id = client.delete(
            BASE + "/antigravity/media-models/image/ag-0",
            params={"ownerType": "oauth"},
            headers={**headers, "If-Match": revision_80},
        )
        assert oauth_without_id.status_code == 422, oauth_without_id.text
        assert fixture.config.value == snapshot

        cleared = client.patch(
            BASE + "/antigravity/media-settings", json={"imageModels": []},
            headers={**headers, "If-Match": revision_80},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["data"]["imageModels"] == []


def test_shared_cache_atomic_suffix_cleanup_and_symlink_boundary(tmp_path):
    root = tmp_path / "cache"
    cfg = {
        "cacheEnabled": True,
        "cachePath": str(root),
        "cacheRetentionDays": 1,
        "cacheMaxBytes": 1024,
    }
    raw = b"\x89PNG\r\n\x1a\nmedia"
    result = media_cache.cache_inline_base64(
        [{"base64": base64.b64encode(raw).decode(), "mime": "image/png"}],
        cfg=cfg, provider="antigravity", media_type="image", action="generate",
    )
    assert result.status == "cached" and result.total_bytes == len(raw)
    assert len(result.paths) == 1
    cached = os.path.realpath(result.paths[0])
    assert os.path.commonpath([cached, str(root.resolve())]) == str(root.resolve())
    assert os.path.basename(cached).startswith("antigravity-image-generate-")
    assert "owner" not in cached and "prompt" not in cached
    assert not list(root.rglob("*.tmp"))

    old = root / "20000101" / "antigravity-image-generate-old.png"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old")
    os.utime(old, (1, 1))
    keep_text = root / "20000101" / "not-media.txt"
    keep_text.write_text("keep")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    link = root / "20000101" / "linked.png"
    try:
        link.symlink_to(outside)
    except OSError:
        link = None

    media_cache.cleanup(root, cfg)
    assert not old.exists()
    assert keep_text.exists() and outside.exists()
    if link is not None:
        assert link.is_symlink() and outside.read_bytes() == b"outside"
        assert not media_cache.artifact_path_is_safe(link, cfg)
    assert not media_cache.artifact_path_is_safe(outside, cfg)

    limited_root = tmp_path / "limited"
    limited_cfg = {
        **cfg, "cachePath": str(limited_root),
        "cacheRetentionDays": 0, "cacheMaxBytes": 4,
    }
    limited_root.mkdir()
    limited_files = []
    for index in range(3):
        path = limited_root / f"antigravity-{index}.png"
        path.write_bytes(b"xx")
        os.utime(path, (10 + index, 10 + index))
        limited_files.append(path)
    media_cache.cleanup(limited_root, limited_cfg)
    remaining = [path for path in limited_files if path.exists()]
    assert sum(path.stat().st_size for path in remaining) <= 4
    assert not limited_files[0].exists()


def test_media_db_cache_status_and_management_artifact_root_containment(tmp_path):
    media_db.init()
    log_id = media_db.start_call(
        request_id="ag-cache-status-row", api_key_name="key",
        provider="antigravity", media_type="image", action="generate",
        model="gemini-image", prompt="safe prompt",
    )
    media_db.finish_call(
        log_id, status="success", image_count=1,
        cached_media_count=0, media_bytes=10, cache_paths=[],
        cache_status="failed", cache_error_class="OSError", http_status=200,
    )
    stored = media_db.get_log(log_id)
    assert stored["status"] == "success"
    assert stored["cache_status"] == "failed"
    assert stored["cache_error_class"] == "OSError"

    root = tmp_path / "cache"
    root.mkdir()
    inside = root / "inside.png"
    inside.write_bytes(b"inside")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    row = {
        **stored,
        "cache_paths": json.dumps([str(inside), str(outside)]),
    }

    class Db:
        def get_log(self, identifier):
            return row if identifier == log_id else None

    class Config:
        def get(self):
            return {"images": {"cachePath": str(root)}}

    control = MediaControl(media_db=Db(), config=Config())
    detail = control.detail(admin_context(), str(log_id))
    assert detail["cacheStatus"] == "failed"
    assert detail["cacheErrorClass"] == "OSError"
    assert detail["paths"] == ["inside.png"]
    artifacts = control.artifacts(admin_context(), str(log_id))
    assert len(artifacts) == 1 and artifacts[0]["fileName"] == "inside.png"
    assert b"".join(
        control.download(admin_context(), str(log_id), artifacts[0]["id"]).chunks
    ) == b"inside"


class FakeUpstreamResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aiter_bytes(self):
        yield json.dumps(self.body).encode()


class FakeNetworkClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def stream(self, *args, **kwargs):
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_format,cache_enabled",
    [("b64_json", True), ("url", True), ("b64_json", False), ("url", False)],
)
async def test_antigravity_generate_cache_output_and_media_log(
    monkeypatch, tmp_path, response_format, cache_enabled,
):
    model = "gemini-image"
    channel = SimpleNamespace(
        key="oauth:ag", account_key="antigravity:owner@example.test:project",
        email="owner@example.test", project_id="project", base_url="https://example.invalid",
        _build_headers=lambda token, stream=False: {"Authorization": "Bearer fake"},
    )
    monkeypatch.setattr(ag_images, "_eligible", lambda _: [channel])
    monkeypatch.setattr(ag_images.cooldown, "clear_on_success", lambda *args: None)
    async def acquire(_):
        return True
    monkeypatch.setattr(ag_images.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(ag_images.concurrency, "release", lambda _: None)
    import src.oauth_manager as oauth_manager
    async def token(_):
        return "fake-token"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    encoded = base64.b64encode(b"generated-image").decode()
    body = {"response": {"candidates": [{"content": {"parts": [{
        "inlineData": {"mimeType": "image/webp", "data": encoded},
    }]}}]}}
    monkeypatch.setattr(
        ag_images.network, "async_client",
        lambda **kwargs: FakeNetworkClient(FakeUpstreamResponse(body)),
    )
    cache_root = tmp_path / "shared-cache"
    monkeypatch.setattr(ag_images, "_media_cache_settings", lambda: {
        "cacheEnabled": cache_enabled, "cachePath": str(cache_root),
        "cacheRetentionDays": 0, "cacheMaxBytes": 1024,
    })
    starts, finishes = [], []
    async def start(**fields):
        starts.append(fields)
        return 7
    async def finish(log_id, **fields):
        finishes.append((log_id, fields))
    monkeypatch.setattr(ag_images, "_start_log", start)
    monkeypatch.setattr(ag_images, "_finish_log", finish)

    parsed = SimpleNamespace(
        model=model, prompt="private prompt", requested_n=1, size="1024x1024",
        response_format=response_format, native_options={},
    )
    response = await ag_images.handle_image(
        parsed, action="generate", key_name="key-a", allowed_models=[],
    )
    assert response.status_code == 200
    payload = json.loads(response.body)
    if response_format == "b64_json":
        assert payload["data"] == [{"b64_json": encoded}]
    else:
        assert payload["data"] == [{"url": "data:image/webp;base64," + encoded}]
    assert "cache" not in payload and str(cache_root) not in response.body.decode()
    assert starts[0]["provider"] == "antigravity"
    assert starts[0]["prompt"] == "private prompt"
    logged = finishes[-1][1]
    assert logged["status"] == "success"
    assert logged["cache_status"] == ("cached" if cache_enabled else "disabled")
    assert logged["cached_media_count"] == (1 if cache_enabled else 0)
    assert logged["media_bytes"] == len(b"generated-image")
    assert len(logged["cache_paths"]) == (1 if cache_enabled else 0)
    if cache_enabled:
        assert logged["cache_paths"][0].endswith(".webp")
    else:
        assert not cache_root.exists()


@pytest.mark.asyncio
async def test_antigravity_cache_failure_does_not_change_success(monkeypatch, tmp_path):
    model = "gemini-image"
    channel = SimpleNamespace(
        key="oauth:ag", account_key="antigravity:owner@example.test:project",
        email="owner@example.test", project_id="project", base_url="https://example.invalid",
        _build_headers=lambda token, stream=False: {},
    )
    monkeypatch.setattr(ag_images, "_eligible", lambda _: [channel])
    monkeypatch.setattr(ag_images.cooldown, "clear_on_success", lambda *args: None)
    async def acquire(_):
        return True
    monkeypatch.setattr(ag_images.concurrency, "try_acquire", acquire)
    monkeypatch.setattr(ag_images.concurrency, "release", lambda _: None)
    import src.oauth_manager as oauth_manager
    async def token(_):
        return "fake-token"
    monkeypatch.setattr(oauth_manager, "ensure_valid_token", token)
    encoded = base64.b64encode(b"generated-image").decode()
    body = {"response": {"candidates": [{"content": {"parts": [{
        "inlineData": {"mimeType": "image/png", "data": encoded},
    }]}}]}}
    monkeypatch.setattr(
        ag_images.network, "async_client",
        lambda **kwargs: FakeNetworkClient(FakeUpstreamResponse(body)),
    )
    monkeypatch.setattr(ag_images, "_media_cache_settings", lambda: {
        "cacheEnabled": True, "cachePath": str(tmp_path / "cache"),
        "cacheRetentionDays": 0, "cacheMaxBytes": 1024,
    })
    def fail_write(*args, **kwargs):
        raise OSError("simulated cache failure")
    monkeypatch.setattr(media_cache, "write_bytes", fail_write)
    finishes = []
    async def start(**fields):
        return 8
    async def finish(log_id, **fields):
        finishes.append(fields)
    monkeypatch.setattr(ag_images, "_start_log", start)
    monkeypatch.setattr(ag_images, "_finish_log", finish)

    response = await ag_images.handle_image(
        SimpleNamespace(
            model=model, prompt="prompt", requested_n=1, size=None,
            response_format="b64_json", native_options={},
        ),
        action="generate", key_name="key", allowed_models=[],
    )
    assert response.status_code == 200
    assert json.loads(response.body)["data"] == [{"b64_json": encoded}]
    assert finishes[-1]["status"] == "success"
    assert finishes[-1]["cache_status"] == "failed"
    assert finishes[-1]["cache_error_class"] == "OSError"
    assert finishes[-1]["cached_media_count"] == 0
