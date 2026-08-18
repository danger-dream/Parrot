"""统计汇总（合并 cc-proxy 一屏全展 + openai-proxy 维度切片 + 多渠道增强）。

视图模式：
  - 汇总 (all)：cc-proxy 风格——一屏展示总览 + 三个维度 Top 3 + 未命中样本 + 最近调用
  - 渠道/模型/Key (channel/model/apikey)：该维度展开 Top 10，每条详细一些

callback_data：`stats:view:<period>:<dim>`
  period: 0 (今天) | 3 | 7 | month
  dim:    all | channel | model | apikey
"""

from __future__ import annotations

import time

from ... import concurrency, config, log_db
from .. import menu_cache, ui


_VALID_PERIODS = ("0", "3", "7", "month")
_VALID_DIMS = ("all", "channel", "model", "apikey")

_PERIOD_LABELS = {"0": "今天", "3": "最近 3 天", "7": "最近 7 天", "month": "本月"}
_DIM_LABELS = {"all": "汇总", "channel": "按渠道", "model": "按模型", "apikey": "按 Key"}


def _since_ts(period: str) -> float:
    if period == "0":
        return menu_cache.today_start_ts()
    if period == "month":
        return menu_cache.month_start_ts()
    try:
        days = int(period)
    except Exception:
        days = 3
    return time.time() - days * 86400


# ─── 共用渲染片段 ─────────────────────────────────────────────────

def _channel_icon(key: str) -> str:
    if key.startswith("oauth:"):
        return "🔐"
    if key.startswith("api:"):
        return "📡"
    return "•"


def _ch_short_name(key: str) -> str:
    """Human-facing channel name; never expose OpenAI workspace ids."""
    return ui.channel_display_name(key, with_family=False)


def _channel_identity_icon(key: str) -> str:
    provider_icon = ui.channel_provider_custom_emoji_html(key)
    if provider_icon:
        return f"{provider_icon} 🔐"
    return _channel_icon(key)


def _fmt_cost(metrics: dict, *, decimal_places: int = 2) -> str:
    return ui.fmt_cost(metrics, decimal_places=decimal_places)


def _section_overall(overall: dict) -> str:
    """cc-proxy 同款 6 段总览：Tokens / 请求 / 缓存 / 耗时 / 重试 / 亲和。"""
    total = int(overall.get("total") or 0)
    succ = int(overall.get("success_count") or 0)
    err = int(overall.get("error_count") or 0)
    pend = int(overall.get("pending_count") or 0)
    raw_inp = int(overall.get("total_input_tokens") or 0)
    raw_out = int(overall.get("total_output_tokens") or 0)
    raw_cc = int(overall.get("total_cache_creation") or 0)
    raw_cr = int(overall.get("total_cache_read") or 0)
    total_inp = raw_inp + raw_cc + raw_cr

    total_retries = int(overall.get("total_retries") or 0)
    retried = int(overall.get("retried_requests") or 0)
    affinity_hits = int(overall.get("affinity_hits") or 0)
    succ_hit = int(overall.get("success_with_cache_hit") or 0)
    succ_write = int(overall.get("success_with_cache_write") or 0)

    avg_conn = overall.get("avg_connect_ms")
    avg_first = overall.get("avg_first_token_ms")
    avg_total = overall.get("avg_total_ms")
    avg_tps = overall.get("avg_tps")
    max_tps = overall.get("max_tps")
    min_tps = overall.get("min_tps")

    token_line = f"↑ {ui.fmt_tokens(total_inp)} | ↓ {ui.fmt_tokens(raw_out)}"
    if raw_cr > 0:
        token_line += f" | {ui.fmt_cache_phrase(raw_cr, total_inp)}"

    lines = [
        "<b>Tokens:</b>",
        token_line,
        "",
        "<b>请求:</b>",
        f"共 {total} 次 | ✅ {succ} | ❌ {err} | ⏳ {pend}",
        f"成功率 {ui.fmt_rate(succ, total)}",
        "",
        "<b>缓存:</b>",
        f"命中请求 {succ_hit}/{succ} ({ui.fmt_rate(succ_hit, succ)})"
        + (f" · {ui.fmt_cache_phrase(raw_cr, total_inp)}" if raw_cr > 0 else ""),
        "",
        "<b>耗时（平均）:</b>",
        f"连接 {ui.fmt_ms(avg_conn)} | 首字 {ui.fmt_ms(avg_first)} | 总 {ui.fmt_ms(avg_total)}",
        "",
        "<b>生成速度:</b>",
        f"平均 {ui.fmt_tps(avg_tps)} | 峰值 {ui.fmt_tps(max_tps)} | 最低 {ui.fmt_tps(min_tps)}",
    ]
    if total > 0:
        if total_retries > 0:
            lines += [
                "",
                "<b>重试:</b>",
                f"共 {total_retries} 次 · 涉及 {retried}/{total} 个请求 ({ui.fmt_rate(retried, total)})",
            ]
        lines += [
            "",
            "<b>亲和:</b>",
            f"命中率 {ui.fmt_rate(affinity_hits, total)} ({affinity_hits}/{total})",
        ]
    lines += [
        "",
        "<b>金额:</b>",
        f"累计金额 {_fmt_cost(overall)}",
    ]
    return "\n".join(lines)


