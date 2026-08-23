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
import random
import re
import time
import traceback
import uuid
from typing import Optional, Any

import httpx
import websockets
from fastapi.responses import JSONResponse, Response, StreamingResponse
from websockets.exceptions import InvalidStatus

import threading

from . import (
    affinity, blacklist, channel_state, compact_rescue, concurrency, config, cooldown, errors, fingerprint,
    local_web_tools, log_db, model_metadata, model_pricing, notifier, oauth_manager, quota_errors, scorer, state_db,
    token_counter, upstream,
)
from .channel.base import Channel
from .channel.openai_oauth_channel import OpenAIOAuthChannel
from .transform import cc_mimicry
from .openai import compaction_owner, deepseek_reasoning, reasoning_replay
from .openai.codex_identity_confuse import ConfuseState
from .openai.responses_ws_runtime import (
    build_oauth_responses_ws_frame,
    drop_headers_case_insensitive,
    ensure_oauth_responses_ws_session_headers,
    get_header_case_insensitive,
    identity_expose_frame,
    identity_log_text,
    merge_oauth_responses_ws_headers,
    prepare_oauth_responses_ws_request_parts,
)
from .proxy.connector import SOCKS5Connector, SS2022Connector
from .oauth import openai as openai_provider
from .providers import registry as provider_registry
from .providers.antigravity_errors import parse_antigravity_429
from .protocols import finalize as finalize_policy
from .protocols import errors as protocol_errors
from .protocols.runtime import (
    AttemptResult,
    DEFAULT_TRANSIENT_RETRY_DELAYS_S,
    MAX_CONFIGURED_TRANSIENT_RETRIES,
    apply_non_stream_response_translator,
    bounded_account_quota_error,
    configured_transient_retry_delays,
    connection_lifecycle_outcome,
    failover_final_http_status,
    is_context_1m_credit_error,
    is_responses_ws_visible_event_type,
    json_error_for_ingress,
    make_stream_translator,
    parse_retry_after_seconds,
    retry_after_cooldown_until,
    prepare_non_stream_response,
    recovery_retry_allowed,
    is_context_length_exceeded_error,
    is_invalid_encrypted_content_error,
    is_html_error_document,
    retryable_transient_error_kind,
    responses_ws_error_detail,
    transient_retry_allowed,
    transient_retry_config,
    transient_retry_limit,
    request_invalid_result_if_needed,
    retry_body_without_encrypted_content,
    retry_body_without_context_1m,
    should_cooldown,
    sse_error_for_ingress,
    toolkit_for_channel,
    upstream_ws_http_status_from_attempt,
)
from .scheduler import ScheduleResult
from .transports import (
    aggregate_stream_as_non_stream_response,
    close_proxy_client,
    close_response_context,
    finalize_opened_http_response,
    http_url_to_ws,
    metadata_from_response,
    open_response_with_proxy_chain,
    prepare_stream_response_start,
    read_next_responses_ws_step,
    read_until_first_responses_ws_visible_event,
    read_http_error_response,
    read_next_stream_step,
    read_non_stream_body,
    ManagedWsConnection,
    BusinessTimeoutError,
    RoundTimeouts,
    WsAttemptTiming,
    WsProxyBytes,
    await_ws_owned,
    connect_upstream_ws,
    legacy_socks5_connector,
    open_socket_via_ss2022,
    resolve_ws_route_chain,
    socks5h_url,
    wait_ws_round_io,
    ws_event_type,
    ws_frame_size,
    ws_route_kwargs,
)
from .transports import policy as transport_policy


# ─── OpenAI Codex 响应头 snapshot 节流 ───────────────────────────
#
# ChatGPT internal API 把 rate-limit 放在每次请求的 response header 里，没有
# 独立 usage 端点。为避免每次请求都写一次 state_db，按账号 30s 节流。
# 快照持久化属于旁路能力，异常不得影响主请求链路。

_CODEX_SNAPSHOT_WRITE_INTERVAL_S = 30.0
_codex_snapshot_last: dict[str, float] = {}
_codex_snapshot_lock = threading.Lock()
_codex_snapshot_inflight: set[str] = set()


def _maybe_record_codex_snapshot(ch: Channel, resp: Any) -> None:
    if not isinstance(ch, OpenAIOAuthChannel):
        return
    try:
        snap = openai_provider.parse_rate_limit_headers(dict(resp.headers))
        if not snap:
            return
        account_key = getattr(ch, "account_key", None) or ch.email
        email = ch.email

        # Auto-disable is based on this response, not on whether the auxiliary
        # SQLite snapshot can be persisted. A BUSY/FULL/READONLY cache must
        # never leave an explicitly over-limit account enabled.
        _maybe_auto_disable_by_codex_snapshot(account_key, email, snap)

        # throttle bucket 用 account_key 作 key；OpenAI 同一邮箱可能有多个
        # workspace，不能按 email 合并。只在成功写入后推进 last；inflight
        # 防止多个并发响应同时穿透，但写失败会立刻允许下一次重试。
        now = time.time()
        with _codex_snapshot_lock:
            last = _codex_snapshot_last.get(account_key, 0.0)
            if (
                now - last < _CODEX_SNAPSHOT_WRITE_INTERVAL_S
                or account_key in _codex_snapshot_inflight
            ):
                return
            _codex_snapshot_inflight.add(account_key)
        try:
            normalized = openai_provider.normalize_codex_snapshot(snap)
            state_db.quota_save_openai_snapshot(
                account_key, snap, normalized, email=email,
            )
        except BaseException:
            # Do not advance the throttle bucket: a following response should
            # retry the cache write instead of waiting 30 seconds.
            raise
        else:
            with _codex_snapshot_lock:
                _codex_snapshot_last[account_key] = time.time()
        finally:
            with _codex_snapshot_lock:
                _codex_snapshot_inflight.discard(account_key)
    except Exception as exc:
        print(f"[failover] codex snapshot record failed for {getattr(ch, 'email', '?')}: {exc}")


# ─── Anthropic 响应头被动采样 snapshot 节流 ──────────────────────
#
# Anthropic 在每次成功响应的响应头里带 5h/7d rate-limit utilization，
# 比主动拉 /api/oauth/usage
# 新鲜得多且无 rate-limit 成本。与 Codex 节流机制对称：按 account_key 30s
# 节流，避免每次请求都写 state_db。
#
# 注意：这条路径**只更新 five_hour_* / seven_day_* 四个字段**，不碰主动拉
# 才有的 sonnet/opus/extra 维度；详见 state_db.quota_patch_passive。

_ANTHROPIC_SNAPSHOT_WRITE_INTERVAL_S = 30.0
_anthropic_snapshot_last: dict[str, float] = {}
_anthropic_snapshot_lock = threading.Lock()
_anthropic_snapshot_inflight: set[str] = set()


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

        # Keep realtime disable independent from the best-effort quota cache.
        _maybe_auto_disable_by_headers(
            account_key, ch.email, dict(resp.headers),
            provider="claude",
        )

        now = time.time()
        with _anthropic_snapshot_lock:
            last = _anthropic_snapshot_last.get(account_key, 0.0)
            if (
                now - last < _ANTHROPIC_SNAPSHOT_WRITE_INTERVAL_S
                or account_key in _anthropic_snapshot_inflight
            ):
                return
            _anthropic_snapshot_inflight.add(account_key)
        try:
            state_db.quota_patch_passive(account_key, patch, email=email)
        except BaseException:
            raise
        else:
            with _anthropic_snapshot_lock:
                _anthropic_snapshot_last[account_key] = time.time()
        finally:
            with _anthropic_snapshot_lock:
                _anthropic_snapshot_inflight.discard(account_key)
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
        _anthropic_snapshot_inflight.discard(email)
        _anthropic_snapshot_inflight.discard(key)


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
# 幂等：账号已是 disabled_reason="quota" 时不重复通知或移动恢复时间，但仍
# 持久化最新 observation generation，供并发恢复 CAS 使用。
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
            f"{notifier.provider_tag('claude')}{_plan_tag}\n"
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
    if acc is None or acc.get("disabled_reason") not in (None, "quota"):
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
        disable_result = oauth_manager.set_disabled_by_quota(
            account_key,
            latest_iso,
            observation=oauth_manager.codex_quota_observation(snap),
        )
    except Exception as exc:
        print(f"[failover] auto-disable (codex) failed for {account_key}: {exc}")
        return

    # A quota-disabled account still records a new generation/observation for
    # recovery CAS, but only the transition from enabled emits a notification.
    if (disable_result or {}).get("state") != "disabled":
        return

    try:
        ek = notifier.escape_html
        _acc = oauth_manager.get_account(account_key)
        _label = oauth_manager.openai_plan_workspace_label(_acc) if _acc else "OpenAI"
        notifier.notify_event(
            "quota_disabled",
            "⚠ <b>OAuth 配额已用尽（响应头实时触发）</b>\n"
            f"账号: <code>{ek(email)}</code>\n"
            f"{notifier.provider_custom_emoji_html('openai')} {ek(_label)}\n"
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
        _codex_snapshot_inflight.discard(email)
        _codex_snapshot_inflight.discard(key)


def _toolkit_for(ch: Channel) -> dict:
    return toolkit_for_channel(ch)


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
    fp_query: Optional[str] = None,
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
    prompt_cache_key = _openai_prompt_cache_key_from_body(ingress_protocol, body)
    # Stable session fp_query must follow the channel that actually succeeded.
    # This is also safe for legacy transcript fp_query and closes the stale-owner
    # window during failover; fp_write remains the forward transcript bridge.
    if fp_query:
        affinity.upsert(
            fp_query, channel_key, resolved_model,
            prompt_cache_key=prompt_cache_key,
        )
    if fp_write:
        affinity.upsert(
            fp_write, channel_key, resolved_model,
            prompt_cache_key=prompt_cache_key,
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


def _maybe_save_native_responses_store(
    response_obj: Any,
    *,
    body: Optional[dict],
    api_key_name: Optional[str],
    channel_key: Optional[str],
    model: str,
) -> None:
    """Persist native Responses output locally so fallback routes can replay it.

    Native Responses providers may keep their own server-side state, but Parrot
    still needs a local copy to preserve `previous_response_id` semantics if a
    later turn falls back to Chat/Anthropic or another provider.
    """
    if not api_key_name or not isinstance(response_obj, dict):
        return
    if channel_key and channel_state.is_deleted(channel_key):
        return
    if channel_key:
        channel_key = channel_state.resolve(channel_key)
    response_id = response_obj.get("id")
    output_items = response_obj.get("output")
    if not isinstance(response_id, str) or not response_id:
        return
    if not isinstance(output_items, list):
        return
    request_body = body or {}
    try:
        from .openai import store as openai_store
        if not openai_store.is_enabled():
            return
        openai_store.save(
            response_id=response_id,
            parent_id=str(request_body.get("previous_response_id") or "") or None,
            api_key_name=api_key_name or "",
            model=model,
            channel_key=channel_key,
            input_items=_responses_current_input_items(request_body),
            output_items=output_items,
        )
    except Exception as exc:
        should_log = True
        try:
            ek = notifier.escape_html
            should_log = notifier.throttled_notify_event_sync(
                "openai_store_save_failed",
                f"openai_store_save_failed:{api_key_name}",
                f"❌ {notifier.provider_custom_emoji_html('openai')} <b>OpenAI Store 写入失败</b>（native Responses）\n"
                f"API Key: <code>{ek(str(api_key_name or ''))}</code>\n"
                f"模型: <code>{ek(model)}</code> · 渠道: <code>{ek(str(channel_key or '?'))}</code>\n"
                f"resp_id: <code>{ek(response_id)}</code>\n"
                f"原因: <code>{ek(str(exc))[:300]}</code>\n"
                "⚠ 后续 fallback 使用该 previous_response_id 时可能无法展开历史。",
            )
        except Exception:
            pass
        if should_log:
            traceback.print_exc()


def _make_stream_translator(translator_ctx: Optional[dict]):
    return make_stream_translator(translator_ctx)


def _apply_non_stream_response_translator(obj: dict, translator_ctx: dict) -> dict:
    return apply_non_stream_response_translator(obj, translator_ctx)


def _sse_error_for_ingress(
    ingress: str,
    anth_err_type: str,
    message: str,
    *,
    code: Optional[str] = None,
) -> bytes:
    return sse_error_for_ingress(ingress, anth_err_type, message, code=code)


def _json_error_for_ingress(
    ingress: str,
    status: int,
    anth_err_type: str,
    message: str,
    *,
    code: Optional[str] = None,
    details: Optional[dict] = None,
):
    return json_error_for_ingress(
        ingress, status, anth_err_type, message, code=code, details=details,
    )


def _should_cooldown(outcome: str) -> bool:
    return should_cooldown(outcome)


def _is_invalid_encrypted_content_error(error_detail: Optional[str]) -> bool:
    return is_invalid_encrypted_content_error(error_detail)


def _is_context_length_exceeded_error(error_detail: Optional[str]) -> bool:
    return is_context_length_exceeded_error(error_detail)


def _request_invalid_status(result: AttemptResult) -> int:
    if isinstance(result.http_status, int) and 400 <= result.http_status < 500:
        return int(result.http_status)
    return 400


def _mark_request_invalid(result: AttemptResult, status: int) -> AttemptResult:
    result.outcome = "request_invalid"
    result.http_status = int(status)
    if result.response is None:
        result.stream_started = False
    return result


def _request_invalid_result_if_needed(result: AttemptResult) -> AttemptResult:
    return request_invalid_result_if_needed(result)


def _maybe_cache_codex_reasoning_replay(translator_ctx: Optional[dict], response_obj: Any) -> None:
    """Best-effort cache of Codex Responses output items for stateless replay."""
    try:
        reasoning_replay.cache_from_translator_ctx(translator_ctx, response_obj)
    except Exception as exc:
        print(f"[failover] codex reasoning replay cache failed: {exc}")


def _maybe_cache_deepseek_reasoning(ch: Channel, model: str, response_obj: Any) -> None:
    """Cache terminal DeepSeek reasoning for later tool-call replay."""
    try:
        is_deepseek = getattr(ch, "_is_deepseek", None)
        if not callable(is_deepseek) or not is_deepseek(model):
            return
        if getattr(ch, "protocol", "") == "openai-responses":
            deepseek_reasoning.cache_from_responses_response(response_obj, model=model)
        else:
            deepseek_reasoning.cache_from_chat_response(response_obj, model=model)
    except Exception as exc:
        print(f"[failover] deepseek reasoning replay cache failed: {exc}")


def _maybe_clear_codex_reasoning_replay(translator_ctx: Optional[dict]) -> bool:
    """Clear replay scope after upstream rejects encrypted reasoning state."""
    try:
        if reasoning_replay.delete_from_translator_ctx(translator_ctx):
            print("[failover] cleared codex reasoning replay scope after invalid encrypted_content")
            return True
    except Exception as exc:
        print(f"[failover] codex reasoning replay clear failed: {exc}")
    return False


def _strip_encrypted_content_value(value: Any) -> tuple[Any, int]:
    from .protocols.runtime import _strip_encrypted_content_value as _strip
    return _strip(value)


def _retry_body_without_encrypted_content(body: dict) -> tuple[dict, int]:
    return retry_body_without_encrypted_content(body)


def _invalid_ec_cleanup_retry_allowed(body: dict, message: str, cfg: dict,
                                      already_retried: bool) -> bool:
    """Compaction is indivisible; reasoning-only EC keeps legacy recovery."""
    return bool(
        _recovery_retry_allowed("invalidEncryptedContent", cfg)
        and _is_invalid_encrypted_content_error(message)
        and not compaction_owner.has_complete_compaction(body)
        and not already_retried
    )


def _attempt_body_for_channel(
    body: dict,
    channel_key: str,
    bound_channel_key: Optional[str],
    portable_body: Optional[dict] = None,
) -> dict:
    """Return the body safe for one candidate without mutating request state."""
    if compaction_owner.has_complete_compaction(body):
        if not bound_channel_key or channel_key != bound_channel_key:
            raise compaction_owner.CompactionRouteError(
                "compaction_owner_mismatch", "refusing to send compaction without its exact owner",
            )
        return body
    if channel_key == bound_channel_key:
        return body
    if portable_body is not None:
        return portable_body
    if bound_channel_key:
        stripped, _ = _retry_body_without_encrypted_content(body)
        return stripped
    return body


def _is_context_1m_credit_error(result: AttemptResult, resolved_model: str, body: dict) -> bool:
    return is_context_1m_credit_error(result, resolved_model, body)


def _retry_body_without_context_1m(body: dict) -> dict:
    return retry_body_without_context_1m(body)


def _channel_forces_context_1m(ch: Channel, resolved_model: str) -> bool:
    check = getattr(ch, "forces_context_1m", None)
    return bool(callable(check) and check(resolved_model))


def _channel_uses_max_context(ch: Channel | None, body: dict, resolved_model: str) -> bool:
    check = getattr(ch, "uses_max_context", None)
    if not callable(check):
        return False
    try:
        return bool(check(body, resolved_model))
    except Exception:
        return False


def _proxy_route_kwargs(ch: Channel, resolved_model: str) -> dict:
    return transport_policy.proxy_route_kwargs(ch, resolved_model)


def _pick_non_direct_proxy_name(ch: Channel, resolved_model: str) -> str | None:
    return transport_policy.pick_non_direct_proxy_name(ch, resolved_model)


def _proxy_byte_snapshot(proxy_bytes: Optional[dict]) -> tuple[int, int]:
    return transport_policy.proxy_byte_snapshot(proxy_bytes)


def _request_stage_kwargs(result: AttemptResult | None) -> dict:
    if result is None:
        return {}
    return {
        "request_upload_ms": result.request_upload_ms,
        "response_headers_wait_ms": result.response_headers_wait_ms,
        "response_body_first_byte_wait_ms": result.response_body_first_byte_wait_ms,
    }


def _scorer_connect_ms(result: AttemptResult | None):
    """Drop only the identifiable legacy header-timeout pseudo sample."""
    if result is None:
        return None
    return log_db.compatible_connect_ms(
        result.connect_ms,
        outcome=result.outcome,
        error_detail=result.error_detail,
        stage_timing_present=result.response_headers_wait_ms is not None,
    )


def _timing_stage_kwargs(timing, *, terminal: bool) -> dict:
    if timing is None:
        return {}
    snapshot = timing.snapshot(terminal=terminal)
    return {
        "request_upload_ms": snapshot.request_upload_ms,
        "response_headers_wait_ms": snapshot.response_headers_wait_ms,
        "response_body_first_byte_wait_ms": snapshot.response_body_first_byte_wait_ms,
    }


def _responses_upstream_ws_enabled(cfg: Optional[dict] = None) -> bool:
    return transport_policy.responses_upstream_ws_enabled(cfg)


def _should_use_responses_upstream_ws(
    ch: Channel,
    *,
    ingress_protocol: str,
    cfg: Optional[dict] = None,
) -> bool:
    return transport_policy.should_use_responses_upstream_ws(
        ch,
        ingress_protocol=ingress_protocol,
        cfg=cfg,
    )


# ─── 辅助 ─────────────────────────────────────────────────────────

def _elapsed_ms(start_monotonic: float) -> int:
    return max(0, int((time.monotonic() - start_monotonic) * 1000))


_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
_SECRET_RE = re.compile(r"(?i)\b(?:sk|sess|key)-[A-Za-z0-9_-]{8,}\b")
_FIELD_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|credential)"
    r"\s*[:=]\s*[^\s,;]+"
)


