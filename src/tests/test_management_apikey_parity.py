from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from src.tests.test_management_apikey_api import build_app, session_headers
from src.tests.test_tg_contract_channels_support import SEGMENT, current_cases, run_menu_case
from src.tests.tg_contract import assert_strict_equal, load_jsonl


# Reviewed overlays, not the raw archive: the API-key page gained the MCP rows.
CASES = {case["caseId"]: case for case in current_cases()}


def test_apikey_telegram_adapter_has_no_direct_business_store_import_or_access():
    source = Path("src/telegram/menus/apikey_menu.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    forbidden = {"config", "apikey_limiter", "log_db", "registry"}
    imported = {
        alias.name.rsplit(".", 1)[-1]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    accessed = {
        node.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    assert forbidden.isdisjoint(imported | accessed)
    control_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_CONTROL"
    }
    assert {
        "snapshot_api_keys", "get_api_key", "create_api_key", "update_api_key",
        "delete_api_key", "regenerate_api_key", "replace_api_key_secret",
        "reorder_api_keys", "limiter_snapshot", "load_model_stats",
    } <= control_calls


def test_telegram_and_management_api_share_update_delete_business_outcome(
    tmp_path, monkeypatch,
):
    """§15.3 parity: same initial fixture, TG trace and API business result."""
    frozen = CASES["TG-AK-02.toggle-delete-cleanup"]
    telegram_result = run_menu_case(frozen, "apikey", monkeypatch)
    assert_strict_equal(frozen, telegram_result)

    app, runtime, _, config_store, limiter = build_app(
        tmp_path,
        initial_config=deepcopy(frozen["initialConfig"]),
    )
    client = TestClient(app)
    headers = session_headers(runtime)
    try:
        before = client.get(
            "/api/management/v1/api-keys/alpha", headers=headers,
        )
        assert before.status_code == 200, before.text
        dto = before.json()["data"]
        assert {
            "enabled": dto["enabled"],
            "allowImages": dto["allowImages"],
            "allowVideos": dto["allowVideos"],
            "allowedModels": dto["allowedModels"],
        } == {
            "enabled": True,
            "allowImages": False,
            "allowVideos": False,
            "allowedModels": [],
        }

        changed = client.patch(
            "/api/management/v1/api-keys/alpha",
            headers={**headers, "If-Match": dto["revision"]},
            json={"enabled": False, "allowImages": True, "allowVideos": True},
        )
        assert changed.status_code == 200, changed.text
        changed_dto = changed.json()["data"]
        deleted = client.delete(
            "/api/management/v1/api-keys/alpha",
            headers={**headers, "If-Match": changed_dto["revision"]},
        )
        assert deleted.status_code == 204

        assert config_store.value["apiKeys"] == telegram_result["finalBusinessState"]["config"]["apiKeys"]
        assert limiter.forgot == telegram_result["finalBusinessState"]["runtimeEvents"]["limiterForget"]
    finally:
        runtime.close()
