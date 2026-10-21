#!/usr/bin/env python3
"""Install Qoder's native device-identity helper (runtime-info) from its official CLI package.

Qoder international's daily-credits campaign is listed only to requests that
carry this helper's machine identity. The helper ships embedded (base64) in
@qoder-ai/qodercli; this script takes the build for one Linux architecture out
of the pinned package without unpacking it, and never runs it.

Every byte is pinned: the tarball by its npm registry integrity, the program
by its sha256. Bumping QODERCLI_VERSION means updating all three pins.

    python scripts/qoder_runtime_info.py                      # this machine, /opt/qoder/runtime-info
    python scripts/qoder_runtime_info.py --arch arm64 --output ./runtime-info
    python scripts/qoder_runtime_info.py --tarball qodercli-1.1.74.tgz
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import platform
import re
import sys
import tarfile
import tempfile
import urllib.request

QODERCLI_VERSION = "1.1.74"
TARBALL_URL = f"https://registry.npmjs.org/@qoder-ai/qodercli/-/qodercli-{QODERCLI_VERSION}.tgz"
TARBALL_SHA512 = "aO8Zu9Lq3JU7zojzs8PS3gehMZMVjRizb/rIIRkVEQbohHH2owRdvfHGn8bmaiFrkZrj/L5c097OmT59Q8Kl8g=="
BINARY_SHA256 = {
    "x64": "e30b307eaae12680d2f4e220d4d34b0ea4f70355a624d6d55ee916e03f7d7478",
    "arm64": "2df9d2c5aa44cb6c45ad27f493bf3dca2aaf1e067fa0ad6e88c6cad0153329fe",
}
ARCH_ALIASES = {"amd64": "x64", "x86_64": "x64", "x64": "x64", "arm64": "arm64", "aarch64": "arm64"}
ELF_MACHINE = {"x64": 0x3E, "arm64": 0xB7}
BUNDLE_MEMBER = "package/bundle/qodercli.js"
# A 64-bit little-endian ELF header, base64-encoded: \x7fELF\x02\x01\x01
_ELF64_LITERAL = re.compile(rb'"(f0VMRgIBAQ[A-Za-z0-9+/]*={0,2})"')


def fetch_tarball(url: str = TARBALL_URL, timeout: float = 120.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def verify_tarball(data: bytes) -> None:
    digest = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != TARBALL_SHA512:
        raise SystemExit(f"qodercli {QODERCLI_VERSION} tarball integrity mismatch: sha512-{digest}")


def extract(data: bytes, arch: str) -> bytes:
    """The pinned runtime-info program for arch, read from the bundle in the verified tarball."""
    verify_tarball(data)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember(BUNDLE_MEMBER)
        bundle = archive.extractfile(member).read()
    for match in _ELF64_LITERAL.finditer(bundle):
        program = base64.b64decode(match.group(1))
        if len(program) < 20 or int.from_bytes(program[18:20], "little") != ELF_MACHINE[arch]:
            continue
        if hashlib.sha256(program).hexdigest() == BINARY_SHA256[arch]:
            return program
    raise SystemExit(f"runtime-info for linux-{arch} not found in qodercli {QODERCLI_VERSION}")


def install(program: bytes, output: str) -> None:
    directory = os.path.dirname(os.path.abspath(output)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".runtime-info.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(program)
        os.chmod(temporary, 0o755)
        os.replace(temporary, output)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--arch", default=platform.machine(),
                        help="amd64/x86_64 or arm64/aarch64 (default: this machine)")
    parser.add_argument("--output", default="/opt/qoder/runtime-info", help="where to install the program")
    parser.add_argument("--tarball", help="a local copy of the pinned qodercli tarball instead of downloading it")
    args = parser.parse_args(argv)
    arch = ARCH_ALIASES.get(str(args.arch).strip().lower())
    if arch is None:
        raise SystemExit(f"unsupported architecture for Qoder runtime-info: {args.arch}")
    if args.tarball:
        with open(args.tarball, "rb") as handle:
            data = handle.read()
    else:
        data = fetch_tarball()
    install(extract(data, arch), args.output)
    print(f"installed Qoder runtime-info (qodercli {QODERCLI_VERSION}, linux-{arch}) to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
