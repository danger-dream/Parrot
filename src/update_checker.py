"""GitHub Release 版本更新检查。

设计：
- 默认 1 小时拉一次 `https://api.github.com/repos/<repo>/releases`，
  按 `prerelease + draft` 标志过滤，挑最高 semver 当 latest。
- 与本地 `__version__` 比对：latest > local 才算"有新版"。
- 持久化：StateStore snapshots 单行 `app_update_state` 记录最新一次结果；
  `notified_for` 字段记录"已经推过通知的版本号"，避免重复推。
- 忽略列表：`config.updateChecker.ignoredVersions`（TG 操作时写入），
  存在该版本时不 banner / 不推。
- 推送：notifier.notify_event("app_update", text)；走总通知开关。
- Banner：A 方案——只要 latest > local 且未被忽略，主菜单底部一直显示。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any, Mapping, Optional

from packaging.version import InvalidVersion, Version

from . import __version__, config, network, notifier, state_db


# ─── 配置 ────────────────────────────────────────────────────────


def _cfg() -> dict:
    uc = config.get().get("updateChecker") or {}
    return {
        "enabled": bool(uc.get("enabled", True)),
        "intervalSeconds": int(uc.get("intervalSeconds", 3600) or 3600),
        "includePrerelease": bool(uc.get("includePrerelease", True)),
        "autoUpdate": bool(uc.get("autoUpdate", False)),
        "silentUpdate": bool(uc.get("silentUpdate", False)),
        "repo": str(uc.get("repo") or "danger-dream/Parrot").strip(),
        "ignoredVersions": list(uc.get("ignoredVersions") or []),
        "apiBase": str(uc.get("apiBase") or "https://api.github.com").strip().rstrip("/"),
    }


def _set_ignored(versions: list[str]) -> None:
    def _mut(c):
        c.setdefault("updateChecker", {})["ignoredVersions"] = list(versions)
    config.update(_mut)


def add_ignored(version: str) -> None:
    cfg = _cfg()
    s = set(cfg["ignoredVersions"])
    s.add(version)
    _set_ignored(sorted(s))


def remove_ignored(version: str) -> None:
    cfg = _cfg()
    s = set(cfg["ignoredVersions"])
    s.discard(version)
    _set_ignored(sorted(s))


def clear_ignored() -> None:
    _set_ignored([])


# ─── StateStore durable state ────────────────────────────────────


def _ensure_schema() -> None:
    """Compatibility no-op; StateStore owns its versioned schema."""


def _load_state(repo: str) -> Optional[dict]:
    row = state_db.update_state_load(repo)
    if row is not None:
        row["latest_prerelease"] = bool(row.get("latest_prerelease"))
    return row


def _save_state(repo: str, *, latest_version: Optional[str], latest_name: Optional[str],
                latest_url: Optional[str], latest_body: Optional[str],
                latest_published_at: Optional[str], latest_prerelease: bool,
                notified_for: Optional[str], silent_checks: dict | None = None) -> None:
    state_db.update_state_save(repo, {
        "latest_version": latest_version, "latest_name": latest_name,
        "latest_url": latest_url, "latest_body": latest_body,
        "latest_published_at": latest_published_at,
        "latest_prerelease": 1 if latest_prerelease else 0,
        "notified_for": notified_for,
        "silent_checks": silent_checks or {},
        "silent_checked_from": __version__,
    })


# ─── 内存缓存（供 banner / 菜单读）────────────────────────────────

_state_lock = threading.Lock()
_latest_cache: Optional[dict] = None


def _set_cache(state: Optional[dict]) -> None:
    global _latest_cache
    with _state_lock:
        _latest_cache = state


def get_cached() -> Optional[dict]:
    with _state_lock:
        return dict(_latest_cache) if _latest_cache else None


# ─── version 比较 ────────────────────────────────────────────────


def _strip_v(tag: str) -> str:
    return tag.lstrip("vV").strip()


def _parse(tag: str) -> Optional[Version]:
    try:
        return Version(_strip_v(tag))
    except InvalidVersion:
        return None


def _has_newer(latest_tag: Optional[str]) -> bool:
    if not latest_tag:
        return False
    cur = _parse(__version__)
    lat = _parse(latest_tag)
    if cur is None or lat is None:
        # 解析失败 → 仅在字符串不同且 latest 不为空时算"有新版"
        return latest_tag.lstrip("vV") != __version__.lstrip("vV")
    return lat > cur


_NEGATED_BREAKING_RE = re.compile(
    r"(?i)(?:无|没有|不含|未引入|不涉及|non[\s-]*|no\s+|without\s+)"
    r"(?:breaking[\s_-]*changes?|破坏性(?:变更|更新|改动)|不兼容(?:更新|变更|改动))"
)
_BREAKING_MARKER_RE = re.compile(
    r"(?i)(?:\bbreaking[\s_-]*changes?\b|\[breaking\]|破坏性(?:变更|更新|改动)|不兼容(?:更新|变更|改动))"
)
_CONVENTIONAL_BREAKING_RE = re.compile(r"(?im)^\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*|#{1,6}\s+)*[`*_]*[a-z]+(?:\([^)\n]+\))?!:")


def is_breaking_release(
    current_version: str,
    target_tag: str,
    release: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Identify major version jumps or explicit breaking-change markers in release notes."""
    cur = _parse(current_version)
    lat = _parse(target_tag)
    if cur is not None and lat is not None and lat.major > cur.major:
        return True, f"主版本号升级（v{cur.major} → v{lat.major}）"
    if isinstance(release, Mapping):
        combined = f"{release.get('name') or ''}\n{release.get('body') or ''}"
        cleaned = _NEGATED_BREAKING_RE.sub("", combined)
        if _BREAKING_MARKER_RE.search(cleaned) or _CONVENTIONAL_BREAKING_RE.search(cleaned):
            return True, "发布说明包含破坏性变更（Breaking Change）标记"
    return False, ""


