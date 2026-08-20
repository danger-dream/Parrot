"""分级 provider 与 models discovery 添加向导（由 channel_menu 路由调用）。"""
from __future__ import annotations
import math
import time

from ... import config
from ...channel import api_channel
from ...channel.url_utils import detect_suffix_protocol, split_base_url
from ...models_discovery import ModelsDiscoveryError, derive_custom_models_url, discover_models
from ...providers.catalog import PROVIDER_CATALOG, get_preset
from .. import states, ui

PAGE = 10
NAV = [ui.btn("❌ 取消", "chw:cancel")]


def _cm():
    from . import channel_menu
    return channel_menu


def _bounds(n, page):
    pages = max(1, math.ceil(n / PAGE)); page = max(0, min(int(page), pages - 1))
    return page, page * PAGE, pages


def _providers_kb(page=0):
    page, start, pages = _bounds(len(PROVIDER_CATALOG), page)
    rows = [[ui.btn(b.display_name, f"chw:brand:{i}:{page}")]
            for i, b in enumerate(PROVIDER_CATALOG[start:start + PAGE], start)]
    if pages > 1:
        rows.append([ui.btn("◀", f"chw:brands:{page-1}"), ui.btn(f"{page+1}/{pages}", "chw:noop"),
                     ui.btn("▶", f"chw:brands:{page+1}")])
    rows.append(NAV); return ui.inline_kb(rows)


def show_providers(chat_id, message_id=None, page=0):
    text = ("➕ <b>添加渠道（2/5）</b>\n\n可直接输入自定义 <b>Base URL</b>，或按品牌选择提供商模板：\n\n"
            "<i>自定义 URL 可填写域名、API 根路径或完整调用路径。</i>")
    fn = ui.edit if message_id is not None else ui.send
    args = (chat_id, message_id, text) if message_id is not None else (chat_id, text)
    fn(*args, reply_markup=_providers_kb(page))


def wiz_on_name_input(chat_id, text):
    name = (text or "").strip()
    if not name: ui.send(chat_id, "❌ 名称不能为空，请重新输入："); return
    if len(name) > 64: ui.send(chat_id, "❌ 名称过长（上限 64 字符），请重新输入："); return
    if any(c.get("name") == name for c in config.get().get("channels", [])):
        ui.send(chat_id, f"❌ 渠道名称 <code>{ui.escape_html(name)}</code> 已存在，请换一个："); return
    states.set_state(chat_id, "ch_wiz_url", {"name": name, "provider_page": 0}); show_providers(chat_id)


def wiz_show_brands(chat_id, message_id, cb_id, page):
    state = states.get_state(chat_id); ui.answer_cb(cb_id)
    if not state or state.get("action") != "ch_wiz_url": return
    state["data"]["provider_page"] = max(0, page); states.set_state(chat_id, "ch_wiz_url", state["data"])
    show_providers(chat_id, message_id, page)


def wiz_select_brand(chat_id, message_id, cb_id, idx, page):
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_url": ui.answer_cb(cb_id, "会话已过期"); return
    try: brand = PROVIDER_CATALOG[idx]
    except IndexError: ui.answer_cb(cb_id, "无效提供商"); return
    data = state["data"]; data["provider_page"] = page
    if len(brand.presets) == 1:
        ui.answer_cb(cb_id, brand.display_name); _apply_preset(chat_id, message_id, data, idx, 0); return
    data["brand_idx"] = idx; states.set_state(chat_id, "ch_wiz_preset", data); ui.answer_cb(cb_id)
    rows = [[ui.btn(p.display_name, f"chw:preset:{i}")] for i, p in enumerate(brand.presets)]
    rows += [[ui.btn("◀ 返回提供商列表", "chw:preset_back")], NAV]
    ui.edit(chat_id, message_id, f"➕ <b>添加渠道（2/5）</b>\n\n请选择 <b>{ui.escape_html(brand.display_name)}</b> 的方案：",
            reply_markup=ui.inline_kb(rows))


