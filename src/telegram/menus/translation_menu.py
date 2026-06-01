"""翻译层设置菜单。

callback_data 前缀：`tl:...`
状态机 action：`tl_*`
"""

from __future__ import annotations

import asyncio
import threading

from ... import config, translation
from ...channel import registry
from .. import states, ui


# ─── 常量 ─────────────────────────────────────────────────────────

_PAGE_SIZE = 10  # 模型选择每页条数

# 常用目标语言列表
_LANGUAGES = [
    ("English", "🇬🇧 English"),
    ("Japanese", "🇯🇵 日本語"),
    ("Korean", "🇰🇷 한국어"),
    ("French", "🇫🇷 Français"),
    ("German", "🇩🇪 Deutsch"),
    ("Italian", "🇮🇹 Italiano"),
    ("Spanish", "🇪🇸 Español"),
    ("Portuguese", "🇵🇹 Português"),
    ("Russian", "🇷🇺 Русский"),
    ("Malay", "🇲🇾 Bahasa Melayu"),
    ("Thai", "🇹🇭 ภาษาไทย"),
    ("Vietnamese", "🇻🇳 Tiếng Việt"),
    ("Arabic", "🇸🇦 العربية"),
    ("Chinese", "🇨🇳 中文"),
]


def _get_cfg() -> dict:
    return translation._get_cfg()


# ─── 主页面 ───────────────────────────────────────────────────────

def _main_text_and_kb() -> tuple[str, dict]:
    cfg = _get_cfg()
    enabled = bool(cfg.get("enabled"))
    model = cfg.get("model") or "(未设置)"
    fallback = cfg.get("fallbackModel") or "(未设置)"
    lang = cfg.get("targetLanguage") or "English"
    timeout = int(cfg.get("timeoutSeconds", 10))
    max_hist = int(cfg.get("maxHistoryMessages", 20))
    ttl_days = int(cfg.get("cacheTtlDays", 3))
    preload = int(cfg.get("cachePreloadCount", 100))
    alert_threshold = int(cfg.get("failureAlertThreshold", 10))
    mem_mb = int(cfg.get("memoryCacheMaxMb", 100))
    mem_ttl = int(cfg.get("memoryCacheTtlSeconds", 7200))
    translate_system = bool(cfg.get("translateSystemMessages", False))
    ready_ok, ready_reason = translation.validate_ready(cfg, require_enabled=False)

    cache_n = translation.cache_count()
    stats = translation.cache_hit_stats()
    mem_bytes = int(stats.get("memoryBytes", 0) or 0)
    mem_entries = int(stats.get("memoryEntries", 0) or 0)

    text = (
        "🗣 <b>翻译层</b>\n\n"
        "将用户输入翻译为指定语言后再发送给模型，\n"
        "模型回复不翻译（在提示词中指定回复语言即可）。\n\n"
        f"状态: <code>{'✅ 开' if enabled else '关'}</code>\n"
        f"翻译模型: <code>{ui.escape_html(model)}</code>\n"
        f"备用模型: <code>{ui.escape_html(fallback)}</code>\n"
        f"目标语言: <code>{ui.escape_html(lang)}</code>\n"
        f"超时: <code>{timeout}s</code>\n"
        f"最大历史翻译: <code>{max_hist}</code> 条\n"
        f"缓存 TTL: <code>{ttl_days}</code> 天\n"
        f"预加载: <code>{preload}</code> 条\n"
        f"连续失败告警: <code>{alert_threshold}</code> 次\n"
        f"内存缓存: <code>{mem_mb}MB / TTL {mem_ttl}s</code>\n"
        f"翻译系统消息: <code>{'开' if translate_system else '关'}</code>\n"
        f"可用性: <code>{'✅ 可用' if ready_ok else '⚠ ' + ready_reason}</code>\n\n"
        f"缓存: <code>{cache_n}</code> 条"
        f" · 内存 <code>{mem_entries}</code> 条 / <code>{mem_bytes / 1024 / 1024:.2f}MB</code>"
        f" · 命中 <code>{stats.get('hits', 0)}</code>"
        f" · 未命中 <code>{stats.get('misses', 0)}</code>"
    )

    toggle_label = "🔴 关闭翻译层" if enabled else "🟢 开启翻译层"
    kb = ui.inline_kb([
        [ui.btn(toggle_label, "tl:toggle")],
        [ui.btn("✏ 翻译模型", "tl:edit:model:0"),
         ui.btn("✏ 备用模型", "tl:edit:fallback:0")],
        [ui.btn("✏ 目标语言", "tl:edit:lang"),
         ui.btn("✏ 超时", "tl:edit:timeout")],
        [ui.btn("✏ 最大历史翻译", "tl:edit:max_hist"),
         ui.btn("✏ 缓存 TTL", "tl:edit:ttl")],
        [ui.btn("✏ 预加载数量", "tl:edit:preload"),
         ui.btn("✏ 告警阈值", "tl:edit:alert")],
        [ui.btn("✏ 内存上限", "tl:edit:mem_mb"),
         ui.btn("✏ 内存TTL", "tl:edit:mem_ttl")],
        [ui.btn("🧩 翻译系统消息", "tl:toggle:system"),
         ui.btn("🧪 测试翻译", "tl:test")],
        [ui.btn("📝 翻译提示词", "tl:show:prompt"),
         ui.btn("🗑 清空缓存", "tl:cache:clear")],
        [ui.btn("◀ 返回设置", "menu:settings"),
         ui.btn("🏠 返回主菜单", "menu:main")],
    ])
    return text, kb


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    text, kb = _main_text_and_kb()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _main_text_and_kb()
    ui.send(chat_id, text, reply_markup=kb)


