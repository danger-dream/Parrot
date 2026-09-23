#!/usr/bin/env python3
"""Reproduce/check the reviewed Codex release profile from a local tag worktree.

No network, application imports, config access, or writes to the source tree.
The two retired model baselines remain explicit data, not a model-name heuristic.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1] / "src/openai/codex_profiles"
TAG = "rust-v0.157.0-alpha.10"
COMMIT = "2170d8b3c77883dbe743078fb8bbb017f27caa9c"
BASELINE = "rust-v0.153.4"
SOURCE_FILES = (
    "codex-rs/Cargo.toml",
    "codex-rs/models-manager/models.json",
    "codex-rs/core/src/client.rs",
    "codex-rs/core/src/responses_metadata.rs",
    "codex-rs/codex-api/src/common.rs",
    "codex-rs/protocol/src/openai_models.rs",
    "codex-rs/protocol/src/openai_models/reasoning_effort.rs",
)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def generate(source: Path) -> dict[Path, bytes]:
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()
    if commit != COMMIT:
        raise ValueError(f"Expected {TAG} at {COMMIT}, got {commit}")
    source_bytes = {name: (source / name).read_bytes() for name in SOURCE_FILES}
    for name, raw in source_bytes.items():
        committed = subprocess.check_output(["git", "-C", str(source), "show", f"{COMMIT}:{name}"])
        if raw != committed:
            raise ValueError(f"Source file differs from released commit: {name}")
    version = tomllib.loads(source_bytes[SOURCE_FILES[0]].decode())["workspace"]["package"]["version"]
    if f"rust-v{version}" != TAG:
        raise ValueError("Release Cargo version does not match tag")
    baseline_raw = (ROOT / f"{BASELINE}.json").read_bytes()
    baseline = json.loads(baseline_raw)
    catalog_raw = source_bytes[SOURCE_FILES[1]]
    catalog = json.loads(catalog_raw)
    profile = {
        "schemaVersion": 1, "id": TAG,
        "source": {
            "codexTag": TAG, "codexCommit": COMMIT,
            "modelsPath": SOURCE_FILES[1], "modelsSha256": digest(catalog_raw),
            "filesSha256": {name: digest(raw) for name, raw in source_bytes.items()},
            "baselineProfilesSha256": {BASELINE: digest(baseline_raw)},
            "modelProfileHashEncoding": "UTF-8 JSON, sorted keys, compact separators, ensure_ascii=false",
            "instructions": "literal model_messages.instructions_template (no personality interpolation)",
            "ultra": "ModelInfo::resolve_reasoning_effort: valid multi_agent_reasoning_effort, else max, else last non-ultra, else medium",
        },
        "clientVersion": version,
        "identity": {"originator": "codex_cli_rs", "userAgent": f"codex_cli_rs/{version} (Unknown unknown; unknown) unknown"},
        # Reviewed request struct still rejects these API-only fields; WS beta unchanged.
        "protocol": {**baseline["protocol"], "ultraReasoningEffortFallback": True},
        "models": {},
    }
    artifacts: dict[Path, bytes] = {}
    for model in catalog["models"]:
        slug = model["slug"]
        efforts = [item["effort"] for item in model["supported_reasoning_levels"]]
        policy = {
            "minimalClientVersion": model["minimal_client_version"],
            "useResponsesLite": model.get("use_responses_lite", False),
            "reasoningEfforts": efforts,
            "defaultReasoningEffort": model.get("default_reasoning_level"),
            "supportVerbosity": model["support_verbosity"],
            "defaultVerbosity": model.get("default_verbosity"),
            "supportsReasoningSummaryParameter": model.get("supports_reasoning_summary_parameter", True),
        }
        if "multi_agent_reasoning_effort" in model:
            policy["multiAgentReasoningEffort"] = model["multi_agent_reasoning_effort"]
        instructions = model["model_messages"]["instructions_template"].encode("utf-8")
        relative = f"{TAG}/{slug}-base-instructions.txt"
        artifacts[ROOT / relative] = instructions
        policy.update({
            "baseInstructionsFile": relative,
            "baseInstructionsSha256": digest(instructions),
            "sourceCodexTag": TAG,
            "sourceModelProfileSha256": digest(json.dumps(model, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()),
        })
        profile["models"][slug] = policy
    # Absent in the latest bundled catalog does not revoke authenticated accounts'
    # legacy model IDs. Keep their reviewed wire policies, without listing them as
    # newly discovered models or claiming they came from the latest source.
    for slug, policy in baseline["models"].items():
        if slug not in profile["models"]:
            profile["models"][slug] = {**policy, "sourceCodexTag": BASELINE}
    artifacts[ROOT / f"{TAG}.json"] = (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode()
    return artifacts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifacts = generate(args.source)
    for path, raw in artifacts.items():
        if args.check:
            if not path.is_file() or path.read_bytes() != raw:
                raise SystemExit(f"Profile/source mismatch: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
    print(f"{'Verified' if args.check else 'Generated'} {len(artifacts)} artifacts from {TAG} ({COMMIT})")


if __name__ == "__main__":
    main()