def _strip_unknown(groups: list[dict]) -> list[dict]:
    """过滤掉 key='?' 的维度条目——这些是 final_channel_key/requested_model
    为 NULL 的请求（通常是调度失败 / error 中止），不应占据 Top 位置。"""
    return [g for g in groups if (g.get("key") or "?") != "?"]


def _channel_type_label(ch_type: str) -> str:
    """渠道类型短标签：api / oauth / ?。"""
    if ch_type in ("api", "oauth"):
        return ch_type
    return "?"


def _render_model_channels(items: list[dict], limit: int = 3) -> str:
    """把 channels_by_requested_model 返回的单个模型 items 渲染成一行"所属"。"""
    if not items:
        return ""
    parts: list[str] = []
    for it in items[:limit]:
        short = _ch_short_name(it.get("key") or "?")
        icon = _channel_icon(it.get("key") or "?")
        typ = _channel_type_label(it.get("type") or "?")
        parts.append(
            f"{icon} <code>{ui.escape_html(short)}</code>({typ})·{it.get('count', 0)}"
        )
    line = " · ".join(parts)
    if len(items) > limit:
        line += f" · 等 {len(items)} 个"
    return line


def _summary_dim_block(title: str, groups: list[dict], render_key,
                       extra_line=None) -> str:
    """汇总视图里某个维度的 Top 块（紧凑两行/条）。

    extra_line: 可选 callable(key) -> str；非空时追加为第三行（带 2 空格缩进）。
    """
    if not groups:
        return ""
    out = [f"<b>{title}:</b>"]
    for g in groups:
        m = g["metrics"]
        key = render_key(g["key"])
        total = int(m.get("total") or 0)
        succ = int(m.get("success_count") or 0)
        hit = int(m.get("hit_requests") or 0)
        prompt = int(m.get("total_prompt_tokens") or 0)
        cr = int(m.get("total_cache_read") or 0)
        out.append(f"\n{key}")
        line = (
            f"  {total} 次 ({ui.fmt_rate(succ, total)}) · "
            f"命中请求 {hit}/{succ} ({ui.fmt_rate(hit, succ)}) · "
            f"↑ {ui.fmt_tokens(prompt)}"
        )
        if cr > 0:
            line += f" · {ui.fmt_cache_phrase(cr, prompt)}"
        out.append(line)
        if extra_line is not None:
            extra = extra_line(g["key"])
            if extra:
                out.append(f"  {extra}")
        out.append(f"  累计金额 {_fmt_cost(m)}")
    return "\n".join(out)


