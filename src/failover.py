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

import threading

from . import (
    affinity, blacklist, concurrency, config, cooldown, errors, fingerprint,
    log_db, notifier, oauth_manager, scorer, state_db, upstream,
)
from .channel.base import Channel
from .channel.openai_oauth_channel import OpenAIOAuthChannel
from .oauth import openai as openai_provider
from .scheduler import ScheduleResult


# ─── OpenAI Codex 响应头 snapshot 节流 ───────────────────────────
#
# ChatGPT internal API 把 rate-limit 放在每次请求的 response header 里，没有
# 独立 usage 端点。为避免每次请求都写一次 state_db，按 email 30s 节流（与
# sub2api openAICodexSnapshotPersistMinInterval 对齐）。吞掉所有异常，不影响主链路。

_CODEX_SNAPSHOT_WRITE_INTERVAL_S = 30.0
_codex_snapshot_last: dict[str, float] = {}
_codex_snapshot_lock = threading.Lock()


def _maybe_record_codex_snapshot(ch: Channel, resp: httpx.Response) -> None:
    if not isinstance(ch, OpenAIOAuthChannel):
        return
    try:
        snap = openai_provider.parse_rate_limit_headers(dict(resp.headers))
        if not snap:
            return
        account_key = getattr(ch, "account_key", None) or ch.email
        email = ch.email
        # throttle bucket 用 email 作 key（同一邮箱下 OpenAI 最多一个账号，不会冲突；
        # 同时保留 forget_codex_snapshot(email) / forget_codex_snapshot(account_key) 两种语义）
        now = time.time()
        with _codex_snapshot_lock:
            last = _codex_snapshot_last.get(email, 0.0)
            if now - last < _CODEX_SNAPSHOT_WRITE_INTERVAL_S:
                return
            _codex_snapshot_last[email] = now
        normalized = openai_provider.normalize_codex_snapshot(snap)
        state_db.quota_save_openai_snapshot(account_key, snap, normalized, email=email)

        # 🚨 响应头超限自动禁用（2026-04-20 新增）
        # Codex 无 surpassed-threshold，但有 primary/secondary used percent；
        # 判断任一 ≥ disableThresholdPercent 则触发（与 quota_monitor_once 语义一致）
        _maybe_auto_disable_by_codex_snapshot(account_key, email, snap)
    except Exception as exc:
        print(f"[failover] codex snapshot record failed for {getattr(ch, 'email', '?')}: {exc}")


# ─── Anthropic 响应头被动采样 snapshot 节流 ──────────────────────
#
# 参考 sub2api ratelimit_service.go::UpdateSessionWindow。Anthropic 在每次
# 成功响应的响应头里带 5h/7d rate-limit utilization，比主动拉 /api/oauth/usage
# 新鲜得多且无 rate-limit 成本。与 Codex 节流机制对称：按 account_key 30s
# 节流，避免每次请求都写 state_db。
#
# 注意：这条路径**只更新 five_hour_* / seven_day_* 四个字段**，不碰主动拉
# 才有的 sonnet/opus/extra 维度；详见 state_db.quota_patch_passive。

_ANTHROPIC_SNAPSHOT_WRITE_INTERVAL_S = 30.0
_anthropic_snapshot_last: dict[str, float] = {}
_anthropic_snapshot_lock = threading.Lock()


def _maybe_record_anthropic_snapshot(ch: Channel, resp: httpx.Response) -> None:
    # 延迟 import 避免循环依赖
    from .channel.oauth_channel import OAuthChannel
    from .anthropic.rate_limit_headers import parse_rate_limit_headers

    if not isinstance(ch, OAuthChannel):
        return
    try:
        patch = parse_rate_limit_headers(dict(resp.headers))
        if not patch:
            return
        account_key = getattr(ch, "account_key", None) or ch.email
        email = ch.email
        now = time.time()
        with _anthropic_snapshot_lock:
            last = _anthropic_snapshot_last.get(account_key, 0.0)
            if now - last < _ANTHROPIC_SNAPSHOT_WRITE_INTERVAL_S:
                return
            _anthropic_snapshot_last[account_key] = now
        state_db.quota_patch_passive(account_key, patch, email=email)

        # 🚨 响应头超限自动禁用（2026-04-20 新增）
        # 5h/7d 任一超限且账号当前未被禁用 → 立即置为 quota disabled
        # 这比 quota_monitor_loop 的轮询快得多（下一次请求前就禁用，不用等 30min）
        _maybe_auto_disable_by_headers(
            account_key, ch.email, dict(resp.headers),
            provider="claude",
        )
    except Exception as exc:
        print(f"[failover] anthropic snapshot record failed for "
              f"{getattr(ch, 'email', '?')}: {exc}")


def forget_anthropic_snapshot(account_key_or_email: str) -> None:
    """账户删除时清 Anthropic 节流桶，避免内存无限累积。

    与 forget_codex_snapshot 对称：同时按 account_key 与拆出的 email 两个 key
    清理（兼容性保险）。
    """
    if not account_key_or_email:
        return
    key = account_key_or_email
    email = key.split(":", 1)[1] if ":" in key else key
    with _anthropic_snapshot_lock:
        _anthropic_snapshot_last.pop(email, None)
        _anthropic_snapshot_last.pop(key, None)


# ─── 响应头超限自动禁用（2026-04-20 新增） ───────────────────────
#
# 两家 OAuth 都在每次请求时从响应头解析出 rate-limit 状态。与 `quota_monitor_loop`
# 的轮询判断相比，响应头判断是**实时**的——一旦某次请求返回已超限的头，就可以
# 立即把账号标 quota disabled，避免下一次请求再打过去被 429。
#
# 触发条件：
#   - Anthropic: surpassed-threshold=true OR utilization>=1.0（任一窗口）
#   - OpenAI  : primary/secondary used_percent ≥ disableThresholdPercent (default 95)
#
# 幂等：账号已是 disabled_reason="quota" 时不重复置位。
# auth_error / user 禁用的账号不碰（保留原始禁用原因）。


def _get_quota_disable_threshold_pct() -> float:
    cfg = config.get()
    qm = cfg.get("quotaMonitor") or {}
    try:
        return float(qm.get("disableThresholdPercent", 95))
    except Exception:
        return 95.0


