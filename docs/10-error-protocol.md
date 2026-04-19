# 10 — 错误返回协议（Anthropic 标准）

所有错误路径统一按 Anthropic Messages API 规范返回，客户端无需分叉处理。

## 10.1 错误类型枚举

Anthropic 标准错误 type 值（`src/errors.py` 中作为常量）：

```python
ERR_INVALID_REQUEST    = "invalid_request_error"     # 400
ERR_AUTHENTICATION     = "authentication_error"      # 401
ERR_PERMISSION         = "permission_error"          # 403
ERR_NOT_FOUND          = "not_found_error"           # 404
ERR_REQUEST_TOO_LARGE  = "request_too_large"         # 413
ERR_RATE_LIMIT         = "rate_limit_error"          # 429
ERR_TIMEOUT            = "timeout_error"             # 408 / 504
ERR_API                = "api_error"                 # 500 / 502 / 503
ERR_OVERLOADED         = "overloaded_error"          # 529
```

内部触发条件 → 错误类型的映射：

| 触发条件 | HTTP status | err type | message 示例 |
|---|---|---|---|
| 下游 API Key 缺失/错误 | 401 | authentication_error | `Missing API key` / `Invalid API key` |
| 下游请求体非法 JSON | 400 | invalid_request_error | `invalid json: ...` |
| 下游 `model` 缺失 | 400 | invalid_request_error | `model is required` |
| 请求的 model 无任何渠道支持 | 503 | api_error | `No available channels for model: xxx` |
| 所有候选渠道都失败（未发首包） | 503 | api_error | `All upstream channels failed. Last error: xxx` |
| 连接超时 | 504 | timeout_error | `upstream connect timeout > 10s` |
| 首字超时（未发首包） | 504 | timeout_error | `upstream first byte timeout > 30s` |
| 连接失败（DNS/Refused） | 502 | api_error | `upstream connect failed: ...` |
| 传输错误（RemoteProtocolError 等，未发首包） | 502 | api_error | `upstream transport error: ...` |
| 上游返回 5xx 且所有候选耗尽 | 502 | api_error | `HTTP 5xx from xxx: ...` |
| OAuth token 不可刷新 | 502 | api_error | `oauth refresh failed: ...` |
| 转换异常（transform_request 抛错） | 400 | invalid_request_error | `transform error: ...` |
| 空闲超时（已发首包） | — | 流内 error event | `upstream idle timeout > 30s` |
| 总超时（已发首包） | — | 流内 error event | `upstream total timeout > 600s` |
| 流中断（已发首包） | — | 流内 error event | `stream error: ...` |

## 10.2 未发首包的错误（普通 HTTP 响应）

```python
def build_error_json(err_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": err_type, "message": message}}

def json_error_response(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=build_error_json(err_type, message),
        headers={"content-type": "application/json"},
    )
```

示例响应：
```http
HTTP/1.1 503 Service Unavailable
Content-Type: application/json

{"type":"error","error":{"type":"api_error","message":"All upstream channels failed"}}
```

## 10.3 已发首包的错误（流内收尾）

当代理已经向下游写过任何 SSE 字节（通常是 `message_start` 事件），不能再换 HTTP 状态，只能用流内 `error` event 收尾：

```python
def build_sse_error_line(err_type: str, message: str) -> bytes:
    payload = json.dumps({"type": "error", "error": {"type": err_type, "message": message}})
    return f"event: error\ndata: {payload}\n\n".encode("utf-8")
```

发送顺序（已发首包后出错）：
```
... 正常的 message_start / content_block_* / message_delta 事件 ...
event: error
data: {"type":"error","error":{"type":"api_error","message":"..."}}

（关闭连接）
```

**不**再发 `message_stop`。Anthropic 规范对 `error` event 后的流中止本身就是这样处理（见官方 stream 文档）。

## 10.4 errors.py 模块完整接口

```python
# src/errors.py

from fastapi.responses import JSONResponse
import json

# 常量
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
    payload = json.dumps(build_error_payload(err_type, message), ensure_ascii=False)
    return f"event: error\ndata: {payload}\n\n".encode("utf-8")

# 上游 HTTP 状态 → 错误类型
def classify_http_status(status: int) -> str:
    if status == 400: return ErrType.INVALID_REQUEST
    if status == 401: return ErrType.AUTH
    if status == 403: return ErrType.PERMISSION
    if status == 404: return ErrType.NOT_FOUND
    if status == 408: return ErrType.TIMEOUT
    if status == 413: return ErrType.REQUEST_TOO_LARGE
    if status == 429: return ErrType.RATE_LIMIT
    if status == 504: return ErrType.TIMEOUT
    if status == 529: return ErrType.OVERLOADED
    if status >= 500: return ErrType.API
    if status >= 400: return ErrType.INVALID_REQUEST
    return ErrType.API
```

## 10.5 特殊场景：上游返回 200 + error JSON

当上游 HTTP 200 但 body 含 `{"type":"error",...}`（某些云厂商的坏习惯）：

- 未发首包（首包判定阶段）：视为上游错误，走故障转移；最终若所有候选都这样失败，返回 502 `api_error`。
- 已发首包（不可能走到这里，因为首包判定失败会阻止 writeHead）：不适用。

## 10.6 上游返回错误但首包已发

流式上游在发了若干 event 后，中途发出 `event: error` → 代理透传该 event 给下游，然后关闭连接。

实现上 `_consume_stream` 的 "持续转发" 循环里无需特殊处理——`_restore_tool_names_in_chunk` 后原样写给下游即可（Anthropic 标准 error event）。

## 10.7 客户端取消连接

若下游（FastAPI 侧）检测到客户端断开（`request.is_disconnected()` 或 Starlette 的 connection close），应：
1. 中止上游的 stream（`await upstream_resp.aclose()`）
2. 写入 DB：`finish_error(..., error_message="client disconnected")`

不需要给客户端发任何东西（它已经不听了）。

## 10.8 特殊：OAuth 401/403 单次自动刷新

OAuth 渠道返回 401/403 时：
1. 首次发生 → `force_refresh(email)` → 重新发起请求（计入 retry_chain，但**不记入 cooldown**）
2. 刷新后仍 401/403 → 认为 refresh_token 已失效，标记 `disabled_reason = "auth_error"`，本次请求走下一个候选
3. 管理员在 TG Bot 中通过"重新登录"或"手动设置 OAuth JSON"恢复

记录：
- retry_chain 中 outcome = `http_auth_error`
- 若第二次仍失败，单独记录 `auth_error_persistent`

## 10.9 下游客户端的错误处理契约

客户端应能处理以下两种情况，逻辑与官方 Anthropic API 一致：

1. **HTTP 错误响应**（未发首包）：
   - 读取 response.json()
   - 检查 `body["type"] == "error"` 和 `body["error"]["type"]`
   - 按标准错误类型处理（重试 rate_limit、报错 authentication 等）

2. **SSE 中途 error event**（已发首包）：
   - 在解析 SSE 时识别 `event: error` 行
   - 此时之前已接收的部分内容可以保留（如已显示给用户的文本）
   - 报错并停止解析

Anthropic 官方 Python SDK 与 Claude Code CLI 都正确实现这两种处理。代理保证行为对齐，客户端无需改动。
