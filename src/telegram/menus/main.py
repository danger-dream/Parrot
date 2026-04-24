"""主菜单与 /start 欢迎页。

布局（2 列 4 行）：
  [📊 状态总览]  [📋 最近日志]
  [📈 统计汇总]  [🔀 渠道管理]
  [🔐 管理 OAuth] [🔑 管理 API Key]
  [⚙ 系统设置]   [❓ 帮助]
"""

from __future__ import annotations

from ... import __version__, affinity, concurrency, config, log_db, oauth_manager, public_ip, state_db
from ...oauth_ids import account_key as _account_key
from ...channel import registry
from .. import ui


def _kb() -> dict:
    return ui.inline_kb([
        [ui.btn("📊 状态总览", "menu:status"),
         ui.btn("📋 最近日志", "menu:logs")],
        [ui.btn("📈 统计汇总", "menu:stats"),
         ui.btn("🔀 渠道管理", "menu:channel")],
        [ui.btn("🔐 管理 OAuth", "menu:oauth"),
         ui.btn("🔑 管理 API Key", "menu:apikey")],
        [ui.btn("🔁 模型映射", "map:show"),
         ui.btn("🧩 OAuth 默认", "odm:show")],
        [ui.btn("⚙ 系统设置", "menu:settings"),
         ui.btn("❓ 帮助", "menu:help")],
    ])


def _quota_hot_count(threshold_pct: float = 80.0) -> int:
    """返回当前用量 >= threshold 的 OAuth 账户数量（不含已禁用）。"""
    # 使用 oauth_manager.list_accounts() 作为唯一数据源，
    # 覆盖所有 provider（Claude + OpenAI），而非仅 cfg.oauthAccounts。
    accounts = oauth_manager.list_accounts()
    account_keys = [
        _account_key(a) for a in accounts
        if a.get("email") and not a.get("disabled_reason")
    ]
    if account_keys:
        oauth_manager.ensure_quota_fresh_sync(account_keys)
    n = 0
    for acc in accounts:
        email = acc.get("email")
        if not email:
            continue
        ak = _account_key(acc)
        row = state_db.quota_load(ak)
        if not row:
            continue
        utils = [row.get(k) for k in ("five_hour_util", "seven_day_util",
                                       "sonnet_util", "opus_util")]
        if any(u is not None and u >= threshold_pct for u in utils):
            n += 1
    return n


def _first_run_banner() -> str:
    """空配置时的引导文字。"""
    return (
        "⚠ <b>首次使用检测</b>\n\n"
        "请按以下步骤快速启用服务：\n"
        "1️⃣ 「🔐 管理 OAuth」→ 登录获取 Token\n"
        "    或「🔀 渠道管理」→ 添加第三方 API 渠道\n"
        "2️⃣ 「🔑 管理 API Key」→ 创建下游调用用的 Key\n"
        "3️⃣ 下游客户端配置代理 URL 即可使用\n"
    )


def _overview() -> str:
    """主菜单顶部的服务一览。"""
    cfg = config.get()
    oauth_accounts = cfg.get("oauthAccounts") or []
    api_channels = cfg.get("channels") or []
    api_keys = cfg.get("apiKeys") or {}

    oauth_enabled = sum(
        1 for a in oauth_accounts
        if a.get("enabled", True) and not a.get("disabled_reason")
    )
    oauth_quota = sum(1 for a in oauth_accounts if a.get("disabled_reason") == "quota")
    oauth_user = sum(1 for a in oauth_accounts if a.get("disabled_reason") == "user")
    oauth_auth_err = sum(1 for a in oauth_accounts if a.get("disabled_reason") == "auth_error")

    api_enabled = sum(
        1 for c in api_channels
        if c.get("enabled", True) and not c.get("disabled_reason")
    )

    chs = registry.all_channels()
    total_registered = len(chs)

    listen = cfg.get("listen") or {}
    port = listen.get("port", 18082)
    cch = cfg.get("cchMode", "disabled")
    mode = cfg.get("channelSelection", "smart")

    # 配额预警高亮（≥80%）
    quota_hot = _quota_hot_count(80.0)

    lines = [
        "🦜 <b>Parrot · TG 管理面板</b> <code>v" + __version__ + "</code>",
        "",
        f"📡 监听 <code>:{port}</code> · 调度 <code>{mode}</code> · CCH <code>{cch}</code>",
        f"🔐 OAuth: {oauth_enabled}/{len(oauth_accounts)} 可用"
        + (f" · 🔒 配额 {oauth_quota}" if oauth_quota else "")
        + (f" · 🚫 用户 {oauth_user}" if oauth_user else "")
        + (f" · ❌ 认证失败 {oauth_auth_err}" if oauth_auth_err else ""),
        f"🔀 API 渠道: {api_enabled}/{len(api_channels)} 可用 · registry {total_registered}",
        f"🔑 下游 Key: {len(api_keys)} 个 · 🔗 亲和绑定 {affinity.count()}",
    ]

    # 并发队列（总开关关闭时标注为"关"，开启时显示在途 / 排队 / 追踪渠道数）
    cc_cfg = cfg.get("concurrency") or {}
    if bool(cc_cfg.get("enabled", True)):
        cc_totals = concurrency.totals()
        inf = cc_totals["in_flight"]
        wait = cc_totals["waiting"]
        track = cc_totals["tracked_channels"]
        icon = "⚡"
        if wait > 0:
            icon = "🟡"  # 有排队 → 有压力
        elif inf == 0 and track == 0:
            icon = "💤"  # 冷启动
        lines.append(
            f"{icon} 并发: 在途 <b>{inf}</b> · 排队 <b>{wait}</b>"
            f" · 追踪 {track} 个渠道"
        )
    else:
        lines.append("⚡ 并发: <code>关闭</code>")

    # 配额预警提示
    if quota_hot > 0:
        lines.append("")
        lines.append(f"⚠ <b>{quota_hot} 个 OAuth 账号用量 ≥80%</b>，请在「🔐 管理 OAuth」查看详情。")

    # ─── 底部固定信息块（每次进入主菜单都重新生成） ───
    lines.append("")
    lines.append("─" * 18)
    lines.extend(_address_block(port))
    lines.append("")
    lines.extend(_lifetime_stats_block())

    return "\n".join(lines)


