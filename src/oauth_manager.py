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
import copy
import concurrent.futures
import hashlib
import inspect
import json
import math
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import cache_display, config, load_balancing, network, notifier, oauth_errors, state_db
from . import oauth_model_discovery
from .oauth import (
    DEFAULT_PROVIDER as _DEFAULT_PROVIDER,
    VALID_PROVIDERS as _VALID_PROVIDERS,
    normalize_provider as _normalize_provider,
)
from .oauth_ids import (
    account_key as _account_key,
    antigravity_project_id as _antigravity_project_id,
    cursor_subject as _cursor_subject,
    openai_workspace_id as _openai_workspace_id,
    split_account_key as _split_ak,
    xai_subject as _xai_subject,
)
from .oauth import antigravity as antigravity_provider
from .oauth import cursor as cursor_provider
from .oauth import openai as openai_provider
from .oauth import xai as xai_provider
from .openai.codex_constants import (
    codex_backend_base_url,
    codex_cli_version,
    codex_protocol_profile,
)
from .transform.cc_mimicry import CLI_USER_AGENT


# ─── 常量 ────────────────────────────────────────────────────────

OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_TOKEN_URL_LEGACY = "https://api.anthropic.com/v1/oauth/token"
OAUTH_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BOOTSTRAP_URL = "https://api.anthropic.com/api/claude_cli/bootstrap"

OAUTH_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
OAUTH_MANUAL_REDIRECT = "https://platform.claude.com/oauth/code/callback"
OAUTH_SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)

# OAuth model-catalog maintenance policy.  These are intentionally code-level
# constants: the timings are operational invariants, not user-facing tuning.
OAUTH_MODEL_SYNC_SUCCESS_TTL_SECONDS = 6 * 60 * 60
OAUTH_MODEL_SYNC_FAILURE_RETRY_SECONDS = 15 * 60
OAUTH_MODEL_SYNC_CHECK_INTERVAL_SECONDS = 60
OAUTH_MODEL_SYNC_STARTUP_DELAY_SECONDS = 2
OAUTH_MODEL_SYNC_MAX_CONCURRENCY = 3
OAUTH_MODEL_SYNC_REQUEST_TIMEOUT_SECONDS = 45.0
OAUTH_MODEL_SYNC_FOREGROUND_TIMEOUT_SECONDS = 20.0
OAUTH_MODEL_CHANGE_LIST_LIMIT = 10


def _supports_keyword(callable_obj, keyword: str) -> bool | None:
    """Return whether a callable accepts *keyword*; None means unknowable.

    Compatibility is decided before invocation so a TypeError raised inside the
    callable can never trigger a second request. ``inspect.signature`` handles
    Python functions, bound methods, partials and signature-aware mocks.  For
    opaque builtins/extensions we keep the modern call (None) and propagate its
    error rather than risk duplicate side effects.
    """
    try:
        params = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return None
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD or
        (param.name == keyword and param.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for param in params
    )


def _compatible_kwargs(callable_obj, **kwargs) -> dict:
    """Drop only keywords that an inspectable legacy callable cannot accept."""
    return {
        key: value for key, value in kwargs.items()
        if _supports_keyword(callable_obj, key) is not False
    }


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
    return _normalize_provider(acc.get("provider") or acc.get("type") or _DEFAULT_PROVIDER)


def _is_openai_acc(acc: dict) -> bool:
    return _acc_provider(acc) == "openai"


def _is_xai_acc(acc: dict) -> bool:
    return _acc_provider(acc) == "xai"


def _is_cursor_acc(acc: dict) -> bool:
    return _acc_provider(acc) == "cursor"


def _is_antigravity_acc(acc: dict) -> bool:
    return _acc_provider(acc) == "antigravity"


