"""Model sources on the detail page: scheduling order, merged blocks and per-source switches.

The model_center_menu facade owns the one control binding and session state.
"""
from __future__ import annotations

from ...management_control import ManagementError
from ...management_control.models import (
    ModelKind,
    ModelSelection,
    ModelSelectionMode,
    ModelSourceRef,
    ModelSourceType,
    ModelStateField,
    ModelStateTarget,
    ModelView,
)
from .. import ui
from . import model_center_menu as menu
from .model_center_icons import inline_kb


def _wire_efforts(model_id: str) -> dict[str, str]:
    """Upstream ids fixed to one effort level (Antigravity's -low/-high ids), by id."""
    from ...model_registry import runtime as model_registry
    from ...model_registry.names import EFFORTS
    registry = model_registry.current()
    model = registry.models.get(model_id) if registry else None
    return {offer.wire: offer.traits["effort"] for offer in (model.offers if model else ())
            if offer.traits.get("effort") in EFFORTS}


def _fold_source_ids(
    model_id: str, blocks: list[tuple[str, str, list[str]]], wires: list[str | None],
    refs: list[tuple], ranks: list[int | None],
) -> tuple[list[tuple[str, str, list[str]]], list[int | None]]:
    """One block per source, prefixed with its upstream ids.

    A source serving the model under several ids (one per effort, as Antigravity
    does) lists them in one block when everything else reads the same.
    """
    from ...model_registry.names import EFFORTS
    fixed = _wire_efforts(model_id)
    effort = {wire: EFFORTS.index(level) for wire, level in fixed.items()}
    members: dict[tuple, list[int]] = {}
    for position, ref in enumerate(refs):
        members.setdefault(ref, []).append(position)

    def upstream(ids) -> list[str]:
        ids = sorted(dict.fromkeys(item for item in ids if item), key=lambda item: effort.get(item, len(effort)))
        return ["上游名：" + " / ".join(f"<code>{ui.escape_html(item)}</code>" for item in ids)] if ids else []

    folded, folded_ranks = [], []
    for ref, positions in members.items():
        if len({tuple(blocks[position][2]) for position in positions}) == 1:
            groups = [positions]
        else:
            groups = [[position] for position in positions]
        for group in groups:
            brand, label, body = blocks[group[0]]
            levels = sorted({fixed[wires[position]] for position in group if wires[position] in fixed}, key=EFFORTS.index)
            if len(group) > 1 and levels:
                # Each level is its own id here; the catalog's list would overstate what can be asked for.
                line = f"思考档位：<code>{ui.escape_html(' / '.join(levels))}</code>（每档一个上游 ID）"
                body = [line if item.startswith("思考档位：") else item for item in body]
            folded.append((brand, label, upstream(wires[position] for position in group) + list(body)))
            folded_ranks.append(ranks[group[0]] if ranks else None)
    return folded, folded_ranks if ranks else []


def _merged_source_blocks(blocks: list[tuple[str, str, list[str]]], ranks: list[int | None] | None = None) -> list[str]:
    """One block per provider and identical content, naming every account it covers.

    With ``ranks`` (the scheduling order), only neighbours merge so the order stays readable.
    """
    groups: list[list] = []
    index: dict[tuple, list] = {}
    for position, (brand, label, body) in enumerate(blocks):
        provider, sep, name = label.partition(" · ")
        key = (brand, provider, bool(sep), tuple(body))
        rank = ranks[position] if ranks else None
        group = index.get(key) if not ranks else (groups[-1] if groups and groups[-1][0] == key else None)
        if group is None:
            group = [key, [], []]
            groups.append(group)
            index[key] = group
        group[1].append(name)
        group[2].append(rank)
    lines: list[str] = []
    for (brand, provider, named, body), names, group_ranks in groups:
        if not named:
            title = provider + (f"（{len(names)} 个）" if len(names) > 1 else "")
        elif len(names) == 1:
            title = f"{provider} · {names[0]}"
        else:
            title = f"{provider} · {'、'.join(names)}（{len(names)} 个账号）"
        known = [rank for rank in group_ranks if rank is not None]
        if ranks:
            title = (f"{known[0]}" if len(known) == 1 else f"{known[0]}–{known[-1]}" if known else "—") + ". " + title
        lines.extend(["", f"{brand}<b>{ui.escape_html(title)}</b>", *body])
    return lines


def _source_key(source_view) -> str:
    if source_view.type is ModelSourceType.OAUTH:
        from ...management_control.routing_account_ids import oauth_channel_key_from_account_id
        return oauth_channel_key_from_account_id(source_view.id)
    return str(source_view.id)


