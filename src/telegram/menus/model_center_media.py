"""Image/video settings and precise owner-bound media model actions.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from ...management_control import ManagementError
from ...management_control.models import (
    ModelFilters,
    ModelKind,
    ModelOwnerRef,
    ModelSourceRef,
    ModelSourceType,
    ModelView,
)
from .. import ui
from . import model_center_menu as menu


def _fmt_bytes(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    amount = float(max(0, number))
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    return f"{int(amount)}B" if index == 0 else f"{amount:.1f}{units[index]}"


def _ag_settings_optional(chat_id: int):
    try:
        return menu._CONTROL.antigravity_media.get_settings(menu._ctx(chat_id))
    except ManagementError as exc:
        if menu._error_code(exc) == "SERVICE_NOT_READY":
            return None
        raise


def _image_settings_render(
    chat_id: int, back_callback: str = "mc:settings",
) -> tuple[str, dict]:
    image = menu._CONTROL.images.get_settings(menu._ctx(chat_id))
    xai = menu._CONTROL.xai_media.get_settings(menu._ctx(chat_id))
    ag = menu._ag_settings_optional(chat_id)
    ag_count = len(ag.image_models) if ag is not None else 0
    ag_text = str(ag_count) if ag is not None else "后端暂未就绪"
    image_callback = menu._freeze(
        chat_id, "settings_page", page="image", parent_callback=back_callback,
    )
    lines = [
        "🖼 <b>图片设置</b>", "",
        f"图片接口：<code>{'开' if image.enabled else '关'}</code>（GPT / Grok / Antigravity 共用）",
        f"媒体缓存：<code>{'开' if image.cache_enabled else '关'}</code>（GPT / Grok / Antigravity 图片 + Grok 视频）",
        f"缓存目录：<code>{ui.escape_html(image.cache_path)}</code>",
        f"保留天数：<code>{image.cache_retention_days}</code> 天（0=永久）",
        f"空间上限：<code>{menu._fmt_bytes(image.cache_max_bytes)}</code>（0=不限）", "",
        f"Grok 图片模型：<code>{len(xai.image_models)}</code> 个",
        f"Antigravity 全局图片模型：<code>{ui.escape_html(ag_text)}</code>", "",
        "已配置的 Grok 图片模型走 Grok；未配置的 grok-imagine-* 名称明确拒绝，不转 GPT。",
    ]
    rows = [
        [ui.btn(
            "关闭图片接口" if image.enabled else "开启图片接口",
            menu._freeze(chat_id, "image_patch", patch={"enabled": not image.enabled}, revision=image.revision, page_back=back_callback),
        ), ui.btn(
            "关闭媒体缓存" if image.cache_enabled else "开启媒体缓存",
            menu._freeze(chat_id, "image_patch", patch={"cacheEnabled": not image.cache_enabled}, revision=image.revision, page_back=back_callback),
        )],
        [ui.provider_button("GPT 图片管线", menu._freeze(
            chat_id, "settings_page", page="gpt_images", parent_callback=image_callback,
        ), "openai")],
        [ui.provider_button("Grok 图片模型", menu._freeze(
            chat_id, "media_manager", provider="xai", kind="image", page=1,
            back_callback=image_callback,
        ), "xai"), ui.provider_button("AG 图片模型", menu._freeze(
            chat_id, "media_manager", provider="antigravity", kind="image", page=1,
            back_callback=image_callback,
        ), "antigravity")],
        [ui.btn("缓存目录", menu._freeze(
            chat_id, "image_input", field="cachePath", revision=image.revision,
            page_back=image_callback,
        )), ui.btn("保留天数", menu._freeze(
            chat_id, "image_input", field="cacheRetentionDays", revision=image.revision,
            page_back=image_callback,
        ))],
        [ui.btn("空间上限", menu._freeze(
            chat_id, "image_input", field="cacheMaxBytes", revision=image.revision,
            page_back=image_callback,
        ))],
        [ui.btn("返回模型设置", back_callback)],
    ]
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _gpt_images_render(
    chat_id: int, back_callback: str = "mc:image_settings",
) -> tuple[str, dict]:
    settings = menu._CONTROL.images.get_settings(menu._ctx(chat_id))
    accounts = menu._CONTROL.images.list_accounts(menu._ctx(chat_id))
    gpt_callback = menu._freeze(
        chat_id, "settings_page", page="gpt_images", parent_callback=back_callback,
    )
    lines = [
        "🖼 <b>GPT / Codex 图片管线</b>", "",
        f"主模型：<code>{ui.escape_html(settings.main_model)}</code>",
        f"图片工具模型：<code>{ui.escape_html(settings.tool_model)}</code>", "",
        "参与账号（只影响 GPT 图片，不影响普通对话模型）：",
    ]
    rows = [[
        ui.btn("修改主模型", menu._freeze(
            chat_id, "image_input", field="mainModel", revision=settings.revision,
            page_back=gpt_callback,
        )),
        ui.btn("修改工具模型", menu._freeze(
            chat_id, "image_input", field="toolModel", revision=settings.revision,
            page_back=gpt_callback,
        )),
    ]]
    for account in accounts:
        state = "参与" if account.image_enabled else "排除"
        extra = ""
        if not account.oauth_enabled:
            extra = " · OAuth 已停用"
        elif account.missing_account_id:
            extra = " · 缺 account_id"
        elif account.image_cooldown_until:
            extra = " · 图片冷却中"
        lines.append(
            f"{'☑' if account.image_enabled else '☐'} <code>{ui.escape_html(account.email or account.account_id)}</code>"
            f" · {state}{extra}"
        )
        rows.append([ui.provider_button(
            f"{'排除' if account.image_enabled else '加入'} · {account.email or account.account_id}",
            menu._freeze(
                chat_id, "image_account", account_id=account.account_id,
                target=not account.image_enabled, revision=account.revision,
                page_back=back_callback,
            ),
            "openai",
        )])
    if not accounts:
        lines.append("（暂无 OpenAI OAuth 账号）")
    rows.append([ui.btn("返回图片设置", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _video_settings_render(
    chat_id: int, back_callback: str = "mc:settings",
) -> tuple[str, dict]:
    settings = menu._CONTROL.xai_media.get_settings(menu._ctx(chat_id))
    video_callback = menu._freeze(
        chat_id, "settings_page", page="video", parent_callback=back_callback,
    )
    lines = [
        "🎬 <b>视频设置</b>", "",
        f"Grok 视频模型：<code>{len(settings.video_models)}</code> 个",
        f"任务与账号关联时长：<code>{settings.job_ttl_seconds}s</code>",
        f"媒体请求超时：<code>{settings.request_timeout_seconds}s</code>", "",
        "媒体缓存使用图片设置中的共享缓存开关、目录、保留时间与空间上限。",
    ]
    rows = [
        [ui.provider_button("Grok 视频模型", menu._freeze(
            chat_id, "media_manager", provider="xai", kind="video", page=1,
            back_callback=video_callback,
        ), "xai")],
        [ui.btn("任务关联时长", menu._freeze(
            chat_id, "xai_duration_input", field="jobTtlSeconds",
            revision=settings.revision, page_back=video_callback,
        )), ui.btn("请求超时", menu._freeze(
            chat_id, "xai_duration_input", field="requestTimeoutSeconds",
            revision=settings.revision, page_back=video_callback,
        ))],
        [ui.btn("返回模型设置", back_callback)],
    ]
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _media_values(chat_id: int, provider: str, kind: str):
    if provider == "xai":
        settings = menu._CONTROL.xai_media.get_settings(menu._ctx(chat_id))
        models = settings.image_models if kind == "image" else settings.video_models
        return tuple(models), settings.revision, ()
    if provider == "antigravity" and kind == "image":
        settings = menu._CONTROL.antigravity_media.get_settings(menu._ctx(chat_id))
        return tuple(settings.image_models), settings.revision, tuple(settings.account_overrides)
    raise ValueError("unsupported media scope")


def _media_limits(provider: str) -> tuple[int, int]:
    return (50, 128) if provider == "xai" else (80, 80)


def _media_manager_render(
    chat_id: int, provider: str, kind: str, page_no: int,
    *, back_callback: str | None = None,
) -> tuple[str, dict]:
    models, revision, account_overrides = menu._media_values(chat_id, provider, kind)
    page_count = max(1, math.ceil(len(models) / menu._PAGE_SIZE))
    page_no = min(max(1, page_no), page_count)
    start = (page_no - 1) * menu._PAGE_SIZE
    visible = models[start:start + menu._PAGE_SIZE]
    provider_label = "Grok" if provider == "xai" else "Antigravity"
    kind_label = "图片" if kind == "image" else "视频"
    if back_callback is None:
        back_callback = "mc:image_settings" if kind == "image" else "mc:video_settings"
    manager_callback = menu._freeze(
        chat_id, "media_manager", provider=provider, kind=kind,
        page=page_no, back_callback=back_callback,
    )
    lines = [
        f"{'🖼' if kind == 'image' else '🎬'} <b>{provider_label} {kind_label}模型 · {len(models)} 个</b>", "",
    ]
    rows: list[list[dict]] = []
    buttons: list[dict] = []
    for offset, model_id in enumerate(visible):
        index = start + offset + 1
        lines.append(f"{index}. <code>{ui.escape_html(model_id)}</code>")
        buttons.append(ui.provider_button(
            str(index), menu._freeze(
                chat_id, "media_detail", provider=provider, kind=kind,
                model_id=model_id, revision=revision, page=page_no,
                owner=ModelOwnerRef(ModelSourceType.GLOBAL), readonly=False,
                back_callback=manager_callback,
            ), provider,
        ))
        if len(buttons) == 4:
            rows.append(buttons)
            buttons = []
    if buttons:
        rows.append(buttons)
    if not models:
        lines.append("（空；可使用添加或批量编辑恢复）")
    if provider == "antigravity" and account_overrides:
        lines.extend(["", "<b>账户专属（只读）</b>"])
        account_labels = {
            item.ref.id: item.label for item in menu._source_options(chat_id)
            if item.ref.type is ModelSourceType.OAUTH
        }
        for account_id, values in account_overrides:
            account_label = account_labels.get(account_id, "已移除的账户")
            lines.append(f"• <code>{ui.escape_html(account_label)}</code>：{len(values)} 个")
            for model_id in values:
                lines.append(f"  <code>{ui.escape_html(model_id)}</code>")
    if page_count > 1:
        rows.append([
            ui.btn("◀ 上一页", menu._freeze(
                chat_id, "media_manager", provider=provider, kind=kind,
                page=max(1, page_no - 1), back_callback=back_callback,
            )),
            ui.btn(f"{page_no}/{page_count}", "mc:noop"),
            ui.btn("下一页 ▶", menu._freeze(
                chat_id, "media_manager", provider=provider, kind=kind,
                page=min(page_count, page_no + 1), back_callback=back_callback,
            )),
        ])
    rows.append([
        ui.btn("添加模型", menu._freeze(
            chat_id, "media_input", mode="add", provider=provider,
            kind=kind, revision=revision, page=page_no,
            manager_back=manager_callback,
        )),
        ui.btn("批量编辑", menu._freeze(
            chat_id, "media_input", mode="bulk", provider=provider,
            kind=kind, revision=revision, page=page_no,
            manager_back=manager_callback,
        )),
    ])
    rows.append([ui.btn(f"返回{kind_label}设置", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _media_view_detail_render(
    chat_id: int, view: ModelView, *, back_callback: str,
) -> tuple[str, dict]:
    """Render a list media row as its actionable detail, without a jump page."""
    provider = str(view.identity.provider or "")
    kind = menu._enum_value(view.identity.kind)
    owner = view.identity.owner or ModelOwnerRef(ModelSourceType.GLOBAL)
    if provider == "openai" and kind == "image":
        settings = menu._CONTROL.images.get_settings(menu._ctx(chat_id))
        if view.model_id != settings.main_model:
            return (
                "图片模型已变化，请返回列表刷新。",
                ui.inline_kb([[ui.btn("返回模型列表", back_callback)]]),
            )
        self_callback = menu._freeze(
            chat_id, "gpt_media_detail", model_id=view.model_id,
            back_callback=back_callback,
        )
        text = "\n".join([
            f"{ui.provider_tag('openai')} · <b>图片模型</b>", "",
            f"模型：<code>{ui.escape_html(view.model_id)}</code>",
            "归属：<code>全局</code>",
            "用途：<code>GPT / Codex 图片主模型</code>",
        ])
        rows = [[ui.btn("修改当前名称", menu._freeze(
            chat_id, "image_input", field="mainModel", revision=settings.revision,
            detail_back=back_callback, detail_callback=self_callback,
        ))], [ui.btn("返回模型列表", back_callback)]]
        return menu._paged(chat_id, text, ui.inline_kb(rows))
    if provider in {"xai", "antigravity"}:
        return menu._media_detail_render(
            chat_id, provider, kind, view.model_id, view.revision, 1,
            owner, not view.editable, back_callback=back_callback,
        )
    return (
        "该媒体模型的设置入口暂不可用。",
        ui.inline_kb([[ui.btn("返回模型列表", back_callback)]]),
    )


def _media_detail_render(
    chat_id: int, provider: str, kind: str, model_id: str,
    revision: str, page_no: int, owner: ModelOwnerRef, readonly: bool,
    *, back_callback: str | None = None,
) -> tuple[str, dict]:
    del revision
    models, current_revision, account_overrides = menu._media_values(chat_id, provider, kind)
    if owner.type is ModelSourceType.GLOBAL:
        exists = model_id in models
    else:
        exists = any(
            account_id == owner.id and model_id in values
            for account_id, values in account_overrides
        )
    if back_callback is None:
        back_callback = menu._freeze(
            chat_id, "media_manager", provider=provider, kind=kind, page=page_no,
        )
    if not exists:
        return (
            "媒体模型已变化，请返回直接父页刷新。",
            ui.inline_kb([[ui.btn("返回", back_callback)]]),
        )
    revision = current_revision
    readonly = owner.type is not ModelSourceType.GLOBAL or readonly
    provider_label = "Grok" if provider == "xai" else "Antigravity"
    kind_label = "图片" if kind == "image" else "视频"
    owner_text = (
        "全局" if owner.type is ModelSourceType.GLOBAL
        else f"账户专属 · {menu._source_label(chat_id, ModelSourceRef(owner.type, owner.id))}"
    )
    lines = [
        f"{'🖼' if kind == 'image' else '🎬'} <b>{provider_label} {kind_label}模型</b>", "",
        f"模型：<code>{ui.escape_html(model_id)}</code>",
        f"归属：<code>{ui.escape_html(owner_text)}</code>",
    ]
    rows: list[list[dict]] = []
    if readonly:
        lines.extend(["", "账户专属 Antigravity 图片模型只读；全局设置不会覆盖此项。"])
    else:
        rows.append([
            ui.btn("修改当前名称", menu._freeze(
                chat_id, "media_input", mode="rename", provider=provider,
                kind=kind, model_id=model_id, revision=revision,
                owner=owner, page=page_no, detail_back=back_callback,
            )),
            ui.btn("移除当前模型", menu._freeze(
                chat_id, "media_remove_ask", provider=provider,
                kind=kind, model_id=model_id, revision=revision,
                owner=owner, page=page_no, detail_back=back_callback,
            )),
        ])
    rows.append([ui.btn("返回", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _media_method(control: Any, name: str):
    method = getattr(control, name, None)
    if method is None:
        raise ManagementError("SERVICE_NOT_READY")
    return method


def _media_owner(provider: str) -> ModelOwnerRef:
    del provider
    return ModelOwnerRef(ModelSourceType.GLOBAL)


def _parse_media_model(text: str, provider: str) -> str:
    value = str(text or "").strip()
    _count_limit, length_limit = menu._media_limits(provider)
    if not value or "\n" in value or "\r" in value or len(value) > length_limit:
        raise ValueError(f"模型名需为1—{length_limit}字符的单行文本。")
    return value


def _parse_media_models(text: str, provider: str) -> tuple[str, ...]:
    import re
    raw = str(text or "").strip()
    if raw.lower() in {"-", "clear", "none", "清空", "无"}:
        return ()
    count_limit, length_limit = menu._media_limits(provider)
    values: list[str] = []
    for part in re.split(r"[\s,，;；]+", raw):
        value = part.strip()
        if not value:
            continue
        if len(value) > length_limit:
            raise ValueError(f"单个模型名不能超过{length_limit}字符。")
        if value not in values:
            values.append(value)
    if not values:
        raise ValueError("模型列表不能为空；如需清空请发送 -。")
    if len(values) > count_limit:
        raise ValueError(f"模型数量不能超过{count_limit}个。")
    return tuple(values)


def _parse_duration_input(text: str, *, allow_days: bool) -> int:
    import re
    matched = re.fullmatch(r"(\d+)\s*([smhd]?)", str(text or "").strip().lower())
    if matched is None:
        raise ValueError("请输入正整数，并可附加 s/m/h" + ("/d" if allow_days else "") + " 单位。")
    value = int(matched.group(1))
    unit = matched.group(2)
    if value <= 0 or (unit == "d" and not allow_days):
        raise ValueError("请求超时仅支持正整数及 s/m/h。" if not allow_days else "时长必须大于0。")
    seconds = value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > 2_147_483_647:
        raise ValueError("时长过大。")
    return seconds


def _parse_bytes_input(text: str) -> int:
    raw = str(text or "").strip().upper().replace(" ", "")
    if raw in {"", "0"}:
        return 0
    for suffix, multiplier in (
        ("GB", 1024**3), ("G", 1024**3), ("MB", 1024**2),
        ("M", 1024**2), ("KB", 1024), ("K", 1024), ("B", 1),
    ):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)]
            break
    else:
        multiplier = 1
    try:
        value = Decimal(raw) * multiplier
    except InvalidOperation as exc:
        raise ValueError("请输入如 1GB / 500MB / 0。") from exc
    if value != value.to_integral_value() or value < 0 or value > 2**63 - 1:
        raise ValueError("缓存空间必须是0以上的整数大小。")
    return int(value)


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    s = menu._session(chat_id)
    if name == "image_patch":
        try:
            menu._CONTROL.images.update_settings(
                menu._ctx(chat_id), dict(data["patch"]),
                expected_revision=data.get("revision"),
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "图片设置已更新")
        text, kb = menu._image_settings_render(
            chat_id, str(data.get("page_back") or "mc:settings"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "image_account":
        try:
            menu._CONTROL.images.update_account(
                menu._ctx(chat_id), data["account_id"], enabled=bool(data["target"]),
                expected_revision=data.get("revision"),
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "GPT 图片参与账号已更新")
        text, kb = menu._gpt_images_render(
            chat_id, str(data.get("page_back") or "mc:image_settings"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "gpt_media_detail":
        settings = menu._CONTROL.images.get_settings(menu._ctx(chat_id))
        views = menu._CONTROL.list_models(
            menu._ctx(chat_id),
            filters=ModelFilters(kinds=(ModelKind.IMAGE,), text=settings.main_model),
            page=1, page_size=20,
        )
        view = next(
            (
                item for item in views.items
                if item.model_id == settings.main_model
                and item.identity.provider == "openai"
            ),
            None,
        )
        if view is None:
            ui.answer_cb(cb_id, "图片模型已变化，请返回列表刷新", show_alert=True)
            return True
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._media_view_detail_render(
            chat_id, view, back_callback=str(data["back_callback"]),
        ))
        return True


    if name == "image_input":
        field_name = str(data["field"])
        prompts = {
            "mainModel": "发送 GPT 图片主模型名称（1—128字符）。",
            "toolModel": "发送 image_generation 工具模型名称（1—128字符）。",
            "cachePath": "发送媒体缓存路径；相对路径位于 Parrot 数据目录内。",
            "cacheRetentionDays": "发送缓存保留天数，0 表示永久。",
            "cacheMaxBytes": "发送缓存空间上限，例如 1GB、500MB 或 0（不限）。",
        }
        back = str(data.get("detail_callback") or data.get("page_back") or (
            "mc:gpt_images" if field_name in {"mainModel", "toolModel"} else "mc:image_settings"
        ))
        menu._prompt(
            chat_id, "mc_image_field", dict(data), prompts[field_name], back,
        )
        ui.answer_cb(cb_id)
        return True


    if name == "xai_duration_input":
        field_name = str(data["field"])
        prompt = (
            "发送视频任务与账号的关联时长，支持 s/m/h/d，例如 180m、3h。"
            if field_name == "jobTtlSeconds" else
            "发送媒体请求超时，支持 s/m/h（不支持 d），例如 180、3m。"
        )
        menu._prompt(
            chat_id, "mc_xai_duration", dict(data), prompt,
            str(data.get("page_back") or "mc:video_settings"),
        )
        ui.answer_cb(cb_id)
        return True


    if name == "media_manager":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._media_manager_render(
            chat_id, str(data["provider"]), str(data["kind"]), int(data.get("page") or 1),
            back_callback=data.get("back_callback"),
        ))
        return True


    if name == "media_detail":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._media_detail_render(
            chat_id, str(data["provider"]), str(data["kind"]), str(data["model_id"]),
            str(data["revision"]), int(data.get("page") or 1), data["owner"],
            bool(data.get("readonly")), back_callback=data.get("back_callback"),
        ))
        return True


    if name == "media_input":
        provider = str(data["provider"])
        kind = str(data["kind"])
        mode = str(data["mode"])
        count_limit, length_limit = menu._media_limits(provider)
        if mode == "bulk":
            prompt = (
                f"发送完整模型列表（逗号/换行分隔），最多{count_limit}项、每项最多{length_limit}字符；"
                "发送 - 清空。"
            )
        else:
            prompt = f"发送模型名称（1—{length_limit}字符）。"
        if mode == "rename":
            cancel = menu._freeze(
                chat_id, "media_detail", provider=provider, kind=kind,
                model_id=data["model_id"], revision=data["revision"],
                page=int(data.get("page") or 1), owner=data["owner"],
                readonly=False, back_callback=data.get("detail_back"),
            )
        else:
            cancel = str(data.get("manager_back") or menu._freeze(
                chat_id, "media_manager", provider=provider, kind=kind,
                page=int(data.get("page") or 1),
            ))
        menu._prompt(chat_id, "mc_media_edit", dict(data), prompt, cancel)
        ui.answer_cb(cb_id)
        return True


    if name == "media_remove_ask":
        provider = str(data["provider"])
        note = "\n账户专属列表保持不变。" if provider == "antigravity" else ""
        text = (
            f"移除当前模型 <code>{ui.escape_html(data['model_id'])}</code>？\n"
            f"只移除这一项，其他模型及顺序保持。{note}"
        )
        confirm = menu._freeze(chat_id, "media_remove", **dict(data))
        back = menu._freeze(
            chat_id, "media_detail", provider=provider, kind=data["kind"],
            model_id=data["model_id"], revision=data["revision"],
            page=int(data.get("page") or 1), owner=data["owner"], readonly=False,
            back_callback=data.get("detail_back"),
        )
        ui.answer_cb(cb_id)
        ui.edit(chat_id, message_id, text, reply_markup=ui.inline_kb([[
            ui.btn("确认移除", confirm), ui.btn("取消", back),
        ]]))
        return True


    if name == "media_remove":
        provider = str(data["provider"])
        kind = str(data["kind"])
        try:
            if provider == "xai":
                method = menu._media_method(menu._CONTROL.xai_media, "remove_model")
                method(
                    menu._ctx(chat_id), kind=ModelKind(kind), model_id=data["model_id"],
                    expected_revision=data.get("revision"),
                )
            else:
                method = menu._media_method(menu._CONTROL.antigravity_media, "remove_model")
                method(
                    menu._ctx(chat_id), owner=data["owner"], model_id=data["model_id"],
                    expected_revision=data.get("revision"),
                )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "已移除当前模型")
        parent = str(data.get("detail_back") or menu._freeze(
            chat_id, "media_manager", provider=provider, kind=kind,
            page=int(data.get("page") or 1),
        ))
        frozen_parent = menu._thaw(chat_id, parent.split(":", 2)[2]) if parent.startswith("mc:a:") else None
        if frozen_parent is not None and frozen_parent.name == "media_manager":
            text, kb = menu._media_manager_render(
                chat_id, provider, kind, int(data.get("page") or 1),
                back_callback=frozen_parent.data.get("back_callback"),
            )
        else:
            context = frozen_parent.data.get("context") if frozen_parent and frozen_parent.name == "restore_list" else None
            if isinstance(context, menu._ListContext):
                menu._restore_list_context(s, context)
            text, kb = menu.render(chat_id)
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    return False
