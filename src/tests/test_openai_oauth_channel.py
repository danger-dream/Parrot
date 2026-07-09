"""OpenAIOAuthChannel + codex_oauth_transform 测试（Commit 2）。

覆盖：
  - codex_oauth_transform.apply_codex_oauth_transform 的强制改造语义：
    store=false / stream=true / 不支持字段剥离 / 模型名规范化 / input 字符串
    包成消息数组 / input 里 system 提 instructions / instructions 兜底 /
    legacy functions-function_call 转换
  - 模型名直接透传（v0.6+ 移除别名映射）
  - registry.rebuild_from_config 按 provider 分派 OAuth 渠道
  - OpenAIOAuthChannel.build_upstream_request：
      * responses ingress 透传 + 强制改造 + 完整 headers
      * chat ingress 先走 chat_to_responses.translate_request 再 codex transform
      * anthropic ingress 翻译成 Responses shape 后走 Codex
      * 有稳定 session anchor 时附带 Codex reasoning replay scope
      * 老版本缺 chatgpt_account_id 时继续不带该 header 请求
  - supports_model / list_client_models 覆盖账户 models 与默认 codex 列表

所有 OAuth 网络调用被 mockMode 兜住（DISABLE_OAUTH_NETWORK_CALLS=1）。
"""

from __future__ import annotations

import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()

import asyncio
import base64
import json
import os
import sys


def _valid_encrypted_content(seed: int = 1) -> str:
    payload = bytearray(1 + 8 + 16 + 16 + 32)
    payload[0] = 0x80
    for i in range(9, len(payload)):
        payload[i] = (seed + i) % 256
    return base64.urlsafe_b64encode(bytes(payload)).decode("ascii").rstrip("=")


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ["DISABLE_OAUTH_NETWORK_CALLS"] = "1"
    from src import config, oauth_manager, state_db
    from src.channel import registry
    from src.channel.oauth_channel import OAuthChannel
    from src.channel.openai_oauth_channel import (
        OpenAIOAuthChannel, CODEX_UPSTREAM_URL, CODEX_CLI_USER_AGENT,
    )
    from src.openai.channel.registration import register_factories
    from src.openai import reasoning_replay
    from src.openai.transform import codex_oauth_transform as transform
    # 必须注册 openai API factory 否则 config 里的 openai-* api channel 会走错分支
    register_factories()
    return {
        "config": config, "oauth_manager": oauth_manager, "state_db": state_db,
        "registry": registry,
        "OAuthChannel": OAuthChannel,
        "OpenAIOAuthChannel": OpenAIOAuthChannel,
        "CODEX_UPSTREAM_URL": CODEX_UPSTREAM_URL,
        "CODEX_CLI_USER_AGENT": CODEX_CLI_USER_AGENT,
        "transform": transform,
        "reasoning_replay": reasoning_replay,
    }


def _setup(m):
    m["state_db"].init()
    m["reasoning_replay"].clear()
    def _reset(c):
        c.setdefault("oauth", {})["mockMode"] = True
        c["oauthAccounts"] = []
        c["channels"] = []
    m["config"].update(_reset)


def _add_openai_acc(m, email="o@openai.test", **kw):
    entry = {
        "email": email,
        "provider": "openai",
        "access_token": "at-" + email,
        "refresh_token": "rt-" + email,
        "id_token": "h.p.s",
        "chatgpt_account_id": kw.get("chatgpt_account_id", "acct-123"),
        "plan_type": kw.get("plan_type", "plus"),
        "models": kw.get("models") or ["gpt-5.1", "gpt-5.1-codex"],
    }
    m["oauth_manager"].add_account(entry)


# ─── codex_oauth_transform ───────────────────────────────────────

def test_transform_basic(m):
    t = m["transform"]
    body = {
        "model": "gpt-5",
        "input": "hi",
        "temperature": 0.7,
        "top_p": 1,
        "max_output_tokens": 100,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "prompt_cache_retention": "1h",
        "stream": False,
        "store": True,
        "user": "u",
        "metadata": {"x": 1},
        "safety_identifier": "sid",
        "stream_options": {"include_usage": True},
        "background": False,
    }
    out = t.apply_codex_oauth_transform(body)
    assert out["model"] == "gpt-5"                 # 直接透传（不再做别名映射）
    assert out["store"] is False                   # 强制
    assert out["stream"] is True                   # 强制
    for k in ("temperature", "top_p", "max_output_tokens",
              "frequency_penalty", "presence_penalty", "prompt_cache_retention",
              "user", "metadata", "safety_identifier", "stream_options", "background"):
        assert k not in out, f"{k} should be stripped"
    assert out["input"] == [{"type": "message", "role": "user", "content": "hi"}]
    assert out["instructions"] == "You are a helpful coding assistant."
    print("  [PASS] transform: basic forced flags + strip + model normalize")


