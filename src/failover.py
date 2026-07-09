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
from typing import Optional, Any

import httpx
import websockets
from fastapi.responses import JSONResponse, Response, StreamingResponse
from websockets.exceptions import InvalidStatus

import threading

from . import (
    affinity, blacklist, compact_rescue, concurrency, config, cooldown, errors, fingerprint,
    local_web_tools, log_db, model_metadata, notifier, oauth_manager, scorer, state_db,
    token_counter, upstream,
)
from .channel.base import Channel
from .channel.openai_oauth_channel import OpenAIOAuthChannel
from .transform import cc_mimicry
from .openai import reasoning_replay
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
from .protocols import finalize as finalize_policy
from .protocols import errors as protocol_errors
from .protocols.runtime import (
    AttemptResult,
    apply_non_stream_response_translator,
    failover_final_http_status,
    is_context_1m_credit_error,
    is_responses_ws_visible_event_type,
    json_error_for_ingress,
    make_stream_translator,
    prepare_non_stream_response,
    is_context_length_exceeded_error,
    is_invalid_encrypted_content_error,
    responses_ws_error_detail,
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
    WsProxyBytes,
    connect_upstream_ws,
    legacy_socks5_connector,
    open_socket_via_ss2022,
    resolve_ws_route_chain,
    socks5h_url,
    ws_event_type,
    ws_frame_size,
    ws_route_kwargs,
)
from .transports import policy as transport_policy


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
        traceback.print_exc()
        try:
            ek = notifier.escape_html
            notifier.throttled_notify_event_sync(
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
):
    return json_error_for_ingress(ingress, status, anth_err_type, message, code=code)


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


def _is_context_1m_credit_error(result: AttemptResult, resolved_model: str, body: dict) -> bool:
    return is_context_1m_credit_error(result, resolved_model, body)


def _retry_body_without_context_1m(body: dict) -> dict:
    return retry_body_without_context_1m(body)


def _proxy_route_kwargs(ch: Channel, resolved_model: str) -> dict:
    return transport_policy.proxy_route_kwargs(ch, resolved_model)


def _pick_non_direct_proxy_name(ch: Channel, resolved_model: str) -> str | None:
    return transport_policy.pick_non_direct_proxy_name(ch, resolved_model)


def _proxy_byte_snapshot(proxy_bytes: Optional[dict]) -> tuple[int, int]:
    return transport_policy.proxy_byte_snapshot(proxy_bytes)


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

def _remaining_ms(deadline_ts: float) -> int:
    return max(0, int((deadline_ts - time.time()) * 1000))


def _err_type_from_outcome(outcome: str, http_status: Optional[int]) -> str:
    return protocol_errors.classify_attempt_outcome(outcome, http_status).anthropic_error_type


def _pick_upstream_headers(resp: httpx.Response) -> dict:
    """转发部分上游 headers 到下游（限定范围）。"""
    return metadata_from_response(resp).forward_headers()


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
    start_time: float,
    *,
    retry_count: int,
    affinity_hit: int,
) -> None:
    total_ms = int((time.time() - start_time) * 1000)
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
        total_ms=total_ms,
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

    direct_body = compact_rescue.build_direct_summary_body(
        body,
        model=compression_model,
        max_tokens=model_metadata.summary_reserve_tokens(compression_model),
    )

    prompt_tokens = token_counter.count_request_tokens(direct_body, model=compression_model)
    if not model_metadata.can_fit_for_compact(compression_model, prompt_tokens):
        required = model_metadata.required_context_for_compact(prompt_tokens, compression_model)
        window = model_metadata.context_window(compression_model)
        return (
            None,
            f"compression model {compression_model} context not enough: "
            f"required={required} window={window}",
        )

    from . import scheduler as scheduler_mod

    route = scheduler_mod.schedule(
        direct_body,
        api_key_name=api_key_name or "",
        client_ip=client_ip,
        ingress_protocol=ingress_protocol,
    )
    if not route:
        return None, f"compression model {compression_model} has no available route"

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
    return compression_model, route, None