def wiz_select_preset(chat_id, message_id, cb_id, idx):
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_preset": ui.answer_cb(cb_id, "会话已过期"); return
    ui.answer_cb(cb_id); _apply_preset(chat_id, message_id, state["data"], state["data"]["brand_idx"], idx)


def _apply_preset(chat_id, message_id, data, brand_idx, preset_idx):
    try: brand, preset = PROVIDER_CATALOG[brand_idx], PROVIDER_CATALOG[brand_idx].presets[preset_idx]
    except IndexError: ui.send(chat_id, "❌ 提供商模板已变化，请重新选择"); return
    data.update(providerId=brand.id, providerPresetId=preset.id, brand_idx=brand_idx, preset_idx=preset_idx)
    for k in ("baseUrl", "apiPath", "protocol"): data.pop(k, None)
    states.set_state(chat_id, "ch_wiz_protocol", data)
    if len(preset.protocols) == 1: _select_provider_protocol(chat_id, message_id, data, next(iter(preset.protocols)))
    else: send_protocol_panel(chat_id, message_id)


def wiz_preset_back(chat_id, message_id, cb_id):
    state = states.get_state(chat_id); ui.answer_cb(cb_id)
    if not state or state.get("action") != "ch_wiz_preset": return
    data = state["data"]; states.set_state(chat_id, "ch_wiz_url", data)
    show_providers(chat_id, message_id, data.get("provider_page", 0))


def wiz_on_url_input(chat_id, text):
    url = (text or "").strip().rstrip("/"); state = states.get_state(chat_id)
    if not url.startswith(("http://", "https://")): ui.send(chat_id, "❌ URL 需以 http:// 或 https:// 开头，请重新输入："); return
    if not state or state.get("action") != "ch_wiz_url": ui.send(chat_id, "❌ 会话过期，请重新添加"); return
    try: base, path = split_base_url(url)
    except ValueError as exc: ui.send(chat_id, f"❌ URL 无效：{ui.escape_html(str(exc))}"); return
    data = state["data"]
    for k in ("providerId", "providerPresetId", "brand_idx", "preset_idx"): data.pop(k, None)
    data["baseUrl"] = base
    if path: data["apiPath"] = path
    else: data.pop("apiPath", None)
    states.set_state(chat_id, "ch_wiz_protocol", data); send_protocol_panel(chat_id)


def send_protocol_panel(chat_id, message_id=None):
    cm = _cm(); state = states.get_state(chat_id) or {}; data = state.get("data") or {}
    preset = get_preset(data.get("providerId", ""), data.get("providerPresetId", ""))
    protocols = list(preset.protocols) if preset else list(cm._PROTOCOL_LABEL)
    rows = [[cm._protocol_button(p, f"chw:proto:{p}")] for p in protocols] + [NAV]
    head = "✅ 提供商模板已设置" if preset else "✅ URL 已设置"
    text = f"{head}\n\n➕ <b>添加渠道（3/5）</b>\n\n请选择该渠道的上游协议："
    if message_id is None: ui.send(chat_id, text, reply_markup=ui.inline_kb(rows))
    else: ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb(rows))


def _to_key(chat_id, message_id, data, protocol):
    cm = _cm(); data["protocol"] = protocol
    if protocol != "anthropic": data["cc_mimicry"] = False
    elif "cc_mimicry" not in data: data["cc_mimicry"] = True
    states.set_state(chat_id, "ch_wiz_key", data)
    ui.edit(chat_id, message_id, f"✅ 协议：{cm._protocol_body_label(protocol)}\n\n➕ <b>添加渠道（4/5）</b>\n\n请输入该渠道的 API Key：",
            reply_markup=ui.inline_kb([NAV]))


def _select_provider_protocol(chat_id, message_id, data, protocol):
    preset = get_preset(data.get("providerId", ""), data.get("providerPresetId", ""))
    if not preset or protocol not in preset.protocols: return False
    data["baseUrl"], data["apiPath"] = split_base_url(preset.protocols[protocol])
    data["cc_mimicry"] = bool(protocol == "anthropic" and preset.cc_mimicry)
    _to_key(chat_id, message_id, data, protocol); return True


