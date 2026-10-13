"""Responses → Claude request and Responses return, with no real upstream calls."""
import asyncio
import json
import uuid

import pytest

from src import config
from src.openai import store
from src.openai.transform import responses_to_anthropic as bridge
from src.openai.transform.guard import GuardError
from src.channel.api_channel import ApiChannel
from src.openai.transform.stream_anthropic_to_responses import StreamTranslator


def wire(*events, crlf=False):
    nl = '\r\n' if crlf else '\n'
    return ''.join('event: ' + e['type'] + nl + 'data: ' + json.dumps(e, ensure_ascii=False) + nl + nl for e in events).encode()


def events(chunks):
    return [json.loads(line[6:]) for line in b''.join(chunks).decode().splitlines() if line.startswith('data: ')]


def test_format_and_effort_preserved_through_bridge():
    schema = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer']}
    out = bridge.translate_request({'model': 'claude-sonnet-4-6', 'input': 'hello', 'max_output_tokens': 100,
        'text': {'format': {'type': 'json_schema', 'name': 'Answer', 'schema': schema, 'strict': True}},
        'reasoning': {'effort': 'high'}})
    assert out['output_config'] == {'format': {'type': 'json_schema', 'schema': schema}, 'effort': 'high'}
    assert out['thinking'] == {'type': 'adaptive'}
    assert 'response_format' not in out and 'reasoning_effort' not in out
    assert bridge.translate_request({'model': 'claude-sonnet-4-6', 'input': 'x', 'reasoning': {'effort': 'none'}})['thinking'] == {'type': 'disabled'}


def test_legacy_effort_and_incompatible_sampling_fall_back_without_upstream_400():
    out = bridge.translate_request({'model': 'public-alias', 'input': 'x', 'max_output_tokens': 3000,
        'reasoning': {'effort': 'high'}, 'temperature': 0.3, 'top_p': 0.5},
        target_model='claude-sonnet-4-5')
    assert out['thinking'] == {'type': 'enabled', 'budget_tokens': 2999}
    assert 'temperature' not in out and 'top_p' not in out
    forced = bridge.translate_request({'model': 'public-alias', 'input': 'x', 'max_output_tokens': 3000,
        'reasoning': {'effort': 'high'}, 'tool_choice': {'type': 'function', 'name': 'f'},
        'tools': [{'type': 'function', 'name': 'f', 'parameters': {'type': 'object'}}]},
        target_model='claude-sonnet-4-5')
    assert forced['tool_choice'] == {'type': 'tool', 'name': 'f'}
    assert 'thinking' not in forced


@pytest.mark.parametrize('model,effort,expected', [
    ('claude-sonnet-4-6', 'xhigh', {'type': 'adaptive', 'effort': 'high'}),
    ('claude-sonnet-4-6', 'max', {'type': 'adaptive', 'effort': 'high'}),
    ('claude-opus-4-6', 'xhigh', {'type': 'adaptive', 'effort': 'max'}),
    ('claude-3-5-haiku', 'high', None),
    ('claude-3-5-haiku', 'none', None),
    ('claude-3-5-sonnet', 'high', None),
    ('claude-3-7-sonnet', 'high', {'type': 'enabled', 'budget_tokens': 2999}),
])
def test_claude_model_specific_reasoning_capability(model, effort, expected):
    out = bridge.translate_request({'model': model, 'input': 'go', 'max_output_tokens': 3000,
        'reasoning': {'effort': effort}})
    if expected is None:
        assert 'thinking' not in out and 'output_config' not in out
    else:
        assert out['thinking']['type'] == expected['type']
        if 'budget_tokens' in expected:
            assert out['thinking']['budget_tokens'] == expected['budget_tokens']
            assert 'output_config' not in out
        else:
            assert out['output_config']['effort'] == expected['effort']


def test_api_channel_uses_resolved_claude_model_for_reasoning():
    channel = ApiChannel({'name': 'anth-legacy', 'baseUrl': 'https://api.example.com', 'apiKey': 'dummy',
        'protocol': 'anthropic', 'cc_mimicry': False,
        'models': [{'alias': 'public-alias', 'real': 'claude-sonnet-4-5'}]})
    request = asyncio.run(channel.build_upstream_request({
        'model': 'public-alias', 'input': 'x', 'max_output_tokens': 3000,
        'reasoning': {'effort': 'high'}}, 'claude-sonnet-4-5', ingress_protocol='responses'))
    outgoing = json.loads(request.body)
    assert outgoing['thinking'] == {'type': 'enabled', 'budget_tokens': 2999}
    assert outgoing['model'] == 'claude-sonnet-4-5'


