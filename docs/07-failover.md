# 07 — 故障转移

故障转移是整个代理的"最后一道坎"，必须小心处理"向下游发首包"这个不可逆时刻。

## 7.1 概念：三个关键时刻

对每次上游调用，定义三个时刻：

```
 t0 ─── 向上游发起请求
 │
 │   [连接中]     ← connect 阶段
 │
 t1 ─── 连接建立，等待上游返回
 │
 │   [等首字]     ← first_byte 阶段
 │
 t2 ─── 上游首个字节/事件到达（此时还没发给下游）
 │
 │   [首包判定]   ← 解析首个 chunk，是否 error / 黑名单 / OK
 │
 t3 ─── 若通过检查，开始向下游发 headers + first_chunk
 │      ★ 从这一刻起：stream_started=True，锁定渠道 ★
 │
 │   [持续转发]   ← idle 超时保护每个后续 chunk
 │
 t4 ─── 上游发完 [DONE]；向下游写完最后字节
```

**故障转移只在 `t0..t3` 之间发生。** `t3` 之后的任何错误只能以 Anthropic 标准错误事件收尾。

## 7.2 故障转移主循环

`src/failover.py`：

```python
async def run_failover(
    schedule_result: ScheduleResult,   # 含 candidates / fp_query / affinity_hit
    body: dict,
    request_id: str,
    api_key_name: Optional[str],
    client_ip: str,
    is_stream: bool,
    start_time: float,
) -> Response:   # 直接返回 FastAPI Response（JSONResponse 或 StreamingResponse）
    """
    顺序尝试 candidates，直到有一个成功发首包或全部失败。
    本函数返回时，要么已向下游发完响应，要么已写入错误 JSON。
    """
    cfg = config.get()
    timeouts = cfg["timeouts"]
    retry_count = 0
    last_error = None

    for attempt_order, (ch, resolved_model) in enumerate(candidates, start=1):
        log_db.record_retry_attempt(
            request_id, attempt_order, ch.key, ch.type, resolved_model,
            started_at=time.time(),
        )

        result = await _try_channel(
            ch, resolved_model, body, request_id, timeouts,
            response_writer, start_time,
            api_key_name, client_ip,
        )

        log_db.update_retry_attempt(
            request_id, attempt_order,
            connect_ms=result.connect_ms, first_byte_ms=result.first_byte_ms,
            ended_at=time.time(), outcome=result.outcome, error_detail=result.error_detail,
        )

        # 成功且已完成下游响应
        if result.outcome == "success" and result.stream_started_to_client:
            cooldown.clear(ch.key, resolved_model)
            scorer.record_success(ch.key, resolved_model,
                                  result.connect_ms, result.first_byte_ms, result.total_ms)
            # 写亲和
            fp_write = fingerprint_write(api_key_name, client_ip,
                                         body.get("messages", []), result.assistant_response)
            if fp_write:
                affinity.upsert(fp_write, ch.key, resolved_model)
            log_db.finish_success(request_id, ch.key, ch.type, resolved_model,
                                  **result.usage_fields(),
                                  connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms,
                                  total_ms=result.total_ms, retry_count=retry_count,
                                  affinity_hit=0,  # 由调度器侧在 pending 阶段已记录
                                  response_body=result.full_response_text)
            return

        # 已开始向下游发 SSE/HTTP 响应但异常中断
        if result.stream_started_to_client:
            # 不能切换渠道，已经发过字节了
            # response_writer 已负责写入错误收尾事件
            scorer.record_failure(ch.key, resolved_model, result.connect_ms)
            cooldown.record_error(ch.key, resolved_model, result.error_detail)
            log_db.finish_error(request_id, result.error_detail, retry_count,
                                final_channel_key=ch.key, final_channel_type=ch.type,
                                final_model=resolved_model, http_status=200,
                                connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms,
                                total_ms=result.total_ms,
                                response_body=result.full_response_text)
            return

        # 未发首包 → 可切换
        retry_count += 1
        last_error = result

        # 判断是否应记入 cooldown（某些"渠道级别"错误才记）
        if _should_cooldown(result.outcome):
            scorer.record_failure(ch.key, resolved_model, result.connect_ms)
            cooldown.record_error(ch.key, resolved_model, result.error_detail)

        # 401/403 对 OAuth 渠道：尝试刷新 token 一次后再计入下一轮
        if ch.type == "oauth" and result.http_status in (401, 403) and not result.already_refreshed:
            await oauth_manager.force_refresh(ch.email)
            # 构造一个"追加尝试"插入到 candidates 首位
            candidates.insert(attempt_order, (ch, resolved_model))   # 同渠道重试一次
            continue

    # 所有候选都失败，未发首包 → 返回 503
    response_writer.write_error_json(
        status=503, err_type="api_error",
        message=f"All channels failed. Last error: {last_error.error_detail if last_error else 'none'}",
    )
    log_db.finish_error(request_id, "all_channels_failed", retry_count,
                        http_status=503,
                        total_ms=int((time.time() - start_time) * 1000))
```

