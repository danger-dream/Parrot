"""Correlate TG failures with safe operation context and credential-free stacks."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
import traceback

import httpx

from ..management_control.errors import ManagementError
from ..oauth.zhipu.common import ZhipuError
from . import states, ui

_MENUS = {
    "oa": "OAuth账户管理", "mc": "模型中心", "map": "模型映射", "ak": "API Key管理",
    "ch": "渠道管理", "stats": "统计", "logs": "请求日志", "menu": "菜单导航",
    "search": "搜索设置", "proxy": "代理设置", "system": "系统设置", "mauth": "管理登录审批",
}
_ROUTES = {
    "oa:zh:login": "智谱OAuth登录", "oa:zh:project": "智谱项目选择",
    "oa:zh:reset": "智谱重置卡查询", "oa:zh:confirm": "智谱确认操作",
    "oa:claude_reset_ask": "Claude重置状态查询", "oa:claude_reset_confirm": "Claude重置确认",
    "oa:claude_reset_execute": "Claude重置执行", "oa:view": "OAuth账户详情",
    "oa:refresh_token": "OAuth凭据刷新", "oa:refresh_usage": "OAuth额度查询",
}
_STATES = {"oa_zh_input": "智谱账号导入", "oa_zh_name": "智谱账号命名", "oa_zh_login": "智谱OAuth登录", "oa_zh_callback": "智谱OAuth回调处理"}
_MESSAGES = {
    "RESOURCE_NOT_FOUND": "对象已移除，请重新打开菜单。",
    "INVALID_OPERATION_STATE": "操作或确认已过期，请重新打开菜单。",
    "REVISION_CONFLICT": "账号或设置已变更，请重新打开页面。",
    "STATE_CONFLICT": "状态已变更或正在处理，请重新查看当前状态。",
    "IDENTITY_CONFLICT": "该账号已存在，请通过覆盖确认流程更新。",
    "CAPABILITY_DENIED": "当前账号没有此操作权限。",
    "AUTHENTICATION_FAILED": "认证失败，请重新登录。",
    "VALIDATION_FAILED": "输入不符合要求，请按当前页面说明检查。",
    "RATE_LIMITED": "请求过于频繁，请稍后重试。",
    "UPSTREAM_TIMEOUT": "上游响应超时。",
    "UPSTREAM_ERROR": "上游服务请求失败。",
    "SERVICE_NOT_READY": "服务尚未就绪，请稍后重试。",
}


def update_context(update: dict) -> dict:
    """Capture before dispatch (handlers may pop state); never retain input/data."""
    cb = update.get("callback_query") or {}
    msg = cb.get("message") or update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    state = states.get_state(chat_id) if type(chat_id) is int else None
    action = str((state or {}).get("action") or "").partition(":")[0]
    data = str(cb.get("data") or "")
    route = next((key for key in _ROUTES if data == key or data.startswith(key + ":")), "")
    root = data.partition(":")[0]
    if not route and root in _MENUS:
        route = root
    kind = "callback" if cb else "document" if msg.get("document") else "message"
    operation = _ROUTES.get(route) or _MENUS.get(route) or _STATES.get(action) or "消息处理"
    return {"operation": operation, "event_kind": kind, "route": route,
            "state_action": action if action in _STATES else "",
            "update_id": update.get("update_id") if type(update.get("update_id")) is int else None}


def _stack(exc: BaseException) -> list[dict]:
    """All frame locations and chained types; no locals, source text or raw messages.

    Exception strings (including HTTP URLs/KeyError keys) can contain credentials.
    Their safe code/status is recorded separately, not guessed with a redactor.
    """
    result, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        frames = [{"file": frame.filename, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(current.__traceback__)]
        result.append({"type": type(current).__name__, "frames": frames})
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return result


def report(exc: BaseException, *, operation: str, event_kind="background", route="", state_action="", update_id=None) -> str:
    incident = "TG-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:10].upper()
    code = exc.code.value if isinstance(exc, ManagementError) else exc.code if isinstance(exc, ZhipuError) else ""
    stage = exc.stage if isinstance(exc, ZhipuError) and exc.stage in {
        "request", "response", "login", "project_lookup", "model_key", "quota", "network",
        "projects", "profile", "subscription", "key_list", "key_copy", "key_create", "key_save", "mcp",
        "reset_status", "reset_use", "reset_opportunity",
    } else ""
    kind = exc.kind if isinstance(exc, ZhipuError) and exc.kind in {
        "business", "network", "timeout", "upstream", "invalid_json", "too_large", "disabled",
    } else ""
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else getattr(exc, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    message = _MESSAGES.get(code, "操作未完成，请将故障编号提供给管理员排查。")
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        message = "上游响应超时。"
    elif isinstance(exc, httpx.TransportError):
        message = "无法连接上游服务。"
    elif status in (401, 403):
        message = "上游拒绝认证或权限，请检查登录状态。"
    elif status == 429:
        message = "上游请求限流，请稍后重试。"
    elif status:
        message = f"上游请求失败（HTTP {status}）。"
    if isinstance(exc, ZhipuError):
        if kind == "business":
            message = f"上游业务请求被拒绝（业务码 {code}）。" if code is not None else "上游业务请求被拒绝。"
        elif kind in {"network", "timeout"}:
            message = "上游网络连接失败或响应超时。"
    timeout_phase = exc.timeout_phase if isinstance(exc, ZhipuError) and exc.timeout_phase in {"connect", "read", "write", "pool", "total"} else ""
    if kind == "timeout" and timeout_phase:
        message = {"connect": "连接上游超时。", "read": "等待上游响应/读取超时。", "write": "发送请求超时。",
                   "pool": "等待可用连接超时。", "total": "本次查询已达到 180 秒总时限。"}[timeout_phase]
    diagnostics = {}
    if isinstance(exc, ZhipuError):
        from ..oauth.zhipu.diagnostics import error_facts
        from ..oauth_errors import describe_oauth_error
        diagnostics = error_facts(exc)
        message = describe_oauth_error(exc, provider="zhipu").reason
    stack = _stack(exc)
    print("[tg-error] " + json.dumps({"incident_id": incident, "operation": operation,
        "event_kind": event_kind, "update_id": update_id, "route": route,
        "state_action": state_action, "error_code": code, "http_status": status,
        "upstream_stage": stage, "upstream_kind": kind, "timeout_phase": timeout_phase,
        **diagnostics, "exceptions": stack}, ensure_ascii=False), flush=True)
    return (f"❌ <b>{ui.escape_html(operation)}失败</b>\n{ui.escape_html(message)}\n"
            f"故障编号：<code>{incident}</code>\n"
            "涉及消费或提交时，请先核对结果，不要直接重复操作。")
