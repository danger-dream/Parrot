"""Old image menu callbacks are read-only redirects; historical viewing remains."""
from ...management_control.auxiliary import get_auxiliary_controls
from ...management_control.auxiliary.common import telegram_context
from .. import states, ui
_CONTROL = get_auxiliary_controls().images
_ctx = telegram_context


def _render(chat_id=0):
    from . import model_center_menu
    return model_center_menu._image_settings_render(chat_id)


def show(chat_id, message_id, cb_id=None):
    from . import model_center_menu
    if not ui.is_admin(chat_id): return
    model_center_menu._show_rendered(chat_id, message_id, cb_id, lambda: _render(chat_id))


def send_new(chat_id):
    if not ui.is_admin(chat_id): return
    text, kb = _render(chat_id)
    ui.send(chat_id, text, reply_markup=kb)


def on_view_image(chat_id: int, message_id: int, cb_id: str, short: str) -> None:
    full = ui.resolve_code(short) or ""
    if not full.startswith("imglog:"):
        ui.answer_cb(cb_id, "日志按钮已过期")
        return
    try:
        log_id = int(full[len("imglog:"):])
    except Exception:
        ui.answer_cb(cb_id, "日志无效")
        return
    row = _CONTROL.cached_image_log(_ctx(chat_id), log_id)
    if not row:
        ui.answer_cb(cb_id, "日志不存在")
        return
    paths = list(row.paths)
    if not paths:
        ui.answer_cb(cb_id, "图片缓存不存在或已清理", show_alert=True)
        return
    ui.answer_cb(cb_id, "正在发送图片…")
    for p in paths[:5]:
        ui.send_photo(
            chat_id, p,
            caption=(
                f"🖼 图片日志 #{row.id} · {'生成' if row.action == 'generate' else '编辑'}\n"
                f"账号: <code>{ui.escape_html(row.account_email or '?')}</code>"
            ),
        )


def handle_callback(chat_id, message_id, cb_id, data):
    if data.startswith('img:view:'):
        if ui.is_admin(chat_id): on_view_image(chat_id, message_id, cb_id, data.split(':', 2)[2])
        return True
    if data in {'menu:images', 'img:show', 'img:toggle', 'img:cache_toggle', 'img:set_main', 'img:set_tool', 'img:set_path', 'img:set_retention', 'img:set_max', 'img:accounts'} or data.startswith('img:acc_toggle:'):
        show(chat_id, message_id, cb_id)
        return True
    return False


def handle_text_state(chat_id, action, text):
    if not action.startswith('img_set_'): return False
    states.pop_state(chat_id)
    if ui.is_admin(chat_id):
        ui.send_result(chat_id, '旧图片编辑页已过期，请在图片面板重新操作；模型名只由配置 / API 维护。',
            back_label='返回图片面板', back_callback='mc:tab:image')
    return True
