# 07 — Anthropic 侧改动清单

**本章覆盖所有在 `src/` 根目录下（非 `src/openai/`）文件里的改动。全部是"纯追加"或"带默认值的签名扩展"，不会改变 anthropic 既有行为。**

下方每一项都标明：
- 改动形式（ADD / EXTEND / NOOP）
- 对 anthropic 调用路径的影响（必须证明：等价）
- 预估行数

## 7.1 `src/config.py` —— ADD 默认值

**位置**：`DEFAULT_CONFIG` 字典

**内容**：追加

```python
DEFAULT_CONFIG["openai"] = {
    "store": {"enabled": True, "ttlMinutes": 60, "cleanupIntervalSeconds": 300},
    "reasoningBridge": "passthrough",
    "translation": {"enabled": True, "rejectOnBuiltinTools": True, "rejectOnMultiCandidate": True},
}
```

**对 anthropic 影响**：无（没有 anthropic 代码读 `openai.*` 字段）。

**行数**：~10

---

## 7.2 `src/auth.py` —— ADD 辅助函数

**改动**：追加一个模块函数。**不改 `validate()` 签名**。

```python
def get_allowed_protocols(key_name: str) -> list[str]:
    """返回该 Key 的 allowedProtocols 列表；空 = 无限制。"""
    if not key_name: return []
    cfg = config.get()
    entry = (cfg.get("apiKeys") or {}).get(key_name) or {}
    return list(entry.get("allowedProtocols") or [])
```

**对 anthropic 影响**：无（新函数，anthropic 路径不调用）。

**行数**：~10

---

## 7.3 `src/errors.py` —— ADD OpenAI 错误格式函数

**改动**：追加函数，不改既有 `json_error_response` / `sse_error_line` / `classify_http_status`。

```python
# 新增
def json_error_openai(status: int, err_type: str, message: str, *, param: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": None, "param": param}},
    )

def sse_error_line_chat(err_type: str, message: str) -> bytes:
    return (f"data: " + json.dumps({"error":{"message":message,"type":err_type}}, ensure_ascii=False) + "\n\n").encode("utf-8")

def sse_error_line_responses(err_type: str, message: str) -> bytes:
    payload = {"type":"error","code":None,"message":message,"param":None,"sequence_number":0}
    return (f"event: error\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

# 可选：为两个家族的 ErrType 对齐
class ErrTypeOpenAI:
    INVALID_REQUEST = "invalid_request_error"
    AUTH = "authentication_error"
    PERMISSION = "permission_error"
    NOT_FOUND = "not_found_error"
    RATE_LIMIT = "rate_limit_exceeded"
    SERVER = "server_error"
    # 可按需补
```

**对 anthropic 影响**：无。

**行数**：~50

---

## 7.4 `src/fingerprint.py` —— ADD 两套归一化

**改动**：在现有 `_make_hash` 等公共 helper 之外，追加：

```python
def fingerprint_query_chat(api_key_name, client_ip, messages: list) -> str | None:
    if not messages or len(messages) < 3: return None
    last_two = messages[:-1][-2:]
    return _make_hash_canon("openai", api_key_name, client_ip, last_two[0], last_two[1],
                            canon=_canon_chat)

def fingerprint_write_chat(api_key_name, client_ip, messages, assistant_msg) -> str | None:
    full = list(messages) + [assistant_msg]
    if len(full) < 2: return None
    last_two = full[-2:]
    return _make_hash_canon("openai", api_key_name, client_ip, last_two[0], last_two[1],
                            canon=_canon_chat)

def fingerprint_query_responses(api_key_name, client_ip, input_items: list) -> str | None:
    # 过滤掉 reasoning / 内置工具 item（它们在历史里不稳定）
    rel = [it for it in input_items if it.get("type") in ("message","function_call","function_call_output")]
    if len(rel) < 3: return None
    last_two = rel[:-1][-2:]
    return _make_hash_canon("openai", api_key_name, client_ip, last_two[0], last_two[1],
                            canon=_canon_responses)

def fingerprint_write_responses(api_key_name, client_ip, input_items, assistant_items) -> str | None:
    rel = [it for it in input_items if it.get("type") in ("message","function_call","function_call_output")]
    for it in assistant_items:
        if it.get("type") in ("message","function_call"): rel.append(it)
    if len(rel) < 2: return None
    last_two = rel[-2:]
    return _make_hash_canon("openai", api_key_name, client_ip, last_two[0], last_two[1],
                            canon=_canon_responses)

def _make_hash_canon(ns, key, ip, a, b, canon) -> str:
    raw = f"{ns}|{key or ''}|{ip or ''}|{canon(a)}|{canon(b)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

def _canon_chat(msg): ...         # openai chat message 归一化
def _canon_responses(item): ...    # responses input item 归一化
```

