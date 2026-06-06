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
import socket
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional, Any
from urllib.parse import urlparse

import httpx
import websockets
from fastapi.responses import JSONResponse, Response, StreamingResponse
from websockets.exceptions import InvalidStatus

import threading

from . import (
    affinity, blacklist, concurrency, config, cooldown, errors, fingerprint,
    log_db, notifier, oauth_manager, scorer, state_db, upstream,
)
from .channel.base import Channel
from .channel.openai_oauth_channel import OpenAIOAuthChannel
from .transform import cc_mimicry
from .openai.transform import common as openai_common
from .openai.transform import codex_oauth_transform
from .openai.codex_identity_confuse import (
    ConfuseState,
    confuse_client_metadata,
    confuse_headers as confuse_identity_headers,
    expose_response_payload,
)
from .proxy.connector import DirectConnector, SOCKS5Connector, SS2022Connector
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


def _maybe_record_codex_snapshot(ch: Channel, resp: Any) -> None:
    if not isinstance(ch, OpenAIOAuthChannel):
        return
    try:
        snap = openai_provider.parse_rate_limit_headers(dict(resp.headers))
        if not snap:
            return
        account_key = getattr(ch, "account_key", None) or ch.email
        email = ch.email
        # throttle bucket 用 account_key 作 key；OpenAI 同一邮箱可能有多个
        # workspace，不能按 email 合并。
        now = time.time()
        with _codex_snapshot_lock:
            last = _codex_snapshot_last.get(account_key, 0.0)
            if now - last < _CODEX_SNAPSHOT_WRITE_INTERVAL_S:
                return
            _codex_snapshot_last[account_key] = now
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
    email = oauth_manager.account_key_to_email(key) if ":" in key else key
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
        _acc = oauth_manager.get_account(account_key)
        _plan = oauth_manager.claude_plan_label(_acc) if _acc else ""
        _plan_tag = f" · {ek(_plan)}" if _plan else ""
        notifier.notify_event(
            "quota_disabled",
            "⚠ <b>OAuth 配额已用尽（响应头实时触发）</b>\n"
            f"账号: <code>{ek(email)}</code>\n"
            f"🅰️ Claude{_plan_tag}\n"
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
        _acc = oauth_manager.get_account(account_key)
        _plan = (_acc.get("plan_type") or "") if _acc else ""
        _plan_tag = f" · {ek(_plan)}" if _plan else ""
        notifier.notify_event(
            "quota_disabled",
            "⚠ <b>OAuth 配额已用尽（响应头实时触发）</b>\n"
            f"账号: <code>{ek(email)}</code>\n"
            f"🅾️ OpenAI{_plan_tag}\n"
            f"超限窗口: <code>{' / '.join(over_windows)}</code> "
            f"(阈值 {threshold:.0f}%)\n"
            f"恢复时间: <code>{latest_iso or 'unknown'}</code>"
        )
    except Exception:
        pass


def forget_codex_snapshot(account_key_or_email: str) -> None:
    """账户删除时清本地节流桶，避免内存无限累积。

    入参既接受 account_key，也接受纯 email（兼容老调用）。OpenAI 新 key
    是 workspace identity；同时清 email 与 key 两种历史桶。
    """
    if not account_key_or_email:
        return
    key = account_key_or_email
    email = oauth_manager.account_key_to_email(key) if ":" in key else key
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
    proxy_name: Optional[str] = None
    proxy_bytes_up: int = 0
    proxy_bytes_down: int = 0
    # HTTP /v1/responses 内部转 WS 时需要继续带上 OAuth channel 构造出的
    # translator_ctx，保证日志 / Store / affinity 口径与 HTTP/SSE 路径一致。
    translator_ctx: Optional[dict] = None


_OUTCOMES_NO_COOLDOWN = {
    "success",
    "http_auth_error",   # 先刷 token 再判
    "transform_error",   # 代理自己 bug，和上游无关
    "guard_error",       # 请求级 4xx：跨变体 guard 拒绝，与 ch 无关
}


def _should_cooldown(outcome: str) -> bool:
    return outcome not in _OUTCOMES_NO_COOLDOWN


def _is_context_1m_credit_error(result: AttemptResult, resolved_model: str, body: dict) -> bool:
    """Context 1M entitlement 不足：不是渠道故障，去掉 context-1m 同渠道重试一次。

    Anthropic 对缺少 usage credits / group credit 的 long-context 请求会返回
    `429 Usage credits are required for long context requests.`。这类错误如果按普通
    http_error 记 scorer/cooldown，会很快把健康渠道打坏；正确处理是对所有支持
    context-1m 的模型（Opus 4.x / Sonnet 4.5+），关闭 1M 后同渠道重试一次。仅对
    本次确实启用了 context-1m 的请求，关闭 1M 后同渠道重试一次。
    """
    if result.http_status != 429:
        return False
    if result.outcome not in ("http_error", "upstream_error_json"):
        return False
    if "usage credits are required for long context requests" not in (result.error_detail or "").lower():
        return False
    if not cc_mimicry.model_supports_context_1m(resolved_model):
        return False
    # 只处理本次明确带/要求 1M 的请求，避免误吞普通 429。
    if body.get(cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY) is True:
        return True
    return cc_mimicry.request_wants_context_1m(
        body,
        downstream_betas=body.get(cc_mimicry.PARROT_DOWNSTREAM_BETAS_KEY),
        original_model=body.get(cc_mimicry.PARROT_ORIGINAL_MODEL_KEY),
        resolved_model=resolved_model,
    )


def _retry_body_without_context_1m(body: dict) -> dict:
    retry_body = dict(body)
    retry_body[cc_mimicry.PARROT_WANTS_CONTEXT_1M_KEY] = False
    return retry_body


def _proxy_route_kwargs(ch: Channel, resolved_model: str) -> dict:
    """Build proxy routing context for a channel/model attempt.

    Routing priority is account = channel > model > function family > default.
    The family-level function route is the default outlet for every request in
    that provider family, including OAuth login/refresh/probes and normal
    channel requests.
    """
    proto = getattr(ch, "protocol", "anthropic") or "anthropic"
    purpose = "oauth_anthropic" if proto == "anthropic" else "oauth_openai"
    return {
        "channel_key": ch.key,
        "model": resolved_model,
        "purpose": purpose,
        "account_key": getattr(ch, "account_key", "") or "",
    }


def _pick_non_direct_proxy_name(ch: Channel, resolved_model: str) -> str | None:
    try:
        from .proxy import manager as pm
        pm.init()
        target = pm.resolve_proxy_target(**_proxy_route_kwargs(ch, resolved_model))
        for name in pm.expand_target(target):
            conn = pm.get_connector(name)
            if conn is not None and conn.type != "direct":
                return name
    except Exception:
        pass
    return None


def _proxy_byte_snapshot(proxy_bytes: Optional[dict]) -> tuple[int, int]:
    if not proxy_bytes:
        return 0, 0
    return int(proxy_bytes.get("up") or 0), int(proxy_bytes.get("down") or 0)


def _responses_upstream_ws_enabled(cfg: Optional[dict] = None) -> bool:
    """HTTP /v1/responses 是否启用 OAuth Codex WS 上游传输。

    下游 WebSocket /v1/responses 入口独立于此配置；这里仅控制普通
    HTTP/SSE Responses 请求在选中 OpenAI OAuth Codex 渠道时是否把内部
    upstream transport 从 HTTP/SSE 切到 Responses WebSocket。
    """
    c = cfg or config.get()
    openai_cfg = c.get("openai") or {}
    if "responsesUpstreamWsForOAuth" in openai_cfg:
        return bool(openai_cfg.get("responsesUpstreamWsForOAuth"))
    # 兼容内测旧 key；主配置语义固定为 boolean responsesUpstreamWsForOAuth。
    transport = str(openai_cfg.get("responsesUpstreamTransport") or "").strip().lower()
    if transport in ("ws", "websocket", "websockets"):
        return True
    if "responsesUpstreamWs" in openai_cfg:
        return bool(openai_cfg.get("responsesUpstreamWs"))
    return False


def _should_use_responses_upstream_ws(
    ch: Channel,
    *,
    ingress_protocol: str,
    cfg: Optional[dict] = None,
) -> bool:
    if ingress_protocol != "responses":
        return False
    if not isinstance(ch, OpenAIOAuthChannel):
        return False
    if getattr(ch, "protocol", "") != "openai-responses":
        return False
    return _responses_upstream_ws_enabled(cfg)


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
    retried_without_context_1m: set[tuple[str, str]] = set()
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

        # Resolve proxy for this attempt (read from proxy manager)
        _attempt_proxy: str | None = _pick_non_direct_proxy_name(ch, resolved_model)

        attempt_id = log_db.record_retry_attempt(
            request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
            proxy_name=_attempt_proxy,
        )
        if _attempt_proxy:
            log_db.update_pending(request_id, proxy_name=_attempt_proxy)

        release_done = False
        def _release_once(_key=ch.key):
            nonlocal release_done
            if release_done:
                return
            release_done = True
            concurrency.release(_key)

        try:
            if _should_use_responses_upstream_ws(ch, ingress_protocol=ingress_protocol, cfg=cfg):
                result = await _try_openai_oauth_responses_ws_channel(
                    ch, resolved_model, body, is_stream, deadline_ts, start_time,
                    fp_query, body.get("messages") or [], api_key_name, client_ip,
                    request_id, retry_count, affinity_hit, client_key=client_key,
                    retry_attempt_id=attempt_id,
                )
            else:
                result = await _try_channel(
                    ch, resolved_model, body, is_stream, deadline_ts, start_time,
                    fp_query, body.get("messages") or [], api_key_name, client_ip,
                    request_id, retry_count, affinity_hit,
                    ingress_protocol=ingress_protocol,
                    client_key=client_key,
                    retry_attempt_id=attempt_id,
                )
        except BaseException:
            _release_once()
            raise
        last_result = result
        if _attempt_proxy and not result.proxy_name:
            result.proxy_name = _attempt_proxy

        log_db.update_retry_attempt(
            attempt_id,
            connect_ms=result.connect_ms, first_byte_ms=result.first_byte_ms,
            ended_at=time.time(), outcome=result.outcome,
            error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
            proxy_name=result.proxy_name,
            bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
            bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
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

        # Sonnet 1M entitlement 不足不是渠道故障：同渠道去掉 context-1m 重试一次，
        # 避免显式 1M 下游持续请求时把健康渠道打进 cooldown/禁用。
        context_retry_key = (ch.key, resolved_model)
        if (
            _is_context_1m_credit_error(result, resolved_model, body)
            and context_retry_key not in retried_without_context_1m
        ):
            retried_without_context_1m.add(context_retry_key)
            body = _retry_body_without_context_1m(body)
            print(f"[failover] context-1m rejected for {ch.key}/{resolved_model}; retrying same channel without context-1m")
            retry_count += 1
            continue

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
            candidate_keys: list[tuple[str, object]] = [(ch.key, (ch, m)) for ch, m in saturated_all]
            acquired = await concurrency.acquire_from_candidates(candidate_keys, queue_timeout)
            if acquired is not None:
                _ch_key, payload = acquired
                ch, resolved_model = payload  # type: ignore[assignment]
                attempt_order += 1
                last_ch_key, last_ch_type, last_model = ch.key, ch.type, resolved_model
                last_ch_protocol = getattr(ch, "protocol", "anthropic")

                _attempt_proxy2: str | None = _pick_non_direct_proxy_name(ch, resolved_model)
                attempt_id = log_db.record_retry_attempt(
                    request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
                    proxy_name=_attempt_proxy2,
                )
                release_done2 = False
                def _release_q(_key=ch.key):
                    nonlocal release_done2
                    if release_done2:
                        return
                    release_done2 = True
                    concurrency.release(_key)
                try:
                    if _should_use_responses_upstream_ws(ch, ingress_protocol=ingress_protocol, cfg=cfg):
                        result = await _try_openai_oauth_responses_ws_channel(
                            ch, resolved_model, body, is_stream, deadline_ts, start_time,
                            fp_query, body.get("messages") or [], api_key_name, client_ip,
                            request_id, retry_count, affinity_hit, client_key=client_key,
                            retry_attempt_id=attempt_id,
                        )
                    else:
                        result = await _try_channel(
                            ch, resolved_model, body, is_stream, deadline_ts, start_time,
                            fp_query, body.get("messages") or [], api_key_name, client_ip,
                            request_id, retry_count, affinity_hit,
                            ingress_protocol=ingress_protocol,
                            client_key=client_key,
                            retry_attempt_id=attempt_id,
                        )
                except BaseException:
                    _release_q()
                    raise
                last_result = result
                if _attempt_proxy2 and not result.proxy_name:
                    result.proxy_name = _attempt_proxy2
                log_db.update_retry_attempt(
                    attempt_id,
                    connect_ms=result.connect_ms, first_byte_ms=result.first_byte_ms,
                    ended_at=time.time(), outcome=result.outcome,
                    error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
                    proxy_name=result.proxy_name,
                    bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
                    bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
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
        proxy_name=(last_result.proxy_name if last_result else None),
        proxy_bytes_up=(last_result.proxy_bytes_up if last_result else None),
        proxy_bytes_down=(last_result.proxy_bytes_down if last_result else None),
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


# ─── OpenAI OAuth Responses: HTTP ingress → WS upstream ───────────────

from .openai.codex_constants import (
    CODEX_CLI_VERSION as _CODEX_CLI_VERSION,
    CODEX_CLI_USER_AGENT as _CODEX_CLI_UA,
    CODEX_ORIGINATOR as _CODEX_ORIGINATOR,
    RESPONSES_WEBSOCKETS_BETA as _RESPONSES_WEBSOCKETS_BETA,
)

_SKIP_WS_HEADERS = {
    "host", "connection", "upgrade", "sec-websocket-key", "sec-websocket-version",
    "sec-websocket-extensions", "sec-websocket-protocol", "content-length",
    "accept-encoding", "openai-beta",
}


@dataclass
class _WsProxyBytes:
    up: int = 0
    down: int = 0

    def count(self, up: int = 0, down: int = 0) -> None:
        self.up += int(up or 0)
        self.down += int(down or 0)


def _http_url_to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _socks5h_url(url: str) -> str:
    if url.startswith("socks5://"):
        return "socks5h://" + url[len("socks5://"):]
    return url


def _ws_proxy_snapshot(proxy_bytes: _WsProxyBytes) -> tuple[int, int]:
    return int(proxy_bytes.up or 0), int(proxy_bytes.down or 0)


def _drop_headers_case_insensitive(headers: dict[str, str], names: set[str]) -> dict[str, str]:
    drop = {n.lower() for n in names}
    return {k: v for k, v in (headers or {}).items() if str(k).lower() not in drop}


def _get_header_case_insensitive(headers: dict[str, str] | None, key: str) -> str:
    if not headers:
        return ""
    for k, v in headers.items():
        if str(k).lower() == key.lower():
            return str(v)
    return ""


def _merge_oauth_responses_ws_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk in _SKIP_WS_HEADERS:
            continue
        out[str(k)] = str(v)
    out["OpenAI-Beta"] = _RESPONSES_WEBSOCKETS_BETA
    out.setdefault("originator", _CODEX_ORIGINATOR)
    out.setdefault("version", _CODEX_CLI_VERSION)
    # httpx 请求头可能已有 lower-case user-agent；WS 只保留一个 canonical User-Agent。
    out = _drop_headers_case_insensitive(out, {"user-agent"})
    out["User-Agent"] = _CODEX_CLI_UA

    sid = out.get("session-id") or out.get("session_id")
    tid = out.get("thread-id") or sid
    if sid:
        out.setdefault("session-id", sid)
    if tid:
        out.setdefault("thread-id", tid)
        out.setdefault("x-client-request-id", tid)
    # Codex CLI only sends session-id / thread-id (hyphenated).
    # Remove legacy underscore / conversation variants.
    out = _drop_headers_case_insensitive(out, {"session_id", "conversation_id", "conversation-id"})
    return out


def _ensure_oauth_responses_ws_session_headers(
    headers: dict[str, str],
    body: dict,
) -> None:
    """Populate Codex WS session/thread headers from prompt_cache_key.

    OpenAIOAuthChannel derives HTTP session_id from the transformed Codex
    payload. That transform strips prompt_cache_key for ChatGPT internal API;
    when we switch HTTP Responses ingress to WS upstream, derive the same
    isolated identity from the original request body as a fallback.
    """
    sid = headers.get("session-id") or headers.get("session_id")
    tid = headers.get("thread-id") or sid
    if not sid:
        try:
            from .channel.openai_oauth_channel import _isolate_session_id
            api_key_name = str((body or {}).get("_api_key_name") or "")
            raw_anchor = str((body or {}).get("prompt_cache_key") or "").strip()
            if api_key_name and raw_anchor:
                sid = _isolate_session_id(api_key_name, raw_anchor)
                tid = sid
        except Exception:
            pass
    if sid:
        headers.setdefault("session-id", sid)
    if tid:
        headers.setdefault("thread-id", tid)
        headers.setdefault("x-client-request-id", tid)
    # Codex CLI only sends session-id / thread-id (hyphenated).
    # Remove legacy underscore / conversation variants.
    for _ck in [k for k in list(headers) if str(k).lower() in ("session_id", "conversation_id", "conversation-id")]:
        del headers[_ck]


def _capture_reasoning_items(body: Optional[dict], resolved_model: str,
                             output_items: list) -> None:
    """v3 捕获：把上游本轮整批 output 按 session_key 存入 reasoning_store。

    从 body 派生 session_key（api_key_name + prompt_cache_key），与回填同源。
    任何异常吞掉，绝不影响主响应流。
    """
    if not body or not isinstance(output_items, list):
        return
    try:
        ak = str((body or {}).get("_api_key_name") or "")
        pck = str((body or {}).get("prompt_cache_key") or "").strip()
        if not pck:
            return
        from .openai.reasoning_store import make_session_key, save_items
        sk = make_session_key(ak, pck)
        if not sk:
            return
        save_items(sk, str(resolved_model or ""), output_items)
    except Exception:
        pass


def _maybe_invalidate_reasoning_on_error(body: Optional[dict], resolved_model: str,
                                         error_detail: Optional[str]) -> None:
    """上游拒绝加密推理块时清该会话缓存。判据：错误信息含 encrypted_content
    或 reasoning 签名失效关键词。任何异常吞掉。"""
    if not body or not error_detail:
        return
    low = str(error_detail).lower()
    if not any(k in low for k in (
            "invalid_encrypted_content", "encrypted_content",
            "reasoning", "thinking_signature", "signature")):
        return
    try:
        ak = str((body or {}).get("_api_key_name") or "")
        pck = str((body or {}).get("prompt_cache_key") or "").strip()
        if not pck:
            return
        from .openai.reasoning_store import make_session_key, invalidate
        sk = make_session_key(ak, pck)
        if sk:
            invalidate(sk, str(resolved_model or ""))
    except Exception:
        pass


def _build_oauth_responses_ws_frame(body: dict, resolved_model: str) -> dict:
    payload = openai_common.filter_responses_passthrough(body)
    payload["model"] = resolved_model
    # v3 回填：WS 是 OAuth Codex 的真实主路径（responsesUpstreamWsForOAuth=True），
    # 必须在这里派生 session_key 传给 transform，否则回填只在 HTTP 路径生效、WS 漏补。
    _rs_session_key = None
    try:
        _ak = str((body or {}).get("_api_key_name") or "")
        _pck = str(payload.get("prompt_cache_key") or "").strip()
        if _pck:
            from .openai.reasoning_store import make_session_key
            _rs_session_key = make_session_key(_ak, _pck)
    except Exception:
        _rs_session_key = None
    payload = codex_oauth_transform.apply_codex_oauth_transform(
        payload, resolved_model=resolved_model,
        session_key=_rs_session_key,
    )
    payload["type"] = "response.create"
    return payload


def _ws_route_kwargs(ch: Channel, resolved_model: str) -> dict:
    return {
        "channel_key": ch.key,
        "model": resolved_model,
        "purpose": "oauth_openai",
        "account_key": getattr(ch, "account_key", "") or "",
    }


def _resolve_ws_route_chain_for_channel(ch: Channel, resolved_model: str) -> list[tuple[str, Any | None]]:
    try:
        from .proxy import manager as pm
        pm.init()
        if pm.is_configured():
            out: list[tuple[str, Any | None]] = []
            valid_seen = False
            for name in pm.resolve_proxy_chain(**_ws_route_kwargs(ch, resolved_model)):
                conn = pm.get_connector(name)
                if conn is None:
                    continue
                valid_seen = True
                if getattr(conn, "type", "") == "direct":
                    out.append(("direct", None))
                else:
                    out.append((name, conn))
            if valid_seen:
                return out
            return []
    except Exception:
        pass
    legacy = _legacy_socks5_connector()
    if legacy is not None:
        return [("legacy-socks5", legacy)]
    return [("direct", None)]


def _legacy_socks5_connector() -> SOCKS5Connector | None:
    try:
        from . import network as _network
        url = _network.active_socks5_url()
    except Exception:
        url = None
    if not url:
        return None
    return SOCKS5Connector("legacy-socks5", url)


class _ManagedWsConnection:
    """WebSocket connection with an attached async cleanup hook.

    websockets takes ownership of a socket passed via ``sock=`` but knows
    nothing about the SS2022<->socketpair pump tasks feeding that socket.  If
    those tasks are left fire-and-forget, closed file descriptors can remain
    registered in the event loop and later WS connects become flaky.  This thin
    proxy preserves the websocket API while making close() also tear down the
    bridge deterministically.
    """

    def __init__(self, ws, cleanup):
        self._ws = ws
        self._cleanup = cleanup
        self._cleanup_done = False

    def __getattr__(self, name: str):
        return getattr(self._ws, name)

    async def close(self, *args, **kwargs):
        try:
            return await self._ws.close(*args, **kwargs)
        finally:
            if not self._cleanup_done:
                self._cleanup_done = True
                await self._cleanup()


async def _open_socket_via_ss2022(
    url: str,
    connector: SS2022Connector,
    proxy_bytes: _WsProxyBytes,
    *,
    timeout: float,
):
    p = urlparse(url)
    host = p.hostname
    if not host:
        raise OSError("websocket URL missing host")
    port = p.port or (443 if p.scheme == "wss" else 80)

    from .proxy.ss2022 import SS2022Connection

    conn = SS2022Connection(connector.cipher, connector.password, connector.server, connector.port)
    await conn.connect(host, port, timeout=timeout)

    loop = asyncio.get_running_loop()
    left, right = socket.socketpair()
    left.setblocking(False)
    right.setblocking(False)
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    cleanup_lock = asyncio.Lock()
    cleanup_done = False

    def shutdown_sock(sock) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    async def pump_sock_to_ss() -> None:
        try:
            while not stop.is_set():
                data = await loop.sock_recv(right, 65536)
                if not data:
                    return
                proxy_bytes.count(up=len(data))
                await conn.write(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            stop.set()

    async def pump_ss_to_sock() -> None:
        try:
            while not stop.is_set():
                data = await conn.read(65536)
                if not data:
                    return
                proxy_bytes.count(down=len(data))
                await loop.sock_sendall(right, data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            stop.set()
            # Wake the websocket side if upstream closes first.  Full async
            # cleanup is owned by the wrapper close() below; don't await in a
            # coroutine finalizer (prevents "coroutine ignored GeneratorExit").
            shutdown_sock(right)

    async def cleanup() -> None:
        nonlocal cleanup_done
        async with cleanup_lock:
            if cleanup_done:
                return
            cleanup_done = True
            stop.set()
            for task in tasks:
                task.cancel()
            shutdown_sock(right)
            shutdown_sock(left)
            try:
                await conn.close()
            except Exception:
                pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    tasks.append(asyncio.create_task(pump_sock_to_ss()))
    tasks.append(asyncio.create_task(pump_ss_to_sock()))
    return left, cleanup


async def _connect_oauth_responses_ws(
    url: str,
    *,
    headers: dict[str, str],
    connector,
    proxy_bytes: _WsProxyBytes,
    open_timeout: float,
):
    kwargs = dict(
        additional_headers=headers,
        user_agent_header=None,
        open_timeout=open_timeout,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=None,
        max_queue=64,
        compression="deflate",
    )
    if connector is None or isinstance(connector, DirectConnector):
        return await websockets.connect(url, proxy=None, **kwargs)
    if isinstance(connector, SOCKS5Connector):
        return await websockets.connect(url, proxy=_socks5h_url(connector.url), **kwargs)
    if isinstance(connector, SS2022Connector):
        sock, cleanup = await _open_socket_via_ss2022(url, connector, proxy_bytes, timeout=open_timeout)
        try:
            ws = await websockets.connect(url, proxy=None, sock=sock, **kwargs)
        except BaseException:
            await cleanup()
            raise
        return _ManagedWsConnection(ws, cleanup)
    return await websockets.connect(url, proxy=None, **kwargs)


def _maybe_record_codex_ws_snapshot(ch: Channel, ws_response: Any) -> None:
    if not isinstance(ch, OpenAIOAuthChannel) or ws_response is None:
        return
    try:
        headers_obj = getattr(ws_response, "headers", None)
        if not headers_obj:
            return
        headers = {str(k): str(v) for k, v in headers_obj.items()}
        fake_resp = type("_WsResp", (), {"headers": headers})()
        _maybe_record_codex_snapshot(ch, fake_resp)
    except Exception as exc:
        print(f"[failover] codex WS snapshot record failed for {getattr(ch, 'email', '?')}: {exc}")


def _maybe_record_codex_rate_limits_event(ch: Channel | None, event: dict) -> None:
    """Consume Codex WS rate-limit events internally; never forward to HTTP clients."""
    if not isinstance(ch, OpenAIOAuthChannel) or not isinstance(event, dict):
        return
    candidates = []
    for key in ("rate_limits", "rateLimits", "limits", "headers", "data"):
        val = event.get(key)
        if isinstance(val, dict):
            candidates.append(val)
    candidates.append(event)
    try:
        for src in candidates:
            flat = {str(k).lower(): v for k, v in src.items()}
            headerish = {
                "x-codex-primary-used-percent": flat.get("x-codex-primary-used-percent") or flat.get("primary_used_pct") or flat.get("primary_used_percent"),
                "x-codex-primary-reset-after-seconds": flat.get("x-codex-primary-reset-after-seconds") or flat.get("primary_reset_sec") or flat.get("primary_reset_after_seconds"),
                "x-codex-primary-window-minutes": flat.get("x-codex-primary-window-minutes") or flat.get("primary_window_min") or flat.get("primary_window_minutes"),
                "x-codex-secondary-used-percent": flat.get("x-codex-secondary-used-percent") or flat.get("secondary_used_pct") or flat.get("secondary_used_percent"),
                "x-codex-secondary-reset-after-seconds": flat.get("x-codex-secondary-reset-after-seconds") or flat.get("secondary_reset_sec") or flat.get("secondary_reset_after_seconds"),
                "x-codex-secondary-window-minutes": flat.get("x-codex-secondary-window-minutes") or flat.get("secondary_window_min") or flat.get("secondary_window_minutes"),
                "x-codex-primary-over-secondary-limit-percent": flat.get("x-codex-primary-over-secondary-limit-percent") or flat.get("primary_over_secondary_pct") or flat.get("primary_over_secondary_limit_percent"),
            }
            snap = openai_provider.parse_rate_limit_headers(headerish)
            if snap:
                fake_resp = type("_WsRateLimitsResp", (), {"headers": headerish})()
                _maybe_record_codex_snapshot(ch, fake_resp)
                return
    except Exception as exc:
        print(f"[failover] codex WS rate_limits event record failed for {getattr(ch, 'email', '?')}: {exc}")


def _frame_size(data: str | bytes) -> int:
    if isinstance(data, bytes):
        return len(data)
    return len(data.encode("utf-8", errors="replace"))


def _ws_event_type(data: str | bytes) -> str:
    if not isinstance(data, str):
        return ""
    try:
        obj = json.loads(data)
    except Exception:
        return ""
    return str(obj.get("type") or "") if isinstance(obj, dict) else ""


def _ws_error_detail(data: str | bytes) -> tuple[Optional[int], str]:
    if not isinstance(data, str):
        return None, "upstream websocket error"
    try:
        obj = json.loads(data)
    except Exception:
        return None, data[:2000]
    if not isinstance(obj, dict):
        return None, str(obj)[:2000]
    err: Any = obj.get("error")
    if isinstance(obj.get("response"), dict) and isinstance(obj["response"].get("error"), dict):
        err = obj["response"]["error"]
    if isinstance(err, dict):
        status = err.get("status") or obj.get("status")
        try:
            status_i = int(status) if status is not None else None
        except Exception:
            status_i = None
        code = err.get("code") or err.get("type") or err.get("error_type")
        msg = err.get("message") or err.get("reason") or "upstream websocket error"
        detail = f"{code}: {msg}" if code and str(code) not in str(msg) else str(msg)
        return status_i, detail[:2000]
    status = obj.get("status")
    try:
        status_i = int(status) if status is not None else None
    except Exception:
        status_i = None
    msg = obj.get("message") or obj.get("reason") or json.dumps(obj, ensure_ascii=False)
    return status_i, str(msg)[:2000]


def _is_ws_visible_event_type(event_type: str) -> bool:
    try:
        return bool(event_type) and event_type in upstream.RESPONSES_VISIBLE_EVENTS
    except Exception:
        return False


class _WsResponsesTracker:
    def __init__(self, channel: OpenAIOAuthChannel | None = None) -> None:
        self.channel = channel
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0}
        self.response_completed = False
        self.response_failed = False
        self.stream_error_message: Optional[str] = None
        self._frames: list[str] = []
        self._items: dict[int, dict] = {}
        self._fc_args: dict[int, str] = {}
        self._msg_text: dict[tuple[int, int], str] = {}
        self._response_obj: Optional[dict] = None

    def feed_text(self, text: str) -> None:
        if not text:
            return
        try:
            evt = json.loads(text)
        except Exception:
            self._frames.append(text)
            return
        if not isinstance(evt, dict):
            self._frames.append(text)
            return
        typ = str(evt.get("type") or "")
        if typ == "codex.rate_limits":
            _maybe_record_codex_rate_limits_event(self.channel, evt)
            return
        self._frames.append(text)
        if typ == "error" or isinstance(evt.get("error"), dict):
            self.response_failed = True
            _, self.stream_error_message = _ws_error_detail(text)
            return
        if typ == "response.failed":
            self.response_failed = True
            _, self.stream_error_message = _ws_error_detail(text)
        elif typ == "response.completed":
            self.response_completed = True

        if typ in ("response.completed", "response.failed", "response.incomplete"):
            resp = evt.get("response") if isinstance(evt.get("response"), dict) else None
            if isinstance(resp, dict):
                self._response_obj = resp
                usage = resp.get("usage")
                if isinstance(usage, dict):
                    self.usage = upstream.extract_usage_responses_json({"usage": usage})
                if isinstance(resp.get("output"), list):
                    for idx, item in enumerate(resp.get("output") or []):
                        if isinstance(item, dict):
                            self._items[idx] = dict(item)
        if typ == "response.output_item.added":
            idx = _safe_int(evt.get("output_index"), 0)
            item = evt.get("item")
            if isinstance(item, dict):
                self._items[idx] = dict(item)
        elif typ == "response.output_item.done":
            idx = _safe_int(evt.get("output_index"), 0)
            item = evt.get("item")
            if isinstance(item, dict):
                self._items[idx] = dict(item)
        elif typ == "response.output_text.delta":
            idx = _safe_int(evt.get("output_index"), 0)
            cidx = _safe_int(evt.get("content_index"), 0)
            delta = evt.get("delta")
            if isinstance(delta, str) and delta:
                self._msg_text[(idx, cidx)] = self._msg_text.get((idx, cidx), "") + delta
        elif typ == "response.function_call_arguments.delta":
            idx = _safe_int(evt.get("output_index"), 0)
            delta = evt.get("delta")
            if isinstance(delta, str) and delta:
                self._fc_args[idx] = self._fc_args.get(idx, "") + delta

    def get_output_items(self) -> list[dict]:
        out: list[dict] = []
        for idx in sorted(self._items.keys()):
            item = dict(self._items[idx])
            if item.get("type") == "message":
                content = list(item.get("content") or [])
                merged = {ci: text for (oi, ci), text in self._msg_text.items() if oi == idx}
                for ci in sorted(merged.keys()):
                    if ci < len(content) and isinstance(content[ci], dict):
                        if not content[ci].get("text"):
                            content[ci]["text"] = merged[ci]
                    else:
                        content.append({"type": "output_text", "text": merged[ci], "annotations": []})
                item["content"] = content
            elif item.get("type") == "function_call":
                args = self._fc_args.get(idx)
                if args and not item.get("arguments"):
                    item["arguments"] = args
            out.append(item)
        return out

    def to_full_json(self, *, fallback_model: str) -> dict:
        base = dict(self._response_obj or {})
        base["output"] = self.get_output_items()
        base.setdefault("object", "response")
        base.setdefault("status", "completed" if self.response_completed else "incomplete")
        base.setdefault("model", fallback_model)
        if self.usage:
            base.setdefault("usage", {
                "input_tokens": int(self.usage.get("input_tokens") or 0) + int(self.usage.get("cache_read") or 0),
                "output_tokens": int(self.usage.get("output_tokens") or 0),
                "input_tokens_details": {"cached_tokens": int(self.usage.get("cache_read") or 0)},
            })
        return base

    def get_full_response(self) -> str:
        return "\n".join(self._frames)[-200000:]


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _ws_http_status_from_outcome(result: AttemptResult) -> int:
    if result.http_status is not None:
        return int(result.http_status)
    if result.outcome in ("connect_timeout", "first_byte_timeout", "idle_timeout", "total_timeout"):
        return 504
    if result.outcome in ("blacklist_hit", "upstream_error_json", "stream_upstream_error"):
        return 503
    if result.outcome == "guard_error":
        return 400
    return 502


def _invalid_ws_status_detail(exc: InvalidStatus) -> tuple[Optional[int], str]:
    resp = getattr(exc, "response", None)
    status = int(getattr(resp, "status_code", 0) or 0) or None
    body = b""
    try:
        body = getattr(resp, "body", b"") or b""
    except Exception:
        body = b""
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
    return status, (f"HTTP {status}: {text}" if status else str(exc))[:2000]


async def _build_oauth_responses_ws_upstream_request(
    ch: OpenAIOAuthChannel,
    body: dict,
    resolved_model: str,
) -> tuple[str, dict[str, str], str, Optional[dict], ConfuseState]:
    # 复用 OAuth channel 的鉴权 / session 隔离 / header 构造；只把 URL 改成 WS，
    # body 用 response.create frame 单独生成，避免把 HTTP JSON body 直接发成 WS frame。
    req = await ch.build_upstream_request(body, resolved_model, ingress_protocol="responses")
    ws_url = _http_url_to_ws(req.url)
    headers = _merge_oauth_responses_ws_headers(req.headers)
    _ensure_oauth_responses_ws_session_headers(headers, body)
    frame_obj = _build_oauth_responses_ws_frame(body, resolved_model)

    api_key_name = str((body or {}).get("_api_key_name") or "")
    sid = _get_header_case_insensitive(headers, "session-id") or _get_header_case_insensitive(headers, "session_id")
    raw_anchor = str((body or {}).get("prompt_cache_key") or "").strip()
    identity_state = ConfuseState()
    if api_key_name and sid:
        cm = frame_obj.get("client_metadata") if isinstance(frame_obj.get("client_metadata"), dict) else {}
        confused_cm, identity_state = confuse_client_metadata(
            api_key_name, cm, session_prompt_cache_key=sid,
            original_prompt_cache_key=raw_anchor,
        )
        if confused_cm:
            frame_obj["client_metadata"] = confused_cm
        elif "client_metadata" in frame_obj:
            frame_obj.pop("client_metadata", None)
        if identity_state.confused_prompt_cache_key:
            frame_obj["prompt_cache_key"] = identity_state.confused_prompt_cache_key
        headers = confuse_identity_headers(headers, identity_state, session_prompt_cache_key=sid)
    else:
        headers = _drop_headers_case_insensitive(headers, {"conversation_id", "conversation-id"})

    frame = json.dumps(frame_obj, ensure_ascii=False, separators=(",", ":"))
    return ws_url, headers, frame, req.translator_ctx, identity_state


async def _try_openai_oauth_responses_ws_channel(
    ch: Channel, resolved_model: str, body: dict,
    is_stream: bool, deadline_ts: float, start_time: float,
    fp_query: Optional[str], messages: list,
    api_key_name: Optional[str], client_ip: str,
    request_id: str, retry_count_so_far: int, affinity_hit: int,
    *, client_key: Optional[str] = None,
    retry_attempt_id: int | None = None,
) -> AttemptResult:
    if not isinstance(ch, OpenAIOAuthChannel):
        return AttemptResult(outcome="guard_error", error_detail="Responses WS upstream requires OpenAI OAuth channel", http_status=400)

    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    connect_timeout = int(timeouts.get("connect", 10))
    first_byte_timeout = int(timeouts.get("firstByte", 30))
    idle_timeout = int(timeouts.get("idle", 120))

    try:
        ws_url, ws_headers, first_frame, translator_ctx, identity_state = await _build_oauth_responses_ws_upstream_request(
            ch, body, resolved_model,
        )
    except Exception as exc:
        if hasattr(exc, "status") and hasattr(exc, "err_type") and hasattr(exc, "message"):
            return AttemptResult(
                outcome="guard_error",
                error_detail=str(getattr(exc, "message", exc))[:2000],
                http_status=int(getattr(exc, "status", 400)),
            )
        return AttemptResult(outcome="transform_error", error_detail=f"transform error: {exc}")

    last_error: Optional[AttemptResult] = None
    proxy_attempt_order = 0
    for route_name, connector in _resolve_ws_route_chain_for_channel(ch, resolved_model):
        proxy_name = None if connector is None else route_name
        proxy_bytes = _WsProxyBytes()
        t0 = time.time()
        proxy_attempt_id: int | None = None
        proxy_attempt_order += 1
        if proxy_name:
            try:
                proxy_attempt_id = log_db.record_proxy_attempt(
                    request_id, retry_attempt_id, proxy_attempt_order, proxy_name, t0,
                )
            except Exception:
                proxy_attempt_id = None
        upstream_ws = None
        try:
            if connector is not None:
                connector.stats.total_attempts += 1
                connector.stats.last_attempt_ts = t0
            upstream_ws = await _connect_oauth_responses_ws(
                ws_url,
                headers=ws_headers,
                connector=connector,
                proxy_bytes=proxy_bytes,
                open_timeout=connect_timeout,
            )
            connect_ms = int((time.time() - t0) * 1000)
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, connect_ms=connect_ms, ended_at=time.time(),
                        outcome="connected", bytes_up=proxy_bytes.up, bytes_down=proxy_bytes.down,
                    )
                except Exception:
                    pass
            if connector is not None:
                connector.stats.total_successes += 1
                connector.stats.last_success_ts = time.time()
                connector.stats.last_latency_ms = connect_ms
            _maybe_record_codex_ws_snapshot(ch, getattr(upstream_ws, "response", None))
            result = await _consume_oauth_responses_ws(
                upstream_ws,
                first_frame=first_frame,
                ch=ch,
                resolved_model=resolved_model,
                is_stream=is_stream,
                deadline_ts=deadline_ts,
                start_time=start_time,
                connect_ms=connect_ms,
                first_byte_timeout=first_byte_timeout,
                idle_timeout=idle_timeout,
                request_id=request_id,
                messages=messages,
                api_key_name=api_key_name,
                client_ip=client_ip,
                fp_query=fp_query,
                retry_count_so_far=retry_count_so_far,
                affinity_hit=affinity_hit,
                translator_ctx=translator_ctx,
                body=body,
                identity_state=identity_state,
                client_key=client_key,
                proxy_name=proxy_name,
                proxy_bytes=proxy_bytes,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, ended_at=time.time(), outcome=result.outcome,
                        error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
                        bytes_up=proxy_bytes.up, bytes_down=proxy_bytes.down,
                    )
                except Exception:
                    pass
            if result.stream_started:
                # StreamingResponse 还要继续从 upstream_ws 读取；交给生成器 finally 关闭。
                upstream_ws = None
            return result
        except asyncio.TimeoutError:
            last_error = AttemptResult(
                outcome="connect_timeout",
                error_detail=f"connect timeout > {connect_timeout}s",
                proxy_name=proxy_name,
                proxy_bytes_up=proxy_bytes.up,
                proxy_bytes_down=proxy_bytes.down,
            )
        except InvalidStatus as exc:
            status, detail = _invalid_ws_status_detail(exc)
            last_error = AttemptResult(
                outcome="http_auth_error" if status in (401, 403) else "http_error",
                error_detail=detail,
                http_status=status,
                proxy_name=proxy_name,
                proxy_bytes_up=proxy_bytes.up,
                proxy_bytes_down=proxy_bytes.down,
            )
        except Exception as exc:
            last_error = AttemptResult(
                outcome="connect_error",
                error_detail=f"connect error: {exc}"[:2000],
                proxy_name=proxy_name,
                proxy_bytes_up=proxy_bytes.up,
                proxy_bytes_down=proxy_bytes.down,
            )
        finally:
            if proxy_attempt_id is not None and last_error is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, connect_ms=int((time.time() - t0) * 1000), ended_at=time.time(),
                        outcome=last_error.outcome,
                        error_detail=(last_error.error_detail or "")[:4000] if last_error.error_detail else None,
                        bytes_up=proxy_bytes.up, bytes_down=proxy_bytes.down,
                    )
                except Exception:
                    pass
            if upstream_ws is not None:
                try:
                    await upstream_ws.close()
                except Exception:
                    pass
        if connector is not None and last_error is not None:
            connector.stats.total_failures += 1
            connector.stats.last_error = (last_error.error_detail or last_error.outcome)[:200]
        continue

    return last_error or AttemptResult(outcome="proxy_connect_error", error_detail="proxy route has no usable target")


async def _consume_oauth_responses_ws(
    upstream_ws,
    *,
    first_frame: str,
    ch: OpenAIOAuthChannel,
    resolved_model: str,
    is_stream: bool,
    deadline_ts: float,
    start_time: float,
    connect_ms: int,
    first_byte_timeout: int,
    idle_timeout: int,
    request_id: str,
    messages: list,
    api_key_name: Optional[str],
    client_ip: str,
    fp_query: Optional[str],
    retry_count_so_far: int,
    affinity_hit: int,
    translator_ctx: Optional[dict],
    body: Optional[dict],
    identity_state: ConfuseState,
    client_key: Optional[str],
    proxy_name: Optional[str],
    proxy_bytes: _WsProxyBytes,
) -> AttemptResult:
    tracker = _WsResponsesTracker(ch)
    try:
        proxy_bytes.count(up=_frame_size(first_frame))
        await asyncio.wait_for(upstream_ws.send(first_frame), timeout=idle_timeout)
    except Exception as exc:
        return AttemptResult(
            outcome="transport_error",
            error_detail=f"send first websocket frame: {exc}",
            connect_ms=connect_ms,
            proxy_name=proxy_name,
            proxy_bytes_up=proxy_bytes.up,
            proxy_bytes_down=proxy_bytes.down,
            translator_ctx=translator_ctx,
        )

    if is_stream:
        return await _consume_oauth_responses_ws_stream(
            upstream_ws,
            tracker=tracker,
            ch=ch,
            resolved_model=resolved_model,
            deadline_ts=deadline_ts,
            start_time=start_time,
            connect_ms=connect_ms,
            first_byte_timeout=first_byte_timeout,
            idle_timeout=idle_timeout,
            request_id=request_id,
            messages=messages,
            api_key_name=api_key_name,
            client_ip=client_ip,
            fp_query=fp_query,
            retry_count_so_far=retry_count_so_far,
            affinity_hit=affinity_hit,
            translator_ctx=translator_ctx,
            body=body,
            identity_state=identity_state,
            client_key=client_key,
            proxy_name=proxy_name,
            proxy_bytes=proxy_bytes,
        )
    return await _consume_oauth_responses_ws_non_stream(
        upstream_ws,
        tracker=tracker,
        ch=ch,
        resolved_model=resolved_model,
        deadline_ts=deadline_ts,
        start_time=start_time,
        connect_ms=connect_ms,
        first_byte_timeout=first_byte_timeout,
        idle_timeout=idle_timeout,
        request_id=request_id,
        messages=messages,
        api_key_name=api_key_name,
        client_ip=client_ip,
        fp_query=fp_query,
        retry_count_so_far=retry_count_so_far,
        affinity_hit=affinity_hit,
        translator_ctx=translator_ctx,
        body=body,
        identity_state=identity_state,
        client_key=client_key,
        proxy_name=proxy_name,
        proxy_bytes=proxy_bytes,
    )


async def _recv_oauth_ws_until_visible(
    upstream_ws,
    tracker: _WsResponsesTracker,
    *,
    ch: Channel,
    deadline_ts: float,
    first_wait: float,
    idle_timeout: int,
    proxy_bytes: _WsProxyBytes,
    start_time: float,
) -> tuple[list[str | bytes], Optional[AttemptResult], Optional[int]]:
    pending: list[str | bytes] = []
    wait_sec = first_wait
    first_packet_ms: Optional[int] = None
    while True:
        try:
            data = await asyncio.wait_for(upstream_ws.recv(), timeout=wait_sec)
        except asyncio.TimeoutError:
            return pending, AttemptResult(
                outcome="first_byte_timeout",
                error_detail=f"first websocket packet timeout > {first_wait}s"
                if first_packet_ms is None else
                f"first websocket visible event timeout > {first_wait}s",
            ), first_packet_ms
        if first_packet_ms is None:
            # Log口径对齐 HTTP/SSE：记录上游 raw 首包时间；但首包可能只是
            # response.created / in_progress / codex.rate_limits，不代表已经可以
            # 锁定当前渠道或向下游发送。failover 边界仍由首个可见事件决定。
            first_packet_ms = int((time.time() - start_time) * 1000)
        proxy_bytes.count(down=_frame_size(data))
        if isinstance(data, str):
            event_type = _ws_event_type(data)
            tracker.feed_text(data)
            if event_type == "codex.rate_limits":
                pending.append(data)
                # 配额帧是首包但不下发；继续等真正可见事件。
                pass
            elif tracker.response_failed:
                status, detail = _ws_error_detail(data)
                # response.failed 是真实下游终态事件；普通 error/包装错误发生在首可见前可 failover。
                if event_type == "response.failed":
                    pending.append(data)
                    return pending, AttemptResult(
                        outcome="stream_upstream_error",
                        error_detail=detail,
                        http_status=status,
                        stream_started=True,
                    ), first_packet_ms
                return pending, AttemptResult(outcome="upstream_error_json", error_detail=detail, http_status=status), first_packet_ms
            elif _is_ws_visible_event_type(event_type):
                bl_hit = blacklist.match(data, ch.key)
                if bl_hit:
                    return pending, AttemptResult(outcome="blacklist_hit", error_detail=f"blacklist: {bl_hit}"), first_packet_ms
                pending.append(data)
                return pending, None, first_packet_ms
            else:
                pending.append(data)
                if tracker.response_completed:
                    return pending, None, first_packet_ms
        else:
            pending.append(data)
            return pending, None, first_packet_ms
        remaining = deadline_ts - time.time()
        if remaining <= 0:
            return pending, AttemptResult(outcome="total_timeout", error_detail="upstream total timeout before first visible websocket event"), first_packet_ms
        wait_sec = min(float(idle_timeout), max(1.0, remaining))


def _identity_expose_frame(data: str | bytes, state: ConfuseState) -> str | bytes:
    if not state.enabled:
        return data
    if isinstance(data, str):
        return expose_response_payload(data.encode("utf-8"), state).decode("utf-8", errors="replace")
    if isinstance(data, (bytes, bytearray)):
        return expose_response_payload(bytes(data), state)
    return data


def _identity_log_text(text: str, state: ConfuseState) -> str:
    if not text or not state.enabled:
        return text
    try:
        return expose_response_payload(text.encode("utf-8"), state).decode("utf-8", errors="replace")
    except Exception:
        return text


def _ws_json_to_responses_sse(data: str | bytes) -> bytes | None:
    if isinstance(data, bytes):
        return data
    typ = _ws_event_type(data)
    if typ == "codex.rate_limits":
        return None
    prefix = f"event: {typ}\n" if typ else ""
    return prefix.encode("utf-8") + b"data: " + data.encode("utf-8") + b"\n\n"


async def _consume_oauth_responses_ws_non_stream(
    upstream_ws,
    *,
    tracker: _WsResponsesTracker,
    ch: OpenAIOAuthChannel,
    resolved_model: str,
    deadline_ts: float,
    start_time: float,
    connect_ms: int,
    first_byte_timeout: int,
    idle_timeout: int,
    request_id: str,
    messages: list,
    api_key_name: Optional[str],
    client_ip: str,
    fp_query: Optional[str],
    retry_count_so_far: int,
    affinity_hit: int,
    translator_ctx: Optional[dict],
    body: Optional[dict],
    identity_state: ConfuseState,
    client_key: Optional[str],
    proxy_name: Optional[str],
    proxy_bytes: _WsProxyBytes,
) -> AttemptResult:
    first_wait = min(first_byte_timeout, max(1, int(deadline_ts - time.time())))
    first_chunks, pre_error, first_packet_ms = await _recv_oauth_ws_until_visible(
        upstream_ws, tracker, ch=ch, deadline_ts=deadline_ts,
        first_wait=first_wait, idle_timeout=idle_timeout, proxy_bytes=proxy_bytes,
        start_time=start_time,
    )
    first_byte_ms = first_packet_ms if first_packet_ms is not None else int((time.time() - start_time) * 1000)
    if pre_error is not None and not pre_error.stream_started:
        pre_error.connect_ms = connect_ms
        pre_error.first_byte_ms = first_byte_ms
        pre_error.proxy_name = proxy_name
        pre_error.proxy_bytes_up = proxy_bytes.up
        pre_error.proxy_bytes_down = proxy_bytes.down
        pre_error.translator_ctx = translator_ctx
        return pre_error

    # pre_error.stream_started 只会来自 response.failed：这是终态错误，不能再透明 failover。
    if pre_error is not None:
        await _finalize_oauth_ws_error(
            pre_error, ch, resolved_model, request_id, retry_count_so_far,
            affinity_hit, start_time, connect_ms, first_byte_ms,
            tracker, proxy_name, proxy_bytes, identity_state,
        )
        pre_error.proxy_name = proxy_name
        pre_error.proxy_bytes_up = proxy_bytes.up
        pre_error.proxy_bytes_down = proxy_bytes.down
        return pre_error

    while not tracker.response_completed and not tracker.response_failed:
        remaining = deadline_ts - time.time()
        if remaining <= 0:
            err = AttemptResult(outcome="total_timeout", error_detail="upstream total timeout", connect_ms=connect_ms, first_byte_ms=first_byte_ms)
            return err
        wait_sec = min(float(idle_timeout), max(1.0, remaining))
        try:
            data = await asyncio.wait_for(upstream_ws.recv(), timeout=wait_sec)
        except asyncio.TimeoutError:
            return AttemptResult(outcome="idle_timeout", error_detail=f"upstream idle timeout > {idle_timeout}s", connect_ms=connect_ms, first_byte_ms=first_byte_ms)
        except websockets.ConnectionClosed:
            break
        proxy_bytes.count(down=_frame_size(data))
        if isinstance(data, str):
            event_type = _ws_event_type(data)
            tracker.feed_text(data)
            if event_type == "codex.rate_limits":
                continue
            if tracker.response_failed:
                err = AttemptResult(outcome="stream_upstream_error", error_detail=tracker.stream_error_message or "upstream stream error", connect_ms=connect_ms, first_byte_ms=first_byte_ms, http_status=503)
                await _finalize_oauth_ws_error(err, ch, resolved_model, request_id, retry_count_so_far, affinity_hit, start_time, connect_ms, first_byte_ms, tracker, proxy_name, proxy_bytes, identity_state)
                err.proxy_name = proxy_name
                err.proxy_bytes_up = proxy_bytes.up
                err.proxy_bytes_down = proxy_bytes.down
                return err

    obj = tracker.to_full_json(fallback_model=resolved_model)
    if identity_state.enabled:
        try:
            obj = json.loads(expose_response_payload(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                identity_state,
            ).decode("utf-8"))
        except Exception:
            pass
    usage = upstream.extract_usage_responses_json(obj)
    total_ms = int((time.time() - start_time) * 1000)
    scorer.record_success(ch.key, resolved_model, connect_ms=connect_ms, first_byte_ms=first_byte_ms, total_ms=total_ms)
    cooldown.clear(ch.key, resolved_model)
    out_obj = _apply_non_stream_response_translator(obj, translator_ctx or {})
    _write_affinity_non_stream(
        "responses", api_key_name, client_ip, messages,
        {"role": "assistant", "content": obj.get("output") or []},
        body, out_obj, ch.key, resolved_model, client_key=client_key,
    )
    await asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=_identity_log_text(tracker.get_full_response(), identity_state), http_status=200,
        upstream_protocol="openai-responses", upstream_transport="ws",
        proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
    )
    return AttemptResult(
        outcome="success", success=True,
        response=JSONResponse(content=out_obj, status_code=200),
        http_status=200, connect_ms=connect_ms, first_byte_ms=first_byte_ms,
        total_ms=total_ms, usage=usage, full_response_text=_identity_log_text(tracker.get_full_response(), identity_state),
        proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
        translator_ctx=translator_ctx,
    )


async def _finalize_oauth_ws_error(
    result: AttemptResult,
    ch: Channel,
    resolved_model: str,
    request_id: str,
    retry_count_so_far: int,
    affinity_hit: int,
    start_time: float,
    connect_ms: int,
    first_byte_ms: Optional[int],
    tracker: _WsResponsesTracker,
    proxy_name: Optional[str],
    proxy_bytes: _WsProxyBytes,
    identity_state: ConfuseState,
) -> None:
    if _should_cooldown(result.outcome):
        cooldown.record_error(ch.key, resolved_model, result.error_detail)
    scorer.record_failure(ch.key, resolved_model, connect_ms=connect_ms)
    await asyncio.shield(asyncio.to_thread(
        log_db.finish_error,
        request_id, (result.error_detail or result.outcome or "upstream websocket error")[:4000],
        retry_count_so_far,
        final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
        connect_ms=connect_ms, first_token_ms=first_byte_ms,
        total_ms=int((time.time() - start_time) * 1000),
        http_status=_ws_http_status_from_outcome(result), affinity_hit=affinity_hit,
        response_body=_identity_log_text(tracker.get_full_response(), identity_state) or None,
        upstream_protocol="openai-responses", upstream_transport="ws",
        proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
    ))


async def _consume_oauth_responses_ws_stream(
    upstream_ws,
    *,
    tracker: _WsResponsesTracker,
    ch: OpenAIOAuthChannel,
    resolved_model: str,
    deadline_ts: float,
    start_time: float,
    connect_ms: int,
    first_byte_timeout: int,
    idle_timeout: int,
    request_id: str,
    messages: list,
    api_key_name: Optional[str],
    client_ip: str,
    fp_query: Optional[str],
    retry_count_so_far: int,
    affinity_hit: int,
    translator_ctx: Optional[dict],
    body: Optional[dict],
    identity_state: ConfuseState,
    client_key: Optional[str],
    proxy_name: Optional[str],
    proxy_bytes: _WsProxyBytes,
) -> AttemptResult:
    first_wait = min(first_byte_timeout, max(1, int(deadline_ts - time.time())))
    first_chunks, pre_error, first_packet_ms = await _recv_oauth_ws_until_visible(
        upstream_ws, tracker, ch=ch, deadline_ts=deadline_ts,
        first_wait=first_wait, idle_timeout=idle_timeout, proxy_bytes=proxy_bytes,
        start_time=start_time,
    )
    first_byte_ms = first_packet_ms if first_packet_ms is not None else int((time.time() - start_time) * 1000)
    if pre_error is not None and not pre_error.stream_started:
        pre_error.connect_ms = connect_ms
        pre_error.first_byte_ms = first_byte_ms
        pre_error.proxy_name = proxy_name
        pre_error.proxy_bytes_up = proxy_bytes.up
        pre_error.proxy_bytes_down = proxy_bytes.down
        pre_error.translator_ctx = translator_ctx
        return pre_error

    state = {"finalized": False}

    async def finalize_success() -> None:
        if state["finalized"]:
            return
        state["finalized"] = True
        total_ms = int((time.time() - start_time) * 1000)
        usage = tracker.usage
        scorer.record_success(ch.key, resolved_model, connect_ms=connect_ms, first_byte_ms=first_byte_ms, total_ms=total_ms)
        cooldown.clear(ch.key, resolved_model)
        _write_affinity_non_stream(
            "responses", api_key_name, client_ip, messages,
            {"role": "assistant", "content": tracker.get_output_items()},
            body, tracker.to_full_json(fallback_model=resolved_model), ch.key, resolved_model,
            client_key=client_key,
        )
        # v3 捕获：把上游本轮整批 output（reasoning + 工具调用，带 encrypted_content）
        # 按 session_key 存入本地缓存，供下一轮下游删了 reasoning 时回填。WS 流式路径。
        _capture_reasoning_items(body, resolved_model, tracker.get_output_items())
        await asyncio.shield(asyncio.to_thread(
            log_db.finish_success,
            request_id, ch.key, ch.type, resolved_model,
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
            connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
            retry_count=retry_count_so_far, affinity_hit=affinity_hit,
            response_body=_identity_log_text(tracker.get_full_response(), identity_state), http_status=200,
            upstream_protocol="openai-responses", upstream_transport="ws",
            proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
        ))

    async def finalize_error(result: AttemptResult) -> None:
        if state["finalized"]:
            return
        state["finalized"] = True
        # v3 兜底：上游拒绝 encrypted_content（invalid_encrypted_content / 推理签名
        # 失效）时，清掉该会话缓存，避免下一轮继续拿失效加密块撞上游。
        _maybe_invalidate_reasoning_on_error(body, resolved_model, result.error_detail)
        await _finalize_oauth_ws_error(
            result, ch, resolved_model, request_id, retry_count_so_far,
            affinity_hit, start_time, connect_ms, first_byte_ms,
            tracker, proxy_name, proxy_bytes, identity_state,
        )

    async def stream_generator():
        try:
            for data in first_chunks:
                out = _ws_json_to_responses_sse(_identity_expose_frame(data, identity_state))
                if out is not None:
                    yield out
            if pre_error is not None:
                await finalize_error(pre_error)
                return
            while True:
                if tracker.response_completed:
                    await finalize_success()
                    return
                if tracker.response_failed:
                    await finalize_error(AttemptResult(
                        outcome="stream_upstream_error",
                        error_detail=tracker.stream_error_message or "upstream stream error",
                        http_status=503,
                    ))
                    return
                remaining = deadline_ts - time.time()
                if remaining <= 0:
                    err = AttemptResult(outcome="total_timeout", error_detail="upstream total timeout", http_status=504)
                    await finalize_error(err)
                    yield _sse_error_for_ingress("responses", errors.ErrType.TIMEOUT, "upstream total timeout")
                    return
                wait_sec = min(float(idle_timeout), max(1.0, remaining))
                try:
                    data = await asyncio.wait_for(upstream_ws.recv(), timeout=wait_sec)
                except asyncio.TimeoutError:
                    err = AttemptResult(outcome="idle_timeout", error_detail=f"upstream idle timeout > {idle_timeout}s", http_status=504)
                    await finalize_error(err)
                    yield _sse_error_for_ingress("responses", errors.ErrType.TIMEOUT, err.error_detail or "upstream idle timeout")
                    return
                except websockets.ConnectionClosed:
                    if tracker.response_completed:
                        await finalize_success()
                        return
                    err = AttemptResult(outcome="upstream_closed", error_detail="upstream websocket closed", http_status=502)
                    await finalize_error(err)
                    yield _sse_error_for_ingress("responses", errors.ErrType.API, err.error_detail or "upstream websocket closed")
                    return
                proxy_bytes.count(down=_frame_size(data))
                if isinstance(data, str):
                    event_type = _ws_event_type(data)
                    tracker.feed_text(data)
                    if event_type == "codex.rate_limits":
                        continue
                    if tracker.response_failed:
                        out = _ws_json_to_responses_sse(_identity_expose_frame(data, identity_state))
                        if out is not None:
                            yield out
                        await finalize_error(AttemptResult(
                            outcome="stream_upstream_error",
                            error_detail=tracker.stream_error_message or "upstream stream error",
                            http_status=503,
                        ))
                        return
                    bl_hit = blacklist.match(data, ch.key)
                    if bl_hit:
                        err = AttemptResult(outcome="blacklist_hit", error_detail=f"blacklist: {bl_hit}", http_status=503)
                        await finalize_error(err)
                        yield _sse_error_for_ingress("responses", errors.ErrType.API, err.error_detail or "blacklist")
                        return
                out = _ws_json_to_responses_sse(_identity_expose_frame(data, identity_state))
                if out is not None:
                    yield out
        except asyncio.CancelledError:
            if not state["finalized"]:
                await asyncio.shield(asyncio.to_thread(
                    log_db.finish_error,
                    request_id, "client disconnected", retry_count_so_far,
                    final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                    connect_ms=connect_ms, first_token_ms=first_byte_ms,
                    total_ms=int((time.time() - start_time) * 1000),
                    http_status=499, affinity_hit=affinity_hit,
                    response_body=_identity_log_text(tracker.get_full_response(), identity_state) or None,
                    upstream_protocol="openai-responses", upstream_transport="ws",
                    proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
                ))
            raise
        finally:
            try:
                await upstream_ws.close()
            except Exception:
                pass

    response = StreamingResponse(
        stream_generator(),
        status_code=200,
        media_type="text/event-stream",
    )
    return AttemptResult(
        outcome="success",
        success=True,
        stream_started=True,
        response=response,
        http_status=200,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        proxy_name=proxy_name,
        proxy_bytes_up=proxy_bytes.up,
        proxy_bytes_down=proxy_bytes.down,
        translator_ctx=translator_ctx,
    )


# ─── 单渠道尝试 ──────────────────────────────────────────────────

async def _try_channel(
    ch: Channel, resolved_model: str, body: dict,
    is_stream: bool, deadline_ts: float, start_time: float,
    fp_query: Optional[str], messages: list,
    api_key_name: Optional[str], client_ip: str,
    request_id: str, retry_count_so_far: int, affinity_hit: int,
    *, ingress_protocol: str = "anthropic",
    client_key: Optional[str] = None,
    retry_attempt_id: int | None = None,
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

    # Proxy routing: resolve per channel/account/model.  A proxy group is
    # failover at the proxy layer: only pre-response-header failures switch to
    # the next proxy.  Once we have HTTP response headers, the upstream attempt
    # is locked to that proxy/channel; HTTP status/body/first-byte/read errors
    # are channel outcomes and must not silently retry through another proxy.
    _proxy_client = None
    _proxy_name_used: str | None = None
    _proxy_bytes = {"up": 0, "down": 0}

    def _new_proxy_bytes() -> dict:
        return {"up": 0, "down": 0}

    def _count_proxy_bytes_for(bucket: dict):
        def _cb(up: int = 0, down: int = 0):
            bucket["up"] += int(up or 0)
            bucket["down"] += int(down or 0)
        return _cb

    def _snapshot_bytes(bucket: dict) -> tuple[int, int]:
        return int(bucket.get("up") or 0), int(bucket.get("down") or 0)

    def _proxy_attempt_result(outcome: str, detail: str,
                              bucket: dict | None = None,
                              pname: str | None = None) -> AttemptResult:
        up, down = _snapshot_bytes(bucket or {})
        return AttemptResult(
            outcome=outcome,
            error_detail=detail,
            proxy_name=pname,
            proxy_bytes_up=up,
            proxy_bytes_down=down,
        )

    route_chain: list[tuple[str, Any | None]] = []
    try:
        from .proxy import manager as _pm
        _pm.init()
        if _pm.is_configured():
            chain = _pm.resolve_proxy_chain(**_proxy_route_kwargs(ch, resolved_model))
            valid_seen = False
            for _pn in chain:
                _pc = _pm.get_connector(_pn)
                if _pc is None:
                    continue
                valid_seen = True
                # Direct is an explicit candidate in the failover chain, but it
                # still uses the shared upstream client so tests/runtime pooling
                # semantics stay unchanged.  It is never logged as a proxy.
                if getattr(_pc, "type", "") == "direct":
                    route_chain.append(("direct", None))
                else:
                    route_chain.append((_pn, _pc))
            if not valid_seen:
                return AttemptResult(
                    outcome="proxy_connect_error",
                    error_detail=f"proxy route has no valid target: {chain}",
                )
    except Exception:
        route_chain = []
    if not route_chain:
        route_chain = [("direct", None)]

    ctx = None
    upstream_resp: Optional[httpx.Response] = None
    connect_ms = 0
    last_pre_header: AttemptResult | None = None
    proxy_attempt_id: int | None = None
    proxy_attempt_order = 0

    for _route_name, _pc in route_chain:
        _proxy_client = None
        _proxy_name_used = None
        _proxy_bytes = _new_proxy_bytes()
        client = upstream.get_client()
        proxy_attempt_id = None
        proxy_attempt_order += 1
        proxy_started_at = time.time()

        if _pc is not None:
            _proxy_name_used = str(_route_name)
            try:
                proxy_attempt_id = log_db.record_proxy_attempt(
                    request_id, retry_attempt_id, proxy_attempt_order, _proxy_name_used, proxy_started_at,
                )
            except Exception:
                proxy_attempt_id = None
            try:
                _pc.stats.total_attempts += 1
                _pc.stats.last_attempt_ts = proxy_started_at
                _proxy_client = _pc.create_httpx_client(
                    timeout=httpx.Timeout(connect=connect_timeout,
                                          read=330, write=30, pool=connect_timeout),
                    byte_counter=_count_proxy_bytes_for(_proxy_bytes),
                )
                client = _proxy_client
            except Exception as exc:
                _pc.stats.total_failures += 1
                _pc.stats.last_error = str(exc)[:200]
                last_pre_header = _proxy_attempt_result(
                    "proxy_connect_error", f"proxy client error: {exc}",
                    _proxy_bytes, _proxy_name_used,
                )
                if proxy_attempt_id is not None:
                    try:
                        log_db.update_proxy_attempt(
                            proxy_attempt_id, ended_at=time.time(), outcome=last_pre_header.outcome,
                            error_detail=(last_pre_header.error_detail or "")[:4000],
                            bytes_up=_proxy_bytes.get("up"), bytes_down=_proxy_bytes.get("down"),
                        )
                    except Exception:
                        pass
                await _close_proxy_client(_proxy_client)
                continue

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
            await _close_proxy_client(_proxy_client)
            if _pc is not None:
                _pc.stats.total_failures += 1
                _pc.stats.last_error = str(exc)[:200]
            last_pre_header = _proxy_attempt_result(
                "transport_error", f"send build error: {exc}",
                _proxy_bytes, _proxy_name_used,
            )
            continue

        enter_timeout = max(1.0, deadline_ts - time.time())
        try:
            upstream_resp = await asyncio.wait_for(
                ctx.__aenter__(), timeout=enter_timeout,
            )
        except asyncio.TimeoutError:
            await _close_proxy_client(_proxy_client)
            last_pre_header = _proxy_attempt_result(
                "total_timeout",
                f"total timeout during connect/headers (> {int(enter_timeout)}s)",
                _proxy_bytes, _proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, connect_ms=int((time.time() - t_send) * 1000), ended_at=time.time(),
                        outcome=last_pre_header.outcome, error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=_proxy_bytes.get("up"), bytes_down=_proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            # Overall deadline is exhausted; another proxy cannot help without
            # violating the request timeout budget.
            return last_pre_header
        except httpx.ConnectTimeout:
            await _close_proxy_client(_proxy_client)
            if _pc is not None:
                _pc.stats.total_failures += 1
                _pc.stats.last_error = f"connect timeout > {connect_timeout}s"
            last_pre_header = _proxy_attempt_result(
                "connect_timeout", f"connect timeout > {connect_timeout}s",
                _proxy_bytes, _proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, connect_ms=int((time.time() - t_send) * 1000), ended_at=time.time(),
                        outcome=last_pre_header.outcome, error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=_proxy_bytes.get("up"), bytes_down=_proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue
        except httpx.ConnectError as exc:
            await _close_proxy_client(_proxy_client)
            if _pc is not None:
                _pc.stats.total_failures += 1
                _pc.stats.last_error = str(exc)[:200]
            last_pre_header = _proxy_attempt_result(
                "connect_error", f"connect error: {exc}",
                _proxy_bytes, _proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, connect_ms=int((time.time() - t_send) * 1000), ended_at=time.time(),
                        outcome=last_pre_header.outcome, error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=_proxy_bytes.get("up"), bytes_down=_proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue
        except httpx.TimeoutException as exc:
            await _close_proxy_client(_proxy_client)
            if _pc is not None:
                _pc.stats.total_failures += 1
                _pc.stats.last_error = str(exc)[:200]
            last_pre_header = _proxy_attempt_result(
                "connect_timeout", f"timeout: {exc}",
                _proxy_bytes, _proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, connect_ms=int((time.time() - t_send) * 1000), ended_at=time.time(),
                        outcome=last_pre_header.outcome, error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=_proxy_bytes.get("up"), bytes_down=_proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue
        except Exception as exc:
            await _close_proxy_client(_proxy_client)
            if _pc is not None:
                _pc.stats.total_failures += 1
                _pc.stats.last_error = str(exc)[:200]
            last_pre_header = _proxy_attempt_result(
                "transport_error", f"transport: {exc}",
                _proxy_bytes, _proxy_name_used,
            )
            if proxy_attempt_id is not None:
                try:
                    log_db.update_proxy_attempt(
                        proxy_attempt_id, connect_ms=int((time.time() - t_send) * 1000), ended_at=time.time(),
                        outcome=last_pre_header.outcome, error_detail=(last_pre_header.error_detail or "")[:4000],
                        bytes_up=_proxy_bytes.get("up"), bytes_down=_proxy_bytes.get("down"),
                    )
                except Exception:
                    pass
            continue

        connect_ms = int((time.time() - t_send) * 1000)
        if proxy_attempt_id is not None:
            try:
                log_db.update_proxy_attempt(
                    proxy_attempt_id, connect_ms=connect_ms, ended_at=time.time(), outcome="connected",
                    bytes_up=_proxy_bytes.get("up"), bytes_down=_proxy_bytes.get("down"),
                )
            except Exception:
                pass
        if _pc is not None:
            _pc.stats.total_successes += 1
            _pc.stats.last_success_ts = time.time()
            _pc.stats.last_latency_ms = connect_ms
        break
    else:
        return last_pre_header or AttemptResult(
            outcome="proxy_connect_error",
            error_detail="proxy route has no usable target",
        )

    try:
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
                    proxy_name=_proxy_name_used,
                    proxy_bytes_up=_proxy_byte_snapshot(_proxy_bytes)[0],
                    proxy_bytes_down=_proxy_byte_snapshot(_proxy_bytes)[1],
                )
            except Exception as exc:
                await _safe_exit(ctx)
                return AttemptResult(
                    outcome="transport_error",
                    connect_ms=connect_ms,
                    error_detail=f"read http error body: {exc}",
                    proxy_name=_proxy_name_used,
                    proxy_bytes_up=_proxy_byte_snapshot(_proxy_bytes)[0],
                    proxy_bytes_down=_proxy_byte_snapshot(_proxy_bytes)[1],
                )
            err_text = raw.decode("utf-8", errors="replace")
            status = upstream_resp.status_code
            resp_headers = _pick_upstream_headers(upstream_resp)
            await _safe_exit(ctx)
            await _close_proxy_client(_proxy_client)

            outcome = "http_auth_error" if status in (401, 403) else "http_error"
            return AttemptResult(
                outcome=outcome,
                http_status=status,
                connect_ms=connect_ms,
                error_detail=f"HTTP {status}: {err_text[:2000]}",
                proxy_name=_proxy_name_used,
                proxy_bytes_up=_proxy_byte_snapshot(_proxy_bytes)[0],
                proxy_bytes_down=_proxy_byte_snapshot(_proxy_bytes)[1],
            )

        # 3. 非流式分支
        if not is_stream:
            result = await _consume_non_stream(
                ctx, upstream_resp, ch, resolved_model, dynamic_map,
                connect_ms, start_time, request_id,
                messages, api_key_name, client_ip,
                fp_query, retry_count_so_far, affinity_hit,
                ingress_protocol=ingress_protocol,
                translator_ctx=upstream_req.translator_ctx,
                body=body,
                client_key=client_key,
                proxy_name=_proxy_name_used,
                proxy_bytes=_proxy_bytes,
            )
            await _close_proxy_client(_proxy_client)
            return result

        # 4. 流式分支
        result = await _consume_stream(
            ctx, upstream_resp, ch, resolved_model, dynamic_map,
            connect_ms, start_time, deadline_ts,
            first_byte_timeout, idle_timeout,
            request_id, messages, api_key_name, client_ip,
            fp_query, retry_count_so_far, affinity_hit,
            client_key=client_key,
            ingress_protocol=ingress_protocol,
            translator_ctx=upstream_req.translator_ctx,
            body=body,
            proxy_name=_proxy_name_used,
            proxy_bytes=_proxy_bytes,
            proxy_client=_proxy_client,
        )
        if not result.stream_started:
            await _close_proxy_client(_proxy_client)
        return result
    except Exception as exc:
        traceback.print_exc()
        try:
            await _safe_exit(ctx)
        except Exception:
            pass
        await _close_proxy_client(_proxy_client)
        return AttemptResult(
            outcome="transport_error",
            error_detail=f"unexpected: {exc}",
        )


async def _safe_exit(ctx) -> None:
    try:
        await ctx.__aexit__(None, None, None)
    except Exception:
        pass


async def _close_proxy_client(client) -> None:
    if client is None:
        return
    try:
        await client.aclose()
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
    proxy_name: Optional[str] = None,
    proxy_bytes: Optional[dict] = None,
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
            proxy_name=proxy_name,
            proxy_bytes=proxy_bytes,
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
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
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
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
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
    proxy_name: Optional[str] = None,
    proxy_bytes: Optional[dict] = None,
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
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
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
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
    )


# ─── 流式 ────────────────────────────────────────────────────────


async def _read_until_first_downstream_chunk(
    aiter,
    ch: Channel,
    dynamic_map: Optional[dict],
    tracker,
    builder,
    deadline_ts: float,
    idle_timeout: int,
    *,
    protocol: str,
    first_chunk: bytes,
    stream_translator=None,
) -> tuple[list[bytes], Optional[dict]]:
    """Buffer upstream SSE until the first actual downstream bytes.

    The failover lock boundary is not "first upstream Responses event". Responses
    metadata/control events (created/in_progress/keepalive) and translator-dropped
    events (for example response.output_item.added for a chat-translated message)
    are still pre-first-byte. If an upstream error appears before this function
    has produced downstream bytes, the caller can still fail over invisibly.

    Returns (downstream_chunks, error_event). downstream_chunks are already in
    the downstream protocol shape. For same-protocol Responses streams, metadata
    events are buffered and replayed only after a real visible event proves this
    attempt should be used.
    """
    pending = b""
    buffered_same_protocol_events: list[bytes] = []

    def feed_downstream_event(event_bytes: bytes) -> list[bytes]:
        if stream_translator is not None:
            return list(stream_translator.feed(event_bytes))
        return [event_bytes]

    async def feed_restored(restored: bytes) -> tuple[list[bytes], Optional[dict]]:
        nonlocal pending
        tracker.feed(restored)
        builder.feed(restored)
        pending += restored
        pending, events = upstream.split_sse_events(pending)
        downstream_chunks: list[bytes] = []
        downstream_started = False
        for block in events:
            event_name, data = upstream.parse_sse_event_bytes(block)
            event_bytes = block + b"\n\n"
            if upstream.is_stream_error_event(event_name, data):
                error_obj = dict(data or {})
                error_obj["_event_name"] = event_name or ""
                return [], error_obj

            visible_event = upstream.is_downstream_visible_event(event_name, data, protocol)
            if not downstream_started and not visible_event:
                if stream_translator is None:
                    # Same-protocol Responses clients should eventually receive
                    # metadata, but sending it now would lock the channel before
                    # any meaningful content. Hold it until a visible event arrives.
                    buffered_same_protocol_events.append(event_bytes)
                else:
                    # Translator state may need metadata; if a translator ever emits
                    # bytes for it, those bytes are by definition the downstream boundary.
                    outs = feed_downstream_event(event_bytes)
                    if outs:
                        downstream_started = True
                        downstream_chunks.extend(outs)
                continue

            outs = feed_downstream_event(event_bytes)
            if outs:
                if not downstream_started and stream_translator is None and buffered_same_protocol_events:
                    downstream_chunks.extend(buffered_same_protocol_events)
                    buffered_same_protocol_events.clear()
                downstream_started = True
                downstream_chunks.extend(outs)

        if downstream_started:
            if pending:
                # Partial trailing bytes only happen when a chunk contains the
                # start of a following SSE block after the first downstream event.
                # It is now safe to pass through/feed them; subsequent reads will
                # continue from the network iterator.
                if stream_translator is not None:
                    downstream_chunks.extend(stream_translator.feed(pending))
                else:
                    downstream_chunks.append(pending)
                pending = b""
            return downstream_chunks, None
        return [], None

    restored_first = await ch.restore_response(first_chunk, dynamic_map=dynamic_map)
    downstream_chunks, err = await feed_restored(restored_first)
    if downstream_chunks or err is not None:
        return downstream_chunks, err

    while True:
        remaining = _remaining_ms(deadline_ts)
        if remaining <= 0:
            raise asyncio.TimeoutError("upstream total timeout before first downstream chunk")
        wait_sec = min(idle_timeout, max(1, remaining / 1000))
        chunk = await asyncio.wait_for(aiter.__anext__(), timeout=wait_sec)
        if not chunk:
            continue
        restored = await ch.restore_response(chunk, dynamic_map=dynamic_map)
        downstream_chunks, err = await feed_restored(restored)
        if downstream_chunks or err is not None:
            return downstream_chunks, err

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
    proxy_name: Optional[str] = None,
    proxy_bytes: Optional[dict] = None,
    proxy_client=None,
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
    toolkit = _toolkit_for(ch)
    tracker = toolkit["stream_tracker"]()
    builder = toolkit["stream_builder"]()
    ch_proto = getattr(ch, "protocol", "anthropic")
    # 跨变体：上游字节 → translator.feed → 下游字节；同协议 translator=None 原样 yield
    stream_translator = _make_stream_translator(translator_ctx)

    if ch_proto == "openai-responses":
        # Responses stream starts with metadata/control events (created,
        # in_progress, keepalive). Do not treat those as the irreversible first
        # downstream byte. Buffer/feed events until the first actual downstream
        # bytes. Errors before that boundary are still retryable.
        try:
            first_downstream_chunks, pre_visible_error = await _read_until_first_downstream_chunk(
                aiter, ch, dynamic_map, tracker, builder, deadline_ts, idle_timeout,
                protocol=ch_proto, first_chunk=first_chunk, stream_translator=stream_translator,
            )
        except asyncio.TimeoutError:
            await _safe_exit(ctx)
            return AttemptResult(
                outcome="first_byte_timeout", connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                error_detail=f"first downstream chunk timeout > {idle_timeout}s",
            )
        except StopAsyncIteration:
            await _safe_exit(ctx)
            return AttemptResult(
                outcome="closed_before_first_byte", connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                error_detail="upstream closed stream before first downstream chunk",
            )
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.TimeoutException) as exc:
            await _safe_exit(ctx)
            return AttemptResult(
                outcome="transport_error", connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                error_detail=f"first downstream chunk transport: {exc}",
            )
        if pre_visible_error:
            await _safe_exit(ctx)
            return AttemptResult(
                outcome="upstream_error_json",
                connect_ms=connect_ms, first_byte_ms=first_byte_ms,
                error_detail=json.dumps(pre_visible_error.get("error", pre_visible_error), ensure_ascii=False)[:2000],
            )
    else:
        first_chunk_restored = await ch.restore_response(first_chunk, dynamic_map=dynamic_map)
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
        tracker.feed(first_chunk_restored)
        builder.feed(first_chunk_restored)
        if stream_translator is not None:
            first_downstream_chunks = list(stream_translator.feed(first_chunk_restored))
        else:
            first_downstream_chunks = [first_chunk_restored]

    # 2b) 黑名单：对真正会发给下游的第一段内容检查，而不是 metadata。
    bl_target = b"".join(first_downstream_chunks)
    bl_hit = blacklist.match(bl_target, ch.key)
    if bl_hit:
        await _safe_exit(ctx)
        return AttemptResult(
            outcome="blacklist_hit",
            connect_ms=connect_ms, first_byte_ms=first_byte_ms,
            error_detail=f"blacklist: {bl_hit}",
        )

    # 3. 通过检查 → 开始向下游发 ★
    resp_headers = _pick_upstream_headers(upstream_resp)
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
                assistant_msg = (getattr(stream_translator, "get_downstream_chat_assistant")()
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
                output_items = getattr(stream_translator, "_collect_output_items")() if stream_translator else []
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
            proxy_name=proxy_name,
            proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
            proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
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
            proxy_name=proxy_name,
            proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
            proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
        ))

    async def _finalize_client_cancelled():
        """客户端断开：不计 cooldown/scorer，仅记日志便于审计。

        tracker.saw_stream_end=True 表示上游已送达收尾事件
        （anthropic message_stop / chat [DONE] or finish_reason / responses completed 等）。
        这种情况服务端视角已成功完成，client 只是没收完最后几帧就断，归 success。

        tracker.saw_stream_error=True 表示上游 stream 内已给出终态错误
        （event:error / response.failed / chat error chunk）。这种情况下若下游收到
        error 帧后立刻断开，不能再把 DB 误标成 "client disconnected"。
        """
        if state["finalized"]:
            return
        if getattr(tracker, "saw_stream_error", False):
            msg = getattr(tracker, "stream_error_message", None) or "upstream stream error"
            await _emit_error_and_finalize("api_error", msg, outcome="stream_upstream_error")
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
            proxy_name=proxy_name,
            proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
            proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
        ))

    async def stream_generator():
        """把首包 + 后续 chunk 转发给下游，同时在中途错误时用 SSE error event 收尾。"""
        if state["finalized"]:
            return
        try:
            # 首个实际下游 chunk 已在返回 StreamingResponse 前确定，确保
            # failover 锁定点等于“下游真的会收到字节”。
            for out in first_downstream_chunks:
                yield out

            if getattr(tracker, "saw_stream_error", False):
                msg = getattr(tracker, "stream_error_message", None) or "upstream stream error"
                await _emit_error_and_finalize(
                    "api_error", msg,
                    outcome="stream_upstream_error",
                )
                return

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

                # 上游在 stream 中途给出终态错误（Responses event:error /
                # response.failed，或 Chat/Anthropic error chunk）时，下游通常会在
                # 收到错误帧后立即断开。这里先把上游原始错误帧转发出去，再落库真实
                # 错误并结束，避免被 finally/CancelledError 误标成 client disconnected。
                if getattr(tracker, "saw_stream_error", False):
                    msg = getattr(tracker, "stream_error_message", None) or "upstream stream error"
                    await _emit_error_and_finalize(
                        "api_error", msg,
                        outcome="stream_upstream_error",
                    )
                    return

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
            await _close_proxy_client(proxy_client)

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
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
    )
