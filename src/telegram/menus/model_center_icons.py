"""Model-center-only button decoration; never change callbacks or provider icons."""
from __future__ import annotations

import re
import unicodedata
from .. import ui

_LABELS = {
    '对话': '💬', '别名': '🔀', '图片': '🖼', '视频': '🎬', '适配': '🔌',
    '容量': '📐', '能力': '🧩', '价格': '💰',
    '上下文': '📐', '最大输入': '📥', '最大输出': '📤', '压缩阈值': '🗜️',
    '图片输入': '🖼', '工具调用': '🛠️', '结构化输出': '🧱', '思考档位': '🧠',
    '服务档位': '⚡', '知识截止': '📅', '输入价格': '💰', '输出价格': '💰',
    '缓存读取价格': '💰', '缓存写入价格': '💰', '长上下文输入价格': '💰', '长上下文输出价格': '💰',
    '多选': '☑️', '退出多选': '☑️', '全选结果': '☑️', '反选': '🔄', '清空': '🧹',
    '新增别名': '➕', '编辑别名名称': '✏️', '选择真实模型': '🎯',
    '图片设置': '🖼', '视频设置': '🎬',
    '全部来源': '📡', '全部状态': '🚦', '仅可用': '✅', '仅停用': '🚫',
    '对下游隐藏': '🙈', '对下游展示': '👁️',
    '压缩模型': '🗜️', '清除压缩指定': '🧹', '设置为压缩模型': '🗜️',
    '调整元数据': '🧩', '模型通用值': '🌐', '校正目录匹配': '🎯',
    '恢复继承': '♻️', '全部恢复继承': '♻️', '恢复自动匹配 / 继承': '♻️',
    '保留天数': '📅', '设置保留天数': '📅', '空间上限': '💾', '设置空间上限': '💾',
    '缓存目录': '📁', '请求超时': '⏱️', '任务关联时长': '⏳',
    '设置缓存目录': '📁', '设置请求超时': '⏱️', '设置任务 TTL': '⏳', '多媒体日志': '📋',
    '支持': '✅', '不支持': '🚫', '完成': '✅', '保存': '💾', '取消': '❌',
}
_PREFIXES = (
    ('返回', '◀'), ('来源：', '📡'), ('状态：', '🚦'), ('查询', '🔎'),
    ('展示开关：', '👁️'), ('下游展示开关：', '👁️'), ('适配', '🔌'),
    ('加入图片来源', '➕'), ('Max Context', '📐'),
    ('编辑', '✏️'), ('修改', '✏️'), ('调整', '🧩'), ('批量编辑', '✏️'),
    ('选择', '🎯'), ('新增', '➕'), ('添加', '➕'), ('删除', '🗑️'),
    ('移除', '🗑️'), ('确认删除', '🗑️'), ('确认移除', '🗑️'),
    ('确认恢复', '♻️'), ('恢复', '♻️'), ('清除', '🧹'),
    ('同步', '🔄'), ('刷新', '🔄'), ('查看', '🔎'), ('打开', '📂'),
    ('停用', '🚫'), ('禁用', '🚫'), ('启用', '✅'), ('开启', '✅'), ('关闭', '🚫'),
    ('图片接口', '🖼'), ('视频接口', '🎬'), ('缓存', '💾'),
    ('图片缓存', '💾'), ('视频缓存', '💾'),
)
_ACTIONS = {
    'field_edit': '✏️', 'field_bool': '🧩', 'field_inherit': '♻️',
    'metadata_editor': '🧩', 'metadata_targets': '🧩', 'metadata_reset': '♻️',
    'metadata_reset_ask': '♻️', 'matching_picker': '🎯', 'matching_save': '🎯',
    'matching_clear': '♻️', 'max_context': '📐', 'set_status': '🚦',
    'set_source': '📡', 'metadata_sync': '🔄',
    'image_input': '✏️', 'xai_duration_input': '⏱️', 'operation': '⏳',
}


def label_with_icon(text: str, *, action: str = '') -> str:
    marker = ''
    base = text
    for selected in ('✓ ', '☑ ', '☐ '):
        if base.startswith(selected):
            marker, base = selected, base[len(selected):]
            break
    if not base or re.fullmatch(r'\d+(?:/\d+)?', base):
        return text  # compact numbered selectors/pagers deliberately stay compact
    if unicodedata.category(base[0]) in ('So', 'Sk'):
        return text  # already decorated; do not stack icons on rerender/paging
    icon = _LABELS.get(base.removesuffix(' ✎'))
    if not icon:
        icon = next((value for prefix, value in _PREFIXES if base.startswith(prefix)), None)
    icon = icon or _ACTIONS.get(action)
    return marker + icon + ' ' + base if icon else text


def decorate_keyboard(keyboard: dict) -> dict:
    from . import model_center_menu as menu
    rows = []
    for row in keyboard.get('inline_keyboard', []):
        buttons = []
        for original in row:
            button = dict(original)
            if not button.get('icon_custom_emoji_id'):
                callback = str(button.get('callback_data') or '')
                action = None
                if callback.startswith('mc:a:'):
                    # Action lookup is display-only: no state mutation or rebinding.
                    frozen = menu._actions.get(callback.split(':', 2)[2])
                    action = frozen.name if frozen else None
                button['text'] = ui._truncate_btn_label(label_with_icon(str(button.get('text') or ''), action=action or ''))
            buttons.append(button)
        rows.append(buttons)
    return {**keyboard, 'inline_keyboard': rows}


def inline_kb(rows: list[list[dict]]) -> dict:
    return decorate_keyboard(ui.inline_kb(rows))