def wiz_on_protocol_select(chat_id, message_id, cb_id, protocol):
    cm = _cm(); state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_protocol": ui.answer_cb(cb_id, "会话已过期"); return
    data = state["data"]
    if data.get("providerId"):
        if not _select_provider_protocol(chat_id, message_id, data, protocol): ui.answer_cb(cb_id, "该模板不支持此协议")
        else: ui.answer_cb(cb_id)
        return
    if protocol not in cm._PROTOCOL_LABEL: ui.answer_cb(cb_id, "无效协议"); return
    path = data.get("apiPath"); detected = detect_suffix_protocol(path) if path else None
    if path and detected and detected != protocol:
        ui.answer_cb(cb_id); ui.edit(chat_id, message_id, "⚠ <b>协议与路径不匹配</b>\n\n如何处理？",
          reply_markup=ui.inline_kb([[cm._protocol_button(detected, f"chw:proto_adopt:{detected}", prefix="✅ 使用 ")],
            [cm._protocol_button(protocol, f"chw:proto_force:{protocol}", prefix="⚠ 坚持 ")],
            [ui.btn("◀ 返回修改 URL", "chw:back_to_url")]])); return
    ui.answer_cb(cb_id); _to_key(chat_id, message_id, data, protocol)


def wiz_back_to_url(chat_id, message_id, cb_id):
    state = states.get_state(chat_id); ui.answer_cb(cb_id)
    if not state: return
    data = state["data"]
    for k in ("baseUrl", "apiPath", "protocol", "providerId", "providerPresetId", "brand_idx", "preset_idx"): data.pop(k, None)
    states.set_state(chat_id, "ch_wiz_url", data); show_providers(chat_id, message_id, data.get("provider_page", 0))


def manual_panel(chat_id, message_id, data):
    data["models_mode"] = "manual"
    data["models_source"] = "manual"
    states.set_state(chat_id, "ch_wiz_models", data)
    prefix = ""
    if data.get("discovery_error"):
        prefix = ("⚠️ <b>自动获取模型失败，已切换为手动输入</b>\n\n"
                  f"原因：{ui.escape_html(str(data['discovery_error']))}\n\n")
    elif data.get("manual_notice"):
        prefix = f"ℹ️ {ui.escape_html(str(data['manual_notice']))}\n\n"
    text = (prefix + "➕ <b>添加渠道（5/5）</b>\n\n"
            "请输入模型列表。格式 <code>真实名[:别名]</code>，以 ,/，/;/； 分隔。\n\n"
            "不写别名则别名=真实名；别名不可重复。")
    rows = []
    if data.get("discovery_retry_available"):
        rows.append([ui.btn("🔄 重试自动获取", "chw:discover_retry"),
                     ui.btn("◀ 返回修改 Key", "chw:key_back")])
    else:
        rows.append([ui.btn("◀ 返回修改 Key", "chw:key_back")])
    rows.append(NAV)
    kb = ui.inline_kb(rows)
    if message_id is None: ui.send(chat_id, text, reply_markup=kb)
    else: ui.edit(chat_id, message_id, text, reply_markup=kb)


def _enter_model_select(chat_id, message_id, data, ids, *, source, error=None, retry=False):
    data.update(
        discovered_models=list(dict.fromkeys(ids)),
        selected_models=[],
        model_page=0,
        models_mode="discovered",
        models_source=source,
        discovery_retry_available=bool(retry),
    )
    data.pop("manual_notice", None)
    if error:
        data["discovery_error"] = str(error)
    else:
        data.pop("discovery_error", None)
    states.set_state(chat_id, "ch_wiz_model_select", data)
    render_models(chat_id, message_id, data)


