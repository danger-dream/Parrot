"""OAuth 默认模型配置菜单 (一级页面 + 两个编辑入口 + 引用扫描确认页)。

配置字段:
  - Anthropic OAuth → cfg["oauthDefaultModels"] (顶层 list[str])
  - OpenAI    OAuth → cfg["openaiOAuth"]["defaultModels"]
  - Grok/xAI  OAuth → cfg["xaiOAuth"]["defaultModels"]

语义: OAuth 账户 entry 未手动填 models 时的回落列表。改完走 `config.update`
自动触发 registry 重建, 热生效。

⚠ 删除模型的安全保护:
  保存前扫描 3 个位置对"被删模型"的引用:
    1. apiKeys[*].allowedModels    API Key 白名单
    2. modelMapping[*][alias]=real 别名映射的 value 侧
    3. ingressDefaultModel[*]      入口默认模型
  有引用时弹确认页:
    ✅ 继续保存 (保留引用, 用户请求可能 503)
    🧹 保存并清理全部引用
       - 清 API Key: 删除白名单里这些模型, 但若会清空则跳过(避免语义从
         "只允许 X" 变成 "无限制"); UI 明确告知
       - 清映射: 删除别名条目
       - 清默认: 清除 ingressDefaultModel[line]

callback_data 前缀: `odm:...`
状态机 action: `odm_edit:<family>`
pending 保存: cfg["_odm_pending"]["<family>"] = {"new": [...], "old": [...]}
  (下划线前缀 → 配置 sanitize 时如果需要可剥离; 当前版本不在 save/load
  做 sanitize, 但保存策略仍在持久前 pop)
"""

from __future__ import annotations

import json

from ... import config
from .. import states, ui


_FAMILIES: tuple[str, ...] = ("anthropic", "openai", "xai")

_FAM_LABEL = {
    "anthropic": "Anthropic OAuth",
    "openai":    "OpenAI OAuth",
    "xai":       "Grok OAuth",
}
_FAM_ICON = {
    "anthropic": ui.provider_icon("claude"),
    "openai":    ui.provider_icon("openai"),
    "xai":       ui.provider_icon("xai"),
}

_FAM_PROVIDER = {
    "anthropic": "claude",
    "openai":    "openai",
    "xai":       "xai",
}


def _fam_body_label(family: str, *, bold: bool = True) -> str:
    icon = ui.provider_custom_emoji_html(_FAM_PROVIDER.get(family, family))
    label = ui.escape_html(_FAM_LABEL.get(family, family))
    return f"{icon} <b>{label}</b>" if bold else f"{icon} {label}"


def _ingress_body_label(ingress: str) -> str:
    if ingress == "anthropic":
        return f"{ui.provider_custom_emoji_html('claude')} Anthropic (/v1/messages)"
    if ingress == "openai-chat":
        return f"{ui.provider_custom_emoji_html('openai')}/{ui.provider_custom_emoji_html('xai')} OpenAI & Grok Chat (/v1/chat/completions)"
    if ingress == "openai-responses":
        return f"{ui.provider_custom_emoji_html('openai')}/{ui.provider_custom_emoji_html('xai')} OpenAI & Grok Responses (/v1/responses)"
    return ui.escape_html(ingress)


# ─── 读写底层 ────────────────────────────────────────────────────

def _read_list(family: str) -> list[str]:
    cfg = config.get()
    if family == "anthropic":
        raw = cfg.get("oauthDefaultModels") or []
    elif family == "xai":
        raw = (cfg.get("xaiOAuth") or {}).get("defaultModels") or []
    else:
        raw = (cfg.get("openaiOAuth") or {}).get("defaultModels") or []
    return [str(x) for x in raw if isinstance(x, str) and x.strip()]


def _write_list(family: str, models: list[str]) -> None:
    def _mutate(cfg: dict) -> None:
        if family == "anthropic":
            cfg["oauthDefaultModels"] = list(models)
        elif family == "xai":
            cfg.setdefault("xaiOAuth", {})["defaultModels"] = list(models)
        else:
            cfg.setdefault("openaiOAuth", {})["defaultModels"] = list(models)
    config.update(_mutate)


def _parse_input(text: str) -> list[str]:
    """把用户输入解析成模型列表。

    支持 ',' / '，' / ';' / '；' / 换行 / 制表符 作分隔。
    去空 + 保持原顺序去重。
    """
    if not text:
        return []
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


