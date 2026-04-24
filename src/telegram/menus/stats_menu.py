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
from datetime import datetime, timedelta, timezone

from ... import concurrency, config, log_db
from .. import ui


_BJT = timezone(timedelta(hours=8))
_VALID_PERIODS = ("0", "3", "7", "month")
_VALID_DIMS = ("all", "channel", "model", "apikey")

_PERIOD_LABELS = {"0": "今天", "3": "最近 3 天", "7": "最近 7 天", "month": "本月"}
_DIM_LABELS = {"all": "汇总", "channel": "按渠道", "model": "按模型", "apikey": "按 Key"}


def _since_ts(period: str) -> float:
    now = time.time()
    if period == "0":
        today = datetime.now(_BJT).replace(hour=0, minute=0, second=0, microsecond=0)
        return today.timestamp()
    if period == "month":
        month_start = datetime.now(_BJT).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return month_start.timestamp()
    try:
        days = int(period)
    except Exception:
        days = 3
    return now - days * 86400


# ─── 共用渲染片段 ─────────────────────────────────────────────────

def _channel_icon(key: str) -> str:
    if key.startswith("oauth:"):
        return "🔐"
    if key.startswith("api:"):
        return "🔀"
    return "•"


def _ch_short_name(key: str) -> str:
    """oauth:claude:foo@bar → foo@bar 🅰；oauth:openai:foo@bar → foo@bar 🅾；api:智谱 → 智谱。"""
    if key.startswith("oauth:"):
        body = key[len("oauth:"):]
        if ":" in body:
            prov, email = body.split(":", 1)
            tag = " 🅾" if prov == "openai" else (" 🅰" if prov == "claude" else "")
            return f"{email}{tag}"
        return body  # 兜底：老格式 oauth:<email>
    if ":" in key:
        return key.split(":", 1)[1]
    return key


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
            f"  ↑{ui.fmt_tokens(inp)} · 写 {ui.fmt_tokens(write)} · "
            f"msgs {msgs} · tools {tools}"
        )
    return "\n".join(out)


def _section_recent_calls(calls: list[dict]) -> str:
    if not calls:
        return ""
    out = ["<b>最近调用:</b>"]
    for r in calls:
        ts = ui.fmt_bjt_ts(r.get("created_at"), "%m-%d %H:%M:%S")
        icon = {"success": "✅", "error": "❌", "pending": "⏳"}.get(r.get("status"), "?")
        model = ui.escape_html(r.get("requested_model") or "?")
        fam_icon = _protocol_icon(r.get("upstream_protocol"))
        fam_suffix = f" {fam_icon}" if fam_icon else ""
        out.append(f"\n<code>[{ts}]</code> {icon} {model}{fam_suffix}")
        if r.get("final_channel_key"):
            out.append(f"  渠道: <code>{ui.escape_html(_ch_short_name(r['final_channel_key']))}</code>")
        if r.get("status") == "success":
            inp = (r.get("input_tokens") or 0) + (r.get("cache_creation_tokens") or 0) + (r.get("cache_read_tokens") or 0)
            cr = r.get("cache_read_tokens") or 0
            out.append(
                f"  ↑ {ui.fmt_tokens(inp)} · ↓ {ui.fmt_tokens(r.get('output_tokens'))}"
                + (f" · {ui.fmt_cache_phrase(cr, inp)}" if cr else "")
            )
        timing = []
        if r.get("connect_time_ms") is not None:
            timing.append(f"连接 {ui.fmt_ms(r['connect_time_ms'])}")
        if r.get("is_stream") and r.get("first_token_time_ms") is not None:
            timing.append(f"首字 {ui.fmt_ms(r['first_token_time_ms'])}")
        if r.get("total_time_ms") is not None:
            timing.append(f"总 {ui.fmt_ms(r['total_time_ms'])}")
        tps_v = ui.calc_row_tps(r)
        if tps_v is not None:
            timing.append(f"⚡ {ui.fmt_tps(tps_v)}")
        if (r.get("retry_count") or 0) > 0:
            timing.append(f"重试 {r['retry_count']} 次")
        if timing:
            out.append(f"  耗时: {' · '.join(timing)}")
        if r.get("status") == "error" and r.get("error_message"):
            err_short = ui.escape_html(r["error_message"][:120])
            out.append(f"  错误: <code>{err_short}</code>")
    return "\n".join(out)


# ─── 家族分段（新）────────────────────────────────────────────────

