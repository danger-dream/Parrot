"""渠道注册表：从 config 构造所有 Channel 实例，并在 config 热加载时重建。

同时负责 state.db 的级联清理（删除孤儿渠道的历史数据）。
"""

from __future__ import annotations

import threading
from typing import Optional

from .. import config, state_db
from ..oauth import normalize_provider as _normalize_provider
from .api_channel import ApiChannel
from .base import Channel
from .oauth_channel import OAuthChannel
from .openai_oauth_channel import OpenAIOAuthChannel


_lock = threading.Lock()
_channels: dict[str, Channel] = {}

# 按 protocol 名分派到 Channel 子类的 factory。未注册的 protocol 回落到 ApiChannel
# （保持 anthropic 现状 —— 老配置 / 未设 protocol 的 entry 继续走 ApiChannel）。
# OpenAI 家族在 server.py 的 lifespan 启动时通过 src/openai/channel/registration.py
# 注入两条：openai-chat / openai-responses → OpenAIApiChannel。
_channel_factories: dict[str, type[Channel]] = {}


def register_channel_factory(protocol: str, cls: type[Channel]) -> None:
    """注册一个 protocol → Channel 子类的 factory。重复注册会覆盖。"""
    _channel_factories[protocol] = cls


def rebuild_from_config() -> None:
    """根据当前 config 重建所有渠道实例。"""
    cfg = config.get()
    default_models = list(cfg.get("oauthDefaultModels") or [])

    new: dict[str, Channel] = {}

    for acc in cfg.get("oauthAccounts", []):
        provider = _normalize_provider(acc.get("provider"))
        try:
            if provider == "openai":
                ch = OpenAIOAuthChannel(acc)
            else:
                ch = OAuthChannel(acc, default_models)
            new[ch.key] = ch
        except Exception as exc:
            print(f"[registry] skip invalid OAuth account (provider={provider}): {exc}")

    for entry in cfg.get("channels", []):
        proto = entry.get("protocol", "anthropic")
        cls = _channel_factories.get(proto, ApiChannel)
        try:
            ch = cls(entry)
            new[ch.key] = ch
        except Exception as exc:
            print(f"[registry] skip invalid API channel (protocol={proto}): {exc}")

    with _lock:
        global _channels
        _channels = new

    _sync_state_db_with_channels()


def _sync_state_db_with_channels() -> None:
    """清理 state.db 中不再存在的 channel_key。"""
    with _lock:
        live_keys = set(_channels.keys())

    for row in state_db.perf_load_all():
        if row["channel_key"] not in live_keys:
            state_db.perf_delete(row["channel_key"])

    for row in state_db.error_load_all():
        if row["channel_key"] not in live_keys:
            state_db.error_delete(row["channel_key"])

    state_db.affinity_delete_stale_channels(live_keys)


def all_channels() -> list[Channel]:
    with _lock:
        return list(_channels.values())


def get_channel(key: str) -> Optional[Channel]:
    with _lock:
        return _channels.get(key)


def enabled_channels() -> list[Channel]:
    with _lock:
        return [ch for ch in _channels.values() if ch.enabled]


def find_by_display_name(name: str) -> Optional[Channel]:
    with _lock:
        for ch in _channels.values():
            if ch.display_name == name:
                return ch
    return None


def channel_count() -> int:
    with _lock:
        return len(_channels)


def available_models() -> list[str]:
    """跨所有启用渠道的客户端可见模型名（去重、排序）。

    用于 `/v1/models` 列表。OAuth 渠道返回真实模型名，API 渠道返回 alias。
    """
    return available_models_for_families(None)


def available_models_for_families(families: Optional[set[str]]) -> list[str]:
    """按家族集合过滤后的可见模型列表。

    `families=None` 或空集 → 不过滤，返回所有（等价于 available_models()）。
    家族名从 Channel.protocol 推导：`anthropic` → "anthropic"，其他 → "openai"。
    """
    models: set[str] = set()
    with _lock:
        channels = list(_channels.values())
    for ch in channels:
        if not ch.enabled or ch.disabled_reason:
            continue
        if families:
            proto = getattr(ch, "protocol", "anthropic")
            fam = "anthropic" if proto == "anthropic" else "openai"
            if fam not in families:
                continue
        for m in ch.list_client_models():
            if m:
                models.add(m)
    return sorted(models)


def install_config_reload_hook() -> None:
    """在 config 热加载 / 保存后自动重建 registry。"""
    def _on_reload(new_cfg):
        rebuild_from_config()
    config.on_reload(_on_reload)