# ─── 引用扫描 ────────────────────────────────────────────────────

def _scan_references(family: str, removed: set[str]) -> dict:
    """扫描被删模型在配置中的引用。

    只关心与 `family` 相关的入口:
      anthropic → ingressDefaultModel["anthropic"] + modelMapping["anthropic"]
      openai/xai → ingressDefaultModel["openai-chat"/"openai-responses"]
                + modelMapping["openai-chat"/"openai-responses"]
    API Key 白名单本身无家族概念 — OpenAI & Grok 家族模型可能和 Anthropic 模型
    同名吗? 实践上不会(Claude vs GPT 名字不会碰撞), 但为求精确, 只在白名单里
    按 "模型名是否在 removed 集合内" 做命中, 不分家族。

    返回:
      {
        "apiKeys":  [{"name": "default-key", "hits": ["gpt-5.2-codex"]}, ...],
        "mappings": [{"ingress": "openai-chat", "alias": "gpt-5.5",
                      "real": "gpt-5.4"}, ...],
        "defaults": [{"ingress": "openai-chat", "value": "gpt-5.4"}, ...],
        "would_empty_keys": ["trial", ...]  # API Key 清理后会清空的名单
      }
    """
    cfg = config.get()
    # family -> 相关 ingress 集合
    fam_ingress = {
        "anthropic": {"anthropic"},
        "openai":    {"openai-chat", "openai-responses"},
        "xai":       {"openai-chat", "openai-responses"},
    }
    ingresses = fam_ingress.get(family, set())

    # 1) API Key 白名单
    api_key_hits: list[dict] = []
    would_empty: list[str] = []
    keys = cfg.get("apiKeys") or {}
    for name, entry in keys.items():
        if not isinstance(entry, dict):
            continue
        allowed = entry.get("allowedModels") or []
        if not isinstance(allowed, list) or not allowed:
            continue
        hits = sorted(m for m in allowed if m in removed)
        if hits:
            api_key_hits.append({"name": name, "hits": hits})
            # 清理后是否会清空?
            remaining = [m for m in allowed if m not in removed]
            if not remaining:
                would_empty.append(name)

    # 2) modelMapping value 侧
    mapping_hits: list[dict] = []
    mm = cfg.get("modelMapping") or {}
    for line in ingresses:
        line_map = mm.get(line) or {}
        for alias, real in sorted(line_map.items()):
            if isinstance(real, str) and real in removed:
                mapping_hits.append({
                    "ingress": line, "alias": alias, "real": real,
                })

    # 3) ingressDefaultModel
    default_hits: list[dict] = []
    idm = cfg.get("ingressDefaultModel") or {}
    for line in ingresses:
        v = idm.get(line)
        if isinstance(v, str) and v in removed:
            default_hits.append({"ingress": line, "value": v})

    return {
        "apiKeys":  api_key_hits,
        "mappings": mapping_hits,
        "defaults": default_hits,
        "would_empty_keys": would_empty,
    }


def _has_any_refs(refs: dict) -> bool:
    return bool(
        refs.get("apiKeys") or refs.get("mappings") or refs.get("defaults")
    )


# ─── 保存与清理 ──────────────────────────────────────────────────

_INGRESS_LABEL = {
    "anthropic":        "Anthropic (/v1/messages)",
    "openai-chat":      "OpenAI Chat (/v1/chat/completions)",
    "openai-responses": "OpenAI Responses (/v1/responses)",
}


