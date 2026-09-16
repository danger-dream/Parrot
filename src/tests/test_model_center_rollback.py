"""MIG-05: exercise a real, pinned pre-model-center loader in isolated storage.

This targeted acceptance test needs the 0.32.2 Git object locally. It neither
checks out a branch nor starts a service. Run only via isolated_pytest.py.
"""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from src import config, model_metadata, model_state


ROLLBACK_COMMIT = "55178591ddfb6b4caf6c530c1a0fa6e7d8951089"
REPO = Path(__file__).resolve().parents[2]


def _old_source(tmp_path: Path) -> Path:
    result = subprocess.run(
        ["git", "archive", "--format=zip", ROLLBACK_COMMIT, "src"],
        cwd=REPO, capture_output=True, check=True,
    )
    root = tmp_path / "old-code"
    root.mkdir()
    with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
        assert all(
            not Path(name).is_absolute() and ".." not in Path(name).parts
            for name in archive.namelist()
        )
        archive.extractall(root)
    return root


def test_old_version_preserves_new_state_but_does_not_enforce_it(tmp_path, monkeypatch):
    assert os.environ.get("PARROT_TEST_ISOLATED") == "1"
    before = copy.deepcopy(config.get())
    before.pop("modelCenter", None)
    before.pop("modelMetadataOverrides", None)
    before["channels"] = [{
        "name": "rollback-fixture", "type": "api", "enabled": True,
        "protocol": "anthropic", "providerId": "claude",
        "baseUrl": "https://upstream.invalid", "apiKey": "fixture-only",
        "models": [
            {"real": name, "alias": name}
            for name in ("disabled-model", "hidden-model", "source-off")
        ],
    }]
    before["oauthAccounts"] = []
    before["telegram"] = {"botToken": "", "adminIds": []}
    before["apiKeys"] = {}
    upgraded = copy.deepcopy(before)
    model_state.set_global_enabled_in_config(upgraded, ["disabled-model"], False)
    model_state.set_visible_in_config(upgraded, ["hidden-model"], False)
    model_state.set_api_source_enabled_in_config(
        upgraded, "api:rollback-fixture", ["source-off"], False,
    )
    state = copy.deepcopy(upgraded["modelCenter"])
    upgraded["modelMetadataOverrides"] = {
        "defaults": {"hidden-model": {"fields": {
            "contextWindow": 1_000_000, "cost.input": 0,
            "vision": False, "reasoningEfforts": [],
        }}},
        "scoped": {"api:rollback-fixture": {"hidden-model": {
            "outboundModel": "hidden-model", "fields": {"contextWindow": 300_000},
        }}},
    }
    upgraded["modelBindings"] = {"defaults": {}, "scoped": {
        "api:rollback-fixture": {"hidden-model": {
            "target": "fixture/hidden-model", "source": "auto",
            "outboundModel": "hidden-model", "autoSnapshot": {
                "catalogRevision": "fixture-rollback-revision",
                "catalogSource": "fixture", "tariff": None,
                "metadata": {"contextWindow": 1_000_000, "maxOutputTokens": 32_000},
            },
        }},
    }}
    assert not model_state.is_global_enabled("disabled-model", upgraded)
    assert not model_state.is_discovery_visible("hidden-model", upgraded)
    assert not model_state.is_source_enabled("api:rollback-fixture", "source-off", upgraded)

    # Separate, explicit snapshots: rolling config.bak.* files are not the
    # operator's retained upgrade/rollback copies.
    pre_backup = tmp_path / "pre-upgrade.json"
    post_backup = tmp_path / "post-upgrade.json"
    working = tmp_path / "rollback-working.json"
    pre_backup.write_text(json.dumps(before), encoding="utf-8")
    post_backup.write_text(json.dumps(upgraded), encoding="utf-8")
    working.write_bytes(post_backup.read_bytes())
    old_root = _old_source(tmp_path)
    result_file = tmp_path / "old-result.json"
    env = os.environ.copy()
    env.update({
        "ANTHROPIC_PROXY_CONFIG": str(working),
        "PYTHONPATH": str(old_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    script = """
import json
from pathlib import Path
from src import config
from src.channel import registry
loaded = config.get()
assert loaded['telegram']['botToken'] == '' and not loaded['oauthAccounts']
# Only registry construction is needed, not durable-state synchronization or
# any server/background lifecycle.
registry._sync_state_db_with_channels = lambda: None
registry.rebuild_from_config()
models = registry.available_models()
config.update(lambda cfg: cfg.setdefault('notifications', {}).update(enabled=False))
Path(__import__('sys').argv[1]).write_text(json.dumps({
    'modelCenter': config.reload()['modelCenter'],
    'availableModels': sorted(models),
}), encoding='utf-8')
"""
    subprocess.run(
        [sys.executable, "-c", script, str(result_file)], cwd=old_root,
        env=env, capture_output=True, text=True, check=True, timeout=45,
    )
    old_result = json.loads(result_file.read_text(encoding="utf-8"))
    assert old_result["modelCenter"] == state
    assert {"disabled-model", "source-off"} <= set(old_result["availableModels"])
    old_saved = json.loads(working.read_text(encoding="utf-8"))
    assert old_saved["modelCenter"] == state
    assert old_saved["modelMetadataOverrides"] == upgraded["modelMetadataOverrides"]
    assert old_saved["modelBindings"] == upgraded["modelBindings"]
    assert json.loads(post_backup.read_text(encoding="utf-8")) == upgraded

    # A full pre-upgrade restore necessarily loses post-upgrade model policies;
    # retaining the post-upgrade copy lets a later re-upgrade restore them.
    working.write_bytes(pre_backup.read_bytes())
    restored_old = json.loads(working.read_text(encoding="utf-8"))
    assert "modelCenter" not in restored_old
    assert "modelMetadataOverrides" not in restored_old
    assert restored_old["modelBindings"] == before["modelBindings"]
    assert model_state.is_global_enabled("disabled-model", restored_old)
    working.write_bytes(post_backup.read_bytes())
    monkeypatch.setattr(config, "CONFIG_PATH", str(working))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0.0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    restored_new = config.reload()
    assert model_state.state_snapshot(restored_new) == state
    assert not model_state.is_global_enabled("disabled-model", restored_new)
    assert not model_state.is_discovery_visible("hidden-model", restored_new)
    assert not model_state.is_source_enabled("api:rollback-fixture", "source-off", restored_new)
    assert restored_new["channels"] == before["channels"]
    assert restored_new["modelMetadataOverrides"] == upgraded["modelMetadataOverrides"]
    assert restored_new["modelBindings"] == upgraded["modelBindings"]
    common, scoped = model_metadata.get_override_fields(
        "hidden-model", scope_key="api:rollback-fixture",
        outbound_model="hidden-model", cfg=restored_new,
    )
    assert common == {
        "contextWindow": 1_000_000, "cost.input": 0,
        "vision": False, "reasoningEfforts": [],
    }
    assert scoped == {"contextWindow": 300_000}
