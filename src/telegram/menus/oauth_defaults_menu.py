"""OAuth 默认模型配置菜单 (一级页面 + 两个编辑入口)。

配置字段:
  - Anthropic OAuth → cfg["oauthDefaultModels"] (顶层 list[str])
  - OpenAI    OAuth → cfg["oauth"]["providers"]["openai"]["defaultModels"]

语义: OAuth 账户 entry 未手动填 models 时的回落列表。改完走 `config.update`
自动触发 registry 重建, 热生效。

callback_data 前缀: `odm:...`
状态机 action: `odm_edit:<family>` where family ∈ {anthropic, openai}
"""

from __future__ import annotations

from typing import Any

from ... import config
from .. import states, ui


_FAMILIES: tuple[str, ...] = ("anthropic", "openai")

_FAM_LABEL = {
    "anthropic": "Anthropic OAuth",
    "openai":    "OpenAI OAuth",
}
_FAM_ICON = {
    "anthropic": "🅰",
    "openai":    "🅞",
}


# ─── 读写底层 ────────────────────────────────────────────────────

def _read_list(family: str) -> list[str]:
    cfg = config.get()
    if family == "anthropic":
        raw = cfg.get("oauthDefaultModels") or []
    else:
        raw = ((cfg.get("oauth") or {}).get("providers") or {}).get("openai", {}).get("defaultModels") or []
    return [str(x) for x in raw if isinstance(x, str) and x.strip()]


def _write_list(family: str, models: list[str]) -> None:
    def _mutate(cfg: dict) -> None:
        if family == "anthropic":
            cfg["oauthDefaultModels"] = list(models)
        else:
            oauth = cfg.setdefault("oauth", {})
            providers = oauth.setdefault("providers", {})
            openai_cfg = providers.setdefault("openai", {})
            openai_cfg["defaultModels"] = list(models)
    config.update(_mutate)


def _parse_input(text: str) -> list[str]:
    """把用户输入的字符串解析成模型列表。

    支持 ',' / '，' / 换行 / 空白 作为分隔符, 去空 + 保持原顺序去重。
    """
    if not text:
        return []
    # 先把各种分隔符统一成逗号, 再 split
    normalized = (
        text.replace("，", ",")
            .replace(";", ",")
            .replace("；", ",")
            .replace("\n", ",")
            .replace("\t", ",")
    )
    parts = [p.strip() for p in normalized.split(",")]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


# ─── Level 1 总览 ─────────────────────────────────────────────────

def _overview_text() -> str:
    lines = [
        "🧩 <b>OAuth 默认模型配置</b>",
        "",
        "<i>OAuth 账户 entry 未填写 <code>models</code> 时回落到这里。改动热生效, 无需重启。</i>",
        "",
    ]
    for fam in _FAMILIES:
        icon = _FAM_ICON[fam]
        label = _FAM_LABEL[fam]
        models = _read_list(fam)
        lines.append(f"{icon} <b>{label}</b> ({len(models)}):")
        if models:
            # 用 <code> 块, 单击即可整段复制
            joined = ", ".join(ui.escape_html(m) for m in models)
            lines.append(f"<code>{joined}</code>")
        else:
            lines.append("<i>(空)</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def _overview_kb() -> dict:
    return ui.inline_kb([
        [ui.btn("✏ 修改 Anthropic", "odm:edit:anthropic"),
         ui.btn("✏ 修改 OpenAI",    "odm:edit:openai")],
        [ui.btn("◀ 返回主菜单", "menu:main")],
    ])


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _overview_text(), reply_markup=_overview_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, _overview_text(), reply_markup=_overview_kb())


# ─── Level 2 编辑页 (进入状态机, 等待文本输入) ────────────────────

def _start_edit(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    if family not in _FAMILIES:
        ui.answer_cb(cb_id, "未知家族")
        return
    ui.answer_cb(cb_id)
    states.set_state(chat_id, f"odm_edit:{family}")
    current = _read_list(family)
    current_line = ", ".join(current) if current else "(空)"
    icon = _FAM_ICON[family]
    label = _FAM_LABEL[family]
    text = (
        f"✏ <b>修改 {icon} {label} 默认模型</b>\n\n"
        f"当前列表 ({len(current)}, 点击可复制作为起点):\n"
        f"<code>{ui.escape_html(current_line)}</code>\n\n"
        "请直接发送<b>新的模型列表</b>:\n"
        "  • 用英文逗号 <code>,</code> 或换行分隔多个模型名\n"
        "  • 前后空白会自动忽略、重复自动去重\n"
        "  • 发送 <code>-</code> 或 <code>empty</code> 则清空为 <code>[]</code>\n\n"
        "<i>提示: 发送消息即保存 — 没有额外的保存按钮。</i>"
    )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("❌ 取消", "odm:show")],
    ]))


def _on_edit_input(chat_id: int, action: str, text: str) -> None:
    """状态机回调: 用户发来新列表文本。action = odm_edit:<family>"""
    parts = action.split(":", 1)
    if len(parts) < 2:
        states.pop_state(chat_id); return
    family = parts[1]
    if family not in _FAMILIES:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 会话异常, 请重新进入菜单")
        return

    raw = (text or "").strip()
    # 允许清空
    if raw.lower() in ("-", "empty", "空", "清空"):
        models: list[str] = []
    else:
        models = _parse_input(raw)

    # 简单长度保护 (防止一次粘贴几千个)
    if len(models) > 200:
        ui.send(chat_id, f"❌ 列表过长 ({len(models)} 项), 最多 200 个模型。请精简后重发:")
        return

    # 校验每个模型名: 不允许空格 / 反斜杠 / 控制字符
    for m in models:
        if any(c in m for c in ("\\", " ", "\x00")):
            ui.send(
                chat_id,
                f"❌ 非法模型名: <code>{ui.escape_html(m)}</code>"
                " (不能含空格 / 反斜杠 / 控制字符)。请重新输入:",
            )
            return

    _write_list(family, models)
    states.pop_state(chat_id)

    icon = _FAM_ICON[family]
    label = _FAM_LABEL[family]
    if models:
        preview = ", ".join(ui.escape_html(m) for m in models)
        body = f"<code>{preview}</code>"
    else:
        body = "<i>(已清空为 [])</i>"
    ui.send_result(
        chat_id,
        f"✅ 已保存 {icon} <b>{label}</b> 默认模型 ({len(models)} 项):\n\n{body}\n\n"
        "<i>热生效 — 现有 OAuth 渠道实例已重建。</i>",
        back_label="◀ 返回 OAuth 默认",
        back_callback="odm:show",
    )


# ─── 路由 ─────────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str,
                    data: str) -> bool:
    if not data.startswith("odm:"):
        return False
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "show":
        show(chat_id, message_id, cb_id)
        return True
    if action == "edit":
        family = parts[2] if len(parts) > 2 else ""
        _start_edit(chat_id, message_id, cb_id, family)
        return True
    ui.answer_cb(cb_id, "未知操作")
    return True


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if not action.startswith("odm_edit:"):
        return False
    _on_edit_input(chat_id, action, text)
    return True
