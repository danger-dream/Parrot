"""Controller review regressions: real contract gaps, not test-count expansion."""
import copy
import json

import pytest
from fastapi.responses import JSONResponse
from src import search_tool_policy as policy
from src import local_web_tools as web


@pytest.fixture(autouse=True)
def isolated_policy(monkeypatch):
    policy._REPLAY.clear()
    monkeypatch.setattr(web, '_settings', lambda: {'functionMode': 'managed', 'hostedMode': 'managed'})
    monkeypatch.setattr(web, 'max_tool_rounds', lambda: 3)
    async def execute(calls, **kwargs):
        return [web.LocalToolResult(c.id, '{"results": [{"title":"actual result"}]}') for c in calls]
    monkeypatch.setattr(web, 'execute_local_tool_calls', execute)
    yield
    policy._REPLAY.clear()


def test_legacy_function_replay_does_not_replace_another_conversation():
    def history(text):
        return {'model': 'same-model', 'tools': [{'type':'function','function':{'name':'web_search'}}],
                'messages': [{'role':'user','content':text},
                             {'role':'assistant','function_call':{'name':'calculate','arguments':'{}'}}]}
    first, other = history('Conversation A private input'), history('Conversation B different private input')
    policy._remember(first, 'chat', 'shared-app-key', ['calculate'])
    policy._remember(other, 'chat', 'shared-app-key', ['calculate'])
    returned = copy.deepcopy(first)
    returned['messages'].append({'role':'function','name':'calculate','content':'A result'})
    restored = policy.restore_replay(returned, 'chat', 'shared-app-key')
    assert restored['messages'][0]['content'] == 'Conversation A private input'
    assert 'Conversation B' not in json.dumps(restored)


@pytest.mark.asyncio
async def test_managed_usage_includes_hidden_model_rounds():
    request = {'model':'same-model','tools':[{'type':'function','name':'web_search','parameters':{'type':'object'}}],
               'input':[{'role':'user','content':'Find documentation'}]}
    count = 0
    async def invoke(body):
        nonlocal count
        count += 1
        output = [{'type':'function_call','id':'fc_1','call_id':'search_1','name':'web_search','arguments':'{"query":"docs"}'}] if count == 1 else [{'type':'message','role':'assistant','content':[{'type':'output_text','text':'answer'}]}]
        return JSONResponse({'id':'resp_'+str(count),'object':'response','model':'same-model','status':'completed','output':output,
                             'usage':{'input_tokens':12,'output_tokens':5,'total_tokens':17}})
    result = json.loads((await policy.run(request, 'responses', invoke, api_key_name='key')).body)
    assert count == 2
    assert result['usage']['input_tokens'] == 24
    assert result['usage']['output_tokens'] == 10
    assert result['usage']['total_tokens'] == 34


@pytest.mark.asyncio
async def test_chat_choices_continue_in_separate_histories():
    request = {'model':'same-model','n':2,'tools':[{'type':'function','function':{'name':'web_search','parameters':{'type':'object'}}}],
               'messages':[{'role':'user','content':'Give two alternatives'}]}
    count = 0
    async def invoke(body):
        nonlocal count
        count += 1
        if count == 1:
            choices = [{'index':0,'message':{'role':'assistant','tool_calls':[{'id':'search_choice0','type':'function','function':{'name':'web_search','arguments':'{"query":"docs"}'}}]},'finish_reason':'tool_calls'},
                       {'index':1,'message':{'role':'assistant','content':'Already completed independent alternative'},'finish_reason':'stop'}]
        else:
            assert body.get('n', 1) == 1, 'A continuation belongs to one branch, not n fresh alternatives'
            assert 'Already completed independent alternative' not in json.dumps(body['messages'])
            choices = [{'index':0,'message':{'role':'assistant','content':'Completed searched alternative'},'finish_reason':'stop'}]
        return JSONResponse({'id':'chat-'+str(count),'model':'same-model','object':'chat.completion','choices':choices,'usage':{'prompt_tokens':12,'completion_tokens':5,'total_tokens':17}})
    result = json.loads((await policy.run(request, 'chat', invoke, api_key_name='key')).body)
    assert [(c['index'], c['message']['content']) for c in result['choices']] == [(0,'Completed searched alternative'),(1,'Already completed independent alternative')]
