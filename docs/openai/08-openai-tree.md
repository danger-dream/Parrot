# 08 — `src/openai/` 子树文件清单

所有 OpenAI 特有的"业务逻辑"都在这棵子树里；它只 import `src/` 根的**协议无关**模块（scheduler / scorer / cooldown / affinity / state_db / log_db / notifier / public_ip / config / channel/registry / channel/base）以及 `src/errors`（追加的 OpenAI 错误函数）、`src/fingerprint`（追加的 OpenAI fp 函数）、`src/upstream`（追加的 OpenAI SSE 工具）、`src/auth.get_allowed_protocols`。

**约定**：`src/openai/` 内部永远不 import `src/transform/*` / `src/channel/api_channel.py` / `src/channel/oauth_channel.py`。

```
src/openai/
├── __init__.py
├── handler.py
├── auth_ex.py
├── store.py
├── channel/
│   ├── __init__.py
│   ├── api_channel.py
│   └── registration.py
└── transform/
    ├── __init__.py
    ├── common.py
    ├── guard.py
    ├── chat_to_responses.py
    ├── responses_to_chat.py
    ├── stream_c2r.py
    └── stream_r2c.py
```

## 8.1 `handler.py` （~220 行）

唯一的入口函数，对应 anthropic 的 `server.proxy_messages`，但协议泛化。

```python
async def handle(request: Request, *, ingress_protocol: str) -> Response:
    """
    ingress_protocol ∈ {"chat", "responses"}.
    流程：auth → allowedProtocols/allowedModels 检查 → body 解析 → guard
          → fingerprint_query → log_db.insert_pending → scheduler.schedule
          → failover.run_failover。
    """
    start_time = time.time()
    request_id = str(uuid.uuid4())
    client_ip = request.client.host if request.client else "?"

    # 1. auth
    key_name, allowed_models, err = auth.validate(request.headers)
    if err:
        return errors.json_error_openai(401, ErrTypeOpenAI.AUTH, err)
    allowed_protos = auth.get_allowed_protocols(key_name)
    if allowed_protos and ingress_protocol not in allowed_protos:
        return errors.json_error_openai(403, ErrTypeOpenAI.PERMISSION,
            f"Protocol '{ingress_protocol}' not allowed for this API key")

    # 2. body
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except Exception as exc:
        return errors.json_error_openai(400, ErrTypeOpenAI.INVALID_REQUEST, f"invalid json: {exc}")

    # 3. model
    model = body.get("model")
    if not model:
        return errors.json_error_openai(400, ErrTypeOpenAI.INVALID_REQUEST, "model is required")
    if allowed_models and model not in allowed_models:
        return errors.json_error_openai(403, ErrTypeOpenAI.PERMISSION,
            f"Model '{model}' is not allowed for this API key")

    # 4. CapabilityGuard（ingress 视角做死角检查）
    try:
        if ingress_protocol == "chat":
            guard.guard_chat_ingress(body)
        else:
            guard.guard_responses_ingress(body, store_enabled=_store_enabled())
    except guard.GuardError as ge:
        return errors.json_error_openai(ge.status, ge.err_type, ge.message)

    is_stream = bool(body.get("stream", False))   # OpenAI 默认非流式（和 anthropic 默认流式相反！）

    # 5. fingerprint（按 ingress 选函数）
    if ingress_protocol == "chat":
        fp_query = fingerprint.fingerprint_query_chat(key_name or "", client_ip, body.get("messages") or [])
        msg_count = len(body.get("messages") or [])
    else:
        fp_query = fingerprint.fingerprint_query_responses(key_name or "", client_ip, body.get("input") or [])
        msg_count = len(body.get("input") or []) if isinstance(body.get("input"), list) else 1

    tool_count = len(body.get("tools") or [])

    # 6. pending log（与 anthropic 同）
    req_headers = _sanitize_headers(dict(request.headers))
    await asyncio.to_thread(
        log_db.insert_pending,
        request_id, client_ip, key_name, model, is_stream, msg_count, tool_count,
        req_headers, body, fingerprint=fp_query,
    )

    # 7. 调度
    result = scheduler.schedule(body, api_key_name=key_name, client_ip=client_ip,
                                ingress_protocol=ingress_protocol)
    if result.affinity_hit:
        await asyncio.to_thread(log_db.update_pending, request_id, affinity_hit=1)

    if not result.candidates:
        # 与 anthropic 版同样的错误路径，只是错误格式用 openai
        ...
        return errors.json_error_openai(503, ErrTypeOpenAI.SERVER,
            f"No available upstream channels for model: {model}")

    # 8. failover
    try:
        response = await failover.run_failover(
            result, body, request_id, key_name, client_ip,
            is_stream=is_stream, start_time=start_time,
            ingress_protocol=ingress_protocol,
        )
    except Exception as e:
        traceback.print_exc()
        total_ms = int((time.time() - start_time) * 1000)
        await asyncio.to_thread(log_db.finish_error, request_id, f"unexpected: {e}", 0,
            http_status=500, total_ms=total_ms, affinity_hit=(1 if result.affinity_hit else 0))
        return errors.json_error_openai(500, ErrTypeOpenAI.SERVER, f"internal: {e}")

    return response
```

