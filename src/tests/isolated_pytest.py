#!/usr/bin/env python3
"""Exec pytest only after fail-closed absolute-path isolation is installed.

This file intentionally imports only Python stdlib modules.  Invoke it by file
path, never with ``-m src.tests...``::

    python3 src/tests/isolated_pytest.py src/tests/test_upstream_round_timing.py -q

Each invocation creates one fresh absolute temporary root, writes a minimal
config whose DB/log/image paths are all below that root, marks the environment
for ``conftest.py``, then ``exec`` replaces this process with a fresh pytest
interpreter.  No ``src`` module can be imported before those steps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main(argv: list[str]) -> int:
    early_src = sorted(name for name in sys.modules if name == "src" or name.startswith("src."))
    if early_src:
        raise SystemExit(f"isolation refused: src imported before temp paths: {early_src}")

    root = Path(tempfile.mkdtemp(prefix="parrot-isolated-pytest-")).resolve()
    data_dir = (root / "data").resolve()
    log_dir = (data_dir / "logs").resolve()
    config_path = (data_dir / "config.json").resolve()
    state_path = (data_dir / "state.db").resolve()
    image_path = (data_dir / "image_logs.db").resolve()
    for path in (data_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    for path in (data_dir, log_dir, config_path, state_path, image_path):
        if not path.is_absolute() or not _under(path, root):
            raise SystemExit(f"isolation refused: unsafe path {path}")

    minimal = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "apiKeys": {},
        "oauthAccounts": [],
        "channels": [],
        "stateDbPath": str(state_path),
        "logDir": str(log_dir),
        "telegram": {"botToken": "", "adminIds": []},
        "oauth": {"mockMode": True},
        "images": {"dbPath": str(image_path)},
    }
    config_path.write_text(json.dumps(minimal, ensure_ascii=False, indent=2))

    env = os.environ.copy()
    env.update({
        "ANTHROPIC_PROXY_DATA_DIR": str(data_dir),
        "ANTHROPIC_PROXY_CONFIG": str(config_path),
        "PARROT_TEST_ISOLATED": "1",
        "PARROT_TEST_ROOT": str(root),
        "PARROT_TEST_STATE_PATH": str(state_path),
        "PARROT_TEST_LOG_DIR": str(log_dir),
        "PARROT_TEST_IMAGE_PATH": str(image_path),
        "PARROT_TEST_NO_NETWORK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    # The exec target starts with a new sys.modules.  conftest independently
    # revalidates this marker and all absolute paths before test collection.
    print(
        "ISOLATION_PREEXEC_OK "
        f"root={root} config={config_path} state={state_path} logs={log_dir} image={image_path}",
        flush=True,
    )
    if argv == ["--probe-only"]:
        if any(name == "src" or name.startswith("src.") for name in sys.modules):
            raise SystemExit("probe failed: src appeared during bootstrap")
        print("ISOLATION_ZERO_SRC_IMPORT_PROBE_OK", flush=True)
        return 0
    command = [sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin", *argv]
    os.execvpe(sys.executable, command, env)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
