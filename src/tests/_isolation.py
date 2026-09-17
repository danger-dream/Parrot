"""测试隔离：所有测试共用的环境初始化。

父 bootstrap 在任何 src import 前创建绝对临时根；各测试调用
`isolate()` 只复核并复用该根，把 config.json / state.db / logs/ 保持在其中。
"""

import json
import copy
import os
from pathlib import Path
import sys


_ISOLATED = False
_TMP_DIR: str | None = None

# 与 isolated_pytest.py 的 bootstrap 保持同一份最小配置。并行时每个 worker
# 都要有一份：若该文件缺失，config._load_from_disk() 会改用 DEFAULT_CONFIG
# （其中 oauth.mockMode 为 False）并把这份默认值写回磁盘，导致测试真的发起
# OAuth 网络请求。
MINIMAL_CONFIG_KEYS = ("oauth", "stateDbPath", "logDir", "images")


def minimal_config() -> dict:
    """构造最小 config 内容；路径全部来自当前的隔离环境变量。"""
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        "apiKeys": {},
        "oauthAccounts": [],
        "channels": [],
        "stateDbPath": os.environ["PARROT_TEST_STATE_PATH"],
        "logDir":      os.environ["PARROT_TEST_LOG_DIR"],
        "telegram": {"botToken": "", "adminIds": []},
        # 确保测试里 mock 模式开（OAuth 不触网）
        "oauth": {"mockMode": True},
        # 不固定会过期的版本/profile；首次 config.get() 必须走生产归一化，
        # 从 codex_profiles/current.json 选择当前已审核组合。
        "images": {"dbPath": os.environ["PARROT_TEST_IMAGE_PATH"]},
    }


def write_minimal_config(cfg_path: str | Path) -> None:
    """把最小配置写到指定路径（幂等覆盖）。"""
    with open(cfg_path, "w") as f:
        json.dump(minimal_config(), f, indent=2, ensure_ascii=False)


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
    write_minimal_config(cfg_path)

    _ISOLATED = True
    print(f"[tests] isolated to {tmp}")
    return tmp