def test_transform_keeps_resolved_model(m):
    t = m["transform"]
    # 传了 resolved_model → 用它覆盖 body.model（不做别名映射）
    out = t.apply_codex_oauth_transform(
        {"model": "anything-else", "input": []},
        resolved_model="gpt-5-codex",
    )
    assert out["model"] == "gpt-5-codex"
    # body 无 model → 用 resolved_model
    out2 = t.apply_codex_oauth_transform(
        {"input": []}, resolved_model="gpt-5-codex",
    )
    assert out2["model"] == "gpt-5-codex"
    print("  [PASS] transform: resolved_model overrides body.model; no mapping")


def test_transform_extracts_system(m):
    t = m["transform"]
    body = {
        "model": "gpt-5.1",
        "input": [
            {"type": "message", "role": "system", "content": "first"},
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "message", "role": "system",
             "content": [{"type": "input_text", "text": "second"}]},
            {"type": "function_call", "name": "foo"},
        ],
    }
    out = t.apply_codex_oauth_transform(body)
    instr = out["instructions"]
    assert "first" in instr and "second" in instr, instr
    # system 消息被移除，user + function_call 保留
    roles = [i.get("role") for i in out["input"] if i.get("type") == "message"]
    assert "system" not in roles
    assert any(i.get("type") == "function_call" for i in out["input"])
    print("  [PASS] transform: system msgs extracted to instructions")


def test_transform_system_appended_to_existing_instructions(m):
    t = m["transform"]
    body = {
        "model": "gpt-5.1",
        "instructions": "PRE",
        "input": [{"type": "message", "role": "system", "content": "SYS"}],
    }
    out = t.apply_codex_oauth_transform(body)
    assert out["instructions"].startswith("PRE")
    assert "SYS" in out["instructions"]
    print("  [PASS] transform: system appended to existing instructions (not overwritten)")


def test_transform_legacy_functions(m):
    t = m["transform"]
    out = t.apply_codex_oauth_transform({
        "model": "gpt-5.1", "input": [],
        "functions": [{"name": "f1"}, {"name": "f2"}],
        "function_call": {"name": "f1"},
    })
    assert "functions" not in out
    assert "function_call" not in out
    # 经过 _convert_legacy_tools + _normalize_codex_tools 两步后：
    # tools 都是 responses-style（顶层有 name），function 子对象可能仍在（sub2api 行为）
    assert isinstance(out["tools"], list) and len(out["tools"]) == 2
    names = sorted(t.get("name") for t in out["tools"])
    assert names == ["f1", "f2"], f"got top-level names: {names}"
    assert all(t.get("type") == "function" for t in out["tools"])
    assert out["tool_choice"] == {"type": "function", "name": "f1"}
    # string function_call (auto) without functions → tool_choice stripped (no tools)
    out2 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1", "input": [], "function_call": "auto",
    })
    assert "tool_choice" not in out2
    assert "tools" not in out2
    print("  [PASS] transform: legacy functions/function_call → tools/tool_choice (flat name)")


def test_transform_tool_choice_and_input_refs(m):
    """OAuth Codex transform: tool_choice / item refs / call ids 按 sub2api 兼容。"""
    t = m["transform"]
    body = {
        "model": "gpt-5.1",
        "input": [
            {"type": "message", "role": "user", "id": "msg_1", "call_id": "bad",
             "content": [{"type": "input_text", "text": {"hello": "world"}}]},
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "item_reference", "id": "call_1"},
            {"type": "function_call", "id": "call_1", "name": "lookup", "arguments": "{}"},
            {"type": "message", "role": "tool", "tool_call_id": "call_1", "content": "ok"},
        ],
        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    }
    out = t.apply_codex_oauth_transform(body)
    assert out["tool_choice"] == {"type": "function", "name": "lookup"}
    items = out["input"]
    types = [i.get("type") for i in items if isinstance(i, dict)]
    assert "reasoning" not in types
    assert any(i.get("type") == "item_reference" and i.get("id") == "fc1" for i in items)
    fc = next(i for i in items if i.get("type") == "function_call")
    assert fc["call_id"] == "fc1"
    fco = next(i for i in items if i.get("type") == "function_call_output")
    assert fco["call_id"] == "fc1" and fco["output"] == "ok"
    msg = next(i for i in items if i.get("type") == "message")
    assert msg["content"][0]["text"] == '{"hello":"world"}'

    # 无工具续链信号时，普通非 tool item 的 id/call_id 要剥掉，避免 store=false 引用持久化 ID。
    out2 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1",
        "input": [{"type": "message", "role": "user", "id": "msg_2", "call_id": "bad", "content": "hi"}],
    })
    assert "id" not in out2["input"][0] and "call_id" not in out2["input"][0]

    # tool_choice 指向不存在的工具时降级 auto。
    out3 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1", "input": [],
        "tools": [{"type": "function", "name": "exists"}],
        "tool_choice": {"type": "function", "name": "missing"},
    })
    assert out3["tool_choice"] == "auto"

    # sub2api 对齐：tool_search_output 是工具续链 item，call_id 应保留并规范化。
    out4 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1",
        "input": [{"type": "tool_search_output", "call_id": "call_search_1", "output": "ok"}],
    })
    assert out4["input"][0]["call_id"] == "fcsearch_1"

    # sub2api 对齐：local_shell_call / tool_search_call 不主动补 name。
    out5 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1",
        "input": [
            {"type": "local_shell_call", "call_id": "call_shell_1"},
            {"type": "tool_search_call", "call_id": "call_search_2"},
        ],
    })
    assert "name" not in out5["input"][0]
    assert "name" not in out5["input"][1]
    print("  [PASS] transform: tool_choice + item_reference/call_id/id normalization")


