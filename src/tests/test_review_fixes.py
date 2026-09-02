from __future__ import annotations

import json
import os
import sys

import pytest

import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(
    0,
    _ap_os.path.dirname(
        _ap_os.path.dirname(
            _ap_os.path.dirname(_ap_os.path.abspath(__file__))
        )
    ),
)
from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import cache_hints, config, state_db
    from src.openai.transform import stream_c2r
    from src.transform import cc_mimicry, standard
    return {
        "cache_hints": cache_hints,
        "config": config,
        "state_db": state_db,
        "stream_c2r": stream_c2r,
        "cc_mimicry": cc_mimicry,
        "standard": standard,
    }


def _setup(m):
    m["state_db"].init()
    for row in m["state_db"].quota_load_all():
        m["state_db"].quota_delete(row["account_key"])


def _parse_responses_events(frames):
    text = b"".join(frames).decode("utf-8", errors="replace")
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = ""
        data_str = ""
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if not data_str:
            continue
        out.append((event_name, json.loads(data_str)))
    return out


def test_config_write_atomic_preserves_live_file_on_serialize_error(m, tmp_path):
    cfg = m["config"]
    original_path = cfg.CONFIG_PATH
    cfg.CONFIG_PATH = str(tmp_path / "config.json")
    try:
        with open(cfg.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"ok": 1}, f)
        with pytest.raises(TypeError):
            cfg._write_atomic({"bad": object()})
        assert os.path.exists(cfg.CONFIG_PATH)
        with open(cfg.CONFIG_PATH, "r", encoding="utf-8") as f:
            assert json.load(f) == {"ok": 1}
    finally:
        cfg.CONFIG_PATH = original_path


def test_quota_save_preserves_openai_snapshot_columns(m):
    _setup(m)
    st = m["state_db"]
    account_key = "openai:test@example.com"
    st.quota_save_openai_snapshot(account_key, {
        "primary_used_pct": 42.0,
        "primary_reset_sec": 3600,
        "primary_window_min": 10080,
        "secondary_used_pct": 7.0,
        "secondary_reset_sec": 120,
        "secondary_window_min": 300,
        "primary_over_secondary_pct": 10.0,
        "fetched_at": 1234567890000,
    })
    before = st.quota_load(account_key)
    assert before["codex_primary_used_pct"] == 42.0
    assert before["last_passive_update_at"] == 1234567890000

    st.quota_save(account_key, {
        "fetched_at": 1234567899999,
        "five_hour_util": 7.0,
        "five_hour_reset": "2026-04-20T00:00:00Z",
        "seven_day_util": 42.0,
        "seven_day_reset": "2026-04-27T00:00:00Z",
        "raw_data": "{}",
    }, email="test@example.com")
    after = st.quota_load(account_key)
    assert after["codex_primary_used_pct"] == 42.0
    assert after["codex_secondary_used_pct"] == 7.0
    assert after["last_passive_update_at"] == 1234567890000


def test_stream_c2r_preserves_all_completed_output_items(m):
    T = m["stream_c2r"].StreamTranslator
    tr = T(model="gpt-5.4")

    def frame(obj):
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()

    seq = [
        {"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "sum", "arguments": "{\"a\":1"},
        }]}, "finish_reason": None}]},
        {"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "function": {"arguments": ",\"b\":2}"},
        }]}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
    ]
    for obj in seq:
        list(tr.feed(frame(obj)))
    events = _parse_responses_events(list(tr.close()))
    completed = [data["response"] for name, data in events if name == "response.completed"][-1]
    output = completed["output"]
    assert len(output) == 3, output
    assert output[0]["type"] == "message"
    assert output[0]["content"][0]["text"] == "hello "
    assert output[1]["type"] == "function_call"
    assert output[1]["arguments"] == "{\"a\":1,\"b\":2}"
    assert output[2]["type"] == "message"
    assert output[2]["content"][0]["text"] == "world"
    assert completed["output_text"] == "hello world"


