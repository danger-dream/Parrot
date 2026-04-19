"""Anthropic 标准错误响应工具。

统一两种路径：
- 未发首包 → `json_error_response()`（HTTP 4xx/5xx + JSON body）
- 已发首包 → `sse_error_line()`（SSE 流内 error event）
"""

import json
from fastapi.responses import JSONResponse


class ErrType:
    INVALID_REQUEST = "invalid_request_error"
    AUTH = "authentication_error"
    PERMISSION = "permission_error"
    NOT_FOUND = "not_found_error"
    REQUEST_TOO_LARGE = "request_too_large"
    RATE_LIMIT = "rate_limit_error"
    TIMEOUT = "timeout_error"
    API = "api_error"
    OVERLOADED = "overloaded_error"


def build_error_payload(err_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": err_type, "message": message}}


def json_error_response(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=build_error_payload(err_type, message),
    )


def sse_error_line(err_type: str, message: str) -> bytes:
    """生成一条符合 Anthropic SSE 规范的 error event（含结尾空行）。"""
    payload = json.dumps(build_error_payload(err_type, message), ensure_ascii=False)
    return f"event: error\ndata: {payload}\n\n".encode("utf-8")


def classify_http_status(status: int) -> str:
    """把上游 HTTP 状态码归类到 Anthropic 错误类型。"""
    if status == 400:
        return ErrType.INVALID_REQUEST
    if status == 401:
        return ErrType.AUTH
    if status == 403:
        return ErrType.PERMISSION
    if status == 404:
        return ErrType.NOT_FOUND
    if status == 408:
        return ErrType.TIMEOUT
    if status == 413:
        return ErrType.REQUEST_TOO_LARGE
    if status == 429:
        return ErrType.RATE_LIMIT
    if status == 504:
        return ErrType.TIMEOUT
    if status == 529:
        return ErrType.OVERLOADED
    if status >= 500:
        return ErrType.API
    if status >= 400:
        return ErrType.INVALID_REQUEST
    return ErrType.API