async def _run_compact_map_reduce_rescue(
    schedule_result: ScheduleResult,
    body: dict,
    request_id: str,
    api_key_name: Optional[str],
    client_ip: str,
    is_stream: bool,
    start_time: float,
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
            start_time,
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

    chunks = compact_rescue.split_messages_for_compact(
        messages,
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
            start_time,
            retry_count=len(chunks),
            affinity_hit=affinity_hit,
        )
        if is_stream:
            return local_web_tools.maybe_wrap_anthropic_json_response_as_sse(final_response)
        return final_response
    except Exception as exc:
        msg = f"compact rescue failed: {exc}"
        total_ms = int((time.time() - start_time) * 1000)
        await asyncio.to_thread(
            log_db.finish_error,
            request_id,
            msg[:4000],
            0,
            final_channel_key="compact-rescue",
            final_channel_type="internal",
            final_model=str(body.get("model") or "compact"),
            total_ms=total_ms,
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
        ))
        return local_web_tools.stream_responses_response_task_with_pings(task)

    downstream_stream_requested = bool(is_stream)
    local_web_rounds = 0
    local_web_limit_reported = False

    retry_count = 0
    refreshed_once: set[str] = set()
    retried_without_context_1m: set[tuple[str, str]] = set()
    retried_without_encrypted_content = False
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
            candidate_local_web_loop = local_web_loop_active and getattr(ch, "protocol", "anthropic") != "anthropic"
            candidate_openai_local_web_loop = openai_local_web_loop_active
            effective_is_stream = is_stream and not (candidate_local_web_loop or candidate_openai_local_web_loop)
            attempt_body = body
            if (candidate_local_web_loop or candidate_openai_local_web_loop) and body.get("stream"):
                attempt_body = dict(body)
                attempt_body["stream"] = False
            if _should_use_responses_upstream_ws(ch, ingress_protocol=ingress_protocol, cfg=cfg):
                result = await _try_openai_oauth_responses_ws_channel(
                    ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                    fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                    request_id, retry_count, affinity_hit, client_key=client_key,
                    retry_attempt_id=attempt_id,
                )
            else:
                result = await _try_channel(
                    ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                    fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                    request_id, retry_count, affinity_hit,
                    ingress_protocol=ingress_protocol,
                    client_key=client_key,
                    retry_attempt_id=attempt_id,
                )
        except BaseException:
            _release_once()
            raise
        result = _request_invalid_result_if_needed(result)
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
                max_rounds = local_web_tools.max_tool_rounds()
                if local_web_rounds >= max_rounds:
                    if local_web_limit_reported:
                        _release_once()
                        msg = "local web tool loop kept requesting WebSearch/WebFetch after maxToolRounds"
                        total_ms = int((time.time() - start_time) * 1000)
                        await asyncio.to_thread(
                            log_db.finish_error, request_id, msg, retry_count,
                            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                            connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms, total_ms=total_ms,
                            http_status=400, affinity_hit=affinity_hit,
                            upstream_protocol=getattr(ch, "protocol", "anthropic"),
                            proxy_name=result.proxy_name,
                            proxy_bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
                            proxy_bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
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
                max_rounds = local_web_tools.max_tool_rounds()
                if local_web_rounds >= max_rounds:
                    if local_web_limit_reported:
                        _release_once()
                        msg = "local OpenAI web_search loop kept requesting web_search after maxToolRounds"
                        total_ms = int((time.time() - start_time) * 1000)
                        await asyncio.to_thread(
                            log_db.finish_error, request_id, msg, retry_count,
                            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                            connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms, total_ms=total_ms,
                            http_status=400, affinity_hit=affinity_hit,
                            upstream_protocol=getattr(ch, "protocol", "anthropic"),
                            proxy_name=result.proxy_name,
                            proxy_bytes_up=int(getattr(result, "proxy_bytes_up", 0) or 0),
                            proxy_bytes_down=int(getattr(result, "proxy_bytes_down", 0) or 0),
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

        # 请求级 guard 错误：所有 openai 候选语义一致，切也无用，直接短路 4xx
        if result.outcome == "guard_error":
            status = int(result.http_status or 400)
            msg = result.error_detail or "request rejected by guard"
            # err_type 直接从 status 反推（保持与 classify_http_status 一致）
            anth_err_type = protocol_errors.legacy_anthropic_error_type_for_http_status(status)
            total_ms = int((time.time() - start_time) * 1000)
            await asyncio.to_thread(
                log_db.finish_error, request_id, msg[:4000], retry_count,
                final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                connect_ms=None, first_token_ms=None, total_ms=total_ms,
                http_status=status, affinity_hit=affinity_hit,
                upstream_protocol=getattr(ch, "protocol", "anthropic"),
            )
            return _json_error_for_ingress(ingress_protocol, status, anth_err_type, msg)

        # 下游请求内容错误（例如坏 encrypted_content）：不要冷却渠道。
        # 对坏 EC 做一次同渠道降级重试：剥掉本次请求 input 里的 encrypted_content，
        # 让下游 transcript owner 在成功响应中拿到新的 EC；失败则返回 400。
        if result.outcome == "request_invalid":
            msg = result.error_detail or "invalid request"
            if (
                _is_invalid_encrypted_content_error(msg)
                and not retried_without_encrypted_content
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
            total_ms = int((time.time() - start_time) * 1000)
            await asyncio.to_thread(
                log_db.finish_error, request_id, msg[:4000], retry_count,
                final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms, total_ms=total_ms,
                http_status=status, affinity_hit=affinity_hit,
                upstream_protocol=getattr(ch, "protocol", "anthropic"),
            )
            return _json_error_for_ingress(
                ingress_protocol,
                status,
                protocol_errors.legacy_anthropic_error_type_for_http_status(status),
                msg,
                code=(
                    protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                    if _is_context_length_exceeded_error(msg)
                    else None
                ),
            )

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
        plan = finalize_policy.error_plan(result.outcome, failure_policy="runtime")
        finalize_policy.apply_error_health_effects(
            plan,
            scorer=scorer,
            cooldown=cooldown,
            channel_key=ch.key,
            model=resolved_model,
            error_detail=result.error_detail,
            connect_ms=result.connect_ms,
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
                    candidate_local_web_loop = local_web_loop_active and getattr(ch, "protocol", "anthropic") != "anthropic"
                    candidate_openai_local_web_loop = openai_local_web_loop_active
                    effective_is_stream = is_stream and not (candidate_local_web_loop or candidate_openai_local_web_loop)
                    attempt_body = body
                    if (candidate_local_web_loop or candidate_openai_local_web_loop) and body.get("stream"):
                        attempt_body = dict(body)
                        attempt_body["stream"] = False
                    if _should_use_responses_upstream_ws(ch, ingress_protocol=ingress_protocol, cfg=cfg):
                        result = await _try_openai_oauth_responses_ws_channel(
                            ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                            fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                            request_id, retry_count, affinity_hit, client_key=client_key,
                            retry_attempt_id=attempt_id,
                        )
                    else:
                        result = await _try_channel(
                            ch, resolved_model, attempt_body, effective_is_stream, deadline_ts, start_time,
                            fp_query, attempt_body.get("messages") or [], api_key_name, client_ip,
                            request_id, retry_count, affinity_hit,
                            ingress_protocol=ingress_protocol,
                            client_key=client_key,
                            retry_attempt_id=attempt_id,
                        )
                except BaseException:
                    _release_q()
                    raise
                result = _request_invalid_result_if_needed(result)
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
                if result.success and candidate_local_web_loop and downstream_stream_requested:
                    result.response = local_web_tools.maybe_wrap_anthropic_json_response_as_sse(result.response)
                if result.success and candidate_openai_local_web_loop and downstream_stream_requested:
                    result.response = local_web_tools.maybe_wrap_responses_json_response_as_sse(result.response)
                if result.success or result.stream_started:
                    _attach_release_to_response(result.response, _release_q)
                    return result.response
                _release_q()
                if result.outcome == "request_invalid":
                    status = int(result.http_status or 400)
                    msg = result.error_detail or "invalid request"
                    total_ms = int((time.time() - start_time) * 1000)
                    await asyncio.to_thread(
                        log_db.finish_error, request_id, msg[:4000], retry_count,
                        final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
                        connect_ms=result.connect_ms, first_token_ms=result.first_byte_ms, total_ms=total_ms,
                        http_status=status, affinity_hit=affinity_hit,
                        upstream_protocol=getattr(ch, "protocol", "anthropic"),
                    )
                    return _json_error_for_ingress(
                        ingress_protocol,
                        status,
                        protocol_errors.legacy_anthropic_error_type_for_http_status(status),
                        msg,
                        code=(
                            protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                            if _is_context_length_exceeded_error(msg)
                            else None
                        ),
                    )
                # 排队拿到的这次也失败了 → 落入"全失败"分支
                plan = finalize_policy.error_plan(result.outcome, failure_policy="runtime")
                finalize_policy.apply_error_health_effects(
                    plan,
                    scorer=scorer,
                    cooldown=cooldown,
                    channel_key=ch.key,
                    model=resolved_model,
                    error_detail=result.error_detail,
                    connect_ms=result.connect_ms,
                )
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
    status = failover_final_http_status(last_result)
    if last_result and last_result.outcome == "candidate_guard":
        status = int(last_result.http_status or 400)
        err_type = protocol_errors.legacy_anthropic_error_type_for_http_status(status)
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

_WsProxyBytes = WsProxyBytes


def _http_url_to_ws(url: str) -> str:
    return http_url_to_ws(url)


def _socks5h_url(url: str) -> str:
    return socks5h_url(url)


def _ws_proxy_snapshot(proxy_bytes: _WsProxyBytes) -> tuple[int, int]:
    return int(proxy_bytes.up or 0), int(proxy_bytes.down or 0)


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
):
    return await connect_upstream_ws(
        url,
        headers=headers,
        connector=connector,
        proxy_bytes=proxy_bytes,
        open_timeout=open_timeout,
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
    req = await ch.build_upstream_request(body, resolved_model, ingress_protocol="responses")
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
            outcome = "candidate_guard" if getattr(exc, "scope", "request") == "candidate" else "guard_error"
            return AttemptResult(
                outcome=outcome,
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
    step = await read_until_first_responses_ws_visible_event(
        upstream_ws,
        tracker,
        channel_key=ch.key,
        deadline_ts=deadline_ts,
        first_wait=first_wait,
        idle_timeout=idle_timeout,
        proxy_bytes=proxy_bytes,
        start_time=start_time,
        parse_wrapped_errors=False,
        timeout_detail_mode="packet_or_visible",
        timeout_label_seconds=first_wait,
        use_tracker_error_detail=False,
    )
    if step.outcome is None or step.ok:
        return step.pending, None, step.first_packet_ms
    return step.pending, AttemptResult(
        outcome=step.outcome,
        error_detail=step.error_detail,
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
        step = await read_next_responses_ws_step(
            upstream_ws,
            tracker,
            channel_key=ch.key,
            deadline_ts=deadline_ts,
            idle_timeout=idle_timeout,
            proxy_bytes=proxy_bytes,
            closed_error_detail="upstream websocket closed",
            check_blacklist=False,
        )
        if step.outcome == "total_timeout":
            return AttemptResult(outcome="total_timeout", error_detail=step.error_detail, connect_ms=connect_ms, first_byte_ms=first_byte_ms)
        if step.outcome == "idle_timeout":
            return AttemptResult(outcome="idle_timeout", error_detail=step.error_detail, connect_ms=connect_ms, first_byte_ms=first_byte_ms)
        if step.outcome == "upstream_closed":
            break
        if step.outcome == "stream_upstream_error":
            err = AttemptResult(outcome="stream_upstream_error", error_detail=step.error_detail or "upstream stream error", connect_ms=connect_ms, first_byte_ms=first_byte_ms, http_status=503)
            await _finalize_oauth_ws_error(err, ch, resolved_model, request_id, retry_count_so_far, affinity_hit, start_time, connect_ms, first_byte_ms, tracker, proxy_name, proxy_bytes, identity_state)
            err.proxy_name = proxy_name
            err.proxy_bytes_up = proxy_bytes.up
            err.proxy_bytes_down = proxy_bytes.down
            return err
        if step.outcome == "request_invalid":
            err = AttemptResult(
                outcome="request_invalid",
                error_detail=step.error_detail or protocol_errors.responses_max_output_context_error_message(),
                connect_ms=connect_ms,
                first_byte_ms=first_byte_ms,
                http_status=400,
            )
            await _finalize_oauth_ws_error(err, ch, resolved_model, request_id, retry_count_so_far, affinity_hit, start_time, connect_ms, first_byte_ms, tracker, proxy_name, proxy_bytes, identity_state)
            err.proxy_name = proxy_name
            err.proxy_bytes_up = proxy_bytes.up
            err.proxy_bytes_down = proxy_bytes.down
            return err
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
    total_ms = int((time.time() - start_time) * 1000)
    finalize_policy.apply_success_health_effects(
        finalize_policy.success_plan(),
        scorer=scorer,
        cooldown=cooldown,
        channel_key=ch.key,
        model=resolved_model,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        total_ms=total_ms,
    )
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
    result = _request_invalid_result_if_needed(result)
    plan = finalize_policy.error_plan(result.outcome, failure_policy="cooldown_only")
    finalize_policy.apply_error_health_effects(
        plan,
        scorer=scorer,
        cooldown=cooldown,
        channel_key=ch.key,
        model=resolved_model,
        error_detail=result.error_detail,
        connect_ms=connect_ms,
    )
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
        finalize_policy.apply_success_health_effects(
            finalize_policy.success_plan(),
            scorer=scorer,
            cooldown=cooldown,
            channel_key=ch.key,
            model=resolved_model,
            connect_ms=connect_ms,
            first_byte_ms=first_byte_ms,
            total_ms=total_ms,
        )
        _write_affinity_non_stream(
            "responses", api_key_name, client_ip, messages,
            {"role": "assistant", "content": tracker.get_output_items()},
            body, tracker.to_full_json(fallback_model=resolved_model), ch.key, resolved_model,
            client_key=client_key,
        )
        # encrypted_content 透明透传：上游产出的 reasoning 只返回给下游，
        # Parrot 不做本地持久化或后续回填。
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
        # encrypted_content 只做透明透传；无本地 cache 需要清理。
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
                    is_context_error = (
                        tracker.stream_error_code == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
                    )
                    msg = tracker.stream_error_message or (
                        protocol_errors.responses_max_output_context_error_message()
                        if is_context_error else "upstream stream error"
                    )
                    await finalize_error(AttemptResult(
                        outcome="request_invalid" if is_context_error else "stream_upstream_error",
                        error_detail=msg,
                        http_status=400 if is_context_error else 503,
                    ))
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
                )
                if step.outcome == "total_timeout":
                    err = AttemptResult(outcome="total_timeout", error_detail=step.error_detail, http_status=504)
                    await finalize_error(err)
                    yield _sse_error_for_ingress("responses", errors.ErrType.TIMEOUT, "upstream total timeout")
                    return
                if step.outcome == "idle_timeout":
                    err = AttemptResult(outcome="idle_timeout", error_detail=step.error_detail, http_status=504)
                    await finalize_error(err)
                    yield _sse_error_for_ingress("responses", errors.ErrType.TIMEOUT, err.error_detail or "upstream idle timeout")
                    return
                if step.outcome == "upstream_closed":
                    err = AttemptResult(outcome="upstream_closed", error_detail=step.error_detail, http_status=502)
                    await finalize_error(err)
                    yield _sse_error_for_ingress("responses", errors.ErrType.API, err.error_detail or "upstream websocket closed")
                    return
                if step.outcome == "blacklist_hit":
                    err = AttemptResult(outcome="blacklist_hit", error_detail=step.error_detail, http_status=503)
                    await finalize_error(err)
                    yield _sse_error_for_ingress("responses", errors.ErrType.API, err.error_detail or "blacklist")
                    return
                if step.outcome == "request_invalid":
                    err = AttemptResult(
                        outcome="request_invalid",
                        error_detail=step.error_detail or protocol_errors.responses_max_output_context_error_message(),
                        http_status=400,
                    )
                    await finalize_error(err)
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
                    await finalize_error(AttemptResult(
                        outcome="stream_upstream_error",
                        error_detail=step.error_detail or "upstream stream error",
                        http_status=503,
                    ))
                    return
                if step.outcome == "success":
                    await finalize_success()
                    return
                if step.skip_downstream:
                    continue
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

    opened = await open_response_with_proxy_chain(
        channel=ch,
        resolved_model=resolved_model,
        upstream_req=upstream_req,
        deadline_ts=deadline_ts,
        connect_timeout=connect_timeout,
        request_id=request_id,
        retry_attempt_id=retry_attempt_id,
    )
    if opened.error is not None:
        return opened.error

    ctx = opened.ctx
    upstream_resp = opened.response
    connect_ms = opened.connect_ms
    _proxy_name_used = opened.proxy_name
    _proxy_bytes = opened.proxy_bytes
    _proxy_client = opened.proxy_client

    try:
        # 1.5 响应头 snapshot 采样：成功/失败分支前都先记一次
        _maybe_record_codex_snapshot(ch, upstream_resp)
        _maybe_record_anthropic_snapshot(ch, upstream_resp)

        # 2. HTTP 状态码检查
        if upstream_resp.status_code >= 400:
            result = await read_http_error_response(
                ctx,
                upstream_resp,
                deadline_ts=deadline_ts,
                connect_ms=connect_ms,
                proxy_name=_proxy_name_used,
                proxy_bytes=_proxy_bytes,
                translator_ctx=upstream_req.translator_ctx,
            )
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
    await close_response_context(ctx)


async def _close_proxy_client(client) -> None:
    await close_proxy_client(client)


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
    body_read = await read_non_stream_body(
        ctx,
        upstream_resp,
        deadline_ts=deadline_ts,
        connect_ms=connect_ms,
    )
    if body_read.error is not None:
        return body_read.error
    raw = body_read.raw or b""
    resp_headers = body_read.response_headers

    total_ms = int((time.time() - start_time) * 1000)
    prepared = await prepare_non_stream_response(
        ch,
        raw,
        dynamic_map=dynamic_map,
        connect_ms=connect_ms,
        total_ms=total_ms,
        translator_ctx=translator_ctx,
    )
    if prepared.error is not None:
        return prepared.error
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
        channel_key=ch.key,
        model=resolved_model,
        connect_ms=connect_ms,
        first_byte_ms=None,
        total_ms=total_ms,
    )

    # 落库（用**上游原始响应体**，方便排错；翻译后的下游响应体由 JSONResponse 现场构造）
    await asyncio.to_thread(
        log_db.finish_success, request_id, ch.key, ch.type, resolved_model,
        input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
        cache_creation_tokens=usage["cache_creation"], cache_read_tokens=usage["cache_read"],
        connect_ms=connect_ms, first_token_ms=None, total_ms=total_ms,
        retry_count=retry_count_so_far, affinity_hit=affinity_hit,
        response_body=restored_text,
        http_status=upstream_resp.status_code,
        upstream_protocol=getattr(ch, "protocol", "anthropic"),
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
    )

    # 跨变体：把上游 JSON 反向成 ingress 期望的格式；同协议 translator_ctx=None 即原样
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
        full_response_text=restored_text,
        proxy_name=proxy_name,
        proxy_bytes_up=_proxy_byte_snapshot(proxy_bytes)[0],
        proxy_bytes_down=_proxy_byte_snapshot(proxy_bytes)[1],
        translator_ctx=translator_ctx,
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

    prepared = await aggregate_stream_as_non_stream_response(
        ctx,
        upstream_resp,
        ch,
        resolved_model,
        dynamic_map=dynamic_map,
        connect_ms=connect_ms,
        start_time=start_time,
        deadline_ts=deadline_ts,
        total_timeout=total_timeout,
        first_byte_timeout=first_byte_timeout,
        idle_timeout=idle_timeout,
        translator_ctx=translator_ctx,
    )
    if prepared.error is not None:
        return prepared.error

    obj = prepared.obj or {}
    resp_headers = prepared.response_headers
    usage = prepared.usage
    assistant_msg = prepared.assistant_msg
    first_byte_ms = prepared.first_byte_ms
    total_ms = int(prepared.total_ms or int((time.time() - start_time) * 1000))
    response_body_text = prepared.response_body_text

    finalize_policy.apply_success_health_effects(
        finalize_policy.success_plan(),
        scorer=scorer,
        cooldown=cooldown,
        channel_key=ch.key,
        model=resolved_model,
        connect_ms=connect_ms,
        first_byte_ms=first_byte_ms,
        total_ms=total_ms,
    )

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
        translator_ctx=translator_ctx,
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
        translator_ctx=translator_ctx,
    )
    if stream_start.error is not None:
        return stream_start.error

    aiter = stream_start.aiter
    tracker = stream_start.tracker
    builder = stream_start.builder
    stream_translator = stream_start.stream_translator
    first_downstream_chunks = stream_start.first_downstream_chunks
    first_byte_ms = int(stream_start.first_byte_ms or connect_ms)
    resp_headers = stream_start.response_headers
    upstream_status = int(stream_start.upstream_status or upstream_resp.status_code)
    ch_proto = getattr(ch, "protocol", "anthropic")

    # 3. 通过检查 → 开始向下游发 ★
    state: dict = {"total_ms": None, "finalized": False}

    async def _finalize_success():
        if state["finalized"]:
            return
        state["finalized"] = True
        total_ms = int((time.time() - start_time) * 1000)

        finalize_policy.apply_success_health_effects(
            finalize_policy.success_plan(
                cache_reasoning_replay=(
                    getattr(ch, "protocol", "anthropic") == "openai-responses"
                    and hasattr(builder, "to_full_json")
                )
            ),
            scorer=scorer,
            cooldown=cooldown,
            channel_key=ch.key,
            model=resolved_model,
            connect_ms=connect_ms,
            first_byte_ms=first_byte_ms,
            total_ms=total_ms,
        )

        if getattr(ch, "protocol", "anthropic") == "openai-responses" and hasattr(builder, "to_full_json"):
            try:
                native_response_obj = builder.to_full_json(fallback_model=resolved_model)
                _maybe_cache_codex_reasoning_replay(translator_ctx, native_response_obj)
                if ingress_protocol == "responses":
                    _maybe_save_native_responses_store(
                        native_response_obj,
                        body=body,
                        api_key_name=api_key_name,
                        channel_key=ch.key,
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

        # 已发首包的普通上游错误视为本次渠道失败；但上下文/请求级错误
        # 不是渠道健康问题，即使在流中途才被上游明确揭示，也按 runtime
        # request_invalid 语义处理，避免误伤渠道评分/冷却。
        failure_policy = "runtime" if outcome == "request_invalid" else "post_commit_stream"
        plan = finalize_policy.error_plan(outcome, failure_policy=failure_policy)
        finalize_policy.apply_error_health_effects(
            plan,
            scorer=scorer,
            cooldown=cooldown,
            channel_key=ch.key,
            model=resolved_model,
            error_detail=message,
            connect_ms=connect_ms,
        )

        await asyncio.shield(asyncio.to_thread(
            log_db.finish_error,
            request_id, message, retry_count_so_far,
            final_channel_key=ch.key, final_channel_type=ch.type, final_model=resolved_model,
            connect_ms=connect_ms, first_token_ms=first_byte_ms, total_ms=total_ms,
            http_status=(400 if outcome == "request_invalid" else upstream_status),
            affinity_hit=affinity_hit,
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
            if (
                getattr(tracker, "saw_stream_error", False)
                and getattr(tracker, "stream_error_code", None) == protocol_errors.CONTEXT_LENGTH_EXCEEDED_CODE
            ):
                msg = (
                    getattr(tracker, "stream_error_message", None)
                    or protocol_errors.responses_max_output_context_error_message()
                )
                await _emit_error_and_finalize(
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
                    translator_ctx=translator_ctx,
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
                    await _emit_error_and_finalize(
                        err_type,
                        msg,
                        outcome=step.outcome or "transport_error",
                    )
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
                    await _emit_error_and_finalize(
                        errors.ErrType.API,
                        msg,
                        outcome="blacklist_hit",
                    )
                    yield _sse_error_for_ingress(ingress_protocol, errors.ErrType.API, msg)
                    return

                for out in step.downstream_chunks:
                    yield out

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

            # 上游已正常收尾 → 先让 translator 生成终态帧/完成内部副作用，
            # 再落库 success，最后 yield 终态帧。这样 close()/Store 阶段若异常，
            # 不会先把日志标成成功；而 success 已落库后客户端在终态帧期间断开，
            # state["finalized"] 也会避免误标成 client disconnected。
            terminal_chunks: list[bytes] = []
            if stream_translator is not None:
                terminal_chunks = list(stream_translator.close())
            await _finalize_success()
            for out in terminal_chunks:
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