def test_anthropic_transforms_default_stream_false(m):
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
    }
    std = m["standard"].standard_transform(body)
    cc, _ = m["cc_mimicry"].transform_request(body)
    assert std["stream"] is False
    assert cc["stream"] is False


def test_standard_anthropic_transform_preserves_native_request_fields(m):
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "service_tier": "auto",
        "speed": "fast",
        "container": {"id": "ctr_1"},
        "mcp_servers": [{"name": "tools"}],
    }

    std = m["standard"].standard_transform(body)

    assert std["service_tier"] == "auto"
    assert std["speed"] == "fast"
    assert std["container"] == {"id": "ctr_1"}
    assert std["mcp_servers"] == [{"name": "tools"}]


def test_dynamic_tool_map_seed_is_process_independent(m):
    cc = m["cc_mimicry"]
    tools = [
        "read", "edit", "write", "exec", "process", "cron", "message",
        "gateway", "sessions_list", "sessions_history", "sessions_send",
        "subagents", "session_status", "sessions_spawn", "browser",
        "browser_extra",
    ]
    mapping1 = cc._build_dynamic_tool_map(tools)
    mapping2 = cc._build_dynamic_tool_map(list(tools))
    assert mapping1 == mapping2
    assert mapping1["exec"] == "extract_exe03"
    assert mapping1["browser_extra"] == "resolve_bro15"


def test_restore_tool_names_only_protocol_tool_name_fields(m):
    cc = m["cc_mimicry"]
    dynamic_map = {"original_tool": "fake_tool"}
    event = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "fake_tool",
            "input": {},
        },
    }
    text_event = {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "text_delta", "text": "fake_tool cc_sess_list"},
    }
    raw = (
        "event: content_block_start\n"
        "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        "event: content_block_delta\n"
        "data: " + json.dumps(text_event, ensure_ascii=False) + "\n\n"
    ).encode("utf-8")

    restored = cc._restore_tool_names_in_chunk(raw, dynamic_map).decode("utf-8")
    blocks = _parse_responses_events([restored.encode("utf-8")])
    assert blocks[0][1]["content_block"]["name"] == "original_tool"
    assert blocks[1][1]["delta"]["text"] == "fake_tool cc_sess_list"


def test_restore_static_tool_prefix_only_protocol_tool_name_fields(m):
    cc = m["cc_mimicry"]
    obj = {
        "type": "message",
        "content": [
            {"type": "text", "text": "cc_sess_list should stay in text"},
            {"type": "tool_use", "id": "toolu_1", "name": "cc_sess_list", "input": {}},
        ],
    }
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    restored = json.loads(cc._restore_tool_names_in_chunk(raw).decode("utf-8"))
    assert restored["content"][0]["text"] == "cc_sess_list should stay in text"
    assert restored["content"][1]["name"] == "sessions_list"


def test_restore_tool_name_field_in_incomplete_sse_json(m):
    cc = m["cc_mimicry"]
    raw = b'data: {"type":"content_block_start","content_block":{"type":"tool_use","name":"cc_sess_list"'
    restored = cc._restore_tool_names_in_chunk(raw)
    assert b'"name":"sessions_list"' in restored
    assert b'cc_sess_list' not in restored



def test_anthropic_tool_cache_breakpoint_uses_last_non_deferred_tool(m):
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"name": "resident_1", "input_schema": {"type": "object"}},
            {"name": "resident_2", "input_schema": {"type": "object"}},
            {"name": "deferred", "input_schema": {"type": "object"}, "defer_loading": True},
        ],
    }
    all_deferred_body = {
        **body,
        "tools": [
            {"name": "deferred_1", "input_schema": {"type": "object"}, "defer_loading": True},
            {"name": "deferred_2", "input_schema": {"type": "object"}, "defer_loading": True},
        ],
    }

    payloads = [
        (m["standard"].standard_transform(body), {"type": "ephemeral", "ttl": "1h"}),
        (m["cc_mimicry"].transform_request(body, session_id="s")[0], {"type": "ephemeral"}),
    ]
    all_deferred_payloads = [
        m["standard"].standard_transform(all_deferred_body),
        m["cc_mimicry"].transform_request(all_deferred_body, session_id="s")[0],
    ]

    for payload, expected_cache_control in payloads:
        assert "cache_control" not in payload["tools"][0]
        assert payload["tools"][1]["cache_control"] == expected_cache_control
        assert payload["tools"][2]["defer_loading"] is True
        assert "cache_control" not in payload["tools"][2]
    for payload in all_deferred_payloads:
        assert all(tool["defer_loading"] is True for tool in payload["tools"])
        assert all("cache_control" not in tool for tool in payload["tools"])