### 7.2.1 `_should_cooldown` 规则

```python
# 仅"渠道级别"错误记入 cooldown：
# - HTTP 5xx
# - 连接失败 / 连接超时
# - first_byte 超时
# - transport 错误（RemoteProtocolError 等）
# - 首包文本黑名单命中
# - 首包 JSON 含 error 字段

# 不记入 cooldown（通常是请求级问题）：
# - HTTP 400（客户端错误）
# - HTTP 401/403（未刷新成功前）
_DO_NOT_COOLDOWN = {"http_4xx_non_auth"}
```

## 7.3 _try_channel 实现

```python
async def _try_channel(
    ch, resolved_model, body, request_id, timeouts,
    response_writer, start_time,
    api_key_name, client_ip,
) -> AttemptResult:
    is_stream = body.get("stream", True)
    connect_timeout = timeouts["connect"]
    first_byte_timeout = timeouts["firstByte"]
    idle_timeout = timeouts["idle"]
    total_timeout = timeouts["total"]

    # 构造上游请求
    upstream_req = await ch.build_upstream_request(body, resolved_model)

    t_connect_start = time.time()
    try:
        # 用 httpx stream 模式发起（首字超时在外层用 wait_for 控制）
        cm = _http_client.stream(
            upstream_req.method, upstream_req.url,
            headers=upstream_req.headers, content=upstream_req.body,
            timeout=httpx.Timeout(connect=connect_timeout, read=total_timeout,
                                  write=30.0, pool=connect_timeout),
        )
        resp = await cm.__aenter__()
    except httpx.ConnectError as e:
        return AttemptResult(outcome="connect_error", error_detail=str(e))
    except httpx.ConnectTimeout:
        return AttemptResult(outcome="connect_timeout",
                             error_detail=f"connect timeout > {connect_timeout}s")
    except httpx.TimeoutException as e:
        return AttemptResult(outcome="connect_timeout", error_detail=str(e))

    connect_ms = int((time.time() - t_connect_start) * 1000)

    try:
        # HTTP 状态码处理
        if resp.status_code >= 400:
            raw = await resp.aread()
            err_text = raw.decode("utf-8", errors="replace")
            http_status = resp.status_code

            # 4xx 中的 401/403 由外层决定是否刷新 token 后重试
            outcome = ("http_auth_error" if resp.status_code in (401, 403)
                       else "http_error")

            return AttemptResult(
                outcome=outcome, http_status=http_status, connect_ms=connect_ms,
                error_detail=f"HTTP {http_status}: {err_text[:2000]}",
            )

        # 200 状态，继续
        if not is_stream:
            return await _consume_non_stream(resp, ch, resolved_model, response_writer,
                                             connect_ms, start_time, request_id, body,
                                             api_key_name, client_ip)
        else:
            return await _consume_stream(resp, ch, resolved_model, response_writer,
                                         connect_ms, start_time, request_id, body,
                                         first_byte_timeout, idle_timeout, total_timeout)
    finally:
        await cm.__aexit__(None, None, None)
```

