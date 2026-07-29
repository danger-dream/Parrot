"""OpenAI 自动 prompt_cache_key 与亲和链绑定测试。

只覆盖 OpenAI 协议辅助逻辑：下游没传 prompt_cache_key 时自动补；
成功后通过 affinity 的 fp_write 继续传递同一个 key。
"""

from __future__ import annotations

import asyncio
import json
import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    root = _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__))))
    if root not in _ap_sys.path:
        _ap_sys.path.insert(0, root)
    from src import affinity, config, state_db
    from src.openai import handler
    return {"affinity": affinity, "config": config, "state_db": state_db, "handler": handler}


def _setup(m):
    m["state_db"].init()
    m["affinity"].delete_all()

    def _cfg(c):
        c.setdefault("openai", {}).setdefault("autoPromptCacheKey", {})["enabled"] = True
        c.setdefault("openai", {}).setdefault("autoPromptCacheKey", {})["prefix"] = "parrot:auto:v1"

    m["config"].update(_cfg)


def test_auto_prompt_cache_key_generated_when_missing(m):
    _setup(m)
    body = {"model": "gpt-5.5"}

    key = m["handler"]._maybe_apply_auto_prompt_cache_key(body, fp_query=None)

    assert key is not None
    assert key.startswith("parrot:auto:v1:")
    assert not key.startswith("parrot:auto:v1:stable:")
    assert body["prompt_cache_key"] == key


def test_auto_prompt_cache_key_stable_fallback_chat(m):
    _setup(m)
    body1 = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "bootstrap user"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "real task"},
    ]}
    body2 = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "bootstrap user"},
        {"role": "assistant", "content": "a1 changed should not affect anchor"},
        {"role": "user", "content": "real task"},
        {"role": "assistant", "content": "later"},
        {"role": "user", "content": "next"},
    ]}

    key1 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body1, fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
    )
    key2 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body2, fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
    )

    assert key1 == key2
    assert key1.startswith("parrot:auto:v1:stable:")


def test_auto_prompt_cache_key_does_not_lock_on_single_bootstrap_user(m):
    _setup(m)
    body1 = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "same system"},
        {"role": "user", "content": "same bootstrap user"},
    ]}
    body2 = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "same system"},
        {"role": "user", "content": "same bootstrap user"},
    ]}

    key1 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body1, fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
    )
    key2 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body2, fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
    )

    assert key1 != key2
    assert not key1.startswith("parrot:auto:v1:stable:")
    assert not key2.startswith("parrot:auto:v1:stable:")


def test_auto_prompt_cache_key_uses_claude_session_in_random_fallback(m):
    _setup(m)

    def _body():
        return {"model": "gpt-5.5", "messages": [
            {"role": "system", "content": "same system"},
            {"role": "user", "content": "single bootstrap user"},
        ]}

    session_id = "123e4567-e89b-12d3-a456-426614174000"
    key1 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        _body(), fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
        claude_code_session_id=session_id,
    )
    key2 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        _body(), fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
        claude_code_session_id=session_id,
    )
    changed_session = m["handler"]._maybe_apply_auto_prompt_cache_key(
        _body(), fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
        claude_code_session_id="223e4567-e89b-12d3-a456-426614174000",
    )
    changed_ip = m["handler"]._maybe_apply_auto_prompt_cache_key(
        _body(), fp_query=None, api_key_name="alice", client_ip="5.6.7.8",
        model="gpt-5.5", ingress_protocol="chat",
        claude_code_session_id=session_id,
    )

    assert key1 == key2
    assert key1.startswith("parrot:auto:v1:claude-session:")
    assert session_id not in key1
    assert key1 != changed_session
    assert key1 != changed_ip


def test_auto_prompt_cache_key_claude_session_precedes_stable_anchor(m):
    _setup(m)
    early_body = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
    ]}
    mature_body = {"model": "gpt-5.5", "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
        {"role": "assistant", "content": "ack"},
        {"role": "tool", "content": "tool result"},
        {"role": "user", "content": "real task"},
    ]}
    kwargs = {
        "fp_query": None,
        "api_key_name": "alice",
        "client_ip": "1.2.3.4",
        "model": "gpt-5.5",
        "ingress_protocol": "chat",
        "claude_code_session_id": "123e4567-e89b-12d3-a456-426614174000",
    }

    early_key = m["handler"]._maybe_apply_auto_prompt_cache_key(
        early_body, **kwargs,
    )
    mature_key = m["handler"]._maybe_apply_auto_prompt_cache_key(
        mature_body, **kwargs,
    )

    assert early_key == mature_key
    assert early_key.startswith("parrot:auto:v1:claude-session:")