def _sanitize_upstream_message(value: Any) -> str:
    """Extract one readable, bounded message without nested JSON or credentials."""
    text = str(value or "").strip()
    text = re.sub(r"^HTTP\s+\d{3}\s*:\s*", "", text, flags=re.IGNORECASE)
    code_message = text
    for _ in range(2):
        candidate = code_message.strip()
        start = candidate.find("{")
        if start < 0:
            break
        try:
            obj = json.loads(candidate[start:])
        except Exception:
            break
        if not isinstance(obj, dict):
            break
        error_obj = obj.get("error")
        if isinstance(error_obj, dict):
            next_value = (
                error_obj.get("message") or error_obj.get("detail")
                or obj.get("message") or obj.get("detail")
            )
        elif isinstance(error_obj, str):
            next_value = error_obj
        else:
            next_value = obj.get("message") or obj.get("detail")
        if next_value is None:
            code_message = "Upstream returned an error"
            break
        code_message = str(next_value)
    leftover = code_message.lstrip()
    if leftover.startswith(("{", "[")):
        try:
            parsed = json.loads(leftover)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            code_message = "Upstream returned an error"
    code_message = _BEARER_RE.sub("Bearer [redacted]", code_message)
    code_message = _FIELD_SECRET_RE.sub("credential=[redacted]", code_message)
    code_message = _SECRET_RE.sub("[redacted credential]", code_message)
    code_message = _EMAIL_RE.sub("[redacted account]", code_message)
    code_message = re.sub(r"\s+", " ", code_message).strip()
    return (code_message or "Unknown upstream error")[:500]


def _structured_attempt_error(
    result: AttemptResult,
    ordinal: int,
    channel: Optional[Channel] = None,
) -> dict:
    quota = bounded_account_quota_error(result)
    status = int(result.http_status) if isinstance(result.http_status, int) else None
    detail = str(result.error_detail or "")
    code = str(result.error_code or "").strip() or None
    if code is None:
        start = detail.find("{")
        if start >= 0:
            try:
                obj = json.loads(detail[start:])
            except Exception:
                obj = None
            if isinstance(obj, dict):
                error_obj = obj.get("error") if isinstance(obj.get("error"), dict) else obj
                if isinstance(error_obj, dict):
                    raw_code = error_obj.get("code") or error_obj.get("type") or obj.get("code")
                    code = str(raw_code).strip() if raw_code is not None else None
    if code and (
        len(code) > 120
        or _EMAIL_RE.search(code)
        or _SECRET_RE.search(code)
        or _FIELD_SECRET_RE.search(code)
    ):
        code = None
    if result.openai_oauth_html_403:
        classification = "upstream_http_error"
    elif quota is not None:
        classification = quota["classification"]
    elif result.outcome in ("request_invalid", "guard_error", "candidate_guard"):
        classification = "invalid_request"
    elif status == 401:
        classification = "authentication_error"
    elif status == 403:
        classification = "permission_error"
    elif status == 429:
        classification = "rate_limit_error"
    elif status is not None and status >= 500:
        classification = "upstream_server_error"
    elif status is not None:
        classification = "upstream_http_error"
    elif "timeout" in str(result.outcome or ""):
        classification = "timeout"
    elif result.outcome in ("connect_error", "transport_error", "proxy_connect_error"):
        classification = "transport_error"
    else:
        classification = str(result.outcome or "upstream_error")

    transient_kind = retryable_transient_error_kind(channel, result) if channel is not None else None
    if quota is not None:
        retryable, retry_scope = True, "next_candidate"
    elif transient_kind:
        retryable, retry_scope = True, "same_candidate"
    elif classification == "invalid_request":
        retryable, retry_scope = False, "none"
    else:
        retryable, retry_scope = True, "next_candidate"
    return {
        "attempt": int(ordinal),
        "status": status,
        "classification": classification,
        "code": code,
        "message": (
            "Upstream returned an HTTP 403 response"
            if result.openai_oauth_html_403
            else _sanitize_upstream_message(
                quota.get("message") if quota is not None else detail
            )
        ),
        "retryable": retryable,
        "retry_scope": retry_scope,
        "openai_oauth_html_403": result.openai_oauth_html_403,
    }


def _structured_failure_details(attempts: list[dict]) -> dict:
    safe_attempts = [dict(item) for item in attempts]
    if not safe_attempts:
        safe_attempts = [{
            "attempt": 0,
            "status": None,
            "classification": "no_candidates",
            "code": None,
            "message": "No upstream candidate produced a response",
            "retryable": False,
            "retry_scope": "none",
        }]
    priority = {
        "invalid_request": 100,
        "balance_exhausted": 96,
        "quota_exhausted": 95,
        "authentication_error": 90,
        "permission_error": 85,
        "rate_limit_error": 80,
        "upstream_http_error": 75,
        "upstream_server_error": 70,
        "timeout": 60,
        "transport_error": 50,
    }
    root_attempt = max(
        safe_attempts,
        key=lambda item: priority.get(str(item.get("classification") or ""), 40),
    )
    root_cause = {
        key: root_attempt.get(key)
        for key in ("status", "classification", "code", "message")
    }
    # Attempt entries describe Parrot's internal candidate progression.  The
    # terminal root cause instead tells the downstream whether replaying the
    # whole Parrot request is useful after every candidate has been exhausted.
    if root_cause.get("classification") in {"balance_exhausted", "quota_exhausted"}:
        root_cause.update(retryable=False, retry_scope="none")
    elif bool(root_attempt.get("retryable")):
        root_cause.update(retryable=True, retry_scope="request")
    else:
        root_cause.update(retryable=False, retry_scope="none")
    status_text = f" {root_cause['status']}" if root_cause.get("status") is not None else ""
    summary = (
        f"Upstream{status_text} {root_cause['classification']}: "
        f"{root_cause['message']}"
    )
    if len(safe_attempts) > 1:
        summary += f" ({len(safe_attempts)} upstream attempts failed)"
    return {
        "summary": summary[:700],
        "root_cause": root_cause,
        "attempts": safe_attempts,
    }


# Only explicit transient provider signals are retried on the same candidate.
# The request-scoped budget prevents candidate count from multiplying retries.
_DEFAULT_TRANSIENT_RETRY_DELAYS_S = DEFAULT_TRANSIENT_RETRY_DELAYS_S
_MAX_CONFIGURED_TRANSIENT_RETRIES = MAX_CONFIGURED_TRANSIENT_RETRIES


def _effective_retry_cfg(cfg: Optional[dict] = None) -> dict:
    return cfg if isinstance(cfg, dict) else config.get()


def _transient_retry_config(cfg: Optional[dict] = None) -> dict:
    return transient_retry_config(_effective_retry_cfg(cfg))


def _transient_retry_limit(cfg: Optional[dict] = None) -> int:
    return transient_retry_limit(_effective_retry_cfg(cfg))


def _transient_retry_allowed(kind: str | None, cfg: Optional[dict] = None) -> bool:
    return transient_retry_allowed(kind, _effective_retry_cfg(cfg))


def _recovery_retry_allowed(name: str, cfg: Optional[dict] = None) -> bool:
    return recovery_retry_allowed(name, _effective_retry_cfg(cfg))


def _configured_transient_retry_delays() -> tuple[float, ...]:
    return configured_transient_retry_delays(config.get())


def _overload_retry_delay_seconds(retry_ordinal: int) -> float:
    """Compatibility-named delay hook, now shared by all transient retry kinds."""
    delays = _configured_transient_retry_delays()
    index = min(max(0, int(retry_ordinal)), len(delays) - 1)
    return delays[index] + random.uniform(0.0, 0.25)


async def _wait_for_overload_retry(
    retry_ordinal: int,
    deadline_ts: float,
    *,
    retry_after_seconds: float | None = None,
) -> float | None:
    """Wait Retry-After or configured jittered backoff within the request deadline."""
    if retry_after_seconds is None:
        delay = _overload_retry_delay_seconds(retry_ordinal)
    else:
        parsed = parse_retry_after_seconds(retry_after_seconds)
        delay = (
            _overload_retry_delay_seconds(retry_ordinal)
            if parsed is None
            else parsed
        )
    if deadline_ts > 0 and time.time() + delay >= deadline_ts:
        return None
    await asyncio.sleep(delay)
    return delay


def _notify_zhipu_quota_cooldown(ch: Channel, model: str, reset_ms: int) -> None:
    """Best-effort TG notice with a direct link to an API channel's detail page."""
    channel_name = str(getattr(ch, "display_name", None) or getattr(ch, "key", "?"))
    reset_text = quota_errors.format_bjt_ms(reset_ms)
    ek = notifier.escape_html
    reply_markup = None
    if getattr(ch, "type", "") == "api":
        try:
            from .telegram import ui as telegram_ui
            short = telegram_ui.register_code(channel_name)
            reply_markup = telegram_ui.inline_kb([
                [telegram_ui.btn("📡 查看渠道详情", f"ch:view:{short}:1")],
            ])
        except Exception:
            reply_markup = None
    notifier.throttled_notify_event_sync(
        "quota_cooldown",
        f"quota_cooldown:{getattr(ch, 'key', channel_name)}:{model}:{reset_ms}",
        "🟠 <b>渠道模型进入配额冷却</b>\n"
        f"渠道: <code>{ek(channel_name)}</code>\n"
        f"模型: <code>{ek(model)}</code>\n"
        "原因: <b>周/月使用额度已达上限</b>（上游 <code>1310</code>）\n"
        f"自动恢复: <code>{ek(reset_text)}</code>（北京时间）\n\n"
        "<b>调度影响</b>\n"
        f"恢复前仅跳过 <code>{ek(channel_name)} / {ek(model)}</code>；"
        "同模型的其他渠道仍可继续承接请求。\n\n"
        "<i>这不是手动禁用，也不是永久冻结。到达上游给出的重置时间后自动恢复调度。</i>",
        cooldown_seconds=86_400,
        reply_markup=reply_markup,
    )


def _err_type_from_outcome(outcome: str, http_status: Optional[int]) -> str:
    return protocol_errors.classify_attempt_outcome(outcome, http_status).anthropic_error_type


def _pick_upstream_headers(resp: httpx.Response) -> dict:
    """转发部分上游 headers 到下游（限定范围）。"""
    return metadata_from_response(resp).forward_headers()


def _attach_retry_after_from_response(
    result: AttemptResult,
    response: httpx.Response | None,
    channel: Channel | None = None,
) -> AttemptResult:
    if result.retry_after_seconds is None and response is not None:
        try:
            raw = response.headers.get("Retry-After")
        except Exception:
            raw = None
        parsed = parse_retry_after_seconds(raw)
        if parsed is not None:
            result.retry_after_seconds = parsed
            if result.http_status == 429:
                result.cooldown_until = retry_after_cooldown_until(raw)
    if str(getattr(channel, "provider", "")) == "antigravity" and result.http_status == 429:
        google = parse_antigravity_429(result.full_response_text or result.error_detail)
        if google.get("quota_exhausted") and not result.error_code:
            result.error_code = str(google.get("reason") or "quota_exhausted")
        delay = google.get("retry_after")
        if result.retry_after_seconds is None and isinstance(delay, (int, float)):
            result.retry_after_seconds = parse_retry_after_seconds(delay)
            # Short delays are consumed by same-owner retry and must not park the
            # model. Medium delays become an exact non-blocking model cooldown.
            if 3 <= float(delay) < 300:
                result.cooldown_until = int((time.time() + float(delay)) * 1000)
    return result


def _response_body_text(response: Response) -> str | None:
    body = getattr(response, "body", None)
    if isinstance(body, str):
        return body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", errors="replace")
    return None


def _anthropic_json_from_response(response: Response) -> tuple[dict | None, str | None]:
    raw = _response_body_text(response) or ""
    if not raw:
        return None, "empty compact rescue response"
    try:
        obj = json.loads(raw)
    except Exception as exc:
        return None, f"invalid compact rescue response json: {exc}"
    if isinstance(obj, dict) and obj.get("type") == "error":
        err = obj.get("error") if isinstance(obj.get("error"), dict) else obj
        msg = err.get("message") if isinstance(err, dict) else None
        return None, str(msg or "compact rescue upstream error")
    if not isinstance(obj, dict):
        return None, "compact rescue response is not an object"
    return obj, None


def _compact_response_log_fields(response: Response, fallback_model: str) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "final_model": fallback_model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
    }
    raw = _response_body_text(response)
    if not raw:
        return fields
    try:
        obj = json.loads(raw)
    except Exception:
        return fields
    if not isinstance(obj, dict):
        return fields
    model = str(obj.get("model") or "").strip()
    if model:
        fields["final_model"] = model
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    fields["input_tokens"] = int(usage.get("input_tokens") or 0)
    fields["output_tokens"] = int(usage.get("output_tokens") or 0)
    fields["cache_creation_tokens"] = int(
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_creation_tokens")
        or 0
    )
    fields["cache_read_tokens"] = int(
        usage.get("cache_read_input_tokens")
        or usage.get("cache_read_tokens")
        or 0
    )
    return fields


async def _finish_compact_success(
    request_id: str,
    body: dict,
    response: Response,
    start_monotonic: float,
    *,
    retry_count: int,
    affinity_hit: int,
) -> None:
    request_elapsed_ms = _elapsed_ms(start_monotonic)
    raw_body = _response_body_text(response)
    fields = _compact_response_log_fields(
        response,
        fallback_model=str(body.get("model") or "compact"),
    )
    await asyncio.to_thread(
        log_db.finish_success,
        request_id,
        "compact-rescue",
        "internal",
        fields["final_model"],
        input_tokens=fields["input_tokens"],
        output_tokens=fields["output_tokens"],
        cache_creation_tokens=fields["cache_creation_tokens"],
        cache_read_tokens=fields["cache_read_tokens"],
        total_ms=None,
        request_elapsed_ms=request_elapsed_ms,
        retry_count=max(0, retry_count),
        affinity_hit=affinity_hit,
        response_body=raw_body,
        upstream_protocol="compact-rescue",
    )


async def _run_compact_direct_rescue_with_compression_model(
    body: dict,
    request_id: str,
    api_key_name: Optional[str],
    client_ip: str,
    *,
    ingress_protocol: str,
) -> tuple[Response | None, str | None]:
    """Try Claude Code compact with the configured large compression model.

    This is the fast path before map-reduce: if the configured compression
    model's metadata says it can fit the entire compact request plus summary
    reserve and buffer, call it directly.  Any failure returns (None, reason) so
    callers can fall back to segmented map-reduce.
    """
    compression_model = model_metadata.get_compression_model()
    if not compression_model:
        return None, "no compression model configured"

    from . import scheduler as scheduler_mod

    probe_body, _ = compact_rescue.sanitized_compact_base(body)
    probe_body.update({
        "model": compression_model,
        "stream": False,
        "_client_visible_model": compression_model,
    })
    route = scheduler_mod.schedule(
        probe_body,
        api_key_name=api_key_name or "",
        client_ip=client_ip,
        ingress_protocol=ingress_protocol,
    )
    if not route:
        return None, f"compression model {compression_model} has no available route"
    first_routes = list(route.candidates) + list(route.saturated)
    metadata_channel, metadata_outbound = first_routes[0]
    scope_key = str(metadata_channel.key)
    reserve = model_metadata.summary_reserve_tokens(
        compression_model, scope_key=scope_key, outbound_model=metadata_outbound,
    )
    direct_body = compact_rescue.build_direct_summary_body(
        body,
        model=compression_model,
        max_tokens=reserve,
    )
    direct_body["_client_visible_model"] = compression_model

    prompt_tokens = token_counter.count_request_tokens(direct_body, model=compression_model)
    use_max_context = _channel_uses_max_context(
        metadata_channel, body, str(metadata_outbound or compression_model),
    )
    if not model_metadata.can_fit_for_compact(
        compression_model, prompt_tokens,
        scope_key=scope_key, outbound_model=metadata_outbound,
        use_max_context=use_max_context,
    ):
        required = model_metadata.required_context_for_compact(
            prompt_tokens, compression_model,
            scope_key=scope_key, outbound_model=metadata_outbound,
        )
        window = model_metadata.context_window(
            compression_model,
            scope_key=scope_key,
            outbound_model=metadata_outbound,
            use_max_context=use_max_context,
        )
        trigger = model_metadata.compact_trigger_tokens(
            compression_model,
            scope_key=scope_key,
            outbound_model=metadata_outbound,
            use_max_context=use_max_context,
        )
        safe_limit = model_metadata.safe_prompt_limit(
            compression_model,
            scope_key=scope_key,
            outbound_model=metadata_outbound,
            use_max_context=use_max_context,
        )
        return (
            None,
            f"compression model {compression_model} prompt exceeds compact limit: "
            f"prompt={prompt_tokens} limit={safe_limit} trigger={trigger} "
            f"required={required} window={window}",
        )

    print(
        f"[compact-rescue] direct compression request={request_id} "
        f"model={compression_model} prompt_tokens≈{prompt_tokens}"
    )
    response = await run_failover(
        route,
        direct_body,
        f"{request_id}:compact:direct",
        api_key_name,
        client_ip,
        False,
        time.time(),
        ingress_protocol=ingress_protocol,
    )
    obj, err = _anthropic_json_from_response(response)
    if err or obj is None:
        return None, f"compression model failed: {err}"
    text = compact_rescue.extract_anthropic_message_text(obj).strip()
    if not text:
        return None, "compression model produced empty summary"
    print(
        f"[compact-rescue] direct compression succeeded request={request_id} "
        f"summary_chars={len(text)}"
    )
    return response, None


def _schedule_compact_compression_model_for_map_reduce(
    body: dict,
    api_key_name: Optional[str],
    client_ip: str,
    *,
    ingress_protocol: str,
) -> tuple[str | None, ScheduleResult | None, str | None]:
    """Return a route for compact segment/reduce calls using compressionModel.

    Even when the full compact prompt cannot fit the compression model for a
    single direct call, the configured compression model should still own the
    segmented map-reduce work.  Only fall back to the original model route when
    no compression model or no route is available.
    """
    compression_model = model_metadata.get_compression_model()
    if not compression_model:
        return None, None, "no compression model configured"

    from . import scheduler as scheduler_mod

    probe_body, _meta = compact_rescue.sanitized_compact_base(body)
    probe_body["stream"] = False
    probe_body["model"] = compression_model
    probe_body["_client_visible_model"] = compression_model
    probe_body["max_tokens"] = compact_rescue.reduce_max_tokens()
    probe_body.pop("max_output_tokens", None)
    route = scheduler_mod.schedule(
        probe_body,
        api_key_name=api_key_name or "",
        client_ip=client_ip,
        ingress_protocol=ingress_protocol,
    )
    if not route:
        return compression_model, None, f"compression model {compression_model} has no available route"
    first_routes = list(route.candidates) + list(route.saturated)
    metadata_channel, metadata_outbound = first_routes[0]
    binding = model_metadata.resolve_binding(
        compression_model,
        scope_key=str(metadata_channel.key),
        outbound_model=str(metadata_outbound),
    )
    if binding is None:
        return (
            compression_model,
            None,
            f"compression model {compression_model} has no effective metadata binding",
        )
    return compression_model, route, None


