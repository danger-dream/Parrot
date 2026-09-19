from __future__ import annotations

import asyncio
import copy
import json
from contextlib import nullcontext

import httpx
import pytest

from src import search_service as search


@pytest.fixture
def setup(monkeypatch):
    cfg = {"search": {"backends": []}, "oauthAccounts": []}
    monkeypatch.setattr(search.config, "get", lambda: cfg)
    calls = []
    def transport(handler):
        def capture(request):
            calls.append(request)
            return handler(request)
        monkeypatch.setattr(search.network, "async_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    return cfg, calls, transport


def backend(kind, **kw):
    return {**search.default_backend(kind), "apiKeys": ["private-test-key"], **kw}


def test_legacy_read_only_migration_and_optout(setup):
    cfg, _, _ = setup
    cfg.pop("search")
    cfg["anysearch"] = {"enabled": False, "endpoint": "https://api.anysearch.com/mcp", "apiKey": "secret"}
    before = copy.deepcopy(cfg)
    settings = search.settings()
    assert settings["functionMode"] == settings["hostedMode"] == "passthrough"
    assert settings["timeoutSeconds"] == 10 and settings["maxAttempts"] == 3
    assert settings["backends"][0]["endpoint"] == "https://api.anysearch.com"
    assert settings["backends"][0]["apiKeys"] == ["secret"]
    assert cfg == before
    assert "secret" not in json.dumps(search.backend_statuses())


def test_backend_readiness_respects_disabled_oauth_and_empty_keys(setup):
    cfg, _, _ = setup
    cfg["search"]["backends"] = [backend("tavily"), backend("openai"), backend("anthropic")]
    cfg["oauthAccounts"] = [{"provider": "openai", "email": "private@example.test", "access_token": "token", "enabled": False}]
    statuses = search.backend_statuses()
    assert [s["available"] for s in statuses] == [True, False, False]
    assert statuses[2]["verified"] is False
    cfg["search"]["backends"][1]["allowDisabledAccounts"] = True
    assert search.backend_statuses()[1]["available"] is True
    assert cfg["oauthAccounts"][0]["enabled"] is False
    assert "token" not in json.dumps(search.backend_statuses())
    cfg["oauthAccounts"][0]["disabled_reason"] = "auth_error"
    assert search.backend_statuses()[1]["available"] is False
    assert search.backend_statuses()[1]["reason"] == "no_eligible_accounts"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,response", [
    ("anysearch", {"code": 0, "data": {"results": [{"title": "Python", "url": "https://docs.python.org/3", "snippet": "text"}]}}),
    ("tavily", {"results": [{"title": "Python", "url": "https://docs.python.org/3", "content": "text"}]}),
    ("exa", {"results": [{"title": "Python", "url": "https://docs.python.org/3", "text": "text"}], "costDollars": {"total": .01}}),
    ("brave", {"web": {"results": [{"title": "Python", "url": "https://docs.python.org/3", "description": "<strong>text</strong>"}]}}),
])
async def test_real_response_shapes_normalized(setup, kind, response):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend(kind)]
    install(lambda r: httpx.Response(200, json=response))
    result = await search.search({"query": "Python", "max_results": 3, "allowed_domains": ["docs.python.org"]})
    assert result["results"] == [{"title": "Python", "url": "https://docs.python.org/3", "snippet": "text"}]
    assert result["provider"] == kind
    assert len(calls) == 1 and "private-test-key" not in json.dumps(result)
    if kind == "anysearch":
        assert calls[0].url.path == "/v1/search"
        assert "jsonrpc" not in json.loads(calls[0].content)
    if kind == "tavily": assert calls[0].headers["authorization"] == "Bearer private-test-key"
    if kind == "exa": assert calls[0].headers["x-api-key"] == "private-test-key"
    if kind == "brave": assert calls[0].headers["x-subscription-token"] == "private-test-key"


@pytest.mark.asyncio
async def test_priority_keys_attempts_and_safe_errors(setup):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend("tavily", apiKeys=["one", "two"]), backend("exa", apiKeys=["three"])]
    install(lambda r: httpx.Response(401, json={"error": "private upstream request with token"}) if r.url.host == "api.tavily.com"
            else httpx.Response(200, json={"results": []}))
    result = await search.search({"query": "hello"})
    assert [r.headers.get("authorization") or r.headers.get("x-api-key") for r in calls] == ["Bearer one", "Bearer two", "three"]
    assert [a["provider"] for a in result["attempts"]] == ["tavily", "tavily", "exa"]
    assert "private upstream" not in json.dumps(result)
    calls.clear()
    install(lambda r: httpx.Response(429, json={"error": "private-test-key"}))
    with pytest.raises(search.SearchError) as exc:
        await search.search({"query": "hello"})
    assert len(calls) == 3
    assert "private-test-key" not in str(exc.value)


