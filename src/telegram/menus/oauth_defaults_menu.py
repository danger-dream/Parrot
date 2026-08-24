"""OAuth 默认模型配置菜单 (一级页面 + 两个编辑入口 + 引用扫描确认页)。

配置字段:
  - Anthropic OAuth → cfg["oauthDefaultModels"] (顶层 list[str])
  - OpenAI    OAuth → cfg["openaiOAuth"]["defaultModels"]
  - Grok/xAI  OAuth → cfg["xaiOAuth"]["defaultModels"]
  - Antigravity OAuth → cfg["antigravityOAuth"]["defaultModels"]

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
状态机 action:
  - `odm_discovery` / `odm_model_select` / `odm_edit:<family>`
pending 保存: cfg["_odm_pending"]["<family>"] = {"new": [...], "old": [...]}
  (下划线前缀 → 配置 sanitize 时如果需要可剥离; 当前版本不在 save/load
  做 sanitize, 但保存策略仍在持久前 pop)
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time

from ... import config
from ...models_discovery import ModelsDiscoveryError, discover_models
from ...oauth_ids import account_key as _account_key
from .. import states, ui


_FAMILIES: tuple[str, ...] = ("anthropic", "openai", "xai", "antigravity")

_FAM_LABEL = {
    "anthropic": "Claude",
    "openai":    "OpenAI",
    "xai":       "Grok",
    "antigravity": "Antigravity",
}
PAGE = 12
_SYNC_SPAWN = False
_FAM_PROVIDER = {
    "anthropic": "claude",
    "openai":    "openai",
    "xai":       "xai",
    "antigravity": "antigravity",
}


def _fam_body_label(family: str, *, bold: bool = True) -> str:
    icon = ui.provider_custom_emoji_html(_FAM_PROVIDER.get(family, family))
    label = ui.escape_html(_FAM_LABEL.get(family, family))
    return f"{icon} <b>{label}</b>" if bold else f"{icon} {label}"


def _ingress_body_label(ingress: str) -> str:
    if ingress == "anthropic":
        return f"{ui.family_tag('anthropic')} (/v1/messages)"
    if ingress == "openai-chat":
        return f"{ui.family_tag('openai')} Chat (/v1/chat/completions)"
    if ingress == "openai-responses":
        return f"{ui.family_tag('openai')} Responses (/v1/responses)"
    return ui.escape_html(ingress)


# ─── 读写底层 ────────────────────────────────────────────────────

def _read_list(family: str) -> list[str]:
    cfg = config.get()
    if family == "anthropic":
        raw = cfg.get("oauthDefaultModels") or []
    elif family == "xai":
        raw = (cfg.get("xaiOAuth") or {}).get("defaultModels") or []
    elif family == "antigravity":
        raw = (cfg.get("antigravityOAuth") or {}).get("defaultModels") or []
    else:
        raw = (cfg.get("openaiOAuth") or {}).get("defaultModels") or []
    return [str(x) for x in raw if isinstance(x, str) and x.strip()]


def _write_list(family: str, models: list[str]) -> None:
    def _mutate(cfg: dict) -> None:
        if family == "anthropic":
            cfg["oauthDefaultModels"] = list(models)
        elif family == "xai":
            cfg.setdefault("xaiOAuth", {})["defaultModels"] = list(models)
        elif family == "antigravity":
            cfg.setdefault("antigravityOAuth", {})["defaultModels"] = list(models)
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
      openai/xai/antigravity → ingressDefaultModel["openai-chat"/"openai-responses"]
                + modelMapping["openai-chat"/"openai-responses"]
    API Key 白名单本身无家族概念 — OpenAI/Grok/Cursor 家族模型可能和 Anthropic 模型
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
        "antigravity": {"openai-chat", "openai-responses"},
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
        "antigravity": {"openai-chat", "openai-responses"},
    }
    ingresses = fam_ingress.get(family, set())

    def _mutate(cfg: dict) -> None:
        # a) 先写 OAuth 默认
        if family == "anthropic":
            cfg["oauthDefaultModels"] = list(new_models)
        elif family == "xai":
            cfg.setdefault("xaiOAuth", {})["defaultModels"] = list(new_models)
        elif family == "antigravity":
            cfg.setdefault("antigravityOAuth", {})["defaultModels"] = list(new_models)
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

def _spawn_async_task(coro_factory, name: str = "odm-task") -> None:
    if _SYNC_SPAWN:
        try:
            asyncio.run(coro_factory())
        except Exception:
            import traceback
            traceback.print_exc()
        return

    def _runner():
        try:
            asyncio.run(coro_factory())
        except Exception:
            import traceback
            traceback.print_exc()

    threading.Thread(target=_runner, daemon=True, name=name).start()


def _bounds(n, page):
    pages = max(1, math.ceil(n / PAGE))
    page = max(0, min(int(page), pages - 1))
    return page, page * PAGE, pages


def abandon_edit(chat_id: int) -> None:
    st = states.get_state(chat_id)
    if not st:
        return
    action = str(st.get("action") or "")
    if action in ("odm_discovery", "odm_model_select") or action.startswith("odm_edit:"):
        states.pop_state(chat_id)


def _cursor_account_count() -> int:
    from ... import oauth_manager
    return sum(
        1 for acc in (config.get().get("oauthAccounts") or [])
        if isinstance(acc, dict) and oauth_manager.provider_of(acc) == "cursor"
    )


def _static_models(family: str) -> list[str]:
    defaults = config.DEFAULT_CONFIG
    if family == "anthropic":
        raw = defaults.get("oauthDefaultModels") or []
    elif family == "xai":
        raw = (defaults.get("xaiOAuth") or {}).get("defaultModels") or []
    elif family == "antigravity":
        raw = (defaults.get("antigravityOAuth") or {}).get("defaultModels") or []
    else:
        raw = (defaults.get("openaiOAuth") or {}).get("defaultModels") or []
    return [str(x) for x in raw if str(x).strip()]


def _has_live_endpoint(family: str) -> bool:
    return family == "xai"


def _xai_models_url() -> str:
    raw = config.get().get("xaiOAuth")
    cfg = raw if isinstance(raw, dict) else {}
    base = str(cfg.get("apiBaseUrl") or cfg.get("baseUrl") or "https://api.x.ai/v1").rstrip("/")
    return base if base.endswith("/models") else base + "/models"


def _first_enabled_account_key(provider: str) -> str | None:
    from ... import oauth_manager
    for acc in config.get().get("oauthAccounts") or []:
        if not isinstance(acc, dict):
            continue
        if oauth_manager.provider_of(acc) != provider:
            continue
        if acc.get("enabled", True) and not acc.get("disabled_reason"):
            try:
                return _account_key(acc)
            except Exception:
                continue
    return None


def _filter_xai_text_models(ids: list[str]) -> list[str]:
    out: list[str] = []
    for mid in ids:
        low = mid.lower()
        if low.startswith("grok-imagine") or "imagine-image" in low or "imagine-video" in low:
            continue
        out.append(mid)
    return out


def _merge_ids(ids: list[str], existing: list[str]) -> list[str]:
    merged = list(dict.fromkeys(ids))
    for mid in existing:
        if mid and mid not in merged:
            merged.append(mid)
    return merged


def _overview_text() -> str:
    lines = [
        "🧬 <b>默认模型</b>",
        "",
        "这里维护各 Provider 的普通模型 ID 字符串列表。",
        "仅当某个 OAuth 账户没有可用的实时/LKG 目录时，才作为该账户的无状态兜底；账户故障不会反向修改此列表。",
        "Cursor 仍按账号自动同步，不在这里改。",
        "",
    ]
    for fam in _FAMILIES:
        models = _read_list(fam)
        lines.append(f"{_fam_body_label(fam)}  {len(models)} 个默认模型")
    cursor_n = _cursor_account_count()
    lines.append(f"{ui.provider_tag('cursor')}  账号详情里看目录")
    if cursor_n:
        lines[-1] += f"（{cursor_n} 个账号）"
    return "\n".join(lines)


def _overview_kb() -> dict:
    return ui.inline_kb([
        [
            ui.provider_button("Claude", "odm:edit:anthropic", "claude"),
            ui.provider_button("OpenAI", "odm:edit:openai", "openai"),
            ui.provider_button("Grok", "odm:edit:xai", "xai"),
        ],
        [ui.provider_button("Antigravity", "odm:edit:antigravity", "antigravity")],
        [ui.btn("◀ 返回账户设置", "oa:settings")],
    ])


def show(chat_id: int, message_id: int, cb_id: str) -> None:
    abandon_edit(chat_id)
    ui.answer_cb(cb_id)
    ui.edit(chat_id, message_id, _overview_text(), reply_markup=_overview_kb())


def send_new(chat_id: int) -> None:
    ui.send(chat_id, _overview_text(), reply_markup=_overview_kb())


# ─── Level 2 发现 / 多选 / 手填 ──────────────────────────────────

def _manual_panel(chat_id: int, message_id, data: dict) -> None:
    family = data["family"]
    data["models_mode"] = "manual"
    data["models_source"] = "manual"
    states.set_state(chat_id, f"odm_edit:{family}", data)
    prefix = ""
    if data.get("discovery_error"):
        prefix = ("⚠️ <b>自动获取模型失败，已切换为手动输入</b>\n\n"
                  f"原因：{ui.escape_html(str(data['discovery_error']))}\n\n")
    elif data.get("manual_notice"):
        prefix = f"ℹ️ {ui.escape_html(str(data['manual_notice']))}\n\n"
    current = ", ".join(data.get("existing_models") or [])
    current_line = f"当前：<code>{ui.escape_html(current)}</code>\n\n" if current else "当前：<i>(空)</i>\n\n"
    text = (
        prefix + f"✏ <b>修改</b> {_fam_body_label(family)} <b>默认模型</b>\n\n"
        + current_line +
        "请输入新的模型列表，逗号/换行分隔。\n"
        "发送 <code>-</code> 或 <code>empty</code> 则清空。\n\n"
        "<i>若删除的模型仍被 API Key / 映射 / 入口默认引用，保存前会再确认。</i>"
    )
    rows = []
    if data.get("discovery_retry_available"):
        rows.append([ui.btn("🔄 重试自动获取", "odm:retry"),
                     ui.btn("◀ 返回目录", "odm:show")])
    elif data.get("discovered_models"):
        rows.append([ui.btn("◀ 返回模型选择", "odm:backsel"),
                     ui.btn("◀ 返回目录", "odm:show")])
    else:
        rows.append([ui.btn("◀ 返回目录", "odm:show")])
    kb = ui.inline_kb(rows)
    if message_id is None:
        ui.send(chat_id, text, reply_markup=kb)
    else:
        ui.edit(chat_id, message_id, text, reply_markup=kb)


def _enter_select(chat_id, message_id, data, ids, *, source, error=None, retry=False):
    existing = data.get("existing_models") or []
    existing_set = set(existing)
    merged = sorted(_merge_ids(ids, existing), key=str.casefold)
    # Stable second pass: enabled defaults first, disabled candidates last;
    # toggling a draft does not reshuffle indices until the editor is reopened.
    merged.sort(key=lambda model: model not in existing_set)
    data.update(
        discovered_models=merged,
        selected_models=[mid for mid in merged if mid in existing_set],
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
    states.set_state(chat_id, "odm_model_select", data)
    _render_models(chat_id, message_id, data)


def _model_kb(data):
    models = data["discovered_models"]
    selected = set(data.get("selected_models") or [])
    page, start, pages = _bounds(len(models), data.get("model_page", 0))
    data["model_page"] = page
    rows: list[list[dict]] = []
    number_row: list[dict] = []
    for i, _mid in enumerate(models[start:start + PAGE], start):
        number_row.append(ui.btn(str(i + 1), f"odm:t:{i}:{page}"))
        if len(number_row) == 6:
            rows.append(number_row)
            number_row = []
    if number_row:
        rows.append(number_row)
    if pages > 1:
        rows.append([
            ui.btn("⬅ 上一页" if page > 0 else "◁ 上一页", f"odm:p:{page-1}" if page > 0 else "odm:noop"),
            ui.btn(f"{page+1}/{pages}", "odm:noop"),
            ui.btn("➡ 下一页" if page + 1 < pages else "下一页 ▷", f"odm:p:{page+1}" if page + 1 < pages else "odm:noop"),
        ])
    rows += [[ui.btn("✅ 全选", "odm:all"), ui.btn("🔄 反选", "odm:inv")],
             [ui.btn(f"确认保存（{len(selected)}）", "odm:ok")]]
    if data.get("discovery_retry_available"):
        rows.append([ui.btn("✍️ 手动输入", "odm:manual"),
                     ui.btn("🔄 重试实时获取", "odm:retry")])
    else:
        rows.append([ui.btn("✍️ 手动输入", "odm:manual")])
    rows.append([ui.btn("◀ 返回目录", "odm:show")])
    return ui.inline_kb(rows)


def _render_models(chat_id, message_id, data):
    models = data["discovered_models"]
    count = len(models)
    if data.get("models_source") == "static":
        if data.get("discovery_error"):
            head = ("⚠️ <b>实时模型列表获取失败</b>\n\n"
                    f"原因：{ui.escape_html(str(data['discovery_error']))}\n\n"
                    f"当前显示 {count} 个内置参考模型，可能不是最新版本。")
        else:
            head = f"ℹ️ 当前显示 {count} 个内置参考模型，可能不是最新版本。"
    else:
        head = f"✅ 已从上游获取 {count} 个模型"
    page, start, pages = _bounds(count, data.get("model_page", 0))
    data["model_page"] = page
    selected = set(data.get("selected_models") or [])
    existing = set(data.get("existing_models") or [])
    lines = [
        head,
        "",
        f"✏ <b>修改</b> {_fam_body_label(data['family'])} <b>默认模型</b>",
        f"第 <b>{page+1}/{pages}</b> 页 · 每页最多 <b>{PAGE}</b> 项",
        "点击下方数字切换是否加入默认模型列表；翻页会保留草稿。",
        "",
    ]
    for i, mid in enumerate(models[start:start + PAGE], start):
        is_selected = mid in selected
        suffix = ""
        if mid not in existing:
            suffix = " · 新" if data.get("models_source") == "live" else " · 参考"
        lines.append(
            f"{i+1}. {'✅' if is_selected else '⬜'} "
            f"<code>{ui.escape_html(mid)}</code> - "
            f"{'已加入' if is_selected else '未加入'}{suffix}"
        )
    text = ui.truncate("\n".join(lines))
    if message_id is None:
        ui.send(chat_id, text, reply_markup=_model_kb(data))
    else:
        ui.edit(chat_id, message_id, text, reply_markup=_model_kb(data))


async def _discover_family_models(family: str):
    static = _static_models(family)
    error = None
    ids: list[str] = []
    source = "live"
    retry = _has_live_endpoint(family)
    try:
        if family == "xai":
            account_key = _first_enabled_account_key("xai")
            if not account_key:
                raise ModelsDiscoveryError("没有可用的 Grok 账户用于拉取模型")
            from ... import oauth_manager
            try:
                token = await oauth_manager.ensure_valid_token(account_key)
            except Exception:
                raise ModelsDiscoveryError("无法获取 Grok 访问令牌") from None
            ids = _filter_xai_text_models(await discover_models(_xai_models_url(), token))
            if not ids:
                raise ModelsDiscoveryError("上游未返回可用文本模型")
        elif static:
            ids = list(static)
            source = "static"
            retry = False
        else:
            raise ModelsDiscoveryError("该提供商未公开模型列表")
    except ModelsDiscoveryError as exc:
        error = str(exc)
        if static:
            ids = list(static)
            source = "static"
    return ids, source, error, retry


def _start_discovery(chat_id, message_id, data):
    generation = time.time_ns()
    data["discovery_generation"] = generation
    states.set_state(chat_id, "odm_discovery", data)

    async def run():
        ids, source, error, retry = await _discover_family_models(data["family"])
        cur = states.get_state(chat_id)
        if (not cur or cur.get("action") != "odm_discovery"
                or (cur.get("data") or {}).get("discovery_generation") != generation):
            return
        current = cur["data"]
        if ids:
            _enter_select(
                chat_id, message_id, current, ids, source=source,
                error=error if source == "static" else None,
                retry=bool(error and retry),
            )
        else:
            current["discovery_error"] = error or "上游未返回可用模型"
            current["discovery_retry_available"] = retry
            current.pop("manual_notice", None)
            _manual_panel(chat_id, message_id, current)

    _spawn_async_task(run, name=f"odm-models-{chat_id}")


def _start_edit(chat_id: int, message_id: int, cb_id: str, family: str) -> None:
    if family not in _FAMILIES:
        ui.answer_cb(cb_id, "未知家族")
        return
    ui.answer_cb(cb_id)
    existing = _read_list(family)
    data = {
        "family": family,
        "existing_models": existing,
        "selected_models": list(existing),
    }
    static = _static_models(family)
    if not _has_live_endpoint(family):
        if static:
            _enter_select(chat_id, message_id, data, static, source="static")
        else:
            data["manual_notice"] = "该提供商未公开模型列表，已直接进入手动输入。"
            _manual_panel(chat_id, message_id, data)
        return
    ui.edit(
        chat_id, message_id,
        "🔄 <b>正在发现模型…</b>\n\n请稍候，可随时取消。",
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "odm:show")]]),
    )
    _start_discovery(chat_id, message_id, data)


def _select_state(chat_id):
    st = states.get_state(chat_id)
    if not st or st.get("action") != "odm_model_select":
        return None
    return st.get("data") or {}


def _model_page(chat_id, message_id, cb_id, page):
    data = _select_state(chat_id)
    if data is None:
        ui.answer_cb(cb_id, "会话已过期")
        return
    page, _, _ = _bounds(len(data["discovered_models"]), page)
    data["model_page"] = page
    states.set_state(chat_id, "odm_model_select", data)
    ui.answer_cb(cb_id)
    _render_models(chat_id, message_id, data)


def _model_toggle(chat_id, message_id, cb_id, idx, page):
    data = _select_state(chat_id)
    if data is None:
        ui.answer_cb(cb_id, "会话已过期")
        return
    try:
        model = data["discovered_models"][idx]
    except IndexError:
        ui.answer_cb(cb_id, "模型快照已失效")
        return
    selected = data.setdefault("selected_models", [])
    selected.remove(model) if model in selected else selected.append(model)
    data["model_page"] = page
    states.set_state(chat_id, "odm_model_select", data)
    ui.answer_cb(cb_id)
    _render_models(chat_id, message_id, data)


def _model_bulk(chat_id, message_id, cb_id, invert):
    data = _select_state(chat_id)
    if data is None:
        ui.answer_cb(cb_id, "会话已过期")
        return
    selected = set(data.get("selected_models") or [])
    data["selected_models"] = (
        [m for m in data["discovered_models"] if m not in selected]
        if invert else list(data["discovered_models"])
    )
    states.set_state(chat_id, "odm_model_select", data)
    ui.answer_cb(cb_id)
    _render_models(chat_id, message_id, data)


def _model_confirm(chat_id, message_id, cb_id):
    data = _select_state(chat_id)
    if data is None:
        ui.answer_cb(cb_id, "会话已过期")
        return
    selected = set(data.get("selected_models") or [])
    new_models = [mid for mid in data["discovered_models"] if mid in selected]
    _apply_new_models(chat_id, data["family"], new_models, cb_id=cb_id)


def _model_manual(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    action = (state or {}).get("action") or ""
    if not state or (
        action not in ("odm_model_select", "odm_discovery")
        and not action.startswith("odm_edit:")
    ):
        ui.answer_cb(cb_id, "会话已过期")
        return
    ui.answer_cb(cb_id)
    _manual_panel(chat_id, message_id, state["data"])


def _model_back_select(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    if not state or not (state.get("data") or {}).get("discovered_models"):
        ui.answer_cb(cb_id, "没有可返回的选择页")
        return
    ui.answer_cb(cb_id)
    data = state["data"]
    states.set_state(chat_id, "odm_model_select", data)
    _render_models(chat_id, message_id, data)


def _discovery_retry(chat_id, message_id, cb_id):
    state = states.get_state(chat_id)
    action = (state or {}).get("action") or ""
    if not state or (
        action not in ("odm_model_select", "odm_discovery")
        and not action.startswith("odm_edit:")
    ):
        ui.answer_cb(cb_id, "当前不能重试", show_alert=True)
        return
    data = state["data"]
    if not data.get("discovery_retry_available") and action != "odm_discovery":
        ui.answer_cb(cb_id, "该提供商没有可重试的模型接口", show_alert=True)
        return
    ui.answer_cb(cb_id, "正在重试")
    ui.edit(chat_id, message_id, "🔄 <b>正在发现模型…</b>",
            reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "odm:show")]]))
    _start_discovery(chat_id, message_id, data)


def _apply_new_models(chat_id: int, family: str, new_models: list[str], *, cb_id: str | None = None) -> None:
    if len(new_models) > 200:
        if cb_id:
            ui.answer_cb(cb_id, "列表过长", show_alert=True)
        else:
            ui.send(chat_id, f"❌ 列表过长 ({len(new_models)} 项), 最多 200 个模型。请精简后重发:")
        return
    for m in new_models:
        if any(c in m for c in ("\\", " ", "\x00")):
            msg = (f"❌ 非法模型名: <code>{ui.escape_html(m)}</code>"
                   " (不能含空格 / 反斜杠 / 控制字符)。请重新输入:")
            if cb_id:
                ui.answer_cb(cb_id, "非法模型名", show_alert=True)
            else:
                ui.send(chat_id, msg)
            return

    old_models = _read_list(family)
    removed = set(old_models) - set(new_models)
    refs = _scan_references(family, removed) if removed else {}
    if not removed or not _has_any_refs(refs):
        _commit_save(family, new_models, removed, cleanup=False)
        states.pop_state(chat_id)
        if cb_id:
            ui.answer_cb(cb_id)
        _send_saved_result(chat_id, family, new_models, summary=None)
        return

    pending_code = ui.register_code(
        "odm:pending:" + json.dumps({
            "family": family,
            "new":    new_models,
            "removed": sorted(removed),
        }, ensure_ascii=False)
    )
    states.pop_state(chat_id)
    if cb_id:
        ui.answer_cb(cb_id)
    ui.send(chat_id, _render_confirm(family, new_models, removed, refs), reply_markup=ui.inline_kb([
        [ui.btn("✅ 继续保存 (保留引用)", f"odm:commit:{pending_code}:keep")],
        [ui.btn("🧹 保存并清理全部引用", f"odm:commit:{pending_code}:clean")],
        [ui.btn("❌ 取消", "odm:show")],
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
    _apply_new_models(chat_id, family, new_models)


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
                    f"  • {_ingress_body_label(row['ingress'])}:"
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
        extra_rows=[[ui.btn("◀ 返回模型目录", "odm:show")]],
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
    if action == "noop":
        ui.answer_cb(cb_id); return True
    if action == "all":
        _model_bulk(chat_id, message_id, cb_id, False); return True
    if action == "inv":
        _model_bulk(chat_id, message_id, cb_id, True); return True
    if action == "ok":
        _model_confirm(chat_id, message_id, cb_id); return True
    if action == "manual":
        _model_manual(chat_id, message_id, cb_id); return True
    if action == "retry":
        _discovery_retry(chat_id, message_id, cb_id); return True
    if action == "backsel":
        _model_back_select(chat_id, message_id, cb_id); return True
    if action == "p":
        _model_page(chat_id, message_id, cb_id, int(parts[2]) if len(parts) > 2 else 0); return True
    if action == "t":
        if len(parts) < 4:
            ui.answer_cb(cb_id, "非法 callback"); return True
        _model_toggle(
            chat_id, message_id, cb_id,
            int(parts[2]), int(parts[3]),
        ); return True
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