def test_anthropic_cache_breakpoints_fill_missing_tool_section_up_to_four(m):
    five_minute = {"type": "ephemeral"}
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 64,
        "system": [
            {"type": "text", "text": "system 1", "cache_control": five_minute},
            {"type": "text", "text": "system 2", "cache_control": five_minute},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "u1"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
            {"role": "user", "content": [{
                "type": "text",
                "text": "u2",
                "cache_control": five_minute,
            }]},
        ],
        "tools": [
            {"name": "resident", "input_schema": {"type": "object"}},
            {"name": "deferred", "input_schema": {"type": "object"}, "defer_loading": True},
        ],
    }

    payload = m["standard"].standard_transform(body)

    assert payload["tools"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in payload["tools"][1]
    assert sum("cache_control" in block for block in payload["system"]) == 2
    assert sum(
        "cache_control" in block
        for message in payload["messages"]
        for block in message["content"]
    ) == 1

    at_limit = {
        **body,
        "messages": [
            {"role": "user", "content": [{
                "type": "text", "text": "u1", "cache_control": five_minute,
            }]},
            {"role": "user", "content": [{
                "type": "text", "text": "u2", "cache_control": five_minute,
            }]},
        ],
    }
    limited_payload = m["standard"].standard_transform(at_limit)
    assert all("cache_control" not in tool for tool in limited_payload["tools"])


def test_anthropic_tool_namespace_type_is_stripped_like_claude_code(m):
    cc = m["cc_mimicry"]
    std = m["standard"]
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "namespace",
                "namespace": "browser",
                "name": "browser_open",
                "description": "Open a page",
                "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
                "extra_client_field": "must not reach Anthropic",
            }
        ],
    }

    cc_payload, _ = cc.transform_request(body, session_id="s")
    std_payload = std.standard_transform(body)

    for payload, expected_cache_control in (
        (cc_payload, {"type": "ephemeral"}),
        (std_payload, {"type": "ephemeral", "ttl": "1h"}),
    ):
        tool = payload["tools"][0]
        assert tool["name"].endswith("browser_open")
        assert tool["description"] == "Open a page"
        assert tool["input_schema"] == {"type": "object", "properties": {"url": {"type": "string"}}}
        assert tool["cache_control"] == expected_cache_control
        assert "type" not in tool
        assert "namespace" not in tool
        assert "extra_client_field" not in tool


def test_anthropic_chat_style_function_tool_is_flattened(m):
    cc = m["cc_mimicry"]
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup data",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }],
    }
    payload, _ = cc.transform_request(body, session_id="s")
    tool = payload["tools"][0]
    assert tool["name"] == "lookup"
    assert tool["description"] == "Lookup data"
    assert tool["input_schema"] == {"type": "object", "properties": {"q": {"type": "string"}}}
    assert "type" not in tool
    assert "function" not in tool


def test_anthropic_server_tool_type_is_preserved(m):
    cc = m["cc_mimicry"]
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
    }
    payload, _ = cc.transform_request(body, session_id="s")
    tool = payload["tools"][0]
    assert tool["type"] == "web_search_20250305"
    assert tool["name"] == "web_search"
    assert tool["max_uses"] == 2