def _maybe_auto_disable_by_headers(account_key: str, email: str,
                                   headers: dict, *, provider: str) -> None:
    """Anthropic 路径：用 is_window_exceeded 判断 + set_disabled_by_quota 触发。"""
    from . import oauth_manager
    from .anthropic.rate_limit_headers import (
        is_window_exceeded, _parse_reset_iso, H_5H_RESET, H_7D_RESET,
    )

    acc = oauth_manager.get_account(account_key)
    if acc is None:
        return
    # 已被禁用 → 不动（避免重复通知 / 覆盖已有 disabled_until）
    if acc.get("disabled_reason"):
        return

    hit_5h = is_window_exceeded(headers, "5h")
    hit_7d = is_window_exceeded(headers, "7d")
    if not (hit_5h or hit_7d):
        return

    # 撞哪个窗口锁哪个窗口：只在撞到的窗口里取 reset；两个都撞则取 max。
    # 不会出现「只 5h 撞了却用 7d reset 锁 7 天」的不合理情况。
    reset_5h = _parse_reset_iso(headers.get(H_5H_RESET)) if hit_5h else None
    reset_7d = _parse_reset_iso(headers.get(H_7D_RESET)) if hit_7d else None
    latest = reset_5h
    if reset_7d and (latest is None or reset_7d > latest):
        latest = reset_7d

    try:
        oauth_manager.set_disabled_by_quota(account_key, latest)
    except Exception as exc:
        print(f"[failover] auto-disable failed for {account_key}: {exc}")
        return

    # 发通知
    try:
        ek = notifier.escape_html
        windows = []
        if hit_5h: windows.append("5h")
        if hit_7d: windows.append("7d")
        notifier.notify_event(
            "quota_disabled",
            "⚠ <b>OAuth 配额已用尽（响应头实时触发）</b>\n"
            f"账号: <code>{ek(email)}</code> · 🅰 Claude\n"
            f"超限窗口: <code>{' / '.join(windows)}</code>\n"
            f"恢复时间: <code>{latest or 'unknown'}</code>\n"
            "达到该时间后由 quota_monitor 自动恢复。"
        )
    except Exception:
        pass


def _maybe_auto_disable_by_codex_snapshot(account_key: str, email: str,
                                          snap: dict) -> None:
    """OpenAI 路径：primary/secondary used_percent 任一 ≥ 阈值 → 禁用。"""
    from . import oauth_manager

    acc = oauth_manager.get_account(account_key)
    if acc is None or acc.get("disabled_reason"):
        return

    threshold = _get_quota_disable_threshold_pct()
    primary_pct = snap.get("primary_used_pct")
    secondary_pct = snap.get("secondary_used_pct")
    over_threshold = False
    over_windows = []
    if primary_pct is not None and primary_pct >= threshold:
        over_threshold = True
        over_windows.append(f"primary {primary_pct:.0f}%")
    if secondary_pct is not None and secondary_pct >= threshold:
        over_threshold = True
        over_windows.append(f"secondary {secondary_pct:.0f}%")
    if not over_threshold:
        return

    # 撞哪个窗口锁哪个窗口：只在实际超阈的窗口里取 reset_sec。
    # 不会出现「只 primary 撞了却用 secondary reset 锁到周末」的不合理情况。
    from datetime import datetime, timezone, timedelta
    reset_candidates = []
    _window_map = {
        "primary":   ("primary_used_pct",   "primary_reset_sec"),
        "secondary": ("secondary_used_pct", "secondary_reset_sec"),
    }
    for _name, (_pct_key, _sec_key) in _window_map.items():
        _pct = snap.get(_pct_key)
        if _pct is None or _pct < threshold:
            continue
        _sec = snap.get(_sec_key)
        if _sec is None:
            continue
        try:
            reset_candidates.append(
                datetime.now(timezone.utc) + timedelta(seconds=int(_sec))
            )
        except Exception:
            pass
    latest_iso = None
    if reset_candidates:
        latest = max(reset_candidates)
        latest_iso = latest.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        oauth_manager.set_disabled_by_quota(account_key, latest_iso)
    except Exception as exc:
        print(f"[failover] auto-disable (codex) failed for {account_key}: {exc}")
        return

    try:
        ek = notifier.escape_html
        notifier.notify_event(
            "quota_disabled",
            "⚠ <b>OAuth 配额已用尽（响应头实时触发）</b>\n"
            f"账号: <code>{ek(email)}</code> · 🅾 OpenAI\n"
            f"超限窗口: <code>{' / '.join(over_windows)}</code> "
            f"(阈值 {threshold:.0f}%)\n"
            f"恢复时间: <code>{latest_iso or 'unknown'}</code>"
        )
    except Exception:
        pass


def forget_codex_snapshot(account_key_or_email: str) -> None:
    """账户删除时清本地节流桶，避免内存无限累积。

    入参既接受 account_key (=provider:email)，也接受纯 email（兼容老调用）。
    统一把 account_key 里的 email 段拆出来清。
    """
    if not account_key_or_email:
        return
    key = account_key_or_email
    email = key.split(":", 1)[1] if ":" in key else key
    with _codex_snapshot_lock:
        _codex_snapshot_last.pop(email, None)
        _codex_snapshot_last.pop(key, None)


# ─── 协议相关工具集分派 ──────────────────────────────────────────
#
# 每个上游协议对应一组 (stream tracker 类, stream builder 类, first-event 解析器,
# 非流式 usage 提取函数, 非流式错误 JSON 识别器)。failover 按 ch.protocol 查表
# 选一组使用，避免在主流程里散落多处 `if protocol == ...`。

def _is_anthropic_error_json(obj: dict) -> bool:
    # anthropic 非流式响应格式：{"type":"error","error":{...}} 或嵌顶层 {"error":{...}}
    return obj.get("type") == "error" or isinstance(obj.get("error"), dict)


def _is_openai_error_json(obj: dict) -> bool:
    # OpenAI 家族错误格式：顶层 {"error":{"message":...,"type":...,...}}
    return isinstance(obj.get("error"), dict)


