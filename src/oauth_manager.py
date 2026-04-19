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
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import config, notifier, state_db
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


def get_account(email: str) -> dict | None:
    for acc in config.get().get("oauthAccounts", []):
        if acc.get("email") == email:
            return acc
    return None


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


def _get_refresh_lock(email: str) -> threading.Lock:
    with _refresh_lock_for_dict:
        lock = _refresh_locks.get(email)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[email] = lock
    return lock


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


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


def _save_token_fields(email: str, new: dict) -> None:
    """把刷新后的 token 字段写回 config.oauthAccounts。

    若该账号此前因 `auth_error` 被自动禁用，刷新成功视为身份恢复：
    同时清掉 disabled_reason / disabled_until 并把 enabled 重新置 True。
    （只清 reason 而不重置 enabled 会让账号显示"正常"但仍被调度跳过。）
    """
    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if acc.get("email") == email:
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


def _refresh_sync_locked(email: str, force: bool) -> str:
    """同步刷新（持 threading.Lock，跨线程跨 loop 串行）。

    force=False 时进入锁后做一次"双重检查"：若另一并发刷新已完成且 token 仍有效则跳过实际请求。
    force=True 时无视剩余时间，强制刷新。
    """
    lock = _get_refresh_lock(email)
    with lock:
        acc = get_account(email)
        if acc is None:
            raise ValueError(f"unknown OAuth account: {email}")

        # 双重检查：force 路径不做（强制刷）
        if not force:
            expired = _parse_iso(acc.get("expired"))
            if expired and (expired - datetime.now(timezone.utc)).total_seconds() >= 300:
                return acc["access_token"]

        if mock_mode_enabled():
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

        _save_token_fields(email, new_fields)
        return new_fields["access_token"]


async def ensure_valid_token(email: str) -> str:
    """调用方：OAuthChannel.build_upstream_request。

    返回可用的 access_token。剩余 ≥ 5min 直接返回缓存；否则在线程中持锁刷新。
    同一 email 的并发请求由 threading.Lock 串行（跨 event loop 安全）。
    """
    acc = get_account(email)
    if acc is None:
        raise ValueError(f"unknown OAuth account: {email}")

    # 快速路径：无锁直接返回
    expired = _parse_iso(acc.get("expired"))
    if expired and (expired - datetime.now(timezone.utc)).total_seconds() >= 300:
        return acc["access_token"]

    # 慢速路径：在线程池中持 threading.Lock 做双重检查 + 刷新
    return await asyncio.to_thread(_refresh_sync_locked, email, False)


