"""Direct image/video purpose panels; model names are configuration/API owned."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Any
from ...management_control import ManagementError
from ...management_control.models import ModelFilters, ModelKind
from .. import ui
from . import model_center_menu as menu


def _control(kind):
    return menu._CONTROL.images if kind == 'image' else menu._CONTROL.videos


def _panel(chat_id: int, kind: str, back_callback: str = 'menu:main') -> tuple[str, dict]:
    ctx = menu._ctx(chat_id)
    control = _control(kind)
    settings = control.get_settings(ctx)
    sources = control.list_sources(ctx)
    stats = control.statistics(ctx)
    totals = {row['model']: row for row in stats['models']}
    filters = ModelFilters(kinds=(ModelKind(kind),))
    first = menu._CONTROL.list_models(ctx, filters=filters, page=1, page_size=200)
    models = list(first.items)
    for page in range(2, (first.total + 199) // 200 + 1):
        models.extend(menu._CONTROL.list_models(ctx, filters=filters, page=page, page_size=200).items)
    noun, unit = ('图片', '张图片') if kind == 'image' else ('视频', '段视频')
    callback = menu._freeze(chat_id, 'media_panel', kind=kind, back_callback=back_callback)
    lines = [f'🤖 <b>模型中心 · {noun} · {len(models)} 个</b>', '',
        f'{noun}接口：{"开" if settings.enabled else "关"}',
        f'{noun}缓存：{"开" if settings.cache_enabled else "关"}',
        f'保留天数：{settings.cache_retention_days} 天（0=永久）',
        f'空间上限：{_fmt_bytes(settings.cache_max_bytes)}（0=不限）',
        f'当前缓存占用：{stats["cache"]["files"]} 个文件 · {_fmt_bytes(stats["cache"]["bytes"])}',
        f'缓存目录：<code>{ui.escape_html(settings.cache_path)}</code>',
        f'默认模型：<code>{ui.escape_html(settings.default_model or "未设置（按可用列表自动选择）")}</code>']
    if kind == 'video':
        lines.extend([f'请求超时：{settings.request_timeout_seconds} 秒', f'任务 TTL：{settings.job_ttl_seconds} 秒'])
    lines.extend(['', '<b>可用模型：</b>'])
    for index, view in enumerate(models, 1):
        active = [row for row in view.sources if row.effective_routable]
        provider = active[0].provider if active else view.sources[0].provider if view.sources else next((p for p, names in settings.models.items() if view.model_id in names), '')
        available = bool(active) and view.global_enabled is not False
        lines.append(f'{index}. {"✅" if available else "⏸"} {ui.provider_tag(provider)} <code>{ui.escape_html(view.model_id)}</code>')
        labels = list(dict.fromkeys(row.label for row in active)) if available else []
        lines.append('    可用来源：' + ('、'.join(ui.escape_html(label) for label in labels) if labels else '暂无'))
        # Logs use the resolved public model, not every upstream alias target.
        names = {view.model_id}
        rows = [totals[name] for name in names if name in totals]
        count = sum(row['generated_count'] for row in rows)
        size = sum(row['recorded_bytes'] for row in rows)
        lines.append(f'    已生成：{count} {unit} · 已记录 {_fmt_bytes(size)}')
    if not models: lines.append('暂无已配置模型。')
    keyboard = [menu._tabs(kind)]
    for source in sources:
        state = '已启用' if source['enabled'] else '已禁用'
        keyboard.append([ui.provider_button(f'{source["label"]} · {state}', menu._freeze(
            chat_id, 'media_source', kind=kind, source_id=source['source_id'], target=not source['enabled'],
            revision=source['revision'], back_callback=back_callback), source['provider'])])
        if source['unavailable_reason'] and not source['effective_available']:
            lines.append(f'{ui.escape_html(source["label"])}：{ui.escape_html(source["unavailable_reason"])}')
    def patch(label, field, target):
        return ui.btn(label, menu._freeze(chat_id, 'media_patch', kind=kind, field=field, target=target,
            revision=settings.revision, back_callback=back_callback))
    def edit(label, field):
        return ui.btn(label, menu._freeze(chat_id, 'media_field', kind=kind, field=field,
            revision=settings.revision, back_callback=back_callback, page_back=callback))
    keyboard.extend([[patch(f'{noun}接口：{"开启" if settings.enabled else "关闭"}', 'enabled', not settings.enabled),
                      patch(f'缓存：{"开启" if settings.cache_enabled else "关闭"}', 'cacheEnabled', not settings.cache_enabled)],
                     [ui.btn('⭐ 设置默认模型', menu._freeze(chat_id, 'media_default_model', kind=kind,
                                                            back_callback=back_callback))],
                     [edit('设置保留天数', 'cacheRetentionDays'), edit('设置空间上限', 'cacheMaxBytes')],
                     [edit('设置缓存目录', 'cachePath'), edit('设置请求超时', 'requestTimeoutSeconds')]])
    back = ui.btn('返回主菜单' if back_callback == 'menu:main' else '返回', back_callback)
    if kind == 'video':
        keyboard.append([edit('设置任务 TTL', 'jobTtlSeconds'), ui.btn('多媒体日志', 'media:logs')])
        keyboard.append([back])
    else:
        keyboard.append([ui.btn('多媒体日志', 'media:logs'), back])
    return menu._paged(chat_id, '\n'.join(lines), ui.inline_kb(keyboard))


def _image_settings_render(chat_id, back_callback='menu:main'):
    return _panel(chat_id, 'image', back_callback)


def _video_settings_render(chat_id, back_callback='menu:main'):
    return _panel(chat_id, 'video', back_callback)


# Old messages safely land on their media panel, never resurrect TG model edits.
def _gpt_images_render(chat_id, back_callback='menu:main'):
    return _image_settings_render(chat_id, back_callback)


def _media_manager_render(chat_id, provider, kind, *args, back_callback=None, **kwargs):
    if provider == 'antigravity': raise ValueError('AG 生图支持已移除')
    return _panel(chat_id, kind, back_callback or 'menu:main')


def _media_detail_render(chat_id, provider, kind, *args, back_callback=None, **kwargs):
    return _panel(chat_id, kind, back_callback or 'menu:main')


def _media_view_detail_render(chat_id, view, *, back_callback='mc:list'):
    return _panel(chat_id, menu._enum_value(view.identity.kind), back_callback)


def _media_values(chat_id, provider, kind):
    settings = _control(kind).get_settings(menu._ctx(chat_id))
    return tuple(settings.models.get(provider, [])), settings.revision, ()


def _ag_settings_optional(chat_id):
    return None


def _media_limits(provider):
    return 50, 128


def _media_method(*args):
    raise ValueError('模型名请通过配置 / 管理 API 维护。')


def _media_owner(provider):
    return None


def _parse_media_model(*args):
    raise ValueError('模型名请通过配置 / 管理 API 维护。')


_parse_media_models = _parse_media_model

def _fmt_bytes(value: Any) -> str:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    amount = float(max(0, number))
    while amount >= 1024 and index < len(units) - 1:
        amount /= 1024
        index += 1
    return f"{int(amount)}B" if index == 0 else f"{amount:.1f}{units[index]}"


def _parse_duration_input(text: str, *, allow_days: bool) -> int:
    import re
    matched = re.fullmatch(r"(\d+)\s*([smhd]?)", str(text or "").strip().lower())
    if matched is None:
        raise ValueError("请输入正整数，并可附加 s/m/h" + ("/d" if allow_days else "") + " 单位。")
    value = int(matched.group(1))
    unit = matched.group(2)
    if value <= 0 or (unit == "d" and not allow_days):
        raise ValueError("请求超时仅支持正整数及 s/m/h。" if not allow_days else "时长必须大于0。")
    seconds = value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > 2_147_483_647:
        raise ValueError("时长过大。")
    return seconds


def _parse_bytes_input(text: str) -> int:
    raw = str(text or "").strip().upper().replace(" ", "")
    if raw in {"", "0"}:
        return 0
    for suffix, multiplier in (
        ("GB", 1024**3), ("G", 1024**3), ("MB", 1024**2),
        ("M", 1024**2), ("KB", 1024), ("K", 1024), ("B", 1),
    ):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)]
            break
    else:
        multiplier = 1
    try:
        value = Decimal(raw) * multiplier
    except InvalidOperation as exc:
        raise ValueError("请输入如 1GB / 500MB / 0。") from exc
    if value != value.to_integral_value() or value < 0 or value > 2**63 - 1:
        raise ValueError("缓存空间必须是0以上的整数大小。")
    return int(value)


_DEFAULT_MODEL_PAGE_SIZE = 8


def _available_model_names(chat_id: int, kind: str) -> list[str]:
    """可供选择的模型名单：来自模型中心的图片/视频分类，按字母序稳定排列。"""
    filters = ModelFilters(kinds=(ModelKind(kind),))
    views = menu._CONTROL.list_models(menu._ctx(chat_id), filters=filters, page=1, page_size=200)
    names = [view.model_id for view in views.items]
    return sorted(set(names))


def _default_model_panel(chat_id: int, kind: str, page: int = 0,
                         back_callback: str = 'menu:main'):
    """默认模型选择页：只从可用列表里选，不接受自由输入。"""
    control = _control(kind)
    settings = control.get_settings(menu._ctx(chat_id))
    noun = '图片' if kind == 'image' else '视频'
    names = _available_model_names(chat_id, kind)
    selected = settings.default_model

    pages = max(1, (len(names) + _DEFAULT_MODEL_PAGE_SIZE - 1) // _DEFAULT_MODEL_PAGE_SIZE)
    page = max(0, min(int(page or 0), pages - 1))
    start = page * _DEFAULT_MODEL_PAGE_SIZE
    visible = names[start:start + _DEFAULT_MODEL_PAGE_SIZE]

    lines = [
        f'⭐ <b>{noun}默认模型</b>',
        '',
        f'当前：<code>{ui.escape_html(selected or "未设置（按可用列表自动选择）")}</code>',
        f'共 {len(names)} 个可用模型 · 第 {page + 1}/{pages} 页',
        '',
        '不指定来源时使用该模型；它当前不可用时自动回落到可用列表。',
    ]
    if not names:
        lines.append('')
        lines.append('当前没有可用模型；请先配置图片来源。')

    rows: list[list[dict]] = []
    if selected:
        rows.append([ui.btn('↩ 清除（恢复自动选择）', menu._freeze(
            chat_id, 'media_default_set', kind=kind, model='', revision=settings.revision,
            back_callback=back_callback))])

    def _frozen(model):
        return menu._freeze(chat_id, 'media_default_set', kind=kind, model=model,
                            revision=settings.revision, back_callback=back_callback)

    for name in visible:
        mark = '✅ ' if name == selected else ''
        rows.append([ui.btn(f'{mark}{name}', _frozen(name))])

    if pages > 1:
        pager = []
        if page > 0:
            pager.append(ui.btn('◀ 上页', menu._freeze(
                chat_id, 'media_default_page', kind=kind, page=page - 1,
                back_callback=back_callback)))
        pager.append(ui.btn(f'{page + 1}/{pages}', menu._freeze(
            chat_id, 'media_default_page', kind=kind, page=page, back_callback=back_callback)))
        if page < pages - 1:
            pager.append(ui.btn('下页 ▶', menu._freeze(
                chat_id, 'media_default_page', kind=kind, page=page + 1,
                back_callback=back_callback)))
        rows.append(pager)

    rows.append([ui.btn('◀ 返回' if back_callback != 'menu:main' else '◀ 返回主菜单',
                        menu._freeze(chat_id, 'media_default_back', kind=kind,
                                     back_callback=back_callback))])
    return '\n'.join(lines), ui.inline_kb(rows)


def handle_action(chat_id: int, message_id: int, cb_id: str, action) -> bool:
    name, data = action.name, action.data
    if data.get('provider') == 'antigravity':
        ui.answer_cb(cb_id, 'AG 生图支持已移除', show_alert=True)
        return True
    if name == 'media_default_model':
        kind = data.get('kind', 'image')
        if kind not in ('image', 'video'): return False
        back = str(data.get('back_callback') or 'menu:main')
        menu._show_rendered(chat_id, message_id, cb_id,
                            lambda: _default_model_panel(chat_id, kind, 0, back))
        return True
    if name == 'media_default_page':
        kind = data.get('kind', 'image')
        if kind not in ('image', 'video'): return False
        back = str(data.get('back_callback') or 'menu:main')
        page = int(data.get('page') or 0)
        menu._show_rendered(chat_id, message_id, cb_id,
                            lambda: _default_model_panel(chat_id, kind, page, back))
        return True
    if name == 'media_default_set':
        kind = data.get('kind', 'image')
        if kind not in ('image', 'video'): return False
        back = str(data.get('back_callback') or 'menu:main')
        if not data.get('revision'):
            ui.answer_cb(cb_id, '页面已过期，请重新选择默认模型', show_alert=True)
            return True
        try:
            _control(kind).update_settings(
                menu._ctx(chat_id), {'defaultModel': str(data.get('model') or '')},
                expected_revision=data['revision'])
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        chosen = str(data.get('model') or '')
        ui.answer_cb(cb_id, f'已设为 {chosen}' if chosen else '已恢复自动选择')
        menu._show_rendered(chat_id, message_id, None,
                            lambda: _default_model_panel(chat_id, kind, 0, back))
        return True
    if name == 'media_default_back':
        kind = data.get('kind', 'image')
        if kind not in ('image', 'video'): return False
        back = str(data.get('back_callback') or 'menu:main')
        menu._show_rendered(chat_id, message_id, cb_id, lambda: _panel(chat_id, kind, back))
        return True
    if name in {'media_panel', 'media_patch', 'media_source', 'media_field'}:
        kind = data.get('kind', 'image')
        if kind not in ('image', 'video'): return False
        back = str(data.get('back_callback') or 'menu:main')
        control = _control(kind)
        try:
            if name == 'media_patch':
                control.update_settings(menu._ctx(chat_id), {data['field']: data['target']}, expected_revision=data['revision'])
            elif name == 'media_source':
                control.update_source(menu._ctx(chat_id), data['source_id'], enabled=data['target'], expected_revision=data['revision'])
            elif name == 'media_field':
                prompts = {'cachePath': '请输入缓存目录（不移动或删除历史文件）：', 'cacheRetentionDays': '请输入保留天数（0=永久）：',
                    'cacheMaxBytes': '请输入空间上限，例如 1GB / 500MB / 0（不限）：', 'requestTimeoutSeconds': '请输入请求超时，例如 180s / 3m：',
                    'jobTtlSeconds': '请输入任务 TTL，例如 3h / 1d：'}
                field = data.get('field')
                if field not in prompts: return False
                menu._prompt(chat_id, 'mc_media_field', dict(data), prompts[field], data.get('page_back') or 'mc:list')
                ui.answer_cb(cb_id)
                return True
        except ManagementError as exc:
            menu._answer_error(cb_id, exc)
            return True
        menu._show_rendered(chat_id, message_id, cb_id, lambda: _panel(chat_id, kind, back))
        return True
    legacy = {'image_patch', 'image_account', 'image_independent', 'gpt_media_detail', 'image_input', 'xai_duration',
              'media_manager', 'media_detail', 'media_input', 'media_remove_ask', 'media_remove'}
    if name in legacy:
        kind = data.get('kind') or ('video' if name == 'xai_duration' else 'image')
        menu._show_rendered(chat_id, message_id, cb_id, lambda: _panel(chat_id, kind, 'menu:main'))
        return True
    return False
