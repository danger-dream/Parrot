#!/usr/bin/env python3
"""Maintainer-only build of the precompiled libraries shipped with Parrot."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def artifact_module(source):
    spec = importlib.util.spec_from_file_location("native_artifacts", source / "native_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(source: Path, arches: list[str]):
    artifacts = artifact_module(source)
    identity = artifacts.source_hash(source)
    version_text = (source / "python/parrot_go_tls_client/__init__.py").read_text()
    version = re.search(r'^__version__ = "([^"]+)"', version_text, re.M).group(1)
    base_env = dict(os.environ, CGO_ENABLED="1", GOTOOLCHAIN="local", GOOS="linux")
    base_env.setdefault("HOME", str(Path.home()))
    go_version = subprocess.check_output(["go", "env", "GOVERSION"], env=base_env, text=True).strip()
    if not go_version.startswith("go1.24."):
        raise RuntimeError("maintainer build requires Go 1.24.x")
    host = "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "amd64"
    manifest_path = source / "prebuilt/manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if manifest.get("source_sha256") != identity:
        manifest = {"schema": 1, "version": version, "source_sha256": identity, "artifacts": {}}
    with tempfile.TemporaryDirectory(prefix="parrot-native-build-") as directory:
        pending = []
        for arch in arches:
            cc = os.environ.get("CC_" + arch.upper())
            if not cc:
                cc = "gcc" if arch == host else {"arm64": "aarch64-linux-gnu-gcc", "amd64": "x86_64-linux-gnu-gcc"}[arch]
            if not shutil.which(cc):
                raise RuntimeError(f"maintainer compiler unavailable: {cc}")
            env = dict(base_env, GOARCH=arch, CC=cc)
            output = Path(directory) / arch / "libparrot_go_tls_client.so"
            output.parent.mkdir(parents=True)
            subprocess.run(["go", "build", "-trimpath", "-buildvcs=false", "-buildmode=c-shared",
                            f"-ldflags=-s -w -X main.sourceID={identity}", "-o", str(output), "."], cwd=source, env=env, check=True)
            artifacts.verify_elf(output, arch)
            symbols = subprocess.check_output(["readelf", "--version-info", str(output)], text=True)
            glibc = re.findall(r"GLIBC_(\d+(?:\.\d+)+)", symbols)
            minimum = max(glibc, key=lambda value: tuple(map(int, value.split(".")))) if glibc else "0"
            relative = f"prebuilt/linux_{arch}/libparrot_go_tls_client.so"
            manifest["artifacts"][arch] = {"path": relative, "sha256": artifacts.file_hash(output),
                "glibc_min": minimum, "go_version": go_version, "compiler": cc}
            pending.append((output, source / relative))
        if artifacts.source_hash(source) != identity:
            raise RuntimeError("package source changed during compilation; no build receipt committed")
        for output, target in pending:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output, target)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        temporary.replace(manifest_path)
    for arch in arches:
        artifacts.verify(source, arch)
    print(json.dumps(manifest, indent=2, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "vendor/parrot-go-tls-client")
    parser.add_argument("--arch", choices=("amd64", "arm64", "all"), default="all")
    args = parser.parse_args()
    build(args.source.resolve(), ["amd64", "arm64"] if args.arch == "all" else [args.arch])


if __name__ == "__main__":
    main()