# ─── 添加 / 更新 / 删除 API 渠道 ─────────────────────────────────

def add_api_channel(entry: dict) -> dict:
    """
    添加一个 API 渠道（type="api"），写入 config 并触发重建。
    entry 需含 name/baseUrl/apiKey/models；可含 cc_mimicry/enabled。
    重名则抛 ValueError。
    """
    name = entry.get("name")
    if not name:
        raise ValueError("channel name is required")

    protocol = entry.get("protocol") or "anthropic"
    # openai-* 渠道不走 Claude Code 伪装，强制 False
    default_cc = True if protocol == "anthropic" else False

    def _mutate(cfg):
        channels = cfg.setdefault("channels", [])
        if any(c.get("name") == name for c in channels):
            raise ValueError(f"channel name already exists: {name}")
        normalized = {
            "name": name,
            "type": "api",
            "baseUrl": (entry.get("baseUrl") or "").rstrip("/"),
            "apiKey": entry.get("apiKey", ""),
            "protocol": protocol,
            "models": list(entry.get("models") or []),
            "cc_mimicry": bool(entry.get("cc_mimicry", default_cc)),
            "enabled": bool(entry.get("enabled", True)),
            "disabled_reason": None,
        }
        channels.append(normalized)
    config.update(_mutate)
    rebuild_from_config()
    return {"name": name}


def update_api_channel(name: str, patch: dict) -> dict | None:
    """
    编辑渠道。patch 可含 name/baseUrl/apiKey/models/cc_mimicry/enabled。
    改名时自动在 state.db / scorer / affinity 上级联。
    返回更新后的 entry；若渠道不存在返回 None。
    """
    old_key = f"api:{name}"

    def _mutate(cfg):
        channels = cfg.get("channels", [])
        target = None
        for c in channels:
            if c.get("name") == name:
                target = c
                break
        if target is None:
            raise KeyError(f"channel not found: {name}")

        # 改名前置检查
        if "name" in patch and patch["name"] != name:
            if any(c.get("name") == patch["name"] for c in channels):
                raise ValueError(f"channel name already exists: {patch['name']}")

        if "baseUrl" in patch:
            target["baseUrl"] = (patch["baseUrl"] or "").rstrip("/")
        if "apiKey" in patch:
            target["apiKey"] = patch["apiKey"]
        if "models" in patch:
            target["models"] = list(patch["models"] or [])
        if "cc_mimicry" in patch:
            target["cc_mimicry"] = bool(patch["cc_mimicry"])
        if "protocol" in patch:
            new_proto = patch["protocol"] or "anthropic"
            if new_proto not in ("anthropic", "openai-chat", "openai-responses"):
                raise ValueError(f"unsupported protocol: {new_proto}")
            target["protocol"] = new_proto
            # 切换到 openai-* 时强制关闭 CC 伪装；切回 anthropic 保留用户原设置（若无则 True）
            if new_proto != "anthropic":
                target["cc_mimicry"] = False
            elif "cc_mimicry" not in target:
                target["cc_mimicry"] = True
        if "enabled" in patch:
            target["enabled"] = bool(patch["enabled"])
            target["disabled_reason"] = None if patch["enabled"] else "user"
        if "name" in patch:
            target["name"] = patch["name"]

    try:
        config.update(_mutate)
    except (KeyError, ValueError) as exc:
        raise exc

    # 若改了名，做级联迁移（scorer/cooldown/affinity 内部各自负责把 state.db 同步改名）
    new_name = patch.get("name", name)
    if new_name != name:
        from .. import scorer, affinity, cooldown
        new_key = f"api:{new_name}"
        scorer.rename_channel(old_key, new_key)
        cooldown.rename_channel(old_key, new_key)
        affinity.rename_channel(old_key, new_key)

    rebuild_from_config()
    return {"name": new_name}


def delete_api_channel(name: str) -> bool:
    key = f"api:{name}"
    found = {"ok": False}

    def _mutate(cfg):
        channels = cfg.get("channels", [])
        for i, c in enumerate(channels):
            if c.get("name") == name:
                channels.pop(i)
                found["ok"] = True
                return
    config.update(_mutate)
    if not found["ok"]:
        return False

    # 级联清理（scorer/cooldown/affinity 内部各自负责把 state.db 一并清掉）
    from .. import scorer, affinity, cooldown
    scorer.clear_stats(key)
    cooldown.clear(key)
    affinity.delete_by_channel(key)
    rebuild_from_config()
    return True