def test_transform_normalizes_chat_style_tools(m):
    """Commit 5 ①: responses ingress 收到 chat-style tools 时必须拍平成
    Responses-style（顶层 name/parameters）。否则 codex endpoint 会 400。"""
    t = m["transform"]
    out = t.apply_codex_oauth_transform({
        "model": "gpt-5.1", "input": "hi",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "get weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    "strict": True,
                },
            },
            {   # 已是 responses-style 的不动
                "type": "function", "name": "existing",
                "parameters": {"type": "object"},
            },
        ],
    })
    tools = out["tools"]
    # 第一个：顶层必须有 name / description / parameters / strict
    assert tools[0]["name"] == "get_weather"
    assert tools[0]["description"] == "get weather"
    assert tools[0]["parameters"]["type"] == "object"
    assert tools[0]["strict"] is True
    # 第二个：原样保留
    assert tools[1]["name"] == "existing"
    # invalid 工具会被丢弃; empty tools array stripped entirely
    out2 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1", "input": "hi",
        "tools": [{"type": "function"}],   # 无 name 也无 function 对象
    })
    assert "tools" not in out2
    assert "tool_choice" not in out2
    # 非 function 类型的工具原样保留
    out3 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1", "input": "hi",
        "tools": [{"type": "web_search"}],
    })
    assert out3["tools"] == [{"type": "web_search"}]
    print("  [PASS] transform: chat-style tools flattened; invalid dropped; non-function preserved")


def test_channel_model_passthrough(m):
    """v0.6.x 起：账号 models 列表中的名字原样透传给上游，transform 不做别名映射。"""
    _setup(m)
    m["oauth_manager"].add_account({
        "email": "alias@openai.test", "provider": "openai",
        "access_token": "x", "refresh_token": "r",
        "chatgpt_account_id": "acct-alias",
        # 新版语义：配什么名字上游就收什么名字；账号调度白名单 = 上游请求体 model。
        # 包含：新模型 (gpt-5.5) / codex 变体 / 带 reasoning 后缀的别名
        "models": ["gpt-5.5", "gpt-5.1-codex", "gpt-5.4-high"],
    })
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:alias@openai.test:acct-alias"))
    for name in ("gpt-5.5", "gpt-5.1-codex", "gpt-5.4-high"):
        assert ch.supports_model(name) == name, f"{name} should be supported"
    # 不在账户列表里的仍然拒绝
    assert ch.supports_model("gpt-5.2") is None
    assert ch.supports_model("gpt-4o") is None
    # 重点：build_upstream_request 后上游 body 的 model 完全透传，不被翻译
    import asyncio, json
    for name in ("gpt-5.5", "gpt-5.1-codex", "gpt-5.4-high"):
        req = asyncio.run(ch.build_upstream_request(
            {"model": name, "input": "hi"}, name,
            ingress_protocol="responses",
        ))
        payload = json.loads(req.body)
        assert payload["model"] == name, (
            f"model should passthrough unchanged: got {payload['model']!r}, want {name!r}"
        )
    print("  [PASS] channel: account.models passthrough to upstream unchanged")


def test_transform_model_passthrough(m):
    """transform 层对 model 字段的处理：resolved_model 直接透传。"""
    t = m["transform"]
    # resolved_model 传啥就写啥
    for name in ("gpt-5.5", "gpt-5.1-codex", "gpt-5.4-high",
                 "gpt-6-future", "some-random-name"):
        body = {"model": "anything-else", "input": "hi"}
        t.apply_codex_oauth_transform(body, resolved_model=name)
        assert body["model"] == name, (
            f"resolved_model should win unchanged: got {body['model']!r}, want {name!r}"
        )
    # resolved_model 缺失时保留 body 里的 model
    body = {"model": "gpt-5.5", "input": "hi"}
    t.apply_codex_oauth_transform(body, resolved_model=None)
    assert body["model"] == "gpt-5.5"
    # 极端兜底：两者都缺→保守默认 gpt-5
    body = {"input": "hi"}
    t.apply_codex_oauth_transform(body, resolved_model=None)
    assert body["model"] == "gpt-5"
    print("  [PASS] transform: resolved_model passthrough; no alias mapping")


# ─── Channel 构造与路由 ──────────────────────────────────────────

