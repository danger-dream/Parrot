"""Cross-segment gate for the frozen v0.31.13 Telegram contract manifest."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from src.tests.tg_contract import (
    assert_capability_coverage,
    assert_strict_equal,
    load_jsonl,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).parent / "fixtures/tg_contract/v0.31.13"
MANIFEST = FIXTURE_ROOT / "manifest.jsonl"
SEGMENT_ROOT = FIXTURE_ROOT / "segments"
DOC = ROOT / "docs/13-management-control-api-refactor.md"

SEGMENT_CAPABILITIES = {
    "core": {f"TG-CORE-{number:02d}" for number in range(1, 7)},
    "main_status": {"TG-MAIN-01", "TG-HELP-01", "TG-STATUS-01"},
    "oauth": {
        *(f"TG-OA-{number:02d}" for number in range(1, 8)),
        "TG-ODM-01",
        "TG-OA-SET-01",
    },
    "channels_apikey": {
        *(f"TG-CH-{number:02d}" for number in range(1, 6)),
        *(f"TG-AK-{number:02d}" for number in range(1, 5)),
    },
    "observability": {
        "TG-CACHE-01",
        "TG-STATS-01",
        "TG-STATS-02",
        *(f"TG-LOG-{number:02d}" for number in range(1, 5)),
        "TG-MEDIA-01",
    },
    "system": {f"TG-SYS-{number:02d}" for number in range(1, 9)},
    "model_routing": {
        "TG-MAP-01",
        "TG-MAP-02",
        "TG-LB-01",
        "TG-PX-01",
        "TG-PX-02",
    },
    "auxiliary": {
        "TG-TL-01",
        "TG-STAT-01",
        "TG-UPD-01",
        "TG-IMG-01",
        "TG-XIM-01",
    },
}
SEGMENT_ORDER = tuple(SEGMENT_CAPABILITIES)
EXPECTED_CAPABILITIES = set().union(*SEGMENT_CAPABILITIES.values())
EXPECTED_CASE_COUNT = 663
EXPECTED_FIXTURE_SHA256 = {
    "manifest.jsonl": "9743317cf7b195623008e9a3aee6e43dd6ca2a74feda544cecf02f78680eef2a",
    "segments/auxiliary.jsonl": "f8ff628ff205ed682b0f7406ddc6f358479a49abe668a8340e012bd8d006dbf9",
    "segments/channels_apikey.jsonl": "12c189f697a1c4868caca3608bdb15185af6959fb0aac5e5a8177be68f624539",
    "segments/core.jsonl": "5cdd2559fc563397ee1b020f4b7067ed68ce4816174fd18e908784ea34598d0b",
    "segments/main_status.jsonl": "32d065524cf3f7e727e8d894cfa552a1293dcee3bf28c50f37b77f67143dbe00",
    "segments/model_routing.jsonl": "6477b4bb46f3874c0f170729cef1ec817099741d9ea065b11ba8e6f9adcf9b6d",
    "segments/oauth.jsonl": "d098f379c7daa7a2e2cc691807a67032d98051849551f531d3acb987e9a8869b",
    "segments/observability.jsonl": "ff6acee94170c119e9bae472ff0562f19d0457a16d83fd0c28c94cce1d81f444",
    "segments/system.jsonl": "1840150adab1acac78dc35f0acc5b7d191dc59e265890a030a261b0ee2d13c59",
}


def _doc_capabilities() -> set[str]:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## 14. Telegram 零变化完整功能清单", 1)[1]
    section = section.split("### 14.6 基线轨迹格式", 1)[0]
    return set(re.findall(r"TG-[A-Z]+(?:-[A-Z]+)*-\d{2}", section))


def test_frozen_fixture_content_hashes_are_pinned():
    assert {
        path.relative_to(FIXTURE_ROOT).as_posix()
        for path in FIXTURE_ROOT.rglob("*.jsonl")
    } == set(EXPECTED_FIXTURE_SHA256)
    for relative_path, expected in EXPECTED_FIXTURE_SHA256.items():
        actual = hashlib.sha256((FIXTURE_ROOT / relative_path).read_bytes()).hexdigest()
        assert actual == expected, relative_path


def test_final_manifest_is_exact_stable_concatenation_of_all_segments():
    paths = [SEGMENT_ROOT / f"{name}.jsonl" for name in SEGMENT_ORDER]
    assert {path.name for path in SEGMENT_ROOT.glob("*.jsonl")} == {
        path.name for path in paths
    }
    expected_bytes = b"".join(path.read_bytes() for path in paths)
    assert all(path.read_bytes().endswith(b"\n") for path in paths)
    assert MANIFEST.read_bytes() == expected_bytes

    expected_cases = [case for path in paths for case in load_jsonl(path)]
    manifest_cases = load_jsonl(MANIFEST)
    assert_strict_equal(expected_cases, manifest_cases)
    assert len(manifest_cases) == EXPECTED_CASE_COUNT
    assert len({case["caseId"] for case in manifest_cases}) == EXPECTED_CASE_COUNT


def test_manifest_capabilities_match_doc_and_segment_ownership_bidirectionally():
    assert len(EXPECTED_CAPABILITIES) == 53
    assert EXPECTED_CAPABILITIES == _doc_capabilities()
    manifest_cases = load_jsonl(MANIFEST)
    assert_capability_coverage(EXPECTED_CAPABILITIES, manifest_cases)

    for name, expected_ids in SEGMENT_CAPABILITIES.items():
        assert_capability_coverage(
            expected_ids,
            load_jsonl(SEGMENT_ROOT / f"{name}.jsonl"),
        )


def test_frozen_manifest_has_no_management_auth_surface_or_update_mode():
    manifest_text = MANIFEST.read_text(encoding="utf-8")
    assert "mauth:" not in manifest_text
    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path(__file__).parent.glob("test_tg_contract_*.py")
    )
    forbidden = "TG_CONTRACT_" + "RECORD"
    assert forbidden not in source_text
    assert re.search(
        r"(?:SEGMENT|MANIFEST)\.write_(?:text|bytes)\s*\(", source_text
    ) is None
    assert re.search(
        r"open\s*\(\s*(?:SEGMENT|MANIFEST)[^\n]*['\"]w", source_text
    ) is None
