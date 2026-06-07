"""🆕 版本更新菜单。

入口：「⚙ 系统设置 → 🆕 版本更新」
功能：
- 显示当前版本 / 最新版本 / 发布时间 / changelog 摘要
- 立即检查（同步阻塞拉一次）
- 忽略此版本（加入 ignoredVersions）/ 清空忽略列表
- 设置：总开关 / 间隔 / 是否含 prerelease / 自动更新开关
- 🚀 一键自更新（双重确认）：备份 → 拉取 → staged → 二次确认重启 → 健康校验/回滚
"""

from __future__ import annotations

from typing import Optional

from ... import __version__, config, update_checker, updater
from .. import states, ui


def _cfg() -> dict:
    return config.get().get("updateChecker") or {}


def _update_cfg(patch: dict) -> None:
    def _mut(c):
        c.setdefault("updateChecker", {}).update(patch)
    config.update(_mut)


# ─── 主菜单文本 ──────────────────────────────────────────────────

def _format_main_text() -> str:
    cfg = _cfg()
    enabled = bool(cfg.get("enabled", True))
    interval = int(cfg.get("intervalSeconds", 3600) or 3600)
    include_pre = bool(cfg.get("includePrerelease", True))
    auto_update = bool(cfg.get("autoUpdate", False))
    repo = cfg.get("repo") or "danger-dream/Parrot"
    ignored = list(cfg.get("ignoredVersions") or [])

    st = update_checker.get_cached() or {}
    latest = st.get("latest_version")
    latest_name = st.get("latest_name") or ""
    latest_pub = st.get("latest_published_at") or ""
    latest_pre = bool(st.get("latest_prerelease"))
    latest_url = st.get("latest_url") or ""
    latest_body = (st.get("latest_body") or "").strip()
    if len(latest_body) > 800:
        latest_body = latest_body[:800].rstrip() + "…"

    mode = updater.get_mode()
    mode_label = {"docker": "🐳 Docker", "systemd": "⚙ systemd", "bare": "📄 源码"}.get(mode, mode)

    lines = [
        "🆕 <b>版本更新</b>",
        "",
        f"当前版本: <code>v{__version__}</code>",
        f"运行形态: {mode_label}",
        f"仓库: <code>{ui.escape_html(repo)}</code>",
        f"自动检查: <code>{'开' if enabled else '关'}</code> · 间隔 <code>{interval}s</code> · "
        f"含预发布: <code>{'是' if include_pre else '否'}</code>",
        f"自动更新: <code>{'开' if auto_update else '关'}</code>",
        f"已忽略: <code>{', '.join(ignored) if ignored else '无'}</code>",
        "",
    ]

    # 自更新进行中的状态提示
    su = updater.load_state()
    su_stage = su.get("stage")
    if su_stage and su_stage not in (updater.STAGE_IDLE,):
        stage_label = {
            updater.STAGE_BACKING_UP: "📦 备份中",
            updater.STAGE_PULLING: "⬇️ 拉取中",
            updater.STAGE_STAGED: "⏸ 已就绪，等待确认重启",
            updater.STAGE_RESTARTING: "🔄 重启生效中",
            updater.STAGE_VERIFYING: "🔎 健康检查中",
            updater.STAGE_SUCCESS: "✅ 上次更新成功",
            updater.STAGE_FAILED: "❌ 上次更新失败",
            updater.STAGE_ROLLED_BACK: "↩️ 上次更新已回滚",
        }.get(su_stage, su_stage)
        lines.append(f"<b>更新状态</b>: {stage_label}")
        if su.get("message"):
            lines.append(f"  <i>{ui.escape_html(str(su.get('message')))}</i>")
        lines.append("")

    if not latest:
        lines.append("ℹ 暂无 release 数据；点「🔄 立即检查」拉取。")
    else:
        is_newer = update_checker._has_newer(latest)
        head = "🟢 有新版本" if is_newer and latest not in set(ignored) else (
               "🔕 新版本已忽略" if is_newer else "✅ 已是最新")
        lines.append(f"<b>最新 release</b>: <code>{ui.escape_html(latest)}</code>"
                     f"{' (pre-release)' if latest_pre else ''} — {head}")
        if latest_name:
            lines.append(f"标题: {ui.escape_html(latest_name)}")
        if latest_pub:
            lines.append(f"发布: <code>{ui.escape_html(latest_pub)}</code>")
        if latest_body:
            lines.append("")
            lines.append("<b>Changelog:</b>")
            lines.append(ui.escape_html(latest_body))
        if latest_url:
            lines.append("")
            lines.append(f"🔗 <code>{ui.escape_html(latest_url)}</code>")

    return "\n".join(lines)


