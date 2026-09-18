"""Exact search/retired-AG route delta and reviewed System Settings goldens."""
from __future__ import annotations

import asyncio
import copy
import csv
import inspect
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

import server
from src.management_api.routers import search
from src.management_auth import Capability
from src.management_control import ManagementError
from src.tests.test_search_management import api, memory, control, ctx
from src.tests.test_tg_contract_system_helpers import SystemEnv, actual_case, SEGMENT
from src.tests.test_tg_contract_system_settings import RUNNERS as SETTINGS_RUNNERS
from src.tests.test_tg_contract_system_runtime import RUNNERS as RUNTIME_RUNNERS
from src.tests.tg_contract import assert_strict_equal, load_jsonl
from src.tests.tg_contract.current import load_current_jsonl

FIXTURES = Path(__file__).parent / "fixtures/management_api"
with (FIXTURES / "search-operation-manifest.tsv").open(encoding="utf-8", newline="") as handle:
    SEARCH_ROWS = list(csv.DictReader(handle, delimiter="\t"))
SEARCH_OPERATIONS = {row["operationId"]: (row["method"], row["path"]) for row in SEARCH_ROWS}
RETIRED_AG_OPERATIONS = {
    "getAntigravityMediaSettings": ("GET", "/api/management/v1/antigravity/media-settings"),
    "updateAntigravityMediaSettings": ("PATCH", "/api/management/v1/antigravity/media-settings"),
    "addAntigravityMediaModel": ("POST", "/api/management/v1/antigravity/media-models/image"),
    "renameAntigravityMediaModel": ("PATCH", "/api/management/v1/antigravity/media-models/image/{modelId}"),
    "removeAntigravityMediaModel": ("DELETE", "/api/management/v1/antigravity/media-models/image/{modelId}"),
}
SYSTEM_CASE_IDS = (
    "TG-SYS-01.001-home-edit", "TG-SYS-01.002-home-send",
    "TG-SYS-08.002-concurrency-active", "TG-SYS-08.004-limiter-active",
    "TG-SYS-08.006-limiter-durations",
)
# 搜索工具与 MCP 服务现在是同一行（与系统设置里其他成对按钮一致）。
SEARCH_MCP_BUTTON_ROW = [
    {"text": "🔎 搜索工具", "callback_data": "srch:show"},
    {"text": "🔌 MCP 服务", "callback_data": "mcp:show"},
]


def test_exact_search_route_methods_paths_and_retired_ag_absence():
    document = server.app.openapi()
    methods = {"get", "patch", "put", "post", "delete"}
    actual = {operation["operationId"]: (method.upper(), path)
              for path, item in document["paths"].items() if path.startswith("/api/management/v1")
              for method, operation in item.items() if method in methods}
    search_ops = {key: value for key, value in actual.items() if value[1].startswith("/api/management/v1/search")}
    assert len(SEARCH_ROWS) == len(SEARCH_OPERATIONS) == 10
    assert search_ops == SEARCH_OPERATIONS
    for path, item in document["paths"].items():
        if not path.startswith("/api/management/v1/search/backends/{"):
            continue
        assert "{backendId}" in path and "{backend_id}" not in path
        for method, operation in item.items():
            if method in methods:
                assert [p["name"] for p in operation.get("parameters", []) if p["in"] == "path"] == ["backendId"]
    assert set(RETIRED_AG_OPERATIONS).isdisjoint(actual)
    assert {path for _, path in RETIRED_AG_OPERATIONS.values()}.isdisjoint(document["paths"])
    assert len({path for _, path in SEARCH_OPERATIONS.values()}) == 8
    assert len({path for _, path in RETIRED_AG_OPERATIONS.values()}) == 3
    manifest = (FIXTURES / "production-operation-ids.txt").read_text().splitlines()
    assert len(manifest) == len(set(manifest)) == 237
    assert set(manifest) == set(actual)


def test_search_capability_mapping_is_exact_and_executable(ctx):
    from dataclasses import replace
    routes = {route.operation_id: route for route in search.router.routes if isinstance(route, APIRoute)}
    assert set(routes) == set(SEARCH_OPERATIONS)
    assert {row["operationId"]: row["secretWriteFields"] for row in SEARCH_ROWS if row["secretWriteFields"]} == {
        "createSearchBackend": "apiKeys,addApiKeys,removeKeyIndices",
        "updateSearchBackend": "apiKeys,addApiKeys,removeKeyIndices",
    }
    for row in SEARCH_ROWS:
        dependencies = [dep.call for dep in routes[row["operationId"]].dependant.dependencies
                        if dep.name == "context"]
        assert len(dependencies) == 1
        guard = dependencies[0]
        declared = inspect.getclosurevars(guard).nonlocals["capability"]
        assert declared.value == row["capability"]
        for grant in Capability:
            context = replace(ctx, actor=replace(ctx.actor, capabilities=frozenset({grant})))
            if grant == declared:
                assert asyncio.run(guard(context)) is context
            else:
                with pytest.raises(ManagementError, match="Required management capability"):
                    asyncio.run(guard(context))


@pytest.mark.parametrize("operation_id", ["createSearchBackend", "updateSearchBackend"])
@pytest.mark.parametrize("field", ["apiKeys", "addApiKeys", "removeKeyIndices"])
def test_search_conditional_secret_permission_matches_manifest(api, ctx, operation_id, field):
    from dataclasses import replace
    from src.management_api.dependencies import get_management_context
    client, headers, _, app = api
    writer = replace(ctx, actor=replace(ctx.actor, capabilities=frozenset({Capability.READ, Capability.WRITE})))
    app.dependency_overrides[get_management_context] = lambda: writer
    method, path = SEARCH_OPERATIONS[operation_id]
    body = {field: []}
    if method == "POST":
        body["type"] = "tavily"
    else:
        path = path.replace("{backendId}", "tavily")
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CAPABILITY_DENIED"


@pytest.mark.parametrize("case_id", SYSTEM_CASE_IDS)
def test_system_golden_difference_is_only_reviewed_entries(case_id, monkeypatch):
    # Derive a reviewed expectation from the frozen archive, never from output.
    archived = next(case for case in load_jsonl(SEGMENT) if case["caseId"] == case_id)
    reviewed = copy.deepcopy(archived)
    insertion_indices = []
    for index, call in enumerate(reviewed["tgApi"]):
        payload = call["payload"]
        if not payload.get("text", "").startswith("⚙ <b>系统设置</b>"):
            continue
        keyboard = payload["reply_markup"]["inline_keyboard"]
        # 相对冻结归档只多了一行：搜索工具与 MCP 服务并排的那一行。
        assert len(keyboard) == 8
        assert keyboard[-1] == [{"text": "🔁 重试设置", "callback_data": "sys:show:retry"},
                                {"text": "◀ 返回主菜单", "callback_data": "menu:main"}]
        keyboard.insert(7, copy.deepcopy(SEARCH_MCP_BUTTON_ROW))
        insertion_indices.append(index)
    assert insertion_indices
    print(f"REVIEWED {case_id}: tgApi indices {insertion_indices}; only inline_keyboard[7] added")
    scenario = archived["entry"]["scenario"]
    env = SystemEnv(archived, monkeypatch)
    (SETTINGS_RUNNERS | RUNTIME_RUNNERS)[scenario](env)
    assert_strict_equal(reviewed, actual_case(archived, env))
    current = next(case for case in load_current_jsonl(SEGMENT) if case["caseId"] == case_id)
    assert_strict_equal(reviewed, current)