def wiz_on_key_input(chat_id, text):
    key = (text or "").strip(); state = states.get_state(chat_id)
    if len(key) < 5: ui.send(chat_id, "❌ API Key 过短，请重新输入："); return
    if not state or state.get("action") != "ch_wiz_key": ui.send(chat_id, "❌ 会话过期，请重新添加"); return
    data = state["data"]; data["apiKey"] = key
    preset = get_preset(data.get("providerId", ""), data.get("providerPresetId", ""))
    if preset and not preset.models_url:
        data.pop("discovery_error", None)
        data["discovery_retry_available"] = False
        if preset.static_models:
            _enter_model_select(chat_id, None, data, preset.static_models, source="static")
        else:
            data["manual_notice"] = "该提供商未公开模型列表，已直接进入手动输入。"
            manual_panel(chat_id, None, data)
        return
    data.pop("manual_notice", None)
    msg = ui.send(chat_id, "🔄 <b>正在发现模型…</b>\n\n请稍候，可随时取消。", reply_markup=ui.inline_kb([NAV]))
    start_discovery(chat_id, ((msg or {}).get("result") or {}).get("message_id"), data)


def start_discovery(chat_id, message_id, data):
    generation = time.time_ns(); data["discovery_generation"] = generation
    states.set_state(chat_id, "ch_wiz_discovery", data)
    preset = get_preset(data.get("providerId", ""), data.get("providerPresetId", ""))
    async def run():
        error = None; ids = []; source = "live"
        try:
            if preset and preset.models_url:
                ids = await discover_models(preset.models_url, data["apiKey"], auth=preset.models_auth, parser=preset.models_parser)
            elif preset and preset.static_models:
                ids = list(preset.static_models); source = "static"
            elif preset:
                raise ModelsDiscoveryError("该提供商未公开模型列表")
            else:
                ids = await discover_models(derive_custom_models_url(data["baseUrl"], data.get("apiPath")), data["apiKey"])
        except ModelsDiscoveryError as exc:
            error = str(exc)
            if preset and preset.static_models:
                ids = list(preset.static_models); source = "static"
        cur = states.get_state(chat_id)
        if not cur or cur.get("action") != "ch_wiz_discovery" or cur["data"].get("discovery_generation") != generation: return
        current = cur["data"]
        retry = bool((preset and preset.models_url) or not preset)
        if ids:
            _enter_model_select(chat_id, message_id, current, ids, source=source,
                                error=error if source == "static" else None,
                                retry=bool(error and retry))
        else:
            current["discovery_error"] = error or "上游未返回可用模型"
            current["discovery_retry_available"] = retry
            current.pop("manual_notice", None)
            manual_panel(chat_id, message_id, current)
    _cm()._spawn_async_task(run, name=f"wiz-models-{chat_id}")


def model_kb(data):
    models = data["discovered_models"]; selected = set(data.get("selected_models", []))
    page, start, pages = _bounds(len(models), data.get("model_page", 0)); data["model_page"] = page
    rows = [[ui.btn(("✅ " if m in selected else "⬜ ") + m, f"chw:mt:{i}:{page}")]
            for i, m in enumerate(models[start:start + PAGE], start)]
    if pages > 1: rows.append([ui.btn("◀", f"chw:mp:{page-1}"), ui.btn(f"{page+1}/{pages}", "chw:noop"), ui.btn("▶", f"chw:mp:{page+1}")])
    rows += [[ui.btn("✅ 全选", "chw:mall"), ui.btn("🔄 反选", "chw:minvert")],
             [ui.btn(f"确认选择（{len(selected)}）", "chw:mconfirm")]]
    if data.get("discovery_retry_available"):
        rows.append([ui.btn("✍️ 手动输入最新模型", "chw:manual"),
                     ui.btn("🔄 重试实时获取", "chw:discover_retry")])
        rows.append([ui.btn("◀ 返回修改 Key", "chw:key_back")])
    else:
        rows.append([ui.btn("✍️ 手动输入最新模型", "chw:manual"),
                     ui.btn("◀ 返回修改 Key", "chw:key_back")])
    rows.append(NAV)
    return ui.inline_kb(rows)


