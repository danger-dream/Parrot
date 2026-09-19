"""Metadata synchronization, detail compression actions and operation views.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

from typing import Mapping

from ...management_control import ManagementError
from ...management_control.models import ModelKind
from .. import ui
from . import model_center_menu as menu
from .model_center_icons import inline_kb


def _operation_render(chat_id: int, operation_id: str, back_callback: str) -> tuple[str, dict]:
    operation = menu._CONTROL.operations.get(menu._ctx(chat_id), operation_id)
    status = menu._enum_value(operation.status)
    labels = {
        "queued": "排队中", "running": "处理中", "succeeded": "已完成",
        "failed": "失败", "cancelled": "已取消", "partial_failed": "部分失败",
    }
    result = getattr(operation, "result", None)
    upstream = getattr(operation, "kind", "") == "model_center.upstream.sync"
    display_status = str(result.get("status") or status) if upstream and isinstance(result, Mapping) else status
    lines = [
        "🔄 <b>模型任务</b>", "",
        f"任务：<code>{ui.escape_html(operation.id)}</code>",
        f"状态：<code>{ui.escape_html(labels.get(display_status, display_status))}</code>",
    ]
    progress = getattr(operation, "progress", None)
    if progress is not None:
        lines.append(
            f"进度：<code>{progress.current}/{progress.total}</code> · "
            f"{ui.escape_html(labels.get(display_status, display_status) if upstream else progress.message_code)}"
        )
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
            f"{ui.escape_html(labels.get(key, key))} {value}" for key, value in sorted(counts.items())
        ))
        if upstream:
            errors = {
                "UPSTREAM_ERROR": "上游同步失败，原目录保留",
                "UPSTREAM_TIMEOUT": "上游超时，原目录保留",
                "RESOURCE_NOT_FOUND": "来源已移除",
                "REVISION_CONFLICT": "来源已变化，请重新同步",
                "UNSUPPORTED_VALUE": "上游不支持实时模型目录",
                "DEPENDENCY_UNAVAILABLE": "依赖或运行时刷新异常，请检查当前状态",
                "ALIAS_CONFLICT": "部分新模型与手工别名冲突，未覆盖",
            }
            for item in records:
                if not isinstance(item, Mapping):
                    continue
                label = ui.escape_html(item.get("label") or "来源")
                item_status = str(item.get("status") or "unknown")
                detail = f"{int(item.get('count') or 0)} 个模型" if item_status == "succeeded" else errors.get(str(item.get("errorCode")), "同步未完成，请重试")
                icon = "✅" if item_status == "succeeded" else "⚠️"
                lines.append(f"{icon} {label} · {ui.escape_html(detail)}")
    if upstream and isinstance(result, Mapping) and isinstance(result.get("metadataSync"), Mapping):
        metadata = result["metadataSync"]
        labels = {
            "succeeded": "已更新并重新匹配",
            "partial_failed": "拉取失败，已使用本地目录匹配",
            "failed": "匹配失败，已同步的模型目录保留",
            "skipped": "自动更新已关闭，已跳过",
        }
        lines.append("🧬 元数据：" + labels.get(str(metadata.get("status")), "未完成"))
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
    return menu._paged(chat_id, "\n".join(lines), inline_kb(rows))


def _operation_keyboard(chat_id: int, operation_id: str, back_callback: str) -> dict:
    return inline_kb([
        [ui.btn("查看任务状态", menu._freeze(
            chat_id, "operation", operation_id=operation_id,
            back_callback=back_callback,
        ))],
        [ui.btn("返回", back_callback)],
    ])


def _metadata_sync_render(
    chat_id: int, back_callback: str = "mc:list",
) -> tuple[str, dict]:
    page = menu._CONTROL.mapping.search_catalog(
        menu._ctx(chat_id), provider=None, query=None, sort="name", page=1, page_size=1,
    )
    metadata_callback = menu._freeze(
        chat_id, "metadata_sync", back_callback=back_callback,
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
        [ui.btn("返回模型列表", back_callback)],
    ]
    return menu._paged(chat_id, "\n".join(lines), inline_kb(rows))


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    if name == "metadata_sync":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._metadata_sync_render(
            chat_id, str(data.get("back_callback") or "mc:list"),
        ))
        return True

    if name == "operation":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: menu._operation_render(
            chat_id, str(data["operation_id"]), str(data["back_callback"]),
        ))
        return True


    if name in {"compression_save", "compression_clear"}:
        # Old picker callbacks must not remain a hidden writable settings page.
        if not data.get("resource_key"):
            ui.answer_cb(cb_id, "请从模型详情设置压缩模型", show_alert=True)
            return True
        try:
            ctx = menu._ctx(chat_id)
            view = menu._CONTROL.get_model(ctx, data["resource_key"])
            if view.identity.kind is not ModelKind.CHAT:
                ui.answer_cb(cb_id, "仅对话模型可用于压缩", show_alert=True)
                return True
            if name == "compression_save":
                menu._CONTROL.mapping.put_compression(
                    ctx, view.model_id, expected_revision=data.get("revision"),
                )
            else:
                current, _ = menu._CONTROL.mapping.get_compression(ctx)
                if current != view.model_id:
                    ui.answer_cb(cb_id, "压缩模型已变化，请刷新详情", show_alert=True)
                    return True
                menu._CONTROL.mapping.delete_compression(
                    ctx, expected_revision=data.get("revision"),
                )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "已设置压缩模型" if name == "compression_save" else "已清除压缩模型")
        text, kb = menu._detail_render(
            chat_id, view.resource_key,
            back_callback=str(data.get("detail_back") or "mc:list"),
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