命名空间前缀 `"openai|"` 保证 openai 和 anthropic 的 fp 字符串永远不碰撞（即使落到同一个 affinity 表里）。

**对 anthropic 影响**：无（仅追加符号，不改既有 `fingerprint_query` / `fingerprint_write` / `_canon`）。

**行数**：~160

---

## 7.5 `src/upstream.py` —— ADD OpenAI SSE 工具

**改动**：追加类和函数，不改既有 `SSEUsageTracker` / `SSEAssistantBuilder` / `parse_first_sse_event`。

```python
# 追加
class ChatSSEUsageTracker: ...         # 累积 [DONE] 前的 usage chunk
class ChatSSEAssistantBuilder: ...     # 累积 delta.content + tool_calls → assistant message
def parse_first_chat_sse_event(chunk: bytes) -> dict | None: ...

class ResponsesSSEUsageTracker: ...    # 从 response.completed 抽 usage
class ResponsesSSEAssistantBuilder: ...# 累积 output_item 事件还原 output[]
def parse_first_responses_sse_event(chunk: bytes) -> dict | None: ...

def extract_usage_chat_json(obj) -> dict: ...
def extract_usage_responses_json(obj) -> dict: ...
```

**对 anthropic 影响**：无。

**行数**：~400

---

## 7.6 `src/scheduler.py` —— EXTEND 带默认参数

**改动**：`schedule()` 签名加 `ingress_protocol` 默认 `"anthropic"`；`_filter_candidates` 做 family 筛选。

```python
def schedule(body, api_key_name, client_ip, ingress_protocol: str = "anthropic") -> ScheduleResult:
    candidates = _filter_candidates(body.get("model"), ingress_protocol)
    ...

def _filter_candidates(requested_model: str, ingress_protocol: str = "anthropic"):
    ingress_family = _family(ingress_protocol)
    for ch in registry.all_channels():
        if not ch.enabled: continue
        if ch.disabled_reason: continue
        ch_protocol = getattr(ch, "protocol", "anthropic")
        if _family(ch_protocol) != ingress_family:
            continue
        resolved = ch.supports_model(requested_model)
        if resolved is None: continue
        if cooldown.is_blocked(ch.key, resolved): continue
        out.append((ch, resolved))
    return out

def _family(proto: str) -> str:
    return "anthropic" if proto in ("anthropic",) else "openai"
```

**对 anthropic 影响的证明**：
- 现有调用 `scheduler.schedule(body, key, ip)` 默认 `ingress_protocol="anthropic"` → `_family() == "anthropic"`
- 现有 anthropic 渠道（OAuth / Api 默认 protocol）→ `_family(ch.protocol) == "anthropic"`，不会被排除
- 假设此前无 openai 家族渠道（未启用此功能时用户不会配），`_filter_candidates` 结果与旧版完全一致
- 即使后续用户加了 openai 渠道，它们在 anthropic 入口调度时被新增的 family 过滤排除——但它们本来 `supports_model("claude-*")` 也返回 None，也会被排除。两种机制叠加，语义等价
- 现有所有 `test_m*` 的 scheduler 相关测试应一次通过

**行数**：~25

---

## 7.7 `src/failover.py` —— EXTEND 带默认参数

**改动量最大**。需要三类修改：

### 7.7.1 签名扩展

```python
async def run_failover(schedule_result, body, request_id, api_key_name, client_ip,
                       is_stream, start_time, ingress_protocol: str = "anthropic") -> Response:
    ...
```

### 7.7.2 SSE toolkit 分派

现在代码里硬引 `upstream.SSEUsageTracker` / `SSEAssistantBuilder` / `parse_first_sse_event`。改为通过 toolkit 字典：

```python
# failover.py 文件顶部
_SSE_TOOLKIT = {
    "anthropic":        (upstream.SSEUsageTracker,         upstream.SSEAssistantBuilder,         upstream.parse_first_sse_event),
    "openai-chat":      (upstream.ChatSSEUsageTracker,     upstream.ChatSSEAssistantBuilder,     upstream.parse_first_chat_sse_event),
    "openai-responses": (upstream.ResponsesSSEUsageTracker,upstream.ResponsesSSEAssistantBuilder,upstream.parse_first_responses_sse_event),
}

def _get_toolkit(ch):
    return _SSE_TOOLKIT.get(getattr(ch, "protocol", "anthropic"), _SSE_TOOLKIT["anthropic"])
```