def test_message_content_block_whitelist_is_shallow_and_preserves_nested_schema(m):
    cc = m["cc_mimicry"]
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_1",
                "name": "validate",
                "caller": {"type": "namespace", "namespace": "tool_search_only"},
                "input": {
                    "schema": {
                        "type": "object",
                        "properties": {"kind": {"type": "string"}},
                    },
                    "payload": {"type": "namespace", "namespace": "nested-data-must-stay"},
                },
                "extra_client_field": "drop",
            }],
            "client_side_meta": "drop",
        }],
    }
    payload, _ = cc.transform_request(body, session_id="s")
    assistant_msg = next(msg for msg in payload["messages"] if msg.get("role") == "assistant")
    block = assistant_msg["content"][0]
    assert block["type"] == "tool_use"
    assert set(block.keys()) <= {"type", "id", "name", "input", "cache_control"}
    assert "caller" not in block
    assert "extra_client_field" not in block
    assert block["input"]["schema"]["type"] == "object"
    assert block["input"]["payload"] == {"type": "namespace", "namespace": "nested-data-must-stay"}
    assert "client_side_meta" not in payload["messages"][0]


def test_unknown_message_content_block_passes_through_for_future_betas(m):
    cc = m["cc_mimicry"]
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{
            "role": "user",
            "content": [{"type": "future_beta_block", "foo": {"type": "namespace"}}],
        }],
    }
    payload, _ = cc.transform_request(body, session_id="s")
    block = payload["messages"][0]["content"][0]
    assert block["type"] == "future_beta_block"
    assert block["foo"] == {"type": "namespace"}


def test_tool_input_schema_nested_type_fields_are_not_recursively_stripped(m):
    cc = m["cc_mimicry"]
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{
            "type": "namespace",
            "namespace": "outer-should-drop",
            "name": "schema_tool",
            "description": "schema",
            "input_schema": {
                "type": "object",
                "properties": {
                    "nested": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    },
                    "tagged_payload": {"type": "namespace", "description": "data tag in schema"},
                },
            },
        }],
    }
    payload, _ = cc.transform_request(body, session_id="s")
    tool = payload["tools"][0]
    assert "type" not in tool
    assert "namespace" not in tool
    assert tool["input_schema"]["type"] == "object"
    assert tool["input_schema"]["properties"]["nested"]["type"] == "object"
    assert tool["input_schema"]["properties"]["tagged_payload"]["type"] == "namespace"

def test_counting_transport_counts_streamed_request_and_response_bytes(m):
    import asyncio
    import httpx
    from src.proxy.connector import CountingAsyncTransport

    async def main():
        seen = {"up": 0, "down": 0}

        def on_bytes(up=0, down=0):
            seen["up"] += up
            seen["down"] += down

        async def handler(request: httpx.Request) -> httpx.Response:
            body = b""
            async for chunk in request.stream:
                body += chunk
            assert body == b"abcdef"
            return httpx.Response(200, content=b"xyz123")

        transport = CountingAsyncTransport(httpx.MockTransport(handler), on_bytes)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.post("https://example.test/", content=b"abcdef")
            assert resp.text == "xyz123"
        assert seen == {"up": 6, "down": 6}

    asyncio.run(main())


def test_anthropic_cache_ttl_repair_matches_long_conversation_error_path(m):
    cache_hints = m["cache_hints"]
    source_system = [{
        "type": "text",
        "text": "stable system",
        "cache_control": {"type": "ephemeral"},
    }]
    source_messages = [
        {"role": "user", "content": [{"type": "text", "text": f"history-{index}"}]}
        for index in range(274)
    ]
    source_messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": "block-0"},
            {"type": "text", "text": "block-1"},
            {
                "type": "text",
                "text": "block-2",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
        ],
    })
    payload = {"system": source_system, "messages": source_messages}

    promoted = cache_hints.promote_anthropic_cache_ttls_for_order(payload)

    assert promoted == 1
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert payload["messages"][274]["content"][2]["cache_control"]["ttl"] == "1h"
    assert [block["text"] for block in payload["messages"][274]["content"]] == [
        "block-0", "block-1", "block-2",
    ]
    # Repair is copy-on-write: caller-owned source objects remain unchanged.
    assert source_system[0]["cache_control"] == {"type": "ephemeral"}
    assert source_messages[274]["content"][2]["cache_control"]["ttl"] == "1h"