def _expanded_dim_block(title: str, groups: list[dict], render_key,
                        extra_line=None) -> str:
    """专题视图（按某个维度展开）：每条 4 行详细信息。

    extra_line: 可选 callable(key) -> str；非空时紧跟在 key 行之后。
    """
    if not groups:
        return f"<b>{title}</b>\n\n暂无数据"
    out = [f"<b>{title}</b>"]
    for g in groups:
        m = g["metrics"]
        key = render_key(g["key"])
        total = int(m.get("total") or 0)
        succ = int(m.get("success_count") or 0)
        err = int(m.get("error_count") or 0)
        hit = int(m.get("hit_requests") or 0)
        write = int(m.get("write_requests") or 0)
        prompt = int(m.get("total_prompt_tokens") or 0)
        output = int(m.get("total_output_tokens") or 0)
        cr = int(m.get("total_cache_read") or 0)
        cc = int(m.get("total_cache_creation") or 0)
        avg_conn = m.get("avg_connect_ms")
        avg_first = m.get("avg_first_token_ms")
        avg_tps = m.get("avg_tps")
        max_tps = m.get("max_tps")
        min_tps = m.get("min_tps")

        out.append(f"\n{key}")
        if extra_line is not None:
            extra = extra_line(g["key"])
            if extra:
                out.append(f"  {extra}")
        out.append(f"  请求 {total} | ✅ {succ} ({ui.fmt_rate(succ, total)}) | ❌ {err}")
        token_line = f"  ↑ {ui.fmt_tokens(prompt)} · ↓ {ui.fmt_tokens(output)}"
        if cr > 0:
            token_line += f" · {ui.fmt_cache_phrase(cr, prompt)}"
        out.append(token_line)
        out.append(f"  命中请求 {hit}/{succ} ({ui.fmt_rate(hit, succ)})")
        if avg_conn is not None or avg_first is not None:
            out.append(f"  连接 {ui.fmt_ms(avg_conn)} | 首字 {ui.fmt_ms(avg_first)}")
        if avg_tps is not None or max_tps is not None:
            out.append(
                f"  ⚡ TPS: 平均 {ui.fmt_tps(avg_tps)} · "
                f"峰值 {ui.fmt_tps(max_tps)} · 最低 {ui.fmt_tps(min_tps)}"
            )
        out.append(f"  累计金额 {_fmt_cost(m)}")
    return "\n".join(out)


def _section_cache_misses(misses: list[dict]) -> str:
    if not misses:
        return ""
    out = ["<b>最近未命中样本:</b>"]
    for r in misses:
        ts = ui.fmt_bjt_ts(r.get("created_at"), "%m-%d %H:%M:%S")
        model = ui.escape_html((r.get("requested_model") or "?")[:36])
        key = ui.escape_html((r.get("api_key_name") or "?")[:18])
        ch = r.get("final_channel_key") or "?"
        ch_disp = ui.escape_html(_ch_short_name(ch))
        inp = (r.get("input_tokens") or 0) + (r.get("cache_creation_tokens") or 0) + (r.get("cache_read_tokens") or 0)
        write = r.get("cache_creation_tokens") or 0
        msgs = r.get("msg_count") or 0
        tools = r.get("tool_count") or 0
        out.append(f"\n<code>[{ts}]</code> {model} / {key}")
        out.append(f"  渠道: <code>{ch_disp}</code>")
        out.append(
            f"  ↑{ui.fmt_tokens(inp)} · 写 {ui.fmt_tokens(write)}"
            f" · msgs {msgs} · tools {tools}"
        )
        out.append(f"  💵 {ui.fmt_cost_from_row(r)}")
    return "\n".join(out)


def _section_recent_calls(calls: list[dict]) -> str:
    if not calls:
        return ""
    out = ["<b>最近调用:</b>"]
    for r in calls:
        headline = ui.fmt_log_entry_headline(r, prefix="\n")
        body = ui.fmt_log_entry_body(r)
        out.append(headline)
        if body:
            out.append(body)
    return "\n".join(out)


# ─── 家族分段（新）────────────────────────────────────────────────

def _protocol_icon(proto: str | None) -> str:
    """根据 upstream_protocol 返回消息正文 rich 家族标签。"""
    fam = ui.family_of(proto)
    return ui.family_tag(fam) if fam else ""


