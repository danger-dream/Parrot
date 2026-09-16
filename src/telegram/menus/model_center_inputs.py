"""Text input validation and submission using the facade-owned editor state.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

from decimal import InvalidOperation

from ...management_control import ManagementError
from ...management_control.models import ModelKind
from .. import states, ui
from . import model_center_menu as menu


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
        ui.send(chat_id, "页面已过期，请重新打开模型中心。", reply_markup=ui.inline_kb([[ui.btn("打开模型中心", "mc:show")]]))
        return True
    raw = str(text or "").strip()
    if raw.lower() in menu._CANCEL_WORDS:
        menu._clear_input(chat_id)
        if data.get("cancel_callback") == "mc:aliases":
            menu._alias_drafts.pop(chat_id, None)
        ui.send(chat_id, "已取消，未保存。", reply_markup=ui.inline_kb([[ui.btn("返回", data.get("cancel_callback") or "mc:list")]]))
        return True
    if action == "mc_query":
        s.text = "" if raw == "-" else raw
        s.page = 1
        states.pop_state(chat_id)
        menu.send_new(chat_id, origin=s.origin)
        return True
    if action == "mc_alias_query":
        s.alias_query = "" if raw == "-" else raw
        s.alias_page = 1
        s.tab = "alias"
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
        draft.alias = raw
        states.pop_state(chat_id)
        text_out, kb = menu._alias_draft_render(chat_id)
        ui.send(chat_id, text_out, reply_markup=kb)
        return True
    if action == "mc_image_field":
        field_name = str(data.get("field") or "")
        try:
            if field_name in {"mainModel", "toolModel"}:
                value = menu._parse_media_model(raw, "xai")
            elif field_name == "cachePath":
                if not raw or "\x00" in raw:
                    raise ValueError("缓存路径不能为空。")
                value = raw
            elif field_name == "cacheRetentionDays":
                value = int(raw)
                if value < 0 or value > 36500:
                    raise ValueError("保留天数需为0—36500的整数。")
            elif field_name == "cacheMaxBytes":
                value = menu._parse_bytes_input(raw)
            else:
                raise ValueError("图片设置字段已过期。")
            menu._CONTROL.images.update_settings(
                menu._ctx(chat_id), {field_name: value},
                expected_revision=data.get("revision"),
            )
        except (ValueError, InvalidOperation) as exc:
            ui.send(chat_id, "❌ " + ui.escape_html(str(exc)) + " 请重新输入：")
            return True
        except ManagementError as exc:
            if menu._error_code(exc) in {"RESOURCE_CONFLICT", "IDENTITY_CONFLICT", "STATE_CONFLICT", "VALIDATION_FAILED", "UNSUPPORTED_VALUE"}:
                ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)) + " 请修改后重试：")
                return True
            states.pop_state(chat_id)
            back = str(data.get("detail_callback") or "mc:image_settings")
            ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)), reply_markup=ui.inline_kb([[ui.btn("返回", back)]]))
            return True
        states.pop_state(chat_id)
        back = str(data.get("detail_callback") or data.get("page_back") or (
            "mc:gpt_images" if field_name in {"mainModel", "toolModel"} else "mc:image_settings"
        ))
        ui.send_result(chat_id, "✅ 图片设置已更新。", back_label="返回", back_callback=back)
        return True
    if action == "mc_xai_duration":
        field_name = str(data.get("field") or "")
        try:
            value = menu._parse_duration_input(raw, allow_days=field_name == "jobTtlSeconds")
            menu._CONTROL.xai_media.update_settings(
                menu._ctx(chat_id), {field_name: value},
                expected_revision=data.get("revision"),
            )
        except ValueError as exc:
            ui.send(chat_id, "❌ " + ui.escape_html(str(exc)) + " 请重新输入：")
            return True
        except ManagementError as exc:
            if menu._error_code(exc) in {"VALIDATION_FAILED", "UNSUPPORTED_VALUE"}:
                ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)) + " 请修改后重试：")
                return True
            states.pop_state(chat_id)
            back = str(data.get("page_back") or "mc:video_settings")
            ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)), reply_markup=ui.inline_kb([[ui.btn("返回视频设置", back)]]))
            return True
        states.pop_state(chat_id)
        back = str(data.get("page_back") or "mc:video_settings")
        ui.send_result(chat_id, "✅ 视频运行参数已更新。", back_label="返回视频设置", back_callback=back)
        return True
    if action == "mc_media_edit":
        provider = str(data.get("provider") or "")
        kind = str(data.get("kind") or "")
        mode = str(data.get("mode") or "")
        owner = data.get("owner") or menu._media_owner(provider)
        try:
            if mode == "bulk":
                values = menu._parse_media_models(raw, provider)
                if provider == "xai":
                    field_name = "imageModels" if kind == "image" else "videoModels"
                    menu._CONTROL.xai_media.update_settings(
                        menu._ctx(chat_id), {field_name: list(values)},
                        expected_revision=data.get("revision"),
                    )
                else:
                    menu._CONTROL.antigravity_media.update_settings(
                        menu._ctx(chat_id), image_models=values,
                        expected_revision=data.get("revision"),
                    )
            else:
                value = menu._parse_media_model(raw, provider)
                control = menu._CONTROL.xai_media if provider == "xai" else menu._CONTROL.antigravity_media
                if mode == "add":
                    method = menu._media_method(control, "add_model")
                    kwargs = {"model_id": value, "expected_revision": data.get("revision")}
                elif mode == "rename":
                    method = menu._media_method(control, "rename_model")
                    kwargs = {
                        "old_model_id": data["model_id"], "new_model_id": value,
                        "expected_revision": data.get("revision"),
                    }
                else:
                    raise ValueError("媒体编辑页已过期。")
                if provider == "xai":
                    kwargs["kind"] = ModelKind(kind)
                else:
                    kwargs["owner"] = owner
                method(menu._ctx(chat_id), **kwargs)
        except ValueError as exc:
            ui.send(chat_id, "❌ " + ui.escape_html(str(exc)) + " 请重新输入：")
            return True
        except ManagementError as exc:
            if menu._error_code(exc) in {"RESOURCE_CONFLICT", "IDENTITY_CONFLICT", "STATE_CONFLICT", "VALIDATION_FAILED", "UNSUPPORTED_VALUE"}:
                ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)) + " 请修改后重试：")
                return True
            states.pop_state(chat_id)
            back = str(data.get("cancel_callback") or data.get("manager_back") or "mc:list")
            ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)), reply_markup=ui.inline_kb([[
                ui.btn("返回", back),
            ]]))
            return True
        states.pop_state(chat_id)
        if mode == "rename":
            _models, current_revision, _overrides = menu._media_values(chat_id, provider, kind)
            back = menu._freeze(
                chat_id, "media_detail", provider=provider, kind=kind,
                model_id=value, revision=current_revision,
                page=int(data.get("page") or 1), owner=owner, readonly=False,
                back_callback=data.get("detail_back"),
            )
            back_label = "返回当前模型"
        else:
            back = str(data.get("manager_back") or menu._freeze(
                chat_id, "media_manager", provider=provider, kind=kind,
                page=int(data.get("page") or 1),
            ))
            back_label = "返回模型列表"
        ui.send_result(
            chat_id, "✅ 媒体模型已更新。", back_label=back_label,
            back_callback=back,
        )
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
            ui.send(chat_id, "❌ " + ui.escape_html(menu._error_text(exc)), reply_markup=ui.inline_kb([[ui.btn("返回管理模型", "mc:list")]]))
            return True
        states.pop_state(chat_id)
        ui.send_result(
            chat_id, f"✅ {item.label}已保存。", back_label="返回字段编辑",
            back_callback=menu._freeze(
                chat_id, "metadata_editor", resource_key=data["resource_key"],
                source=data.get("source"), group=data.get("group") or item.group,
                detail_back=data.get("detail_back"),
            ),
        )
        return True
    return False
