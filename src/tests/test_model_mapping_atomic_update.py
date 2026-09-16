from __future__ import annotations

import copy

import pytest

from src import config, model_mapping
from src.management_control.errors import ManagementError, ManagementErrorCode
from src.management_control.mapping import MappingControl
from src.tests.test_management_mapping_support import domain_client


@pytest.fixture(autouse=True)
def isolated_mapping_config():
    def mutate(cfg):
        cfg.clear()
        cfg.update(copy.deepcopy(config.DEFAULT_CONFIG))
        cfg["modelMapping"] = {
            "global": {"old": "target-old", "other": "target-other"},
            "anthropic": {"old": "legacy-old", "legacy-only": "legacy-target"},
            "openai-chat": {"old": "legacy-chat"},
            "openai-responses": {},
        }
        cfg["apiKeys"] = {"client": {"key": "secret", "allowedModels": ["old"]}}
        cfg["ingressDefaultModel"] = {"global": "old"}
        cfg["compressionModel"] = "old"
        cfg["loadBalancing"] = {"mode": "order", "priorityOrders": {"openai": ["old"]}}
        cfg["modelBindings"] = {
            "defaults": {"old": {"target": "catalog-old"}},
            "scoped": {"api:one": {"old": {"target": "catalog-scoped"}}},
        }
    config.update(mutate)


def _unrelated_snapshot() -> dict:
    cfg = config.get()
    return copy.deepcopy({
        key: cfg.get(key) for key in (
            "apiKeys", "ingressDefaultModel", "compressionModel",
            "loadBalancing", "modelBindings",
        )
    })


def test_atomic_rename_and_retarget_single_commit_without_cross_domain_rewrites(monkeypatch):
    control = MappingControl()
    revision = control.list_mappings(None, query=None, sort="alias", page=1, page_size=50).revision
    unrelated = _unrelated_snapshot()
    original_update = config.update
    commits = []

    def counted(mutator, *args, **kwargs):
        commits.append(mutator)
        return original_update(mutator, *args, **kwargs)

    monkeypatch.setattr(config, "update", counted)
    result = control.update_mapping(
        control.current_context(), "old", new_alias="new", real_model="target-new",
        expected_revision=revision,
    )
    assert len(commits) == 1
    assert result.alias == "new" and result.real_model == "target-new"
    root = config.get()["modelMapping"]
    assert all("old" not in (root.get(line) or {}) for line in ("global", *model_mapping.INGRESS_LINES))
    assert root["global"]["new"] == "target-new"
    assert root["global"]["other"] == "target-other"
    assert root["anthropic"]["legacy-only"] == "legacy-target"
    assert _unrelated_snapshot() == unrelated
    body = {"model": "old"}
    assert model_mapping.apply_mapping(body, "anthropic") is None
    body = {"model": "new"}
    assert model_mapping.apply_mapping(body, "anthropic") == ("new", "target-new")


def test_atomic_update_missing_revision_stale_unknown_and_conflict_are_zero_write():
    control = MappingControl()
    revision = control.list_mappings(None, query=None, sort="alias", page=1, page_size=50).revision
    cases = [
        (None, "old", "new", "target", ManagementErrorCode.CONFIRMATION_REQUIRED),
        ("rev_stale", "old", "new", "target", ManagementErrorCode.REVISION_CONFLICT),
        (revision, "missing", "new", "target", ManagementErrorCode.RESOURCE_NOT_FOUND),
        (revision, "old", "other", "target", ManagementErrorCode.RESOURCE_CONFLICT),
    ]
    for expected, old, new, target, code in cases:
        before = copy.deepcopy(config.get())
        with pytest.raises(ManagementError) as exc:
            control.update_mapping(
                control.current_context(), old, new_alias=new,
                real_model=target, expected_revision=expected,
            )
        assert exc.value.code is code
        assert config.get() == before

    # A real/media resource name cannot be captured as a new alias either.
    config.update(lambda cfg: cfg.__setitem__("channels", [{
        "name": "one", "models": [{"real": "occupied", "alias": "occupied"}],
    }]))
    current = control.list_mappings(None, query=None, sort="alias", page=1, page_size=50).revision
    before = copy.deepcopy(config.get())
    with pytest.raises(ManagementError) as occupied:
        control.update_mapping(
            control.current_context(), "old", new_alias="occupied",
            real_model="target", expected_revision=current,
        )
    assert occupied.value.code is ManagementErrorCode.RESOURCE_CONFLICT
    assert config.get() == before


def test_mapping_patch_http_requires_revision_and_is_atomic(domain_client):
    client, _runtime, admin, _read_only, _denied = domain_client
    config.update(lambda cfg: cfg.__setitem__(
        "modelMapping", {"global": {"old": "target-old"}},
    ))
    listed = client.get("/api/management/v1/model-mappings", headers=admin).json()
    revision = listed["meta"]["revision"]
    no_match = client.patch(
        "/api/management/v1/model-mappings/old", headers=admin,
        json={"alias": "renamed", "realModel": "retargeted"},
    )
    assert no_match.status_code == 400
    assert no_match.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    updated = client.patch(
        "/api/management/v1/model-mappings/old",
        headers={**admin, "If-Match": revision},
        json={"alias": "renamed", "realModel": "retargeted"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["alias"] == "renamed"
    assert model_mapping.get_global_map()["renamed"] == "retargeted"
