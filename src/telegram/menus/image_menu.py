"""GPT / Codex 图片生成设置菜单。

callback_data 前缀：`img:...`
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ... import config, image_db
from ...openai import images_simple
from .. import states, ui


def _fmt_bytes(n) -> str:
    try:
        n = float(n or 0)
    except Exception:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    if i == 0:
        return f"{int(n)}B"
    return f"{n:.1f}{units[i]}"


def _cfg() -> dict:
    return images_simple.settings()


def _render() -> tuple[str, dict]:
    c = _cfg()
    disabled = c.get("disabledAccounts") or []
    lines = [
        "🖼 <b>GPT / Codex 图片设置</b>",
        "",
        f"功能状态: {'✅ 启用' if c.get('enabled', True) else '❌ 停用'}",
        f"主模型: <code>{ui.escape_html(c.get('mainModel'))}</code>",
        f"图片模型: <code>{ui.escape_html(c.get('toolModel'))}</code>",
        f"禁用账号: <b>{len(disabled)}</b> 个",
        f"媒体缓存: {'✅ 开启' if c.get('cacheEnabled') else '❌ 关闭'}（GPT/Grok 图片 + Grok 视频）",
        f"缓存路径: <code>{ui.escape_html(c.get('cachePath'))}</code>",
        f"缓存保留: <code>{int(c.get('cacheRetentionDays') or 0)}</code> 天（0=永久）",
        f"缓存上限: <code>{_fmt_bytes(c.get('cacheMaxBytes') or 0)}</code>（0=不限）",
        "",
        "<i>GPT 与 Grok 的图片/视频统计、账号排行和任务详情已统一到多媒体日志。</i>",
    ]
    rows = [
        [ui.btn("✅/❌ 启停功能", "img:toggle"), ui.btn("🔁 媒体缓存开关", "img:cache_toggle")],
        [ui.btn("🧠 修改主模型", "img:set_main"), ui.btn("🎨 修改图片模型", "img:set_tool")],
        [ui.btn("🚫 禁用账号", "img:accounts"), ui.btn("📁 缓存路径", "img:set_path")],
        [ui.btn("🗓 保留天数", "img:set_retention"), ui.btn("💾 缓存上限", "img:set_max")],
        [ui.btn("🎞 查看多媒体日志", "media:logs")],
        [ui.btn("◀ 返回 OAuth 设置", "oa:settings"), ui.btn("🏠 主菜单", "menu:main")],
    ]
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _render()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _render()
    ui.send(chat_id, text, reply_markup=kb)


def _mutate_images(fn) -> None:
    def m(cfg):
        img = cfg.setdefault("images", {})
        fn(img)
    config.update(m)


def on_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    _mutate_images(lambda img: img.__setitem__("enabled", not bool(img.get("enabled", True))))
    show(chat_id, message_id, cb_id)


def on_cache_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    _mutate_images(lambda img: img.__setitem__("cacheEnabled", not bool(img.get("cacheEnabled", False))))
    show(chat_id, message_id, cb_id)


def _ask(chat_id: int, message_id: int, cb_id: str, action: str, text: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, action)
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "img:show")]]))


def on_set_main(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(chat_id, message_id, cb_id, "img_set_main", "请输入图片生成使用的主模型名称：\n\n默认：<code>gpt-5.4-mini</code>")


def on_set_tool(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(chat_id, message_id, cb_id, "img_set_tool", "请输入 image_generation 工具模型名称：\n\n默认：<code>gpt-image-2</code>")


def on_set_path(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(
        chat_id, message_id, cb_id, "img_set_path",
        "请输入图片/视频缓存路径：\n\n"
        "• 相对路径会放在 Parrot 运行数据目录下，例如 <code>images</code>\n"
        "• 绝对路径会按原样使用\n"
        "• 文件名由系统生成，不会使用用户输入拼路径",
    )


def on_set_retention(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(chat_id, message_id, cb_id, "img_set_retention", "请输入缓存保留天数：\n\n<code>0</code> = 永久保存，不按时间清理。")


def on_set_max(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(
        chat_id, message_id, cb_id, "img_set_max",
        "请输入缓存空间上限，单位可以是 B / KB / MB / GB：\n\n"
        "例如：<code>1GB</code>、<code>500MB</code>、<code>0</code>。\n"
        "<code>0</code> = 不按空间清理；超过上限时会删除最老的约 20% 媒体文件。",
    )


def _parse_bytes(s: str) -> int:
    raw = (s or "").strip().upper().replace(" ", "")
    if raw in ("", "0"):
        return 0
    mult = 1
    for suf, m in (("GB", 1024**3), ("G", 1024**3), ("MB", 1024**2), ("M", 1024**2), ("KB", 1024), ("K", 1024), ("B", 1)):
        if raw.endswith(suf):
            mult = m
            raw = raw[: -len(suf)]
            break
    return int(float(raw) * mult)


def on_accounts(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    rows = []
    lines = ["🚫 <b>图片禁用账号</b>", "", "点击账号切换图片模块禁用状态；这里只影响图片生成/编辑，不影响普通 API。", ""]
    accounts = images_simple.list_image_accounts(include_disabled=True)
    if not accounts:
        lines.append(f"暂无 {ui.provider_tag('openai')} OAuth 账号。")
    for row in accounts:
        ak = row["account_key"]
        email = row.get("email") or ak
        mark = "☑ 禁用" if row.get("image_disabled") else "☐ 可用"
        extra = ""
        if not row.get("enabled"):
            extra = "（OAuth 已停用）"
        elif row.get("missing_account_id"):
            extra = "（缺 account_id）"
        elif row.get("image_cooldown_until"):
            extra = "（图片冷却中）"
        lines.append(f"{mark} <code>{ui.escape_html(email)}</code>{extra}")
        short = ui.register_code(f"imgacc:{ak}")
        rows.append([ui.btn(f"{mark} {email}", f"img:acc_toggle:{short}")])
    rows.append([ui.btn("◀ 返回 GPT 图片设置", "img:show")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def on_account_toggle(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    full = ui.resolve_code(short) or ""
    if not full.startswith("imgacc:"):
        ui.answer_cb(cb_id, "已过期")
        on_accounts(chat_id, message_id, cb_id)
        return
    ak = full[len("imgacc:"):]
    def m(img):
        vals = list(img.get("disabledAccounts") or [])
        low = {str(x).lower(): i for i, x in enumerate(vals)}
        if ak.lower() in low:
            vals.pop(low[ak.lower()])
        else:
            vals.append(ak)
        img["disabledAccounts"] = vals
    _mutate_images(m)
    on_accounts(chat_id, message_id, cb_id)


def on_view_image(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    full = ui.resolve_code(short) or ""
    if not full.startswith("imglog:"):
        ui.answer_cb(cb_id, "日志按钮已过期")
        return
    try:
        log_id = int(full[len("imglog:"):])
    except Exception:
        ui.answer_cb(cb_id, "日志无效")
        return
    row = image_db.get_log(log_id)
    if not row:
        ui.answer_cb(cb_id, "日志不存在")
        return
    try:
        paths = json.loads(row.get("cache_paths") or "[]")
    except Exception:
        paths = []
    paths = [p for p in paths if isinstance(p, str) and os.path.exists(p)]
    if not paths:
        ui.answer_cb(cb_id, "图片缓存不存在或已清理", show_alert=True)
        return
    ui.answer_cb(cb_id, "正在发送图片…")
    for p in paths[:5]:
        ui.send_photo(
            chat_id, p,
            caption=(
                f"🖼 图片日志 #{row.get('id')} · {'生成' if row.get('action') == 'generate' else '编辑'}\n"
                f"账号: <code>{ui.escape_html(row.get('account_email') or '?')}</code>"
            ),
        )


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data in ("img:show", "menu:images"):
        show(chat_id, message_id, cb_id)
        return True
    if data == "img:toggle":
        on_toggle(chat_id, message_id, cb_id); return True
    if data == "img:cache_toggle":
        on_cache_toggle(chat_id, message_id, cb_id); return True
    if data == "img:set_main":
        on_set_main(chat_id, message_id, cb_id); return True
    if data == "img:set_tool":
        on_set_tool(chat_id, message_id, cb_id); return True
    if data == "img:set_path":
        on_set_path(chat_id, message_id, cb_id); return True
    if data == "img:set_retention":
        on_set_retention(chat_id, message_id, cb_id); return True
    if data == "img:set_max":
        on_set_max(chat_id, message_id, cb_id); return True
    if data == "img:accounts":
        on_accounts(chat_id, message_id, cb_id); return True
    if data.startswith("img:acc_toggle:"):
        on_account_toggle(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    if data.startswith("img:view:"):
        on_view_image(chat_id, message_id, cb_id, data.split(":", 2)[2]); return True
    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    val = (text or "").strip()
    if action == "img_set_main":
        if not val:
            ui.send(chat_id, "❌ 模型名不能为空，请重新输入：")
            return True
        _mutate_images(lambda img: img.__setitem__("mainModel", val))
        states.pop_state(chat_id)
        ui.send_result(chat_id, f"✅ 主模型已更新为 <code>{ui.escape_html(val)}</code>", back_label="◀ 返回 GPT 图片设置", back_callback="img:show")
        return True
    if action == "img_set_tool":
        if not val:
            ui.send(chat_id, "❌ 模型名不能为空，请重新输入：")
            return True
        _mutate_images(lambda img: img.__setitem__("toolModel", val))
        states.pop_state(chat_id)
        ui.send_result(chat_id, f"✅ 图片模型已更新为 <code>{ui.escape_html(val)}</code>", back_label="◀ 返回 GPT 图片设置", back_callback="img:show")
        return True
    if action == "img_set_path":
        if not val:
            ui.send(chat_id, "❌ 路径不能为空，请重新输入：")
            return True
        # 只做基本路径规范化；真正写文件时仍由 images_simple 用安全文件名写入。
        _mutate_images(lambda img: img.__setitem__("cachePath", val))
        states.pop_state(chat_id)
        ui.send_result(chat_id, f"✅ 缓存路径已更新为 <code>{ui.escape_html(val)}</code>", back_label="◀ 返回 GPT 图片设置", back_callback="img:show")
        return True
    if action == "img_set_retention":
        try:
            days = int(val)
            if days < 0:
                raise ValueError
        except Exception:
            ui.send(chat_id, "❌ 请输入非负整数天数，例如 <code>0</code> 或 <code>30</code>：")
            return True
        _mutate_images(lambda img: img.__setitem__("cacheRetentionDays", days))
        states.pop_state(chat_id)
        ui.send_result(chat_id, f"✅ 缓存保留天数已更新为 <code>{days}</code>", back_label="◀ 返回 GPT 图片设置", back_callback="img:show")
        return True
    if action == "img_set_max":
        try:
            n = _parse_bytes(val)
            if n < 0:
                raise ValueError
        except Exception:
            ui.send(chat_id, "❌ 格式不对，请输入如 <code>1GB</code> / <code>500MB</code> / <code>0</code>：")
            return True
        _mutate_images(lambda img: img.__setitem__("cacheMaxBytes", n))
        states.pop_state(chat_id)
        ui.send_result(chat_id, f"✅ 缓存空间上限已更新为 <code>{_fmt_bytes(n)}</code>", back_label="◀ 返回 GPT 图片设置", back_callback="img:show")
        return True
    return False
