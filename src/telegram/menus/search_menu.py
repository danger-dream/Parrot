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
_PAGE_SIZE = 10
_MODES = {"managed": "Parrot 接管", "passthrough": "透传", "disabled": "禁用（返回错误）"}
_FIELDS = {
    "maxAttempts": "总尝试次数（含首次）", "timeoutSeconds": "单次超时（秒）",
    "maxResults": "结果数", "maxToolRounds": "工具轮数", "maxFetchChars": "提取字符上限",
    "minQueryChars": "最短搜索词", "maxFetchUrlChars": "URL 长度上限",
    "maxConcurrentToolCalls": "工具并发（0 不限）", "requireKnownUrlForFetch": "仅提取已知 URL",
    "language": "默认语言", "country": "默认国家", "freshness": "默认时间范围",
}


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
        nav.append(ui.btn("◀ 上一页", callback(page - 1)))
    if page + 1 < pages:
        nav.append(ui.btn("下一页 ▶", callback(page + 1)))
    if nav:
        rows.append(nav)


def _row(cfg, backend_id):
    return next(row for row in cfg["backends"] if row["id"] == backend_id)


def _back(backend_id, page=0):
    return "srch:backend:" + _code(backend_id, page)


def _status(row):
    if not row["enabled"]:
        return "停用"
    if row["available"]:
        return "配置就绪"
    return "无可用账户" if row.get("reason") == "no_eligible_accounts" else "缺少凭据"