# ─── 开关 ─────────────────────────────────────────────────────────

def _on_toggle(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool(_get_cfg().get("enabled"))
    new_val = not cur
    if new_val:
        cfg = _get_cfg()
        probe_cfg = dict(cfg)
        probe_cfg["enabled"] = True
        ok, reason = translation.validate_ready(probe_cfg, require_enabled=True)
        if not ok:
            ui.answer_cb(cb_id, f"不能开启: {reason}", show_alert=True)
            show(chat_id, message_id, "-")
            return

    def _m(c):
        c.setdefault("translation", {})["enabled"] = new_val
    config.update(_m)
    ui.answer_cb(cb_id, "已开启" if new_val else "已关闭")
    show(chat_id, message_id, "-")


def _on_toggle_system(chat_id: int, message_id: int, cb_id: str) -> None:
    cur = bool(_get_cfg().get("translateSystemMessages", False))

    def _m(c):
        c.setdefault("translation", {})["translateSystemMessages"] = not cur
    config.update(_m)
    ui.answer_cb(cb_id, "已开启系统消息翻译" if not cur else "已关闭系统消息翻译")
    show(chat_id, message_id, "-")


# ─── 模型选择（picker 模式） ─────────────────────────────────────

def _model_picker(
    chat_id: int, message_id: int, cb_id: str,
    *, field: str, page: int,
) -> None:
    """显示模型选择器。field = "model" 或 "fallback"。"""
    ui.answer_cb(cb_id)
    models = registry.available_models()
    if not models:
        ui.edit(
            chat_id, message_id,
            "⚠ 当前无可用渠道/模型，请先添加 OAuth 或渠道。",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回翻译层", "tl:show")]]),
        )
        return

    total = len(models)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)

    current = _get_cfg().get(field) or "(未设置)"
    label = "翻译模型" if field == "model" else "备用模型"

    text = (
        f"🗣 <b>选择{label}</b>\n\n"
        f"当前: <code>{ui.escape_html(current)}</code>\n\n"
        f"请选择模型（推荐轻量快速模型）:\n"
        f"<i>第 {page + 1}/{total_pages} 页, 共 {total} 个可选模型。</i>"
    )

    rows: list[list[dict]] = []
    for m in models[start:end]:
        mc = ui.register_code(f"tl:m:{m}")
        rows.append([ui.btn(m, f"tl:pick:{field}:{mc}")])

    nav: list[dict] = []
    if page > 0:
        nav.append(ui.btn("◀ 上一页", f"tl:edit:{field}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(ui.btn("下一页 ▶", f"tl:edit:{field}:{page + 1}"))
    if nav:
        rows.append(nav)

    # 备用模型可以清除
    if field == "fallback":
        rows.append([ui.btn("🗑 清除备用模型", "tl:clear:fallback")])
    rows.append([ui.btn("◀ 返回翻译层", "tl:show")])

    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


def _on_pick_model(
    chat_id: int, message_id: int, cb_id: str,
    field: str, model_code: str,
) -> None:
    model_tag = ui.resolve_code(model_code)
    if not model_tag or not model_tag.startswith("tl:m:"):
        ui.answer_cb(cb_id, "会话已过期")
        return
    model_name = model_tag[5:]  # strip "tl:m:"

    config_key = "model" if field == "model" else "fallbackModel"

    def _m(c):
        c.setdefault("translation", {})[config_key] = model_name
    config.update(_m)

    label = "翻译模型" if field == "model" else "备用模型"
    ui.answer_cb(cb_id, f"✅ {label}: {model_name}")
    show(chat_id, message_id, "-")


def _on_clear_fallback(chat_id: int, message_id: int, cb_id: str) -> None:
    def _m(c):
        c.setdefault("translation", {})["fallbackModel"] = ""
    config.update(_m)
    ui.answer_cb(cb_id, "已清除备用模型")
    show(chat_id, message_id, "-")


# ─── 目标语言选择 ─────────────────────────────────────────────────

def _show_lang_picker(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    current = _get_cfg().get("targetLanguage") or "English"
    text = (
        "🗣 <b>选择目标语言</b>\n\n"
        f"当前: <code>{ui.escape_html(current)}</code>"
    )
    rows: list[list[dict]] = []
    # 每行 2 个语言按钮
    for i in range(0, len(_LANGUAGES), 2):
        row: list[dict] = []
        for lang_val, lang_label in _LANGUAGES[i:i + 2]:
            mark = "✓ " if lang_val == current else ""
            row.append(ui.btn(f"{mark}{lang_label}", f"tl:pick:lang:{lang_val}"))
        rows.append(row)
    rows.append([ui.btn("◀ 返回翻译层", "tl:show")])
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


def _on_pick_lang(chat_id: int, message_id: int, cb_id: str, lang: str) -> None:
    def _m(c):
        c.setdefault("translation", {})["targetLanguage"] = lang
    config.update(_m)
    ui.answer_cb(cb_id, f"✅ 目标语言: {lang}")
    show(chat_id, message_id, "-")


# ─── 数值编辑（超时/最大历史/TTL/预加载/告警阈值） ────────────────

_NUMERIC_FIELDS = {
    "timeout":  ("超时（秒）",         "timeoutSeconds",          1,  60),
    "max_hist": ("最大历史翻译（条）", "maxHistoryMessages",      1, 200),
    "ttl":      ("缓存 TTL（天）",    "cacheTtlDays",             1,  30),
    "preload":  ("预加载数量（条）",   "cachePreloadCount",       0, 1000),
    "alert":    ("连续失败告警阈值",   "failureAlertThreshold",   0, 100),
    "mem_mb":   ("内存缓存上限（MB）", "memoryCacheMaxMb",        0, 1024),
    "mem_ttl":  ("内存缓存 TTL（秒）", "memoryCacheTtlSeconds",   0, 86400),
}


def _edit_numeric(chat_id: int, message_id: int, cb_id: str, key: str) -> None:
    if key not in _NUMERIC_FIELDS:
        ui.answer_cb(cb_id, "未知字段")
        return
    ui.answer_cb(cb_id)
    label, config_key, lo, hi = _NUMERIC_FIELDS[key]
    current = _get_cfg().get(config_key, "?")
    states.set_state(chat_id, f"tl_{key}")
    ui.edit(
        chat_id, message_id,
        f"请输入{label}（整数，范围 {lo}-{hi}）：\n\n"
        f"当前: <code>{current}</code>",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "tl:show")]]),
    )


