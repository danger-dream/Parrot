"""渠道注册表：从 config 构造所有 Channel 实例，并在 config 热加载时重建。

同时负责 state.db 的级联清理（删除孤儿渠道的历史数据）。
"""

from __future__ import annotations

import copy
import threading
import uuid
from typing import Optional

from .. import affinity, channel_state, config, cooldown, load_balancing, scorer, state_db
from ..oauth import normalize_provider as _normalize_provider
from .api_channel import ApiChannel
from .base import Channel
from .compatibility import normalize_mode, normalize_models
from .cursor_oauth_channel import CursorOAuthChannel
from .oauth_channel import OAuthChannel
from .openai_oauth_channel import OpenAIOAuthChannel
from .xai_oauth_channel import XAIOAuthChannel
from .url_utils import (
    normalize_api_path,
    split_base_url,
    validate_api_path_for_protocol,
)


_lock = channel_state.mutation_lock
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
    # Snapshot, generation validation, publication, and stale cleanup are one
    # lifecycle. A paused old rebuild must not overwrite a completed rename.
    with channel_state.mutation_lock:
        _rebuild_from_config_locked()


def _rebuild_from_config_locked() -> None:
    """根据当前 config 重建所有渠道实例。"""
    cfg = config.get()
    default_models = list(cfg.get("oauthDefaultModels") or [])

    new: dict[str, Channel] = {}

    for acc in cfg.get("oauthAccounts", []):
        provider = _normalize_provider(acc.get("provider"))
        try:
            if provider == "openai":
                ch = OpenAIOAuthChannel(acc)
            elif provider == "xai":
                ch = XAIOAuthChannel(acc)
            elif provider == "cursor":
                ch = CursorOAuthChannel(acc)
            else:
                ch = OAuthChannel(acc, default_models)
            if channel_state.is_retired_source(ch.key):
                print(f"[registry] skip reused retired channel key: {ch.key}")
                continue
            new[ch.key] = ch
        except Exception as exc:
            print(f"[registry] skip invalid OAuth account (provider={provider}): {exc}")

    for entry in cfg.get("channels", []):
        proto = entry.get("protocol", "anthropic")
        cls = _channel_factories.get(proto, ApiChannel)
        try:
            ch = cls(entry)
            ch.state_key = channel_state.register_api_generation(
                ch.key, entry.get("generationId"),
            )
            if (
                channel_state.is_deleted(ch.state_key)
                or channel_state.is_retired_source(ch.key)
            ):
                print(f"[registry] skip retired API channel generation: {ch.key}")
                continue
            new[ch.key] = ch
        except Exception as exc:
            print(f"[registry] skip invalid API channel (protocol={proto}): {exc}")

    with _lock:
        global _channels
        _channels = new

    _sync_state_db_with_channels()


def _sync_state_db_with_channels() -> None:
    """清理 state.db 和内存镜像中不再存在的 channel_key。"""
    with channel_state.mutation_lock:
        with _lock:
            live_keys = channel_state.include_transitions(set(_channels.keys()))

        stale_perf = {
            row["channel_key"] for row in state_db.perf_load_all()
            if row["channel_key"] not in live_keys
        }
        # 可选 SQLite 写失败时 scorer 会合法地保留 memory-only 评分；
        # stale 集必须同时覆盖 DB 和内存，否则同名渠道重建会继承旧分数。
        stale_perf.update(
            channel_key for channel_key in scorer.channel_keys()
            if channel_key not in live_keys
        )
        for channel_key in stale_perf:
            scorer.clear_stats(channel_key)

        stale_errors = {
            row["channel_key"] for row in state_db.error_load_all()
            if row["channel_key"] not in live_keys
        }
        for channel_key in stale_errors:
            # cooldown 的内存态与 state.db 必须通过同一个提交点删除。
            # resolve_alias=False 清理的就是被判定为 stale 的旧 generation。
            cooldown.clear(
                channel_key, notify_recovered=False, resolve_alias=False,
            )

        affinity.delete_stale_channels(live_keys)
        affinity.client_delete_stale_channels(live_keys)


def all_channels() -> list[Channel]:
    with channel_state.mutation_lock:
        with _lock:
            return list(_channels.values())