def test_channel_basic(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    assert ch.key == "oauth:openai:o@openai.test:acct-123"
    assert ch.account_key == "openai:o@openai.test:acct-123"
    assert ch.type == "oauth"
    assert ch.protocol == "openai-responses"
    assert ch.cc_mimicry is False
    assert ch.chatgpt_account_id == "acct-123"
    assert ch.supports_model("gpt-5.1") == "gpt-5.1"
    assert ch.supports_model("not-in-list") is None
    disp = ch.display()
    assert disp.type == "oauth"
    assert "o@openai.test" in disp.display_name
    assert "acct-123" not in disp.display_name
    print("  [PASS] channel: basic attrs / supports_model / display")


def test_channel_default_models_fallback(m):
    """账户不设 models → Channel 回落到 config.openaiOAuth.defaultModels"""
    _setup(m)
    # 直接调 add_account（不走 _add_openai_acc helper，后者会塞硬编码的 models）
    m["oauth_manager"].add_account({
        "email": "no-models@x",
        "provider": "openai",
        "access_token": "x", "refresh_token": "r",
        "chatgpt_account_id": "acct",
        # 故意不给 models
    })
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:no-models@x:acct"))
    models = ch.list_client_models()
    # 默认模型跟随 Codex 官方目录：GPT-5.6 系列优先，保留旧稳定模型。
    expected = {
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
        "gpt-5.2", "gpt-5.2-codex", "gpt-5.3-codex",
    }
    assert set(models) == expected, models
    # supports_model 命中
    for m_id in expected:
        assert ch.supports_model(m_id) == m_id
    # 不在默认列表的别名不会命中（需用户手动补 models）
    assert ch.supports_model("gpt-5") is None
    assert ch.supports_model("gpt-5.1") is None
    print("  [PASS] channel: default models from openaiOAuth.defaultModels")


def test_channel_responses_ingress(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {"model": "gpt-5.1", "input": "hi", "stream": False, "temperature": 0.3}
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1",
                                                ingress_protocol="responses"))
    assert req.url == m["CODEX_UPSTREAM_URL"]
    assert req.translator_ctx is None          # 无 session anchor 时同协议透传无需 ctx
    h = {k.lower(): v for k, v in req.headers.items()}
    assert h["chatgpt-account-id"] == "acct-123"
    assert h["openai-beta"] == "responses=experimental"
    assert h["originator"] == "codex_cli_rs"
    assert h["version"] == "0.144.0"
    assert h["accept"] == "text/event-stream"
    assert h["user-agent"] == m["CODEX_CLI_USER_AGENT"]
    assert h["authorization"].startswith("Bearer ")
    assert h.get("host") == "chatgpt.com"
    payload = json.loads(req.body)
    assert payload["model"] == "gpt-5.1"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert "temperature" not in payload
    assert "x-openai-internal-codex-responses-lite" not in h
    print("  [PASS] channel: responses ingress → full codex request shape")


def test_channel_responses_ingress_gpt56_enables_responses_lite(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {"model": "gpt-5.6-luna", "input": "hi", "stream": False}
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.6-luna",
                                                ingress_protocol="responses"))
    h = {k.lower(): v for k, v in req.headers.items()}
    assert h["version"] == "0.144.0"
    assert h["user-agent"] == m["CODEX_CLI_USER_AGENT"]
    assert h["x-openai-internal-codex-responses-lite"] == "true"
    payload = json.loads(req.body)
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["instructions"] == ""
    assert payload["parallel_tool_calls"] is False
    assert payload["reasoning"]["context"] == "all_turns"
    assert "tools" not in payload
    assert payload["input"][0] == {"type": "additional_tools", "role": "developer", "tools": []}
    assert payload["input"][1]["role"] == "developer"
    assert payload["input"][1]["content"][0]["type"] == "input_text"
    assert payload["input"][2] == {"type": "message", "role": "user", "content": "hi"}
    print("  [PASS] channel: GPT-5.6 enables Codex Responses Lite")


def test_channel_responses_ingress_replay_scope_and_injection(m):
    _setup(m)
    _add_openai_acc(m)
    rr = m["reasoning_replay"]
    encrypted_content = _valid_encrypted_content(13)
    rr.cache_items(
        "gpt-5.1",
        "prompt-cache:anchor",
        [{"type": "reasoning", "encrypted_content": encrypted_content}],
    )
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {"model": "gpt-5.1", "input": "continue", "prompt_cache_key": "anchor"}
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
    ctx = req.translator_ctx
    assert ctx["codex_reasoning_replay"] == {"model": "gpt-5.1", "session_key": "prompt-cache:anchor"}
    assert ctx["codex_reasoning_replay_injected"] == 1
    payload = json.loads(req.body)
    assert payload["input"][0] == {"type": "reasoning", "summary": [], "content": None, "encrypted_content": encrypted_content}
    assert payload["input"][1] == {"type": "message", "role": "user", "content": "continue"}
    print("  [PASS] channel: responses ingress injects cached reasoning replay")


