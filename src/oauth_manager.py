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

from . import cache_display, config, load_balancing, network, notifier, oauth_errors, state_db
from .oauth import (
    DEFAULT_PROVIDER as _DEFAULT_PROVIDER,
    VALID_PROVIDERS as _VALID_PROVIDERS,
    normalize_provider as _normalize_provider,
)
from .oauth_ids import (
    account_key as _account_key,
    openai_workspace_id as _openai_workspace_id,
    split_account_key as _split_ak,
)
from .oauth import openai as openai_provider
from .transform.cc_mimicry import CLI_USER_AGENT


# ─── 常量 ────────────────────────────────────────────────────────

OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
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


class AmbiguousOAuthAccountKey(ValueError):
    """Legacy email key matches more than one logical OAuth account."""


def _acc_provider(acc: dict) -> str:
    return _normalize_provider(acc.get("provider") or _DEFAULT_PROVIDER)


def _is_openai_acc(acc: dict) -> bool:
    return _acc_provider(acc) == "openai"


def _canonical_key(acc: dict) -> str:
    return _account_key(acc)


def _matching_accounts_for_key(key: str) -> list[dict]:
    """Return config accounts matching either a canonical key or a legacy key.

    OpenAI canonical keys are `openai:<email>:<workspace_id>`.
    Compatibility also accepts legacy `openai:<email>`, historical
    `openai:<workspace_id>`/`openai:<chatgpt_account_id>`, and the short-lived
    `openai:<email>:<workspace_id>:<chatgpt_account_id>` trial keys, returning
    all matches so callers can reject ambiguous cases.
    """
    if not key:
        return []
    accounts = list(config.get().get("oauthAccounts", []))
    if ":" in key:
        provider, identity = _split_ak(key)
        exact = [
            acc for acc in accounts
            if _acc_provider(acc) == provider and _canonical_key(acc) == key
        ]
        if exact:
            return exact
        if provider == "openai":
            parts = identity.split(":")
            if len(parts) >= 2:
                email, workspace_id = parts[0], parts[1]
                chatgpt_account_id = ":".join(parts[2:]) if len(parts) >= 3 else ""
                return [
                    acc for acc in accounts
                    if _is_openai_acc(acc)
                    and str(acc.get("email") or "") == email
                    and (not workspace_id or _openai_workspace_id(acc) == workspace_id)
                    and (not chatgpt_account_id or str(acc.get("chatgpt_account_id") or acc.get("workspace_id") or "") == chatgpt_account_id)
                ]
            return [
                acc for acc in accounts
                if _is_openai_acc(acc)
                and (
                    acc.get("email") == identity
                    or _openai_workspace_id(acc) == identity
                    or str(acc.get("chatgpt_account_id") or "") == identity
                    or str(acc.get("workspace_id") or "") == identity
                )
            ]
        return [
            acc for acc in accounts
            if _acc_provider(acc) == provider and acc.get("email") == identity
        ]
    return [acc for acc in accounts if acc.get("email") == key]


def resolve_account_key(value: str | None) -> str | None:
    """Resolve canonical account_key from canonical or legacy input.

    - Canonical keys return themselves.
    - Legacy `openai:<email>` resolves only when exactly one OpenAI account has
      that email.
    - Bare email resolves only when exactly one OAuth account has that email.
    - Ambiguous legacy input raises instead of silently selecting an account.
    """
    if value is None:
        return None
    raw = str(value or "")
    if not raw:
        return raw

    matches = _matching_accounts_for_key(raw)
    if not matches:
        return raw
    canonical = {_canonical_key(acc) for acc in matches}
    if len(canonical) == 1:
        return next(iter(canonical))
    raise AmbiguousOAuthAccountKey(
        f"ambiguous OAuth account key {raw!r}; matches {len(matches)} accounts"
    )


def _resolve_existing_account_key(value: str) -> str | None:
    try:
        key = resolve_account_key(value)
    except AmbiguousOAuthAccountKey:
        raise
    if key is None:
        return None
    for acc in config.get().get("oauthAccounts", []):
        if _canonical_key(acc) == key:
            return key
    return None


def _resolve_existing_account_key_or_raise(value: str) -> str:
    key = _resolve_existing_account_key(value)
    if key is None:
        raise ValueError(f"unknown OAuth account: {value}")
    return key


