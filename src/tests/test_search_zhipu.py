"""Zhipu MCP wire, credential selection, management and public search integration."""
from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from src import log_db, oauth_manager, search_service as search
from src.mcp import catalog
from src.oauth_ids import account_key
from src.tests.test_search_service import setup, backend  # noqa: F401
from src.tests.test_search_call_log import search_log, _rows  # noqa: F401
from src.tests.test_search_management import (  # noqa: F401
    memory, ctx, control, tg, buttons, callback, latest, search_workers,
)
from src.tests.test_zhipu_provider import credential


def wire(install, *, sse=True, result=None, tool_name="web_search_prime", fail=None):
    rows = [{"title": "Python", "link": "https://docs.python.org/3/", "content": "Documentation"},
            {"title": "Other", "link": "https://other.test/", "content": "other"}]
    result = result if result is not None else {"content": [{"type": "text", "text": json.dumps(json.dumps(rows))}]}

    def handle(request):
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            data = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}
        else:
            assert request.headers["Mcp-Session-Id"] == "session-1"
            assert request.headers["MCP-Protocol-Version"] == "2024-11-05"
            if method == "notifications/initialized":
                assert "id" not in body
                return httpx.Response(202)
            if method == "tools/list":
                data = {"tools": [{"name": tool_name}]}
            else:
                assert method == "tools/call"
                assert body["params"]["name"] == tool_name
                if fail:
                    return fail(request, body)
                data = result
        envelope = {"jsonrpc": "2.0", "id": body["id"], "result": data}
        headers = {"Mcp-Session-Id": "session-1"}
        if sse:
            headers["content-type"] = "text/event-stream"
            return httpx.Response(200, headers=headers, text=': keepalive\r\n\r\ndata: {"jsonrpc":"2.0","method":"notice"}\r\n\r\nevent: message\r\ndata: ' + json.dumps(envelope) + '\r\n\r\n')
        return httpx.Response(200, headers=headers, json=envelope)
    install(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("sse,tool_name", [(True, "web_search_prime"), (False, "webSearchPrime")])
async def test_search_wire_normalization_and_accounting(setup, search_log, sse, tool_name):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend("zhipu")]
    wire(install, sse=sse, tool_name=tool_name)
    result = await search.search({"query": "Python 文档", "allowed_domains": ["python.org"], "freshness": "week", "max_results": 1})
    assert result["results"] == [{"title": "Python", "url": "https://docs.python.org/3/", "snippet": "Documentation"}]
    assert len(calls) == 4
    params = json.loads(calls[-1].content)["params"]["arguments"]
    assert params == {"search_query": "Python 文档", "search_domain_filter": "python.org", "search_recency_filter": "oneWeek"}
    assert all(r.url.host == "open.bigmodel.cn" and r.url.path == "/api/mcp/web_search_prime/mcp" for r in calls)
    assert all(r.headers["authorization"] == "Bearer private-test-key" for r in calls)
    assert "private-test-key" not in json.dumps(result)
    rows = _rows(next(iter(log_db._write_conn_registry)))
    assert len(rows) == 1 and rows[0]["source_type"] == "zhipu" and rows[0]["status"] == "success"
    assert rows[0]["cost_source"] == "unpriced" and not rows[0]["usage_observed"]
    assert catalog._search_sources("web_search")[0] == ["zhipu"]
    assert catalog._search_sources("web_fetch")[0] == ["zhipu"]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["plain", "json", "structured"])
