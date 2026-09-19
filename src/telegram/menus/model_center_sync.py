"""模型中心 · 同步上游模型：来源多选页与实时进度页。

callback_data 沿用模型中心的 ``mc:a:<token>`` 冻结动作机制。

设计要点：
- **多选来源**：账户与渠道来自 ``model_center_catalog._source_options()``（与来源
  选择器同一份清单），因此标签与身份口径不会出现两处不一致。
- **来源分页**：按稳定来源身份跨页选择；全选/反选仍作用于渲染时全部来源。
- **实时进度**：同步跑在管理层的 OperationStore 线程里，这里注册一个 sink，
  任务每进入/完成一项就回调一次并 edit 同一条消息；任务结束即注销 sink。
  与 channel_menu 的模型测试进度消息同一做法（都是编辑同一条消息）。
- **取消**：协作式，只在两项之间检查；已经发出的上游请求无法中断。
"""

from __future__ import annotations

import secrets
import threading
from typing import Any, Callable

from ...management_control import ManagementError
from ...management_control.models import ModelSourceRef
from .. import menu_cache, ui
from . import model_center_menu as menu

# 进度页最多列出多少个模型名，以及标签/错误的字符上限。
_MODEL_PREVIEW_COUNT = 5
_LABEL_CLIP = 60
_ERROR_CLIP = 80

# 每行放几个序号按钮，与负载均衡的多选页保持一致。
_NUMBERS_PER_ROW = 5
_SOURCE_PAGE_SIZE = 20

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
# sink 可能先于 start() 返回而触发；每次启动捕获自己的消息/视图 token，
# 不借用 chat 级可变位置，导航后后台事件不能重新认领新页面。
_LOCK = threading.RLock()
_EVENTS: dict[str, list[dict[str, Any]]] = {}
_CHAT_BY_OPERATION: dict[str, int] = {}
_MESSAGE_BY_OPERATION: dict[str, tuple[int, int, int]] = {}

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
    """勾选保存来源身份；选择页代号阻止旧页面修改新一轮选择。"""
    state = menu._session(chat_id)
    store = getattr(state, "_mc_sync_selection", None)
    if store is None:
        store = {"selected": [], "id": secrets.token_hex(8)}
        setattr(state, "_mc_sync_selection", store)
    return store


def _available_sources(chat_id: int) -> list[tuple[str, ModelSourceRef]]:
    """(展示标签, ref)；标签给人看，ref 才是身份。"""
    from . import model_center_catalog as catalog

    return [(str(option.label), option.ref) for option in catalog._source_options(chat_id)]