def test_handler_forwards_claude_session_header_to_pck_resolver(m):
    _setup(m)
    handler = m["handler"]
    captured = {}

    class _StopAfterPckResolution(Exception):
        pass

    class _Headers(dict):
        def __init__(self, data):
            super().__init__((k.lower(), v) for k, v in data.items())

    class _Client:
        host = "1.2.3.4"

    class _Request:
        headers = _Headers({"x-claude-code-session-id": "session-from-header"})
        client = _Client()

        async def body(self):
            return json.dumps({
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
            }).encode("utf-8")

    original_validate = handler.auth.validate
    original_resolver = handler._maybe_apply_auto_prompt_cache_key

    def _validate(_headers):
        return "alice", [], None

    def _capture_resolver(_body, **kwargs):
        captured.update(kwargs)
        raise _StopAfterPckResolution

    try:
        handler.auth.validate = _validate
        handler._maybe_apply_auto_prompt_cache_key = _capture_resolver
        try:
            asyncio.run(handler.handle(_Request(), ingress_protocol="chat"))
        except _StopAfterPckResolution:
            pass
    finally:
        handler.auth.validate = original_validate
        handler._maybe_apply_auto_prompt_cache_key = original_resolver

    assert captured["claude_code_session_id"] == "session-from-header"


def test_auto_prompt_cache_key_stable_fallback_isolated_by_client_ip(m):
    _setup(m)
    body1 = {"model": "gpt-5.5", "messages": [
        {"role": "user", "content": "bootstrap"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "real task"},
    ]}
    body2 = {"model": "gpt-5.5", "messages": [
        {"role": "user", "content": "bootstrap"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "real task"},
    ]}

    key1 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body1, fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="chat",
    )
    key2 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body2, fp_query=None, api_key_name="alice", client_ip="5.6.7.8",
        model="gpt-5.5", ingress_protocol="chat",
    )

    assert key1 != key2


def test_auto_prompt_cache_key_respects_downstream_value(m):
    _setup(m)
    body = {"model": "gpt-5.5", "input": "hi", "prompt_cache_key": "client-key"}

    key = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body, fp_query="fp-any",
        claude_code_session_id="123e4567-e89b-12d3-a456-426614174000",
    )

    assert key == "client-key"
    assert body["prompt_cache_key"] == "client-key"


def test_auto_prompt_cache_key_reuses_affinity_chain_value(m):
    _setup(m)
    m["affinity"].upsert(
        "fp-query", "oauth:openai:user@example.com", "gpt-5.5",
        prompt_cache_key="parrot:auto:v1:stable",
    )
    body = {"model": "gpt-5.5", "input": "next"}

    key = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body, fp_query="fp-query",
        claude_code_session_id="123e4567-e89b-12d3-a456-426614174000",
    )

    assert key == "parrot:auto:v1:stable"
    assert body["prompt_cache_key"] == "parrot:auto:v1:stable"


def test_affinity_upsert_preserves_prompt_cache_key_when_omitted(m):
    _setup(m)
    m["affinity"].upsert(
        "fp", "oauth:openai:user@example.com", "gpt-5.5",
        prompt_cache_key="parrot:auto:v1:keep-me",
    )

    # 老调用路径/非 OpenAI 协议不传 prompt_cache_key 时，不应清空已有绑定。
    m["affinity"].upsert("fp", "oauth:openai:user@example.com", "gpt-5.5")

    assert m["affinity"].get("fp")["prompt_cache_key"] == "parrot:auto:v1:keep-me"
    row = m["state_db"].affinity_load("fp")
    assert row["prompt_cache_key"] == "parrot:auto:v1:keep-me"


def test_auto_prompt_cache_key_can_be_disabled(m):
    _setup(m)

    def _cfg(c):
        c.setdefault("openai", {}).setdefault("autoPromptCacheKey", {})["enabled"] = False

    m["config"].update(_cfg)
    body = {"model": "gpt-5.5", "input": "hi"}

    key = m["handler"]._maybe_apply_auto_prompt_cache_key(body, fp_query=None)

    assert key is None
    assert "prompt_cache_key" not in body