def _is_openai_family_provider(provider: str) -> bool:
    return provider in ("openai", "xai", "cursor", "antigravity")


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
        if provider == "cursor":
            return [
                acc for acc in accounts
                if _is_cursor_acc(acc)
                and (
                    _canonical_key(acc) == key
                    or _cursor_subject(acc) == identity
                    or str(acc.get("email") or "") == identity
                )
            ]
        if provider == "xai":
            parts = identity.split(":")
            if len(parts) >= 2:
                email, subject = parts[0], ":".join(parts[1:])
                return [
                    acc for acc in accounts
                    if _is_xai_acc(acc)
                    and str(acc.get("email") or "") == email
                    and (not subject or _xai_subject(acc) == subject)
                ]
            return [
                acc for acc in accounts
                if _is_xai_acc(acc)
                and (
                    acc.get("email") == identity
                    or _xai_subject(acc) == identity
                )
            ]
        if provider == "antigravity":
            parts = identity.split(":", 1)
            if len(parts) == 2:
                email, project_id = parts[0], parts[1]
                return [
                    acc for acc in accounts
                    if _is_antigravity_acc(acc)
                    and str(acc.get("email") or "") == email
                    and _antigravity_project_id(acc) == project_id
                ]
            return [
                acc for acc in accounts
                if _is_antigravity_acc(acc)
                and str(acc.get("email") or "") == identity
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
        if _acc_provider(acc) == "cursor":
            return str(acc.get("label") or acc.get("email") or "")
        return str(acc.get("email") or "")
    provider, identity = _split_ak(account_key)
    if provider in ("openai", "xai", "antigravity") and ":" in identity:
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
    """按账户拿到 provider（"claude" / "openai" / "xai"）。

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


def _rename_priority_orders_in_config(cfg: dict, old_channel_key: str,
                                      new_channel_key: str, family: str) -> None:
    """Rename one key in unified/model orders and legacy family compatibility."""
    load_balancing.mutate_channel_renamed(
        cfg, old_channel_key, new_channel_key, family,
    )


def _rename_runtime_oauth_identity(old_account_key: str, new_account_key: str, *,
                                   config_mutator, rollback_mutator,
                                   email: str | None = None) -> None:
    """Atomically coordinate config publication with all mirrored state."""
    if not old_account_key or not new_account_key or old_account_key == new_account_key:
        config.update(config_mutator)
        return
    from . import channel_state

    old_channel_key = f"oauth:{old_account_key}"
    new_channel_key = f"oauth:{new_account_key}"
    channel_state.rename_with_config(
        old_channel_key=old_channel_key,
        new_channel_key=new_channel_key,
        old_account_key=old_account_key,
        new_account_key=new_account_key,
        email=email,
        config_mutator=config_mutator,
        rollback_mutator=rollback_mutator,
    )


def _save_token_fields(account_key: str, new: dict) -> bool:
    """Persist one refresh result only if its exact account generation survives.

    A refresh may finish after the account was renamed or deleted.  The
    process-lifetime channel generation map is authoritative here: a rename
    may forward the result to its exact destination, while a deleted or
    otherwise missing generation must drop it.  Never fall back to matching a
    bare email, because Claude and OpenAI accounts may legitimately share one.
    """
    from . import channel_state

    with config.serialized_updates(), channel_state.mutation_lock:
        return _save_token_fields_serialized(account_key, new)


def _save_token_fields_serialized(account_key: str, new: dict) -> bool:
    """把刷新后的 token 字段写回 config.oauthAccounts（按 account_key 精确匹配）。

    若刷新返回了新的 OpenAI workspace/account id，则需要把相关运行时状态和
    priorityOrders 从旧 account_key 一并改名到新 account_key。真实 OpenAI 一般
    不会换 identity；此路径主要用于账号/团队切换后的安全收敛。

    若该账号此前因 `auth_error` 被自动禁用，刷新成功视为身份恢复：
    同时清掉 disabled_reason / disabled_until 并把 enabled 重新置 True。
    """
    from . import channel_state

    # Production callers always pass a canonical key.  Keep the unambiguous
    # legacy bare-email helper behavior for tests/admin paths, but resolve it
    # before entering the generation check and never use email for mutation.
    source_account_key = account_key
    if ":" not in source_account_key:
        try:
            source_account_key = _resolve_existing_account_key(source_account_key) or ""
        except AmbiguousOAuthAccountKey:
            return False
        if not source_account_key:
            return False

    source_channel_key = f"oauth:{source_account_key}"
    target_channel_key = channel_state.resolve(source_channel_key)
    if (
        channel_state.is_deleted(source_channel_key)
        or channel_state.is_deleted(target_channel_key)
        or not target_channel_key.startswith("oauth:")
    ):
        return False
    target_key = target_channel_key[len("oauth:"):]

    old_acc = next(
        (
            account for account in config.get().get("oauthAccounts", [])
            if _canonical_key(account) == target_key
        ),
        None,
    )
    if old_acc is None:
        return False

    source_provider, _source_identity = _split_ak(source_account_key)
    if _acc_provider(old_acc) != _normalize_provider(source_provider):
        return False

    old_acc_snapshot = copy.deepcopy(old_acc) if old_acc is not None else None
    old_load_balancing = copy.deepcopy(
        config.get().get("loadBalancing", {})
    )
    target_email = str(old_acc.get("email") or account_key_to_email(target_key))
    old_key = _canonical_key(old_acc)
    new_key = old_key
    rename_family = "anthropic"
    if old_acc:
        preview = dict(old_acc)
        preview.update(new)
        new_key = _canonical_key(preview)
        target_email = str(preview.get("email") or target_email)
        rename_family = (
            "openai" if _is_openai_family_provider(provider_of(preview))
            else "anthropic"
        )

    if old_key != new_key:
        from . import channel_state
        channel_state.assert_reusable(f"oauth:{new_key}")
        for account in config.get().get("oauthAccounts", []):
            candidate = _canonical_key(account)
            if candidate == new_key and candidate != old_key:
                raise ValueError(f"OAuth account identity already exists: {new_key}")

    def mutate(cfg):
        updated = False
        for acc in cfg.get("oauthAccounts", []):
            if _canonical_key(acc) != old_key:
                continue
            acc.update(new)
            if _acc_provider(acc) == "openai":
                from .openai.codex_device_fingerprint import normalize_account_device
                provider_cfg = cfg.get("openaiOAuth") or {}
                normalize_account_device(
                    acc,
                    protocol_profile=codex_protocol_profile(provider_cfg).profile_id,
                )
            if acc.get("disabled_reason") == "auth_error":
                acc["disabled_reason"] = None
                acc["disabled_until"] = None
                acc["enabled"] = True
            updated = True
            break
        if updated and old_key != new_key:
            _rename_priority_orders_in_config(
                cfg, f"oauth:{old_key}", f"oauth:{new_key}", rename_family,
            )

    def rollback(cfg):
        if old_acc_snapshot is not None:
            accounts = cfg.get("oauthAccounts", [])
            for index, acc in enumerate(accounts):
                if _canonical_key(acc) == new_key:
                    accounts[index] = copy.deepcopy(old_acc_snapshot)
                    break
        cfg["loadBalancing"] = copy.deepcopy(old_load_balancing)

    if old_key and new_key and old_key != new_key:
        _rename_runtime_oauth_identity(
            old_key,
            new_key,
            email=target_email,
            config_mutator=mutate,
            rollback_mutator=rollback,
        )
    else:
        config.update(mutate)
    if _acc_provider(old_acc) == "openai":
        from .openai.codex_identity import register_account_identity
        refreshed = get_account(new_key)
        if refreshed is None:
            raise RuntimeError("refreshed OpenAI account disappeared before identity registration")
        register_account_identity(refreshed)
    return True


def _post_refresh_candidate(url: str, refresh_token: str, *, scope: str | None,
                            proxy_channel: str = "") -> dict:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }
    if scope:
        body["scope"] = scope
    resp = network.post_sync(
        url,
        json=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": CLI_USER_AGENT,
        },
        timeout=30,
        proxy_purpose="oauth_anthropic",
        proxy_channel=proxy_channel,
    )
    resp.raise_for_status()
    return resp.json()


def _raise_last_refresh_error(errors: list[BaseException]) -> None:
    if not errors:
        raise RuntimeError("Claude OAuth refresh failed without recorded error")
    # 如果所有候选都明确是 invalid_grant / invalid_token / revoked / expired，
    # 才把最后一个错误交给上层标 auth_error。否则优先抛一个非 auth_error 的
    # 协议兼容失败，避免 400 invalid_request / invalid_scope 误禁用账号。
    classified = [
        oauth_errors.describe_oauth_error(
            exc, provider="claude", operation="refresh_token"
        )
        for exc in errors
    ]
    invalid_errors = [exc for exc, err in zip(errors, classified) if err.auth_error]
    if len(invalid_errors) == len(errors):
        raise errors[-1]
    # 网络/超时类保留原异常，避免兼容层包装后丢失 claude_oauth_network_error
    # / claude_oauth_timeout 的可重试语义。
    if classified and all(err.code in {"claude_oauth_network_error", "claude_oauth_timeout"} for err in classified):
        raise errors[-1]
    summary = [f"{err.code}:{err.status or '-'}" for err in classified]
    raise RuntimeError("Claude OAuth refresh compatibility failed: " + ", ".join(summary))


def _do_refresh_http(refresh_token: str, scopes: str = "", *,
                     account_key: str = "") -> dict:
    """Claude OAuth refresh 兼容层。

    旧账号没有登录响应 scope，沿用已验证的 api.anthropic.com + no-scope；
    新账号如果保存了真实 scope，则优先尝试 platform.claude.com + scope。
    任一候选成功即返回；失败候选不写配置、不禁用账号。
    """
    scope = (scopes or "").strip()
    candidates: list[tuple[str, str, str | None]] = []
    if scope:
        candidates.append(("platform+scope", OAUTH_TOKEN_URL, scope))
        candidates.append(("legacy+scope", OAUTH_TOKEN_URL_LEGACY, scope))
    candidates.append(("legacy-no-scope", OAUTH_TOKEN_URL_LEGACY, None))
    candidates.append(("platform-no-scope", OAUTH_TOKEN_URL, None))

    errors: list[BaseException] = []
    for name, url, cand_scope in candidates:
        try:
            data = _post_refresh_candidate(
                url, refresh_token, scope=cand_scope,
                proxy_channel=f"oauth:{account_key}" if account_key else "",
            )
            if errors:
                print(f"[oauth] Claude refresh fallback succeeded via {name}")
            return data
        except Exception as exc:
            errors.append(exc)
            err = oauth_errors.describe_oauth_error(
                exc, provider="claude", operation="refresh_token"
            )
            print(f"[oauth] Claude refresh candidate {name} failed: {err.code}")
            # 明确 token 本身失效时，继续尝试其它候选通常没意义；但为了兼容
            # 上游把协议错误也包成 400 invalid_grant 的情况，仍让候选链跑完。
            continue

    _raise_last_refresh_error(errors)


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
            if (
                expired
                and (expired - datetime.now(timezone.utc)).total_seconds() >= 300
                and (provider_of(acc) != "openai" or _openai_workspace_id(acc))
            ):
                return acc["access_token"]

        provider = provider_of(acc)
        if provider == "openai":
            data = openai_provider.refresh_sync(
                acc["refresh_token"], email=email, account_key=account_key,
                workspace_id=acc.get("workspace_id") or acc.get("chatgpt_account_id") or None,
                org_id=acc.get("organization_id") or None,
            )
        elif provider == "xai":
            data = xai_provider.refresh_sync(
                acc["refresh_token"],
                token_endpoint=acc.get("token_endpoint") or None,
                email=email,
                subject=_xai_subject(acc) or None,
                account_key=account_key,
            )
        elif provider == "cursor":
            data = cursor_provider.refresh_sync(
                acc["refresh_token"], account_key=account_key,
            )
        elif provider == "antigravity":
            data = antigravity_provider.refresh_sync(
                acc["refresh_token"],
                token_endpoint=acc.get("token_endpoint") or None,
                email=email,
                project_id=_antigravity_project_id(acc) or None,
                account_key=account_key,
            )
        elif mock_mode_enabled():
            data = _do_refresh_mock(acc["refresh_token"])
        else:
            data = _do_refresh_http(
                acc["refresh_token"], acc.get("scopes", ""), account_key=account_key,
            )

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
                if k == "workspace_name":
                    incoming_name = str(data[k] or "").strip()
                    existing_name = str(acc.get("workspace_name") or "").strip()
                    existing_type = str(acc.get("workspace_type") or acc.get("plan_type") or "").lower()
                    # OpenAI accounts/check can report Team workspaces with a
                    # generic display name like "Personal". Do not overwrite a
                    # previously curated Team workspace label (SP/AU/UK/IN/AU2,
                    # etc.) with that generic Personal label during refresh.
                    if (
                        incoming_name.lower() == "personal"
                        and "team" in existing_type
                        and existing_name
                        and existing_name.lower() not in {"personal", "workspace", "team"}
                    ):
                        continue
                new_fields[k] = data[k]

        # xAI: refresh 响应若带 id_token 同步 subject/email 元数据。
        if provider == "xai":
            if data.get("id_token"):
                new_fields["id_token"] = data["id_token"]
                try:
                    claims = xai_provider.decode_id_token(data["id_token"])
                    info = xai_provider.extract_user_info(claims)
                    if info.get("email"):
                        new_fields["email"] = info["email"]
                    if info.get("subject"):
                        new_fields["subject"] = info["subject"]
                        new_fields["sub"] = info["subject"]
                except Exception as exc:
                    print(f"[oauth] xai refresh: id_token decode failed for {email}: {exc}")
            for k in ("subject", "sub", "base_url", "token_endpoint", "redirect_uri"):
                if data.get(k):
                    new_fields[k] = data[k]

        # Antigravity: refresh must never rewrite project_id. The Cloud Code
        # project is part of the canonical account key.
        if provider == "antigravity":
            for k in ("base_url", "token_endpoint", "redirect_uri"):
                if data.get(k):
                    new_fields[k] = data[k]

        # Cursor: subject is the stable account key. Refresh must not silently
        # move an existing account to another identity, but metadata-poor legacy
        # entries may be completed once.
        if provider == "cursor":
            refreshed_subject = cursor_provider.subject_from_access_token(
                new_fields["access_token"]
            )
            if refreshed_subject and not _cursor_subject(acc):
                new_fields["subject"] = refreshed_subject
                new_fields["sub"] = refreshed_subject

        # Claude: refresh 后 best-effort 拉 profile 更新套餐信息
        if provider == "claude":
            try:
                profile = _profile_sync(
                    new_fields["access_token"], account_key=account_key,
                )
                plan_info = extract_claude_plan_info(profile)
                for k, v in plan_info.items():
                    if v not in (None, ""):
                        new_fields[k] = v
            except Exception as exc:
                print(f"[oauth] claude refresh: profile fetch failed for {email}: {exc}")

        if not _save_token_fields(account_key, new_fields):
            print(
                f"[oauth] discarded refresh result for retired generation: "
                f"{account_key}"
            )
        return new_fields["access_token"]


async def ensure_valid_token(account_key: str) -> str:
    """调用方：OAuthChannel.build_upstream_request。

    返回可用的 access_token。剩余 ≥ 5min 且身份完整时返回缓存；否则持锁刷新。
    旧 OpenAI 账号缺 workspace 时也需刷新一次，不能因 token 尚有效而跳过补全。
    同一 account_key 的并发请求由 threading.Lock 串行（跨 event loop 安全）。
    """
    account_key = _resolve_existing_account_key_or_raise(account_key)
    acc = get_account(account_key)
    if acc is None:
        raise ValueError(f"unknown OAuth account: {account_key}")

    expired = _parse_iso(acc.get("expired"))
    if (
        expired
        and (expired - datetime.now(timezone.utc)).total_seconds() >= 300
        and (provider_of(acc) != "openai" or _openai_workspace_id(acc))
    ):
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
        "seven_day_fable": {},
        "extra_usage": {"is_enabled": False, "used_credits": 0, "monthly_limit": 0, "utilization": 0},
    }


def _profile_sync(access_token: str, *, account_key: str = "") -> dict:
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
        proxy_channel=f"oauth:{account_key}" if account_key else "",
    )
    resp.raise_for_status()
    return resp.json()


def _usage_sync(access_token: str, *, account_key: str = "") -> dict:
    """调 Anthropic /api/oauth/usage 拿 usage 数据。"""
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
        proxy_channel=f"oauth:{account_key}" if account_key else "",
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_profile(access_token: str) -> dict:
    return await asyncio.to_thread(_profile_sync, access_token)


def extract_claude_plan_info(profile: dict) -> dict:
    """从 /api/oauth/profile 响应中提取套餐信息，返回可直接 merge 到 account entry 的 dict。"""
    org = profile.get("organization") or {}
    return {
        "plan_type": org.get("organization_type") or "",
        "rate_limit_tier": org.get("rate_limit_tier") or "",
        "billing_type": org.get("billing_type") or "",
        "subscription_status": org.get("subscription_status") or "",
        "subscription_created_at": org.get("subscription_created_at") or "",
        "has_extra_usage_enabled": bool(org.get("has_extra_usage_enabled")),
        "seat_tier": org.get("seat_tier") or "",
    }


def claude_plan_label(acc: dict) -> str:
    """生成人类可读的套餐标签，如 'claude_max (Max 5x)'。"""
    plan = acc.get("plan_type") or ""
    tier = acc.get("rate_limit_tier") or ""
    # rate_limit_tier → 人类可读简称
    _TIER_SHORT = {
        "default_claude_ai": "Free",
        "default_claude_pro": "Pro",
        "default_claude_max_5x": "Max 5x",
        "default_claude_max_20x": "Max 20x",
    }
    short = _TIER_SHORT.get(tier, tier.replace("default_claude_", "").replace("_", " ") if tier else "")
    if plan and short:
        return f"{plan} ({short})"
    return plan or short or ""


def _bootstrap_sync(access_token: str) -> dict:
    """调 /api/claude_cli/bootstrap 拿实时套餐信息。"""
    if mock_mode_enabled():
        return {}
    resp = network.get_sync(
        OAUTH_BOOTSTRAP_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "claude-code/2.1.156",
        },
        timeout=15,
        proxy_purpose="oauth_anthropic",
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_bootstrap(access_token: str) -> dict:
    return await asyncio.to_thread(_bootstrap_sync, access_token)


class QuotaNotSupported(Exception):
    """向后兼容保留：fetch_usage 现按 provider 分派，不再抛出此异常。

    2026-04-20 统一 OAuth 用量机制后，OpenAI 也走 fetch_usage 门面；
    2026-05-30 起 OpenAI 主动 quota 改走 ChatGPT wham/usage。此类仅作为
    类型占位保留，避免外部 `except QuotaNotSupported` 调用链崩溃；不会再真正抛出。
    """


# 兼容旧测试/旧运行时的 OpenAI probe 节流桶。主动 quota 已切到 wham/usage，
# 这里不再由 fetch_usage 写入；删除账号时仍清理，避免老进程残留内存键。
_OPENAI_PROBE_LAST: dict[str, float] = {}
_openai_probe_lock = threading.Lock()


def forget_openai_probe(account_key_or_email: str) -> None:
    """账户删除时清旧 probe 节流桶。"""
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
      - OpenAI (Codex)    : 调 ChatGPT backend-api/wham/usage（零 Codex 请求成本）
      - xAI / Grok        : 调 Grok CLI proxy billing/user/settings（零模型 token 成本）
      - Cursor            : 调 DashboardService/GetCurrentPeriodUsage 等零模型端点

    返回：与 Anthropic 原生 `/oauth/usage` JSON 结构兼容的 dict（顶层含
    five_hour / seven_day / ...），让 extract_utils_percent / latest_reset_iso / flatten_usage
    能无差别消费。
    """
    account_key = _resolve_existing_account_key_or_raise(account_key)
    provider = provider_of(account_key)

    access_token = await ensure_valid_token(account_key)

    if provider == "xai":
        return await xai_provider.fetch_cli_billing_usage(
            access_token, account_key=account_key,
        )

    if provider == "cursor":
        return await cursor_provider.fetch_usage(access_token, account_key=account_key)

    if provider == "antigravity":
        return await antigravity_provider.fetch_usage(
            access_token, account_key=account_key,
        )

    if provider != "openai":
        # Claude 路径：直接走 /api/oauth/usage
        return await asyncio.to_thread(
            _usage_sync, access_token, account_key=account_key,
        )

    # OpenAI 路径：主动 quota 走 ChatGPT 私有 wham/usage。业务响应头里的
    # x-codex-* 仍由 failover/images/ws 实时采样，不在这里发最小 Codex 请求。
    # 注意：这里只通过 ensure_valid_token 在 token 临期/过期时刷新；菜单里的
    # “刷新用量”不应无条件 force_refresh，否则 access_token 仍可调用时也可能
    # 因 refresh_token 被上游轮换/吊销而误报 401。
    acc = get_account(account_key) or {}
    account_id = _openai_workspace_id(acc) or None
    kwargs = _compatible_kwargs(
        openai_provider.fetch_wham_usage,
        account_id=account_id, account_key=account_key,
    )
    return await openai_provider.fetch_wham_usage(access_token, **kwargs)


async def fetch_openai_rate_limit_reset_credits(account_key: str) -> dict:
    """Fetch OpenAI/Codex reset-credit card details for one OAuth account."""
    account_key = _resolve_existing_account_key_or_raise(account_key)
    if provider_of(account_key) != "openai":
        raise ValueError("rate limit reset credits are only available for OpenAI OAuth accounts")
    acc = get_account(account_key) or {}
    access_token = await ensure_valid_token(account_key)
    account_id = _openai_workspace_id(acc) or None
    kwargs = _compatible_kwargs(
        openai_provider.fetch_rate_limit_reset_credits,
        account_id=account_id, account_key=account_key,
    )
    return await openai_provider.fetch_rate_limit_reset_credits(
        access_token, **kwargs,
    )


def attach_openai_reset_credit_details_to_usage(
    usage: dict,
    details: dict | None,
    *,
    sync_available_count: bool = True,
) -> dict:
    """Store OpenAI reset-card details beside the usage summary in raw_data.

    Parrot's quota cache has a single raw_data JSON blob. Keep the WHAM usage
    summary and reset-card detail list in that same blob so UI rendering can be
    cache-only and never has to call the slow detail endpoint synchronously.
    """
    if not isinstance(usage, dict):
        usage = {}
    if not isinstance(details, dict):
        return usage

    out = dict(usage)
    openai = dict(out.get("openai") or {})
    stored_details = dict(details)
    stored_details.setdefault("fetched_at", _format_utc(datetime.now(timezone.utc)))
    openai["rate_limit_reset_credit_details"] = stored_details

    if sync_available_count:
        try:
            count = int(stored_details.get("available_count"))
        except (TypeError, ValueError):
            count = None
        if count is not None:
            summary = dict(openai.get("rate_limit_reset_credits") or {})
            summary["available_count"] = count
            openai["rate_limit_reset_credits"] = summary

    out["openai"] = openai
    return out


def _openai_reset_credit_count_from_usage(usage: dict | None) -> int | None:
    if not isinstance(usage, dict):
        return None
    openai = usage.get("openai")
    summary = openai.get("rate_limit_reset_credits") if isinstance(openai, dict) else None
    if not isinstance(summary, dict):
        summary = usage.get("rate_limit_reset_credits")
    if not isinstance(summary, dict):
        return None
    try:
        return int(summary.get("available_count"))
    except (TypeError, ValueError):
        return None


def _openai_reset_credit_details_from_usage(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    openai = usage.get("openai")
    details = openai.get("rate_limit_reset_credit_details") if isinstance(openai, dict) else None
    if not isinstance(details, dict):
        details = usage.get("rate_limit_reset_credit_details")
    return details if isinstance(details, dict) else None


def _cached_openai_reset_credit_details(account_key: str) -> dict | None:
    try:
        row = state_db.quota_load(account_key) or {}
        raw = json.loads(row.get("raw_data") or "{}")
    except Exception:
        return None
    return _openai_reset_credit_details_from_usage(raw)


def preserve_openai_reset_credit_details(account_key: str, usage: dict) -> dict:
    """Keep the last card list when a summary refresh cannot update details.

    WHAM ``/usage`` only includes ``available_count``. A temporary failure of
    the separate reset-credit endpoint must not let that summary-only response
    erase a previously cached card list. The new usage count remains
    authoritative, so stale details never overwrite it.
    """
    if provider_of(account_key) != "openai" or not isinstance(usage, dict):
        return usage
    if _openai_reset_credit_details_from_usage(usage) is not None:
        return usage
    old_details = _cached_openai_reset_credit_details(account_key)
    if old_details is None:
        return usage
    return attach_openai_reset_credit_details_to_usage(
        usage, old_details, sync_available_count=False,
    )


async def enrich_openai_reset_credit_details(account_key: str, usage: dict) -> dict:
    """Attach a fresh OpenAI reset-card list to one usage snapshot.

    A known zero count needs no second request; storing an explicit empty detail
    snapshot also clears cards that were available in an older refresh.
    """
    account_key = _resolve_existing_account_key_or_raise(account_key)
    if provider_of(account_key) != "openai" or not isinstance(usage, dict):
        return usage
    count = _openai_reset_credit_count_from_usage(usage)
    if count is not None and count <= 0:
        return attach_openai_reset_credit_details_to_usage(
            usage, {"available_count": count, "data": []},
            sync_available_count=False,
        )
    details = await fetch_openai_rate_limit_reset_credits(account_key)
    return attach_openai_reset_credit_details_to_usage(
        usage, details, sync_available_count=(count is None),
    )


async def fetch_usage_snapshot(account_key: str, *,
                               usage_timeout_s: float | None = None,
                               detail_timeout_s: float | None = None) -> dict:
    """Fetch usage plus provider-specific detail data for one cache snapshot.

    OpenAI exposes reset-card rows through a second endpoint. All routine
    refresh paths use this helper so startup/monitor/access refreshes cannot
    overwrite those rows with a summary-only ``/usage`` payload.
    """
    account_key = _resolve_existing_account_key_or_raise(account_key)
    usage_coro = fetch_usage(account_key)
    usage = (
        await asyncio.wait_for(usage_coro, timeout=usage_timeout_s)
        if usage_timeout_s is not None else await usage_coro
    )
    usage = preserve_antigravity_cached_summary(account_key, usage)
    if provider_of(account_key) != "openai":
        return usage

    try:
        detail_coro = enrich_openai_reset_credit_details(account_key, usage)
        return (
            await asyncio.wait_for(detail_coro, timeout=detail_timeout_s)
            if detail_timeout_s is not None else await detail_coro
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[oauth] reset-credit detail refresh failed for {account_key}: {exc}")
        return preserve_openai_reset_credit_details(account_key, usage)


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
        "seven_day_fable": {},
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

    2026-05-30 起，OpenAI 账号主动刷新也走 wham/usage；响应头被动采样
    只作为业务请求的实时补充。Claude 路径维持原 accessRefreshThrottleSeconds
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
            usage = await fetch_usage_snapshot(
                account_key, usage_timeout_s=timeout_s, detail_timeout_s=timeout_s,
            )
        except asyncio.TimeoutError:
            print(f"[oauth] ensure_quota_fresh timeout ({timeout_s}s): {account_key}")
            return False
        except Exception as exc:
            print(f"[oauth] ensure_quota_fresh failed for {account_key}: {exc}")
            return False
        try:
            usage = preserve_antigravity_cached_summary(account_key, usage)
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

def preserve_antigravity_cached_summary(account_key: str, usage: dict) -> dict:
    """On summary-only failure retain old display data, never as fresh signals."""
    if provider_of(account_key) != "antigravity" or not isinstance(usage, dict):
        return usage
    block = usage.get("antigravity")
    if not isinstance(block, dict) or not block.get("quota_error") or block.get("quota_groups"):
        return usage
    row = state_db.quota_load(account_key)
    raw = _raw_usage_from_flat(row) if isinstance(row, dict) else {}
    old = raw.get("antigravity") if isinstance(raw.get("antigravity"), dict) else {}
    groups = old.get("quota_groups")
    if isinstance(groups, list) and groups:
        block["quota_groups"] = groups
        block["quota_groups_stale"] = True
    return usage


def _normalize_quota_model_label(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())


def _is_fable_model_label(value: str) -> bool:
    """Match only explicit Claude Fable / F5 labels and model IDs."""
    label = _normalize_quota_model_label(value)
    if not label:
        return False
    tokens = label.split()
    if "fable" in tokens:
        return True
    compact = "".join(tokens)
    return compact in {"f5", "claudef5", "claudefable", "claudefable5"}


def _fable_scoped_candidates(
    usage: dict | None, *, include_inactive: bool,
) -> tuple[bool, list[tuple[int, float, float, int, dict]]]:
    """Collect ranked Claude Fable / F5 weekly scoped limit windows."""
    data = usage if isinstance(usage, dict) else {}
    candidates: list[tuple[int, float, float, int, dict]] = []
    saw_scoped_fable = False
    limits = data.get("limits")
    if not isinstance(limits, list):
        return False, candidates
    for index, item in enumerate(limits):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in {"weekly_scoped", "seven_day_scoped"}:
            continue
        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        name = str(model.get("display_name") or model.get("id") or "")
        if not _is_fable_model_label(name):
            continue
        saw_scoped_fable = True
        if item.get("is_active") is False and not include_inactive:
            continue
        raw_util = item.get("utilization")
        if raw_util is None:
            raw_util = item.get("percent")
        try:
            utilization = float(raw_util)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(utilization):
            continue
        utilization = max(0.0, min(100.0, utilization))
        reset = item.get("resets_at")
        reset_dt = _parse_iso(reset)
        reset_rank = reset_dt.timestamp() if reset_dt is not None else -1.0
        candidates.append((
            1 if item.get("is_active") is True else 0,
            reset_rank,
            utilization,
            -index,
            {"utilization": utilization, "resets_at": reset},
        ))
    return saw_scoped_fable, candidates


def _pick_fable_candidate(
    candidates: list[tuple[int, float, float, int, dict]],
) -> dict:
    if not candidates:
        return {}
    return max(candidates, key=lambda candidate: candidate[:4])[4]


def fable_usage_block(usage: dict | None) -> dict:
    """Extract the active Claude Fable / F5 weekly quota window.

    Newer /api/oauth/usage payloads keep Sonnet/Opus null and put Fable on
    ``limits[]`` as ``weekly_scoped`` (``scope.model.display_name = Fable``).
    Explicitly inactive entries are historical and must not drive routing or
    model cooldown.  If more than one viable entry is returned, prefer an
    explicitly active entry, then the latest reset, then the highest
    utilization.  Older or mock payloads may still expose ``seven_day_fable``
    as a fallback.  Telegram display uses ``fable_display_block`` so a lone
    inactive scoped window can still be shown.
    """
    data = usage if isinstance(usage, dict) else {}
    saw_scoped_fable, candidates = _fable_scoped_candidates(
        data, include_inactive=False,
    )
    if candidates:
        return _pick_fable_candidate(candidates)
    if saw_scoped_fable:
        return {}

    direct = data.get("seven_day_fable")
    if (
        isinstance(direct, dict)
        and direct.get("is_active") is not False
        and direct.get("utilization") is not None
    ):
        return direct
    return {}


def fable_display_block(usage: dict | None) -> dict:
    """Quota window for Telegram / refresh copy.

    Prefer the live active Fable pool used by routing.  Anthropic currently
    returns the Fable weekly scoped cap with ``is_active: false`` even when it
    is the only Fable window and still has percent + reset; show that instead of
    hiding the row.  Inactive entries never override an active scoped window.
    """
    data = usage if isinstance(usage, dict) else {}
    active = fable_usage_block(data)
    if active:
        return active
    _saw, inactive = _fable_scoped_candidates(data, include_inactive=True)
    if inactive:
        return _pick_fable_candidate(inactive)
    direct = data.get("seven_day_fable")
    if isinstance(direct, dict) and direct.get("utilization") is not None:
        return direct
    return {}


def _fable_util_from_block(block: dict | None) -> tuple[float | None, str | None]:
    if not block or block.get("utilization") is None:
        return None, None
    try:
        value = float(block["utilization"])
    except (TypeError, ValueError):
        return None, None
    if math.isnan(value) or math.isinf(value):
        return None, None
    return max(0.0, min(100.0, value)), block.get("resets_at")


def fable_from_quota_row(row: dict | None) -> tuple[float | None, str | None]:
    """Return active (util%, resets_at) for Fable, including raw_data fallback.

    This path is for routing, cooldown and cached reconstruction.  UI should
    call ``fable_display_from_quota_row`` so inactive scoped windows remain visible.
    """
    if not isinstance(row, dict):
        return None, None
    util = row.get("fable_util")
    if util is not None:
        try:
            value = float(util)
        except (TypeError, ValueError):
            value = None
        else:
            if math.isnan(value) or math.isinf(value):
                value = None
            else:
                return max(0.0, min(100.0, value)), row.get("fable_reset")
    return _fable_util_from_block(fable_usage_block(_raw_usage_from_flat(row)))


def fable_display_from_quota_row(row: dict | None) -> tuple[float | None, str | None]:
    """Return Fable (util%, resets_at) for Telegram list/detail/refresh copy."""
    if not isinstance(row, dict):
        return None, None
    active_util, active_reset = fable_from_quota_row(row)
    if active_util is not None:
        return active_util, active_reset
    return _fable_util_from_block(fable_display_block(_raw_usage_from_flat(row)))


def flatten_usage(usage: dict) -> dict:
    """把 /api/oauth/usage 返回的嵌套结构展平，便于写 state_db.oauth_quota_cache。

    单位：/api/oauth/usage body 返回 0..100 百分比，_util_pct 做类型校验 +
    NaN/Inf 保护 + clamp(0,100)。响应头的 0..1 小数在 rate_limit_headers.py 单独处理。
    """
    def _safe_float(v) -> float:
        """安全转 float，None / NaN / Inf / 非法值 → 0.0。"""
        if v is None:
            return 0.0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0.0
        if math.isnan(f) or math.isinf(f):
            return 0.0
        return f

    def _util_pct(obj) -> float | None:
        if not obj or obj.get("utilization") is None:
            return None
        raw = obj["utilization"]
        # 类型校验：非数字 → None
        if not isinstance(raw, (int, float)):
            try:
                raw = float(raw)
            except (TypeError, ValueError):
                return None
        v = float(raw)
        # NaN / Inf → None（上游偶发异常值保护）
        if math.isnan(v) or math.isinf(v):
            return None
        # Anthropic /api/oauth/usage body 返回 0..100 百分比，直接透传。
        # 不做 0..1 → ×100 启发式转换（用户真实 1% 会被误判为 100%）。
        # 钳位 0..100（参考 claude-code-rust clamp），异常高值不会触发误禁用。
        return max(0.0, min(100.0, v))

    fh = usage.get("five_hour") or {}
    sd = usage.get("seven_day") or {}
    td = ((usage.get("openai") or {}).get("thirty_day") or {})
    sds = usage.get("seven_day_sonnet") or {}
    sdo = usage.get("seven_day_opus") or {}
    sdf = fable_usage_block(usage)
    extra = usage.get("extra_usage") or {}

    return {
        "fetched_at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "five_hour_util": _util_pct(fh),
        "five_hour_reset": fh.get("resets_at"),
        "seven_day_util": _util_pct(sd),
        "seven_day_reset": sd.get("resets_at"),
        "thirty_day_util": _util_pct(td),
        "thirty_day_reset": td.get("resets_at"),
        "sonnet_util": _util_pct(sds),
        "sonnet_reset": sds.get("resets_at"),
        "opus_util": _util_pct(sdo),
        "opus_reset": sdo.get("resets_at"),
        "fable_util": _util_pct(sdf),
        "fable_reset": sdf.get("resets_at"),
        "extra_used": _safe_float(extra.get("used_credits")) / 100.0,
        "extra_limit": _safe_float(extra.get("monthly_limit")) / 100.0,
        "extra_util": _util_pct(extra),
        "raw_data": json.dumps(usage, ensure_ascii=False),
    }


def extract_utils_percent(usage: dict) -> list[float | None]:
    """返回 [five_hour, seven_day, 30d, sonnet, opus, fable] 的百分比（None 表示该指标缺失）。"""
    flat = flatten_usage(usage)
    return [
        flat["five_hour_util"],
        flat["seven_day_util"],
        flat.get("thirty_day_util"),
        flat["sonnet_util"],
        flat["opus_util"],
        flat.get("fable_util"),
    ]


def latest_reset_iso(usage: dict) -> str | None:
    """各时间窗 resets_at 中最大的那个（向后兼容旧调用方）。

    ⚠ 建议新代码使用 `reset_iso_for_hit_windows(usage, threshold)`，它只
    考虑**实际撞到限额的窗口**的 reset 时间，避免「只有 5h 撞了却锁到 7d」的
    不合理情况。
    """
    candidates: list[datetime] = []
    for obj in (
        usage.get("five_hour") or {},
        usage.get("seven_day") or {},
        ((usage.get("openai") or {}).get("thirty_day") or {}),
        usage.get("seven_day_sonnet") or {},
        usage.get("seven_day_opus") or {},
    ):
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
    合成结构（five_hour / seven_day / openai.thirty_day）同样适用。
    """
    candidates: list[datetime] = []
    for obj in (
        usage.get("five_hour") or {},
        usage.get("seven_day") or {},
        ((usage.get("openai") or {}).get("thirty_day") or {}),
        usage.get("seven_day_sonnet") or {},
        usage.get("seven_day_opus") or {},
    ):
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

    raw_cursor = None
    raw_data = row.get("raw_data")
    if isinstance(raw_data, str) and raw_data:
        try:
            raw_payload = json.loads(raw_data)
            candidate = raw_payload.get("cursor") if isinstance(raw_payload, dict) else None
            raw_cursor = candidate if isinstance(candidate, dict) else None
        except Exception:
            raw_cursor = None

    result = {
        "five_hour": _block(row.get("five_hour_util"), row.get("five_hour_reset")),
        "seven_day": _block(row.get("seven_day_util"), row.get("seven_day_reset")),
        "openai": {
            "thirty_day": _block(row.get("thirty_day_util"), row.get("thirty_day_reset")),
        },
        "seven_day_sonnet": _block(row.get("sonnet_util"), row.get("sonnet_reset")),
        "seven_day_opus": _block(row.get("opus_util"), row.get("opus_reset")),
        "seven_day_fable": _block(*(fable_from_quota_row(row))),
        "extra_usage": {
            "is_enabled": bool(row.get("extra_limit")),
            "used_credits": row.get("extra_used") or 0,
            "monthly_limit": row.get("extra_limit") or 0,
            "utilization": row.get("extra_util") or 0,
        },
    }
    if raw_cursor is not None:
        result["cursor"] = raw_cursor
    return result


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
    if provider_of(account_key) == "cursor" and isinstance(usage.get("cursor"), dict):
        return evaluate_and_toggle_by_usage(
            account_key, usage, threshold=threshold, fresh=False,
        )
    utils = extract_utils_percent(usage)
    if not any(u is not None and u >= threshold for u in utils):
        return {"action": "cached_below_threshold", "utils": utils,
                "any_over": False, "hit_windows": [],
                "disabled_until": None}
    return evaluate_and_toggle_by_usage(account_key, usage, threshold=threshold)

def _usage_has_any_quota_signal(usage: dict) -> bool:
    return any(u is not None for u in extract_utils_percent(usage))


def _explicit_openai_wham_limit(usage: dict) -> bool:
    """Whether a WHAM response explicitly says routing is unavailable."""
    openai = usage.get("openai") if isinstance(usage, dict) else None
    if not isinstance(openai, dict) or openai.get("source") != "wham_usage":
        return False
    return openai.get("allowed") is False or openai.get("limit_reached") is True


def openai_plan_workspace_label(acc: dict | None) -> str:
    """Human label for OpenAI OAuth accounts.

    The same email can appear in multiple ChatGPT workspaces. Keep the label
    readable for notifications: plan plus workspace name, without leaking a raw
    workspace/account UUID suffix.
    """
    if not acc:
        return "OpenAI"
    plan_raw = str(acc.get("plan_type") or "").strip()
    workspace = str(acc.get("workspace_name") or "").strip()
    plan_map = {
        "team": "Team",
        "plus": "Plus",
        "pro": "Pro",
        "free": "Free",
        "enterprise": "Enterprise",
    }
    plan = plan_map.get(plan_raw.lower(), plan_raw[:1].upper() + plan_raw[1:] if plan_raw else "")
    if plan and workspace and workspace.lower() != "personal":
        return f"OpenAI · {plan}（{workspace}）"
    if plan:
        return f"OpenAI · {plan}"
    if workspace and workspace.lower() != "personal":
        return f"OpenAI · {workspace}"
    return "OpenAI"


def _ms_timestamp(value) -> int | None:
    try:
        if value is None:
            return None
        v = float(value)
    except (TypeError, ValueError):
        return None
    # state_db historically stores quota timestamps in milliseconds, while a few
    # call sites/comments still talk in seconds. Accept both for safety.
    if v > 10_000_000_000:
        return int(v)
    if v > 1_000_000_000:
        return int(v * 1000)
    return None


def _iso_from_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        return _format_utc(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))
    except Exception:
        return None


def _latest_iso(*values: str | None) -> str | None:
    latest = None
    for value in values:
        dt = _parse_iso(value)
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return _format_utc(latest.astimezone(timezone.utc)) if latest else None


_CODEX_MISSING_RESET_FALLBACK_SECONDS = 10 * 60

# A cached Codex response-header snapshot only guards the WHAM/Codex boundary,
# where both sources describe roughly the same moment. Once fresh WHAM usage is
# clearly newer than the snapshot, the snapshot is a stale observation and must
# not veto recovery. Keep the margin comfortably above WHAM propagation delay
# and the 30s response-header sampling throttle.
_CODEX_SNAPSHOT_SUPERSEDED_BY_USAGE_MS = 15 * 60 * 1000
_QUOTA_OBSERVATION_GENERATION_FIELD = "quota_observation_generation"
_QUOTA_OBSERVATION_FIELD = "quota_observation"
_CODEX_OBSERVATION_SOURCE = "openai_codex_headers"


def _quota_observation_generation(acc: dict | None) -> int | None:
    """Return the persistent quota-observation generation, or None if corrupt."""
    if not isinstance(acc, dict):
        return None
    value = acc.get(_QUOTA_OBSERVATION_GENERATION_FIELD, 0)
    if type(value) is not int or value < 0:
        return None
    return value


def codex_quota_observation(snap: dict) -> dict:
    """Build a JSON-safe persistent observation from Codex response headers."""
    observed_at = _ms_timestamp(snap.get("fetched_at"))
    if observed_at is None:
        observed_at = int(datetime.now(timezone.utc).timestamp() * 1000)

    sanitized: dict[str, int | float] = {}
    for name in ("primary", "secondary"):
        pct_key = f"{name}_used_pct"
        try:
            pct = float(snap.get(pct_key)) if snap.get(pct_key) is not None else None
        except (TypeError, ValueError):
            pct = None
        if pct is not None and math.isfinite(pct):
            sanitized[pct_key] = pct

        for suffix in ("reset_sec", "window_min"):
            key = f"{name}_{suffix}"
            try:
                value = int(snap.get(key)) if snap.get(key) is not None else None
            except (TypeError, ValueError):
                value = None
            if value is not None and value >= 0:
                sanitized[key] = value

    return {
        "source": _CODEX_OBSERVATION_SOURCE,
        "observed_at": observed_at,
        "snapshot": sanitized,
        "windows": openai_provider.codex_snapshot_window_observations(
            sanitized, observed_at=observed_at,
        ),
    }


def _codex_windows_from_observation(observation: dict | None) -> dict[str, dict]:
    if not isinstance(observation, dict):
        return {}
    if observation.get("source") != _CODEX_OBSERVATION_SOURCE:
        return {}
    stored = openai_provider.sanitize_codex_window_observations(
        observation.get("windows"),
    )
    legacy = openai_provider.codex_snapshot_window_observations(
        observation.get("snapshot") or {},
        observed_at=observation.get("observed_at"),
    )
    return openai_provider.merge_codex_window_observations(stored, legacy)


def _merge_codex_quota_observations(*observations: dict | None) -> dict | None:
    valid = [
        observation for observation in observations
        if isinstance(observation, dict)
        and observation.get("source") == _CODEX_OBSERVATION_SOURCE
    ]
    if not valid:
        return None

    latest = valid[0]
    latest_ms = _ms_timestamp(latest.get("observed_at"))
    for observation in valid[1:]:
        observed_ms = _ms_timestamp(observation.get("observed_at"))
        if observed_ms is not None and (latest_ms is None or observed_ms >= latest_ms):
            latest = observation
            latest_ms = observed_ms

    merged = copy.deepcopy(latest)
    merged["windows"] = openai_provider.merge_codex_window_observations(
        *(_codex_windows_from_observation(observation) for observation in valid),
    )
    return merged


def _codex_cached_reset_ms(row: dict, base_ms: int | None,
                           reset_key: str) -> int | None:
    try:
        reset_sec = row.get(reset_key)
        if reset_sec is not None:
            reset_sec = int(reset_sec)
            # Most rows store reset-after-seconds relative to the passive
            # snapshot. Be tolerant of future absolute epoch seconds too.
            if reset_sec > 1_000_000_000:
                return reset_sec * 1000
            if base_ms is not None:
                return base_ms + max(0, reset_sec) * 1000
            return None
    except Exception:
        pass

    if base_ms is None:
        return None
    return base_ms + _CODEX_MISSING_RESET_FALLBACK_SECONDS * 1000


def _codex_snapshot_from_quota_row(row: dict) -> dict:
    return {
        f"{name}_{suffix}": row.get(f"codex_{name}_{suffix}")
        for name in ("primary", "secondary")
        for suffix in ("used_pct", "reset_sec", "window_min")
    }


def _persistent_codex_observation(account_key: str) -> dict | None:
    acc = get_account(account_key)
    observation = acc.get(_QUOTA_OBSERVATION_FIELD) if isinstance(acc, dict) else None
    if not isinstance(observation, dict):
        return None
    if observation.get("source") != _CODEX_OBSERVATION_SOURCE:
        return None
    if not _codex_windows_from_observation(observation):
        return None
    return observation


def _codex_window_candidates(account_key: str, row: dict) -> dict[str, list[dict]]:
    """Return newest known Codex evidence for each semantic quota window.

    The config observation is written before the auxiliary SQLite snapshot, so
    it covers BUSY/FULL/READONLY failures. Partial snapshots are merged per
    semantic window rather than allowing one newly observed window to erase an
    older, still-relevant other window.
    """
    grouped: dict[str, list[dict]] = {
        "five_hour": [],
        "seven_day": [],
        "thirty_day": [],
    }

    def add(snapshot: dict | None, observed_ms: int | None, *, row_fallback: dict | None = None):
        if not isinstance(snapshot, dict):
            return
        window_map = openai_provider.codex_snapshot_window_map(snapshot)
        for raw_name in ("primary", "secondary"):
            pct_key = f"{raw_name}_used_pct"
            try:
                pct = float(snapshot.get(pct_key)) if snapshot.get(pct_key) is not None else None
            except (TypeError, ValueError):
                pct = None
            if pct is None or not math.isfinite(pct):
                continue
            semantic = window_map[raw_name]
            reset_ms = _codex_cached_reset_ms(
                snapshot, observed_ms, f"{raw_name}_reset_sec",
            )
            if reset_ms is None and row_fallback is not None:
                reset_dt = _parse_iso(row_fallback.get(f"{semantic}_reset"))
                if reset_dt is not None:
                    reset_ms = int(reset_dt.timestamp() * 1000)
            grouped[semantic].append({
                "raw_name": raw_name,
                "semantic": semantic,
                "pct": pct,
                "observed_ms": observed_ms,
                "reset_ms": reset_ms,
            })

    def add_windows(windows: dict | None):
        for semantic, item in openai_provider.sanitize_codex_window_observations(
            windows,
        ).items():
            observed_ms = _ms_timestamp(item.get("observed_at"))
            reset_ms = _codex_cached_reset_ms(
                item, observed_ms, "reset_sec",
            )
            grouped[semantic].append({
                "raw_name": item["raw_name"],
                "semantic": semantic,
                "pct": item["used_pct"],
                "observed_ms": observed_ms,
                "reset_ms": reset_ms,
            })

    add(
        _codex_snapshot_from_quota_row(row),
        _ms_timestamp(row.get("last_passive_update_at")),
        row_fallback=row,
    )
    sqlite_observations = row.get("codex_window_observations")
    if isinstance(sqlite_observations, str):
        try:
            sqlite_observations = json.loads(sqlite_observations)
        except (TypeError, ValueError):
            sqlite_observations = {}
    add_windows(sqlite_observations)
    persistent_observation = _persistent_codex_observation(account_key)
    add_windows(_codex_windows_from_observation(persistent_observation))

    # Known timestamps are comparable, so only the newest observation for that
    # semantic window remains relevant. SQLite and config observations can be
    # written from consecutive responses in the same millisecond while SQLite
    # is throttled; break those ties fail-closed by keeping the higher
    # utilization, then the later reset. Timestamp-less legacy evidence stays as
    # an additional fail-closed candidate until its absolute reset expires.
    for semantic, candidates in tuple(grouped.items()):
        unknown = [item for item in candidates if item["observed_ms"] is None]
        known = [item for item in candidates if item["observed_ms"] is not None]
        newest = max(
            known,
            key=lambda item: (
                item["observed_ms"],
                item["pct"],
                item["reset_ms"] if item["reset_ms"] is not None else -1,
            ),
        ) if known else None
        grouped[semantic] = unknown + ([newest] if newest is not None else [])
    return grouped


def _fresh_wham_window_utils(usage: dict | None) -> dict[str, float | None]:
    openai = usage.get("openai") if isinstance(usage, dict) else None
    if not isinstance(openai, dict) or openai.get("source") != "wham_usage":
        return {}

    def explicit_util(block: dict | None) -> float | None:
        raw = block.get("utilization") if isinstance(block, dict) else None
        if isinstance(raw, bool) or raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0 or value > 100:
            return None
        return value

    return {
        "five_hour": explicit_util(usage.get("five_hour")),
        "seven_day": explicit_util(usage.get("seven_day")),
        "thirty_day": explicit_util(openai.get("thirty_day")),
    }


def _cached_openai_codex_quota_hit(account_key: str, threshold: float,
                                    usage: dict | None = None) -> dict:
    """Return active Codex response-header quota hits cached for an OpenAI account.

    WHAM /usage and Codex response headers can disagree near reset boundaries.
    If a quota-disabled account has a still-active Codex header snapshot over the
    threshold, quota_monitor must not immediately resume it just because WHAM is
    below threshold. Use last_passive_update_at + reset_after_seconds so expired
    header snapshots don't keep accounts disabled forever. If a rare snapshot has
    no reset-after header, bound it by a short TTL.

    A Codex window is superseded only when active WHAM usage is clearly newer
    *and* contains the corresponding semantic window explicitly below threshold.
    Missing 5h/7d/30d evidence never clears another window. A persistent config
    observation closes the safety gap when auxiliary SQLite snapshot writes fail
    and also provides the generation used by the final account-enable CAS.
    """
    row = state_db.quota_load(account_key) or {}

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    usage_ms = _ms_timestamp(row.get("fetched_at"))
    wham_windows = _fresh_wham_window_utils(usage)
    hits: list[str] = []
    resets: list[str | None] = []

    for semantic, candidates in _codex_window_candidates(account_key, row).items():
        for candidate in candidates:
            pct = candidate["pct"]
            if pct < threshold:
                continue
            reset_ms = candidate["reset_ms"]
            if reset_ms is not None and reset_ms <= now_ms:
                continue

            observed_ms = candidate["observed_ms"]
            wham_pct = wham_windows.get(semantic)
            superseded = bool(
                observed_ms is not None
                and usage_ms is not None
                and usage_ms - observed_ms >= _CODEX_SNAPSHOT_SUPERSEDED_BY_USAGE_MS
                and wham_pct is not None
                and wham_pct < threshold
            )
            if superseded:
                continue

            label = f"codex {candidate['raw_name']} {pct:.0f}%"
            if label not in hits:
                hits.append(label)
            resets.append(_iso_from_ms(reset_ms))

    return {
        "any_over": bool(hits),
        "hit_windows": hits,
        "latest_reset": _latest_iso(*resets),
    }


def _evaluate_cursor_model_pools(
    account_key: str,
    account: dict,
    usage: dict,
    *,
    threshold: float,
    fresh: bool,
) -> dict:
    """Cool only models belonging to an exhausted Cursor billing pool."""
    from . import cooldown
    from .cursor_bridge.catalog import catalog_records, is_cursor_first_party_model

    cursor = usage.get("cursor") if isinstance(usage, dict) else None
    if not isinstance(cursor, dict):
        return {
            "action": "cursor_quota_unknown",
            "utils": [], "any_over": False, "hit_windows": [],
            "disabled_until": None, "cooled_models": 0, "recovered_models": 0,
        }

    def pct(name: str) -> float | None:
        try:
            value = float(cursor.get(name))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and 0 <= value <= 100 else None

    auto_pct = pct("auto_percent_used")
    api_pct = pct("api_percent_used")
    reset_iso = str(cursor.get("billing_cycle_end") or "") or None
    reset_dt = _parse_iso(reset_iso)
    reset_ms = int(reset_dt.timestamp() * 1000) if reset_dt is not None else None
    now_ms = int(time.time() * 1000)
    if reset_ms is not None and reset_ms <= now_ms:
        reset_ms = None

    auto_bucket = cursor.get("auto_bucket_models") or []
    records = catalog_records(account)
    cooled = 0
    recovered = 0
    over_pools: list[str] = []
    channel_key = f"oauth:{account_key}"

    for record in records:
        model = str(record.get("id") or "")
        if not model:
            continue
        pool = "cursor_models" if is_cursor_first_party_model(record, auto_bucket) else "other_models"
        utilization = auto_pct if pool == "cursor_models" else api_pct
        over = utilization is not None and utilization >= threshold
        state = cooldown.get_state(channel_key, model) or {}
        previous_message = str(state.get("last_error_message") or "")
        owned = '"code":"cursor_quota_pool"' in previous_message or "cursor_quota_pool" in previous_message
        same_pool = f'"pool":"{pool}"' in previous_message

        if over:
            if pool not in over_pools:
                over_pools.append(pool)
            # A percentage without a bounded reset must not create an indefinite
            # model freeze. Real 429 responses still follow normal cooldown.
            if reset_ms is None:
                continue
            if owned and same_pool and cooldown.is_blocked(channel_key, model):
                continue
            message = json.dumps({
                "error": {
                    "code": "cursor_quota_pool",
                    "message": "Cursor billing pool reached configured threshold",
                    "pool": pool,
                    "utilization": utilization,
                    "resets_at": reset_iso,
                }
            }, ensure_ascii=False, separators=(",", ":"))
            cooldown.record_error(
                channel_key, model, message, cooldown_until=reset_ms,
            )
            cooled += 1
        elif fresh and owned:
            cooldown.clear(
                channel_key, model=model, notify_recovered=False,
            )
            recovered += 1

    if cooled:
        try:
            notifier.notify_event(
                "quota_cooldown",
                "🟠 <b>Cursor 模型池已进入额度冷却</b>\n"
                f"账号: <code>{notifier.escape_html(account_key_to_email(account_key))}</code> · {notifier.provider_tag('cursor')}\n"
                f"额度池: <code>{notifier.escape_html(' / '.join(over_pools))}</code>\n"
                f"冷却模型: <code>{cooled}</code>\n"
                f"恢复时间: <code>{notifier.escape_html(_to_bjt(reset_iso))}</code>"
            )
        except Exception:
            pass
        action = "cursor_pool_cooldown"
    elif recovered:
        try:
            notifier.notify_event(
                "quota_resumed",
                "♻️ <b>Cursor 模型池额度已恢复</b>\n"
                f"账号: <code>{notifier.escape_html(account_key_to_email(account_key))}</code> · {notifier.provider_tag('cursor')}\n"
                f"恢复模型: <code>{recovered}</code>"
            )
        except Exception:
            pass
        action = "cursor_pool_recovered"
    elif over_pools:
        action = "cursor_pool_still_cooling"
    else:
        action = "cursor_pool_available"
    return {
        "action": action,
        "utils": [auto_pct, api_pct],
        "any_over": bool(over_pools),
        "hit_windows": over_pools,
        "disabled_until": reset_iso if over_pools else None,
        "cooled_models": cooled,
        "recovered_models": recovered,
    }


def _claude_fable_models(account: dict) -> list[str]:
    """Return configured Claude models governed by the Fable scoped quota."""
    selected = account.get("models") or config.get().get("oauthDefaultModels") or []
    return sorted({
        model.strip()
        for model in selected
        if isinstance(model, str)
        and model.strip()
        and _is_fable_model_label(model)
    })


def claude_fable_models(account: dict) -> list[str]:
    """Public read-only view of models governed by Claude's Fable quota."""
    return _claude_fable_models(account)


def _evaluate_claude_fable_pool(
    account_key: str,
    account: dict,
    usage: dict,
    *,
    threshold: float,
    fresh: bool,
) -> dict:
    """Cool only Fable models when the Claude weekly scoped pool is exhausted."""
    from . import cooldown

    block = fable_usage_block(usage)
    flat = flatten_usage(usage)
    utilization = flat.get("fable_util")
    reset_iso = str(block.get("resets_at") or "") or None
    reset_dt = _parse_iso(reset_iso)
    reset_ms = int(reset_dt.timestamp() * 1000) if reset_dt is not None else None
    now_ms = int(time.time() * 1000)
    if reset_ms is not None and reset_ms <= now_ms:
        reset_ms = None

    models = _claude_fable_models(account)
    over = utilization is not None and utilization >= threshold
    channel_key = f"oauth:{account_key}"
    cooled = 0
    recovered = 0

    for model in models:
        state = cooldown.get_state(channel_key, model) or {}
        previous_message = str(state.get("last_error_message") or "")
        owned = (
            '"code":"claude_fable_quota"' in previous_message
            or "claude_fable_quota" in previous_message
        )
        if over:
            # A scoped percentage without a bounded reset remains visible and
            # alertable, but must not create an indefinite model freeze.
            if reset_ms is None:
                continue
            if owned and cooldown.is_blocked(channel_key, model):
                continue
            message = json.dumps({
                "error": {
                    "code": "claude_fable_quota",
                    "message": "Claude Fable scoped quota reached configured threshold",
                    "scope": "fable",
                    "utilization": utilization,
                    "resets_at": reset_iso,
                }
            }, ensure_ascii=False, separators=(",", ":"))
            cooldown.record_error(
                channel_key, model, message, cooldown_until=reset_ms,
            )
            cooled += 1
        elif utilization is not None and fresh and owned:
            cooldown.clear(
                channel_key, model=model, notify_recovered=False,
            )
            recovered += 1

    if cooled:
        try:
            notifier.notify_event(
                "quota_cooldown",
                "🟠 <b>Claude Fable 模型已进入额度冷却</b>\n"
                f"账号: <code>{notifier.escape_html(account_key_to_email(account_key))}</code> · {notifier.provider_tag('claude')}\n"
                f"冷却模型: <code>{cooled}</code>\n"
                f"恢复时间: <code>{notifier.escape_html(_to_bjt(reset_iso))}</code>"
            )
        except Exception:
            pass
        action = "claude_fable_model_cooldown"
    elif recovered:
        try:
            notifier.notify_event(
                "quota_resumed",
                "♻️ <b>Claude Fable 模型额度已恢复</b>\n"
                f"账号: <code>{notifier.escape_html(account_key_to_email(account_key))}</code> · {notifier.provider_tag('claude')}\n"
                f"恢复模型: <code>{recovered}</code>"
            )
        except Exception:
            pass
        action = "claude_fable_model_recovered"
    elif over and models and reset_ms is not None:
        action = "claude_fable_model_still_cooling"
    elif over:
        action = "claude_fable_quota_alert"
    else:
        action = "claude_fable_available"
    return {
        "action": action,
        "utils": [utilization],
        "any_over": over,
        "hit_windows": ["fable"] if over else [],
        "disabled_until": reset_iso if over else None,
        "cooled_models": cooled,
        "recovered_models": recovered,
        "quota_signal_known": utilization is not None,
    }


def _evaluate_antigravity_credits(
    account_key: str,
    acc: dict,
    usage: dict,
    *,
    threshold: float,
    fresh: bool,
    expected_quota_generation,
) -> dict:
    """Combine reliable Credits and 5h/weekly signals into one quota gate."""
    utils = extract_utils_percent(usage)
    block = usage.get("antigravity") if isinstance(usage, dict) else None
    credits_known = isinstance(block, dict) and bool(block.get("known"))
    credits_available = bool(block.get("available")) if credits_known else None
    summary_failed = isinstance(block, dict) and isinstance(block.get("quota_error"), dict)
    window_hits = [label for label, util in zip(("5h", "7d"), utils[:2])
                   if util is not None and util >= threshold]
    has_window_signal = any(util is not None for util in utils[:2])
    hits = list(window_hits)
    if credits_known and not credits_available:
        hits.append("Credits")
    any_over = bool(hits)
    has_reliable_signal = credits_known or has_window_signal
    reason = acc.get("disabled_reason")
    base = {"utils": utils, "any_over": any_over, "hit_windows": hits,
            "disabled_until": acc.get("disabled_until")}

    if any_over:
        if reason == "quota":
            return {**base, "action": "still_over_quota"}
        latest_reset = None if "Credits" in hits else reset_iso_for_hit_windows(usage, threshold)
        try:
            disable_result = set_disabled_by_quota(account_key, latest_reset)
        except Exception as exc:
            print(f"[oauth] evaluate antigravity disable failed for {account_key}: {exc}")
            return {**base, "action": "disable_failed", "disabled_until": latest_reset}
        disable_state = (disable_result or {}).get("state")
        if disable_state != "disabled":
            return {**base,
                    "action": ("still_over_quota" if disable_state == "already_quota_disabled"
                               else disable_state or "disable_failed"),
                    "disabled_until": (disable_result or {}).get("disabled_until"),
                    "disabled_reason": (disable_result or {}).get("disabled_reason"),
                    "error_code": "account_state_conflict"}
        return {**base, "action": "disabled", "disabled_until": latest_reset}

    if reason != "quota":
        return {**base, "action": "kept_enabled"}
    # Credits and 5h/weekly are independent gates.  A successful Credits read
    # cannot prove that a previously exhausted window recovered when the quota
    # summary endpoint failed in this refresh.  Keep the account disabled until
    # a fresh window snapshot is available again.
    if summary_failed and not has_window_signal:
        return {**base, "action": "quota_partial_keep_disabled"}
    if not has_reliable_signal:
        return {**base, "action": "quota_unknown_keep_disabled"}
    if not fresh:
        return {**base, "action": "quota_stale_keep_disabled"}
    if expected_quota_generation is None:
        return {**base, "action": "resume_failed",
                "error_code": "quota_observation_generation_invalid"}
    try:
        enable_result = set_enabled(
            account_key, True, expected_disabled_reason="quota",
            expected_quota_observation_generation=expected_quota_generation,
        )
    except Exception as exc:
        print(f"[oauth] evaluate antigravity resume failed for {account_key}: {exc}")
        return {**base, "action": "resume_failed"}
    enable_state = (enable_result or {}).get("state")
    if enable_state != "enabled":
        return {**base, "action": enable_state or "resume_failed",
                "error_code": "account_state_conflict"}
    return {**base, "action": "resumed", "disabled_until": None}


def evaluate_and_toggle_by_usage(account_key: str, usage: dict,
                                 *, threshold: float | None = None,
                                 fresh: bool = True) -> dict:
    """核心策略：拿到新鲜 usage 后评估禁用/恢复，并执行状态切换。

    规则：
      • disabled_reason in ("user", "auth_error") → 完全不碰（手动禁用永远不自动恢复）
      • 任一账号级窗口 util ≥ threshold → 需要禁用
          - 账号已是 quota 禁用：保持不动（不刷新 disabled_until，避免目标移动）
          - 账号未禁用：set_disabled_by_quota，disabled_until = 撞到窗口的最大 reset
      • Claude Fable scoped 窗口只冷却该账号的 Fable 模型，不禁用整个账号
      • 所有窗口 util < threshold → 可用
          - OpenAI / Grok 账号若 usage 没有任何窗口指标，或这份 usage 不是本轮新鲜探测，
            不能作为恢复依据；保持原 quota 禁用状态，避免“未知=恢复”误判。
          - OpenAI 账号若仍有未过期的 Codex 响应头超限快照，继续保持 quota
            禁用，避免 WHAM/Codex 边界不同步导致“假恢复”。
          - 若 Codex 没有活动的超限快照，且本轮新鲜有效 usage 全部低于阈值，
            说明上游窗口已经重置；直接恢复并清除旧 disabled_until。旧时间只是
            上次超限时的预测，不能覆盖更新后的上游事实。
          - Grok 账号使用官方 billing 当前周期快照，fresh usage 低于阈值即可恢复。
          - 其他账号是 quota 禁用：set_enabled(True) 自动恢复。
          - 账号未禁用：无事发生

    返回: {
      "action": "noop_user"|"noop_auth_error"|"disabled"|"still_over_quota"|
                "wham_limit_disabled"|"wham_limit_keep_disabled"|
                "resumed"|"kept_enabled"|"disable_failed"|"resume_failed"|"noop_missing",
      "utils": [5h, 7d, 30d, sonnet, opus, fable],   # None 表示该指标缺失
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

    canonical = _resolve_existing_account_key(account_key)
    if canonical:
        account_key = canonical
    provider = provider_of(account_key)
    utils = extract_utils_percent(usage)
    # Fable is a model-scoped weekly sub-cap.  It is evaluated separately below
    # and must never participate in the account-level disable decision.
    window_labels = ["5h", "周额度" if provider == "xai" else "7d", "30d", "sonnet", "opus"]
    hit_windows = [lbl for lbl, u in zip(window_labels, utils[:5])
                   if u is not None and u >= threshold]
    any_over = bool(hit_windows)

    acc = get_account(account_key)
    if acc is None:
        return {"action": "noop_missing", "utils": utils, "any_over": any_over,
                "hit_windows": hit_windows, "disabled_until": None}
    reason = acc.get("disabled_reason")
    expected_quota_generation = _quota_observation_generation(acc)
    if reason in ("user", "auth_error"):
        return {"action": f"noop_{reason}", "utils": utils, "any_over": any_over,
                "hit_windows": hit_windows,
                "disabled_until": acc.get("disabled_until")}

    fable_pool_result = None
    if provider == "claude":
        fable_pool_result = _evaluate_claude_fable_pool(
            account_key,
            acc,
            usage,
            threshold=float(threshold),
            fresh=fresh,
        )

    if provider == "cursor":
        return _evaluate_cursor_model_pools(
            account_key,
            acc,
            usage,
            threshold=float(threshold),
            fresh=fresh,
        )

    if provider == "antigravity":
        return _evaluate_antigravity_credits(
            account_key,
            acc,
            usage,
            threshold=float(threshold),
            fresh=fresh,
            expected_quota_generation=expected_quota_generation,
        )

    # WHAM's explicit gate is authoritative even when its percentage windows
    # are absent or temporarily report a low number.  In particular, never turn
    # "allowed: false" / "limit_reached: true" into quota recovery.
    wham_limit = provider == "openai" and _explicit_openai_wham_limit(usage)
    if wham_limit:
        hit_windows.append("WHAM limit")
        if reason == "quota":
            return {"action": "wham_limit_keep_disabled", "utils": utils,
                    "any_over": True, "hit_windows": hit_windows,
                    "disabled_until": acc.get("disabled_until")}
        latest_reset = reset_iso_for_hit_windows(usage, threshold)
        try:
            disable_result = set_disabled_by_quota(account_key, latest_reset)
        except Exception as exc:
            print(f"[oauth] evaluate WHAM disable failed for {account_key}: {exc}")
            return {"action": "disable_failed", "utils": utils,
                    "any_over": True, "hit_windows": hit_windows,
                    "disabled_until": None}
        disable_state = (disable_result or {}).get("state")
        if disable_state != "disabled":
            return {"action": (
                        "wham_limit_keep_disabled"
                        if disable_state == "already_quota_disabled"
                        else disable_state or "disable_failed"
                    ),
                    "utils": utils, "any_over": True,
                    "hit_windows": hit_windows,
                    "disabled_until": (disable_result or {}).get("disabled_until"),
                    "disabled_reason": (disable_result or {}).get("disabled_reason"),
                    "error_code": "account_state_conflict"}
        return {"action": "wham_limit_disabled", "utils": utils,
                "any_over": True, "hit_windows": hit_windows,
                "disabled_until": latest_reset}

    cached_codex_hit = None
    if provider_of(account_key) == "openai":
        cached_codex_hit = _cached_openai_codex_quota_hit(
            account_key, threshold, usage if fresh else None,
        )
        if cached_codex_hit.get("any_over"):
            any_over = True
            for _hit in cached_codex_hit.get("hit_windows") or []:
                if _hit not in hit_windows:
                    hit_windows.append(_hit)

    if any_over:
        if reason == "quota":
            return {"action": "still_over_quota", "utils": utils,
                    "any_over": True, "hit_windows": hit_windows,
                    "disabled_until": acc.get("disabled_until") or (cached_codex_hit or {}).get("latest_reset")}
        latest_reset = _latest_iso(
            reset_iso_for_hit_windows(usage, threshold),
            (cached_codex_hit or {}).get("latest_reset"),
        )
        try:
            disable_result = set_disabled_by_quota(account_key, latest_reset)
        except Exception as exc:
            print(f"[oauth] evaluate set_disabled_by_quota failed for {account_key}: {exc}")
            return {"action": "disable_failed", "utils": utils,
                    "any_over": True, "hit_windows": hit_windows,
                    "disabled_until": None}
        disable_state = (disable_result or {}).get("state")
        if disable_state != "disabled":
            return {"action": (
                        "still_over_quota"
                        if disable_state == "already_quota_disabled"
                        else disable_state or "disable_failed"
                    ),
                    "utils": utils, "any_over": True,
                    "hit_windows": hit_windows,
                    "disabled_until": (disable_result or {}).get("disabled_until"),
                    "disabled_reason": (disable_result or {}).get("disabled_reason"),
                    "error_code": "account_state_conflict"}
        return {"action": "disabled", "utils": utils, "any_over": True,
                "hit_windows": hit_windows, "disabled_until": latest_reset}

    # 全部窗口都可用。空缓存或被节流跳过的旧缓存不能证明额度恢复；尤其
    # quota 禁用账号不能因 [None, None, None, None] 被误恢复。OpenAI 若仍有
    # 活动的 Codex 超限快照，前面的 any_over 分支已经保持禁用；否则新鲜
    # WHAM 低用量应覆盖旧 disabled_until，因为后者只是上次超限时的预测。
    if reason == "quota":
        if provider in ("openai", "xai"):
            if not _usage_has_any_quota_signal(usage):
                return {"action": "quota_unknown_keep_disabled", "utils": utils,
                        "any_over": False, "hit_windows": [],
                        "disabled_until": acc.get("disabled_until")}
            if not fresh:
                return {"action": "quota_stale_keep_disabled", "utils": utils,
                        "any_over": False, "hit_windows": [],
                        "disabled_until": acc.get("disabled_until")}
        if expected_quota_generation is None:
            return {"action": "resume_failed", "utils": utils,
                    "any_over": False, "hit_windows": [],
                    "disabled_until": acc.get("disabled_until"),
                    "error_code": "quota_observation_generation_invalid"}
        runtime_state = None
        if provider == "openai" and fresh:
            # Clear persistent/runtime routing blockers before enabling. The
            # cooldown clear itself is DB-first, so a failed delete leaves the
            # current process and a restarted process consistently blocked.
            runtime_state = _clear_oauth_runtime_state(
                account_key,
                clear_quota_cache=False,
                notify_recovered=False,
            )
            if not runtime_state.get("required_state_cleared"):
                print(
                    f"[oauth] evaluate resume blocked by runtime state for "
                    f"{account_key}: {runtime_state}"
                )
                return {"action": "resume_failed", "utils": utils,
                        "any_over": False, "hit_windows": [],
                        "disabled_until": acc.get("disabled_until"),
                        "error_code": "runtime_state_clear_failed",
                        "runtime_state": runtime_state}
        try:
            enable_result = set_enabled(
                account_key, True, expected_disabled_reason="quota",
                expected_quota_observation_generation=expected_quota_generation,
            )
        except Exception as exc:
            print(f"[oauth] evaluate set_enabled failed for {account_key}: {exc}")
            return {"action": "resume_failed", "utils": utils,
                    "any_over": False, "hit_windows": [],
                    "disabled_until": acc.get("disabled_until"),
                    "error_code": "account_enable_failed",
                    "runtime_state": runtime_state}
        enable_state = (enable_result or {}).get("state")
        if enable_state == "enabled":
            return {"action": "resumed", "utils": utils, "any_over": False,
                    "hit_windows": [], "disabled_until": None,
                    "runtime_state": runtime_state}
        if enable_state == "already_enabled":
            return {"action": "kept_enabled", "utils": utils, "any_over": False,
                    "hit_windows": [], "disabled_until": None,
                    "runtime_state": runtime_state}
        if enable_state == "missing":
            action = "noop_missing"
        else:
            current_reason = (enable_result or {}).get("disabled_reason")
            action = (f"noop_{current_reason}"
                      if current_reason in ("user", "auth_error")
                      else enable_state or "state_conflict")
        return {"action": action, "utils": utils, "any_over": False,
                "hit_windows": [],
                "disabled_until": (enable_result or {}).get("disabled_until"),
                "disabled_reason": (enable_result or {}).get("disabled_reason"),
                "error_code": "account_state_conflict",
                "runtime_state": runtime_state}
    if fable_pool_result and (
        fable_pool_result.get("any_over")
        or fable_pool_result.get("cooled_models")
        or fable_pool_result.get("recovered_models")
    ):
        return {**fable_pool_result, "utils": utils}
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
    """启动时调用，幂等执行 StateStore snapshots 的联合主键迁移。

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

        def expand_order(values) -> list[str]:
            result: list[str] = []
            seen: set[str] = set()
            for key in list(values or []):
                expanded = priority_expansion.get(key)
                if expanded is None:
                    expanded = [channel_mapping.get(key, key)]
                for candidate_key in expanded:
                    new_key = str(candidate_key or "")
                    if new_key and new_key not in seen:
                        result.append(new_key)
                        seen.add(new_key)
                if expanded != [key]:
                    config_stats["priority_entries"] += 1
            return result

        po = lb.setdefault("priorityOrders", {})
        for fam in ("anthropic", "openai"):
            po[fam] = expand_order(po.get(fam) or [])
        if isinstance(lb.get("channelPriorityOrder"), list):
            lb["channelPriorityOrder"] = expand_order(lb["channelPriorityOrder"])
        model_orders = lb.setdefault("modelPriorityOrders", {})
        for model, order in list(model_orders.items()):
            model_orders[model] = expand_order(order)

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
    from .openai.codex_identity import normalize_account_identity

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
    if "codexDeviceInstallationId" in entry:
        patch["codexDeviceInstallationId"] = entry.get("codexDeviceInstallationId")
    if "codexIdentity" in entry:
        patch["codexIdentity"] = copy.deepcopy(entry.get("codexIdentity"))
    candidate = {"provider": "openai", **patch}
    provider_cfg = config.get().get("openaiOAuth") or {}
    identity_cfg = provider_cfg.get("codexIdentity") or {}
    normalize_account_identity(
        candidate,
        protocol_profile=codex_protocol_profile(provider_cfg).profile_id,
        new_identity_generation_version=identity_cfg.get(
            "newIdentityGenerationVersion", 1
        ),
    )
    candidate.pop("provider", None)
    return candidate


def find_exact_identity(entry: dict) -> tuple[str, dict] | None:
    """Return the configured account whose canonical identity exactly matches ``entry``.

    This is intentionally stricter than the legacy add-account matching rules: a
    subject/email fallback that would change the canonical key is not a duplicate.
    The returned account is a snapshot and must not be mutated by callers.
    """
    provider = _normalize_provider(entry.get("provider"))
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"unsupported provider: {entry.get('provider')!r}")
    incoming_key = _canonical_key({**entry, "provider": provider})
    if not incoming_key.partition(":")[2]:
        return None
    matches = [
        account for account in config.get().get("oauthAccounts", [])
        if _acc_provider(account) == provider and _canonical_key(account) == incoming_key
    ]
    if len(matches) > 1:
        raise AmbiguousOAuthAccountKey(incoming_key)
    if not matches:
        return None
    return incoming_key, copy.deepcopy(matches[0])


def replace_exact_identity(expected_account_key: str, entry: dict) -> dict:
    """Atomically replace credentials/profile for one unchanged OAuth identity.

    No delete, rename, state cleanup, log migration or LB publication occurs.  A
    stale confirmation therefore fails closed instead of becoming an add/rename.
    """
    provider = _normalize_provider(entry.get("provider"))
    incoming = copy.deepcopy(entry)
    incoming["provider"] = provider
    required = ("email", "access_token", "refresh_token")
    missing = [key for key in required if not incoming.get(key)]
    if missing:
        raise ValueError(f"missing required fields: {missing}")
    incoming_key = _canonical_key(incoming)
    if expected_account_key != incoming_key:
        return {"status": "identity_conflict", "account_key": expected_account_key}

    result = {"status": "missing", "account_key": expected_account_key}
    with config.serialized_updates():
        def mutate(cfg):
            accounts = cfg.setdefault("oauthAccounts", [])
            indexes = [
                index for index, account in enumerate(accounts)
                if _canonical_key(account) == expected_account_key
            ]
            if not indexes:
                return
            if len(indexes) != 1:
                result["status"] = "identity_conflict"
                return
            index = indexes[0]
            current = accounts[index]
            current_key = _canonical_key(current)
            if (
                current_key != expected_account_key
                or _acc_provider(current) != provider
                or incoming_key != expected_account_key
            ):
                result["status"] = "identity_conflict"
                return

            # Preserve every field not supplied by the fresh observation, then
            # explicitly protect user settings from login defaults.  A successful
            # interactive login recovers an account that was auto-disabled only
            # because its previous credential failed authentication.
            replacement = copy.deepcopy(current)
            replacement.update(incoming)
            if "maxConcurrent" in current:
                replacement["maxConcurrent"] = copy.deepcopy(current["maxConcurrent"])
            if current.get("disabled_reason") == "auth_error":
                replacement["enabled"] = True
                replacement["disabled_reason"] = None
                replacement["disabled_until"] = None
            else:
                for key in ("enabled", "disabled_reason", "disabled_until"):
                    if key in current:
                        replacement[key] = copy.deepcopy(current[key])
            if provider != "cursor" or not incoming.get("models"):
                if "models" in current:
                    replacement["models"] = copy.deepcopy(current["models"])
            if provider == "openai":
                from .openai.codex_identity import account_identity_from_account
                replacement.pop("codexDeviceConvergenceEnabled", None)
                for key in ("codexIdentity", "codexDeviceInstallationId"):
                    if key not in entry and key in current:
                        replacement[key] = copy.deepcopy(current[key])
                current_identity = account_identity_from_account(current, require=False)
                replacement_identity = account_identity_from_account(replacement, require=False)
                if (
                    current_identity is not None
                    and replacement_identity is not None
                    and current_identity.installation_id != replacement_identity.installation_id
                ):
                    result["status"] = "identity_conflict"
                    return
            if provider == "cursor":
                for key in ("cursor_max_context_disabled_models", "cursor_disabled_models"):
                    if key not in entry and key in current:
                        replacement[key] = copy.deepcopy(current[key])
                if not entry.get("cursor_profile_id") and current.get("cursor_profile_id"):
                    for key in ("email", "label", "cursor_profile_name", "cursor_profile_id", "cursor_email_verified"):
                        if key in current:
                            replacement[key] = copy.deepcopy(current[key])
            if _canonical_key(replacement) != expected_account_key:
                result["status"] = "identity_conflict"
                return
            accounts[index] = replacement
            result.update(status="replaced", index=index, account=copy.deepcopy(replacement))

        config.update(mutate)
    return result


def add_account(entry: dict) -> None:
    """Serialize target resolution, snapshots, config publication, and state rename."""
    with config.serialized_updates():
        _add_account_serialized(entry)


def add_account_if_identity_absent(entry: dict) -> dict:
    """Atomically add only while the incoming canonical identity is still absent."""
    incoming_key = _canonical_key(entry)
    with config.serialized_updates():
        if any(
            _canonical_key(account) == incoming_key
            for account in config.get().get("oauthAccounts", [])
        ):
            return {"status": "identity_conflict", "account_key": incoming_key}
        _add_account_serialized(entry)
    return {"status": "added", "account_key": incoming_key}


def _add_account_serialized(entry: dict) -> None:
    """entry 需至少含 email / access_token / refresh_token。

    支持可选字段：
      - provider: "claude" (默认) / "openai" / "xai" / "cursor" / "antigravity"
      - id_token / chatgpt_account_id / workspace_id / workspace_name /
        workspace_type / organization_id / plan_type / subscription_expires_at /
        codexIdentity / codexDeviceInstallationId
        (OpenAI 专属；按 canonical workspace 强制建立 versioned UUIDv4 identity)
      - id_token / subject / sub / base_url / token_endpoint / redirect_uri
        (xAI 专属)
      - cursor_max_context_disabled_models（Cursor 每账号显式关闭 Max Context 的例外）
      - cursor_disabled_models（Cursor 每账号禁用的 canonical 模型）
    """
    required = ("email", "access_token", "refresh_token")
    missing = [k for k in required if not entry.get(k)]
    if missing:
        raise ValueError(f"missing required fields: {missing}")

    email = entry["email"]
    provider = _normalize_provider(entry.get("provider") or entry.get("type"))
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"unsupported provider: {entry.get('provider') or entry.get('type')!r}")

    # 规范化字段
    normalized = {
        "email": email,
        "provider": provider,
        "access_token": entry["access_token"],
        "refresh_token": entry["refresh_token"],
        "expired": entry.get("expired", ""),
        "last_refresh": entry.get("last_refresh", _format_utc(datetime.now(timezone.utc))),
        "type": entry.get("type", provider if provider in ("openai", "xai", "cursor", "antigravity") else "claude"),
        "enabled": entry.get("enabled", True),
        "disabled_reason": entry.get("disabled_reason"),
        "disabled_until": entry.get("disabled_until"),
        "models": entry.get("models") or [],
        # Small allow-listed provider-neutral LKG metadata. Defaults never own
        # this field; an absent field remains backward compatible.
        "account_model_catalog": copy.deepcopy(entry.get("account_model_catalog") or {}),
        # Non-Cursor account model policy. ``models`` remains the canonical LKG
        # ID list for backward compatibility; user disablement is independent.
        "disabledModels": sorted({
            str(model).strip() for model in entry.get("disabledModels") or []
            if str(model).strip()
        }),
        "last_model_sync": entry.get("last_model_sync") or "",
        "last_model_sync_source": entry.get("last_model_sync_source") or "",
        "last_model_sync_error": entry.get("last_model_sync_error") or "",
        "last_model_sync_attempt": entry.get("last_model_sync_attempt") or "",
        # §9-1：存登录响应的 scope（空格分隔），供 refresh 时带真实 scope；
        # 老账号缺省空串，refresh 时回退完整六项 OAUTH_SCOPES。
        "scopes": entry.get("scopes", "") or "",
    }
    if provider == "cursor":
        # Cursor keeps its existing dedicated catalog/disable schema.
        normalized.pop("disabledModels", None)
        for key in (
            "last_model_sync_source", "last_model_sync_error", "last_model_sync_attempt",
        ):
            normalized.pop(key, None)
    # OpenAI 专属字段（缺失时保持空串，渲染端按需展示）
    if provider == "openai":
        normalized.update(_openai_metadata_patch(entry))
        normalized["last_model_sync_client_version"] = str(
            entry.get("last_model_sync_client_version") or ""
        )
        normalized["last_model_sync_profile"] = str(
            entry.get("last_model_sync_profile") or ""
        )
        for field in ("last_model_sync_attempt_client_version", "last_model_sync_attempt_profile"):
            normalized[field] = str(entry.get(field) or "")
        normalized["models_etag"] = str(entry.get("models_etag") or "")
        normalized["models_etag_client_version"] = str(
            entry.get("models_etag_client_version") or ""
        )
        normalized["models_etag_profile"] = str(
            entry.get("models_etag_profile") or ""
        )
    # xAI 专属字段（缺失时保持空串；subject 用于稳定 account_key）
    elif provider == "xai":
        subject = str(entry.get("subject") or entry.get("sub") or "")
        normalized.update({
            "id_token": entry.get("id_token", "") or "",
            "subject": subject,
            "sub": subject,
            "base_url": entry.get("base_url") or entry.get("baseUrl") or xai_provider.api_base_url(),
            "token_endpoint": entry.get("token_endpoint") or xai_provider.token_url(),
            "redirect_uri": entry.get("redirect_uri") or xai_provider.redirect_uri(),
        })
    # Antigravity 专属字段。project_id 是身份主键的一部分，缺了不许保存。
    elif provider == "antigravity":
        project_id = _antigravity_project_id(entry)
        if not project_id:
            raise ValueError("antigravity account requires project_id")
        normalized.update({
            "project_id": project_id,
            "base_url": entry.get("base_url") or entry.get("baseUrl") or antigravity_provider.api_base_url(),
            "token_endpoint": entry.get("token_endpoint") or antigravity_provider.token_url(),
            "redirect_uri": entry.get("redirect_uri") or antigravity_provider.redirect_uri(),
        })
    # Cursor 专属字段。模型目录来自该账号自己的 AvailableModels，不能
    # 与 models.dev 元数据合并，否则上下文和变体限制会失真。
    elif provider == "cursor":
        subject = str(entry.get("subject") or entry.get("sub") or "")
        normalized.update({
            "subject": subject,
            "sub": subject,
            "label": entry.get("label") or entry.get("email") or "Cursor",
            "cursor_profile_name": entry.get("cursor_profile_name") or "",
            "cursor_profile_id": entry.get("cursor_profile_id") or "",
            "cursor_email_verified": (
                entry.get("cursor_email_verified")
                if isinstance(entry.get("cursor_email_verified"), bool)
                else None
            ),
            "cursor_model_catalog": copy.deepcopy(entry.get("cursor_model_catalog") or {}),
            "plan_type": entry.get("plan_type") or "",
            "subscription_status": entry.get("subscription_status") or "",
            "billing_cycle_start": entry.get("billing_cycle_start") or "",
            "billing_cycle_end": entry.get("billing_cycle_end") or "",
            "last_model_sync": entry.get("last_model_sync") or "",
        })
        # Omitted on re-login means preserve the existing local preference.
        if "cursor_max_context_disabled_models" in entry:
            normalized["cursor_max_context_disabled_models"] = sorted({
                str(model).strip()
                for model in entry.get("cursor_max_context_disabled_models") or []
                if str(model).strip()
            })
        if "cursor_disabled_models" in entry:
            normalized["cursor_disabled_models"] = sorted({
                str(model).strip()
                for model in entry.get("cursor_disabled_models") or []
                if str(model).strip()
            })
    # Claude 专属字段（套餐/订阅信息，来自 /api/oauth/profile）
    elif provider == "claude":
        for k in ("plan_type", "rate_limit_tier", "billing_type",
                   "subscription_status", "subscription_created_at",
                   "has_extra_usage_enabled", "seat_tier"):
            if entry.get(k) is not None:
                normalized[k] = entry[k]

    normalized_key = _account_key(normalized)

    def _matches_cursor_target(acc: dict) -> bool:
        if _acc_provider(acc) != "cursor":
            return False
        old_subject = _cursor_subject(acc)
        new_subject = _cursor_subject(normalized)
        if old_subject and new_subject:
            return old_subject == new_subject
        return str(acc.get("email") or "") == email

    def _matches_xai_target(acc: dict) -> bool:
        if _acc_provider(acc) != "xai":
            return False
        acc_subject = _xai_subject(acc)
        new_subject = _xai_subject(normalized)
        if acc_subject and new_subject:
            return acc_subject == new_subject
        # Legacy/imported entries may not have subject yet.  Same email is only
        # used when at least one side lacks subject, so two known xAI subjects
        # sharing an email never collapse into one account.
        return str(acc.get("email") or "") == email

    def _matches_antigravity_target(acc: dict) -> bool:
        if _acc_provider(acc) != "antigravity":
            return False
        return (
            str(acc.get("email") or "") == email
            and _antigravity_project_id(acc) == _antigravity_project_id(normalized)
        )

    added = {"v": False}
    existing_target = None
    for a in config.get().get("oauthAccounts", []):
        if provider == "openai":
            if _acc_provider(a) == provider and _canonical_key(a) == normalized_key:
                existing_target = a
                break
        elif provider == "xai":
            if _matches_xai_target(a):
                existing_target = a
                break
        elif provider == "antigravity":
            if _matches_antigravity_target(a):
                existing_target = a
                break
        elif provider == "cursor":
            if _matches_cursor_target(a):
                existing_target = a
                break
        elif a.get("email") == email and _acc_provider(a) == provider:
            existing_target = a
            break
    rename_old_key = _canonical_key(existing_target) if existing_target else ""
    rename_new_key = normalized_key if rename_old_key and rename_old_key != normalized_key else ""
    from . import channel_state
    if existing_target is None or rename_new_key:
        channel_state.assert_reusable(f"oauth:{normalized_key}")
    if rename_new_key:
        for account in config.get().get("oauthAccounts", []):
            if account is not existing_target and _canonical_key(account) == normalized_key:
                raise ValueError(f"OAuth account identity already exists: {normalized_key}")
    if provider == "openai":
        from .openai.codex_identity import (
            account_identity_from_account,
            register_account_identity,
        )
        incoming_explicit_identity = (
            "codexIdentity" in entry or "codexDeviceInstallationId" in entry
        )
        if existing_target is not None:
            existing_identity = account_identity_from_account(existing_target, require=False)
            incoming_identity = account_identity_from_account(normalized, require=False)
            if (
                incoming_explicit_identity
                and existing_identity is not None
                and incoming_identity is not None
                and existing_identity.installation_id != incoming_identity.installation_id
            ):
                raise ValueError("OpenAI OAuth import cannot rotate an existing Codex identity")
            if not incoming_explicit_identity and existing_identity is not None:
                normalized["codexIdentity"] = existing_identity.as_config()
                normalized["codexDeviceInstallationId"] = existing_identity.installation_id
        # Claim before credentials are published.  A later config write failure may
        # leave only the non-secret tombstone, which safely preserves continuity.
        if account_identity_from_account(normalized, require=False) is not None:
            register_account_identity(normalized)

    existing_snapshot = copy.deepcopy(existing_target) if existing_target else None
    old_load_balancing = copy.deepcopy(
        config.get().get("loadBalancing", {})
    )

    def mutate(cfg):
        accounts = cfg.setdefault("oauthAccounts", [])
        target: dict | None = None
        if provider == "openai":
            for a in accounts:
                if _acc_provider(a) != provider:
                    continue
                if _canonical_key(a) == normalized_key:
                    target = a
                    break
        elif provider == "xai":
            for a in accounts:
                if _matches_xai_target(a):
                    target = a
                    break
        elif provider == "antigravity":
            for a in accounts:
                if _matches_antigravity_target(a):
                    target = a
                    break
        elif provider == "cursor":
            for a in accounts:
                if _matches_cursor_target(a):
                    target = a
                    break
        else:
            for a in accounts:
                if a.get("email") == email and _acc_provider(a) == provider:
                    target = a
                    break

        if target is not None:
            if provider not in ("openai", "xai", "cursor", "antigravity"):
                raise ValueError(
                    f"account already exists: provider={provider} email={email}"
                )
            keep_models = target.get("models")
            keep_model_policy = {
                key: copy.deepcopy(target.get(key))
                for key in (
                    "disabledModels", "account_model_catalog", "cursor_model_catalog",
                    "cursor_disabled_models", "cursor_max_context_disabled_models",
                    "last_model_sync", "last_model_sync_source",
                    "last_model_sync_error", "last_model_sync_attempt",
                    "last_model_sync_client_version", "last_model_sync_profile",
                    "last_model_sync_attempt_client_version", "last_model_sync_attempt_profile",
                    "models_etag", "models_etag_client_version", "models_etag_profile",
                ) if key in target
            }
            keep_max = target.get("maxConcurrent")
            keep_device_id = target.get("codexDeviceInstallationId")
            keep_codex_identity = copy.deepcopy(target.get("codexIdentity"))
            keep_enabled = target.get("enabled")
            keep_disabled_reason = target.get("disabled_reason")
            keep_disabled_until = target.get("disabled_until")
            keep_cursor_profile = {
                key: target.get(key)
                for key in (
                    "email", "label", "cursor_profile_name",
                    "cursor_profile_id", "cursor_email_verified",
                )
            }
            target.update(normalized)
            if keep_models is not None and not entry.get("models"):
                target["models"] = keep_models
            for key, value in keep_model_policy.items():
                if key not in entry:
                    target[key] = value
            if keep_max is not None and "maxConcurrent" not in entry:
                target["maxConcurrent"] = keep_max
            if provider == "cursor" and keep_disabled_reason in {"user", "quota"}:
                target["enabled"] = keep_enabled
                target["disabled_reason"] = keep_disabled_reason
                target["disabled_until"] = keep_disabled_until
            if (
                provider == "cursor"
                and not entry.get("cursor_profile_id")
                and keep_cursor_profile.get("cursor_profile_id")
            ):
                # A transient cursor.com profile failure during re-login must not
                # replace previously verified identity metadata with a hash label.
                target.update(keep_cursor_profile)
            # Same canonical OpenAI workspace reimport preserves the versioned
            # identity whenever the import omitted identity state.
            if (
                provider == "openai"
                and "codexDeviceInstallationId" not in entry
                and keep_device_id is not None
            ):
                target["codexDeviceInstallationId"] = keep_device_id
            if (
                provider == "openai"
                and "codexIdentity" not in entry
                and keep_codex_identity is not None
            ):
                target["codexIdentity"] = keep_codex_identity
            if rename_new_key:
                fam = "openai" if _is_openai_family_provider(provider) else "anthropic"
                _rename_priority_orders_in_config(
                    cfg,
                    f"oauth:{rename_old_key}",
                    f"oauth:{rename_new_key}",
                    fam,
                )
            return
        accounts.append(normalized)
        added["v"] = True

    def rollback(cfg):
        if existing_snapshot is not None:
            accounts = cfg.get("oauthAccounts", [])
            for index, account in enumerate(accounts):
                if _canonical_key(account) == rename_new_key:
                    accounts[index] = copy.deepcopy(existing_snapshot)
                    break
        cfg["loadBalancing"] = copy.deepcopy(old_load_balancing)

    if rename_new_key:
        _rename_runtime_oauth_identity(
            rename_old_key,
            rename_new_key,
            email=email,
            config_mutator=mutate,
            rollback_mutator=rollback,
        )
    else:
        config.update(mutate)
    if added["v"]:
        load_balancing.sync_channel_added(
            f"oauth:{normalized_key}",
            "openai" if _is_openai_family_provider(provider) else "anthropic",
        )


def delete_account(account_key: str) -> None:
    with config.serialized_updates():
        _delete_account_serialized(account_key)


def _delete_account_serialized(account_key: str) -> None:
    """按 account_key 精确删除一个账号 + 级联清理。

    兼容：若入参是裸 email（老 API），按 email 删除（可能删掉多条同邮箱的老数据）。
    """
    configured_keys = {
        _canonical_key(account) for account in config.get().get("oauthAccounts", [])
    }
    if account_key not in configured_keys:
        from . import channel_state
        raw_channel = f"oauth:{account_key}"
        resolved_channel = channel_state.resolve(raw_channel)
        if resolved_channel != raw_channel:
            account_key = resolved_channel[len("oauth:"):]
    try:
        canonical = _resolve_existing_account_key(account_key)
    except AmbiguousOAuthAccountKey:
        # Legacy bare-email deletion intentionally removes every provider entry
        # sharing that email; ambiguity is only unsafe for single-account reads.
        if ":" in account_key:
            raise
        canonical = None
    has_prov = ":" in account_key
    target_provider, target_identity = _split_ak(account_key)

    def matches(account: dict) -> bool:
        if canonical:
            return _canonical_key(account) == canonical
        if account.get("email") != target_identity:
            return False
        if not has_prov:
            return True
        return _acc_provider(account) == target_provider

    matched_accounts = [
        copy.deepcopy(account)
        for account in config.get().get("oauthAccounts", [])
        if matches(account)
    ]
    cleanup_keys = [_canonical_key(account) for account in matched_accounts]
    if not cleanup_keys:
        return
    # Preserve only the non-secret owner→installation mapping before credentials
    # are removed. Failure aborts deletion rather than breaking continuity.
    from .openai.codex_identity import account_identity_from_account, register_account_identity
    removed_owner_digests: set[str] = set()
    for account in matched_accounts:
        identity = account_identity_from_account(account, require=False)
        if identity is not None:
            register_account_identity(account)
            removed_owner_digests.add(identity.owner_digest)
    channel_keys = {f"oauth:{key}" for key in cleanup_keys}

    def mutate(cfg):
        accounts = cfg.get("oauthAccounts", [])
        cfg["oauthAccounts"] = [account for account in accounts if not matches(account)]
        load_balancing.mutate_channels_removed(cfg, channel_keys)

    # Remove config + priority entries atomically, then keep every removed
    # generation tombstoned until restart. Late in-flight responses must not
    # recreate scorer/cooldown/affinity/quota rows after the cleanup commit.
    from . import affinity as _affinity, channel_state, concurrency
    from . import cooldown as _cooldown, scorer as _scorer
    with channel_state.mutation_lock:
        retirement_plan = {
            channel_key: sorted(channel_state.alias_sources(channel_key)) + [channel_key]
            for channel_key in channel_keys
        }
        frozen_limits = {
            generation_key: concurrency.capture_rename_limit(generation_key)
            for generation_keys in retirement_plan.values()
            for generation_key in generation_keys
        }
        for channel_key in channel_keys:
            channel_state.retire_deleted(channel_key)
        try:
            config.update(mutate)
        except BaseException:
            for channel_key in channel_keys:
                channel_state.restore_deleted(channel_key)
            raise
        for channel_key, generation_keys in retirement_plan.items():
            for generation_key in generation_keys:
                concurrency.retire_channel(
                    generation_key,
                    frozen_max=frozen_limits[generation_key],
                    deleted_target=channel_key,
                )

        for cleanup_key in cleanup_keys:
            ch_key = f"oauth:{cleanup_key}"
            _scorer.clear_stats(ch_key)
            _cooldown.clear(
                ch_key, notify_recovered=False, resolve_alias=False,
            )
            _affinity.delete_by_channel(ch_key)
            _affinity.client_delete_by_channel(ch_key)
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
            if cleanup_key.startswith("cursor:"):
                try:
                    from .cursor_bridge import runtime as cursor_bridge_runtime
                    cursor_bridge_runtime.drop_account(cleanup_key)
                except Exception:
                    pass

        # Credential deletion destroys session/reasoning/compaction state but
        # deliberately retains the owner installation tombstone registered above.
        from .openai import reasoning_replay
        for owner_digest in removed_owner_digests:
            state_db.codex_logical_session_delete_owner(owner_digest)
            state_db.compaction_owner_delete_owner(owner_digest)
            reasoning_replay.delete_owner(owner_digest)


def forget_codex_identity(owner_digest: str) -> bool:
    """Explicitly forget a deleted OAuth owner's installation identity.

    This high-risk operation is intentionally not coupled to the existing delete
    UI. Credentials must be removed first so a live account cannot silently rotate.
    """
    from .openai.codex_identity import account_identity_from_account, forget_owner_identity
    owner = str(owner_digest or "").strip()
    for account in config.get().get("oauthAccounts", []):
        if _acc_provider(account) != "openai":
            continue
        identity = account_identity_from_account(account, require=False)
        if identity is not None and identity.owner_digest == owner:
            raise ValueError("delete OAuth credentials before forgetting Codex identity")
    from .openai import reasoning_replay
    reasoning_replay.delete_owner(owner)
    return forget_owner_identity(owner)


_EXPECTED_REASON_UNSET = object()
_EXPECTED_QUOTA_GENERATION_UNSET = object()


def set_enabled(account_key: str, enabled: bool, reason: str | None = None,
                disabled_until: str | None = None, *,
                expected_disabled_reason=_EXPECTED_REASON_UNSET,
                expected_quota_observation_generation=_EXPECTED_QUOTA_GENERATION_UNSET,
                ) -> dict | None:
    """Set account state with optional quota reason/generation CAS guards."""
    canonical = _resolve_existing_account_key(account_key)
    has_prov = ":" in account_key
    target_provider, target_identity = _split_ak(account_key)
    reason_conditional = expected_disabled_reason is not _EXPECTED_REASON_UNSET
    generation_conditional = (
        expected_quota_observation_generation is not _EXPECTED_QUOTA_GENERATION_UNSET
    )
    conditional = reason_conditional or generation_conditional
    decision = {
        "state": "missing",
        "disabled_reason": None,
        "disabled_until": None,
        "quota_observation_generation": None,
    }

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
            if conditional:
                current_reason = acc.get("disabled_reason")
                current_enabled = acc.get("enabled")
                current_generation = _quota_observation_generation(acc)
                decision.update(disabled_reason=current_reason,
                                disabled_until=acc.get("disabled_until"),
                                quota_observation_generation=current_generation)
                if type(current_enabled) is not bool:
                    decision["state"] = "invalid_state"
                    return
                if current_enabled:
                    decision["state"] = (
                        "already_enabled" if not current_reason else "invalid_state"
                    )
                    return
                if reason_conditional and current_reason != expected_disabled_reason:
                    decision["state"] = "state_conflict"
                    return
                if generation_conditional and (
                    current_generation is None
                    or current_generation != expected_quota_observation_generation
                ):
                    decision["state"] = "quota_observation_conflict"
                    return
                decision["state"] = "enabled"
            acc["enabled"] = enabled
            if enabled:
                acc["disabled_reason"] = None
                acc["disabled_until"] = None
                acc.pop(_QUOTA_OBSERVATION_FIELD, None)
            else:
                acc["disabled_reason"] = reason or "user"
                acc["disabled_until"] = disabled_until
                if (reason or "user") != "quota":
                    acc.pop(_QUOTA_OBSERVATION_FIELD, None)
            return
    config.update(mutate, skip_if_unchanged=conditional)
    return decision if conditional else None


def set_disabled_by_quota(account_key: str, resets_at: str | None, *,
                          observation: dict | None = None) -> dict:
    """Persist a quota disable and advance its observation generation.

    Repeated real-time observations keep the original disabled_until but still
    advance the generation. This makes a concurrent recovery CAS fail even when
    the auxiliary SQLite snapshot could not be written.
    """
    canonical = _resolve_existing_account_key(account_key)
    has_prov = ":" in account_key
    target_provider, target_identity = _split_ak(account_key)
    decision = {
        "state": "missing",
        "disabled_reason": None,
        "disabled_until": None,
        "quota_observation_generation": None,
    }

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

            current_enabled = acc.get("enabled", True)
            current_reason = acc.get("disabled_reason")
            decision.update(
                disabled_reason=current_reason,
                disabled_until=acc.get("disabled_until"),
            )
            if type(current_enabled) is not bool:
                decision["state"] = "invalid_state"
                return
            if current_reason not in (None, "quota"):
                decision["state"] = "state_conflict"
                return
            if current_enabled and current_reason is not None:
                decision["state"] = "invalid_state"
                return
            if not current_enabled and current_reason != "quota":
                decision["state"] = "invalid_state"
                return

            current_generation = _quota_observation_generation(acc)
            next_generation = (current_generation if current_generation is not None else 0) + 1
            acc[_QUOTA_OBSERVATION_GENERATION_FIELD] = next_generation
            if isinstance(observation, dict):
                merged_observation = _merge_codex_quota_observations(
                    acc.get(_QUOTA_OBSERVATION_FIELD),
                    observation,
                )
                acc[_QUOTA_OBSERVATION_FIELD] = copy.deepcopy(
                    merged_observation or observation,
                )

            if current_enabled:
                acc["enabled"] = False
                acc["disabled_reason"] = "quota"
                acc["disabled_until"] = resets_at
                decision["state"] = "disabled"
                decision["disabled_reason"] = "quota"
                decision["disabled_until"] = resets_at
            else:
                decision["state"] = "already_quota_disabled"
            decision["quota_observation_generation"] = next_generation
            return

    config.update(mutate, skip_if_unchanged=True)
    return decision


def _clear_oauth_runtime_state(canonical: str, *, clear_quota_cache: bool,
                               notify_recovered: bool = True) -> dict:
    """Clear local runtime state for one OAuth channel.

    `clear_quota_cache=False` preserves the fresh quota row used to prove
    recovery. ``required_state_cleared`` is true only when every persistent
    state required for routing recovery was committed successfully. Callers
    which must persist a later account-enable commit pass ``notify_recovered=False``
    so clearing an intermediate blocker cannot emit a false recovery notice.
    """
    ch_key = f"oauth:{canonical}"
    out = {
        "channel_key": ch_key,
        "cooldown_cleared": False,
        "quota_cache_cleared": False,
        "snapshots_cleared": False,
    }
    try:
        from . import cooldown
        cooldown.clear(
            ch_key, model=None, notify_recovered=notify_recovered,
        )
        out["cooldown_cleared"] = True
    except Exception as exc:
        out["cooldown_error"] = type(exc).__name__
        print(f"[oauth] runtime clear cooldown failed for {canonical}: {exc}")

    if clear_quota_cache:
        try:
            state_db.quota_delete(canonical)
            out["quota_cache_cleared"] = True
        except Exception as exc:
            out["quota_cache_error"] = type(exc).__name__
            print(f"[oauth] runtime clear quota cache failed for {canonical}: {exc}")

    try:
        from . import failover
        failover.forget_codex_snapshot(canonical)
        failover.forget_anthropic_snapshot(canonical)
        out["snapshots_cleared"] = True
    except Exception as exc:
        out["snapshot_error"] = type(exc).__name__
        print(f"[oauth] runtime clear snapshot failed for {canonical}: {exc}")
    forget_openai_probe(canonical)
    out["required_state_cleared"] = bool(
        out["cooldown_cleared"]
        and (not clear_quota_cache or out["quota_cache_cleared"])
    )
    return out


def reset_quota(account_key: str) -> dict:
    """Manually clear local quota/cooldown state for one OAuth account.

    This does not reset upstream limits; it only clears Parrot's local
    quota-disabled state and model cooldown so
    the account can participate in routing again. User-disabled and auth_error
    accounts are intentionally left untouched. A quota-disabled account is only
    enabled after every required local blocker was durably cleared.
    """
    acc = get_account(account_key)
    if acc is None:
        return {"action": "noop_missing", "account_key": account_key}

    canonical = _canonical_key(acc)
    reason = acc.get("disabled_reason")
    if reason in ("user", "auth_error"):
        return {"action": f"noop_{reason}", "account_key": canonical,
                "disabled_reason": reason}

    # For quota-disabled accounts, TG is the final success receipt. Suppress the
    # lower-level channel recovery notice until the account-enable commit exists;
    # on failure there must be no recovery signal at all.
    runtime = _clear_oauth_runtime_state(
        canonical,
        clear_quota_cache=True,
        notify_recovered=reason != "quota",
    )
    if not runtime.get("required_state_cleared"):
        return {
            "action": "reset_failed",
            "error_code": "runtime_state_clear_failed",
            "account_key": canonical,
            "disabled_reason": reason,
            **runtime,
        }

    if reason == "quota":
        try:
            enable_result = set_enabled(
                canonical, True, expected_disabled_reason="quota",
            )
        except Exception as exc:
            print(f"[oauth] reset quota enable failed for {canonical}: {exc}")
            return {
                "action": "reset_failed",
                "error_code": "account_enable_failed",
                "enable_error": type(exc).__name__,
                "account_key": canonical,
                "disabled_reason": reason,
                **runtime,
            }
        enable_state = (enable_result or {}).get("state")
        if enable_state == "enabled":
            action = "reset"
        elif enable_state == "already_enabled":
            action = "already_enabled"
        elif enable_state == "missing":
            action = "noop_missing"
        else:
            current_reason = (enable_result or {}).get("disabled_reason")
            action = (f"noop_{current_reason}"
                      if current_reason in ("user", "auth_error")
                      else enable_state or "state_conflict")
            return {"action": action, "error_code": "account_state_conflict",
                    "account_key": canonical, "disabled_reason": current_reason,
                    "disabled_until": (enable_result or {}).get("disabled_until"),
                    **runtime}
    else:
        action = "cleared_runtime_state"

    return {"action": action, "account_key": canonical,
            "disabled_reason": reason, **runtime}


async def redeem_openai_rate_limit_reset_credit(account_key: str,
                                                *, idempotency_key: str | None = None) -> dict:
    """Consume one official OpenAI/Codex banked reset credit for an account.

    OpenAI Codex now exposes earned rate-limit reset credits via WHAM. This path
    consumes an upstream credit first; only `reset` / `alreadyRedeemed` outcomes
    clear Parrot's local quota-disabled/cooldown/cache state.
    """
    acc = get_account(account_key)
    if acc is None:
        return {"action": "noop_missing", "account_key": account_key}

    canonical = _canonical_key(acc)
    if provider_of(acc) != "openai":
        return {"action": "not_openai", "account_key": canonical}
    if acc.get("disabled_reason") == "user":
        return {"action": "noop_user", "account_key": canonical}
    if acc.get("disabled_reason") == "auth_error":
        return {"action": "noop_auth_error", "account_key": canonical}

    idem = idempotency_key or str(uuid.uuid4())
    access_token = await ensure_valid_token(canonical)
    account_id = _openai_workspace_id(acc) or None
    response = await openai_provider.consume_rate_limit_reset_credit(
        access_token, idempotency_key=idem, account_id=account_id,
    )
    outcome = response.get("outcome")
    out = {
        "action": "upstream_reset_result",
        "account_key": canonical,
        "outcome": outcome,
        "idempotency_key": idem,
        "windows_reset": response.get("windows_reset"),
    }

    if outcome not in ("reset", "alreadyRedeemed"):
        return out

    # Official Codex UI refetches rate limits after consuming a reset. Do the
    # same before clearing any local quota restriction: fresh usage must prove
    # the windows are below threshold before Parrot auto-resumes a quota-disabled
    # account. If this fetch fails, keep the local restriction in place.
    try:
        usage = await fetch_usage_snapshot(canonical)
    except Exception as exc:
        out["refresh_error"] = str(exc)
        out["quota_action"] = {"action": "refresh_failed_keep_disabled"}
        return out

    # A successful upstream reset makes any previously cached Codex response-header
    # hit stale. Delete the row before writing fresh WHAM usage so old codex_* columns
    # do not immediately re-disable the account on the next monitor tick.
    state_db.quota_delete(canonical)
    state_db.quota_save(canonical, flatten_usage(usage), email=str(acc.get("email") or ""))
    out["usage"] = usage
    reset_credits = ((usage.get("openai") or {}).get("rate_limit_reset_credits") or {})
    if isinstance(reset_credits, dict) and reset_credits.get("available_count") is not None:
        out["available_count"] = reset_credits.get("available_count")

    eval_result = evaluate_and_toggle_by_usage(canonical, usage, fresh=True)
    out["quota_action"] = eval_result
    if eval_result.get("action") == "resumed":
        out["runtime_clear"] = eval_result.get("runtime_state")
    elif eval_result.get("action") == "kept_enabled" and not eval_result.get("any_over"):
        out["runtime_clear"] = _clear_oauth_runtime_state(canonical, clear_quota_cache=False)
    return out


def _openai_metadata_new_fields(acc: dict, info: dict) -> dict:
    fields: dict[str, str] = {}
    for k in ("plan_type", "subscription_expires_at", "workspace_type", "organization_id"):
        v = info.get(k)
        if v not in (None, ""):
            fields[k] = str(v)

    incoming_name = str(info.get("workspace_name") or "").strip()
    if incoming_name:
        existing_name = str(acc.get("workspace_name") or "").strip()
        existing_type = str(acc.get("workspace_type") or acc.get("plan_type") or "").lower()
        if not (
            incoming_name.lower() == "personal"
            and "team" in existing_type
            and existing_name
            and existing_name.lower() not in {"personal", "workspace", "team"}
        ):
            fields["workspace_name"] = incoming_name
    return fields


def refresh_openai_metadata_sync(account_key: str, *,
                                 force: bool = False,
                                 min_interval_seconds: int = 3600) -> dict:
    """Refresh OpenAI account plan/workspace metadata without rotating tokens."""
    canonical = _resolve_existing_account_key(account_key)
    if canonical:
        account_key = canonical
    acc = get_account(account_key)
    if acc is None:
        return {"action": "noop_missing", "account_key": account_key}
    if provider_of(acc) != "openai":
        return {"action": "noop_not_openai", "account_key": _canonical_key(acc)}

    canonical = _canonical_key(acc)
    if not force:
        last = _parse_iso(acc.get("last_metadata_refresh"))
        if last is not None:
            age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
            if age < max(0, int(min_interval_seconds)):
                return {"action": "skipped_fresh", "account_key": canonical, "age_seconds": int(age)}

    access_token = acc.get("access_token") or ""
    if not access_token:
        return {"action": "skipped_no_access_token", "account_key": canonical}
    expired = _parse_iso(acc.get("expired"))
    if expired is not None and (expired - datetime.now(timezone.utc)).total_seconds() <= 60:
        return {"action": "skipped_token_expiring", "account_key": canonical}

    metadata_kwargs = {
        "org_id": acc.get("organization_id") or None,
        "workspace_id": _openai_workspace_id(acc) or None,
        "email": str(acc.get("email") or "") or None,
    }
    metadata_kwargs.update(_compatible_kwargs(
        openai_provider.fetch_accounts_check_sync,
        proxy_channel=f"oauth:{canonical}",
    ))
    info = openai_provider.fetch_accounts_check_sync(
        access_token, **metadata_kwargs,
    )
    if not info:
        return {"action": "fetch_no_metadata", "account_key": canonical}

    fields = _openai_metadata_new_fields(acc, info)
    fields["last_metadata_refresh"] = _format_utc(datetime.now(timezone.utc))

    def mutate(cfg):
        for item in cfg.get("oauthAccounts", []):
            if _canonical_key(item) == canonical:
                item.update(fields)
                return
    config.update(mutate)
    return {"action": "updated", "account_key": canonical, "fields": fields}


async def ensure_openai_metadata_fresh(account_key: str, *,
                                       force: bool = False,
                                       min_interval_seconds: int = 3600,
                                       timeout_s: float = 5.0) -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                refresh_openai_metadata_sync, account_key,
                force=force, min_interval_seconds=min_interval_seconds,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        return {"action": "timeout", "account_key": account_key}
    except Exception as exc:
        return {"action": "error", "account_key": account_key, "error": str(exc)}


async def ensure_openai_metadata_fresh_many(account_keys: list[str], *,
                                            force: bool = False,
                                            min_interval_seconds: int = 3600,
                                            timeout_s: float = 5.0) -> list[dict]:
    return await asyncio.gather(*[
        ensure_openai_metadata_fresh(
            k, force=force, min_interval_seconds=min_interval_seconds, timeout_s=timeout_s,
        ) for k in account_keys
    ])


def ensure_openai_metadata_fresh_sync(account_keys: list[str] | str, *,
                                      force: bool = False,
                                      min_interval_seconds: int = 3600,
                                      timeout_s: float = 5.0) -> None:
    try:
        if isinstance(account_keys, str):
            asyncio.run(ensure_openai_metadata_fresh(
                account_keys, force=force, min_interval_seconds=min_interval_seconds,
                timeout_s=timeout_s,
            ))
        else:
            asyncio.run(ensure_openai_metadata_fresh_many(
                account_keys, force=force, min_interval_seconds=min_interval_seconds,
                timeout_s=timeout_s,
            ))
    except Exception as exc:
        print(f"[oauth] ensure_openai_metadata_fresh_sync error: {exc}")


def _provider_default_models(provider: str) -> tuple[list[str], str]:
    """Return an explicit provider fallback; OpenAI falls back only to its profile."""
    cfg = config.get()
    section = {
        "openai": "openaiOAuth",
        "xai": "xaiOAuth",
        "antigravity": "antigravityOAuth",
    }.get(provider)
    if provider == "claude":
        configured = cfg.get("oauthDefaultModels")
        built_in = config.DEFAULT_CONFIG.get("oauthDefaultModels") or []
    else:
        configured = (cfg.get(section) or {}).get("defaultModels") if section else []
        built_in = ((config.DEFAULT_CONFIG.get(section) or {}).get("defaultModels") or []) if section else []
    models = list(dict.fromkeys(
        str(model).strip() for model in configured or [] if str(model).strip()
    ))
    if models:
        return models, "default:configured"
    if provider == "openai":
        try:
            profile = codex_protocol_profile()
        except Exception:
            # Invalid pinned configuration must not revive a mutable Python fallback.
            return [], "profile:unavailable"
        return list(profile.models), f"profile:{profile.profile_id}"
    return list(dict.fromkeys(
        str(model).strip() for model in built_in if str(model).strip()
    )), "default:built-in"


def account_disabled_models(account_or_key: dict | str) -> set[str]:
    account = account_or_key if isinstance(account_or_key, dict) else get_account(account_or_key)
    if not isinstance(account, dict):
        return set()
    field = "cursor_disabled_models" if provider_of(account) == "cursor" else "disabledModels"
    return {str(model).strip() for model in account.get(field) or [] if str(model).strip()}


def account_model_records(account_or_key: dict | str) -> list[dict]:
    """Expose provider-neutral, ID-scoped records without decorating fallbacks."""
    account = account_or_key if isinstance(account_or_key, dict) else get_account(account_or_key)
    if not isinstance(account, dict):
        return []
    if provider_of(account) == "cursor":
        from .cursor_bridge import catalog as cursor_catalog

        records = []
        for item in cursor_catalog.catalog_records(account):
            metadata = cursor_catalog.metadata_from_record(item)
            records.append({
                "id": str(item["id"]), "name": metadata.get("name"),
                "description": metadata.get("description"),
                "contextWindow": metadata.get("contextWindow"),
                "contextWindowMaxMode": metadata.get("contextWindowMaxMode"),
                "maxOutputTokens": metadata.get("maxOutputTokens"),
                "reasoning": metadata.get("reasoning"),
                "reasoningEfforts": list(metadata.get("reasoningEfforts") or []),
                "supportsImages": metadata.get("supportsImages"),
                "cursorUpstreamVision": metadata.get("cursorUpstreamVision"),
                "aliases": list(metadata.get("aliases") or []),
            })
        return records
    raw = ((account.get("account_model_catalog") or {}).get("models") or [])
    return [copy.deepcopy(item) for item in raw if isinstance(item, dict) and str(item.get("id") or "").strip()]


def account_model_selection(account_or_key: dict | str) -> dict:
    """Resolve one account's selected source without adding runtime state."""
    account = account_or_key if isinstance(account_or_key, dict) else get_account(account_or_key)
    if not isinstance(account, dict):
        raise ValueError("OAuth account not found")
    provider = provider_of(account)
    records = account_model_records(account)
    configured_models = list(dict.fromkeys(
        str(model).strip() for model in account.get("models") or [] if str(model).strip()
    ))
    if provider == "cursor":
        # Cursor routes only the intersection of the last successful model list
        # and native catalog. Neither a legacy list nor stale records may revive
        # a model independently after discovery has failed.
        native_ids = {
            str(item.get("id") or "").strip()
            for item in records if str(item.get("id") or "").strip()
        }
        models = [model for model in configured_models if model in native_ids]
    else:
        models = configured_models
    if models:
        source = str(account.get("last_model_sync_source") or "lkg:legacy-config")
        fallback = False
    else:
        models, source = _provider_default_models(provider)
        fallback = True
    disabled = account_disabled_models(account)
    return {
        "models": models,
        "effective_models": [model for model in models if model not in disabled],
        "disabled_models": disabled,
        "source": source,
        "fallback": fallback,
        "synced_at": str(account.get("last_model_sync") or ""),
        "attempted_at": str(account.get("last_model_sync_attempt") or ""),
        "error": str(account.get("last_model_sync_error") or ""),
        # Cursor records follow the intersection above; other providers retain
        # their existing account-catalog behavior. Stateless fallbacks expose no
        # records for any provider.
        "records": (
            [item for item in records if str(item.get("id") or "").strip() in models]
            if provider == "cursor" and not fallback
            else records if not fallback else []
        ),
    }


def set_account_model_disabled(account_key: str, model: str, disabled: bool) -> bool:
    """Atomically persist one non-Cursor user preference; hidden IDs are retained."""
    canonical = _resolve_existing_account_key_or_raise(account_key)
    model_id = str(model or "").strip()
    account = get_account(canonical)
    if not model_id or not isinstance(account, dict):
        raise ValueError("OAuth account/model required")
    selected = set(account_model_selection(account)["models"])
    if model_id not in selected:
        raise ValueError(f"OAuth account model not found: {model_id}")
    wanted = bool(disabled)

    def mutate(cfg):
        for item in cfg.get("oauthAccounts", []):
            if _canonical_key(item) != canonical:
                continue
            values = account_disabled_models(item)
            if wanted:
                values.add(model_id)
            else:
                values.discard(model_id)
            field = "cursor_disabled_models" if provider_of(item) == "cursor" else "disabledModels"
            item[field] = sorted(values)
            return
        raise ValueError("OAuth account not found")

    config.update(mutate, skip_if_unchanged=True)
    return wanted


def set_account_disabled_models(
    account_key: str,
    models: Iterable[str],
    *,
    visible_models: Iterable[str] | None = None,
) -> set[str]:
    """Persist one batch draft while retaining disabled IDs outside its snapshot.

    ``visible_models`` is the catalog snapshot shown by the Telegram editor. A
    background catalog refresh may add/remove IDs while that draft is open, so
    saving may replace only the snapshot's scope. Hidden disabled IDs remain in
    config and apply again if an upstream model later reappears.
    """
    canonical = _resolve_existing_account_key_or_raise(account_key)
    account = get_account(canonical)
    if not isinstance(account, dict):
        raise ValueError("OAuth account required")

    visible = {
        str(model).strip() for model in (
            visible_models if visible_models is not None
            else account_model_selection(account)["models"]
        )
        if str(model).strip()
    }
    selected = {
        str(model).strip() for model in models or [] if str(model).strip()
    }
    unknown = sorted(selected - visible)
    if unknown:
        raise ValueError(f"OAuth account model not in editor snapshot: {unknown[0]}")
    saved: set[str] = set()

    def mutate(cfg):
        nonlocal saved
        for item in cfg.get("oauthAccounts", []):
            if _canonical_key(item) != canonical:
                continue
            hidden = account_disabled_models(item) - visible
            saved = hidden | selected
            field = "cursor_disabled_models" if provider_of(item) == "cursor" else "disabledModels"
            item[field] = sorted(saved)
            return
        raise ValueError("OAuth account not found")

    config.update(mutate, skip_if_unchanged=True)
    return set(saved)


_model_discovery_flights: dict[str, concurrent.futures.Future] = {}
_model_discovery_tasks_guard = threading.Lock()
# The gate is shared by scheduler, manual refreshes and foreground account-add
# workers, so the limit is truly process-wide rather than per event loop.
# The network executor itself is the process-wide gate. A timed-out/cancelled
# coroutine cannot free a slot while its non-cancellable thread is still alive.
_model_discovery_network_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=OAUTH_MODEL_SYNC_MAX_CONCURRENCY, thread_name_prefix="oauth-model-network",
)
_model_discovery_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=OAUTH_MODEL_SYNC_MAX_CONCURRENCY, thread_name_prefix="oauth-model-sync",
)


