"""Grok Imagine 图片 / 视频设置菜单。

callback_data 前缀：`xim:...`
"""

from __future__ import annotations

import re
from typing import Optional

from ... import config
from .. import states, ui


_IMAGE_MODELS_STATE = "xim_edit_image_models"
_VIDEO_MODELS_STATE = "xim_edit_video_models"
_JOB_TTL_STATE = "xim_edit_job_ttl"
_REQUEST_TIMEOUT_STATE = "xim_edit_request_timeout"

_CLEAR_VALUES = {"-", "clear", "none", "清空", "无"}
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd]?)$", re.IGNORECASE)


def _provider_cfg() -> dict:
    raw = config.get().get("xaiOAuth") or {}
    return raw if isinstance(raw, dict) else {}


def _models(key: str) -> list[str]:
    raw = _provider_cfg().get(key) or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        model = str(item or "").strip()
        if model and model not in out:
            out.append(model)
    return out


def _positive_int(key: str, default: int) -> int:
    try:
        value = int(_provider_cfg().get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _fmt_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds % 86400 == 0:
        return f"{seconds // 86400} 天"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} 小时"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _model_lines(models: list[str]) -> list[str]:
    if not models:
        return ["<i>（空；该类 Imagine 模型不可用）</i>"]
    return [f"• <code>{ui.escape_html(model)}</code>" for model in models]


def _render() -> tuple[str, dict]:
    image_models = _models("imageModels")
    video_models = _models("videoModels")
    job_ttl = _positive_int("videoJobTtlSeconds", 10800)
    request_timeout = _positive_int("mediaRequestTimeoutSeconds", 180)

    lines = [
        "🎨 <b>Grok Imagine 设置</b>",
        "",
        f"<b>🖼 图片模型</b> ({len(image_models)})",
        *_model_lines(image_models),
        "",
        f"<b>🎬 视频模型</b> ({len(video_models)})",
        *_model_lines(video_models),
        "",
        "<b>⏱ 运行参数</b>",
        f"视频任务绑定: <code>{_fmt_duration(job_ttl)}</code>（{job_ttl}s）",
        f"媒体请求超时: <code>{_fmt_duration(request_timeout)}</code>（{request_timeout}s）",
        "",
        "<b>模型路由</b>",
        "• 已配置的 <code>grok-imagine-image*</code> → xAI OAuth",
        "• 其他图片模型 → GPT / Codex",
        "• 未配置的 Grok Imagine 图片模型会明确拒绝，不会串到 GPT 管线",
        "",
        "<i>这里只管理 Imagine 配置；统一统计、费用和任务详情请到多媒体日志查看。API Key 的图片/视频权限仍在 Key 详情页单独开启。</i>",
    ]
    rows = [
        [
            ui.btn("🖼 编辑图片模型", "xim:edit:image"),
            ui.btn("🎬 编辑视频模型", "xim:edit:video"),
        ],
        [
            ui.btn("🕒 任务绑定时长", "xim:edit:ttl"),
            ui.btn("⏱ 请求超时", "xim:edit:timeout"),
        ],
        [ui.btn("🎞 查看多媒体日志", "media:logs")],
        [
            ui.btn("◀ OAuth 设置", "oa:settings"),
            ui.btn("🏠 主菜单", "menu:main"),
        ],
    ]
    return ui.truncate("\n".join(lines)), ui.inline_kb(rows)


def show(chat_id: int, message_id: int, cb_id: Optional[str] = None) -> None:
    # `xim:show` 同时承担文本输入页的取消按钮；回到设置页即结束旧状态。
    states.pop_state(chat_id)
    if cb_id is not None:
        ui.answer_cb(cb_id)
    text, kb = _render()
    ui.edit(chat_id, message_id, text, reply_markup=kb)


def send_new(chat_id: int) -> None:
    text, kb = _render()
    ui.send(chat_id, text, reply_markup=kb)


def _ask(
    chat_id: int,
    message_id: int,
    cb_id: str,
    *,
    action: str,
    text: str,
) -> None:
    ui.answer_cb(cb_id)
    states.set_state(chat_id, action)
    ui.edit(
        chat_id,
        message_id,
        text,
        reply_markup=ui.inline_kb([[ui.btn("❌ 取消", "xim:show")]]),
    )


def _mutate_xai(key: str, value) -> None:
    def _mutate(cfg: dict) -> None:
        section = cfg.get("xaiOAuth")
        if not isinstance(section, dict):
            section = {}
            cfg["xaiOAuth"] = section
        section[key] = value

    config.update(_mutate)