@pytest.mark.asyncio
async def test_backend_test_never_fails_over_to_other_provider(setup):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend("tavily"), backend("exa")]
    install(lambda r: httpx.Response(401))
    with pytest.raises(search.SearchError):
        await search.search({"query": "hello"}, backend_id="tavily")
    assert len(calls) == 1 and calls[0].url.host == "api.tavily.com"


@pytest.mark.asyncio
async def test_anysearch_http200_error_is_not_success(setup):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend("anysearch")]
    install(lambda r: httpx.Response(200, json={"code": 100, "message": "secret upstream"}))
    with pytest.raises(search.SearchError) as exc:
        await search.search({"query": "hello"})
    assert len(calls) == 3 and "secret upstream" not in str(exc.value)


@pytest.mark.asyncio
async def test_constraints_do_not_silently_allow_offline_or_wrong_domains(setup):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend("brave")]
    install(lambda r: httpx.Response(200, json={"web": {"results": [
        {"url": "https://example.com.evil.test", "title": "wrong"},
        {"url": "https://no.example.com", "title": "blocked"},
        {"url": "https://www.example.com", "title": "right"}]}}))
    with pytest.raises(search.SearchError, match="离线"):
        await search.search({"query": "hello", "external_web_access": False})
    assert calls == []
    result = await search.search({"query": "hello", "allowed_domains": ["example.com"], "blocked_domains": ["no.example.com"]})
    assert [r["title"] for r in result["results"]] == ["right"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,response", [
    ("anysearch", {"code": 0, "data": {"content": "page"}}),
    ("tavily", {"results": [{"raw_content": "page"}]}),
    ("exa", {"results": [{"text": "page"}]}),
])
async def test_extract_result_contract(setup, kind, response):
    cfg, _, install = setup
    cfg["search"]["backends"] = [backend(kind)]
    install(lambda r: httpx.Response(200, json=response))
    result = await search.extract({"url": "https://example.com"})
    assert result["content"] == "page"
    assert result["url"] == "https://example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1", "http://[::1]", "https://[bad", "http://u:p@example.com"])
async def test_extract_rejects_local_and_credential_urls_before_dispatch(setup, url):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend("anysearch")]
    install(lambda r: httpx.Response(200))
    with pytest.raises(search.SearchError): await search.extract({"url": url})
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('constraints', [
    {'allowed_domains': ['example.com']},
    {'blocked_domains': ['other.test']},
    {'blocked_domains': [], 'excluded_domains': ['other.test']},
    {'filters': {'allowed_domains': ['example.com']}},
])
async def test_extract_domain_constraints_apply_before_network(setup, constraints):
    cfg, calls, install = setup
    cfg['search']['backends'] = [backend('tavily')]
    install(lambda r: httpx.Response(200, json={'results': [{'raw_content': 'not allowed'}]}))
    with pytest.raises(search.SearchError) as exc:
        await search.extract({'url': 'https://other.test/page', **constraints})
    assert exc.value.code == 'url_not_allowed'
    assert calls == []


@pytest.mark.asyncio
async def test_extract_reported_redirect_cannot_bypass_domain_constraint(setup):
    cfg, calls, install = setup
    cfg['search']['backends'] = [backend('tavily')]
    install(lambda r: httpx.Response(200, json={'results': [
        {'url': 'https://blocked.example.com/page', 'raw_content': 'excluded source'}]}))
    with pytest.raises(search.SearchError) as exc:
        await search.extract({'url': 'https://example.com/page', 'blocked_domains': ['blocked.example.com']})
    assert exc.value.code == 'url_not_allowed'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_total_attempt_deadline_and_cancellation(setup, monkeypatch):
    cfg, _, _ = setup
    cfg["search"].update(backends=[backend("tavily")], timeoutSeconds=.01, maxAttempts=3)
    n = 0
    async def slow(*args):
        nonlocal n
        n += 1
        await asyncio.sleep(.1)
    monkeypatch.setattr(search, "_http_adapter", slow)
    with pytest.raises(search.SearchError) as exc: await search.search({"query": "hello"})
    assert exc.value.code == "search_timeout" and n == 3
    async def cancel(*args): raise asyncio.CancelledError()
    monkeypatch.setattr(search, "_http_adapter", cancel)
    with pytest.raises(asyncio.CancelledError): await search.search({"query": "hello"})

