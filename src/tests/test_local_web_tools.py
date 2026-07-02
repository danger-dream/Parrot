from __future__ import annotations

import asyncio

from fastapi.responses import JSONResponse, StreamingResponse

from src import local_web_tools


def test_extracts_supported_local_web_tool_calls_only():
    message = {
        "content": [
            {"type": "tool_use", "id": "call_search", "name": "WebSearch", "input": {"query": "parrot"}},
            {"type": "tool_use", "id": "call_fetch", "name": "web_fetch", "input": {"url": "https://example.com"}},
            {"type": "tool_use", "id": "call_other", "name": "Read", "input": {"file": "x"}},
        ]
    }

    calls = local_web_tools.extract_local_tool_calls(message, [
        {"name": "WebSearch", "allowed_domains": ["example.com"], "blocked_domains": ["spam.example"]},
    ])

    assert [(c.id, c.name, c.input) for c in calls] == [
        ("call_search", "WebSearch", {"query": "parrot", "allowed_domains": ["example.com"], "blocked_domains": ["spam.example"]}),
        ("call_fetch", "web_fetch", {"url": "https://example.com"}),
    ]
    assert local_web_tools.tool_use_count(message) == 3


def test_extracts_openai_responses_function_call_for_local_web_tools():
    message = {
        "role": "assistant",
        "content": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_search",
                "name": "WebSearch",
                "arguments": '{"query":"中文分段算法","allowed_domains":["example.com"]}',
                "status": "completed",
            },
            {
                "type": "function_call",
                "id": "fc_2",
                "call_id": "call_read",
                "name": "Read",
                "arguments": '{"file":"x"}',
                "status": "completed",
            },
        ],
    }

    normalized = local_web_tools.normalize_assistant_message_for_local_tools(message)
    assert normalized["content"][0] == {
        "type": "tool_use",
        "id": "call_search",
        "name": "WebSearch",
        "input": {"query": "中文分段算法", "allowed_domains": ["example.com"]},
    }
    assert local_web_tools.tool_use_count(normalized) == 2

    calls = local_web_tools.extract_local_tool_calls(normalized)

    assert [(c.id, c.name, c.input) for c in calls] == [
        ("call_search", "WebSearch", {"query": "中文分段算法", "allowed_domains": ["example.com"]}),
    ]


def test_append_tool_results_normalizes_responses_function_call_history():
    body = {"messages": [{"role": "user", "content": "search"}]}
    assistant = local_web_tools.normalize_assistant_message_for_local_tools({
        "content": [{
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_search",
            "name": "WebSearch",
            "arguments": '{"query":"parrot"}',
        }]
    })

    local_web_tools.append_tool_results_to_body(
        body,
        assistant,
        [local_web_tools.LocalToolResult("call_search", "result text")],
    )

    assert body["messages"][-2] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_search", "name": "WebSearch", "input": {"query": "parrot"}}],
    }
    assert body["messages"][-1]["content"][0]["tool_use_id"] == "call_search"


def test_known_urls_are_collected_for_web_fetch_policy():
    message = {"content": [{
        "type": "tool_use",
        "id": "call_fetch",
        "name": "WebFetch",
        "input": {"url": "https://example.com/article"},
    }]}
    body = {"messages": [{"role": "user", "content": "read https://example.com/article/"}]}

    calls = local_web_tools.extract_local_tool_calls(message, conversation_body=body)

    assert calls[0].input["_known_urls"] == ["https://example.com/article"]


def test_anysearch_current_default_policy_baseline(monkeypatch):
    # Baseline before adding anysearch config knobs: keep the current defaults
    # and local validation behavior unchanged.
    assert local_web_tools._max_results() == 8
    assert local_web_tools._max_fetch_chars() == 50000
    assert local_web_tools.max_tool_rounds() == 50

    short_query = asyncio.run(local_web_tools.execute_local_tool_call(
        local_web_tools.LocalToolCall("call_search", "WebSearch", {"query": "x"})
    ))
    assert short_query.is_error is True
    assert "too short" in short_query.content

    long_url = "https://example.com/" + ("a" * 260)
    long_url_result = asyncio.run(local_web_tools.execute_local_tool_call(
        local_web_tools.LocalToolCall("call_fetch", "WebFetch", {"url": long_url})
    ))
    assert long_url_result.is_error is True
    assert "250 characters" in long_url_result.content

    active = 0
    max_active = 0

    async def fake_call(tool_name: str, arguments: dict) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    monkeypatch.setattr(local_web_tools, "_call_anysearch", fake_call)
    calls = [local_web_tools.LocalToolCall(f"call_{i}", "WebSearch", {"query": f"query {i}"}) for i in range(5)]
    results = asyncio.run(local_web_tools.execute_local_tool_calls(calls))
    assert all(not r.is_error for r in results)
    assert max_active == 5