def test_channel_chat_ingress_translator(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {
        "model": "gpt-5.1",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "response_format": {"type": "json_schema"},
        "_api_key_name": "internal",
    }
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1",
                                                ingress_protocol="chat"))
    assert req.url == m["CODEX_UPSTREAM_URL"]
    ctx = req.translator_ctx
    assert ctx["ingress"] == "chat"
    assert ctx["upstream_protocol"] == "openai-responses"
    assert ctx["response_translator"] == "chat_to_responses"
    assert ctx["model_for_response"] == "gpt-5.1"
    assert ctx["include_usage"] is True
    payload = json.loads(req.body)
    # chat→responses translator 应该已把 messages 翻译成 input
    assert isinstance(payload.get("input"), list) and payload["input"]
    assert "response_format" not in payload
    assert "_api_key_name" not in payload
    # codex transform 强制 flag
    assert payload["stream"] is True
    assert payload["store"] is False
    print("  [PASS] channel: chat ingress → translator_ctx + input converted")


def test_channel_filters_translated_payload_before_codex_transform(m, monkeypatch):
    _setup(m)
    _add_openai_acc(m)
    from src.channel import openai_oauth_channel as oauth_mod

    def fake_translate_request(body, *, target_model=None, codex_oauth=False):
        return {
            "model": target_model or body.get("model"),
            "input": "hi",
            "stream": False,
            "messages": [{"role": "user", "content": "should not leak"}],
            "response_format": {"type": "json_schema"},
            "container": {"id": "anthropic-only"},
            "_api_key_name": "internal",
        }

    monkeypatch.setattr(oauth_mod.anthropic_to_responses, "translate_request", fake_translate_request)

    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    req = asyncio.run(ch.build_upstream_request(
        {"model": "gpt-5.1", "messages": [{"role": "user", "content": "hi"}]},
        "gpt-5.1",
        ingress_protocol="anthropic",
    ))
    payload = json.loads(req.body)

    assert payload["model"] == "gpt-5.1"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert "messages" not in payload
    assert "response_format" not in payload
    assert "container" not in payload
    assert "_api_key_name" not in payload


def test_channel_previous_response_id_rejected(m):
    """OAuth Codex HTTP SSE route store=false：previous_response_id 不允许直透。"""
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    try:
        asyncio.run(ch.build_upstream_request(
            {"model": "gpt-5.1", "input": "continue", "previous_response_id": "resp_1"},
            "gpt-5.1", ingress_protocol="responses",
        ))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "previous_response_id" in str(exc) and "store=false" in str(exc), str(exc)
    print("  [PASS] channel: previous_response_id rejected on OAuth store=false route")


def test_channel_codex_rejects_unsupported_responses_server_state(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))

    for body, label in (
        ({"model": "gpt-5.1", "conversation": "conv_1", "input": "hi"}, "conversation"),
        ({"model": "gpt-5.1", "background": True, "input": "hi"}, "background"),
        ({"model": "gpt-5.1", "input": "hi", "tools": [{"type": "web_search", "name": "search"}]}, "tools:web_search"),
        ({"model": "gpt-5.1", "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_file", "file_id": "file_doc"},
        ]}]}, "input_file.file_id"),
        ({"model": "gpt-5.1", "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "AAAA"}},
        ]}]}, "input_audio"),
    ):
        try:
            asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert label in str(exc), str(exc)

    print("  [PASS] channel: Codex rejects unsupported Responses server-state before upstream")


def test_channel_codex_rejects_translated_chat_file_id(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))

    try:
        asyncio.run(ch.build_upstream_request({
            "model": "gpt-5.1",
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"file_id": "file_img"}},
            ]}],
        }, "gpt-5.1", ingress_protocol="chat"))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "input_image.file_id" in str(exc), str(exc)

    print("  [PASS] channel: translated Chat file_id rejected on Codex route")


def test_channel_anthropic_ingress_translator(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {
        "model": "gpt-5.1",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32,
        "stream": False,
    }
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="anthropic"))
    assert req.url == m["CODEX_UPSTREAM_URL"]
    ctx = req.translator_ctx
    assert ctx["ingress"] == "anthropic"
    assert ctx["upstream_protocol"] == "openai-responses"
    assert ctx["response_translator"] == "anthropic_to_responses"
    assert ctx["model_for_response"] == "gpt-5.1"
    payload = json.loads(req.body)
    assert payload["model"] == "gpt-5.1"
    assert payload["store"] is False
    assert payload["stream"] is True
    # Anthropic→Responses translator 先把 messages 转成 input；Codex transform
    # 再保留 include=reasoning.encrypted_content 透明透传能力。
    assert payload["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert "max_output_tokens" not in payload   # Codex transform 会剥不支持字段
    print("  [PASS] channel: anthropic ingress → codex responses translator_ctx")


def test_channel_anthropic_ingress_keeps_history_system_at_tail_for_cache(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {
        "model": "gpt-5.1",
        "system": [{"type": "text", "text": "stable root system"}],
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "system", "content": "dynamic reminder should stay in tail"},
            {"role": "user", "content": "continue"},
        ],
        "metadata": {"user_id": '{"session_id":"session-abc"}'},
        "_parrot_api_key_name": "cc-switch",
        "_parrot_client_ip": "203.0.113.8",
    }

    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="anthropic"))
    payload = json.loads(req.body)

    assert payload["instructions"] == "stable root system"
    roles = [item.get("role") for item in payload["input"] if item.get("type") == "message"]
    assert roles == ["user", "developer", "user"]
    assert any(
        item.get("role") == "developer" and item.get("content") == [{"type": "input_text", "text": "dynamic reminder should stay in tail"}]
        for item in payload["input"]
    )
    print("  [PASS] channel: Anthropic history system stays as developer tail on Codex route")