def _release_path_check(target: str, releases: list, *, complete: bool) -> dict:
    current, end = _parse(__version__), _parse(target)
    reason = ""
    target_release = None
    if current is None or end is None:
        reason = "版本号无法解析，无法确认升级路径"
    elif not complete:
        reason = "发布列表不完整，无法确认全部中间版本"
    else:
        for release in releases:
            if not isinstance(release, Mapping) or release.get("draft"):
                continue
            version = _parse(str(release.get("tag_name") or ""))
            if version == end:
                target_release = release
            if version is not None and current < version <= end:
                breaking, detail = is_breaking_release(__version__, str(release["tag_name"]), release)
                if breaking:
                    reason = f"{release['tag_name']}: {detail}"
                    break
        if not reason and target_release is None:
            reason = "目标版本的发布说明不可用"
    return {"blocked": bool(reason), "reason": reason,
            "prerelease": bool((end and end.is_prerelease) or (target_release and target_release.get("prerelease")))}


# ─── banner 生成 ─────────────────────────────────────────────────


def get_update_banner() -> Optional[str]:
    """主菜单底部 banner：仅在 latest>local 且未被忽略时返回字符串，否则 None。
    开启静默自动更新且非破坏性版本时不再弹出催更横幅。
    """
    st = get_cached()
    if not st:
        return None
    latest = st.get("latest_version")
    if not latest or not _has_newer(latest):
        return None
    cfg = _cfg()
    ignored = set(cfg.get("ignoredVersions") or [])
    if latest in ignored:
        return None
    if cfg.get("silentUpdate"):
        verdict = (st.get("silent_checks") or {}).get(latest)
        if st.get("silent_checked_from") == __version__ and verdict and not verdict.get("blocked"):
            return None
    return (
        f"🆕 <b>Parrot 新版本可用</b>: <code>v{__version__}</code> → "
        f"<code>{notifier.escape_html(latest)}</code> — "
        "进入「⚙ 系统设置 → 🆕 版本更新」查看详情"
    )