def _format_kb() -> dict:
    cfg = _cfg()
    enabled = bool(cfg.get("enabled", True))
    include_pre = bool(cfg.get("includePrerelease", True))
    auto_update = bool(cfg.get("autoUpdate", False))
    interval = int(cfg.get("intervalSeconds", 3600) or 3600)
    ignored = set(cfg.get("ignoredVersions") or [])
    st = update_checker.get_cached() or {}
    latest = st.get("latest_version")

    rows: list[list[dict]] = []

    # ── 自更新行（最显眼，放最上）──
    su = updater.load_state()
    su_stage = su.get("stage")
    if su_stage == updater.STAGE_STAGED:
        # staged 态：二次确认
        rows.append([
            ui.btn("✅ 确认重启生效", "upd:confirm_restart"),
            ui.btn("↩️ 取消并回滚", "upd:cancel_staged"),
        ])
    elif su_stage in (updater.STAGE_BACKING_UP, updater.STAGE_PULLING,
                      updater.STAGE_RESTARTING, updater.STAGE_VERIFYING):
        rows.append([ui.btn("⏳ 更新进行中…", "upd:noop")])
    elif latest and update_checker._has_newer(latest) and latest not in ignored:
        # 有新版且未忽略：可一键更新
        rows.append([ui.btn(f"🚀 立即更新到 {latest}", f"upd:do_update:{latest}")])

    rows.append([
        ui.btn(f"{'🟢 自动检查: 开' if enabled else '🔴 自动检查: 关'}", "upd:toggle_enabled"),
        ui.btn(f"{'🧪 含预发布' if include_pre else '🚫 不含预发布'}", "upd:toggle_pre"),
    ])
    rows.append([
        ui.btn(f"{'🤖 自动更新: 开' if auto_update else '🚦 自动更新: 关'}", "upd:toggle_auto"),
        ui.btn(f"⏱ 间隔: {interval}s", "upd:edit_interval"),
    ])
    rows.append([
        ui.btn("🔄 立即检查", "upd:refresh"),
        ui.btn("🗂 备份列表", "upd:backups"),
    ])
    rows.append([ui.btn("📋 更新日志", "upd:faillog")])
    if latest and update_checker._has_newer(latest):
        if latest in ignored:
            rows.append([ui.btn(f"✅ 取消忽略 {latest}", f"upd:unignore:{latest}")])
        else:
            rows.append([ui.btn(f"🔕 忽略 {latest}", f"upd:ignore:{latest}")])
    if ignored:
        rows.append([ui.btn(f"🧹 清空忽略列表（{len(ignored)}）", "upd:clear_ignored")])
    rows.append([ui.btn("◀ 返回设置", "menu:settings"), ui.btn("🏠 主菜单", "menu:main")])
    return ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, ui.truncate(_format_main_text()), reply_markup=_format_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, ui.truncate(_format_main_text()), reply_markup=_format_kb())


# ─── 设置类操作 ──────────────────────────────────────────────────