def render_models(chat_id, message_id, data):
    count = len(data["discovered_models"])
    if data.get("models_source") == "static":
        if data.get("discovery_error"):
            head = ("⚠️ <b>实时模型列表获取失败</b>\n\n"
                    f"原因：{ui.escape_html(str(data['discovery_error']))}\n\n"
                    f"当前显示 {count} 个内置参考模型，可能不是最新版本。")
        else:
            head = f"ℹ️ 当前显示 {count} 个内置参考模型，可能不是最新版本。"
    else:
        head = f"✅ 已从上游获取 {count} 个模型"
    text = (head + "\n\n➕ <b>添加渠道（5/5）</b>\n\n"
            "请选择要启用的模型（可跨页多选），也可以手动输入最新模型名：")
    (ui.edit(chat_id, message_id, text, reply_markup=model_kb(data)) if message_id else ui.send(chat_id, text, reply_markup=model_kb(data)))


def wiz_model_page(chat_id, message_id, cb_id, page):
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_model_select":
        ui.answer_cb(cb_id, "会话已过期")
        return
    data = state["data"]
    page, _, _ = _bounds(len(data["discovered_models"]), page)
    data["model_page"] = page
    states.set_state(chat_id, "ch_wiz_model_select", data)
    ui.answer_cb(cb_id)
    render_models(chat_id, message_id, data)


def wiz_model_toggle(chat_id, message_id, cb_id, idx, page):
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_model_select": ui.answer_cb(cb_id, "会话已过期"); return
    data = state["data"]
    try: model = data["discovered_models"][idx]
    except IndexError: ui.answer_cb(cb_id, "模型快照已失效"); return
    selected = data.setdefault("selected_models", []); selected.remove(model) if model in selected else selected.append(model)
    data["model_page"] = page; states.set_state(chat_id, "ch_wiz_model_select", data); ui.answer_cb(cb_id); render_models(chat_id, message_id, data)


def wiz_model_bulk(chat_id, message_id, cb_id, invert):
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_model_select": return
    data = state["data"]; selected = set(data.get("selected_models", []))
    data["selected_models"] = [m for m in data["discovered_models"] if m not in selected] if invert else list(data["discovered_models"])
    states.set_state(chat_id, "ch_wiz_model_select", data); ui.answer_cb(cb_id); render_models(chat_id, message_id, data)