_UPSTREAM_TOOLKIT = {
    "anthropic": {
        "stream_tracker": upstream.SSEUsageTracker,
        "stream_builder": upstream.SSEAssistantBuilder,
        "first_event_parser": upstream.parse_first_sse_event,
        "extract_usage_json": upstream.extract_usage_from_json,
        "is_upstream_error_json": _is_anthropic_error_json,
    },
    "openai-chat": {
        "stream_tracker": upstream.ChatSSEUsageTracker,
        "stream_builder": upstream.ChatSSEAssistantBuilder,
        "first_event_parser": upstream.parse_first_chat_sse_event,
        "extract_usage_json": upstream.extract_usage_chat_json,
        "is_upstream_error_json": _is_openai_error_json,
    },
    "openai-responses": {
        "stream_tracker": upstream.ResponsesSSEUsageTracker,
        "stream_builder": upstream.ResponsesSSEAssistantBuilder,
        "first_event_parser": upstream.parse_first_responses_sse_event,
        "extract_usage_json": upstream.extract_usage_responses_json,
        "is_upstream_error_json": _is_openai_error_json,
    },
}


def _toolkit_for(ch: Channel) -> dict:
    proto = getattr(ch, "protocol", "anthropic")
    tk = _UPSTREAM_TOOLKIT.get(proto)
    if tk is None:
        # 未登记的 protocol 走哪套解析器都是错——宁可在日志里爆出来也不静默回退到
        # anthropic（曾遇到过的坑：回退后 SSE 解析 / 错误识别全部错位）。
        raise ValueError(
            f"no upstream toolkit registered for protocol {proto!r} "
            f"(channel={getattr(ch, 'key', '?')})"
        )
    return tk


# 错误 type：failover 内部统一用 anthropic 风味（errors.ErrType.*）。
# 在 emit 到下游之前，按 ingress_protocol 翻译成对应家族的 type。
_ERR_TYPE_ANTHROPIC_TO_OPENAI = {
    errors.ErrType.API: errors.ErrTypeOpenAI.SERVER,
    errors.ErrType.TIMEOUT: errors.ErrTypeOpenAI.TIMEOUT,
    errors.ErrType.RATE_LIMIT: errors.ErrTypeOpenAI.RATE_LIMIT,
    errors.ErrType.INVALID_REQUEST: errors.ErrTypeOpenAI.INVALID_REQUEST,
    errors.ErrType.AUTH: errors.ErrTypeOpenAI.AUTH,
    errors.ErrType.PERMISSION: errors.ErrTypeOpenAI.PERMISSION,
    errors.ErrType.NOT_FOUND: errors.ErrTypeOpenAI.NOT_FOUND,
    errors.ErrType.OVERLOADED: errors.ErrTypeOpenAI.SERVER,
    errors.ErrType.REQUEST_TOO_LARGE: errors.ErrTypeOpenAI.INVALID_REQUEST,
}


def _openai_prompt_cache_key_from_body(ingress_protocol: str, body: Optional[dict]) -> Optional[str]:
    """仅 OpenAI 协议使用的自动 prompt_cache_key 传递值。"""
    if ingress_protocol not in ("chat", "responses") or not isinstance(body, dict):
        return None
    val = str(body.get("prompt_cache_key") or "").strip()
    return val or None


def _write_affinity_non_stream(
    ingress_protocol: str,
    api_key_name: Optional[str],
    client_ip: str,
    messages: list,
    assistant_msg_anthropic: dict,
    body: Optional[dict],
    out_obj: dict,
    channel_key: str,
    resolved_model: str,
    client_key: Optional[str] = None,
) -> None:
    """成功完成非流式请求后按 ingress 走对应家族的 fingerprint_write。"""
    fp_write: Optional[str] = None
    if ingress_protocol == "anthropic":
        fp_write = fingerprint.fingerprint_write(
            api_key_name or "", client_ip or "", messages, assistant_msg_anthropic,
        )
    elif ingress_protocol == "chat":
        ds_choice = (out_obj.get("choices") or [{}])[0] if isinstance(out_obj, dict) else {}
        ds_msg = (ds_choice or {}).get("message") or {}
        fp_write = fingerprint.fingerprint_write_chat(
            api_key_name or "", client_ip or "",
            (body or {}).get("messages") or [], ds_msg,
        )
    elif ingress_protocol == "responses":
        ds_output = out_obj.get("output") or [] if isinstance(out_obj, dict) else []
        cur_input = _responses_current_input_items(body or {})
        fp_write = fingerprint.fingerprint_write_responses(
            api_key_name or "", client_ip or "", cur_input, ds_output,
        )
    if fp_write:
        affinity.upsert(
            fp_write, channel_key, resolved_model,
            prompt_cache_key=_openai_prompt_cache_key_from_body(ingress_protocol, body),
        )
    # 同步更新 client-level soft affinity
    if client_key:
        affinity.client_upsert(client_key, channel_key, resolved_model)


def _responses_current_input_items(body: dict) -> list:
    """延迟 import 的 responses_to_chat.resolve_current_input_items 代理，避免模块顶层循环。"""
    try:
        from .openai.transform.responses_to_chat import resolve_current_input_items
        return resolve_current_input_items(body)
    except Exception:
        return []


def _make_stream_translator(translator_ctx: Optional[dict]):
    """根据 translator_ctx 实例化跨变体流翻译器；非跨变体返回 None。

    translator_ctx 由 OpenAIApiChannel.build_upstream_request 填入。
    - response_translator=="chat_to_responses"：下游期待 chat，上游发 responses
      → 用 stream_r2c（responses SSE → chat SSE）
    - response_translator=="responses_to_chat"：下游期待 responses，上游发 chat
      → 用 stream_c2r（chat SSE → responses SSE）；translator 在 close() 时
      把翻译后的 response 写入 openai.store（Store 开启 + api_key_name 非空时）
    """
    if not isinstance(translator_ctx, dict):
        return None
    name = translator_ctx.get("response_translator")
    model = translator_ctx.get("model_for_response") or ""
    if name == "chat_to_responses":
        from .openai.transform.stream_r2c import StreamTranslator as _R2C
        return _R2C(model=model,
                    include_usage=bool(translator_ctx.get("include_usage", False)))
    if name == "responses_to_chat":
        from .openai.transform.stream_c2r import StreamTranslator as _C2R
        return _C2R(
            model=model,
            previous_response_id=translator_ctx.get("previous_response_id"),
            api_key_name=translator_ctx.get("api_key_name"),
            channel_key=translator_ctx.get("channel_key"),
            current_input_items=translator_ctx.get("current_input_items"),
        )
    return None


