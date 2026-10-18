"""Complete Chat tool identities before publishing immutable cross-protocol blocks.

Only the tool-bearing suffix is held. Earlier text remains streaming; later
text stays behind the tool it followed. This also gives Anthropic adapters a
complete argument value to conservatively recover before exposing JSON.
"""
from __future__ import annotations

import copy


def merge_identifier_delta(current: str, piece: str) -> str:
    # Some compatible relays repeat/cumulate the full identifier on each chunk.
    if not current or piece.startswith(current):
        return piece
    if piece == current:
        return current
    return current + piece


class ChatToolDeltaBuffer:
    def __init__(self):
        self.calls: dict[int, dict] = {}
        self.append_name_deltas = False
        self.pending: list[tuple[str, object]] = []

    def feed(self, event: dict) -> list[dict]:
        if event.get('error'):
            self.calls.clear()
            self.pending.clear()
            return [event]
        choices = event.get('choices') or []
        if not choices:
            return [event]
        choice = choices[0]
        delta = choice.get('delta') or {}
        calls = delta.get('tool_calls') or []
        if not calls and isinstance(delta.get('function_call'), dict):
            calls = [{'index': 0, 'type': 'function', 'function': delta['function_call']}]
        if not calls and not self.pending:
            return [event]
        result = []
        plain = {key: value for key, value in delta.items() if key not in ('tool_calls', 'function_call')}
        if plain:
            frame = {**event, 'choices': [{**choice, 'delta': plain, 'finish_reason': None}]}
            if self.pending:
                self.pending.append(('event', frame))
            else:
                result.append(frame)
        for call in calls:
            if not isinstance(call, dict):
                continue
            index = int(call.get('index') or 0)
            if index not in self.calls:
                kind = call.get('type') or ('custom' if 'custom' in call else 'function')
                self.calls[index] = {'index': index, 'type': kind, kind: {}}
                self.pending.append(('tool', index))
            slot = self.calls[index]
            if isinstance(call.get('id'), str) and call['id']:
                slot['id'] = merge_identifier_delta(slot.get('id', ''), call['id'])
            kind = slot['type']
            values = call.get(kind) or {}
            target = slot[kind]
            if isinstance(values.get('name'), str) and values['name']:
                target['name'] = (target.get('name', '') + values['name'] if self.append_name_deltas
                                  else merge_identifier_delta(target.get('name', ''), values['name']))
            field = 'input' if kind == 'custom' else 'arguments'
            if isinstance(values.get(field), str):
                target[field] = target.get(field, '') + values[field]
        if choice.get('finish_reason'):
            result.extend(self.flush(choice['finish_reason']))
            result.append({**event, 'choices': [{**choice, 'delta': {}}]})
        return result

    def flush(self, finish_reason=None) -> list[dict]:
        result = []
        for kind, value in self.pending:
            if kind == 'event':
                result.append(value)
            else:
                result.append({'_parrot_tool_finish_reason': finish_reason,
                               'choices': [{'index': 0, 'finish_reason': None,
                                            'delta': {'tool_calls': [copy.deepcopy(self.calls[value])]}}]})
        self.calls.clear()
        self.pending.clear()
        return result