在 `_consume_stream` / `_consume_non_stream` 原本 `SSEUsageTracker()` 的地方改成：

```python
tracker_cls, builder_cls, first_parser = _get_toolkit(ch)
tracker = tracker_cls()
builder = builder_cls()
```

**anthropic 等价性**：`ch.protocol` 默认 `"anthropic"`，查表得到原来的三件套。行为完全一致。

### 7.7.3 错误协议选择

原代码在流内错误时：
```python
yield errors.sse_error_line(errors.ErrType.API, "...")
```

改为：
```python
yield _sse_error_for_ingress(ingress_protocol, errors.ErrType.API, "...")

def _sse_error_for_ingress(ingress, err_type, msg):
    if ingress == "anthropic": return errors.sse_error_line(err_type, msg)
    if ingress == "chat":      return errors.sse_error_line_chat(err_type, msg)
    return errors.sse_error_line_responses(err_type, msg)
```

非流式错误路径类似：`_json_error_for_ingress`。

**anthropic 等价性**：anthropic 路径 `ingress_protocol="anthropic"` → 回到原 `errors.sse_error_line` 调用。

### 7.7.4 流翻译器接入点

在 `_try_channel` 里：

```python
ch_protocol = getattr(ch, "protocol", "anthropic")
stream_translator = None
if ingress_protocol in ("chat","responses") and ch_protocol != _ingress_native(ingress_protocol):
    from .openai.transform import stream_c2r, stream_r2c
    if ingress_protocol == "chat" and ch_protocol == "openai-responses":
        stream_translator = stream_r2c.StreamTranslator(...)   # wraps tracker/builder
    elif ingress_protocol == "responses" and ch_protocol == "openai-chat":
        stream_translator = stream_c2r.StreamTranslator(...)
```

非流式类似：在 `_consume_non_stream` 返回前若需要翻译则过 `translate_response(...)`。

Stream translator 的接入点非常隔离：只在 `_consume_stream` 的 `yield chunk` 处包一层。anthropic 路径 `ingress_protocol="anthropic"` 永远不会进入这些分支。

### 7.7.5 请求体翻译时机

`ch.build_upstream_request(body, model)` 现在的签名不变（只加一个可选 kw-only 参数）：

```python
async def build_upstream_request(self, body, model, *, ingress_protocol: str = "anthropic") -> UpstreamRequest: ...
```

anthropic `ApiChannel.build_upstream_request` / `OAuthChannel.build_upstream_request` 的函数体**完全不改**——它们多收一个 kwarg 且忽略即可（Python 的 `**kwargs` 可选语法处理）。或者加一行 `_ = ingress_protocol` 显式吞掉。

最干净的做法：在 `Channel` 基类里给 `build_upstream_request` 加 `*, ingress_protocol="anthropic"`（`channel/base.py` 的抽象签名），两个 anthropic 子类忽略此参数；`OpenAIApiChannel` 使用此参数。

### 行数估计

- 7.7.1 签名：2 行
- 7.7.2 toolkit 替换（~6 处）：~20 行
- 7.7.3 错误格式替换（~5 处）：~20 行
- 7.7.4 stream_translator 接入：~30 行
- 7.7.5 build_upstream_request 透传 ingress：~5 行

**合计 ~80 行改动（纯扩展）**。

### 回归测试

- 现有 `test_m4_failover.py` / `test_m5.py` 等必须全绿
- 新增 `tests/test_openai_failover.py` 覆盖 family 分派、translator 接入、错误格式分派

---

## 7.8 `src/probe.py` —— EXTEND 按 protocol 分派

当前 probe 直接发 `/v1/messages` payload。改：

```python
def _probe_payload_for(ch) -> tuple[str, dict]:
    proto = getattr(ch, "protocol", "anthropic")
    if proto == "anthropic":
        return "/v1/messages", {"model":"...", "messages":[{"role":"user","content":"1+1=?"}], "max_tokens":50}
    if proto == "openai-chat":
        return "/v1/chat/completions", {"model":"...", "messages":[{"role":"user","content":"1+1=?"}], "max_tokens":50}
    if proto == "openai-responses":
        return "/v1/responses", {"model":"...", "input":"1+1=?", "max_output_tokens":50}
```