def _section_overall_compact(overall: dict) -> str:
    """家族段内的精简 overall：单家族数据精炼到 5 行以内。

    与 _section_overall 区别：
      - 省略单独的 Tokens 段（合并到请求行）
      - 省略缓存详细段（仅保留命中率）
      - 保留重试 / 亲和（两家族对称）
    """
    total = int(overall.get("total") or 0)
    succ = int(overall.get("success_count") or 0)
    err = int(overall.get("error_count") or 0)
    pend = int(overall.get("pending_count") or 0)
    raw_inp = int(overall.get("total_input_tokens") or 0)
    raw_out = int(overall.get("total_output_tokens") or 0)
    raw_cc = int(overall.get("total_cache_creation") or 0)
    raw_cr = int(overall.get("total_cache_read") or 0)
    total_inp = raw_inp + raw_cc + raw_cr

    total_retries = int(overall.get("total_retries") or 0)
    retried = int(overall.get("retried_requests") or 0)
    affinity_hits = int(overall.get("affinity_hits") or 0)
    succ_hit = int(overall.get("success_with_cache_hit") or 0)

    avg_first = overall.get("avg_first_token_ms")
    avg_total = overall.get("avg_total_ms")
    avg_tps = overall.get("avg_tps")

    # 请求行 + 成功率
    lines = [
        f"请求 {total} · ✅ {succ} · ❌ {err}"
        + (f" · ⏳ {pend}" if pend else "")
        + f" · 成功率 {ui.fmt_rate(succ, total)}",
    ]
    # Tokens 行；金额由家族区块在运行指标之后单独展示。
    lines.append(
        f"↑ {ui.fmt_tokens(total_inp)} · ↓ {ui.fmt_tokens(raw_out)}"
        + (f" · {ui.fmt_cache_phrase(raw_cr, total_inp)}" if raw_cr else "")
    )
    # 耗时 / 速度
    timing_bits = []
    if avg_first is not None:
        timing_bits.append(f"首字 {ui.fmt_ms(avg_first)}")
    if avg_total is not None:
        timing_bits.append(f"总 {ui.fmt_ms(avg_total)}")
    if avg_tps is not None:
        timing_bits.append(f"⚡ {ui.fmt_tps(avg_tps)}")
    if timing_bits:
        lines.append(" · ".join(timing_bits))
    # 缓存命中 + 重试 + 亲和（两家族对称；值为 0 也展示，保持对照）
    if total > 0:
        extras = [f"命中请求 {succ_hit}/{succ} ({ui.fmt_rate(succ_hit, succ)})"]
        if total_retries > 0:
            extras.append(f"重试 {total_retries} 次 ({retried}/{total})")
        else:
            extras.append("重试 0")
        extras.append(f"亲和 {ui.fmt_rate(affinity_hits, total)}")
        lines.append(" · ".join(extras))
    return "\n".join(lines)


def _section_family(family: str, result: dict,
                    model_channels: dict[str, list[dict]] | None = None,
                    *,
                    show_by_channel: bool = True,
                    show_by_model: bool = True) -> str:
    """单个家族完整段：overall + (可选) by_channel Top3 + (可选) by_model Top3。"""
    overall = result.get("overall") or {}
    if int(overall.get("total") or 0) == 0:
        return ""  # 没流量不展示

    tag = ui.family_tag(family)
    parts = [f"<b>{tag}</b>", _section_overall_compact(overall)]
    parts.append(f"累计金额 {_fmt_cost(overall, decimal_places=3)}")

    if show_by_channel:
        by_channel = _strip_unknown(result.get("by_channel") or [])
        if by_channel:
            block = _summary_dim_block(
                "按渠道 Top",
                by_channel[:3],
                lambda k: f"{_channel_identity_icon(k)} <code>{ui.escape_html(_ch_short_name(k))}</code>",
            )
            parts.append("")
            parts.append(block)

    if show_by_model:
        by_model = _strip_unknown(result.get("by_model") or [])
        if by_model:
            mc = model_channels or {}
            block = _summary_dim_block(
                "按模型 Top",
                by_model[:3],
                lambda k: f"<code>{ui.escape_html(k)}</code>",
                extra_line=lambda k: (
                    "所属: " + _render_model_channels(mc.get(k) or [])
                    if mc.get(k) else ""
                ),
            )
            parts.append("")
            parts.append(block)

    return "\n".join(parts)