def _on_numeric_input(chat_id: int, key: str, text: str) -> None:
    if key not in _NUMERIC_FIELDS:
        states.pop_state(chat_id)
        return
    label, config_key, lo, hi = _NUMERIC_FIELDS[key]
    try:
        v = int((text or "").strip())
    except ValueError:
        ui.send(chat_id, f"❌ 非法数字，请重新输入{label}：")
        return
    if v < lo or v > hi:
        ui.send(chat_id, f"❌ 范围 {lo}-{hi}，请重新输入：")
        return

    def _m(c):
        c.setdefault("translation", {})[config_key] = v
    config.update(_m)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ {label}已更新为 <code>{v}</code>",
        back_label="◀ 返回翻译层", back_callback="tl:show",
    )


# ─── 翻译提示词 ──────────────────────────────────────────────────

def _show_prompt(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    cfg = _get_cfg()
    prompt = cfg.get("prompt") or ""
    is_default = not prompt
    display_prompt = prompt if prompt else translation.DEFAULT_TRANSLATION_PROMPT
    target_lang = cfg.get("targetLanguage") or "English"

    text = (
        "📝 <b>翻译提示词</b>\n\n"
        f"当前使用: <code>{'内置默认' if is_default else '自定义'}</code>\n\n"
        f"<pre>{ui.escape_html(display_prompt)}</pre>\n\n"
        f"<i>提示词中 {{target_language}} 会被替换为"
        f" <code>{ui.escape_html(target_lang)}</code></i>"
    )
    rows = [
        [ui.btn("✏ 自定义提示词", "tl:edit:prompt"),
         ui.btn("🔄 恢复默认", "tl:prompt:reset")],
        [ui.btn("◀ 返回翻译层", "tl:show")],
    ]
    ui.edit(chat_id, message_id, ui.truncate(text), reply_markup=ui.inline_kb(rows))


def _edit_prompt(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, "tl_prompt")
    ui.edit(
        chat_id, message_id,
        "请输入自定义翻译提示词。\n\n"
        "支持 <code>{target_language}</code> 占位符（运行时替换为目标语言）。\n\n"
        "直接发送多行文本即可：",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "tl:show:prompt")]]),
    )