def get_channel(key: str) -> Optional[Channel]:
    with channel_state.mutation_lock:
        with _lock:
            ch = _channels.get(key)
            if ch is not None:
                return ch
            # 兼容：调用方可能还在传老格式 "oauth:<email>"（不含 provider 段）
            if key.startswith("oauth:") and key.count(":") == 1:
                email = key[len("oauth:"):]
                matches = [
                    c for c in _channels.values()
                    if getattr(c, "email", None) == email and c.type == "oauth"
                ]
                return matches[0] if len(matches) == 1 else None
            if key.startswith("oauth:openai:"):
                identity = key[len("oauth:openai:"):]
                matches = [
                    c for c in _channels.values()
                    if c.type == "oauth"
                    and getattr(c, "protocol", "") == "openai-responses"
                    and getattr(c, "email", None) == identity
                ]
                return matches[0] if len(matches) == 1 else None
            if key.startswith("oauth:xai:"):
                identity = key[len("oauth:xai:"):]
                legacy_email = ""
                legacy_subject = ""
                if ":" in identity:
                    legacy_email, _, legacy_subject = identity.partition(":")
                matches = []
                for c in _channels.values():
                    if c.type != "oauth" or getattr(c, "provider", "") != "xai":
                        continue
                    email = str(getattr(c, "email", "") or "")
                    subject = str(getattr(c, "subject", "") or "")
                    if identity in (email, subject) or (legacy_email == email and legacy_subject == subject):
                        matches.append(c)
                return matches[0] if len(matches) == 1 else None
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
    with config.serialized_updates():
        return _add_api_channel_serialized(entry)


def _add_api_channel_serialized(entry: dict) -> dict:
    """
    添加一个 API 渠道（type="api"），写入 config 并触发重建。
    entry 需含 name/baseUrl/apiKey/models；可含 cc_mimicry/enabled/apiPath，
    以及渠道兼容策略字段。

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
    # Rename sources remain aliases and cannot be reused safely. Deleted API
    # generations no longer tombstone this logical display key.
    channel_state.assert_reusable(f"api:{name}")
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
            # Stable across normal reload/backup/restore; replaced only by a
            # real delete + add lifecycle.
            "generationId": uuid.uuid4().hex,
            "baseUrl": split_base,
            "apiKey": entry.get("apiKey", ""),
            "protocol": protocol,
            "providerId": entry.get("providerId") or None,
            "providerPresetId": entry.get("providerPresetId") or None,
            "models": list(entry.get("models") or []),
            "cc_mimicry": (bool(entry.get("cc_mimicry", default_cc))
                            if protocol == "anthropic" else False),
            "omitTemperature": bool(entry.get("omitTemperature", False)),
            "omitThinking": bool(entry.get("omitThinking", False)),
            "context1mMode": normalize_mode(entry.get("context1mMode")),
            "context1mModels": normalize_models(entry.get("context1mModels")),
            "fastMode": normalize_mode(entry.get("fastMode")),
            "fastModels": normalize_models(entry.get("fastModels")),
            "maxConcurrent": int(entry.get("maxConcurrent", 0) or 0),
            "enabled": bool(entry.get("enabled", True)),
            "disabled_reason": None,
        }
        if split_path:
            normalized["apiPath"] = split_path
        # custom/旧渠道不写空身份字段；未知非空身份仍原样保留，不影响运行时。
        if not normalized["providerId"]:
            normalized.pop("providerId")
        if not normalized["providerPresetId"]:
            normalized.pop("providerPresetId")
        channels.append(normalized)
    config.update(_mutate)
    load_balancing.sync_channel_added(
        f"api:{name}",
        load_balancing.family_for_protocol(protocol),
    )
    rebuild_from_config()
    return {"name": name}


def update_api_channel(name: str, patch: dict) -> dict | None:
    with config.serialized_updates():
        return _update_api_channel_serialized(name, patch)


def _update_api_channel_serialized(name: str, patch: dict) -> dict | None:
    """
    编辑渠道。patch 可含 name/baseUrl/apiKey/models/cc_mimicry/enabled/apiPath/protocol，
    以及 omitTemperature/omitThinking/context1m*/fast* 兼容策略字段。
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
    old_entry = next(
        (c for c in config.get().get("channels", []) if c.get("name") == name),
        {},
    )
    old_entry_snapshot = copy.deepcopy(old_entry)
    current_channel = get_channel(old_key)
    from .. import provider_usage
    old_usage_account_id = provider_usage.account_id(current_channel) if current_channel is not None else None
    current_generation_id = channel_state.generation_id(
        channel_state.effect_key(current_channel)
    ) if current_channel is not None else None
    old_priority_orders = copy.deepcopy(
        config.get().get("loadBalancing", {}).get("priorityOrders", {})
    )
    old_family = load_balancing.family_for_protocol(old_entry.get("protocol", "anthropic"))

    def _mutate(cfg):
        channels = cfg.get("channels", [])
        target = None
        for c in channels:
            if c.get("name") == name:
                target = c
                break
        if target is None:
            raise KeyError(f"channel not found: {name}")
        # Editing/renaming a legacy entry upgrades it in-place to the stable
        # generation already owned by its live Channel object.
        if not target.get("generationId") and current_generation_id:
            target["generationId"] = current_generation_id

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
        if "providerId" in patch:
            if patch["providerId"]:
                target["providerId"] = str(patch["providerId"])
            else:
                target.pop("providerId", None)
        if "providerPresetId" in patch:
            if patch["providerPresetId"]:
                target["providerPresetId"] = str(patch["providerPresetId"])
            else:
                target.pop("providerPresetId", None)
        if "models" in patch:
            target["models"] = list(patch["models"] or [])
        if "cc_mimicry" in patch:
            target["cc_mimicry"] = bool(patch["cc_mimicry"])
        if "omitTemperature" in patch:
            target["omitTemperature"] = bool(patch["omitTemperature"])
        if "omitThinking" in patch:
            target["omitThinking"] = bool(patch["omitThinking"])
        if "context1mMode" in patch:
            target["context1mMode"] = normalize_mode(patch["context1mMode"])
        if "context1mModels" in patch:
            target["context1mModels"] = normalize_models(patch["context1mModels"])
        if "fastMode" in patch:
            target["fastMode"] = normalize_mode(patch["fastMode"])
        if "fastModels" in patch:
            target["fastModels"] = normalize_models(patch["fastModels"])
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

        final_name = target.get("name", name)
        final_family = load_balancing.family_for_protocol(
            target.get("protocol", "anthropic")
        )
        final_key = f"api:{final_name}"
        if load_balancing.is_initialized(cfg) and (
            final_key != old_key or final_family != old_family
        ):
            load_balancing.mutate_channel_renamed(
                cfg, old_key, final_key, final_family,
            )

    new_name = patch.get("name", name)
    new_key = f"api:{new_name}"
    if new_key != old_key:
        channel_state.assert_reusable(new_key)

    def _rollback(cfg):
        channels = cfg.get("channels", [])
        for index, entry in enumerate(channels):
            if entry.get("name") == new_name:
                channels[index] = copy.deepcopy(old_entry_snapshot)
                break
        cfg.setdefault("loadBalancing", {})["priorityOrders"] = copy.deepcopy(
            old_priority_orders
        )

    try:
        if new_key != old_key:
            channel_state.rename_with_config(
                old_channel_key=old_key,
                new_channel_key=new_key,
                config_mutator=_mutate,
                rollback_mutator=_rollback,
            )
        else:
            config.update(_mutate)
    except (KeyError, ValueError) as exc:
        raise exc

    rebuild_from_config()
    provider_usage.cleanup_account_if_orphaned(old_usage_account_id)
    return {"name": new_name}