def test_anthropic_cache_ttl_repair_uses_tools_system_messages_order(m):
    cache_hints = m["cache_hints"]
    payload = {
        "tools": [{
            "name": "lookup",
            "input_schema": {"type": "object"},
            "cache_control": {"type": "ephemeral", "ttl": "5m"},
        }],
        "system": [{
            "type": "text",
            "text": "system",
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": [
            {
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": "long breakpoint",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                }],
            },
            {
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": "valid short suffix",
                    "cache_control": {"type": "ephemeral", "ttl": "5m"},
                }],
            },
        ],
    }

    promoted = cache_hints.promote_anthropic_cache_ttls_for_order(payload)

    assert promoted == 2
    assert payload["tools"][0]["cache_control"]["ttl"] == "1h"
    assert payload["system"][0]["cache_control"]["ttl"] == "1h"
    assert payload["messages"][0]["content"][0]["cache_control"]["ttl"] == "1h"
    # A 5m breakpoint after the final 1h is already valid and stays 5m.
    assert payload["messages"][1]["content"][0]["cache_control"]["ttl"] == "5m"


@pytest.mark.parametrize("ttls", [
    ("1h", "1h", "5m"),
    ("5m", "5m", "5m"),
])
def test_anthropic_cache_ttl_repair_leaves_valid_sequences_unchanged(m, ttls):
    cache_hints = m["cache_hints"]
    payload = {
        "tools": [{"cache_control": {"type": "ephemeral", "ttl": ttls[0]}}],
        "system": [{"cache_control": {"type": "ephemeral", "ttl": ttls[1]}}],
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": "hi", "cache_control": {
                "type": "ephemeral", "ttl": ttls[2],
            }}],
        }],
    }
    before = json.loads(json.dumps(payload))

    assert cache_hints.promote_anthropic_cache_ttls_for_order(payload) == 0
    assert payload == before


def test_anthropic_transforms_repair_mixed_ttls_before_cc_signing(m, monkeypatch):
    cc = m["cc_mimicry"]
    standard = m["standard"]
    cache_hints = m["cache_hints"]
    body = {
        "model": "claude-fable-5",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": [{
                "type": "text",
                "text": "earlier",
                "cache_control": {"type": "ephemeral"},
            }]},
            {"role": "assistant", "content": [{"type": "text", "text": "middle"}]},
            {"role": "user", "content": [
                {"type": "text", "text": "block-0"},
                {"type": "text", "text": "block-1"},
                {
                    "type": "text",
                    "text": "block-2",
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
            ]},
        ],
    }

    monkeypatch.setattr(cc, "load_config", lambda: {"cch_mode": "dynamic"})
    standard_payload = standard.standard_transform(body)
    cc_payload, _ = cc.transform_request(body, session_id="stable-session")

    for payload in (standard_payload, cc_payload):
        controls = list(cache_hints._anthropic_prompt_order_cache_controls(payload))
        ttls = [control.get("ttl", "5m") for control in controls]
        assert "1h" in ttls
        first_five = next((index for index, ttl in enumerate(ttls) if ttl == "5m"), len(ttls))
        assert all(ttl != "1h" for ttl in ttls[first_five:])
        assert payload["messages"][0]["content"][0]["cache_control"]["ttl"] == "1h"
        assert payload["messages"][2]["content"][2]["cache_control"]["ttl"] == "1h"

    # CC repair happened inside transform_request, before sign_body computes CCH.
    wire_payload = json.loads(cc.sign_body(cc_payload))
    billing = cc._generated_billing_block(wire_payload)
    assert billing is not None
    assert f"cch={cc.compute_cch(wire_payload)};" in billing["text"]
    # Neither transform may rewrite the caller's request object.
    assert "ttl" not in body["messages"][0]["content"][0]["cache_control"]
