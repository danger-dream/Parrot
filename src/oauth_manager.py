"""多 OAuth 账户管理。

职责：
  - 读取 config.oauthAccounts 并提供账户查询接口
  - 管理 access_token 刷新（5min 内过期阻塞刷；主动刷新在 < 10min 时）
  - 拉取 usage / profile（支持 mockMode 开发期跳过真实 HTTP）
  - 账户添加 / 删除 / 启停 / 配额禁用自动恢复

⚠ 开发期约束（docs/08 §8.0）：
  config.oauth.mockMode=true 或 env DISABLE_OAUTH_NETWORK_CALLS=1 时，
  所有到 api.anthropic.com 的请求替换为 mock，不发真实 HTTP。
  全部 OAuth 远端入口集中在本模块，易于控制。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import config, notifier, state_db
from .oauth import (
    DEFAULT_PROVIDER as _DEFAULT_PROVIDER,
    VALID_PROVIDERS as _VALID_PROVIDERS,
    normalize_provider as _normalize_provider,
)
from .oauth_ids import account_key as _account_key, split_account_key as _split_ak
from .oauth import openai as openai_provider
from .transform.cc_mimicry import CLI_USER_AGENT


# ─── 常量 ────────────────────────────────────────────────────────

OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

OAUTH_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
OAUTH_MANUAL_REDIRECT = "https://platform.claude.com/oauth/code/callback"
OAUTH_SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)


# ─── 开发期 mock 开关 ────────────────────────────────────────────

def mock_mode_enabled() -> bool:
    if os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1":
        return True
    cfg = config.get()
    return bool(cfg.get("oauth", {}).get("mockMode", False))


# ─── 账户查询（只读） ─────────────────────────────────────────────

def list_accounts() -> list[dict]:
    return list(config.get().get("oauthAccounts", []))


def get_account(account_key: str) -> dict | None:
    """按 account_key (=f"{provider}:{email}") 精确匹配账户。

    历史上本函数按 email 查找；同邮箱下 Claude + OpenAI 共存后必须联合键。
    若传入的字符串不含 ":" 则按旧 email 语义回退（兼容过渡期的老调用）。
    """
    if ":" in account_key:
        provider, email = _split_ak(account_key)
        for acc in config.get().get("oauthAccounts", []):
            if acc.get("email") != email:
                continue
            acc_prov = _normalize_provider(acc.get("provider") or _DEFAULT_PROVIDER)
            if acc_prov == provider:
                return acc
        return None
    # 兼容：纯 email 的老写法 → 仍可用（返回首个匹配）
    for acc in config.get().get("oauthAccounts", []):
        if acc.get("email") == account_key:
            return acc
    return None


def get_account_key(acc: dict) -> str:
    """账户 entry → 标准 account_key。"""
    return _account_key(acc)


def iter_account_keys() -> list[str]:
    """列出所有账户的 account_key。"""
    return [_account_key(a) for a in config.get().get("oauthAccounts", [])]


def account_key_to_email(account_key: str) -> str:
    """反查 email（用于日志/通知的人类可读字段）。"""
    _, email = _split_ak(account_key)
    return email


# ─── 刷新 token ──────────────────────────────────────────────────
#
# 注意：必须用 threading.Lock 而非 asyncio.Lock。
# 调用方有两类：
#   1) FastAPI 主 event loop（failover._try_channel → ensure_valid_token）
#   2) TG Bot 线程的临时 event loop（asyncio.run(force_refresh(...))）
# asyncio.Lock 绑定到创建它的 loop，跨 loop 使用行为不安全；
# threading.Lock 是 OS 级，跨线程跨 loop 都能正确串行同一 email 的刷新。

_refresh_locks: dict[str, threading.Lock] = {}
_refresh_lock_for_dict = threading.Lock()


def _get_refresh_lock(account_key: str) -> threading.Lock:
    """按 account_key 取刷新锁，保证同一账号（而非同一邮箱）串行刷新。"""
    with _refresh_lock_for_dict:
        lock = _refresh_locks.get(account_key)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[account_key] = lock
    return lock


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def provider_of(key_or_account: str | dict) -> str:
    """按账户拿到 provider（"claude" / "openai"）。

    入参既可以是 account entry（dict），也可以是 account_key 字符串。
    若入参是 "provider:email" 三段式 → 直接拆出 provider 返回（不必查 config）。
    """
    if isinstance(key_or_account, dict):
        return _normalize_provider(key_or_account.get("provider") or _DEFAULT_PROVIDER)
    if isinstance(key_or_account, str) and ":" in key_or_account:
        prov, _ = _split_ak(key_or_account)
        return prov
    acc = get_account(key_or_account)
    if acc is None:
        return _DEFAULT_PROVIDER
    return _normalize_provider(acc.get("provider") or _DEFAULT_PROVIDER)


def _format_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_BJT_TZ = timezone(timedelta(hours=8))


def _to_bjt(iso_or_none: str | None) -> str:
    """ISO UTC 字符串 → 北京时间 'YYYY-MM-DD HH:MM:SS'；空/无效返回 '?'。"""
    dt = _parse_iso(iso_or_none) if iso_or_none else None
    if dt is None:
        return "?"
    return dt.astimezone(_BJT_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _remaining_str(iso_or_none: str | None) -> str:
    """返回距 ISO 时间还有多久（'1h 7m' / '36m' / '已过期' / '?'）。"""
    dt = _parse_iso(iso_or_none) if iso_or_none else None
    if dt is None:
        return "?"
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "已过期"
    h = int(delta // 3600)
    m = int((delta % 3600) // 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m"


def _save_token_fields(account_key: str, new: dict) -> None:
    """把刷新后的 token 字段写回 config.oauthAccounts（按 account_key 精确匹配）。

    兼容：若入参不含 ":"（裸 email，老调用），则只按 email 匹配、不再过滤 provider，
    避免把 OpenAI 账号的刷新结果漏写回去（同邮箱 Claude+OpenAI 共存前唯一的用法）。

    若该账号此前因 `auth_error` 被自动禁用，刷新成功视为身份恢复：
    同时清掉 disabled_reason / disabled_until 并把 enabled 重新置 True。
    """
    has_prov = ":" in account_key
    target_provider, target_email = _split_ak(account_key)

    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if acc.get("email") != target_email:
                continue
            if has_prov:
                acc_prov = _normalize_provider(acc.get("provider") or _DEFAULT_PROVIDER)
                if acc_prov != target_provider:
                    continue
            acc.update(new)
            if acc.get("disabled_reason") == "auth_error":
                acc["disabled_reason"] = None
                acc["disabled_until"] = None
                acc["enabled"] = True
            break
    config.update(mutate)


def _do_refresh_http(refresh_token: str) -> dict:
    """真实请求 Anthropic token endpoint。"""
    resp = httpx.post(
        OAUTH_TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        },
        headers={
            "Content-Type": "application/json",
            "User-Agent": CLI_USER_AGENT,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _do_refresh_mock(refresh_token: str) -> dict:
    """Mock 实现：返回一个伪造的 token 对，8h 过期。"""
    return {
        "access_token": "mock-access-" + secrets.token_hex(8),
        "refresh_token": refresh_token,  # 保持不变
        "expires_in": 28800,
    }


def _refresh_sync_locked(account_key: str, force: bool) -> str:
    """同步刷新（持 threading.Lock，跨线程跨 loop 串行）。

    force=False 时进入锁后做一次"双重检查"：若另一并发刷新已完成且 token 仍有效则跳过实际请求。
    force=True 时无视剩余时间，强制刷新。
    """
    email = account_key_to_email(account_key)
    lock = _get_refresh_lock(account_key)
    with lock:
        acc = get_account(account_key)
        if acc is None:
            raise ValueError(f"unknown OAuth account: {account_key}")

        # 双重检查：force 路径不做（强制刷）
        if not force:
            expired = _parse_iso(acc.get("expired"))
            if expired and (expired - datetime.now(timezone.utc)).total_seconds() >= 300:
                return acc["access_token"]

        provider = provider_of(acc)
        if provider == "openai":
            data = openai_provider.refresh_sync(
                acc["refresh_token"], email=email,
            )
        elif mock_mode_enabled():
            data = _do_refresh_mock(acc["refresh_token"])
        else:
            data = _do_refresh_http(acc["refresh_token"])

        new_expired = datetime.now(timezone.utc) + timedelta(
            seconds=int(data.get("expires_in", 28800))
        )
        new_fields = {
            "access_token": data["access_token"],
            "expired": _format_utc(new_expired),
            "last_refresh": _format_utc(datetime.now(timezone.utc)),
        }
        if "refresh_token" in data and data["refresh_token"]:
            new_fields["refresh_token"] = data["refresh_token"]
        # OpenAI: 刷新响应若带 id_token 同步更新；解码拿出最新 metadata
        # （plan_type / chatgpt_account_id / organization_id 都可能随账户升级
        # 或换组织而变）。email 理论上不变，不覆盖以免生成孤儿 entry。
        if provider == "openai" and data.get("id_token"):
            new_fields["id_token"] = data["id_token"]
            try:
                claims = openai_provider.decode_id_token(data["id_token"])
                info = openai_provider.extract_user_info(claims)
                for k in ("chatgpt_account_id", "organization_id", "plan_type"):
                    v = info.get(k)
                    if v:   # 空值不覆盖已有字段
                        new_fields[k] = v
            except Exception as exc:
                print(f"[oauth] openai refresh: id_token decode failed for {email}: {exc}")

        _save_token_fields(account_key, new_fields)
        return new_fields["access_token"]


async def ensure_valid_token(account_key: str) -> str:
    """调用方：OAuthChannel.build_upstream_request。

    返回可用的 access_token。剩余 ≥ 5min 直接返回缓存；否则在线程中持锁刷新。
    同一 account_key 的并发请求由 threading.Lock 串行（跨 event loop 安全）。
    """
    acc = get_account(account_key)
    if acc is None:
        raise ValueError(f"unknown OAuth account: {account_key}")

    expired = _parse_iso(acc.get("expired"))
    if expired and (expired - datetime.now(timezone.utc)).total_seconds() >= 300:
        return acc["access_token"]

    return await asyncio.to_thread(_refresh_sync_locked, account_key, False)


async def force_refresh(account_key: str) -> str:
    """无视剩余时间，强制刷一次（用于 401/403 重试前 / 管理员手动触发）。"""
    return await asyncio.to_thread(_refresh_sync_locked, account_key, True)


# ─── Profile & Usage ─────────────────────────────────────────────

def _mock_profile() -> dict:
    return {"account": {"email": "mock@example.com", "uuid": "mock-uuid"}}


def _mock_usage() -> dict:
    return {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 0.0, "resets_at": None},
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
        "seven_day_opus": {"utilization": 0.0, "resets_at": None},
        "extra_usage": {"is_enabled": False, "used_credits": 0, "monthly_limit": 0, "utilization": 0},
    }


def _profile_sync(access_token: str) -> dict:
    if mock_mode_enabled():
        return _mock_profile()
    resp = httpx.get(
        OAUTH_PROFILE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLI_USER_AGENT,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _usage_sync(access_token: str) -> dict:
    """调 Anthropic /api/oauth/usage 拿 usage 数据。

    请求头与 sub2api 的 claudeUsageService.FetchUsageWithOptions 对齐（2026-04-20）：
      - Accept / Content-Type / anthropic-beta 与用户抓包一致
      - User-Agent 用 usage 专用默认值 `claude-code/2.1.7`
      - timeout 30s（sub2api 产线验证值）
    """
    if mock_mode_enabled():
        return _mock_usage()
    resp = httpx.get(
        OAUTH_USAGE_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.7",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_profile(access_token: str) -> dict:
    return await asyncio.to_thread(_profile_sync, access_token)


class QuotaNotSupported(Exception):
    """向后兼容保留：fetch_usage 现按 provider 分派，不再抛出此异常。

    2026-04-20 统一 OAuth 用量机制后，OpenAI 也走 fetch_usage 门面
    （内部转发到 channel.probe_usage）。此类仅作为类型占位保留，避免外部
    `except QuotaNotSupported` 调用链崩溃；不会再真正抛出。
    """


# 每个 OpenAI 账号的 probe 节流桶（避免 quota_monitor_loop 把 token 烧光）
# 规则：两次 probe 之间最少间隔 `openaiProbeMinIntervalSeconds`（默认 30min）
_OPENAI_PROBE_LAST: dict[str, float] = {}
_openai_probe_lock = threading.Lock()


def _openai_probe_min_interval_seconds() -> int:
    qm = config.get().get("quotaMonitor") or {}
    try:
        return int(qm.get("openaiProbeMinIntervalSeconds", 1800))
    except Exception:
        return 1800


def _openai_probe_should_skip(account_key: str) -> bool:
    """响应头被动采样足够新鲜时跳过 probe；否则按最小间隔节流。"""
    # 若最近 5 分钟内有响应头被动采样，认为数据足够新鲜，无需发 probe
    row = state_db.quota_load(account_key)
    if row:
        last_passive_ms = int(row.get("last_passive_update_at") or 0)
        if last_passive_ms > 0:
            age_s = (state_db.now_ms() - last_passive_ms) / 1000.0
            if age_s < 300:
                return True
    # 否则按 probe 节流桶判断
    min_interval = _openai_probe_min_interval_seconds()
    now = time.time()
    with _openai_probe_lock:
        last = _OPENAI_PROBE_LAST.get(account_key, 0.0)
        if now - last < min_interval:
            return True
    return False


def _openai_probe_mark(account_key: str) -> None:
    with _openai_probe_lock:
        _OPENAI_PROBE_LAST[account_key] = time.time()


def forget_openai_probe(account_key_or_email: str) -> None:
    """账户删除时清 probe 节流桶。"""
    if not account_key_or_email:
        return
    key = account_key_or_email
    email = key.split(":", 1)[1] if ":" in key else key
    with _openai_probe_lock:
        _OPENAI_PROBE_LAST.pop(email, None)
        _OPENAI_PROBE_LAST.pop(key, None)


async def fetch_usage(account_key: str) -> dict:
    """统一 usage 拉取门面。按 provider 分派到具体实现：

      - Claude / Anthropic: 调 /api/oauth/usage（零 token 成本，JSON body）
      - OpenAI (Codex)    : 复用 OpenAIOAuthChannel.probe_usage 发最小探测
                            请求拉响应头，内部已写入 state_db；再反查一次
                            quota_load 把 flat dict 返回（保持与 Claude 的
                            返回形状一致）

    返回：与 Anthropic 原生 `/oauth/usage` JSON 结构兼容的 dict（顶层含
    five_hour / seven_day / ...）。OpenAI 路径下返回一个**合成结构**，
    让上层 extract_utils_percent / flatten_usage 能无差别消费。
    """
    provider = provider_of(account_key)

    if provider != "openai":
        # Claude 路径：直接走 /api/oauth/usage
        access_token = await ensure_valid_token(account_key)
        return await asyncio.to_thread(_usage_sync, access_token)

    # OpenAI 路径：通过 channel.probe_usage 拉响应头
    if _openai_probe_should_skip(account_key):
        row = state_db.quota_load(account_key) or {}
        return _synthesize_openai_usage_from_row(row)

    from .channel import registry
    ch = registry.get_channel(f"oauth:{account_key}")
    if ch is None:
        # 渠道未注册（比如账号刚加还没 rebuild）→ 直接抛，调用方可跳过
        raise RuntimeError(f"openai channel not registered: {account_key}")

    # 延迟 import 避免循环依赖
    from .channel.openai_oauth_channel import OpenAIOAuthChannel
    if not isinstance(ch, OpenAIOAuthChannel):
        raise RuntimeError(
            f"account {account_key} resolved to wrong channel type: {type(ch).__name__}"
        )

    result = await ch.probe_usage()
    _openai_probe_mark(account_key)
    if not result.get("ok"):
        raise RuntimeError(f"openai probe failed: {result.get('reason')}")

    # probe_usage 已写入 state_db，反查组装成 Anthropic 风格 dict
    row = state_db.quota_load(account_key) or {}
    return _synthesize_openai_usage_from_row(row)


def _synthesize_openai_usage_from_row(row: dict) -> dict:
    """把 OpenAI codex snapshot 行映射到 Anthropic 风格 usage dict。

    让 extract_utils_percent / latest_reset_iso / flatten_usage 可以统一消费。
    OpenAI 无 sonnet/opus/extra 维度，对应字段为 None。util 从 0..100 反推 0..100
    百分比（flatten_usage 会原样写回）。
    """
    def _block(util_pct, reset):
        # util_pct 是 0..100 百分比；flatten_usage 的 _util_pct 会直接透传
        return {"utilization": util_pct, "resets_at": reset} if util_pct is not None else None

    return {
        "five_hour": _block(row.get("five_hour_util"), row.get("five_hour_reset")) or {},
        "seven_day": _block(row.get("seven_day_util"), row.get("seven_day_reset")) or {},
        "seven_day_sonnet": {},
        "seven_day_opus": {},
        "extra_usage": {"is_enabled": False},
    }


# ─── 按访问节流刷新 usage ────────────────────────────────────────
#
# 场景：quotaMonitor.enabled=False 时，后台轮询不跑，UI 读到的都是旧缓存。
# 打开状态总览 / OAuth 面板 / 详情时按需刷一次，同一 email 节流窗口内跳过。
# 真实 HTTP 限 5 秒；超时/出错不抛，调用方照常读旧缓存。

_QUOTA_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def _quota_refresh_lock(account_key: str) -> asyncio.Lock:
    lk = _QUOTA_REFRESH_LOCKS.get(account_key)
    if lk is None:
        lk = asyncio.Lock()
        _QUOTA_REFRESH_LOCKS[account_key] = lk
    return lk


def _access_refresh_throttle_seconds() -> int:
    qm = config.get().get("quotaMonitor") or {}
    try:
        return int(qm.get("accessRefreshThrottleSeconds", 180))
    except Exception:
        return 180


def _should_skip_access_refresh() -> bool:
    """quotaMonitor.enabled=True 时由后台循环负责刷新，按访问节流路径直接跳过。"""
    qm = config.get().get("quotaMonitor") or {}
    return bool(qm.get("enabled", False))


async def ensure_quota_fresh(account_key: str, *, timeout_s: float = 5.0) -> bool:
    """若该账号的配额缓存已过节流窗口，触发一次真实 fetch_usage 并回写。

    2026-04-20 统一路径后，OpenAI 账号也走此路径；但 fetch_usage 内部会先看
    响应头被动采样是否足够新鲜，若新鲜则跳过 probe（零成本），否则按
    openaiProbeMinIntervalSeconds 节流。Claude 路径维持原 accessRefreshThrottleSeconds
    行为不变。
    """
    if not account_key:
        return False
    if _should_skip_access_refresh():
        return False

    throttle_s = _access_refresh_throttle_seconds()
    row = state_db.quota_load(account_key)
    if row:
        fetched_at_ms = int(row.get("fetched_at") or 0)
        if fetched_at_ms > 0:
            age_s = (state_db.now_ms() - fetched_at_ms) / 1000.0
            if age_s < throttle_s:
                return False

    lock = _quota_refresh_lock(account_key)
    async with lock:
        row = state_db.quota_load(account_key)
        if row:
            fetched_at_ms = int(row.get("fetched_at") or 0)
            if fetched_at_ms > 0:
                age_s = (state_db.now_ms() - fetched_at_ms) / 1000.0
                if age_s < throttle_s:
                    return False
        try:
            usage = await asyncio.wait_for(fetch_usage(account_key), timeout=timeout_s)
        except asyncio.TimeoutError:
            print(f"[oauth] ensure_quota_fresh timeout ({timeout_s}s): {account_key}")
            return False
        except Exception as exc:
            print(f"[oauth] ensure_quota_fresh failed for {account_key}: {exc}")
            return False
        try:
            state_db.quota_save(account_key, flatten_usage(usage),
                                email=account_key_to_email(account_key))
        except Exception as exc:
            print(f"[oauth] ensure_quota_fresh save failed for {account_key}: {exc}")
            return False
    return True


async def ensure_quota_fresh_many(account_keys: list[str], *,
                                  timeout_s: float = 5.0) -> dict[str, bool]:
    """并发对多个账号触发节流刷新。单个超时/失败不影响其他。"""
    if not account_keys:
        return {}
    coros = [ensure_quota_fresh(k, timeout_s=timeout_s) for k in account_keys]
    results = await asyncio.gather(*coros, return_exceptions=True)
    out: dict[str, bool] = {}
    for k, res in zip(account_keys, results):
        out[k] = bool(res) if not isinstance(res, Exception) else False
    return out


def ensure_quota_fresh_sync(account_keys: list[str] | str, *,
                            timeout_s: float = 5.0) -> None:
    """同步包装：TG bot polling 线程用。吞所有异常。"""
    try:
        if isinstance(account_keys, str):
            asyncio.run(ensure_quota_fresh(account_keys, timeout_s=timeout_s))
        else:
            asyncio.run(ensure_quota_fresh_many(account_keys, timeout_s=timeout_s))
    except Exception as exc:
        print(f"[oauth] ensure_quota_fresh_sync error: {exc}")


# ─── 配额缓存辅助 ─────────────────────────────────────────────────

def flatten_usage(usage: dict) -> dict:
    """把 /api/oauth/usage 返回的嵌套结构展平，便于写 state_db.oauth_quota_cache。

    ⚠ 单位约定（2026-04-20 二次修复，对齐 sub2api 产线实现）：

      Anthropic 两条 usage 通道单位不同：
        • `/api/oauth/usage` JSON body（本函数处理的路径）：utilization 已是 0..100 百分比
          （例：5.0 表示 5%、1.0 表示 1%、65.2 表示 65.2%）
        • 响应头 `anthropic-ratelimit-unified-5h/7d-utilization`（本项目暂未接入）：
          0..1 小数，需 × 100 转百分比

      参考 sub2api `backend/internal/service/account_usage_service.go::buildUsageInfo`
      （line 1208: `Utilization: resp.FiveHour.Utilization` 直接透传），确认主动拉
      的 JSON body 单位就是百分比。

      历史上 Parrot 做了 "v <= 1.0 → v*100" 的启发式单位探测，遇到用户实际用量 1%
      （上游返回 1.0）会被误判成 100%。现在改为直接透传，与 sub2api 一致。
    """
    def _util_pct(obj) -> float | None:
        if not obj or obj.get("utilization") is None:
            return None
        # Anthropic /api/oauth/usage 已是 0..100 百分比，直接透传（对齐 sub2api）
        return float(obj["utilization"])

    fh = usage.get("five_hour") or {}
    sd = usage.get("seven_day") or {}
    sds = usage.get("seven_day_sonnet") or {}
    sdo = usage.get("seven_day_opus") or {}
    extra = usage.get("extra_usage") or {}

    return {
        "fetched_at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "five_hour_util": _util_pct(fh),
        "five_hour_reset": fh.get("resets_at"),
        "seven_day_util": _util_pct(sd),
        "seven_day_reset": sd.get("resets_at"),
        "sonnet_util": _util_pct(sds),
        "sonnet_reset": sds.get("resets_at"),
        "opus_util": _util_pct(sdo),
        "opus_reset": sdo.get("resets_at"),
        "extra_used": float(extra.get("used_credits", 0) or 0),
        "extra_limit": float(extra.get("monthly_limit", 0) or 0),
        "extra_util": float(extra.get("utilization", 0) or 0),
        "raw_data": json.dumps(usage, ensure_ascii=False),
    }


def extract_utils_percent(usage: dict) -> list[float | None]:
    """返回 [five_hour, seven_day, sonnet, opus] 的百分比（None 表示该指标缺失）。"""
    flat = flatten_usage(usage)
    return [
        flat["five_hour_util"],
        flat["seven_day_util"],
        flat["sonnet_util"],
        flat["opus_util"],
    ]


def latest_reset_iso(usage: dict) -> str | None:
    """各时间窗 resets_at 中最大的那个（作为 disabled_until 的保守值）。"""
    candidates: list[datetime] = []
    for key in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"):
        obj = usage.get(key) or {}
        dt = _parse_iso(obj.get("resets_at"))
        if dt is not None:
            candidates.append(dt)
    if not candidates:
        return None
    latest = max(candidates)
    return _format_utc(latest.astimezone(timezone.utc))


# ─── 账户增删改 ───────────────────────────────────────────────────

def migrate_provider_field() -> int:
    """给所有没有 provider 字段的账户回填默认值（claude）。

    幂等：已填过的账户不动；无变更时不触发 config.update（避免对磁盘做无
    意义的 rewrite，也不会触发 registry 的 reload callback 重建 channels）。
    启动时调用一次即可。返回本次回填数量。
    """
    cfg = config.get()
    pending: list[int] = [
        i for i, acc in enumerate(cfg.get("oauthAccounts", []))
        if not acc.get("provider")
    ]
    if not pending:
        return 0

    def mutate(c):
        accounts = c.get("oauthAccounts", [])
        for i in pending:
            if 0 <= i < len(accounts) and not accounts[i].get("provider"):
                accounts[i]["provider"] = _DEFAULT_PROVIDER

    config.update(mutate)
    return len(pending)


def bootstrap_composite_key_migration() -> dict:
    """启动时调用，幂等执行 state.db 的联合主键迁移。

    依赖：`migrate_provider_field()` 已经跑过（保证每条 account 都有 provider）。
    行为：按当前 config 构建 email→account_key 映射，委托 state_db 执行。
    """
    email_to_key: dict[str, str] = {}
    for acc in config.get().get("oauthAccounts", []):
        email = acc.get("email")
        if not email:
            continue
        # 旧数据唯一约束：email 唯一。所以 email_to_key 不会发生冲突。
        email_to_key[email] = _account_key(acc)
    return state_db.run_composite_key_migration(email_to_key)


def add_account(entry: dict) -> None:
    """entry 需至少含 email / access_token / refresh_token。

    支持可选字段：
      - provider: "claude" (默认) / "openai"
      - id_token / chatgpt_account_id / organization_id / plan_type  (OpenAI 专属)
    """
    required = ("email", "access_token", "refresh_token")
    missing = [k for k in required if not entry.get(k)]
    if missing:
        raise ValueError(f"missing required fields: {missing}")

    email = entry["email"]
    provider = _normalize_provider(entry.get("provider"))
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"unsupported provider: {entry.get('provider')!r}")

    def mutate(cfg):
        accounts = cfg.setdefault("oauthAccounts", [])
        for a in accounts:
            if a.get("email") != email:
                continue
            a_prov = _normalize_provider(a.get("provider") or _DEFAULT_PROVIDER)
            if a_prov == provider:
                raise ValueError(
                    f"account already exists: provider={provider} email={email}"
                )
        # 规范化字段
        normalized = {
            "email": email,
            "provider": provider,
            "access_token": entry["access_token"],
            "refresh_token": entry["refresh_token"],
            "expired": entry.get("expired", ""),
            "last_refresh": entry.get("last_refresh", _format_utc(datetime.now(timezone.utc))),
            "type": entry.get("type", "claude"),
            "enabled": entry.get("enabled", True),
            "disabled_reason": entry.get("disabled_reason"),
            "disabled_until": entry.get("disabled_until"),
            "models": entry.get("models") or [],
        }
        # OpenAI 专属字段（缺失时保持空串，渲染端按需展示）
        if provider == "openai":
            normalized["id_token"] = entry.get("id_token", "") or ""
            normalized["chatgpt_account_id"] = entry.get("chatgpt_account_id", "") or ""
            normalized["organization_id"] = entry.get("organization_id", "") or ""
            normalized["plan_type"] = entry.get("plan_type", "") or ""
        accounts.append(normalized)

    config.update(mutate)


def delete_account(account_key: str) -> None:
    """按 account_key 精确删除一个账号 + 级联清理。

    兼容：若入参是裸 email（老 API），按 email 删除（可能删掉多条同邮箱的老数据）。
    """
    has_prov = ":" in account_key
    target_provider, target_email = _split_ak(account_key)

    def mutate(cfg):
        accounts = cfg.get("oauthAccounts", [])
        def _keep(a):
            if a.get("email") != target_email:
                return True
            if not has_prov:
                return False  # 老 API：按 email 删除（同邮箱可能多条，统一删）
            return _normalize_provider(a.get("provider") or _DEFAULT_PROVIDER) != target_provider
        cfg["oauthAccounts"] = [a for a in accounts if _keep(a)]
    config.update(mutate)

    # state.db 级联清理
    ch_key = f"oauth:{account_key}"
    state_db.perf_delete(ch_key)
    state_db.error_delete(ch_key)
    state_db.affinity_delete_by_channel(ch_key)
    state_db.quota_delete(account_key)

    # failover 的响应头 snapshot 节流桶（Codex + Anthropic 都清）
    try:
        from . import failover
        failover.forget_codex_snapshot(account_key)
        failover.forget_anthropic_snapshot(account_key)
    except Exception:
        pass
    # OpenAI probe 节流桶（fetch_usage 统一路径后新增）
    forget_openai_probe(account_key)


def set_enabled(account_key: str, enabled: bool, reason: str | None = None,
                disabled_until: str | None = None) -> None:
    has_prov = ":" in account_key
    target_provider, target_email = _split_ak(account_key)

    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if acc.get("email") != target_email:
                continue
            if has_prov:
                acc_prov = _normalize_provider(acc.get("provider") or _DEFAULT_PROVIDER)
                if acc_prov != target_provider:
                    continue
            acc["enabled"] = enabled
            if enabled:
                acc["disabled_reason"] = None
                acc["disabled_until"] = None
            else:
                acc["disabled_reason"] = reason or "user"
                acc["disabled_until"] = disabled_until
            return
    config.update(mutate)


def set_disabled_by_quota(account_key: str, resets_at: str | None) -> None:
    set_enabled(account_key, False, reason="quota", disabled_until=resets_at)


def update_models(account_key: str, models: list[str]) -> None:
    has_prov = ":" in account_key
    target_provider, target_email = _split_ak(account_key)

    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if acc.get("email") != target_email:
                continue
            if has_prov:
                acc_prov = _normalize_provider(acc.get("provider") or _DEFAULT_PROVIDER)
                if acc_prov != target_provider:
                    continue
            acc["models"] = list(models)
            return
    config.update(mutate)


# ─── PKCE 登录 ───────────────────────────────────────────────────

def pkce_generate() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge)。code_challenge 使用 S256。"""
    verifier_bytes = secrets.token_bytes(32)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode()
    challenge_hash = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_hash).rstrip(b"=").decode()
    return code_verifier, code_challenge


