"""代理管理菜单：代理列表 / 添加 / 代理组 / 路由规则。

callback_data 前缀：``px:``
状态机 action：``px_*``
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from ... import config, oauth_manager
from ...proxy import manager as pm
from ...proxy.connector import parse_proxy_url, _mask_url
from ...channel import registry
from .. import states, ui


# ── helpers ──────────────────────────────────────────────────────

def _proxy_icon(c) -> str:
    if c.type == "direct":
        return "🌐"
    if c.stats.total_attempts == 0:
        return "⚪"
    if c.stats.last_success_ts >= c.stats.last_attempt_ts - 1:
        return "🟢"
    return "🔴"


def _oauth_provider_for_channel(ch) -> str:
    provider = str(getattr(ch, "provider", "") or "")
    if provider:
        return provider
    key = str(getattr(ch, "key", "") or "")
    if key.startswith("oauth:"):
        try:
            return oauth_manager.provider_of(key[len("oauth:"):])
        except Exception:
            pass
    return "claude" if getattr(ch, "protocol", "anthropic") == "anthropic" else "openai"


def _oauth_provider_icon(ch) -> str:
    return ui.provider_icon(_oauth_provider_for_channel(ch))


def _route_model_icon(model: str) -> str:
    m = str(model or "").lower()
    if m.startswith(("claude", "anthropic")):
        return ui.provider_icon("claude")
    if m.startswith(("grok", "xai")):
        return ui.provider_icon("xai")
    if m.startswith(("gpt", "o1", "o3", "o4")):
        return ui.provider_icon("openai")
    return "🤖"


def _type_badge(t: str) -> str:
    return {"ss2022": "🔒", "socks5": "🧦", "direct": "🌐"}.get(t, "❓")


def _target_sync_note(name: str) -> str:
    """All route targets are valid for routing rules.

    Async-only proxy types such as SS2022 are supported for family/channel/model
    upstream traffic, and Telegram/OAuth helper calls use the same resolver.
    The UI must not mark them as unsupported.
    """
    return ""


def _all_targets() -> list[tuple[str, str]]:
    """Return [(name, badge_label)] for groups + proxies + direct."""
    pm.init()
    items: list[tuple[str, str]] = []
    for gn, members in pm.all_groups().items():
        items.append((gn, f"📋 {gn} ({len(members)}个){_target_sync_note(gn)}"))
    for n, c in pm.all_connectors().items():
        if n == "direct":
            continue
        items.append((n, f"{_type_badge(c.type)} {n}{_target_sync_note(n)}"))
    items.append(("direct", "🌐 direct"))
    return items


def _valid_name(s: str) -> bool:
    return bool(re.match(r'^[a-z0-9][a-z0-9_-]{0,30}$', s))


def _proxy_detail_line(c) -> str:
    """Return a non-redundant proxy detail line for Telegram lists."""
    if c.type == "ss2022":
        server = ui.escape_html(getattr(c, "server", ""))
        port = ui.escape_html(getattr(c, "port", ""))
        return f"📍 SS2022 · <code>{server}:{port}</code>"
    if c.type == "socks5":
        url = ui.escape_html(_mask_url(getattr(c, "url", "")))
        return f"📍 SOCKS5 · <code>{url}</code>"
    return f"📍 <code>{ui.escape_html(c.display())}</code>"


def _fmt_ms(ms: int) -> str:
    """Format milliseconds: >10s shows as seconds, otherwise ms."""
    if ms >= 10000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"


def _fmt_bytes(n: int) -> str:
    """Format byte count to human readable."""
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    return f"{n / (1024 * 1024 * 1024):.1f}GB"


def _fmt_proxy_stats(ps: dict, *, compact: bool = False) -> str:
    """Format one proxy/group stats dict.

    Required display metrics:
      次数、tokens、平均连接、平均首字、平均耗时、总代理字节数。
    """
    reqs = int(ps.get("requests", 0) or 0)
    ok = int(ps.get("successes", 0) or 0)
    fail = int(ps.get("failures", 0) or 0)
    inp = int(ps.get("input_tokens", 0) or 0)
    out = int(ps.get("output_tokens", 0) or 0)
    cache_c = int(ps.get("cache_creation_tokens", 0) or 0)
    cache_r = int(ps.get("cache_read_tokens", 0) or 0)
    tok = int(ps.get("total_tokens", inp + out + cache_c + cache_r) or 0)
    traffic = int(ps.get("total_bytes", 0) or 0)
    conn_ms = int(ps.get("avg_connect_ms", 0) or 0)
    first_ms = int(ps.get("avg_first_byte_ms", 0) or 0)
    total_ms = int(ps.get("avg_total_ms", 0) or 0)

    tok_str = ui.fmt_tokens(tok)
    traffic_str = _fmt_bytes(traffic)
    if compact:
        return (
            f"📊 {reqs}次 · 🧮 {tok_str} tok · 📦 {traffic_str}\n"
            f"⏱ 连接 {_fmt_ms(conn_ms)} · 首字 {_fmt_ms(first_ms)} · 总耗时 {_fmt_ms(total_ms)}"
        )
    return (
        f"📊 请求 <code>{reqs}</code> 次 · ✅ <code>{ok}</code> / ❌ <code>{fail}</code>\n"
        f"🧮 Tokens: <code>{tok_str}</code> tok\n"
        f"📦 代理流量: <code>{traffic_str}</code>\n"
        f"⏱ 平均: 连接 <code>{_fmt_ms(conn_ms)}</code> · 首字 <code>{_fmt_ms(first_ms)}</code> · 总耗时 <code>{_fmt_ms(total_ms)}</code>"
    )


def _append_stats_block(lines: list[str], ps: dict, *, prefix: str = "") -> None:
    """Append multi-line stats without relying on fragile leading spaces."""
    for line in _fmt_proxy_stats(ps).split("\n"):
        lines.append(f"{prefix}{line}" if prefix else line)


def _get_proxy_stats() -> dict[str, dict]:
    """Fetch proxy stats as {name: stats_dict}."""
    from ... import log_db
    return {ps["proxy_name"]: ps for ps in log_db.proxy_stats(limit=1000)}


def _merge_group_stats(members: list[str], pstats: dict) -> dict:
    """Merge stats for group members into one combined dict."""
    merged = {"requests": 0, "successes": 0, "failures": 0,
              "input_tokens": 0, "output_tokens": 0,
              "cache_creation_tokens": 0, "cache_read_tokens": 0,
              "total_tokens": 0,
              "bytes_up": 0, "bytes_down": 0, "total_bytes": 0,
              "connect_sum_ms": 0, "connect_sample_count": 0,
              "first_byte_sum_ms": 0, "first_byte_sample_count": 0,
              "idle_sum_ms": 0, "idle_sample_count": 0,
              "total_sum_ms": 0, "total_sample_count": 0,
              "avg_connect_ms": 0, "avg_first_byte_ms": 0,
              "avg_idle_ms": 0, "avg_total_ms": 0}
    for m in members:
        if m == "direct":
            continue
        ps = pstats.get(m)
        if not ps:
            continue
        merged["requests"] += ps["requests"]
        merged["successes"] += ps["successes"]
        merged["failures"] += ps["failures"]
        merged["input_tokens"] += int(ps.get("input_tokens", 0) or 0)
        merged["output_tokens"] += int(ps.get("output_tokens", 0) or 0)
        merged["cache_creation_tokens"] += int(ps.get("cache_creation_tokens", 0) or 0)
        merged["cache_read_tokens"] += int(ps.get("cache_read_tokens", 0) or 0)
        merged["total_tokens"] += int(ps.get("total_tokens", 0) or 0)
        merged["bytes_up"] += int(ps.get("bytes_up", 0) or 0)
        merged["bytes_down"] += int(ps.get("bytes_down", 0) or 0)
        merged["total_bytes"] += int(ps.get("total_bytes", 0) or 0)
        for sum_key, count_key in (
            ("connect_sum_ms", "connect_sample_count"),
            ("first_byte_sum_ms", "first_byte_sample_count"),
            ("idle_sum_ms", "idle_sample_count"),
            ("total_sum_ms", "total_sample_count"),
        ):
            merged[sum_key] += int(ps.get(sum_key) or 0)
            merged[count_key] += int(ps.get(count_key) or 0)
    for avg_key, sum_key, count_key in (
        ("avg_connect_ms", "connect_sum_ms", "connect_sample_count"),
        ("avg_first_byte_ms", "first_byte_sum_ms", "first_byte_sample_count"),
        ("avg_idle_ms", "idle_sum_ms", "idle_sample_count"),
        ("avg_total_ms", "total_sum_ms", "total_sample_count"),
    ):
        count = int(merged[count_key] or 0)
        merged[avg_key] = round(merged[sum_key] / count) if count else 0
    return merged


_PROXY_PAGE_SIZE = 5


def _page_callback(page: int) -> str:
    return f"px:page:{max(1, int(page or 1))}"


def _split_short_page(payload: str, default_page: int = 1) -> tuple[str, int]:
    raw = (payload or "").strip()
    if ":" not in raw:
        return raw, default_page
    short, _, page_s = raw.rpartition(":")
    try:
        page = int(page_s)
    except ValueError:
        return raw, default_page
    return short, max(1, page)


def _proxy_payload(name: str, page: int = 1) -> str:
    return f"{ui.register_code(name)}:{max(1, int(page or 1))}"


def _pagination_row(current: int, total_pages: int) -> list[dict]:
    if total_pages <= 1:
        return []
    row: list[dict] = []
    if current > 1:
        row.append(ui.btn("⬅ 上一页", _page_callback(current - 1)))
    else:
        row.append(ui.btn("◁ 上一页", "px:noop"))
    row.append(ui.btn(f"{current}/{total_pages}", "px:noop"))
    if current < total_pages:
        row.append(ui.btn("➡ 下一页", _page_callback(current + 1)))
    else:
        row.append(ui.btn("下一页 ▷", "px:noop"))
    return row


def _proxy_by_name(name: str):
    pm.init()
    return pm.all_connectors().get(name)


_GROUP_PAGE_SIZE = 5


def _group_page_callback(page: int) -> str:
    return f"px:grp_page:{max(1, int(page or 1))}"


def _group_payload(name: str, page: int = 1) -> str:
    return f"{ui.register_code(name)}:{max(1, int(page or 1))}"


def _group_by_name(name: str) -> list[str] | None:
    pm.init()
    groups = pm.all_groups()
    return groups.get(name)


# ═══════════════════════════════════════════════════════════════════
#  1. 代理列表（首页）
# ═══════════════════════════════════════════════════════════════════

def show(chat_id: int, message_id: int, cb_id: str = "", page: int = 1) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    pm.init()
    conns = {n: c for n, c in pm.all_connectors().items() if n != "direct"}
    names = list(conns.keys())
    pstats = _get_proxy_stats()

    import math
    total = len(names)
    total_pages = max(1, math.ceil(total / _PROXY_PAGE_SIZE)) if total else 1
    page = max(1, min(int(page or 1), total_pages))
    start_i = (page - 1) * _PROXY_PAGE_SIZE
    page_names = names[start_i:start_i + _PROXY_PAGE_SIZE]
    page_info = f" · 第 {page}/{total_pages} 页" if total_pages > 1 else ""

    lines = [f"🔀 <b>代理管理</b>{page_info}", ""]
    if not names:
        lines.append("<i>暂无代理，点下方按钮添加。</i>")
    else:
        for idx, name in enumerate(page_names, start=start_i + 1):
            c = conns[name]
            icon = _proxy_icon(c)
            lines.append(f"{idx}. {icon} <b>{ui.escape_html(name)}</b> {_type_badge(c.type)}")
            lines.append(f" {_proxy_detail_line(c)}")
            ps = pstats.get(name)
            if ps and ps["requests"] > 0:
                _append_stats_block(lines, ps, prefix=" ")
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()

    rows: list[list[dict]] = []
    for idx in range(0, len(page_names), 2):
        row: list[dict] = []
        for off, name in enumerate(page_names[idx:idx + 2], start=idx):
            c = conns[name]
            num = start_i + off + 1
            row.append(ui.btn(f"{num}. {_type_badge(c.type)} {name}", f"px:view:{_proxy_payload(name, page)}"))
        rows.append(row)

    pag = _pagination_row(page, total_pages)
    if pag:
        rows.append(pag)

    rows.append([ui.btn("➕ 添加代理", "px:add")])
    rows.append([ui.btn("📋 代理组", "px:groups"), ui.btn("🎯 路由规则", "px:routing")])
    rows.append([ui.btn("◀ 返回网络设置", "sys:show:network")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _proxy_detail_text_and_kb(name: str, page: int = 1) -> tuple[str, dict] | tuple[None, None]:
    c = _proxy_by_name(name)
    if c is None or name == "direct":
        return None, None
    pstats = _get_proxy_stats()
    lines = [
        f"{_proxy_icon(c)} <b>{ui.escape_html(name)}</b> {_type_badge(c.type)}",
        _proxy_detail_line(c),
    ]
    ps = pstats.get(name)
    if ps and ps["requests"] > 0:
        lines.append("")
        _append_stats_block(lines, ps)
    else:
        lines.append("")
        lines.append("<i>暂无代理请求统计。</i>")

    payload = _proxy_payload(name, page)
    rows = [
        [ui.btn("🔍 测试代理", f"px:testv:{payload}")],
        [ui.btn("🗑 删除", f"px:del_confirm_v:{payload}")],
        [ui.btn("◀ 返回代理列表", _page_callback(page))],
    ]
    return "\n".join(lines), ui.inline_kb(rows)


def _proxy_view(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效，请返回重试")
        show(chat_id, message_id, page=page)
        return
    ui.answer_cb(cb_id)
    text, kb = _proxy_detail_text_and_kb(name, page=page)
    if text is None:
        ui.edit(chat_id, message_id, f"⚠ 代理 <code>{ui.escape_html(name)}</code> 已不存在",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回代理列表", _page_callback(page))]]))
        return
    ui.edit(chat_id, message_id, text, reply_markup=kb)


# ═══════════════════════════════════════════════════════════════════
#  2. 添加代理
# ═══════════════════════════════════════════════════════════════════

def _add_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "px_add_url")
    ui.edit(
        chat_id, message_id,
        "➕ <b>添加代理</b>\n\n"
        "请输入代理地址（URL 格式）：\n\n"
        "🔒 <b>SS2022</b>\n"
        "<code>ss://&lt;base64&gt;@host:port#名称</code>\n\n"
        "🧦 <b>SOCKS5</b>\n"
        "<code>socks5://[user:pass@]host:port#名称</code>\n\n"
        "<i>URL 中 # 后面的名称可选；如未包含会要求输入。</i>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "px:show")]]),
    )


def _on_add_url_input(chat_id: int, text: str) -> None:
    pm.init()
    try:
        parsed = parse_proxy_url(text.strip())
    except ValueError as exc:
        ui.send(chat_id, f"❌ {ui.escape_html(str(exc))}\n请重新输入代理地址：")
        return

    name = parsed.pop("name", "").strip()
    if name and not _valid_name(name.lower()):
        ui.send(chat_id, "❌ URL 里的名称格式不对：只能包含小写字母/数字/横杠/下划线(1-31字符)。请重新输入代理地址，或去掉 #名称 后再输入：")
        return
    if name and name.lower() == "direct":
        ui.send(chat_id, "❌ <code>direct</code> 是保留名。请重新输入代理地址，或换一个 #名称：")
        return
    if name and name.lower() in pm.all_connectors():
        ui.send(chat_id, f"❌ <code>{ui.escape_html(name.lower())}</code> 已存在。请重新输入代理地址，或换一个 #名称：")
        return
    if not name:
        states.set_state(chat_id, "px_add_name", {"parsed": parsed})
        t = parsed.get("type", "?")
        ui.send(
            chat_id,
            f"✅ 解析成功！类型: <code>{t}</code>\n\n"
            "请输入代理名称（小写字母/数字/横杠，如 <code>us-vmiss</code>）：",
        )
        return
    _finalize_add(chat_id, name.lower(), parsed)


def _on_add_name_input(chat_id: int, text: str) -> None:
    pm.init()
    st = states.get_state(chat_id) or {}
    parsed = (st.get("data") or {}).get("parsed")
    if not parsed:
        states.pop_state(chat_id)
        ui.send(chat_id, "❌ 状态已过期，请重新添加。")
        return
    name = text.strip().lower()
    if not _valid_name(name):
        ui.send(chat_id, "❌ 名称只能包含小写字母、数字、横杠、下划线(1-31字符)，请重试：")
        return
    if name == "direct":
        ui.send(chat_id, "❌ <code>direct</code> 是保留名，请换一个：")
        return
    if name in pm.all_connectors():
        ui.send(chat_id, f"❌ <code>{ui.escape_html(name)}</code> 已存在，请换一个：")
        return
    _finalize_add(chat_id, name, parsed)


def _finalize_add(chat_id: int, name: str, parsed: dict) -> None:
    states.pop_state(chat_id)
    proxy_cfg = {k: v for k, v in parsed.items() if k != "name"}

    progress = ui.send(chat_id, f"⏳ 正在测试 <code>{ui.escape_html(name)}</code> ...")
    msg_id = ((progress or {}).get("result") or {}).get("message_id")

    pm.add_proxy(name, proxy_cfg)
    pm.init()

    try:
        test = asyncio.run(pm.test_proxy(name, timeout=10))
    except Exception as e:
        test = {"ok": False, "error": str(e)[:200]}

    if test.get("ok"):
        text = (
            f"✅ 代理 <code>{ui.escape_html(name)}</code> 添加成功！\n\n"
            f"类型: <code>{proxy_cfg.get('type')}</code>\n"
            f"出口 IP: <code>{test.get('ip', '?')}</code>\n"
            f"延迟: <code>{test.get('latency_ms', '?')}ms</code>"
        )
    else:
        text = (
            f"⚠️ 代理 <code>{ui.escape_html(name)}</code> 已保存，但测试未通过：\n\n"
            f"<code>{ui.escape_html(test.get('error', '未知错误'))}</code>"
        )

    rows = [[ui.btn("◀ 返回代理列表", "px:show")]]
    if msg_id:
        ui.edit(chat_id, msg_id, text, reply_markup=ui.inline_kb(rows))
    else:
        ui.send(chat_id, text, reply_markup=ui.inline_kb(rows))


def _test_proxy(chat_id: int, message_id: int, cb_id: str, name: str, *, back_cb: str = "px:show") -> None:
    ui.answer_cb(cb_id, "测试中...")
    try:
        r = asyncio.run(pm.test_proxy(name, timeout=10))
    except Exception as e:
        r = {"ok": False, "error": str(e)[:200]}

    icon = "✅" if r.get("ok") else "❌"
    if r.get("ok"):
        text = (
            f"{icon} <b>{ui.escape_html(name)}</b>\n"
            f"📍 出口 IP: <code>{ui.escape_html(r.get('ip'))}</code>\n"
            f"⏱ 延迟: <code>{ui.escape_html(r.get('latency_ms'))}ms</code>"
        )
    else:
        text = f"{icon} <b>{ui.escape_html(name)}</b>\n<code>{ui.escape_html(r.get('error', ''))}</code>"
    ui.edit(chat_id, message_id, text,
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回", back_cb)]]))


def _test_proxy_view(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id, page=page)
        return
    _test_proxy(chat_id, message_id, cb_id, name, back_cb=f"px:view:{_proxy_payload(name, page)}")


def _del_confirm(chat_id: int, message_id: int, cb_id: str, name: str, *, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    payload = _proxy_payload(name, page)
    ui.edit(chat_id, message_id,
            f"确定删除代理 <code>{ui.escape_html(name)}</code> ？\n\n"
            f"<i>删除后会同时从所有代理组和路由规则中移除。</i>",
            reply_markup=ui.inline_kb([
                [ui.btn("🗑 确认删除", f"px:del_exec_v:{payload}"),
                 ui.btn("❌ 取消", f"px:view:{payload}")],
            ]))


def _del_confirm_view(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id, page=page)
        return
    _del_confirm(chat_id, message_id, cb_id, name, page=page)


def _del_exec(chat_id: int, message_id: int, cb_id: str, name: str, *, page: int = 1) -> None:
    pm.remove_proxy(name)
    ui.answer_cb(cb_id, f"已删除 {name}")
    show(chat_id, message_id, page=page)


def _del_exec_view(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    name = ui.resolve_code(short)
    if not name:
        ui.answer_cb(cb_id, "短码已失效")
        show(chat_id, message_id, page=page)
        return
    _del_exec(chat_id, message_id, cb_id, name, page=page)


# ═══════════════════════════════════════════════════════════════════
#  3. 代理组管理
# ═══════════════════════════════════════════════════════════════════

def _show_groups(chat_id: int, message_id: int, cb_id: str, page: int = 1) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    pm.init()
    groups = pm.all_groups()
    group_names = list(groups.keys())
    pstats = _get_proxy_stats()

    import math
    total = len(group_names)
    total_pages = max(1, math.ceil(total / _GROUP_PAGE_SIZE)) if total else 1
    page = max(1, min(int(page or 1), total_pages))
    start_i = (page - 1) * _GROUP_PAGE_SIZE
    page_names = group_names[start_i:start_i + _GROUP_PAGE_SIZE]
    page_info = f" · 第 {page}/{total_pages} 页" if total_pages > 1 else ""

    lines = [f"📋 <b>代理组</b>{page_info}", "",
             "<i>按成员顺序故障转移：前一个连不上自动切换下一个。</i>", ""]
    if not group_names:
        lines.append("<i>暂无代理组。</i>")
    else:
        for idx, gname in enumerate(page_names, start=start_i + 1):
            members = groups[gname]
            merged = _merge_group_stats(members, pstats)
            lines.append(f"{idx}. 📋 <b>{ui.escape_html(gname)}</b>")
            lines.append(f" 成员: <code>{ui.escape_html(' → '.join(members))}</code>")
            if merged["requests"] > 0:
                lines.append(" 🧾 组内总计")
                _append_stats_block(lines, merged, prefix=" ")
            else:
                lines.append(" <i>暂无组内代理请求统计。</i>")
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()

    rows: list[list[dict]] = []
    for idx in range(0, len(page_names), 2):
        row: list[dict] = []
        for off, gname in enumerate(page_names[idx:idx + 2], start=idx):
            num = start_i + off + 1
            row.append(ui.btn(f"{num}. 📋 {gname}", f"px:grp_view:{_group_payload(gname, page)}"))
        rows.append(row)

    pag = _pagination_row(page, total_pages)
    if pag:
        # translate px:page callbacks to group callbacks
        grp_pag = []
        for b in pag:
            cb = b.get("callback_data", "")
            if cb.startswith("px:page:"):
                b = dict(b); b["callback_data"] = "px:grp_page:" + cb[8:]
            elif cb == "px:noop":
                b = dict(b); b["callback_data"] = "px:grp_noop"
            grp_pag.append(b)
        rows.append(grp_pag)

    rows.append([ui.btn("➕ 新建代理组", "px:grp_add")])
    rows.append([ui.btn("🔀 代理列表", "px:show"), ui.btn("🎯 路由规则", "px:routing")])
    rows.append([ui.btn("◀ 返回网络设置", "sys:show:network")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _group_detail_text_and_kb(gname: str, page: int = 1) -> tuple[str, dict] | tuple[None, None]:
    members = _group_by_name(gname)
    if members is None:
        return None, None
    merged = _merge_group_stats(members, _get_proxy_stats())
    lines = [
        f"📋 <b>{ui.escape_html(gname)}</b>",
        f"成员: <code>{ui.escape_html(' → '.join(members))}</code>",
        "",
    ]
    if merged["requests"] > 0:
        lines.append("🧾 组内总计")
        _append_stats_block(lines, merged)
    else:
        lines.append("<i>暂无组内代理请求统计。</i>")

    payload = _group_payload(gname, page)
    rows = [
        [ui.btn("🔍 测试代理组", f"px:grp_test_v:{payload}")],
        [ui.btn("✏️ 编辑成员", f"px:grp_edit_v:{payload}"), ui.btn("🗑 删除", f"px:grp_del_ask:{payload}")],
        [ui.btn("◀ 返回代理组", _group_page_callback(page))],
    ]
    return "\n".join(lines), ui.inline_kb(rows)


def _grp_view(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    gname = ui.resolve_code(short)
    if not gname:
        ui.answer_cb(cb_id, "短码已失效，请返回重试")
        _show_groups(chat_id, message_id, "", page=page)
        return
    ui.answer_cb(cb_id)
    text, kb = _group_detail_text_and_kb(gname, page=page)
    if text is None:
        ui.edit(chat_id, message_id, f"⚠ 代理组 <code>{ui.escape_html(gname)}</code> 已不存在",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回代理组", _group_page_callback(page))]]))
        return
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def _grp_add_start(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "px_grp_add_name")
    ui.edit(chat_id, message_id,
            "➕ <b>新建代理组</b>\n\n请输入组名（小写字母/数字/横杠）：",
            reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "px:groups")]]))


def _on_grp_add_name(chat_id: int, text: str) -> None:
    pm.init()
    name = text.strip().lower()
    if not _valid_name(name):
        ui.send(chat_id, "❌ 名称格式不对，请重试：")
        return
    if name in pm.all_groups():
        ui.send(chat_id, f"❌ 组名 <code>{ui.escape_html(name)}</code> 已存在：")
        return
    states.set_state(chat_id, "px_grp_pick_members", {"group_name": name, "members": []})
    _send_member_picker(chat_id, name, [])


def _send_member_picker(chat_id: int, group_name: str, current: list[str],
                        *, message_id: int = 0) -> None:
    """Show proxy picker for group members."""
    conns = pm.all_connectors()
    lines = [
        f"📋 <b>代理组: {ui.escape_html(group_name)}</b>",
        "",
        f"当前成员: {' → '.join(current) if current else '<i>空</i>'}",
        "",
        "点击下方代理添加到组中：",
    ]
    rows = []
    proxy_row = []
    for n, c in conns.items():
        if n in current:
            continue  # already added
        badge = _type_badge(c.type)
        proxy_row.append(ui.btn(f"{badge} {n}", f"px:grp_pick:{n}"))
        if len(proxy_row) >= 3:
            rows.append(proxy_row)
            proxy_row = []
    if proxy_row:
        rows.append(proxy_row)
    if current:
        rm_row = []
        for n in current:
            rm_row.append(ui.btn(f"➖ {n}", f"px:grp_rm:{n}"))
            if len(rm_row) >= 2:
                rows.append(rm_row)
                rm_row = []
        if rm_row:
            rows.append(rm_row)
        rows.append([ui.btn("🧹 清空", "px:grp_clear"), ui.btn("✅ 完成保存", "px:grp_save")])
    back_page = 1
    try:
        st = states.get_state(chat_id) or {}
        back_page = int((st.get("data") or {}).get("page") or 1)
    except Exception:
        back_page = 1
    rows.append([ui.btn("❌ 取消", _group_page_callback(back_page))])

    text = "\n".join(lines)
    if message_id:
        ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))
    else:
        ui.send(chat_id, text, reply_markup=ui.inline_kb(rows))


def _grp_pick_member(chat_id: int, message_id: int, cb_id: str, proxy_name: str) -> None:
    ui.answer_cb(cb_id)
    st = states.get_state(chat_id) or {}
    data = st.get("data") or {}
    group_name = data.get("group_name", "")
    members = list(data.get("members") or [])
    if proxy_name not in members:
        members.append(proxy_name)
    data["members"] = members
    states.set_state(chat_id, st.get("action", "px_grp_pick_members"), data)
    _send_member_picker(chat_id, group_name, members, message_id=message_id)


def _grp_rm_member(chat_id: int, message_id: int, cb_id: str, proxy_name: str) -> None:
    ui.answer_cb(cb_id)
    st = states.get_state(chat_id) or {}
    data = st.get("data") or {}
    group_name = data.get("group_name", "")
    members = [m for m in list(data.get("members") or []) if m != proxy_name]
    data["members"] = members
    states.set_state(chat_id, st.get("action", "px_grp_pick_members"), data)
    _send_member_picker(chat_id, group_name, members, message_id=message_id)


def _grp_clear_members(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    st = states.get_state(chat_id) or {}
    data = st.get("data") or {}
    group_name = data.get("group_name", "")
    data["members"] = []
    states.set_state(chat_id, st.get("action", "px_grp_pick_members"), data)
    _send_member_picker(chat_id, group_name, [], message_id=message_id)


def _grp_save(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    st = states.get_state(chat_id) or {}
    data = st.get("data") or {}
    group_name = data.get("group_name", "")
    page = int(data.get("page") or 1)
    members = data.get("members") or []
    if not group_name or not members:
        ui.answer_cb(cb_id, "数据不完整")
        return
    pm.add_group(group_name, members)
    states.pop_state(chat_id)
    ui.edit(chat_id, message_id,
            f"✅ 代理组 <code>{ui.escape_html(group_name)}</code> 已保存\n"
            f"成员: {' → '.join(members)}",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回代理组", _group_page_callback(page))]]))


def _grp_edit(chat_id: int, message_id: int, cb_id: str, gname: str, *, page: int = 1) -> None:
    ui.answer_cb(cb_id)
    pm.init()
    members = pm.get_group(gname) or []
    states.set_state(chat_id, "px_grp_pick_members", {"group_name": gname, "members": members, "page": page})
    _send_member_picker(chat_id, gname, members, message_id=message_id)


def _grp_edit_view(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    gname = ui.resolve_code(short)
    if not gname:
        ui.answer_cb(cb_id, "短码已失效")
        _show_groups(chat_id, message_id, "", page=page)
        return
    _grp_edit(chat_id, message_id, cb_id, gname, page=page)


def _grp_test(chat_id: int, message_id: int, cb_id: str, gname: str, *, back_cb: str = "px:groups") -> None:
    ui.answer_cb(cb_id, "测试中...")
    try:
        results = asyncio.run(pm.test_group(gname, timeout=10))
    except Exception as e:
        results = [{"name": gname, "ok": False, "error": str(e)[:200]}]

    lines = [f"🔍 <b>测试代理组: {ui.escape_html(gname)}</b>", ""]
    all_ok = True
    for r in results:
        ok = r.get("ok")
        if not ok:
            all_ok = False
        icon = "✅" if ok else "❌"
        n = r.get("name", "?")
        if ok:
            lines.append(f"{icon} {n} → <code>{r.get('ip')}</code> ({r.get('latency_ms')}ms)")
        else:
            lines.append(f"{icon} {n}: {ui.escape_html(r.get('error', '')[:80])}")

    lines.append(f"\n{'✅ 全部通过' if all_ok else '⚠️ 部分失败'}")
    ui.edit(chat_id, message_id, "\n".join(lines),
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回", back_cb)]]))


def _grp_test_view(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    gname = ui.resolve_code(short)
    if not gname:
        ui.answer_cb(cb_id, "短码已失效")
        _show_groups(chat_id, message_id, "", page=page)
        return
    _grp_test(chat_id, message_id, cb_id, gname, back_cb=f"px:grp_view:{_group_payload(gname, page)}")


def _grp_del_ask(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    gname = ui.resolve_code(short)
    if not gname:
        ui.answer_cb(cb_id, "短码已失效")
        _show_groups(chat_id, message_id, "", page=page)
        return
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id,
            f"确定删除代理组 <code>{ui.escape_html(gname)}</code> ？\n\n"
            f"<i>删除后会同时从路由规则中移除该组引用。</i>",
            reply_markup=ui.inline_kb([
                [ui.btn("🗑 确认删除", f"px:grp_del_exec:{_group_payload(gname, page)}"),
                 ui.btn("❌ 取消", f"px:grp_view:{_group_payload(gname, page)}")],
            ]))


def _grp_del_exec(chat_id: int, message_id: int, cb_id: str, payload: str) -> None:
    short, page = _split_short_page(payload)
    gname = ui.resolve_code(short)
    if not gname:
        ui.answer_cb(cb_id, "短码已失效")
        _show_groups(chat_id, message_id, "", page=page)
        return
    pm.remove_group(gname)
    ui.answer_cb(cb_id, f"已删除组 {gname}")
    _show_groups(chat_id, message_id, "", page=page)


def _grp_del(chat_id: int, message_id: int, cb_id: str, gname: str) -> None:
    pm.remove_group(gname)
    ui.answer_cb(cb_id, f"已删除组 {gname}")
    _show_groups(chat_id, message_id, "")


# ═══════════════════════════════════════════════════════════════════
#  4. 路由规则
# ═══════════════════════════════════════════════════════════════════

# ── index maps (OAuth/channel keys can exceed TG 64-byte callback limit) ──

_item_index: dict[int, list[str]] = {}   # chat_id -> [key0, key1, ...]


def _set_index(chat_id: int, keys: list[str]) -> None:
    _item_index[chat_id] = list(keys)


def _get_key(chat_id: int, idx: int) -> Optional[str]:
    arr = _item_index.get(chat_id, [])
    return arr[idx] if 0 <= idx < len(arr) else None


# ── routing overview ─────────────────────────────────────────────

def _show_routing(chat_id: int, message_id: int, cb_id: str) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    pm.init()
    r = pm.get_routing()

    default_route = r.get("default", "direct")
    direct_fallback = bool(r.get("directFallback", False))
    acct_count = len(r.get("accounts") or {})
    ch_count = len(r.get("channels") or {})
    model_count = len(r.get("models") or {})

    # 功能路由摘要
    func_keys = {"telegram": "Telegram", "oauth_anthropic": "Anthropic 家族", "oauth_openai": "OpenAI & Grok 家族"}
    func_lines = []
    for k, label in func_keys.items():
        v = r.get(k)
        if v:
            func_lines.append(f"  • {label} → <code>{ui.escape_html(str(v))}</code>")

    lines = [
        "🎯 <b>路由规则</b>", "",
        "<i>优先级: 账号 = 渠道 > 模型 > 功能路由 > 默认路由</i>", "",
        f"📌 默认路由: <code>{ui.escape_html(str(default_route))}</code>",
        f"🛟 直连兜底: <b>{'开启' if direct_fallback else '关闭'}</b>",
        (
            "<i>已配置的非直连路由异常时，会在代理链末尾尝试 direct；可能暴露本机出口。</i>"
            if direct_fallback else
            "<i>已配置的非直连路由异常时保持失败，不会静默改走 direct；未配置网络规则时仍正常直连。</i>"
        ),
        "",
        "📡 功能路由：",
    ]
    if func_lines:
        lines.extend(func_lines)
    else:
        lines.append("  <i>全部走默认</i>")
    lines.append("")
    lines.append(f"👤 账号路由: <code>{acct_count}</code> 条 · 📦 渠道路由: <code>{ch_count}</code> 条 · 🤖 模型路由: <code>{model_count}</code> 条")

    rows = [
        [ui.btn("📌 默认路由", "px:rt_pick:default"),
         ui.btn("📡 功能路由", "px:rt_func")],
        [ui.btn(f"🛟 直连兜底：{'开启' if direct_fallback else '关闭'}", "px:rt_df")],
        [ui.btn("👤 账号路由", "px:rt_accounts"),
         ui.btn("📦 渠道路由", "px:rt_channels"),
         ui.btn("🤖 模型路由", "px:rt_models")],
        [ui.btn("◀ 返回网络设置", "sys:show:network")],
    ]
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _toggle_direct_fallback(chat_id: int, message_id: int, cb_id: str) -> None:
    enabled = not pm.direct_fallback_enabled()
    pm.set_direct_fallback(enabled)
    ui.answer_cb(cb_id, "直连兜底已开启" if enabled else "直连兜底已关闭")
    _show_routing(chat_id, message_id, "")


# ── target picker (shared for all) ──────────────────────────────

def _show_target_picker(chat_id: int, message_id: int, cb_id: str,
                        context: str, *, title: str = "",
                        back_cb: str = "px:routing") -> None:
    """Target picker for simple contexts (default/telegram/oauth_*)."""
    if cb_id:
        ui.answer_cb(cb_id)
    # For short contexts (default, telegram, etc.), use inline callback_data
    targets = _all_targets()
    title = title or f"<code>{ui.escape_html(context)}</code>"
    lines = [f"🎯 {title}", "", "<i>选择路由目标：</i>"]
    rows = []
    for name, label in targets:
        cb_data = f"px:rt_do:{context}:{name}"
        if len(cb_data.encode()) <= 64:
            rows.append([ui.btn(label, cb_data)])
        else:
            # Fallback: store in state
            states.set_state(chat_id, "px_rt_pending", {"context": context, "back": back_cb})
            return _show_target_picker_stateful(chat_id, message_id, "", title=title, back_cb=back_cb)
    del_data = f"px:rt_do:{context}:__del__"
    if len(del_data.encode()) <= 64:
        rows.append([ui.btn("🚫 清除规则（走默认）", del_data)])
    rows.append([ui.btn("◀ 返回", back_cb)])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _show_target_picker_stateful(chat_id: int, message_id: int, cb_id: str,
                                  *, title: str = "",
                                  back_cb: str = "px:routing") -> None:
    """Target picker that reads context from session state (for long keys)."""
    if cb_id:
        ui.answer_cb(cb_id)
    targets = _all_targets()
    lines = [f"🎯 {title}", "", "<i>选择路由目标：</i>"]
    rows = []
    for name, label in targets:
        # px:rt_s:<target> — context is read from state
        rows.append([ui.btn(label, f"px:rt_s:{name}")])
    rows.append([ui.btn("🚫 清除规则（走默认）", "px:rt_s:__del__")])
    rows.append([ui.btn("◀ 返回", back_cb)])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


def _rt_do(chat_id: int, message_id: int, cb_id: str,
           context: str, value: str) -> None:
    ui.answer_cb(cb_id)
    # Determine which list page to go back to
    back_fn = _show_routing
    if context.startswith("accounts:"):
        back_fn = _show_account_routing
    elif context.startswith("channels:"):
        back_fn = _show_channel_routing
    elif context.startswith("models:"):
        back_fn = _show_model_routing

    if value == "__del__":
        if ":" in context:
            section, key = context.split(":", 1)
            pm.remove_routing(key, section=section)
        else:
            pm.remove_routing(context)
    else:
        if ":" in context:
            section, key = context.split(":", 1)
            pm.set_routing(key, value, section=section)
        else:
            pm.set_routing(context, value)
    back_fn(chat_id, message_id, "")


# ── 功能路由 ─────────────────────────────────────────────────────

def _show_func_routing(chat_id: int, message_id: int, cb_id: str) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    r = pm.get_routing()
    funcs = [
        ("telegram", "📱 Telegram", "📱 Telegram", "Bot 所有功能调用"),
        ("oauth_anthropic", f"{ui.provider_tag('claude', full=True)} 家族", f"{ui.provider_icon('claude')} Anthropic 家族", "OAuth、登录/刷新、渠道请求、测试、/v1/messages"),
        ("oauth_openai", f"{ui.provider_tag('openai')}/{ui.provider_tag('xai')} 家族", f"{ui.provider_icon('openai')}/{ui.provider_icon('xai')} OpenAI & Grok 家族", "OAuth、登录/刷新、渠道请求、测试、OpenAI-style / Grok 入口"),
    ]
    lines = ["📡 <b>功能路由</b>", "",
             "<i>家族级默认出口；账号/渠道/模型未命中时走这里，未设置则走默认路由。</i>",
             f"<i>Telegram / {ui.provider_tag('claude', full=True)} 家族 / {ui.provider_tag('openai')}/{ui.provider_tag('xai')} 家族均支持代理组、代理、直连、默认。</i>", ""]
    for key, label, _btn_label, desc in funcs:
        val = r.get(key)
        route = f"<code>{ui.escape_html(str(val))}</code>" if val else "<i>默认</i>"
        lines.append(f"{label}  {desc}")
        lines.append(f"  当前: {route}")
        lines.append("")

    rows = []
    for key, _label, btn_label, _ in funcs:
        short = btn_label.split(" ", 1)[-1]
        rows.append([ui.btn(f"✏️ {short}", f"px:rt_pick:{key}")])
    rows.append([ui.btn("◀ 返回路由规则", "px:routing")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


# ── 账号路由 (OAuth) ────────────────────────────────────────────

def _show_account_routing(chat_id: int, message_id: int, cb_id: str,
                          page: int = 1) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    r = pm.get_routing()
    acct_cfg = r.get("accounts") or {}
    chs = [c for c in registry.all_channels() if c.type == "oauth"]

    keys = [c.key for c in chs]
    _set_index(chat_id, keys)

    lines = ["👤 <b>账号路由</b>", "",
             "<i>为 OAuth 账号指定出站代理。点击修改。</i>", ""]
    if not chs:
        lines.append("<i>没有 OAuth 账号。</i>")
    else:
        for i, c in enumerate(chs):
            icon = _oauth_provider_icon(c)
            route = acct_cfg.get(c.key)
            r_str = f"→ <code>{ui.escape_html(str(route))}</code>" if route else ""
            display = ui.channel_display_name(c.key, with_family=False)
            lines.append(f"{icon} {ui.escape_html(display)} {r_str}")

    rows = []
    for i, c in enumerate(chs):
        icon = _oauth_provider_icon(c)
        route = acct_cfg.get(c.key)
        tag = f" [{route}]" if route else ""
        display = ui.channel_display_name(c.key, with_family=False)
        rows.append([ui.btn(f"{icon} {display}{tag}", f"px:rt_item:a:{i}")])

    rows.append([ui.btn("◀ 返回路由规则", "px:routing")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


# ── 渠道路由 (API) ──────────────────────────────────────────────

def _show_channel_routing(chat_id: int, message_id: int, cb_id: str) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    r = pm.get_routing()
    ch_cfg = r.get("channels") or {}
    chs = [c for c in registry.all_channels() if c.type == "api"]

    keys = [c.key for c in chs]
    _set_index(chat_id, keys)

    lines = ["📦 <b>渠道路由</b>", "",
             "<i>为 API 渠道指定出站代理。点击修改。</i>", ""]
    if not chs:
        lines.append("<i>没有 API 渠道。</i>")
    else:
        for c in chs:
            route = ch_cfg.get(c.key)
            r_str = f"→ <code>{ui.escape_html(str(route))}</code>" if route else ""
            lines.append(f"🔑 {ui.escape_html(c.display_name)} {r_str}")

    rows = []
    for i, c in enumerate(chs):
        route = ch_cfg.get(c.key)
        tag = f" [{route}]" if route else ""
        rows.append([ui.btn(f"🔑 {c.display_name}{tag}", f"px:rt_item:c:{i}")])

    rows.append([ui.btn("◀ 返回路由规则", "px:routing")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


# ── 模型路由（分页，每页 8 个）───────────────────────────────────

_MODEL_PAGE_SIZE = 8


def _show_model_routing(chat_id: int, message_id: int, cb_id: str,
                        page: int = 1) -> None:
    if cb_id:
        ui.answer_cb(cb_id)
    r = pm.get_routing()
    model_cfg = r.get("models") or {}
    try:
        models = registry.available_models()
    except Exception:
        models = []

    _set_index(chat_id, models)
    total = len(models)
    total_pages = max(1, (total + _MODEL_PAGE_SIZE - 1) // _MODEL_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * _MODEL_PAGE_SIZE
    page_models = models[start:start + _MODEL_PAGE_SIZE]

    lines = ["🤖 <b>模型路由</b>", "",
             "<i>为模型指定出站代理。点击修改。</i>", ""]
    if not models:
        lines.append("<i>没有可用模型。</i>")
    else:
        # Show current page models in text
        for i, m in enumerate(page_models, start=start):
            route = model_cfg.get(m)
            r_str = f"→ <code>{ui.escape_html(str(route))}</code>" if route else ""
            icon = _route_model_icon(m)
            lines.append(f"{icon} <code>{ui.escape_html(m)}</code> {r_str}")
        lines.append(f"\n第 {page}/{total_pages} 页 · 共 {total} 个模型")

    rows = []
    for i, m in enumerate(page_models, start=start):
        route = model_cfg.get(m)
        tag = f" [{route}]" if route else ""
        icon = _route_model_icon(m)
        rows.append([ui.btn(f"{icon} {m}{tag}", f"px:rt_item:m:{i}")])

    # Pagination
    pag = []
    if page > 1:
        pag.append(ui.btn("◀ 上一页", f"px:rt_mp:{page - 1}"))
    if page < total_pages:
        pag.append(ui.btn("下一页 ▶", f"px:rt_mp:{page + 1}"))
    if pag:
        rows.append(pag)

    rows.append([ui.btn("◀ 返回路由规则", "px:routing")])
    ui.edit(chat_id, message_id, "\n".join(lines), reply_markup=ui.inline_kb(rows))


# ── unified item → target picker ────────────────────────────────

def _rt_item_pick(chat_id: int, message_id: int, cb_id: str,
                  category: str, idx: int) -> None:
    """Item selected from a list → show target picker.

    Stores the routing context (section + full key) in session state to avoid
    exceeding Telegram's 64-byte callback_data limit.
    """
    key = _get_key(chat_id, idx)
    if not key:
        ui.answer_cb(cb_id, "项目不存在")
        return

    section_map = {"a": "accounts", "c": "channels", "m": "models"}
    back_map = {"a": "px:rt_accounts", "c": "px:rt_channels", "m": "px:rt_models"}
    icon_map = {"a": "👤", "c": "📦", "m": "🤖"}
    section = section_map.get(category, "")
    back = back_map.get(category, "px:routing")
    icon = icon_map.get(category, "")

    display = key
    if category == "a":
        chs = {c.key: c for c in registry.all_channels() if c.type == "oauth"}
        if key in chs:
            display = ui.channel_display_name(chs[key].key, with_family=False)
    elif category == "c":
        chs = {c.key: c for c in registry.all_channels() if c.type == "api"}
        if key in chs:
            display = chs[key].display_name

    # Store full context in state (avoids 64-byte callback limit)
    context = f"{section}:{key}"
    states.set_state(chat_id, "px_rt_pending", {"context": context, "back": back})

    _show_target_picker_stateful(
        chat_id, message_id, cb_id,
        title=f"{icon} <b>{ui.escape_html(display)}</b>",
        back_cb=back,
    )


# ═══════════════════════════════════════════════════════════════════
#  Callback / text dispatchers
# ═══════════════════════════════════════════════════════════════════

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    # 代理列表
    if data == "px:show":
        show(chat_id, message_id, cb_id); return True
    if data == "px:noop":
        ui.answer_cb(cb_id); return True
    if data.startswith("px:page:"):
        try:
            show(chat_id, message_id, cb_id, page=int(data[8:])); return True
        except ValueError:
            ui.answer_cb(cb_id); return True
    if data.startswith("px:view:"):
        _proxy_view(chat_id, message_id, cb_id, data[8:]); return True
    if data == "px:add":
        _add_start(chat_id, message_id, cb_id); return True
    if data.startswith("px:testv:"):
        _test_proxy_view(chat_id, message_id, cb_id, data[9:]); return True
    if data.startswith("px:test:"):
        _test_proxy(chat_id, message_id, cb_id, data[8:]); return True
    if data.startswith("px:del_confirm_v:"):
        _del_confirm_view(chat_id, message_id, cb_id, data[17:]); return True
    if data.startswith("px:del_confirm:"):
        _del_confirm(chat_id, message_id, cb_id, data[15:]); return True
    if data.startswith("px:del_exec_v:"):
        _del_exec_view(chat_id, message_id, cb_id, data[14:]); return True
    if data.startswith("px:del_exec:"):
        _del_exec(chat_id, message_id, cb_id, data[12:]); return True

    # 代理组
    if data == "px:groups":
        _show_groups(chat_id, message_id, cb_id); return True
    if data == "px:grp_noop":
        ui.answer_cb(cb_id); return True
    if data.startswith("px:grp_page:"):
        try:
            _show_groups(chat_id, message_id, cb_id, page=int(data[12:])); return True
        except ValueError:
            ui.answer_cb(cb_id); return True
    if data.startswith("px:grp_view:"):
        _grp_view(chat_id, message_id, cb_id, data[12:]); return True
    if data == "px:grp_add":
        _grp_add_start(chat_id, message_id, cb_id); return True
    if data.startswith("px:grp_edit_v:"):
        _grp_edit_view(chat_id, message_id, cb_id, data[14:]); return True
    if data.startswith("px:grp_edit:"):
        _grp_edit(chat_id, message_id, cb_id, data[12:]); return True
    if data.startswith("px:grp_test_v:"):
        _grp_test_view(chat_id, message_id, cb_id, data[14:]); return True
    if data.startswith("px:grp_test:"):
        _grp_test(chat_id, message_id, cb_id, data[12:]); return True
    if data.startswith("px:grp_del_ask:"):
        _grp_del_ask(chat_id, message_id, cb_id, data[15:]); return True
    if data.startswith("px:grp_del_exec:"):
        _grp_del_exec(chat_id, message_id, cb_id, data[16:]); return True
    if data.startswith("px:grp_del:"):
        _grp_del(chat_id, message_id, cb_id, data[11:]); return True
    if data.startswith("px:grp_pick:"):
        _grp_pick_member(chat_id, message_id, cb_id, data[12:]); return True
    if data.startswith("px:grp_rm:"):
        _grp_rm_member(chat_id, message_id, cb_id, data[10:]); return True
    if data == "px:grp_clear":
        _grp_clear_members(chat_id, message_id, cb_id); return True
    if data == "px:grp_save":
        _grp_save(chat_id, message_id, cb_id); return True

    # 路由规则
    if data == "px:routing":
        _show_routing(chat_id, message_id, cb_id); return True
    if data == "px:rt_df":
        _toggle_direct_fallback(chat_id, message_id, cb_id); return True
    if data == "px:rt_func":
        _show_func_routing(chat_id, message_id, cb_id); return True
    if data == "px:rt_accounts":
        _show_account_routing(chat_id, message_id, cb_id); return True
    if data == "px:rt_channels":
        _show_channel_routing(chat_id, message_id, cb_id); return True
    if data == "px:rt_models":
        _show_model_routing(chat_id, message_id, cb_id); return True

    # Model pagination
    if data.startswith("px:rt_mp:"):
        try:
            _show_model_routing(chat_id, message_id, cb_id, page=int(data[9:]))
            return True
        except ValueError:
            pass

    # Unified item picker: px:rt_item:<category>:<index>
    if data.startswith("px:rt_item:"):
        parts = data[11:].split(":", 1)
        if len(parts) == 2:
            try:
                idx_val = int(parts[1])
            except ValueError:
                idx_val = None
            if idx_val is not None:
                _rt_item_pick(chat_id, message_id, cb_id, parts[0], idx_val)
                return True

    # Target picker: px:rt_pick:<context>
    if data.startswith("px:rt_pick:"):
        # Determine back button based on context
        ctx = data[11:]
        back = "px:routing"
        if ctx.startswith("accounts:"): back = "px:rt_accounts"
        elif ctx.startswith("channels:"): back = "px:rt_channels"
        elif ctx.startswith("models:"): back = "px:rt_models"
        _show_target_picker(chat_id, message_id, cb_id, ctx, back_cb=back)
        return True

    # Stateful save: px:rt_s:<target> (context from state)
    if data.startswith("px:rt_s:"):
        target = data[8:]
        st = states.get_state(chat_id) or {}
        ctx_data = st.get("data") or {}
        context = ctx_data.get("context", "")
        if context:
            states.pop_state(chat_id)
            _rt_do(chat_id, message_id, cb_id, context, target)
            return True

    # Save/delete: px:rt_do:<context>:<value> (inline, for short contexts)
    if data.startswith("px:rt_do:"):
        rest = data[9:]
        idx = rest.rfind(":")
        if idx > 0:
            _rt_do(chat_id, message_id, cb_id, rest[:idx], rest[idx + 1:])
            return True

    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == "px_add_url":
        _on_add_url_input(chat_id, text); return True
    if action == "px_add_name":
        _on_add_name_input(chat_id, text); return True
    if action == "px_grp_add_name":
        _on_grp_add_name(chat_id, text); return True
    return False