# ─── HTTP 抓取 ───────────────────────────────────────────────────


def _http_get_json(url: str, timeout: int = 15):
    try:
        resp = network.get_sync(url, timeout=timeout, headers={
            "User-Agent": "parrot-update-checker/0.1",
            "Accept": "application/vnd.github+json",
        })
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[update_checker] fetch failed: {url} -> {exc}")
        return None


def _pick_latest(releases: list, *, include_prerelease: bool) -> Optional[dict]:
    """从 releases 列表里挑出 semver 最高的，过滤 draft 与（按需）prerelease。"""
    candidates: list[tuple[Version, dict]] = []
    fallback: Optional[dict] = None
    for r in releases or []:
        if r.get("draft"):
            continue
        if r.get("prerelease") and not include_prerelease:
            continue
        tag = r.get("tag_name") or ""
        ver = _parse(tag)
        if ver is None:
            fallback = fallback or r
            continue
        candidates.append((ver, r))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    return fallback


_workers_lock = threading.Lock()
_workers = 0
_stopping = False


def start() -> None:
    global _stopping
    with _workers_lock:
        _stopping = False


def begin_shutdown() -> None:
    global _stopping
    with _workers_lock:
        _stopping = True


async def stop() -> None:
    begin_shutdown()
    while True:
        with _workers_lock:
            if not _workers:
                return
        await asyncio.sleep(0.02)


def _check_once(*, push: bool = True, raise_on_error: bool = False, manual: bool = False) -> None:
    """Track the actual synchronous worker, not its cancellable asyncio wrapper."""
    global _workers
    with _workers_lock:
        if _stopping:
            return
        _workers += 1
    try:
        _check_once_impl(push=push, raise_on_error=raise_on_error, manual=manual)
    finally:
        with _workers_lock:
            _workers -= 1