def get_account(account_key: str) -> dict | None:
    """按 canonical account_key 精确匹配账户。

    历史上本函数按 email 查找；同邮箱下 Claude + OpenAI 共存后必须联合键。
    兼容期内：
      - `openai:<email>` 只有在该 email 下恰好一个 OpenAI 工作区时才回退
      - 裸 email 只有在恰好一个账户匹配时才回退
    匹配不唯一时返回 None，避免静默选错工作区。
    """
    try:
        canonical = resolve_account_key(account_key)
    except AmbiguousOAuthAccountKey:
        return None
    if not canonical:
        return None
    for acc in config.get().get("oauthAccounts", []):
        if _canonical_key(acc) == canonical:
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
    try:
        acc = get_account(account_key)
    except AmbiguousOAuthAccountKey:
        acc = None
    if acc is not None:
        return str(acc.get("email") or "")
    provider, identity = _split_ak(account_key)
    if provider == "openai" and ":" in identity:
        return identity.split(":", 1)[0]
    return identity


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
        return _acc_provider(key_or_account)
    if isinstance(key_or_account, str) and ":" in key_or_account:
        try:
            acc = get_account(key_or_account)
        except AmbiguousOAuthAccountKey:
            acc = None
        if acc is not None:
            return _acc_provider(acc)
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

    若刷新返回了新的 OpenAI workspace/account id，则需要把相关运行时状态和
    priorityOrders 从旧 account_key 一并改名到新 account_key。真实 OpenAI 一般
    不会换 identity；此路径主要用于账号/团队切换后的安全收敛。

    若该账号此前因 `auth_error` 被自动禁用，刷新成功视为身份恢复：
    同时清掉 disabled_reason / disabled_until 并把 enabled 重新置 True。
    """
    canonical = _resolve_existing_account_key(account_key)
    target_key = canonical or account_key
    old_acc = get_account(target_key)
    target_email = account_key_to_email(target_key)
    old_key = _canonical_key(old_acc) if old_acc else (canonical or account_key)
    new_key = old_key
    if old_acc:
        preview = dict(old_acc)
        preview.update(new)
        new_key = _canonical_key(preview)
        target_email = str(preview.get("email") or target_email)

    if old_key and new_key and old_key != new_key:
        try:
            state_db.rename_oauth_identity(old_key, new_key, email=target_email)
        except Exception as exc:
            print(f"[oauth] state rename failed {old_key} -> {new_key}: {exc}")
        try:
            load_balancing.sync_channel_renamed(
                f"oauth:{old_key}", f"oauth:{new_key}",
                "openai" if provider_of(old_acc or old_key) == "openai" else "anthropic",
            )
        except Exception as exc:
            print(f"[oauth] priority rename failed {old_key} -> {new_key}: {exc}")

    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if old_acc is not None:
                if _canonical_key(acc) != old_key:
                    continue
            elif canonical:
                if _canonical_key(acc) != canonical:
                    continue
            elif acc.get("email") != target_email:
                continue
            acc.update(new)
            if acc.get("disabled_reason") == "auth_error":
                acc["disabled_reason"] = None
                acc["disabled_until"] = None
                acc["enabled"] = True
            break
    config.update(mutate)


def _do_refresh_http(refresh_token: str, scopes: str = "") -> dict:
    """真实请求 Anthropic token endpoint。

    scopes: 账号登录时存下的真实 scope（空格分隔）；缺省回退完整六项 OAUTH_SCOPES。
    源码 QA$() refresh body 明确带 scope 字段（lineage §4.2）。
    """
    resp = network.post_sync(
        OAUTH_TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
            "scope": scopes or OAUTH_SCOPES,
        },
        headers={
            "Content-Type": "application/json",
            "User-Agent": CLI_USER_AGENT,
        },
        timeout=30,
        proxy_purpose="oauth_anthropic",
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
    # ⛔ 双实例重构期硬禁用刷新：副本与线上共享同一批 OAuth 账号，
    # 任一方刷新会轮换 access_token/refresh_token，击穿线上 token 导致小夕 401。
    # 三条刷新入口（proactive_refresh_loop / ensure_valid_token / force_refresh）
    # 最终都汇到这里，单点拦截即全堵。返回现有 token，绝不发刷新请求。
    if os.environ.get("PARROT_NO_REFRESH") == "1":
        _acc = get_account(_resolve_existing_account_key_or_raise(account_key))
        if _acc and _acc.get("access_token"):
            return _acc["access_token"]
        raise RuntimeError(
            "refresh disabled in dual-instance rebuild mode (PARROT_NO_REFRESH=1)"
        )
    account_key = _resolve_existing_account_key_or_raise(account_key)
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
                workspace_id=acc.get("workspace_id") or acc.get("chatgpt_account_id") or None,
                org_id=acc.get("organization_id") or None,
            )
        elif mock_mode_enabled():
            data = _do_refresh_mock(acc["refresh_token"])
        else:
            data = _do_refresh_http(acc["refresh_token"], acc.get("scopes", ""))

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
        # §9-3：refresh 响应带新 scope 时回写（源码 QA$() refresh 响应返 scope）
        if data.get("scope"):
            new_fields["scopes"] = data["scope"]
        # OpenAI: 刷新响应若带 id_token 同步更新；解码拿出最新 metadata
        # （plan_type / chatgpt_account_id / organization_id 都可能随账户升级
        # 或换组织而变）。email 理论上不变，不覆盖以免生成孤儿 entry。
        # refresh_sync 还会 best-effort 调 accounts/check，返回 plan_type /
        # subscription_expires_at；这些字段即使 id_token 解析失败也可以写回。
        if provider == "openai":
            if data.get("id_token"):
                new_fields["id_token"] = data["id_token"]
                try:
                    claims = openai_provider.decode_id_token(data["id_token"])
                    info = openai_provider.extract_user_info(claims)
                    for k in (
                        "chatgpt_account_id", "workspace_id", "workspace_name",
                        "workspace_type", "organization_id", "plan_type",
                    ):
                        v = info.get(k)
                        if not v:   # 空值不覆盖已有字段
                            continue
                        if k in ("chatgpt_account_id", "workspace_id"):
                            old_identity = _openai_workspace_id(acc)
                            # OpenAI refresh_token 绑定一个 ChatGPT workspace/account。
                            # 如果上游/mock 返回了不同 identity，不要原地改主键，
                            # 否则会把现有 account_key 重命名到另一个 workspace。
                            if old_identity and str(v) != old_identity:
                                continue
                        new_fields[k] = v
                except Exception as exc:
                    print(f"[oauth] openai refresh: id_token decode failed for {email}: {exc}")
            if data.get("plan_type"):
                new_fields["plan_type"] = data["plan_type"]
            if data.get("subscription_expires_at"):
                new_fields["subscription_expires_at"] = data["subscription_expires_at"]
            for k in (
                "workspace_id", "workspace_name", "workspace_type",
                "organization_id",
            ):
                if not data.get(k):
                    continue
                if k == "workspace_id":
                    old_identity = _openai_workspace_id(acc)
                    if old_identity and str(data[k]) != old_identity:
                        continue
                new_fields[k] = data[k]

        _save_token_fields(account_key, new_fields)
        return new_fields["access_token"]


async def ensure_valid_token(account_key: str) -> str:
    """调用方：OAuthChannel.build_upstream_request。

    返回可用的 access_token。剩余 ≥ 5min 直接返回缓存；否则在线程中持锁刷新。
    同一 account_key 的并发请求由 threading.Lock 串行（跨 event loop 安全）。
    """
    account_key = _resolve_existing_account_key_or_raise(account_key)
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
    resp = network.get_sync(
        OAUTH_PROFILE_URL,
        headers={
            # §14.1：CC v2.1.156 调 profile 只带 Bearer + json，不带 beta/UA（源码实证）
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        timeout=15,
        proxy_purpose="oauth_anthropic",
    )
    resp.raise_for_status()
    return resp.json()


def _usage_sync(access_token: str) -> dict:
    """调 Anthropic /api/oauth/usage 拿 usage 数据。

    请求头与 sub2api 的 claudeUsageService.FetchUsageWithOptions 对齐（2026-04-20）：
      - Accept / Content-Type / anthropic-beta 与用户抓包一致
      - User-Agent 跟随 CC 版本（v2.1.156）
      - timeout 30s（sub2api 产线验证值）
    """
    if mock_mode_enabled():
        return _mock_usage()
    resp = network.get_sync(
        OAUTH_USAGE_URL,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.156",
        },
        timeout=30,
        proxy_purpose="oauth_anthropic",
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
    try:
        account_key = _resolve_existing_account_key_or_raise(account_key)
    except Exception:
        return True
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
    try:
        account_key = resolve_account_key(account_key) or account_key
    except AmbiguousOAuthAccountKey:
        pass
    with _openai_probe_lock:
        _OPENAI_PROBE_LAST[account_key] = time.time()


def forget_openai_probe(account_key_or_email: str) -> None:
    """账户删除时清 probe 节流桶。"""
    if not account_key_or_email:
        return
    key = account_key_or_email
    try:
        email = account_key_to_email(key)
    except Exception:
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
    account_key = _resolve_existing_account_key_or_raise(account_key)
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
    try:
        account_key = _resolve_existing_account_key_or_raise(account_key)
    except Exception as exc:
        print(f"[oauth] ensure_quota_fresh unknown account {account_key}: {exc}")
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
        # 访问路径刷新到新鲜用量后，也要按 quotaMonitor.disableThresholdPercent
        # 立即收敛状态；否则 UI 会显示已超当前配置阈值但账户仍保持 enabled，
        # 直到后台轮询下一轮才禁用。
        try:
            evaluate_and_toggle_by_usage(account_key, usage)
        except Exception as exc:
            print(f"[oauth] ensure_quota_fresh evaluate failed for {account_key}: {exc}")
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
    """各时间窗 resets_at 中最大的那个（向后兼容旧调用方）。

    ⚠ 建议新代码使用 `reset_iso_for_hit_windows(usage, threshold)`，它只
    考虑**实际撞到限额的窗口**的 reset 时间，避免「只有 5h 撞了却锁到 7d」的
    不合理情况。
    """
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


def reset_iso_for_hit_windows(usage: dict, threshold: float) -> str | None:
    """按「撞哪个窗口锁哪个窗口」的原则计算 disabled_until。

    只在 util >= threshold 的窗口里取 resets_at，然后取 max（保守：等所有
    撞到的窗口都过去才恢复；但**不会**因为 5h 撞了而锁到 7d）。

    入参 usage：Anthropic 风格 JSON（`flatten_usage` 消费的那种），对 OpenAI
    合成结构（只有 five_hour / seven_day）同样适用。
    """
    candidates: list[datetime] = []
    for key in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"):
        obj = usage.get(key) or {}
        util = obj.get("utilization")
        if util is None:
            continue
        try:
            if float(util) < threshold:
                continue
        except (TypeError, ValueError):
            continue
        dt = _parse_iso(obj.get("resets_at"))
        if dt is not None:
            candidates.append(dt)
    if not candidates:
        return None
    latest = max(candidates)
    return _format_utc(latest.astimezone(timezone.utc))




def usage_from_quota_row(row: dict) -> dict:
    """Build a usage-shaped dict from oauth_quota_cache row.

    This is intentionally display/cache oriented: it lets UI paths that already
    show a cached value over the current quotaMonitor.disableThresholdPercent
    apply the same quota-disable rule without waiting for the next monitor loop.
    """
    def _block(util, reset):
        return {"utilization": util, "resets_at": reset} if util is not None else {}

    return {
        "five_hour": _block(row.get("five_hour_util"), row.get("five_hour_reset")),
        "seven_day": _block(row.get("seven_day_util"), row.get("seven_day_reset")),
        "seven_day_sonnet": _block(row.get("sonnet_util"), row.get("sonnet_reset")),
        "seven_day_opus": _block(row.get("opus_util"), row.get("opus_reset")),
        "extra_usage": {
            "is_enabled": bool(row.get("extra_limit")),
            "used_credits": row.get("extra_used") or 0,
            "monthly_limit": row.get("extra_limit") or 0,
            "utilization": row.get("extra_util") or 0,
        },
    }


def evaluate_and_toggle_by_cached_quota(account_key: str,
                                        *, threshold: float | None = None) -> dict:
    """Disable account from cached quota data when it is already over threshold.

    This is deliberately disable-only for below-threshold cache rows: cached data
    may be stale, so automatic resume remains the job of quota_monitor/manual
    refresh after fetching fresh usage. Over-threshold cache rows are still safe
    to disable immediately because the UI is already showing that value.
    """
    row = state_db.quota_load(account_key)
    if not row:
        return {"action": "noop_no_cache", "utils": [], "any_over": False,
                "hit_windows": [], "disabled_until": None}
    if threshold is None:
        qm = config.get().get("quotaMonitor") or {}
        try:
            threshold = float(qm.get("disableThresholdPercent", 95))
        except Exception:
            threshold = 95.0
    usage = usage_from_quota_row(row)
    utils = extract_utils_percent(usage)
    if not any(u is not None and u >= threshold for u in utils):
        return {"action": "cached_below_threshold", "utils": utils,
                "any_over": False, "hit_windows": [],
                "disabled_until": None}
    return evaluate_and_toggle_by_usage(account_key, usage, threshold=threshold)

def evaluate_and_toggle_by_usage(account_key: str, usage: dict,
                                 *, threshold: float | None = None) -> dict:
    """核心策略：拿到新鲜 usage 后评估禁用/恢复，并执行状态切换。

    规则：
      • disabled_reason in ("user", "auth_error") → 完全不碰（手动禁用永远不自动恢复）
      • 任一窗口 util ≥ threshold → 需要禁用
          - 账号已是 quota 禁用：保持不动（不刷新 disabled_until，避免目标移动）
          - 账号未禁用：set_disabled_by_quota，disabled_until = 撞到窗口的最大 reset
      • 所有窗口 util < threshold → 可用
          - 账号是 quota 禁用：set_enabled(True) 自动恢复（忽略 disabled_until，
            因为 usage 本身已是真实状态）
          - 账号未禁用：无事发生

    返回: {
      "action": "noop_user"|"noop_auth_error"|"disabled"|"still_over_quota"|
                "resumed"|"kept_enabled"|"disable_failed"|"resume_failed"|"noop_missing",
      "utils": [5h, 7d, sonnet, opus],   # None 表示该指标缺失
      "any_over": bool,
      "hit_windows": ["5h", "7d", ...],   # util ≥ threshold 的窗口标签
      "disabled_until": str|None,
    }
    """
    if threshold is None:
        qm = config.get().get("quotaMonitor") or {}
        try:
            threshold = float(qm.get("disableThresholdPercent", 95))
        except Exception:
            threshold = 95.0

    utils = extract_utils_percent(usage)
    window_labels = ["5h", "7d", "sonnet", "opus"]
    hit_windows = [lbl for lbl, u in zip(window_labels, utils)
                   if u is not None and u >= threshold]
    any_over = bool(hit_windows)

    canonical = _resolve_existing_account_key(account_key)
    if canonical:
        account_key = canonical
    acc = get_account(account_key)
    if acc is None:
        return {"action": "noop_missing", "utils": utils, "any_over": any_over,
                "hit_windows": hit_windows, "disabled_until": None}
    reason = acc.get("disabled_reason")
    if reason in ("user", "auth_error"):
        return {"action": f"noop_{reason}", "utils": utils, "any_over": any_over,
                "hit_windows": hit_windows,
                "disabled_until": acc.get("disabled_until")}

    if any_over:
        if reason == "quota":
            return {"action": "still_over_quota", "utils": utils,
                    "any_over": True, "hit_windows": hit_windows,
                    "disabled_until": acc.get("disabled_until")}
        latest_reset = reset_iso_for_hit_windows(usage, threshold)
        try:
            set_disabled_by_quota(account_key, latest_reset)
        except Exception as exc:
            print(f"[oauth] evaluate set_disabled_by_quota failed for {account_key}: {exc}")
            return {"action": "disable_failed", "utils": utils,
                    "any_over": True, "hit_windows": hit_windows,
                    "disabled_until": None}
        return {"action": "disabled", "utils": utils, "any_over": True,
                "hit_windows": hit_windows, "disabled_until": latest_reset}

    # 全部窗口都可用
    if reason == "quota":
        try:
            set_enabled(account_key, True)
        except Exception as exc:
            print(f"[oauth] evaluate set_enabled failed for {account_key}: {exc}")
            return {"action": "resume_failed", "utils": utils,
                    "any_over": False, "hit_windows": [],
                    "disabled_until": acc.get("disabled_until")}
        return {"action": "resumed", "utils": utils, "any_over": False,
                "hit_windows": [], "disabled_until": None}
    return {"action": "kept_enabled", "utils": utils, "any_over": False,
            "hit_windows": [], "disabled_until": None}


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


def _openai_trial_or_composite_legacy_keys(acc: dict) -> list[str]:
    """Legacy OpenAI keys that safely point to exactly this account.

    Includes:
      - old email key: openai:<email>
      - temporary workspace key: openai:<workspace_id>
      - short-lived trial key: openai:<email>:<workspace_id>:<chatgpt_account_id>
    """
    email = str(acc.get("email") or "")
    workspace_id = _openai_workspace_id(acc)
    chatgpt_account_id = str(acc.get("chatgpt_account_id") or workspace_id or "")
    keys: list[str] = []
    if email:
        keys.append(f"openai:{email}")
    if workspace_id:
        keys.append(f"openai:{workspace_id}")
    if email and workspace_id and chatgpt_account_id:
        keys.append(f"openai:{email}:{workspace_id}:{chatgpt_account_id}")
    return keys


def _unique_openai_legacy_key_mapping(accounts: list[dict]) -> dict[str, dict[str, str]]:
    """Build safe OpenAI legacy-key → canonical composite-key mappings.

    Migrates old `openai:<email>`, temporary workspace-only
    `openai:<workspace_id>`, and short-lived trial
    `openai:<email>:<workspace_id>:<chatgpt_account_id>` keys when they map to
    exactly one current account. Ambiguous legacy inputs are deliberately left in
    place for state/log tables; priority lists can expand them separately.
    """
    candidates: dict[str, list[dict]] = {}
    for acc in accounts:
        if not _is_openai_acc(acc):
            continue
        for key in _openai_trial_or_composite_legacy_keys(acc):
            candidates.setdefault(key, []).append(acc)

    mapping: dict[str, dict[str, str]] = {}
    for old_key, items in candidates.items():
        canonical_keys = {_account_key(acc) for acc in items}
        if len(items) != 1 or len(canonical_keys) != 1:
            continue
        new_key = next(iter(canonical_keys))
        if old_key == new_key:
            continue
        acc = items[0]
        mapping[old_key] = {"new": new_key, "email": str(acc.get("email") or "")}
    return mapping


def _migrate_openai_quota_rows_by_email(accounts: list[dict]) -> dict:
    """Migrate leftover quota rows when the row email uniquely identifies account.

    This is intentionally quota-only: logs/state channel rows with workspace-only
    keys can be historical/ambiguous, but a quota row carries its own email and
    can therefore be mapped to email+workspace safely when that account exists.
    """
    stats = {"rows": 0, "errors": []}
    by_pair = {
        (str(acc.get("email") or ""), _openai_workspace_id(acc)): _account_key(acc)
        for acc in accounts if _is_openai_acc(acc)
    }
    try:
        for row in state_db.quota_load_all():
            old_key = str(row.get("account_key") or "")
            if not old_key.startswith("openai:"):
                continue
            if old_key in by_pair.values():
                continue
            email = str(row.get("email") or "")
            _, identity = _split_ak(old_key)
            # Only workspace-only rows are repaired here. Email-only rows are
            # handled by normal mapping when unambiguous.
            if ":" in identity or not email:
                continue
            new_key = by_pair.get((email, identity))
            if not new_key or new_key == old_key:
                continue
            try:
                stats["rows"] += state_db.quota_rename_account_key(old_key, new_key, email=email)
            except Exception as exc:
                stats["errors"].append(f"{old_key}->{new_key}: {exc}")
    except Exception as exc:
        stats["errors"].append(str(exc))
    return stats


def _openai_priority_expansion_mapping(accounts: list[dict]) -> dict[str, list[str]]:
    """Map legacy priority channel keys to one or more canonical channel keys."""
    by_key: dict[str, list[str]] = {}
    for acc in accounts:
        if not _is_openai_acc(acc):
            continue
        canonical = f"oauth:{_account_key(acc)}"
        for key in _openai_trial_or_composite_legacy_keys(acc):
            ch_key = f"oauth:{key}"
            if ch_key == canonical:
                continue
            by_key.setdefault(ch_key, []).append(canonical)
    out: dict[str, list[str]] = {}
    for old, vals in by_key.items():
        dedup: list[str] = []
        seen: set[str] = set()
        for v in vals:
            if v not in seen:
                dedup.append(v); seen.add(v)
        if dedup:
            out[old] = dedup
    return out


def _openai_workspace_mapping_scope_key(mapping: dict[str, dict[str, str]]) -> str:
    """Stable schema_meta key for the exact set of safe OpenAI mappings."""
    payload = json.dumps(mapping, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"openai_workspace_key_version:{digest}"


def bootstrap_openai_workspace_key_migration() -> dict:
    """Startup-safe migration from OpenAI legacy keys to composite keys.

    Only unique OpenAI legacy mappings are migrated. If the same email or
    workspace id maps to multiple accounts, old rows are intentionally left in
    place so no refresh or stats view silently picks the wrong workspace/account.
    """
    accounts = list(config.get().get("oauthAccounts", []))
    mapping = _unique_openai_legacy_key_mapping(accounts)
    channel_mapping = {
        f"oauth:{old}": f"oauth:{meta['new']}"
        for old, meta in mapping.items()
        if meta.get("new")
    }
    priority_expansion = _openai_priority_expansion_mapping(accounts)

    config_stats = {"accounts_patched": 0, "priority_entries": 0, "image_disabled_entries": 0}

    def mutate(c):
        for acc in c.get("oauthAccounts", []):
            if not _is_openai_acc(acc):
                continue
            if not acc.get("workspace_id") and acc.get("chatgpt_account_id"):
                acc["workspace_id"] = acc.get("chatgpt_account_id")
                config_stats["accounts_patched"] += 1

        lb = c.setdefault("loadBalancing", {})
        po = lb.setdefault("priorityOrders", {})
        for fam in ("anthropic", "openai"):
            arr = list(po.get(fam) or [])
            new_arr: list[str] = []
            seen: set[str] = set()
            for key in arr:
                expanded = priority_expansion.get(key)
                if expanded is None:
                    expanded = [channel_mapping.get(key, key)]
                for candidate_key in expanded:
                    new_key = str(candidate_key or "")
                    if new_key and new_key not in seen:
                        new_arr.append(new_key)
                        seen.add(new_key)
                if expanded != [key]:
                    config_stats["priority_entries"] += 1
            po[fam] = new_arr

        images = c.setdefault("images", {})
        disabled = list(images.get("disabledAccounts") or [])
        new_disabled: list[str] = []
        seen_disabled: set[str] = set()
        for value in disabled:
            raw = str(value or "")
            new_value = raw
            if raw in mapping:
                new_value = mapping[raw]["new"]
            elif raw.startswith("oauth:") and raw in channel_mapping:
                new_value = channel_mapping[raw]
            elif raw.startswith("openai:") and raw in mapping:
                new_value = mapping[raw]["new"]
            elif raw:
                maybe = f"openai:{raw}"
                if maybe in mapping:
                    new_value = mapping[maybe]["new"]
            if new_value and new_value not in seen_disabled:
                new_disabled.append(new_value)
                seen_disabled.add(new_value)
            if new_value != raw:
                config_stats["image_disabled_entries"] += 1
        images["disabledAccounts"] = new_disabled

    config.update(mutate)

    state_scope_key = _openai_workspace_mapping_scope_key(mapping) if mapping else None
    state_stats = state_db.run_openai_workspace_key_migration(mapping, scope_key=state_scope_key)
    quota_email_stats = _migrate_openai_quota_rows_by_email(accounts)
    log_stats: dict = {}
    image_stats: dict = {}
    try:
        from . import log_db
        log_stats = log_db.migrate_channel_keys(channel_mapping)
    except Exception as exc:
        log_stats = {"error": str(exc)}
    try:
        from . import image_db
        image_stats = image_db.migrate_account_keys(mapping)
    except Exception as exc:
        image_stats = {"error": str(exc)}

    return {
        "mapping_count": len(mapping),
        "mapping": mapping,
        "channel_mapping": channel_mapping,
        "config": config_stats,
        "state": state_stats,
        "quota_email": quota_email_stats,
        "logs": log_stats,
        "images": image_stats,
    }


def _openai_metadata_patch(entry: dict) -> dict:
    patch: dict[str, Any] = {
        "id_token": entry.get("id_token", "") or "",
        "chatgpt_account_id": entry.get("chatgpt_account_id", "") or "",
        "workspace_id": entry.get("workspace_id") or entry.get("chatgpt_account_id", "") or "",
        "workspace_name": entry.get("workspace_name", "") or "",
        "workspace_type": entry.get("workspace_type", "") or "",
        "organization_id": entry.get("organization_id", "") or "",
        "plan_type": entry.get("plan_type", "") or "",
        "subscription_expires_at": entry.get("subscription_expires_at", "") or "",
    }
    return patch


def add_account(entry: dict) -> None:
    """entry 需至少含 email / access_token / refresh_token。

    支持可选字段：
      - provider: "claude" (默认) / "openai"
      - id_token / chatgpt_account_id / workspace_id / workspace_name /
        workspace_type / organization_id / plan_type / subscription_expires_at
        (OpenAI 专属)
    """
    required = ("email", "access_token", "refresh_token")
    missing = [k for k in required if not entry.get(k)]
    if missing:
        raise ValueError(f"missing required fields: {missing}")

    email = entry["email"]
    provider = _normalize_provider(entry.get("provider"))
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"unsupported provider: {entry.get('provider')!r}")

    # 规范化字段
    normalized = {
        "email": email,
        "provider": provider,
        "access_token": entry["access_token"],
        "refresh_token": entry["refresh_token"],
        "expired": entry.get("expired", ""),
        "last_refresh": entry.get("last_refresh", _format_utc(datetime.now(timezone.utc))),
        "type": entry.get("type", "openai" if provider == "openai" else "claude"),
        "enabled": entry.get("enabled", True),
        "disabled_reason": entry.get("disabled_reason"),
        "disabled_until": entry.get("disabled_until"),
        "models": entry.get("models") or [],
        # §9-1：存登录响应的 scope（空格分隔），供 refresh 时带真实 scope；
        # 老账号缺省空串，refresh 时回退完整六项 OAUTH_SCOPES。
        "scopes": entry.get("scopes", "") or "",
    }
    # OpenAI 专属字段（缺失时保持空串，渲染端按需展示）
    if provider == "openai":
        normalized.update(_openai_metadata_patch(entry))

    normalized_key = _account_key(normalized)
    added = {"v": False}
    existing_target = None
    for a in config.get().get("oauthAccounts", []):
        if provider == "openai":
            if _is_openai_acc(a) and _canonical_key(a) == normalized_key:
                existing_target = a
                break
        elif a.get("email") == email and _acc_provider(a) == provider:
            existing_target = a
            break
    rename_old_key = _canonical_key(existing_target) if existing_target else ""
    rename_new_key = normalized_key if rename_old_key and rename_old_key != normalized_key else ""
    if rename_new_key:
        try:
            state_db.rename_oauth_identity(rename_old_key, rename_new_key, email=email)
        except Exception as exc:
            print(f"[oauth] pre-rename state failed {rename_old_key} -> {rename_new_key}: {exc}")

    def mutate(cfg):
        accounts = cfg.setdefault("oauthAccounts", [])
        target: dict | None = None
        if provider == "openai":
            for a in accounts:
                if not _is_openai_acc(a):
                    continue
                if _canonical_key(a) == normalized_key:
                    target = a
                    break
        else:
            for a in accounts:
                if a.get("email") == email and _acc_provider(a) == provider:
                    target = a
                    break

        if target is not None:
            if provider != "openai":
                raise ValueError(
                    f"account already exists: provider={provider} email={email}"
                )
            keep_models = target.get("models")
            keep_max = target.get("maxConcurrent")
            target.update(normalized)
            if keep_models is not None and not entry.get("models"):
                target["models"] = keep_models
            if keep_max is not None and "maxConcurrent" not in entry:
                target["maxConcurrent"] = keep_max
            if rename_new_key:
                lb = cfg.setdefault("loadBalancing", {})
                po = lb.setdefault("priorityOrders", {})
                fam = "openai" if provider == "openai" else "anthropic"
                for f in ("anthropic", "openai"):
                    arr = list(po.get(f) or [])
                    new_arr: list[str] = []
                    seen: set[str] = set()
                    for key in arr:
                        if f == fam and key == f"oauth:{rename_old_key}":
                            key = f"oauth:{rename_new_key}"
                        elif key in (f"oauth:{rename_old_key}", f"oauth:{rename_new_key}"):
                            if f != fam:
                                continue
                        if key not in seen:
                            new_arr.append(key)
                            seen.add(key)
                    po[f] = new_arr
            return
        accounts.append(normalized)
        added["v"] = True

    config.update(mutate)
    if added["v"]:
        load_balancing.sync_channel_added(
            f"oauth:{normalized_key}",
            "openai" if provider == "openai" else "anthropic",
        )


def delete_account(account_key: str) -> None:
    """按 account_key 精确删除一个账号 + 级联清理。

    兼容：若入参是裸 email（老 API），按 email 删除（可能删掉多条同邮箱的老数据）。
    """
    canonical = _resolve_existing_account_key(account_key)
    has_prov = ":" in account_key
    target_provider, target_identity = _split_ak(account_key)

    def mutate(cfg):
        accounts = cfg.get("oauthAccounts", [])
        def _keep(a):
            if canonical:
                return _canonical_key(a) != canonical
            if a.get("email") != target_identity:
                return True
            if not has_prov:
                return False  # 老 API：按 email 删除（同邮箱可能多条，统一删）
            return _acc_provider(a) != target_provider
        cfg["oauthAccounts"] = [a for a in accounts if _keep(a)]
    config.update(mutate)

    # state.db 级联清理
    cleanup_key = canonical or account_key
    ch_key = f"oauth:{cleanup_key}"
    load_balancing.sync_channel_removed(ch_key)
    state_db.perf_delete(ch_key)
    state_db.error_delete(ch_key)
    # 走内存 + state.db 双清接口，避免只清硬盘留下内存脏亲和。
    from . import affinity as _affinity
    _affinity.delete_by_channel(ch_key)
    try:
        _affinity.client_delete_by_channel(ch_key)
    except Exception:
        pass
    state_db.quota_delete(cleanup_key)

    # failover 的响应头 snapshot 节流桶（Codex + Anthropic 都清）
    try:
        from . import failover
        failover.forget_codex_snapshot(cleanup_key)
        failover.forget_anthropic_snapshot(cleanup_key)
    except Exception:
        pass
    # OpenAI probe 节流桶（fetch_usage 统一路径后新增）
    forget_openai_probe(cleanup_key)


def set_enabled(account_key: str, enabled: bool, reason: str | None = None,
                disabled_until: str | None = None) -> None:
    canonical = _resolve_existing_account_key(account_key)
    has_prov = ":" in account_key
    target_provider, target_identity = _split_ak(account_key)

    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if canonical:
                if _canonical_key(acc) != canonical:
                    continue
            else:
                if acc.get("email") != target_identity:
                    continue
                if has_prov and _acc_provider(acc) != target_provider:
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
    canonical = _resolve_existing_account_key(account_key)
    has_prov = ":" in account_key
    target_provider, target_identity = _split_ak(account_key)

    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if canonical:
                if _canonical_key(acc) != canonical:
                    continue
            else:
                if acc.get("email") != target_identity:
                    continue
                if has_prov and _acc_provider(acc) != target_provider:
                    continue
            acc["models"] = list(models)
            return
    config.update(mutate)


def update_max_concurrent(account_key: str, max_concurrent: int) -> None:
    """设置 OAuth 账户的并发上限。0 / 负数 → 用全局 defaultMaxConcurrent。"""
    canonical = _resolve_existing_account_key(account_key)
    has_prov = ":" in account_key
    target_provider, target_identity = _split_ak(account_key)
    v = max(0, int(max_concurrent or 0))

    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if canonical:
                if _canonical_key(acc) != canonical:
                    continue
            else:
                if acc.get("email") != target_identity:
                    continue
                if has_prov and _acc_provider(acc) != target_provider:
                    continue
            acc["maxConcurrent"] = v
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
    resp = network.post_sync(
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
        proxy_purpose="oauth_anthropic",
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
            prompt = cache_display.prompt_total(ts["input"], ts["cache_creation"], ts["cache_read"])
            line = f"💎 月度统计: ↑ {cache_display.fmt_tokens(prompt)} · ↓ {cache_display.fmt_tokens(ts['output'])}"
            if (ts.get("cache_read") or 0) > 0:
                line += f" · {cache_display.cache_read_phrase(ts['cache_read'], prompt)}"
            parts.append(line)
    except Exception as exc:
        print(f"[oauth] monthly stats lookup failed: {exc}")
    return "\n".join(parts)


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
            err = oauth_errors.describe_oauth_error(
                exc, provider=provider_of(ak), operation="refresh_token",
            )
            out[email] = f"failed:{err.code}"
            disabled_line = ""
            if err.auth_error:
                try:
                    set_enabled(ak, False, reason="auth_error")
                    disabled_line = "\n账号已被自动禁用 (auth_error)。请到「🔐 管理 OAuth」重新登录或粘贴新 JSON。"
                except Exception:
                    disabled_line = "\n⚠ 自动禁用写入失败，请查看 systemd 日志。"
            else:
                disabled_line = "\n账号未自动禁用；可稍后重试。"
            print(f"[oauth] proactive refresh failed for {ak}: {oauth_errors.technical_detail(exc)}")
            notifier.notify_event(
                "oauth_refresh_failed",
                "⚠ <b>OAuth Token 刷新失败</b>\n"
                f"账号: <code>{notifier.escape_html(disp)}</code>\n"
                + oauth_errors.format_oauth_error_html(err)
                + disabled_line
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

        result = evaluate_and_toggle_by_usage(ak, usage, threshold=threshold)
        utils = result["utils"]
        action = result["action"]

        if action == "disabled":
            latest_reset = result["disabled_until"]
            hit = " / ".join(result["hit_windows"]) or "?"
            out[email] = f"disabled_quota:{latest_reset}"
            notifier.notify_event(
                "quota_disabled",
                "⚠ <b>OAuth 配额已用尽，账号被自动禁用</b>\n"
                f"账号: <code>{notifier.escape_html(email)}</code>\n"
                f"撞到窗口: <code>{hit}</code>\n"
                f"重置时间: <code>{_to_bjt(latest_reset) if latest_reset else 'unknown'}</code>\n"
                "所有撞到窗口恢复后即可自动解禁。"
            )
        elif action == "still_over_quota":
            out[email] = "still_over_quota"
        elif action == "resumed":
            out[email] = "resumed"
            notifier.notify_event(
                "quota_resumed",
                "✅ <b>OAuth 配额已恢复，账号重新启用</b>\n"
                f"账号: <code>{notifier.escape_html(email)}</code>",
            )
        elif action == "kept_enabled":
            parts = [f"{u:.0f}%" if u is not None else "-" for u in utils]
            out[email] = f"ok:{','.join(parts)}"
        else:
            out[email] = action
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