def test_strict_namespace_and_allowed_choice():
    plan = bridge.NamespaceToolMap()
    out = bridge.translate_request({'model': 'm', 'input': 'x', 'tools': [{'type': 'namespace', 'name': 'db', 'tools': [
        {'type': 'function', 'name': 'lookup', 'parameters': {'type': 'object'}, 'strict': True},
        {'type': 'function', 'name': 'insert', 'parameters': {'type': 'object'}, 'strict': False}]}],
        'tool_choice': {'type': 'allowed_tools', 'mode': 'required', 'tools': [{'type': 'function', 'namespace': 'db', 'name': 'lookup'}]}},
        namespace_tool_map=plan)
    assert [x['name'] for x in out['tools']] == ['db__lookup']
    assert out['tools'][0]['strict'] is True
    assert out['tool_choice'] == {'type': 'any'}
    assert bridge.translate_request({'model': 'm', 'input': 'x', 'tools': [{'type': 'function', 'name': 'f', 'parameters': {'type': 'object'}, 'strict': False}]})['tools'][0]['strict'] is False


def test_text_custom_tool_roundtrip_and_grammar_rejection():
    plan = bridge.NamespaceToolMap()
    payload = bridge.translate_request({'model': 'm', 'input': [
        {'type': 'custom_tool_call', 'call_id': 'call_old', 'name': 'shell', 'input': 'print(\"hi\")'},
        {'type': 'custom_tool_call_output', 'call_id': 'call_old', 'output': 'hi'}],
        'tools': [{'type': 'custom', 'name': 'shell', 'format': {'type': 'text'}}],
        'tool_choice': {'type': 'custom', 'name': 'shell'}}, namespace_tool_map=plan)
    assert payload['messages'][0]['content'][0]['input'] == {'__parrot_raw_input': 'print(\"hi\")'}
    literal_json = bridge.translate_request({'model': 'm', 'input': [
        {'type': 'custom_tool_call', 'call_id': 'json', 'name': 'shell', 'input': '{"cmd":"pwd"}'}],
        'tools': [{'type': 'custom', 'name': 'shell', 'format': {'type': 'text'}}]})
    assert literal_json['messages'][0]['content'][0]['input'] == {'__parrot_raw_input': '{"cmd":"pwd"}'}
    assert payload['tools'][0]['strict'] is True
    assert payload['tool_choice'] == {'type': 'tool', 'name': 'shell'}
    resp = bridge.translate_response({'content': [{'type': 'tool_use', 'id': 'call_new', 'name': 'shell',
        'input': {'__parrot_raw_input': 'hello\n🌙'}}], 'stop_reason': 'tool_use', 'usage': {}},
        model='m', namespace_tool_map=plan)
    assert resp['output'][0]['type'] == 'custom_tool_call'
    assert resp['output'][0]['input'] == 'hello\n🌙'
    with pytest.raises(ValueError, match='raw-text wrapper'):
        bridge.translate_response({'content': [{'type': 'tool_use', 'id': 'bad', 'name': 'shell',
            'input': {'wrong': 'shape'}}], 'stop_reason': 'tool_use', 'usage': {}},
            model='m', namespace_tool_map=plan)
    with pytest.raises(GuardError, match='grammar'):
        bridge.translate_request({'model': 'm', 'input': 'x', 'tools': [{'type': 'custom', 'name': 'f',
            'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: "a"'}}]})


def test_stream_custom_chunked_crlf_and_end_state():
    plan = bridge.NamespaceToolMap()
    bridge.translate_request({'model': 'm', 'input': 'go', 'tools': [{'type': 'custom', 'name': 'shell',
        'format': {'type': 'text'}}]}, namespace_tool_map=plan)
    tr = StreamTranslator(model='m', namespace_tool_map=plan)
    raw = wire({'type': 'message_start', 'message': {'model': 'm', 'usage': {'input_tokens': 4}}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'call_1', 'name': 'shell', 'input': {}}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"__parrot_raw_'}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': 'input":"hello\\n🌙"}'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 5}},
        {'type': 'message_stop'}, crlf=True)
    out = events([chunk for b in raw for chunk in tr.feed(bytes([b]))] + list(tr.close()))
    assert out[-1]['type'] == 'response.completed'
    assert out[-1]['response']['usage']['output_tokens'] == 5
    assert out[-1]['response']['output'][0]['input'] == 'hello\n🌙'
    assert [e['delta'] for e in out if e['type'] == 'response.custom_tool_call_input.delta'] == ['hello\n🌙']
    assert all('encrypted_content' not in str(e) for e in out)


