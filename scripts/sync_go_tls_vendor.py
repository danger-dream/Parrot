#!/usr/bin/env python3
"""Synchronize a verified source/prebuilt release; never compile on user installs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor/parrot-go-tls-client"
PATTERNS = ("*.go", "go.mod", "go.sum", "*.py", "README.md", "pyproject.toml", "MANIFEST.in",
            ".gitignore", ".dockerignore", "python/parrot_go_tls_client/*.py", "test/*.py", "test/*.go",
            "docker/*.md", "docker/Dockerfile.build", "prebuilt/manifest.json",
            "prebuilt/linux_amd64/libparrot_go_tls_client.so",
            "prebuilt/linux_arm64/libparrot_go_tls_client.so")


def files(root):
    return sorted({path.relative_to(root).as_posix() for pattern in PATTERNS
                   for path in root.glob(pattern) if path.is_file()})


def digest(root, names):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def aggregate(mapping):
    return hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def artifact_module(root):
    spec = importlib.util.spec_from_file_location("native_artifacts", root / "native_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(root):
    saved = json.loads((root / "SOURCE.json").read_text())
    actual = digest(root, files(root))
    native = artifact_module(root).verify(root)
    if (actual != saved["files"] or aggregate(actual) != saved["snapshot_sha256"]
            or native["source_sha256"] != saved["source_sha256"]
            or native["version"] != saved["version"]):
        raise SystemExit("vendor source/artifact identity mismatch; maintainer rebuild/resync required")
    allowed = {root / record["path"] for record in native["artifacts"].values()}
    if any(path not in allowed for path in root.rglob("*.so")
           if path.relative_to(root).parts[0] not in {"build", "dist"}):
        raise SystemExit("vendor contains an unrecorded shared library")
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        saved = check(VENDOR)
        if args.source and digest(args.source.resolve(), files(args.source.resolve())) != saved["files"]:
            raise SystemExit("independent source and vendor have drifted")
        print(f'vendor {saved["version"]} verified: {saved["source_sha256"]}')
        return
    if args.source is None:
        parser.error("source is required unless --check")
    source = args.source.resolve()
    if source == VENDOR.resolve():
        parser.error("source must be the independent package, not vendor itself")
    native = artifact_module(source).verify(source)
    names = files(source)
    receipt = VENDOR / "SOURCE.json"
    old = json.loads(receipt.read_text())["files"] if receipt.exists() else {}
    for name in sorted(set(old) - set(names)):
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise SystemExit("invalid previous vendor path")
        (VENDOR / name).unlink(missing_ok=True)
    for name in names:
        target = VENDOR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    hashes = digest(VENDOR, names)
    receipt.write_text(json.dumps({"package": "parrot-go-tls-client", "version": native["version"],
                                  "source_sha256": native["source_sha256"], "snapshot_sha256": aggregate(hashes),
                                  "files": hashes}, indent=2, sort_keys=True) + "\n")
    check(VENDOR)
    print(f'synced {len(names)} verified source/artifact files, version {native["version"]}')


if __name__ == "__main__":
    main()