def _discovery_generation(account: dict) -> str:
    provider = provider_of(account)
    # A hot Codex identity change invalidates any in-flight model fetch as well
    # as the six-hour success TTL checked by ``_model_sync_due``.
    if provider == "openai":
        profile = codex_protocol_profile()
        client_identity = "\0".join((
            profile.profile_id, profile.client_version, codex_backend_base_url(),
        ))
    else:
        client_identity = ""
    raw = "\0".join((
        _canonical_key(account), provider,
        str(account.get("access_token") or ""),
        str(account.get("project_id") or account.get("workspace_id") or ""),
        client_identity,
    ))
    return hashlib.sha256(raw.encode()).hexdigest()


def _safe_discovery_error(exc: BaseException | str) -> str:
    """Return a bounded credential-free error summary suitable for config/UI."""
    if isinstance(exc, BaseException):
        return type(exc).__name__
    text = str(exc or "discovery failed")
    return text if text in {"timeout", "empty catalog", "fetch_empty"} else "discovery failed"


def _persist_model_discovery_failure(account_key: str, generation: str, error: str) -> bool:
    now = _format_utc(datetime.now(timezone.utc))
    changed = {"value": False}

    def mutate(cfg):
        for item in cfg.get("oauthAccounts", []):
            if _canonical_key(item) == account_key and _discovery_generation(item) == generation:
                item["last_model_sync_attempt"] = now
                item["last_model_sync_error"] = str(error or "discovery failed")[:500]
                if provider_of(item) == "openai":
                    profile = codex_protocol_profile()
                    item["last_model_sync_attempt_client_version"] = profile.client_version
                    item["last_model_sync_attempt_profile"] = profile.profile_id
                changed["value"] = True
                return

    config.update(mutate, skip_if_unchanged=True)
    return changed["value"]


