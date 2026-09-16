from __future__ import annotations

import ast
import csv
import json
from pathlib import Path

import server


FIXTURES = Path(__file__).parent / "fixtures"
TG_MANIFEST = FIXTURES / "tg_contract/v0.31.13/manifest.jsonl"
CROSSWALK = FIXTURES / "management_api/tg-capability-crosswalk.tsv"
PRODUCTION_OPERATIONS = FIXTURES / "management_api/production-operation-ids.txt"


def _management_operation_ids() -> set[str]:
    methods = {"get", "post", "put", "patch", "delete"}
    return {
        operation["operationId"]
        for path, path_item in server.app.openapi()["paths"].items()
        if path.startswith("/api/management/v1")
        for method, operation in path_item.items()
        if method in methods
    }


def test_all_53_tg_capabilities_map_to_the_complete_production_api_surface():
    capability_ids = {
        json.loads(line)["capabilityId"]
        for line in TG_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    with CROSSWALK.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    mapped_capabilities = {row["capabilityId"] for row in rows}
    assert len(rows) == len(mapped_capabilities) == len(capability_ids) == 53
    assert mapped_capabilities == capability_ids
    assert {row["coverage"] for row in rows} <= {"adapter-only", "auth", "shared", "shared+adapter"}

    for row in rows:
        operations = [value for value in row["operationIds"].split(",") if value]
        if row["coverage"] == "adapter-only":
            assert operations == []
            assert row["owner"] == "Telegram adapter"
        else:
            assert operations
            assert row["owner"] != "Telegram adapter"

    mapped_operations = {
        value
        for row in rows
        for value in row["operationIds"].split(",")
        if value
    }
    expected_operations = set(
        PRODUCTION_OPERATIONS.read_text(encoding="utf-8").splitlines()
    )
    actual_operations = _management_operation_ids()
    assert len(mapped_operations) == len(expected_operations) == len(actual_operations) == 226
    assert mapped_operations == expected_operations == actual_operations


def test_model_center_operations_have_explicit_capability_owners():
    with CROSSWALK.open(encoding="utf-8", newline="") as handle:
        rows = {row["capabilityId"]: row for row in csv.DictReader(handle, delimiter="\t")}

    expected = {
        "TG-MAP-01": {
            "listModels", "getModel", "setModelState", "updateModelMapping",
        },
        "TG-MAP-02": {
            "patchModelMetadataOverrides", "deleteModelMetadataOverrides",
        },
        "TG-XIM-01": {
            "addXaiMediaModel", "renameXaiMediaModel", "removeXaiMediaModel",
        },
        "TG-IMG-01": {
            "getAntigravityMediaSettings", "updateAntigravityMediaSettings",
            "addAntigravityMediaModel", "renameAntigravityMediaModel",
            "removeAntigravityMediaModel",
        },
    }
    assert sum(map(len, expected.values())) == 14
    for capability_id, operations in expected.items():
        row = rows[capability_id]
        assert row["coverage"] == "shared"
        assert operations <= set(row["operationIds"].split(","))
    assert "ModelCenterControl" in rows["TG-MAP-01"]["owner"]
    assert "AntigravityMediaControl" in rows["TG-IMG-01"]["owner"]


def test_telegram_main_reads_business_state_through_status_control():
    source_path = Path("src/telegram/menus/main.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    imported_names = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "DEFAULT_STATUS_CONTROL" in imported_names
    assert imported_names.isdisjoint({
        "affinity",
        "concurrency",
        "config",
        "load_balancing",
        "network_monitor",
        "oauth_manager",
        "public_ip",
        "registry",
        "state_db",
        "status_monitor",
        "update_checker",
    })