def _apply_non_stream_response_translator(obj: dict, translator_ctx: dict) -> dict:
    """跨变体非流式响应反向：对下游 JSON 做格式转换。

    `translator_ctx` 由 OpenAIApiChannel.build_upstream_request 填入；
    目前两个合法值：
      - "chat_to_responses"：上游 responses JSON → 下游 chat.completion JSON
      - "responses_to_chat"：上游 chat.completion JSON → 下游 responses JSON
    其他值原样返回。
    """
    if not isinstance(translator_ctx, dict):
        return obj
    name = translator_ctx.get("response_translator")
    model = translator_ctx.get("model_for_response") or ""
    if name == "chat_to_responses":
        from .openai.transform.chat_to_responses import translate_response as _t
        return _t(obj, model=model)
    if name == "responses_to_chat":
        from .openai.transform.responses_to_chat import translate_response as _t2
        return _t2(
            obj, model=model,
            previous_response_id=translator_ctx.get("previous_response_id"),
            api_key_name=translator_ctx.get("api_key_name"),
            channel_key=translator_ctx.get("channel_key"),
            current_input_items=translator_ctx.get("current_input_items"),
        )
    return obj


def _translate_err_type(anth_type: str, ingress: str) -> str:
    if ingress == "anthropic":
        return anth_type
    return _ERR_TYPE_ANTHROPIC_TO_OPENAI.get(anth_type, errors.ErrTypeOpenAI.API)


def _sse_error_for_ingress(ingress: str, anth_err_type: str, message: str) -> bytes:
    if ingress == "anthropic":
        return errors.sse_error_line(anth_err_type, message)
    mapped = _translate_err_type(anth_err_type, ingress)
    if ingress == "chat":
        return errors.sse_error_line_chat(mapped, message)
    return errors.sse_error_line_responses(mapped, message)


def _json_error_for_ingress(ingress: str, status: int, anth_err_type: str, message: str):
    if ingress == "anthropic":
        return errors.json_error_response(status, anth_err_type, message)
    mapped = _translate_err_type(anth_err_type, ingress)
    return errors.json_error_openai(status, mapped, message)


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
    "guard_error",       # 请求级 4xx：跨变体 guard 拒绝，与 ch 无关
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
    ingress_protocol: str = "anthropic",
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
    client_key = getattr(schedule_result, "client_key", None)

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
    last_ch_protocol: Optional[str] = None

    # 把 candidates 改成可扩展的 list（OAuth 刷 token 后重试同渠道）
    pending = list(candidates)  # 仍从首位取
    idx = 0
    attempt_order = 0
    # 并发饱和的候选：scheduler filter 挑出来的 + main loop 中竞态占满的
    saturated_extras: list[tuple[Channel, str]] = []

    while idx < len(pending):
        ch, resolved_model = pending[idx]
        attempt_order += 1
        last_ch_key, last_ch_type, last_model = ch.key, ch.type, resolved_model
        last_ch_protocol = getattr(ch, "protocol", "anthropic")

        # 并发 slot 获取（快速路径；filter 过但竞态满了 → 放到 saturated 备选）
        acquired = await concurrency.try_acquire(ch.key)
        if not acquired:
            # 竞态：filter 时还有位置，现在满了 → 作为排队备选
            # 注：_filter_candidates 已把饱和的挑走，这里主要兜底并发 filter 后瞬间占满的情况
            saturated_extras.append((ch, resolved_model))
            idx += 1
            continue

        attempt_id = log_db.record_retry_attempt(
            request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
        )

        release_done = False
        def _release_once(_key=ch.key):
            nonlocal release_done
            if release_done:
                return
            release_done = True
            concurrency.release(_key)

        try:
            result = await _try_channel(
                ch, resolved_model, body, is_stream, deadline_ts, start_time,
                fp_query, body.get("messages") or [], api_key_name, client_ip,
                request_id, retry_count, affinity_hit,
                ingress_protocol=ingress_protocol,
                client_key=client_key,
            )
        except BaseException:
            _release_once()
            raise
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
            # 并发 slot release 挂到响应体 finally：stream 消费完 / 客户端断开都会释放
            _attach_release_to_response(result.response, _release_once)
            return result.response
        # 非成功：立即释放 slot，进入下一候选
        _release_once()

        # 请求级 guard 错误：所有 openai 候选语义一致，切也无用，直接短路 4xx
        if result.outcome == "guard_error":
            status = int(result.http_status or 400)
            msg = result.error_detail or "request rejected by guard"
            # err_type 直接从 status 反推（保持与 classify_http_status 一致）
            anth_err_type = errors.classify_http_status(status)
            total_ms = int((time.time() - start_time) * 1000)
            await asyncio.to_thread(
                log_db.finish_error, request_id, msg[:4000], retry_count,
                final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                connect_ms=None, first_token_ms=None, total_ms=total_ms,
                http_status=status, affinity_hit=affinity_hit,
                upstream_protocol=getattr(ch, "protocol", "anthropic"),
            )
            return _json_error_for_ingress(ingress_protocol, status, anth_err_type, msg)

        # 未发首包失败：判断是否 OAuth 401/403 可刷一次
        if (
            ch.type == "oauth"
            and result.http_status in (401, 403)
            and ch.key not in refreshed_once
        ):
            refreshed_once.add(ch.key)
            ak = getattr(ch, "account_key", None) or getattr(ch, "email", "")
            try:
                await oauth_manager.force_refresh(ak)
                print(f"[failover] OAuth 401/403 on {ch.key}, refreshed; retrying same channel")
                retry_count += 1
                continue
            except Exception as exc:
                print(f"[failover] OAuth refresh failed for {ch.key}: {exc}")
                email = getattr(ch, "email", "?")
                try:
                    oauth_manager.set_enabled(ak, False, reason="auth_error")
                except Exception:
                    pass
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

    # 排队等位：pending 全部失败 / 全部饱和 → 汇总 saturated 候选去排队等任一空位
    # （scheduler 已挑出的 + main loop 竞态占满的）
    saturated_all: list[tuple[Channel, str]] = list(schedule_result.saturated) + saturated_extras
    # 去重：同 (ch.key, model) 保留首次出现，保持原优先级
    if saturated_all:
        seen = set()
        deduped: list[tuple[Channel, str]] = []
        for ch, m in saturated_all:
            k = (ch.key, m)
            if k in seen:
                continue
            seen.add(k)
            deduped.append((ch, m))
        saturated_all = deduped

    if saturated_all:
        cc_cfg = cfg.get("concurrency") or {}
        queue_wait_s = float(cc_cfg.get("queueWaitSeconds", 30))
        # 不能超过整体 deadline
        remaining_total = max(0.0, deadline_ts - time.time())
        queue_timeout = min(queue_wait_s, remaining_total)
        if queue_timeout > 0:
            candidate_keys = [(ch.key, (ch, m)) for ch, m in saturated_all]
            acquired = await concurrency.acquire_from_candidates(candidate_keys, queue_timeout)
            if acquired is not None:
                _ch_key, payload = acquired
                ch, resolved_model = payload  # type: ignore[assignment]
                attempt_order += 1
                last_ch_key, last_ch_type, last_model = ch.key, ch.type, resolved_model
                last_ch_protocol = getattr(ch, "protocol", "anthropic")

                attempt_id = log_db.record_retry_attempt(
                    request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
                )
                release_done2 = False
                def _release_q(_key=ch.key):
                    nonlocal release_done2
                    if release_done2:
                        return
                    release_done2 = True
                    concurrency.release(_key)
                try:
                    result = await _try_channel(
                        ch, resolved_model, body, is_stream, deadline_ts, start_time,
                        fp_query, body.get("messages") or [], api_key_name, client_ip,
                        request_id, retry_count, affinity_hit,
                        ingress_protocol=ingress_protocol,
                        client_key=client_key,
                    )
                except BaseException:
                    _release_q()
                    raise
                last_result = result
                log_db.update_retry_attempt(
                    attempt_id,
                    connect_ms=result.connect_ms, first_byte_ms=result.first_byte_ms,
                    ended_at=time.time(), outcome=result.outcome,
                    error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
                )
                if result.success or result.stream_started:
                    _attach_release_to_response(result.response, _release_q)
                    return result.response
                _release_q()
                # 排队拿到的这次也失败了 → 落入"全失败"分支
                if _should_cooldown(result.outcome):
                    cooldown.record_error(ch.key, resolved_model, result.error_detail)
                scorer.record_failure(ch.key, resolved_model, connect_ms=result.connect_ms)
                retry_count += 1
            else:
                # 队列超时 → 直接返回 429 rate_limit_error，不混入上游失败
                total_ms = int((time.time() - start_time) * 1000)
                queue_err_msg = (
                    f"All candidate channels saturated; queue wait {queue_wait_s:.0f}s timed out."
                )
                await asyncio.to_thread(
                    log_db.finish_error, request_id, queue_err_msg, retry_count,
                    final_channel_key=None, final_channel_type=None, final_model=None,
                    connect_ms=None, first_token_ms=None, total_ms=total_ms,
                    http_status=429, affinity_hit=affinity_hit,
                    upstream_protocol=None,
                )
                return _json_error_for_ingress(
                    ingress_protocol, 429, "rate_limit_error", queue_err_msg,
                )

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
        upstream_protocol=last_ch_protocol,
    )
    return _json_error_for_ingress(ingress_protocol, status, err_type, msg)


