"""故障转移主循环。

设计（docs/07）：
  - 向下游发送任何字节前为"可切换"区（未发首包）；发出后锁定当前渠道。
  - 流式首包成功解析通过 safety check（黑名单、上游 error JSON）才开始回写下游。
  - 四段超时独立：connect / first_byte / idle / total。
  - OAuth 渠道 401/403 尝试 force_refresh 后同渠道重试一次；刷失败标 auth_error。
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import (
    affinity, blacklist, config, cooldown, errors, fingerprint, log_db,
    notifier, oauth_manager, scorer, upstream,
)
from .channel.base import Channel
from .scheduler import ScheduleResult


# ─── 结果结构 ─────────────────────────────────────────────────────

@dataclass
class AttemptResult:
    outcome: str
    success: bool = False
    stream_started: bool = False
    response: Optional[Response] = None
    http_status: Optional[int] = None
    connect_ms: Optional[int] = None
    first_byte_ms: Optional[int] = None
    total_ms: Optional[int] = None
    error_detail: Optional[str] = None
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0})
    full_response_text: Optional[str] = None
    assistant_response: Optional[dict] = None


_OUTCOMES_NO_COOLDOWN = {
    "success",
    "http_auth_error",   # 先刷 token 再判
    "transform_error",   # 代理自己 bug，和上游无关
}


def _should_cooldown(outcome: str) -> bool:
    return outcome not in _OUTCOMES_NO_COOLDOWN


# ─── 辅助 ─────────────────────────────────────────────────────────

def _remaining_ms(deadline_ts: float) -> int:
    return max(0, int((deadline_ts - time.time()) * 1000))


def _err_type_from_outcome(outcome: str, http_status: Optional[int]) -> str:
    if http_status is not None:
        return errors.classify_http_status(http_status)
    if outcome in ("connect_timeout", "first_byte_timeout", "idle_timeout", "total_timeout"):
        return errors.ErrType.TIMEOUT
    if outcome == "transform_error":
        return errors.ErrType.INVALID_REQUEST
    return errors.ErrType.API


def _pick_upstream_headers(resp: httpx.Response) -> dict:
    """转发部分上游 headers 到下游（限定范围）。"""
    out = {}
    for h in ("content-type", "x-request-id", "request-id"):
        if h in resp.headers:
            out[h] = resp.headers[h]
    return out


# ─── 主入口 ───────────────────────────────────────────────────────

async def run_failover(
    schedule_result: ScheduleResult,
    body: dict,
    request_id: str,
    api_key_name: Optional[str],
    client_ip: str,
    is_stream: bool,
    start_time: float,
) -> Response:
    """执行调度候选的顺序重试。返回 FastAPI Response。

    内部完成：
      - retry_chain 插入 / 更新
      - scorer / cooldown 更新（成功清零、失败记入）
      - affinity 命中 touch；成功后（non-stream 或 stream 全量完成）写入新绑定
      - log_db 的 finish_success / finish_error
    """
    candidates = list(schedule_result.candidates)
    affinity_hit = 1 if schedule_result.affinity_hit else 0
    fp_query = schedule_result.fp_query

    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    total_timeout = int(timeouts.get("total", 600))
    deadline_ts = start_time + total_timeout

    retry_count = 0
    refreshed_once: set[str] = set()
    last_result: Optional[AttemptResult] = None
    # 跟踪真实最后尝试的渠道（不同于"候选列表最后一条"，因为 OAuth 重刷会重试同 ch）
    last_ch_key: Optional[str] = None
    last_ch_type: Optional[str] = None
    last_model: Optional[str] = None

    # 把 candidates 改成可扩展的 list（OAuth 刷 token 后重试同渠道）
    pending = list(candidates)  # 仍从首位取
    idx = 0
    attempt_order = 0

    while idx < len(pending):
        ch, resolved_model = pending[idx]
        attempt_order += 1
        last_ch_key, last_ch_type, last_model = ch.key, ch.type, resolved_model

        attempt_id = log_db.record_retry_attempt(
            request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
        )

        result = await _try_channel(
            ch, resolved_model, body, is_stream, deadline_ts, start_time,
            fp_query, body.get("messages") or [], api_key_name, client_ip,
            request_id, retry_count, affinity_hit,
        )
        last_result = result

        log_db.update_retry_attempt(
            attempt_id,
            connect_ms=result.connect_ms, first_byte_ms=result.first_byte_ms,
            ended_at=time.time(), outcome=result.outcome,
            error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
        )

        if result.success or result.stream_started:
            # 成功已完成；或已发首包但出错（已用 SSE error 收尾）
            # 注意：scorer / cooldown / affinity / log_db 在 _try_channel 内完成
            return result.response

        # 未发首包失败：判断是否 OAuth 401/403 可刷一次
        if (
            ch.type == "oauth"
            and result.http_status in (401, 403)
            and ch.key not in refreshed_once
        ):
            refreshed_once.add(ch.key)
            try:
                await oauth_manager.force_refresh(getattr(ch, "email"))
                print(f"[failover] OAuth 401/403 on {ch.key}, refreshed; retrying same channel")
                retry_count += 1
                continue  # 不 idx++，重试同 ch
            except Exception as exc:
                print(f"[failover] OAuth refresh failed for {ch.key}: {exc}")
                email = getattr(ch, "email", "?")
                try:
                    oauth_manager.set_enabled(email, False, reason="auth_error")
                except Exception:
                    pass
                # 通知 admin（与 proactive_refresh_loop 行为对齐）
                try:
                    ek = notifier.escape_html
                    notifier.notify_event(
                        "oauth_refresh_failed",
                        "⚠ <b>OAuth Token 刷新失败</b>（请求路径触发）\n"
                        f"账号: <code>{ek(email)}</code>\n"
                        f"原因: <code>{ek(str(exc))}</code>\n"
                        "账号已被自动禁用 (auth_error)。请通过 TG Bot 重新登录或粘贴新 JSON。"
                    )
                except Exception:
                    pass
                # fallthrough 到普通失败处理

        # 普通失败处理
        if _should_cooldown(result.outcome):
            cooldown.record_error(ch.key, resolved_model, result.error_detail)
        scorer.record_failure(ch.key, resolved_model, connect_ms=result.connect_ms)
        retry_count += 1
        idx += 1

    # 全失败
    err_detail = (last_result.error_detail if last_result else "no candidates") or "unknown"
    err_type = _err_type_from_outcome(
        last_result.outcome if last_result else "no_candidates",
        last_result.http_status if last_result else None,
    )
    # 状态码（设计 doc §10.1）：
    #   - 全候选耗尽 → 503 api_error（默认）
    #   - 最后一次是连接/首字/总超时 → 504 timeout_error
    #   - 最后一次是连接/传输错误 → 502 api_error
    status = 503
    if last_result and last_result.outcome in ("connect_timeout", "first_byte_timeout", "total_timeout"):
        status = 504
    elif last_result and last_result.outcome in ("connect_error", "transport_error"):
        status = 502
    msg = f"All upstream channels failed. Last error: {err_detail[:400]}"

    total_ms = int((time.time() - start_time) * 1000)
    await asyncio.to_thread(
        log_db.finish_error, request_id, err_detail[:4000], retry_count,
        final_channel_key=last_ch_key,
        final_channel_type=last_ch_type,
        final_model=last_model,
        connect_ms=(last_result.connect_ms if last_result else None),
        first_token_ms=(last_result.first_byte_ms if last_result else None),
        total_ms=total_ms, http_status=status, affinity_hit=affinity_hit,
    )
    return errors.json_error_response(status, err_type, msg)


# ─── 单渠道尝试 ──────────────────────────────────────────────────

async def _try_channel(
    ch: Channel, resolved_model: str, body: dict,
    is_stream: bool, deadline_ts: float, start_time: float,
    fp_query: Optional[str], messages: list,
    api_key_name: Optional[str], client_ip: str,
    request_id: str, retry_count_so_far: int, affinity_hit: int,
) -> AttemptResult:
    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    connect_timeout = int(timeouts.get("connect", 10))
    first_byte_timeout = int(timeouts.get("firstByte", 30))
    idle_timeout = int(timeouts.get("idle", 30))

    # 1. 构造上游请求
    try:
        upstream_req = await ch.build_upstream_request(body, resolved_model)
    except Exception as exc:
        traceback.print_exc()
        return AttemptResult(
            outcome="transform_error",
            error_detail=f"transform error: {exc}",
            http_status=None,
        )

    # 与本次请求一一对应的工具名映射；不再依赖 channel 实例属性，避免并发覆盖
    dynamic_map = upstream_req.dynamic_tool_map

    client = upstream.get_client()
    t_send = time.time()
    remaining = max(1.0, deadline_ts - t_send)

    try:
        ctx = client.stream(
            upstream_req.method,
            upstream_req.url,
            headers=upstream_req.headers,
            content=upstream_req.body,
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=remaining,
                write=30.0,
                pool=connect_timeout,
            ),
        )
    except Exception as exc:
        return AttemptResult(
            outcome="transport_error",
            error_detail=f"send build error: {exc}",
        )

    # 进入 stream context
    upstream_resp: Optional[httpx.Response] = None
    try:
        # ctx.__aenter__() 发送请求 + 读 response header。httpx 的阶段超时不保证
        # 总时长（如果上游慢慢吐字节，每阶段单独不超时但累积可能远超 total）。
        # 用 asyncio.wait_for 做硬性总超时兜底。
        enter_timeout = max(1.0, deadline_ts - time.time())
        try:
            upstream_resp = await asyncio.wait_for(
                ctx.__aenter__(), timeout=enter_timeout,
            )
        except asyncio.TimeoutError:
            # 总时长耗尽：ctx 可能未成功 enter，不必 safe_exit
            return AttemptResult(
                outcome="total_timeout",
                error_detail=f"total timeout during connect/headers (> {int(enter_timeout)}s)",
            )
        except httpx.ConnectTimeout:
            return AttemptResult(outcome="connect_timeout",
                                 error_detail=f"connect timeout > {connect_timeout}s")
        except httpx.ConnectError as exc:
            return AttemptResult(outcome="connect_error",
                                 error_detail=f"connect error: {exc}")
        except httpx.TimeoutException as exc:
            return AttemptResult(outcome="connect_timeout",
                                 error_detail=f"timeout: {exc}")
        except Exception as exc:
            return AttemptResult(outcome="transport_error",
                                 error_detail=f"transport: {exc}")

        connect_ms = int((time.time() - t_send) * 1000)

        # 2. HTTP 状态码检查
        if upstream_resp.status_code >= 400:
            # 读错误 body：用剩余总时间作为硬超时，防止上游慢慢吐字节吃完总时长
            read_timeout = max(1.0, deadline_ts - time.time())
            try:
                raw = await asyncio.wait_for(
                    upstream_resp.aread(), timeout=read_timeout,
                )
            except asyncio.TimeoutError:
                await _safe_exit(ctx)
                return AttemptResult(
                    outcome="total_timeout",
                    connect_ms=connect_ms,
                    error_detail=f"total timeout reading error body (> {int(read_timeout)}s)",
                )
            except Exception as exc:
                await _safe_exit(ctx)
                return AttemptResult(
                    outcome="transport_error",
                    connect_ms=connect_ms,
                    error_detail=f"read http error body: {exc}",
                )
            err_text = raw.decode("utf-8", errors="replace")
            status = upstream_resp.status_code
            resp_headers = _pick_upstream_headers(upstream_resp)
            await _safe_exit(ctx)

            outcome = "http_auth_error" if status in (401, 403) else "http_error"
            return AttemptResult(
                outcome=outcome,
                http_status=status,
                connect_ms=connect_ms,
                error_detail=f"HTTP {status}: {err_text[:2000]}",
            )

        # 3. 非流式分支
        if not is_stream:
            return await _consume_non_stream(
                ctx, upstream_resp, ch, resolved_model, dynamic_map,
                connect_ms, start_time, request_id,
                messages, api_key_name, client_ip,
                fp_query, retry_count_so_far, affinity_hit,
            )

        # 4. 流式分支
        return await _consume_stream(
            ctx, upstream_resp, ch, resolved_model, dynamic_map,
            connect_ms, start_time, deadline_ts,
            first_byte_timeout, idle_timeout,
            request_id, messages, api_key_name, client_ip,
            fp_query, retry_count_so_far, affinity_hit,
        )
    except Exception as exc:
        traceback.print_exc()
        try:
            await _safe_exit(ctx)
        except Exception:
            pass
        return AttemptResult(
            outcome="transport_error",
            error_detail=f"unexpected: {exc}",
        )


async def _safe_exit(ctx) -> None:
    try:
        await ctx.__aexit__(None, None, None)
    except Exception:
        pass


# ─── 非流式 ──────────────────────────────────────────────────────

async def _consume_non_stream(
    ctx, upstream_resp: httpx.Response, ch: Channel, resolved_model: str,
    dynamic_map: Optional[dict],
    connect_ms: int, start_time: float, request_id: str,
    messages: list, api_key_name: Optional[str], client_ip: str,
    fp_query: Optional[str], retry_count_so_far: int, affinity_hit: int,
) -> AttemptResult:
    # 读 body：用剩余总时间作为硬超时（httpx 的 read timeout 只保证单次 chunk 间隔）
    cfg = config.get()
    total_timeout = int((cfg.get("timeouts") or {}).get("total", 600))
    deadline_ts = start_time + total_timeout
    read_timeout = max(1.0, deadline_ts - time.time())
    try:
        raw = await asyncio.wait_for(upstream_resp.aread(), timeout=read_timeout)
    except asyncio.TimeoutError:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="total_timeout",
            connect_ms=connect_ms,
            error_detail=f"total timeout reading non-stream body (> {int(read_timeout)}s)",
        )
    except Exception as exc:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="transport_error",
            connect_ms=connect_ms,
            error_detail=f"read non-stream body: {exc}",
        )

    resp_headers = _pick_upstream_headers(upstream_resp)
    await _safe_exit(ctx)

    if not raw:
        return AttemptResult(
            outcome="closed_before_first_byte",
            connect_ms=connect_ms,
            error_detail="upstream empty body",
        )

    # 渠道还原（如 OAuth / cc_mimicry 工具名）
    restored = await ch.restore_response(raw, dynamic_map=dynamic_map)
    total_ms = int((time.time() - start_time) * 1000)

    # 解析 JSON
    try:
        obj = json.loads(restored)
    except Exception as exc:
        return AttemptResult(
            outcome="upstream_malformed",
            connect_ms=connect_ms,
            total_ms=total_ms,
            error_detail=f"non-JSON response: {exc}",
        )

    # 上游 error
    if obj.get("type") == "error" or ("error" in obj and isinstance(obj.get("error"), dict)):
        return AttemptResult(
            outcome="upstream_error_json",
            connect_ms=connect_ms,
            total_ms=total_ms,
            error_detail=json.dumps(obj.get("error", obj), ensure_ascii=False)[:2000],
        )

    # 黑名单
    bl_hit = blacklist.match(restored, ch.key)
    if bl_hit:
        return AttemptResult(
            outcome="blacklist_hit",
            connect_ms=connect_ms,
            total_ms=total_ms,
            error_detail=f"blacklist: {bl_hit}",
        )

    # 成功：记录并构造响应
    usage = upstream.extract_usage_from_json(obj)
    assistant_msg = {"role": obj.get("role", "assistant"), "content": obj.get("content") or []}

    scorer.record_success(
        ch.key, resolved_model,
        connect_ms=connect_ms, first_byte_ms=None, total_ms=total_ms,
    )
    cooldown.clear(ch.key, resolved_model)

    # 亲和写入
    fp_write = fingerprint.fingerprint_write(
        api_key_name or "", client_ip or "", messages, assistant_msg,
    )
    if fp_write:
        affinity.upsert(fp_write, ch.key, resolved_model)

    # 落库
    await asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=connect_ms, first_token_ms=None, total_ms=total_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=restored.decode("utf-8", errors="replace") if isinstance(restored, bytes) else str(restored),
        http_status=upstream_resp.status_code,
    )

    response = JSONResponse(
        content=obj,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )
    return AttemptResult(
        outcome="success", success=True, response=response,
        connect_ms=connect_ms, total_ms=total_ms, http_status=upstream_resp.status_code,
        usage=usage, assistant_response=assistant_msg,
        full_response_text=restored.decode("utf-8", errors="replace") if isinstance(restored, bytes) else str(restored),
    )


# ─── 流式 ────────────────────────────────────────────────────────

async def _consume_stream(
    ctx, upstream_resp: httpx.Response, ch: Channel, resolved_model: str,
    dynamic_map: Optional[dict],
    connect_ms: int, start_time: float, deadline_ts: float,
    first_byte_timeout: int, idle_timeout: int,
    request_id: str, messages: list, api_key_name: Optional[str], client_ip: str,
    fp_query: Optional[str], retry_count_so_far: int, affinity_hit: int,
) -> AttemptResult:
    aiter = upstream_resp.aiter_bytes()

    # 1. 等首字节（first_byte_timeout 或 total 剩余，取小者）
    t_first_start = time.time()
    remaining_ms = _remaining_ms(deadline_ts)
    first_wait = min(first_byte_timeout, max(1, remaining_ms / 1000))

    try:
        first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=first_wait)
    except asyncio.TimeoutError:
        await _safe_exit(ctx)
        # 重新算 remaining：wait 之后 deadline 可能已耗尽
        if _remaining_ms(deadline_ts) <= 0:
            return AttemptResult(
                outcome="total_timeout", connect_ms=connect_ms,
                error_detail=f"total timeout during first byte wait",
            )
        return AttemptResult(
            outcome="first_byte_timeout", connect_ms=connect_ms,
            error_detail=f"first byte timeout > {first_byte_timeout}s",
        )
    except StopAsyncIteration:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="closed_before_first_byte", connect_ms=connect_ms,
            error_detail="upstream closed stream before first byte",
        )
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="transport_error", connect_ms=connect_ms,
            error_detail=f"first byte transport: {exc}",
        )

    first_byte_ms = int((time.time() - t_first_start) * 1000 + connect_ms)
    if not first_chunk:
        # 拿到空 chunk，继续读下一个；简化：视为 closed
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="closed_before_first_byte", connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail="upstream sent empty first chunk",
        )

    # 2. 首包还原 + 安全检查
    first_chunk_restored = await ch.restore_response(first_chunk, dynamic_map=dynamic_map)

    # 2a) 首个 SSE event 是 error？
    first_event = upstream.parse_first_sse_event(first_chunk_restored)
    if first_event and (
        first_event.get("type") == "error"
        or (isinstance(first_event.get("error"), dict))
    ):
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="upstream_error_json",
            connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail=json.dumps(first_event.get("error", first_event), ensure_ascii=False)[:2000],
        )

    # 2b) 黑名单
    bl_hit = blacklist.match(first_chunk_restored, ch.key)
    if bl_hit:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="blacklist_hit",
            connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail=f"blacklist: {bl_hit}",
        )

    # 3. 通过检查 → 开始向下游发 ★
    resp_headers = _pick_upstream_headers(upstream_resp)
    tracker = upstream.SSEUsageTracker()
    builder = upstream.SSEAssistantBuilder()
    tracker.feed(first_chunk_restored)
    builder.feed(first_chunk_restored)
    upstream_status = upstream_resp.status_code

    state: dict = {"total_ms": None, "finalized": False}

    async def _finalize_success():
        if state["finalized"]:
            return
        state["finalized"] = True
        total_ms = int((time.time() - start_time) * 1000)

        scorer.record_success(
            ch.key, resolved_model,
            connect_ms=connect_ms, first_byte_ms=first_byte_ms, total_ms=total_ms,
        )
        cooldown.clear(ch.key, resolved_model)

        assistant_msg = builder.get_assistant()
        fp_write = fingerprint.fingerprint_write(
            api_key_name or "", client_ip or "", messages, assistant_msg,
        )
        if fp_write:
            affinity.upsert(fp_write, ch.key, resolved_model)

        await asyncio.to_thread(
            log_db.finish_success,
            request_id, ch.key, ch.type, resolved_model,
            input_tokens=tracker.usage["input_tokens"],
            output_tokens=tracker.usage["output_tokens"],
            cache_creation_tokens=tracker.usage["cache_creation"],
            cache_read_tokens=tracker.usage["cache_read"],
            connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
            retry_count=retry_count_so_far, affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(),
            http_status=upstream_status,
        )

    async def _emit_error_and_finalize(err_type: str, message: str, outcome: str):
        if state["finalized"]:
            return
        state["finalized"] = True
        total_ms = int((time.time() - start_time) * 1000)

        # 已发首包的错误：视为"这一次失败"，记入 cooldown/scorer
        if _should_cooldown(outcome):
            cooldown.record_error(ch.key, resolved_model, message)
        scorer.record_failure(ch.key, resolved_model, connect_ms=connect_ms)

        await asyncio.to_thread(
            log_db.finish_error,
            request_id, message, retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
            http_status=upstream_status, affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(),
        )

    async def _finalize_client_cancelled():
        """客户端断开：不计 cooldown/scorer，仅记日志便于审计。"""
        if state["finalized"]:
            return
        state["finalized"] = True
        total_ms = int((time.time() - start_time) * 1000)
        await asyncio.to_thread(
            log_db.finish_error,
            request_id, "client disconnected", retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
            http_status=upstream_status, affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(),
        )

    async def stream_generator():
        """把首包 + 后续 chunk 转发给下游，同时在中途错误时用 SSE error event 收尾。"""
        if state["finalized"]:
            return
        try:
            # 首包
            yield first_chunk_restored

            # 后续 chunk，带 idle / total 超时
            while True:
                remaining = _remaining_ms(deadline_ts)
                if remaining <= 0:
                    await _emit_error_and_finalize(
                        "api_error", f"upstream total timeout > {int((deadline_ts - start_time))}s",
                        outcome="total_timeout",
                    )
                    yield errors.sse_error_line(errors.ErrType.API, f"upstream total timeout")
                    return
                wait_sec = min(idle_timeout, max(1, remaining / 1000))
                try:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=wait_sec)
                except asyncio.TimeoutError:
                    if _remaining_ms(deadline_ts) <= 0:
                        await _emit_error_and_finalize(
                            "api_error", f"upstream total timeout",
                            outcome="total_timeout",
                        )
                        yield errors.sse_error_line(errors.ErrType.API, "upstream total timeout")
                        return
                    await _emit_error_and_finalize(
                        "api_error", f"upstream idle timeout > {idle_timeout}s",
                        outcome="idle_timeout",
                    )
                    yield errors.sse_error_line(errors.ErrType.API, f"upstream idle timeout > {idle_timeout}s")
                    return
                except StopAsyncIteration:
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                    await _emit_error_and_finalize(
                        "api_error", f"stream transport error: {exc}",
                        outcome="transport_error",
                    )
                    yield errors.sse_error_line(errors.ErrType.API, f"stream transport error: {exc}")
                    return

                if not chunk:
                    continue
                restored = await ch.restore_response(chunk, dynamic_map=dynamic_map)
                tracker.feed(restored)
                builder.feed(restored)
                yield restored

            # 正常完成
            await _finalize_success()
        except asyncio.CancelledError:
            # 客户端断开（或上层取消）：不归咎上游，不记 cooldown/scorer
            await _finalize_client_cancelled()
            raise
        except BaseException as exc:
            await _emit_error_and_finalize(
                "api_error", f"stream error: {exc}",
                outcome="transport_error",
            )
            raise
        finally:
            await _safe_exit(ctx)

    sresp = StreamingResponse(
        stream_generator(),
        status_code=upstream_status,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
    )

    return AttemptResult(
        outcome="success", success=True, stream_started=True,
        response=sresp, http_status=upstream_status,
        connect_ms=connect_ms, first_byte_ms=first_byte_ms,
    )