## 7.4 非流式分支 `_consume_non_stream`

```python
async def _consume_non_stream(resp, ch, resolved_model, rw, connect_ms, start_time, ...):
    raw = await resp.aread()
    restored = await ch.restore_response(raw)
    total_ms = int((time.time() - start_time) * 1000)

    # 解析 JSON
    try:
        obj = json.loads(restored)
    except Exception:
        # 响应不是合法 JSON，视为上游错误
        return AttemptResult(outcome="upstream_malformed",
                             connect_ms=connect_ms, total_ms=total_ms,
                             error_detail=f"non-JSON response: {restored[:500]!r}")

    # 检查上游 error
    if obj.get("type") == "error" or "error" in obj and isinstance(obj["error"], dict):
        return AttemptResult(outcome="upstream_error_json",
                             connect_ms=connect_ms, total_ms=total_ms,
                             error_detail=str(obj.get("error", obj))[:2000])

    # 黑名单检查（full body 文本）
    bl_hit = blacklist.match(restored, ch.key)
    if bl_hit:
        return AttemptResult(outcome="blacklist_hit",
                             connect_ms=connect_ms, total_ms=total_ms,
                             error_detail=f"blacklist: {bl_hit}")

    # OK：写入下游
    rw.write_json(obj, status=200, headers=_pick_upstream_headers(resp))

    usage = extract_usage_from_response_json(obj)
    assistant_response = {"role": "assistant", "content": obj.get("content", [])}
    return AttemptResult(
        outcome="success", stream_started_to_client=True,
        connect_ms=connect_ms, first_byte_ms=None, total_ms=total_ms,
        usage=usage, full_response_text=restored.decode("utf-8", errors="replace"),
        assistant_response=assistant_response,
    )
```

## 7.5 流式分支 `_consume_stream`

这里是最复杂的部分，关键在于**把"上游首个 chunk"和"向下游发首字节"之间的窗口最大化利用**做安全检查。