def test_channel_anthropic_ingress_maps_cache_to_prompt_cache_and_session(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {
        "model": "gpt-5.1",
        "system": [{"type": "text", "text": "stable system", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": '{"device_id":"dev-1","session_id":"session-abc"}'},
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
        "_parrot_api_key_name": "cc-switch",
        "_parrot_client_ip": "203.0.113.8",
    }

    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="anthropic"))
    payload = json.loads(req.body)

    assert payload["prompt_cache_key"].startswith("parrot:cache:v1:a2o-session:")
    assert "prompt_cache_retention" not in payload  # Codex endpoint rejects retention; transform strips it.
    assert "metadata" not in payload               # stripped only after deriving the cache/session key.
    sid = req.headers.get("session_id")
    assert sid and len(sid) == 16 and all(ch_ in "0123456789abcdef" for ch_ in sid)
    assert "conversation_id" not in req.headers
    print("  [PASS] channel: anthropic ingress maps cache_control/session to Codex prompt_cache_key + session_id")


def test_channel_anthropic_ingress_metadata_session_replay(m):
    _setup(m)
    _add_openai_acc(m)
    rr = m["reasoning_replay"]
    encrypted_content = _valid_encrypted_content(17)
    rr.cache_items(
        "gpt-5.1",
        "claude:session-abc",
        [{"type": "reasoning", "encrypted_content": encrypted_content}],
    )
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {
        "model": "gpt-5.1",
        "messages": [{"role": "user", "content": "continue"}],
        "max_tokens": 32,
        "metadata": {"user_id": '{"session_id":"session-abc"}'},
    }
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="anthropic"))
    ctx = req.translator_ctx
    assert ctx["codex_reasoning_replay"] == {"model": "gpt-5.1", "session_key": "claude:session-abc"}
    assert ctx["codex_reasoning_replay_injected"] == 1
    payload = json.loads(req.body)
    assert payload["input"][0]["type"] == "reasoning"
    assert payload["input"][0]["encrypted_content"] == encrypted_content
    assert "metadata" not in payload  # Codex transform strips API metadata after deriving scope.
    print("  [PASS] channel: anthropic metadata session injects reasoning replay")


def test_channel_missing_chatgpt_account_id_legacy_keeps_working(m):
    _setup(m)
    _add_openai_acc(m, email="no-acct@x", chatgpt_account_id="")
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:no-acct@x"))
    req = asyncio.run(ch.build_upstream_request(
        {"model": "gpt-5.1", "input": "hi"}, "gpt-5.1",
        ingress_protocol="responses",
    ))
    assert "chatgpt-account-id" not in req.headers
    assert req.headers["authorization"].startswith("Bearer ")
    print("  [PASS] channel: legacy missing chatgpt_account_id keeps working without header")


# ─── registry 分派 ───────────────────────────────────────────────

def test_registry_dispatches_by_provider(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "c@claude.test",
        "provider": "claude",
        "access_token": "a", "refresh_token": "r",
    })
    _add_openai_acc(m, email="o@openai.test")

    m["registry"].rebuild_from_config()
    chs = {ch.key: ch for ch in m["registry"].all_channels()}
    claude = chs["oauth:claude:c@claude.test"]
    openai = chs["oauth:openai:o@openai.test:acct-123"]
    assert isinstance(claude, m["OAuthChannel"]), type(claude).__name__
    assert isinstance(openai, m["OpenAIOAuthChannel"]), type(openai).__name__
    assert claude.protocol == "anthropic"
    assert openai.protocol == "openai-responses"
    print("  [PASS] registry: dispatches OAuth by provider field")


def test_openai_oauth_channel_max_concurrent(m):
    _setup(m)
    om = m["oauth_manager"]
    m["config"].update(lambda c: c.setdefault("concurrency", {}).__setitem__("defaultMaxConcurrent", 0))
    _add_openai_acc(m, email="limited@openai.test")
    om.update_max_concurrent("openai:limited@openai.test:acct-123", 2)

    m["registry"].rebuild_from_config()
    ch = m["registry"].get_channel("oauth:openai:limited@openai.test:acct-123")
    assert isinstance(ch, m["OpenAIOAuthChannel"]), type(ch).__name__
    assert ch.max_concurrent == 2

    from src import concurrency
    assert concurrency._get_channel_max("oauth:openai:limited@openai.test:acct-123") == 2
    print("  [PASS] OpenAI OAuth channel honors maxConcurrent")


