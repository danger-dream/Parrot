"""配置加载 / 保存 / 热加载。

单一入口 `get()` 返回当前生效配置（dict）。文件 mtime 变化时自动重载。
写入使用 tmp + os.replace 原子方式。
"""

import copy
import json
import os
import shutil
import threading
from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# DATA_DIR 是所有运行时持久化文件的根目录（config.json / state.db / logs/ / .anthropic_proxy_ids.json）。
# 优先使用环境变量 ANTHROPIC_PROXY_DATA_DIR（容器内通常是 /app/data），不设则回退到 BASE_DIR，
# 保持现有源码安装方式（systemd 直跑）行为完全不变。
DATA_DIR = os.environ.get("ANTHROPIC_PROXY_DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

# CONFIG_PATH 仍单独支持 ANTHROPIC_PROXY_CONFIG（测试场景用），否则走 DATA_DIR/config.json。
CONFIG_PATH = os.environ.get("ANTHROPIC_PROXY_CONFIG") or os.path.join(DATA_DIR, "config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "listen": {"host": "0.0.0.0", "port": 18082},
    "apiKeys": {},
    "oauthAccounts": [],
    "channels": [],
    "timeouts": {
        "connect": 10,
        "firstByte": 30,
        "idle": 120,    # chunk 之间最长空闲；上游推理慢需要更宽松
        "total": 600,
    },
    # 渠道并发限制（2026-04-22 新增）
    # 每个渠道同一时刻最多多少个在途请求；满了则在候选渠道里排队等位。
    # queueWaitSeconds 到了仍无位置 → 客户端收到 429 rate_limit_error。
    "concurrency": {
        "enabled": True,
        "queueWaitSeconds": 30,           # TG Bot 可改，全满排队超时
        "defaultMaxConcurrent": 0,        # 渠道未配 maxConcurrent 时的默认（0=不限）
    },
    "errorWindows": [1, 3, 5, 10, 15, 0],
    # OAuth 渠道宽容次数：前 N 次失败只累计计数不进入冷却（成功一次清零）。
    # 第 N+1 次失败开始按 errorWindows 阶梯。设计目的：避免单 OAuth 账号
    # 因偶发 timeout 立即冷却导致所有 Claude 模型不可用。
    "oauthGraceCount": 3,
    # Ladder throttle（2026-04-21 新增，防客户端/并发爆发把渠道打穿）：
    # 两次阶梯推进最少间隔 N 秒，期间失败仅累计计数、不推进 cooldown_until。
    # 设 0 关闭该保护。默认 30 秒足够挡住客户端秒级重试。
    "cooldownLadderMinIntervalSeconds": 30,
    # 永久冷却门槛：从首次失败（first_error_at）起，至少持续 N 秒仍在失败
    # 才允许进入永久档；不够时回退到倒数第二档。避免短时爆发误判为永久。
    # 与默认 errorWindows=[1,3,5,10,15,0] 配合：正常爬到永久需 1+3+5+10+15=34min，
    # 默认 300s=5min 几乎不影响正常路径，只挡爆发式失败。设 0 关闭该保护。
    "cooldownPermanentMinAgeSeconds": 300,
    "affinity": {
        "ttlMinutes": 30,
        "threshold": 3.0,
        "cleanupIntervalSeconds": 300,
        "clientTtlMinutes": 120,
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
        # 按访问节流刷新 usage：quotaMonitor.enabled=False 时，TG bot 每次打开
        # 主菜单 / 状态总览 / OAuth 面板 / 详情，若 oauth_quota_cache 已超过该
        # 秒数没刷新，会同步触发一次 fetch_usage（真实 HTTP 限 5s 超时，失败读旧值）。
        # enabled=True 时此节流忽略，刷新由 intervalSeconds 后台循环负责。
        "accessRefreshThrottleSeconds": 180,
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
            "openai_store_save_failed": True,  # OpenAI previous_response_id Store 写入失败
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
        # 统计汇总页各段可见性（仅影响 TG Bot 「📈 统计汇总」汇总视图；
        # 专题视图不受影响）。默认全可见；用户可在「📈 统计汇总」→「⚙ 设置」切换。
        "statsVisibility": {
            "byChannel": True,     # 按渠道 Top（家族段内）
            "byModel": True,       # 按模型 Top（家族段内）
            "byApiKey": True,      # 按 Key Top（跨家族）
            "cacheMisses": True,   # 最近未命中样本
            "recentCalls": True,   # 最近调用
        },
    },
    "oauth": {
        "mockMode": False,
        # provider 专属设置（claude 无配置项;openai 在此登记）
        "providers": {
            "openai": {
                # 是否强制把上游请求的 User-Agent 伪装成 Codex CLI
                # 官方 UA。默认 True（与 sub2api 一致）。关掉则不设置 UA,
                # 交给 httpx 默认（可能触发上游风控，不建议）。
                "forceCodexCLI": True,
                # TLS 指纹伪装开关。默认 False——httpx 直连 chatgpt.com/backend-api/codex
                # 在当前 cloudfront 策略下可通过；若被拦再手动开启。
                # 真正实装（引入 curl_cffi）在需要时单独 commit。
                "enableTLSFingerprint": False,
                # session_id / conversation_id 隔离：把下游 api_key_name 混进派生
                # 出的 session 标识，防止不同 API Key 之间会话粘性交叉污染。
                # 默认 True，基于 prompt_cache_key 派生。
                "isolateSessionId": True,
                # 账户未手填 models 时的默认模型列表。下游客户端发其中任何一个
                # 都能命中调度；发列表外的 codex 家族别名会被 scheduler 跳过
                # （需要账户手动补入 models）。transform 会把别名规范化到
                # gpt-5.1 / gpt-5.1-codex 等上游 canonical 名再发出。
                "defaultModels": [
                    "gpt-5.2",
                    "gpt-5.2-codex",
                    "gpt-5.3-codex",
                    "gpt-5.4",
                    "gpt-5.5",
                ],
            },
        },
    },
    "channelSelection": "smart",  # "smart" | "order"
    "logDir": "logs",
    "stateDbPath": "state.db",
    # OpenAI 支持相关默认值（只在 /v1/chat/completions、/v1/responses 入口或 openai-* 渠道上生效）
    "openai": {
        # previous_response_id 本地 store（跨变体 chat↔responses 必需，同协议可选）
        "store": {
            "enabled": True,
            "ttlMinutes": 60,
            "cleanupIntervalSeconds": 300,
        },
        # reasoning 跨协议桥接："passthrough" = 通过非官方字段 reasoning_content 双向映射；"drop" = 丢弃
        "reasoningBridge": "passthrough",
        # 自动补 OpenAI prompt_cache_key：仅 /v1/chat/completions 与 /v1/responses 生效。
        # 下游显式传入时绝不覆盖；未传时根据亲和链复用会话级 key，
        # 帮 OpenAI/Codex 上游稳定 prompt cache 路由。
        "autoPromptCacheKey": {
            "enabled": True,
            "prefix": "parrot:auto:v1",
        },
        # 跨变体翻译能力开关
        "translation": {
            "enabled": True,
            "rejectOnBuiltinTools": True,
            "rejectOnMultiCandidate": True,
        },
    },
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
    """在覆盖 config.json 前刷新备份链，但不移动当前 live config。

    这样即使后续写 tmp / replace 失败，live config 仍保留在原位。
    """
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
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak.1")
    except OSError:
        pass


def _write_atomic(data: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _rotate_backups()
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