def on_edit_image_models(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(
        chat_id,
        message_id,
        cb_id,
        action=_IMAGE_MODELS_STATE,
        text=(
            "请输入 xAI Imagine 图片模型，每行一个或用逗号分隔：\n\n"
            "例如：\n"
            "<code>grok-imagine-image\n"
            "grok-imagine-image-quality</code>\n\n"
            "发送 <code>-</code> 可清空并停用 Grok 图片模型路由。"
        ),
    )


def on_edit_video_models(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(
        chat_id,
        message_id,
        cb_id,
        action=_VIDEO_MODELS_STATE,
        text=(
            "请输入 xAI Imagine 视频模型，每行一个或用逗号分隔：\n\n"
            "例如：\n"
            "<code>grok-imagine-video\n"
            "grok-imagine-video-1.5</code>\n\n"
            "发送 <code>-</code> 可清空并停用 Grok 视频模型。"
        ),
    )


def on_edit_job_ttl(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(
        chat_id,
        message_id,
        cb_id,
        action=_JOB_TTL_STATE,
        text=(
            "请输入视频任务与 OAuth 账号的绑定时长。\n\n"
            "支持秒数或 <code>s / m / h / d</code>，例如："
            "<code>10800</code>、<code>180m</code>、<code>3h</code>。"
        ),
    )


def on_edit_request_timeout(chat_id: int, message_id: int, cb_id: str) -> None:
    _ask(
        chat_id,
        message_id,
        cb_id,
        action=_REQUEST_TIMEOUT_STATE,
        text=(
            "请输入 Imagine 上游 HTTP 请求超时。\n\n"
            "支持秒数或 <code>s / m / h</code>，例如："
            "<code>180</code>、<code>3m</code>。"
        ),
    )


def _parse_models(text: str) -> list[str]:
    raw = (text or "").strip()
    if raw.lower() in _CLEAR_VALUES:
        return []
    values: list[str] = []
    for item in re.split(r"[\s,，;；]+", raw):
        model = item.strip()
        if not model:
            continue
        if len(model) > 128:
            raise ValueError("单个模型名不能超过 128 个字符")
        if model not in values:
            values.append(model)
    if not values:
        raise ValueError("模型列表不能为空；如需清空请发送 -")
    if len(values) > 50:
        raise ValueError("模型数量不能超过 50 个")
    return values


def _parse_duration(text: str, *, allow_days: bool = True) -> int:
    raw = (text or "").strip().lower()
    match = _DURATION_RE.fullmatch(raw)
    if match is None:
        raise ValueError("请输入正整数，并可附加 s/m/h/d 单位")
    value = int(match.group(1))
    unit = match.group(2).lower()
    if value <= 0:
        raise ValueError("时长必须大于 0")
    if unit == "d" and not allow_days:
        raise ValueError("请求超时仅支持 s/m/h")
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = value * multiplier
    if seconds > 2_147_483_647:
        raise ValueError("时长过大")
    return seconds


def _save_models(chat_id: int, *, key: str, label: str, text: str) -> None:
    try:
        models = _parse_models(text)
    except ValueError as exc:
        ui.send(chat_id, f"❌ {ui.escape_html(str(exc))}，请重新输入：")
        return
    _mutate_xai(key, models)
    states.pop_state(chat_id)
    value = "、".join(models) if models else "（空）"
    ui.send_result(
        chat_id,
        f"✅ {label}已更新：\n<code>{ui.escape_html(value)}</code>",
        back_label="◀ 返回 Grok Imagine",
        back_callback="xim:show",
    )


def _save_duration(
    chat_id: int,
    *,
    key: str,
    label: str,
    text: str,
    allow_days: bool,
) -> None:
    try:
        seconds = _parse_duration(text, allow_days=allow_days)
    except ValueError as exc:
        ui.send(chat_id, f"❌ {ui.escape_html(str(exc))}，请重新输入：")
        return
    _mutate_xai(key, seconds)
    states.pop_state(chat_id)
    ui.send_result(
        chat_id,
        f"✅ {label}已更新为 <code>{_fmt_duration(seconds)}</code>（{seconds}s）",
        back_label="◀ 返回 Grok Imagine",
        back_callback="xim:show",
    )


def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str) -> bool:
    if data == "xim:show":
        show(chat_id, message_id, cb_id)
        return True
    if data == "xim:edit:image":
        on_edit_image_models(chat_id, message_id, cb_id)
        return True
    if data == "xim:edit:video":
        on_edit_video_models(chat_id, message_id, cb_id)
        return True
    if data == "xim:edit:ttl":
        on_edit_job_ttl(chat_id, message_id, cb_id)
        return True
    if data == "xim:edit:timeout":
        on_edit_request_timeout(chat_id, message_id, cb_id)
        return True
    return False


def handle_text_state(chat_id: int, action: str, text: str) -> bool:
    if action == _IMAGE_MODELS_STATE:
        _save_models(
            chat_id,
            key="imageModels",
            label="图片模型",
            text=text,
        )
        return True
    if action == _VIDEO_MODELS_STATE:
        _save_models(
            chat_id,
            key="videoModels",
            label="视频模型",
            text=text,
        )
        return True
    if action == _JOB_TTL_STATE:
        _save_duration(
            chat_id,
            key="videoJobTtlSeconds",
            label="视频任务绑定时长",
            text=text,
            allow_days=True,
        )
        return True
    if action == _REQUEST_TIMEOUT_STATE:
        _save_duration(
            chat_id,
            key="mediaRequestTimeoutSeconds",
            label="媒体请求超时",
            text=text,
            allow_days=False,
        )
        return True
    return False