async def _discover_account_models_once(account_key: str, *, timeout_s: float) -> dict:
    canonical = _resolve_existing_account_key_or_raise(account_key)
    deadline = time.monotonic() + max(0.001, float(timeout_s))
    initial = copy.deepcopy(get_account(canonical))
    if not isinstance(initial, dict):
        return {"action": "stale", "account_key": canonical}
    initial_generation = _discovery_generation(initial)
    try:
        await ensure_valid_token(canonical)
    except asyncio.TimeoutError:
        _persist_model_discovery_failure(canonical, initial_generation, "timeout")
        return {"action": "timeout", "account_key": canonical}
    except Exception as exc:
        error = _safe_discovery_error(exc)
        _persist_model_discovery_failure(canonical, initial_generation, error)
        return {"action": "error", "account_key": canonical, "error": error}

    # Refresh may atomically replace the token. All discovery and persistence
    # below must therefore use a fresh snapshot/generation, never the old one.
    account = copy.deepcopy(get_account(canonical))
    if not isinstance(account, dict):
        return {"action": "stale", "account_key": canonical}
    provider = provider_of(account)
    if mock_mode_enabled():
        return {"action": "network_disabled", "account_key": canonical}
    generation = _discovery_generation(account)
    remaining_s = deadline - time.monotonic()
    if remaining_s <= 0:
        _persist_model_discovery_failure(canonical, generation, "timeout")
        return {"action": "timeout", "account_key": canonical}
    loop = asyncio.get_running_loop()
    try:
        if provider == "cursor":
            result = await loop.run_in_executor(
                _model_discovery_network_executor,
                lambda: refresh_cursor_models_sync(
                    canonical, force=True, timeout_s=remaining_s,
                    expected_generation=generation,
                ),
            )
            return result
        def discover_with_context():
            snapshot = copy.deepcopy(account)
            kwargs = _compatible_kwargs(
                oauth_model_discovery.discover,
                timeout=remaining_s, proxy_channel=f"oauth:{canonical}",
            )
            return oauth_model_discovery.discover(snapshot, **kwargs)

        result = await loop.run_in_executor(
            _model_discovery_network_executor,
            discover_with_context,
        )
        if time.monotonic() >= deadline:
            _persist_model_discovery_failure(canonical, generation, "timeout")
            return {"action": "timeout", "account_key": canonical}
    except (asyncio.TimeoutError, TimeoutError):
        _persist_model_discovery_failure(canonical, generation, "timeout")
        return {"action": "timeout", "account_key": canonical}
    except Exception as exc:
        error = _safe_discovery_error(exc)
        _persist_model_discovery_failure(canonical, generation, error)
        return {"action": "error", "account_key": canonical, "error": error}

    if provider == "openai" and getattr(result, "not_modified", False):
        existing_models = _model_ids(account)
        if not existing_models or not _model_catalog_complete(account, existing_models):
            _persist_model_discovery_failure(canonical, generation, "empty catalog")
            return {"action": "empty", "account_key": canonical}
        now = _format_utc(datetime.now(timezone.utc))
        saved = {"value": False}

        def refresh_lkg(cfg):
            for item in cfg.get("oauthAccounts", []):
                if _canonical_key(item) != canonical or _discovery_generation(item) != generation:
                    continue
                item["last_model_sync"] = now
                item["last_model_sync_attempt"] = now
                item["last_model_sync_source"] = result.source
                item["last_model_sync_error"] = ""
                item["last_model_sync_client_version"] = str(result.client_version or "")
                item["last_model_sync_profile"] = str(result.profile_id or "")
                if str(result.etag or ""):
                    item["models_etag"] = str(result.etag)
                    item["models_etag_client_version"] = str(result.client_version or "")
                    item["models_etag_profile"] = str(result.profile_id or "")
                saved["value"] = True
                return

        config.update(refresh_lkg, skip_if_unchanged=True)
        return {
            "action": "not_modified" if saved["value"] else "stale",
            "account_key": canonical, "models": len(existing_models),
            "source": result.source, "fetched_at": now,
        }

    models = list(dict.fromkeys(str(model).strip() for model in result.models if str(model).strip()))
    if not models:
        _persist_model_discovery_failure(canonical, generation, "empty catalog")
        return {"action": "empty", "account_key": canonical}
    now = _format_utc(datetime.now(timezone.utc))
    saved = {"value": False}

    def mutate(cfg):
        for item in cfg.get("oauthAccounts", []):
            if _canonical_key(item) != canonical or _discovery_generation(item) != generation:
                continue
            # Atomic LKG replacement: IDs, allow-listed metadata and sync facts
            # are committed by one config mutation. Errors/empty never reach here.
            item["models"] = models
            item["account_model_catalog"] = copy.deepcopy(getattr(result, "catalog", {}) or {})
            item["last_model_sync"] = now
            item["last_model_sync_attempt"] = now
            item["last_model_sync_source"] = result.source
            item["last_model_sync_error"] = ""
            if provider == "openai":
                result_version = (
                    str(getattr(result, "client_version", "") or "")
                    or codex_cli_version()
                )
                result_profile = (
                    str(getattr(result, "profile_id", "") or "")
                    or codex_protocol_profile().profile_id
                )
                item["last_model_sync_client_version"] = result_version
                item["last_model_sync_profile"] = result_profile
                item["models_etag"] = str(getattr(result, "etag", "") or "")
                item["models_etag_client_version"] = result_version
                item["models_etag_profile"] = result_profile
            saved["value"] = True
            return

    config.update(mutate, skip_if_unchanged=True)
    return {
        "action": "updated" if saved["value"] else "stale",
        "account_key": canonical, "models": len(models), "source": result.source,
        "fetched_at": now,
    }