def _render_key_family_split(apikey: str, anth_total: int, oai_total: int) -> str:
    """按 Key Top 每条的 rich 家族细分。"""
    bits = []
    if anth_total > 0:
        bits.append(f"{ui.family_tag('anthropic')} {anth_total} 次")
    if oai_total > 0:
        bits.append(f"{ui.family_tag('openai')} {oai_total} 次")
    return " · ".join(bits)


# ─── 组装：汇总 / 专题 ───────────────────────────────────────────

def _render_overall(result: dict, period: str,
                    model_channels: dict[str, list[dict]] | None = None,
                    family_results: dict | None = None,
                    key_family_split: dict | None = None) -> str:
    """汇总视图：两家族分段 + 跨家族 Key Top + 最近调用 / 未命中样本。

    布局：
      📊 统计 — 今天
      ──────────────
      [Anthropic rich tag]    ← 家族段（overall 精简 + by_channel/by_model Top3）
      ...
      ──────────────
      [OpenAI rich tag]       ← 家族段（同上，含 Grok/Cursor）
      ...
      ──────────────
      按 Key Top              ← 跨家族，每条带 rich 家族拆分
      最近未命中样本 / 最近调用（跨家族，每条带家族图标）
    """
    sep = "─" * 18
    header = f"📊 <b>统计 — {_PERIOD_LABELS.get(period, period)}</b>"

    # 读可见性配置
    vis = (config.get().get("telegram") or {}).get("statsVisibility") or {}
    show_by_channel = bool(vis.get("byChannel", True))
    show_by_model = bool(vis.get("byModel", True))
    show_by_apikey = bool(vis.get("byApiKey", True))
    show_misses = bool(vis.get("cacheMisses", True))
    show_recent = bool(vis.get("recentCalls", True))

    sections: list[str] = [header]

    # 并发概要（启用时）
    cc_cfg = config.get().get("concurrency") or {}
    if bool(cc_cfg.get("enabled", True)):
        totals = concurrency.totals()
        if totals["in_flight"] > 0 or totals["waiting"] > 0:
            sections.append(
                f"⚡ 并发: 在途 <b>{totals['in_flight']}</b>"
                f" · 排队 <b>{totals['waiting']}</b>"
                f" · 追踪 {totals['tracked_channels']} 个"
            )

    # 家族段：按固定顺序 anthropic → openai
    family_results = family_results or {}
    rendered_any_family = False
    for fam in ("anthropic", "openai"):
        fr = family_results.get(fam)
        if not fr:
            continue
        block = _section_family(
            fam, fr, model_channels,
            show_by_channel=show_by_channel,
            show_by_model=show_by_model,
        )
        if block:
            sections.append(sep)
            sections.append(block)
            rendered_any_family = True

    # 如果没流量（或无家族数据），fallback 到旧的全家族 overall
    if not rendered_any_family:
        sections.append(sep)
        sections.append(_section_overall(result.get("overall") or {}))

    # 跨家族 Key Top
    by_apikey = _strip_unknown(result.get("by_apikey") or []) if show_by_apikey else []
    if by_apikey:
        sections.append(sep)
        kfs = key_family_split or {}

        def _render_key(k: str) -> str:
            return f"🔑 <code>{ui.escape_html(k)}</code>"

        def _extra_key(k: str) -> str:
            split = kfs.get(k) or (0, 0)
            s = _render_key_family_split(k, split[0], split[1])
            return s

        block = _summary_dim_block(
            "按 Key Top", by_apikey,
            _render_key,
            extra_line=_extra_key,
        )
        sections.append(block)

    # 未命中样本 / 最近调用（跨家族，带家族标签）
    misses = _section_cache_misses(result.get("recent_cache_misses") or []) if show_misses else ""
    if misses:
        sections.append("")
        sections.append(misses)

    calls = _section_recent_calls(result.get("recent_calls") or []) if show_recent else ""
    if calls:
        sections.append("")
        sections.append(calls)

    return "\n".join(sections)


