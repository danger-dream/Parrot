"""API 渠道探测。

两个用途：
  1. TG Bot "测试模型" 面板：验证渠道是否可用（单次调用，带进度更新）
  2. 后台 cooldown_probe_loop：对处于 cooldown 的 (ch, model) 做探测，成功即解除

⚠ OAuth 渠道**不做探测**（避免消耗配额 + 触发风控）。
OAuth 的恢复由 `quota_monitor_once` 负责（通过用量判断 < 95% 后解禁）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Awaitable, Callable, Optional

import httpx

from . import config, cooldown
from .channel import registry
from .channel.api_channel import ApiChannel
from .channel.base import Channel


ProgressCallback = Callable[[str], Awaitable[None]]


# ─── 单次 probe ──────────────────────────────────────────────────

def _probe_payload_for(ch: Channel, *, max_tokens: int, user_message: str) -> tuple[dict, str]:
    """按 ch.protocol 构造探测 body + 推导 ingress_protocol。

    返回的 body 直接喂给 ch.build_upstream_request(body, model, ingress_protocol=...)，
    同协议透传路径会把它原样（白名单过滤后）发给上游。
    """
    proto = getattr(ch, "protocol", "anthropic")
    if proto == "openai-responses":
        body = {
            "model": "",
            "input": user_message,
            "max_output_tokens": max_tokens,
            "stream": False,
        }
        return body, "responses"
    if proto == "openai-chat":
        body = {
            "model": "",
            "messages": [{"role": "user", "content": user_message}],
            "max_tokens": max_tokens,
            "temperature": 0,
            "stream": False,
        }
        return body, "chat"
    # anthropic（默认）
    body = {
        "model": "",
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "user", "content": user_message}],
    }
    return body, "anthropic"


async def probe_channel_model(
    ch: Channel, model: str, timeout_s: Optional[float] = None,
) -> tuple[bool, int, Optional[str]]:
    """对 (channel, model) 做一次探测请求。

    返回 (ok, elapsed_ms, reason)。reason 仅在失败时有值。
    - 仅支持 ApiChannel（含 OpenAIApiChannel）；OAuthChannel 直接返回
      (False, 0, "oauth not probable")
    - 按 ch.protocol 自动构造 anthropic / openai-chat / openai-responses 三种 payload
    - 超时由 timeout_s 参数或 config.probe.timeoutSeconds 决定
    """
    if ch.type != "api":
        return False, 0, "oauth not probable"

    cfg = config.get()
    probe_cfg = cfg.get("probe") or {}
    timeout = float(timeout_s if timeout_s is not None else probe_cfg.get("timeoutSeconds", 60))
    max_tokens = int(probe_cfg.get("maxTokens", 50))
    user_message = str(probe_cfg.get("userMessage", "1+1=?"))

    body, ingress = _probe_payload_for(ch, max_tokens=max_tokens, user_message=user_message)
    body["model"] = model  # 会由 build_upstream_request 替换为 resolved_model

    t0 = time.time()
    try:
        upstream_req = await ch.build_upstream_request(body, model, ingress_protocol=ingress)
    except Exception as exc:
        return False, 0, f"transform error: {exc}"

    # 用临时 client（独立于服务端主 client 的连接池；避免主池长连接影响 probe 结果）
    # 关键：httpx.Timeout(timeout) 只保证每个阶段（connect/read/write）单独不超过 timeout，
    # 不是总时长。用 asyncio.wait_for 再加一层总时长硬性限制，确保真的不超过 timeout 秒。
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        try:
            resp = await asyncio.wait_for(
                client.post(
                    upstream_req.url,
                    headers=upstream_req.headers,
                    content=upstream_req.body,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return False, int((time.time() - t0) * 1000), f"timeout > {timeout}s"
        except httpx.ConnectTimeout:
            return False, int((time.time() - t0) * 1000), f"connect timeout > {timeout}s"
        except httpx.ConnectError as exc:
            return False, int((time.time() - t0) * 1000), f"connect error: {exc}"
        except httpx.TimeoutException as exc:
            return False, int((time.time() - t0) * 1000), f"timeout: {exc}"
        except Exception as exc:
            return False, int((time.time() - t0) * 1000), f"transport: {exc}"

        elapsed_ms = int((time.time() - t0) * 1000)

        if resp.status_code != 200:
            return False, elapsed_ms, f"HTTP {resp.status_code}: {resp.text[:200]}"

        try:
            obj = resp.json()
        except Exception:
            return False, elapsed_ms, f"non-JSON response: {resp.text[:200]}"

        if isinstance(obj, dict) and (
            obj.get("type") == "error" or isinstance(obj.get("error"), dict)
        ):
            return False, elapsed_ms, f"upstream error: {json.dumps(obj.get('error', obj))[:200]}"

        return True, elapsed_ms, None


# ─── 带进度的 probe（TG Bot 测试面板用） ──────────────────────────

async def probe_with_progress(
    ch: Channel, model: str,
    progress_cb: Optional[ProgressCallback] = None,
    timeout_s: Optional[float] = None,
    progress_interval: int = 10,
) -> tuple[bool, int, Optional[str]]:
    """同 probe_channel_model，但会周期性回调 progress_cb 报告"调用时长 > Xs..."。

    每 progress_interval 秒调用一次 progress_cb（若非 None）。
    """
    t0 = time.time()

    async def _ticker():
        """每 interval 秒报一次进度。"""
        sec = progress_interval
        while True:
            await asyncio.sleep(progress_interval)
            if progress_cb is not None:
                try:
                    await progress_cb(f"调用时长超过 {sec}s...")
                except Exception as exc:
                    print(f"[probe] progress_cb failed: {exc}")
            sec += progress_interval

    ticker_task = asyncio.create_task(_ticker()) if progress_cb is not None else None
    try:
        ok, elapsed, reason = await probe_channel_model(ch, model, timeout_s=timeout_s)
        return ok, elapsed, reason
    finally:
        if ticker_task is not None:
            ticker_task.cancel()
            try:
                await ticker_task
            except (asyncio.CancelledError, Exception):
                pass


# ─── 后台 cooldown 恢复 ───────────────────────────────────────────

async def recovery_run_once() -> int:
    """遍历所有冷却中的 (ch, model)，对 API 渠道做 probe，成功则清除。

    返回清除的条数。
    """
    cfg = config.get()
    recovery_cfg = cfg.get("cooldownRecovery") or {}
    if not recovery_cfg.get("enabled", True):
        return 0
    timeout_s = float(recovery_cfg.get("timeoutSeconds", 15))

    cleared = 0
    for entry in cooldown.active_entries():
        ch = registry.get_channel(entry["channel_key"])
        if ch is None or ch.type != "api" or not ch.enabled:
            continue
        ok, elapsed_ms, reason = await probe_channel_model(ch, entry["model"], timeout_s=timeout_s)
        if ok:
            cooldown.clear(ch.key, entry["model"])
            cleared += 1
            print(f"[probe] cleared cooldown for {ch.key}:{entry['model']} ({elapsed_ms}ms)")
        else:
            # 探测失败不额外记 cooldown（本身就在 cooldown 中），只打日志
            print(f"[probe] still failing {ch.key}:{entry['model']} — {reason}")
    return cleared


async def recovery_loop() -> None:
    """后台任务：每 intervalSeconds 触发一次 run_once。"""
    while True:
        try:
            cfg = config.get()
            recovery_cfg = cfg.get("cooldownRecovery") or {}
            if not recovery_cfg.get("enabled", True):
                await asyncio.sleep(30)
                continue
            interval = int(recovery_cfg.get("intervalSeconds", 30))
        except Exception:
            interval = 30
        await asyncio.sleep(interval)
        try:
            await recovery_run_once()
        except Exception as exc:
            print(f"[probe] recovery_run_once error: {exc}")