def _model_ids(account: dict | None) -> list[str]:
    if not isinstance(account, dict):
        return []
    return list(dict.fromkeys(
        str(model).strip() for model in account.get("models") or [] if str(model).strip()
    ))


def _normalize_model_refresh_result(canonical: str, before: dict | None, result: dict) -> dict:
    """Attach catalog-delta facts without exposing account credentials."""
    after = get_account(canonical)
    old_ids = _model_ids(before)
    new_ids = _model_ids(after)
    old_set, new_set = set(old_ids), set(new_ids)
    normalized = dict(result or {})
    normalized.update({
        "account_key": canonical,
        "old_model_ids": old_ids,
        "new_model_ids": new_ids,
        "added": [model for model in new_ids if model not in old_set],
        "removed": [model for model in old_ids if model not in new_set],
        "had_success_baseline": bool((before or {}).get("last_model_sync")),
    })
    normalized["changed"] = bool(normalized["added"] or normalized["removed"])
    return normalized


async def refresh_account_models(
    account_key: str, *, timeout_s: float = OAUTH_MODEL_SYNC_REQUEST_TIMEOUT_SECONDS,
) -> dict:
    """Cross-event-loop single-flight with identity/token generation gating."""
    canonical = _resolve_existing_account_key_or_raise(account_key)
    owner = False
    with _model_discovery_tasks_guard:
        flight = _model_discovery_flights.get(canonical)
        if flight is None or flight.done():
            flight = concurrent.futures.Future()
            _model_discovery_flights[canonical] = flight
            owner = True
    if not owner:
        return await asyncio.wrap_future(flight)
    before = copy.deepcopy(get_account(canonical))
    generation = _discovery_generation(before or {})
    try:
        result = await _discover_account_models_once(canonical, timeout_s=timeout_s)
        # Cursor's native fetch owns its LKG, but the unified scheduler owns
        # retry metadata so all five providers obey the same backoff policy.
        if provider_of(before or {}) == "cursor":
            if result.get("action") == "updated" and int(result.get("models") or 0) > 0:
                now = str(result.get("fetched_at") or _format_utc(datetime.now(timezone.utc)))
                def mark_cursor_success(cfg):
                    for item in cfg.get("oauthAccounts", []):
                        if _canonical_key(item) == canonical and _discovery_generation(item) == generation:
                            item["last_model_sync"] = now
                            item["last_model_sync_attempt"] = now
                            item["last_model_sync_source"] = "upstream:cursor"
                            item["last_model_sync_error"] = ""
                            return
                config.update(mark_cursor_success, skip_if_unchanged=True)
            elif result.get("action") in {"timeout", "error", "fetch_empty", "profile_updated"}:
                _persist_model_discovery_failure(
                    canonical, generation,
                    str(result.get("error") or result.get("model_error") or result.get("action")),
                )
        normalized = _normalize_model_refresh_result(canonical, before, result)
        flight.set_result(normalized)
        return normalized
    except BaseException as exc:
        flight.set_exception(exc)
        try:
            flight.exception()
        except BaseException:
            pass
        raise
    finally:
        with _model_discovery_tasks_guard:
            if _model_discovery_flights.get(canonical) is flight:
                _model_discovery_flights.pop(canonical, None)