def test_responses_stable_fallback_includes_input_system(m):
    _setup(m)
    body1 = {"model": "gpt-5.5", "input": [
        {"type": "message", "role": "system", "content": "system A"},
        {"type": "message", "role": "user", "content": "bootstrap"},
        {"type": "message", "role": "assistant", "content": "ack"},
        {"type": "message", "role": "user", "content": "real task"},
    ]}
    body2 = {"model": "gpt-5.5", "input": [
        {"type": "message", "role": "system", "content": "system B"},
        {"type": "message", "role": "user", "content": "bootstrap"},
        {"type": "message", "role": "assistant", "content": "ack"},
        {"type": "message", "role": "user", "content": "real task"},
    ]}

    key1 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body1, fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="responses",
    )
    key2 = m["handler"]._maybe_apply_auto_prompt_cache_key(
        body2, fp_query=None, api_key_name="alice", client_ip="1.2.3.4",
        model="gpt-5.5", ingress_protocol="responses",
    )

    assert key1 != key2
    assert key1.startswith("parrot:auto:v1:stable:")
    assert key2.startswith("parrot:auto:v1:stable:")


def test_openai_prompt_total_legacy_input_includes_cached(m):
    from src import cache_display

    row = {
        "upstream_protocol": "openai-responses",
        "input_tokens": 28010,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 26112,
    }
    assert cache_display.prompt_total_from_row(row) == 28010
    assert "93.2%" in cache_display.cache_read_phrase_from_row(row)


def test_get_client_ip_prefers_cdn_headers(m):
    from src.client_ip import get_client_ip

    class H:
        def __init__(self, data):
            self._d = {k.lower(): v for k, v in data.items()}
        def get(self, k, default=None):
            return self._d.get(k.lower(), default)
        def items(self):
            return self._d.items()

    class C:
        host = "10.0.0.9"

    class R:
        headers = H({
            "X-Forwarded-For": "203.0.113.10, 10.0.0.1",
            "CF-Connecting-IP": "198.51.100.8",
        })
        client = C()

    assert get_client_ip(R()) == "198.51.100.8"


def test_get_client_ip_fallback_x_forwarded_for_then_socket(m):
    from src.client_ip import get_client_ip

    class H:
        def __init__(self, data):
            self._d = {k.lower(): v for k, v in data.items()}
        def get(self, k, default=None):
            return self._d.get(k.lower(), default)
        def items(self):
            return self._d.items()

    class C:
        host = "10.0.0.9"

    class R1:
        headers = H({"X-Forwarded-For": "203.0.113.10, 10.0.0.1"})
        client = C()

    class R2:
        headers = H({})
        client = C()

    assert get_client_ip(R1()) == "203.0.113.10"
    assert get_client_ip(R2()) == "10.0.0.9"


# ─── main ────────────────────────────────────────────────────────

def main() -> int:
    m = _import_modules()
    tests = [
        test_auto_prompt_cache_key_generated_when_missing,
        test_auto_prompt_cache_key_stable_fallback_chat,
        test_auto_prompt_cache_key_does_not_lock_on_single_bootstrap_user,
        test_auto_prompt_cache_key_uses_claude_session_in_random_fallback,
        test_auto_prompt_cache_key_stable_anchor_precedes_claude_session,
        test_handler_forwards_claude_session_header_to_pck_resolver,
        test_auto_prompt_cache_key_stable_fallback_isolated_by_client_ip,
        test_auto_prompt_cache_key_respects_downstream_value,
        test_auto_prompt_cache_key_reuses_affinity_chain_value,
        test_affinity_upsert_preserves_prompt_cache_key_when_omitted,
        test_auto_prompt_cache_key_can_be_disabled,
        test_responses_stable_fallback_includes_input_system,
        test_openai_prompt_total_legacy_input_includes_cached,
        test_get_client_ip_prefers_cdn_headers,
        test_get_client_ip_fallback_x_forwarded_for_then_socket,
    ]
    passed = 0
    print("── OpenAI auto prompt_cache_key ────────────")
    for t in tests:
        try:
            t(m)
            passed += 1
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            import traceback; traceback.print_exc()
        except Exception as e:
            print(f"  [ERR ] {t.__name__}: {e}")
            import traceback; traceback.print_exc()
    print(f"\nRESULT: {passed} / {len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
