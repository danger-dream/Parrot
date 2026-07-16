"""Parrot 主入口（多家族 AI 协议代理）。

启动时：
  - 加载配置、state.db、logs/YYYY-MM.db
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
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from uvicorn.server import HANDLED_SIGNALS
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from src import (
    __version__, drain,
    affinity, apikey_limiter, auth, compact_rescue, config, cooldown, errors, failover,
    fingerprint, image_db, log_db, model_mapping, model_metadata, network,
    network_monitor, notifier, oauth_manager, probe, public_ip, scheduler, scorer,
    state_db, status_monitor, token_counter, translation, update_checker, updater,
    upstream,
)
from src.channel import registry
from src.client_ip import get_client_ip
from datetime import datetime, timezone
from src.telegram import bot as tgbot
from src.protocols import errors as protocol_errors
from src.transform.cc_mimicry import (
    DEVICE_ID,
    PARROT_DOWNSTREAM_BETAS_KEY,
    PARROT_ORIGINAL_MODEL_KEY,
    PARROT_WANTS_CONTEXT_1M_KEY,
    PARROT_WANTS_FAST_MODE_KEY,
    parse_beta_header,
    request_wants_context_1m,
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
            state_db.checkpoint()
        except Exception as e:
            print(f"[state_db] checkpoint failed: {e}")
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


async def _stale_pending_loop():
    while True:
        await asyncio.sleep(300)
        try:
            cleared = await asyncio.to_thread(log_db.cleanup_stale_pending, 1800)
            if cleared:
                print(f"[log_db] cleaned {cleared} stale pending records")
        except Exception as e:
            print(f"[log_db] stale cleanup failed: {e}")


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
    # 出站网络层必须最先初始化，确保后续 OAuth/TG/status/update 等请求都走统一 DNS/代理。
    network.init()
    network.bootstrap_system_dns_once()

    # 持久化层
    state_db.init()
    log_db.init()
    image_db.init()
    translation.init()
    await asyncio.to_thread(log_db.cleanup_stale_pending, 1800)

    # 老数据 provider 字段回填（无 provider 字段的账户默认 claude；幂等）
    try:
        migrated = oauth_manager.migrate_provider_field()
        if migrated:
            print(f"[oauth] migrated provider='claude' for {migrated} legacy account(s)")
    except Exception as exc:
        print(f"[oauth] provider field migration failed: {exc}")

    # 联合主键迁移：email → account_key (=f"{provider}:{email}")。幂等，已迁移过直接跳过。
    try:
        # 迁移前备份 state.db 做保险（已存在备份则不覆盖）
        import os as _os, shutil as _shutil
        _src = state_db._db_path
        _bak = (_src or "") + ".pre_composite_key.bak"
        if _src and _os.path.exists(_src) and not _os.path.exists(_bak):
            try:
                _shutil.copy2(_src, _bak)
                print(f"[state_db] backup created: {_bak}")
            except Exception as _exc:
                print(f"[state_db] backup failed (continuing): {_exc}")
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

    # 内存表从 state.db 恢复
    affinity.init()
    affinity.client_init()
    cooldown.init()
    scorer.init()

    # OpenAI 家族 factory 注入（必须在 rebuild_from_config 之前，否则带 protocol=openai-*
    # 的 channel entry 会回落到 ApiChannel 并被 assert 拒绝）
    from src.openai.channel.registration import register_factories as _openai_register_factories
    _openai_register_factories()

    # OpenAI previous_response_id Store（挂在同一张 state.db，独立表）
    from src.openai import store as openai_store
    openai_store.init()

    # 渠道注册表 + 热加载钩子
    registry.rebuild_from_config()
    registry.install_config_reload_hook()

    # httpx 客户端
    upstream.create_client()

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
    _background_tasks.append(asyncio.create_task(probe.recovery_loop()))
    _background_tasks.append(asyncio.create_task(status_monitor.monitor_loop()))
    _background_tasks.append(asyncio.create_task(network_monitor.monitor_loop()))
    _background_tasks.append(asyncio.create_task(update_checker.update_loop()))
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
        await upstream.close_client()


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
}


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
    """Return a retryable service error when bounded spool I/O is unavailable."""
    message = str(exc) or "queued request body spool is unavailable"
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


@app.middleware("http")
async def _drain_http_middleware(request: Request, call_next):
    """Reject new work while draining and keep active bodies counted.

    The active lease is held until the response body iterator finishes.  That
    matters for StreamingResponse/SSE because the route handler returns before
    the chunked body is done.  下游 API Key 限流也在这里统一挂载，避免每个
    handler 分别处理 streaming release。
    """

    path = request.url.path
    if drain.is_draining() and not drain.allow_path_during_drain(path):
        return drain.reject_response()

    # Health checks must not keep a draining process alive.
    if drain.allow_path_during_drain(path):
        return await call_next(request)

    lease = await drain.enter(f"http {request.method} {path}")
    key_lease = None
    try:
        if request.method.upper() == "POST" and path in _API_KEY_LIMITED_HTTP_PATHS:
            key_name, _allowed_models, err = auth.validate(request.headers)
            if not err and key_name:
                try:
                    key_lease = await apikey_limiter.acquire(key_name, request)
                except apikey_limiter.RequestBodyTooLarge as exc:
                    await lease.aclose()
                    return _request_body_limit_error_response(path, exc)
                except apikey_limiter.QueuedBodySpoolError as exc:
                    await lease.aclose()
                    return _queued_body_spool_error_response(path, exc)
                except apikey_limiter.ApiKeyLimitError as exc:
                    await lease.aclose()
                    return _api_key_limit_error_response(path, exc)
        response = await call_next(request)
    except apikey_limiter.RequestBodyTooLarge as exc:
        if key_lease is not None:
            await key_lease.release()
        await lease.aclose()
        return _request_body_limit_error_response(path, exc)
    except apikey_limiter.QueuedBodySpoolError as exc:
        if key_lease is not None:
            await key_lease.release()
        await lease.aclose()
        return _queued_body_spool_error_response(path, exc)
    except BaseException as exc:
        if key_lease is not None:
            await key_lease.release()
        await lease.aclose()
        body_limit_error = _find_request_body_limit_error(exc)
        if body_limit_error is not None:
            return _request_body_limit_error_response(path, body_limit_error)
        spool_error = _find_queued_body_spool_error(exc)
        if spool_error is not None:
            return _queued_body_spool_error_response(path, spool_error)
        raise

    if key_lease is not None:
        response = apikey_limiter.attach_release_to_response(response, key_lease)

    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        await lease.aclose()
        return response

    async def _wrapped_body_iterator():
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            await lease.aclose()

    response.body_iterator = _wrapped_body_iterator()
    return response


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
    model_candidates = []
    for candidate in (resolved_model, body.get("model")):
        name = str(candidate or "").strip()
        if name and name not in model_candidates:
            model_candidates.append(name)
    metadata_model = ""
    safe_limit = None
    for name in model_candidates:
        limit = model_metadata.safe_prompt_limit(name)
        if limit is not None and limit > 0:
            metadata_model = name
            safe_limit = limit
            break
    if not metadata_model or safe_limit is None:
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

    # 把 modelMapping 里的别名也当成可用模型暴露出去:
    # 条件 = 别名指向的真实模型也在 visible 集合里 (否则客户端调不通,
    # 暴露就是坑)。API Key 不再按协议入口过滤，模型权限仍由 allowedModels 控制。
    visible_set = set(visible)
    alias_seen: set[str] = set()
    for _line in model_mapping.INGRESS_LINES:
        _mp = model_mapping.get_ingress_map(_line)
        for _alias, _real in _mp.items():
            if _alias in visible_set or _alias in alias_seen:
                continue
            if _real not in visible_set:
                continue
            # Key 有白名单时, 别名必须显式授权 (白名单按真名语义, 但下游看到的
            # 是别名, 这里做 strict 检查: 白名单里如果没别名就不暴露)
            if allowed_models and _alias not in set(allowed_models):
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


# OpenAI Images API 兼容入口：标准 schema、对接现有 OAuth 账号管线。
@app.post(
    "/v1/images/generations",
    summary="OpenAI-compatible image generation",
    description=(
        "Standard OpenAI `/v1/images/generations` endpoint. Accepts `prompt`, "
        "`model`, `n`, `size`, `response_format`, `quality`, `background`, "
        "`output_format`, `moderation`, `style`, `output_compression`, "
        "`partial_images`. Internally uses Parrot's OpenAI OAuth account pool. "
        "`n > 1` is downgraded to 1 (one image per upstream call) and the "
        "response includes a `parrot_warning` field."
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


@app.post("/v1/messages")
async def proxy_messages(request: Request):
    start_time = time.time()
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
    explicit_context_1m = request_wants_context_1m(
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
    # True = 下游显式要求 1M；None = 交给 Parrot 默认策略（目前仅 Opus 4.x 默认开启）。
    body[PARROT_WANTS_CONTEXT_1M_KEY] = explicit_context_1m or None
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
        body["_parrot_claude_code_session_id"] = claude_session_id

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
                total_ms=int((time.time() - start_time) * 1000),
            )
            return errors.json_error_response(
                400, errors.ErrType.INVALID_REQUEST, msg
            )

        msg = f"No available upstream channels for model: {model}"
        await asyncio.to_thread(
            log_db.finish_error, request_id, msg, 0,
            http_status=503, affinity_hit=(1 if result.affinity_hit else 0),
            total_ms=int((time.time() - start_time) * 1000),
        )
        # 主动告警（节流 5min）：帮助运维第一时间发现
        ek = notifier.escape_html
        await _throttled_notify(
            f"no_channels:{model}",
            "🚨 <b>无可用渠道</b>\n"
            f"客户端: <code>{ek(client_ip)}</code> / Key <code>{ek(str(key_name))}</code>\n"
            f"请求模型: <code>{ek(model)}</code>\n"
            "请检查渠道是否全部禁用或全部进入冷却。"
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
            total_ms=int((time.time() - start_time) * 1000),
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
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        total_ms = int((time.time() - start_time) * 1000)
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