async def _run_compact_map_reduce_rescue(
    schedule_result: ScheduleResult,
    body: dict,
    request_id: str,
    api_key_name: Optional[str],
    client_ip: str,
    is_stream: bool,
    start_time: float,
    start_monotonic: float,
    *,
    ingress_protocol: str,
    affinity_hit: int,
) -> Response:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    direct_response, direct_skip_reason = await _run_compact_direct_rescue_with_compression_model(
        body,
        request_id,
        api_key_name,
        client_ip,
        ingress_protocol=ingress_protocol,
    )
    if direct_response is not None:
        await _finish_compact_success(
            request_id,
            body,
            direct_response,
            start_monotonic,
            retry_count=0,
            affinity_hit=affinity_hit,
        )
        if is_stream:
            return local_web_tools.maybe_wrap_anthropic_json_response_as_sse(direct_response)
        return direct_response
    if direct_skip_reason:
        print(f"[compact-rescue] direct compression skipped request={request_id}: {direct_skip_reason}")

    map_reduce_model, map_reduce_route, map_reduce_skip_reason = _schedule_compact_compression_model_for_map_reduce(
        body,
        api_key_name,
        client_ip,
        ingress_protocol=ingress_protocol,
    )
    if map_reduce_route is not None and map_reduce_model:
        print(
            f"[compact-rescue] map-reduce using compression model request={request_id} "
            f"model={map_reduce_model}"
        )
    elif map_reduce_skip_reason:
        print(f"[compact-rescue] map-reduce compression model skipped request={request_id}: {map_reduce_skip_reason}")
    active_schedule_result = map_reduce_route or schedule_result
    active_model = map_reduce_model if map_reduce_route is not None else str(body.get("model") or "")
    active_routes = list(active_schedule_result.candidates) + list(active_schedule_result.saturated)
    active_channel, active_outbound = active_routes[0] if active_routes else (None, None)
    active_scope = str(getattr(active_channel, "key", "") or "")
    bound_output_limit = model_metadata.max_output_tokens(
        active_model,
        scope_key=active_scope,
        outbound_model=str(active_outbound or ""),
    )
    bound_prompt_limit = model_metadata.safe_prompt_limit(
        active_model,
        scope_key=active_scope,
        outbound_model=str(active_outbound or ""),
        use_max_context=_channel_uses_max_context(
            active_channel, body, str(active_outbound or active_model),
        ),
    )
    segment_target = compact_rescue.chunk_target_tokens()
    if map_reduce_route is not None and bound_prompt_limit is not None:
        segment_target = max(1, min(segment_target, bound_prompt_limit))

    chunks = compact_rescue.split_messages_for_compact(
        messages,
        target_tokens=segment_target,
        model=active_model,
    )
    print(
        f"[compact-rescue] map-reduce request={request_id} "
        f"messages={len(messages)} chunks={len(chunks)} "
        f"target_tokens={compact_rescue.chunk_target_tokens()} "
        f"model={active_model or body.get('model')}"
    )
    try:
        async def run_segment(idx: int, chunk: list[Any]) -> str:
            chunk_body = compact_rescue.build_segment_summary_body(
                body,
                chunk,
                segment_index=idx,
                segment_count=len(chunks),
            )
            if map_reduce_route is not None and map_reduce_model:
                chunk_body["model"] = map_reduce_model
            chunk_body["_client_visible_model"] = active_model
            if bound_output_limit is not None:
                chunk_body["max_tokens"] = min(
                    int(chunk_body.get("max_tokens") or bound_output_limit),
                    bound_output_limit,
                )
            sub_id = f"{request_id}:compact:{idx}"
            response = await run_failover(
                active_schedule_result,
                chunk_body,
                sub_id,
                api_key_name,
                client_ip,
                False,
                time.time(),
                ingress_protocol=ingress_protocol,
            )
            obj, err = _anthropic_json_from_response(response)
            if err or obj is None:
                raise RuntimeError(f"segment {idx}/{len(chunks)} failed: {err}")
            text = compact_rescue.extract_anthropic_message_text(obj).strip()
            if not text:
                raise RuntimeError(f"segment {idx}/{len(chunks)} produced empty summary")
            print(f"[compact-rescue] segment {idx}/{len(chunks)} summary chars={len(text)}")
            return text

        concurrency = compact_rescue.segment_concurrency()
        print(
            f"[compact-rescue] running {len(chunks)} segment summaries in parallel "
            f"concurrency={'unlimited' if concurrency <= 0 else concurrency}"
        )
        if concurrency > 0:
            sem = asyncio.Semaphore(concurrency)

            async def run_segment_limited(idx: int, chunk: list[Any]) -> str:
                async with sem:
                    return await run_segment(idx, chunk)

            segment_results = await asyncio.gather(
                *(run_segment_limited(idx, chunk) for idx, chunk in enumerate(chunks, start=1)),
                return_exceptions=True,
            )
        else:
            segment_results = await asyncio.gather(
                *(run_segment(idx, chunk) for idx, chunk in enumerate(chunks, start=1)),
                return_exceptions=True,
            )
        summaries: list[str] = []
        for idx, result in enumerate(segment_results, start=1):
            if isinstance(result, Exception):
                raise RuntimeError(f"segment {idx}/{len(chunks)} failed: {result}")
            summaries.append(result)

        reduce_body = compact_rescue.build_reduce_summary_body(body, summaries)
        if map_reduce_route is not None and map_reduce_model:
            reduce_body["model"] = map_reduce_model
        reduce_body["_client_visible_model"] = active_model
        if bound_output_limit is not None:
            reduce_body["max_tokens"] = min(
                int(reduce_body.get("max_tokens") or bound_output_limit),
                bound_output_limit,
            )
        final_response = await run_failover(
            active_schedule_result,
            reduce_body,
            f"{request_id}:compact:reduce",
            api_key_name,
            client_ip,
            False,
            time.time(),
            ingress_protocol=ingress_protocol,
        )
        obj, err = _anthropic_json_from_response(final_response)
        if err or obj is None:
            raise RuntimeError(f"reduce failed: {err}")
        text = compact_rescue.extract_anthropic_message_text(obj).strip()
        if not text:
            raise RuntimeError("reduce produced empty summary")
        await _finish_compact_success(
            request_id,
            body,
            final_response,
            start_monotonic,
            retry_count=len(chunks),
            affinity_hit=affinity_hit,
        )
        if is_stream:
            return local_web_tools.maybe_wrap_anthropic_json_response_as_sse(final_response)
        return final_response
    except Exception as exc:
        msg = f"compact rescue failed: {exc}"
        request_elapsed_ms = _elapsed_ms(start_monotonic)
        await asyncio.to_thread(
            log_db.finish_error,
            request_id,
            msg[:4000],
            0,
            final_channel_key="compact-rescue",
            final_channel_type="internal",
            final_model=str(body.get("model") or "compact"),
            total_ms=None,
            request_elapsed_ms=request_elapsed_ms,
            http_status=500,
            affinity_hit=affinity_hit,
            upstream_protocol="compact-rescue",
        )
        return _json_error_for_ingress(ingress_protocol, 500, errors.ErrType.API, msg)


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
    start_monotonic: float | None = None,
) -> Response:
    """执行调度候选的顺序重试。返回 FastAPI Response。

    内部完成：
      - retry_chain 插入 / 更新
      - scorer / cooldown 更新（成功清零、失败记入）
      - affinity 命中 touch；成功后（non-stream 或 stream 全量完成）写入新绑定
      - log_db 的 finish_success / finish_error
    """
    if start_monotonic is None:
        # Legacy callers start outer elapsed at entry; wall time never enters durations.
        start_monotonic = time.monotonic()
    candidates = list(schedule_result.candidates)
    client_visible_model = str(
        body.get("_client_visible_model") or body.get("model") or ""
    ).strip()
    affinity_hit = 1 if schedule_result.affinity_hit else 0
    fp_query = schedule_result.fp_query
    client_key = getattr(schedule_result, "client_key", None)
    bound_channel_key = getattr(schedule_result, "bound_channel_key", None)
    encrypted_content_count = int(
        getattr(schedule_result, "encrypted_content_count", 0) or 0
    )
    portable_body: Optional[dict] = None
    request_has_compaction = compaction_owner.has_complete_compaction(body)
    if encrypted_content_count > 0 and not request_has_compaction:
        stripped_body, removed_ec = _retry_body_without_encrypted_content(body)
        if removed_ec > 0:
            portable_body = stripped_body

    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    total_timeout = int(timeouts.get("total", 600))
    deadline_ts = start_time + total_timeout

    if ingress_protocol == "anthropic" and compact_rescue.is_claude_code_compact_request(body):
        try:
            log_db.update_pending(
                request_id,
                msg_count=len(body.get("messages") or []),
                tool_count=0,
                reasoning_effort=None,
            )
        except Exception:
            pass
        if is_stream:
            task = asyncio.create_task(_run_compact_map_reduce_rescue(
                schedule_result,
                body,
                request_id,
                api_key_name,
                client_ip,
                False,
                start_time,
                start_monotonic,
                ingress_protocol=ingress_protocol,
                affinity_hit=affinity_hit,
            ))
            return local_web_tools.stream_anthropic_response_task_with_pings(task)
        return await _run_compact_map_reduce_rescue(
            schedule_result,
            body,
            request_id,
            api_key_name,
            client_ip,
            False,
            start_time,
            start_monotonic,
            ingress_protocol=ingress_protocol,
            affinity_hit=affinity_hit,
        )

    # Anthropic web_search/web_fetch server tools cannot be executed by
    # OpenAI-family upstreams.  When such tools are declared, Parrot runs a
    # small non-streaming tool loop locally (AnySearch-backed) and converts the
    # final answer back to SSE if the client requested streaming.
    local_web_loop_active = (
        ingress_protocol == "anthropic"
        and local_web_tools.request_declares_supported_tools(body)
        and local_web_tools.max_tool_rounds() > 0
    )
    openai_local_web_loop_active = (
        ingress_protocol == "responses"
        and local_web_tools.openai_responses_local_web_active(body)
        and local_web_tools.max_tool_rounds() > 0
    )
    if local_web_loop_active and is_stream:
        inner_body = dict(body)
        inner_body["stream"] = False
        task = asyncio.create_task(run_failover(
            schedule_result,
            inner_body,
            request_id,
            api_key_name,
            client_ip,
            False,
            start_time,
            ingress_protocol=ingress_protocol,
            start_monotonic=start_monotonic,
        ))
        return local_web_tools.stream_anthropic_response_task_with_pings(task)
    if openai_local_web_loop_active and is_stream:
        inner_body = dict(body)
        inner_body["stream"] = False
        task = asyncio.create_task(run_failover(
            schedule_result,
            inner_body,
            request_id,
            api_key_name,
            client_ip,
            False,
            start_time,
            ingress_protocol=ingress_protocol,
            start_monotonic=start_monotonic,
        ))
        return local_web_tools.stream_responses_response_task_with_pings(task)

    downstream_stream_requested = bool(is_stream)
    local_web_rounds = 0
    local_web_limit_reported = False

    retry_count = 0
    # Shared across the whole request: changing candidates must not replenish
    # the configured same-candidate transient retry budget.
    transient_retry_limit = _transient_retry_limit(cfg)
    transient_retries_used = 0
    refreshed_once: set[str] = set()
    retried_without_context_1m: set[tuple[str, str]] = set()
    retried_without_encrypted_content = False
    last_result: Optional[AttemptResult] = None
    structured_attempts: list[dict] = []
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
        acquired = await concurrency.try_acquire(channel_state.effect_key(ch))
        if not acquired:
            # 竞态：filter 时还有位置，现在满了 → 作为排队备选
            # 注：_filter_candidates 已把饱和的挑走，这里主要兜底并发 filter 后瞬间占满的情况
            saturated_extras.append((ch, resolved_model))
            idx += 1
            continue

        # Resolve proxy for this attempt (read from proxy manager)
        _attempt_proxy: str | None = _pick_non_direct_proxy_name(ch, resolved_model)

        attempt_started_monotonic = time.monotonic()
        attempt_id = log_db.record_retry_attempt(
            request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
            proxy_name=_attempt_proxy,
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
            client_visible_model=client_visible_model,
        )
        if _attempt_proxy:
            log_db.update_pending(request_id, proxy_name=_attempt_proxy)

        release_done = False
        def _release_once(_key=channel_state.effect_key(ch)):
            nonlocal release_done
            if release_done:
                return
            release_done = True
            concurrency.release(_key)

        try:
            candidate_local_web_loop = local_web_loop_active and getattr(ch, "protocol", "anthropic") != "anthropic"
            candidate_openai_local_web_loop = openai_local_web_loop_active
            effective_is_stream = is_stream and not (candidate_local_web_loop or candidate_openai_local_web_loop)
            attempt_body = _attempt_body_for_channel(
                body, ch.key, bound_channel_key, portable_body,
            )
            if (candidate_local_web_loop or candidate_openai_local_web_loop) and attempt_body.get("stream"):
                attempt_body = dict(attempt_body)
                attempt_body["stream"] = False
            if _should_use_responses_upstream_ws(ch, ingress_protocol=ingress_protocol, cfg=cfg):
                result = await _try_openai_oauth_responses_ws_channel(
                    ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                    fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                    request_id, retry_count, affinity_hit, client_key=client_key,
                    retry_attempt_id=attempt_id,
                    start_monotonic=start_monotonic,
                    attempt_start_monotonic=attempt_started_monotonic,
                )
            else:
                result = await _try_channel(
                    ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                    fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                    request_id, retry_count, affinity_hit,
                    ingress_protocol=ingress_protocol,
                    client_key=client_key,
                    retry_attempt_id=attempt_id,
                    start_monotonic=start_monotonic,
                    attempt_start_monotonic=attempt_started_monotonic,
                    terminal_release=_release_once,
                )
        except BaseException:
            _release_once()
            raise
        result = _request_invalid_result_if_needed(result)
        last_result = result
        quota_exhaustion = bounded_account_quota_error(result)
        if not result.success and not result.stream_started:
            structured_attempts.append(
                _structured_attempt_error(result, attempt_order, ch),
            )
        if _attempt_proxy and not result.proxy_name:
            result.proxy_name = _attempt_proxy

        log_db.update_retry_attempt(
            attempt_id,
            final_round_id=result.round_id,
            connect_ms=result.connect_ms,
            first_byte_ms=result.first_byte_ms,
            idle_ms=result.idle_ms,
            attempt_elapsed_ms=(
                None if result.stream_started else _elapsed_ms(attempt_started_monotonic)
            ),
            request_upload_ms=result.request_upload_ms,
            response_headers_wait_ms=result.response_headers_wait_ms,
            response_body_first_byte_wait_ms=result.response_body_first_byte_wait_ms,
            total_ms=result.total_ms,
            ended_at=(None if result.stream_started else time.time()),
            outcome=("open" if result.stream_started else result.outcome),
            error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
            proxy_name=result.proxy_name,
            bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
            bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
            response_body=getattr(result, "full_response_text", None),
            usage=getattr(result, "usage", None),
            usage_observed=getattr(result, "usage_observed", None),
        )

        if result.success and candidate_local_web_loop:
            assistant_msg = result.assistant_response if isinstance(result.assistant_response, dict) else None
            assistant_msg = local_web_tools.normalize_assistant_message_for_local_tools(assistant_msg)
            local_calls = local_web_tools.extract_local_tool_calls(
                assistant_msg,
                body.get("tools") or [],
                conversation_body=body,
            )
            tool_use_total = local_web_tools.tool_use_count(assistant_msg)
            if local_calls and len(local_calls) == tool_use_total:
                # finish_success() has recorded this real upstream call and
                # released the terminal handle. The tool loop proves another
                # round is needed, so keep subsequent rows in the same month.
                log_db.retain_request_handle(request_id, attempt_id)
                max_rounds = local_web_tools.max_tool_rounds()
                if local_web_rounds >= max_rounds:
                    if local_web_limit_reported:
                        _release_once()
                        msg = "local web tool loop kept requesting WebSearch/WebFetch after maxToolRounds"
                        request_elapsed_ms = _elapsed_ms(start_monotonic)
                        await asyncio.to_thread(
                            log_db.finish_error, request_id, msg, retry_count,
                            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                            connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms,
                            idle_ms=result.idle_ms, total_ms=result.total_ms,
                            final_round_id=result.round_id, request_elapsed_ms=request_elapsed_ms,
                            http_status=400, affinity_hit=affinity_hit,
                            upstream_protocol=getattr(ch, "protocol", "anthropic"),
                            proxy_name=result.proxy_name,
                            proxy_bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
                            proxy_bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
                            **_request_stage_kwargs(result),
                        )
                        return _json_error_for_ingress(ingress_protocol, 400, errors.ErrType.INVALID_REQUEST, msg)

                    local_web_limit_reported = True
                    removed = local_web_tools.remove_supported_tools_from_body(body)
                    log_db.update_retry_attempt(
                        attempt_id,
                        outcome="local_web_tool_limit",
                        error_detail=(
                            f"maxToolRounds={max_rounds}; appended synthetic tool_result(s) "
                            f"for {len(local_calls)} call(s); removed_tools={removed}"
                        ),
                    )
                    print(
                        f"[local-web-tools] maxToolRounds reached for request {request_id}; "
                        f"appending limit tool_result(s), removed_tools={removed}"
                    )
                    _release_once()
                    tool_results = local_web_tools.round_limit_results(local_calls, max_rounds)
                    local_web_tools.append_tool_results_to_body(body, assistant_msg or {}, tool_results)
                    continue

                local_web_rounds += 1
                log_db.update_retry_attempt(
                    attempt_id,
                    outcome="local_web_tool_round",
                    error_detail=f"executed {len(local_calls)} local web tool call(s), round={local_web_rounds}",
                )
                print(
                    f"[local-web-tools] executing {len(local_calls)} call(s) "
                    f"for request {request_id} round={local_web_rounds}"
                )
                _release_once()
                tool_results = await local_web_tools.execute_local_tool_calls(
                    local_calls, request_id=request_id, round_no=local_web_rounds
                )
                local_web_tools.append_tool_results_to_body(body, assistant_msg or {}, tool_results)
                continue

        if result.success and candidate_openai_local_web_loop:
            response_obj = None
            try:
                raw_body = getattr(result.response, "body", b"")
                response_obj = json.loads(raw_body.decode("utf-8")) if raw_body else None
            except Exception:
                response_obj = None
            assistant_msg = local_web_tools.openai_response_assistant_message(response_obj)
            assistant_msg = local_web_tools.normalize_assistant_message_for_local_tools(assistant_msg)
            local_calls = local_web_tools.extract_local_tool_calls(
                assistant_msg,
                body.get("tools") or [],
                conversation_body=body,
            )
            tool_use_total = local_web_tools.tool_use_count(assistant_msg)
            if local_calls and len(local_calls) == tool_use_total:
                log_db.retain_request_handle(request_id, attempt_id)
                max_rounds = local_web_tools.max_tool_rounds()
                if local_web_rounds >= max_rounds:
                    if local_web_limit_reported:
                        _release_once()
                        msg = "local OpenAI web_search loop kept requesting web_search after maxToolRounds"
                        request_elapsed_ms = _elapsed_ms(start_monotonic)
                        await asyncio.to_thread(
                            log_db.finish_error, request_id, msg, retry_count,
                            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                            connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms,
                            idle_ms=result.idle_ms, total_ms=result.total_ms,
                            final_round_id=result.round_id, request_elapsed_ms=request_elapsed_ms,
                            http_status=400, affinity_hit=affinity_hit,
                            upstream_protocol=getattr(ch, "protocol", "anthropic"),
                            proxy_name=result.proxy_name,
                            proxy_bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
                            proxy_bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
                            **_request_stage_kwargs(result),
                        )
                        return _json_error_for_ingress(ingress_protocol, 400, errors.ErrType.INVALID_REQUEST, msg)

                    local_web_limit_reported = True
                    removed = local_web_tools.remove_openai_supported_tools_from_body(body)
                    log_db.update_retry_attempt(
                        attempt_id,
                        outcome="local_openai_web_tool_limit",
                        error_detail=(
                            f"maxToolRounds={max_rounds}; appended synthetic function_call_output(s) "
                            f"for {len(local_calls)} call(s); removed_tools={removed}"
                        ),
                    )
                    print(
                        f"[local-web-tools] OpenAI maxToolRounds reached for request {request_id}; "
                        f"appending limit function_call_output(s), removed_tools={removed}"
                    )
                    _release_once()
                    tool_results = local_web_tools.round_limit_results(local_calls, max_rounds)
                    local_web_tools.append_openai_tool_results_to_body(body, assistant_msg or {}, tool_results)
                    continue

                local_web_rounds += 1
                log_db.update_retry_attempt(
                    attempt_id,
                    outcome="local_openai_web_tool_round",
                    error_detail=f"executed {len(local_calls)} local OpenAI web_search call(s), round={local_web_rounds}",
                )
                print(
                    f"[local-web-tools] executing {len(local_calls)} OpenAI web_search call(s) "
                    f"for request {request_id} round={local_web_rounds}"
                )
                _release_once()
                tool_results = await local_web_tools.execute_local_tool_calls(
                    local_calls, request_id=request_id, round_no=local_web_rounds
                )
                local_web_tools.append_openai_tool_results_to_body(body, assistant_msg or {}, tool_results)
                continue

        if result.success or result.stream_started:
            # Non-stream success is also rebound at the orchestration boundary so
            # every successful candidate (including specialized transports) moves
            # the stable session owner.  Streaming success rebinds on completion.
            if result.success and not result.stream_started and fp_query:
                affinity.upsert(
                    fp_query, channel_state.effect_key(ch), resolved_model,
                    prompt_cache_key=_openai_prompt_cache_key_from_body(ingress_protocol, body),
                )
            # 成功已完成；或已发首包但出错（已用 SSE error 收尾）
            # 注意：scorer / cooldown / affinity / log_db 在 _try_channel 内完成
            # 并发 slot release 挂到响应体 finally：stream 消费完 / 客户端断开都会释放
            if result.success and candidate_local_web_loop and downstream_stream_requested:
                result.response = local_web_tools.maybe_wrap_anthropic_json_response_as_sse(result.response)
            if result.success and candidate_openai_local_web_loop and downstream_stream_requested:
                result.response = local_web_tools.maybe_wrap_responses_json_response_as_sse(result.response)
            _attach_release_to_response(result.response, _release_once)
            return result.response
        # 非成功：立即释放 slot，进入下一候选
        _release_once()

        # Antigravity's explicit quota reasons (or RetryInfo >=5 minutes) are
        # account-wide, not a model-local throttle. Advance the existing quota
        # observation generation before moving to another candidate so recovery
        # cannot race a newer exhaustion observation.
        if (
            str(getattr(ch, "provider", "")) == "antigravity"
            and result.http_status == 429
        ):
            google_quota = parse_antigravity_429(
                result.full_response_text or result.error_detail
            )
            if google_quota.get("quota_exhausted"):
                account_key = getattr(ch, "account_key", None)
                if account_key:
                    try:
                        oauth_manager.set_disabled_by_quota(account_key, None)
                    except Exception as exc:
                        print(f"[failover] Antigravity quota disable failed for {account_key}: {exc}")
                retry_count += 1
                idx += 1
                continue

        # 请求级 guard 错误：所有 openai 候选语义一致，切也无用，直接短路 4xx
        if result.outcome == "guard_error":
            status = int(result.http_status or 400)
            msg = result.error_detail or "request rejected by guard"
            # err_type 直接从 status 反推（保持与 classify_http_status 一致）
            anth_err_type = protocol_errors.legacy_anthropic_error_type_for_http_status(status)
            request_elapsed_ms = _elapsed_ms(start_monotonic)
            await asyncio.to_thread(
                log_db.finish_error, request_id, msg[:4000], retry_count,
                final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                connect_ms=None, first_token_ms=None, total_ms=None,
                request_elapsed_ms=request_elapsed_ms,
                http_status=status, affinity_hit=affinity_hit,
                upstream_protocol=getattr(ch, "protocol", "anthropic"),
            )
            return _json_error_for_ingress(ingress_protocol, status, anth_err_type, msg)

        # 下游请求内容错误（例如坏 encrypted_content）：不要冷却渠道。
        # 对坏 EC 做一次同渠道降级重试：剥掉本次请求 input 里的 encrypted_content，
        # 让下游 transcript owner 在成功响应中拿到新的 EC；失败则返回 400。
        if result.outcome == "request_invalid":
            msg = result.error_detail or "invalid request"
            if _invalid_ec_cleanup_retry_allowed(
                body, msg, cfg, retried_without_encrypted_content,
            ):
                cleared_replay = _maybe_clear_codex_reasoning_replay(result.translator_ctx)
                retry_body, removed_ec = _retry_body_without_encrypted_content(body)
                if removed_ec > 0 or cleared_replay:
                    retried_without_encrypted_content = True
                    body = retry_body
                    retry_count += 1
                    print(
                        f"[failover] invalid encrypted_content for {ch.key}/{resolved_model}; "
                        f"retrying same channel without {removed_ec} encrypted_content item(s)"
                    )
                    continue
            status = int(result.http_status or 400)
            request_elapsed_ms = _elapsed_ms(start_monotonic)
            await asyncio.to_thread(
                log_db.finish_error, request_id, msg[:4000], retry_count,
                final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms,
                idle_ms=result.idle_ms, total_ms=result.total_ms,
                final_round_id=result.round_id, request_elapsed_ms=request_elapsed_ms,
                http_status=status, affinity_hit=affinity_hit,
                response_body=result.full_response_text,
                usage=result.usage,
                usage_observed=result.usage_observed,
                upstream_protocol=getattr(ch, "protocol", "anthropic"),
                **_request_stage_kwargs(result),
            )
            return _json_error_for_ingress(
                ingress_protocol,
                status,
                protocol_errors.legacy_anthropic_error_type_for_http_status(status),
                msg,
                code=(
                    getattr(result, "error_code", None)
                    or (
                        protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                        if _is_context_length_exceeded_error(msg)
                        else None
                    )
                ),
            )

        # 未发首包失败：判断是否 OAuth 401/403 可刷一次
        if (
            _recovery_retry_allowed("oauthRefresh", cfg)
            and ch.type == "oauth"
            and result.http_status in (401, 403)
            and not result.openai_oauth_html_403
            and quota_exhaustion is None
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
                    prov = getattr(ch, "provider", "") or oauth_manager.provider_of(ak)
                    notifier.notify_event(
                        "oauth_refresh_failed",
                        "⚠ <b>OAuth Token 刷新失败</b>（请求路径触发）\n"
                        f"账号: <code>{ek(email)}</code> · {notifier.provider_tag(prov)}\n"
                        f"原因: <code>{ek(str(exc))}</code>\n"
                        "账号已被自动禁用 (auth_error)。请通过 TG Bot 重新登录或粘贴新 JSON。"
                    )
                except Exception:
                    pass
                # fallthrough 到普通失败处理

        # 自动透传的 1M entitlement 不足不是渠道故障：同渠道去掉 context-1m
        # 重试一次；渠道明确强制 1M 时不能反向撤销该兼容策略。
        context_retry_key = (ch.key, resolved_model)
        if (
            _recovery_retry_allowed("claudeContext1mFallback", cfg)
            and not _channel_forces_context_1m(ch, resolved_model)
            and _is_context_1m_credit_error(result, resolved_model, body)
            and context_retry_key not in retried_without_context_1m
        ):
            retried_without_context_1m.add(context_retry_key)
            body = _retry_body_without_context_1m(body)
            print(f"[failover] context-1m rejected for {ch.key}/{resolved_model}; retrying same channel without context-1m")
            retry_count += 1
            continue

        # Zhipu's explicit weekly/monthly quota signal is not a short rate limit.
        # Park only this channel/model until the validated upstream reset time,
        # then continue the normal candidate failover without disabling the channel.
        quota_reset_ms = quota_errors.zhipu_1310_reset_ms(
            ch,
            http_status=result.http_status,
            error_detail=result.error_detail,
        )
        if quota_reset_ms is not None:
            plan = finalize_policy.error_plan(result.outcome, failure_policy="runtime")
            if plan.record_cooldown_error:
                cooldown.record_error(
                    channel_state.effect_key(ch),
                    resolved_model,
                    result.error_detail,
                    cooldown_until=quota_reset_ms,
                )
            if plan.record_failure:
                scorer.record_failure(
                    channel_state.effect_key(ch),
                    resolved_model,
                    connect_ms=_scorer_connect_ms(result),
                )
            try:
                _notify_zhipu_quota_cooldown(ch, resolved_model, quota_reset_ms)
            except Exception as exc:
                print(f"[failover] quota cooldown notification failed for {ch.key}: {exc}")
            retry_count += 1
            idx += 1
            continue

        # OpenAI server_is_overloaded/server_error, Claude overloaded_error/529,
        # and direct xAI 503 are explicit transient signals.  The slot is already
        # released, so backoff does not occupy channel concurrency.  Intermediate
        # retries do not score/cool down; the terminal failure below does so once.
        transient_kind = retryable_transient_error_kind(ch, result)
        if (
            quota_exhaustion is None
            and transient_retries_used < transient_retry_limit
            and _transient_retry_allowed(transient_kind, cfg)
        ):
            delay = await _wait_for_overload_retry(
                transient_retries_used,
                deadline_ts,
                retry_after_seconds=result.retry_after_seconds,
            )
            if delay is not None:
                transient_retries_used += 1
                retry_count += 1
                print(
                    f"[failover] transient {transient_kind} on {ch.key}/{resolved_model}; "
                    f"retrying same channel ({transient_retries_used}/"
                    f"{transient_retry_limit}) after {delay:.2f}s"
                )
                continue

        # 普通失败处理；HTML 403 只推进候选，不归咎账号或渠道健康。
        if not result.openai_oauth_html_403:
            plan = finalize_policy.error_plan(result.outcome, failure_policy="runtime")
            finalize_policy.apply_error_health_effects(
                plan,
                scorer=scorer,
                cooldown=cooldown,
                channel_key=channel_state.effect_key(ch),
                model=resolved_model,
                error_detail=result.error_detail,
                connect_ms=_scorer_connect_ms(result),
                cooldown_until=(result.cooldown_until if result.http_status == 429 else None),
            )
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
        # Queue wait is outside every upstream round and cannot consume a round total budget.
        queue_timeout = queue_wait_s
        if queue_timeout > 0:
            candidate_keys: list[tuple[str, object]] = [
                (channel_state.effect_key(ch), (ch, m)) for ch, m in saturated_all
            ]
            acquired = await concurrency.acquire_from_candidates(candidate_keys, queue_timeout)
            if acquired is not None:
                _ch_key, payload = acquired
                ch, resolved_model = payload  # type: ignore[assignment]
                attempt_order += 1
                last_ch_key, last_ch_type, last_model = ch.key, ch.type, resolved_model
                last_ch_protocol = getattr(ch, "protocol", "anthropic")

                _attempt_proxy2: str | None = _pick_non_direct_proxy_name(ch, resolved_model)
                attempt_started_monotonic2 = time.monotonic()
                attempt_id = log_db.record_retry_attempt(
                    request_id, attempt_order, ch.key, ch.type, resolved_model, time.time(),
                    proxy_name=_attempt_proxy2,
                    upstream_protocol=getattr(ch, "protocol", "anthropic"),
                    client_visible_model=client_visible_model,
                )
                release_done2 = False
                def _release_q(_key=channel_state.effect_key(ch)):
                    nonlocal release_done2
                    if release_done2:
                        return
                    release_done2 = True
                    concurrency.release(_key)
                try:
                    candidate_local_web_loop = local_web_loop_active and getattr(ch, "protocol", "anthropic") != "anthropic"
                    candidate_openai_local_web_loop = openai_local_web_loop_active
                    effective_is_stream = is_stream and not (candidate_local_web_loop or candidate_openai_local_web_loop)
                    attempt_body = _attempt_body_for_channel(
                        body, ch.key, bound_channel_key, portable_body,
                    )
                    if (candidate_local_web_loop or candidate_openai_local_web_loop) and attempt_body.get("stream"):
                        attempt_body = dict(attempt_body)
                        attempt_body["stream"] = False
                    if _should_use_responses_upstream_ws(ch, ingress_protocol=ingress_protocol, cfg=cfg):
                        result = await _try_openai_oauth_responses_ws_channel(
                            ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                            fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                            request_id, retry_count, affinity_hit, client_key=client_key,
                            retry_attempt_id=attempt_id,
                            start_monotonic=start_monotonic,
                            attempt_start_monotonic=attempt_started_monotonic2,
                        )
                    else:
                        result = await _try_channel(
                            ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                            fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                            request_id, retry_count, affinity_hit,
                            ingress_protocol=ingress_protocol,
                            client_key=client_key,
                            retry_attempt_id=attempt_id,
                            start_monotonic=start_monotonic,
                            attempt_start_monotonic=attempt_started_monotonic2,
                            terminal_release=_release_q,
                        )
                except BaseException:
                    _release_q()
                    raise
                result = _request_invalid_result_if_needed(result)
                last_result = result
                if not result.success and not result.stream_started:
                    structured_attempts.append(
                        _structured_attempt_error(result, attempt_order, ch),
                    )
                if _attempt_proxy2 and not result.proxy_name:
                    result.proxy_name = _attempt_proxy2
                log_db.update_retry_attempt(
                    attempt_id,
                    final_round_id=result.round_id,
                    connect_ms=result.connect_ms,
                    first_byte_ms=result.first_byte_ms,
                    idle_ms=result.idle_ms,
                    attempt_elapsed_ms=(
                        None if result.stream_started else _elapsed_ms(attempt_started_monotonic2)
                    ),
                    request_upload_ms=result.request_upload_ms,
                    response_headers_wait_ms=result.response_headers_wait_ms,
                    response_body_first_byte_wait_ms=result.response_body_first_byte_wait_ms,
                    total_ms=result.total_ms,
                    ended_at=(None if result.stream_started else time.time()),
                    outcome=("open" if result.stream_started else result.outcome),
                    error_detail=(result.error_detail or "")[:4000] if result.error_detail else None,
                    proxy_name=result.proxy_name,
                    bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
                    bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
                    response_body=getattr(result, "full_response_text", None),
                    usage=getattr(result, "usage", None),
                    usage_observed=getattr(result, "usage_observed", None),
                )
                if result.success and candidate_local_web_loop and downstream_stream_requested:
                    result.response = local_web_tools.maybe_wrap_anthropic_json_response_as_sse(result.response)
                if result.success and candidate_openai_local_web_loop and downstream_stream_requested:
                    result.response = local_web_tools.maybe_wrap_responses_json_response_as_sse(result.response)
                if result.success or result.stream_started:
                    if result.success and not result.stream_started and fp_query:
                        affinity.upsert(
                            fp_query, channel_state.effect_key(ch), resolved_model,
                            prompt_cache_key=_openai_prompt_cache_key_from_body(ingress_protocol, body),
                        )
                    _attach_release_to_response(result.response, _release_q)
                    return result.response
                _release_q()
                if result.outcome == "request_invalid":
                    status = int(result.http_status or 400)
                    msg = result.error_detail or "invalid request"
                    request_elapsed_ms = _elapsed_ms(start_monotonic)
                    await asyncio.to_thread(
                        log_db.finish_error, request_id, msg[:4000], retry_count,
                        final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                        connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms,
                        idle_ms=result.idle_ms, total_ms=result.total_ms,
                        final_round_id=result.round_id, request_elapsed_ms=request_elapsed_ms,
                        http_status=status, affinity_hit=affinity_hit,
                        response_body=result.full_response_text,
                        usage=result.usage,
                        usage_observed=result.usage_observed,
                        upstream_protocol=getattr(ch, "protocol", "anthropic"),
                        **_request_stage_kwargs(result),
                    )
                    return _json_error_for_ingress(
                        ingress_protocol,
                        status,
                        protocol_errors.legacy_anthropic_error_type_for_http_status(status),
                        msg,
                        code=(
                            getattr(result, "error_code", None)
                            or (
                                protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                                if _is_context_length_exceeded_error(msg)
                                else None
                            )
                        ),
                    )
                # 排队拿到的这次也失败了 → 落入"全失败"分支
                if not result.openai_oauth_html_403:
                    plan = finalize_policy.error_plan(result.outcome, failure_policy="runtime")
                    finalize_policy.apply_error_health_effects(
                        plan,
                        scorer=scorer,
                        cooldown=cooldown,
                        channel_key=channel_state.effect_key(ch),
                        model=resolved_model,
                        error_detail=result.error_detail,
                        connect_ms=_scorer_connect_ms(result),
                        cooldown_until=(result.cooldown_until if result.http_status == 429 else None),
                    )
                retry_count += 1
            else:
                # 队列超时 → 直接返回 429 rate_limit_error，不混入上游失败
                request_elapsed_ms = _elapsed_ms(start_monotonic)
                queue_err_msg = (
                    f"All candidate channels saturated; queue wait {queue_wait_s:.0f}s timed out."
                )
                await asyncio.to_thread(
                    log_db.finish_error, request_id, queue_err_msg, retry_count,
                    final_channel_key=None, final_channel_type=None, final_model=None,
                    connect_ms=None, first_token_ms=None, total_ms=None,
                    request_elapsed_ms=request_elapsed_ms,
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
    status = failover_final_http_status(last_result)
    classifications = {
        str(item.get("classification") or "") for item in structured_attempts
    }
    if structured_attempts and all(
        bool(item.get("openai_oauth_html_403")) for item in structured_attempts
    ):
        status = 403
        err_type = "api_error"
    elif structured_attempts and classifications <= {"authentication_error", "permission_error"}:
        statuses = {item.get("status") for item in structured_attempts}
        status = 401 if statuses == {401} else 403
    elif structured_attempts and classifications == {"balance_exhausted"}:
        status = 402
    elif structured_attempts and classifications == {"rate_limit_error"}:
        status = 429
    if last_result and last_result.outcome == "candidate_guard":
        status = int(last_result.http_status or 400)
        err_type = protocol_errors.legacy_anthropic_error_type_for_http_status(status)
    failure_details = _structured_failure_details(structured_attempts)
    msg = str(failure_details["summary"])

    request_elapsed_ms = _elapsed_ms(start_monotonic)
    await asyncio.to_thread(
        log_db.finish_error, request_id, err_detail[:4000], retry_count,
        final_channel_key=last_ch_key,
        final_channel_type=last_ch_type,
        final_model=last_model,
        connect_ms=(last_result.connect_ms if last_result else None),
        first_token_ms=(last_result.first_byte_ms if last_result else None),
        idle_ms=(last_result.idle_ms if last_result else None),
        total_ms=(last_result.total_ms if last_result else None),
        final_round_id=(last_result.round_id if last_result else None),
        request_elapsed_ms=request_elapsed_ms,
        http_status=status, affinity_hit=affinity_hit,
        response_body=(last_result.full_response_text if last_result else None),
        usage=(last_result.usage if last_result else None),
        usage_observed=(last_result.usage_observed if last_result else None),
        upstream_protocol=last_ch_protocol,
        proxy_name=(last_result.proxy_name if last_result else None),
        proxy_bytes_up=(last_result.proxy_bytes_up if last_result else None),
        proxy_bytes_down=(last_result.proxy_bytes_down if last_result else None),
        **_request_stage_kwargs(last_result),
    )
    return _json_error_for_ingress(
        ingress_protocol,
        status,
        err_type,
        msg,
        code=failure_details["root_cause"].get("code"),
        details=failure_details,
    )


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
            # Closing an async-generator wrapper while it is suspended at
            # ``yield`` does not automatically aclose the wrapped iterator.
            # Propagate ownership explicitly so its request-log finalizer and
            # upstream response-context cleanup always run.
            try:
                aclose = getattr(original, "aclose", None)
                if callable(aclose):
                    try:
                        await await_ws_owned(aclose())
                    except Exception:
                        pass
            finally:
                try:
                    release_fn()
                except Exception:
                    pass

    response.body_iterator = _wrapped()


# ─── OpenAI OAuth Responses: HTTP ingress → WS upstream ───────────────

_WsProxyBytes = WsProxyBytes


def _http_url_to_ws(url: str) -> str:
    return http_url_to_ws(url)


def _socks5h_url(url: str) -> str:
    return socks5h_url(url)


def _ws_proxy_snapshot(proxy_bytes: _WsProxyBytes) -> tuple[int, int]:
    return int(proxy_bytes.up or 0), int(proxy_bytes.down or 0)


def _persist_ws_route_round(
    proxy_attempt_id,
    timing: WsAttemptTiming,
    proxy_bytes: _WsProxyBytes,
    *,
    outcome: str,
    error_detail: str | None = None,
    terminal: bool,
):
    """Persist one WS route from its authoritative snapshot."""

    snapshot = (
        timing.finish(outcome, error_detail)
        if terminal
        else timing.snapshot(terminal=False)
    )
    if proxy_attempt_id is not None:
        try:
            log_db.update_proxy_attempt(
                proxy_attempt_id,
                started_at=snapshot.started_at,
                connect_ms=snapshot.connection_ms,
                first_byte_ms=snapshot.first_byte_ms,
                idle_ms=snapshot.idle_ms,
                ws_handshake_ms=snapshot.ws_handshake_ms,
                total_ms=snapshot.total_ms,
                ended_at=snapshot.ended_at if terminal else None,
                outcome=outcome,
                error_detail=(error_detail or "")[:4000] if error_detail else None,
                bytes_up=proxy_bytes.up,
                bytes_down=proxy_bytes.down,
            )
        except Exception:
            pass
    return snapshot


def _finalize_ws_attempt_result(
    result: AttemptResult,
    *,
    proxy_attempt_id,
    timing: WsAttemptTiming,
    proxy_bytes: _WsProxyBytes,
) -> AttemptResult:
    if not timing.terminal:
        _persist_ws_route_round(
            proxy_attempt_id,
            timing,
            proxy_bytes,
            outcome=result.outcome,
            error_detail=result.error_detail,
            terminal=True,
        )
    timing.apply_to(result)
    result.proxy_bytes_up = proxy_bytes.up
    result.proxy_bytes_down = proxy_bytes.down
    return result


def _drop_headers_case_insensitive(headers: dict[str, str], names: set[str]) -> dict[str, str]:
    return drop_headers_case_insensitive(headers, names)


def _get_header_case_insensitive(headers: dict[str, str] | None, key: str) -> str:
    return get_header_case_insensitive(headers, key)


def _merge_oauth_responses_ws_headers(headers: dict[str, str]) -> dict[str, str]:
    return merge_oauth_responses_ws_headers(headers)


def _ensure_oauth_responses_ws_session_headers(
    headers: dict[str, str],
    body: dict,
) -> None:
    ensure_oauth_responses_ws_session_headers(headers, body)


def _build_oauth_responses_ws_frame(
    body: dict,
    resolved_model: str,
    *,
    channel: OpenAIOAuthChannel | None = None,
) -> dict:
    return build_oauth_responses_ws_frame(body, resolved_model, channel=channel)


def _ws_route_kwargs(ch: Channel, resolved_model: str) -> dict:
    return ws_route_kwargs(ch, resolved_model)


def _resolve_ws_route_chain_for_channel(ch: Channel, resolved_model: str) -> list[tuple[str, Any | None]]:
    return resolve_ws_route_chain(ch, resolved_model)


def _legacy_socks5_connector() -> SOCKS5Connector | None:
    return legacy_socks5_connector()


_ManagedWsConnection = ManagedWsConnection


async def _open_socket_via_ss2022(
    url: str,
    connector: SS2022Connector,
    proxy_bytes: _WsProxyBytes,
    *,
    timeout: float,
):
    return await open_socket_via_ss2022(url, connector, proxy_bytes, timeout=timeout)


async def _connect_oauth_responses_ws(
    url: str,
    *,
    headers: dict[str, str],
    connector,
    proxy_bytes: _WsProxyBytes,
    open_timeout: float,
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
):
    return await connect_upstream_ws(
        url,
        headers=headers,
        connector=connector,
        proxy_bytes=proxy_bytes,
        open_timeout=open_timeout,
        timing=timing,
        round_timeouts=round_timeouts,
        open_socket_func=_open_socket_via_ss2022,
        connect_func=websockets.connect,
    )


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
    return ws_frame_size(data)


def _ws_event_type(data: str | bytes) -> str:
    return ws_event_type(data)


def _ws_error_detail(data: str | bytes) -> tuple[Optional[int], str]:
    return responses_ws_error_detail(data)


def _is_ws_visible_event_type(event_type: str) -> bool:
    return is_responses_ws_visible_event_type(event_type)


class _WsResponsesTracker:
    def __init__(self, channel: OpenAIOAuthChannel | None = None) -> None:
        self.channel = channel
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0}
        self.usage_observed = False
        self.actual_service_tier: Optional[str] = None
        self.actual_cost_ticks: Optional[int] = None
        self._billing_event_type: Optional[str] = None
        self.response_completed = False
        self.response_failed = False
        self.stream_error_message: Optional[str] = None
        self.stream_error_code: Optional[str] = None
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
        response_obj = evt.get("response") if isinstance(evt.get("response"), dict) else None
        usage_present = "usage" in evt or (
            isinstance(response_obj, dict) and "usage" in response_obj
        )
        normalized = model_pricing.normalize_response_billing(evt)
        if normalized.service_tier is not None:
            self.actual_service_tier = normalized.service_tier
        if normalized.actual_cost_ticks is not None:
            self.actual_cost_ticks = normalized.actual_cost_ticks
        if usage_present or normalized.service_tier is not None:
            self._billing_event_type = typ or "response.in_progress"
        if usage_present:
            self.usage_observed = normalized.usage_observed
            self.usage = {
                "input_tokens": normalized.input_tokens,
                "output_tokens": normalized.output_tokens,
                "cache_creation": normalized.cache_creation_tokens,
                "cache_read": normalized.cache_read_tokens,
            } if self.usage_observed else {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation": 0,
                "cache_read": 0,
            }
        if typ == "error" or isinstance(evt.get("error"), dict):
            self.response_failed = True
            _status, self.stream_error_message = _ws_error_detail(text)
            self.stream_error_code = None
            return
        if typ == "response.failed":
            self.response_failed = True
            _status, self.stream_error_message = _ws_error_detail(text)
            self.stream_error_code = None
        elif typ == "response.incomplete":
            if protocol_errors.is_responses_max_output_incomplete(evt):
                self.response_failed = True
                self.stream_error_code = protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                self.stream_error_message = protocol_errors.responses_max_output_context_error_message(
                    protocol_errors.responses_incomplete_reason(evt)
                )
            else:
                self.response_completed = True
        elif typ == "response.completed":
            self.response_completed = True

        if typ in ("response.completed", "response.failed", "response.incomplete"):
            resp = response_obj
            if isinstance(resp, dict):
                self._response_obj = resp
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
        if self.usage_observed:
            base.setdefault("usage", {
                "input_tokens": int(self.usage.get("input_tokens") or 0) + int(self.usage.get("cache_read") or 0),
                "output_tokens": int(self.usage.get("output_tokens") or 0),
                "input_tokens_details": {"cached_tokens": int(self.usage.get("cache_read") or 0)},
            })
        return base

    def get_full_response(self) -> str:
        return model_pricing.preserve_billing_evidence_tail(
            "\n".join(self._frames),
            usage=self.usage,
            usage_observed=self.usage_observed,
            service_tier=self.actual_service_tier,
            actual_cost_ticks=self.actual_cost_ticks,
            event_type=self._billing_event_type,
        )


def _hydrate_oauth_ws_attempt_result(
    result: AttemptResult,
    tracker: _WsResponsesTracker,
    *,
    identity_state: ConfuseState | None = None,
    proxy_name: str | None = None,
    proxy_bytes: _WsProxyBytes | None = None,
    translator_ctx: dict | None = None,
) -> AttemptResult:
    """Copy observed WS facts before immutable attempt settlement."""

    result.usage = dict(tracker.usage)
    result.usage_observed = tracker.usage_observed
    response_text = tracker.get_full_response()
    if identity_state is not None:
        response_text = identity_log_text(response_text, identity_state)
    result.full_response_text = response_text or None
    result.proxy_name = proxy_name
    if proxy_bytes is not None:
        result.proxy_bytes_up = proxy_bytes.up
        result.proxy_bytes_down = proxy_bytes.down
    result.translator_ctx = translator_ctx
    return result


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _ws_http_status_from_outcome(result: AttemptResult) -> int:
    return upstream_ws_http_status_from_attempt(result)


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
    req = await ch.build_upstream_request(
        body, resolved_model, ingress_protocol="responses",
        defer_device_fingerprint=True,
    )
    ws_url, headers, frame, identity_state = prepare_oauth_responses_ws_request_parts(
        req,
        body,
        resolved_model,
        channel=ch,
    )
    return ws_url, headers, frame, req.translator_ctx, identity_state


async def _try_openai_oauth_responses_ws_channel(
    ch: Channel, resolved_model: str, body: dict,
    is_stream: bool, deadline_ts: float, start_time: float,
    fp_query: Optional[str], messages: list,
    api_key_name: Optional[str], client_ip: str,
    request_id: str, retry_count_so_far: int, affinity_hit: int,
    *, client_key: Optional[str] = None,
    retry_attempt_id: int | None = None,
    start_monotonic: float | None = None,
    attempt_start_monotonic: float | None = None,
) -> AttemptResult:
    if start_monotonic is None:
        start_monotonic = time.monotonic()
    if attempt_start_monotonic is None:
        attempt_start_monotonic = time.monotonic()
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
            outcome = "candidate_guard" if getattr(exc, "scope", "request") == "candidate" else "guard_error"
            return AttemptResult(
                outcome=outcome,
                error_detail=str(getattr(exc, "message", exc))[:2000],
                http_status=int(getattr(exc, "status", 400)),
            )
        return AttemptResult(outcome="transform_error", error_detail=f"transform error: {exc}")

    round_timeouts = RoundTimeouts.from_config(timeouts)
    last_error: Optional[AttemptResult] = None
    proxy_attempt_order = 0
    for route_name, connector in _resolve_ws_route_chain_for_channel(ch, resolved_model):
        last_error = None
        proxy_name = None if connector is None else route_name
        route_log_name = route_name if connector is not None else "direct"
        route_type = str(getattr(connector, "type", "direct") or "direct")
        proxy_bytes = _WsProxyBytes()
        route_selected_at = time.time()
        round_id = str(uuid.uuid4())
        proxy_attempt_order += 1
        proxy_attempt_id = None
        try:
            proxy_attempt_id = log_db.record_proxy_attempt(
                request_id,
                retry_attempt_id,
                proxy_attempt_order,
                route_log_name,
                time.time(),
                round_id=round_id,
                transport="ws",
                request_mode="ws",
            )
        except Exception:
            proxy_attempt_id = None

        upstream_ws = None
        timing = WsAttemptTiming(route_type=route_type, round_id=round_id)
        route_state = {"dispatched": False}
        tracker = _WsResponsesTracker(ch)
        try:
            if connector is not None:
                connector.stats.total_attempts += 1
                connector.stats.last_attempt_ts = route_selected_at
            upstream_ws = await _connect_oauth_responses_ws(
                ws_url,
                headers=ws_headers,
                connector=connector,
                proxy_bytes=proxy_bytes,
                open_timeout=round_timeouts.connection + 0.5,
                timing=timing,
                round_timeouts=round_timeouts,
            )
            if not timing.connection_complete:
                timing.mark_handshake_complete()
            open_snapshot = _persist_ws_route_round(
                proxy_attempt_id,
                timing,
                proxy_bytes,
                outcome="open",
                terminal=False,
            )
            connect_ms = open_snapshot.connection_ms
            if connector is not None:
                connector.stats.total_successes += 1
                connector.stats.last_success_ts = time.time()
                connector.stats.last_latency_ms = int(connect_ms or 0)
            _maybe_record_codex_ws_snapshot(ch, getattr(upstream_ws, "response", None))
            result = await _consume_oauth_responses_ws(
                upstream_ws,
                tracker=tracker,
                first_frame=first_frame,
                ch=ch,
                resolved_model=resolved_model,
                is_stream=is_stream,
                deadline_ts=deadline_ts,
                start_time=start_time,
                start_monotonic=start_monotonic,
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
                timing=timing,
                round_timeouts=round_timeouts,
                proxy_attempt_id=proxy_attempt_id,
                retry_attempt_id=retry_attempt_id,
                attempt_start_monotonic=attempt_start_monotonic,
                on_dispatch=lambda: route_state.__setitem__("dispatched", True),
            )
            result = _attach_retry_after_from_response(
                result, getattr(upstream_ws, "response", None), ch,
            )
            if result.stream_started and isinstance(result.response, StreamingResponse):
                # The post-commit generator owns WS + round terminalization.
                upstream_ws = None
                timing.apply_to(result, terminal=False)
                return result
            result = _finalize_ws_attempt_result(
                result,
                proxy_attempt_id=proxy_attempt_id,
                timing=timing,
                proxy_bytes=proxy_bytes,
            )
            return result
        except asyncio.CancelledError:
            if upstream_ws is not None:
                try:
                    await await_ws_owned(upstream_ws.close())
                except BaseException:
                    pass
                upstream_ws = None
            cancelled = AttemptResult(
                outcome="cancelled",
                error_detail="cancelled",
                proxy_name=proxy_name,
            )
            _hydrate_oauth_ws_attempt_result(
                cancelled,
                tracker,
                identity_state=identity_state,
                proxy_name=proxy_name,
                proxy_bytes=proxy_bytes,
                translator_ctx=translator_ctx,
            )
            cancelled = _finalize_ws_attempt_result(
                cancelled,
                proxy_attempt_id=proxy_attempt_id,
                timing=timing,
                proxy_bytes=proxy_bytes,
            )
            async def finish_cancelled_attempt() -> None:
                if retry_attempt_id is not None:
                    await asyncio.to_thread(
                        log_db.update_retry_attempt,
                        retry_attempt_id,
                        final_round_id=cancelled.round_id,
                        connect_ms=cancelled.connect_ms,
                        first_byte_ms=cancelled.first_byte_ms,
                        idle_ms=cancelled.idle_ms,
                        total_ms=cancelled.total_ms,
                        attempt_elapsed_ms=_elapsed_ms(attempt_start_monotonic),
                        ended_at=time.time(),
                        outcome="cancelled",
                        error_detail="cancelled",
                        proxy_name=proxy_name,
                        bytes_up=proxy_bytes.up,
                        bytes_down=proxy_bytes.down,
                        response_body=cancelled.full_response_text,
                        usage=cancelled.usage,
                        usage_observed=cancelled.usage_observed,
                        settle=False,
                    )
                await asyncio.to_thread(
                    log_db.finish_error,
                    request_id,
                    "client disconnected",
                    retry_count_so_far,
                    final_channel_key=ch.key,
                    final_channel_type=ch.type,
                    final_model=resolved_model,
                    connect_ms=cancelled.connect_ms,
                    first_token_ms=cancelled.first_byte_ms,
                    idle_ms=cancelled.idle_ms,
                    total_ms=cancelled.total_ms,
                    final_round_id=cancelled.round_id,
                    request_elapsed_ms=_elapsed_ms(start_monotonic),
                    http_status=499,
                    response_body=cancelled.full_response_text,
                    usage=cancelled.usage,
                    usage_observed=cancelled.usage_observed,
                    affinity_hit=affinity_hit,
                    upstream_protocol="openai-responses",
                    upstream_transport="ws",
                    proxy_name=proxy_name,
                    proxy_bytes_up=proxy_bytes.up,
                    proxy_bytes_down=proxy_bytes.down,
                    status="cancelled",
                )

            await await_ws_owned(finish_cancelled_attempt())
            raise
        except BusinessTimeoutError as exc:
            last_error = AttemptResult(
                outcome=exc.outcome,
                error_detail=exc.outcome,
                proxy_name=proxy_name,
            )
        except asyncio.TimeoutError as exc:
            last_error = AttemptResult(
                outcome="transport_timeout",
                error_detail=f"websocket transport timeout: {exc}"[:2000],
                proxy_name=proxy_name,
            )
        except InvalidStatus as exc:
            invalid_response = getattr(exc, "response", None)
            status, detail = _invalid_ws_status_detail(exc)
            last_error = AttemptResult(
                outcome="http_auth_error" if status in (401, 403) else "http_error",
                error_detail=detail,
                http_status=status,
                openai_oauth_html_403=(
                    status == 403
                    and is_html_error_document(getattr(invalid_response, "body", None))
                ),
                proxy_name=proxy_name,
            )
            last_error = _attach_retry_after_from_response(last_error, invalid_response, ch)
        except Exception as exc:
            connected = timing.connection_complete
            last_error = AttemptResult(
                outcome="transport_error" if connected else "connect_error",
                error_detail=f"{'websocket relay' if connected else 'connect'} error: {exc}"[:2000],
                proxy_name=proxy_name,
            )
        finally:
            if last_error is not None and not timing.terminal:
                _finalize_ws_attempt_result(
                    last_error,
                    proxy_attempt_id=proxy_attempt_id,
                    timing=timing,
                    proxy_bytes=proxy_bytes,
                )
            if upstream_ws is not None:
                try:
                    await upstream_ws.close()
                except Exception:
                    pass
        if connector is not None and last_error is not None:
            connector.stats.total_failures += 1
            connector.stats.last_error = (last_error.error_detail or last_error.outcome)[:200]
        # A typed HTML403 is an account-protection signal: advance the outer
        # candidate exactly once rather than trying another proxy route.
        if last_error is not None and last_error.openai_oauth_html_403:
            return last_error
        # Do not replay one logical retry row over another proxy route after a
        # create frame may have reached the upstream.
        if last_error is not None and route_state["dispatched"]:
            return last_error
        continue

    return last_error or AttemptResult(outcome="proxy_connect_error", error_detail="proxy route has no usable target")


async def _consume_oauth_responses_ws(
    upstream_ws,
    *,
    tracker: _WsResponsesTracker,
    first_frame: str,
    ch: OpenAIOAuthChannel,
    resolved_model: str,
    is_stream: bool,
    deadline_ts: float,
    start_time: float,
    start_monotonic: float,
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
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
    proxy_attempt_id,
    retry_attempt_id,
    attempt_start_monotonic: float,
    on_dispatch,
) -> AttemptResult:
    try:
        log_db.update_pending_fast_mode_from_upstream(request_id, first_frame)
        if retry_attempt_id is not None:
            try:
                log_db.mark_retry_attempt_dispatch(retry_attempt_id, first_frame)
            except Exception:
                pass
        on_dispatch()
        proxy_bytes.count(up=_frame_size(first_frame))
        await wait_ws_round_io(
            upstream_ws.send(first_frame),
            timing=timing,
            round_timeouts=round_timeouts,
        )
    except BusinessTimeoutError as exc:
        return AttemptResult(
            outcome=exc.outcome,
            error_detail=exc.outcome,
            connect_ms=connect_ms,
            proxy_name=proxy_name,
            proxy_bytes_up=proxy_bytes.up,
            proxy_bytes_down=proxy_bytes.down,
            translator_ctx=translator_ctx,
        )
    except asyncio.TimeoutError as exc:
        return AttemptResult(
            outcome="transport_timeout",
            error_detail=f"send first websocket frame transport timeout: {exc}",
            connect_ms=connect_ms,
            proxy_name=proxy_name,
            proxy_bytes_up=proxy_bytes.up,
            proxy_bytes_down=proxy_bytes.down,
            translator_ctx=translator_ctx,
        )
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
            start_monotonic=start_monotonic,
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
            timing=timing,
            round_timeouts=round_timeouts,
            proxy_attempt_id=proxy_attempt_id,
            retry_attempt_id=retry_attempt_id,
            attempt_start_monotonic=attempt_start_monotonic,
        )
    return await _consume_oauth_responses_ws_non_stream(
        upstream_ws,
        tracker=tracker,
        ch=ch,
        resolved_model=resolved_model,
        deadline_ts=deadline_ts,
        start_time=start_time,
        start_monotonic=start_monotonic,
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
        timing=timing,
        round_timeouts=round_timeouts,
        proxy_attempt_id=proxy_attempt_id,
        retry_attempt_id=retry_attempt_id,
        attempt_start_monotonic=attempt_start_monotonic,
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
    start_monotonic: float,
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
) -> tuple[list[str | bytes], Optional[AttemptResult], Optional[int]]:
    step = await read_until_first_responses_ws_visible_event(
        upstream_ws,
        tracker,
        channel_key=ch.key,
        deadline_ts=deadline_ts,
        first_wait=first_wait,
        idle_timeout=idle_timeout,
        proxy_bytes=proxy_bytes,
        start_time=start_time,
        start_monotonic=start_monotonic,
        parse_wrapped_errors=False,
        timeout_detail_mode="packet_or_visible",
        timeout_label_seconds=first_wait,
        use_tracker_error_detail=False,
        timing=timing,
        round_timeouts=round_timeouts,
    )
    if step.outcome is None or step.ok:
        return step.pending, None, step.first_packet_ms
    return step.pending, AttemptResult(
        outcome=step.outcome,
        error_detail=step.error_detail,
        error_code=step.error_code,
        http_status=step.http_status,
        stream_started=step.stream_started,
    ), step.first_packet_ms