_codex_catalog_observation_lock = threading.Lock()
_codex_catalog_refresh_pending: set[tuple[str, str]] = set()
_codex_catalog_refresh_last: dict[tuple[str, str], float] = {}
_CODEX_CATALOG_REFRESH_DEBOUNCE_SECONDS = 60.0


def _observed_header(headers: Any, *names: str) -> str:
    wanted = {name.lower() for name in names}
    try:
        items = headers.items()
    except Exception:
        return ""
    for raw_name, raw_value in items:
        if str(raw_name).lower() not in wanted:
            continue
        value = str(raw_value or "").strip()
        if value and len(value) <= 512 and "\r" not in value and "\n" not in value:
            return value
    return ""


def observe_openai_response_metadata(
    account_key: str,
    headers: Any,
    translator_ctx: dict | None = None,
) -> dict[str, Any]:
    """Capture actual model and schedule one non-blocking catalog refresh on ETag drift."""
    actual_model = _observed_header(headers, "openai-model", "x-openai-model")
    if actual_model and isinstance(translator_ctx, dict):
        translator_ctx["codex_actual_model"] = actual_model

    etag = _observed_header(headers, "x-models-etag")
    if not etag:
        return {"actual_model": actual_model, "refresh_scheduled": False}
    try:
        canonical = _resolve_existing_account_key_or_raise(account_key)
        account = get_account(canonical)
        if not isinstance(account, dict) or provider_of(account) != "openai":
            return {"actual_model": actual_model, "refresh_scheduled": False}
        profile = codex_protocol_profile()
        cached_matches_scope = (
            str(account.get("models_etag_client_version") or "") == profile.client_version
            and str(account.get("models_etag_profile") or "") == profile.profile_id
        )
        if cached_matches_scope and str(account.get("models_etag") or "") == etag:
            return {"actual_model": actual_model, "refresh_scheduled": False}
        generation = _discovery_generation(account)
        key = (canonical, generation)
        now = time.monotonic()
        with _codex_catalog_observation_lock:
            if (
                key in _codex_catalog_refresh_pending
                or now - _codex_catalog_refresh_last.get(key, 0.0)
                < _CODEX_CATALOG_REFRESH_DEBOUNCE_SECONDS
            ):
                return {"actual_model": actual_model, "refresh_scheduled": False}
            loop = asyncio.get_running_loop()
            _codex_catalog_refresh_pending.add(key)
            _codex_catalog_refresh_last[key] = now

        async def refresh_observed_catalog() -> None:
            try:
                await refresh_account_models(canonical)
            except Exception:
                # Response observation is auxiliary; the existing LKG remains usable.
                pass
            finally:
                with _codex_catalog_observation_lock:
                    _codex_catalog_refresh_pending.discard(key)

        loop.create_task(
            refresh_observed_catalog(),
            name="codex-model-catalog-etag-refresh",
        )
        return {"actual_model": actual_model, "refresh_scheduled": True}
    except Exception:
        return {"actual_model": actual_model, "refresh_scheduled": False}


