"""Lossless, entity-safe continuation pages for model-center HTML bodies.

Only display content is paginated. The original frozen action keyboard and
parent callbacks stay attached to every continuation; no business target is
recomputed when an old content-page button is used.
"""
from __future__ import annotations

import re

from .. import ui
from . import model_center_menu as menu

_LIMIT = 3900
_TOKEN = re.compile(r'<tg-emoji\b[^>]*>.*?</tg-emoji>|<[^>]+>|&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);|.', re.S)
_TAG = re.compile(r'<(/?)([a-zA-Z][a-zA-Z0-9-]*)\b')


def _size(text: str) -> int:
    # Bound both Telegram's UTF-16 units and the raw HTML message length.
    return max(len(text), len(text.encode('utf-16-le')) // 2)


def split_html(text: str, limit: int = _LIMIT) -> tuple[str, ...]:
    if _size(text) <= limit:
        return (text,)
    stack: list[tuple[str, str]] = []
    body = ''
    pages: list[str] = []

    def closing(tags):
        return ''.join(f'</{tag}>' for tag, _ in reversed(tags))

    for match in _TOKEN.finditer(text):
        token = match.group()
        next_stack = list(stack)
        tag = _TAG.match(token)
        if tag and not token.startswith('<tg-emoji'):
            is_end, name = tag.groups()
            if is_end:
                if not next_stack or next_stack[-1][0] != name:
                    raise ValueError('unbalanced model-center HTML')
                next_stack.pop()
            else:
                next_stack.append((name, token))
        if _size(body + token + closing(next_stack)) > limit:
            pages.append(body + closing(stack))
            body = ''.join(opening for _, opening in stack)
        if _size(body + token + closing(next_stack)) > limit:
            raise ValueError('model-center HTML entity exceeds page size')
        body += token
        stack = next_stack
    if stack:
        raise ValueError('unclosed model-center HTML')
    if body:
        pages.append(body)
    return tuple(pages)


def page_render(
    chat_id: int, pages: tuple[str, ...], keyboard: dict, page: int,
    *, draft_id: str | None = None,
) -> tuple[str, dict]:
    page = min(max(0, int(page)), len(pages) - 1)
    if len(pages) == 1:
        return pages[0], keyboard
    def callback(index):
        return menu._freeze(
            chat_id, 'content_page', pages=pages, keyboard=keyboard,
            page=index, draft_id=draft_id,
        )
    row = [
        ui.btn('◀ 内容', callback(page - 1) if page else 'mc:noop'),
        ui.btn(f'正文 {page + 1}/{len(pages)}', 'mc:noop'),
        ui.btn('内容 ▶', callback(page + 1) if page + 1 < len(pages) else 'mc:noop'),
    ]
    return pages[page], {**keyboard, 'inline_keyboard': [row, *keyboard['inline_keyboard']]}


def paged(
    chat_id: int, text: str, keyboard: dict, *, draft_id: str | None = None,
) -> tuple[str, dict]:
    return page_render(chat_id, split_html(text), keyboard, 0, draft_id=draft_id)
