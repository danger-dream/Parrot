"""配置加载 / 保存 / 热加载。

单一入口 `get()` 返回当前生效配置（dict）。文件 mtime 变化时自动重载。
写入使用 tmp + os.replace 原子方式。
"""

import copy
import json
import os
import threading
from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 路径可通过环境变量覆盖（测试场景用；生产不设就用默认位置）。
# 这样能彻底防止测试污染生产 config.json。
CONFIG_PATH = os.environ.get("ANTHROPIC_PROXY_CONFIG") or os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "listen": {"host": "0.0.0.0", "port": 18082},
    "apiKeys": {},
    "oauthAccounts": [],
    "channels": [],
    "timeouts": {
        "connect": 10,
        "firstByte": 30,
        "idle": 30,
        "total": 600,
    },
    "errorWindows": [1, 3, 5, 10, 15, 0],
    "affinity": {
        "ttlMinutes": 30,
        "threshold": 3.0,
        "cleanupIntervalSeconds": 300,
    },
    "scoring": {
        "emaAlpha": 0.25,
        "recentWindow": 50,
        "defaultScore": 3000,
        "errorPenaltyFactor": 8,
        "staleMinutes": 15,
        "staleFullDecayMinutes": 30,
        "explorationRate": 0.2,
    },
    "cooldownRecovery": {
        "enabled": True,
        "intervalSeconds": 30,
        "timeoutSeconds": 15,
    },
    "quotaMonitor": {
        # 默认关闭：避免每 60s 拉一次 /api/oauth/usage 频繁请求 Anthropic 风控盯上。
        # 用户可在 TG bot「⚙ 系统设置」→「📈 配额监控」按需启用。
        "enabled": False,
        "intervalSeconds": 60,
        "disableThresholdPercent": 95,
        "resumeThresholdPercent": 95,
    },
    "contentBlacklist": {
        "default": [],
        "byChannel": {},
    },
    # ─── 通知开关（事件级分类） ───────────────────────────────────
    # enabled = 总开关；events 里每个事件可独立开关。
    # notifier.notify_event(key, text) 会同时检查 enabled 和 events[key]。
    "notifications": {
        "enabled": True,
        "events": {
            "channel_permanent": True,    # 渠道/模型连续失败进入永久冷却
            "channel_recovered": True,    # 永久/长冷却被清除（手动 / probe 恢复）
            "quota_disabled": True,       # OAuth 配额到达阈值被自动禁用
            "quota_resumed": True,        # OAuth 配额恢复被自动启用
            "oauth_refreshed": True,      # OAuth Token 自动刷新成功
            "oauth_refresh_failed": True, # OAuth Token 自动刷新失败（标 auth_error）
            "no_channels": True,          # 无可用渠道（503）
        },
    },
    "cchMode": "disabled",
    "cchStaticValue": "00000",
    "oauthDefaultModels": [
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ],
    "probe": {
        "timeoutSeconds": 60,
        "maxTokens": 50,
        "userMessage": "1+1=?",
    },
    "telegram": {
        "botToken": "",
        "adminIds": [],
    },
    "oauth": {
        "mockMode": False,
    },
    "channelSelection": "smart",  # "smart" | "order"
    "logDir": "logs",
    "stateDbPath": "state.db",
}


_cache: dict[str, Any] | None = None
_mtime: float = 0.0
# 必须是可重入锁 (RLock)：
# update() 持锁后调 _fire_reload_callbacks → registry._on_reload →
# rebuild_from_config() → config.get() 又试图获取本锁。
# 用 threading.Lock (non-reentrant) 会让同线程二次 acquire 永久死锁。
_lock = threading.RLock()
_reload_callbacks: list = []


