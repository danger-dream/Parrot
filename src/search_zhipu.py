"""Coding Plan search/reader MCP upstream, behind the normal search attempt deadline."""
from __future__ import annotations

import json

import httpx

from . import network
from .oauth.zhipu.common import MODEL_ORIGINS, site_of


async def _rpc(client, url, headers, number, method, params=None):
    from .search_service import SearchError

    body = {"jsonrpc": "2.0", "method": method}
    if number is not None:
        body["id"] = number
    if params is not None:
        body["params"] = params
    async with client.stream("POST", url, headers=headers, json=body) as response:
        status = response.status_code
        if status >= 400:
            raise SearchError(f"智谱 MCP 返回 HTTP {status}",
                              code="search_rate_limited" if status == 429 else "search_upstream_error",
                              retryable=status in (408, 409, 425, 429) or status >= 500)
        if status >= 300:
            raise SearchError("智谱 MCP 返回重定向", code="invalid_search_response", retryable=False)
        if response.headers.get("mcp-session-id"):
            headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        if number is None:
            return None
        sse = response.headers.get("content-type", "").split(";")[0] == "text/event-stream"
        buffer, size = "", 0

        def envelope(raw):
            try:
                value = json.loads(raw)
            except ValueError:
                raise SearchError("智谱 MCP 返回无效 JSON", code="invalid_search_response") from None
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                raise SearchError("智谱 MCP 响应结构无效", code="invalid_search_response")
            if value.get("id") != number:
                return None  # Server notifications are not the requested response.
            if "error" in value:
                raise SearchError("智谱 MCP 报告执行错误", code="search_upstream_error", retryable=False)
            result = value.get("result")
            if not isinstance(result, dict):
                raise SearchError("智谱 MCP 缺少结果", code="invalid_search_response")
            return result

        def event(raw):
            data = "\n".join(line[5:].lstrip(" ") for line in raw.splitlines() if line.startswith("data:"))
            return envelope(data) if data else None

        # Stop at our response instead of waiting for a persistent SSE stream to close.
        async for chunk in response.aiter_text():
            size += len(chunk)
            if size > 10 * 1024 * 1024:
                raise SearchError("智谱 MCP 响应过大", code="invalid_search_response", retryable=False)
            buffer += chunk
            if sse:
                buffer = buffer.replace("\r\n", "\n")
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    result = event(raw)
                    if result is not None:
                        return result
        result = event(buffer) if sse else envelope(buffer)
        if result is None:
            raise SearchError("智谱 MCP 缺少匹配的响应", code="invalid_search_response")
        return result


def _decode(value):
    # Current upstream text contains a JSON string containing a JSON array.
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            break
    return value


def _content(result):
    from .search_service import SearchError

    if result.get("isError"):
        raise SearchError("智谱 MCP 工具执行失败", code="search_upstream_error", retryable=False)
    if isinstance(result.get("structuredContent"), dict):
        return _decode(result["structuredContent"])
    texts = [block["text"] for block in result.get("content") or []
             if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    if not texts:
        raise SearchError("智谱 MCP 未返回内容", code="invalid_search_response")
    return _decode("\n".join(texts))


def _parse(result, args, operation, cfg):
    from .search_service import SearchError, _check_extract_domains, _normalize_rows, _set_extract_content

    data = _content(result)
    if isinstance(data, dict):
        if data.get("error") or data.get("success") is False or data.get("code") not in (None, 0, 200, "0", "200"):
            raise SearchError("智谱 MCP 报告业务错误", code="search_upstream_error", retryable=False)
        if "data" in data:
            data = _decode(data["data"])
    output = {}
    if operation == "search":
        rows = data.get("results") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise SearchError("智谱搜索缺少结果数组", code="invalid_search_response")
        rows = [{**row, "url": row.get("link") or row.get("url"),
                 "snippet": row.get("content") or row.get("snippet") or ""}
                for row in rows if isinstance(row, dict)]
        output.update(query=args["query"], results=_normalize_rows(rows, args))
    else:
        url = str(data.get("url") or args["url"]) if isinstance(data, dict) else args["url"]
        content = (data.get("content") or data.get("markdown") or data.get("text")) if isinstance(data, dict) else data
        if not isinstance(content, str) or not content.strip():
            raise SearchError("智谱未返回网页正文", code="empty_extract_response")
        _check_extract_domains(url, args)
        _set_extract_content(output, url, content, cfg)
    return output


async def adapter(backend, credential, args, operation, cfg):
    from .search_service import SearchError, _query_text

    if isinstance(credential, dict):
        from . import oauth_manager
        from .oauth_ids import account_key
        state_key = oauth_manager.account_state_key(credential)
        with oauth_manager.account_generation_guard(state_key) as current:
            if not current:
                raise SearchError("搜索账户身份已变更", code="search_account_retired", status_code=503, retryable=False)
            account = oauth_manager.get_account(account_key(credential))
            if not account or not account.get("model_key"):
                raise SearchError("智谱账户缺少模型 Key", code="no_search_backend", retryable=False)
            origin, key = MODEL_ORIGINS[site_of(account)], account["model_key"]
    else:
        origin, key = str(backend.get("endpoint") or MODEL_ORIGINS["bigmodel"]).rstrip("/"), credential
        if origin not in MODEL_ORIGINS.values():
            raise SearchError("智谱来源须使用中国站或国际站官方地址", code="invalid_search_backend", retryable=False)
    tool_path = "web_reader" if operation == "extract" else "web_search_prime"
    names = ("webReader",) if operation == "extract" else ("web_search_prime", "webSearchPrime")
    url = origin + "/api/mcp/" + tool_path + "/mcp"
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if operation == "extract":
        arguments = {"url": args["url"]}
    else:
        arguments = {"search_query": _query_text(args)}
        domains = args["allowed_domains"]
        if len(domains) == 1:
            arguments["search_domain_filter"] = domains[0]
        # Multiple allowed/blocked domains are enforced on returned rows; the
        # upstream's single-domain string does not define an OR syntax.
        if args.get("freshness"):
            arguments["search_recency_filter"] = {
                "day": "oneDay", "week": "oneWeek", "month": "oneMonth", "year": "oneYear",
            }[args["freshness"]]
    async with network.async_client(timeout=httpx.Timeout(float(cfg["timeoutSeconds"])),
                                    follow_redirects=False, proxy_purpose="oauth_openai") as client:
        hello = await _rpc(client, url, headers, 1, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "parrot", "version": "1.0"},
        })
        version = hello.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
            raise SearchError("智谱 MCP 协议版本不支持", code="invalid_search_response", retryable=False)
        headers["MCP-Protocol-Version"] = version
        await _rpc(client, url, headers, None, "notifications/initialized")
        listing = await _rpc(client, url, headers, 2, "tools/list", {})
        available = {tool.get("name") for tool in listing.get("tools") or [] if isinstance(tool, dict)}
        name = next((name for name in names if name in available), None)
        if not name:
            raise SearchError("智谱 MCP 未提供所需工具", code="search_capability_unavailable", retryable=False)
        result = await _rpc(client, url, headers, 3, "tools/call", {"name": name, "arguments": arguments})
    return _parse(result, args, operation, cfg)