async def force_refresh(email: str) -> str:
    """无视剩余时间，强制刷一次（用于 401/403 重试前 / 管理员手动触发）。"""
    return await asyncio.to_thread(_refresh_sync_locked, email, True)


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
    if mock_mode_enabled():
        return _mock_usage()
    resp = httpx.get(
        OAUTH_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLI_USER_AGENT,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_profile(access_token: str) -> dict:
    return await asyncio.to_thread(_profile_sync, access_token)


async def fetch_usage(email: str) -> dict:
    access_token = await ensure_valid_token(email)
    return await asyncio.to_thread(_usage_sync, access_token)


# ─── 配额缓存辅助 ─────────────────────────────────────────────────

def flatten_usage(usage: dict) -> dict:
    """把 /api/oauth/usage 返回的嵌套结构展平，便于写 state_db.oauth_quota_cache。"""
    def _util_pct(obj) -> float | None:
        if not obj or obj.get("utilization") is None:
            return None
        v = float(obj["utilization"])
        # Anthropic 返回的 utilization 单位可能是 0..1 或 0..100；统一转 0..100
        if v <= 1.0:
            v *= 100
        return v

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

def add_account(entry: dict) -> None:
    """entry 需至少含 email / access_token / refresh_token。"""
    required = ("email", "access_token", "refresh_token")
    missing = [k for k in required if not entry.get(k)]
    if missing:
        raise ValueError(f"missing required fields: {missing}")

    email = entry["email"]

    def mutate(cfg):
        accounts = cfg.setdefault("oauthAccounts", [])
        if any(a.get("email") == email for a in accounts):
            raise ValueError(f"email already exists: {email}")
        # 规范化字段
        normalized = {
            "email": email,
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
        accounts.append(normalized)

    config.update(mutate)


def delete_account(email: str) -> None:
    def mutate(cfg):
        accounts = cfg.get("oauthAccounts", [])
        cfg["oauthAccounts"] = [a for a in accounts if a.get("email") != email]
    config.update(mutate)

    # state.db 级联清理
    key = f"oauth:{email}"
    state_db.perf_delete(key)
    state_db.error_delete(key)
    state_db.affinity_delete_by_channel(key)
    state_db.quota_delete(email)


def set_enabled(email: str, enabled: bool, reason: str | None = None,
                disabled_until: str | None = None) -> None:
    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if acc.get("email") != email:
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


def set_disabled_by_quota(email: str, resets_at: str | None) -> None:
    set_enabled(email, False, reason="quota", disabled_until=resets_at)


def update_models(email: str, models: list[str]) -> None:
    def mutate(cfg):
        for acc in cfg.get("oauthAccounts", []):
            if acc.get("email") == email:
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

def _build_refresh_notice(email: str, usage_flat: dict | None) -> str:
    """构造 OAuth Token 刷新成功通知文案（中文 + HTML + 北京时间 + 用量摘要）。"""
    new_exp = (get_account(email) or {}).get("expired")
    parts = [
        "✅ <b>OAuth Token 已刷新</b>",
        f"账号: <code>{notifier.escape_html(email)}</code>",
        f"新过期时间: <code>{_to_bjt(new_exp)}</code>"
        f" (剩 {_remaining_str(new_exp)})",
    ]
    # 用量
    if usage_flat:
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
        ts = log_db.tokens_for_channel(f"oauth:{email}", since_ts=month_start)
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
            await force_refresh(email)
            out[email] = "refreshed"
            # 顺便刷一次用量（失败不影响）
            usage_flat: dict | None = None
            try:
                usage = await fetch_usage(email)
                usage_flat = flatten_usage(usage)
                state_db.quota_save(email, usage_flat)
            except Exception as exc:
                print(f"[oauth] usage fetch after refresh failed for {email}: {exc}")

            # 组装中文通知（含格式化时间 + 用量 + 月度统计）；3 分钟后自动删除
            notifier.notify_event(
                "oauth_refreshed",
                _build_refresh_notice(email, usage_flat),
                auto_delete_seconds=180,
            )
        except Exception as exc:
            out[email] = f"failed:{exc}"
            try:
                set_enabled(email, False, reason="auth_error")
            except Exception:
                pass
            notifier.notify_event(
                "oauth_refresh_failed",
                "⚠ <b>OAuth Token 刷新失败</b>\n"
                f"账号: <code>{notifier.escape_html(email)}</code>\n"
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
        # 用户主动禁用 / auth_error 一律跳过
        if acc.get("disabled_reason") in ("user", "auth_error"):
            out[email] = f"skipped:{acc['disabled_reason']}"
            continue

        try:
            usage = await fetch_usage(email)
        except Exception as exc:
            out[email] = f"fetch_failed:{exc}"
            continue

        state_db.quota_save(email, flatten_usage(usage))

        utils = extract_utils_percent(usage)
        any_over = any(u is not None and u >= threshold for u in utils)

        if any_over:
            if acc.get("disabled_reason") == "quota":
                out[email] = "still_over_quota"
                continue
            latest_reset = latest_reset_iso(usage)
            set_disabled_by_quota(email, latest_reset)
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
                # resets_at 未过则继续保持禁用（防止刚好写满的账号被误判）
                if du is not None and du > datetime.now(timezone.utc):
                    out[email] = "quota_pending_reset"
                    continue
                set_enabled(email, True)
                out[email] = "resumed"
                notifier.notify_event(
                    "quota_resumed",
                    "✅ <b>OAuth 配额已恢复，账号重新启用</b>\n"
                    f"账号: <code>{notifier.escape_html(email)}</code>",
                )
            else:
                # 运行正常
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