def _commit_save(
    family: str, new_models: list[str], removed: set[str],
    *, cleanup: bool,
) -> dict:
    """一次 config.update 里原子完成: 写新 OAuth 默认 (+ 可选清理引用)。

    返回清理摘要(用于结果页展示):
      {
        "keys_cleaned": [{"name": "...", "removed": [...]}],
        "keys_skipped_empty": ["..."],
        "mappings_removed": [{"ingress": "...", "alias": "..."}],
        "defaults_cleared": ["..."],
      }
    """
    summary = {
        "keys_cleaned": [],
        "keys_skipped_empty": [],
        "mappings_removed": [],
        "defaults_cleared": [],
    }

    fam_ingress = {
        "anthropic": {"anthropic"},
        "openai":    {"openai-chat", "openai-responses"},
        "xai":       {"openai-chat", "openai-responses"},
    }
    ingresses = fam_ingress.get(family, set())

    def _mutate(cfg: dict) -> None:
        # a) 先写 OAuth 默认
        if family == "anthropic":
            cfg["oauthDefaultModels"] = list(new_models)
        elif family == "xai":
            cfg.setdefault("xaiOAuth", {})["defaultModels"] = list(new_models)
        else:
            cfg.setdefault("openaiOAuth", {})["defaultModels"] = list(new_models)

        if not cleanup or not removed:
            return

        # b) 清理 API Key 白名单 (避免清空 → 语义变成无限制)
        keys = cfg.get("apiKeys") or {}
        for name, entry in keys.items():
            if not isinstance(entry, dict):
                continue
            allowed = entry.get("allowedModels") or []
            if not isinstance(allowed, list) or not allowed:
                continue
            remaining = [m for m in allowed if m not in removed]
            cleaned_out = [m for m in allowed if m in removed]
            if not cleaned_out:
                continue
            if not remaining:
                # 清空会使白名单语义变成"无限制", 跳过
                summary["keys_skipped_empty"].append(name)
                continue
            entry["allowedModels"] = remaining
            summary["keys_cleaned"].append({
                "name": name, "removed": cleaned_out,
            })

        # c) 清理 modelMapping (value 侧命中就删整条)
        mm = cfg.get("modelMapping") or {}
        for line in ingresses:
            line_map = mm.get(line)
            if not isinstance(line_map, dict):
                continue
            for alias in list(line_map.keys()):
                real = line_map.get(alias)
                if isinstance(real, str) and real in removed:
                    del line_map[alias]
                    summary["mappings_removed"].append({
                        "ingress": line, "alias": alias,
                    })

        # d) 清理 ingressDefaultModel
        idm = cfg.get("ingressDefaultModel") or {}
        for line in ingresses:
            v = idm.get(line)
            if isinstance(v, str) and v in removed:
                del idm[line]
                summary["defaults_cleared"].append(line)

    config.update(_mutate)
    return summary


# ─── Level 1 总览 ─────────────────────────────────────────────────

def _overview_text() -> str:
    lines = [
        "🧩 <b>OAuth 默认模型配置</b>",
        "",
        "<i>OAuth 账户 entry 未填写 <code>models</code> 时回落到这里。改动热生效, 无需重启。</i>",
        "",
    ]
    for fam in _FAMILIES:
        models = _read_list(fam)
        lines.append(f"{_fam_body_label(fam)} ({len(models)}):")
        if models:
            joined = ", ".join(ui.escape_html(m) for m in models)
            lines.append(f"<code>{joined}</code>")
        else:
            lines.append("<i>(空)</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def _overview_kb() -> dict:
    return ui.inline_kb([
        [ui.btn(f"✏ 修改 {ui.provider_icon('claude')} Claude", "odm:edit:anthropic"),
         ui.btn(f"✏ 修改 {ui.provider_icon('openai')} OpenAI",    "odm:edit:openai")],
        [ui.btn(f"✏ 修改 {ui.provider_icon('xai')} Grok",      "odm:edit:xai")],
        [ui.btn("◀ 返回主菜单", "menu:main")],
    ])


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _overview_text(), reply_markup=_overview_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, _overview_text(), reply_markup=_overview_kb())


# ─── Level 2 编辑页 (进入状态机) ──────────────────────────────────