async def test_reader_truncation(setup, shape):
    cfg, calls, install = setup
    cfg["search"].update(backends=[backend("zhipu", endpoint="https://api.z.ai")], maxFetchChars=7)
    content = "Example Domain body"
    if shape == "plain":
        value = {"content": [{"type": "text", "text": content}]}
    elif shape == "json":
        value = {"content": [{"type": "text", "text": json.dumps({"url": "https://example.com/", "content": content})}]}
    else:
        value = {"structuredContent": {"data": {"url": "https://example.com/", "content": content}}}
    wire(install, result=value, tool_name="webReader")
    result = await search.extract({"url": "https://example.com/"})
    assert result["content"] == "Example" and result["truncated"] and result["warnings"]
    assert all(r.url.host == "api.z.ai" and r.url.path == "/api/mcp/web_reader/mcp" for r in calls)
    assert json.loads(calls[-1].content)["params"]["arguments"] == {"url": "https://example.com/"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,site", [("api_key", "bigmodel"), ("oauth", "zai")])
async def test_account_model_key_site_and_proxy(setup, monkeypatch, mode, site):
    cfg, calls, install = setup
    account = credential(mode, site, generationId="search-generation-" + site)
    cfg["oauthAccounts"] = [account]
    cfg["search"]["backends"] = [backend("zhipu", apiKeys=[], endpoint="https://open.bigmodel.cn")]
    before = copy.deepcopy(cfg)
    wire(install)
    factory = search.network.async_client
    def client(**kw):
        assert kw["proxy_purpose"] == "oauth_zhipu"
        assert kw["follow_redirects"] is False
        return factory(**kw)
    monkeypatch.setattr(search.network, "async_client", client)
    await search.search({"query": "Python"})
    assert all(r.headers["authorization"] == "Bearer fixture.secret" for r in calls)
    assert all(r.url.host == ("api.z.ai" if site == "zai" else "open.bigmodel.cn") for r in calls)
    assert cfg == before


def test_account_readiness_and_deduplication(setup):
    cfg, _, _ = setup
    account = credential("api_key", generationId="search-selection")
    cfg["oauthAccounts"] = [account]
    source = backend("zhipu", apiKeys=[])
    cfg["search"]["backends"] = [source]
    assert search.backend_statuses()[0]["accountCount"] == 1
    source["apiKeys"] = ["fixture.secret"]
    assert search._credentials(source) == ["fixture.secret"]
    source["apiKeys"] = []
    account.update(enabled=False, disabled_reason="user")
    assert not search.backend_statuses()[0]["available"]
    source["allowDisabledAccounts"] = True
    assert search.backend_statuses()[0]["available"]
    account["disabled_reason"] = "quota"
    assert not search.backend_statuses()[0]["available"]
    account.update(enabled=True, disabled_reason=None)
    source["accountIds"] = ["zhipu:other"]
    assert not search.backend_statuses()[0]["available"]
    source["accountIds"] = [account_key(account)]
    assert search.backend_statuses()[0]["available"]
    del account["model_key"]
    account["access_token"] = "management-not-for-mcp"
    assert not search.backend_statuses()[0]["available"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["isError", "rpc", "business", "auth", "quota", "malformed"])
async def test_errors_are_sanitized_and_bound_retries(setup, failure):
    cfg, calls, install = setup
    cfg["search"].update(backends=[backend("zhipu")], maxAttempts=2)
    def fail(request, body):
        if failure in ("auth", "quota"):
            return httpx.Response(401 if failure == "auth" else 429, text="private-test-key")
        if failure == "malformed":
            return httpx.Response(200, text="private-test-key")
        envelope = {"jsonrpc": "2.0", "id": body["id"]}
        if failure == "rpc":
            envelope["error"] = {"code": -32603, "message": "private-test-key"}
        else:
            envelope["result"] = {"isError": failure == "isError", "content": [{"type": "text", "text": json.dumps({"code": 500, "message": "private-test-key"})}]}
        return httpx.Response(200, json=envelope)
    wire(install, fail=fail)
    with pytest.raises(search.SearchError) as exc:
        await search.search({"query": "hello"})
    assert "private-test-key" not in str(exc.value)
    assert len(calls) == (8 if failure == "malformed" else 4)


@pytest.mark.asyncio
async def test_failover_and_domain_policy(setup):
    cfg, calls, install = setup
    cfg["search"]["backends"] = [backend("zhipu"), backend("tavily")]
    install(lambda r: httpx.Response(401) if r.url.host == "open.bigmodel.cn" else httpx.Response(200, json={"results": []}))
    result = await search.search({"query": "hello"})
    assert [attempt["status"] for attempt in result["attempts"]] == ["error", "success"]
    calls.clear()
    cfg["search"]["backends"] = [backend("zhipu")]
    wire(install)
    result = await search.search({"query": "Python", "allowed_domains": ["python.org", "other.test"], "blocked_domains": ["other.test"]})
    assert len(result["results"]) == 1
    assert "search_domain_filter" not in json.loads(calls[-1].content)["params"]["arguments"]
    calls.clear()
    with pytest.raises(search.SearchError, match="离线"):
        await search.search({"query": "hello", "external_web_access": False})
    with pytest.raises(search.SearchError):
        await search.extract({"url": "http://127.0.0.1/"})
    assert not calls
    wire(install, tool_name="webReader", result={"structuredContent": {"url": "https://other.test/", "content": "body"}})
    with pytest.raises(search.SearchError) as exc:
        await search.extract({"url": "https://example.com/", "allowed_domains": ["example.com"]})
    assert exc.value.code == "url_not_allowed" and len(calls) == 4


@pytest.mark.asyncio
async def test_timeout_and_cancellation(setup, monkeypatch):
    cfg, _, _ = setup
    cfg["search"].update(backends=[backend("zhipu")], timeoutSeconds=0.01, maxAttempts=1)
    async def waiting(request):
        await asyncio.sleep(10)
    monkeypatch.setattr(search.network, "async_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(waiting)))
    with pytest.raises(search.SearchError) as exc:
        await search.search({"query": "hello"})
    assert exc.value.code == "search_timeout"
    task = asyncio.create_task(search.search({"query": "hello"}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_management_hybrid_credentials_and_menu(memory, control, ctx, tg):
    from src.telegram.menus import search_menu
    from src.management_api.schemas.search import SearchBackendCreate, SearchSettingsData
    from src.management_control import ManagementError
    account = credential("api_key", generationId="search-management")
    memory.value["oauthAccounts"] = [account]
    before = copy.deepcopy(memory.value)
    assert control.accounts(ctx, "zhipu")[0]["credentialConfigured"]
    assert memory.value == before
    body = SearchBackendCreate(type="zhipu", id="zhipu-cn", apiKeys=["standalone"], accountIds=[account_key(account)])
    view = control.add_backend(ctx, body.model_dump(exclude_none=True))
    SearchSettingsData.model_validate(view)
    row = next(row for row in view["backends"] if row["id"] == "zhipu-cn")
    assert row["keyCount"] == row["accountCount"] == 1
    assert "standalone" not in json.dumps(view) and "fixture.secret" not in json.dumps(view)
    for patch in ({"model": "GLM-5.3"}, {"endpoint": "https://wrong.test"}):
        with pytest.raises(ManagementError):
            control.patch_backend(ctx, "zhipu-cn", patch)
    control.patch_backend(ctx, "zhipu-cn", {"endpoint": "https://api.z.ai", "allowDisabledAccounts": True})
    assert not control.models(ctx, "zhipu-cn")["supported"]
    code = search_menu._code("zhipu-cn")
    callback("srch:backend:" + code)
    page = latest(tg)
    actions = [b["callback_data"] for b in buttons(page)]
    assert "srch:keys:" + code in actions and "srch:accounts:" + code in actions and "srch:extract:" + code in actions
    assert "srch:models:" + code not in actions
    assert "共享套餐额度" in page["text"]
    callback("srch:endpoint:" + code)
    assert "https://api.z.ai" in latest(tg)["text"]
