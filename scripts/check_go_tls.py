#!/usr/bin/env python3
"""Bind an installed native package to this candidate's verified artifacts."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform

ROOT = Path(__file__).resolve().parents[1]


def check(root: Path, *, load: bool = False) -> dict:
    vendor = root / "vendor/parrot-go-tls-client"
    spec = importlib.util.spec_from_file_location("candidate_native_artifacts", vendor / "native_artifacts.py")
    artifacts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifacts)
    arch = artifacts.current_arch()
    manifest = artifacts.verify(vendor, arch)
    import parrot_go_tls_client as package
    directory = Path(package.__file__).resolve().parent
    identity = json.loads((directory / "native_build.json").read_text())
    record = manifest["artifacts"][arch]
    expected = {"schema": 1, "version": manifest["version"], "source_sha256": manifest["source_sha256"],
                "arch": arch, "sha256": record["sha256"], "glibc_min": record["glibc_min"]}
    if identity != expected or importlib.metadata.version("parrot-go-tls-client") != manifest["version"]:
        raise RuntimeError("installed Go TLS build is not this candidate; reinstall requirements.txt")
    for source in (vendor / "python/parrot_go_tls_client").glob("*.py"):
        if artifacts.file_hash(source) != artifacts.file_hash(directory / source.name):
            raise RuntimeError("installed Go TLS Python wrapper differs from the candidate")
    library = Path(os.environ.get("PARROT_GO_TLS_LIBRARY") or directory / "libparrot_go_tls_client.so").resolve()
    actual = hashlib.sha256(library.read_bytes()).hexdigest()
    if actual != record["sha256"]:
        raise RuntimeError("loaded-path Go TLS library differs from the candidate artifact")
    artifacts.verify_elf(library, arch)
    libc, version = platform.libc_ver()
    if libc != "glibc" or tuple(map(int, version.split("."))) < tuple(map(int, record["glibc_min"].split("."))):
        raise RuntimeError(f"Go TLS library requires glibc >= {record['glibc_min']}; use the published Docker image")
    if load:
        with package.Client(lib_path=str(library)) as client:
            if client.native_info.get("source_id") != manifest["source_sha256"]:
                raise RuntimeError("native build info does not match candidate source identity")
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--load", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.root.resolve(), load=args.load), sort_keys=True))


if __name__ == "__main__":
    main()
