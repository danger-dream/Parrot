"""Verify maintainer-built libraries; installation never invokes a compiler."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import struct

SOURCE_PATTERNS = ("*.go", "go.mod", "go.sum", "*.py", "pyproject.toml", "MANIFEST.in",
                   "README.md", ".gitignore", ".dockerignore",
                   "python/parrot_go_tls_client/*.py", "test/*.py", "test/*.go", "docker/*.md",
                   "docker/Dockerfile.build")
MACHINES = {"amd64": 62, "arm64": 183}


def source_files(root: Path) -> list[str]:
    return sorted({p.relative_to(root).as_posix() for pattern in SOURCE_PATTERNS
                   for p in root.glob(pattern) if p.is_file()})


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hash(root: Path) -> str:
    mapping = {name: file_hash(root / name) for name in source_files(root)}
    return hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def current_arch() -> str:
    machine = platform.machine().lower()
    if platform.system() != "Linux" or machine not in {"x86_64", "amd64", "aarch64", "arm64"}:
        raise RuntimeError("Go TLS prebuilt package supports Linux amd64/arm64; use the published Docker image")
    return "amd64" if machine in {"x86_64", "amd64"} else "arm64"


def verify_elf(path: Path, arch: str) -> None:
    header = path.read_bytes()[:20]
    if (len(header) != 20 or header[:6] != b"\x7fELF\x02\x01"
            or struct.unpack_from("<H", header, 18)[0] != MACHINES[arch]):
        raise RuntimeError(f"prebuilt library has the wrong ELF architecture: {arch}")


def verify(root: Path, arch: str | None = None) -> dict:
    manifest = json.loads((root / "prebuilt/manifest.json").read_text())
    if manifest.get("schema") != 1 or manifest.get("source_sha256") != source_hash(root):
        raise RuntimeError("prebuilt library source identity mismatch; maintainer rebuild required")
    arches = [arch] if arch else list(MACHINES)
    for target in arches:
        record = manifest["artifacts"][target]
        expected = f"prebuilt/linux_{target}/libparrot_go_tls_client.so"
        if record.get("path") != expected:
            raise RuntimeError("unexpected prebuilt library path")
        path = root / expected
        if path.is_symlink() or file_hash(path) != record["sha256"]:
            raise RuntimeError(f"prebuilt library checksum mismatch: {target}")
        verify_elf(path, target)
    return manifest