def wiz_model_confirm(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_model_select": return
    data = state["data"]; selected = set(data.get("selected_models", []))
    if not selected: ui.answer_cb(cb_id, "请至少选择一个模型", show_alert=True); return
    data["models"] = [{"real": m, "alias": m} for m in data["discovered_models"] if m in selected]
    data["test_results"] = {}; data["test_page"] = 0; states.set_state(chat_id, "ch_wiz_test", data); ui.answer_cb(cb_id)
    cm = _cm(); ui.edit(chat_id, message_id, cm._wiz_test_intro(data), reply_markup=test_kb(data))


def wiz_manual(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    if state: ui.answer_cb(cb_id); manual_panel(chat_id, message_id, state["data"])


def wiz_key_back(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    if not state: return
    data = state["data"]; data["discovery_generation"] = time.time_ns(); states.set_state(chat_id, "ch_wiz_key", data); ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, "➕ <b>添加渠道（4/5）</b>\n\n请重新输入 API Key：", reply_markup=ui.inline_kb([NAV]))


def wiz_discovery_retry(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    if not state or state.get("action") not in ("ch_wiz_models", "ch_wiz_model_select", "ch_wiz_discovery_error"):
        ui.answer_cb(cb_id, "当前不能重试", show_alert=True)
        return
    data = state["data"]
    if not data.get("discovery_retry_available") and state.get("action") != "ch_wiz_discovery_error":
        ui.answer_cb(cb_id, "该提供商没有可重试的模型接口", show_alert=True)
        return
    ui.answer_cb(cb_id, "正在重试")
    ui.edit(chat_id, message_id, "🔄 <b>正在发现模型…</b>", reply_markup=ui.inline_kb([NAV]))
    start_discovery(chat_id, message_id, data)


def wiz_on_models_input(chat_id, text):
    try: models = api_channel.parse_models_input(text or "")
    except ValueError as exc: ui.send(chat_id, f"❌ {ui.escape_html(str(exc))}\n请重新输入："); return
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_models": ui.send(chat_id, "❌ 会话过期，请重新添加"); return
    data = state["data"]; data["models"] = models; data["test_results"] = {}; data["test_page"] = 0
    states.set_state(chat_id, "ch_wiz_test", data); _cm()._wiz_send_test_panel(chat_id, data)


def test_intro(data):
    models = data["models"]
    page, start, pages = _bounds(len(models), data.get("test_page", 0))
    data["test_page"] = page
    header = (
        "🧪 <b>渠道测试</b>\n\n"
        f"渠道: <code>{ui.escape_html(data['name'])}</code>\n"
        f"模型: {len(models)} 个（第 {page + 1}/{pages} 页）\n\n"
        "请选择模型进行联通性测试。至少有一个模型测试成功才能保存渠道。\n"
        "<i>（若跳过测试，全部模型默认标记为可用，由后台探测机制处理后续）</i>"
    )
    results = data.get("test_results") or {}
    page_results = []
    for model in models[start:start + PAGE]:
        result = results.get(model["real"])
        if result is not None:
            page_results.append((model, result))
    if page_results:
        header += "\n\n<b>测试结果</b>:"
        for model, (ok, elapsed, reason) in page_results:
            name = ui.escape_html(model["alias"])
            if ok:
                header += f"\n  ✅ <code>{name}</code> — 耗时 {elapsed}ms"
            else:
                header += f"\n  ❌ <code>{name}</code> — {ui.escape_html((reason or '')[:80])}"
    return header


def test_kb(data):
    models = data["models"]; page, start, pages = _bounds(len(models), data.get("test_page", 0)); data["test_page"] = page
    rows = []; row = []
    for i, m in enumerate(models[start:start + PAGE], start):
        status = data.get("test_results", {}).get(m["real"]); prefix = "🧪 " if status is None else "✅ " if status[0] else "❌ "
        label = m["alias"] if m["alias"] == m["real"] else f"{m['alias']}({m['real']})"; row.append(ui.btn(prefix + label, f"chw:test:{i}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    if pages > 1: rows.append([ui.btn("◀", f"chw:tp:{page-1}"), ui.btn(f"{page+1}/{pages}", "chw:noop"), ui.btn("▶", f"chw:tp:{page+1}")])
    rows.append([ui.btn("🧪 测试全部模型", "chw:test_all"), ui.btn("⏭ 跳过测试", "chw:skip_test")])
    save = []
    if any(r[0] for r in data.get("test_results", {}).values()): save.append(ui.btn("💾 保存渠道", "chw:save"))
    save.append(ui.btn("◀ 返回模型选择/手填", "chw:back")); rows += [save, NAV]; return ui.inline_kb(rows)


def wiz_test_page(chat_id, message_id, cb_id, page):
    state = states.get_state(chat_id)
    if not state or state.get("action") != "ch_wiz_test":
        ui.answer_cb(cb_id, "会话已过期")
        return
    data = state["data"]
    page, _, _ = _bounds(len(data["models"]), page)
    data["test_page"] = page
    states.set_state(chat_id, "ch_wiz_test", data)
    ui.answer_cb(cb_id)
    _cm()._wiz_refresh_test_panel(chat_id, message_id, data)


def wiz_back_to_models(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    if not state: return
    data = state["data"]; data.pop("test_results", None); ui.answer_cb(cb_id)
    if data.get("models_mode") == "discovered" and data.get("discovered_models"):
        states.set_state(chat_id, "ch_wiz_model_select", data); render_models(chat_id, message_id, data)
    else: manual_panel(chat_id, message_id, data)