def _identity_expose_frame(data: str | bytes, state: ConfuseState) -> str | bytes:
    return identity_expose_frame(data, state)


def _identity_log_text(text: str, state: ConfuseState) -> str:
    return identity_log_text(text, state)


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
    start_monotonic: float,
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
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
    proxy_attempt_id,
    retry_attempt_id,
    attempt_start_monotonic: float,
) -> AttemptResult:
    first_wait = round_timeouts.first_byte
    first_chunks, pre_error, first_packet_ms = await _recv_oauth_ws_until_visible(
        upstream_ws, tracker, ch=ch, deadline_ts=deadline_ts,
        first_wait=first_wait, idle_timeout=idle_timeout, proxy_bytes=proxy_bytes,
        start_time=start_time, start_monotonic=start_monotonic,
        timing=timing, round_timeouts=round_timeouts,
    )
    first_byte_ms = timing.snapshot().first_byte_ms

    def hydrate(result: AttemptResult) -> AttemptResult:
        result.connect_ms = connect_ms
        result.first_byte_ms = first_byte_ms
        return _hydrate_oauth_ws_attempt_result(
            result,
            tracker,
            identity_state=identity_state,
            proxy_name=proxy_name,
            proxy_bytes=proxy_bytes,
            translator_ctx=translator_ctx,
        )

    async def finalize_terminal_error(result: AttemptResult) -> AttemptResult:
        # No downstream bytes have been emitted for this HTTP non-stream call.
        # ``stream_started`` from the shared pre-visible helper means the
        # upstream terminal frame was protocol-visible, not that an HTTP body
        # was committed. Return a normal failed attempt so outer failover owns
        # channel selection, root finalization, and the month-bound handle.
        result.stream_started = False
        return hydrate(result)

    if pre_error is not None and not pre_error.stream_started:
        return hydrate(pre_error)

    # ``response.failed`` may set the helper's stream flag, but this non-stream
    # caller has not committed anything downstream and can still fail over.
    if pre_error is not None:
        return await finalize_terminal_error(pre_error)

    while not tracker.response_completed and not tracker.response_failed:
        step = await read_next_responses_ws_step(
            upstream_ws,
            tracker,
            channel_key=ch.key,
            deadline_ts=deadline_ts,
            idle_timeout=idle_timeout,
            proxy_bytes=proxy_bytes,
            closed_error_detail="upstream websocket closed",
            check_blacklist=False,
            timing=timing,
            round_timeouts=round_timeouts,
        )
        if step.outcome in (
            "connection_timeout", "first_byte_timeout", "idle_timeout",
            "total_timeout", "transport_timeout",
        ):
            return await finalize_terminal_error(AttemptResult(
                outcome=step.outcome,
                error_detail=step.error_detail,
                http_status=504,
            ))
        if step.outcome in ("upstream_closed", "connection_lifecycle"):
            return await finalize_terminal_error(AttemptResult(
                outcome=step.outcome,
                error_detail=step.error_detail or "upstream websocket closed",
                http_status=(502 if step.outcome == "upstream_closed" else None),
            ))
        if step.outcome == "stream_upstream_error":
            return await finalize_terminal_error(AttemptResult(
                outcome="stream_upstream_error",
                error_detail=step.error_detail or "upstream stream error",
                http_status=503,
            ))
        if step.outcome == "request_invalid":
            return await finalize_terminal_error(AttemptResult(
                outcome="request_invalid",
                error_detail=(
                    step.error_detail
                    or protocol_errors.responses_max_output_context_error_message()
                ),
                http_status=400,
            ))
        if step.outcome == "success":
            break
        if step.skip_downstream:
            continue

    obj = tracker.to_full_json(fallback_model=resolved_model)
    if identity_state.enabled:
        try:
            exposed = identity_expose_frame(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                identity_state,
            )
            obj = json.loads(exposed if isinstance(exposed, str) else exposed.decode("utf-8", errors="replace"))
        except Exception:
            pass
    usage = upstream.extract_usage_responses_json(obj)
    timing_snapshot = _persist_ws_route_round(
        proxy_attempt_id, timing, proxy_bytes,
        outcome="success", terminal=True,
    )
    request_elapsed_ms = _elapsed_ms(start_monotonic)
    total_ms = timing_snapshot.total_ms
    finalize_policy.apply_success_health_effects(
        finalize_policy.success_plan(),
        scorer=scorer,
        cooldown=cooldown,
        channel_key=channel_state.effect_key(ch),
        model=resolved_model,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        total_ms=total_ms,
    )
    out_obj = _apply_non_stream_response_translator(obj, translator_ctx or {})
    compaction_owner.persist_observed_safe(
        ch, body, obj, path="oauth_upstream_ws_non_stream",
    )
    _write_affinity_non_stream(
        "responses", api_key_name, client_ip, messages,
        {"role": "assistant", "content": obj.get("output") or []},
        body, out_obj, ch.key, resolved_model, client_key=client_key,
        fp_query=fp_query,
    )
    await await_ws_owned(asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=timing_snapshot.connection_ms,
        first_token_ms=timing_snapshot.first_byte_ms,
        idle_ms=timing_snapshot.idle_ms,
        total_ms=timing_snapshot.total_ms,
        final_round_id=timing_snapshot.round_id,
        request_elapsed_ms=request_elapsed_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=_identity_log_text(tracker.get_full_response(), identity_state), http_status=200,
        usage_observed=tracker.usage_observed,
        upstream_protocol="openai-responses", upstream_transport="ws",
        proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
    ))
    return AttemptResult(
        outcome="success", success=True,
        response=JSONResponse(content=out_obj, status_code=200),
        http_status=200,
        round_id=timing_snapshot.round_id,
        connect_ms=timing_snapshot.connection_ms,
        first_byte_ms=timing_snapshot.first_byte_ms,
        idle_ms=timing_snapshot.idle_ms,
        total_ms=timing_snapshot.total_ms,
        usage=usage,
        usage_observed=tracker.usage_observed,
        full_response_text=_identity_log_text(tracker.get_full_response(), identity_state),
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
    start_monotonic: float,
    connect_ms: int,
    first_byte_ms: Optional[int],
    tracker: _WsResponsesTracker,
    proxy_name: Optional[str],
    proxy_bytes: _WsProxyBytes,
    identity_state: ConfuseState,
    timing: WsAttemptTiming,
) -> None:
    result = _request_invalid_result_if_needed(result)
    plan = finalize_policy.error_plan(result.outcome, failure_policy="cooldown_only")
    finalize_policy.apply_error_health_effects(
        plan,
        scorer=scorer,
        cooldown=cooldown,
        channel_key=channel_state.effect_key(ch),
        model=resolved_model,
        error_detail=result.error_detail,
        connect_ms=connect_ms,
    )
    timing_snapshot = timing.snapshot(terminal=True)
    request_elapsed_ms = _elapsed_ms(start_monotonic)
    await asyncio.shield(asyncio.to_thread(
        log_db.finish_error,
        request_id, (result.error_detail or result.outcome or "upstream websocket error")[:4000],
        retry_count_so_far,
        final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
        connect_ms=timing_snapshot.connection_ms,
        first_token_ms=timing_snapshot.first_byte_ms,
        idle_ms=timing_snapshot.idle_ms,
        total_ms=timing_snapshot.total_ms,
        final_round_id=timing_snapshot.round_id,
        request_elapsed_ms=request_elapsed_ms,
        http_status=_ws_http_status_from_outcome(result), affinity_hit=affinity_hit,
        response_body=_identity_log_text(tracker.get_full_response(), identity_state) or None,
        usage=tracker.usage,
        usage_observed=tracker.usage_observed,
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
    start_monotonic: float,
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
    timing: WsAttemptTiming,
    round_timeouts: RoundTimeouts,
    proxy_attempt_id,
    retry_attempt_id,
    attempt_start_monotonic: float,
) -> AttemptResult:
    first_wait = round_timeouts.first_byte
    first_chunks, pre_error, first_packet_ms = await _recv_oauth_ws_until_visible(
        upstream_ws, tracker, ch=ch, deadline_ts=deadline_ts,
        first_wait=first_wait, idle_timeout=idle_timeout, proxy_bytes=proxy_bytes,
        start_time=start_time, start_monotonic=start_monotonic,
        timing=timing, round_timeouts=round_timeouts,
    )
    first_byte_ms = timing.snapshot().first_byte_ms

    def hydrate(result: AttemptResult) -> AttemptResult:
        result.connect_ms = connect_ms
        result.first_byte_ms = first_byte_ms
        return _hydrate_oauth_ws_attempt_result(
            result,
            tracker,
            identity_state=identity_state,
            proxy_name=proxy_name,
            proxy_bytes=proxy_bytes,
            translator_ctx=translator_ctx,
        )

    if pre_error is not None and not pre_error.stream_started:
        return hydrate(pre_error)

    state = {"finalized": False}

    async def persist_terminal(outcome: str, error_detail: str | None = None):
        snapshot = _persist_ws_route_round(
            proxy_attempt_id,
            timing,
            proxy_bytes,
            outcome=outcome,
            error_detail=error_detail,
            terminal=True,
        )
        if retry_attempt_id is not None:
            try:
                await asyncio.to_thread(
                    log_db.update_retry_attempt,
                    retry_attempt_id,
                    final_round_id=snapshot.round_id,
                    connect_ms=snapshot.connection_ms,
                    first_byte_ms=snapshot.first_byte_ms,
                    idle_ms=snapshot.idle_ms,
                    total_ms=snapshot.total_ms,
                    attempt_elapsed_ms=_elapsed_ms(attempt_start_monotonic),
                    ended_at=time.time(),
                    outcome=outcome,
                    error_detail=(error_detail or "")[:4000] if error_detail else None,
                    proxy_name=proxy_name,
                    bytes_up=proxy_bytes.up,
                    bytes_down=proxy_bytes.down,
                    settle=False,
                )
            except Exception:
                pass
        return snapshot

    async def finalize_success() -> None:
        if state["finalized"]:
            return
        state["finalized"] = True
        timing_snapshot = await persist_terminal("success")
        request_elapsed_ms = _elapsed_ms(start_monotonic)
        total_ms = timing_snapshot.total_ms
        usage = tracker.usage
        finalize_policy.apply_success_health_effects(
            finalize_policy.success_plan(),
            scorer=scorer,
            cooldown=cooldown,
            channel_key=channel_state.effect_key(ch),
            model=resolved_model,
            connect_ms=connect_ms,
            first_byte_ms=first_byte_ms,
            total_ms=total_ms,
        )
        compaction_owner.persist_observed_safe(
            ch, body, tracker.to_full_json(fallback_model=resolved_model),
            path="oauth_upstream_ws_stream_finalize",
        )
        _write_affinity_non_stream(
            "responses", api_key_name, client_ip, messages,
            {"role": "assistant", "content": tracker.get_output_items()},
            body, tracker.to_full_json(fallback_model=resolved_model), ch.key, resolved_model,
            client_key=client_key, fp_query=fp_query,
        )
        # encrypted_content 透明透传：上游产出的 reasoning 只返回给下游，
        # Parrot 不做本地持久化或后续回填。
        await asyncio.shield(asyncio.to_thread(
            log_db.finish_success,
            request_id, ch.key, ch.type, resolved_model,
            input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
            cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
            connect_ms=timing_snapshot.connection_ms,
            first_token_ms=timing_snapshot.first_byte_ms,
            idle_ms=timing_snapshot.idle_ms,
            total_ms=timing_snapshot.total_ms,
            final_round_id=timing_snapshot.round_id,
            request_elapsed_ms=request_elapsed_ms,
            retry_count=retry_count_so_far, affinity_hit=affinity_hit,
            response_body=_identity_log_text(tracker.get_full_response(), identity_state), http_status=200,
            usage_observed=tracker.usage_observed,
            upstream_protocol="openai-responses", upstream_transport="ws",
            proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
        ))

    async def finalize_error(result: AttemptResult) -> None:
        if state["finalized"]:
            return
        state["finalized"] = True
        await persist_terminal(result.outcome, result.error_detail)
        # encrypted_content 只做透明透传；无本地 cache 需要清理。
        await _finalize_oauth_ws_error(
            result, ch, resolved_model, request_id, retry_count_so_far,
            affinity_hit, start_time, start_monotonic, connect_ms, first_byte_ms,
            tracker, proxy_name, proxy_bytes, identity_state, timing,
        )

    async def finalize_disconnect() -> None:
        if state["finalized"]:
            return
        state["finalized"] = True
        timing_snapshot = await persist_terminal(
            "client_disconnected", "client disconnected",
        )
        request_elapsed_ms = _elapsed_ms(start_monotonic)
        await asyncio.to_thread(
            log_db.finish_error,
            request_id, "client disconnected", retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=timing_snapshot.connection_ms,
            first_token_ms=timing_snapshot.first_byte_ms,
            idle_ms=timing_snapshot.idle_ms,
            total_ms=timing_snapshot.total_ms,
            final_round_id=timing_snapshot.round_id,
            request_elapsed_ms=request_elapsed_ms,
            http_status=499, affinity_hit=affinity_hit,
            response_body=_identity_log_text(tracker.get_full_response(), identity_state) or None,
            usage=tracker.usage,
            usage_observed=tracker.usage_observed,
            upstream_protocol="openai-responses", upstream_transport="ws",
            proxy_name=proxy_name, proxy_bytes_up=proxy_bytes.up, proxy_bytes_down=proxy_bytes.down,
        )

    async def stream_generator():
        try:
            for data in first_chunks:
                out = _ws_json_to_responses_sse(_identity_expose_frame(data, identity_state))
                if out is not None:
                    yield out
            if pre_error is not None:
                await await_ws_owned(finalize_error(pre_error))
                return
            while True:
                if tracker.response_completed:
                    await await_ws_owned(finalize_success())
                    return
                if tracker.response_failed:
                    is_context_error = (
                        tracker.stream_error_code == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                    )
                    msg = tracker.stream_error_message or (
                        protocol_errors.responses_max_output_context_error_message()
                        if is_context_error else "upstream stream error"
                    )
                    await await_ws_owned(finalize_error(AttemptResult(
                        outcome="request_invalid" if is_context_error else "stream_upstream_error",
                        error_detail=msg,
                        http_status=400 if is_context_error else 503,
                    )))
                    if is_context_error:
                        yield _sse_error_for_ingress(
                            "responses",
                            errors.ErrType.INVALID_REQUEST,
                            msg,
                            code=protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
                        )
                    return
                step = await read_next_responses_ws_step(
                    upstream_ws,
                    tracker,
                    channel_key=ch.key,
                    deadline_ts=deadline_ts,
                    idle_timeout=idle_timeout,
                    proxy_bytes=proxy_bytes,
                    closed_error_detail="upstream websocket closed",
                    blacklist_before_error=False,
                    timing=timing,
                    round_timeouts=round_timeouts,
                )
                if step.outcome in (
                    "connection_timeout", "first_byte_timeout", "idle_timeout",
                    "total_timeout", "transport_timeout",
                ):
                    err = AttemptResult(
                        outcome=step.outcome,
                        error_detail=step.error_detail,
                        http_status=504,
                    )
                    await await_ws_owned(finalize_error(err))
                    yield _sse_error_for_ingress(
                        "responses", errors.ErrType.TIMEOUT,
                        err.error_detail or step.outcome,
                    )
                    return
                if step.outcome in ("upstream_closed", "connection_lifecycle"):
                    err = AttemptResult(
                        outcome=step.outcome,
                        error_detail=step.error_detail,
                        http_status=(502 if step.outcome == "upstream_closed" else None),
                    )
                    await await_ws_owned(finalize_error(err))
                    yield _sse_error_for_ingress("responses", errors.ErrType.API, err.error_detail or "upstream websocket closed")
                    return
                if step.outcome == "blacklist_hit":
                    err = AttemptResult(outcome="blacklist_hit", error_detail=step.error_detail, http_status=503)
                    await await_ws_owned(finalize_error(err))
                    yield _sse_error_for_ingress("responses", errors.ErrType.API, err.error_detail or "blacklist")
                    return
                if step.outcome == "request_invalid":
                    err = AttemptResult(
                        outcome="request_invalid",
                        error_detail=step.error_detail or protocol_errors.responses_max_output_context_error_message(),
                        http_status=400,
                    )
                    await await_ws_owned(finalize_error(err))
                    yield _sse_error_for_ingress(
                        "responses",
                        errors.ErrType.INVALID_REQUEST,
                        err.error_detail or "invalid request",
                        code=(
                            protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                            if _is_context_length_exceeded_error(err.error_detail)
                            else None
                        ),
                    )
                    return
                if step.data is not None and not step.skip_downstream:
                    out = _ws_json_to_responses_sse(_identity_expose_frame(step.data, identity_state))
                    if out is not None:
                        yield out
                if step.outcome == "stream_upstream_error":
                    await await_ws_owned(finalize_error(AttemptResult(
                        outcome="stream_upstream_error",
                        error_detail=step.error_detail or "upstream stream error",
                        http_status=503,
                    )))
                    return
                if step.outcome == "success":
                    await await_ws_owned(finalize_success())
                    return
                if step.skip_downstream:
                    continue
        except (asyncio.CancelledError, GeneratorExit):
            if not state["finalized"]:
                await await_ws_owned(finalize_disconnect())
            raise
        except Exception as exc:
            if not state["finalized"]:
                lifecycle = connection_lifecycle_outcome(exc)
                await await_ws_owned(finalize_error(AttemptResult(
                    outcome=lifecycle or "transport_error",
                    error_detail=f"websocket stream error: {exc}"[:2000],
                    http_status=None if lifecycle else 502,
                )))
            raise
        finally:
            try:
                await await_ws_owned(upstream_ws.close())
            except BaseException:
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
    start_monotonic: float | None = None,
    attempt_start_monotonic: float | None = None,
    terminal_release=None,
) -> AttemptResult:
    if start_monotonic is None:
        start_monotonic = time.monotonic()
    if attempt_start_monotonic is None:
        attempt_start_monotonic = time.monotonic()
    cfg = config.get()
    timeouts = cfg.get("timeouts") or {}
    connect_timeout = int(timeouts.get("connect", 10))
    first_byte_timeout = int(timeouts.get("firstByte", 30))
    idle_timeout = int(timeouts.get("idle", 30))
    total_timeout = int(timeouts.get("total", 600))

    # 1. 构造上游请求
    try:
        upstream_req = await ch.build_upstream_request(
            body, resolved_model, ingress_protocol=ingress_protocol,
        )
        log_db.update_pending_fast_mode_from_upstream(
            request_id, upstream_req.body, upstream_req.headers,
        )
    except Exception as exc:
        # GuardError（OpenAI 跨变体死角）带 .status / .err_type / .message 属性；
        # scope=request 表示请求级 guard，可短路到客户端 4xx；scope=candidate
        # 表示当前 provider/model 不支持，后续候选可能支持，不能短路整个 failover。
        if hasattr(exc, "status") and hasattr(exc, "err_type") and hasattr(exc, "message"):
            outcome = "candidate_guard" if getattr(exc, "scope", "request") == "candidate" else "guard_error"
            return AttemptResult(
                outcome=outcome,
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
    cancel_state: dict[str, Any] = {}

    async def finish_cancelled_http_attempt(
        result: AttemptResult,
        *,
        proxy_name: str | None = None,
        proxy_bytes: dict | None = None,
    ) -> None:
        raw_buf = cancel_state.get("raw_buf")
        parts = cancel_state.get("parts")
        partial = (
            bytes(raw_buf)
            if isinstance(raw_buf, (bytes, bytearray)) and raw_buf
            else b"".join(parts)
            if isinstance(parts, list) and all(
                isinstance(item, (bytes, bytearray)) for item in parts
            )
            else b""
        )
        if not partial:
            tracker = cancel_state.get("tracker")
            if tracker is not None:
                try:
                    tracked = tracker.get_full_response()
                except Exception:
                    tracked = None
                if isinstance(tracked, str):
                    partial = tracked.encode("utf-8", errors="replace")
                elif isinstance(tracked, (bytes, bytearray)):
                    partial = bytes(tracked)
        if partial and not result.full_response_text:
            result.full_response_text = partial.decode("utf-8", errors="replace")
            normalized = model_pricing.normalize_response_billing(
                result.full_response_text
            )
            result.usage = {
                "input_tokens": normalized.input_tokens,
                "output_tokens": normalized.output_tokens,
                "cache_creation": normalized.cache_creation_tokens,
                "cache_read": normalized.cache_read_tokens,
            }
            if (
                normalized.cache_creation_5m_tokens is not None
                and normalized.cache_creation_1h_tokens is not None
            ):
                result.usage["cache_creation_5m"] = normalized.cache_creation_5m_tokens
                result.usage["cache_creation_1h"] = normalized.cache_creation_1h_tokens
            result.usage_observed = normalized.usage_observed
        bytes_up, bytes_down = _proxy_byte_snapshot(proxy_bytes)
        if retry_attempt_id is not None:
            await asyncio.to_thread(
                log_db.update_retry_attempt,
                retry_attempt_id,
                final_round_id=result.round_id,
                connect_ms=result.connect_ms,
                first_byte_ms=result.first_byte_ms,
                idle_ms=result.idle_ms,
                total_ms=result.total_ms,
                attempt_elapsed_ms=_elapsed_ms(attempt_start_monotonic),
                ended_at=time.time(),
                outcome="cancelled",
                error_detail="upstream HTTP round cancelled",
                proxy_name=proxy_name,
                bytes_up=bytes_up,
                bytes_down=bytes_down,
                response_body=result.full_response_text,
                usage=result.usage,
                usage_observed=result.usage_observed,
                settle=False,
            )
        await asyncio.to_thread(
            log_db.finish_error,
            request_id,
            "client disconnected",
            retry_count_so_far,
            final_channel_key=ch.key,
            final_channel_type=ch.type,
            final_model=resolved_model,
            connect_ms=result.connect_ms,
            first_token_ms=result.first_byte_ms,
            idle_ms=result.idle_ms,
            total_ms=result.total_ms,
            final_round_id=result.round_id,
            request_elapsed_ms=_elapsed_ms(start_monotonic),
            http_status=499,
            affinity_hit=affinity_hit,
            response_body=result.full_response_text,
            usage=result.usage,
            usage_observed=result.usage_observed,
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
            proxy_name=proxy_name,
            proxy_bytes_up=bytes_up,
            proxy_bytes_down=bytes_down,
            status="cancelled",
            **_request_stage_kwargs(result),
        )

    try:
        opened = await open_response_with_proxy_chain(
            channel=ch,
            resolved_model=resolved_model,
            upstream_req=upstream_req,
            connect_timeout=connect_timeout,
            first_byte_timeout=first_byte_timeout,
            idle_timeout=idle_timeout,
            total_timeout=total_timeout,
            response_mode=(
                "stream"
                if is_stream or getattr(ch, "upstream_stream_only", False)
                else "non_stream"
            ),
            request_id=request_id,
            retry_attempt_id=retry_attempt_id,
        )
    except asyncio.CancelledError:
        cancelled = AttemptResult(
            outcome="cancelled",
            error_detail="upstream HTTP round cancelled before response headers",
        )
        try:
            await await_ws_owned(finish_cancelled_http_attempt(cancelled))
        except Exception:
            traceback.print_exc()
        raise
    if opened.error is not None:
        return opened.error

    ctx = opened.ctx
    upstream_resp = opened.response
    connect_ms = opened.connect_ms
    _timing = opened.timing
    _proxy_name_used = opened.proxy_name
    _proxy_bytes = opened.proxy_bytes
    _proxy_client = opened.proxy_client

    try:
        # Cursor's loopback bridge returns the exact upstream conversation ID in
        # an internal-only header. Persist it before body consumption/settlement
        # so a delayed dashboard event can be joined without time/model guessing.
        if retry_attempt_id is not None and getattr(ch, "provider", "") == "cursor":
            conversation_id = str(
                upstream_resp.headers.get("X-Parrot-Cursor-Conversation-Id") or ""
            ).strip()
            if conversation_id:
                await asyncio.to_thread(
                    log_db.set_retry_attempt_cursor_conversation_id,
                    retry_attempt_id,
                    conversation_id,
                )

        # 1.5 响应头 snapshot 采样：成功/失败分支前都先记一次
        _maybe_record_codex_snapshot(ch, upstream_resp)
        _maybe_record_anthropic_snapshot(ch, upstream_resp)

        # 2. HTTP 状态码检查
        if upstream_resp.status_code >= 400:
            result = await read_http_error_response(
                ctx,
                upstream_resp,
                connect_ms=connect_ms,
                timing=_timing,
                round_timeouts=opened.round_timeouts,
                proxy_name=_proxy_name_used,
                proxy_bytes=_proxy_bytes,
                translator_ctx=upstream_req.translator_ctx,
                partial_state=cancel_state,
            )
            result = _attach_retry_after_from_response(result, upstream_resp, ch)
            result.openai_oauth_html_403 = (
                isinstance(ch, OpenAIOAuthChannel)
                and result.http_status == 403
                and is_html_error_document(result.full_response_text)
            )
            result = _request_invalid_result_if_needed(result)
            result = _finalize_http_attempt(opened, result)
            await _close_proxy_client(_proxy_client)
            return result

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
                timing=_timing,
                round_timeouts=opened.round_timeouts,
                start_monotonic=start_monotonic,
                cancel_state=cancel_state,
            )
            result = _attach_retry_after_from_response(result, upstream_resp, ch)
            result = _finalize_http_attempt(opened, result)
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
            timing=_timing,
            round_timeouts=opened.round_timeouts,
            opened_response=opened,
            retry_attempt_id=retry_attempt_id,
            start_monotonic=start_monotonic,
            attempt_start_monotonic=attempt_start_monotonic,
            cancel_state=cancel_state,
            terminal_release=terminal_release,
        )
        result = _attach_retry_after_from_response(result, upstream_resp, ch)
        if not result.stream_started:
            result = _finalize_http_attempt(opened, result)
            await _close_proxy_client(_proxy_client)
        return result
    except asyncio.CancelledError:
        await asyncio.shield(_safe_exit(ctx))
        await asyncio.shield(_close_proxy_client(_proxy_client))
        cancelled = AttemptResult(
            outcome="cancelled",
            error_detail="upstream HTTP round cancelled before downstream commit",
        )
        cancelled = await await_ws_owned(
            asyncio.to_thread(_finalize_http_attempt, opened, cancelled)
        )
        try:
            await await_ws_owned(finish_cancelled_http_attempt(
                cancelled,
                proxy_name=_proxy_name_used,
                proxy_bytes=_proxy_bytes,
            ))
        except Exception:
            traceback.print_exc()
        raise
    except Exception as exc:
        traceback.print_exc()
        try:
            await _safe_exit(ctx)
        except Exception:
            pass
        await _close_proxy_client(_proxy_client)
        lifecycle = connection_lifecycle_outcome(exc)
        result = AttemptResult(
            outcome=lifecycle or "transport_error",
            error_detail=f"unexpected: {exc}",
        )
        return _finalize_http_attempt(opened, result)


