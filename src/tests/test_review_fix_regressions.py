"""Concrete independent-review counterexamples, promoted to regression tests."""
import json
import pytest
from src import config
from src.openai import anthropic_reasoning_replay as native_replay, store
from src.openai.transform import responses_to_anthropic
from src.openai.transform.stream_responses_to_anthropic import StreamTranslator as R2A
from src.openai.transform.stream_r2c import StreamTranslator as R2C
from src.tests.test_order_audit_fixes import event_stream


def message(item_id, texts):
    return {'type': 'message', 'id': item_id, 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text} for text in texts]}


def ev(kind, **fields):
    return {'type': kind, **fields}


@pytest.mark.parametrize('translator_class', [R2A, R2C])
@pytest.mark.parametrize('count', [2, 3, 4])
@pytest.mark.parametrize('completion', ['item', 'terminal', 'parts'])
def test_each_part_drains_before_its_snapshot_without_duplicates(translator_class, count, completion):
    texts = list('ABCD'[:count])
    output = message('m', [t * 2 for t in texts])
    events = [ev('response.output_item.added', output_index=0, item={**output, 'content': []})]
    for i, text in enumerate(texts):
        events.append(ev('response.output_text.delta', output_index=0, item_id='m', content_index=i, delta=text))
        if completion == 'parts':
            events.append(ev('response.output_text.done', output_index=0, item_id='m', content_index=i, text=text * 2))
    if completion != 'terminal':
        events.append(ev('response.output_item.done', output_index=0, item=output))
    events.append(ev('response.completed', response={'status': 'completed', 'output': [output]}))
    translator = translator_class(model='x')
    frames = event_stream(translator, events)
    if translator_class is R2A:
        actual = ''.join(e.get('delta', {}).get('text', '') for e in frames)
        saved = ''.join(b.get('text', '') for b in translator.get_downstream_anthropic_assistant()['content'])
    else:
        actual = ''.join(c.get('delta', {}).get('content', '') for e in frames for c in e.get('choices', []))
        saved = translator.get_downstream_chat_assistant()['content']
    assert actual == saved == ''.join(t * 2 for t in texts)


@pytest.mark.parametrize('translator_class', [R2A, R2C])
def test_item_done_waiting_behind_previous_output_is_released_part_by_part(translator_class):
    first, second, third = message('a', ['AA']), message('b', ['BB', 'CC', 'DD']), message('c', ['E'])
    events = [ev('response.output_text.delta', output_index=0, item_id='a', content_index=0, delta='A')]
    events += [ev('response.output_text.delta', output_index=1, item_id='b', content_index=i, delta=t)
               for i, t in enumerate('BCD')]
    events += [ev('response.output_item.done', output_index=1, item=second),
               ev('response.output_text.delta', output_index=2, item_id='c', content_index=0, delta='E'),
               ev('response.output_item.done', output_index=0, item=first),
               ev('response.completed', response={'status': 'completed', 'output': [first, second, third]})]
    translator = translator_class(model='x')
    frames = event_stream(translator, events)
    actual = (''.join(e.get('delta', {}).get('text', '') for e in frames) if translator_class is R2A else
              ''.join(c.get('delta', {}).get('content', '') for e in frames for c in e.get('choices', [])))
    assert actual == 'AABBCCDDE'


@pytest.mark.parametrize('bridge', ['passthrough', 'drop'])
def test_nonstream_thinking_keeps_each_original_position_and_existing_drop_policy(monkeypatch, bridge):
    monkeypatch.setitem(config.get().setdefault('openai', {}), 'reasoningBridge', bridge)
    saved = []
    monkeypatch.setattr(store, 'is_enabled', lambda: True)
    monkeypatch.setattr(store, 'save', lambda **kw: saved.append(kw))
    source = {'id': 'm', 'model': 'x', 'stop_reason': 'tool_use', 'content': [
        {'type': 'redacted_thinking', 'data': 'not-openai-ciphertext'},
        {'type': 'thinking', 'thinking': 'R1', 'signature': 's1'}, {'type': 'text', 'text': 'A'},
        {'type': 'tool_use', 'id': 'c', 'name': 'Read', 'input': {}},
        {'type': 'thinking', 'thinking': 'R2', 'signature': 's2'}, {'type': 'text', 'text': 'B'},
    ]}
    response = responses_to_anthropic.translate_response(source, model='x', api_key_name='offline', current_input_items=[])
    output = response['output']
    expected = [
        {'type': 'reasoning', 'summary': [],
         'encrypted_content': 'claude-redacted-thinking:not-openai-ciphertext'},
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'R1'}],
         'encrypted_content': 'claude#s1'},
        {'type': 'message', 'role': 'assistant', 'status': 'completed',
         'content': [{'type': 'output_text', 'text': 'A', 'annotations': []}]},
        {'type': 'function_call', 'call_id': 'c', 'name': 'Read', 'arguments': '{}', 'status': 'completed'},
        {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'R2'}],
         'encrypted_content': 'claude#s2'},
        {'type': 'message', 'role': 'assistant', 'status': 'completed',
         'content': [{'type': 'output_text', 'text': 'B', 'annotations': []}]},
    ]
    if bridge == 'drop':
        expected = [item for item in expected if item['type'] != 'reasoning']
    # Only generated IDs vary: native carriers, summaries and tool/text order
    # must match exactly, including the redacted block's empty summary.
    assert [{k: v for k, v in item.items() if k != 'id'} for item in output] == expected
    if bridge == 'passthrough':
        assert [native_replay.decode(output[i]) for i in (0, 1, 4)] == [source['content'][i] for i in (0, 1, 4)]
    assert response['output_text'] == 'AB'
    assert len({i['id'] for i in output}) == len(output)
    assert saved[0]['output_items'] == output