def test_session_id_isolation_with_prompt_cache_key(m):
    """Commit 4: 下游 prompt_cache_key + api_key_name 派生上游 session_id。"""
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body_a = {
        "model": "gpt-5.1",
        "input": "hi",
        "prompt_cache_key": "chat-abc",
        "_api_key_name": "user_alice",
    }
    body_b = {
        "model": "gpt-5.1",
        "input": "hi",
        "prompt_cache_key": "chat-abc",   # 同一个 cache_key
        "_api_key_name": "user_bob",       # 不同 api_key_name
    }
    req_a = asyncio.run(ch.build_upstream_request(body_a, "gpt-5.1", ingress_protocol="responses"))
    req_b = asyncio.run(ch.build_upstream_request(body_b, "gpt-5.1", ingress_protocol="responses"))
    sid_a = req_a.headers.get("session_id")
    sid_b = req_b.headers.get("session_id")
    assert sid_a and sid_b
    assert sid_a != sid_b, "相同 prompt_cache_key 的不同 api_key 不应共享 session_id"
    # conversation_id deprecated — should no longer be present
    assert "conversation_id" not in req_a.headers
    # 长度 16 hex
    assert len(sid_a) == 16 and all(ch_ in "0123456789abcdef" for ch_ in sid_a)
    print("  [PASS] session_id: api_key_name-based isolation, conversation_id removed")


def test_session_id_isolation_disabled(m):
    """isolateSessionId=False 时不写 session_id / conversation_id 头。"""
    _setup(m)
    _add_openai_acc(m)
    def _off(c):
        c.setdefault("openaiOAuth", {})["isolateSessionId"] = False
    m["config"].update(_off)

    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    body = {
        "model": "gpt-5.1", "input": "hi",
        "prompt_cache_key": "chat-abc", "_api_key_name": "alice",
    }
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
    assert "session_id" not in req.headers
    assert "conversation_id" not in req.headers

    # 恢复默认
    def _on(c):
        c.setdefault("openaiOAuth", {})["isolateSessionId"] = True
    m["config"].update(_on)
    print("  [PASS] session_id: isolateSessionId=false disables header injection")


def test_force_codex_cli_switch(m):
    """forceCodexCLI=True（默认）写死 codex UA；=False 则不设 UA。"""
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))

    # 默认 True
    body = {"model": "gpt-5.1", "input": "hi"}
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
    assert req.headers.get("user-agent") == m["CODEX_CLI_USER_AGENT"]

    # 关掉
    def _off(c):
        c.setdefault("openaiOAuth", {})["forceCodexCLI"] = False
    m["config"].update(_off)
    req2 = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
    assert "user-agent" not in req2.headers

    # 恢复
    def _on(c):
        c.setdefault("openaiOAuth", {})["forceCodexCLI"] = True
    m["config"].update(_on)
    print("  [PASS] forceCodexCLI switch: True injects UA, False omits it")


def test_openai_oauth_legacy_provider_runtime_fallback_when_short_config_default(m):
    _setup(m)
    _add_openai_acc(m)
    def _legacy(c):
        c.setdefault("oauth", {}).setdefault("providers", {})["openai"] = {"forceCodexCLI": False}
        c["openaiOAuth"] = dict(m["config"].DEFAULT_CONFIG["openaiOAuth"])
    m["config"].update(_legacy)

    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    req = asyncio.run(ch.build_upstream_request({"model": "gpt-5.1", "input": "hi"}, "gpt-5.1", ingress_protocol="responses"))
    assert "user-agent" not in req.headers
    print("  [PASS] legacy oauth.providers.openai still works when openaiOAuth is default")


def test_openai_oauth_short_config_overrides_codex_url_and_default_instructions(m):
    _setup(m)
    _add_openai_acc(m)
    def _custom(c):
        c.setdefault("openaiOAuth", {})["codexUpstreamUrl"] = "https://example.test/backend-api/codex/responses"
        c.setdefault("openaiOAuth", {})["defaultInstructions"] = "Custom default instructions."
        c.setdefault("openaiOAuth", {})["forceCodexCLI"] = False
    m["config"].update(_custom)

    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("openai:o@openai.test:acct-123"))
    req = asyncio.run(ch.build_upstream_request({"model": "gpt-5.1", "input": "hi"}, "gpt-5.1", ingress_protocol="responses"))
    payload = json.loads(req.body)
    assert req.url == "https://example.test/backend-api/codex/responses"
    assert payload["instructions"] == "Custom default instructions."
    assert "user-agent" not in req.headers
    print("  [PASS] openaiOAuth short config overrides Codex URL/instructions/UA")


def test_config_backfills_openai_oauth_from_legacy_provider(m):
    raw = {
        "oauth": {
            "providers": {
                "openai": {
                    "forceCodexCLI": False,
                    "isolateSessionId": False,
                    "defaultModels": ["legacy-model"],
                }
            }
        }
    }
    merged = m["config"]._deep_merge_defaults(m["config"].DEFAULT_CONFIG, raw)
    changed = m["config"]._normalize_openai_oauth_config(merged, raw)
    assert changed is True
    assert merged["openaiOAuth"]["forceCodexCLI"] is False
    assert merged["openaiOAuth"]["isolateSessionId"] is False
    assert merged["openaiOAuth"]["defaultModels"] == ["legacy-model"]
    assert merged["openaiOAuth"]["codexUpstreamUrl"].startswith("https://chatgpt.com/")
    print("  [PASS] config: legacy oauth.providers.openai backfills openaiOAuth")