def _start_edit(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    if family not in _FAMILIES:
        ui.answer_cb(cb_id, "未知家族")
        return
    ui.answer_cb(cb_id)
    states.set_state(chat_id, f"odm_edit:{family}")
    current = _read_list(family)
    current_line = ", ".join(current) if current else "(空)"
    text = (
        f"✏ <b>修改</b> {_fam_body_label(family)} <b>默认模型</b>\n\n"
        f"当前列表 ({len(current)}, 点击可复制作为起点):\n"
        f"<code>{ui.escape_html(current_line)}</code>\n\n"
        "请直接发送<b>新的模型列表</b>:\n"
        "  • 用英文逗号 <code>,</code> 或换行分隔多个模型名\n"
        "  • 前后空白会自动忽略、重复自动去重\n"
        "  • 发送 <code>-</code> 或 <code>empty</code> 则清空为 <code>[]</code>\n\n"
        "<i>提示: 发送消息后若检测到删除, 会弹确认页告知你哪些 API Key / "
        "映射 / 默认模型会受影响。</i>"
    )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("❌ 取消", "oa:settings")],
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
    if raw.lower() in ("-", "empty", "空", "清空"):
        new_models: list[str] = []
    else:
        new_models = _parse_input(raw)

    if len(new_models) > 200:
        ui.send(chat_id,
                f"❌ 列表过长 ({len(new_models)} 项), 最多 200 个模型。"
                "请精简后重发:")
        return
    for m in new_models:
        if any(c in m for c in ("\\", " ", "\x00")):
            ui.send(
                chat_id,
                f"❌ 非法模型名: <code>{ui.escape_html(m)}</code>"
                " (不能含空格 / 反斜杠 / 控制字符)。请重新输入:",
            )
            return

    old_models = _read_list(family)
    removed = set(old_models) - set(new_models)

    # 无删除 → 直接保存, 跳过确认
    if not removed:
        _commit_save(family, new_models, set(), cleanup=False)
        states.pop_state(chat_id)
        _send_saved_result(chat_id, family, new_models, summary=None)
        return

    # 有删除 → 扫引用
    refs = _scan_references(family, removed)

    if not _has_any_refs(refs):
        # 无引用, 直接保存不弹确认
        _commit_save(family, new_models, removed, cleanup=False)
        states.pop_state(chat_id)
        _send_saved_result(chat_id, family, new_models, summary=None)
        return

    # 有引用 → 弹确认页, 把 pending 存进 cfg (可在重启/超时后恢复)
    pending_code = ui.register_code(
        "odm:pending:" + json.dumps({
            "family": family,
            "new":    new_models,
            "removed": sorted(removed),
        }, ensure_ascii=False)
    )
    states.pop_state(chat_id)  # 状态机结束, 用 pending_code 接力

    text = _render_confirm(family, new_models, removed, refs)
    kb = ui.inline_kb([
        [ui.btn("✅ 继续保存 (保留引用)",
                f"odm:commit:{pending_code}:keep")],
        [ui.btn("🧹 保存并清理全部引用",
                f"odm:commit:{pending_code}:clean")],
        [ui.btn("❌ 取消", "oa:settings")],
    ])
    ui.send(chat_id, text, reply_markup=kb)


def _render_confirm(
    family: str, new_models: list[str], removed: set[str], refs: dict,
) -> str:
    lines = [
        f"⚠ <b>确认保存</b> {_fam_body_label(family)} <b>默认模型</b>",
        "",
        f"即将移除 ({len(removed)} 项):",
    ]
    for m in sorted(removed):
        lines.append(f"  • <code>{ui.escape_html(m)}</code>")
    lines.append("")
    lines.append("⚡ <b>以下位置仍在引用这些模型</b>, 删除后用户请求")
    lines.append("   可能报 <code>503</code> (无渠道支持):")
    lines.append("")

    if refs["apiKeys"]:
        lines.append(f"🔑 <b>API Key 白名单</b> ({len(refs['apiKeys'])}):")
        would_empty_set = set(refs.get("would_empty_keys") or [])
        for row in refs["apiKeys"]:
            name = row["name"]
            hits = ", ".join(ui.escape_html(m) for m in row["hits"])
            warn = ""
            if name in would_empty_set:
                warn = " <i>(⚠ 清理后会清空; 跳过以保护权限)</i>"
            lines.append(
                f"  • <code>{ui.escape_html(name)}</code>: {hits}{warn}"
            )
        lines.append("")

    if refs["mappings"]:
        lines.append(f"🔁 <b>模型映射</b> ({len(refs['mappings'])}):")
        for row in refs["mappings"]:
            lines.append(
                f"  • {_ingress_body_label(row['ingress'])}: "
                f"<code>{ui.escape_html(row['alias'])}</code> → "
                f"<code>{ui.escape_html(row['real'])}</code>"
            )
        lines.append("")

    if refs["defaults"]:
        lines.append(f"🎯 <b>入口默认模型</b> ({len(refs['defaults'])}):")
        for row in refs["defaults"]:
            lines.append(
                f"  • {_ingress_body_label(row['ingress'])}: "
                f"<code>{ui.escape_html(row['value'])}</code>"
            )
        lines.append("")

    lines.append(
        "<i>注: 若第三方 API 渠道自己仍列出了同名模型, "
        "删除 OAuth 默认后请求依然可能走第三方渠道成功。</i>"
    )

    lines.append("")
    lines.append(f"新列表将保存为 ({len(new_models)} 项):")
    if new_models:
        joined = ", ".join(ui.escape_html(m) for m in new_models)
        lines.append(f"<code>{joined}</code>")
    else:
        lines.append("<i>(空)</i>")

    return "\n".join(lines)