## 8.2 `auth_ex.py` （~20 行）

其实 `auth.get_allowed_protocols` 已经在 `src/auth.py`（见 07）；这里只放一个**可选**的便利包装器，若无额外逻辑可删，handler 直接 import `auth.get_allowed_protocols`。

## 8.3 `store.py` （~180 行）

见 [05-store.md](./05-store.md)。挂在 state.db 上的一张表 + CRUD + 后台清理 loop。

## 8.4 `channel/api_channel.py` （~200 行）

```python
class OpenAIApiChannel(Channel):
    type = "api"
    cc_mimicry = False   # OpenAI 永远不走 CC 伪装

    def __init__(self, entry: dict):
        self.name = entry["name"]
        self.key = f"api:{self.name}"
        self.display_name = self.name
        self.base_url = (entry.get("baseUrl") or "").rstrip("/")
        self.api_key = entry.get("apiKey", "")
        self.models: list[dict] = list(entry.get("models") or [])
        self.enabled = bool(entry.get("enabled", True))
        self.disabled_reason = entry.get("disabled_reason")
        self.protocol = entry.get("protocol", "openai-chat")
        assert self.protocol in ("openai-chat", "openai-responses"), \
            f"OpenAIApiChannel got invalid protocol: {self.protocol}"

    def supports_model(self, requested_model):
        for m in self.models:
            if m.get("alias") == requested_model:
                return m.get("real")
        return None

    def list_client_models(self):
        return [m.get("alias") for m in self.models if m.get("alias")]

    async def build_upstream_request(self, body, model, *, ingress_protocol="chat"):
        # 1. 按 (ingress, self.protocol) 选 body 策略
        if self.protocol == "openai-chat":
            path = "/v1/chat/completions"
            if ingress_protocol == "chat":
                payload = common.filter_chat_passthrough(body)
            else:  # responses → chat
                payload = responses_to_chat.translate_request(body,
                            store=_store_if_enabled(), api_key_name=body.get("_api_key_name",""))
        else:   # openai-responses
            path = "/v1/responses"
            if ingress_protocol == "responses":
                payload = common.filter_responses_passthrough(body)
            else:  # chat → responses
                payload = chat_to_responses.translate_request(body)

        payload["model"] = model

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "anthropic-proxy/openai-adapter",
        }
        return UpstreamRequest(
            url=f"{self.base_url}{path}",
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False, separators=(",",":")).encode("utf-8"),
            dynamic_tool_map=None,
        )

    async def restore_response(self, chunk, dynamic_map=None):
        # OpenAI 家族不做工具名还原，原样返回
        return chunk

    def display(self):
        return ChannelDisplay(
            key=self.key, type="api", display_name=self.name,
            enabled=self.enabled, disabled_reason=self.disabled_reason,
            models=self.list_client_models(),
        )
```

