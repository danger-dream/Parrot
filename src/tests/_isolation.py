"""测试隔离：所有测试共用的环境初始化。

父 bootstrap 在任何 src import 前创建绝对临时根；各测试调用
`isolate()` 只复核并复用该根，把 config.json / state.db / logs/ 保持在其中。
"""

import os
from pathlib import Path
import sys


_ISOLATED = False
_TMP_DIR: str | None = None


def isolate() -> str:
    """复核父bootstrap并复用其config/state/log绝对临时路径。

    必须在业务 `from src import ...` 之前调用。返回隔离data目录。
    """
    global _ISOLATED, _TMP_DIR
    if _ISOLATED:
        assert _TMP_DIR is not None
        return _TMP_DIR

    if os.environ.get("PARROT_TEST_ISOLATED") != "1":
        raise RuntimeError(
            "test isolation must be installed before importing src; "
            "use ./venv/bin/python src/tests/isolated_pytest.py"
        )
    root = Path(os.environ["PARROT_TEST_ROOT"]).resolve()
    data_dir = Path(os.environ["ANTHROPIC_PROXY_DATA_DIR"]).resolve()
    try:
        data_dir.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"isolated data dir escaped root: {data_dir}") from exc
    tmp = str(data_dir)
    _TMP_DIR = tmp

    cfg_path = str(Path(os.environ["ANTHROPIC_PROXY_CONFIG"]).resolve())

    # 若已经有 src.config 模块被加载，强制指过去（防止跨文件先后 import）
    mod = sys.modules.get("src.config")
    if mod is not None:
        mod.CONFIG_PATH = cfg_path

    # state.db / log_db 通过"config 的 stateDbPath / logDir 取相对路径 + BASE_DIR 组合"决定位置。
    # 为了让它们也落在 tmpdir，我们把 stateDbPath/logDir 写成绝对路径放进初始 config。
    # 但 config 还没初始化；先写一份最小 config.json 过去，让 config.get() 读到。
    import json
    minimal = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "apiKeys": {},
        "oauthAccounts": [],
        "channels": [],
        "stateDbPath": os.environ["PARROT_TEST_STATE_PATH"],
        "logDir":      os.environ["PARROT_TEST_LOG_DIR"],
        "telegram": {"botToken": "", "adminIds": []},
        # 确保测试里 mock 模式开（OAuth 不触网）
        "oauth": {"mockMode": True},
        "images": {"dbPath": os.environ["PARROT_TEST_IMAGE_PATH"]},
    }
    with open(cfg_path, "w") as f:
        json.dump(minimal, f, indent=2, ensure_ascii=False)

    _ISOLATED = True
    print(f"[tests] isolated to {tmp}")
    return tmp