def _channel_family_icon(channel_key: str) -> str:
    """Return a rich provider/family tag for statistics message bodies."""
    provider_icon = ui.channel_provider_custom_emoji_html(channel_key)
    if provider_icon:
        provider = ui.channel_provider(channel_key)
        return f"{provider_icon} {ui.escape_html(ui.provider_label(provider))}"
    try:
        from ...channel import registry
        ch = registry.get_channel(channel_key)
        if ch is not None:
            fam = ui.family_of(getattr(ch, "protocol", None))
            if fam:
                return ui.family_tag(fam)
    except Exception:
        pass
    return ""


def _model_family_icon(model: str,
                       model_channels: dict[str, list[dict]] | None) -> str:
    """由 model 反查到它主要打的渠道，再由渠道判断家族。"""
    if not model_channels:
        return ""
    chans = model_channels.get(model) or []
    for it in chans:
        icon = _channel_family_icon(it.get("key") or "")
        if icon:
            return icon
    return ""


def _render_expanded(result: dict, period: str, dim: str,
                     model_channels: dict[str, list[dict]] | None = None) -> str:
    """专题视图：把指定维度展开到 Top 10，每条带家族图标。"""
    label = _DIM_LABELS.get(dim, dim)
    sep = "─" * 18
    sections = [
        f"📊 <b>{label} — {_PERIOD_LABELS.get(period, period)}</b>",
        sep,
        _section_overall(result.get("overall") or {}),
        "",
    ]
    if dim == "channel":
        groups = _strip_unknown(result.get("by_channel") or [])

        def _rk_ch(k: str) -> str:
            # OAuth rows already have an exact provider icon; API rows only know
            # their protocol family and therefore use the family tag.
            fam_i = "" if ui.channel_provider(k) else _channel_family_icon(k)
            fam_prefix = f"{fam_i} " if fam_i else ""
            return (
                f"{fam_prefix}{_channel_identity_icon(k)} "
                f"<code>{ui.escape_html(_ch_short_name(k))}</code>"
            )

        block = _expanded_dim_block(
            f"按渠道（Top {len(groups)}）", groups, _rk_ch,
        )
    elif dim == "model":
        groups = _strip_unknown(result.get("by_model") or [])
        mc = model_channels or {}

        def _rk_m(k: str) -> str:
            fam_i = _model_family_icon(k, mc)
            fam_prefix = f"{fam_i} " if fam_i else ""
            return f"{fam_prefix}<code>{ui.escape_html(k)}</code>"

        block = _expanded_dim_block(
            f"按模型（Top {len(groups)}）", groups, _rk_m,
            extra_line=lambda k: (
                "所属: " + _render_model_channels(mc.get(k) or [], limit=5)
                if mc.get(k) else ""
            ),
        )
    elif dim == "apikey":
        groups = _strip_unknown(result.get("by_apikey") or [])
        block = _expanded_dim_block(
            f"按 Key（Top {len(groups)}）",
            groups,
            lambda k: f"🔑 <code>{ui.escape_html(k)}</code>",
        )
    else:
        block = ""
    sections.append(block)
    return "\n".join(sections)


# ─── 按钮 ─────────────────────────────────────────────────────────

def _kb(period: str, dim: str) -> dict:
    def _cell(p, d, label):
        mark = " ✓" if (period == p and dim == d) else ""
        return ui.btn(label + mark, f"stats:view:{p}:{d}")

    row_period = [
        _cell("0", dim, "今天"),
        _cell("3", dim, "3天"),
        _cell("7", dim, "7天"),
        _cell("month", dim, "本月"),
    ]
    row_dim = [
        _cell(period, "all", "汇总"),
        _cell(period, "channel", "渠道"),
        _cell(period, "model", "模型"),
        _cell(period, "apikey", "Key"),
    ]
    rows = [row_period, row_dim]
    if dim == "all":
        rows.append([
            ui.btn("🔄 刷新", f"stats:view:{period}:{dim}"),
            ui.btn("⚙ 设置", f"stats:vis:{period}"),
            ui.btn("◀ 返回主菜单", "menu:main"),
        ])
    else:
        rows.append([
            ui.btn("🔄 刷新", f"stats:view:{period}:{dim}"),
            ui.btn("◀ 返回主菜单", "menu:main"),
        ])
    return ui.inline_kb(rows)