def _check_once_impl(*, push: bool = True, raise_on_error: bool = False, manual: bool = False) -> None:
    """单次检查；API 可选择不推送并显式报告抓取失败，TG/后台默认不变。

    ``manual``（TG「立即检查」/ API checkForUpdates）不受自动检查开关影响：
    关闭自动检查只停后台轮询，手动检查照常联网。自动检查关闭时手动检查
    不推送通知、不触发自动更新（结果直接显示在发起检查的页面上）。
    """
    cfg = _cfg()
    if not cfg["repo"] or (not cfg["enabled"] and not manual):
        return
    if not cfg["enabled"]:
        push = False
    page_size = 100 if cfg.get("silentUpdate", False) else 20
    url = f"{cfg['apiBase']}/repos/{cfg['repo']}/releases?per_page={page_size}"
    releases = _http_get_json(url)
    if _stopping:
        return
    if not isinstance(releases, list):
        cached = get_cached()
        if cached and "silent_checks" in cached:
            _set_cache({**cached, "silent_checks": {}})
        if raise_on_error:
            raise RuntimeError("Release check failed")
        return
    complete = len(releases) < page_size
    if cfg.get("silentUpdate", False):
        for page in range(2, 11):
            if complete or _stopping:
                break
            more = _http_get_json(f"{url}&page={page}")
            if not isinstance(more, list):
                break
            releases.extend(more)
            complete = len(more) < page_size
    releases = [r for r in releases if isinstance(r, Mapping)]
    latest = _pick_latest(releases, include_prerelease=cfg["includePrerelease"])
    if not latest:
        # 没有任何符合条件的 release：清掉缓存即可
        _save_state(
            cfg["repo"],
            latest_version=None, latest_name=None, latest_url=None,
            latest_body=None, latest_published_at=None, latest_prerelease=False,
            notified_for=(_load_state(cfg["repo"]) or {}).get("notified_for"),
        )
        _set_cache(_load_state(cfg["repo"]))
        return

    tag = latest.get("tag_name") or ""
    prev = _load_state(cfg["repo"]) or {}
    notified_for = prev.get("notified_for")

    is_newer = _has_newer(tag)
    ignored = set(cfg["ignoredVersions"])
    silent_mode = bool(cfg.get("silentUpdate")) and not manual
    checks = {}
    if cfg.get("silentUpdate", False):
        from . import updater
        targets = {tag}
        staged = updater.load_state()
        if staged.get("stage") == updater.STAGE_STAGED and staged.get("target_tag"):
            targets.add(staged["target_tag"])
        checks = {target: _release_path_check(target, releases, complete=complete) for target in targets}
    verdict = checks.get(tag) or {}
    breaking = bool(silent_mode and verdict.get("blocked"))
    breaking_reason = verdict.get("reason") or ""
    should_push = (
        push
        and is_newer
        and tag not in ignored
        and tag != notified_for      # 同一版本不重复推
        and (not silent_mode or breaking)
    )

    if should_push:
        try:
            reply_markup = _build_update_buttons(tag)
            text = _format_release(tag, latest)
            if silent_mode and breaking:
                text = (
                    f"⚠️ <b>升级安全检查未通过（{notifier.escape_html(breaking_reason)}），已跳过静默更新，请人工确认</b>\n\n"
                    + text
                )
            notifier.notify_event("app_update", text, reply_markup=reply_markup)
            notified_for = tag
        except Exception as exc:
            print(f"[update_checker] notify failed: {exc}")
        # 自动下载模式（非静默）：检测到新版直接走备份+拉取进 staged，重启仍需人工二次确认
        try:
            if not _stopping and not silent_mode and _cfg_auto_update() and tag not in ignored:
                from . import updater
                if not updater.is_busy():
                    admin_chat = None
                    try:
                        admins = (config.get().get("telegram") or {}).get("adminIds") or []
                        if admins:
                            admin_chat = int(admins[0])
                    except Exception:
                        admin_chat = None
                    updater.stage_update(tag, chat_id=admin_chat)
        except Exception as exc:
            print(f"[update_checker] auto-update trigger failed: {exc}")

    _save_state(
        cfg["repo"],
        latest_version=tag,
        latest_name=latest.get("name"),
        latest_url=latest.get("html_url"),
        latest_body=latest.get("body"),
        latest_published_at=latest.get("published_at"),
        latest_prerelease=bool(latest.get("prerelease")),
        notified_for=notified_for,
        silent_checks=checks,
    )
    _set_cache(_load_state(cfg["repo"]))

    if silent_mode and not _stopping:
        _drive_silent_update(tag)


def _drive_silent_update(tag: str) -> None:
    """在静默更新模式下推进：未暂存则静默备份拉取，已暂存且业务空闲则触发重启。"""
    try:
        from . import updater
        cfg = _cfg()
        if _stopping or not cfg["enabled"] or not cfg["silentUpdate"]:
            return
        st = updater.load_state()
        stage = st.get("stage")
        if stage == updater.STAGE_STAGED:
            if not st.get("silent") or st.get("target_repo") != cfg["repo"]:
                return
            tag = st.get("target_tag") or ""
        if stage in (updater.STAGE_FAILED, updater.STAGE_ROLLED_BACK) and st.get("target_tag") == tag:
            updater.notify_silent_failure(str(st.get("message") or "自动更新失败，请查看日志"))
            return
        if st.get("cancelled_target") == tag and st.get("cancelled_repo") == cfg["repo"]:
            return
        cached = get_cached() or {}
        verdict = (cached.get("silent_checks") or {}).get(tag)
        if (not tag or not _has_newer(tag) or tag in cfg["ignoredVersions"]
                or cached.get("repo") != cfg["repo"] or cached.get("silent_checked_from") != __version__
                or not verdict or verdict.get("blocked")
                or (verdict.get("prerelease") and not cfg["includePrerelease"])
                or time.time() - float(cached.get("checked_at") or 0) > max(300, cfg["intervalSeconds"]) * 2):
            return
        if stage == updater.STAGE_STAGED:
            updater.try_silent_activate()
            return
        if not updater.is_busy():
            ok, _ = updater.stage_update(tag, silent=True)
            if ok and not _stopping:
                updater.try_silent_activate()
    except Exception as exc:
        print(f"[update_checker] silent update step failed: {exc}")
        from . import updater
        updater.notify_silent_failure(f"后台更新异常: {type(exc).__name__}")