def test_anysearch_custom_policy_config(monkeypatch):
    base_cfg = local_web_tools.config.get()
    custom_cfg = dict(base_cfg)
    custom_anysearch = dict(custom_cfg.get("anysearch") or {})
    custom_anysearch.update({
        "minQueryChars": 4,
        "maxFetchUrlChars": 30,
        "requireKnownUrlForFetch": False,
        "maxConcurrentToolCalls": 2,
    })
    custom_cfg["anysearch"] = custom_anysearch
    monkeypatch.setattr(local_web_tools.config, "get", lambda: custom_cfg)

    too_short = asyncio.run(local_web_tools.execute_local_tool_call(
        local_web_tools.LocalToolCall("call_search", "WebSearch", {"query": "abc"})
    ))
    assert too_short.is_error is True
    assert "min 4" in too_short.content

    seen_calls = []
    active = 0
    max_active = 0

    async def fake_call(tool_name: str, arguments: dict) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        seen_calls.append((tool_name, arguments))
        return "ok"

    monkeypatch.setattr(local_web_tools, "_call_anysearch", fake_call)
    fetch = asyncio.run(local_web_tools.execute_local_tool_call(
        local_web_tools.LocalToolCall("call_fetch", "WebFetch", {
            "url": "https://x.co/a",
            "_known_urls": ["https://other.example/"],
        })
    ))
    assert fetch.is_error is False
    assert seen_calls[-1] == ("extract", {"url": "https://x.co/a"})

    calls = [local_web_tools.LocalToolCall(f"call_{i}", "WebSearch", {"query": f"query {i}"}) for i in range(5)]
    results = asyncio.run(local_web_tools.execute_local_tool_calls(calls))
    assert all(not r.is_error for r in results)
    assert max_active <= 2


def test_web_fetch_rejects_urls_not_seen_in_conversation(monkeypatch):
    async def fake_call(tool_name: str, arguments: dict) -> str:  # pragma: no cover - should not be reached
        raise AssertionError("AnySearch should not be called for disallowed fetch URL")

    monkeypatch.setattr(local_web_tools, "_call_anysearch", fake_call)

    result = asyncio.run(local_web_tools.execute_local_tool_call(
        local_web_tools.LocalToolCall("call_fetch", "WebFetch", {
            "url": "https://evil.example/",
            "_known_urls": ["https://example.com/article"],
        })
    ))

    assert result.is_error is True
    assert result.content.startswith("url_not_allowed")


def test_execute_search_and_fetch_with_anysearch_monkeypatch(monkeypatch):
    seen: list[tuple[str, dict]] = []

    async def fake_call(tool_name: str, arguments: dict) -> str:
        seen.append((tool_name, arguments))
        if tool_name == "search":
            return "## Search Results\nresult"
        return "Fetched page content"

    monkeypatch.setattr(local_web_tools, "_call_anysearch", fake_call)
    monkeypatch.setattr(local_web_tools, "_max_results", lambda: 3)
    monkeypatch.setattr(local_web_tools, "_max_fetch_chars", lambda: 1000)

    results = asyncio.run(local_web_tools.execute_local_tool_calls([
        local_web_tools.LocalToolCall("call_search", "WebSearch", {
            "query": "Claude web search",
            "allowed_domains": ["platform.claude.com"],
            "blocked_domains": ["spam.example"],
        }),
        local_web_tools.LocalToolCall("call_fetch", "WebFetch", {
            "url": "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool",
            "prompt": "summarize",
        }),
    ]))

    assert [r.tool_use_id for r in results] == ["call_search", "call_fetch"]
    assert results[0].content == "## Search Results\nresult"
    assert "Fetched page content" in results[1].content
    assert seen[0] == (
        "search",
        {"query": "(site:platform.claude.com) Claude web search -site:spam.example", "max_results": 3},
    )
    assert seen[1] == (
        "extract",
        {"url": "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool"},
    )