```python
async def _consume_stream(resp, ch, resolved_model, rw, connect_ms, start_time,
                          request_id, body, first_byte_timeout, idle_timeout, total_timeout):
    # 1. 等第一个字节（first_byte_timeout）
    t_first_start = time.time()
    aiter = resp.aiter_bytes()

    try:
        first_chunk = await asyncio.wait_for(anext(aiter), timeout=first_byte_timeout)
    except asyncio.TimeoutError:
        return AttemptResult(outcome="first_byte_timeout",
                             connect_ms=connect_ms,
                             error_detail=f"first byte timeout > {first_byte_timeout}s")
    except StopAsyncIteration:
        return AttemptResult(outcome="closed_before_first_byte",
                             connect_ms=connect_ms,
                             error_detail="upstream closed before first byte")
    except Exception as e:
        return AttemptResult(outcome="transport_error",
                             connect_ms=connect_ms, error_detail=str(e))

    first_byte_ms = int((time.time() - t_first_start) * 1000 + (time.time() - start_time) * 0)
    # 实际 first_byte_ms = now - fetchStart：详见 upstream.py

    # 2. 还原首个 chunk（工具名还原对 OAuth / cc_mimicry=True 的 API 有效）
    first_chunk_restored = await ch.restore_response(first_chunk)

    # 3. 首包安全检查
    #    ① 首包文本黑名单
    #    ② 首个 SSE event 是 error / type=error
    parsed_first = _parse_first_sse_event(first_chunk_restored)
    if parsed_first and (parsed_first.get("type") == "error" or "error" in parsed_first):
        return AttemptResult(outcome="upstream_error_json",
                             connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                             error_detail=str(parsed_first.get("error", parsed_first))[:2000])

    bl_hit = blacklist.match(first_chunk_restored, ch.key)
    if bl_hit:
        return AttemptResult(outcome="blacklist_hit",
                             connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                             error_detail=f"blacklist: {bl_hit}")

    # 4. 通过检查 → 开始向下游发送 ★
    rw.write_sse_headers(status=200, headers=_pick_upstream_headers(resp))
    await rw.write_bytes(first_chunk_restored)

    # 5. 进入持续转发（带 idle 超时、total 超时）
    tracker = SSEUsageTracker()
    builder = SSEAssistantBuilder()
    tracker.feed(first_chunk_restored)
    builder.feed(first_chunk_restored)

    deadline = start_time + total_timeout
    while True:
        remaining_total = max(0.1, deadline - time.time())
        try:
            chunk = await asyncio.wait_for(
                anext(aiter),
                timeout=min(idle_timeout, remaining_total),
            )
        except asyncio.TimeoutError:
            # 区分 idle vs total
            if time.time() >= deadline:
                reason = f"total timeout > {total_timeout}s"
                outcome = "total_timeout"
            else:
                reason = f"idle timeout > {idle_timeout}s"
                outcome = "idle_timeout"
            # 已发首包，用 SSE 错误事件收尾
            rw.write_sse_error(err_type="api_error", message=f"upstream {reason}")
            rw.end()
            return AttemptResult(
                outcome=outcome, stream_started_to_client=True,
                connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                total_ms=int((time.time() - start_time) * 1000),
                error_detail=reason,
                full_response_text=tracker.get_full_response(),
                assistant_response=builder.get_assistant(),
            )
        except StopAsyncIteration:
            break
        except Exception as e:
            rw.write_sse_error(err_type="api_error", message=f"stream error: {e}")
            rw.end()
            return AttemptResult(
                outcome="transport_error", stream_started_to_client=True,
                connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                total_ms=int((time.time() - start_time) * 1000),
                error_detail=str(e),
                full_response_text=tracker.get_full_response(),
                assistant_response=builder.get_assistant(),
            )

        if not chunk:
            continue
        chunk_restored = await ch.restore_response(chunk)
        tracker.feed(chunk_restored)
        builder.feed(chunk_restored)
        await rw.write_bytes(chunk_restored)

    rw.end()
    return AttemptResult(
        outcome="success", stream_started_to_client=True,
        connect_ms=connect_ms, first_byte_ms=first_byte_ms,
        total_ms=int((time.time() - start_time) * 1000),
        usage=tracker.usage,
        full_response_text=tracker.get_full_response(),
        assistant_response=builder.get_assistant(),
    )
```

## 7.6 SSEAssistantBuilder

用于累积流式响应的 assistant 消息（用于亲和指纹写入）：

```python
class SSEAssistantBuilder:
    """从 SSE 流累积 content_block_start/delta/stop 事件，还原成完整 assistant 消息对象。"""

    def __init__(self):
        self._buf = b""
        self._content_blocks = {}  # index -> accumulated block dict
        self._stop_reason = None

    def feed(self, chunk: bytes):
        self._buf += chunk
        while b"\n" in self._buf:
            line_bytes, self._buf = self._buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                evt = json.loads(data)
            except Exception:
                continue
            t = evt.get("type", "")
            if t == "content_block_start":
                idx = evt.get("index", 0)
                self._content_blocks[idx] = dict(evt.get("content_block", {}))
            elif t == "content_block_delta":
                idx = evt.get("index", 0)
                delta = evt.get("delta", {})
                self._apply_delta(idx, delta)
            elif t == "content_block_stop":
                pass  # nothing special
            elif t == "message_delta":
                sr = evt.get("delta", {}).get("stop_reason")
                if sr:
                    self._stop_reason = sr

    def _apply_delta(self, idx, delta):
        block = self._content_blocks.get(idx, {})
        dt = delta.get("type", "")
        if dt == "text_delta":
            block["text"] = (block.get("text") or "") + delta.get("text", "")
        elif dt == "input_json_delta":
            block["_partial_json"] = (block.get("_partial_json") or "") + delta.get("partial_json", "")
        elif dt == "thinking_delta":
            block["thinking"] = (block.get("thinking") or "") + delta.get("thinking", "")
        # 其他 delta 类型按需扩展
        self._content_blocks[idx] = block

    def get_assistant(self) -> dict:
        blocks = []
        for idx in sorted(self._content_blocks.keys()):
            b = dict(self._content_blocks[idx])
            # 若是 tool_use，_partial_json 组装为 input 字段
            if b.get("type") == "tool_use" and "_partial_json" in b:
                try:
                    b["input"] = json.loads(b.pop("_partial_json"))
                except Exception:
                    b["input"] = {}
                    b.pop("_partial_json", None)
            else:
                b.pop("_partial_json", None)
            blocks.append(b)
        return {"role": "assistant", "content": blocks}
```

