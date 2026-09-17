"""Legacy Imagine menu safely redirects to independent image/video panels."""
from ...management_control.auxiliary import get_auxiliary_controls
from .. import states, ui
_CONTROL = get_auxiliary_controls().xai_media
_IMAGE_MODELS_STATE = 'xim_edit_image_models'
_VIDEO_MODELS_STATE = 'xim_edit_video_models'
_JOB_TTL_STATE = 'xim_edit_job_ttl'
_REQUEST_TIMEOUT_STATE = 'xim_edit_request_timeout'


def _render(chat_id=0):
    from . import model_center_menu
    return model_center_menu._video_settings_render(chat_id)


def show(chat_id, message_id, cb_id=None):
    from . import model_center_menu
    if ui.is_admin(chat_id):
        model_center_menu._show_rendered(chat_id, message_id, cb_id, lambda: _render(chat_id))


def send_new(chat_id):
    if not ui.is_admin(chat_id): return
    text, kb = _render(chat_id)
    ui.send(chat_id, text, reply_markup=kb)


def handle_callback(chat_id, message_id, cb_id, data):
    if data not in {'xim:show', 'xim:edit:image', 'xim:edit:video', 'xim:edit:ttl', 'xim:edit:timeout'}: return False
    from . import model_center_menu
    if ui.is_admin(chat_id):
        kind = 'image' if data == 'xim:edit:image' else 'video'
        renderer = model_center_menu._image_settings_render if kind == 'image' else model_center_menu._video_settings_render
        model_center_menu._show_rendered(chat_id, message_id, cb_id, lambda: renderer(chat_id))
    return True


def handle_text_state(chat_id, action, text):
    if action not in (_IMAGE_MODELS_STATE, _VIDEO_MODELS_STATE, _JOB_TTL_STATE, _REQUEST_TIMEOUT_STATE): return False
    states.pop_state(chat_id)
    if ui.is_admin(chat_id):
        ui.send_result(chat_id, '旧媒体编辑页已过期，请重新打开面板；模型名只由配置 / API 维护。',
            back_label='返回媒体面板', back_callback='mc:tab:image' if action == _IMAGE_MODELS_STATE else 'mc:tab:video')
    return True
