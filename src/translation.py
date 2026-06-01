"""请求翻译层。

将用户输入翻译为指定语言后再发给上游模型。
默认关闭；用户在 TG Bot「系统设置」→「翻译层」启用并选择翻译模型。

设计约束：
  - 翻译失败一律静默回退原文，不阻断主请求
  - 翻译调用绕过 HTTP 入口，直接走 ch.build_upstream_request + httpx（参考 probe.py）
  - 不经过 auth / scheduler / log_db / failover，不会递归
  - 缓存：sqlite 持久化 + 有 TTL/容量上限的内存热层
  - 默认只翻译 user 输入；system/developer/instructions 可通过开关启用
  - assistant / tool / function 消息不翻译
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
import traceback
from collections import OrderedDict
from typing import Any, Optional

import httpx

from . import config, network
from .channel import registry
from .channel.base import Channel


# ─── 默认翻译提示词 ──────────────────────────────────────────────

DEFAULT_TRANSLATION_PROMPT = """\
Translate the following text to {target_language}.

Rules:
- Preserve all code blocks, inline code, JSON, XML, HTML, URLs, file paths, and technical terms exactly as-is
- Preserve all markdown formatting
- Preserve all variable names, function names, class names as-is
- Do not add explanations, notes, or commentary
- Do not wrap the output in code blocks or quotes
- Output ONLY the translated text"""


# ─── 默认配置 ─────────────────────────────────────────────────────

DEFAULT_TRANSLATION_CONFIG: dict[str, Any] = {
    "enabled": False,
    "model": "",
    "fallbackModel": "",
    "targetLanguage": "English",
    "prompt": "",
    "timeoutSeconds": 10,
    "maxHistoryMessages": 20,
    "cacheTtlDays": 3,
    "cachePreloadCount": 100,
    "failureAlertThreshold": 10,
    # 内存热层：仅影响内存缓存，不删除 sqlite 持久缓存。
    "memoryCacheMaxMb": 100,
    "memoryCacheTtlSeconds": 7200,
    # 默认不翻译 system/developer/instructions，避免改写高优先级提示词。
    "translateSystemMessages": False,
}

_TEXT_BLOCK_TYPES = ("text", "input_text")
_MEM_ENTRY_OVERHEAD_BYTES = 256


# ─── 缓存层（sqlite + 内存热层） ─────────────────────────────────

_db: Optional[sqlite3.Connection] = None
_db_lock = threading.Lock()
# cache_key → {"translated": str, "cached_at": float, "size": int}
_mem_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
_mem_cache_bytes = 0
_mem_lock = threading.Lock()
_cache_stats = {"hits": 0, "misses": 0}

# 连续失败计数器（用于告警）
_consecutive_failures = 0
_failure_lock = threading.Lock()


def _get_cfg() -> dict:
    """读取翻译配置，缺失字段用默认值补齐。"""
    cfg = config.get()
    raw = cfg.get("translation") or {}
    out = dict(DEFAULT_TRANSLATION_CONFIG)
    out.update({k: v for k, v in raw.items() if k in DEFAULT_TRANSLATION_CONFIG})
    return out


def _as_int(value: Any, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    if lo is not None:
        out = max(lo, out)
    if hi is not None:
        out = min(hi, out)
    return out


def _effective_system_prompt(cfg: dict, target_lang: str) -> str:
    template = str(cfg.get("prompt") or DEFAULT_TRANSLATION_PROMPT)
    return template.replace("{target_language}", target_lang)


def _model_signature(cfg: dict) -> str:
    # 让用户切换翻译模型/备用模型后不会继续命中旧模型产物。
    return "|".join([
        str(cfg.get("model") or ""),
        str(cfg.get("fallbackModel") or ""),
    ])


def _configured_models(cfg: dict) -> list[str]:
    models: list[str] = []
    for key in ("model", "fallbackModel"):
        value = str(cfg.get(key) or "").strip()
        if value and value not in models:
            models.append(value)
    return models


def validate_ready(cfg: Optional[dict] = None, *, require_enabled: bool = True) -> tuple[bool, str]:
    """检查翻译层是否满足启用条件。返回 (ok, reason)。"""
    cfg = cfg or _get_cfg()
    if require_enabled and not cfg.get("enabled"):
        return False, "翻译层未启用"

    models = _configured_models(cfg)
    if not models:
        return False, "未设置翻译模型"

    target_lang = str(cfg.get("targetLanguage") or "English")
    if not _effective_system_prompt(cfg, target_lang).strip():
        return False, "翻译提示词为空"

    for model in models:
        if _find_channel_for_model(model) is not None:
            return True, ""
    return False, f"模型所在渠道/账号不可用: {models[0]}"


def _db_path() -> str:
    from .config import DATA_DIR
    return os.path.join(DATA_DIR, "translation_cache.db")


def init() -> None:
    """初始化 sqlite 缓存库 + 预加载热层。由 server.py lifespan 调用。"""
    global _db
    with _db_lock:
        if _db is not None:
            return
        path = _db_path()
        _db = sqlite3.connect(path, check_same_thread=False)
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")
        _db.execute("""
            CREATE TABLE IF NOT EXISTS translation_cache (
                cache_key  TEXT PRIMARY KEY,
                translated TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        _db.commit()

    preload_count = _as_int(_get_cfg().get("cachePreloadCount"), 100, lo=0)
    _preload(preload_count)


def _mem_entry_size(key: str, translated: str) -> int:
    return (
        len(str(key).encode("utf-8", errors="ignore"))
        + len(str(translated).encode("utf-8", errors="ignore"))
        + _MEM_ENTRY_OVERHEAD_BYTES
    )


def _mem_limits(cfg: Optional[dict] = None) -> tuple[int, int]:
    cfg = cfg or _get_cfg()
    max_mb = _as_int(cfg.get("memoryCacheMaxMb"), 100, lo=0)
    ttl_s = _as_int(cfg.get("memoryCacheTtlSeconds"), 7200, lo=0)
    return max_mb * 1024 * 1024, ttl_s


def _mem_remove_locked(key: str) -> None:
    global _mem_cache_bytes
    item = _mem_cache.pop(key, None)
    if item:
        _mem_cache_bytes = max(0, _mem_cache_bytes - int(item.get("size") or 0))


def _mem_prune_locked(now: Optional[float] = None, *, max_bytes: Optional[int] = None,
                       ttl_s: Optional[int] = None) -> None:
    """在持有 _mem_lock 时清理过期/超限内存缓存。"""
    global _mem_cache_bytes
    now = time.time() if now is None else now
    if max_bytes is None or ttl_s is None:
        max_bytes, ttl_s = _mem_limits()

    if ttl_s > 0:
        expired = [
            key for key, item in _mem_cache.items()
            if now - float(item.get("cached_at") or 0) > ttl_s
        ]
        for key in expired:
            _mem_remove_locked(key)

    if max_bytes <= 0:
        _mem_cache.clear()
        _mem_cache_bytes = 0
        return

    while _mem_cache_bytes > max_bytes and _mem_cache:
        oldest_key = next(iter(_mem_cache))
        _mem_remove_locked(oldest_key)


def _mem_put(key: str, translated: str, *, cfg: Optional[dict] = None) -> None:
    global _mem_cache_bytes
    cfg = cfg or _get_cfg()
    max_bytes, ttl_s = _mem_limits(cfg)
    if max_bytes <= 0:
        return

    size = _mem_entry_size(key, translated)
    # 单条超过上限时不放入内存，避免反复插入/逐出抖动。
    if size > max_bytes:
        return

    now = time.time()
    with _mem_lock:
        _mem_remove_locked(key)
        _mem_cache[key] = {"translated": translated, "cached_at": now, "size": size}
        _mem_cache.move_to_end(key)
        _mem_cache_bytes += size
        _mem_prune_locked(now, max_bytes=max_bytes, ttl_s=ttl_s)


def _preload(count: int) -> None:
    """从 sqlite 加载最近 count 条到内存热层。"""
    if _db is None or count <= 0:
        return
    with _db_lock:
        rows = _db.execute(
            "SELECT cache_key, translated FROM translation_cache "
            "ORDER BY created_at DESC LIMIT ?",
            (count,),
        ).fetchall()
    cfg = _get_cfg()
    for key, translated in reversed(rows):  # 最新的放后面（OrderedDict 末尾）
        _mem_put(key, translated, cfg=cfg)


def _make_cache_key(
    target_language: str,
    original_text: str,
    system_prompt: Optional[str] = None,
    model_signature: str = "",
) -> str:
    """生成缓存 key。

    key 绑定目标语言、有效提示词 hash、翻译模型签名和原文，避免修改提示词/模型后
    继续命中旧翻译。
    """
    if system_prompt is None:
        system_prompt = DEFAULT_TRANSLATION_PROMPT.replace("{target_language}", target_language)
    payload = {
        "v": 2,
        "target_language": target_language,
        "prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "model_signature": model_signature,
        "text": original_text,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_key_for_text(target_language: str, original_text: str, cfg: dict) -> str:
    return _make_cache_key(
        target_language,
        original_text,
        _effective_system_prompt(cfg, target_language),
        _model_signature(cfg),
    )


def _cache_get(key: str) -> Optional[str]:
    """查缓存：内存 → sqlite。命中 sqlite 则提升到内存。"""
    cfg = _get_cfg()
    max_bytes, ttl_s = _mem_limits(cfg)
    now = time.time()

    # 1. 内存热层：受独立内存 TTL 和容量上限约束。
    with _mem_lock:
        item = _mem_cache.get(key)
        if item is not None:
            if ttl_s <= 0 or now - float(item.get("cached_at") or 0) <= ttl_s:
                _mem_cache.move_to_end(key)
                _cache_stats["hits"] += 1
                return str(item.get("translated") or "")
            _mem_remove_locked(key)
        _mem_prune_locked(now, max_bytes=max_bytes, ttl_s=ttl_s)

    # 2. sqlite：受持久缓存 TTL 约束。
    if _db is None:
        return None
    ttl_days = _as_int(cfg.get("cacheTtlDays"), 3, lo=1)
    cutoff = now - ttl_days * 86400
    with _db_lock:
        row = _db.execute(
            "SELECT translated FROM translation_cache "
            "WHERE cache_key = ? AND created_at > ?",
            (key, cutoff),
        ).fetchone()
    if row is None:
        _cache_stats["misses"] += 1
        return None

    translated = str(row[0])
    _mem_put(key, translated, cfg=cfg)
    _cache_stats["hits"] += 1
    return translated


def _cache_put(key: str, translated: str) -> None:
    """写入 sqlite + 内存。"""
    now = time.time()
    cfg = _get_cfg()
    _mem_put(key, translated, cfg=cfg)
    if _db is None:
        return
    with _db_lock:
        _db.execute(
            "INSERT OR REPLACE INTO translation_cache (cache_key, translated, created_at) "
            "VALUES (?, ?, ?)",
            (key, translated, now),
        )
        _db.commit()


def cache_count() -> int:
    """当前 sqlite 中的缓存条目数。"""
    if _db is None:
        return 0
    with _db_lock:
        row = _db.execute("SELECT COUNT(*) FROM translation_cache").fetchone()
    return row[0] if row else 0


def cache_hit_stats() -> dict:
    """返回缓存统计。"""
    with _mem_lock:
        mem_entries = len(_mem_cache)
        mem_bytes = _mem_cache_bytes
    out = dict(_cache_stats)
    out.update({"memoryEntries": mem_entries, "memoryBytes": mem_bytes})
    return out


def clear_cache() -> int:
    """清空所有缓存。返回被清除的 sqlite 条目数。"""
    global _mem_cache_bytes
    count = cache_count()
    with _mem_lock:
        _mem_cache.clear()
        _mem_cache_bytes = 0
    if _db is not None:
        with _db_lock:
            _db.execute("DELETE FROM translation_cache")
            _db.commit()
    _cache_stats["hits"] = 0
    _cache_stats["misses"] = 0
    return count


def cleanup_expired() -> int:
    """清理过期 sqlite 条目，并顺手收缩内存热层。返回 sqlite 清理数。"""
    cfg = _get_cfg()
    max_bytes, ttl_s = _mem_limits(cfg)
    with _mem_lock:
        _mem_prune_locked(time.time(), max_bytes=max_bytes, ttl_s=ttl_s)

    if _db is None:
        return 0
    ttl_days = _as_int(cfg.get("cacheTtlDays"), 3, lo=1)
    cutoff = time.time() - ttl_days * 86400
    with _db_lock:
        cursor = _db.execute(
            "DELETE FROM translation_cache WHERE created_at < ?", (cutoff,)
        )
        _db.commit()
        return cursor.rowcount


def checkpoint() -> None:
    """WAL checkpoint，由 server.py 的 _wal_checkpoint_loop 调用。"""
    if _db is None:
        return
    with _db_lock:
        try:
            _db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass


# ─── 翻译调用（内部直调上游，不走 HTTP 入口） ─────────────────────

def _find_channel_for_model(model_name: str) -> Optional[tuple[Channel, str]]:
    """从 registry 中找到能服务指定模型的第一个启用渠道。"""
    if not model_name:
        return None
    for ch in registry.all_channels():
        if not ch.enabled or ch.disabled_reason:
            continue
        resolved = ch.supports_model(model_name)
        if resolved is not None:
            return ch, resolved
    return None


def _build_translation_body(
    ch: Channel, text: str, system_prompt: str, max_tokens: int,
) -> tuple[dict, str]:
    """按 channel 协议构造翻译请求 body + ingress_protocol。"""
    proto = getattr(ch, "protocol", "anthropic")
    if proto == "openai-responses":
        return {
            "model": "",
            "stream": False,
            "max_output_tokens": max_tokens,
            "instructions": system_prompt,
            "input": text,
        }, "responses"
    if proto == "openai-chat":
        return {
            "model": "",
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        }, "chat"
    # anthropic（默认）
    return {
        "model": "",
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0,
        "system": system_prompt,
        "messages": [{"role": "user", "content": text}],
    }, "anthropic"


def _extract_text_from_response(data: dict, protocol: str) -> Optional[str]:
    """从上游响应 JSON 中提取翻译文本。"""
    if not isinstance(data, dict):
        return None

    if data.get("type") == "error" or isinstance(data.get("error"), dict):
        return None

    if protocol == "anthropic":
        content = data.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "".join(parts) if parts else None
        return None

    if protocol == "openai-chat":
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            msg = (choices[0] or {}).get("message") or {}
            content = msg.get("content")
            return content if isinstance(content, str) else None
        return None

    if protocol == "openai-responses":
        output = data.get("output")
        if isinstance(output, list):
            parts = []
            for item in output:
                if isinstance(item, dict) and item.get("type") == "message":
                    for c in (item.get("content") or []):
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(c["text"])
            return "".join(parts) if parts else None
        return None

    return None


def _iter_sse_event_objects(raw: bytes) -> list[tuple[Optional[str], Optional[dict]]]:
    """解析完整 SSE 字节串，返回 [(event_name, data_obj)]。"""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    out: list[tuple[Optional[str], Optional[dict]]] = []
    text = text.replace("\r\n", "\n")
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event_name: Optional[str] = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                event_name = line[6:].strip() or None
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            out.append((event_name, None))
            continue
        data_str = "\n".join(data_lines).strip()
        if not data_str or data_str == "[DONE]":
            out.append((event_name, None))
            continue
        try:
            out.append((event_name, json.loads(data_str)))
        except Exception:
            out.append((event_name, None))
    return out


def _looks_like_sse(raw: bytes, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if "text/event-stream" in ct:
        return True
    prefix = raw.lstrip()[:32]
    return prefix.startswith(b"event:") or prefix.startswith(b"data:")


def _extract_text_from_sse(raw: bytes, protocol: str, resolved_model: str) -> Optional[str]:
    """从 OpenAI/Responses SSE 流中提取最终文本，供 Codex/OAuth 翻译模型使用。"""
    events = _iter_sse_event_objects(raw)
    if not events:
        return None

    if protocol == "openai-chat":
        parts: list[str] = []
        final_obj: Optional[dict] = None
        for _event, data in events:
            if not isinstance(data, dict):
                continue
            if isinstance(data.get("error"), dict):
                return None
            # 有些兼容服务可能直接把完整 chat.completion JSON 放在 SSE 里。
            if isinstance(data.get("choices"), list):
                choices = data.get("choices") or []
                if choices:
                    msg = (choices[0] or {}).get("message") or {}
                    if isinstance(msg.get("content"), str):
                        final_obj = data
                    delta = (choices[0] or {}).get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        parts.append(delta["content"])
        if final_obj is not None:
            return _extract_text_from_response(final_obj, "openai-chat")
        return "".join(parts) if parts else None

    if protocol == "openai-responses":
        deltas: list[str] = []
        done_texts: list[str] = []
        completed_text: Optional[str] = None
        for event_name, data in events:
            if not isinstance(data, dict):
                continue
            if data.get("type") == "error" or isinstance(data.get("error"), dict):
                return None
            typ = str(data.get("type") or event_name or "")
            if typ == "response.output_text.delta" and isinstance(data.get("delta"), str):
                deltas.append(data["delta"])
            elif typ == "response.output_text.done" and isinstance(data.get("text"), str):
                done_texts.append(data["text"])
            elif typ == "response.completed":
                resp = data.get("response") if isinstance(data.get("response"), dict) else data
                text = _extract_text_from_response(resp, "openai-responses")
                if text:
                    completed_text = text
        if completed_text:
            return completed_text
        if done_texts:
            return "".join(done_texts)
        return "".join(deltas) if deltas else None

    # Anthropic SSE 兜底（正常翻译请求不会主动启用）。
    if protocol == "anthropic":
        parts: list[str] = []
        for event_name, data in events:
            if not isinstance(data, dict):
                continue
            if data.get("type") == "error" or isinstance(data.get("error"), dict):
                return None
            if data.get("type") == "content_block_delta":
                delta = data.get("delta") or {}
                if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                    parts.append(delta["text"])
        return "".join(parts) if parts else None

    return None


async def _call_model(
    model_name: str, text: str, system_prompt: str, timeout_s: float,
) -> Optional[str]:
    """用指定模型翻译 text。成功返回翻译文本，失败返回 None。"""
    found = _find_channel_for_model(model_name)
    if found is None:
        print(f"[translation] no channel for model {model_name}")
        return None

    ch, resolved_model = found
    proto = getattr(ch, "protocol", "anthropic")

    estimated_tokens = max(1024, min(len(text) * 2, 16384))
    body, ingress = _build_translation_body(ch, text, system_prompt, estimated_tokens)
    body["model"] = resolved_model

    try:
        upstream_req = await ch.build_upstream_request(
            body, resolved_model, ingress_protocol=ingress,
        )
    except Exception as exc:
        print(f"[translation] build_upstream_request failed: {exc}")
        return None

    try:
        async with network.async_client(
            timeout=httpx.Timeout(timeout_s),
            proxy_purpose="translation",
            proxy_channel=ch.key,
            proxy_model=resolved_model,
        ) as client:
            resp = await asyncio.wait_for(
                client.post(
                    upstream_req.url,
                    headers=upstream_req.headers,
                    content=upstream_req.body,
                ),
                timeout=timeout_s,
            )
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        print(f"[translation] timeout calling {model_name}: {exc}")
        return None
    except Exception as exc:
        print(f"[translation] http error calling {model_name}: {exc}")
        return None

    if resp.status_code != 200:
        print(f"[translation] HTTP {resp.status_code} from {model_name}: {resp.text[:200]}")
        return None

    raw = resp.content or b""
    content_type = resp.headers.get("content-type", "")
    if _looks_like_sse(raw, content_type) or bool(getattr(ch, "upstream_stream_only", False)):
        result = _extract_text_from_sse(raw, proto, resolved_model)
        if result is not None:
            return result
        print(f"[translation] SSE response from {model_name} had no text")
        return None

    try:
        data = resp.json()
    except Exception:
        # 有些上游 content-type 不准但实际吐 SSE，再兜一次。
        result = _extract_text_from_sse(raw, proto, resolved_model)
        if result is not None:
            return result
        print(f"[translation] non-JSON response from {model_name}")
        return None

    return _extract_text_from_response(data, proto)


async def _translate_text(text: str, cfg: dict) -> Optional[str]:
    """翻译单条文本。先用主模型，失败用备用模型。都失败返回 None。"""
    target_lang = cfg.get("targetLanguage") or "English"
    system_prompt = _effective_system_prompt(cfg, target_lang)
    timeout = float(cfg.get("timeoutSeconds", 10))

    model = str(cfg.get("model") or "").strip()
    if model:
        start = time.time()
        result = await _call_model(model, text, system_prompt, timeout)
        if result is not None:
            return result
        elapsed = time.time() - start
        remaining = max(1.0, timeout - elapsed)
    else:
        remaining = timeout

    fallback = str(cfg.get("fallbackModel") or "").strip()
    if fallback:
        result = await _call_model(fallback, text, system_prompt, remaining)
        if result is not None:
            return result

    return None


def _record_success() -> None:
    global _consecutive_failures
    with _failure_lock:
        _consecutive_failures = 0


def _record_failure(cfg: dict) -> None:
    global _consecutive_failures
    with _failure_lock:
        _consecutive_failures += 1
        count = _consecutive_failures
    threshold = int(cfg.get("failureAlertThreshold", 10))
    if threshold > 0 and count == threshold:
        try:
            from . import notifier
            notifier.notify(
                f"⚠ <b>翻译层连续失败 {count} 次</b>\n"
                f"主模型: <code>{cfg.get('model', '?')}</code>\n"
                f"备用模型: <code>{cfg.get('fallbackModel', '?')}</code>\n"
                "请检查翻译模型可用性。"
            )
        except Exception:
            pass


# ─── 消息内容提取与替换 ───────────────────────────────────────────

def _extract_text_content(content: Any) -> Optional[str]:
    """从 message content 中提取纯文本（跳过图片/工具等）。"""
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in _TEXT_BLOCK_TYPES:
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        joined = "".join(parts)
        return joined if joined.strip() else None
    return None


def _collect_content_segments(prefix: str, content: Any) -> list[tuple[str, str]]:
    """收集可翻译文本段。list content 按 text block 单独收集，不跨多模态合并。"""
    if isinstance(content, str):
        return [(prefix, content)] if content.strip() else []
    if isinstance(content, list):
        out: list[tuple[str, str]] = []
        for idx, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") not in _TEXT_BLOCK_TYPES:
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                out.append((f"{prefix}:b:{idx}", text))
        return out
    return []


def _apply_content_translations(content: Any, prefix: str, translated_map: dict[str, str]) -> Any:
    """把翻译结果写回 content；保持多模态 block 顺序与结构。"""
    if isinstance(content, str):
        return translated_map.get(prefix, content)
    if isinstance(content, list):
        new_content = list(content)
        changed = False
        for idx, block in enumerate(content):
            key = f"{prefix}:b:{idx}"
            if key not in translated_map or not isinstance(block, dict):
                continue
            translated = translated_map[key]
            original = block.get("text")
            if isinstance(original, str) and translated != original:
                new_content[idx] = {**block, "text": translated}
                changed = True
        return new_content if changed else content
    return content


def _replace_text_content(content: Any, translated: str) -> Any:
    """兼容旧测试/工具：替换文本内容。list content 只替换首个文本块并保留结构。"""
    if isinstance(content, str):
        return translated
    if isinstance(content, list):
        new_content = list(content)
        for idx, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") in _TEXT_BLOCK_TYPES:
                new_content[idx] = {**block, "text": translated}
                return new_content
        return content
    return translated


# ─── 主入口：按入口协议翻译 body ─────────────────────────────────

async def translate_body(
    body: dict, *, ingress_protocol: str,
) -> dict:
    """翻译层主入口。对 body 中的 user 消息做翻译。

    system/developer/instructions 默认不翻译，可通过 translateSystemMessages 开关启用。
    翻译失败静默回退原文。
    """
    cfg = _get_cfg()
    ok, _reason = validate_ready(cfg, require_enabled=True)
    if not ok:
        return body

    target_lang = cfg.get("targetLanguage") or "English"
    max_history = _as_int(cfg.get("maxHistoryMessages"), 20, lo=1)

    try:
        if ingress_protocol == "anthropic":
            return await _translate_anthropic(body, cfg, target_lang, max_history)
        if ingress_protocol == "chat":
            return await _translate_openai_chat(body, cfg, target_lang, max_history)
        if ingress_protocol == "responses":
            return await _translate_openai_responses(body, cfg, target_lang, max_history)
        return body
    except Exception as exc:
        print(f"[translation] unexpected error: {exc}")
        traceback.print_exc()
        return body


async def _translate_single(text: str, target_lang: str, cfg: dict) -> str:
    """翻译单条文本，带缓存。失败返回原文。"""
    if not text or not text.strip():
        return text

    cache_key = _cache_key_for_text(target_lang, text, cfg)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result = await _translate_text(text, cfg)
    if result is not None:
        _cache_put(cache_key, result)
        _record_success()
        return result

    _record_failure(cfg)
    return text


async def translate_text_for_test(text: str) -> dict[str, Any]:
    """TG 测试入口：翻译单段文本，返回结构化结果，不静默吞掉模型错误。"""
    cfg = _get_cfg()
    ok, reason = validate_ready(cfg, require_enabled=False)
    if not ok:
        return {"ok": False, "reason": reason, "original": text, "translated": text}

    target_lang = cfg.get("targetLanguage") or "English"
    cache_key = _cache_key_for_text(target_lang, text, cfg)
    cached = _cache_get(cache_key)
    if cached is not None:
        return {
            "ok": True, "cached": True, "targetLanguage": target_lang,
            "original": text, "translated": cached,
        }

    result = await _translate_text(text, cfg)
    if result is None:
        _record_failure(cfg)
        return {
            "ok": False,
            "reason": "翻译调用失败（主请求会静默回退原文）",
            "targetLanguage": target_lang,
            "original": text,
            "translated": text,
        }

    _cache_put(cache_key, result)
    _record_success()
    return {
        "ok": True, "cached": False, "targetLanguage": target_lang,
        "original": text, "translated": result,
    }


async def _translate_batch(
    texts: list[tuple[str, str]], target_lang: str, cfg: dict,
) -> dict[str, str]:
    """批量翻译，并行但限制并发。"""
    sem = asyncio.Semaphore(5)

    async def _do(key: str, text: str) -> tuple[str, str]:
        async with sem:
            translated = await _translate_single(text, target_lang, cfg)
            return key, translated

    tasks = [_do(k, t) for k, t in texts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, str] = {}
    for r in results:
        if isinstance(r, tuple):
            out[r[0]] = r[1]
    return out


# ─── Anthropic /v1/messages ───────────────────────────────────────

async def _translate_anthropic(
    body: dict, cfg: dict, target_lang: str, max_history: int,
) -> dict:
    body = dict(body)

    if bool(cfg.get("translateSystemMessages", False)):
        system = body.get("system")
        if system is not None:
            body["system"] = await _translate_system(system, target_lang, cfg)

    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        body["messages"] = await _translate_messages_by_role(
            messages, target_lang, cfg, max_history,
            user_roles={"user"}, system_roles=set(), prefix="anthropic",
        )

    return body


async def _translate_system(system: Any, target_lang: str, cfg: dict) -> Any:
    """翻译 system prompt（string 或 list of text blocks）。"""
    if isinstance(system, str):
        return await _translate_single(system, target_lang, cfg)
    if isinstance(system, list):
        pending: list[tuple[str, str]] = []
        for i, block in enumerate(system):
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str) and block.get("text", "").strip():
                pending.append((f"sys:b:{i}", block["text"]))
        if not pending:
            return system
        translated_map = await _translate_batch(pending, target_lang, cfg)
        new_blocks = list(system)
        for i, block in enumerate(system):
            key = f"sys:b:{i}"
            if key in translated_map and isinstance(block, dict):
                new_blocks[i] = {**block, "text": translated_map[key]}
        return new_blocks
    return system


async def _translate_messages_by_role(
    messages: list,
    target_lang: str,
    cfg: dict,
    max_history: int,
    *,
    user_roles: set[str],
    system_roles: set[str],
    prefix: str,
) -> list:
    """按角色翻译 messages：system 全量、user 最近 max_history 条。"""
    user_indices = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") in user_roles
    ]
    system_indices = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict) and m.get("role") in system_roles
    ]
    to_translate_indices = set(system_indices)
    to_translate_indices.update(user_indices[-max_history:])

    pending: list[tuple[str, str]] = []
    for i in to_translate_indices:
        msg = messages[i]
        pending.extend(_collect_content_segments(f"{prefix}:m:{i}", msg.get("content")))

    if not pending:
        return messages

    translated_map = await _translate_batch(pending, target_lang, cfg)
    new_messages = list(messages)
    for i in to_translate_indices:
        msg = dict(messages[i])
        msg["content"] = _apply_content_translations(
            msg.get("content"), f"{prefix}:m:{i}", translated_map,
        )
        new_messages[i] = msg
    return new_messages