## 7.7 响应返回策略（实际实现）

**最终实现放弃了单独的 `ResponseWriter` 抽象**（M4 验证中发现不必要）。直接用 FastAPI 的 `JSONResponse` / `StreamingResponse` 作为 `run_failover` 的返回值，上层 `server.py` 的 handler 把这个 Response 对象直接 `return` 交给 FastAPI：

- **未发首包的错误** → `failover` 返回 `JSONResponse(status, {"type":"error",...})`
- **非流式成功** → `failover` 返回 `JSONResponse(200, upstream_json)`
- **流式成功（无论最终是否正常完成）** → `failover` 返回 `StreamingResponse(generator)`

`StreamingResponse.generator` 是一个 async 生成器，内部：
1. yield 首个 chunk（已通过安全检查）
2. `async for chunk in aiter: yield chunk`（带 idle/total 超时）
3. 任何异常 → 用 `errors.sse_error_line()` 追加一条 `event: error` 再 yield
4. finally 关闭上游 stream ctx

所有 DB 记录（scorer/cooldown/affinity/log_db.finish_*）在 generator 的正常完成/异常分支中显式调用，不依赖外部 ResponseWriter 协调。

这个简化比 ResponseWriter+Queue 方案少一层，代码更直观，M4 测试 9/9 过。

## 7.8 黑名单匹配 `src/blacklist.py`

```python
def match(text_or_bytes, channel_key: str) -> str | None:
    """
    文本中包含任一黑名单关键字 → 返回该关键字；否则 None。
    """
    if isinstance(text_or_bytes, bytes):
        try:
            text = text_or_bytes.decode("utf-8", errors="replace")
        except Exception:
            return None
    else:
        text = text_or_bytes

    cfg = config.get()
    bl = cfg.get("contentBlacklist", {})
    words = list(bl.get("default", []))
    # 渠道按 display_name 或 key 分组（支持两种）
    by_ch = bl.get("byChannel", {})
    ch_name = channel_key.split(":", 1)[1] if ":" in channel_key else channel_key
    words.extend(by_ch.get(channel_key, []))
    words.extend(by_ch.get(ch_name, []))

    for w in words:
        if w and w in text:
            return w
    return None
```

## 7.9 错误 outcome 汇总

写入 `retry_chain.outcome` 字段的枚举：

| outcome | 含义 | stream_started | cooldown |
|---|---|---|---|
| `success` | 成功完成 | T | 清除 |
| `connect_error` | 连接失败（DNS/Refused） | F | 记 |
| `connect_timeout` | 连接超时 | F | 记 |
| `first_byte_timeout` | 首字超时 | F | 记 |
| `idle_timeout` | 两 chunk 之间空闲超时 | T | 记 |
| `total_timeout` | 总耗时超时 | T | 记 |
| `closed_before_first_byte` | 上游在首包前断开 | F | 记 |
| `http_error` | HTTP 5xx / 其他 4xx | F | 记（5xx） |
| `http_auth_error` | HTTP 401/403 | F | 不记（先刷 token） |
| `upstream_error_json` | 200 + body 含 error | F/T 看发生时刻 | 记 |
| `blacklist_hit` | 首包文本黑名单命中 | F | 记 |
| `upstream_malformed` | 非法 JSON | F | 记 |
| `transport_error` | 其他传输错误 | F/T 看发生时刻 | 记 |
| `transform_error` | 代理自身的转换异常（很少见） | F | 不记（与上游无关） |

`_should_cooldown(outcome)` 的 True 集合：除 `success` / `http_auth_error` / `transform_error` 外全部为 True。
