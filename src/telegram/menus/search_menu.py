"""Search settings under System Settings. All writes cross SearchControl."""
from __future__ import annotations

import asyncio
import json
import threading

from ...management_control.search import API_TYPES, DEFAULT_SEARCH_CONTROL, INTEGER_LIMITS
from ...management_control.auxiliary.common import telegram_context
from ...management_control.errors import ManagementError
from ...search_service import BACKEND_TYPES, NAMES
from .. import menu_cache, states, ui

_CONTROL = DEFAULT_SEARCH_CONTROL
_PAGE_SIZE = 6
_MODES = {"managed": "Parrot 接管", "passthrough": "透传", "disabled": "禁用（返回错误）"}
_MODE_ICONS = {"managed": "✅", "passthrough": "🔁", "disabled": "⛔"}
_FIELDS = {
    "maxAttempts": "总尝试次数（含首次）", "timeoutSeconds": "单次超时（秒）",
    "maxResults": "结果数", "maxToolRounds": "工具轮数", "maxFetchChars": "提取字符上限",
    "minQueryChars": "最短搜索词", "maxFetchUrlChars": "URL 长度上限",
    "maxConcurrentToolCalls": "工具并发（0 不限）", "requireKnownUrlForFetch": "仅提取已知 URL",
    "language": "默认语言", "country": "默认国家", "freshness": "默认时间范围",
}

# Grouping mirrors System Settings: related values are read together, and the
# button carries the current value so a page never has to be opened to read it.
_FIELD_GROUPS = (
    ("执行", ("maxAttempts", "timeoutSeconds", "maxToolRounds")),
    ("返回内容", ("maxResults", "maxFetchChars", "maxConcurrentToolCalls")),
    ("提取", ("requireKnownUrlForFetch",)),
    ("区域偏好", ("language", "country", "freshness")),
)
_INT_FIELDS = ("maxAttempts", "maxResults", "maxToolRounds", "maxFetchChars",
               "minQueryChars", "maxFetchUrlChars", "maxConcurrentToolCalls")

# Status vocabulary shared with the OAuth/channel lists: one icon per state.
_STATUS_ICON = {"disabled": "🚫", "unavailable": "🔕", "ready": "✅"}


def _short_value(key, value):
    """Compact a setting value for a button label without changing its meaning."""
    if key in ("language", "country", "freshness") and not value:
        return "未指定"
    if key == "requireKnownUrlForFetch":
        return "开启" if value else "关闭"
    if key == "maxConcurrentToolCalls":
        return "不限" if not value else str(value)
    if key == "timeoutSeconds":
        return f"{float(value):g}s"
    if key == "maxFetchChars":
        return f"{int(value) // 1000}k" if int(value) >= 1000 else str(value)
    return str(value)


def _btn_value(key, value):
    return f"✏ {_FIELDS[key]}：{_short_value(key, value)}"


def _mode_label(mode):
    return f"{_MODE_ICONS.get(mode, '•')} {_MODES.get(mode, mode)}"


def _kind_label(backend_type):
    return "API Key 来源" if backend_type in API_TYPES else "OAuth 来源"


def _ctx(chat_id):
    return telegram_context(chat_id)


def _code(backend_id, page=0):
    if page:
        return ui.register_code("search-view:" + json.dumps([backend_id, page]))
    return ui.register_code("search:" + backend_id)


def _target(code):
    value = ui.resolve_code(code) or ""
    if value.startswith("search-view:"):
        backend_id, page = json.loads(value[len("search-view:"):])
        return backend_id, int(page)
    if value.startswith("search:"):
        return value[len("search:"):], 0
    raise ValueError("expired")


def _home(page=0):
    return "srch:show" + (f":{page}" if page else "")


def _page(items, page):
    pages = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    return items[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE], page, pages


def _pager(rows, page, pages, callback):
    nav = []
    if page:
        nav.append(ui.btn("⬅ 上一页", callback(page - 1)))
    else:
        nav.append(ui.btn("◁ 上一页", "srch:noop"))
    nav.append(ui.btn(f"{page + 1}/{pages}", "srch:noop"))
    if page + 1 < pages:
        nav.append(ui.btn("➡ 下一页", callback(page + 1)))
    else:
        nav.append(ui.btn("下一页 ▷", "srch:noop"))
    rows.append(nav)


def _row(cfg, backend_id):
    return next(row for row in cfg["backends"] if row["id"] == backend_id)


def _back(backend_id, page=0):
    return "srch:backend:" + _code(backend_id, page)


def _status(row):
    if not row["enabled"]:
        return "停用"
    if row["available"]:
        return "可用"
    return "无可用账户" if row.get("reason") == "no_eligible_accounts" else "缺少凭据"


def _status_icon(row):
    if not row["enabled"]:
        return _STATUS_ICON["disabled"]
    return _STATUS_ICON["ready"] if row["available"] else _STATUS_ICON["unavailable"]


def _credential_line(row):
    if row["type"] in API_TYPES:
        return f"🔑 Key {row['keyCount']} 个"
    return f"👤 符合条件账户 {row['accountCount']} 个"