# ─── OpenAI Chat /v1/chat/completions ─────────────────────────────

async def _translate_openai_chat(
    body: dict, cfg: dict, target_lang: str, max_history: int,
) -> dict:
    body = dict(body)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return body

    system_roles = {"system", "developer"} if bool(cfg.get("translateSystemMessages", False)) else set()
    body["messages"] = await _translate_messages_by_role(
        messages, target_lang, cfg, max_history,
        user_roles={"user"}, system_roles=system_roles, prefix="chat",
    )
    return body


# ─── OpenAI Responses /v1/responses ───────────────────────────────

async def _translate_openai_responses(
    body: dict, cfg: dict, target_lang: str, max_history: int,
) -> dict:
    body = dict(body)

    if bool(cfg.get("translateSystemMessages", False)):
        instructions = body.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            body["instructions"] = await _translate_single(instructions, target_lang, cfg)

    inp = body.get("input")
    if isinstance(inp, str) and inp.strip():
        body["input"] = await _translate_single(inp, target_lang, cfg)
    elif isinstance(inp, list) and inp:
        body["input"] = await _translate_responses_input_items(
            inp, target_lang, cfg, max_history,
        )

    return body


async def _translate_responses_input_items(
    items: list, target_lang: str, cfg: dict, max_history: int,
) -> list:
    """翻译 responses input items。

    默认只翻译最近 N 条 user message；translateSystemMessages=True 时，额外翻译
    input 列表里的 system/developer message（instructions 在外层单独处理）。
    """
    user_indices = []
    system_indices = []
    translate_system = bool(cfg.get("translateSystemMessages", False))
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        typ = item.get("type")
        if typ not in (None, "message"):
            continue
        if role == "user":
            user_indices.append(i)
        elif translate_system and role in ("system", "developer"):
            system_indices.append(i)

    to_translate_indices = set(system_indices)
    if user_indices:
        to_translate_indices.update(user_indices[-max_history:])

    pending: list[tuple[str, str]] = []
    for i in to_translate_indices:
        item = items[i]
        pending.extend(_collect_content_segments(f"resp:i:{i}", item.get("content")))

    if not pending:
        return items

    translated_map = await _translate_batch(pending, target_lang, cfg)

    new_items = list(items)
    for i in to_translate_indices:
        item = dict(items[i])
        item["content"] = _apply_content_translations(
            item.get("content"), f"resp:i:{i}", translated_map,
        )
        new_items[i] = item

    return new_items


# ─── 后台清理循环 ─────────────────────────────────────────────────

async def cleanup_loop() -> None:
    """后台定期清理过期缓存。"""
    while True:
        await asyncio.sleep(3600)
        try:
            cleared = await asyncio.to_thread(cleanup_expired)
            if cleared:
                print(f"[translation] cleaned {cleared} expired cache entries")
        except Exception as exc:
            print(f"[translation] cleanup error: {exc}")