def _schedule(view: ModelView) -> tuple[str, str, list[str]]:
    """(mode, description, channel keys in scheduling order); smart mode has no fixed order."""
    from ...management_control.load_balancing import load_balancing_control as control
    mode = control.get_mode()
    keys = {_source_key(item) for item in view.sources}
    if mode == "priority":
        channels = [channel for channel in control.channels_for_model(view.model_id) if channel.key in keys]
        own = control.has_model_priority(view.model_id)
        return mode, "优先级 · " + ("本模型专属顺序" if own else "继承统一渠道顺序"), \
            control.effective_order_for_model(view.model_id, channels)
    if mode == "order":
        return mode, "顺序调度 · 按渠道注册顺序", [channel.key for channel in control.all_channels() if channel.key in keys]
    return mode, "智能调度 · 按实时评分挑选，无固定顺序", []


def _ordered_sources(view: ModelView, order: list[str]) -> tuple[list, list[int | None]]:
    if not order:
        return list(view.sources), []
    rank = {key: position for position, key in enumerate(order, 1)}
    sources = sorted(view.sources, key=lambda item: rank.get(_source_key(item), len(rank) + 1))
    return sources, [rank.get(_source_key(item)) for item in sources]


def _source_states_render(chat_id: int, resource_key: str, detail_back: str) -> tuple[str, dict]:
    """Per-account/channel switch for one model: stops only that source scheduling it."""
    from .model_center_catalog import _model_display_name
    view = menu._CONTROL.get_model(menu._ctx(chat_id), resource_key)
    _mode, schedule_text, order = _schedule(view)
    sources, ranks = _ordered_sources(view, order)
    labels = {(item.ref.type, item.ref.id): item.label for item in menu._source_options(chat_id)}
    lines = [
        f"🔌 <b>{ui.escape_html(_model_display_name(view, None))} · 按账户/渠道启停</b>", "",
        "停用某个账户/渠道，只是不再用它调度这个模型；要停用整个模型，请回到详情页点「停用模型」。",
        f"全局：<code>{'启用' if view.global_enabled else '停用'}</code> · 调度：<code>{ui.escape_html(schedule_text)}</code>",
        "",
    ]
    rows: list[list[dict]] = []
    # A source serving the model under several ids is one switch: it covers all of them.
    seen: set[tuple] = set()
    unique: list[tuple] = []
    for item, rank in zip(sources, ranks or [None] * len(sources)):
        if (item.type, item.id) not in seen:
            seen.add((item.type, item.id))
            unique.append((item, rank))
    sources = [item for item, _rank in unique]
    ranks = [rank for _item, rank in unique] if ranks else []
    for number, source_view in enumerate(sources, 1):
        ref = ModelSourceRef(source_view.type, source_view.id)
        enabled, state_text = menu._model_state(view, ref)
        label = labels.get((source_view.type, source_view.id)) or (
            "已移除的账户" if source_view.type is ModelSourceType.OAUTH else "已移除的渠道")
        rank = ranks[number - 1] if ranks else None
        lines.append(f"{number}. {'✅' if enabled else '🚫'} {ui.escape_html(label)} · {ui.escape_html(state_text)}"
                     + (f" · 调度第 {rank} 位" if rank else ""))
        if not source_view.container_enabled:
            continue
        if enabled:
            action = "停用"
        else:
            action = "启用" if view.global_enabled else "仅启用（覆盖全局停用）"
        rows.append([ui.btn(f"{action} {number} · {label}"[:60], menu._freeze(
            chat_id, "source_state_set", resource_key=view.resource_key, source=ref, enabled=not enabled,
            revision=view.revision, detail_back=detail_back))])
    if not sources:
        lines.append("这个模型目前没有来源。")
    lines += ["", "来源整体已停用的账户/渠道要在账户或渠道管理里启用，这里不提供按钮。"]
    rows.append([ui.btn("返回模型详情", menu._detail_callback(chat_id, view.resource_key, detail_back))])
    return menu._paged(chat_id, "\n".join(lines), inline_kb(rows))


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    if name == "source_states":
        menu._show_rendered(chat_id, message_id, cb_id, lambda: _source_states_render(
            chat_id, data["resource_key"], str(data.get("detail_back") or "mc:list")))
        return True
    if name == "source_state_set":
        view_key, back = data["resource_key"], str(data.get("detail_back") or "mc:list")
        try:
            view = menu._CONTROL.get_model(menu._ctx(chat_id), view_key)
            menu._CONTROL.set_state(
                menu._ctx(chat_id), scope=data["source"],
                selection=ModelSelection(mode=ModelSelectionMode.IDS, model_ids=(view.model_id,)),
                target=ModelStateTarget(ModelStateField.ENABLED, bool(data["enabled"])),
                expected_revision=data.get("revision"),
            )
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
        else:
            ui.answer_cb(cb_id, "已启用" if data["enabled"] else "已停用")
        menu._show_rendered(chat_id, message_id, None, lambda: _source_states_render(chat_id, view_key, back))
        return True
    if name == "lb_priority":
        from .load_balancing_menu import open_model_editor
        open_model_editor(chat_id, message_id, cb_id, data["model_id"], menu._detail_callback(
            chat_id, data["resource_key"], str(data.get("detail_back") or "mc:list")))
        return True
    return False
