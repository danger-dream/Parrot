"""Parrot 主入口（多家族 AI 协议代理）。

启动时：
  - 加载配置、StateStore snapshots、logs/YYYY-MM.db
  - 从持久化状态恢复 affinity / cooldown / scorer 内存表
  - 构建渠道注册表并挂 config 重载钩子
  - 构造 httpx AsyncClient
  - 启动最低限度后台任务（WAL / stale / affinity cleanup）

/v1/messages：
  - API Key 验证
  - 请求落库（pending）
  - 调 scheduler.schedule 取候选列表
  - 调 failover.run_failover 顺序重试，返回 FastAPI Response
"""

import asyncio
import os
import json
import signal
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from uvicorn.server import HANDLED_SIGNALS
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src import (
    __version__, drain,
    affinity, apikey_limiter, auth, compact_rescue, config, cooldown,
    errors, failover, fingerprint, image_db, load_balancing, log_db,
    model_mapping, model_metadata, model_pricing, network,
    network_monitor, notifier, oauth_manager, probe, provider_usage, public_ip, scheduler, scorer,
    state_db, status_monitor, token_counter, translation, update_checker, updater,
    upstream,
)
from src.channel import registry
from src.client_ip import get_client_ip
from datetime import datetime, timezone
from src.telegram import bot as tgbot
from src.protocols import errors as protocol_errors
from src.openai.codex_constants import codex_cli_version
from src.transform.cc_mimicry import (
    DEVICE_ID,
    PARROT_DOWNSTREAM_BETAS_KEY,
    PARROT_CC_SESSION_ID_KEY,
    PARROT_ORIGINAL_MODEL_KEY,
    PARROT_WANTS_CONTEXT_1M_KEY,
    PARROT_WANTS_FAST_MODE_KEY,
    parse_beta_header,
    request_context_1m_override,
    request_wants_fast_mode,
    strip_context_1m_model_marker,
)


# ─── 全局告警节流（避免刷屏）────────────────────────────────────

_alert_last_sent: dict[str, float] = {}
_alert_lock = asyncio.Lock()    # async 互斥：FastAPI handler 都跑在主 event loop
_ALERT_COOLDOWN_SEC = 300  # 同一类告警 5 分钟内不重复


async def _throttled_notify(alert_key: str, text: str) -> None:
    """节流告警：同 alert_key 5 分钟内只发一次。

    用 asyncio.Lock 保证 check-and-set 原子（FastAPI 单 loop 多请求并发场景）。
    notifier.notify 本身是非阻塞队列入队，不会卡 event loop。
    """
    import time as _t
    async with _alert_lock:
        now = _t.time()
        last = _alert_last_sent.get(alert_key, 0)
        if now - last < _ALERT_COOLDOWN_SEC:
            return
        _alert_last_sent[alert_key] = now
    notifier.notify_event("no_channels", text)


# ─── 后台循环 ─────────────────────────────────────────────────────

_background_tasks: list[asyncio.Task] = []


async def _wal_checkpoint_loop():
    while True:
        await asyncio.sleep(300)
        try:
            log_db.checkpoint()
        except Exception as e:
            print(f"[log_db] checkpoint failed: {e}")
        try:
            image_db.checkpoint()
        except Exception as e:
            print(f"[image_db] checkpoint failed: {e}")
        try:
            translation.checkpoint()
        except Exception as e:
            print(f"[translation] checkpoint failed: {e}")


def _finalize_state_store() -> bool:
    """Flush and close the public state facade without knowing file details."""
    try:
        result = state_db.close()
        print("[state_store] shutdown snapshots flushed")
        return result
    except Exception as exc:
        print(f"[state_store] shutdown close failed: {exc}")
        return False


async def _stale_pending_loop():
    while True:
        await asyncio.sleep(300)
        try:
            cleared = await asyncio.to_thread(log_db.cleanup_stale_pending, 1800)
            if cleared:
                print(f"[log_db] cleaned {cleared} stale pending records")
        except Exception as e:
            print(f"[log_db] stale cleanup failed: {e}")
        # 留存策略已经由 TG 二次确认后持久化；这里每天最多实际清理一次。
        # 放在后台维护线程而非请求写入路径，避免一条新请求撞上大型 VACUUM。
        try:
            retention = await asyncio.to_thread(log_db.maybe_cleanup_retention)
            if not retention.get("skipped"):
                if retention.get("ok"):
                    removed = int(retention.get("deleted_requests") or 0)
                    freed = int(retention.get("actual_free_bytes") or 0)
                    if removed or freed:
                        print(f"[log_db] retention cleanup removed {removed} requests, freed {freed} bytes")
                else:
                    print(f"[log_db] retention cleanup failed: {retention.get('reason') or retention.get('errors')}")
        except Exception as e:
            print(f"[log_db] retention cleanup failed: {e}")


