"""Text input validation and submission using the facade-owned editor state.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

from decimal import InvalidOperation

from ...management_control import ManagementError
from ...management_control.models import ModelKind
from .. import states, ui
from . import model_center_menu as menu
from .model_center_icons import inline_kb, label_with_icon


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if not action.startswith(menu._INPUT_PREFIX):
        return False
    if not menu._admin(chat_id):
        return True
    current = states.get_state(chat_id)
    data = dict((current or {}).get("data") or {})
    s = menu._session(chat_id)
    if not current or current.get("action") != action or data.get("generation") != s.generation:
        states.pop_state(chat_id)
        ui.send(chat_id, "页面已过期，请重新打开模型中心。", reply_markup=inline_kb([[ui.btn("打开模型中心", "mc:show")]]))
        return True
    raw = str(text or "").strip()
    if raw.lower() in menu._CANCEL_WORDS:
        menu._clear_input(chat_id)
        if data.get("cancel_callback") == "mc:aliases":
            menu._alias_drafts.pop(chat_id, None)
        ui.send(chat_id, "已取消，本次输入未应用。", reply_markup=inline_kb([[ui.btn("返回", data.get("cancel_callback") or "mc:list")]]))
        return True
    if action == "mc_query":
        s.text = "" if raw == "-" else raw
        s.page = 1
        states.pop_state(chat_id)
        menu.send_new(chat_id, origin=s.origin)
        return True
    if action == "mc_alias_name":
        draft = menu._alias_drafts.get(chat_id)
        if draft is None or draft.draft_id != data.get("draft_id"):
            states.pop_state(chat_id)
            ui.send(chat_id, "编辑页已过期。")
            return True
        if not raw or len(raw) > 300 or any(char.isspace() for char in raw):
            ui.send(chat_id, "❌ 别名需为1—300字符且不能含空白，请重新输入：")
            return True
        from .model_center_aliases import _check_new_alias, _commit_alias
        try:
            if draft.old_alias:
                _commit_alias(chat_id, draft, alias=raw)
            else:
                _check_new_alias(chat_id, raw)
                draft.alias = raw
        except ManagementError as exc:
            if menu._error_code(exc) in {"RESOURCE_CONFLICT", "VALIDATION_FAILED"}:
                ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)) + " 请重新输入：")
            else:
                states.pop_state(chat_id)
                ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)), reply_markup=inline_kb([[ui.btn("◀ 返回别名列表", "mc:aliases")]]))
            return True
        states.pop_state(chat_id)
        if draft.old_alias:
            text_out, kb = menu._alias_draft_render(chat_id)
        else:
            text_out, kb = menu._alias_target_render(chat_id, draft.draft_id, 1)
        ui.send(chat_id, text_out, reply_markup=kb)
        return True
    if action in {'mc_image_field', 'mc_xai_duration', 'mc_media_edit'}:
        states.pop_state(chat_id)
        ui.send_result(chat_id, '旧媒体编辑页已过期；模型名仅由配置 / 管理 API 维护。', back_label=label_with_icon('返回媒体面板'),
            back_callback='mc:tab:video' if data.get('kind') == 'video' or action == 'mc_xai_duration' else 'mc:tab:image')
        return True
    if action == 'mc_media_field':
        kind = data.get('kind', 'image')
        field = data.get('field')
        try:
            if field == 'cachePath':
                if not raw or '\x00' in raw: raise ValueError('缓存目录不能为空。')
                value = raw
            elif field == 'cacheRetentionDays':
                value = int(raw)
                if not 0 <= value <= 36500: raise ValueError('保留天数需为0—36500。')
            elif field == 'cacheMaxBytes': value = menu._parse_bytes_input(raw)
            elif field in ('requestTimeoutSeconds', 'jobTtlSeconds'):
                value = menu._parse_duration_input(raw, allow_days=field == 'jobTtlSeconds')
            else: raise ValueError('媒体设置字段已过期。')
            control = menu._CONTROL.images if kind == 'image' else menu._CONTROL.videos
            control.update_settings(menu._ctx(chat_id), {field: value}, expected_revision=data.get('revision'))
        except (ValueError, InvalidOperation) as exc:
            ui.send(chat_id, '❌ ' + ui.escape_html(str(exc)) + ' 请重新输入：')
            return True
        except ManagementError as exc:
            states.pop_state(chat_id)
            ui.send_result(chat_id, '❌ ' + ui.escape_html(menu._error_text(exc)), back_label=label_with_icon('返回媒体面板'),
                back_callback=data.get('page_back') or 'mc:list')
            return True
        states.pop_state(chat_id)
        ui.send_result(chat_id, '✅ 媒体设置已更新。', back_label=label_with_icon('返回媒体面板'), back_callback=data.get('page_back') or 'mc:list')
        return True
    if action == "mc_metadata_field":
        item = menu._META_BY_KEY.get(str(data.get("field") or ""))
        if item is None:
            states.pop_state(chat_id)
            ui.send(chat_id, "字段编辑页已过期。")
            return True
        try:
            value = menu._parse_field(raw, item)
            Patch = menu._mapping_symbol("MetadataOverridePatch")
            source = data.get("source")
            kwargs = menu._metadata_scope_kwargs(source, data.get("outbound_model"))
            view = menu._CONTROL.get_model(menu._ctx(chat_id), data["resource_key"])
            menu._CONTROL.mapping.patch_metadata_overrides(
                menu._ctx(chat_id),
                view.model_id,
                patch=Patch(set_fields={item.key: value}, unset_fields=()),
                expected_revision=data.get("revision"),
                **kwargs,
            )
        except ValueError as exc:
            ui.send(chat_id, "❌ " + ui.escape_html(str(exc)) + " 请重新输入：")
            return True
        except ManagementError as exc:
            if menu._error_code(exc) in {"RESOURCE_CONFLICT", "IDENTITY_CONFLICT", "STATE_CONFLICT", "VALIDATION_FAILED", "UNSUPPORTED_VALUE"}:
                ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)) + " 请修改后重试：")
                return True
            states.pop_state(chat_id)
            ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)), reply_markup=inline_kb([[ui.btn("返回管理模型", "mc:list")]]))
            return True
        states.pop_state(chat_id)
        ui.send_result(
            chat_id, f"✅ {item.label}已保存。", back_label=label_with_icon("返回字段编辑"),
            back_callback=menu._freeze(
                chat_id, "metadata_editor", resource_key=data["resource_key"],
                source=data.get("source"), group=data.get("group") or item.group,
                detail_back=data.get("detail_back"),
            ),
        )
        return True
    return False
