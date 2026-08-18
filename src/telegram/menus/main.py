"""主菜单与 /start 欢迎页。"""

from __future__ import annotations

from ... import __version__, affinity, concurrency, config, load_balancing, oauth_manager, public_ip, state_db
from ...oauth_ids import account_key as _account_key
from ...channel import registry
from .. import menu_cache, ui


def _kb() -> dict:
    return ui.inline_kb([
        [ui.btn("📈 统计汇总", "menu:stats"),
         ui.btn("📋 最近日志", "menu:logs")],
        [ui.btn("🔐 管理 OAuth", "menu:oauth"),
         ui.btn("🔀 管理渠道", "menu:channel")],
        [ui.btn("🤖 模型管理", "map:show"),
         ui.btn("⚖️ 负载均衡", "menu:loadbalancing")],
        [ui.btn("🔑 管理 APIKEY", "menu:apikey"),
         ui.btn("⚙ 系统设置", "menu:settings")],
    ])


def _quota_hot_count(threshold_pct: float = 80.0) -> int:
    """返回当前用量 >= threshold 的 OAuth 账户数量（不含已禁用）。"""
    # 使用 oauth_manager.list_accounts() 作为唯一数据源。
    # Claude/OpenAI 走窗口配额；Grok/xAI 走官方月度 billing percent。
    accounts = oauth_manager.list_accounts()
    n = 0
    for acc in accounts:
        email = acc.get("email")
        if not email:
            continue
        provider = oauth_manager.provider_of(acc)
        if provider not in ("claude", "openai", "xai", "cursor"):
            continue
        ak = _account_key(acc)
        row = state_db.quota_load(ak)
        if not row:
            continue
        if provider in {"xai", "cursor"}:
            utils = [row.get("thirty_day_util")]
        else:
            utils = [row.get(k) for k in ("five_hour_util", "seven_day_util",
                                           "thirty_day_util", "sonnet_util", "opus_util")]
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
        "2️⃣ 发送 /keys → 创建下游调用用的 API Key\n"
        "3️⃣ 下游客户端配置代理 URL 即可使用\n"
    )


def _overview(lifetime_stats: dict | None = None, *, lifetime_loading: bool = False) -> str:
    """主菜单顶部的服务一览；慢统计只能来自进程内快照。"""
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
    mode = load_balancing.display_mode(cfg.get("channelSelection", "smart"))

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
    lines.extend(_lifetime_stats_block(lifetime_stats, loading=lifetime_loading))

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
        f"  {ui.family_tag('anthropic')}",
        f"    本地 <code>http://127.0.0.1:{port}/v1/messages</code>",
    ]
    if pub:
        out.append(f"    公网 <code>http://{pub}:{port}/v1/messages</code>")
    out += [
        f"  {ui.family_tag('openai')} Chat",
        f"    本地 <code>http://127.0.0.1:{port}/v1/chat/completions</code>",
    ]
    if pub:
        out.append(f"    公网 <code>http://{pub}:{port}/v1/chat/completions</code>")
    out += [
        f"  {ui.family_tag('openai')} Responses",
        f"    本地 <code>http://127.0.0.1:{port}/v1/responses</code>",
    ]
    if pub:
        out.append(f"    公网 <code>http://{pub}:{port}/v1/responses</code>")
    out.append("<i>单击地址即可复制（不会跳转）。</i>")
    return out


def _lifetime_stats_block(s: dict | None = None, *, loading: bool = False) -> list[str]:
    """累计统计：只渲染缓存快照，绝不在 polling 线程查库。"""
    if s is None:
        s = {"total": 0, "input_tokens": 0, "output_tokens": 0,
             "cache_creation": 0, "cache_read": 0}
    total_in = ui.prompt_total(s.get("input_tokens"), s.get("cache_creation"), s.get("cache_read"))
    out_tok = s.get("output_tokens") or 0
    token_line = (
        f"  总 Tokens <code>{ui.fmt_tokens(total_in + out_tok)}</code> "
        f"(↑ {ui.fmt_tokens(total_in)} ↓ {ui.fmt_tokens(out_tok)})"
    )
    if (s.get("cache_read") or 0) > 0:
        token_line += f" · {ui.fmt_cache_phrase(s.get('cache_read'), total_in)}"
    return [
        "📊 <b>累计统计</b>",
        f"  总调用 <code>{s.get('total', 0):,}</code> 次",
        token_line,
        f"  累计金额 {ui.fmt_cost(s)}",
    ]


def _maybe_suffix_status_banner(text: str) -> str:
    """在文本底部追加 banner：上游故障 + 新版本可用（任一存在即拼到末尾）。"""
    extras: list[str] = []
    try:
        from ... import status_monitor
        line = status_monitor.get_active_summary()
        if line:
            extras.append(line)
    except Exception:
        pass
    try:
        from ... import update_checker
        line = update_checker.get_update_banner()
        if line:
            extras.append(line)
    except Exception:
        pass
    try:
        from ... import network_monitor
        line = network_monitor.active_summary()
        if line:
            extras.append(line)
    except Exception:
        pass
    if not extras:
        return text
    return text + "\n\n" + "\n".join(extras)


def _compose_text(lifetime_stats: dict | None = None, *, lifetime_loading: bool = False) -> str:
    cfg = config.get()
    empty = (
        not (cfg.get("oauthAccounts") or [])
        and not (cfg.get("channels") or [])
        and not (cfg.get("apiKeys") or {})
    )
    if empty:
        return _maybe_suffix_status_banner(_first_run_banner())
    return _maybe_suffix_status_banner(
        _overview(lifetime_stats, lifetime_loading=lifetime_loading)
    )


def show(chat_id: int) -> None:
    """命令入口：有快照就完整发送；冷启动只发送简短提示。"""
    lifetime = menu_cache.LIFETIME_STATS.peek("lifetime")
    if lifetime.value is None:
        ui.send(chat_id, menu_cache.initialization_text())
        return
    ui.send(chat_id, _compose_text(lifetime.value), reply_markup=_kb())


def show_edit(chat_id: int, message_id: int) -> bool:
    """回调入口：只用最近成功快照一次性渲染，冷缓存保持原页。"""
    lifetime = menu_cache.LIFETIME_STATS.peek("lifetime")
    if lifetime.value is None:
        return False
    menu_cache.begin_view(chat_id, message_id)
    ui.edit(chat_id, message_id, _compose_text(lifetime.value), reply_markup=_kb())
    return True


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
        "3️⃣ 发送 /keys 创建代理 Key 供下游使用\n\n"
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
    if not show_edit(chat_id, message_id):
        ui.answer_cb(cb_id, menu_cache.initialization_text())
        return
    ui.answer_cb(cb_id)
