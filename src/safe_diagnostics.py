"""Bounded failure facts for management/OAuth, never exception text or IO payloads.

This module observes failures only. It does not retry, resolve routes, read a
response stream, or change auth/retry/public error codes. Diagnostic context is
request-local; exceptions retain only a safe projection across lossy wrappers.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import inspect
import json
import logging
import re
import socket
import ssl
import sys
from pathlib import Path

import httpcore
import httpx

_logger = logging.getLogger(__name__)
_context = ContextVar("safe_failure_context", default=("management", "operation", "execute", ""))
_collector = ContextVar("safe_failure_collector", default=None)
_PROVIDERS = {"management", "claude", "anthropic", "openai", "xai", "cursor", "antigravity", "workbuddy", "zhipu",
              "channels", "models", "proxy", "search", "mcp", "translation", "status", "updates", "telegram"}
_LABEL = re.compile(r"[a-z][a-z0-9_.]{0,79}\Z")
# Trusted call-site labels are registered by decorators/scopes, not read from IO.
_STAGES = {
    "execute": "执行", "profile": "登录参数校验", "login_init": "登录初始化", "discovery": "授权发现",
    "state_request": "登录初始化请求", "state_validation": "授权状态校验",
    "authorization_url_validation": "授权地址校验", "token_poll": "登录轮询", "token_exchange": "授权码交换",
    "token_refresh": "凭据刷新", "profile_fetch": "账户资料查询", "project_lookup": "组织/项目查询",
    "projects": "组织/项目查询", "subscription": "订阅查询", "key_list": "已有 Key 查询",
    "key_copy": "模型 Key 读取", "key_create": "模型 Key 创建", "key_save": "模型 Key 保存",
    "reset_status": "重置卡查询", "reset_use": "使用重置卡", "reset_opportunity": "领取重置卡",
    "quota": "额度查询", "mcp": "MCP 用量查询", "request": "上游请求", "response": "响应解析",
    "import_parse": "导入文件解析", "import_validate": "导入凭据校验", "account_save": "账户保存",
    "callback_parse": "授权回调校验", "model_discovery": "模型发现", "usage": "用量查询",
    "refresh": "凭据刷新", "poll": "登录轮询", "login": "登录初始化", "account": "账户资料查询",
    "request_build": "请求构造", "client_build": "传输客户端构造", "route_resolve": "代理路由选择",
    "model_sync": "模型同步", "connectivity_test": "代理连接测试", "history_read": "历史日志读取",
    "refresh_token": "凭据刷新", "fetch_usage": "用量查询", "exchange_code": "授权码交换",
    "rate_limit_reset_credit": "使用重置额度", "on_delete_exec": "删除账户",
    "on_edit_max_concurrent_input": "更新并发限制",
}
_CODES = frozenset({11217, 12153, 11128, "invalid_grant", "invalid_token", "invalid_client", "invalid_request",
                    "invalid_scope", "unauthorized_client", "access_denied", "authorization_pending", "slow_down",
                    "expired_token", "unsupported_grant_type", "temporarily_unavailable", "server_error",
                    "authentication_error", "rate_limit_exceeded", "profile_invalid", "timeout"})
_GENERIC_CODES = {"UPSTREAM_ERROR", "UPSTREAM_TIMEOUT", "DEPENDENCY_UNAVAILABLE", "SERVICE_NOT_READY"}
_KIND_TEXT = {
    "tls_certificate": "TLS 证书校验失败", "tls": "TLS 连接失败", "dns": "DNS 解析失败",
    "connect_timeout": "连接超时", "read_timeout": "等待响应超时", "write_timeout": "发送请求超时",
    "pool_timeout": "等待连接池超时", "timeout": "操作超时", "connection_refused": "连接被拒绝",
    "connection_reset": "连接被重置", "proxy": "代理连接失败", "proxy_route": "代理路由或连接器失败",
    "connect": "连接建立失败", "read": "响应读取失败", "write": "请求发送失败",
    "protocol": "响应协议错误", "transport": "网络传输失败", "os_error": "本地系统或 IO 失败",
    "invalid_json": "响应不是有效 JSON", "invalid_envelope": "响应结构不符合约定",
    "invalid_data": "响应数据不符合约定", "too_large": "响应超过大小限制",
    "validation": "数据校验失败", "disabled": "保护设置禁止上游请求", "http": "上游 HTTP 拒绝",
    "business": "上游业务拒绝", "unexpected": "未分类异常，具体原因尚不确定",
}


@dataclass
class Diagnostic:
    provider: str
    operation: str
    stage: str
    kind: str
    exception_type: str
    http_status: int = 0
    business_code: int | str = "absent"
    request_state: str = "unknown"
    outcome: str = "unknown"
    realm: str = ""
    selected_target: str = "inherit"
    locations: tuple = ()
    reported: bool = field(default=False, repr=False, compare=False)

    def fields(self):
        return {key: getattr(self, key) for key in (
            "provider", "operation", "stage", "kind", "exception_type", "http_status",
            "business_code", "request_state", "outcome", "realm", "selected_target", "locations")}


def _label(value, fallback):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else fallback


@contextmanager
def scope(*, provider="", operation="", stage="", realm=""):
    old = _context.get()
    token = _context.set((provider if isinstance(provider, str) and provider in _PROVIDERS else old[0],
                          _label(operation, old[1]), _label(stage, old[2]),
                          realm if isinstance(realm, str) and realm in {"cn", "global", "bigmodel", "zai"} else old[3]))
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def collect_failures():
    """Keep only safe facts when an existing fallback returns a result, not an exception."""
    parent = _collector.get()
    failures = []
    token = _collector.set(failures)
    try:
        yield failures
    finally:
        _collector.reset(token)
        if parent is not None:
            parent.extend(failures)
            del parent[:-16]


def _chain(exc):
    values, seen = [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(values) < 12:
        seen.add(id(exc)); values.append(exc)
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return values


def transport_diagnostic(exc):
    # Use canonical class labels, not attacker-controlled class names/messages.
    from .proxy.connector import ProxyConnectError
    types = (
        (ssl.SSLCertVerificationError, "tls_certificate", "SSLCertVerificationError"),
        (ssl.SSLError, "tls", "SSLError"), (socket.gaierror, "dns", "gaierror"),
        ((httpx.ConnectTimeout, httpcore.ConnectTimeout), "connect_timeout", "ConnectTimeout"),
        ((httpx.ReadTimeout, httpcore.ReadTimeout), "read_timeout", "ReadTimeout"),
        ((httpx.WriteTimeout, httpcore.WriteTimeout), "write_timeout", "WriteTimeout"),
        ((httpx.PoolTimeout, httpcore.PoolTimeout), "pool_timeout", "PoolTimeout"),
        (TimeoutError, "timeout", "TimeoutError"),
        (ConnectionRefusedError, "connection_refused", "ConnectionRefusedError"),
        (ConnectionResetError, "connection_reset", "ConnectionResetError"),
        ((httpx.ProxyError, httpcore.ProxyError), "proxy", "ProxyError"),
        ((httpx.ConnectError, httpcore.ConnectError), "connect", "ConnectError"),
        ((httpx.ReadError, httpcore.ReadError), "read", "ReadError"),
        ((httpx.WriteError, httpcore.WriteError), "write", "WriteError"),
        ((httpx.ProtocolError, httpcore.ProtocolError), "protocol", "ProtocolError"),
        (httpx.TransportError, "transport", "TransportError"),
        (ProxyConnectError, "proxy_route", "ProxyConnectError"),
        (httpx.HTTPStatusError, "http", "HTTPStatusError"),
        (OSError, "os_error", "OSError"), (json.JSONDecodeError, "invalid_json", "JSONDecodeError"),
        (UnicodeError, "invalid_data", "UnicodeError"),
        (ValueError, "validation", "ValueError"), (KeyError, "invalid_data", "KeyError"),
        (TypeError, "invalid_data", "TypeError"), (RuntimeError, "unexpected", "RuntimeError"),
    )
    values = _chain(exc)
    for cls, kind, name in types:
        if any(isinstance(item, cls) for item in values):
            return kind, name
    return "unexpected", "Exception"


def safe_code(value):
    if type(value) not in {str, int}:
        return "unrecognized"
    return value if value in _CODES else "unrecognized"


def _response_code(response):
    # Inspect only already-buffered JSON, never consume an HTTP stream for logs.
    content = getattr(response, "_content", None)
    if not isinstance(content, bytes) or len(content) > 65536:
        return "absent"
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        return "absent"
    if not isinstance(value, dict):
        return "absent"
    error = value.get("error")
    if isinstance(error, dict):
        value = error
    elif type(error) in {str, int}:
        return safe_code(error)
    for key in ("code", "error_code", "type"):
        if key in value:
            return safe_code(value[key])
    return "absent"


def find(exc):
    for item in _chain(exc):
        value = getattr(item, "_safe_diagnostic", None)
        if isinstance(value, Diagnostic):
            return value
    return None


def capture(exc, *, provider="", operation="", stage=""):
    existing = find(exc)
    with scope(provider=provider, operation=operation, stage=stage):
        prov, op, step, realm = _context.get()
    kind, name = transport_diagnostic(exc)
    result = existing or Diagnostic(prov, op, step, kind, name, realm=realm)
    if result.provider == "management" and prov != "management":
        result.provider = prov
    if result.operation == "operation":
        result.operation = op
    if result.stage == "execute":
        result.stage = step
    if not result.realm:
        result.realm = realm
    if not result.locations:
        root = str(Path(__file__).parent) + "/"
        locations = []
        for error in _chain(exc):
            tb = error.__traceback__
            while tb is not None:
                filename = tb.tb_frame.f_code.co_filename
                if filename.startswith(root) and filename.endswith(".py") and not filename.endswith("safe_diagnostics.py"):
                    relative = filename[len(root):]
                    if re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", relative):
                        locations.append((relative, tb.tb_lineno))
                tb = tb.tb_next
        result.locations = tuple(locations[-6:])
    if existing is None:
        # Only a configured, bounded name; never connector URLs, addresses or keys.
        from .oauth.add_context import current
        attempt = current()
        if attempt is not None and attempt.target is not None:
            from . import config
            target = attempt.target
            net = config.get().get("network") or {}
            result.selected_target = target if (isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", target)
                and (target == "direct" or target in (net.get("proxies") or {}) or target in (net.get("groups") or {}))) else "unavailable"
    # Domain errors already hold controlled facts, but validate every emitted value.
    for item in _chain(exc):
        code = getattr(item, "code", None)
        native_kind = getattr(item, "kind", None)
        if isinstance(native_kind, str) and native_kind in {"invalid_json", "invalid_envelope", "invalid_data", "too_large", "disabled"}:
            result.kind = native_kind
        if native_kind == "timeout" and result.kind == "unexpected":
            result.kind = "timeout"
        response = item.response if isinstance(item, httpx.HTTPStatusError) else None
        status = response.status_code if response is not None else getattr(item, "response_status", getattr(item, "status_code", 0))
        if type(status) is int and 100 <= status <= 599:
            result.http_status = status
            if response is not None or getattr(item, "response_status", 0):
                result.request_state = "response_received"
            if response is not None:
                result.business_code = _response_code(response)
            if status >= 300 and result.kind in {"unexpected", "validation"}:
                result.kind = "http"
        if code is not None and type(code) in {str, int} and result.business_code in {"absent", "unrecognized"}:
            result.business_code = safe_code(code)
        if isinstance(native_kind, str) and native_kind in {"business", "upstream"} and (not result.http_status or result.http_status < 300):
            result.kind = "business"
        if getattr(item, "request_not_sent", False) is True:
            result.request_state = "not_sent"
        native_stage = getattr(item, "stage", None)
        if isinstance(native_stage, str) and native_stage in _STAGES and result.stage in {"execute", "request", "response"}:
            result.stage = native_stage
    try:
        exc._safe_diagnostic = result
    except (AttributeError, TypeError):
        pass
    return result


def inherit(error, cause=None):
    """Carry safe facts at a wrapping site before `raise ... from None` loses them."""
    cause = cause if cause is not None else sys.exception()
    if cause is not None and cause is not error:
        try:
            error._safe_diagnostic = capture(cause)
        except Exception:
            pass  # Metadata extraction must not replace the domain exception.
    return error


def report(exc, **context):
    try:
        diagnostic = capture(exc, **context)
    except Exception:
        prov, op, stage, realm = _context.get()
        diagnostic = Diagnostic(prov, op, stage, "unexpected", "Exception", realm=realm)
    failures = _collector.get()
    if failures is not None and all(value is not diagnostic for value in failures):
        failures.append(diagnostic)
        del failures[:-16]
    if not diagnostic.reported:
        diagnostic.reported = True
        try:
            _logger.warning("management_failure %s", json.dumps(diagnostic.fields(), sort_keys=True))
        except Exception:
            pass  # Logging never changes business behavior.
    return diagnostic


def message(diagnostic):
    label = diagnostic.operation if diagnostic.stage == "execute" else diagnostic.stage
    stage = _STAGES.get(label, label)
    reason = _KIND_TEXT.get(diagnostic.kind, _KIND_TEXT["unexpected"])
    details = f" HTTP {diagnostic.http_status}" if diagnostic.http_status else ""
    if diagnostic.business_code not in {"absent", "unrecognized"}:
        details += f"，业务码 {diagnostic.business_code}"
    return f"{diagnostic.provider} / {stage}：{reason}{details}。"


def guidance(diagnostic):
    if diagnostic.kind in {"tls_certificate", "tls", "dns", "connect", "proxy", "proxy_route", "connect_timeout", "connection_refused", "connection_reset"}:
        action = "请检查所选代理、DNS 或 TLS 配置。"
    elif diagnostic.kind in {"invalid_json", "invalid_envelope", "invalid_data", "too_large"}:
        action = "请核对上游响应与接口兼容性。"
    elif diagnostic.kind in {"http", "business"}:
        action = "请核对上游服务状态、授权或接口限制。"
    elif diagnostic.kind == "validation":
        action = "请核对输入和授权回调；勿发送凭据到日志。"
    elif diagnostic.kind == "disabled":
        action = "请核对当前保护设置。"
    else:
        action = "请按时间核对服务器诊断及操作记录。"
    return action + "远端操作结果未确认时，不要盲目重复提交。"


def summary(exc, **context):
    return message(report(exc, **context))


def diagnose(provider="", operation="", stage="", *, expected=(), emit=True):
    """Explicit provider/operation boundaries; no arguments/return values are logged."""
    def decorate(fn):
        def failed(exc):
            if expected and isinstance(exc, expected):
                return
            code = getattr(getattr(exc, "code", None), "value", "")
            if code and code not in _GENERIC_CODES:
                return  # permission/CAS/pending/confirmation outcomes are intentional
            try:
                (report if emit else capture)(exc)
            except Exception:
                pass  # Optional metadata must not replace an original failure.
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def async_run(*args, **kwargs):
                with scope(provider=provider, operation=operation, stage=stage, realm=kwargs.get("realm", kwargs.get("site", ""))):
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as exc:
                        failed(exc); raise
            return async_run
        @wraps(fn)
        def run(*args, **kwargs):
            with scope(provider=provider, operation=operation, stage=stage, realm=kwargs.get("realm", kwargs.get("site", ""))):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    failed(exc); raise
        return run
    return decorate