既有函数 `probe_channel` / `recovery_loop` 内部调 `_probe_payload_for(ch)` 选择 path + body。

**anthropic 等价性**：默认 `"anthropic"` → 原行为。

**行数**：~40

---

## 7.9 `src/channel/base.py` —— ADD 字段默认值

**改动**：`Channel` 类增加类级属性 `protocol: str = "anthropic"`。不改构造函数。

```python
class Channel(ABC):
    key: str
    type: str
    protocol: str = "anthropic"   # ★ 新增
    display_name: str
    ...
```

以及 `build_upstream_request` 抽象签名加 `*, ingress_protocol: str = "anthropic"`：

```python
@abstractmethod
async def build_upstream_request(self, requested_body, resolved_model, *, ingress_protocol: str = "anthropic") -> UpstreamRequest: ...
```

**anthropic 等价性**：`OAuthChannel` / `ApiChannel` 现有实现无 `self.protocol` 赋值 → 取类属性 `"anthropic"`。行为不变。子类实现 `build_upstream_request` 可接受并忽略 `ingress_protocol`。

**行数**：~5

---

## 7.10 `src/channel/api_channel.py` —— EXTEND 读 protocol

**改动**：`ApiChannel.__init__` 多读一个字段，但做 assert：

```python
def __init__(self, entry: dict):
    ...
    self.protocol = entry.get("protocol", "anthropic")
    # 防御：ApiChannel 只处理 anthropic；openai-* 会走 OpenAIApiChannel
    assert self.protocol == "anthropic", \
        f"ApiChannel expects anthropic protocol, got {self.protocol}"
```

**anthropic 等价性**：新增字段读取 + assert（对 protocol=="anthropic" 的旧配零影响）；assert 永远不会触发，因为 registry factory 分派后只有 anthropic 的渠道进 ApiChannel。

`build_upstream_request` 签名加 `*, ingress_protocol="anthropic"` 并忽略。

**行数**：~5

---

## 7.11 `src/channel/oauth_channel.py` —— EXTEND 签名兼容

```python
class OAuthChannel(Channel):
    protocol = "anthropic"   # 硬编码（覆盖基类默认，保证显式）
    ...
    async def build_upstream_request(self, body, model, *, ingress_protocol: str = "anthropic"):
        # ignore ingress_protocol
        ...
```

**anthropic 等价性**：完全等价（protocol 就是 "anthropic"，ingress_protocol 也是 "anthropic"）。

**行数**：~3

---

## 7.12 `src/channel/registry.py` —— ADD factory 扩展点

**改动**：

```python
# 追加
_channel_factories: dict[str, type[Channel]] = {}

def register_channel_factory(protocol: str, cls: type[Channel]) -> None:
    _channel_factories[protocol] = cls

# rebuild_from_config 里的 ApiChannel(entry) 改成：
for entry in cfg.get("channels", []):
    proto = entry.get("protocol", "anthropic")
    cls = _channel_factories.get(proto, ApiChannel)   # 没注册的 protocol 回落到 ApiChannel（anthropic）
    try:
        ch = cls(entry)
        new[ch.key] = ch
    except Exception as exc:
        print(f"[registry] skip invalid channel ({proto}): {exc}")
```

**anthropic 等价性**：`_channel_factories` 初始空，所有 `entry.protocol`（缺省 `"anthropic"`）都走 fallback `ApiChannel`。行为与现状一致。

OAuth 账户处理逻辑（`for acc in cfg.get("oauthAccounts", [])`）不变。

**行数**：~10

---

## 7.13 `src/server.py` —— ADD 两个路由 + openai lifespan 挂接

**改动 A：两个新路由**（不改 `proxy_messages` / `health` / `list_models`）：

```python
@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    from src.openai.handler import handle
    return await handle(request, ingress_protocol="chat")

@app.post("/v1/responses")
async def proxy_responses(request: Request):
    from src.openai.handler import handle
    return await handle(request, ingress_protocol="responses")
```

**改动 B：`list_models` 按 allowedProtocols 过滤家族**（EXTEND）

```python
@app.get("/v1/models")
async def list_models(request: Request):
    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return errors.json_error_response(401, errors.ErrType.AUTH, err)

    # ★ 新增：按 Key 的 allowedProtocols 推断家族
    from src.auth import get_allowed_protocols
    allowed_protos = get_allowed_protocols(key_name)
    families = {_family(p) for p in allowed_protos} if allowed_protos else {"anthropic","openai"}

    all_models = _available_models_for_families(families)
    ...
```