注意事项：
- **不要改 `Channel.build_upstream_request` 的返回结构**（仍是 `UpstreamRequest`，未来若要加 translator state 可走附加字段，比如在 openai 实现里 `UpstreamRequest.translator_ctx = {"resp_id": ..., "prev_id": ...}`）——新加字段要改 `dataclass`，属于共享扩展，见 §8.6。

## 8.5 `channel/registration.py` （~30 行）

```python
from src.channel import registry
from .api_channel import OpenAIApiChannel

def register_factories() -> None:
    registry.register_channel_factory("openai-chat", OpenAIApiChannel)
    registry.register_channel_factory("openai-responses", OpenAIApiChannel)
```

由 `server.py` lifespan 启动时调用一次。

## 8.6 `transform/*`

见 [04-transform.md](./04-transform.md)。每个文件的公开接口在那一章已详细列出。

## 8.7 跨协议 translator 状态在 failover 里的承载

`failover._consume_stream` 里如果 `ingress != ch.protocol`，需要把翻译器的"收尾 hook"和 Store 写入挂上。推荐路径：

```python
# failover._consume_stream 的接入（伪码，附在第 7 章之外为便于阅读）
translator_ctx = None
if ingress_protocol == "chat" and ch.protocol == "openai-responses":
    from src.openai.transform.stream_r2c import StreamTranslator
    translator_ctx = StreamTranslator(model=resolved_model, include_usage=_include_usage(body))
elif ingress_protocol == "responses" and ch.protocol == "openai-chat":
    from src.openai.transform.stream_c2r import StreamTranslator
    translator_ctx = StreamTranslator(model=resolved_model,
        previous_response_id=body.get("previous_response_id"),
        store_save_cb=_store_save_cb(api_key_name, resolved_model, ch.key, body))

# 循环里
restored = await ch.restore_response(chunk)
tracker.feed(restored); builder.feed(restored)
if translator_ctx:
    async for out in translator_ctx.feed(restored):
        yield out
else:
    yield restored

# 结束
if translator_ctx:
    async for out in translator_ctx.close():
        yield out
```

`StreamTranslator.feed/close` 是生成器；`_store_save_cb` 是一个闭包，在 chat→responses 方向"完结时把翻译前 input_items + 累积 output_items 存 store"。

## 8.8 需要加到 `UpstreamRequest` 的附加字段

为了让 chat→responses 方向 translator 能拿到"翻译后的 input_items"去写 Store，一个干净的做法：`OpenAIApiChannel.build_upstream_request` 返回时在 `UpstreamRequest` 里带上：

```python
# src/channel/base.py 的 UpstreamRequest 加字段：
@dataclass
class UpstreamRequest:
    url: str
    headers: dict[str, str]
    body: bytes
    method: str = "POST"
    dynamic_tool_map: Optional[dict] = None
    translator_ctx: Optional[dict] = None   # ★ 新增：openai 侧带上 {"input_items_for_store": [...]} 之类的上下文
```

**anthropic 等价性**：`translator_ctx` 默认 None，anthropic 渠道不设置，不影响。

## 8.9 子树行数总计

| 文件 | 行 |
|---|---|
| `handler.py` | 220 |
| `auth_ex.py` | 20（或 0） |
| `store.py` | 180 |
| `channel/api_channel.py` | 200 |
| `channel/registration.py` | 30 |
| `transform/common.py` | 200 |
| `transform/guard.py` | 120 |
| `transform/chat_to_responses.py` | 320 |
| `transform/responses_to_chat.py` | 380 |
| `transform/stream_c2r.py` | 420 |
| `transform/stream_r2c.py` | 460 |
| tests | 800 |
| **合计** | **~3350** |