# ─── 编排 + 入口 ─────────────────────────────────────────────────

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
    if not extras:
        return text
    return text + "\n\n" + "\n".join(extras)


def _slice_result(result: dict, dim: str, *, family: bool = False) -> dict:
    """从完整 period 快照切出旧 UI 所需的 Top 3 / Top 10。"""
    out = dict(result or {})
    for name, dim_name in (
        ("by_channel", "channel"), ("by_model", "model"), ("by_apikey", "apikey"),
    ):
        limit = 3 if family or dim == "all" or dim != dim_name else 10
        out[name] = list((result or {}).get(name) or [])[:limit]
    return out


def _compose_snapshot(snapshot: dict, period: str, dim: str) -> tuple[str, dict]:
    if period not in _VALID_PERIODS:
        period = "0"
    if dim not in _VALID_DIMS:
        dim = "all"
    result = _slice_result(snapshot.get("summary") or {}, dim)
    model_channels = snapshot.get("model_channels") or {}
    family_results = {
        fam: _slice_result(value or {}, dim, family=True)
        for fam, value in (snapshot.get("families") or {}).items()
    }
    key_family_split: dict[str, list[int]] = {}
    if dim == "all":
        for fam in ("anthropic", "openai"):
            for group in (family_results.get(fam) or {}).get("by_apikey") or []:
                key = group.get("key") or "?"
                if key == "?":
                    continue
                current = key_family_split.setdefault(key, [0, 0])
                current[0 if fam == "anthropic" else 1] = int(
                    (group.get("metrics") or {}).get("total") or 0
                )
    text = (
        _render_overall(
            result, period, model_channels,
            family_results=family_results,
            key_family_split=key_family_split,
        )
        if dim == "all"
        else _render_expanded(result, period, dim, model_channels)
    )
    return ui.truncate(text), _kb(period, dim)


def _period_cache_key(period: str, since: float) -> tuple:
    if period in ("0", "month"):
        return "period", int(since)
    return "rolling-period", period


def _error_page(exc: Exception) -> tuple[str, dict]:
    return (
        f"❌ 统计查询失败: <code>{ui.escape_html(str(exc))}</code>",
        ui.inline_kb([ui.back_to_main_row()]),
    )


def view(chat_id: int, message_id: int, cb_id: str,
         period: str = "0", dim: str = "all") -> None:
    period = period if period in _VALID_PERIODS else "0"
    dim = dim if dim in _VALID_DIMS else "all"
    since = _since_ts(period)
    key = _period_cache_key(period, since)
    cached = menu_cache.PERIOD_STATS.peek(key)
    if cached.value is None:
        # 今日/本月由主动预热负责；3/7 天只排入同一个串行队列。
        if period not in ("0", "month"):
            menu_cache.PERIOD_STATS.request(
                key, lambda: log_db.stats_period_snapshot(since),
            )
            ui.answer_cb(cb_id, "统计正在准备，请稍后再试")
        else:
            ui.answer_cb(cb_id, menu_cache.initialization_text())
        return

    ui.answer_cb(cb_id)
    menu_cache.begin_view(chat_id, message_id)
    text, kb = _compose_snapshot(cached.value, period, dim)
    ui.edit(chat_id, message_id, _maybe_suffix_status_banner(text), reply_markup=kb)
    if period not in ("0", "month") and not cached.fresh:
        menu_cache.PERIOD_STATS.request(
            key, lambda: log_db.stats_period_snapshot(since),
        )


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    view(chat_id, message_id, cb_id, "0", "all")


