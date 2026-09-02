"""Compatibility entrypoint for the current Claude Code wire-model checks.

The historical script compared Parrot to a neighbouring ``cc-proxy`` checkout.
That implementation is a pre-v2.1.258 baseline and is no longer an oracle.
Run the isolated, fixture-backed tests instead; no config or external service is
read or modified by this entrypoint.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    command = [
        str(ROOT / "venv" / "bin" / "python"),
        str(ROOT / "src" / "tests" / "isolated_pytest.py"),
        "-q",
        str(ROOT / "src" / "tests" / "test_cc_v2_1_258_upgrade.py"),
        str(ROOT / "src" / "tests" / "test_channel_compatibility.py"),
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
