"""Prebuilt distribution integrity and compiler-free installation contracts."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

from scripts import check_go_tls, sync_go_tls_vendor

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "vendor/parrot-go-tls-client"


@pytest.fixture
def artifacts():
    return sync_go_tls_vendor.artifact_module(VENDOR)


@pytest.fixture
def synthetic_package(tmp_path, artifacts):
    root = tmp_path / "package"
    root.mkdir()
    (root / "go.mod").write_text("module fixture\ngo 1.24\n")
    (root / "transport.go").write_text("package fixture\n")
    manifest = {"schema": 1, "version": "0.2.1", "source_sha256": artifacts.source_hash(root), "artifacts": {}}
    for arch, machine in artifacts.MACHINES.items():
        path = root / f"prebuilt/linux_{arch}/libparrot_go_tls_client.so"
        path.parent.mkdir(parents=True)
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 18, machine)
        path.write_bytes(header)
        manifest["artifacts"][arch] = {"path": path.relative_to(root).as_posix(),
            "sha256": artifacts.file_hash(path), "glibc_min": "2.34"}
    (root / "prebuilt/manifest.json").write_text(json.dumps(manifest))
    return root, manifest


def test_shipped_source_and_both_artifact_hashes_match():
    receipt = sync_go_tls_vendor.check(VENDOR)
    assert receipt["version"] == "0.2.1"
    assert {name for name in receipt["files"] if name.endswith(".so")} == {
        "prebuilt/linux_amd64/libparrot_go_tls_client.so",
        "prebuilt/linux_arm64/libparrot_go_tls_client.so",
    }


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_artifact_tampering_is_rejected(artifacts, synthetic_package, arch):
    root, manifest = synthetic_package
    artifacts.verify(root)
    path = root / manifest["artifacts"][arch]["path"]
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="checksum"):
        artifacts.verify(root, arch)


def test_source_change_rejects_stale_prebuilt(artifacts, synthetic_package):
    root, _ = synthetic_package
    (root / "transport.go").write_text("package changed\n")
    with pytest.raises(RuntimeError, match="source identity"):
        artifacts.verify(root)


def test_wrong_architecture_is_rejected_even_with_updated_hash(artifacts, synthetic_package):
    root, manifest = synthetic_package
    path = root / manifest["artifacts"]["amd64"]["path"]
    value = bytearray(path.read_bytes())
    struct.pack_into("<H", value, 18, 183)
    path.write_bytes(value)
    manifest["artifacts"]["amd64"]["sha256"] = artifacts.file_hash(path)
    (root / "prebuilt/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="ELF architecture"):
        artifacts.verify(root)


def test_manifest_cannot_escape_prebuilt_directory(artifacts, synthetic_package):
    root, manifest = synthetic_package
    manifest["artifacts"]["amd64"]["path"] = "../../other-library.so"
    (root / "prebuilt/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="unexpected prebuilt"):
        artifacts.verify(root)


@pytest.mark.parametrize("system,machine", [("Darwin", "arm64"), ("Windows", "AMD64"), ("Linux", "riscv64")])
def test_unsupported_platform_fails_without_compiler_fallback(artifacts, monkeypatch, system, machine):
    monkeypatch.setattr(artifacts.platform, "system", lambda: system)
    monkeypatch.setattr(artifacts.platform, "machine", lambda: machine)
    with pytest.raises(RuntimeError, match="prebuilt package supports"):
        artifacts.current_arch()


def test_compiler_free_local_install_and_loaded_build_identity(tmp_path, artifacts):
    if importlib.util.find_spec("setuptools") is None or importlib.util.find_spec("wheel") is None:
        pytest.fail("distribution test requires setuptools and wheel")
    source = tmp_path / "source"
    shutil.copytree(VENDOR, source, ignore=shutil.ignore_patterns("__pycache__", "build", "dist", "*.egg-info"))
    commands = tmp_path / "bin"
    commands.mkdir()
    marker = tmp_path / "compiler-called"
    for name in ("go", "gcc", "cc", "clang", "aarch64-linux-gnu-gcc"):
        path = commands / name
        path.write_text(f'#!/bin/sh\necho "$0" >> "{marker}"\nexit 97\n')
        path.chmod(0o755)
    target = tmp_path / "installed"
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ.get("PATH", ""),
           "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-build-isolation", "--no-deps",
                             "--no-cache-dir", "--target", str(target), str(source)],
                            env=env, text=True, capture_output=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()
    installed = target / "parrot_go_tls_client"
    assert [p.name for p in installed.glob("*.so")] == ["libparrot_go_tls_client.so"]
    identity = json.loads((installed / "native_build.json").read_text())
    assert identity["source_sha256"] == artifacts.source_hash(VENDOR)
    code = ("import sys;sys.path.insert(0,sys.argv[1]);sys.path.insert(1,sys.argv[2]);"
            "from scripts.check_go_tls import check;from pathlib import Path;"
            "print(check(Path(sys.argv[2]),load=True))")
    loaded = subprocess.run([sys.executable, "-I", "-c", code, str(target), str(ROOT)],
                            env=env, text=True, capture_output=True, timeout=30)
    assert loaded.returncode == 0, loaded.stdout + loaded.stderr
    assert not marker.exists()
    identity_path = installed / "native_build.json"
    identity_path.write_text(json.dumps({**identity, "source_sha256": "0" * 64}))
    rejected = subprocess.run([sys.executable, "-I", "-c", code, str(target), str(ROOT)],
                              env=env, text=True, capture_output=True, timeout=30)
    assert rejected.returncode != 0 and "not this candidate" in rejected.stderr
    identity_path.write_text(json.dumps(identity))
    wrapper = installed / "client.py"
    original = wrapper.read_bytes()
    wrapper.write_bytes(original + b"\n# mismatched wrapper\n")
    rejected = subprocess.run([sys.executable, "-I", "-c", code, str(target), str(ROOT)],
                              env=env, text=True, capture_output=True, timeout=30)
    assert rejected.returncode != 0 and "wrapper differs" in rejected.stderr
    wrapper.write_bytes(original)
    library = installed / "libparrot_go_tls_client.so"
    library.write_bytes(library.read_bytes() + b"changed")
    rejected = subprocess.run([sys.executable, "-I", "-c", code, str(target), str(ROOT)],
                              env=env, text=True, capture_output=True, timeout=30)
    assert rejected.returncode != 0 and "library differs" in rejected.stderr


def test_ci_requires_native_contract_on_both_architectures():
    workflow = (ROOT / ".github/workflows/release-prepare.yml").read_text()
    assert workflow.count('PARROT_REQUIRE_GO_TLS: "1"') == 2
    assert "arch: amd64" in workflow and "arch: arm64" in workflow
    assert "python scripts/check_go_tls.py --load" in workflow
    assert "test_go_tls_transport.py" in workflow
    assert "test_go_tls_distribution.py" in workflow
