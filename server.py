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
import json
import time
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from src import (
    __version__,
    affinity, auth, config, cooldown, errors, failover,
    fingerprint, log_db, model_mapping, notifier, oauth_manager, probe,
    public_ip, scheduler, scorer, state_db, upstream,
)
from src.channel import registry
from datetime import datetime, timezone
from src.telegram import bot as tgbot
from src.transform.cc_mimicry import DEVICE_ID


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
    # 持久化层
    state_db.init()
    log_db.init()
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
    _background_tasks.append(asyncio.create_task(oauth_manager.proactive_refresh_loop()))
    _background_tasks.append(asyncio.create_task(oauth_manager.quota_monitor_loop()))
    _background_tasks.append(asyncio.create_task(probe.recovery_loop()))
    _background_tasks.append(asyncio.create_task(openai_store.cleanup_loop()))

    try:
        yield
    finally:
        for t in _background_tasks:
            t.cancel()
        await asyncio.gather(*_background_tasks, return_exceptions=True)
        tgbot.stop()
        await upstream.close_client()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _model_never_supported(model: str) -> bool:
    """model 在当前任何渠道（包括已禁用）里都不可能被路由 → True。
    用于把"模型不存在"与"模型存在但全都冷却"区分开。"""
    for ch in registry.all_channels():
        if ch.supports_model(model):
            return False
    return True


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
            cd_entries = cooldown.active_entries()
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
        "status": status,
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
    - 若 Key 有 allowedProtocols，按家族过滤（解决两家族同名模型冲突，例：
      openai 与 anthropic 都叫 claude-3.5）
    - 若 Key 有 allowedModels 白名单，再和家族结果取交集
    - 否则返回所有启用渠道聚合的去重模型列表
    """
    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return errors.json_error_response(401, errors.ErrType.AUTH, err)

    # 按 Key 的 allowedProtocols 推断家族。空/未设 = 全部家族。
    allowed_protos = auth.get_allowed_protocols(key_name)
    if allowed_protos:
        families = {"anthropic" if p == "anthropic" else "openai" for p in allowed_protos}
        all_models = registry.available_models_for_families(families)
    else:
        all_models = registry.available_models()
    if allowed_models:
        allowed_set = set(allowed_models)
        visible = [m for m in all_models if m in allowed_set]
    else:
        visible = all_models

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


@app.post("/v1/messages")
async def proxy_messages(request: Request):
    start_time = time.time()
    request_id = str(uuid.uuid4())
    client_ip = request.client.host if request.client else "?"

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

    # 2.1 模型映射 / 入口默认模型：
    #     - body.model 缺失 → 填入该 ingress 的默认（若配置）
    #     - body.model 命中别名 → 改写成真实名（只解一层）
    #     后续白名单/调度/channel 全按真实名走。
    model_mapping.apply_default(body, "anthropic")
    model_mapping.apply_mapping(body, "anthropic")

    model = body.get("model")
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

    is_stream = bool(body.get("stream", True))
    messages = body.get("messages") or []
    tools = body.get("tools") or []

    # 3. 调度：先计算指纹（供 log_db 记录）
    fp_query = fingerprint.fingerprint_query(key_name or "", client_ip, messages)

    # 4. pending 日志
    req_headers = _sanitize_headers(dict(request.headers))
    await asyncio.to_thread(
        log_db.insert_pending,
        request_id, client_ip, key_name, model, is_stream,
        len(messages), len(tools),
        req_headers, body,
        fingerprint=fp_query,
        ingress_protocol="anthropic",
    )

    # 5. 调度
    result = scheduler.schedule(body, api_key_name=key_name, client_ip=client_ip)

    # pending 时更新 affinity_hit（亲和命中本身需要调度之后才知道）
    if result.affinity_hit:
        await asyncio.to_thread(log_db.update_pending, request_id, affinity_hit=1)

    if not result.candidates:
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

    ts = time.strftime("%H:%M:%S", time.localtime(start_time))
    chosen = result.candidates[0][0].key
    print(f"[{ts}] {client_ip} {key_name} → {model} (msgs={len(messages)}, tools={len(tools)}) "
          f"{'★' if result.affinity_hit else ''}first={chosen}")

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

def main() -> None:
    cfg = config.get()
    uvicorn.run(
        app,
        host=cfg["listen"]["host"],
        port=cfg["listen"]["port"],
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
