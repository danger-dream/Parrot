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
    assert cfg.value["image_models"]["xai"] == ["img-c", "img-b"]

    with pytest.raises(ManagementError) as collision:
        xai.rename_model(
            context, kind="image", old_model_id="img-c", new_model_id="img-b",
            expected_revision=renamed.revision,
        )
    assert collision.value.code is ManagementErrorCode.RESOURCE_CONFLICT
    assert cfg.value["image_models"]["xai"] == ["img-c", "img-b"]

    removed = xai.remove_model(
        context, kind=ModelKind.IMAGE, model_id="img-c",
        expected_revision=renamed.revision,
    )
    assert removed.models == ("img-b",)
    assert cfg.value["oauthAccounts"] == account_before

    ag = AntigravityMediaControl(config_gateway=cfg)
    settings = ag.get_settings(context)
    assert settings.image_models == () and settings.account_overrides == ()
    snapshot = copy.deepcopy(cfg.value)
    with pytest.raises(ManagementError) as retired:
        ag.add_model(context, owner=ModelOwnerRef(ModelSourceType.GLOBAL), model_id="no", expected_revision=settings.revision)
    assert retired.value.code is ManagementErrorCode.UNSUPPORTED_VALUE
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

        snapshot = copy.deepcopy(fixture.config.value)
        for method, path, body in (
            ("GET", "/antigravity/media-settings", None),
            ("PATCH", "/antigravity/media-settings", {"imageModels": []}),
            ("POST", "/antigravity/media-models/image", {"modelId": "new"}),
        ):
            response = client.request(method, BASE + path, headers=headers, json=body)
            assert response.status_code == 404
        assert fixture.config.value == snapshot






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
async def test_retired_ag_adapter_has_no_generation_capability():
    assert not ag_images.is_antigravity_image_model("gemini-3.1-flash-image")
    assert (await ag_images.handle_image(None)).status_code == 410