@pytest.mark.asyncio
async def test_openai_wire_omits_empty_domains_and_normalizes_results(setup, monkeypatch):
    from src import oauth_manager
    cfg, calls, install = setup
    cfg['search']['backends'] = [backend('tavily'), backend('openai')]
    from src.openai.codex_constants import current_codex_protocol_profile
    profile = current_codex_protocol_profile()
    cfg['openaiOAuth'] = {'codexCliVersion': profile.client_version, 'codexProtocolProfile': profile.profile_id}
    account = {'provider':'openai','email':'test@example.test','workspace_id':'test-workspace','access_token':'private-token'}
    cfg['oauthAccounts'] = [account]
    async def token(*a, **k): return 'private-token'
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    monkeypatch.setattr(oauth_manager, 'account_state_key', lambda a: 'test-generation')
    monkeypatch.setattr(oauth_manager, 'account_generation_guard', lambda state: nullcontext(True))
    monkeypatch.setattr(oauth_manager, 'get_account', lambda k: account)
    install(lambda r: httpx.Response(200, json={'output':'text', 'results':[{'url':'https://example.com','title':'source','snippet':'real'}]}))
    location = {'type': 'approximate', 'city': 'Kunming', 'country': 'CN'}
    result = await search.search({'query':'hello','allowed_domains':['example.com'], 'external_web_access': False,
                                  'user_location': location, 'search_context_size': 'high'})
    assert len(calls) == 1 and calls[0].url.path.endswith('/alpha/search')
    body = json.loads(calls[0].content)
    assert body['settings']['external_web_access'] is False
    assert body['settings']['user_location'] == location
    assert body['settings']['search_context_size'] == 'high'
    assert 'context_budget' not in result
    assert body['settings']['filters'] == {'allowed_domains':['example.com']}
    assert body['commands']['search_query'][0]['domains'] == ['example.com']
    assert calls[0].headers['chatgpt-account-id'] == 'test-workspace'
    assert result['results'][0]['snippet'] == 'real'


@pytest.mark.asyncio
async def test_xai_search_sse_keeps_plain_text_source_fallback_and_requires_terminal(setup, monkeypatch):
    from src import oauth_manager
    cfg, calls, install = setup
    cfg['search'].update(backends=[backend('xai')], maxAttempts=1)
    account = {'provider':'xai','email':'test@example.test','subject':'test-sub','access_token':'private-token'}
    cfg['oauthAccounts'] = [account]
    async def token(*a, **k): return 'private-token'
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    monkeypatch.setattr(oauth_manager, 'account_state_key', lambda a: 'test-generation')
    monkeypatch.setattr(oauth_manager, 'account_generation_guard', lambda state: nullcontext(True))
    final = {'type':'response.completed','response':{'output':[
        {'type':'web_search_call','action':{'sources':[{'url':'https://example.com/second'}]}},
        {'type':'message','content':[{'type':'output_text','text':'answer','annotations':[
            {'type':'url_citation','url':'https://example.com/first','title':'first'},
            {'type':'url_citation','url':'https://example.com/second','title':'second'}]}]}], 'usage':{'total_tokens':20}}}
    install(lambda r: httpx.Response(200, text='data: '+json.dumps(final)+'\n\n',headers={'content-type':'text/event-stream'}))
    result = await search.search({'query':'hello'})
    assert [r['title'] for r in result['results']] == ['first','second']
    assert all('snippet' not in row for row in result['results'])
    assert result['answer'] == 'answer'
    assert result['usage'] == {'total_tokens':20}
    body = json.loads(calls[0].content)
    assert body['max_tool_calls'] == 1 and body['tool_choice'] == 'required'
    prompt = body['input'][0]['content']
    assert 'results array of at most 8 objects' in prompt
    assert 'otherwise omit snippet' in prompt and 'Do not invent snippets or URLs' in prompt
    install(lambda r: httpx.Response(200, text='data: {"type":"response.created"}\n\n'))
    with pytest.raises(search.SearchError) as exc: await search.search({'query':'hello'})
    assert exc.value.code == 'incomplete_search_response'


def _xai_parse_args(**updates):
    args = {'query': 'hello', 'max_results': 8, 'allowed_domains': [], 'blocked_domains': []}
    args.update(updates)
    return args


def test_xai_structured_results_align_title_url_and_snippet_to_native_sources():
    envelope = {'answer': 'grounded answer', 'results': [
        {'title': 'Second title', 'url': 'https://example.test/second'},
        {'title': 'Not searched', 'url': 'https://invented.test/page', 'snippet': 'Invented summary'},
        {'title': 'First title', 'url': 'https://example.test/first', 'snippet': 'First summary'},
    ]}
    data = {'output': [
        {'type': 'web_search_call', 'action': {'sources': [
            {'url': 'https://example.test/first'},
            {'url': 'https://example.test/second', 'description': 'Second native summary'}]}},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(envelope)}]},
    ]}
    result = search._parse_oauth_result(data, 'xai', _xai_parse_args(), 'search', {})
    assert result['answer'] == 'grounded answer'
    assert result['results'] == [
        {'title': 'Second title', 'url': 'https://example.test/second', 'snippet': 'Second native summary'},
        {'title': 'First title', 'url': 'https://example.test/first', 'snippet': 'First summary'},
    ]


