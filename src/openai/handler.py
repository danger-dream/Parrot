"""OpenAI 入口的统一 handler。

对应 anthropic 侧 `server.proxy_messages`；覆盖 `/v1/chat/completions`
（`ingress_protocol="chat"`）与 `/v1/responses`（`ingress_protocol="responses"`）
两条入口，共用这一份实现。

流程（与 docs/openai/08-openai-tree.md §8.1 对齐）：
  1. auth.validate → key 验证
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
import hashlib
import json
import secrets
import time
import traceback
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import Response

from .. import (
    affinity, auth, config, errors, failover, fingerprint, local_web_tools,
    log_db, model_mapping, notifier, scheduler, translation,
)
from ..client_ip import get_client_ip
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


def _has_xai_oauth_candidate(model: str | None) -> bool:
    """Whether the current model can be served by an enabled xAI OAuth channel."""
    if not model:
        return False
    for ch in registry.all_channels():
        if not getattr(ch, "enabled", False):
            continue
        if getattr(ch, "disabled_reason", None):
            continue
        if getattr(ch, "type", "") != "oauth" or getattr(ch, "provider", "") != "xai":
            continue
        try:
            if ch.supports_model(model) is not None:
                return True
        except Exception:
            continue
    return False


def _openai_family_models_sorted(cfg: dict) -> list[str]:
    """/v1/models 之外，仅给 no_channels 告警文案用（简化描述）。"""
    # 占位：这里给一个空实现，以免引入多余依赖
    return []


def _store_enabled() -> bool:
    cfg = config.get()
    return bool(((cfg.get("openai") or {}).get("store") or {}).get("enabled", True))


def _auto_prompt_cache_cfg() -> dict:
    cfg = config.get()
    return ((cfg.get("openai") or {}).get("autoPromptCacheKey") or {})


def _auto_prompt_cache_enabled() -> bool:
    auto = _auto_prompt_cache_cfg()
    return bool(auto.get("enabled", True))


def _auto_prompt_cache_prefix() -> str:
    auto = _auto_prompt_cache_cfg()
    return str(auto.get("prefix") or "parrot:auto:v1").strip() or "parrot:auto:v1"


def _new_auto_prompt_cache_key() -> str:
    # 随机兜底：仅在无法构造稳定会话 anchor 时使用。
    return f"{_auto_prompt_cache_prefix()}:{secrets.token_hex(16)}"


def _canon_anchor_value(value: Any) -> Any:
    """为稳定 prompt_cache_key anchor 做轻量归一化。

    保留会话开头的稳定内容，但只参与 hash，不把原文放进 key。
    """
    if isinstance(value, dict):
        return {str(k): _canon_anchor_value(value[k]) for k in sorted(value.keys(), key=str)}
    if isinstance(value, list):
        return [_canon_anchor_value(v) for v in value]
    return value


def _chat_anchor_messages(messages: Any) -> list[Any]:
    """取 chat 稳定 anchor。

    不能只取 system/developer + 第一条 user：OpenClaw 这类客户端的
    bootstrap user 可能在所有新会话里完全相同，会把不同会话锁到同一个
    prompt_cache_key。这里要求至少两条 user message，再取开头连续
    system/developer + 前两条 user；不足两条 user 时返回空，让调用方走
    随机 key + fingerprint 亲和链。
    """
    if not isinstance(messages, list):
        return []
    prefix: list[Any] = []
    users: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if not users and role in ("system", "developer"):
            prefix.append(msg)
            continue
        if role == "user":
            users.append(msg)
            if len(users) >= 2:
                break
    if len(users) < 2:
        return []
    return prefix + users[:2]


def _responses_input_anchor_items(items: Any) -> list[Any]:
    """取 responses input 内的稳定 anchor。

    真实 OpenClaw/Codex 请求常把 system 放在 input[0]，而不是顶层
    instructions。这里和 chat 入口对齐：开头连续 system/developer + 前两条
    user；不足两条 user 时返回空，避免固定 bootstrap 首条 user 跨会话锁死。
    """
    if not isinstance(items, list):
        return []
    prefix: list[Any] = []
    users: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        typ = item.get("type")
        is_message = (typ in (None, "message")) and role is not None
        if not users and is_message and role in ("system", "developer"):
            prefix.append(item)
            continue
        if is_message and role == "user":
            users.append(item)
            if len(users) >= 2:
                break
    if len(users) < 2:
        return []
    return prefix + users[:2]


def _responses_anchor_items(body: dict) -> list[Any]:
    """取 responses 稳定 anchor：instructions + input 开头 system/developer + 前两条 user。

    同 chat 一样，至少需要两条 user message 才启用稳定 key，避免固定
    bootstrap 首条 user 导致跨会话锁定。
    """
    items = resolve_current_input_items(body)
    input_anchors = _responses_input_anchor_items(items)
    if not input_anchors:
        return []
    anchors: list[Any] = []
    if body.get("instructions"):
        anchors.append({"instructions": body.get("instructions")})
    anchors.extend(input_anchors)
    return anchors


def _stable_prompt_cache_key(
    body: dict,
    *,
    api_key_name: str,
    client_ip: str,
    model: str,
    ingress_protocol: str,
) -> str | None:
    """基于会话 anchor 生成稳定 prompt_cache_key。

    用于 fingerprint 亲和链未命中时的 fallback，避免同一长会话中途因为
    临时 fp miss 生成新随机 key，进而打断 OpenAI prompt cache/session_id。
    """
    anchors: list[Any] = []
    if ingress_protocol == "chat":
        anchors = _chat_anchor_messages(body.get("messages") or [])
    elif ingress_protocol == "responses":
        anchors = _responses_anchor_items(body)

    anchors = [a for a in anchors if a]
    if not anchors:
        return None

    material = {
        "v": 1,
        "api_key_name": api_key_name or "",
        "client_ip": client_ip or "",
        "model": model or "",
        "ingress_protocol": ingress_protocol or "",
        "anchors": _canon_anchor_value(anchors),
    }
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"{_auto_prompt_cache_prefix()}:stable:{digest}"


def _maybe_apply_auto_prompt_cache_key(
    body: dict,
    *,
    fp_query: str | None,
    api_key_name: str = "",
    client_ip: str = "",
    model: str = "",
    ingress_protocol: str = "chat",
) -> str | None:
    """OpenAI 协议专用：下游未传 prompt_cache_key 时自动补一个。

    优先级：下游显式值 → fingerprint 亲和链值 → 稳定 anchor key → 随机兜底。
    成功响应后由 failover 把最终 key 绑定到 fp_write。
    """
    if not isinstance(body, dict):
        return None
    existing = str(body.get("prompt_cache_key") or "").strip()
    if existing:
        return existing
    if not _auto_prompt_cache_enabled():
        return None

    key: str | None = None
    if fp_query:
        try:
            entry = affinity.get(fp_query)
        except Exception:
            entry = None
        if isinstance(entry, dict):
            val = str(entry.get("prompt_cache_key") or "").strip()
            if val:
                key = val
    if not key:
        key = _stable_prompt_cache_key(
            body,
            api_key_name=api_key_name,
            client_ip=client_ip,
            model=model,
            ingress_protocol=ingress_protocol,
        )
    if not key:
        key = _new_auto_prompt_cache_key()
    body["prompt_cache_key"] = key
    internal = body.setdefault("_internal_injected_fields", [])
    if isinstance(internal, list) and "prompt_cache_key" not in internal:
        internal.append("prompt_cache_key")
    return key


# ─── 主入口 ───────────────────────────────────────────────────────

async def handle(request: Request, *, ingress_protocol: str) -> Response:
    if ingress_protocol not in ("chat", "responses"):
        return errors.json_error_openai(
            500, errors.ErrTypeOpenAI.SERVER,
            f"invalid ingress_protocol: {ingress_protocol}",
        )

    start_time = time.time()
    start_monotonic = time.monotonic()
    request_id = str(uuid.uuid4())
    client_ip = get_client_ip(request)

    # 1. auth
    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return errors.json_error_openai(401, errors.ErrTypeOpenAI.AUTH, err)

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
    # Preserve the original client-provided field set before Parrot mutates the
    # body (default model, internal _api_key_name, auto prompt_cache_key, ...).
    # Cross-protocol guards use this to reject user-explicit fields that have no
    # safe equivalent while allowing internal compatibility hints.
    body["_client_body_fields"] = sorted(str(k) for k in body.keys() if isinstance(k, str))

    # 2.1 模型映射 / 入口默认模型：
    #     - body.model 缺失 → 填入该 ingress 的默认（若配置）
    #     - body.model 命中别名 → 改写成真实名（只解一层）
    #     后续白名单/调度/channel 全按真实名走。
    #     ingress_protocol 这里是 "chat"/"responses"，需要转成配置里的
    #     完整 ingress line 名。
    _ingress_line = (
        "openai-chat" if ingress_protocol == "chat" else "openai-responses"
    )
    model_mapping.apply_default(body, _ingress_line)
    model_mapping.apply_mapping(body, _ingress_line)

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

    if ingress_protocol == "responses":
        # Normalize hosted search-ish tools before scheduling.  xAI/Grok has a
        # native Responses-compatible web_search tool, so keep it hosted when a
        # matching xAI OAuth channel can serve the requested model.  Other
        # OpenAI-family upstreams keep the previous safe behaviour: web_search
        # is converted into Parrot's local AnySearch-backed function loop, while
        # non-portable hosted tools are dropped/guarded.
        if (
            local_web_tools.request_declares_openai_web_search_tools(body)
            and _has_xai_oauth_candidate(model)
        ):
            local_web_tools.prepare_xai_responses_native_web_search_tools(body)
        else:
            local_web_tools.prepare_openai_responses_local_web_tools(body)

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

    # 5.1 OpenAI 专用 prompt cache 路由 hint：下游没传时基于亲和链自动补。
    #     Anthropic/其他协议不走本 handler，不受影响。
    _maybe_apply_auto_prompt_cache_key(
        body,
        fp_query=fp_query,
        api_key_name=key_name or "",
        client_ip=client_ip,
        model=model,
        ingress_protocol=ingress_protocol,
    )

    # 6. pending 日志；剥掉下划线前缀的内部 metadata（_api_key_name 等）后再落盘
    reasoning_effort = log_db.extract_reasoning_effort(body, ingress_protocol)
    req_headers = _sanitize_headers(dict(request.headers))
    fast_mode = log_db.extract_fast_mode(body, ingress_protocol, req_headers)
    log_body = {k: v for k, v in body.items() if not (isinstance(k, str) and k.startswith("_"))}
    await asyncio.to_thread(
        log_db.insert_pending,
        request_id, client_ip, key_name, model, is_stream, msg_count, tool_count,
        req_headers, log_body, fingerprint=fp_query,
        ingress_protocol=ingress_protocol,
        reasoning_effort=reasoning_effort,
        fast_mode=fast_mode,
    )

    # 7. 调度（ingress_protocol 决定家族过滤；fp_query 决定亲和命中）
    result = scheduler.schedule(
        body, api_key_name=key_name or "", client_ip=client_ip,
        ingress_protocol=ingress_protocol, fp_query=fp_query,
    )
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
            return errors.json_error_openai(
                400, errors.ErrTypeOpenAI.INVALID_REQUEST, msg
            )

        msg = f"No available upstream channels for model: {model} (ingress={ingress_protocol})"
        await asyncio.to_thread(
            log_db.finish_error, request_id, msg, 0,
            http_status=503, affinity_hit=(1 if result.affinity_hit else 0),
            total_ms=int((time.monotonic() - start_monotonic) * 1000),
        )
        # 节流告警
        ek = notifier.escape_html
        await notifier.throttled_notify_event(
            "no_channels",
            f"no_channels:{ingress_protocol}:{model}",
            f"🚨 <b>无可用渠道</b>（{notifier.provider_tag('openai')} 入口）\n"
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
    _first_list = result.candidates or result.saturated
    chosen = _first_list[0][0].key if _first_list else "?"
    sat_note = " queued" if (not result.candidates and result.saturated) else ""
    print(f"[{ts}] {client_ip} {key_name} → {ingress_protocol}:{model} "
          f"(msgs={msg_count}, tools={tool_count}) "
          f"{'★' if result.affinity_hit else ''}first={chosen}{sat_note}")

    # 7.5 翻译层
    body = await translation.translate_body(body, ingress_protocol=ingress_protocol, route=result)

    # 7. failover
    try:
        response = await failover.run_failover(
            result, body, request_id, key_name or "", client_ip,
            is_stream=is_stream, start_time=start_time,
            ingress_protocol=ingress_protocol,
            start_monotonic=start_monotonic,
        )
    except Exception as exc:
        traceback.print_exc()
        total_ms = int((time.monotonic() - start_monotonic) * 1000)
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
