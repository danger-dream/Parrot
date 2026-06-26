"""CC v2.1.156 兼容升级回归测试。

覆盖本轮升级的全部代码改动，防止后续回归：
  §1  CC_VERSION = 2.1.156
  §2  CCH_SEED 新值（对 6 组 canonical body _ph.bin 离线 6/6 命中）
  §3  compute_fingerprint：取「第一个 user message 的最后一个 text block」+ 索引 [4,7,18]
      （6 组 canonical body 离线 6/6 命中）；FINGERPRINT_SALT 不变
  §5  CC_ENTRYPOINT = sdk-cli；CLI_USER_AGENT 三者自洽
  §6  BETAS：含 context-1m / mid-conversation-system；不含 redact-thinking；
      messages 出站 anthropic-beta 过滤 oauth-2025-04-20
  §7  build_upstream_headers：补 X-Stainless 全层 + session-id + dangerous-direct-browser；
      删 x-client-request-id；Accept-Encoding 仅 gzip,deflate
  §8  build_metadata 带 session_id；与 header X-Claude-Code-Session-Id 同源
  §15 transform_request：wire order；不注入 temperature；默认 max_tokens=64000

fixtures: /opt/workspace/claude-code-cch/v2.1.156/bodies（只读存量抓包，离线，不触网）。
若 fixtures 目录缺失（换机/未挂载），相关用例 skip，不算失败。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys

import pytest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)
from src.tests import _isolation

_isolation.isolate()

from src.transform import cc_mimicry as m

try:
    import xxhash
    _HAS_XXHASH = True
except ImportError:
    _HAS_XXHASH = False

BODIES = "/opt/workspace/claude-code-cch/v2.1.156/bodies"

# 6 组 canonical body 的真实 fp（cc_version 后 3 hex）与 cch（5 hex），来自抓包
FP_EXPECTED = {
    "default": "523", "effort_high": "472", "effort_low": "664",
    "effort_max": "379", "effort_medium": "b3c", "effort_xhigh": "f53",
}
CCH_EXPECTED = {
    "default": "e5eff", "effort_high": "b383b", "effort_low": "2652c",
    "effort_max": "29b0f", "effort_medium": "be4eb", "effort_xhigh": "64fb0",
}

_bodies_missing = not os.path.isdir(BODIES)
_skip_fixtures = pytest.mark.skipif(
    _bodies_missing, reason=f"canonical bodies fixtures not present: {BODIES}"
)


# ─────────────────────────── 常量同步 (§1/§2/§3/§5) ───────────────────────────

def test_cc_version_2_1_156():
    assert m.CC_VERSION == "2.1.156"


def test_cc_entrypoint_sdk_cli():
    assert m.CC_ENTRYPOINT == "sdk-cli"


def test_user_agent_self_consistent():
    # version / entrypoint / UA 三者必须同步，绝不半新半旧
    assert m.CLI_USER_AGENT == "claude-cli/2.1.156 (external, sdk-cli)"


def test_cch_seed_new_value():
    assert m.CCH_SEED == 0x4D659218E32A3268


def test_fingerprint_salt_unchanged():
    # v2.1.156 fp 算法变的是「取哪个 block + 索引」，salt 本身不变
    assert m.FINGERPRINT_SALT == "59cf53e54c78"


def test_fingerprint_indices_18_not_20():
    # 索引应为 [4,7,18]（旧版 [4,7,20]）。直接对短文本验证行为：
    # 取最后一个 text block，'hello world' 在 [4,7,18] => 'o','o',越界'0' => 'oo0'
    fp = m.compute_fingerprint([{"role": "user", "content": [
        {"type": "text", "text": "skip-me-first-block"},
        {"type": "text", "text": "hello world"},
    ]}])
    expect = hashlib.sha256(f"59cf53e54c78oo02.1.156".encode()).hexdigest()[:3]
    assert fp == expect == "523"


# ─────────────────────────── fp 算法 6/6 (§3) ───────────────────────────

@_skip_fixtures
@pytest.mark.parametrize("name", list(FP_EXPECTED))
def test_compute_fingerprint_canonical_6of6(name):
    d = json.loads(open(f"{BODIES}/body_{name}_original.bin", "rb").read())
    got = m.compute_fingerprint(d["messages"])
    assert got == FP_EXPECTED[name], f"{name}: fp {got} != {FP_EXPECTED[name]}"


def test_fingerprint_takes_last_text_block():
    # 关键回归点：必须取「最后一个」text block（实际 prompt），不是第一个
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>\nlong preamble that must be ignored"},
        {"type": "text", "text": "hello world"},
    ]}]
    # 若错误地取第一个 block，结果会不同于 523
    assert m.compute_fingerprint(msgs) == "523"


# ─────────────────────────── cch 6/6 (§2) ───────────────────────────

@_skip_fixtures
@pytest.mark.skipif(not _HAS_XXHASH, reason="xxhash not installed")
@pytest.mark.parametrize("name", list(CCH_EXPECTED))
def test_cch_seed_canonical_6of6(name):
    body = open(f"{BODIES}/body_{name}_ph.bin", "rb").read()
    h = xxhash.xxh64(body, seed=m.CCH_SEED).intdigest()
    got = f"{h & 0xFFFFF:05x}"
    assert got == CCH_EXPECTED[name], f"{name}: cch {got} != {CCH_EXPECTED[name]}"


# ─────────────────────────── BETAS (§6) ───────────────────────────

def test_betas_added_new():
    assert "fast-mode-2026-02-01" in m.BETAS
    assert "context-1m-2025-08-07" in m.BETAS
    assert "mid-conversation-system-2026-04-07" in m.BETAS


def test_betas_redact_thinking_removed():
    assert "redact-thinking-2026-02-12" not in m.BETAS


def test_messages_beta_excludes_oauth():
    # messages 出站 anthropic-beta 不含 oauth-2025-04-20（那是 token 端点的 beta）
    h = m.build_upstream_headers("tok", session_id="s")
    assert "oauth-2025-04-20" not in h["anthropic-beta"]


def test_messages_beta_matches_capture_head7_for_explicit_1m():
    # 明确请求 1M 且模型支持时，前 7 项与抓包 1M 主路径一致（顺序敏感）。
    h = m.build_upstream_headers(
        "tok", session_id="s", model="claude-opus-4-8",
        wants_context_1m=True,
    )
    mine = h["anthropic-beta"].split(",")
    capture7 = [
        "claude-code-20250219", "context-1m-2025-08-07",
        "interleaved-thinking-2025-05-14", "context-management-2025-06-27",
        "prompt-caching-scope-2026-01-05", "mid-conversation-system-2026-04-07",
        "effort-2025-11-24",
    ]
    assert mine[:7] == capture7


def test_context_1m_defaults_for_opus_only_and_sonnet_requires_explicit_signal():
    # Parrot 可用性策略：Opus 4.x 默认开启 1M；Sonnet 4.5/4.6 只在下游显式要求时开启。
    for model in ("claude-opus-4-8", "claude-opus-4-6"):
        h = m.build_upstream_headers("tok", session_id="s", model=model)
        assert "context-1m-2025-08-07" in h["anthropic-beta"].split(",")

    for model in ("claude-sonnet-4-5", "claude-sonnet-4-6"):
        h = m.build_upstream_headers("tok", session_id="s", model=model)
        assert "context-1m-2025-08-07" not in h["anthropic-beta"].split(",")
        h = m.build_upstream_headers("tok", session_id="s", model=model, wants_context_1m=True)
        assert "context-1m-2025-08-07" in h["anthropic-beta"].split(",")

    # 不支持模型默认不带；即使显式请求也不带。
    h = m.build_upstream_headers("tok", session_id="s", model="claude-haiku-4-5-20251001")
    assert "context-1m-2025-08-07" not in h["anthropic-beta"].split(",")
    h = m.build_upstream_headers(
        "tok", session_id="s", model="claude-haiku-4-5-20251001",
        downstream_betas=["context-1m-2025-08-07"],
    )
    assert "context-1m-2025-08-07" not in h["anthropic-beta"].split(",")

    # 显式 False 仍可强制关闭。
    h = m.build_upstream_headers("tok", session_id="s", model="claude-opus-4-8", wants_context_1m=False)
    assert "context-1m-2025-08-07" not in h["anthropic-beta"].split(",")


def test_fast_mode_request_signals_and_header_gate():
    assert m.request_wants_fast_mode({"speed": "fast"})
    assert m.request_wants_fast_mode({}, downstream_betas="fast-mode-2026-02-01")
    assert m.request_wants_fast_mode({"betas": ["fast-mode-2026-02-01"]})
    assert m.request_wants_fast_mode({m.PARROT_WANTS_FAST_MODE_KEY: True})
    assert not m.request_wants_fast_mode({"speed": "standard"})

    h = m.build_upstream_headers(
        "tok", session_id="s", model="claude-sonnet-4-6",
        payload={"model": "claude-sonnet-4-6", "messages": [], "speed": "fast"},
    )
    assert "fast-mode-2026-02-01" in h["anthropic-beta"].split(",")

    h = m.build_upstream_headers(
        "tok", session_id="s", model="claude-sonnet-4-6",
        downstream_betas=["fast-mode-2026-02-01"],
    )
    assert "fast-mode-2026-02-01" in h["anthropic-beta"].split(",")

    h = m.build_upstream_headers("tok", session_id="s", model="claude-sonnet-4-6")
    assert "fast-mode-2026-02-01" not in h["anthropic-beta"].split(",")


def test_context_1m_request_signals():
    assert m.request_wants_context_1m(
        {"max_context_tokens": 1_000_000}, resolved_model="claude-sonnet-4-6"
    )
    assert m.request_wants_context_1m(
        {}, original_model="claude-sonnet-4-6[1m]", resolved_model="claude-sonnet-4-6"
    )
    assert m.request_wants_context_1m(
        {}, original_model="claude-sonnet-4-6-1m", resolved_model="claude-sonnet-4-6"
    )
    assert m.strip_context_1m_model_marker("claude-sonnet-4-6[1m]") == "claude-sonnet-4-6"
    assert m.strip_context_1m_model_marker("claude-sonnet-4-6-context-1m") == "claude-sonnet-4-6"
    assert m.request_wants_context_1m(
        {}, downstream_betas="context-1m-2025-08-07", resolved_model="claude-sonnet-4-6"
    )
    assert not m.request_wants_context_1m(
        {"max_tokens": 64000}, resolved_model="claude-sonnet-4-6"
    )




def test_context_1m_credit_error_is_retryable_without_channel_failure():
    from src import failover

    result = failover.AttemptResult(
        outcome="http_error",
        http_status=429,
        error_detail='HTTP 429: {"error":{"message":"Usage credits are required for long context requests."}}',
    )
    body = {
        m.PARROT_WANTS_CONTEXT_1M_KEY: True,
        m.PARROT_ORIGINAL_MODEL_KEY: "claude-sonnet-4-6[1m]",
    }
    assert failover._is_context_1m_credit_error(result, "claude-sonnet-4-6", body)
        # Opus 4.x supports context-1m, so it should also match now.
    assert failover._is_context_1m_credit_error(result, "claude-opus-4-8", body)
    # A model that doesn't support 1M should NOT match.
    assert not failover._is_context_1m_credit_error(result, "claude-3-5-haiku-20241022", body)
    retry_body = failover._retry_body_without_context_1m(body)
    assert retry_body[m.PARROT_WANTS_CONTEXT_1M_KEY] is False
    assert body[m.PARROT_WANTS_CONTEXT_1M_KEY] is True


def test_mid_conversation_system_model_gate():
    for model in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"):
        h = m.build_upstream_headers("tok", session_id="s", model=model)
        assert "mid-conversation-system-2026-04-07" in h["anthropic-beta"].split(",")
    h = m.build_upstream_headers("tok", session_id="s", model="not-a-claude-model")
    assert "mid-conversation-system-2026-04-07" not in h["anthropic-beta"].split(",")


def test_payload_gates_context_management_and_extended_ttl():
    payload = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
    }
    h = m.build_upstream_headers("tok", session_id="s", model="claude-sonnet-4-6", payload=payload)
    betas = h["anthropic-beta"].split(",")
    assert "context-management-2025-06-27" in betas
    assert "extended-cache-ttl-2025-04-11" in betas

    payload = {"model": "claude-sonnet-4-6", "messages": []}
    h = m.build_upstream_headers("tok", session_id="s", model="claude-sonnet-4-6", payload=payload)
    betas = h["anthropic-beta"].split(",")
    assert "context-management-2025-06-27" not in betas
    assert "extended-cache-ttl-2025-04-11" not in betas


# ─────────────────────────── headers (§7) ───────────────────────────

def test_headers_no_x_client_request_id():
    h = m.build_upstream_headers("tok", session_id="s")
    assert "x-client-request-id" not in h


def test_headers_stainless_full_layer():
    h = m.build_upstream_headers("tok", session_id="s")
    for k, v in {
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "0.94.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v24.3.0",
        "X-Stainless-Retry-Count": "0",
        "X-Stainless-Timeout": "600",
    }.items():
        assert h[k] == v, f"{k}={h.get(k)} != {v}"
    assert h["anthropic-dangerous-direct-browser-access"] == "true"


def test_headers_accept_encoding_no_br_zstd():
    # venv 无 brotli/zstandard，绝不声明 br/zstd 否则回包解不开
    h = m.build_upstream_headers("tok", session_id="s")
    assert h["Accept-Encoding"] == "gzip, deflate"


def test_headers_oauth_bearer():
    h = m.build_upstream_headers("MYTOK", session_id="s")
    assert h["Authorization"] == "Bearer MYTOK"
    assert "x-api-key" not in h


def test_headers_api_key_scheme():
    h = m.build_upstream_headers("MYKEY", session_id="s", auth_scheme="api_key")
    assert h["x-api-key"] == "MYKEY"
    assert "Authorization" not in h


def test_headers_betas_param_filters_oauth():
    # 调用方传入过滤后的 betas，仍要再剔除 oauth
    custom = ["claude-code-20250219", "oauth-2025-04-20", "effort-2025-11-24"]
    h = m.build_upstream_headers("t", session_id="s", betas=custom)
    assert h["anthropic-beta"] == "claude-code-20250219,effort-2025-11-24"


# ─────────────────────────── session_id 联动 (§7.4/§8) ───────────────────────────

def test_metadata_has_session_id():
    md = m.build_metadata(email="a@b.com", session_id="SID-X")
    uid = json.loads(md["user_id"])
    assert uid["session_id"] == "SID-X"
    assert "device_id" in uid and "account_uuid" in uid


def test_session_id_linked_header_and_body():
    # 同一 sid 喂给 transform 与 header，body.metadata 与 header 必须同值
    sid = "LINK-SID-123"
    body = {"model": "claude-sonnet-4", "messages": [{"role": "user", "content": "hi"}]}
    payload, _ = m.transform_request(body, email="x@y.com", session_id=sid)
    h = m.build_upstream_headers("tok", session_id=sid)
    body_sid = json.loads(payload["metadata"]["user_id"])["session_id"]
    assert body_sid == h["X-Claude-Code-Session-Id"] == sid


# ─────────────────────────── transform_request body (§15) ───────────────────────────

def test_transform_request_sets_speed_for_fast_mode():
    payload, _ = m.transform_request({
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hi"}],
        "speed": "fast",
    }, session_id="s")
    assert payload["speed"] == "fast"

    payload, _ = m.transform_request({
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hi"}],
        m.PARROT_WANTS_FAST_MODE_KEY: True,
    }, session_id="s")
    assert payload["speed"] == "fast"


def test_body_wire_order():
    body = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hi"}],
        "system": "s",
        "thinking": {"type": "enabled", "budget_tokens": 8192},
        "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
        "stream": True,
    }
    payload, _ = m.transform_request(body, session_id="s")
    order = [k for k in payload.keys()]
    expect = ["model", "messages", "system", "tools", "metadata",
              "max_tokens", "thinking", "context_management", "stream"]
    assert [k for k in order if k in expect] == [k for k in expect if k in order]


def test_body_no_temperature():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7, "top_p": 0.9, "top_k": 5}
    payload, _ = m.transform_request(body, session_id="s")
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "top_k" not in payload


def test_body_default_max_tokens_64000():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    payload, _ = m.transform_request(body, session_id="s")
    assert payload["max_tokens"] == 64000


def test_body_explicit_max_tokens_respected():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32000}
    payload, _ = m.transform_request(body, session_id="s")
    assert payload["max_tokens"] == 32000


def test_history_assistant_thinking_blocks_stripped():
    body = {"model": "m", "messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret", "signature": "sig"},
            {"type": "text", "text": "visible"},
            {"type": "redacted_thinking", "data": "encrypted"},
            {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "/tmp/a"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
    ]}
    payload, _ = m.transform_request(body, session_id="s")
    assistant = next(msg for msg in payload["messages"] if msg.get("role") == "assistant")
    types = [b.get("type") for b in assistant["content"] if isinstance(b, dict)]
    assert "thinking" not in types
    assert "redacted_thinking" not in types
    assert types == ["text", "tool_use"]


def test_history_assistant_thinking_only_gets_placeholder():
    body = {"model": "m", "messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret", "signature": "sig"},
            {"type": "redacted_thinking", "data": "encrypted"},
        ]},
        {"role": "user", "content": "next"},
    ]}
    payload, _ = m.transform_request(body, session_id="s")
    assistant = next(msg for msg in payload["messages"] if msg.get("role") == "assistant")
    assert assistant["content"] == [{"type": "text", "text": "[Thinking removed]"}]


def test_tool_choice_passthrough_when_present():
    # 客户端显式传 tool_choice 必须透传（不能被静默丢弃）
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "foo", "description": "d", "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "foo"}}
    payload, _ = m.transform_request(body, session_id="s")
    assert payload.get("tool_choice", {}).get("type") == "tool"


def test_tool_choice_not_added_when_absent():
    # 客户端没传则不主动加（CC body 默认无此字段）
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    payload, _ = m.transform_request(body, session_id="s")
    assert "tool_choice" not in payload


# ─────────────────────────── 端到端 dynamic (§2+§3) ───────────────────────────

@_skip_fixtures
@pytest.mark.parametrize("name", list(FP_EXPECTED))
def test_e2e_fp_in_attribution_block(name, monkeypatch):
    # dynamic 模式下，fp 必须正确拼进 system attribution block
    monkeypatch.setattr(m, "load_config",
                        lambda: {"cch_mode": "dynamic", "cch_static_value": "00000"})
    d = json.loads(open(f"{BODIES}/body_{name}_original.bin", "rb").read())
    blocks = m.build_system_blocks(d["messages"])
    attr = blocks[0]["text"]
    fp_match = re.search(r"cc_version=2\.1\.156\.([0-9a-f]{3})", attr)
    assert fp_match and fp_match.group(1) == FP_EXPECTED[name]
    assert f"cc_entrypoint=sdk-cli" in attr
    assert "cch=00000" in attr  # dynamic 模式占位，待 sign_body 替换


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