def _picker_render(chat_id: int, back_callback: str, page: int = 1) -> tuple[str, dict]:
    sources = _available_sources(chat_id)
    state = _selection(chat_id)
    refs = tuple(ref for _label, ref in sources)
    selected = set(state["selected"]).intersection(refs)
    state["selected"] = [ref for ref in refs if ref in selected]
    total = len(sources)
    pages = max(1, (total + _SOURCE_PAGE_SIZE - 1) // _SOURCE_PAGE_SIZE)
    page = min(max(1, page), pages)
    start = (page - 1) * _SOURCE_PAGE_SIZE
    visible = sources[start:start + _SOURCE_PAGE_SIZE]

    def callback(name, **data):
        return menu._freeze(chat_id, name, selection_id=state["id"], page=page,
                            back_callback=back_callback, **data)

    lines = [
        "🔄 <b>同步上游模型 · 选择来源</b>",
        "",
        f"已选 <b>{len(selected)}</b> / {total} 个来源",
        "",
        "同步所选来源的模型目录；有更新时，全部来源处理完后统一更新一次元数据。人工匹配、字段手工值与来源单独匹配保持不变。",
    ]
    if not sources:
        lines.extend(["", "当前没有可同步的来源。"])
    for index, (label, ref) in enumerate(visible, start + 1):
        mark = "✅" if ref in selected else "▫️"
        lines.append(f"{mark} {index}. {ui.escape_html(_clip(label, _LABEL_CLIP))}")

    keyboard: list[list[dict]] = []
    if sources:
        row: list[dict] = []
        for index, (_label, ref) in enumerate(visible, start + 1):
            label = f"{index} ✅" if ref in selected else str(index)
            row.append(ui.btn(label, callback("sync_pick_toggle", source=ref)))
            if len(row) >= _NUMBERS_PER_ROW:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        if pages > 1:
            keyboard.append([
                ui.btn("◀ 上一页", callback("sync_pick_page", target_page=page - 1) if page > 1 else "mc:noop"),
                ui.btn(f"{page}/{pages}", "mc:noop"),
                ui.btn("下一页 ▶", callback("sync_pick_page", target_page=page + 1) if page < pages else "mc:noop"),
            ])
        keyboard.append([
            ui.btn("✅ 全选", callback("sync_pick_all", mode="all", sources=refs)),
            ui.btn("🔄 反选", callback("sync_pick_all", mode="invert", sources=refs)),
            ui.btn("⬜ 全不选", callback("sync_pick_all", mode="none", sources=refs)),
        ])
        if selected:
            keyboard.append([ui.btn(f"🚀 同步所选的 {len(selected)} 个", callback(
                "sync_pick_start", sources=tuple(ref for ref in refs if ref in selected)))])
        keyboard.append([ui.btn(f"🌐 同步全部（{total} 个）", callback(
            "sync_pick_start", sources=refs))])
    keyboard.append([ui.btn("◀ 返回", back_callback)])
    return menu._paged(chat_id, "\n".join(lines), ui.inline_kb(keyboard))


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
        elif phase == "metadata_start":
            lines.extend(["🧬 来源同步结束，正在统一更新元数据…", ""])
        elif phase == "metadata_done":
            messages = {
                "succeeded": "✅ 公共元数据已更新并重新匹配",
                "partial_failed": "⚠️ 公共元数据拉取失败，已使用本地目录匹配",
                "failed": "⚠️ 元数据匹配失败，已同步的模型目录保留",
                "skipped": "ℹ️ 元数据自动更新已关闭，已跳过",
            }
            lines.extend([messages.get(str(event.get("status")), "元数据更新未完成"), ""])
    if total and done >= total:
        summary = "来源处理完成" if any(str(e.get("phase", "")).startswith("metadata_") for e in events) else "全部完成"
        lines.extend(["──────────────", f"{summary}：{done}/{total}"])
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
    if not chat_id or target is None or not ui.is_admin(chat_id):
        return
    _, message_id, token = target
    try:
        menu_cache.run_if_current(chat_id, message_id, token, lambda: ui.edit(
            chat_id, message_id, ui.truncate(_progress_text(operation_id)),
            reply_markup=_progress_keyboard(
                chat_id, operation_id, back_callback,
                finished=_operation_finished(chat_id, operation_id))))
    except Exception:
        pass


def _paint_current(chat_id: int, message_id: int, operation_id: str) -> None:
    """Only an explicit task callback may bind a fresh progress view."""
    target = menu_cache.subscriber(chat_id, message_id, menu_cache.begin_view(chat_id, message_id))
    with _LOCK:
        _MESSAGE_BY_OPERATION[operation_id] = target
    _paint(operation_id)


_BACK_BY_OPERATION: dict[str, str] = {}


def make_sink(chat_id: int, back_callback: str, *,
              target: tuple[int, int, int] | None = None) -> Callable[[str, dict], None]:
    """构造进度 sink（由同步任务在后台线程调用，参数是 (operation_id, event)）。"""

    def sink(operation_id: str, event: dict) -> None:
        with _LOCK:
            _CHAT_BY_OPERATION.setdefault(operation_id, int(chat_id))
            _BACK_BY_OPERATION.setdefault(operation_id, back_callback)
            if target is not None:
                _MESSAGE_BY_OPERATION.setdefault(operation_id, target)
            events = _EVENTS.setdefault(operation_id, [])
            phase = str(event.get("phase") or "")
            index = int(event.get("index") or 0)
            if phase in {"done", "cancelled"}:
                # 同序号的 start 已被完成事件取代，避免页面同时出现两行。
                events[:] = [row for row in events if not (
                    str(row.get("phase")) == "start" and int(row.get("index") or 0) == index)]
            if phase == "metadata_done":
                events[:] = [row for row in events if row.get("phase") != "metadata_start"]
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
    _selection(chat_id).update(selected=[], id=secrets.token_hex(8))
    menu._show_rendered(chat_id, message_id, cb_id,
                        lambda: _picker_render(chat_id, back_callback))


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name = str(getattr(action, "name", "") or "")
    if name not in {
        "sync_pick_open", "sync_pick_toggle", "sync_pick_all",
        "sync_pick_start", "sync_pick_page", "sync_cancel",
    }:
        return False
    data = action.data
    back = str(data.get("back_callback") or "mc:list")

    if name == "sync_pick_open":
        open_picker(chat_id, message_id, cb_id, back)
        return True

    if name.startswith("sync_pick_"):
        state = _selection(chat_id)
        if data.get("selection_id") != state["id"]:
            ui.answer_cb(cb_id, "来源选择页已过期，请重新打开", show_alert=True)
            return True
        page = int(data.get("page") or 1)
        available = {ref for _label, ref in _available_sources(chat_id)}
        refs = (data.get("source"),) if name == "sync_pick_toggle" else tuple(data.get("sources") or ())
        if any(not isinstance(ref, ModelSourceRef) or ref not in available for ref in refs):
            ui.answer_cb(cb_id, "来源已变化，请重新选择", show_alert=True)
            return True
        if name == "sync_pick_start":
            if not refs:
                ui.answer_cb(cb_id, "请先选择要同步的来源", show_alert=True)
                return True
            return _start(chat_id, message_id, cb_id, refs, back)
        if name == "sync_pick_toggle":
            selected = state["selected"]
            ref = refs[0]
            selected.remove(ref) if ref in selected else selected.append(ref)
        elif name == "sync_pick_all":
            mode = str(data.get("mode") or "")
            selected = set(state["selected"])
            state["selected"] = (list(refs) if mode == "all" else [] if mode == "none"
                                 else [ref for ref in refs if ref not in selected])
        elif name == "sync_pick_page":
            page = int(data.get("target_page") or 1)
        menu._show_rendered(chat_id, message_id, cb_id, lambda: _picker_render(chat_id, back, page))
        return True

    if name == "sync_cancel":
        operation_id = str(data.get("operation_id") or "")
        try:
            menu._CONTROL.operations.cancel(menu._ctx(chat_id), operation_id)
        except ManagementError as exc:
            # 任务可能刚好在这一刻完成；那不是失败，重绘一次让它切到终态即可。
            if str(menu._enum_value(exc.code)) == "INVALID_OPERATION_STATE":
                ui.answer_cb(cb_id, "同步已结束")
                _paint_current(chat_id, message_id, operation_id)
                return True
            menu._answer_error(cb_id, exc)
            return True
        ui.answer_cb(cb_id, "已请求取消，当前项跑完后停止")
        _paint_current(chat_id, message_id, operation_id)
        return True
    return False


def _start(chat_id: int, message_id: int, cb_id: str,
           refs: tuple[ModelSourceRef, ...], back_callback: str) -> bool:
    target = menu_cache.subscriber(chat_id, message_id, menu_cache.begin_view(chat_id, message_id))
    ui.answer_cb(cb_id, "同步已开始")
    try:
        operation = menu._CONTROL.start_upstream_sync(
            menu._ctx(chat_id), None, sources=refs,
            progress_sink=make_sink(chat_id, back_callback, target=target),
        )
    except ManagementError as exc:
        menu._answer_error(cb_id, exc)
        return True
    except Exception:
        ui.edit(chat_id, message_id, "❌ 同步未能启动，请重试。",
                reply_markup=ui.inline_kb([[ui.btn("◀ 返回", back_callback)]]))
        return True

    operation_id = str(operation.id)
    with _LOCK:
        _CHAT_BY_OPERATION.setdefault(operation_id, int(chat_id))
        _BACK_BY_OPERATION.setdefault(operation_id, back_callback)
        _MESSAGE_BY_OPERATION.setdefault(operation_id, target)
        _EVENTS.setdefault(operation_id, [])
    _paint(operation_id)
    return True