def _cfg_auto_update() -> bool:
    return bool((config.get().get("updateChecker") or {}).get("autoUpdate", False))


def _cfg_silent_update() -> bool:
    return bool((config.get().get("updateChecker") or {}).get("silentUpdate", False))


def _build_update_buttons(tag: str) -> dict:
    """构造"更新通知"的 inline 按钮：🚀 立即更新 / 🔕 忽略。

    callback_data 与 update_menu 的路由一致，点击后进入双重确认流程。
    """
    # tag 可能较长，但 callback_data 上限 64B，semver tag 远小于此，安全。
    return {
        "inline_keyboard": [
            [
                {"text": f"🚀 立即更新到 {tag}", "callback_data": f"upd:do_update:{tag}"},
            ],
            [
                {"text": "🔕 忽略此版本", "callback_data": f"upd:ignore:{tag}"},
                {"text": "📋 查看详情", "callback_data": "menu:update"},
            ],
        ]
    }


def _format_release(tag: str, release: dict) -> str:
    name = (release.get("name") or "").strip()
    body = (release.get("body") or "").strip()
    if len(body) > 600:
        body = body[:600].rstrip() + "…"
    url = release.get("html_url") or ""
    published = release.get("published_at") or ""
    pre = " (pre-release)" if release.get("prerelease") else ""
    head = f"🆕 <b>Parrot 新版本可用{notifier.escape_html(pre)}</b>\n"
    head += f"当前: <code>v{__version__}</code> → 最新: <code>{notifier.escape_html(tag)}</code>\n"
    if name:
        head += f"标题: {notifier.escape_html(name)}\n"
    if published:
        head += f"发布: <code>{notifier.escape_html(published)}</code>\n"
    if body:
        head += f"\n{notifier.escape_html(body)}\n"
    if url:
        head += f"\n🔗 {notifier.escape_html(url)}"
    return head


# ─── 外部入口 ────────────────────────────────────────────────────


def force_refresh_sync() -> None:
    """供 TG「立即检查」按钮调用，同步阻塞拉一次（不受自动检查开关影响）。"""
    try:
        _ensure_schema()
    except Exception:
        pass
    _check_once(push=True, manual=True)


async def update_loop() -> None:
    try:
        _ensure_schema()
        # 启动时把上次的状态恢复到内存
        cfg = _cfg()
        st = _load_state(cfg["repo"]) if cfg["repo"] else None
        if st:
            _set_cache(st)
    except Exception as exc:
        print(f"[update_checker] init failed: {exc}")

    # 首次延迟 30s，避免和其他 startup 抢
    await asyncio.sleep(30)
    next_check_at = 0.0
    while True:
        try:
            cfg = _cfg()
            now = time.monotonic()
            if cfg["enabled"] and now >= next_check_at:
                await asyncio.to_thread(_check_once, push=True)
                cfg = _cfg()
                next_check_at = time.monotonic() + max(300, cfg["intervalSeconds"])
            elif not cfg["enabled"]:
                next_check_at = now + max(300, cfg["intervalSeconds"])
            elif cfg.get("silentUpdate"):
                cached = get_cached() or {}
                tag = cached.get("latest_version") or ""
                await asyncio.to_thread(_drive_silent_update, tag)
            sleep_sec = 20.0 if (cfg["enabled"] and cfg.get("silentUpdate")) else max(1.0, min(60.0, next_check_at - time.monotonic()))
            await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[update_checker] loop iteration failed: {exc}")
            await asyncio.sleep(60)
