"""上游服务状态监控（Atlassian Statuspage 兼容）。

目标：在 Claude / OpenAI / Cloudflare 等关键上游出问题或恢复的第一时间通过 TG 推送提醒。

实现要点：
- 三家都用 Atlassian Statuspage，`/api/v2/incidents.json` 接口字段一致；
  无官方 webhook，所以走 polling。
- 用 StateStore snapshots 一张 `status_seen_updates` 表按 `incident_update_id` 去重。
- 启动时**不补推历史**：把当前所有现存 update 直接标记为 seen，避免重启时刷屏。
  但仍把"当前 unresolved incident"记录在内存，恢复时给老大补一条恢复通知。
- 推送复用 notifier.notify_event("status_alert", ...) 走现有总开关 + 事件开关 + TG 通道。
- 按 impact 分级 + 节流：同一 (provider, incident, status) 已发就不重发。

集成位置：
- 后台 loop 由 server.py 启动 `monitor_loop()`
- TG banner / 菜单读 `get_active_summary()` / `snapshot_incidents()`
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from . import config, network, notifier, state_db


# ─── 目标定义 ────────────────────────────────────────────────────

TARGETS: dict[str, dict] = {
    "claude": {
        "label": "Claude",
        "icon": "🅰️",
        "base": "https://status.claude.com",
    },
    "openai": {
        "label": "OpenAI",
        "icon": "🅾️",
        "base": "https://status.openai.com",
    },
    "cloudflare": {
        "label": "Cloudflare",
        "icon": "🟧",
        "base": "https://www.cloudflarestatus.com",
    },
}

# impact 优先级排序（用于 banner 选最严重的）
_IMPACT_RANK = {"none": 0, "maintenance": 1, "minor": 2, "major": 3, "critical": 4}

_IMPACT_ICON = {
    "critical": "🔴",
    "major": "🟠",
    "minor": "🟡",
    "maintenance": "🛠",
    "none": "⚪",
}

_STATUS_ICON = {
    "investigating": "🔍",
    "identified": "🛠",
    "monitoring": "👀",
    "resolved": "✅",
    "postmortem": "📝",
    # maintenance lifecycle
    "scheduled": "🗓",
    "in_progress": "🚧",
    "verifying": "🔎",
    "completed": "✅",
}


def _impact_rank(impact: Optional[str]) -> int:
    return _IMPACT_RANK.get((impact or "none").lower(), 0)


def _provider_label(provider: str) -> str:
    return TARGETS.get(provider, {}).get("label", provider)


def _provider_icon(provider: str) -> str:
    """Plain-text icon for buttons and compact non-rich text."""
    try:
        table = ((config.get().get("telegramUi") or {}).get("providerBtnEmoji") or {})
        if isinstance(table, dict) and provider in table:
            return str(table.get(provider) or TARGETS.get(provider, {}).get("icon", "📡"))
    except Exception:
        pass
    return TARGETS.get(provider, {}).get("icon", "📡")


def _provider_tag(provider: str) -> str:
    """HTML provider tag for message/notification body."""
    if provider in ("claude", "openai", "xai"):
        return notifier.provider_tag(provider)
    return f"{_provider_icon(provider)} {notifier.escape_html(_provider_label(provider))}"


def _statuspage_base(provider: str) -> str:
    return TARGETS.get(provider, {}).get("base", "")


# ─── 配置 ────────────────────────────────────────────────────────


def _cfg() -> dict:
    sm = config.get().get("statusMonitor") or {}
    return {
        "enabled": bool(sm.get("enabled", True)),
        "intervalSeconds": int(sm.get("intervalSeconds", 60) or 60),
        "targets": list(sm.get("targets") or ["claude", "openai", "cloudflare"]),
        # impact 推送门槛：低于该级别的 incident 不主动推（但仍记录 + 顶部 banner 不会显示）。
        # 默认 minor 起步——none/maintenance 静默处理。
        "minImpact": str(sm.get("minImpact", "minor")).lower(),
    }


# ─── StateStore durable state ─────────────────────────────────────


def _ensure_schema() -> None:
    """Compatibility no-op; StateStore owns its versioned schema."""


def _load_seen(provider: str) -> set[str]:
    return state_db.status_seen_load(provider)


# ─── Mute（屏蔽某条 incident）─────────────────────────────────────
#
# 内存缓存 + StateStore snapshots 双写。粒度 (provider, incident_id)；被 mute 的 incident
# 不进 banner、不推任何 update、不推 resolved。statuspage 历史滚出后由
# cleanup_stale_mutes 自动清掉，避免堆积。

_mute_lock = threading.Lock()
_muted: dict[str, set[str]] = {}   # provider -> {incident_id}
_mute_loaded = False


def _load_muted_into_memory() -> None:
    global _mute_loaded
    if _mute_loaded:
        return
    rows = state_db.status_muted_load_all()
    with _mute_lock:
        _muted.clear()
        for row in rows:
            _muted.setdefault(row["provider"], set()).add(row["incident_id"])
    _mute_loaded = True


def is_muted(provider: str, incident_id: str) -> bool:
    _load_muted_into_memory()
    with _mute_lock:
        return incident_id in _muted.get(provider, set())


def mute_incident(provider: str, incident_id: str, name: str = "") -> None:
    _load_muted_into_memory()
    with _mute_lock:
        _muted.setdefault(provider, set()).add(incident_id)
    state_db.status_muted_save(provider, incident_id, name)
    # 立即从活跃表里抽走，避免 banner / 当前活跃区还显示
    with _active_lock:
        _active.get(provider, {}).pop(incident_id, None)


def unmute_incident(provider: str, incident_id: str) -> None:
    _load_muted_into_memory()
    with _mute_lock:
        bucket = _muted.get(provider)
        if bucket:
            bucket.discard(incident_id)
    state_db.status_muted_delete(provider, incident_id)


def list_muted() -> list[dict]:
    """返回所有当前被屏蔽的 incident 记录（含 name / muted_at）。"""
    _load_muted_into_memory()
    return state_db.status_muted_load_all()


def _cleanup_stale_mutes(live_ids_by_provider: dict[str, set[str]]) -> int:
    """删除 statuspage 历史窗口里已经看不到的 mute 记录。

    live_ids_by_provider: 各 provider 当前 incidents.json 返回的 incident_id 集合。
    """
    removed = state_db.status_muted_cleanup(live_ids_by_provider)
    if removed:
        with _mute_lock:
            for provider, live_ids in live_ids_by_provider.items():
                bucket = _muted.get(provider)
                if bucket:
                    bucket.intersection_update(live_ids)
    return removed


def _mark_seen(provider: str, update_id: str, incident_id: str, status: Optional[str]) -> None:
    state_db.status_seen_mark(provider, update_id, incident_id, status)


# ─── 内存快照（供 banner / 菜单查询）─────────────────────────────

# provider -> { incident_id: incident_dict_snapshot }
_active_lock = threading.Lock()
_active: dict[str, dict[str, dict]] = {p: {} for p in TARGETS}
# 启动时是否已"消化历史"过；False 时第一轮直接 mark_seen 不推
_initialized_providers: set[str] = set()


def _set_active(provider: str, incidents: list[dict]) -> tuple[list[dict], list[dict]]:
    """更新内存活跃 incident 表，返回 (新增的, 刚恢复的)。被 mute 的全程跳过。"""
    _load_muted_into_memory()
    muted_ids: set[str] = _muted.get(provider, set()).copy() if provider in _muted else set()
    new_active = {
        i["id"]: i for i in incidents
        if (i.get("status") or "").lower() != "resolved"
        and i["id"] not in muted_ids
    }
    newly_added: list[dict] = []
    just_resolved: list[dict] = []
    with _active_lock:
        prev = _active.get(provider) or {}
        for iid, inc in new_active.items():
            if iid not in prev:
                newly_added.append(inc)
        for iid, prev_inc in prev.items():
            if iid not in new_active:
                # mute 触发的退出不算 resolved
                if iid in muted_ids:
                    continue
                latest = next((x for x in incidents if x.get("id") == iid), prev_inc)
                just_resolved.append(latest)
        _active[provider] = new_active
    return newly_added, just_resolved


def snapshot_active() -> dict[str, list[dict]]:
    """供 TG banner / 菜单查询当前活跃 incident。"""
    with _active_lock:
        return {p: list(v.values()) for p, v in _active.items()}


def forget_provider(provider: str) -> None:
    """清空内存里某 provider 的活跃 incident 表，并允许下次重新进入 silent prime。

    场景：TG 把某 provider 从 targets 列表移除时，banner 不应该还显示它的旧故障。
    """
    with _active_lock:
        _active[provider] = {}
    _initialized_providers.discard(provider)


def get_active_summary() -> Optional[str]:
    """生成一行紧凑摘要，供顶部 banner 使用。无活跃 incident 返回 None。"""
    snap = snapshot_active()
    parts: list[str] = []
    worst_rank = 0
    for p, incs in snap.items():
        if not incs:
            continue
        # 只在 banner 计数；按最严重的 impact 排序
        max_impact = max(((_impact_rank(i.get("impact")), i.get("impact") or "none") for i in incs),
                          key=lambda x: x[0])
        worst_rank = max(worst_rank, max_impact[0])
        parts.append(f"{_provider_tag(p)} × {len(incs)}")
    if not parts:
        return None
    icon = "🔴" if worst_rank >= 3 else ("🟠" if worst_rank >= 2 else "🟡")
    return f"{icon} <b>上游故障中</b>: {' · '.join(parts)} — 进入「⚙ 系统设置 → 📡 故障订阅」查看详情"


# ─── HTTP fetch ──────────────────────────────────────────────────


def _http_get_json(url: str, timeout: int = 15) -> Optional[dict]:
    try:
        resp = network.get_sync(
            url,
            timeout=timeout,
            headers={"User-Agent": "parrot-status-monitor/0.1"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[status_monitor] fetch failed: {url} -> {exc}")
        return None


def _fetch_incidents(provider: str) -> Optional[list[dict]]:
    base = _statuspage_base(provider)
    if not base:
        return None
    data = _http_get_json(f"{base}/api/v2/incidents.json")
    if not data:
        return None
    inc = data.get("incidents")
    if not isinstance(inc, list):
        return None
    return inc


# ─── 推送格式 ────────────────────────────────────────────────────


def _format_new_incident(provider: str, incident: dict) -> str:
    name = (incident.get("name") or "(未命名 incident)").strip()
    impact = (incident.get("impact") or "none").lower()
    status = (incident.get("status") or "investigating").lower()
    started = incident.get("started_at") or incident.get("created_at") or ""
    short = incident.get("shortlink") or f"{_statuspage_base(provider)}/incidents/{incident.get('id','')}"
    icon = _IMPACT_ICON.get(impact, "📡")
    sicon = _STATUS_ICON.get(status, "")
    text = (
        f"{icon} <b>[{notifier.escape_html(_provider_label(provider))}]</b> "
        f"{notifier.escape_html(name)}\n"
        f"影响: <code>{impact}</code> · 状态: {sicon} <code>{status}</code>\n"
        f"开始: <code>{notifier.escape_html(started)}</code>\n"
        f"{notifier.escape_html(short)}"
    )
    return text


def _format_update(provider: str, incident: dict, upd: dict) -> str:
    status = (upd.get("status") or "").lower()
    sicon = _STATUS_ICON.get(status, "📡")
    name = (incident.get("name") or "").strip()
    body = (upd.get("body") or "").strip()
    if len(body) > 600:
        body = body[:600].rstrip() + "..."
    created = upd.get("created_at") or ""
    text = (
        f"{sicon} <b>[{notifier.escape_html(_provider_label(provider))}]</b> "
        f"{notifier.escape_html(name)} — <code>{status}</code>\n"
        f"<i>{notifier.escape_html(created)}</i>\n"
        f"{notifier.escape_html(body)}"
    )
    return text


def _format_resolved(provider: str, incident: dict) -> str:
    name = (incident.get("name") or "").strip()
    started = incident.get("started_at") or incident.get("created_at") or ""
    resolved = incident.get("resolved_at") or ""
    text = (
        f"✅ <b>[{notifier.escape_html(_provider_label(provider))}]</b> "
        f"已恢复: {notifier.escape_html(name)}\n"
        f"开始: <code>{notifier.escape_html(started)}</code>\n"
        f"恢复: <code>{notifier.escape_html(resolved)}</code>"
    )
    return text


# ─── 主轮询逻辑 ──────────────────────────────────────────────────


def _process_provider(provider: str, *, push: bool) -> None:
    """拉一次该 provider 的最新 incidents 并增量推送。

    push=False 仅 mark_seen + 更新内存活跃表（首轮启动用，不刷屏）。
    """
    incidents = _fetch_incidents(provider)
    if incidents is None:
        return

    # 1. 更新内存活跃表，得到"新出现 / 刚恢复"
    newly_added, just_resolved = _set_active(provider, incidents)

    # 2. 按 update_id 增量推送
    seen = _load_seen(provider)
    cfg = _cfg()
    min_rank = _impact_rank(cfg["minImpact"])

    for inc in incidents:
        iid = inc.get("id") or ""
        if iid and is_muted(provider, iid):
            # mute 的 incident：仍然把 update id 标 seen 避免以后取消 mute 后翻历史，
            # 但任何推送/active 更新都跳过
            for upd in (inc.get("incident_updates") or []):
                uid = upd.get("id") or ""
                if uid and uid not in seen:
                    _mark_seen(provider, uid, iid, upd.get("status"))
                    seen.add(uid)
            continue
        impact = (inc.get("impact") or "none").lower()
        impact_ok = _impact_rank(impact) >= min_rank
        updates = inc.get("incident_updates") or []
        # incident_updates 是按时间倒序的；按正序处理（旧 → 新）
        for upd in reversed(updates):
            uid = upd.get("id") or ""
            if not uid or uid in seen:
                continue
            _mark_seen(provider, uid, iid, upd.get("status"))
            seen.add(uid)
            if not push:
                continue
            if not impact_ok and (upd.get("status") or "").lower() != "resolved":
                # 低于阈值的 incident，非 resolved 不推；resolved 还是给一条收尾
                continue
            try:
                text = _format_update(provider, inc, upd)
                notifier.notify_event("status_alert", text)
            except Exception as exc:
                print(f"[status_monitor] notify update failed: {exc}")

    # 3. 对"刚刚出现"的 incident 额外推一条 headline（即使第一条 update 已经推过，
    #    headline 文案更醒目，包含 impact / shortlink）。
    if push:
        for inc in newly_added:
            impact = (inc.get("impact") or "none").lower()
            if _impact_rank(impact) < min_rank:
                continue
            try:
                notifier.notify_event("status_alert", _format_new_incident(provider, inc))
            except Exception as exc:
                print(f"[status_monitor] notify headline failed: {exc}")

    # 4. 对刚刚恢复的 incident 推一条收尾（不受 minImpact 限制——恢复消息总要给）
    if push:
        for inc in just_resolved:
            iid = inc.get("id") or ""
            if iid and is_muted(provider, iid):
                continue
            try:
                notifier.notify_event("status_alert", _format_resolved(provider, inc))
            except Exception as exc:
                print(f"[status_monitor] notify resolved failed: {exc}")


async def monitor_loop() -> None:
    """后台主 loop。由 server.py 启动。"""
    try:
        _ensure_schema()
    except Exception as exc:
        print(f"[status_monitor] schema init failed: {exc}")
        return

    # 启动时按 enabled 立即触发一次 "silent prime"：标 seen 但不推送
    while True:
        try:
            cfg = _cfg()
            if not cfg["enabled"]:
                await asyncio.sleep(max(10, cfg["intervalSeconds"]))
                continue
            active_targets = set(cfg["targets"])
            # 保险层：若上一轮还在监控的 provider 这一轮被移除，清掉内存活跃表
            for known in list(TARGETS.keys()):
                if known not in active_targets:
                    snap = snapshot_active().get(known) or []
                    if snap or known in _initialized_providers:
                        forget_provider(known)
            live_ids: dict[str, set[str]] = {}
            for p in cfg["targets"]:
                if p not in TARGETS:
                    continue
                first_time = p not in _initialized_providers
                await asyncio.to_thread(_process_provider, p, push=not first_time)
                _initialized_providers.add(p)
                # 顺手收集 live id 给 mute 清理用
                try:
                    incs = _fetch_incidents(p) or []
                    live_ids[p] = {i.get("id") for i in incs if i.get("id")}
                except Exception:
                    pass
            if live_ids:
                try:
                    removed = await asyncio.to_thread(_cleanup_stale_mutes, live_ids)
                    if removed:
                        print(f"[status_monitor] cleaned {removed} stale mute records")
                except Exception as exc:
                    print(f"[status_monitor] mute cleanup failed: {exc}")
            await asyncio.sleep(max(10, cfg["intervalSeconds"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[status_monitor] loop iteration failed: {exc}")
            await asyncio.sleep(30)


# ─── TG 菜单辅助 ─────────────────────────────────────────────────


def list_recent_incidents(provider: str, limit: int = 5) -> list[dict]:
    """供 TG 菜单：拉一次最新 incidents（同步阻塞版，调用方应在 worker thread 里跑）。"""
    return (_fetch_incidents(provider) or [])[:limit]