def _finalize_http_attempt(opened, result: AttemptResult) -> AttemptResult:
    finalize_opened_http_response(opened, result.outcome, result.error_detail)
    if opened.timing is not None:
        opened.timing.apply_to(result, terminal=False)
    return result


async def _safe_exit(ctx) -> None:
    await close_response_context(ctx)


async def _close_proxy_client(client) -> None:
    await close_proxy_client(client)


# ─── 非流式 ──────────────────────────────────────────────────────

async def _consume_non_stream(
    ctx, upstream_resp: httpx.Response, ch: Channel, resolved_model: str,
    dynamic_map: Optional[dict],
    connect_ms: int | None, start_time: float, request_id: str,
    messages: list, api_key_name: Optional[str], client_ip: str,
    fp_query: Optional[str], retry_count_so_far: int, affinity_hit: int,
    *, ingress_protocol: str = "anthropic",
    translator_ctx: Optional[dict] = None,
    body: Optional[dict] = None,
    client_key: Optional[str] = None,
    proxy_name: Optional[str] = None,
    proxy_bytes: Optional[dict] = None,
    timing=None,
    round_timeouts=None,
    start_monotonic: float | None = None,
    cancel_state: dict[str, Any] | None = None,
) -> AttemptResult:
    if start_monotonic is None:
        start_monotonic = time.monotonic()
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
            timing=timing,
            round_timeouts=round_timeouts,
            start_monotonic=start_monotonic,
            cancel_state=cancel_state,
        )

    body_read = await read_non_stream_body(
        ctx,
        upstream_resp,
        connect_ms=connect_ms,
        timing=timing,
        round_timeouts=round_timeouts,
        partial_state=cancel_state,
    )
    if body_read.error is not None:
        return body_read.error
    raw = body_read.raw or b""
    resp_headers = body_read.response_headers
    timing_snapshot = timing.snapshot(terminal=True) if timing is not None else None
    first_byte_ms = timing_snapshot.first_byte_ms if timing_snapshot is not None else None

    request_elapsed_ms = _elapsed_ms(start_monotonic)
    round_total_ms = timing_snapshot.total_ms if timing_snapshot is not None else None
    prepared = await prepare_non_stream_response(
        ch,
        raw,
        dynamic_map=dynamic_map,
        connect_ms=connect_ms,
        total_ms=round_total_ms,
        translator_ctx=translator_ctx,
    )
    if prepared.error is not None:
        return timing.apply_to(prepared.error) if timing is not None else prepared.error
    obj = prepared.obj or {}
    restored_text = prepared.restored_text

    # 成功：记录并构造响应
    usage = prepared.usage
    # assistant_msg 仅给亲和 fingerprint_write 用，且目前 fingerprint_write 只支持
    # anthropic 家族；openai 的亲和由 MS-7 补上。这里保持 anthropic 形状即可。
    assistant_msg = prepared.assistant_msg

    finalize_policy.apply_success_health_effects(
        finalize_policy.success_plan(),
        scorer=scorer,
        cooldown=cooldown,
        channel_key=channel_state.effect_key(ch),
        model=resolved_model,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        total_ms=round_total_ms,
    )

    # Owner persistence is auxiliary to the successful upstream response. A DB
    # failure is warned but must not turn this non-stream response into an error.
    compaction_owner.persist_observed_safe(ch, body, obj, path="http_non_stream")

    # 落库（用**上游原始响应体**，方便排错；翻译后的下游响应体由 JSONResponse 现场构造）
    await asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=connect_ms, first_token_ms=first_byte_ms,
        idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
        total_ms=round_total_ms,
        final_round_id=(timing_snapshot.round_id if timing_snapshot is not None else None),
        request_elapsed_ms=request_elapsed_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=restored_text,
        http_status=upstream_resp.status_code,
        upstream_protocol=getattr(ch, "protocol", "anthropic"),
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
        **_timing_stage_kwargs(timing, terminal=True),
    )

    # 跨变体：把上游 JSON 反向成 ingress 期望的格式；同协议 translator_ctx=None 即原样
    _maybe_cache_deepseek_reasoning(ch, resolved_model, obj)
    _maybe_cache_codex_reasoning_replay(translator_ctx, obj)
    out_obj = _apply_non_stream_response_translator(obj, translator_ctx or {})
    if ingress_protocol == "responses" and getattr(ch, "protocol", "") == "openai-responses":
        _maybe_save_native_responses_store(
            obj,
            body=body,
            api_key_name=api_key_name,
            channel_key=channel_state.effect_key(ch),
            model=resolved_model,
        )

    if ingress_protocol == "anthropic" and isinstance(out_obj, dict) and out_obj.get("type") == "message":
        assistant_msg = out_obj

    # 亲和写入（按 ingress 选 fingerprint_write 的参数空间与函数）
    _write_affinity_non_stream(ingress_protocol, api_key_name, client_ip,
                                messages, assistant_msg, body, out_obj,
                                channel_state.effect_key(ch), resolved_model,
                                client_key=client_key, fp_query=fp_query)

    response = JSONResponse(
        content=out_obj,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )
    result = AttemptResult(
        outcome="success", success=True, response=response,
        connect_ms=connect_ms, first_byte_ms=first_byte_ms,
        idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
        total_ms=round_total_ms, http_status=upstream_resp.status_code,
        usage=usage, assistant_response=assistant_msg,
        full_response_text=restored_text,
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
        translator_ctx=translator_ctx,
    )
    return timing.apply_to(result) if timing is not None else result



