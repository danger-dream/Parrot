"""Cursor dashboard event reconciliation.

AgentService realtime usage exposes aggregate prompt/output counts but not Cursor's
cache-read/cache-write split or charged cents.  The dashboard event feed carries
those facts together with the exact upstream conversationId.  This module polls
only while Parrot has unresolved Cursor attempts and asks :mod:`log_db` to attach
normalized events idempotently without overwriting the immutable realtime facts.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from . import config, log_db, oauth_manager
from .cursor_bridge.errors import CursorAuthError
from .oauth import cursor as cursor_provider


def _iso_timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _settings() -> dict[str, Any]:
    raw = config.get().get("cursorOAuth") or {}
    return raw if isinstance(raw, dict) else {}


def _bounded_int(raw: Any, default: int, *, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(lo, min(hi, value))


async def sync_account(account_key: str, *, force: bool = False) -> dict[str, Any]:
    cfg = _settings()
    if not bool(cfg.get("eventSyncEnabled", True)):
        return {"action": "disabled", "account_key": account_key}
    account = oauth_manager.get_account(account_key)
    if account is None or oauth_manager.provider_of(account) != "cursor":
        return {"action": "noop", "account_key": account_key}

    now = time.time()
    lookback = _bounded_int(
        cfg.get("eventLookbackSeconds", 900), 900, lo=60, hi=86_400,
    )
    cycle_start = _iso_timestamp(account.get("billing_cycle_start"))
    pending_floor = cycle_start if cycle_start is not None else now - 86_400
    targets = await asyncio.to_thread(
        log_db.cursor_reconciliation_targets,
        account_key,
        since_ts=pending_floor,
    )
    pending_count = int(
        targets.get("total" if force else "exact") or 0
    )
    search_floor = pending_floor
    if pending_count <= 0:
        if force:
            # Startup/manual force refreshes already-linked events for the whole
            # cycle, allowing Cursor to revise provisional fractional costs.
            if int(targets.get("recent_reconciled") or 0) <= 0:
                return {
                    "action": "up_to_date",
                    "account_key": account_key,
                    "pending": 0,
                }
        else:
            # Refresh only recent already-linked events for provisional cost
            # changes. Unresolved attempts are still discovered across the whole
            # billing cycle so delayed events never fall out of the overlap window.
            recent_floor = now - lookback
            recent_targets = await asyncio.to_thread(
                log_db.cursor_reconciliation_targets,
                account_key,
                since_ts=recent_floor,
            )
            recent_reconciled = int(recent_targets.get("recent_reconciled") or 0)
            if recent_reconciled <= 0:
                return {
                    "action": "up_to_date",
                    "account_key": account_key,
                    "pending": 0,
                }
            targets = recent_targets
            search_floor = recent_floor

    earliest = targets.get("min_created_at")
    if earliest is not None:
        search_floor = min(search_floor, float(earliest) - 120.0)
    if cycle_start is not None:
        search_floor = max(cycle_start, search_floor)

    access_token = await oauth_manager.ensure_valid_token(account_key)
    fetch_kwargs = {
        "start_ms": max(0, int(search_floor * 1000)),
        "end_ms": int((now + 60.0) * 1000),
        "account_key": account_key,
        "page_size": _bounded_int(
            cfg.get("eventPageSize", 1000), 1000, lo=1, hi=1000,
        ),
        "max_pages": _bounded_int(
            cfg.get("eventMaxPages", 20), 20, lo=1, hi=200,
        ),
    }
    try:
        events = await asyncio.to_thread(
            cursor_provider.fetch_usage_events_sync,
            access_token,
            **fetch_kwargs,
        )
    except CursorAuthError:
        await oauth_manager.force_refresh(account_key)
        access_token = await oauth_manager.ensure_valid_token(account_key)
        events = await asyncio.to_thread(
            cursor_provider.fetch_usage_events_sync,
            access_token,
            **fetch_kwargs,
        )

    result = await asyncio.to_thread(
        log_db.reconcile_cursor_usage_events,
        account_key,
        events,
        since_ts=search_floor,
        legacy_match_seconds=float(cfg.get("eventLegacyMatchSeconds", 5) or 5),
        tool_settle_seconds=float(cfg.get("eventToolSettleSeconds", 120) or 120),
    )
    matched = int(result.get("matched") or 0)
    refreshed = int(result.get("refreshed") or 0)
    action = (
        "reconciled" if matched else "refreshed" if refreshed
        else "pending" if pending_count else "up_to_date"
    )
    return {
        "action": action,
        "account_key": account_key,
        "pending": pending_count,
        **result,
    }


async def sync_once(*, force: bool = False) -> list[dict[str, Any]]:
    keys = [
        oauth_manager.get_account_key(account)
        for account in oauth_manager.list_accounts()
        if oauth_manager.provider_of(account) == "cursor"
        and account.get("enabled", True)
    ]
    if not keys:
        return []
    return await asyncio.gather(*[
        sync_account(account_key, force=force) for account_key in keys
    ])


async def sync_loop() -> None:
    first = True
    while True:
        try:
            results = await sync_once(force=first)
            first = False
            matched = sum(int(result.get("matched") or 0) for result in results)
            if matched:
                exact = sum(int(result.get("exact") or 0) for result in results)
                legacy = sum(int(result.get("legacy") or 0) for result in results)
                print(
                    f"[cursor] reconciled {matched} official usage event(s) "
                    f"(exact={exact}, legacy={legacy})"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            first = False
            print(f"[cursor] usage event reconciliation failed: {type(exc).__name__}: {exc}")
        interval = _bounded_int(
            _settings().get("eventSyncSeconds", 30), 30, lo=15, hi=3600,
        )
        await asyncio.sleep(interval)


__all__ = ["sync_account", "sync_loop", "sync_once"]