def test_registry_legacy_account_defaults_to_claude(m):
    _setup(m)
    # 模拟老账户：直接通过 config 写（不走 add_account，不带 provider 字段）
    def _legacy(c):
        c["oauthAccounts"] = [{
            "email": "legacy@old",
            "access_token": "a", "refresh_token": "r",
            "enabled": True,
        }]
    m["config"].update(_legacy)
    # 不做 migrate_provider_field，直接跑 registry —— 它应当读 normalize_provider
    # 回落到 "claude"，不应崩
    m["registry"].rebuild_from_config()
    ch = m["registry"].get_channel("oauth:legacy@old")
    assert ch is not None
    assert isinstance(ch, m["OAuthChannel"])
    print("  [PASS] registry: legacy account without provider → Claude channel")


# ─── main ────────────────────────────────────────────────────────

def main():
    m = _import_modules()
    m["state_db"].init()

    orig_cfg = json.loads(json.dumps(m["config"].get()))

    tests = [
        test_transform_basic,
        test_transform_keeps_resolved_model,
        test_transform_extracts_system,
        test_transform_system_appended_to_existing_instructions,
        test_transform_legacy_functions,
        test_transform_tool_choice_and_input_refs,
        test_transform_normalizes_chat_style_tools,
        test_channel_model_passthrough,
        test_transform_model_passthrough,
        test_channel_basic,
        test_channel_default_models_fallback,
        test_channel_responses_ingress,
        test_channel_responses_ingress_replay_scope_and_injection,
        test_channel_chat_ingress_translator,
        test_channel_previous_response_id_rejected,
        test_channel_codex_rejects_unsupported_responses_server_state,
        test_channel_codex_rejects_translated_chat_file_id,
        test_channel_anthropic_ingress_translator,
        test_channel_anthropic_ingress_keeps_history_system_at_tail_for_cache,
        test_channel_anthropic_ingress_maps_cache_to_prompt_cache_and_session,
        test_channel_anthropic_ingress_metadata_session_replay,
        test_channel_missing_chatgpt_account_id_legacy_keeps_working,
        test_registry_dispatches_by_provider,
        test_openai_oauth_channel_max_concurrent,
        test_session_id_isolation_with_prompt_cache_key,
        test_session_id_isolation_disabled,
        test_force_codex_cli_switch,
        test_openai_oauth_legacy_provider_runtime_fallback_when_short_config_default,
        test_openai_oauth_short_config_overrides_codex_url_and_default_instructions,
        test_config_backfills_openai_oauth_from_legacy_provider,
        test_registry_legacy_account_defaults_to_claude,
    ]

    passed = 0
    try:
        for t in tests:
            try:
                t(m)
                passed += 1
            except AssertionError as exc:
                print(f"  [FAIL] {t.__name__}: {exc}")
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"  [ERR]  {t.__name__}: {exc}")
    finally:
        m["config"].update(lambda c: (c.clear(), c.update(orig_cfg)))

    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())


def test_codex_transform_injects_include_encrypted_content():
    """v3: codex transform 必须主动注入 include=reasoning.encrypted_content。
    store=false 下上游仅在显式 include 时返回加密块，不能依赖下游带。"""
    import src.openai.transform.codex_oauth_transform as t
    # 下游完全没带 include
    out = t.apply_codex_oauth_transform(
        {"model": "gpt-5.5", "input": [{"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]}]},
        resolved_model="gpt-5.5")
    assert "reasoning.encrypted_content" in (out.get("include") or [])


def test_codex_transform_include_no_duplicate():
    """下游已带 include 时不重复注入。"""
    import src.openai.transform.codex_oauth_transform as t
    out = t.apply_codex_oauth_transform(
        {"model": "gpt-5.5", "include": ["reasoning.encrypted_content"],
         "input": [{"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "hi"}]}]},
        resolved_model="gpt-5.5")
    assert (out.get("include") or []).count("reasoning.encrypted_content") == 1


def test_codex_transform_reasoning_with_enc_preserved():
    """v3 Fix A: 带合法 encrypted_content 的 reasoning 块在 input 里要保留透传。"""
    import src.openai.transform.codex_oauth_transform as t
    out = t.apply_codex_oauth_transform(
        {"model": "gpt-5.5", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "encrypted_content": _valid_encrypted_content(19), "summary": []},
        ]},
        resolved_model="gpt-5.5")
    kinds = [it.get("type") for it in out["input"]]
    assert "reasoning" in kinds


def test_codex_transform_bare_reasoning_dropped():
    """裸 reasoning（无 encrypted_content）仍被丢弃。"""
    import src.openai.transform.codex_oauth_transform as t
    out = t.apply_codex_oauth_transform(
        {"model": "gpt-5.5", "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "summary": []},
        ]},
        resolved_model="gpt-5.5")
    kinds = [it.get("type") for it in out["input"]]
    assert "reasoning" not in kinds