# ─── 并发 slot release 辅助 ──────────────────────────────────────

def _attach_release_to_response(response: Response, release_fn) -> None:
    """把 release_fn 挂到 StreamingResponse 的 body_iterator finally 上。

    - StreamingResponse：wrap body_iterator，async for 结束后（含 CancelledError）
      调 release_fn；这样客户端断开 / 流正常完成 / 异常 都会释放。
    - 非 StreamingResponse（JSONResponse 等）：立即调用 release_fn。
    """
    if not isinstance(response, StreamingResponse):
        try:
            release_fn()
        except Exception:
            pass
        return
    original = response.body_iterator

    async def _wrapped():
        try:
            async for chunk in original:
                yield chunk
        finally:
            try:
                release_fn()
            except Exception:
                pass

    response.body_iterator = _wrapped()


# ─── 单渠道尝试 ──────────────────────────────────────────────────

async def _try_channel(
    ch: Channel, resolved_model: str, body: dict,
    is_stream: bool, deadline_ts: float, start_time: float,
    fp_query: Optional[str], messages: list,
    api_key_name: Optional[str], client_ip: str,
    request_id: str, retry_count_so_far: int, affinity_hit: int,
    *, ingress_protocol: str = "anthropic",
    client_key: Optional[str] = None,
) -> AttemptResult:
    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    connect_timeout = int(timeouts.get("connect", 10))
    first_byte_timeout = int(timeouts.get("firstByte", 30))
    idle_timeout = int(timeouts.get("idle", 30))

    # 1. 构造上游请求
    try:
        upstream_req = await ch.build_upstream_request(
            body, resolved_model, ingress_protocol=ingress_protocol,
        )
    except Exception as exc:
        # GuardError（OpenAI 跨变体死角）带 .status / .err_type / .message 属性；
        # 视为"请求在当前 ch 不可服务"，短路到客户端的 4xx，不再切下一候选
        # （所有 openai 候选的 guard 语义一致；切了也同样失败）。
        if hasattr(exc, "status") and hasattr(exc, "err_type") and hasattr(exc, "message"):
            return AttemptResult(
                outcome="guard_error",
                error_detail=str(getattr(exc, "message", exc))[:2000],
                http_status=int(getattr(exc, "status", 400)),
            )
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

        # 1.5 响应头 snapshot 采样：成功/失败分支前都先记一次
        _maybe_record_codex_snapshot(ch, upstream_resp)
        _maybe_record_anthropic_snapshot(ch, upstream_resp)

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
                ingress_protocol=ingress_protocol,
                translator_ctx=upstream_req.translator_ctx,
                body=body,
                client_key=client_key,
            )

        # 4. 流式分支
        return await _consume_stream(
            ctx, upstream_resp, ch, resolved_model, dynamic_map,
            connect_ms, start_time, deadline_ts,
            first_byte_timeout, idle_timeout,
            request_id, messages, api_key_name, client_ip,
            fp_query, retry_count_so_far, affinity_hit,
            client_key=client_key,
            ingress_protocol=ingress_protocol,
            translator_ctx=upstream_req.translator_ctx,
            body=body,
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
    *, ingress_protocol: str = "anthropic",
    translator_ctx: Optional[dict] = None,
    body: Optional[dict] = None,
    client_key: Optional[str] = None,
) -> AttemptResult:
    # stream-only 上游分流：OpenAI OAuth (chatgpt.com/backend-api/codex) 只返回 SSE，
    # 下游若请求非流式，这里把 SSE 聚合成完整 JSON 再走原有 translator / 落库链路。
    if getattr(ch, "upstream_stream_only", False):
        return await _consume_stream_as_non_stream(
            ctx, upstream_resp, ch, resolved_model, dynamic_map,
            connect_ms, start_time, request_id,
            messages, api_key_name, client_ip,
            fp_query, retry_count_so_far, affinity_hit,
            ingress_protocol=ingress_protocol,
            translator_ctx=translator_ctx,
            body=body,
            client_key=client_key,
        )

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

    toolkit = _toolkit_for(ch)

    # 上游 error（按 ch.protocol 选识别器）
    if toolkit["is_upstream_error_json"](obj):
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
    usage = toolkit["extract_usage_json"](obj)
    # assistant_msg 仅给亲和 fingerprint_write 用，且目前 fingerprint_write 只支持
    # anthropic 家族；openai 的亲和由 MS-7 补上。这里保持 anthropic 形状即可。
    assistant_msg = {"role": obj.get("role", "assistant"), "content": obj.get("content") or []}

    scorer.record_success(
        ch.key, resolved_model,
        connect_ms=connect_ms, first_byte_ms=None, total_ms=total_ms,
    )
    cooldown.clear(ch.key, resolved_model)

    # 落库（用**上游原始响应体**，方便排错；翻译后的下游响应体由 JSONResponse 现场构造）
    await asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=connect_ms, first_token_ms=None, total_ms=total_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=restored.decode("utf-8", errors="replace") if isinstance(restored, bytes) else str(restored),
        http_status=upstream_resp.status_code,
        upstream_protocol=getattr(ch, "protocol", "anthropic"),
    )

    # 跨变体：把上游 JSON 反向成 ingress 期望的格式；同协议 translator_ctx=None 即原样
    out_obj = _apply_non_stream_response_translator(obj, translator_ctx or {})

    # 亲和写入（按 ingress 选 fingerprint_write 的参数空间与函数）
    _write_affinity_non_stream(ingress_protocol, api_key_name, client_ip,
                                messages, assistant_msg, body, out_obj,
                                ch.key, resolved_model,
                                client_key=client_key)

    response = JSONResponse(
        content=out_obj,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )
    return AttemptResult(
        outcome="success", success=True, response=response,
        connect_ms=connect_ms, total_ms=total_ms, http_status=upstream_resp.status_code,
        usage=usage, assistant_response=assistant_msg,
        full_response_text=restored.decode("utf-8", errors="replace") if isinstance(restored, bytes) else str(restored),
    )



