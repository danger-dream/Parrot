"""Compatibility entrypoint for v2.1.258 transform verification.

A neighbouring legacy ``cc-proxy`` checkout is intentionally not imported: its
older normalization and fingerprint rules are not a v2.1.258 oracle.  The
current verifier uses captured bodies plus focused double-body tests under the
project's isolated pytest bootstrap.
"""
from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    return subprocess.run(
        [
            str(ROOT / "venv" / "bin" / "python"),
            str(ROOT / "src" / "tests" / "isolated_pytest.py"),
            "-q",
            str(ROOT / "src" / "tests" / "test_cc_v2_1_258_upgrade.py"),
        ],
        cwd=ROOT,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
