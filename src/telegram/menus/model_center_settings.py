"""Common settings navigation, compression selection and operation views.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

import math
from typing import Mapping

from ...management_control import ManagementError
from ...management_control.models import ModelFilters, ModelKind
from .. import ui
from . import model_center_menu as menu


def _settings_render(
    chat_id: int, back_callback: str = "mc:list",
) -> tuple[str, dict]:
    current, _revision = menu._CONTROL.mapping.get_compression(menu._ctx(chat_id))
    lines = [
        "⚙️ <b>模型设置</b>", "",
        "适用于全部账号 / 渠道。",
        f"压缩模型：<code>{ui.escape_html(current or '未设置')}</code>", "",
        "下游请求必须传 model；缺少模型名称返回 400，不补默认值。",
    ]
    settings_callback = menu._freeze(
        chat_id, "settings", back_callback=back_callback,
    )
    rows = [
        [ui.btn("压缩模型", menu._freeze(
            chat_id, "settings_page", page="compression", parent_callback=settings_callback,
        )), ui.btn("OAuth 备用模型", "odm:show")],
        [ui.btn("同步元数据", menu._freeze(
            chat_id, "settings_page", page="metadata_sync", parent_callback=settings_callback,
        ))],
        [ui.btn("图片设置", menu._freeze(
            chat_id, "settings_page", page="image", parent_callback=settings_callback,
        )), ui.btn("视频设置", menu._freeze(
            chat_id, "settings_page", page="video", parent_callback=settings_callback,
        ))],
        [ui.btn("返回模型列表", back_callback)],
    ]
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _compression_render(
    chat_id: int, back_callback: str = "mc:settings",
) -> tuple[str, dict]:
    ctx = menu._ctx(chat_id)
    current, revision = menu._CONTROL.mapping.get_compression(ctx)
    metadata_text = "未匹配"
    trigger_text = "按实际来源的上下文容量"
    detail_key = None
    if current:
        page = menu._CONTROL.list_models(
            ctx, filters=ModelFilters(kinds=(ModelKind.CHAT,), text=current),
            page=1, page_size=20,
        )
        view = next((item for item in page.items if item.model_id == current), None)
        if view is not None:
            detail_key = view.resource_key
            metadata_text = str(view.common_metadata.get("target") or "按模型与来源匹配")
            trigger = menu._display_value(view.common_metadata, "compactTriggerTokens")
            if trigger is not None:
                trigger_text = menu._fmt_meta(trigger, menu._META_BY_KEY["compactTriggerTokens"])
    lines = [
        "🗜 <b>压缩模型</b>", "",
        f"当前：<code>{ui.escape_html(current or '未设置')}</code>",
        f"元数据匹配：<code>{ui.escape_html(metadata_text)}</code>",
        f"压缩阈值：<code>{ui.escape_html(trigger_text)}</code>", "",
        "仅供内部上下文压缩使用，不代替下游必填的 model。",
    ]
    compression_callback = menu._freeze(
        chat_id, "settings_page", page="compression",
        parent_callback=back_callback,
    )
    rows = [[
        ui.btn("选择模型", menu._freeze(
            chat_id, "compression_picker", page=1, revision=revision,
            back_callback=compression_callback, page_back=back_callback,
        )),
        ui.btn("清除压缩指定", menu._freeze(
            chat_id, "compression_clear", revision=revision,
            page_back=back_callback,
        )),
    ]]
    if detail_key:
        rows.append([ui.btn("查看模型", menu._freeze(
            chat_id, "compression_detail", resource_key=detail_key,
            back_callback=compression_callback,
        ))])
    rows.append([ui.btn("返回模型设置", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _compression_picker_render(
    chat_id: int, page_no: int, revision: str,
    back_callback: str = "mc:compression", page_back: str = "mc:settings",
) -> tuple[str, dict]:
    ctx = menu._ctx(chat_id)
    page = menu._CONTROL.list_models(
        ctx, filters=ModelFilters(kinds=(ModelKind.CHAT,)),
        page=max(1, page_no), page_size=menu._PAGE_SIZE,
    )
    pages = max(1, math.ceil(page.total / menu._PAGE_SIZE))
    page_no = min(max(1, page_no), pages)
    if page_no != page.page:
        page = menu._CONTROL.list_models(
            ctx, filters=ModelFilters(kinds=(ModelKind.CHAT,)),
            page=page_no, page_size=menu._PAGE_SIZE,
        )
    current, _current_revision = menu._CONTROL.mapping.get_compression(ctx)
    start = (page_no - 1) * menu._PAGE_SIZE
    lines = [f"🗜 <b>选择压缩模型 · 第 {page_no}/{pages} 页</b>", ""]
    rows: list[list[dict]] = []
    buttons: list[dict] = []
    for offset, view in enumerate(page.items):
        index = start + offset + 1
        selected = current == view.model_id
        lines.append(f"{index}. {'✓ ' if selected else ''}<code>{ui.escape_html(view.model_id)}</code>")
        buttons.append(ui.provider_button(
            str(index),
            menu._freeze(
                chat_id, "compression_save", model_id=view.model_id,
                revision=revision, back_callback=back_callback,
                page_back=page_back,
            ),
            menu._provider_for_view(view, None),
        ))
        if len(buttons) == 4:
            rows.append(buttons)
            buttons = []
    if buttons:
        rows.append(buttons)
    pager = menu._page_row(chat_id, page_no, pages)
    if pager:
        rows.append([
            ui.btn(pager[0]["text"], menu._freeze(
                chat_id, "compression_picker", page=max(1, page_no - 1),
                revision=revision, back_callback=back_callback,
                page_back=page_back,
            ) if page_no > 1 else "mc:noop"),
            ui.btn(f"{page_no}/{pages}", "mc:noop"),
            ui.btn(pager[2]["text"], menu._freeze(
                chat_id, "compression_picker", page=min(pages, page_no + 1),
                revision=revision, back_callback=back_callback,
                page_back=page_back,
            ) if page_no < pages else "mc:noop"),
        ])
    rows.append([ui.btn("返回压缩模型", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _operation_render(chat_id: int, operation_id: str, back_callback: str) -> tuple[str, dict]:
    operation = menu._CONTROL.operations.get(menu._ctx(chat_id), operation_id)
    status = menu._enum_value(operation.status)
    labels = {
        "queued": "排队中", "running": "处理中", "succeeded": "已完成",
        "failed": "失败", "cancelled": "已取消",
    }
    lines = [
        "🔄 <b>模型任务</b>", "",
        f"任务：<code>{ui.escape_html(operation.id)}</code>",
        f"状态：<code>{ui.escape_html(labels.get(status, status))}</code>",
    ]
    progress = getattr(operation, "progress", None)
    if progress is not None:
        lines.append(
            f"进度：<code>{progress.current}/{progress.total}</code> · "
            f"{ui.escape_html(progress.message_code)}"
        )
    result = getattr(operation, "result", None)
    records = []
    if isinstance(result, Mapping):
        candidate = result.get("items") or result.get("targets") or result.get("results")
        if isinstance(candidate, (tuple, list)):
            records = list(candidate)
        elif all(isinstance(value, int) for value in result.values()):
            lines.append("结果：" + " · ".join(
                f"{ui.escape_html(key)} {value}" for key, value in result.items()
            ))
    elif isinstance(result, (tuple, list)):
        records = list(result)
    if records:
        counts: dict[str, int] = {}
        for item in records:
            item_status = (
                str(item.get("status") or "unknown") if isinstance(item, Mapping)
                else str(getattr(item, "status", "unknown"))
            )
            counts[item_status] = counts.get(item_status, 0) + 1
        lines.append("结果：" + " · ".join(
            f"{ui.escape_html(key)} {value}" for key, value in sorted(counts.items())
        ))
    error = getattr(operation, "error", None)
    if error is not None:
        lines.append(f"错误：<code>{ui.escape_html(menu._enum_value(error.code))}</code>")
    rows: list[list[dict]] = []
    if status in {"queued", "running"}:
        rows.append([ui.btn("刷新任务状态", menu._freeze(
            chat_id, "operation", operation_id=operation_id,
            back_callback=back_callback,
        ))])
    rows.append([ui.btn("返回", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def _operation_keyboard(chat_id: int, operation_id: str, back_callback: str) -> dict:
    return ui.inline_kb([
        [ui.btn("查看任务状态", menu._freeze(
            chat_id, "operation", operation_id=operation_id,
            back_callback=back_callback,
        ))],
        [ui.btn("返回", back_callback)],
    ])


def _metadata_sync_render(
    chat_id: int, back_callback: str = "mc:settings",
) -> tuple[str, dict]:
    page = menu._CONTROL.mapping.search_catalog(
        menu._ctx(chat_id), provider=None, query=None, sort="name", page=1, page_size=1,
    )
    metadata_callback = menu._freeze(
        chat_id, "settings_page", page="metadata_sync",
        parent_callback=back_callback,
    )
    lines = [
        "🧬 <b>元数据同步</b>", "",
        f"当前公共目录：<code>{page.total} 条</code>",
        f"目录版本：<code>{ui.escape_html(page.revision)}</code>", "",
        "同步全部会刷新公共目录并重新匹配模型；人工匹配、字段手工值及来源单独匹配保持。",
    ]
    rows = [
        [ui.btn("同步全部元数据", menu._freeze(
            chat_id, "sync_full", revision=page.revision,
            page_back=metadata_callback,
        ))],
        [ui.btn("返回模型设置", back_callback)],
    ]
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(rows))


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    s = menu._session(chat_id)
    if name == "settings":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._settings_render(
            chat_id, str(data.get("back_callback") or "mc:list"),
        ))
        return True


    if name == "settings_page":
        page = str(data.get("page") or "")
        parent = str(data.get("parent_callback") or "mc:settings")
        renderers = {
            "compression": lambda: menu._compression_render(chat_id, parent),
            "metadata_sync": lambda: menu._metadata_sync_render(chat_id, parent),
            "image": lambda: menu._image_settings_render(chat_id, parent),
            "video": lambda: menu._video_settings_render(chat_id, parent),
            "gpt_images": lambda: menu._gpt_images_render(chat_id, parent),
        }
        renderer = renderers.get(page)
        if renderer is None:
            ui.answer_cb(cb_id, "设置页已过期", show_alert=True)
            return True
        menu._show_rendered(chat_id, message_id, cb_id, renderer)
        return True


    if name == "operation":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._operation_render(
            chat_id, str(data["operation_id"]), str(data["back_callback"]),
        ))
        return True


    if name == "compression_picker":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._compression_picker_render(
            chat_id, int(data.get("page") or 1), str(data["revision"]),
            str(data.get("back_callback") or "mc:compression"),
            str(data.get("page_back") or "mc:settings"),
        ))
        return True


    if name == "compression_detail":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._detail_render(
            chat_id, data["resource_key"],
            back_callback=str(data.get("back_callback") or "mc:compression"),
        ))
        return True


    if name == "compression_save":
        try:
            menu._CONTROL.mapping.put_compression(
                menu._ctx(chat_id), data["model_id"],
                expected_revision=data.get("revision"),
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "已设置压缩模型")
        text, kb = menu._compression_render(
            chat_id, str(data.get("page_back") or "mc:settings"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "compression_clear":
        try:
            menu._CONTROL.mapping.delete_compression(
                menu._ctx(chat_id), expected_revision=data.get("revision"),
            )
        except ManagementError as exc:
            if menu._error_code(exc) != "RESOURCE_NOT_FOUND":
                menu._answer_error(cb_id, exc)
                return True
        ui.answer_cb(cb_id, "已清除压缩模型")
        text, kb = menu._compression_render(
            chat_id, str(data.get("page_back") or "mc:settings"),
        )
        ui.edit(chat_id, message_id, text, reply_markup=kb)
        return True


    if name == "sync_full":
        try:
            Mode = menu._mapping_symbol("MetadataSyncMode")
            operation = menu._CONTROL.mapping.start_metadata_sync(
                menu._ctx(chat_id), mode=Mode.FULL, targets=(), source=None,
                refresh_catalog=True, expected_revision=data.get("revision"),
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "元数据同步任务已开始")
        ui.edit(
            chat_id, message_id,
            f"🔄 <b>元数据同步已开始</b>\n\n任务：<code>{ui.escape_html(operation.id)}</code>\n人工匹配与字段手工值保持。",
            reply_markup=menu._operation_keyboard(
                chat_id, operation.id, str(data.get("page_back") or "mc:metadata_sync"),
            ),
        )
        return True


    return False