# ─── Stream-only 上游 → 非流式聚合 ─────────────────────────────────

async def _consume_stream_as_non_stream(
    ctx, upstream_resp: httpx.Response, ch: Channel, resolved_model: str,
    dynamic_map: Optional[dict],
    connect_ms: int, start_time: float, request_id: str,
    messages: list, api_key_name: Optional[str], client_ip: str,
    fp_query: Optional[str], retry_count_so_far: int, affinity_hit: int,
    *, ingress_protocol: str = "anthropic",
    translator_ctx: Optional[dict] = None,
    body: Optional[dict] = None,
    client_key: Optional[str] = None,
) -> AttemptResult:
    """处理 upstream_stream_only=True 渠道的非流式下游请求。

    读取上游 SSE → 用 ResponsesSSEAssistantBuilder 聚合 → 构造成完整 /v1/responses
    JSON → 走与 _consume_non_stream 一致的 translator / 黑名单 / 落库 / 亲和链路。
    """
    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    total_timeout = int(timeouts.get("total", 600))
    first_byte_timeout = int(timeouts.get("firstByte", 30))
    idle_timeout = int(timeouts.get("idle", 30))
    deadline_ts = start_time + total_timeout

    # 上游是 openai-responses SSE（目前唯一 stream-only 渠道是 OpenAIOAuthChannel，
    # 其 protocol 固定为 "openai-responses"）
    assert getattr(ch, "protocol", "") == "openai-responses", \
        f"_consume_stream_as_non_stream only supports openai-responses upstream, got {getattr(ch, 'protocol', None)!r}"

    raw_buf = bytearray()
    aiter = upstream_resp.aiter_bytes()

    # 1) 首字节
    first_wait = min(first_byte_timeout, max(1, int(deadline_ts - time.time())))
    try:
        first_chunk = await asyncio.wait_for(aiter.__anext__(), timeout=first_wait)
    except asyncio.TimeoutError:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="first_byte_timeout", connect_ms=connect_ms,
            error_detail=f"first byte timeout (> {first_wait}s) [stream-only→non-stream]",
        )
    except StopAsyncIteration:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="closed_before_first_byte", connect_ms=connect_ms,
            error_detail="upstream closed stream before first byte [stream-only→non-stream]",
        )
    except Exception as exc:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="transport_error", connect_ms=connect_ms,
            error_detail=f"first byte transport: {exc} [stream-only→non-stream]",
        )

    first_byte_ms = int((time.time() - start_time) * 1000)
    if not first_chunk:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="closed_before_first_byte", connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail="upstream sent empty first chunk [stream-only→non-stream]",
        )

    # 2) 首包还原 + 错误检查（复用流式路径的 toolkit）
    first_chunk_restored = await ch.restore_response(first_chunk, dynamic_map=dynamic_map)
    toolkit = _toolkit_for(ch)

    first_event = toolkit["first_event_parser"](first_chunk_restored)
    if first_event and (
        first_event.get("type") == "error"
        or isinstance(first_event.get("error"), dict)
        or first_event.get("_event_name") == "error"
    ):
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="upstream_error_json",
            connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail=json.dumps(first_event.get("error", first_event), ensure_ascii=False)[:2000],
        )

    bl_hit = blacklist.match(first_chunk_restored, ch.key)
    if bl_hit:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="blacklist_hit",
            connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail=f"blacklist: {bl_hit}",
        )

    # 3) 读完剩余 chunk + 聚合
    builder = toolkit["stream_builder"]()  # ResponsesSSEAssistantBuilder
    tracker = toolkit["stream_tracker"]()  # Usage / 状态追踪
    builder.feed(first_chunk_restored)
    tracker.feed(first_chunk_restored)
    raw_buf.extend(first_chunk_restored if isinstance(first_chunk_restored, (bytes, bytearray)) else first_chunk_restored.encode("utf-8", errors="replace"))

    while True:
        now = time.time()
        if now >= deadline_ts:
            await _safe_exit(ctx)
            return AttemptResult(
                outcome="total_timeout",
                connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                error_detail=f"total timeout reading SSE (> {total_timeout}s) [stream-only→non-stream]",
            )
        wait_s = max(1, min(idle_timeout, int(deadline_ts - now)))
        try:
            chunk = await asyncio.wait_for(aiter.__anext__(), timeout=wait_s)
        except asyncio.TimeoutError:
            await _safe_exit(ctx)
            return AttemptResult(
                outcome="idle_timeout",
                connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                error_detail=f"idle timeout (> {idle_timeout}s) [stream-only→non-stream]",
            )
        except StopAsyncIteration:
            break
        except Exception as exc:
            await _safe_exit(ctx)
            return AttemptResult(
                outcome="transport_error",
                connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                error_detail=f"read SSE chunk: {exc} [stream-only→non-stream]",
            )
        if not chunk:
            continue
        restored_chunk = await ch.restore_response(chunk, dynamic_map=dynamic_map)
        builder.feed(restored_chunk)
        tracker.feed(restored_chunk)
        raw_buf.extend(restored_chunk if isinstance(restored_chunk, (bytes, bytearray)) else restored_chunk.encode("utf-8", errors="replace"))

    resp_headers = _pick_upstream_headers(upstream_resp)
    await _safe_exit(ctx)

    if not builder.has_any_event:
        return AttemptResult(
            outcome="upstream_malformed",
            connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail="stream ended without any SSE event [stream-only→non-stream]",
        )

    # 4) 聚合成完整 /v1/responses JSON
    obj = builder.to_full_json(fallback_model=resolved_model)

    # 把 tracker 收集到的 usage 合并进去（tracker 负责 responses.completed 的 usage 解析）
    try:
        usage_from_tracker = tracker.usage if hasattr(tracker, "usage") else None
        if usage_from_tracker:
            obj.setdefault("usage", usage_from_tracker)
    except Exception:
        pass

    total_ms = int((time.time() - start_time) * 1000)

    # 5) 用标准 extract_usage 抽 usage（对齐现有落库口径）
    usage = toolkit["extract_usage_json"](obj)
    assistant_msg = {"role": "assistant", "content": obj.get("output") or []}

    scorer.record_success(
        ch.key, resolved_model,
        connect_ms=connect_ms, first_byte_ms=first_byte_ms, total_ms=total_ms,
    )
    cooldown.clear(ch.key, resolved_model)

    response_body_text = bytes(raw_buf).decode("utf-8", errors="replace")
    await asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=response_body_text,
        http_status=upstream_resp.status_code,
        upstream_protocol=getattr(ch, "protocol", "anthropic"),
    )

    # 6) 走跨变体 translator（如果 ingress 是 chat，上游 responses JSON 要翻译成 chat.completion JSON）
    out_obj = _apply_non_stream_response_translator(obj, translator_ctx or {})

    # 亲和写入（与 _consume_non_stream 一致）
    _write_affinity_non_stream(ingress_protocol, api_key_name, client_ip,
                                messages, assistant_msg, body, out_obj,
                                ch.key, resolved_model,
                                client_key=client_key)

    response = JSONResponse(
        content=out_obj,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )
    return AttemptResult(
        outcome="success", success=True, response=response,
        connect_ms=connect_ms, first_byte_ms=first_byte_ms, total_ms=total_ms,
        http_status=upstream_resp.status_code,
        usage=usage, assistant_response=assistant_msg,
        full_response_text=response_body_text,
    )