def _address_block(port: int) -> list[str]:
    """服务地址 + 完整接口地址。<code> 包裹便于点击复制。"""
    pub = public_ip.get()
    out = [
        "🌐 <b>服务地址</b> (BaseURL)",
        f"  本地 <code>http://127.0.0.1:{port}</code>",
    ]
    if pub:
        out.append(f"  公网 <code>http://{pub}:{port}</code>")
    out += [
        "",
        "📍 <b>接口地址</b> (POST)",
        "  <b>Anthropic</b>",
        f"    本地 <code>http://127.0.0.1:{port}/v1/messages</code>",
    ]
    if pub:
        out.append(f"    公网 <code>http://{pub}:{port}/v1/messages</code>")
    out += [
        "  <b>OpenAI Chat</b>",
        f"    本地 <code>http://127.0.0.1:{port}/v1/chat/completions</code>",
    ]
    if pub:
        out.append(f"    公网 <code>http://{pub}:{port}/v1/chat/completions</code>")
    out += [
        "  <b>OpenAI Responses</b>",
        f"    本地 <code>http://127.0.0.1:{port}/v1/responses</code>",
    ]
    if pub:
        out.append(f"    公网 <code>http://{pub}:{port}/v1/responses</code>")
    out.append("<i>单击地址即可复制（不会跳转）。</i>")
    return out


def _lifetime_stats_block() -> list[str]:
    """累计统计：每次显示主菜单都现查（跨所有月份的 logs/*.db）。"""
    try:
        s = log_db.stats_lifetime()
    except Exception:
        s = {"total": 0, "input_tokens": 0, "output_tokens": 0,
             "cache_creation": 0, "cache_read": 0}
    total_in = ui.prompt_total(s.get("input_tokens"), s.get("cache_creation"), s.get("cache_read"))
    out_tok = s.get("output_tokens") or 0
    lines = [
        "📊 <b>累计统计</b>",
        f"  总调用 <code>{s.get('total', 0):,}</code> 次",
        f"  总 Tokens <code>{ui.fmt_tokens(total_in + out_tok)}</code> "
        f"(↑ {ui.fmt_tokens(total_in)} ↓ {ui.fmt_tokens(out_tok)})",
    ]
    if (s.get("cache_read") or 0) > 0:
        lines.append(f"  {ui.fmt_cache_phrase(s.get('cache_read'), total_in)}")
    return lines


def _compose_text() -> str:
    cfg = config.get()
    empty = (
        not (cfg.get("oauthAccounts") or [])
        and not (cfg.get("channels") or [])
        and not (cfg.get("apiKeys") or {})
    )
    if empty:
        return _first_run_banner()
    return _overview()


def show(chat_id: int) -> None:
    """命令入口：send 一条新消息。"""
    ui.send(chat_id, _compose_text(), reply_markup=_kb())


def show_edit(chat_id: int, message_id: int) -> None:
    """回调入口：edit 同一条消息。"""
    ui.edit(chat_id, message_id, _compose_text(), reply_markup=_kb())


def welcome(chat_id: int) -> None:
    """/start 时的简短欢迎页（带菜单按钮）。

    不嵌入 _compose_text 的 overview——后者由 /menu 或菜单返回时显示，
    避免出现欢迎语 + 主菜单标题双重出现，以及"服务地址"重复。
    """
    text = (
        "👋 <b>欢迎使用 Parrot · 多家族 AI 协议代理</b>\n\n"
        "<b>快速开始：</b>\n"
        "1️⃣ 「🔐 管理 OAuth」→「➕ 新增账户」添加 Claude OAuth\n"
        "2️⃣ 「🔀 渠道管理」→「➕ 添加渠道」接入第三方云平台\n"
        "3️⃣ 「🔑 管理 API Key」创建代理 Key 供下游使用\n\n"
        "👇 点击下方任意菜单进入管理面板。"
    )
    ui.send(chat_id, text, reply_markup=_kb())


# ─── /start / /menu 命令入口 ──────────────────────────────────────

def on_start_command(chat_id: int) -> None:
    welcome(chat_id)


def on_menu_command(chat_id: int) -> None:
    show(chat_id)


# ─── 回调：回到主菜单 ─────────────────────────────────────────────

def handle_back(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    show_edit(chat_id, message_id)