def delete_api_channel(name: str) -> bool:
    with config.serialized_updates():
        return _delete_api_channel_serialized(name)


def _delete_api_channel_serialized(name: str) -> bool:
    key = f"api:{name}"
    if not any(
        channel.get("name") == name
        for channel in config.get().get("channels", [])
    ):
        return False
    found = {"ok": False}
    current = get_channel(key)
    from .. import provider_usage
    old_usage_account_id = provider_usage.account_id(current) if current is not None else None
    generation_key = channel_state.effect_key(current) if current is not None else key

    def _mutate(cfg):
        channels = cfg.get("channels", [])
        for i, c in enumerate(channels):
            if c.get("name") == name:
                channels.pop(i)
                found["ok"] = True
                load_balancing.mutate_channels_removed(cfg, {key})
                return

    # Config removal, priority removal, and tombstoning are one lifecycle.
    # Requests already using the old Channel object may finish later; their
    # scorer/cooldown/affinity side effects must not recreate deleted state.
    from .. import affinity, concurrency, cooldown, scorer
    with channel_state.mutation_lock:
        # The runtime generation, not the public display key, is retired.
        # A same-name add receives a different generation and is immediately
        # reusable, while already-selected old Channel objects keep this key.
        generation_keys = [generation_key]
        frozen_limits = {
            retired_key: concurrency.capture_rename_limit(retired_key)
            for retired_key in generation_keys
        }
        channel_state.retire_deleted(generation_key)
        try:
            config.update(_mutate)
        except BaseException:
            channel_state.restore_deleted(generation_key)
            raise
        for retired_key in generation_keys:
            concurrency.retire_channel(
                retired_key,
                frozen_max=frozen_limits[retired_key],
                deleted_target=retired_key,
            )
        scorer.clear_stats(key)
        cooldown.clear(key, notify_recovered=False, resolve_alias=False)
        affinity.delete_by_channel(key)
        affinity.client_delete_by_channel(key)
        rebuild_from_config()
        provider_usage.cleanup_account_if_orphaned(old_usage_account_id)
    return True