# ─── 流式 ────────────────────────────────────────────────────────

async def _consume_stream(
    ctx, upstream_resp: httpx.Response, ch: Channel, resolved_model: str,
    dynamic_map: Optional[dict],
    connect_ms: int, start_time: float, deadline_ts: float,
    first_byte_timeout: int, idle_timeout: int,
    request_id: str, messages: list, api_key_name: Optional[str], client_ip: str,
    fp_query: Optional[str], retry_count_so_far: int, affinity_hit: int,
    *, ingress_protocol: str = "anthropic",
    translator_ctx: Optional[dict] = None,
    body: Optional[dict] = None,
    client_key: Optional[str] = None,
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

    toolkit = _toolkit_for(ch)

    # 2a) 首个 SSE event 是 error？（按 ch.protocol 选解析器 + 识别器）
    first_event = toolkit["first_event_parser"](first_chunk_restored)
    if first_event and (
        first_event.get("type") == "error"
        or isinstance(first_event.get("error"), dict)
        or first_event.get("_event_name") == "error"
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
    tracker = toolkit["stream_tracker"]()
    builder = toolkit["stream_builder"]()
    tracker.feed(first_chunk_restored)
    builder.feed(first_chunk_restored)
    # 跨变体：上游字节 → translator.feed → 下游字节；同协议 translator=None 原样 yield
    stream_translator = _make_stream_translator(translator_ctx)
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

        # 亲和写入：按 ingress 走对应家族的 fingerprint_write。
        # 4 种组合都覆盖：anthropic / 同协议 chat-chat / 同协议 resp-resp /
        # 跨变体 resp→chat / 跨变体 chat→resp。跨变体用对应 translator 累积的
        # 下游形状做 fingerprint_write，保证与下次请求的 fingerprint_query 同形。
        ch_proto = getattr(ch, "protocol", "anthropic")
        fp_write: Optional[str] = None
        if ingress_protocol == "anthropic":
            assistant_msg = builder.get_assistant()
            fp_write = fingerprint.fingerprint_write(
                api_key_name or "", client_ip or "", messages, assistant_msg,
            )
        elif ingress_protocol == "chat" and ch_proto == "openai-chat":
            assistant_msg = builder.get_assistant()
            fp_write = fingerprint.fingerprint_write_chat(
                api_key_name or "", client_ip or "",
                (body or {}).get("messages") or [], assistant_msg,
            )
        elif ingress_protocol == "chat" and ch_proto == "openai-responses":
            # stream_r2c translator 累积的下游 chat assistant 形状
            try:
                assistant_msg = (stream_translator.get_downstream_chat_assistant()
                                 if stream_translator else {"role": "assistant", "content": None})
            except Exception:
                assistant_msg = {"role": "assistant", "content": None}
            fp_write = fingerprint.fingerprint_write_chat(
                api_key_name or "", client_ip or "",
                (body or {}).get("messages") or [], assistant_msg,
            )
        elif ingress_protocol == "responses" and ch_proto == "openai-responses":
            # builder 是 ResponsesSSEAssistantBuilder
            output_items = builder.get_output_items() if hasattr(builder, "get_output_items") else []
            cur_input = _responses_current_input_items(body or {})
            fp_write = fingerprint.fingerprint_write_responses(
                api_key_name or "", client_ip or "", cur_input, output_items,
            )
        elif ingress_protocol == "responses" and ch_proto == "openai-chat":
            # stream_c2r translator._collect_output_items() 给出翻译后的下游 output items
            try:
                output_items = stream_translator._collect_output_items() if stream_translator else []
            except Exception:
                output_items = []
            cur_input = _responses_current_input_items(body or {})
            fp_write = fingerprint.fingerprint_write_responses(
                api_key_name or "", client_ip or "", cur_input, output_items,
            )
        if fp_write:
            affinity.upsert(
                fp_write, ch.key, resolved_model,
                prompt_cache_key=_openai_prompt_cache_key_from_body(ingress_protocol, body),
            )
        # 同步更新 client-level soft affinity
        if client_key:
            affinity.client_upsert(client_key, ch.key, resolved_model)

        # shield：客户端断开导致的 CancelledError 不应中断 DB 写入，否则
        # 日志会残留 pending。(参见 _finalize_client_cancelled 早退守卫)
        await asyncio.shield(asyncio.to_thread(
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
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
        ))

    async def _emit_error_and_finalize(err_type: str, message: str, outcome: str):
        if state["finalized"]:
            return
        state["finalized"] = True
        total_ms = int((time.time() - start_time) * 1000)

        # 已发首包的错误：视为"这一次失败"，记入 cooldown/scorer
        if _should_cooldown(outcome):
            cooldown.record_error(ch.key, resolved_model, message)
        scorer.record_failure(ch.key, resolved_model, connect_ms=connect_ms)

        await asyncio.shield(asyncio.to_thread(
            log_db.finish_error,
            request_id, message, retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
            http_status=upstream_status, affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(),
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
        ))

    async def _finalize_client_cancelled():
        """客户端断开：不计 cooldown/scorer，仅记日志便于审计。

        tracker.saw_stream_end=True 表示上游已送达收尾事件
        （anthropic message_stop / chat [DONE] or finish_reason / responses completed 等）。
        这种情况服务端视角已成功完成，client 只是没收完最后几帧就断，归 success。
        """
        if state["finalized"]:
            return
        if getattr(tracker, "saw_stream_end", False):
            await _finalize_success()
            return
        state["finalized"] = True
        total_ms = int((time.time() - start_time) * 1000)
        await asyncio.shield(asyncio.to_thread(
            log_db.finish_error,
            request_id, "client disconnected", retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
            http_status=upstream_status, affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(),
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
        ))

    async def stream_generator():
        """把首包 + 后续 chunk 转发给下游，同时在中途错误时用 SSE error event 收尾。"""
        if state["finalized"]:
            return
        try:
            # 首包
            if stream_translator is not None:
                for out in stream_translator.feed(first_chunk_restored):
                    yield out
            else:
                yield first_chunk_restored

            # 后续 chunk，带 idle / total 超时
            while True:
                remaining = _remaining_ms(deadline_ts)
                if remaining <= 0:
                    await _emit_error_and_finalize(
                        errors.ErrType.TIMEOUT,
                        f"upstream total timeout > {int((deadline_ts - start_time))}s",
                        outcome="total_timeout",
                    )
                    yield _sse_error_for_ingress(
                        ingress_protocol, errors.ErrType.TIMEOUT, "upstream total timeout"
                    )
                    return
                wait_sec = min(idle_timeout, max(1, remaining / 1000))
                try:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=wait_sec)
                except asyncio.TimeoutError:
                    if _remaining_ms(deadline_ts) <= 0:
                        await _emit_error_and_finalize(
                            errors.ErrType.TIMEOUT, "upstream total timeout",
                            outcome="total_timeout",
                        )
                        yield _sse_error_for_ingress(
                            ingress_protocol, errors.ErrType.TIMEOUT, "upstream total timeout"
                        )
                        return
                    await _emit_error_and_finalize(
                        errors.ErrType.TIMEOUT,
                        f"upstream idle timeout > {idle_timeout}s",
                        outcome="idle_timeout",
                    )
                    yield _sse_error_for_ingress(
                        ingress_protocol,
                        errors.ErrType.TIMEOUT,
                        f"upstream idle timeout > {idle_timeout}s",
                    )
                    return
                except StopAsyncIteration:
                    break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
                    await _emit_error_and_finalize(
                        "api_error", f"stream transport error: {exc}",
                        outcome="transport_error",
                    )
                    yield _sse_error_for_ingress(ingress_protocol, errors.ErrType.API,
                                                 f"stream transport error: {exc}")
                    return

                if not chunk:
                    continue
                restored = await ch.restore_response(chunk, dynamic_map=dynamic_map)
                tracker.feed(restored)
                builder.feed(restored)
                if stream_translator is not None:
                    for out in stream_translator.feed(restored):
                        yield out
                else:
                    yield restored

            # 上游已正常收尾 → 先落库成功，再 yield 翻译器收尾帧。
            # 若放到后面，客户端在 yield 期间断开会让 CancelledError 抢先触发
            # _finalize_client_cancelled，日志被错误地标记为 "client disconnected"。
            await _finalize_success()
            if stream_translator is not None:
                for out in stream_translator.close():
                    yield out
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
