"""测试隔离：所有测试共用的环境初始化。

在 import src.* 之前调用 `isolate()`，把 config.json / state.db / logs/
全部重定向到 tmpdir，避免测试直接改生产文件。
"""

import os
import sys
import tempfile


_ISOLATED = False
_TMP_DIR: str | None = None


def isolate() -> str:
    """创建临时目录并通过环境变量把 config / state_db / log_db 路径全部重定向进去。

    必须在 `from src import ...` 之前调用。返回 tmpdir 路径。
    """
    global _ISOLATED, _TMP_DIR
    if _ISOLATED:
        assert _TMP_DIR is not None
        return _TMP_DIR

    tmp = tempfile.mkdtemp(prefix="ap-test-")
    _TMP_DIR = tmp

    # config.json
    cfg_path = os.path.join(tmp, "config.json")
    os.environ["ANTHROPIC_PROXY_CONFIG"] = cfg_path

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
        "stateDbPath": os.path.join(tmp, "state.db"),
        "logDir":      os.path.join(tmp, "logs"),
        "telegram": {"botToken": "", "adminIds": []},
        # 确保测试里 mock 模式开（OAuth 不触网）
        "oauth": {"mockMode": True},
    }
    with open(cfg_path, "w") as f:
        json.dump(minimal, f, indent=2, ensure_ascii=False)

    _ISOLATED = True
    print(f"[tests] isolated to {tmp}")
    return tmp
