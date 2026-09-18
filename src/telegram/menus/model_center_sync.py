"""模型中心 · 同步上游模型：来源多选页与实时进度页。

callback_data 沿用模型中心的 ``mc:a:<token>`` 冻结动作机制。

设计要点：
- **多选来源**：账户与渠道来自 ``model_center_catalog._source_options()``（与来源
  选择器同一份清单），因此标签与身份口径不会出现两处不一致。
- **不分页**：来源数量级很小（渠道 + OAuth 账户），一次列全更利于全选/反选。
- **实时进度**：同步跑在管理层的 OperationStore 线程里，这里注册一个 sink，
  任务每进入/完成一项就回调一次并 edit 同一条消息；任务结束即注销 sink。
  与 channel_menu 的模型测试进度消息同一做法（都是编辑同一条消息）。
- **取消**：协作式，只在两项之间检查；已经发出的上游请求无法中断。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ...management_control import ManagementError
from ...management_control.models import ModelSourceRef
from .. import ui
from . import model_center_menu as menu

# 进度页最多列出多少个模型名，以及标签/错误的字符上限。
_MODEL_PREVIEW_COUNT = 5
_LABEL_CLIP = 60
_ERROR_CLIP = 80

# 每行放几个序号按钮，与负载均衡的多选页保持一致。
_NUMBERS_PER_ROW = 5

_STATUS_TEXT = {
    "succeeded": "模型同步完成",
    "partial_failed": "部分完成（有别名冲突）",
    "failed": "同步失败",
    "cancelled": "已取消",
}

_ERROR_TEXT = {
    "UPSTREAM_ERROR": "上游同步失败，原目录保留",
    "UPSTREAM_TIMEOUT": "上游超时，原目录保留",
    "RESOURCE_NOT_FOUND": "来源已移除",
    "REVISION_CONFLICT": "来源已变化，请重新同步",
    "UNSUPPORTED_VALUE": "上游不支持实时模型目录",
    "DEPENDENCY_UNAVAILABLE": "依赖或运行时刷新异常",
    "ALIAS_CONFLICT": "部分新模型与手工别名冲突，未覆盖",
}

# ── 进度状态 ───────────────────────────────────────────────────────────────
# 进度回调发生在后台线程，只带 operation_id；消息位置与 chat 在开始同步时登记。
# sink 可能先于 start() 返回而触发，此时还不知道 operation_id，因此按 chat 记一个
# 待认领的消息位置，由第一个事件认领过去。
_LOCK = threading.RLock()
_EVENTS: dict[str, list[dict[str, Any]]] = {}
_CHAT_BY_OPERATION: dict[str, int] = {}
_MESSAGE_BY_OPERATION: dict[str, tuple[int, int]] = {}
_PENDING_MESSAGE_BY_CHAT: dict[int, tuple[int, int]] = {}

# 任务结束后进度记录保留一段时间供查看，随后释放；不主动删 Telegram 消息。
_PROGRESS_TTL_SECONDS = 600.0


def _clip(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _fmt_seconds(elapsed_ms: Any) -> str:
    try:
        ms = int(elapsed_ms or 0)
    except (TypeError, ValueError):
        ms = 0
    return f"{ms / 1000:.1f} 秒" if ms >= 1000 else f"{ms} 毫秒"


def _models_line(names: list[str]) -> str:
    """前 N 个模型名 + 省略号，按名称排序。"""
    ordered = sorted(str(name) for name in names if str(name).strip())
    total = len(ordered)
    if not total:
        return "共 0 个模型"
    text = "、".join(ui.escape_html(name) for name in ordered[:_MODEL_PREVIEW_COUNT])
    if total > _MODEL_PREVIEW_COUNT:
        text += "、……"
    return f"共 {total} 个模型：{text}"


# ── 来源多选页 ─────────────────────────────────────────────────────────────


def _selection(chat_id: int) -> dict[str, Any]:
    """本页勾选状态存会话，序号只在从本次渲染有效。"""
    state = menu._session(chat_id)
    store = getattr(state, "_mc_sync_selection", None)
    if store is None:
        store = {"selected": []}
        setattr(state, "_mc_sync_selection", store)
    return store


def _available_sources(chat_id: int) -> list[tuple[str, ModelSourceRef]]:
    """(展示标签, ref)；标签给人看，ref 才是身份。"""
    from . import model_center_catalog as catalog

    return [(str(option.label), option.ref) for option in catalog._source_options(chat_id)]


def _picker_render(chat_id: int, back_callback: str) -> tuple[str, dict]:
    sources = _available_sources(chat_id)
    selected = {int(value) for value in _selection(chat_id).get("selected") or []}
    total = len(sources)

    lines = [
        "🔄 <b>同步上游模型 · 选择来源</b>",
        "",
        f"已选 <b>{len(selected)}</b> / {total} 个来源",
        "",
        "同步只刷新所选来源的模型目录；人工匹配、字段手工值与来源单独匹配保持不变。",
    ]
    if not sources:
        lines.extend(["", "当前没有可同步的来源。"])
    for index, (label, _ref) in enumerate(sources, 1):
        mark = "✅" if index in selected else "▫️"
        lines.append(f"{mark} {index}. {ui.escape_html(_clip(label, _LABEL_CLIP))}")

    keyboard: list[list[dict]] = []
    if sources:
        row: list[dict] = []
        for index in range(1, total + 1):
            label = f"{index} ✅" if index in selected else str(index)
            row.append(ui.btn(label, menu._freeze(
                chat_id, "sync_pick_toggle", index=index, back_callback=back_callback)))
            if len(row) >= _NUMBERS_PER_ROW:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([
            ui.btn("✅ 全选", menu._freeze(chat_id, "sync_pick_all", mode="all",
                                         back_callback=back_callback)),
            ui.btn("🔄 反选", menu._freeze(chat_id, "sync_pick_all", mode="invert",
                                          back_callback=back_callback)),
            ui.btn("⬜ 全不选", menu._freeze(chat_id, "sync_pick_all", mode="none",
                                           back_callback=back_callback)),
        ])
        if selected:
            keyboard.append([ui.btn(f"🚀 同步所选的 {len(selected)} 个", menu._freeze(
                chat_id, "sync_pick_start", back_callback=back_callback))])
        keyboard.append([ui.btn(f"🌐 同步全部（{total} 个）", menu._freeze(
            chat_id, "sync_pick_start", mode="all", back_callback=back_callback))])
    keyboard.append([ui.btn("◀ 返回", back_callback)])
    return "\n".join(lines), ui.inline_kb(keyboard)


# ── 实时进度页 ─────────────────────────────────────────────────────────────


def _progress_text(operation_id: str) -> str:
    with _LOCK:
        events = list(_EVENTS.get(operation_id) or [])
    lines = ["🔄 <b>模型同步中</b>", ""]
    if not events:
        return "\n".join(lines + ["正在准备…"])

    done = 0
    total = int(events[0].get("total") or 0)
    for event in events:
        phase = str(event.get("phase") or "")
        index = int(event.get("index") or 0) + 1
        label = ui.escape_html(_clip(event.get("label"), _LABEL_CLIP))
        if phase == "start":
            lines.extend([f"{index}. {label}", "正在同步…", ""])
        elif phase == "done":
            done += 1
            status = str(event.get("status") or "")
            if status in {"succeeded", "partial_failed"}:
                lines.append(
                    f"{index}. {label}\n"
                    f"✅ {_STATUS_TEXT.get(status, status)}，"
                    f"耗时 {_fmt_seconds(event.get('elapsedMs'))}，"
                    f"{_models_line(event.get('models') or [])}"
                )
            else:
                reason = str(event.get("errorCode") or "")
                lines.append(
                    f"{index}. {label}\n"
                    f"❌ {_STATUS_TEXT.get(status, status)}："
                    f"{ui.escape_html(_ERROR_TEXT.get(reason, '同步未完成，请重试'))}"
                )
            lines.append("")
        elif phase == "cancelled":
            lines.extend([f"{index}. {label}", "⏹ 已取消", ""])
    if total and done >= total:
        lines.extend(["──────────────", f"全部完成：{done}/{total}"])
    return "\n".join(lines)


def _progress_keyboard(chat_id: int, operation_id: str, back_callback: str,
                       *, finished: bool) -> dict:
    if finished:
        return ui.inline_kb([[ui.btn("◀ 返回模型中心", back_callback)]])
    return ui.inline_kb([
        [ui.btn("⏹ 取消同步", menu._freeze(
            chat_id, "sync_cancel", operation_id=operation_id,
            back_callback=back_callback))],
    ])


def _operation_finished(chat_id: int, operation_id: str) -> bool:
    try:
        operation = menu._CONTROL.operations.get(menu._ctx(chat_id), operation_id)
    except Exception:
        return True
    return str(menu._enum_value(operation.status)) not in {"queued", "running"}


def _paint(operation_id: str) -> None:
    """把当前进度渲染到绑定的消息；未绑定则忽略（例如任务不是从界面发起的）。"""
    with _LOCK:
        chat_id = int(_CHAT_BY_OPERATION.get(operation_id) or 0)
        target = _MESSAGE_BY_OPERATION.get(operation_id)
        back_callback = _BACK_BY_OPERATION.get(operation_id, "mc:list")
    if not chat_id or target is None:
        return
    _, message_id = target
    try:
        ui.edit(chat_id, message_id, ui.truncate(_progress_text(operation_id)),
                reply_markup=_progress_keyboard(
                    chat_id, operation_id, back_callback,
                    finished=_operation_finished(chat_id, operation_id)))
    except Exception:
        pass


_BACK_BY_OPERATION: dict[str, str] = {}


def make_sink(chat_id: int, back_callback: str) -> Callable[[str, dict], None]:
    """构造进度 sink（由同步任务在后台线程调用，参数是 (operation_id, event)）。"""

    def sink(operation_id: str, event: dict) -> None:
        with _LOCK:
            _CHAT_BY_OPERATION.setdefault(operation_id, int(chat_id))
            _BACK_BY_OPERATION.setdefault(operation_id, back_callback)
            if operation_id not in _MESSAGE_BY_OPERATION:
                pending = _PENDING_MESSAGE_BY_CHAT.pop(int(chat_id), None)
                if pending is not None:
                    _MESSAGE_BY_OPERATION[operation_id] = pending
            events = _EVENTS.setdefault(operation_id, [])
            phase = str(event.get("phase") or "")
            index = int(event.get("index") or 0)
            if phase in {"done", "cancelled"}:
                # 同序号的 start 已被完成事件取代，避免页面同时出现两行。
                events[:] = [row for row in events if not (
                    str(row.get("phase")) == "start" and int(row.get("index") or 0) == index)]
            if phase != "finished":
                # finished 只是"任务已落地"的重绘信号，本身不是一项来源。
                events.append(dict(event))
        _paint(operation_id)
        if phase == "finished" or (
                phase in {"done", "cancelled"} and index + 1 >= int(event.get("total") or 0)):
            _schedule_cleanup(operation_id)

    return sink


def _schedule_cleanup(operation_id: str) -> None:
    """任务结束后保留一段时间供查看，随后释放记录。"""

    def _cleanup() -> None:
        with _LOCK:
            _EVENTS.pop(operation_id, None)
            _CHAT_BY_OPERATION.pop(operation_id, None)
            _MESSAGE_BY_OPERATION.pop(operation_id, None)
            _BACK_BY_OPERATION.pop(operation_id, None)

    timer = threading.Timer(_PROGRESS_TTL_SECONDS, _cleanup)
    timer.daemon = True
    timer.start()


# ── 动作处理 ───────────────────────────────────────────────────────────────


def open_picker(chat_id: int, message_id: int, cb_id: str, back_callback: str) -> None:
    """从"同步上游模型"按钮进入来源多选页。"""
    if not _available_sources(chat_id):
        ui.answer_cb(cb_id, "没有可同步的来源", show_alert=True)
        return
    _selection(chat_id)["selected"] = []
    menu._show_rendered(chat_id, message_id, cb_id,
                        lambda: _picker_render(chat_id, back_callback))


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name = str(getattr(action, "name", "") or "")
    if name not in {
        "sync_pick_open", "sync_pick_toggle", "sync_pick_all",
        "sync_pick_start", "sync_cancel",
    }:
        return False
    data = action.data
    back = str(data.get("back_callback") or "mc:list")

    if name == "sync_pick_open":
        if not _available_sources(chat_id):
            ui.answer_cb(cb_id, "没有可同步的来源", show_alert=True)
            return True
        _selection(chat_id)["selected"] = []
        menu._show_rendered(chat_id, message_id, cb_id, lambda: _picker_render(chat_id, back))
        return True

    if name == "sync_pick_toggle":
        index = int(data.get("index") or 0)
        state = _selection(chat_id)
        selected = {int(value) for value in state.get("selected") or []}
        selected.symmetric_difference_update({index})
        state["selected"] = sorted(selected)
        ui.answer_cb(cb_id)
        menu._show_rendered(chat_id, message_id, None, lambda: _picker_render(chat_id, back))
        return True

    if name == "sync_pick_all":
        sources = _available_sources(chat_id)
        mode = str(data.get("mode") or "")
        if mode == "all":
            chosen = list(range(1, len(sources) + 1))
        elif mode == "none":
            chosen = []
        else:
            current = {int(v) for v in _selection(chat_id).get("selected") or []}
            chosen = [i for i in range(1, len(sources) + 1) if i not in current]
        _selection(chat_id)["selected"] = chosen
        ui.answer_cb(cb_id, f"已选 {len(chosen)} 个")
        menu._show_rendered(chat_id, message_id, None, lambda: _picker_render(chat_id, back))
        return True

    if name == "sync_pick_start":
        sources = _available_sources(chat_id)
        if str(data.get("mode") or "") == "all":
            refs = tuple(ref for _label, ref in sources)
        else:
            chosen = {int(v) for v in _selection(chat_id).get("selected") or []}
            refs = tuple(ref for i, (_label, ref) in enumerate(sources, 1) if i in chosen)
            if not refs:
                ui.answer_cb(cb_id, "请先选择要同步的来源", show_alert=True)
                return True
        return _start(chat_id, message_id, cb_id, refs, back)

    if name == "sync_cancel":
        operation_id = str(data.get("operation_id") or "")
        try:
            menu._CONTROL.operations.cancel(menu._ctx(chat_id), operation_id)
        except ManagementError as exc:
            # 任务可能刚好在这一刻完成；那不是失败，重绘一次让它切到终态即可。
            if str(menu._enum_value(exc.code)) == "INVALID_OPERATION_STATE":
                ui.answer_cb(cb_id, "同步已结束")
                _paint(operation_id)
                return True
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "已请求取消，当前项跑完后停止")
        _paint(operation_id)
        return True
    return False


def _start(chat_id: int, message_id: int, cb_id: str,
           refs: tuple[ModelSourceRef, ...], back_callback: str) -> bool:
    with _LOCK:
        # 第一条事件可能早于 start() 返回，先登记消息位置供 sink 认领。
        _PENDING_MESSAGE_BY_CHAT[int(chat_id)] = (int(chat_id), int(message_id))
    ui.answer_cb(cb_id, "同步已开始")
    try:
        operation = menu._CONTROL.start_upstream_sync(
            menu._ctx(chat_id), None, sources=refs,
            progress_sink=make_sink(chat_id, back_callback),
        )
    except ManagementError as exc:
        with _LOCK:
            _PENDING_MESSAGE_BY_CHAT.pop(int(chat_id), None)
        menu._answer_error(cb_id, exc)
        return True
    except Exception:
        with _LOCK:
            _PENDING_MESSAGE_BY_CHAT.pop(int(chat_id), None)
        ui.edit(chat_id, message_id, "❌ 同步未能启动，请重试。",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", back_callback)]]))
        return True

    operation_id = str(operation.id)
    with _LOCK:
        _CHAT_BY_OPERATION.setdefault(operation_id, int(chat_id))
        _BACK_BY_OPERATION.setdefault(operation_id, back_callback)
        if operation_id not in _MESSAGE_BY_OPERATION:
            pending = _PENDING_MESSAGE_BY_CHAT.pop(int(chat_id), None)
            if pending is not None:
                _MESSAGE_BY_OPERATION[operation_id] = pending
        _EVENTS.setdefault(operation_id, [])
    _paint(operation_id)
    return True
