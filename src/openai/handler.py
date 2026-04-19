"""OpenAI 入口的统一 handler。

对应 anthropic 侧 `server.proxy_messages`；覆盖 `/v1/chat/completions`
（`ingress_protocol="chat"`）与 `/v1/responses`（`ingress_protocol="responses"`）
两条入口，共用这一份实现。

流程（与 docs/openai/08-openai-tree.md §8.1 对齐）：
  1. auth.validate → key 验证；get_allowed_protocols → 按 Key 限制放行
  2. 读 body
  3. model 白名单 (allowedModels) 检查
  4. CapabilityGuard 自检（n>1 / audio / background / conversation 等）
  5. fingerprint_query（MS-7 接入；此阶段占位传 None）
  6. log_db.insert_pending
  7. scheduler.schedule(ingress_protocol=...)
  8. failover.run_failover(..., ingress_protocol=...)

注：openai 家族没有 CC 伪装，并发量 / usage / 亲和 等细节与 anthropic 共用
调度 / 评分 / 冷却 基础设施。
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from .. import auth, config, errors, failover, fingerprint, log_db, notifier, scheduler
from ..channel import registry
from .transform.guard import GuardError, guard_chat_ingress, guard_responses_ingress
from .transform.responses_to_chat import resolve_current_input_items


# ─── 辅助 ─────────────────────────────────────────────────────────

def _sanitize_headers(headers: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("authorization", "x-api-key"):
            out[k] = "***"
        else:
            out[k] = v
    return out


def _count_msg_tool(body: dict, ingress_protocol: str) -> tuple[int, int]:
    """返回 (msg_count, tool_count)；入 log_db 统计用。"""
    tools = body.get("tools") or []
    tool_count = len(tools) if isinstance(tools, list) else 0

    if ingress_protocol == "chat":
        msgs = body.get("messages") or []
        return (len(msgs) if isinstance(msgs, list) else 0), tool_count

    # responses
    inp = body.get("input")
    if isinstance(inp, list):
        return len(inp), tool_count
    if inp is None:
        return 0, tool_count
    # string input：一条
    return 1, tool_count


def _openai_family_models_sorted(cfg: dict) -> list[str]:
    """/v1/models 之外，仅给 no_channels 告警文案用（简化描述）。"""
    # 占位：这里给一个空实现，以免引入多余依赖
    return []


def _store_enabled() -> bool:
    cfg = config.get()
    return bool(((cfg.get("openai") or {}).get("store") or {}).get("enabled", True))


# ─── 主入口 ───────────────────────────────────────────────────────

async def handle(request: Request, *, ingress_protocol: str) -> Response:
    if ingress_protocol not in ("chat", "responses"):
        return errors.json_error_openai(
            500, errors.ErrTypeOpenAI.SERVER,
            f"invalid ingress_protocol: {ingress_protocol}",
        )

    start_time = time.time()
    request_id = str(uuid.uuid4())
    client_ip = request.client.host if request.client else "?"

    # 1. auth
    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return errors.json_error_openai(401, errors.ErrTypeOpenAI.AUTH, err)

    allowed_protos = auth.get_allowed_protocols(key_name)
    if allowed_protos and ingress_protocol not in allowed_protos:
        return errors.json_error_openai(
            403, errors.ErrTypeOpenAI.PERMISSION,
            f"protocol '{ingress_protocol}' is not allowed for this API key",
        )

    # 2. body
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception as exc:
        return errors.json_error_openai(
            400, errors.ErrTypeOpenAI.INVALID_REQUEST, f"invalid json: {exc}",
        )
    if not isinstance(body, dict):
        return errors.json_error_openai(
            400, errors.ErrTypeOpenAI.INVALID_REQUEST, "request body must be a JSON object",
        )

    # 3. model 白名单
    model = body.get("model")
    if not model:
        return errors.json_error_openai(
            400, errors.ErrTypeOpenAI.INVALID_REQUEST, "model is required",
        )
    if allowed_models and model not in allowed_models:
        return errors.json_error_openai(
            403, errors.ErrTypeOpenAI.PERMISSION,
            f"model '{model}' is not allowed for this API key "
            f"(allowed: {', '.join(allowed_models) or 'none'})",
        )

    # 4. CapabilityGuard
    try:
        if ingress_protocol == "chat":
            guard_chat_ingress(body)
        else:
            guard_responses_ingress(body, store_enabled=_store_enabled())
    except GuardError as ge:
        return errors.json_error_openai(ge.status, ge.err_type, ge.message, param=ge.param)

    # OpenAI 默认非流式（与 anthropic 默认流式相反）
    is_stream = bool(body.get("stream", False))
    msg_count, tool_count = _count_msg_tool(body, ingress_protocol)

    # 传递 api_key_name 给 OpenAIApiChannel.build_upstream_request（通过 body 内嵌字段）。
    # 下划线前缀 + 不在 CHAT/RESPONSES_REQ_ALLOWED 白名单里 → filter_*_passthrough 不会转发给上游。
    body["_api_key_name"] = key_name or ""

    # 5. fingerprint_query（会话亲和；MS-7 接入）
    if ingress_protocol == "chat":
        fp_query = fingerprint.fingerprint_query_chat(
            key_name or "", client_ip, body.get("messages") or []
        )
    else:
        fp_query = fingerprint.fingerprint_query_responses(
            key_name or "", client_ip, resolve_current_input_items(body)
        )

    # 6. pending 日志；剥掉下划线前缀的内部 metadata（_api_key_name 等）后再落盘
    req_headers = _sanitize_headers(dict(request.headers))
    log_body = {k: v for k, v in body.items() if not (isinstance(k, str) and k.startswith("_"))}
    await asyncio.to_thread(
        log_db.insert_pending,
        request_id, client_ip, key_name, model, is_stream, msg_count, tool_count,
        req_headers, log_body, fingerprint=fp_query,
    )

    # 7. 调度（ingress_protocol 决定家族过滤；fp_query 决定亲和命中）
    result = scheduler.schedule(
        body, api_key_name=key_name or "", client_ip=client_ip,
        ingress_protocol=ingress_protocol, fp_query=fp_query,
    )
    if result.affinity_hit:
        await asyncio.to_thread(log_db.update_pending, request_id, affinity_hit=1)

    if not result.candidates:
        msg = f"No available upstream channels for model: {model} (ingress={ingress_protocol})"
        await asyncio.to_thread(
            log_db.finish_error, request_id, msg, 0,
            http_status=503, affinity_hit=(1 if result.affinity_hit else 0),
            total_ms=int((time.time() - start_time) * 1000),
        )
        # 节流告警
        ek = notifier.escape_html
        await notifier.throttled_notify_event(
            "no_channels",
            f"no_channels:{ingress_protocol}:{model}",
            "🚨 <b>无可用渠道</b>（OpenAI 入口）\n"
            f"客户端: <code>{ek(client_ip)}</code> / Key <code>{ek(str(key_name))}</code>\n"
            f"入口: <code>{ingress_protocol}</code> / 模型: <code>{ek(model)}</code>\n"
            "请检查该家族是否有启用且未冷却的渠道。",
        )
        # 区分 model-not-exist（任何家族都没有的模型）与 no-candidates
        err_type = errors.ErrTypeOpenAI.NOT_FOUND if _model_never_supported(model) \
            else errors.ErrTypeOpenAI.SERVER
        status = 404 if err_type == errors.ErrTypeOpenAI.NOT_FOUND else 503
        return errors.json_error_openai(status, err_type, msg)

    ts = time.strftime("%H:%M:%S", time.localtime(start_time))
    chosen = result.candidates[0][0].key
    print(f"[{ts}] {client_ip} {key_name} → {ingress_protocol}:{model} "
          f"(msgs={msg_count}, tools={tool_count}) "
          f"{'★' if result.affinity_hit else ''}first={chosen}")

    # 7. failover
    try:
        response = await failover.run_failover(
            result, body, request_id, key_name or "", client_ip,
            is_stream=is_stream, start_time=start_time,
            ingress_protocol=ingress_protocol,
        )
    except Exception as exc:
        traceback.print_exc()
        total_ms = int((time.time() - start_time) * 1000)
        await asyncio.to_thread(
            log_db.finish_error, request_id, f"unexpected: {exc}", 0,
            http_status=500, total_ms=total_ms,
            affinity_hit=(1 if result.affinity_hit else 0),
        )
        return errors.json_error_openai(
            500, errors.ErrTypeOpenAI.SERVER, f"internal: {exc}",
        )

    return response


def _model_never_supported(model: str) -> bool:
    """model 在任何渠道（含禁用）中都不存在 → True。

    与 server._model_never_supported 等价，但独立一份以免 server 导入 openai
    形成循环依赖。任一实现改动应同步。
    """
    for ch in registry.all_channels():
        if ch.supports_model(model):
            return False
    return True