# ─── Stream-only 上游 → 非流式聚合 ─────────────────────────────────

async def _consume_stream_as_non_stream(
    ctx, upstream_resp: httpx.Response, ch: Channel, resolved_model: str,
    dynamic_map: Optional[dict],
    connect_ms: int | None, start_time: float, request_id: str,
    messages: list, api_key_name: Optional[str], client_ip: str,
    fp_query: Optional[str], retry_count_so_far: int, affinity_hit: int,
    *, ingress_protocol: str = "anthropic",
    translator_ctx: Optional[dict] = None,
    body: Optional[dict] = None,
    client_key: Optional[str] = None,
    proxy_name: Optional[str] = None,
    proxy_bytes: Optional[dict] = None,
    timing=None,
    round_timeouts=None,
    start_monotonic: float | None = None,
    cancel_state: dict[str, Any] | None = None,
) -> AttemptResult:
    """处理 upstream_stream_only=True 渠道的非流式下游请求。

    读取上游 SSE → 用 ResponsesSSEAssistantBuilder 聚合 → 构造成完整 /v1/responses
    JSON → 走与 _consume_non_stream 一致的 translator / 黑名单 / 落库 / 亲和链路。
    """
    if start_monotonic is None:
        start_monotonic = time.monotonic()
    total_timeout = int(round_timeouts.total) if round_timeouts is not None else 600
    first_byte_timeout = int(round_timeouts.first_byte) if round_timeouts is not None else 30
    idle_timeout = int(round_timeouts.idle) if round_timeouts is not None else 30

    # 上游是 openai-responses SSE（目前唯一 stream-only 渠道是 OpenAIOAuthChannel，
    # 其 protocol 固定为 "openai-responses"）
    assert getattr(ch, "protocol", "") == "openai-responses", \
        f"_consume_stream_as_non_stream only supports openai-responses upstream, got {getattr(ch, 'protocol', None)!r}"

    prepared = await aggregate_stream_as_non_stream_response(
        ctx,
        upstream_resp,
        ch,
        resolved_model,
        dynamic_map=dynamic_map,
        connect_ms=connect_ms,
        start_time=start_time,
        deadline_ts=0.0,  # legacy argument; round_timeouts is authoritative
        total_timeout=total_timeout,
        first_byte_timeout=first_byte_timeout,
        idle_timeout=idle_timeout,
        timing=timing,
        round_timeouts=round_timeouts,
        translator_ctx=translator_ctx,
        partial_state=cancel_state,
    )
    if prepared.error is not None:
        return timing.apply_to(prepared.error) if timing is not None else prepared.error

    obj = prepared.obj or {}
    resp_headers = prepared.response_headers
    usage = prepared.usage
    assistant_msg = prepared.assistant_msg
    first_byte_ms = prepared.first_byte_ms
    timing_snapshot = timing.snapshot(terminal=True) if timing is not None else None
    round_total_ms = prepared.total_ms
    request_elapsed_ms = _elapsed_ms(start_monotonic)
    response_body_text = prepared.response_body_text

    finalize_policy.apply_success_health_effects(
        finalize_policy.success_plan(),
        scorer=scorer,
        cooldown=cooldown,
        channel_key=channel_state.effect_key(ch),
        model=resolved_model,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        total_ms=round_total_ms,
    )

    compaction_owner.persist_observed_safe(
        ch, body, obj, path="http_sse_aggregate",
    )
    await asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=connect_ms, first_token_ms=first_byte_ms,
        idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
        total_ms=round_total_ms,
        final_round_id=(timing_snapshot.round_id if timing_snapshot is not None else None),
        request_elapsed_ms=request_elapsed_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=response_body_text,
        http_status=upstream_resp.status_code,
        upstream_protocol=getattr(ch, "protocol", "anthropic"),
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
        **_timing_stage_kwargs(timing, terminal=True),
    )

    # 6) 走跨变体 translator（如果 ingress 是 chat，上游 responses JSON 要翻译成 chat.completion JSON）
    _maybe_cache_deepseek_reasoning(ch, resolved_model, obj)
    _maybe_cache_codex_reasoning_replay(translator_ctx, obj)
    out_obj = _apply_non_stream_response_translator(obj, translator_ctx or {})
    if ingress_protocol == "responses" and getattr(ch, "protocol", "") == "openai-responses":
        _maybe_save_native_responses_store(
            obj,
            body=body,
            api_key_name=api_key_name,
            channel_key=ch.key,
            model=resolved_model,
        )
    if ingress_protocol == "anthropic" and isinstance(out_obj, dict) and out_obj.get("type") == "message":
        assistant_msg = out_obj

    # 亲和写入（与 _consume_non_stream 一致）
    _write_affinity_non_stream(ingress_protocol, api_key_name, client_ip,
                                messages, assistant_msg, body, out_obj,
                                channel_state.effect_key(ch), resolved_model,
                                client_key=client_key, fp_query=fp_query)

    response = JSONResponse(
        content=out_obj,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )
    result = AttemptResult(
        outcome="success", success=True, response=response,
        connect_ms=connect_ms, first_byte_ms=first_byte_ms,
        idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
        total_ms=round_total_ms,
        http_status=upstream_resp.status_code,
        usage=usage, assistant_response=assistant_msg,
        full_response_text=response_body_text,
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
        translator_ctx=translator_ctx,
    )
    return timing.apply_to(result) if timing is not None else result


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
    translator_ctx: Optional[dict] = None,
) -> tuple[list[bytes], Optional[dict]]:
    from .transports.http_runtime import _read_until_first_downstream_chunk as _runtime_read

    return await _runtime_read(
        aiter,
        ch,
        dynamic_map,
        tracker,
        builder,
        deadline_ts,
        idle_timeout,
        protocol=protocol,
        first_chunk=first_chunk,
        stream_translator=stream_translator,
        translator_ctx=translator_ctx,
    )


