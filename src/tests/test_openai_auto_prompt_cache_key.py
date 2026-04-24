"""OpenAI 自动 prompt_cache_key 与亲和链绑定测试。

只覆盖 OpenAI 协议辅助逻辑：下游没传 prompt_cache_key 时自动补；
成功后通过 affinity 的 fp_write 继续传递同一个 key。
"""

from __future__ import annotations

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
    body = {"model": "gpt-5.5", "input": "hi"}

    key = m["handler"]._maybe_apply_auto_prompt_cache_key(body, fp_query=None)

    assert key is not None
    assert key.startswith("parrot:auto:v1:")
    assert body["prompt_cache_key"] == key


def test_auto_prompt_cache_key_respects_downstream_value(m):
    _setup(m)
    body = {"model": "gpt-5.5", "input": "hi", "prompt_cache_key": "client-key"}

    key = m["handler"]._maybe_apply_auto_prompt_cache_key(body, fp_query="fp-any")

    assert key == "client-key"
    assert body["prompt_cache_key"] == "client-key"


def test_auto_prompt_cache_key_reuses_affinity_chain_value(m):
    _setup(m)
    m["affinity"].upsert(
        "fp-query", "oauth:openai:user@example.com", "gpt-5.5",
        prompt_cache_key="parrot:auto:v1:stable",
    )
    body = {"model": "gpt-5.5", "input": "next"}

    key = m["handler"]._maybe_apply_auto_prompt_cache_key(body, fp_query="fp-query")

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
