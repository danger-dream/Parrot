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
from .url_utils import (
    normalize_api_path,
    split_base_url,
    validate_api_path_for_protocol,
)


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
    state_db.client_affinity_delete_stale_channels(live_keys)


def all_channels() -> list[Channel]:
    with _lock:
        return list(_channels.values())


def get_channel(key: str) -> Optional[Channel]:
    with _lock:
        ch = _channels.get(key)
        if ch is not None:
            return ch
        # 兼容：调用方可能还在传老格式 "oauth:<email>"（不含 provider 段）
        if key.startswith("oauth:") and key.count(":") == 1:
            email = key[len("oauth:"):]
            for c in _channels.values():
                if getattr(c, "email", None) == email and c.type == "oauth":
                    return c
        return None


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
    entry 需含 name/baseUrl/apiKey/models；可含 cc_mimicry/enabled/apiPath。

    apiPath 语义（拆分完整调用路径）：
    - 如果用户在 baseUrl 里直接写了完整路径（末段命中 messages / completions /
      responses 白名单），自动拆分：`baseUrl` 只留主机，`apiPath` 放完整路径。
    - 运行期 api_channel.py 看到 apiPath 非空 → 直接拼接 `baseUrl + apiPath`。
    - 如果 entry 里显式带了 apiPath 字段，优先用它且不再对 baseUrl 自动拆分。

    重名则抛 ValueError。
    """
    name = entry.get("name")
    if not name:
        raise ValueError("channel name is required")

    protocol = entry.get("protocol") or "anthropic"
    # openai-* 渠道不走 Claude Code 伪装，强制 False
    default_cc = True if protocol == "anthropic" else False

    raw_base = (entry.get("baseUrl") or "").rstrip("/")
    explicit_api_path = entry.get("apiPath")
    if explicit_api_path:
        # UI 已经拆好，只做归一化 + 协议校验
        split_base = raw_base
        split_path = normalize_api_path(explicit_api_path)
    else:
        # 自动拆分：末段在白名单则拆，否则 (raw_base, None)
        try:
            split_base, split_path = split_base_url(raw_base)
        except ValueError as exc:
            raise ValueError(f"invalid baseUrl: {exc}")
        split_path = normalize_api_path(split_path)

    # 协议校验：apiPath 非空 → 末段必须与 protocol 匹配
    err = validate_api_path_for_protocol(split_path, protocol)
    if err:
        raise ValueError(err)

    def _mutate(cfg):
        channels = cfg.setdefault("channels", [])
        if any(c.get("name") == name for c in channels):
            raise ValueError(f"channel name already exists: {name}")
        normalized = {
            "name": name,
            "type": "api",
            "baseUrl": split_base,
            "apiKey": entry.get("apiKey", ""),
            "protocol": protocol,
            "models": list(entry.get("models") or []),
            "cc_mimicry": bool(entry.get("cc_mimicry", default_cc)),
            "maxConcurrent": int(entry.get("maxConcurrent", 0) or 0),
            "enabled": bool(entry.get("enabled", True)),
            "disabled_reason": None,
        }
        if split_path:
            normalized["apiPath"] = split_path
        channels.append(normalized)
    config.update(_mutate)
    rebuild_from_config()
    return {"name": name}


def update_api_channel(name: str, patch: dict) -> dict | None:
    """
    编辑渠道。patch 可含 name/baseUrl/apiKey/models/cc_mimicry/enabled/apiPath/protocol。
    改名时自动在 state.db / scorer / affinity 上级联。
    返回更新后的 entry；若渠道不存在返回 None。

    baseUrl / apiPath / protocol 联动规则：
    - patch 显式带 `apiPath`（含空串 / None） → 以 patch 为准。
    - 否则 patch 只带 `baseUrl` → 对新 baseUrl 尝试 split_base_url，
      命中白名单则拆分（baseUrl + apiPath），未命中则清空旧的 apiPath。
    - protocol 切换后如果当前 apiPath 与新 protocol 不匹配，抛错。
    - patch 同时指定 baseUrl + apiPath 时以显式值为准。
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

        # 先算出本次更新后的 protocol / baseUrl / apiPath，再统一校验 + 写回
        new_proto = target.get("protocol", "anthropic")
        if "protocol" in patch:
            np = patch["protocol"] or "anthropic"
            if np not in ("anthropic", "openai-chat", "openai-responses"):
                raise ValueError(f"unsupported protocol: {np}")
            new_proto = np

        new_base = target.get("baseUrl", "")
        # apiPath 目标值的三种来源（优先级由高到低）：
        # 1) patch 显式带 apiPath
        # 2) patch 带 baseUrl 但无 apiPath → 用 baseUrl 末段判断是否拆分
        # 3) 都没→保留原值
        explicit_api_path_given = "apiPath" in patch
        if "baseUrl" in patch:
            raw = (patch["baseUrl"] or "").rstrip("/")
            if explicit_api_path_given:
                new_base = raw
            else:
                # 根据新 baseUrl 重新判断是否拆分
                try:
                    split_base, split_path = split_base_url(raw)
                except ValueError as exc:
                    raise ValueError(f"invalid baseUrl: {exc}")
                new_base = split_base
                # 用自动拆分的结果覆盖原 apiPath（包括置空）
                target["apiPath"] = normalize_api_path(split_path)

        if explicit_api_path_given:
            target["apiPath"] = normalize_api_path(patch.get("apiPath"))

        # 空值 / None 同等看待：从 dict 删掉以避免进入序列化
        if not target.get("apiPath"):
            target.pop("apiPath", None)

        # 校验 apiPath 与 new_proto 匹配
        err = validate_api_path_for_protocol(target.get("apiPath"), new_proto)
        if err:
            raise ValueError(err)

        # 写回 baseUrl
        target["baseUrl"] = new_base

        if "apiKey" in patch:
            target["apiKey"] = patch["apiKey"]
        if "models" in patch:
            target["models"] = list(patch["models"] or [])
        if "cc_mimicry" in patch:
            target["cc_mimicry"] = bool(patch["cc_mimicry"])
        if "protocol" in patch:
            target["protocol"] = new_proto
            # 切换到 openai-* 时强制关闭 CC 伪装；切回 anthropic 保留用户原设置（若无则 True）
            if new_proto != "anthropic":
                target["cc_mimicry"] = False
            elif "cc_mimicry" not in target:
                target["cc_mimicry"] = True
        if "enabled" in patch:
            target["enabled"] = bool(patch["enabled"])
            target["disabled_reason"] = None if patch["enabled"] else "user"
        if "maxConcurrent" in patch:
            try:
                target["maxConcurrent"] = max(0, int(patch["maxConcurrent"] or 0))
            except (TypeError, ValueError):
                target["maxConcurrent"] = 0
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
        affinity.client_rename_channel(old_key, new_key)
        from .. import concurrency
        concurrency.rename_channel(old_key, new_key)

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
    affinity.client_delete_by_channel(key)
    from .. import concurrency
    concurrency.forget_channel(key)
    rebuild_from_config()
    return True