def _downstream_stream_protocol(ingress_protocol: str) -> str:
    from .transports.http_runtime import _downstream_stream_protocol as _runtime_protocol

    return _runtime_protocol(ingress_protocol)


async def _consume_stream(
    ctx, upstream_resp: httpx.Response, ch: Channel, resolved_model: str,
    dynamic_map: Optional[dict],
    connect_ms: int | None, start_time: float, deadline_ts: float,
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
    timing=None,
    round_timeouts=None,
    opened_response=None,
    retry_attempt_id: int | None = None,
    start_monotonic: float | None = None,
    attempt_start_monotonic: float | None = None,
    cancel_state: dict[str, Any] | None = None,
    terminal_release=None,
) -> AttemptResult:
    if start_monotonic is None:
        start_monotonic = time.monotonic()
    if attempt_start_monotonic is None:
        attempt_start_monotonic = start_monotonic
    stream_start = await prepare_stream_response_start(
        ctx,
        upstream_resp,
        ch,
        dynamic_map=dynamic_map,
        connect_ms=connect_ms,
        deadline_ts=deadline_ts,
        first_byte_timeout=first_byte_timeout,
        idle_timeout=idle_timeout,
        ingress_protocol=ingress_protocol,
        timing=timing,
        round_timeouts=round_timeouts,
        translator_ctx=translator_ctx,
        partial_state=cancel_state,
    )
    if stream_start.error is not None:
        return timing.apply_to(stream_start.error) if timing is not None else stream_start.error

    aiter = stream_start.aiter
    tracker = stream_start.tracker
    builder = stream_start.builder
    stream_translator = stream_start.stream_translator
    first_downstream_chunks = stream_start.first_downstream_chunks
    first_byte_ms = stream_start.first_byte_ms
    resp_headers = stream_start.response_headers
    upstream_status = int(stream_start.upstream_status or upstream_resp.status_code)
    ch_proto = getattr(ch, "protocol", "anthropic")

    # 3. 通过检查 → 开始向下游发 ★
    state: dict = {"finalized": False}
    stream_resources_closed = False
    terminal_slot_released = False

    async def _close_stream_resources() -> None:
        nonlocal stream_resources_closed
        if stream_resources_closed:
            return
        await _safe_exit(ctx)
        await _close_proxy_client(proxy_client)
        stream_resources_closed = True

    def _release_terminal_slot_once() -> None:
        nonlocal terminal_slot_released
        if terminal_slot_released or terminal_release is None:
            return
        terminal_slot_released = True
        try:
            terminal_release()
        except Exception:
            pass

    async def _close_terminal_resources_and_release() -> None:
        try:
            await _close_stream_resources()
        finally:
            _release_terminal_slot_once()

    def _responses_terminal_received() -> bool:
        """Responses 的显式终态就是 I/O 边界，不再额外等待 HTTP EOF。

        部分兼容上游会在 response.completed/failed/incomplete 后保持连接；继续
        等 EOF 会长期占用共享连接池，而且客户端收到终态后断开还会把正常请求
        带进 cancellation 收尾路径。
        """
        return (
            ch_proto == "openai-responses"
            and bool(getattr(tracker, "saw_stream_end", False))
        )

    def _responses_terminal_error_received() -> bool:
        return (
            _responses_terminal_received()
            and bool(getattr(tracker, "saw_stream_error", False))
        )

    def _chat_done_received() -> bool:
        return ch_proto == "openai-chat" and bool(getattr(tracker, "done_received", False))

    def _finish_stream_timing(outcome: str, error_detail: str | None = None):
        if timing is None:
            return None
        return (
            finalize_opened_http_response(opened_response, outcome, error_detail)
            if opened_response is not None
            else timing.finish(outcome, error_detail)
        )

    async def _persist_stream_retry_attempt(snapshot, outcome: str, error_detail: str | None = None):
        if snapshot is None or retry_attempt_id is None:
            return
        await asyncio.shield(asyncio.to_thread(
            log_db.update_retry_attempt,
            retry_attempt_id,
            final_round_id=snapshot.round_id,
            connect_ms=snapshot.connect_ms,
            first_byte_ms=snapshot.first_byte_ms,
            idle_ms=snapshot.idle_ms,
            attempt_elapsed_ms=_elapsed_ms(attempt_start_monotonic),
            request_upload_ms=snapshot.request_upload_ms,
            response_headers_wait_ms=snapshot.response_headers_wait_ms,
            response_body_first_byte_wait_ms=snapshot.response_body_first_byte_wait_ms,
            total_ms=snapshot.total_ms,
            ended_at=time.time(),
            outcome=outcome,
            error_detail=(error_detail or "")[:4000] if error_detail else None,
            proxy_name=proxy_name,
            bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
            bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
            settle=False,
        ))

    async def _persist_stream_retry(outcome: str, error_detail: str | None = None):
        snapshot = _finish_stream_timing(outcome, error_detail)
        await _persist_stream_retry_attempt(snapshot, outcome, error_detail)
        return snapshot

    async def _finalize_success():
        if state["finalized"]:
            return
        state["finalized"] = True
        request_elapsed_ms = _elapsed_ms(start_monotonic)
        timing_preview = timing.snapshot(terminal=True) if timing is not None else None
        round_total_ms = timing_preview.total_ms if timing_preview is not None else None

        finalize_policy.apply_success_health_effects(
            finalize_policy.success_plan(
                cache_reasoning_replay=(
                    getattr(ch, "protocol", "anthropic") == "openai-responses"
                    and hasattr(builder, "to_full_json")
                )
            ),
            scorer=scorer,
            cooldown=cooldown,
            channel_key=channel_state.effect_key(ch),
            model=resolved_model,
            connect_ms=connect_ms,
            first_byte_ms=first_byte_ms,
            total_ms=round_total_ms,
        )

        upstream_protocol = getattr(ch, "protocol", "anthropic")
        if upstream_protocol == "openai-chat" and hasattr(builder, "get_assistant"):
            _maybe_cache_deepseek_reasoning(ch, resolved_model, {
                "model": resolved_model,
                "choices": [{"message": builder.get_assistant()}],
            })
        if upstream_protocol == "openai-responses" and hasattr(builder, "to_full_json"):
            try:
                native_response_obj = builder.to_full_json(fallback_model=resolved_model)
                compaction_owner.persist_observed_safe(
                    ch, body, native_response_obj, path="http_sse_stream_finalize",
                )
                _maybe_cache_deepseek_reasoning(ch, resolved_model, native_response_obj)
                _maybe_cache_codex_reasoning_replay(translator_ctx, native_response_obj)
                if ingress_protocol == "responses":
                    _maybe_save_native_responses_store(
                        native_response_obj,
                        body=body,
                        api_key_name=api_key_name,
                        channel_key=channel_state.effect_key(ch),
                        model=resolved_model,
                    )
            except Exception:
                pass

        # 亲和写入：按 ingress 走对应家族的 fingerprint_write。
        # 4 种组合都覆盖：anthropic / 同协议 chat-chat / 同协议 resp-resp /
        # 跨变体 resp→chat / 跨变体 chat→resp。跨变体用对应 translator 累积的
        # 下游形状做 fingerprint_write，保证与下次请求的 fingerprint_query 同形。
        ch_proto = getattr(ch, "protocol", "anthropic")
        fp_write: Optional[str] = None
        if ingress_protocol == "anthropic":
            if ch_proto in ("openai-chat", "openai-responses") and stream_translator is not None:
                try:
                    assistant_msg = getattr(stream_translator, "get_downstream_anthropic_assistant")()
                except Exception:
                    assistant_msg = {"role": "assistant", "content": []}
            else:
                assistant_msg = builder.get_assistant()
            fp_write = fingerprint.fingerprint_write(
                api_key_name or "", client_ip or "", messages, assistant_msg,
            )
        elif ingress_protocol == "chat" and ch_proto == "anthropic":
            try:
                assistant_msg = (getattr(stream_translator, "get_downstream_chat_assistant")()
                                 if stream_translator else {"role": "assistant", "content": None})
            except Exception:
                assistant_msg = {"role": "assistant", "content": None}
            fp_write = fingerprint.fingerprint_write_chat(
                api_key_name or "", client_ip or "",
                (body or {}).get("messages") or [], assistant_msg,
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
        elif ingress_protocol == "responses" and ch_proto == "anthropic":
            try:
                output_items = (getattr(stream_translator, "get_downstream_responses_output")()
                                if stream_translator else [])
            except Exception:
                output_items = []
            cur_input = _responses_current_input_items(body or {})
            fp_write = fingerprint.fingerprint_write_responses(
                api_key_name or "", client_ip or "", cur_input, output_items,
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
        prompt_cache_key = _openai_prompt_cache_key_from_body(ingress_protocol, body)
        if fp_query:
            affinity.upsert(
                fp_query, channel_state.effect_key(ch), resolved_model,
                prompt_cache_key=prompt_cache_key,
            )
        if fp_write:
            affinity.upsert(
                fp_write, channel_state.effect_key(ch), resolved_model,
                prompt_cache_key=prompt_cache_key,
            )
        # 同步更新 client-level soft affinity
        if client_key:
            affinity.client_upsert(
                client_key, channel_state.effect_key(ch), resolved_model,
            )

        # shield：客户端断开导致的 CancelledError 不应中断 DB 写入，否则
        # 日志会残留 pending。(参见 _finalize_client_cancelled 早退守卫)
        timing_snapshot = await _persist_stream_retry("success")
        await asyncio.shield(asyncio.to_thread(
            log_db.finish_success,
            request_id, ch.key, ch.type, resolved_model,
            input_tokens=tracker.usage["input_tokens"],
            output_tokens=tracker.usage["output_tokens"],
            cache_creation_tokens=tracker.usage["cache_creation"],
            cache_read_tokens=tracker.usage["cache_read"],
            connect_ms=connect_ms, first_token_ms=first_byte_ms,
            idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
            total_ms=(timing_snapshot.total_ms if timing_snapshot is not None else None),
            final_round_id=(timing_snapshot.round_id if timing_snapshot is not None else None),
            request_elapsed_ms=request_elapsed_ms,
            retry_count=retry_count_so_far, affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(),
            http_status=upstream_status,
            usage_observed=tracker.usage_observed,
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
            proxy_name=proxy_name,
            proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
            proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
            **_timing_stage_kwargs(timing, terminal=True),
        ))

    async def _emit_error_and_finalize(err_type: str, message: str, outcome: str):
        if state["finalized"]:
            return
        state["finalized"] = True
        request_elapsed_ms = _elapsed_ms(start_monotonic)

        # 已发首包的普通上游错误视为本次渠道失败；但上下文/请求级错误
        # 不是渠道健康问题，即使在流中途才被上游明确揭示，也按 runtime
        # request_invalid 语义处理，避免误伤渠道评分/冷却。
        failure_policy = "runtime" if outcome == "request_invalid" else "post_commit_stream"
        plan = finalize_policy.error_plan(outcome, failure_policy=failure_policy)
        finalize_policy.apply_error_health_effects(
            plan,
            scorer=scorer,
            cooldown=cooldown,
            channel_key=channel_state.effect_key(ch),
            model=resolved_model,
            error_detail=message,
            connect_ms=connect_ms,
        )

        timing_snapshot = await _persist_stream_retry(outcome, message)
        await asyncio.shield(asyncio.to_thread(
            log_db.finish_error,
            request_id, message, retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=connect_ms, first_token_ms=first_byte_ms,
            idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
            total_ms=(timing_snapshot.total_ms if timing_snapshot is not None else None),
            final_round_id=(timing_snapshot.round_id if timing_snapshot is not None else None),
            request_elapsed_ms=request_elapsed_ms,
            http_status=(400 if outcome == "request_invalid" else upstream_status),
            affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(),
            usage=tracker.usage,
            usage_observed=tracker.usage_observed,
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
            proxy_name=proxy_name,
            proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
            proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
            **_timing_stage_kwargs(timing, terminal=True),
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
        cancel_plan = finalize_policy.client_cancelled_plan(
            saw_stream_error=bool(getattr(tracker, "saw_stream_error", False)),
            saw_stream_end=bool(getattr(tracker, "saw_stream_end", False)),
        )
        if cancel_plan.terminal == "error":
            msg = getattr(tracker, "stream_error_message", None) or "upstream stream error"
            await _emit_error_and_finalize("api_error", msg, outcome="stream_upstream_error")
            return
        if cancel_plan.terminal == "success":
            await _finalize_success()
            return
        state["finalized"] = True
        request_elapsed_ms = _elapsed_ms(start_monotonic)
        # The outer request record is the user-visible terminal truth.  Persist
        # it before the retry-chain bookkeeping: a disconnect can interrupt the
        # latter after it has been written, which otherwise leaves request_log
        # pending until the stale-record cleaner falsely calls it a crash.
        timing_snapshot = _finish_stream_timing(
            "client_disconnected", "client disconnected",
        )
        await await_ws_owned(asyncio.to_thread(
            log_db.finish_error,
            request_id, "client disconnected", retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=connect_ms, first_token_ms=first_byte_ms,
            idle_ms=(timing_snapshot.idle_ms if timing_snapshot is not None else None),
            total_ms=(timing_snapshot.total_ms if timing_snapshot is not None else None),
            final_round_id=(timing_snapshot.round_id if timing_snapshot is not None else None),
            request_elapsed_ms=request_elapsed_ms,
            http_status=499, affinity_hit=affinity_hit,
            response_body=tracker.get_full_response(), status="cancelled",
            usage=tracker.usage,
            usage_observed=tracker.usage_observed,
            upstream_protocol=getattr(ch, "protocol", "anthropic"),
            proxy_name=proxy_name,
            proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
            proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
            **_timing_stage_kwargs(timing, terminal=True),
        ))
        await _persist_stream_retry_attempt(
            timing_snapshot, "client_disconnected", "client disconnected",
        )

    async def _finalize_terminal_success() -> list[bytes]:
        """Persist success and release upstream resources before terminal output."""

        terminal_chunks: list[bytes] = []
        if stream_translator is not None:
            terminal_chunks = list(stream_translator.close())
        await await_ws_owned(_finalize_success())
        await _close_terminal_resources_and_release()
        return terminal_chunks

    async def _finalize_terminal_error(err_type: str, message: str, outcome: str) -> None:
        """Persist an explicit Responses error and release resources before output."""

        await await_ws_owned(_emit_error_and_finalize(err_type, message, outcome))
        await _close_terminal_resources_and_release()

    async def stream_generator():
        """把首包 + 后续 chunk 转发给下游，同时在中途错误时用 SSE error event 收尾。"""
        if state["finalized"]:
            return
        try:
            # 首个实际下游 chunk 已在返回 StreamingResponse 前确定，确保
            # failover 锁定点等于“下游真的会收到字节”。
            if (
                getattr(tracker, "saw_stream_error", False)
                and getattr(tracker, "stream_error_code", None) == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
            ):
                msg = (
                    getattr(tracker, "stream_error_message", None)
                    or protocol_errors.responses_max_output_context_error_message()
                )
                await _finalize_terminal_error(
                    errors.ErrType.INVALID_REQUEST,
                    msg,
                    outcome="request_invalid",
                )
                yield _sse_error_for_ingress(
                    ingress_protocol,
                    errors.ErrType.INVALID_REQUEST,
                    msg,
                    code=protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE,
                )
                return

            if getattr(tracker, "saw_stream_error", False):
                msg = getattr(tracker, "stream_error_message", None) or "upstream stream error"
                if _responses_terminal_error_received():
                    await _finalize_terminal_error(
                        "api_error", msg,
                        outcome="stream_upstream_error",
                    )
                    for out in first_downstream_chunks:
                        yield out
                else:
                    for out in first_downstream_chunks:
                        yield out
                    await await_ws_owned(_emit_error_and_finalize(
                        "api_error", msg,
                        outcome="stream_upstream_error",
                    ))
                return

            # Responses 客户端可以在读到 response.completed 后立即返回工具调用，
            # 不再继续拉取 HTTP EOF，也不一定显式关闭 body iterator。因此显式
            # 终态必须先完成 Store / retry / request_log 落账，再把终态帧交给下游；
            # 否则生成器会永久停在 yield，最终被 stale cleaner 误标成 crash。
            if _responses_terminal_received():
                terminal_chunks = await _finalize_terminal_success()
                for out in first_downstream_chunks:
                    yield out
                for out in terminal_chunks:
                    yield out
                return

            for out in first_downstream_chunks:
                yield out
            if _chat_done_received():
                terminal_chunks = await _finalize_terminal_success()
                for out in terminal_chunks:
                    yield out
                return

            # 后续 chunk，带 first-byte / idle / total 超时
            while True:
                step = await read_next_stream_step(
                    aiter=aiter,
                    channel=ch,
                    dynamic_map=dynamic_map,
                    tracker=tracker,
                    builder=builder,
                    stream_translator=stream_translator,
                    deadline_ts=deadline_ts,
                    start_time=start_time,
                    idle_timeout=idle_timeout,
                    timing=timing,
                    round_timeouts=round_timeouts,
                    translator_ctx=translator_ctx,
                    upstream_status=upstream_status,
                )
                if step.kind == "end":
                    break
                if step.kind == "error":
                    if step.err_type == "timeout_error":
                        err_type = errors.ErrType.TIMEOUT
                    elif step.err_type == "invalid_request_error" or step.outcome == "request_invalid":
                        err_type = errors.ErrType.INVALID_REQUEST
                    else:
                        err_type = errors.ErrType.API
                    msg = step.message or "stream error"
                    if _responses_terminal_error_received():
                        await _finalize_terminal_error(
                            err_type,
                            msg,
                            outcome=step.outcome or "transport_error",
                        )
                    else:
                        await await_ws_owned(_emit_error_and_finalize(
                            err_type,
                            msg,
                            outcome=step.outcome or "transport_error",
                        ))
                    yield _sse_error_for_ingress(
                        ingress_protocol,
                        err_type,
                        msg,
                        code=(
                            protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                            if step.outcome == "request_invalid" and _is_context_length_exceeded_error(msg)
                            else None
                        ),
                    )
                    return
                if step.kind == "blacklist":
                    msg = step.message or "blacklist"
                    await await_ws_owned(_emit_error_and_finalize(
                        errors.ErrType.API,
                        msg,
                        outcome="blacklist_hit",
                    ))
                    yield _sse_error_for_ingress(ingress_protocol, errors.ErrType.API, msg)
                    return

                # Responses 的显式错误终态与 completed 一样，是客户端停止读取的
                # 正常边界：必须先落库、关闭上游并释放渠道 slot，再原样转发终态。
                # 非终态 event:error 及 Chat/Anthropic error chunk 保持原有时序。
                if getattr(tracker, "saw_stream_error", False):
                    msg = getattr(tracker, "stream_error_message", None) or "upstream stream error"
                    if _responses_terminal_error_received():
                        await _finalize_terminal_error(
                            "api_error", msg,
                            outcome="stream_upstream_error",
                        )
                        for out in step.downstream_chunks:
                            yield out
                    else:
                        for out in step.downstream_chunks:
                            yield out
                        await await_ws_owned(_emit_error_and_finalize(
                            "api_error", msg,
                            outcome="stream_upstream_error",
                        ))
                    return

                if _responses_terminal_received():
                    terminal_chunks = await _finalize_terminal_success()
                    for out in step.downstream_chunks:
                        yield out
                    for out in terminal_chunks:
                        yield out
                    return

                for out in step.downstream_chunks:
                    yield out
                if _chat_done_received():
                    terminal_chunks = await _finalize_terminal_success()
                    for out in terminal_chunks:
                        yield out
                    return

            if not getattr(tracker, "saw_stream_end", False):
                # A transport EOF after visible output cannot be retried without
                # duplicating client-visible text, but it is still not success.
                # Surface a stream error and keep the request/channel logs honest
                # instead of fabricating finish_reason=stop.
                msg = "upstream stream ended without a terminal response event; response may be incomplete"
                await await_ws_owned(_emit_error_and_finalize(
                    errors.ErrType.API,
                    msg,
                    outcome="stream_truncated",
                ))
                yield _sse_error_for_ingress(ingress_protocol, errors.ErrType.API, msg)
                return

            # 上游已正常收尾 → 先让 translator 生成终态帧/完成内部副作用，
            # 再落库 success，最后 yield 终态帧。这样 close()/Store 阶段若异常，
            # 不会先把日志标成成功；而 success 已落库后客户端在终态帧期间断开，
            # state["finalized"] 也会避免误标成 client disconnected。
            terminal_chunks: list[bytes] = []
            if stream_translator is not None:
                terminal_chunks = list(stream_translator.close())
            # terminal owner 在客户端恰好于终态帧附近断开时也必须完成 retry、
            # proxy 和 request_log 三者落账，不能留下半完成 pending。
            await await_ws_owned(_finalize_success())
            for out in terminal_chunks:
                yield out
        except asyncio.CancelledError:
            # 客户端断开（或上层取消）：不归咎上游，不记 cooldown/scorer。
            # 独立 terminal task 必须先完成持久化，再向上重新抛取消。
            await await_ws_owned(_finalize_client_cancelled())
            raise
        except GeneratorExit:
            # StreamingResponse/ASGI 主动 aclose async generator 时可能使用
            # GeneratorExit 而不是 CancelledError，语义同样是下游结束消费。
            await await_ws_owned(_finalize_client_cancelled())
            raise
        except BaseException as exc:
            await await_ws_owned(_emit_error_and_finalize(
                "api_error", f"stream error: {exc}",
                outcome=connection_lifecycle_outcome(
                    exc,
                    http_status=upstream_status,
                    http_phase="response_body",
                ) or "transport_error",
            ))
            raise
        finally:
            await _close_stream_resources()

    sresp = StreamingResponse(
        stream_generator(),
        status_code=upstream_status,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
    )

    result = AttemptResult(
        outcome="success", success=True, stream_started=True,
        response=sresp, http_status=upstream_status,
        connect_ms=connect_ms, first_byte_ms=first_byte_ms,
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
    )
    return timing.apply_to(result, terminal=False) if timing is not None else result