def test_append_tool_results_and_wrap_nonstream_response_as_anthropic_sse():
    body = {"messages": [{"role": "user", "content": "search"}], "tool_choice": {"type": "tool", "name": "WebSearch"}}
    assistant = {"content": [{"type": "tool_use", "id": "call_1", "name": "WebSearch", "input": {"query": "x"}}]}

    local_web_tools.append_tool_results_to_body(
        body,
        assistant,
        [local_web_tools.LocalToolResult("call_1", "result text")],
    )

    assert body["messages"][-2] == {"role": "assistant", "content": assistant["content"]}
    assert body["messages"][-1] == {"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "result text",
        "is_error": False,
    }]}
    assert body["tool_choice"] == {"type": "auto"}

    response = JSONResponse({
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": "done"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    wrapped = local_web_tools.maybe_wrap_anthropic_json_response_as_sse(response)

    assert isinstance(wrapped, StreamingResponse)
    assert wrapped.media_type == "text/event-stream"


def test_remove_supported_tools_from_body_and_round_limit_results():
    body = {
        "tools": [
            {"name": "Read"},
            {"name": "WebSearch"},
            {"name": "WebFetch"},
            {"type": "web_search_20250305"},
        ],
        "tool_choice": {"type": "tool", "name": "WebSearch"},
    }

    removed = local_web_tools.remove_supported_tools_from_body(body)

    assert removed == 3
    assert body["tools"] == [{"name": "Read"}]
    assert body["tool_choice"] == {"type": "auto"}

    results = local_web_tools.round_limit_results([
        local_web_tools.LocalToolCall("call_1", "WebSearch", {"query": "x"}),
    ], 8)
    assert results[0].tool_use_id == "call_1"
    assert results[0].is_error is True
    assert "8 local WebSearch/WebFetch round" in results[0].content


def test_execute_local_tool_calls_records_search_log(monkeypatch):
    events = []

    async def fake_call(tool_name: str, arguments: dict) -> str:
        return "1. Result A https://example.com/a\n2. Result B https://example.com/b"

    def fake_start(request_id, round_no, tool_name, query=None, url=None, started_at=None):
        events.append(("start", request_id, round_no, tool_name, query, url))
        return 42

    def fake_finish(log_id, **kwargs):
        events.append(("finish", log_id, kwargs))

    monkeypatch.setattr(local_web_tools, "_call_anysearch", fake_call)
    monkeypatch.setattr(local_web_tools.log_db, "record_local_web_call", fake_start)
    monkeypatch.setattr(local_web_tools.log_db, "finish_local_web_call", fake_finish)

    results = asyncio.run(local_web_tools.execute_local_tool_calls(
        [local_web_tools.LocalToolCall("call_search", "WebSearch", {"query": "parrot"})],
        request_id="req-1",
        round_no=7,
    ))

    assert results[0].is_error is False
    assert events[0] == ("start", "req-1", 7, "WebSearch", "parrot", None)
    assert events[1][0] == "finish"
    assert events[1][1] == 42
    assert events[1][2]["status"] == "success"
    assert events[1][2]["result_count"] >= 2
    assert events[1][2]["content_bytes"] > 0


def test_stream_anthropic_response_task_with_pings_keeps_stream_alive():
    async def run():
        import asyncio
        from fastapi.responses import JSONResponse

        async def delayed_response():
            await asyncio.sleep(0.02)
            return JSONResponse({
                "id": "msg_keepalive",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })

        task = asyncio.create_task(delayed_response())
        response = local_web_tools.stream_anthropic_response_task_with_pings(
            task,
            ping_interval_seconds=0.001,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")

    out = asyncio.run(run())

    assert "event: ping" in out
    assert "event: message_start" in out
    assert "event: message_stop" in out
    assert "done" in out


def test_prepare_openai_responses_local_web_tools_converts_web_search_and_drops_unsupported():
    body = {
        "model": "m",
        "input": "search",
        "tools": [
            {"type": "web_search_preview_2025_03_11", "description": "Search now"},
            {"type": "tool_search"},
            {"type": "image_generation"},
            {"type": "function", "name": "lookup", "parameters": {"type": "object"}},
        ],
        "tool_choice": {"type": "web_search_preview"},
    }

    assert local_web_tools.prepare_openai_responses_local_web_tools(body) is True

    assert body[local_web_tools.OPENAI_LOCAL_WEB_MARKER] is True
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["name"] == "web_search"
    assert body["tools"][0]["parameters"]["required"] == ["query"]
    assert [t.get("type") for t in body["tools"]] == ["function", "tool_search", "function"]
    assert [t.get("name") for t in body["tools"]] == ["web_search", None, "lookup"]
    assert body["tool_choice"] == {"type": "function", "name": "web_search"}


def test_prepare_openai_responses_preserves_tool_search_and_drops_image_generation_without_marker():
    body = {
        "model": "m",
        "input": "hi",
        "tools": [{"type": "tool_search"}, {"type": "image_generation"}],
        "tool_choice": {"type": "tool_search"},
        "parallel_tool_calls": True,
    }

    assert local_web_tools.prepare_openai_responses_local_web_tools(body) is False

    assert body["tools"] == [{"type": "tool_search"}]
    assert body["tool_choice"] == {"type": "tool_search"}
    assert body["parallel_tool_calls"] is True
    assert local_web_tools.OPENAI_LOCAL_WEB_MARKER not in body


def test_append_openai_tool_results_to_responses_input():
    body = {
        "input": "search parrot",
        "tools": [{"type": "function", "name": "web_search", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "web_search"},
    }
    assistant = {
        "role": "assistant",
        "content": [{
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_search",
            "name": "web_search",
            "arguments": '{"query":"parrot"}',
            "status": "completed",
        }],
    }

    local_web_tools.append_openai_tool_results_to_body(
        body,
        assistant,
        [local_web_tools.LocalToolResult("call_search", "result text")],
    )

    assert body["input"][0] == {"type": "message", "role": "user", "content": "search parrot"}
    assert body["input"][1]["type"] == "function_call"
    assert body["input"][1]["call_id"] == "call_search"
    assert body["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_search",
        "output": "result text",
    }
    assert body["tool_choice"] == "auto"


def test_wrap_responses_json_response_as_sse():
    response = JSONResponse({
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "m",
        "output": [{
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "done", "annotations": []}],
        }],
        "output_text": "done",
    })

    wrapped = local_web_tools.maybe_wrap_responses_json_response_as_sse(response)

    assert isinstance(wrapped, StreamingResponse)
    assert wrapped.media_type == "text/event-stream"