def _render(chat_id=0, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    backends, page, pages = _page(cfg["backends"], page)
    suffix = f":{page}" if page else ""
    lines = ["🔎 <b>搜索工具</b>", "",
             f"普通 function：{_MODES[cfg['functionMode']]}",
             f"原生 hosted：{_MODES[cfg['hostedMode']]}",
             f"总尝试：{cfg['maxAttempts']} 次（含首次） · 单次超时：{cfg['timeoutSeconds']:g} 秒", "",
             "功能启用不等于来源可用；以下仅为配置就绪状态，未实时探活。",
             "优先级从上到下，失败按配置重试/切换来源。"]
    lines.append(f"第 {page + 1}/{pages} 页 · 共 {len(cfg['backends'])} 个来源")
    rows = [[ui.btn("普通 function 策略", "srch:mode:functionMode" + suffix), ui.btn("原生 hosted 策略", "srch:mode:hostedMode" + suffix)],
            [ui.btn("⚙ 默认参数", "srch:defaults" + suffix), ui.btn("➕ 新增来源", "srch:add" + suffix)]]
    for i, row in enumerate(backends, page * _PAGE_SIZE + 1):
        count = f"已设置 {row['keyCount']} 个 Key" if row["type"] in API_TYPES else f"符合条件账户 {row['accountCount']} 个"
        lines.append(f"{i}. {ui.provider_custom_emoji_html(row['type'])} {ui.escape_html(row['name'][:32])} · {_status(row)} · {count}")
        rows.append([ui.provider_button(f"{i}. {row['name'][:60]} · {_status(row)}", _back(row["id"], page), row["type"])])
    _pager(rows, page, pages, _home)
    lines += ["", "Anthropic：原生实现尚未完成账户实测。"]
    rows.append([ui.btn("◀ 返回系统设置", "menu:settings")])
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def show(chat_id, message_id, cb_id=None, page=0):
    if cb_id is not None:
        ui.answer_cb(cb_id)
    before_callback(chat_id, "srch:show")
    text, kb = _render(chat_id, page)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _defaults(chat_id, message_id, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    lines = ["⚙ <b>搜索默认参数</b>", "",
             "单次完整调用默认 10 秒，OAuth 原生搜索可能需要更长，可按需要调整。", ""]
    rows = []
    for key, label in _FIELDS.items():
        value = cfg[key]
        if type(value) is bool:
            value = "开启" if value else "关闭"
        lines.append(f"{label}：<code>{ui.escape_html(str(value) if value != '' else '未指定')}</code>")
        rows.append([ui.btn(label, "srch:edit:" + key + (f":{page}" if page else ""))])
    rows.append([ui.btn("◀ 返回搜索工具", _home(page))])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _detail(chat_id, message_id, backend_id, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    row = _row(cfg, backend_id)
    code = _code(backend_id, page)
    lines = [f"{ui.provider_custom_emoji_html(row['type'])} <b>{ui.escape_html(row['name'])}</b>", "",
             f"功能：{'启用' if row['enabled'] else '停用'} · 来源：{_status(row)}（非实时探活）",
             "实现实测：" + ("曾验证，不代表当前凭据健康" if row["verified"] else "原生实现尚未完成账户实测"),
             f"优先级：{cfg['backends'].index(row) + 1} / {len(cfg['backends'])}"]
    rows = [[ui.btn("❌ 停用来源" if row["enabled"] else "✅ 启用来源", "srch:toggle:" + code), ui.btn("✏ 名称", "srch:name:" + code)],
            [ui.btn("⬆ 上移", "srch:up:" + code), ui.btn("⬇ 下移", "srch:down:" + code)]]
    if row["type"] in API_TYPES:
        lines += [f"API Key：已设置 {row['keyCount']} 个（只写，不回显）",
                  f"地址：<code>{ui.escape_html(row['endpoint'] or '默认')}</code>"]
        rows += [[ui.btn("🔑 管理 Key", "srch:keys:" + code), ui.btn("🌐 地址", "srch:endpoint:" + code)]]
    else:
        lines.append(f"模型：<code>{ui.escape_html(row['model'] or '默认')}</code>")
        rows.append([ui.btn("🧠 模型", "srch:model:" + code)])
        lines += [f"符合条件账户：{row['accountCount']} 个；已选：{len(row['accountIds']) or '全部'}",
                  f"允许搜索使用手动停用的账户：{'开启' if row['allowDisabledAccounts'] else '关闭（默认）'}",
                  "不改变普通对话，也不绕过认证/配额失效。"]
        rows += [[ui.btn("👤 选择账户", "srch:accounts:" + code)],
                 [ui.btn("🔁 允许搜索使用手动停用账户：" + ("开" if row['allowDisabledAccounts'] else "关"), "srch:allow:" + code)]]
    tests = [ui.btn("🧪 测试此来源搜索", "srch:test:" + code)]
    if row["type"] in ("anysearch", "tavily", "exa", "openai"):
        tests.append(ui.btn("🧪 测试提取", "srch:extract:" + code))
    rows += [tests, [ui.btn("🗑 删除此来源", "srch:delete:" + code)],
             [ui.btn("◀ 返回搜索工具", _home(page))]]
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _keys(chat_id, message_id, backend_id, page=0):
    row = _row(_CONTROL.get(_ctx(chat_id)), backend_id)
    code = _code(backend_id, page)
    rows = [[ui.btn("➕ 追加 Key", "srch:addApiKeys:" + code), ui.btn("✏ 替换全部 Key", "srch:apiKeys:" + code)],
            [ui.btn("🗑 按序号移除 Key", "srch:removeKeyIndices:" + code)],
            [ui.btn("◀ 返回来源详情", _back(backend_id, page))]]
    ui.edit(chat_id, message_id, f"🔑 <b>搜索 Key 管理</b>\n\n已设置 {row['keyCount']} 个 Key。\n只写不可读，不显示任何 Key 值。\n序号从 1 开始。保存时保留其它来源和未修改的 Key。", reply_markup=ui.inline_kb(rows))


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
    _pager(rows, account_page, pages, lambda p: f"srch:accounts:{code}:{p}")
    rows += [[ui.btn("使用全部符合条件账户", f"srch:allaccounts:{code}:{account_page}")],
             [ui.btn("◀ 返回来源详情", _back(backend_id, page))]]
    text = "👤 <b>搜索账户选择</b>\n\n未选择任何账户 = 全部符合条件账户。\n只调整搜索选择，不更改普通对话状态。\n当前：" + (f"指定 {len(selected)} 个" if selected else "全部符合条件账户")
    text += f"\n第 {account_page + 1}/{pages} 页 · 共 {len(accounts)} 个账户"
    if not accounts:
        text += "\n此来源暂无账户。"
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


def _ask(chat_id, message_id, field, backend_id=None, page=0):
    cfg = _CONTROL.get(_ctx(chat_id))
    if field == "model" and _row(cfg, backend_id)["type"] in API_TYPES:
        raise ManagementError("UNSUPPORTED_VALUE", "只有 OAuth 搜索来源支持模型设置")
    parent = _back(backend_id, page) if backend_id else "srch:defaults" + (f":{page}" if page else "")
    label = _FIELDS.get(field, {"name": "来源名称", "model": "模型", "endpoint": "后端地址"}.get(field, field))
    text = f"请输入{label}；输入 <code>-</code> 清空字符串（使用默认值）："
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
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[ui.btn("◀ 取消并返回", parent)]]))


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
        elif action == "mode":
            field = parts[2]
            if field not in ("functionMode", "hostedMode"):
                raise ValueError
            page = int(parts[3]) if len(parts) > 3 else 0
            current = _CONTROL.get(ctx)[field]
            rows = [[ui.btn(("✅ 当前 · " if mode == current else "") + label, f"srch:setmode:{field}:{mode}:{page}")] for mode, label in _MODES.items()]
            rows.append([ui.btn("◀ 返回搜索工具", _home(page))])
            kind = "普通 function" if field == "functionMode" else "原生 hosted"
            ui.edit(chat_id, message_id, f"🔎 <b>搜索归属策略</b>\n\n当前：{kind} · {_MODES[current]}\n\n"
                    "managed：Parrot 接管\npassthrough：原样透传\ndisabled：返回错误，不静默成功", reply_markup=ui.inline_kb(rows))
        elif action == "setmode":
            _CONTROL.patch(ctx, {parts[2]: parts[3]})
            show(chat_id, message_id, page=int(parts[4]) if len(parts) > 4 else 0)
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
            elif action == "accounts":
                _accounts(chat_id, message_id, backend_id, page, int(parts[3]) if len(parts) > 3 else 0)
            elif action == "allaccounts":
                _CONTROL.patch_backend(ctx, backend_id, {"accountIds": []}, expected_revision=cfg["revision"])
                _accounts(chat_id, message_id, backend_id, page, int(parts[3]) if len(parts) > 3 else 0)
            elif action in ("toggle", "allow"):
                field = "enabled" if action == "toggle" else "allowDisabledAccounts"
                _CONTROL.patch_backend(ctx, backend_id, {field: not row[field]}, expected_revision=cfg["revision"])
                _detail(chat_id, message_id, backend_id, page)
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
                ui.edit(chat_id, message_id, f"🗑 确定删除搜索来源 <b>{ui.escape_html(row['name'])}</b>？\n\n"
                        "仅移除此来源配置，不删除 OAuth 账户或其它来源的 Key。",
                        reply_markup=ui.inline_kb([[ui.btn("✅ 确认删除", confirm), ui.btn("❌ 取消", parent)]]))
            elif action in ("name", "model", "endpoint", "apiKeys", "addApiKeys", "removeKeyIndices", "test", "extract"):
                _ask(chat_id, message_id, action, backend_id, page)
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