def build_login_url(code_challenge: str, state: str) -> str:
    from urllib.parse import urlencode
    params = {
        "code": "true",
        "client_id": OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": OAUTH_MANUAL_REDIRECT,
        "scope": OAUTH_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, code_verifier: str, state: str) -> dict:
    """用 authorization code 换 token（返回原始 token 响应）。"""
    if mock_mode_enabled():
        return {
            "access_token": "mock-access-" + secrets.token_hex(8),
            "refresh_token": "mock-refresh-" + secrets.token_hex(8),
            "expires_in": 28800,
        }
    resp = httpx.post(
        OAUTH_TOKEN_URL,
        json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_MANUAL_REDIRECT,
            "client_id": OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
            "state": state,
        },
        headers={"Content-Type": "application/json", "User-Agent": CLI_USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ─── 后台循环的 "once" 单步实现 ─────────────────────────────────

def _build_refresh_notice(account_key: str, usage_flat: dict | None) -> str:
    """构造 OAuth Token 刷新成功通知文案（中文 + HTML + 北京时间 + 用量摘要）。"""
    email = account_key_to_email(account_key)
    prov = provider_of(account_key)
    prov_tag = "🅾 OpenAI" if prov == "openai" else "🅰 Claude"
    new_exp = (get_account(account_key) or {}).get("expired")
    parts = [
        "✅ <b>OAuth Token 已刷新</b>",
        f"账号: <code>{notifier.escape_html(email)}</code> · {prov_tag}",
        f"新过期时间: <code>{_to_bjt(new_exp)}</code>"
        f" (剩 {_remaining_str(new_exp)})",
    ]
    # 用量
    if prov == "openai":
        parts.append("📊 用量: <i>由响应头更新（无独立端点）</i>")
    elif usage_flat:
        fh_util = usage_flat.get("five_hour_util")
        sd_util = usage_flat.get("seven_day_util")
        if fh_util is not None:
            fh_reset = usage_flat.get("five_hour_reset")
            parts.append(
                f"📊 5h 用量: <b>{fh_util:.0f}%</b>"
                f" | 重置: <code>{_to_bjt(fh_reset)}</code>"
            )
        if sd_util is not None:
            sd_reset = usage_flat.get("seven_day_reset")
            parts.append(
                f"📊 7d 用量: <b>{sd_util:.0f}%</b>"
                f" | 重置: <code>{_to_bjt(sd_reset)}</code>"
            )
        if fh_util is None and sd_util is None:
            parts.append("📊 用量: <i>本次未拉取到</i>")
    else:
        parts.append("📊 用量: <i>获取失败（不影响 token 刷新）</i>")

    # 月度统计
    try:
        from . import log_db
        month_start = (
            datetime.now(_BJT_TZ)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        ts = log_db.tokens_for_channel(f"oauth:{account_key}", since_ts=month_start)
        if ts and ts["total"] > 0:
            prompt = ts["input"] + ts["cache_creation"] + ts["cache_read"]
            cache_rate = (ts["cache_read"] / prompt * 100) if prompt > 0 else 0
            parts.append(
                f"💎 月度统计: ↑ {_fmt_tokens(prompt)} ↓ {_fmt_tokens(ts['output'])}"
                f" · 缓存率 {cache_rate:.2f}%"
            )
    except Exception as exc:
        print(f"[oauth] monthly stats lookup failed: {exc}")
    return "\n".join(parts)


def _fmt_tokens(n: int) -> str:
    """简单 token 数格式化：1234567 → 1.23M / 1234 → 1.2K。"""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


async def proactive_refresh_once(refresh_threshold_seconds: int = 600) -> dict:
    """遍历所有 enabled 账户，若剩余 < 阈值（默认 10min）则刷新。

    返回 {email: outcome} 字典（outcome: "skipped" / "refreshed" / "failed:<reason>"）。
    """
    out: dict[str, str] = {}
    for acc in list_accounts()[:]:
        email = acc.get("email")
        if not email:
            continue
        ak = _account_key(acc)
        disp = email  # 通知里仍用 email 作人类可读
        if not acc.get("enabled", True):
            out[email] = "skipped:disabled"
            continue
        if acc.get("disabled_reason") in ("user", "auth_error"):
            out[email] = f"skipped:{acc['disabled_reason']}"
            continue

        expired = _parse_iso(acc.get("expired"))
        if expired is None:
            out[email] = "skipped:no_expired"
            continue

        remaining = (expired - datetime.now(timezone.utc)).total_seconds()
        if remaining >= refresh_threshold_seconds:
            out[email] = "skipped:healthy"
            continue

        try:
            await force_refresh(ak)
            out[email] = "refreshed"
            usage_flat: dict | None = None
            try:
                usage = await fetch_usage(ak)
                usage_flat = flatten_usage(usage)
                # 统一用 quota_save 写入；OpenAI 路径下主动拉/probe 产生的行
                # 会保留 codex_* 字段（quota_save INSERT OR REPLACE 时会覆盖，
                # 但 probe_usage 已经先写好了完整行 + 我们这里再次写 five_hour_util
                # / seven_day_util 是同值，语义一致）。
                state_db.quota_save(ak, usage_flat, email=email)
            except Exception as exc:
                print(f"[oauth] usage fetch after refresh failed for {ak}: {exc}")

            notifier.notify_event(
                "oauth_refreshed",
                _build_refresh_notice(ak, usage_flat),
                auto_delete_seconds=180,
            )
        except Exception as exc:
            out[email] = f"failed:{exc}"
            try:
                set_enabled(ak, False, reason="auth_error")
            except Exception:
                pass
            notifier.notify_event(
                "oauth_refresh_failed",
                "⚠ <b>OAuth Token 刷新失败</b>\n"
                f"账号: <code>{notifier.escape_html(disp)}</code>\n"
                f"原因: <code>{notifier.escape_html(str(exc))}</code>\n"
                "账号已被自动禁用 (auth_error)。请到「🔐 管理 OAuth」重新登录或粘贴新 JSON。"
            )
    return out


async def quota_monitor_once() -> dict:
    """遍历所有账户，按 usage 判断是否需要按配额禁用/恢复。

    返回 {email: outcome}。outcome 可能是：
      - "skipped:<reason>"
      - "ok:<util1,util2...>"
      - "disabled_quota:<resets>"
      - "resumed"
      - "fetch_failed:<reason>"
    """
    cfg = config.get()
    monitor_cfg = cfg.get("quotaMonitor") or {}
    threshold = float(monitor_cfg.get("disableThresholdPercent", 95))

    out: dict[str, str] = {}
    for acc in list_accounts()[:]:
        email = acc.get("email")
        if not email:
            continue
        ak = _account_key(acc)
        if acc.get("disabled_reason") in ("user", "auth_error"):
            out[email] = f"skipped:{acc['disabled_reason']}"
            continue

        try:
            usage = await fetch_usage(ak)
        except Exception as exc:
            out[email] = f"fetch_failed:{exc}"
            continue

        state_db.quota_save(ak, flatten_usage(usage), email=email)

        utils = extract_utils_percent(usage)
        any_over = any(u is not None and u >= threshold for u in utils)

        if any_over:
            if acc.get("disabled_reason") == "quota":
                out[email] = "still_over_quota"
                continue
            latest_reset = latest_reset_iso(usage)
            set_disabled_by_quota(ak, latest_reset)
            out[email] = f"disabled_quota:{latest_reset}"
            notifier.notify_event(
                "quota_disabled",
                "⚠ <b>OAuth 配额已用尽，账号被自动禁用</b>\n"
                f"账号: <code>{notifier.escape_html(email)}</code>\n"
                f"重置时间: <code>{_to_bjt(latest_reset) if latest_reset else 'unknown'}</code>\n"
                "达到该时间且各项指标 &lt; 阈值后会自动恢复。"
            )
        else:
            if acc.get("disabled_reason") == "quota":
                du = _parse_iso(acc.get("disabled_until"))
                if du is not None and du > datetime.now(timezone.utc):
                    out[email] = "quota_pending_reset"
                    continue
                set_enabled(ak, True)
                out[email] = "resumed"
                notifier.notify_event(
                    "quota_resumed",
                    "✅ <b>OAuth 配额已恢复，账号重新启用</b>\n"
                    f"账号: <code>{notifier.escape_html(email)}</code>",
                )
            else:
                parts = [f"{u:.0f}%" if u is not None else "-" for u in utils]
                out[email] = f"ok:{','.join(parts)}"
    return out


async def proactive_refresh_loop() -> None:
    """后台任务：初次等 30s，之后每 60s 触发一次 refresh_once。"""
    await asyncio.sleep(30)
    while True:
        try:
            await proactive_refresh_once()
        except Exception as exc:
            print(f"[oauth_manager] proactive_refresh_once error: {exc}")
        await asyncio.sleep(60)


async def quota_monitor_loop() -> None:
    """后台任务：初次等 45s（避开 refresh 第一轮）；之后按配置间隔。"""
    await asyncio.sleep(45)
    while True:
        cfg = config.get()
        monitor_cfg = cfg.get("quotaMonitor") or {}
        if not monitor_cfg.get("enabled", True):
            await asyncio.sleep(60)
            continue
        try:
            await quota_monitor_once()
        except Exception as exc:
            print(f"[oauth_manager] quota_monitor_once error: {exc}")
        await asyncio.sleep(int(monitor_cfg.get("intervalSeconds", 60)))