def _toggle_enabled(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool(_cfg().get("enabled", True))
    _update_cfg({"enabled": not cur})
    ui.answer_cb(cb_id, "已关闭" if cur else "已开启")
    show(chat_id, message_id)


def _toggle_pre(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool(_cfg().get("includePrerelease", True))
    _update_cfg({"includePrerelease": not cur})
    ui.answer_cb(cb_id, "已切换")
    show(chat_id, message_id)


def _toggle_auto(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool(_cfg().get("autoUpdate", False))
    _update_cfg({"autoUpdate": not cur})
    ui.answer_cb(cb_id, "自动更新已开启" if not cur else "自动更新已关闭")
    show(chat_id, message_id)


def _edit_interval(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "upd_interval")
    ui.edit(
        chat_id, message_id,
        "请输入检查间隔（秒，≥300=5 分钟，推荐 3600=1 小时）：\n\n例：<code>3600</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "menu:update")]]),
    )


def _on_interval_input(chat_id: int, text: str) -> None:
    try:
        v = int((text or "").strip())
        if v < 300:
            raise ValueError
    except ValueError:
        ui.send(chat_id, "❌ 需要 ≥300 的整数（最小 5 分钟），请重新输入：")
        return
    _update_cfg({"intervalSeconds": v})
    states.pop_state(chat_id)
    ui.send(chat_id, f"✅ 检查间隔已更新为 <code>{v}s</code>")
    send_new(chat_id)


def _refresh(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "拉取中…")
    try:
        update_checker.force_refresh_sync()
    except Exception as exc:
        ui.send(chat_id, f"❌ 拉取失败: <code>{ui.escape_html(str(exc))}</code>")
        return
    show(chat_id, message_id)


def _ignore_version(chat_id: int, message_id: int, cb_id: str, version: str) -> None:
    if not version:
        ui.answer_cb(cb_id, "版本号为空")
        return
    update_checker.add_ignored(version)
    ui.answer_cb(cb_id, f"已忽略 {version}")
    show(chat_id, message_id)


def _unignore_version(chat_id: int, message_id: int, cb_id: str, version: str) -> None:
    if not version:
        ui.answer_cb(cb_id, "版本号为空")
        return
    update_checker.remove_ignored(version)
    ui.answer_cb(cb_id, f"已取消忽略 {version}")
    show(chat_id, message_id)


def _clear_ignored(chat_id: int, message_id: int, cb_id: str) -> None:
    update_checker.clear_ignored()
    ui.answer_cb(cb_id, "已清空")
    show(chat_id, message_id)


def _backups(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    backups = updater.list_backups()
    if not backups:
        text = "🗂 <b>备份列表</b>\n\n暂无备份。"
    else:
        lines = ["🗂 <b>备份列表</b>（最近在前）", ""]
        for b in backups[:10]:
            ref = b.get("ref", "?")
            ver = b.get("version", "?")
            tgt = b.get("target_tag", "?")
            mode = b.get("mode", "?")
            lines.append(f"• <code>{ui.escape_html(ref)}</code>")
            lines.append(f"  v{ui.escape_html(ver)} → {ui.escape_html(tgt)} ({mode})")
        text = "\n".join(lines)
    ui.edit(chat_id, message_id, ui.truncate(text),
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "menu:update")]]))


def _faillog(chat_id: int, message_id: int, cb_id: str) -> None:
    """展示最近一次更新的失败日志。"""
    ui.answer_cb(cb_id)
    log = updater.get_update_log()
    if not log.strip():
        text = "📋 <b>更新日志</b>\n\n暂无日志记录。"
    else:
        text = f"📋 <b>最近更新日志</b>\n\n<pre>{ui.escape_html(log)}</pre>"
    ui.edit(chat_id, message_id, ui.truncate(text),
            reply_markup=ui.inline_kb([
                [ui.btn("🔄 刷新", "upd:faillog"), ui.btn("◀ 返回", "menu:update")],
            ]))


# ─── 自更新交互（双重确认）──────────────────────────────────────

def _do_update(chat_id: int, message_id: int, cb_id: str, version: str) -> None:
    """点「🚀 立即更新」→ 第一阶段：备份 + 拉取（在 staged 前完成）。"""
    if not version:
        ui.answer_cb(cb_id, "版本号为空")
        return
    if updater.is_busy():
        ui.answer_cb(cb_id, "已有更新进行中", show_alert=True)
        show(chat_id, message_id)
        return
    # 一阶确认弹窗
    ui.answer_cb(cb_id)
    text = (
        f"🚀 <b>确认更新到 {ui.escape_html(version)}？</b>\n\n"
        f"当前: <code>v{__version__}</code>\n"
        f"流程：① 强制备份 → ② 拉取新版本 → ③ <b>停下等你二次确认</b> → ④ 重启生效\n\n"
        f"第一步只做备份和拉取，<b>不会重启</b>，可随时取消。"
    )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("✅ 开始备份并拉取", f"upd:stage:{version}")],
        [ui.btn("❌ 取消", "menu:update")],
    ]))