def test_xai_structured_results_keep_missing_snippet_and_domain_count_rules():
    envelope = {'answer': 'answer', 'results': [
        {'title': 'Wrong domain', 'url': 'https://other.test/result', 'snippet': 'Other'},
        {'title': 'First allowed', 'url': 'https://docs.example.test/first'},
        {'title': 'Second allowed', 'url': 'https://docs.example.test/second', 'snippet': 'Second'},
    ]}
    sources = [{'url': row['url']} for row in envelope['results']]
    data = {'output': [
        {'type': 'web_search_call', 'action': {'sources': sources}},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': '```json\n' + json.dumps(envelope) + '\n```'}]},
    ]}
    args = _xai_parse_args(max_results=1, allowed_domains=['example.test'],
                           blocked_domains=['blocked.example.test'])
    result = search._parse_oauth_result(data, 'xai', args, 'search', {})
    assert result['results'] == [{'title': 'First allowed', 'url': 'https://docs.example.test/first'}]


@pytest.mark.asyncio
async def test_locale_preferences_survive_non_native_adapter(setup):
    cfg, calls, install = setup
    cfg['search'].update(backends=[backend('exa')], language='zh-CN', country='CN')
    install(lambda r: httpx.Response(200, json={'results':[]}))
    result = await search.search({'query':'Python'})
    assert 'preferred language: zh-CN' in json.loads(calls[0].content)['query']
    assert 'region: CN' in json.loads(calls[0].content)['query']
    assert result['warnings'] == ['locale_preferences_applied_to_query_not_a_hard_filter']

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['anysearch', 'tavily', 'exa', 'brave'])
async def test_context_and_approximate_location_are_adapted_not_dropped(setup, kind):
    cfg, calls, install = setup
    cfg['search']['backends'] = [backend(kind)]
    row = {'title': 'Source', 'url': 'https://example.com/result', 'snippet': '中' * 8000}
    payload = {'results': [row], 'answer': 'extra answer'}
    if kind == 'anysearch': payload = {'code': 0, 'data': payload}
    if kind == 'brave': payload = {'web': payload}
    install(lambda request: httpx.Response(200, json=payload))
    result = await search.search({'query': 'local museums', 'search_context_size': 'low',
                                  'user_location': {'type': 'approximate', 'city': 'Kunming', 'country': 'CN'}})
    query = calls[0].url.params['q'] if kind == 'brave' else json.loads(calls[0].content)['query']
    assert 'city=Kunming' in query and 'country=CN' in query
    assert result['context_budget'] == {'requested': 'low', 'method': 'parrot_returned_text_char_cap', 'max_chars': 4000}
    assert len(result['results'][0]['snippet']) == 4000 and result['truncated'] is True
    assert result['results'][0]['url'] == row['url']
    assert len(result['warnings']) == 2
    assert len(calls) == 1  # Adaptation does not add retrieval calls/cost.


@pytest.mark.asyncio
@pytest.mark.parametrize('option', [
    {'search_context_size': 'arbitrary'}, {'search_context_size': []},
    {'user_location': 'CN'}, {'user_location': {'type': 'precise'}},
    {'user_location': {'city': ['invalid']}},
])
async def test_invalid_declared_preferences_never_dispatch(setup, option):
    cfg, calls, install = setup
    cfg['search']['backends'] = [backend('tavily')]
    install(lambda request: httpx.Response(200, json={'results': []}))
    with pytest.raises(search.SearchError) as exc:
        await search.search({'query': 'hello', **option})
    assert exc.value.code == 'invalid_search_input' and not calls


@pytest.mark.asyncio
async def test_retired_oauth_identity_is_not_dispatched(setup, monkeypatch):
    from src import oauth_manager
    cfg, calls, install = setup
    cfg['search']['backends'] = [backend('xai')]
    cfg['oauthAccounts'] = [{'provider':'xai','email':'old@example.test','subject':'old','access_token':'old-token'}]
    async def token(key, **kw):
        assert kw['expected_state_key'] == 'retired-generation'
        return 'old-token'
    monkeypatch.setattr(oauth_manager, 'ensure_valid_token', token)
    monkeypatch.setattr(oauth_manager, 'account_state_key', lambda a: 'retired-generation')
    monkeypatch.setattr(oauth_manager, 'account_generation_guard', lambda state: nullcontext(False))
    install(lambda r: httpx.Response(200))
    with pytest.raises(search.SearchError) as exc: await search.search({'query':'hello'})
    assert exc.value.code == 'search_account_retired'
    assert calls == []