def _on_prompt_input(chat_id: int, text: str) -> None:
    prompt = (text or "").strip()
    if not prompt:
        ui.send(chat_id, "❌ 提示词不能为空，请重新输入：")
        return

    def _m(c):
        c.setdefault("translation", {})["prompt"] = prompt
    config.update(_m)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        "✅ 翻译提示词已更新",
        back_label="◀ 返回翻译层", back_callback="tl:show",
    )


def _on_prompt_reset(chat_id: int, message_id: int, cb_id: str) -> None:
    def _m(c):
        c.setdefault("translation", {})["prompt"] = ""
    config.update(_m)
    ui.answer_cb(cb_id, "已恢复默认")
    _show_prompt(chat_id, message_id, "-")


# ─── 测试翻译 ─────────────────────────────────────────────────────

_SYNC_TEST = False


def _spawn_async_task(coro_factory, name: str = "translation-test") -> None:
    if _SYNC_TEST:
        asyncio.run(coro_factory())
        return

    def _runner():
        try:
            asyncio.run(coro_factory())
        except Exception:
            import traceback
            traceback.print_exc()
    threading.Thread(target=_runner, daemon=True, name=name).start()


def _show_test_prompt(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    cfg = _get_cfg()
    ok, reason = translation.validate_ready(cfg, require_enabled=False)
    if not ok:
        ui.edit(
            chat_id, message_id,
            f"⚠ 当前翻译层还不可测试：<code>{ui.escape_html(reason)}</code>",
            reply_markup=ui.inline_kb([[ui.btn("◀ 返回翻译层", "tl:show")]]),
        )
        return
    states.set_state(chat_id, "tl_test")
    ui.edit(
        chat_id, message_id,
        "🧪 <b>测试翻译</b>\n\n"
        "请直接发送一段文本，我会用当前翻译模型翻译成目标语言。\n"
        "这只测试翻译层效果，不会发送给业务模型。",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "tl:show")]]),
    )


def _on_test_input(chat_id: int, text: str) -> None:
    sample = (text or "").strip()
    if not sample:
        ui.send(chat_id, "❌ 测试文本不能为空，请重新输入：")
        return
    states.pop_state(chat_id)
    sent = ui.send(chat_id, "🧪 正在测试翻译层，请稍等…") or {}
    msg_id = ((sent.get("result") or {}) if isinstance(sent, dict) else {}).get("message_id")

    async def _run():
        result = await translation.translate_text_for_test(sample)
        ok = bool(result.get("ok"))
        target_lang = result.get("targetLanguage") or _get_cfg().get("targetLanguage") or "English"
        if ok:
            cached = " · 命中缓存" if result.get("cached") else ""
            text_out = (
                f"✅ <b>翻译测试完成</b><code>{ui.escape_html(cached)}</code>\n\n"
                f"目标语言: <code>{ui.escape_html(target_lang)}</code>\n\n"
                f"原文:\n<pre>{ui.escape_html(result.get('original') or '')}</pre>\n\n"
                f"译文:\n<pre>{ui.escape_html(result.get('translated') or '')}</pre>"
            )
        else:
            text_out = (
                "❌ <b>翻译测试失败</b>\n\n"
                f"原因: <code>{ui.escape_html(result.get('reason') or 'unknown')}</code>\n\n"
                f"原文:\n<pre>{ui.escape_html(sample)}</pre>"
            )
        kb = ui.inline_kb([[ui.btn("◀ 返回翻译层", "tl:show")]])
        if msg_id:
            ui.edit(chat_id, int(msg_id), ui.truncate(text_out), reply_markup=kb)
        else:
            ui.send(chat_id, ui.truncate(text_out), reply_markup=kb)

    _spawn_async_task(_run, name="tg-translation-test")