def _stage(chat_id: int, message_id: int, cb_id: str, version: str) -> None:
    """一阶确认后：执行备份 + 拉取。进度回填到同一条消息。"""
    ui.answer_cb(cb_id, "开始更新…")

    # 注册进度回调：把每个阶段 edit 到当前消息
    def _progress(stage: str, text: str) -> None:
        try:
            kb = None
            if stage == updater.STAGE_STAGED:
                kb = ui.inline_kb([
                    [ui.btn("✅ 确认重启生效", "upd:confirm_restart"),
                     ui.btn("↩️ 取消并回滚", "upd:cancel_staged")],
                ])
            ui.edit(chat_id, message_id, ui.truncate(f"🚀 <b>更新到 {ui.escape_html(version)}</b>\n\n{text}"),
                    reply_markup=kb)
        except Exception:
            pass

    updater.set_progress_callback(_progress)
    try:
        ok, detail = updater.stage_update(version, chat_id=chat_id, notify_msg_id=message_id)
    finally:
        # 清掉回调，避免后续 resume/其它流程的 _emit 误 edit 这条旧消息
        updater.set_progress_callback(None)
    if not ok:
        ui.edit(chat_id, message_id,
                ui.truncate(f"❌ 更新未完成：{ui.escape_html(detail)}"),
                reply_markup=ui.inline_kb([
                    [ui.btn("📋 查看失败日志", "upd:faillog")],
                    [ui.btn("◀ 返回", "menu:update")],
                ]))


def _confirm_restart(chat_id: int, message_id: int, cb_id: str) -> None:
    """二阶确认：用户确认重启生效。"""
    ui.answer_cb(cb_id, "正在重启…")
    st = updater.load_state()
    if st.get("stage") != updater.STAGE_STAGED:
        ui.edit(chat_id, message_id, "⚠️ 当前不在待确认状态，可能已重启或已取消。",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "menu:update")]]))
        return
    # 记录通知消息位置，重启后由 resume 流程 edit 它
    updater.save_state(chat_id=chat_id, notify_msg_id=message_id)
    ui.edit(chat_id, message_id,
            ui.truncate("🔄 <b>正在重启生效…</b>\n\n稍候将自动回填健康检查结果。"),
            reply_markup=None)
    ok, detail = updater.confirm_restart()
    if not ok:
        ui.send(chat_id, f"❌ 重启触发失败：<code>{ui.escape_html(detail)}</code>")


def _cancel_staged(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id, "正在取消…")
    ok, detail = updater.cancel_staged()
    ui.edit(chat_id, message_id,
            ui.truncate(f"↩️ <b>已取消更新</b>\n\n{ui.escape_html(detail)}"),
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回", "menu:update")]]))


# ─── 路由 ─────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:update":
        show(chat_id, message_id, cb_id); return True
    if data == "upd:noop":
        ui.answer_cb(cb_id, "更新进行中，请稍候"); return True
    if data == "upd:toggle_enabled":
        _toggle_enabled(chat_id, message_id, cb_id); return True
    if data == "upd:toggle_pre":
        _toggle_pre(chat_id, message_id, cb_id); return True
    if data == "upd:toggle_auto":
        _toggle_auto(chat_id, message_id, cb_id); return True
    if data == "upd:edit_interval":
        _edit_interval(chat_id, message_id, cb_id); return True
    if data == "upd:refresh":
        _refresh(chat_id, message_id, cb_id); return True
    if data == "upd:backups":
        _backups(chat_id, message_id, cb_id); return True
    if data == "upd:faillog":
        _faillog(chat_id, message_id, cb_id); return True
    if data.startswith("upd:do_update:"):
        _do_update(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("upd:stage:"):
        _stage(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "upd:confirm_restart":
        _confirm_restart(chat_id, message_id, cb_id); return True
    if data == "upd:cancel_staged":
        _cancel_staged(chat_id, message_id, cb_id); return True
    if data.startswith("upd:ignore:"):
        _ignore_version(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("upd:unignore:"):
        _unignore_version(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data == "upd:clear_ignored":
        _clear_ignored(chat_id, message_id, cb_id); return True
    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "upd_interval":
        _on_interval_input(chat_id, text); return True
    return False