def _protocol_icon(proto: str | None) -> str:
    """根据 upstream_protocol 返回家族图标（recent_calls / recent_errors 末尾展示）。"""
    fam = ui.family_of(proto)
    if not fam:
        return ""
    return ui.FAMILY_ICON.get(fam, "")


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
    # Tokens 行
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

    if show_by_channel:
        by_channel = _strip_unknown(result.get("by_channel") or [])
        if by_channel:
            block = _summary_dim_block(
                "按渠道 Top",
                by_channel[:3],
                lambda k: f"{_channel_icon(k)} <code>{ui.escape_html(_ch_short_name(k))}</code>",
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
    """按 Key Top 每条的家族细分小字：🅰 X 次 · 🅾 Y 次"""
    bits = []
    if anth_total > 0:
        bits.append(f"🅰 {anth_total} 次")
    if oai_total > 0:
        bits.append(f"🅾 {oai_total} 次")
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
      🅰 Anthropic            ← 家族段（overall 精简 + by_channel/by_model Top3）
      ...
      ──────────────
      🅾 OpenAI               ← 家族段（同上）
      ...
      ──────────────
      按 Key Top              ← 跨家族，每条带 🅰/🅾 拆分小字
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
    """用 registry 查出渠道的上游协议，转成家族图标（🅰/🅾）。查不到返回空串。"""
    try:
        from ...channel import registry
        ch = registry.get_channel(channel_key)
        if ch is not None:
            fam = ui.family_of(getattr(ch, "protocol", None))
            if fam:
                return ui.FAMILY_ICON.get(fam, "")
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
            fam_i = _channel_family_icon(k)
            fam_prefix = f"{fam_i} " if fam_i else ""
            return f"{fam_prefix}{_channel_icon(k)} <code>{ui.escape_html(_ch_short_name(k))}</code>"

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

def _compose(period: str, dim: str) -> tuple[str, dict]:
    """统一渲染：返回 (text, kb)。失败时返回错误页 (text, kb)。"""
    if period not in _VALID_PERIODS:
        period = "0"
    if dim not in _VALID_DIMS:
        dim = "all"
    since = _since_ts(period)
    try:
        # 汇总视图：所有维度只取 Top 3；专题视图：对应维度展开 Top 10
        result = log_db.stats_summary(
            since_ts=since,
            group_by=(None if dim == "all" else dim),
            summary_top_limit=3,
            group_limit=10,
        )
    except Exception as exc:
        return (
            f"❌ 统计查询失败: <code>{ui.escape_html(str(exc))}</code>",
            ui.inline_kb([ui.back_to_main_row()]),
        )
    # "按模型 Top"/专题需要补 model → 渠道列表
    model_channels: dict[str, list[dict]] = {}
    if dim in ("all", "model"):
        try:
            model_channels = log_db.channels_by_requested_model(since)
        except Exception as exc:
            print(f"[stats] channels_by_requested_model failed: {exc}")
            model_channels = {}

    # 汇总视图：额外跑两次 family 聚合（含完整 overall / by_channel / by_model），
    # 同时算出每个 api_key 在两家族的次数拆分，供 Key Top 展示小字。
    family_results: dict = {}
    key_family_split: dict = {}
    if dim == "all":
        for fam in ("anthropic", "openai"):
            try:
                family_results[fam] = log_db.stats_summary(
                    since_ts=since, family=fam, summary_top_limit=3,
                )
            except Exception as exc:
                print(f"[stats] family={fam} failed: {exc}")
                family_results[fam] = None
        # 计算每个 Key 在两家族的请求数
        for fam in ("anthropic", "openai"):
            fr = family_results.get(fam) or {}
            for g in (fr.get("by_apikey") or []):
                k = g.get("key") or "?"
                if k == "?":
                    continue
                cur = key_family_split.setdefault(k, [0, 0])  # [anthropic, openai]
                cur[0 if fam == "anthropic" else 1] = int((g.get("metrics") or {}).get("total") or 0)

    text = (
        _render_overall(result, period, model_channels,
                        family_results=family_results,
                        key_family_split=key_family_split) if dim == "all"
        else _render_expanded(result, period, dim, model_channels)
    )
    return ui.truncate(text), _kb(period, dim)


def view(chat_id: int, message_id: int, cb_id: str,
         period: str = "0", dim: str = "all") -> None:
    ui.answer_cb(cb_id)
    text, kb = _compose(period, dim)
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    view(chat_id, message_id, cb_id, "0", "all")


def send_new(chat_id: int) -> None:
    """命令入口：直接 send 一条新消息。"""
    text, kb = _compose("0", "all")
    ui.send(chat_id, text, reply_markup=kb)


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