async def _affinity_cleanup_loop():
    while True:
        try:
            cfg = config.get()
            interval = int(cfg.get("affinity", {}).get("cleanupIntervalSeconds", 300))
        except Exception:
            interval = 300
        await asyncio.sleep(interval)
        try:
            cleared = affinity.cleanup()
            client_cleared = affinity.client_cleanup()
            if cleared or client_cleared:
                print(f"[affinity] cleaned {cleared} fp + {client_cleared} client stale entries")
        except Exception as e:
            print(f"[affinity] cleanup failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate paths, acquire the single-writer lock, and complete any legacy
    # transition before mutable/network lifecycle setup begins.
    state_db.init()
    # Config migration establishes versioned identities; mirror each one into the
    # minimal durable tombstone registry before any outbound OAuth traffic.
    from src.openai.codex_identity import sync_configured_identity_tombstones
    sync_configured_identity_tombstones(config.get().get("oauthAccounts", []))

    # 出站网络层必须在任何后续 OAuth/TG/status/update 请求前初始化。
    network.init()
    network.bootstrap_system_dns_once()
    log_db.init()
    image_db.init()
    translation.init()
    await asyncio.to_thread(log_db.cleanup_stale_pending, 1800)
    # 手工编辑 config 后重启的按天留存策略也应尽快收敛；默认永久保留时只做
    # 一个轻量判断，不会触碰任何日志数据。
    try:
        retention = await asyncio.to_thread(log_db.maybe_cleanup_retention)
        if retention.get("ok") and not retention.get("skipped"):
            removed = int(retention.get("deleted_requests") or 0)
            freed = int(retention.get("actual_free_bytes") or 0)
            if removed or freed:
                print(f"[log_db] startup retention cleanup removed {removed} requests, freed {freed} bytes")
        elif not retention.get("ok"):
            print(f"[log_db] startup retention cleanup failed: {retention.get('reason') or retention.get('errors')}")
    except Exception as exc:
        print(f"[log_db] startup retention cleanup failed: {exc}")

    # 老数据 provider 字段回填（无 provider 字段的账户默认 claude；幂等）
    try:
        migrated = oauth_manager.migrate_provider_field()
        if migrated:
            print(f"[oauth] migrated provider='claude' for {migrated} legacy account(s)")
    except Exception as exc:
        print(f"[oauth] provider field migration failed: {exc}")

    # 联合主键迁移：email → account_key (=f"{provider}:{email}")。幂等，已迁移过直接跳过。
    try:
        _ck_result = oauth_manager.bootstrap_composite_key_migration()
        if _ck_result.get("skipped"):
            print(f"[oauth] composite-key migration: skipped ({_ck_result.get('reason')})")
        else:
            print(
                f"[oauth] composite-key migration: quota_rows={_ck_result['migrated_quota_rows']},"
                f" channel_rows={_ck_result['migrated_channel_rows']}"
            )
    except Exception as _exc:
        print(f"[oauth] composite-key migration FAILED: {_exc}")
        raise

    # OpenAI OAuth workspace identity migration：openai:<email> → openai:<workspace_id>
    # Only unique email→workspace mappings are migrated; ambiguous same-email
    # workspaces remain unresolved so old keys cannot silently hit the wrong team.
    try:
        _ow_result = oauth_manager.bootstrap_openai_workspace_key_migration()
        _state = _ow_result.get("state") or {}
        if _state.get("skipped"):
            print(f"[oauth] openai workspace-key migration: skipped ({_state.get('reason')})")
        else:
            print(
                f"[oauth] openai workspace-key migration: mappings={_ow_result.get('mapping_count', 0)},"
                f" state_quota={_state.get('quota_rows', 0)},"
                f" state_channels={_state.get('channel_rows', 0)},"
                f" log_rows={(_ow_result.get('logs') or {}).get('request_log_rows', 0)}"
                f"+{(_ow_result.get('logs') or {}).get('retry_chain_rows', 0)},"
                f" image_rows={(_ow_result.get('images') or {}).get('call_rows', 0)}"
                f"+{(_ow_result.get('images') or {}).get('attempt_rows', 0)}"
            )
    except Exception as _exc:
        print(f"[oauth] openai workspace-key migration FAILED: {_exc}")
        raise

    # Domain mirrors restore from the authoritative in-memory StateStore
    affinity.init()
    affinity.client_init()
    cooldown.init()
    scorer.init()

    # OpenAI 家族 factory 注入（必须在 rebuild_from_config 之前，否则带 protocol=openai-*
    # 的 channel entry 会回落到 ApiChannel 并被 assert 拒绝）
    from src.openai.channel.registration import register_factories as _openai_register_factories
    _openai_register_factories()

    # OpenAI previous_response_id Store（independent SQLite）
    from src.openai import store as openai_store
    openai_store.init()

    # Cursor OAuth channels reuse the normal HTTP/SSE failover path through a
    # process-private loopback bridge. It must exist before registry construction.
    from src.cursor_bridge import runtime as cursor_bridge_runtime
    cursor_bridge_runtime.ensure_started()

    # 渠道注册表 + priority 统一顺序迁移 + 热加载钩子。
    registry.rebuild_from_config()
    _lb_cfg = config.get()
    if (
        str(_lb_cfg.get("channelSelection") or "smart").lower() == "priority"
        and not ((_lb_cfg.get("loadBalancing") or {}).get("channelPriorityOrder") or [])
    ):
        migrated_order = load_balancing.initialize_priority_orders()
        print(
            f"[load-balancing] migrated legacy family priorities to "
            f"{len(migrated_order)} unified channel entries"
        )
    registry.install_config_reload_hook()

    # API Provider 用量在 Telegram 启动前启动唯一 coordinator 并预热一次。
    # 预热只进入共享队列，不等待网络；禁用语义与 OAuth 主动刷新一致。
    if provider_usage.is_enabled():
        await provider_usage.start()
        _provider_usage_startup = provider_usage.schedule_startup_refresh()
        print(
            "[provider_usage] startup refresh: "
            f"channels={_provider_usage_startup['supported_channels']} "
            f"accounts={_provider_usage_startup['supported_accounts']} "
            f"scheduled={_provider_usage_startup['scheduled_accounts']}"
        )

    # httpx 客户端
    upstream.create_client()
    try:
        model_pricing.initialize()
        migrated = model_metadata.migrate_legacy_config()
        if migrated["bindings"] or migrated["compression"]:
            print(
                "[Metadata] migrated legacy config: "
                f"bindings={migrated['bindings']} compression={migrated['compression']}"
            )
    except Exception as exc:
        # 金额统计是旁路能力，价格表异常不能阻断代理启动；后台刷新仍会继续尝试恢复。
        print(f"[Pricing] local catalog load failed: {exc}")

    # 后台获取公网 IPv4（用于主菜单显示外网 BaseURL，失败则不显示）
    public_ip.fetch_async()

    cfg = config.get()
    # Telegram Bot（M6）
    tg_token = cfg.get("telegram", {}).get("botToken") or ""
    tg_admins = cfg.get("telegram", {}).get("adminIds") or []
    if tg_token:
        tgbot.init(tg_token, tg_admins)
        tgbot.start()

    print(f"Parrot 🦜 v{__version__} (multi-family AI protocol proxy) ready")
    print(f"  device_id: {DEVICE_ID[:16]}...")
    print(f"  listen: http://{cfg['listen']['host']}:{cfg['listen']['port']}/v1/messages")
    print(f"  api_keys: {len(cfg.get('apiKeys', {}))}")
    print(f"  oauth_accounts: {len(cfg.get('oauthAccounts', []))}")
    print(f"  api_channels: {len(cfg.get('channels', []))}")
    print(f"  registry: {registry.channel_count()} channels")
    print(f"  codex_cli_version: {codex_cli_version()}")
    print(f"  cch_mode: {cfg.get('cchMode')}")
    print(f"  oauth_mock: {cfg.get('oauth', {}).get('mockMode', False)}")
    print(f"  timeouts: {cfg.get('timeouts')}")
    print(f"  telegram: {'enabled' if tg_token else 'disabled'} ({len(tg_admins)} admin(s))")

    _background_tasks.append(asyncio.create_task(_wal_checkpoint_loop()))
    _background_tasks.append(asyncio.create_task(_stale_pending_loop()))
    _background_tasks.append(asyncio.create_task(_affinity_cleanup_loop()))
    # ⛔ 双实例重构期：关掉后台主动刷新（每 60s 自动刷将过期 token，最危险）
    # 和 quota_monitor（周期拉 usage，对共享账号的多余访问）。PARROT_NO_REFRESH=1 时跳过。
    if os.environ.get("PARROT_NO_REFRESH") != "1":
        _background_tasks.append(asyncio.create_task(oauth_manager.proactive_refresh_loop()))
        _background_tasks.append(asyncio.create_task(oauth_manager.quota_monitor_loop()))
        _background_tasks.append(asyncio.create_task(oauth_manager.oauth_model_sync_loop()))
    _background_tasks.append(asyncio.create_task(probe.recovery_loop()))
    _background_tasks.append(asyncio.create_task(status_monitor.monitor_loop()))
    _background_tasks.append(asyncio.create_task(network_monitor.monitor_loop()))
    _background_tasks.append(asyncio.create_task(update_checker.update_loop()))
    _background_tasks.append(asyncio.create_task(model_pricing.refresh_loop()))
    # 自更新：若进程是被自更新重启拉起的，恢复流程做健康检查/回滚
    try:
        updater.resume_after_restart()
    except Exception as _exc:
        print(f"[updater] resume_after_restart failed: {_exc}")
    _background_tasks.append(asyncio.create_task(openai_store.cleanup_loop()))
    _background_tasks.append(asyncio.create_task(translation.cleanup_loop()))

    try:
        yield
    finally:
        drain.begin("lifespan_shutdown")
        timeout = drain.shutdown_timeout_seconds()
        drained = await drain.wait_for_zero(timeout)
        if not drained:
            print(f"[drain] lifespan shutdown timeout active={drain.active_count()} timeout={timeout}s")
        for t in _background_tasks:
            t.cancel()
        await asyncio.gather(*_background_tasks, return_exceptions=True)
        await apikey_limiter.shutdown_spooling()
        tgbot.stop()
        # Provider workers may mutate state; stop them before the final snapshot.
        await provider_usage.stop()
        await upstream.close_client()
        cursor_bridge_runtime.stop()
        _finalize_state_store()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


_API_KEY_LIMITED_HTTP_PATHS = {
    "/v1/messages",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/images/generate",
    "/v1/images/edit",
    "/v1/images/generations",
    "/images/generations",
    "/v1/images/edits",
    "/images/edits",
    "/v1/videos",
    "/v1/videos/generations",
    "/v1/videos/edits",
    "/v1/videos/extensions",
    # Codex WebRTC call creation uses the ChatGPT-backend-shaped request body;
    # WebSocket realtime sessions acquire their API-key lease in their handler.
    "/backend-api/codex/realtime/calls",
}


def _is_api_key_limited_http_request(method: str, path: str) -> bool:
    if method.upper() == "POST":
        return path in _API_KEY_LIMITED_HTTP_PATHS
    if method.upper() == "GET" and path.startswith("/v1/videos/"):
        request_id = path[len("/v1/videos/"):]
        return bool(request_id and "/" not in request_id)
    return False


def _api_key_limit_error_response(path: str, exc: apikey_limiter.ApiKeyLimitError):
    if path == "/v1/messages":
        resp = errors.json_error_response(429, errors.ErrType.RATE_LIMIT, exc.message)
    else:
        resp = errors.json_error_openai(429, errors.ErrTypeOpenAI.RATE_LIMIT, exc.message)
    for k, v in exc.headers.items():
        resp.headers[k] = v
    if exc.retry_after is not None:
        resp.headers["Retry-After"] = str(exc.retry_after)
    return resp


def _request_body_limit_error_response(
    path: str, exc: apikey_limiter.RequestBodyTooLarge,
):
    """Map per-request limits to 413 and shared queue pressure to 429."""
    aggregate_pressure = exc.reason in {"key_aggregate", "process_aggregate"}
    status = 429 if aggregate_pressure else 413
    if path == "/v1/messages":
        resp = errors.json_error_response(
            status,
            errors.ErrType.RATE_LIMIT if aggregate_pressure else errors.ErrType.REQUEST_TOO_LARGE,
            str(exc),
            code="queued_body_capacity" if aggregate_pressure else "request_too_large",
        )
    else:
        resp = errors.json_error_openai(
            status,
            errors.ErrTypeOpenAI.RATE_LIMIT if aggregate_pressure else errors.ErrTypeOpenAI.INVALID_REQUEST,
            str(exc),
            code="queued_body_capacity" if aggregate_pressure else "request_too_large",
        )
    if aggregate_pressure:
        resp.headers["Retry-After"] = "1"
    return resp


def _queued_body_spool_error_response(
    path: str, exc: apikey_limiter.QueuedBodySpoolError,
):
    """Return a retryable stable error without exposing spool OS details."""
    message = "queued request body spool is temporarily unavailable"
    if path == "/v1/messages":
        resp = errors.json_error_response(
            503, errors.ErrType.OVERLOADED, message, code="queued_body_spool_unavailable",
        )
    else:
        resp = errors.json_error_openai(
            503, errors.ErrTypeOpenAI.SERVER, message,
            code="queued_body_spool_unavailable",
        )
    resp.headers["Retry-After"] = "1"
    return resp


def _find_request_body_limit_error(exc: BaseException) -> apikey_limiter.RequestBodyTooLarge | None:
    """Unwrap Starlette/AnyIO task-group errors without version-specific APIs."""
    if isinstance(exc, apikey_limiter.RequestBodyTooLarge):
        return exc
    for nested in getattr(exc, "exceptions", ()) or ():
        found = _find_request_body_limit_error(nested)
        if found is not None:
            return found
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        found = _find_request_body_limit_error(cause)
        if found is not None:
            return found
    context = getattr(exc, "__context__", None)
    if context is not None and context is not exc:
        return _find_request_body_limit_error(context)
    return None


def _find_queued_body_spool_error(
    exc: BaseException,
) -> apikey_limiter.QueuedBodySpoolError | None:
    """Unwrap task-group errors containing a spool failure."""
    if isinstance(exc, apikey_limiter.QueuedBodySpoolError):
        return exc
    for nested in getattr(exc, "exceptions", ()) or ():
        found = _find_queued_body_spool_error(nested)
        if found is not None:
            return found
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        found = _find_queued_body_spool_error(cause)
        if found is not None:
            return found
    context = getattr(exc, "__context__", None)
    if context is not None and context is not exc:
        return _find_queued_body_spool_error(context)
    return None


def _with_asgi_response_headers(
    message: Message, headers: dict[str, str],
) -> Message:
    """Return an ASGI response-start event with limiter headers replaced."""
    if message.get("type") != "http.response.start" or not headers:
        return message
    replacement_names = {name.lower().encode("latin-1") for name in headers}
    raw_headers = [
        (name, value) for name, value in message.get("headers", [])
        if name.lower() not in replacement_names
    ]
    raw_headers.extend(
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in headers.items()
    )
    updated = dict(message)
    updated["headers"] = raw_headers
    return updated


class _DrainHttpMiddleware:
    """Pure ASGI drain/API-key middleware with explicit receive ownership.

    A queued request's watcher exclusively owns the original receive callable.
    Once admission finishes and watcher cleanup has completed, the lease's
    replaying receive callable is passed to FastAPI. Response completion or any
    cancellation path releases both leases exactly once.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if drain.is_draining() and not drain.allow_path_during_drain(path):
            await drain.reject_response()(scope, receive, send)
            return

        # Health checks must not keep a draining process alive.
        if drain.allow_path_during_drain(path):
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "")
        drain_lease = await drain.enter(f"http {method} {path}")
        key_lease: apikey_limiter.ApiKeyLease | None = None
        response_started = False
        released = False

        async def release_all() -> None:
            nonlocal released
            if released:
                return
            released = True
            try:
                if key_lease is not None:
                    await key_lease.release()
            finally:
                await drain_lease.aclose()

        async def send_wrapped(message: Message) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
                if key_lease is not None:
                    message = _with_asgi_response_headers(
                        message, key_lease.response_headers,
                    )
            await send(message)
            if (
                message.get("type") == "http.response.body"
                and not message.get("more_body", False)
            ):
                await release_all()

        async def send_error(response) -> None:
            await release_all()
            await response(scope, receive, send)

        try:
            request = Request(scope, receive=receive)
            if _is_api_key_limited_http_request(method, path):
                key_name, _allowed_models, err = auth.validate(request.headers)
                if not err and key_name:
                    key_lease = await apikey_limiter.acquire(
                        key_name, request, receive=receive,
                    )
            downstream_receive = (
                key_lease.receive
                if key_lease is not None and key_lease.receive is not None
                else receive
            )
            await self.app(scope, downstream_receive, send_wrapped)
        except apikey_limiter.ApiKeyLimitError as exc:
            if response_started:
                raise
            await send_error(_api_key_limit_error_response(path, exc))
        except apikey_limiter.RequestBodyTooLarge as exc:
            if response_started:
                raise
            await send_error(_request_body_limit_error_response(path, exc))
        except apikey_limiter.QueuedBodySpoolError as exc:
            if response_started:
                raise
            await send_error(_queued_body_spool_error_response(path, exc))
        except BaseException as exc:
            if response_started:
                raise
            body_limit_error = _find_request_body_limit_error(exc)
            if body_limit_error is not None:
                await send_error(_request_body_limit_error_response(path, body_limit_error))
                return
            spool_error = _find_queued_body_spool_error(exc)
            if spool_error is not None:
                await send_error(_queued_body_spool_error_response(path, spool_error))
                return
            raise
        finally:
            await release_all()


app.add_middleware(_DrainHttpMiddleware)


def _model_never_supported(model: str) -> bool:
    """model 在当前任何渠道（包括已禁用）里都不可能被路由 → True。
    用于把"模型不存在"与"模型存在但全都冷却"区分开。"""
    for ch in registry.all_channels():
        if ch.supports_model(model):
            return False
    return True


def _first_route_channel_and_model(result) -> tuple[object | None, str | None]:
    for ch, resolved in list(getattr(result, "candidates", []) or []) + list(getattr(result, "saturated", []) or []):
        return ch, resolved
    return None, None


def _channel_uses_max_context(ch: object, body: dict, resolved_model: str | None) -> bool:
    """Whether this candidate will use its account/model Max Context tier."""
    check = getattr(ch, "uses_max_context", None)
    if not callable(check):
        return False
    try:
        return bool(check(body, str(resolved_model or "")))
    except Exception:
        return False


def _anthropic_to_openai_context_preflight(body: dict, result) -> dict | None:
    """Return context overflow info for Anthropic→OpenAI cross-family calls.

    Claude Code may believe a Claude-facing endpoint has a 1M context window even
    when Parrot routes it to an OpenAI-family model with a smaller real window.
    When model metadata is available, fail early with a Claude-Code-friendly
    context_length_exceeded error so the client triggers its own autocompact.
    """
    if compact_rescue.is_claude_code_compact_request(body):
        return None
    ch, resolved_model = _first_route_channel_and_model(result)
    if ch is None:
        return None
    if getattr(ch, "protocol", "anthropic") == "anthropic":
        return None
    metadata_model = str(
        body.get("_client_visible_model") or body.get("model") or ""
    ).strip()
    safe_limit = model_metadata.safe_prompt_limit(
        metadata_model,
        scope_key=str(getattr(ch, "key", "") or ""),
        outbound_model=str(resolved_model or ""),
        use_max_context=_channel_uses_max_context(ch, body, resolved_model),
    )
    if not metadata_model or safe_limit is None or safe_limit <= 0:
        return None
    prompt_tokens = token_counter.count_request_tokens(body, model=metadata_model)
    if prompt_tokens <= safe_limit:
        return None
    msg = protocol_errors.context_length_error_message_for_claude_code(
        "context_length_exceeded: Your input exceeds the context window of this model. "
        "Please adjust your input and try again.",
        actual_tokens=prompt_tokens,
        max_tokens=safe_limit,
    )
    return {
        "message": msg,
        "model": metadata_model,
        "prompt_tokens": prompt_tokens,
        "safe_limit": safe_limit,
    }


def _sanitize_headers(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("authorization", "x-api-key"):
            out[k] = "***"
        else:
            out[k] = v
    return out


@app.get("/health")
async def health():
    """运维健康检查。不需要 API Key。

    返回：
      status: ok / degraded / error
      ok 条件：registry 已构建 + 至少一个 enabled 渠道（或 enabled OAuth）
      degraded: 存在 enabled 渠道但全部冷却
      error: 无任何 enabled 渠道
    """
    cfg = config.get()
    chs = registry.all_channels()
    enabled_total = sum(1 for ch in chs if ch.enabled and not ch.disabled_reason)
    status = "ok" if enabled_total > 0 else "error"
    if enabled_total > 0:
        # 检查是否所有都在 cooldown
        active = 0
        for ch in chs:
            if not ch.enabled or ch.disabled_reason:
                continue
            models = getattr(ch, "models", [])
            # 有至少一个模型未冷却
            if ch.type == "oauth":
                model_list = models
            else:
                model_list = [m.get("real") for m in models if isinstance(m, dict)]
            if any(not cooldown.is_blocked(ch.key, m) for m in model_list):
                active += 1
                break
        if active == 0 and enabled_total > 0:
            status = "degraded"
    oauth_count = len(cfg.get("oauthAccounts") or [])
    api_count = len(cfg.get("channels") or [])
    return {
        "status": "draining" if drain.is_draining() else status,
        "drain": drain.status_snapshot(),
        "channels": {
            "total": len(chs),
            "enabled": enabled_total,
            "oauth": oauth_count,
            "api": api_count,
        },
        "affinity_bound": affinity.count(),
        "client_affinity_bound": affinity.client_count(),
        "device_id": DEVICE_ID[:16] + "...",
        "version": __version__,
    }


@app.get("/v1/models")
async def list_models(request: Request):
    """Anthropic 标准 /v1/models：返回当前代理可见的模型清单。

    - 需要 API Key 验证（和 /v1/messages 一致）
    - 若 Key 有 allowedModels 白名单，再和全局模型列表取交集
    - 否则返回所有启用渠道聚合的去重模型列表
    """
    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return errors.json_error_response(401, errors.ErrType.AUTH, err)

    all_models = registry.available_models()
    if allowed_models:
        allowed_set = set(allowed_models)
        visible = [m for m in all_models if m in allowed_set]
    else:
        visible = all_models

    # 共享的 /v1/models 不知道客户端后续会调用 messages/chat/responses
    # 中的哪一个入口，因此只能额外暴露真正跨入口生效的 global 别名。
    # legacy anthropic/openai-* 专属别名仍可在各自入口使用，但不能混入
    # 这份共享清单，否则客户端会看到在其他入口无法路由的模型名。
    visible_set = set(visible)
    alias_seen: set[str] = set()
    for _alias, _real in model_mapping.get_global_map().items():
        if _alias in visible_set:
            continue
        if _real not in visible_set:
            continue
        # Key 有白名单时, 别名必须显式授权 (白名单按真名语义, 但下游看到的
        # 是别名, 这里做 strict 检查: 白名单里如果没别名就不暴露)
        if allowed_models and _alias not in allowed_set:
            continue
        alias_seen.add(_alias)
    if alias_seen:
        visible = sorted(visible_set | alias_seen)

    # Anthropic 的 created_at 字段有真实的模型发布时间，我们没有，用启动后
    # 的一个稳定占位符（保持响应结构兼容，字段不为 null）。
    placeholder_ts = datetime(2025, 1, 1, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    data = [
        {"type": "model", "id": m, "display_name": m, "created_at": placeholder_ts}
        for m in visible
    ]
    return {
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": False,
    }


@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """OpenAI Chat Completions 入口。详细流程在 src/openai/handler.py。"""
    from src.openai.handler import handle
    return await handle(request, ingress_protocol="chat")


@app.post("/v1/responses")
async def proxy_responses(request: Request):
    """OpenAI Responses 入口。详细流程在 src/openai/handler.py。"""
    from src.openai.handler import handle
    return await handle(request, ingress_protocol="responses")


@app.websocket("/v1/responses")
async def proxy_responses_websocket(websocket: WebSocket):
    """OpenAI/Codex Responses WebSocket 入口（非语音 Realtime）。"""
    if drain.is_draining():
        await websocket.close(code=1013, reason="Parrot is draining for graceful restart")
        return
    from src.openai.responses_ws import handle_responses_ws
    async with drain.active("ws /v1/responses"):
        await handle_responses_ws(websocket)


@app.websocket("/v1/realtime")
async def proxy_realtime_websocket(websocket: WebSocket):
    """Codex Realtime V1/V2 transparent WebSocket relay."""
    if drain.is_draining():
        await websocket.close(code=1013, reason="Parrot is draining for graceful restart")
        return
    from src.openai.realtime import handle_realtime_ws
    async with drain.active("ws /v1/realtime"):
        await handle_realtime_ws(websocket, path="/v1/realtime")


@app.websocket("/v1/live")
async def proxy_realtime_live_websocket(websocket: WebSocket):
    """Codex Realtime V3 transparent WebSocket relay."""
    if drain.is_draining():
        await websocket.close(code=1013, reason="Parrot is draining for graceful restart")
        return
    from src.openai.realtime import handle_realtime_ws
    async with drain.active("ws /v1/live"):
        await handle_realtime_ws(websocket, path="/v1/live")


@app.websocket("/v1/live/{call_id}")
async def proxy_realtime_live_sideband_websocket(websocket: WebSocket, call_id: str):
    """Codex Realtime V3 WebRTC sideband relay for an existing call."""
    if drain.is_draining():
        await websocket.close(code=1013, reason="Parrot is draining for graceful restart")
        return
    from src.openai.realtime import handle_realtime_ws
    async with drain.active("ws /v1/live/{call_id}"):
        await handle_realtime_ws(websocket, path=f"/v1/live/{call_id}", live_call_id=call_id)


@app.post("/backend-api/codex/realtime/calls")
async def proxy_realtime_call(request: Request):
    """Codex backend-shaped WebRTC call creation relay."""
    from src.openai.realtime import handle_realtime_call
    return await handle_realtime_call(request)


@app.post("/v1/images/generate")
async def proxy_images_generate(request: Request):
    """Parrot 封装版图片生成入口：prompt + 可选 size。"""
    from src.openai.images_simple import handle_generate
    return await handle_generate(request)


@app.post("/v1/images/edit")
async def proxy_images_edit(request: Request):
    """Parrot 封装版图片编辑入口：prompt + image + 可选 size。"""
    from src.openai.images_simple import handle_edit
    return await handle_edit(request)


# OpenAI Images API 兼容入口：按 model 在 GPT/Codex 与 xAI OAuth 间分流。
@app.post(
    "/v1/images/generations",
    summary="OpenAI-compatible image generation",
    description=(
        "Standard OpenAI `/v1/images/generations` endpoint. Accepts `prompt`, "
        "`model`, `n`, `size`, `response_format`, `quality`, `background`, "
        "`output_format`, `moderation`, `style`, `output_compression`, "
        "`partial_images`. Configured `grok-imagine-image*` models use the xAI "
        "OAuth pool; all other models retain the GPT/Codex image pipeline. "
        "Only the GPT/Codex path downgrades `n > 1` to one image."
    ),
    tags=["images"],
)
@app.post("/images/generations", include_in_schema=False)
async def proxy_images_generations_openai(request: Request):
    from src.openai.images_openai_compat import handle_generations
    return await handle_generations(request)


@app.post(
    "/v1/images/edits",
    summary="OpenAI-compatible image edit",
    description=(
        "Standard OpenAI `/v1/images/edits` endpoint. Supports JSON or "
        "`multipart/form-data` body. Accepts a single `image` or multiple "
        "`images[]` plus an optional `mask`. Same option fields as "
        "generations are passed through."
    ),
    tags=["images"],
)
@app.post("/images/edits", include_in_schema=False)
async def proxy_images_edits_openai(request: Request):
    from src.openai.images_openai_compat import handle_edits
    return await handle_edits(request)


@app.post(
    "/v1/videos/generations",
    summary="Generate a video with xAI Imagine",
    tags=["videos"],
)
@app.post("/v1/videos", include_in_schema=False)
async def proxy_xai_video_generation(request: Request):
    from src.xai.imagine import handle_video_create
    return await handle_video_create(request, action="generate")


@app.post(
    "/v1/videos/edits",
    summary="Edit a video with xAI Imagine",
    tags=["videos"],
)
async def proxy_xai_video_edit(request: Request):
    from src.xai.imagine import handle_video_create
    return await handle_video_create(request, action="edit")


@app.post(
    "/v1/videos/extensions",
    summary="Extend a video with xAI Imagine",
    tags=["videos"],
)
async def proxy_xai_video_extension(request: Request):
    from src.xai.imagine import handle_video_create
    return await handle_video_create(request, action="extend")


@app.get(
    "/v1/videos/{request_id}",
    summary="Get an xAI Imagine video task",
    tags=["videos"],
)
async def proxy_xai_video_result(request: Request, request_id: str):
    from src.xai.imagine import handle_video_result
    return await handle_video_result(request, request_id)


@app.post("/v1/messages")
async def proxy_messages(request: Request):
    start_time = time.time()
    start_monotonic = time.monotonic()
    request_id = str(uuid.uuid4())
    client_ip = get_client_ip(request)

    # 1. API Key 验证
    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return errors.json_error_response(401, errors.ErrType.AUTH, err)

    # 2. 读请求体
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception as e:
        return errors.json_error_response(
            400, errors.ErrType.INVALID_REQUEST, f"invalid json: {e}"
        )

    # 2.1 保存下游显式能力信号，再做模型映射 / 入口默认模型：
    #     - anthropic-beta 可显式请求 context-1m；
    #     - 原始模型名可能是 `sonnet[1m]` / `*-1m` / `*-context-1m` 这类 1M 别名；
    #     - `max_tokens` 是输出上限，不参与 1M context 判断。
    downstream_betas = parse_beta_header(request.headers.get("anthropic-beta"))
    original_model = body.get("model")

    # 模型映射 / 入口默认模型：
    #     - body.model 缺失 → 填入该 ingress 的默认（若配置）
    #     - body.model 命中别名 → 改写成真实名（只解一层）
    #     - body.model 带 [1m]/-1m/context-1m → 剥 marker 后再给映射表二次机会
    #     后续白名单/调度/channel 全按真实名走；显式 1M 意图由私有字段单独传递。
    model_mapping.apply_default(body, "anthropic")
    model_mapping.apply_mapping(body, "anthropic")
    stripped_model = strip_context_1m_model_marker(body.get("model"))
    if stripped_model != body.get("model"):
        body["model"] = stripped_model
        model_mapping.apply_mapping(body, "anthropic")

    model = body.get("model")
    body["_client_visible_model"] = str(model or "").strip()
    explicit_context_1m = request_context_1m_override(
        body,
        downstream_betas=downstream_betas,
        original_model=original_model,
        resolved_model=model,
    )
    explicit_fast_mode = request_wants_fast_mode(
        body,
        downstream_betas=downstream_betas,
    )
    body[PARROT_DOWNSTREAM_BETAS_KEY] = downstream_betas
    if isinstance(original_model, str) and original_model.strip():
        body[PARROT_ORIGINAL_MODEL_KEY] = original_model.strip()
    # True/False = 下游显式覆盖；None = 交给渠道/模型默认策略。
    body[PARROT_WANTS_CONTEXT_1M_KEY] = explicit_context_1m
    # True = 下游显式要求 Claude Fast mode；None = 不启用。
    body[PARROT_WANTS_FAST_MODE_KEY] = explicit_fast_mode or None
    if not model:
        return errors.json_error_response(
            400, errors.ErrType.INVALID_REQUEST, "model is required"
        )

    # 模型白名单检查：allowed_models 为空 = 无限制；非空则必须命中
    if allowed_models and model not in allowed_models:
        return errors.json_error_response(
            403, errors.ErrType.PERMISSION,
            f"Model '{model}' is not allowed for this API key "
            f"(allowed: {', '.join(allowed_models) or 'none'})",
        )

    is_stream = bool(body.get("stream", False))
    messages = body.get("messages") or []
    tools = body.get("tools") or []

    # 3. 调度：先计算指纹（供 log_db 记录）
    fp_query = fingerprint.fingerprint_query(key_name or "", client_ip, messages)

    # 4. pending 日志
    reasoning_effort = log_db.extract_reasoning_effort(body, "anthropic")
    req_headers = _sanitize_headers(dict(request.headers))
    await asyncio.to_thread(
        log_db.insert_pending,
        request_id, client_ip, key_name, model, is_stream,
        len(messages), len(tools),
        req_headers,
        {k: v for k, v in body.items() if not (isinstance(k, str) and k.startswith("_parrot_"))},
        fingerprint=fp_query,
        ingress_protocol="anthropic",
        reasoning_effort=reasoning_effort,
        fast_mode=explicit_fast_mode,
    )

    # Internal-only routing/cache hints for downstream channel builders.  These
    # fields are stripped by provider allowlists and never sent upstream.
    body["_parrot_api_key_name"] = key_name or ""
    body["_parrot_client_ip"] = client_ip or ""
    claude_session_id = str(request.headers.get("x-claude-code-session-id") or "").strip()
    if claude_session_id:
        body[PARROT_CC_SESSION_ID_KEY] = claude_session_id

    # 5. 调度
    result = scheduler.schedule(body, api_key_name=key_name, client_ip=client_ip)

    # pending 时更新 affinity_hit（亲和命中本身需要调度之后才知道）
    if result.affinity_hit:
        await asyncio.to_thread(log_db.update_pending, request_id, affinity_hit=1)

    if not result:
        guard_msg = getattr(result, "guard_error", None)
        if guard_msg:
            msg = f"Request cannot be safely routed: {guard_msg}"
            await asyncio.to_thread(
                log_db.finish_error, request_id, msg, 0,
                http_status=400, affinity_hit=(1 if result.affinity_hit else 0),
                total_ms=int((time.monotonic() - start_monotonic) * 1000),
            )
            return errors.json_error_response(
                400, errors.ErrType.INVALID_REQUEST, msg
            )

        msg = f"No available upstream channels for model: {model}"
        exclusion_summary = result.exclusion_summary()
        print(
            f"[scheduler] no channels ingress=anthropic model={model}: "
            f"{exclusion_summary}"
        )
        await asyncio.to_thread(
            log_db.finish_error, request_id, msg, 0,
            http_status=503, affinity_hit=(1 if result.affinity_hit else 0),
            total_ms=int((time.monotonic() - start_monotonic) * 1000),
        )
        # 主动告警（节流 5min）：帮助运维第一时间发现
        ek = notifier.escape_html
        await _throttled_notify(
            f"no_channels:{model}",
            "🚨 <b>无可用渠道</b>\n"
            f"客户端: <code>{ek(client_ip)}</code> / Key <code>{ek(str(key_name))}</code>\n"
            f"请求模型: <code>{ek(model)}</code>\n"
            f"筛选详情: <code>{ek(exclusion_summary)}</code>\n"
            "请按筛选详情检查渠道状态。"
        )
        # 先尝试更精准的错误类型：model 不在任何渠道 → not_found；所有渠道冷却 → api_error
        err_type = errors.ErrType.NOT_FOUND if _model_never_supported(model) else errors.ErrType.API
        status = 404 if err_type == errors.ErrType.NOT_FOUND else 503
        return errors.json_error_response(status, err_type, msg)

    preflight = _anthropic_to_openai_context_preflight(body, result)
    if preflight:
        msg = preflight["message"]
        await asyncio.to_thread(
            log_db.finish_error,
            request_id,
            msg,
            0,
            http_status=400,
            total_ms=int((time.monotonic() - start_monotonic) * 1000),
            affinity_hit=(1 if result.affinity_hit else 0),
        )
        print(
            f"[context-guard] {client_ip} {key_name} → {model} routed_model={preflight['model']} "
            f"prompt_tokens≈{preflight['prompt_tokens']} safe_limit={preflight['safe_limit']}"
        )
        return errors.json_error_response(
            400,
            errors.ErrType.INVALID_REQUEST,
            msg,
            code=protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
        )

    ts = time.strftime("%H:%M:%S", time.localtime(start_time))
    _first_list = result.candidates or result.saturated
    chosen = _first_list[0][0].key if _first_list else "?"
    sat_note = " queued" if (not result.candidates and result.saturated) else ""
    print(f"[{ts}] {client_ip} {key_name} → {model} (msgs={len(messages)}, tools={len(tools)}) "
          f"{'★' if result.affinity_hit else ''}first={chosen}{sat_note}")

    # 5.5 翻译层：对 body 中的 user/system 消息做翻译（默认关闭；失败静默回退原文）
    body = await translation.translate_body(body, ingress_protocol="anthropic", route=result)

    # 6. 故障转移 + 上游调用
    try:
        response = await failover.run_failover(
            result, body, request_id, key_name, client_ip,
            is_stream=is_stream, start_time=start_time,
            start_monotonic=start_monotonic,
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        total_ms = int((time.monotonic() - start_monotonic) * 1000)
        await asyncio.to_thread(
            log_db.finish_error, request_id, f"unexpected: {e}", 0,
            http_status=500, total_ms=total_ms,
            affinity_hit=(1 if result.affinity_hit else 0),
        )
        return errors.json_error_response(500, errors.ErrType.API, f"internal: {e}")

    return response


# ─── 启动 ─────────────────────────────────────────────────────────


class _DrainAwareServer(uvicorn.Server):
    """Uvicorn server whose signals are routed through Parrot drain first.

    Uvicorn 0.44 installs signal handlers through `capture_signals()`.  We
    override that path rather than `install_signal_handlers()` (removed in newer
    uvicorn) so SIGTERM/SIGINT first enters Parrot drain and only flips
    `should_exit` after active requests have finished or timed out.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._drain_loop: asyncio.AbstractEventLoop | None = None
        self._drain_shutdown_task: asyncio.Task | None = None

    @contextmanager
    def capture_signals(self):  # pragma: no cover - exercised by live process
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original_handlers = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig: int, frame) -> None:  # pragma: no cover - exercised by live process
        signame = signal.Signals(sig).name
        if self._drain_shutdown_task is not None and not self._drain_shutdown_task.done():
            print(f"[drain] received {signame} again; forcing immediate shutdown")
            self.force_exit = True
            self.should_exit = True
            return
        if self.should_exit:
            self.force_exit = True
            return
        drain.begin(f"signal:{signame}")
        loop = self._drain_loop
        if loop is None or not loop.is_running():
            self.should_exit = True
            return
        loop.call_soon_threadsafe(self._start_drain_shutdown_task, signame)

    def _start_drain_shutdown_task(self, signame: str) -> None:
        if self._drain_shutdown_task is not None and not self._drain_shutdown_task.done():
            return
        self._drain_shutdown_task = asyncio.create_task(self._stop_after_drain(signame))

    async def _stop_after_drain(self, signame: str) -> None:
        timeout = drain.shutdown_timeout_seconds()
        active = drain.active_count()
        if active:
            print(f"[drain] received {signame}; waiting active={active} timeout={timeout}s")
        drained = await drain.wait_for_zero(timeout)
        if drained:
            print(f"[drain] drained; stopping server signame={signame}")
        else:
            print(f"[drain] timeout; forcing server stop signame={signame} active={drain.active_count()}")
        self.should_exit = True


async def _serve_with_graceful_drain(server: uvicorn.Server) -> None:
    if isinstance(server, _DrainAwareServer):
        server._drain_loop = asyncio.get_running_loop()
    await server.serve()


def main() -> None:
    cfg = config.get()
    uvicorn_config = uvicorn.Config(
        app,
        host=cfg["listen"]["host"],
        port=cfg["listen"]["port"],
        log_level="warning",
        access_log=False,
    )
    server = _DrainAwareServer(uvicorn_config)
    asyncio.run(_serve_with_graceful_drain(server))


if __name__ == "__main__":
    main()