def observe_openai_response_event(
    account_key: str,
    frame: str | bytes | dict,
    translator_ctx: dict | None = None,
) -> dict[str, Any]:
    """Observe official WS response headers without rewriting the frame."""
    try:
        if isinstance(frame, bytes):
            event = json.loads(frame.decode("utf-8"))
        elif isinstance(frame, str):
            event = json.loads(frame)
        else:
            event = frame
    except Exception:
        return {"actual_model": "", "refresh_scheduled": False}
    if not isinstance(event, dict):
        return {"actual_model": "", "refresh_scheduled": False}
    merged: dict[str, Any] = {}
    top_headers = event.get("headers")
    if isinstance(top_headers, dict):
        merged.update(top_headers)
    response = event.get("response")
    response_headers = response.get("headers") if isinstance(response, dict) else None
    if isinstance(response_headers, dict):
        merged.update(response_headers)
    if not merged:
        return {"actual_model": "", "refresh_scheduled": False}
    return observe_openai_response_metadata(account_key, merged, translator_ctx)


def start_account_model_refresh(
    account_key: str, *, timeout_s: float = OAUTH_MODEL_SYNC_REQUEST_TIMEOUT_SECONDS,
) -> concurrent.futures.Future:
    """Start a non-cancelling worker suitable for a bounded foreground wait."""
    canonical = _resolve_existing_account_key_or_raise(account_key)
    return _model_discovery_executor.submit(
        lambda: asyncio.run(refresh_account_models(canonical, timeout_s=timeout_s))
    )


def refresh_cursor_models_sync(
    account_key: str,
    *,
    force: bool = False,
    min_interval_seconds: int | None = None,
    timeout_s: float = OAUTH_MODEL_SYNC_REQUEST_TIMEOUT_SECONDS,
    expected_generation: str | None = None,
) -> dict:
    """Refresh one Cursor account's canonical models and native metadata."""
    canonical = _resolve_existing_account_key(account_key)
    if canonical:
        account_key = canonical
    acc = get_account(account_key)
    if acc is None:
        return {"action": "noop_missing", "account_key": account_key}
    if provider_of(acc) != "cursor":
        return {"action": "noop_not_cursor", "account_key": _canonical_key(acc)}

    cfg = config.get().get("cursorOAuth") or {}
    if min_interval_seconds is None:
        min_interval_seconds = int(float(cfg.get("modelSyncHours", 6) or 6) * 3600)
    if not force:
        last = _parse_iso(acc.get("last_model_sync"))
        if last is not None:
            age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
            if age < max(0, int(min_interval_seconds)):
                return {"action": "skipped_fresh", "account_key": account_key, "age_seconds": int(age)}

    generation = expected_generation or _discovery_generation(acc)
    deadline = time.monotonic() + max(0.001, float(timeout_s))
    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Cursor model discovery deadline exhausted")
        return value

    access_token = str(acc.get("access_token") or "")
    if not access_token:
        return {"action": "skipped_no_access_token", "account_key": account_key}
    catalog: dict[str, Any] = {}
    models: list[str] = []
    fetched_at = ""
    model_error: Exception | None = None
    model_error_name = ""
    try:
        budget = remaining()
        catalog = cursor_provider.fetch_model_catalog_sync(
            access_token,
            **_compatible_kwargs(
                cursor_provider.fetch_model_catalog_sync,
                timeout=budget, account_key=account_key,
            ),
        )
        records = (catalog.get("models") or []) if isinstance(catalog, dict) else []
        models = [
            str(item.get("id") or "")
            for item in records
            if isinstance(item, dict) and item.get("id")
        ]
        if models:
            fetched_at = str(
                catalog.get("fetched_at") or _format_utc(datetime.now(timezone.utc))
            )
        else:
            model_error_name = "fetch_empty"
    except Exception as exc:
        model_error = exc
        model_error_name = type(exc).__name__

    profile: dict[str, Any] = {}
    profile_error = ""
    try:
        budget = remaining()
        profile = cursor_provider.fetch_profile_sync(
            access_token,
            **_compatible_kwargs(
                cursor_provider.fetch_profile_sync,
                account_key=account_key, timeout=budget,
            ),
        )
    except Exception as exc:
        # Models and account identity are independent last-known-good data. A
        # failure in either source must not discard a successful refresh of the other.
        profile_error = type(exc).__name__

    if not models and not profile:
        if model_error is not None:
            raise model_error
        return {
            "action": "fetch_empty",
            "account_key": account_key,
            "profile_error": profile_error,
        }

    if time.monotonic() >= deadline:
        return {"action": "timeout", "account_key": account_key}
    profile_email = str(profile.get("email") or "").strip()

    def mutate(current_cfg):
        for item in current_cfg.get("oauthAccounts", []):
            if (
                _canonical_key(item) == account_key
                and _discovery_generation(item) == generation
            ):
                if models:
                    item["models"] = list(dict.fromkeys(models))
                    item["cursor_model_catalog"] = copy.deepcopy(catalog)
                    item["last_model_sync"] = fetched_at
                    item["last_model_sync_source"] = "upstream:cursor"
                    item["last_model_sync_attempt"] = fetched_at
                    item["last_model_sync_error"] = ""
                if profile:
                    if profile_email:
                        item["email"] = profile_email
                        item["label"] = profile_email
                    item["cursor_profile_name"] = str(profile.get("name") or "").strip()
                    item["cursor_profile_id"] = str(profile.get("id") or "").strip()
                    verified = profile.get("email_verified")
                    item["cursor_email_verified"] = verified if isinstance(verified, bool) else None
                return

    config.update(mutate)
    return {
        "action": "updated" if models else "profile_updated",
        "account_key": account_key,
        "models": len(models),
        "fetched_at": fetched_at,
        "model_error": model_error_name,
        "profile_updated": bool(profile),
        "profile_error": profile_error,
    }


async def refresh_cursor_models(
    account_key: str,
    *,
    force: bool = False,
    min_interval_seconds: int | None = None,
    timeout_s: float = 30.0,
) -> dict:
    """Compatibility entry point using the same lifecycle-bound network worker."""
    try:
        account_key = _resolve_existing_account_key_or_raise(account_key)
        if not force:
            account = get_account(account_key)
            last = _parse_iso((account or {}).get("last_model_sync"))
            interval = min_interval_seconds
            if interval is None:
                cfg = config.get().get("cursorOAuth") or {}
                interval = int(float(cfg.get("modelSyncHours", 6) or 6) * 3600)
            if last is not None:
                age = (datetime.now(timezone.utc) - last.astimezone(timezone.utc)).total_seconds()
                if age < max(0, int(interval)):
                    return {"action": "skipped_fresh", "account_key": account_key, "age_seconds": int(age)}
        return await _discover_account_models_once(account_key, timeout_s=timeout_s)
    except Exception as exc:
        error = _safe_discovery_error(exc)
        return {"action": "error", "account_key": account_key, "error": error}


def _model_catalog_complete(account: dict, model_ids: Iterable[str]) -> bool:
    """Whether the provider-native catalog has a metadata record for every ID."""
    catalog_key = "cursor_model_catalog" if provider_of(account) == "cursor" else "account_model_catalog"
    catalog = account.get(catalog_key)
    records = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(records, list):
        return False
    catalog_ids = {
        str(record.get("id") or "").strip()
        for record in records
        if isinstance(record, dict) and str(record.get("id") or "").strip()
    }
    return set(model_ids).issubset(catalog_ids)


def _model_sync_due(account: dict, *, now: datetime | None = None) -> bool:
    """Whether IDs/metadata are missing, stale, or a failed attempt may be retried."""
    now = now or datetime.now(timezone.utc)
    last_success = _parse_iso(account.get("last_model_sync"))
    last_attempt = _parse_iso(account.get("last_model_sync_attempt"))
    failed = bool(account.get("last_model_sync_error"))
    if failed and last_attempt is not None:
        if provider_of(account) == "openai":
            profile = codex_protocol_profile()
            # Old-client failures cannot suppress the first upgraded fetch.
            # Failed-attempt identity is separate from the last successful sync
            # so another failure still observes the normal retry backoff.
            if (
                str(account.get("last_model_sync_attempt_client_version") or "")
                != profile.client_version
                or str(account.get("last_model_sync_attempt_profile") or "")
                != profile.profile_id
            ):
                return True
        return (now - last_attempt.astimezone(timezone.utc)).total_seconds() >= OAUTH_MODEL_SYNC_FAILURE_RETRY_SECONDS
    if provider_of(account) == "openai":
        profile = codex_protocol_profile()
        if (
            str(account.get("last_model_sync_client_version") or "")
            != profile.client_version
            or str(account.get("last_model_sync_profile") or "")
            != profile.profile_id
        ):
            return True
    model_ids = _model_ids(account)
    if not model_ids or last_success is None or not _model_catalog_complete(account, model_ids):
        return True
    return (now - last_success.astimezone(timezone.utc)).total_seconds() >= OAUTH_MODEL_SYNC_SUCCESS_TTL_SECONDS