def _deep_merge_defaults(base: dict, override: dict) -> dict:
    """把 override 合并到 base 的深拷贝上，缺失字段用 base 补齐。"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_defaults(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _normalize_api_keys(cfg: dict) -> bool:
    """把 apiKeys 里的旧式字符串条目升级为 dict 结构（向前兼容）。

    旧格式：`{"name": "ccp-xxx"}`
    新格式：`{"name": {"key": "ccp-xxx", "allowedModels": []}}`

    返回 True 表示做了变更，调用方需要 write 回磁盘。allowedModels 为空列表
    代表"无限制"；非空则是白名单。
    """
    keys = cfg.get("apiKeys") or {}
    if not isinstance(keys, dict):
        return False
    changed = False
    for name, v in list(keys.items()):
        if isinstance(v, str):
            keys[name] = {"key": v, "allowedModels": []}
            changed = True
        elif isinstance(v, dict):
            if "key" not in v:
                # 无效条目（无 key），丢弃
                del keys[name]
                changed = True
                continue
            if "allowedModels" not in v:
                v["allowedModels"] = []
                changed = True
        else:
            # 其它类型（list / None 等）视为无效
            del keys[name]
            changed = True
    return changed


def _load_from_disk() -> dict:
    if not os.path.exists(CONFIG_PATH):
        initial = copy.deepcopy(DEFAULT_CONFIG)
        _write_atomic(initial)
        return initial
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    merged = _deep_merge_defaults(DEFAULT_CONFIG, raw)
    # 自动升级旧式 apiKeys 结构并持久化
    if _normalize_api_keys(merged):
        _write_atomic(merged)
        print("[config] upgraded legacy apiKeys to new structure")
    return merged


_BACKUP_KEEP = 3  # 保留最近 3 份 config 备份


def _rotate_backups() -> None:
    """在覆盖 config.json 之前，把旧文件轮转到 .bak.1/2/3（FIFO）。"""
    if not os.path.exists(CONFIG_PATH):
        return
    # 从大到小移位：.bak.2 → .bak.3；.bak.1 → .bak.2
    for i in range(_BACKUP_KEEP, 1, -1):
        src = CONFIG_PATH + f".bak.{i - 1}"
        dst = CONFIG_PATH + f".bak.{i}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    # 当前 config → .bak.1
    try:
        os.replace(CONFIG_PATH, CONFIG_PATH + ".bak.1")
    except OSError:
        pass


def _write_atomic(data: dict) -> None:
    _rotate_backups()
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)


def _current_mtime() -> float:
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


def _ensure_loaded(force: bool = False) -> tuple[dict, bool]:
    """返回 (cfg, need_fire_callbacks)。callback 由调用方在锁外触发。"""
    global _cache, _mtime
    mt = _current_mtime()
    need_reload = force or _cache is None or mt != _mtime
    if need_reload:
        new_cache = _load_from_disk()
        _cache = new_cache
        _mtime = _current_mtime()
        return _cache, True
    return _cache, False


def _fire_reload_callbacks(cfg: dict) -> None:
    for cb in list(_reload_callbacks):
        try:
            cb(cfg)
        except Exception as exc:
            print(f"[config] reload callback failed: {exc}")


def get() -> dict:
    """返回当前生效配置（dict）。每次调用检查 mtime，自动热加载。"""
    with _lock:
        cfg, need_fire = _ensure_loaded()
    if need_fire:
        _fire_reload_callbacks(cfg)
    return cfg


def reload() -> dict:
    """强制重载。"""
    with _lock:
        cfg, _ = _ensure_loaded(force=True)
    _fire_reload_callbacks(cfg)
    return cfg


def save() -> None:
    """把内存中当前 cache 写回磁盘。"""
    global _mtime
    with _lock:
        if _cache is None:
            _ensure_loaded()
        _write_atomic(_cache)
        _mtime = _current_mtime()


def update(mutator) -> dict:
    """以 mutator(cfg) 的方式修改 cfg 并持久化。

    `mutator` 是一个接受当前 cfg dict 的函数，可原地修改；返回值被忽略。
    调用完成后自动持久化并触发回调。

    **callback 在锁外执行**：避免 callback 内访问 config 接口时被自身锁阻塞，
    也消除其它跨模块 callback 链可能产生的死锁。
    """
    global _mtime
    with _lock:
        if _cache is None:
            _ensure_loaded()
        mutator(_cache)
        _write_atomic(_cache)
        _mtime = _current_mtime()
        snapshot = _cache
    _fire_reload_callbacks(snapshot)
    return snapshot


def on_reload(cb) -> None:
    """注册一个回调，每次配置重载（或 update）后被调用。

    回调接受新 cfg dict，不应抛异常。
    """
    _reload_callbacks.append(cb)


def path() -> str:
    return CONFIG_PATH