# ─── 清空缓存 ─────────────────────────────────────────────────────

def _show_clear_cache(chat_id: int, message_id: int, cb_id: str) -> None:
    ui.answer_cb(cb_id)
    count = translation.cache_count()
    text = (
        f"确定要清空翻译缓存吗？\n\n"
        f"当前缓存 <code>{count}</code> 条记录。\n"
        f"清空后下次翻译将全部重新调用模型。"
    )
    ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([
        [ui.btn("✅ 确认清空", "tl:cache:confirm"),
         ui.btn("❌ 取消", "tl:show")],
    ]))


def _on_clear_confirm(chat_id: int, message_id: int, cb_id: str) -> None:
    cleared = translation.clear_cache()
    ui.answer_cb(cb_id, f"已清空 {cleared} 条")
    show(chat_id, message_id, "-")


# ─── callback 路由 ───────────────────────────────────────────────

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "tl:show":
        states.pop_state(chat_id)
        show(chat_id, message_id, cb_id); return True
    if data == "tl:toggle":
        _on_toggle(chat_id, message_id, cb_id); return True
    if data == "tl:toggle:system":
        _on_toggle_system(chat_id, message_id, cb_id); return True
    if data == "tl:test":
        _show_test_prompt(chat_id, message_id, cb_id); return True

    # 模型选择
    if data.startswith("tl:edit:model:"):
        page = _safe_int(data.split(":")[-1])
        _model_picker(chat_id, message_id, cb_id, field="model", page=page)
        return True
    if data.startswith("tl:edit:fallback:"):
        page = _safe_int(data.split(":")[-1])
        _model_picker(chat_id, message_id, cb_id, field="fallback", page=page)
        return True
    if data.startswith("tl:pick:model:"):
        mc = data.split(":", 3)[3]
        _on_pick_model(chat_id, message_id, cb_id, "model", mc)
        return True
    if data.startswith("tl:pick:fallback:"):
        mc = data.split(":", 3)[3]
        _on_pick_model(chat_id, message_id, cb_id, "fallback", mc)
        return True
    if data == "tl:clear:fallback":
        _on_clear_fallback(chat_id, message_id, cb_id); return True

    # 目标语言
    if data == "tl:edit:lang":
        _show_lang_picker(chat_id, message_id, cb_id); return True
    if data.startswith("tl:pick:lang:"):
        lang = data.split(":", 3)[3]
        _on_pick_lang(chat_id, message_id, cb_id, lang); return True

    # 数值编辑
    for key in _NUMERIC_FIELDS:
        if data == f"tl:edit:{key}":
            _edit_numeric(chat_id, message_id, cb_id, key); return True

    # 提示词
    if data == "tl:show:prompt":
        _show_prompt(chat_id, message_id, cb_id); return True
    if data == "tl:edit:prompt":
        _edit_prompt(chat_id, message_id, cb_id); return True
    if data == "tl:prompt:reset":
        _on_prompt_reset(chat_id, message_id, cb_id); return True

    # 缓存
    if data == "tl:cache:clear":
        _show_clear_cache(chat_id, message_id, cb_id); return True
    if data == "tl:cache:confirm":
        _on_clear_confirm(chat_id, message_id, cb_id); return True

    return False


# ─── 文本状态机路由 ───────────────────────────────────────────────

def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    # 数值类
    for key in _NUMERIC_FIELDS:
        if action == f"tl_{key}":
            _on_numeric_input(chat_id, key, text); return True

    # 提示词
    if action == "tl_prompt":
        _on_prompt_input(chat_id, text); return True

    # 测试翻译
    if action == "tl_test":
        _on_test_input(chat_id, text); return True

    return False


# ─── 工具 ─────────────────────────────────────────────────────────

def _safe_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return default