def _format_model_change_notification(account: dict, result: dict) -> str:
    def lines(title: str, values: list[str]) -> list[str]:
        shown = values[:OAUTH_MODEL_CHANGE_LIST_LIMIT]
        out = [f"<b>{title} {len(values)}</b>"]
        out.extend(f"• <code>{notifier.escape_html(value)}</code>" for value in shown)
        if len(values) > len(shown):
            out.append(f"• 另有 {len(values) - len(shown)} 项")
        return out

    provider = provider_of(account)
    label = str(account.get("label") or account.get("email") or _account_key(account))
    parts = [
        "🔄 <b>OAuth 账户模型目录已变化</b>",
        f"账户: <code>{notifier.escape_html(label)}</code>",
        f"类型: <code>{notifier.escape_html(notifier.provider_label(provider))}</code>",
    ]
    added, removed = list(result.get("added") or []), list(result.get("removed") or [])
    if added:
        parts.extend(lines("新增", added))
    if removed:
        parts.extend(lines("删除", removed))
    return "\n".join(parts)


async def oauth_model_sync_once(
    *, force: bool = False, notify_changes: bool = True,
    account_keys: Iterable[str] | None = None, trigger: str = "background",
) -> list[dict]:
    """Refresh due OAuth catalogs with one global concurrency bound."""
    requested = set(account_keys or []) if account_keys is not None else None
    accounts = [copy.deepcopy(acc) for acc in list_accounts()]
    selected: list[tuple[str, dict]] = []
    for account in accounts:
        if provider_of(account) not in {"claude", "openai", "xai", "antigravity", "cursor"}:
            continue
        key = _account_key(account)
        if requested is not None and key not in requested:
            continue
        if force or _model_sync_due(account):
            selected.append((key, account))
    semaphore = asyncio.Semaphore(OAUTH_MODEL_SYNC_MAX_CONCURRENCY)

    async def run(key: str, snapshot: dict) -> dict:
        async with semaphore:
            try:
                result = await refresh_account_models(
                    key, timeout_s=OAUTH_MODEL_SYNC_REQUEST_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                result = {"action": "error", "account_key": key, "error": str(exc)}
        if (
            notify_changes and trigger == "background"
            and result.get("action") == "updated"
            and result.get("had_success_baseline") and result.get("changed")
        ):
            try:
                notifier.notify(_format_model_change_notification(snapshot, result))
            except Exception as exc:
                print(f"[oauth] model change notification failed: {type(exc).__name__}")
        return result

    return await asyncio.gather(*(run(key, account) for key, account in selected))


async def oauth_model_sync_loop() -> None:
    """Non-blocking startup/periodic maintenance loop for all OAuth providers."""
    await asyncio.sleep(OAUTH_MODEL_SYNC_STARTUP_DELAY_SECONDS)
    while True:
        try:
            await oauth_model_sync_once(trigger="background", notify_changes=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[oauth] model sync loop failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(OAUTH_MODEL_SYNC_CHECK_INTERVAL_SECONDS)


async def cursor_model_sync_once(*, force: bool = False) -> list[dict]:
    """Compatibility wrapper; Cursor is now owned by the unified scheduler."""
    keys = [_account_key(acc) for acc in list_accounts() if provider_of(acc) == "cursor"]
    return await oauth_model_sync_once(
        force=force, notify_changes=False, account_keys=keys, trigger="compat-cursor",
    )


async def cursor_model_sync_loop() -> None:
    """Compatibility entry point; do not register alongside the unified loop."""
    await oauth_model_sync_loop()


def cursor_disabled_models(account_or_key: dict | str) -> set[str]:
    """Return canonical models disabled for one Cursor account."""
    account = account_or_key if isinstance(account_or_key, dict) else get_account(account_or_key)
    if not isinstance(account, dict) or provider_of(account) != "cursor":
        return set()
    return {
        str(model).strip()
        for model in account.get("cursor_disabled_models") or []
        if str(model).strip()
    }


def set_cursor_disabled_models(
    account_key: str,
    models: Iterable[str],
    *,
    visible_models: Iterable[str] | None = None,
) -> set[str]:
    """Edit one frozen visible snapshot while retaining hidden disabled IDs."""
    canonical = _resolve_existing_account_key_or_raise(account_key)
    account = get_account(canonical)
    if account is None or provider_of(account) != "cursor":
        raise ValueError("Cursor account not found")
    current_available = {
        str(item.get("id") or "").strip()
        for item in ((account.get("cursor_model_catalog") or {}).get("models") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    available = {
        str(model).strip() for model in (
            visible_models if visible_models is not None else current_available
        ) if str(model).strip()
    }
    wanted = {
        str(model).strip() for model in models or [] if str(model).strip()
    }
    unknown = sorted(wanted - available)
    if unknown:
        raise ValueError(f"Cursor model not found: {unknown[0]}")

    saved: set[str] = set()

    def mutate(cfg):
        nonlocal saved
        for item in cfg.get("oauthAccounts", []):
            if _canonical_key(item) == canonical:
                saved = cursor_disabled_models(item) - available
                saved |= wanted
                item["cursor_disabled_models"] = sorted(saved)
                return
        raise ValueError("Cursor account not found")

    config.update(mutate, skip_if_unchanged=True)
    return set(saved)


def _cursor_max_context_capable_models(account: dict) -> set[str]:
    capable: set[str] = set()
    for record in ((account.get("cursor_model_catalog") or {}).get("models") or []):
        if not isinstance(record, dict):
            continue
        model_id = str(record.get("id") or "").strip()
        normal = int(record.get("context_window") or 0)
        maximum = int(record.get("context_window_max_mode") or normal or 0)
        if model_id and maximum > normal > 0:
            capable.add(model_id)
    return capable


def cursor_max_context_disabled_models(account_or_key: dict | str) -> set[str]:
    """Return explicit per-account exceptions to the default-on Max Context policy."""
    account = account_or_key if isinstance(account_or_key, dict) else get_account(account_or_key)
    if not isinstance(account, dict) or provider_of(account) != "cursor":
        return set()
    return {
        str(model).strip()
        for model in account.get("cursor_max_context_disabled_models") or []
        if str(model).strip()
    }


def cursor_max_context_models(account_or_key: dict | str) -> set[str]:
    """Return capable models whose default-on Max Context policy is active."""
    account = account_or_key if isinstance(account_or_key, dict) else get_account(account_or_key)
    if not isinstance(account, dict) or provider_of(account) != "cursor":
        return set()
    return _cursor_max_context_capable_models(account) - cursor_max_context_disabled_models(account)


def cursor_max_context_default(account_or_key: dict | str, model: str) -> bool:
    return str(model or "").strip() in cursor_max_context_models(account_or_key)


def set_cursor_max_context_default(account_key: str, model: str, enabled: bool) -> bool:
    """Persist one Cursor account/model Max Context default.

    Only canonical models whose account catalog advertises a strictly larger
    ``context_window_max_mode`` can be toggled.  Returns the saved state.
    """
    canonical = _resolve_existing_account_key_or_raise(account_key)
    account = get_account(canonical)
    if account is None or provider_of(account) != "cursor":
        raise ValueError("Cursor account not found")
    model_id = str(model or "").strip()
    record = next((
        item for item in ((account.get("cursor_model_catalog") or {}).get("models") or [])
        if isinstance(item, dict) and str(item.get("id") or "").strip() == model_id
    ), None)
    if record is None:
        raise ValueError(f"Cursor model not found: {model_id}")
    normal_context = int(record.get("context_window") or 0)
    max_context = int(record.get("context_window_max_mode") or 0)
    if max_context <= normal_context:
        raise ValueError(f"Cursor model has no separate Max Context tier: {model_id}")
    wanted = bool(enabled)

    def mutate(cfg):
        for item in cfg.get("oauthAccounts", []):
            if _canonical_key(item) != canonical:
                continue
            disabled = cursor_max_context_disabled_models(item)
            if wanted:
                disabled.discard(model_id)
            else:
                disabled.add(model_id)
            item["cursor_max_context_disabled_models"] = sorted(disabled)
            # One unpublished implementation briefly stored an enabled-list.
            # Default-on semantics supersede it; remove the stale field on write.
            item.pop("cursor_max_context_models", None)
            return
        raise ValueError("Cursor account not found")

    config.update(mutate, skip_if_unchanged=True)
    return wanted


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

def _raw_usage_from_flat(usage_flat: dict | None) -> dict:
    if not isinstance(usage_flat, dict):
        return {}
    raw = usage_flat.get("raw_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _refresh_notice_window_lines(usage_flat: dict | None) -> list[str]:
    if not usage_flat:
        return []
    lines: list[str] = []
    fh_util = usage_flat.get("five_hour_util")
    sd_util = usage_flat.get("seven_day_util")
    fb_util, fb_reset = fable_display_from_quota_row(usage_flat)
    if fh_util is not None:
        lines.append(
            f"📊 5h 用量: <b>{fh_util:.0f}%</b>"
            f" | 重置: <code>{_to_bjt(usage_flat.get('five_hour_reset'))}</code>"
        )
    if sd_util is not None:
        lines.append(
            f"📊 7d 用量: <b>{sd_util:.0f}%</b>"
            f" | 重置: <code>{_to_bjt(usage_flat.get('seven_day_reset'))}</code>"
        )
    if fb_util is not None:
        lines.append(
            f"📖 Fable 7d: <b>{fb_util:.0f}%</b>"
            f" | 重置: <code>{_to_bjt(fb_reset)}</code>"
        )
    return lines


def _refresh_notice_usage_lines(
    provider: str, usage_flat: dict | None, *, usage: dict | None = None,
) -> list[str]:
    """Refresh-toast usage lines. Omit the row when upstream gave no number."""
    raw = usage if isinstance(usage, dict) else _raw_usage_from_flat(usage_flat)
    if provider == "openai":
        return _refresh_notice_window_lines(usage_flat)
    if provider == "antigravity":
        block = raw.get("antigravity") if isinstance(raw.get("antigravity"), dict) else {}
        if not block.get("known"):
            return []
        text = antigravity_provider.format_credits_usage_text(block)
        return [f"📊 Credits: {notifier.escape_html(text)}"]
    if provider == "xai":
        billing = ((raw.get("xai") or {}).get("billing")
                   if isinstance(raw.get("xai"), dict) else None)
        billing = billing if isinstance(billing, dict) else {}
        used = billing.get("used_percent")
        reset = billing.get("period_end") or (usage_flat or {}).get("seven_day_reset")
        if used is None:
            used = (usage_flat or {}).get("seven_day_util")
        try:
            used_n = max(0.0, min(100.0, float(used))) if used is not None else None
        except (TypeError, ValueError):
            used_n = None
        if used_n is None:
            return []
        line = f"📊 官方额度: <b>{used_n:.2f}%</b> 已用"
        if reset:
            line += f" | 重置: <code>{_to_bjt(reset)}</code>"
        return [line]
    if provider == "cursor":
        cursor = raw.get("cursor") if isinstance(raw.get("cursor"), dict) else {}
        used = cursor.get("total_utilization")
        if used is None:
            used = (usage_flat or {}).get("thirty_day_util")
        reset = cursor.get("billing_cycle_end") or (usage_flat or {}).get("thirty_day_reset")
        try:
            used_n = max(0.0, min(100.0, float(used))) if used is not None else None
        except (TypeError, ValueError):
            used_n = None
        if used_n is None:
            return []
        line = f"📊 Cursor 额度: <b>{used_n:.2f}%</b>"
        if reset:
            line += f" | 重置: <code>{_to_bjt(reset)}</code>"
        return [line]
    return _refresh_notice_window_lines(usage_flat)


def _build_refresh_notice(
    account_key: str, usage_flat: dict | None, *, usage: dict | None = None,
) -> str:
    """构造 OAuth Token 刷新成功通知文案（中文 + HTML + 北京时间 + 用量摘要）。"""
    email = account_key_to_email(account_key)
    prov = provider_of(account_key)
    prov_tag = notifier.provider_tag(prov)
    new_exp = (get_account(account_key) or {}).get("expired")
    parts = [
        "✅ <b>OAuth Token 已刷新</b>",
        f"账号: <code>{notifier.escape_html(email)}</code> · {prov_tag}",
        f"新过期时间: <code>{_to_bjt(new_exp)}</code>"
        f" (剩 {_remaining_str(new_exp)})",
    ]
    parts.extend(_refresh_notice_usage_lines(prov, usage_flat, usage=usage))

    # 月度统计
    try:
        from . import log_db
        from .telegram import ui as telegram_ui
        month_start = (
            datetime.now(_BJT_TZ)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        ts = log_db.tokens_for_channel(f"oauth:{account_key}", since_ts=month_start)
        if ts and any(
            int(ts.get(name) or 0) > 0
            for name in (
                "total", "input", "output", "cache_creation", "cache_read",
                "costed_success", "unpriced_success",
            )
        ):
            prompt = cache_display.prompt_total(ts["input"], ts["cache_creation"], ts["cache_read"])
            line = f"💎 月度统计: ↑ {cache_display.fmt_tokens(prompt)} · ↓ {cache_display.fmt_tokens(ts['output'])}"
            if (ts.get("cache_read") or 0) > 0:
                line += f" · {cache_display.cache_read_phrase(ts['cache_read'], prompt)}"
            parts.append(line)
            parts.append(f"💵 {telegram_ui.fmt_cost(ts)}")
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
        disp = (
            str(acc.get("label") or email)
            if provider_of(acc) == "cursor" else email
        )
        if not acc.get("enabled", True):
            out[email] = "skipped:disabled"
            continue
        if acc.get("disabled_reason") in ("user", "auth_error"):
            out[email] = f"skipped:{acc['disabled_reason']}"
            continue

        if provider_of(acc) == "openai":
            try:
                meta_interval = int(
                    (config.get().get("oauth") or {}).get(
                        "openaiMetadataRefreshIntervalSeconds", 6 * 3600,
                    )
                )
                await ensure_openai_metadata_fresh(
                    ak, min_interval_seconds=meta_interval, timeout_s=5.0,
                )
            except Exception as exc:
                print(f"[oauth] openai metadata refresh failed for {ak}: {exc}")

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
            usage: dict | None = None
            try:
                usage = await fetch_usage_snapshot(ak)
                usage_flat = flatten_usage(usage)
                # 统一用 quota_save 写入；OpenAI 主动拉取来自 wham/usage，
                # 不覆盖响应头实时采样保存在 codex_* 列里的细节。
                usage = preserve_antigravity_cached_summary(ak, usage)
                usage_flat = flatten_usage(usage)
                state_db.quota_save(ak, usage_flat, email=email)
            except Exception as exc:
                print(f"[oauth] usage fetch after refresh failed for {ak}: {exc}")

            notifier.notify_event(
                "oauth_refreshed",
                _build_refresh_notice(ak, usage_flat, usage=usage),
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
            _rf_plan_tag = ""
            if provider_of(ak) == "claude":
                _rf_pl = claude_plan_label(acc)
                _rf_plan_tag = f"\n{notifier.provider_tag('claude')} · {notifier.escape_html(_rf_pl)}" if _rf_pl else ""
            elif provider_of(ak) == "openai":
                _rf_pl = acc.get("plan_type") or ""
                _rf_plan_tag = f"\n{notifier.provider_tag('openai')} · {notifier.escape_html(_rf_pl)}" if _rf_pl else ""
            elif provider_of(ak) == "cursor":
                _rf_pl = acc.get("plan_type") or ""
                _rf_plan_tag = f"\n{notifier.provider_tag('cursor')} · {notifier.escape_html(_rf_pl)}" if _rf_pl else ""
            notifier.notify_event(
                "oauth_refresh_failed",
                "⚠ <b>OAuth Token 刷新失败</b>\n"
                f"账号: <code>{notifier.escape_html(disp)}</code>{_rf_plan_tag}\n"
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
        provider = provider_of(acc)
        if acc.get("disabled_reason") in ("user", "auth_error"):
            out[email] = f"skipped:{acc['disabled_reason']}"
            continue

        reason_before = acc.get("disabled_reason")
        try:
            usage = await fetch_usage_snapshot(ak)
        except Exception as exc:
            out[email] = f"fetch_failed:{exc}"
            continue

        usage = preserve_antigravity_cached_summary(ak, usage)
        state_db.quota_save(ak, flatten_usage(usage), email=email)

        # OpenAI quota 恢复必须有本轮主动 fetch_usage 拿到的窗口数据；空 usage
        # 不足以证明恢复。不要用 last_passive_update_at 判定这里的新鲜度：旧的
        # 业务响应头采样时间戳可能早于本轮 wham/usage 主动刷新，误把新数据当 stale。
        # Claude 仍沿用真实 usage API。
        fresh_for_resume = True
        if provider == "openai" and reason_before == "quota":
            fresh_for_resume = _usage_has_any_quota_signal(usage)

        result = evaluate_and_toggle_by_usage(ak, usage, threshold=threshold, fresh=fresh_for_resume)
        utils = result["utils"]
        action = result["action"]

        # 通知里追加套餐标签（Claude 显示套餐/tier，OpenAI 显示 plan_type）
        _plan_tag = ""
        if provider == "claude":
            _pl = claude_plan_label(acc)
            _plan_tag = f"\n{notifier.provider_tag('claude')} · {notifier.escape_html(_pl)}" if _pl else ""
        elif provider == "openai":
            _label = openai_plan_workspace_label(acc)
            _plan_tag = f"\n{notifier.provider_custom_emoji_html('openai')} {notifier.escape_html(_label)}" if _label else ""
        elif provider == "xai":
            _plan_tag = f"\n{notifier.provider_custom_emoji_html('xai')} Grok"
        elif provider == "cursor":
            _plan_tag = f"\n{notifier.provider_tag('cursor')} · {notifier.escape_html(str(acc.get('plan_type') or 'Cursor'))}"
        elif provider == "antigravity":
            _plan_tag = f"\n{notifier.provider_custom_emoji_html('antigravity')} Antigravity"

        if action in ("disabled", "wham_limit_disabled"):
            latest_reset = result["disabled_until"]
            hit = " / ".join(result["hit_windows"]) or "?"
            out[email] = f"disabled_quota:{latest_reset}"
            notifier.notify_event(
                "quota_disabled",
                "⚠ <b>OAuth 配额已用尽，账号被自动禁用</b>\n"
                f"账号: <code>{notifier.escape_html(email)}</code>{_plan_tag}\n"
                f"撞到窗口: <code>{hit}</code>\n"
                f"重置时间: <code>{_to_bjt(latest_reset) if latest_reset else 'unknown'}</code>\n"
                "所有撞到窗口恢复后即可自动解禁。"
            )
        elif action in ("still_over_quota", "wham_limit_keep_disabled"):
            out[email] = action
        elif action == "resumed":
            out[email] = "resumed"
            notifier.throttled_notify_event_sync(
                "quota_resumed",
                f"quota_resumed:{ak}",
                "✅ <b>OAuth 配额已恢复，账号重新启用</b>\n"
                f"账号: <code>{notifier.escape_html(email)}</code>{_plan_tag}",
                cooldown_seconds=300,
            )
        elif action == "kept_enabled":
            parts = [f"{u:.0f}%" if u is not None else "-" for u in utils]
            out[email] = f"ok:{','.join(parts)}"
        else:
            out[email] = action
    return out


async def preload_openai_reset_credit_details_once() -> dict[str, str]:
    """Warm OpenAI usage/card caches once without delaying app readiness.

    ``quota_monitor_loop`` owns this one-shot work, so no second scheduler or
    thread is introduced. It also runs when periodic quota monitoring is off;
    the process-wide ``PARROT_NO_REFRESH`` startup guard still skips the whole
    loop when operators explicitly disable refreshes.
    """
    out: dict[str, str] = {}
    for acc in list_accounts()[:]:
        if provider_of(acc) != "openai":
            continue
        email = str(acc.get("email") or "")
        if not email:
            continue
        ak = _account_key(acc)
        reason = acc.get("disabled_reason")
        if reason in ("user", "auth_error"):
            out[ak] = f"skipped:{reason}"
            continue
        try:
            usage = await fetch_usage_snapshot(
                ak, usage_timeout_s=5.0, detail_timeout_s=5.0,
            )
            state_db.quota_save(ak, flatten_usage(usage), email=email)
            out[ak] = "refreshed"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            out[ak] = f"failed:{type(exc).__name__}"
            print(f"[oauth] startup quota preload failed for {ak}: {exc}")
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
    """后台任务：启动时预热 OpenAI 卡片明细，之后按配置监控额度。"""
    try:
        startup = await preload_openai_reset_credit_details_once()
        refreshed = sum(value == "refreshed" for value in startup.values())
        failed = sum(value.startswith("failed:") for value in startup.values())
        skipped = len(startup) - refreshed - failed
        if startup:
            print(
                "[oauth] startup quota preload: "
                f"refreshed={refreshed} skipped={skipped} failed={failed}"
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"[oauth_manager] startup quota preload error: {exc}")

    # Preserve the original stagger before the first quota-state evaluation.
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