def send_new(chat_id: int) -> None:
    """命令入口：有今日快照就完整发送；冷启动不安排二次编辑。"""
    period, dim = "0", "all"
    since = _since_ts(period)
    cached = menu_cache.PERIOD_STATS.peek(_period_cache_key(period, since))
    if cached.value is None:
        ui.send(chat_id, menu_cache.initialization_text())
        return
    text, kb = _compose_snapshot(cached.value, period, dim)
    ui.send(chat_id, _maybe_suffix_status_banner(text), reply_markup=kb)


# ─── 路由 ─────────────────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "menu:stats":
        show(chat_id, message_id, cb_id)
        return True
    if data.startswith("stats:view:"):
        parts = data.split(":")
        if len(parts) >= 4:
            view(chat_id, message_id, cb_id, parts[2], parts[3])
            return True
    if data.startswith("stats:vis:"):
        parts = data.split(":")
        if len(parts) >= 3:
            view_visibility(chat_id, message_id, cb_id, parts[2])
            return True
    if data.startswith("stats:vistog:"):
        parts = data.split(":")
        if len(parts) >= 4:
            toggle_visibility(chat_id, message_id, cb_id, parts[2], parts[3])
            return True
    return False


# ─── 可见性设置 ───────────────────────────────────────────────────
# 控制「📈 统计汇总」汇总视图 (dim=all) 里各段的显示/隐藏。
# 配置写入 telegram.statsVisibility；默认全 True。

_VIS_ITEMS = [
    # (key, label, description)
    ("byChannel",   "按渠道 Top",   "每个家族内的渠道 Top3"),
    ("byModel",     "按模型 Top",   "每个家族内的模型 Top3"),
    ("byApiKey",    "按 Key Top",   "跨家族的 API Key Top"),
    ("cacheMisses", "未命中样本",   "最近缓存未命中请求"),
    ("recentCalls", "最近调用",     "最近调用记录流"),
]


def _vis_get() -> dict:
    """拿到当前可见性配置，缺失字段全填 True。"""
    from ... import config as _cfg
    tg = _cfg.get().get("telegram") or {}
    cur = dict(tg.get("statsVisibility") or {})
    for k, *_ in _VIS_ITEMS:
        cur.setdefault(k, True)
    return cur


def _vis_text_and_kb(period: str) -> tuple[str, dict]:
    vis = _vis_get()
    lines = [
        "⚙ <b>统计汇总 · 显示设置</b>",
        "",
        "下方每一项可切换显示状态。关闭后刷新 / 下次进入「统计汇总」将不再显示。",
        "",
        "<b>基本信息</b>（两家族概览）· <code>始终可见</code>",
        "",
        "<b>可切换段：</b>",
    ]
    rows = []
    for key, label, desc in _VIS_ITEMS:
        on = bool(vis.get(key, True))
        tag = "✅ 显示" if on else "⬛ 隐藏"
        lines.append(f"  {tag} · <b>{label}</b> — <i>{desc}</i>")
        rows.append([ui.btn(
            ("⬛ 隐藏 " if on else "✅ 显示 ") + label,
            f"stats:vistog:{period}:{key}",
        )])
    rows.append([
        ui.btn("◀ 返回统计汇总", f"stats:view:{period}:all"),
        ui.btn("🏠 主菜单", "menu:main"),
    ])
    return "\n".join(lines), ui.inline_kb(rows)


def view_visibility(chat_id: int, message_id: int, cb_id: str, period: str) -> None:
    ui.answer_cb(cb_id)
    if period not in _VALID_PERIODS:
        period = "0"
    text, kb = _vis_text_and_kb(period)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def toggle_visibility(chat_id: int, message_id: int, cb_id: str,
                      period: str, key: str) -> None:
    valid_keys = {k for k, *_ in _VIS_ITEMS}
    if key not in valid_keys:
        ui.answer_cb(cb_id, "无效项")
        return
    from ... import config as _cfg
    cur = _vis_get()
    new_val = not bool(cur.get(key, True))

    def _mut(c):
        tg = c.setdefault("telegram", {})
        sv = tg.setdefault("statsVisibility", {})
        sv[key] = new_val
    _cfg.update(_mut)
    ui.answer_cb(cb_id, "已显示" if new_val else "已隐藏")
    view_visibility(chat_id, message_id, "", period)