def test_stream_invalid_custom_wrapper_is_failed_not_completed():
    plan = bridge.NamespaceToolMap()
    bridge.translate_request({'model': 'm', 'input': 'go',
        'tools': [{'type': 'custom', 'name': 'shell', 'format': {'type': 'text'}}]},
        namespace_tool_map=plan)
    tr = StreamTranslator(model='m', namespace_tool_map=plan)
    out = events(list(tr.feed(wire(
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': 'c', 'name': 'shell', 'input': {}}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': '{"wrong":"shape"}'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}}, {'type': 'message_stop'}))) + list(tr.close()))
    assert out[-1]['type'] == 'response.failed'
    assert out[-1]['response']['output'][0]['status'] == 'in_progress'
    assert not any(e['type'] == 'response.custom_tool_call_input.done' for e in out)


@pytest.mark.parametrize('mode', ['passthrough', 'drop'])
def test_local_previous_response_id_drops_unreplayable_reasoning_without_losing_tools(monkeypatch, mode):
    monkeypatch.setitem(config.get().setdefault('openai', {}), 'reasoningBridge', mode)
    store.init()  # isolated_pytest supplies a private temporary DATA_DIR.
    response_id = 'resp_opaque_' + uuid.uuid4().hex
    key_name = 'key_opaque_' + uuid.uuid4().hex
    store.save(response_id, None, api_key_name=key_name, model='m', channel_key='anthropic:test',
        input_items=[{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'first'}]}],
        output_items=[
            {'type': 'reasoning', 'id': 'rs1', 'encrypted_content': 'opaque-sensitive-token',
             'summary': [{'type': 'summary_text', 'text': 'Read the file first'}]},
            {'type': 'reasoning', 'id': 'rs2', 'encrypted_content': 'opaque-only-token'},
            {'type': 'function_call', 'call_id': 'call_keep', 'name': 'read', 'arguments': '{"path":"a"}'},
        ])
    request = {'model': 'm', '_api_key_name': key_name, 'previous_response_id': response_id,
        'input': [{'type': 'function_call_output', 'call_id': 'call_keep', 'output': 'file bytes'},
                  {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'finish'}]}],
        'tools': [{'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}}]}
    out = bridge.translate_request(request, api_key_name=key_name)
    assert [msg['role'] for msg in out['messages']] == ['user', 'assistant', 'user']
    assert out['messages'][0]['content'] == [{'type': 'text', 'text': 'first'}]
    assistant = out['messages'][1]['content']
    assert assistant[-1] == {'type': 'tool_use', 'id': 'call_keep', 'name': 'read', 'input': {'path': 'a'}}
    assert len(assistant) == 1
    assert 'Read the file first' not in json.dumps(out)
    followup = out['messages'][2]['content']
    assert followup[0] == {'type': 'tool_result', 'tool_use_id': 'call_keep', 'content': 'file bytes'}
    assert followup[1] == {'type': 'text', 'text': 'finish'}
    assert 'opaque-sensitive-token' not in json.dumps(out)
    assert 'opaque-only-token' not in json.dumps(out)
    # Exercise the actual shared Anthropic-channel request construction too:
    # it must use the same Store history and must not re-guard ciphertext.
    channel = ApiChannel({'name': 'anth-history', 'baseUrl': 'https://api.example.com', 'apiKey': 'dummy',
        'protocol': 'anthropic', 'cc_mimicry': False,
        'models': [{'alias': 'm', 'real': 'claude-sonnet-4-6'}]})
    upstream = asyncio.run(channel.build_upstream_request(request, 'claude-sonnet-4-6', ingress_protocol='responses'))
    sent = json.loads(upstream.body)
    assert [message['role'] for message in sent['messages']] == ['user', 'assistant', 'user']
    assert sent['messages'][1]['content'] == out['messages'][1]['content']
    assert sent['messages'][2]['content'][0] == followup[0]
    assert sent['messages'][2]['content'][1]['text'] == 'finish'
    assert 'opaque-sensitive-token' not in json.dumps(sent)
    assert 'opaque-only-token' not in json.dumps(sent)


def test_stream_missing_stop_fails_and_max_tokens_is_incomplete():
    tr = StreamTranslator(model='m')
    out = events(list(tr.feed(wire({'type': 'message_start', 'message': {}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': 'kept'}}))) + list(tr.close()))
    assert out[-1]['type'] == 'response.failed'
    assert out[-1]['response']['output_text'] == 'kept'
    tr = StreamTranslator(model='m')
    out = events(list(tr.feed(wire({'type': 'message_start', 'message': {}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': 'cut'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}}, {'type': 'message_stop'}))) + list(tr.close()))
    assert out[-1]['type'] == 'response.incomplete'
    assert out[-1]['response']['incomplete_details'] == {'reason': 'max_output_tokens'}
    assert out[-1]['response']['output_text'] == 'cut'
