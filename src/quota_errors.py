"""Provider-specific long-lived quota error parsing.

Only explicit, bounded signals live here.  Generic 429 responses remain on the
normal failover/cooldown path because RPM/TPM throttles must not freeze a model
until an arbitrary date.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse


_BJT = timezone(timedelta(hours=8))
_ZHIPU_RESET_RE = re.compile(r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
_ZHIPU_CODE_RE = re.compile(r'"code"\s*:\s*"?1310"?', re.IGNORECASE)
_ZHIPU_MESSAGE_RE = re.compile(r"\[1310\]\[.*?(?:每周/每月使用上限|限额将在)", re.IGNORECASE)
_MAX_RESET_AHEAD_MS = 45 * 24 * 60 * 60 * 1000


def _json_payload(detail: str | None) -> dict:
    text = str(detail or "").strip()
    start = text.find("{")
    if start < 0:
        return {}
    try:
        obj = json.loads(text[start:])
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _error_fields(detail: str | None) -> tuple[str, str]:
    obj = _json_payload(detail)
    error = obj.get("error") if isinstance(obj.get("error"), dict) else obj
    if not isinstance(error, dict):
        return "", ""
    code = str(error.get("code") or obj.get("code") or "").strip()
    message = str(error.get("message") or obj.get("message") or "").strip()
    return code, message


def is_zhipu_channel(channel: Any) -> bool:
    provider = str(getattr(channel, "provider", "") or "").strip().lower()
    if provider in {"zhipu", "bigmodel", "glm"}:
        return True
    try:
        host = (urlparse(str(getattr(channel, "base_url", "") or "")).hostname or "").lower()
    except Exception:
        return False
    return host == "bigmodel.cn" or host.endswith(".bigmodel.cn")


def is_zhipu_1310_message(detail: str | None) -> bool:
    """Return True only for the explicit BigModel weekly/monthly quota signal."""
    code, message = _error_fields(detail)
    if code == "1310":
        return True
    text = str(detail or "")
    return bool(_ZHIPU_CODE_RE.search(text) or _ZHIPU_MESSAGE_RE.search(message or text))


def zhipu_1310_reset_ms(
    channel: Any,
    *,
    http_status: int | None,
    error_detail: str | None,
    now_ms: int | None = None,
) -> int | None:
    """Parse a validated Beijing reset timestamp from a Zhipu 429/code=1310.

    Absurd or stale dates are rejected rather than letting an upstream body
    freeze a channel/model indefinitely.  The known weekly/monthly window fits
    comfortably inside the 45-day upper bound.
    """
    try:
        status = int(http_status or 0)
    except (TypeError, ValueError):
        return None
    if status != 429 or not is_zhipu_channel(channel):
        return None

    code, message = _error_fields(error_detail)
    if code != "1310":
        return None
    match = _ZHIPU_RESET_RE.search(message)
    if not match:
        return None
    try:
        reset = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BJT)
    except ValueError:
        return None

    reset_ms = int(reset.timestamp() * 1000)
    current = int(now_ms if now_ms is not None else time.time() * 1000)
    if reset_ms <= current or reset_ms - current > _MAX_RESET_AHEAD_MS:
        return None
    return reset_ms


def active_quota_cooldown(entry: dict | None, *, now_ms: int | None = None) -> bool:
    if not isinstance(entry, dict) or not is_zhipu_1310_message(entry.get("last_error_message")):
        return False
    try:
        until = int(entry.get("cooldown_until"))
    except (TypeError, ValueError):
        return False
    current = int(now_ms if now_ms is not None else time.time() * 1000)
    return until > current


def format_bjt_ms(timestamp_ms: int, *, compact: bool = False) -> str:
    dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=_BJT)
    return dt.strftime("%m-%d %H:%M") if compact else dt.strftime("%Y-%m-%d %H:%M:%S")
