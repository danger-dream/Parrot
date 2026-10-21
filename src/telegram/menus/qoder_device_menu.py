"""Local device-draft views; Qoder control owns confirmation and identity binding."""
from __future__ import annotations

from ...management_control.oauth import OAuthProvider
from ...management_control.oauth.menu_bridge import telegram_context
from .. import states, ui
from . import oauth_add_session as add_session


def _menu():
    from . import qoder_oauth_menu
    return qoder_oauth_menu


def render(chat_id, message_id, flow, *, realm, method, relogin=None):
    menu = _menu()
    preview = flow.device_preview
    data = menu._set_state(chat_id, "oa_qd_device", {
        "flow_id": flow.flow_id, "flow_secret": flow.flow_secret, "revision": preview["revision"],
        "realm": realm, "method": method, "relogin": relogin})
    rows = []
    actions = [ui.btn("❌ 取消", "oa:qd:device_cancel:" + data["nonce"])]
    if preview["can_regenerate"]:
        platform_buttons = []
        for choice in preview.get("platform_choices", []):
            if not choice["supported"]:
                continue
            prefix = "✅ " if choice["id"] == preview.get("platform_id") else ""
            platform_buttons.append(ui.btn(prefix + choice["label"],
                "oa:qd:device_platform:" + data["nonce"] + ":" + choice["id"]))
        rows.extend(platform_buttons[i:i + 2] for i in range(0, len(platform_buttons), 2))
        actions.append(ui.btn("🔄 重新生成", "oa:qd:device_regenerate:" + data["nonce"]))
    actions.append(ui.btn("✅ 确认并继续", "oa:qd:device_confirm:" + data["nonce"]))
    rows.append(actions)
    ui.edit(chat_id, message_id, f"{ui.provider_custom_emoji_html('qoder')} <b>Qoder 设备档案草稿</b>\n"
        f"档案: <code>{preview['profile_id']}</code>\n平台: {ui.escape_html(preview.get('platform_label') or preview['platform'])}\n"
        f"版本: {preview['version']}\n🌐 代理: {ui.escape_html(str(preview['proxy']))}\n"
        "仅本地生成，尚未登录或领取。保存账户后长期绑定；重授权沿用。\n"
        + ("旧档案需修正辅助状态；确认并重授权成功后保存，机器ID和身份字段保持。\n" if preview.get("migration") else "")
        + ui.escape_html(flow.instruction), reply_markup=ui.inline_kb(rows))


@add_session.start("qoder")
def prepare(chat_id, message_id, *, realm="global", method="device", relogin=None):
    menu = _menu()
    menu.discard_state(chat_id)
    try:
        flow = menu.oauth_control.start_login_flow(telegram_context(chat_id), OAuthProvider.QODER, realm=realm,
            login_method=method, **({"relogin_account_id": relogin["target_key"]} if relogin else {}))
        render(chat_id, message_id, flow, realm=realm, method=method, relogin=relogin)
    except Exception as exc:
        ui.edit(chat_id, message_id, menu._error(exc))


@add_session.resume
def handle(chat_id, message_id, kind, nonce, platform=None):
    menu = _menu()
    data = menu._state(chat_id, nonce, "oa_qd_device")
    if not data:
        ui.send(chat_id, "按钮已过期，请使用最新的设备档案页面。")
        return
    if kind == "device_cancel":
        menu.discard_state(chat_id)
        menu.show_add(chat_id, message_id)
        return
    try:
        if kind in {"device_regenerate", "device_platform"}:
            flow = menu.oauth_control.regenerate_qoder_device(telegram_context(chat_id), data["flow_id"], data["flow_secret"],
                                                             data["revision"], platform=platform)
            render(chat_id, message_id, flow, realm=data["realm"], method=data["method"], relogin=data.get("relogin"))
            return
        flow = menu.oauth_control.confirm_qoder_device(telegram_context(chat_id), data["flow_id"], data["flow_secret"], data["revision"])
        if data["method"] == "pat":
            menu._set_state(chat_id, "oa_qd_pat", data)
            ui.edit(chat_id, message_id, f"{ui.provider_custom_emoji_html('qoder')} 设备档案已确认。请粘贴以 pt- 开头的 PAT（qoder.com/account/integrations）。",
                    reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "oa:add:qoder")]]))
        else:
            menu._login_started(chat_id, message_id, flow, realm=data["realm"], relogin=data.get("relogin"))
    except Exception as exc:
        ui.send(chat_id, menu._error(exc))


@add_session.resume
def pat_input(chat_id, text):
    menu = _menu()
    state = states.get_state(chat_id)
    if not state or state["action"] != "oa_qd_pat":
        return
    data = state["data"]
    try:
        flow = menu.oauth_control.confirm_qoder_device(telegram_context(chat_id), data["flow_id"], data["flow_secret"],
                                            data["revision"], pat=text.strip())
        message = ui.send(chat_id, "⌛ 正在保存授权…") or {}
        message_id = (message.get("result") or {}).get("message_id")
        if message_id:
            menu._login_started(chat_id, message_id, flow, realm="global", relogin=data.get("relogin"))
    except Exception as exc:
        ui.send(chat_id, menu._error(exc))