def _counts(cfg):
    total = len(cfg["backends"])
    available = sum(1 for row in cfg["backends"] if row["enabled"] and row["available"])
    anomaly = sum(1 for row in cfg["backends"] if row["enabled"] and not row["available"])
    disabled = sum(1 for row in cfg["backends"] if not row["enabled"])
    parts = [f"共 {total} 个来源", f"✅ 可用 {available}"]
    if anomaly:
        parts.append(f"🔕 待配置 {anomaly}")
    if disabled:
        parts.append(f"🚫 停用 {disabled}")
    return " | ".join(parts)


def _render(chat_id=0, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    backends, page, pages = _page(cfg["backends"], page)
    suffix = f":{page}" if page else ""
    lines = ["🔎 <b>搜索工具</b>", _counts(cfg), "", "<b>归属策略</b>",
             f"  普通 function：{_mode_label(cfg['functionMode'])}",
             f"  原生 hosted：{_mode_label(cfg['hostedMode'])}", "",
             "<b>执行</b>",
             f"  总尝试 {cfg['maxAttempts']} 次（含首次） · 单次超时 {cfg['timeoutSeconds']:g} 秒",
             "  按下方顺序依次尝试，失败后切换下一个来源。"]
    if pages > 1:
        lines.append(f"第 {page + 1}/{pages} 页")
    lines.append("")
    rows = [[ui.btn("🔀 归属策略", "srch:modes" + suffix), ui.btn("⚙ 默认参数", "srch:defaults" + suffix)]]
    for i, row in enumerate(backends, page * _PAGE_SIZE + 1):
        lines.append(f"{i}. {ui.provider_custom_emoji_html(row['type'])} "
                     f"{ui.escape_html(row['name'][:32])} · {_status(row)}")
        lines.append(f"   {_credential_line(row)} · {_kind_label(row['type'])}")
        rows.append([ui.provider_button(
            f"{i}. {_status_icon(row)} {row['name'][:48]}", _back(row["id"], page), row["type"])])
    if pages > 1:
        _pager(rows, page, pages, _home)
    rows.append([ui.btn("➕ 新增来源", "srch:add" + suffix),
                 ui.btn("↕ 排序", "srch:sort:" + str(page))])
    rows.append([ui.btn("📋 来源统计", "srch:stats" + suffix),
                 ui.btn("🧾 搜索日志", "srch:logs" + suffix)])
    rows.append([ui.btn("◀ 返回系统设置", "menu:settings")])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def _modes(chat_id, message_id, page=0):
    """Both ownership switches on one page; a radio choice is one tap, not two."""
    cfg = _CONTROL.get(_ctx(chat_id))
    suffix = f":{page}" if page else ""
    lines = ["🔀 <b>搜索归属策略</b>", "",
             "客户端声明 WebSearch / WebFetch 时由谁执行：", ""]
    rows = []
    for field, title in (("functionMode", "普通 function"), ("hostedMode", "原生 hosted")):
        current = cfg[field]
        lines.append(f"<b>{title}</b>：{_mode_label(current)}")
        row = []
        for mode in ("managed", "passthrough", "disabled"):
            # Only the selection mark is drawn: the mode icon for ``managed`` is
            # itself a checkmark, which would make an unselected "Parrot 接管"
            # read as selected. The label text already names the mode.
            mark = "✅ " if mode == current else ""
            row.append(ui.btn(f"{mark}{_MODES[mode]}",
                              f"srch:setmode:{field}:{mode}{suffix}"))
        rows.append(row)
        lines.append("")
    lines += ["<i>Parrot 接管：由 Parrot 转发到上方来源。"
              "透传：交给上游自己处理。禁用：直接返回错误，不静默成功。</i>"]
    rows.append([ui.btn("◀ 返回搜索工具", _home(page))])
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def show(chat_id, message_id, cb_id=None, page=0):
    if cb_id is not None:
        ui.answer_cb(cb_id)
    before_callback(chat_id, "srch:show")
    text, kb = _render(chat_id, page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _defaults(chat_id, message_id, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    suffix = f":{page}" if page else ""
    lines = ["⚙ <b>搜索默认参数</b>", ""]
    rows = []
    for title, keys in _FIELD_GROUPS:
        lines.append(f"<b>{title}</b>")
        remaining = list(keys)
        while remaining:
            pair = remaining[:2]
            remaining = remaining[2:]
            row = []
            for key in pair:
                value = cfg[key]
                lines.append(f"  {_FIELDS[key]}：<code>{ui.escape_html(_short_value(key, value))}</code>")
                row.append(ui.btn(_btn_value(key, value), "srch:edit:" + key + suffix))
            rows.append(row)
        lines.append("")
    lines.append("<i>总尝试次数含首次；单次超时是完整一次来源调用的上限，"
                 "OAuth 原生搜索通常比 API Key 来源慢。</i>")
    rows.append([ui.btn("◀ 返回搜索工具", _home(page))])
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _detail(chat_id, message_id, backend_id, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    row = _row(cfg, backend_id)
    code = _code(backend_id, page)
    position = cfg["backends"].index(row) + 1
    lines = [f"{ui.provider_custom_emoji_html(row['type'])} <b>{ui.escape_html(row['name'])}</b>", "",
             f"状态: {_status_icon(row)} {'启用' if row['enabled'] else '停用'} · 来源{_status(row)}",
             f"排序: {position} / {len(cfg['backends'])}",
             f"类型: {_kind_label(row['type'])}"]
    if row["type"] in API_TYPES:
        lines += ["", f"🔑 Key: 已设置 {row['keyCount']} 个",
                  f"🌐 接口: <code>{ui.escape_html(row['endpoint'] or '默认')}</code>"]
        rows = [[ui.btn("🔑 管理 Key", "srch:keys:" + code), ui.btn("🌐 改接口地址", "srch:endpoint:" + code)]]
    else:
        lines += ["", f"🧠 模型: <code>{ui.escape_html(row['model'] or ('自动（默认 ' + _default_model_of(row['type']) + '）'))}</code>",
                  f"👤 账户: {'全部符合条件账户' if not row['accountIds'] else '指定 ' + str(len(row['accountIds'])) + ' 个'}"
                  f"（符合条件 {row['accountCount']} 个）",
                  f"⏸ 允许使用手动停用账户: {'开' if row['allowDisabledAccounts'] else '关'}"]
        lines.append("<i>只影响搜索选用的账户，不改变普通对话状态，也不绕过认证/配额失效。</i>")
        rows = [[ui.btn("🧠 选择模型", "srch:models:" + code), ui.btn("👤 选择账户", "srch:accounts:" + code)],
                [ui.btn("⏸ 允许停用账户：" + ("开" if row['allowDisabledAccounts'] else "关"), "srch:allow:" + code)]]
    tests = [ui.btn("🧪 测试搜索", "srch:test:" + code)]
    if row["type"] in ("anysearch", "tavily", "exa", "openai"):
        tests.append(ui.btn("📄 测试提取", "srch:extract:" + code))
    rows += [tests,
             [ui.btn("✏ 重命名", "srch:name:" + code),
              ui.btn("❌ 停用来源" if row["enabled"] else "✅ 启用来源", "srch:toggle:" + code)],
             [ui.btn("🗑 删除来源", "srch:delete:" + code)],
             [ui.btn("🏠 主菜单", "menu:main"), ui.btn("◀ 返回搜索工具", _home(page))]]
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _default_model_of(backend_type):
    return _CONTROL._MODEL_FALLBACK.get(backend_type, "")


def _models(chat_id, message_id, backend_id, page=0, model_page=0):
    """Model choice is a list from the account catalog, never free text."""
    code = _code(backend_id, page)
    data = _CONTROL.models(_ctx(chat_id), backend_id)
    if not data["supported"]:
        raise ManagementError("UNSUPPORTED_VALUE", "只有 OAuth 搜索来源支持模型设置")
    names = list(data["models"])
    selected = data["selected"]
    lines = [f"🧠 <b>搜索模型</b>", "",
             f"当前: <code>{ui.escape_html(selected or ('自动（默认 ' + data['default'] + '）'))}</code>",
             "从该来源可用账户的模型目录中选择；搜索会真实调用该模型并计入费用。", ""]
    rows = []
    if selected:
        rows.append([ui.btn("↩ 恢复自动（默认 " + data["default"] + "）", "srch:setmodel:" + code + ":")])
    if not names:
        lines.append("当前账户没有可用模型目录；请先在「🔐 管理 OAuth」同步模型，或保持自动默认。")
    visible, model_page, pages = _page(names, model_page)
    for name in visible:
        mark = "✅ " if name == selected else ""
        rows.append([ui.btn(f"{mark}{name}",
                            "srch:setmodel:" + code + ":" + ui.register_code("search-model:" + json.dumps([backend_id, name])))])
    if pages > 1:
        _pager(rows, model_page, pages, lambda p: f"srch:models:{code}:{p}")
        lines.append(f"第 {model_page + 1}/{pages} 页 · 共 {len(names)} 个模型")
    rows.append([ui.btn("◀ 返回来源详情", _back(backend_id, page))])
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _keys(chat_id, message_id, backend_id, page=0):
    row = _row(_CONTROL.get(_ctx(chat_id)), backend_id)
    code = _code(backend_id, page)
    rows = [[ui.btn("➕ 追加 Key", "srch:addApiKeys:" + code), ui.btn("✏ 替换全部 Key", "srch:apiKeys:" + code)],
            [ui.btn("🗑 按序号移除", "srch:removeKeyIndices:" + code)],
            [ui.btn("◀ 返回来源详情", _back(backend_id, page))]]
    ui.edit(chat_id, message_id,
            f"🔑 <b>{ui.escape_html(row['name'][:32])} · Key 管理</b>\n\n"
            f"已设置 {row['keyCount']} 个 Key（序号从 1 开始）。\n"
            "Key 值不可读，也不会回显；保存不影响其它来源的 Key。",
            reply_markup=ui.inline_kb(rows))


def _accounts(chat_id, message_id, backend_id, page=0, account_page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    row = _row(cfg, backend_id)
    selected = row["accountIds"]
    rows = []
    accounts = _CONTROL.accounts(_ctx(chat_id), backend_id)
    visible, account_page, pages = _page(accounts, account_page)
    for account in visible:
        # Code binds full identity + revision + navigation, never display name.
        code = ui.register_code("search-account:" + json.dumps([backend_id, account["id"], cfg["revision"], page, account_page]))
        label = ("☑ " if account["id"] in selected else "☐ ") + account["name"][:64]
        if not account["enabled"]:
            label += "（普通对话停用）"
        if not account["credentialConfigured"]:
            label += "（缺凭据）"
        rows.append([ui.provider_button(label, "srch:account:" + code, row["type"])])
    code = _code(backend_id, page)
    if pages > 1:
        _pager(rows, account_page, pages, lambda p: f"srch:accounts:{code}:{p}")
    rows += [[ui.btn("使用全部符合条件账户", f"srch:allaccounts:{code}:{account_page}")],
             [ui.btn("◀ 返回来源详情", _back(backend_id, page))]]
    text = ("👤 <b>搜索账户选择</b>\n\n"
            + (f"当前：指定 {len(selected)} 个账户" if selected else "当前：全部符合条件账户")
            + f"（符合条件 {len(accounts)} 个）\n"
            "未选择任何账户 = 使用全部符合条件账户。\n"
            "只调整搜索选用范围，不更改账户的普通对话状态。")
    if not accounts:
        text += "\n\n此来源暂无符合条件账户。"
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


def _sort(chat_id, message_id, page=0, draft=None, selected=None):
    """Reorder every source in one commit, matching the OAuth/channel sort page."""
    cfg = _CONTROL.get(_ctx(chat_id))
    ids = list(draft) if draft else [row["id"] for row in cfg["backends"]]
    names = {row["id"]: row["name"] for row in cfg["backends"]}
    # ``selected`` holds one-based positions, not backend IDs: it is validated
    # against the row count so a stale pick cannot select a non-existent row.
    selected = sorted({int(s) for s in (selected or []) if 1 <= int(s) <= len(ids)})
    lines = ["↕ <b>搜索来源排序</b>", "", "当前顺序:"]
    if not ids:
        lines.append("<i>当前没有搜索来源。</i>")
    for i, backend_id in enumerate(ids, start=1):
        mark = " ✅" if i in selected else ""
        lines.append(f"{i}. {ui.escape_html(names.get(backend_id, backend_id)[:32])}{mark}")
    lines += ["", "先点下方序号勾选，再点置顶/上移/下移/置底，最后保存。"]
    rows = []
    number_row = []
    for i in range(1, len(ids) + 1):
        label = f"{i} ✅" if i in selected else str(i)
        number_row.append(ui.btn(label, f"srch:sortpick:{page}:{i}"))
        if len(number_row) == 5:
            rows.append(number_row)
            number_row = []
    if number_row:
        rows.append(number_row)
    rows.append([ui.btn("⬆ 置顶", f"srch:sortmove:{page}:top"),
                 ui.btn("🔼 上移", f"srch:sortmove:{page}:up"),
                 ui.btn("🔽 下移", f"srch:sortmove:{page}:down"),
                 ui.btn("⬇ 置底", f"srch:sortmove:{page}:bottom")])
    rows.append([ui.btn("💾 保存排序", f"srch:sortsave:{page}"),
                 ui.btn("❌ 取消", _home(page))])
    _sort_draft[chat_id] = {"ids": ids, "selected": selected}
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


_sort_draft: dict = {}


def _sort_take(chat_id):
    """Current draft order, repaired against the live config.

    A source added or removed from another message must never be dropped by a
    stale draft, so unknown IDs are discarded and new ones are appended.
    """
    cfg = _CONTROL.get(_ctx(chat_id))
    current = [row["id"] for row in cfg["backends"]]
    draft = _sort_draft.get(chat_id) or {}
    ids = [i for i in (draft.get("ids") or []) if i in current]
    ids += [i for i in current if i not in ids]
    selected = [s for s in (draft.get("selected") or []) if 1 <= int(s) <= len(ids)]
    return ids, selected


def _sort_apply(chat_id, action):
    """Move every selected position in one direction, or to an end."""
    ids, selected = _sort_take(chat_id)
    if not selected:
        return ids, selected
    picked = {ids[i - 1] for i in selected}
    if action == "top":
        ids = [i for i in ids if i in picked] + [i for i in ids if i not in picked]
    elif action == "bottom":
        ids = [i for i in ids if i not in picked] + [i for i in ids if i in picked]
    else:
        step = -1 if action == "up" else 1
        # Walk from the leading edge so a selected neighbour is never overwritten.
        positions = range(len(ids)) if step > 0 else range(len(ids) - 1, -1, -1)
        for index in positions:
            target = index + step
            if not (0 <= target < len(ids)):
                continue
            if ids[index] in picked and ids[target] not in picked:
                ids[index], ids[target] = ids[target], ids[index]
    _sort_draft[chat_id] = {"ids": ids, "selected": selected}
    return ids, selected


def _source_label(cfg, names, source_id, recorded_name=""):
    """Prefer the live name, fall back to the name recorded with the call.

    A source deleted after the fact must still be identifiable in history rather
    than collapsing to its raw ID.
    """
    return names.get(source_id) or recorded_name or source_id


def _stats(chat_id, message_id, page=0, period="month"):
    from ...telegram import menu_cache as _mc
    since = _mc.today_start_ts() if period == "today" else _mc.month_start_ts()
    rows_data = _CONTROL.stats(_ctx(chat_id), since_ts=since)
    cfg = _CONTROL.get(_ctx(chat_id))
    names = {row["id"]: row["name"] for row in cfg["backends"]}
    label = "今日" if period == "today" else "本自然月"
    lines = [f"📋 <b>搜索来源统计 · {label}</b>", ""]
    total_attempts = sum(int(r["attempts"]) for r in rows_data)
    total_cost = sum(float(r["costUsd"]) for r in rows_data)
    if not rows_data:
        lines.append("<i>该时间范围内还没有搜索调用记录。</i>")
    for r in rows_data:
        source = str(r["sourceId"])
        attempts = int(r["attempts"])
        success = int(r["success"])
        rate = f"{success / attempts * 100:.1f}%" if attempts else "—"
        avg = r.get("averageMs")
        lines.append(f"{ui.provider_custom_emoji_html(str(r['sourceType']))} "
                     f"<b>{ui.escape_html(_source_label(cfg, names, source, str(r.get('sourceName') or ''))[:32])}</b>")
        lines.append(f"  调用 {attempts} 次 · ✅ {success} · ❌ {int(r['failed'])}"
                     + (f" · ⏳ {int(r['running'])}" if int(r["running"]) else ""))
        lines.append(f"  成功率 {rate}" + (f" · 平均 {avg} ms" if avg else ""))
        if int(r["inputTokens"]) or int(r["outputTokens"]):
            lines.append(f"  Tokens ↑{ui.fmt_tokens(r['inputTokens'])} ↓{ui.fmt_tokens(r['outputTokens'])}")
        if float(r["costUsd"]):
            lines.append(f"  费用 ${float(r['costUsd']):.4f}")
        lines.append("")
    lines.append(f"合计：调用 {total_attempts} 次 · 费用 ${total_cost:.4f}")
    lines.append("<i>费用只统计 OAuth 来源真实模型调用的 Token 结算；"
                 "API Key 来源按次计费，不在 Token 用量中。</i>")
    rows = [[ui.btn("✅ 今日" if period == "today" else "今日", "srch:stats:0:today"),
             ui.btn("✅ 本自然月" if period == "month" else "本自然月", "srch:stats:0:month")],
            [ui.btn("🧾 查看搜索日志", f"srch:logs:0:{period}")],
            [ui.btn("◀ 返回搜索工具", _home(page))]]
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _logs(chat_id, message_id, page=0, period="month", only=None, offset=0):
    from ...telegram import menu_cache as _mc
    since = _mc.today_start_ts() if period == "today" else _mc.month_start_ts()
    entries = _CONTROL.logs(_ctx(chat_id), since_ts=since, source_id=only, limit=_PAGE_SIZE, offset=offset)
    cfg = _CONTROL.get(_ctx(chat_id))
    names = {row["id"]: row["name"] for row in cfg["backends"]}
    label = "今日" if period == "today" else "本自然月"
    lines = [f"🧾 <b>搜索日志 · {label}</b>"]
    if only:
        lines.append(f"来源筛选：<b>{ui.escape_html(names.get(only, only)[:32])}</b>")
    lines.append("")
    if not entries:
        lines.append("<i>该时间范围内还没有搜索记录。</i>")
    for e in entries:
        icon = {"success": "✅", "error": "❌"}.get(str(e["status"]), "⏳")
        source = str(e["sourceId"])
        lines.append(f"{icon} {ui.fmt_bjt_ts(e['startedAt'], '%m-%d %H:%M:%S')} "
                     f"{ui.provider_custom_emoji_html(str(e['sourceType']))} "
                     f"{ui.escape_html(names.get(source, source)[:24])}")
        subject = e.get("query") or e.get("url") or ""
        if subject:
            lines.append(f"  <code>{ui.escape_html(str(subject)[:80])}</code>")
        detail = [f"{'搜索' if e['operation'] == 'search' else '提取'}"]
        if e.get("elapsedMs") is not None:
            detail.append(f"{e['elapsedMs']} ms")
        detail.append(f"{e['resultCount']} 条")
        if e.get("errorCode"):
            detail.append(str(e["errorCode"]))
        lines.append("  " + " · ".join(detail))
        credential = str(e.get("credentialLabel") or "")
        if credential:
            cost = f" · ${float(e['costUsd']):.4f}" if float(e["costUsd"]) else ""
            model = f" · {ui.escape_html(str(e['model']))}" if e.get("model") else ""
            lines.append(f"  {ui.escape_html(credential)}{model}{cost}")
        lines.append("")
    rows = [[ui.btn("✅ 今日" if period == "today" else "今日", f"srch:logs:0:today:{only or ''}:0"),
             ui.btn("✅ 本自然月" if period == "month" else "本自然月", f"srch:logs:0:month:{only or ''}:0")]]
    nav = []
    if offset:
        nav.append(ui.btn("⬅ 上一页", f"srch:logs:0:{period}:{only or ''}:{max(0, offset - _PAGE_SIZE)}"))
    if len(entries) >= _PAGE_SIZE:
        nav.append(ui.btn("➡ 下一页", f"srch:logs:0:{period}:{only or ''}:{offset + _PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.append([ui.btn("📋 来源统计", f"srch:stats:0:{period}"),
                 ui.btn("◀ 返回搜索工具", _home(page))])
    ui.edit(chat_id, message_id, ui.truncate("\n".join(lines)), reply_markup=ui.inline_kb(rows))


def _ask(chat_id, message_id, field, backend_id=None, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    if field == "model":
        # The model is chosen from the account catalog; free text is refused at
        # the single entry point so no path can bypass the list.
        raise ManagementError("UNSUPPORTED_VALUE", "请在「选择模型」中从账户模型目录选择")
    parent = _back(backend_id, page) if backend_id else "srch:defaults" + (f":{page}" if page else "")
    label = _FIELDS.get(field, {"name": "来源名称", "endpoint": "接口地址"}.get(field, field))
    current = ""
    if backend_id:
        row = _row(cfg, backend_id)
        current = str(row.get(field) or "")
    elif field in cfg:
        current = str(cfg[field])
    text = f"请输入{label}；输入 <code>-</code> 清空（使用默认值）："
    if current:
        text = f"当前：<code>{ui.escape_html(current[:120] or '未指定')}</code>\n\n" + text
    if field in ("apiKeys", "addApiKeys"):
        parent = "srch:keys:" + _code(backend_id, page)
        text = "请输入 API Key，每行一个；只写不回显。" + ("\n输入 <code>-</code> 清空全部 Key。" if field == "apiKeys" else "")
    elif field == "removeKeyIndices":
        parent = "srch:keys:" + _code(backend_id, page)
        text = "请输入要删除的 Key 序号（从 1 开始），逗号分隔；不显示 Key 值。"
    elif field in ("test", "extract"):
        text = "请输入搜索词：" if field == "test" else "请输入公开 HTTP(S) URL："
        text += "\n将主动调用此来源（可能消耗额度），失败不会切换其它来源。"
    elif field == "freshness":
        text = "请输入 day / week / month / year；<code>-</code> 清空。"
    elif field in INTEGER_LIMITS:
        low, high = INTEGER_LIMITS[field]
        text = f"请输入{_FIELDS[field]}，整数 {low}–{high}："
    elif field == "timeoutSeconds":
        text = "请输入单次完整调用超时（秒），0.1–600；默认 10。OAuth 原生搜索可能需要更长。"
    states.set_state(chat_id, "search_input", {"field": field, "backend_id": backend_id, "revision": cfg["revision"], "parent": parent})
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[ui.btn("❌ 取消", parent)]]))


def before_callback(chat_id, data):
    state = states.get_state(chat_id)
    if state and state.get("action") in ("search_input", "search_delete_confirm"):
        if state["action"] == "search_delete_confirm" and data == state["data"]["confirm"]:
            return
        states.pop_state_if_current(chat_id, state["data"])


def before_command(chat_id, text):
    if text.startswith("/") and text.strip().split("@", 1)[0].lower() != "/cancel":
        before_callback(chat_id, text)


def handle_callback(chat_id, message_id, cb_id, data):
    if not data.startswith("srch:"):
        return False
    before_callback(chat_id, data)
    parts = data.split(":")
    action = parts[1]
    ui.answer_cb(cb_id)
    parent = "srch:show"
    try:
        ctx = _ctx(chat_id)
        if action == "show":
            show(chat_id, message_id, page=int(parts[2]) if len(parts) > 2 else 0)
        elif action == "defaults":
            _defaults(chat_id, message_id, int(parts[2]) if len(parts) > 2 else 0)
        elif action == "modes":
            _modes(chat_id, message_id, int(parts[2]) if len(parts) > 2 else 0)
        elif action == "setmode":
            _CONTROL.patch(ctx, {parts[2]: parts[3]})
            _modes(chat_id, message_id, int(parts[4]) if len(parts) > 4 else 0)
        elif action == "edit":
            field = parts[2]
            page = int(parts[3]) if len(parts) > 3 else 0
            parent = "srch:defaults" + (f":{page}" if page else "")
            if field not in _FIELDS:
                raise ValueError
            if field == "requireKnownUrlForFetch":
                cfg = _CONTROL.get(ctx)
                _CONTROL.patch(ctx, {field: not cfg[field]}, expected_revision=cfg["revision"])
                _defaults(chat_id, message_id, page)
            else:
                _ask(chat_id, message_id, field, page=page)
        elif action == "sort":
            page = int(parts[2]) if len(parts) > 2 else 0
            _sort_draft.pop(chat_id, None)
            _sort(chat_id, message_id, page)
        elif action == "sortpick":
            page = int(parts[2]) if len(parts) > 2 else 0
            position = int(parts[3]) if len(parts) > 3 else 0
            ids, selected = _sort_take(chat_id)
            if not (1 <= position <= len(ids)):
                raise ValueError
            if position in selected:
                selected.remove(position)
            else:
                selected.append(position)
            _sort_draft[chat_id] = {"ids": ids, "selected": list(selected)}
            _sort(chat_id, message_id, page, ids, list(selected))
        elif action == "sortmove":
            page = int(parts[2]) if len(parts) > 2 else 0
            what = parts[3] if len(parts) > 3 else ""
            if what not in ("top", "up", "down", "bottom"):
                raise ValueError
            if not _sort_draft.get(chat_id):
                raise ValueError
            ids, selected = _sort_apply(chat_id, what)
            _sort(chat_id, message_id, page, ids, selected)
        elif action == "sortsave":
            if not _sort_draft.get(chat_id):
                raise ValueError
            ids, _selected = _sort_take(chat_id)
            cfg = _CONTROL.get(ctx)
            if sorted(ids) != sorted(row["id"] for row in cfg["backends"]):
                raise ManagementError("STATE_CONFLICT", "来源列表已变化，请重新排序")
            _CONTROL.priority(ctx, ids, expected_revision=cfg["revision"])
            _sort_draft.pop(chat_id, None)
            show(chat_id, message_id, page=int(parts[2]) if len(parts) > 2 else 0)
        elif action == "stats":
            page = int(parts[2]) if len(parts) > 2 else 0
            period = parts[3] if len(parts) > 3 and parts[3] in ("today", "month") else "month"
            _stats(chat_id, message_id, page, period)
        elif action == "logs":
            page = int(parts[2]) if len(parts) > 2 else 0
            period = parts[3] if len(parts) > 3 and parts[3] in ("today", "month") else "month"
            only = parts[4] if len(parts) > 4 and parts[4] else None
            offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
            _logs(chat_id, message_id, page, period, only, max(0, offset))
        elif action == "noop":
            pass
        elif action == "add":
            page = int(parts[2]) if len(parts) > 2 else 0
            rows = [[ui.provider_button(NAMES[k], f"srch:create:{k}:{page}", k)] for k in BACKEND_TYPES]
            rows.append([ui.btn("◀ 返回搜索工具", _home(page))])
            ui.edit(chat_id, message_id, "➕ <b>新增搜索来源</b>\n\n来源身份固定，排序不会改变身份。", reply_markup=ui.inline_kb(rows))
        elif action == "create":
            cfg = _CONTROL.add_backend(ctx, {"type": parts[2]})
            _detail(chat_id, message_id, cfg["backends"][-1]["id"], int(parts[3]) if len(parts) > 3 else 0)
        elif action == "confirmdelete":
            state = states.get_state(chat_id)
            if not state or state.get("action") != "search_delete_confirm" or state["data"]["confirm"] != data:
                raise ValueError("expired")
            pending = state["data"]
            parent = pending["parent"]
            if states.pop_state_if_current(chat_id, pending) is None:
                return True
            _CONTROL.delete_backend(ctx, pending["backend_id"], expected_revision=pending["revision"])
            show(chat_id, message_id, page=pending["page"])
        elif action == "account":
            full = ui.resolve_code(parts[2]) or ""
            if not full.startswith("search-account:"):
                raise ValueError
            values = json.loads(full[len("search-account:"):])
            backend_id, account_id, revision = values[:3]
            page, account_page = values[3:] if len(values) == 5 else (0, 0)
            parent = f"srch:accounts:{_code(backend_id, page)}:{account_page}"
            row = _row(_CONTROL.get(ctx), backend_id)
            selected = list(row["accountIds"])
            if account_id in selected:
                selected.remove(account_id)
            else:
                selected.append(account_id)
            _CONTROL.patch_backend(ctx, backend_id, {"accountIds": selected}, expected_revision=revision)
            _accounts(chat_id, message_id, backend_id, page, account_page)
        else:
            backend_id, page = _target(parts[2])
            parent = _back(backend_id, page)
            cfg = _CONTROL.get(ctx)
            row = _row(cfg, backend_id)
            if action == "backend":
                _detail(chat_id, message_id, backend_id, page)
            elif action == "keys":
                _keys(chat_id, message_id, backend_id, page)
            elif action == "models":
                _models(chat_id, message_id, backend_id, page, int(parts[3]) if len(parts) > 3 else 0)
            elif action == "setmodel":
                raw = parts[3] if len(parts) > 3 else ""
                full = ui.resolve_code(raw) or "" if raw else ""
                if full.startswith("search-model:"):
                    chosen = json.loads(full[len("search-model:"):])[1]
                elif not raw:
                    chosen = ""  # explicit "restore automatic"
                else:
                    raise ValueError
                _CONTROL.patch_backend(ctx, backend_id, {"model": chosen}, expected_revision=cfg["revision"])
                _models(chat_id, message_id, backend_id, page, int(parts[4]) if len(parts) > 4 else 0)
            elif action == "accounts":
                _accounts(chat_id, message_id, backend_id, page, int(parts[3]) if len(parts) > 3 else 0)
            elif action == "allaccounts":
                _CONTROL.patch_backend(ctx, backend_id, {"accountIds": []}, expected_revision=cfg["revision"])
                _accounts(chat_id, message_id, backend_id, page, int(parts[3]) if len(parts) > 3 else 0)
            elif action in ("toggle", "allow"):
                field = "enabled" if action == "toggle" else "allowDisabledAccounts"
                _CONTROL.patch_backend(ctx, backend_id, {field: not row[field]}, expected_revision=cfg["revision"])
                _detail(chat_id, message_id, backend_id, page)
            # Per-source up/down remains available for a single adjacent move;
            # the sort page handles multi-position changes in one commit.
            elif action in ("up", "down"):
                ids = [v["id"] for v in cfg["backends"]]
                index = ids.index(backend_id)
                target = index + (-1 if action == "up" else 1)
                if 0 <= target < len(ids):
                    ids[index], ids[target] = ids[target], ids[index]
                    _CONTROL.priority(ctx, ids, expected_revision=cfg["revision"])
                _detail(chat_id, message_id, backend_id, page)
            elif action == "delete":
                token = ui.register_code("search-delete:" + json.dumps([chat_id, backend_id, cfg["revision"], page]))
                confirm = "srch:confirmdelete:" + token
                states.set_state(chat_id, "search_delete_confirm", {"confirm": confirm, "backend_id": backend_id,
                                 "revision": cfg["revision"], "page": page, "parent": parent})
                ui.edit(chat_id, message_id,
                        "⚠ <b>确认删除搜索来源？</b>\n\n"
                        f"• 名称: <code>{ui.escape_html(row['name'])}</code>\n"
                        f"• 类型: {_kind_label(row['type'])}\n"
                        + (f"• Key: {row['keyCount']} 个\n" if row["type"] in API_TYPES else
                           f"• 符合条件账户: {row['accountCount']} 个\n")
                        + "\n只移除该来源配置，不影响 OAuth 账户或其它来源的 Key。"
                        "删除后不可恢复。",
                        reply_markup=ui.inline_kb([[ui.btn("✅ 确认删除", confirm), ui.btn("❌ 取消", parent)]]))
            elif action in ("name", "endpoint", "apiKeys", "addApiKeys", "removeKeyIndices", "test", "extract"):
                _ask(chat_id, message_id, action, backend_id, page)
            elif action == "model":
                # Legacy/forged free-text model buttons must fail loudly instead
                # of falling through to an unrelated page.
                raise ManagementError("UNSUPPORTED_VALUE", "请在「选择模型」中从账户模型目录选择")
            else:
                raise ValueError
    except (ValueError, IndexError, StopIteration):
        ui.send_result(chat_id, "❌ 按钮已过期或无效，请重新进入。", back_label="◀ 返回搜索工具", back_callback="srch:show")
    except ManagementError as exc:
        ui.send_result(chat_id, "❌ " + ui.escape_html(exc.message), back_label="◀ 返回上级", back_callback=parent)
    return True


def _spawn_async_task(coro_factory):
    def run():
        asyncio.run(coro_factory())
    worker = threading.Thread(target=run, daemon=True, name="tg-search-test")
    worker.start()
    return worker


def _on_test_input(chat_id, data, value):
    if not value:
        ui.send(chat_id, "❌ 测试内容不能为空，请重新输入。")
        return
    # Consume this exact input once, before feedback or paid dispatch. A newer
    # editor must neither be removed here nor touched by the eventual result.
    if states.pop_state_if_current(chat_id, data) is None:
        return
    kb = ui.inline_kb([[ui.btn("◀ 返回上级", data["parent"])]])
    sent = ui.send(chat_id, "🧪 正在测试此搜索来源，请稍等…", reply_markup=kb) or {}
    message_id = (sent.get("result") or {}).get("message_id")
    if not message_id:
        ui.send_result(chat_id, "❌ 无法创建测试提示，未发起搜索，请重试。", back_label="◀ 返回上级", back_callback=data["parent"])
        return
    token = menu_cache.begin_view(chat_id, message_id)
    control, context = _CONTROL, _ctx(chat_id)

    async def run():
        try:
            field = data["field"]
            result = await control.test(context, data["backend_id"], operation="search" if field == "test" else "extract",
                                        query=value if field == "test" else None, url=value if field == "extract" else None)
            message = f"✅ 此来源测试成功 · 结果 {result['resultCount']} 条 · 提取 {result['contentChars']} 字符 · 尝试 {result['attemptCount']} 次"
        except ManagementError as exc:
            message = "❌ " + ui.escape_html(exc.message)
        except Exception:
            message = "❌ 搜索测试失败，请重试。"
        # Only this progress message, and only if its buttons have not navigated
        # to a newer page. No fallback send and no state cleanup from the worker.
        menu_cache.run_if_current(chat_id, message_id, token,
                                  lambda: ui.edit(chat_id, message_id, message, reply_markup=kb))

    _spawn_async_task(run)


def handle_text_state(chat_id, action, text):
    if action not in ("search_input", "search_delete_confirm"):
        return False
    state = states.get_state(chat_id)
    if not state or state.get("action") != action:
        return False
    data = state["data"]
    value = text.strip()
    if value.split("@", 1)[0].lower() == "/cancel":
        if states.pop_state_if_current(chat_id, data) is not None:
            ui.send_result(chat_id, "已取消，未保存。", back_label="◀ 返回上级", back_callback=data["parent"])
        return True
    if action == "search_delete_confirm":
        ui.send_result(chat_id, "请使用确认/取消按钮，或发送 /cancel。", back_label="◀ 返回上级", back_callback=data["parent"])
        return True
    field, backend_id = data["field"], data["backend_id"]
    if field in ("test", "extract"):
        _on_test_input(chat_id, data, value)
        return True
    try:
        if field in INTEGER_LIMITS:
            value = int(value)
        elif field == "timeoutSeconds":
            value = float(value)
        elif field in ("apiKeys", "addApiKeys"):
            value = [] if value == "-" and field == "apiKeys" else [v.strip() for v in value.splitlines() if v.strip()]
        elif field == "removeKeyIndices":
            value = [int(v.strip()) - 1 for v in value.replace("，", ",").split(",")]
        elif value == "-":
            value = ""
        if backend_id:
            _CONTROL.patch_backend(_ctx(chat_id), backend_id, {field: value}, expected_revision=data["revision"])
        else:
            _CONTROL.patch(_ctx(chat_id), {field: value}, expected_revision=data["revision"])
        message = "✅ 已保存。"  # Never echo user input, particularly keys.
    except (ValueError, ManagementError) as exc:
        message = "❌ 格式无效，请按提示重新输入。" if isinstance(exc, ValueError) else "❌ " + ui.escape_html(exc.message)
        ui.send_result(chat_id, message, back_label="◀ 返回上级", back_callback=data["parent"])
        return True
    states.pop_state_if_current(chat_id, data)
    ui.send_result(chat_id, message, back_label="◀ 返回上级", back_callback=data["parent"])
    return True