`_available_models_for_families` 是 `registry.available_models` 的家族过滤版本（可放在 registry 模块，亦是一处 ADD）。

**anthropic 等价性证明**：
- 对未设 `allowedProtocols` 的 Key（现状绝大多数），`allowed_protos` 为空 → `families = {"anthropic","openai"}` → `_available_models_for_families` 返回所有家族的模型 → 与 `available_models()` 相同（因为此前系统里只有 anthropic 家族的渠道）
- 对设了 `allowedProtocols=["anthropic"]` 的 Key，families={"anthropic"}，只返回 anthropic 家族——符合预期
- 既有测试 `test_m*_list_models` 若断言"返回所有渠道模型"且未设 allowedProtocols，仍然通过

**改动 C：Store 后台清理任务挂接**

```python
# lifespan 里追加：
from src.openai import store as openai_store
openai_store.init()
_background_tasks.append(asyncio.create_task(openai_store.cleanup_loop()))
```

**anthropic 等价性**：新增后台任务，不影响原 6 个任务。

**改动 D：OpenAI registry factory 注册**

```python
# lifespan 的 `registry.rebuild_from_config()` 之前：
from src.openai.channel.registration import register_factories
register_factories()
```

注意 `register_factories` 只往 `_channel_factories` 里插两条，不触发重建。

**anthropic 等价性**：新配置字段 `protocol` 才会走不同 factory；老配置缺省 `"anthropic"` 走 ApiChannel（现状）。

**行数合计**：~50

---

## 7.14 `src/telegram/menus/channel_menu.py` —— ADD 向导 / 编辑项

**改动**：

1. **添加向导**：在"输入 baseUrl"之后、"输入 apiKey"之前插入一步"选择协议"（3 按钮 anthropic / openai-chat / openai-responses，默认 anthropic，一键下一步）
2. **编辑面板**：在现有按钮列后加一个 `🔁 切换协议` 按钮，点击后弹 3 选 1 键盘
3. **详情页展示**：在现有信息行追加 `协议：openai-chat`（仅非 anthropic 时显示，避免信息噪音）
4. **测试按钮**：调的是 `probe.probe_channel`（已在 7.8 按 protocol 分派），菜单侧无感知

**anthropic 等价性**：老渠道 `protocol` 缺省 anthropic，详情页不额外显示行，编辑面板新按钮使用者可不点。

**行数**：~100

---

## 7.15 `src/telegram/menus/apikey_menu.py` —— ADD 按钮

**改动**：Key 详情页加一个 `🔌 允许协议` 按钮，点击进入多选编辑面板（勾选 anthropic / chat / responses 三项）。

Key 列表无变化；现有 `🎯 编辑允许模型` 按钮完全保留。

**anthropic 等价性**：空白 `allowedProtocols` 等同于"全允许"，对现有 Key 行为不变。

**行数**：~40

---

## 7.16 汇总表

| 文件 | 类型 | 行数 |
|---|---|---|
| `config.py` | ADD 默认值 | 10 |
| `auth.py` | ADD 函数 | 10 |
| `errors.py` | ADD 函数 + ErrType class | 50 |
| `fingerprint.py` | ADD 函数 | 160 |
| `upstream.py` | ADD 类 + 函数 | 400 |
| `scheduler.py` | EXTEND 默认参数 | 25 |
| `failover.py` | EXTEND 默认参数 + toolkit 分派 + 错误分派 + translator 接入点 | 80 |
| `probe.py` | EXTEND protocol 分派 | 40 |
| `channel/base.py` | ADD 字段 + 签名 | 5 |
| `channel/api_channel.py` | EXTEND 读 protocol | 5 |
| `channel/oauth_channel.py` | EXTEND 签名 | 3 |
| `channel/registry.py` | ADD factory | 10 |
| `server.py` | ADD 路由 + /v1/models 家族过滤 + lifespan 挂接 | 50 |
| `telegram/menus/channel_menu.py` | ADD 向导 / 编辑项 | 100 |
| `telegram/menus/apikey_menu.py` | ADD 按钮 | 40 |
| **合计** | | **~988（其中约 800 是追加、约 180 是扩展）** |

注：7.5 中 upstream.py 的 OpenAI SSE 工具类也可以挪进 `src/openai/upstream_sse.py`（零 anthropic 影响），视代码风格偏好。本方案先建议留在 `src/upstream.py` 就近（调用方 `failover.py` 不用跨目录 import），但二选一皆可。