def _send_saved_result(
    chat_id: int, family: str, new_models: list[str],
    summary: dict | None,
) -> None:
    parts = [f"✅ 已保存 {_fam_body_label(family)} 默认模型 "
             f"({len(new_models)} 项)"]
    if new_models:
        joined = ", ".join(ui.escape_html(m) for m in new_models)
        parts.append(f"<code>{joined}</code>")
    else:
        parts.append("<i>(已清空为 [])</i>")

    if summary:
        lines = []
        if summary["keys_cleaned"]:
            lines.append("")
            lines.append(f"🔑 清理 API Key 白名单 ({len(summary['keys_cleaned'])}):")
            for row in summary["keys_cleaned"]:
                removed_inline = ", ".join(
                    ui.escape_html(m) for m in row["removed"]
                )
                lines.append(
                    f"  • <code>{ui.escape_html(row['name'])}</code>"
                    f" 移除 {removed_inline}"
                )
        if summary["keys_skipped_empty"]:
            lines.append("")
            lines.append(
                f"⚠ 跳过 {len(summary['keys_skipped_empty'])} 个 API Key "
                "(清理会导致白名单清空 → 语义变无限制, 自动保留):"
            )
            for name in summary["keys_skipped_empty"]:
                lines.append(f"  • <code>{ui.escape_html(name)}</code>")
            lines.append(
                "<i>如需彻底禁用, 请到「🔑 管理 API Key」手动调整。</i>"
            )
        if summary["mappings_removed"]:
            lines.append("")
            lines.append(
                f"🔁 清理模型映射 ({len(summary['mappings_removed'])}):"
            )
            for row in summary["mappings_removed"]:
                lines.append(
                    f"  • {_INGRESS_LABEL.get(row['ingress'], row['ingress'])}:"
                    f" <code>{ui.escape_html(row['alias'])}</code>"
                )
        if summary["defaults_cleared"]:
            lines.append("")
            lines.append(
                f"🎯 清除入口默认 ({len(summary['defaults_cleared'])}):"
            )
            for ing in summary["defaults_cleared"]:
                lines.append(f"  • {_ingress_body_label(ing)}")
        if lines:
            parts.append("\n".join(lines))

    parts.append("")
    parts.append("<i>热生效 — 现有 OAuth 渠道实例已重建。</i>")
    ui.send_result(
        chat_id, "\n\n".join(parts),
        back_label="◀ 返回 OAuth 设置",
        back_callback="oa:settings",
    )


# ─── 确认页的 commit 回调 ────────────────────────────────────────

def _on_commit(
    chat_id: int, message_id: int, cb_id: str,
    pending_code: str, mode: str,
) -> None:
    if mode not in ("keep", "clean"):
        ui.answer_cb(cb_id, "未知模式"); return
    tag = ui.resolve_code(pending_code)
    if not tag or not tag.startswith("odm:pending:"):
        ui.answer_cb(cb_id, "会话已过期, 请重新操作"); return
    try:
        payload = json.loads(tag[len("odm:pending:"):])
    except Exception:
        ui.answer_cb(cb_id, "会话异常"); return

    family = payload.get("family")
    new_models = payload.get("new") or []
    removed = set(payload.get("removed") or [])
    if family not in _FAMILIES or not isinstance(new_models, list):
        ui.answer_cb(cb_id, "会话异常"); return

    summary = _commit_save(
        family, [str(m) for m in new_models], removed,
        cleanup=(mode == "clean"),
    )
    ui.answer_cb(cb_id, "✅ 已保存")
    # 删掉确认页消息, 重新发一条结果消息 (避免 edit 一条旧消息长度增长)
    try:
        ui.delete_message(chat_id, message_id)
    except Exception:
        pass
    _send_saved_result(
        chat_id, family, [str(m) for m in new_models],
        summary=summary if mode == "clean" else None,
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
    if action == "commit":
        # odm:commit:<pending_code>:<mode>
        if len(parts) < 4:
            ui.answer_cb(cb_id, "非法 callback"); return True
        pending_code = parts[2]
        mode = parts[3]
        _on_commit(chat_id, message_id, cb_id, pending_code, mode)
        return True
    ui.answer_cb(cb_id, "未知操作")
    return True


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if not action.startswith("odm_edit:"):
        return False
    _on_edit_input(chat_id, action, text)
    return True
