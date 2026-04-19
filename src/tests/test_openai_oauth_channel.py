"""OpenAIOAuthChannel + codex_oauth_transform 测试（Commit 2）。

覆盖：
  - codex_oauth_transform.apply_codex_oauth_transform 的强制改造语义：
    store=false / stream=true / 不支持字段剥离 / 模型名规范化 / input 字符串
    包成消息数组 / input 里 system 提 instructions / instructions 兜底 /
    legacy functions-function_call 转换
  - normalize_codex_model 的映射表 + 通配兜底
  - registry.rebuild_from_config 按 provider 分派 OAuth 渠道
  - OpenAIOAuthChannel.build_upstream_request：
      * responses ingress 透传 + 强制改造 + 完整 headers
      * chat ingress 先走 chat_to_responses.translate_request 再 codex transform
      * anthropic ingress 直接抛（本家族兼容，不跨家族）
      * chatgpt_account_id 缺失时校验错误
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
import json
import os
import sys


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
    }


def _setup(m):
    m["state_db"].init()
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
    }
    out = t.apply_codex_oauth_transform(body)
    assert out["model"] == "gpt-5.1"               # 别名 → 规范名
    assert out["store"] is False                   # 强制
    assert out["stream"] is True                   # 强制
    for k in ("temperature", "top_p", "max_output_tokens",
              "frequency_penalty", "presence_penalty", "prompt_cache_retention"):
        assert k not in out, f"{k} should be stripped"
    assert out["input"] == [{"type": "message", "role": "user", "content": "hi"}]
    assert out["instructions"] == "You are a helpful coding assistant."
    print("  [PASS] transform: basic forced flags + strip + model normalize")


def test_transform_keeps_resolved_model(m):
    t = m["transform"]
    # 传了 resolved_model 但 body 有 model → 仍规范化 body.model
    out = t.apply_codex_oauth_transform(
        {"model": "gpt-5-codex", "input": []},
        resolved_model="gpt-5-codex",
    )
    assert out["model"] == "gpt-5.1-codex"
    # body 无 model → 用 resolved_model
    out2 = t.apply_codex_oauth_transform(
        {"input": []}, resolved_model="gpt-5-codex",
    )
    assert out2["model"] == "gpt-5.1-codex"
    print("  [PASS] transform: resolved_model fallback + normalize both ways")


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
    assert out["tools"] == [
        {"type": "function", "function": {"name": "f1"}},
        {"type": "function", "function": {"name": "f2"}},
    ]
    assert out["tool_choice"] == {"type": "function", "function": {"name": "f1"}}
    # string function_call (auto)
    out2 = t.apply_codex_oauth_transform({
        "model": "gpt-5.1", "input": [], "function_call": "auto",
    })
    assert out2["tool_choice"] == "auto"
    print("  [PASS] transform: legacy functions/function_call → tools/tool_choice")


def test_transform_model_map(m):
    t = m["transform"]
    cases = [
        ("gpt-5", "gpt-5.1"),
        ("gpt-5-codex", "gpt-5.1-codex"),
        ("gpt-5.3-xhigh", "gpt-5.3-codex"),
        ("gpt-5.1-codex-max-xhigh", "gpt-5.1-codex-max"),
        ("openai/gpt-5-mini", "gpt-5.1"),
        ("gpt-4o-mini", "gpt-5.1"),                # 通配兜底
        ("something-codex", "gpt-5.1-codex"),      # 通配 codex
        ("", "gpt-5.1"),                           # 空
    ]
    for src, expect in cases:
        got = t.normalize_codex_model(src)
        assert got == expect, f"{src!r} → {got!r}, expected {expect!r}"
    # 至少覆盖 12 条 model ID
    assert len(t.codex_model_ids()) >= 10
    print(f"  [PASS] transform: normalize_codex_model × {len(cases)} + codex_model_ids()")


# ─── Channel 构造与路由 ──────────────────────────────────────────

def test_channel_basic(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("o@openai.test"))
    assert ch.key == "oauth:o@openai.test"
    assert ch.type == "oauth"
    assert ch.protocol == "openai-responses"
    assert ch.cc_mimicry is False
    assert ch.chatgpt_account_id == "acct-123"
    assert ch.supports_model("gpt-5.1") == "gpt-5.1"
    assert ch.supports_model("not-in-list") is None
    disp = ch.display()
    assert disp.type == "oauth"
    assert disp.display_name == "o@openai.test"
    print("  [PASS] channel: basic attrs / supports_model / display")


def test_channel_default_models_fallback(m):
    _setup(m)
    # 账户不设 models → Channel 回落到 codex_model_ids
    _add_openai_acc(m, email="no-models@x", models=[])
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("no-models@x"))
    models = ch.list_client_models()
    assert "gpt-5.1" in models and "gpt-5.1-codex" in models
    print("  [PASS] channel: default models fallback when account.models=[]")


def test_channel_responses_ingress(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("o@openai.test"))
    body = {"model": "gpt-5.1", "input": "hi", "stream": False, "temperature": 0.3}
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1",
                                                ingress_protocol="responses"))
    assert req.url == m["CODEX_UPSTREAM_URL"]
    assert req.translator_ctx is None          # 同协议透传无需反向
    h = {k.lower(): v for k, v in req.headers.items()}
    assert h["chatgpt-account-id"] == "acct-123"
    assert h["openai-beta"] == "responses=experimental"
    assert h["originator"] == "codex_cli_rs"
    assert h["accept"] == "text/event-stream"
    assert h["user-agent"] == m["CODEX_CLI_USER_AGENT"]
    assert h["authorization"].startswith("Bearer ")
    assert h.get("host") == "chatgpt.com"
    payload = json.loads(req.body)
    assert payload["model"] == "gpt-5.1"
    assert payload["store"] is False
    assert payload["stream"] is True
    assert "temperature" not in payload
    print("  [PASS] channel: responses ingress → full codex request shape")


def test_channel_chat_ingress_translator(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("o@openai.test"))
    body = {
        "model": "gpt-5.1",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
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
    # codex transform 强制 flag
    assert payload["stream"] is True
    assert payload["store"] is False
    print("  [PASS] channel: chat ingress → translator_ctx + input converted")


def test_channel_anthropic_ingress_rejected(m):
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("o@openai.test"))
    try:
        asyncio.run(ch.build_upstream_request(
            {"model": "gpt-5.1", "input": "hi"}, "gpt-5.1",
            ingress_protocol="anthropic",
        ))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "only serves" in str(exc) or "anthropic" in str(exc), str(exc)
    print("  [PASS] channel: anthropic ingress rejected with ValueError")


def test_channel_missing_chatgpt_account_id(m):
    _setup(m)
    _add_openai_acc(m, email="no-acct@x", chatgpt_account_id="")
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("no-acct@x"))
    try:
        asyncio.run(ch.build_upstream_request(
            {"model": "gpt-5.1", "input": "hi"}, "gpt-5.1",
            ingress_protocol="responses",
        ))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "chatgpt_account_id" in str(exc)
    print("  [PASS] channel: missing chatgpt_account_id → build fails clearly")


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
    claude = chs["oauth:c@claude.test"]
    openai = chs["oauth:o@openai.test"]
    assert isinstance(claude, m["OAuthChannel"]), type(claude).__name__
    assert isinstance(openai, m["OpenAIOAuthChannel"]), type(openai).__name__
    assert claude.protocol == "anthropic"
    assert openai.protocol == "openai-responses"
    print("  [PASS] registry: dispatches OAuth by provider field")


def test_session_id_isolation_with_prompt_cache_key(m):
    """Commit 4: 下游 prompt_cache_key + api_key_name 派生上游 session_id。"""
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("o@openai.test"))
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
    # conversation_id 与 session_id 一致
    assert req_a.headers.get("conversation_id") == sid_a
    # 长度 16 hex
    assert len(sid_a) == 16 and all(ch_ in "0123456789abcdef" for ch_ in sid_a)
    print("  [PASS] session_id: api_key_name-based isolation, conversation_id aligned")


def test_session_id_isolation_disabled(m):
    """isolateSessionId=False 时不写 session_id / conversation_id 头。"""
    _setup(m)
    _add_openai_acc(m)
    def _off(c):
        c.setdefault("oauth", {}).setdefault("providers", {}).setdefault(
            "openai", {})["isolateSessionId"] = False
    m["config"].update(_off)

    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("o@openai.test"))
    body = {
        "model": "gpt-5.1", "input": "hi",
        "prompt_cache_key": "chat-abc", "_api_key_name": "alice",
    }
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
    assert "session_id" not in req.headers
    assert "conversation_id" not in req.headers

    # 恢复默认
    def _on(c):
        c["oauth"]["providers"]["openai"]["isolateSessionId"] = True
    m["config"].update(_on)
    print("  [PASS] session_id: isolateSessionId=false disables header injection")


def test_force_codex_cli_switch(m):
    """forceCodexCLI=True（默认）写死 codex UA；=False 则不设 UA。"""
    _setup(m)
    _add_openai_acc(m)
    ch = m["OpenAIOAuthChannel"](m["oauth_manager"].get_account("o@openai.test"))

    # 默认 True
    body = {"model": "gpt-5.1", "input": "hi"}
    req = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
    assert req.headers.get("user-agent") == m["CODEX_CLI_USER_AGENT"]

    # 关掉
    def _off(c):
        c.setdefault("oauth", {}).setdefault("providers", {}).setdefault(
            "openai", {})["forceCodexCLI"] = False
    m["config"].update(_off)
    req2 = asyncio.run(ch.build_upstream_request(body, "gpt-5.1", ingress_protocol="responses"))
    assert "user-agent" not in req2.headers

    # 恢复
    def _on(c):
        c["oauth"]["providers"]["openai"]["forceCodexCLI"] = True
    m["config"].update(_on)
    print("  [PASS] forceCodexCLI switch: True injects UA, False omits it")


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
        test_transform_model_map,
        test_channel_basic,
        test_channel_default_models_fallback,
        test_channel_responses_ingress,
        test_channel_chat_ingress_translator,
        test_channel_anthropic_ingress_rejected,
        test_channel_missing_chatgpt_account_id,
        test_registry_dispatches_by_provider,
        test_session_id_isolation_with_prompt_cache_key,
        test_session_id_isolation_disabled,
        test_force_codex_cli_switch,
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
