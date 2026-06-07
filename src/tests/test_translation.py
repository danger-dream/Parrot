"""翻译层单测。

覆盖：
  - 缓存层：写入/读取/TTL 过期/清空/预加载
  - 缓存 key：target_language 参与 hash，不同语言不混用
  - 消息内容提取/替换：string / list of blocks
  - translate_body 按入口协议翻译正确的字段
  - translate_body 不翻译 assistant / tool 消息
  - translate_body 关闭时直接返回原 body
  - translate_body 无模型时直接返回原 body
  - maxHistoryMessages 截断
  - _translate_single 缓存命中不调模型
  - _call_model 找不到渠道返回 None
  - 连续失败告警计数
  - 默认提示词占位符替换

运行：
  cd /opt/src-space/parrot && python -m pytest src/tests/test_translation.py -v
"""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_tmpdir = _isolation.isolate()

import asyncio
import json
import time

import pytest

from src import config, translation
from src.channel import registry


# ─── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_translation():
    """每个测试前重置翻译层状态。"""
    translation.init()
    translation.clear_cache()
    translation._consecutive_failures = 0
    # 确保默认关闭
    config.update(lambda c: c.__setitem__("translation", {
        "enabled": False,
        "model": "",
        "fallbackModel": "",
        "targetLanguage": "English",
        "prompt": "",
        "timeoutSeconds": 10,
        "maxHistoryMessages": 20,
        "cacheTtlDays": 3,
        "cachePreloadCount": 100,
        "failureAlertThreshold": 10,
        "memoryCacheMaxMb": 100,
        "memoryCacheTtlSeconds": 7200,
        "translateSystemMessages": True,
        "scope": {"models": [], "channels": []},
        "modelOverrides": {},
    }))
    yield
    translation.clear_cache()


# ─── 缓存层 ──────────────────────────────────────────────────────

class TestCache:
    def test_put_get(self):
        key = translation._make_cache_key("English", "hello world")
        assert translation._cache_get(key) is None
        translation._cache_put(key, "你好世界")
        assert translation._cache_get(key) == "你好世界"

    def test_different_language_different_key(self):
        """不同目标语言产生不同 cache key。"""
        k1 = translation._make_cache_key("English", "你好")
        k2 = translation._make_cache_key("Japanese", "你好")
        assert k1 != k2

        translation._cache_put(k1, "hello")
        translation._cache_put(k2, "こんにちは")
        assert translation._cache_get(k1) == "hello"
        assert translation._cache_get(k2) == "こんにちは"


    def test_prompt_change_changes_cache_key(self):
        cfg = translation._get_cfg()
        k1 = translation._cache_key_for_text("English", "你好", cfg)
        cfg2 = dict(cfg)
        cfg2["prompt"] = "Translate to {target_language}, casually."
        k2 = translation._cache_key_for_text("English", "你好", cfg2)
        assert k1 != k2

    def test_memory_cache_ttl_expires_before_sqlite(self):
        config.update(lambda c: c.setdefault("translation", {}).update({
            "memoryCacheTtlSeconds": 1,
        }))
        k = translation._cache_key_for_text("English", "mem-ttl", translation._get_cfg())
        translation._cache_put(k, "fresh")
        assert translation._cache_get(k) == "fresh"
        with translation._mem_lock:
            translation._mem_cache[k]["cached_at"] = time.time() - 10
        # 内存过期后仍可从 sqlite 命中，并重新提升到内存。
        assert translation._cache_get(k) == "fresh"
        with translation._mem_lock:
            assert k in translation._mem_cache

    def test_memory_cache_max_mb_zero_disables_memory_only(self):
        config.update(lambda c: c.setdefault("translation", {}).update({
            "memoryCacheMaxMb": 0,
        }))
        k = translation._cache_key_for_text("English", "mem-off", translation._get_cfg())
        translation._cache_put(k, "stored")
        with translation._mem_lock:
            assert k not in translation._mem_cache
        assert translation._cache_get(k) == "stored"
        with translation._mem_lock:
            assert k not in translation._mem_cache

    def test_clear_cache(self):
        k = translation._make_cache_key("English", "test")
        translation._cache_put(k, "translated")
        assert translation.cache_count() >= 1
        cleared = translation.clear_cache()
        assert cleared >= 1
        assert translation._cache_get(k) is None
        assert translation.cache_count() == 0

    def test_cache_stats(self):
        translation._cache_stats["hits"] = 0
        translation._cache_stats["misses"] = 0
        k = translation._make_cache_key("English", "test")
        translation._cache_get(k)  # miss
        translation._cache_put(k, "ok")
        translation._cache_get(k)  # hit
        stats = translation.cache_hit_stats()
        assert stats["misses"] >= 1
        assert stats["hits"] >= 1

    def test_expired_cache_not_returned(self):
        """过期条目不应被返回。"""
        k = translation._make_cache_key("English", "expire-test")
        # 直接往 sqlite 写一条过去的记录
        with translation._db_lock:
            translation._db.execute(
                "INSERT OR REPLACE INTO translation_cache (cache_key, translated, created_at) "
                "VALUES (?, ?, ?)",
                (k, "old", time.time() - 400 * 86400),  # 400 天前
            )
            translation._db.commit()
        # 不应该命中（默认 TTL 3 天）
        assert translation._cache_get(k) is None

    def test_cleanup_expired(self):
        k = translation._make_cache_key("English", "cleanup-test")
        with translation._db_lock:
            translation._db.execute(
                "INSERT OR REPLACE INTO translation_cache (cache_key, translated, created_at) "
                "VALUES (?, ?, ?)",
                (k, "old", time.time() - 400 * 86400),
            )
            translation._db.commit()
        cleared = translation.cleanup_expired()
        assert cleared >= 1

    def test_preload(self):
        """预加载应将 sqlite 中的记录加载到内存。"""
        k = translation._make_cache_key("English", "preload-test")
        translation._cache_put(k, "preloaded value")
        # 清除内存缓存
        with translation._mem_lock:
            translation._mem_cache.clear()
        # 验证内存中没有
        with translation._mem_lock:
            assert k not in translation._mem_cache
        # 预加载
        translation._preload(100)
        # 应该在内存中了
        with translation._mem_lock:
            assert k in translation._mem_cache
            assert translation._mem_cache[k]["translated"] == "preloaded value"


# ─── 消息内容提取与替换 ───────────────────────────────────────────

class TestContentExtractReplace:
    def test_extract_string(self):
        assert translation._extract_text_content("hello") == "hello"

    def test_extract_empty_string(self):
        assert translation._extract_text_content("") is None
        assert translation._extract_text_content("   ") is None

    def test_extract_list_of_blocks(self):
        content = [
            {"type": "text", "text": "part1 "},
            {"type": "image", "source": {"type": "base64"}},
            {"type": "text", "text": "part2"},
        ]
        assert translation._extract_text_content(content) == "part1 part2"

    def test_extract_list_no_text(self):
        content = [{"type": "image", "source": {}}]
        assert translation._extract_text_content(content) is None

    def test_extract_none(self):
        assert translation._extract_text_content(None) is None
        assert translation._extract_text_content(42) is None

    def test_replace_string(self):
        result = translation._replace_text_content("original", "translated")
        assert result == "translated"

    def test_replace_list_of_blocks(self):
        content = [
            {"type": "text", "text": "part1"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "part2"},
        ]
        result = translation._replace_text_content(content, "全部翻译")
        # 兼容旧 helper：只替换第一个 text block，不再删除后续 text block。
        assert isinstance(result, list)
        assert result[0]["text"] == "全部翻译"
        assert result[1]["type"] == "image"
        assert result[2]["text"] == "part2"

    def test_apply_content_translations_preserves_multimodal_order(self):
        content = [
            {"type": "text", "text": "before"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "after"},
        ]
        translated = {"m:b:0": "BEFORE", "m:b:2": "AFTER"}
        result = translation._apply_content_translations(content, "m", translated)
        assert result == [
            {"type": "text", "text": "BEFORE"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "AFTER"},
        ]


# ─── translate_body 集成测试（不调真实模型） ─────────────────────

class TestTranslateBodyDisabled:
    """翻译关闭时，body 原样返回。"""

    def test_disabled_returns_same(self):
        body = {"model": "claude", "messages": [{"role": "user", "content": "你好"}]}
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="anthropic")
        )
        assert result is body  # 同一个对象引用

    def test_no_model_returns_same(self):
        config.update(lambda c: c.setdefault("translation", {}).update({
            "enabled": True, "model": "",
        }))
        body = {"model": "claude", "messages": [{"role": "user", "content": "你好"}]}
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="anthropic")
        )
        assert result is body


class TestTranslateBodyWithCache:
    """用预填充的缓存测试 translate_body 的字段选择逻辑（不调真实模型）。"""

    def _enable_translation(self):
        class FakeChannel:
            key = "api:test-translation"
            enabled = True
            disabled_reason = None
            protocol = "anthropic"
            type = "api"
            def supports_model(self, model):
                return "test-model" if model == "test-model" else None
            def list_client_models(self):
                return ["test-model"]
        with registry._lock:
            registry._channels = {"api:test-translation": FakeChannel()}
        config.update(lambda c: c.setdefault("translation", {}).update({
            "enabled": True,
            "model": "test-model",
            "targetLanguage": "English",
        }))

    def _seed_cache(self, original: str, translated: str, lang: str = "English"):
        cfg = translation._get_cfg()
        k = translation._cache_key_for_text(lang, original, cfg)
        translation._cache_put(k, translated)

    # ── Anthropic ──

    def test_anthropic_translates_user_and_system(self):
        self._enable_translation()
        self._seed_cache("你好", "hello")
        self._seed_cache("你是一个助手", "You are an assistant")

        body = {
            "model": "claude",
            "system": "你是一个助手",
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "Hi there!"},
            ],
        }
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="anthropic")
        )
        assert result["system"] == "You are an assistant"
        assert result["messages"][0]["content"] == "hello"
        assert result["messages"][1]["content"] == "Hi there!"  # assistant 不翻译

    def test_anthropic_skips_assistant_and_tool(self):
        self._enable_translation()
        self._seed_cache("查天气", "check weather")

        body = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": "查天气"},
                {"role": "assistant", "content": "好的，我来查"},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "晴天"},
                ]},
            ],
        }
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="anthropic")
        )
        # user[0] 翻译
        assert result["messages"][0]["content"] == "check weather"
        # assistant 不翻译
        assert result["messages"][1]["content"] == "好的，我来查"
        # tool_result 类型的 user 消息，extract_text_content 返回 None → 不翻译
        assert result["messages"][2]["content"][0]["content"] == "晴天"

    def test_anthropic_system_list_blocks(self):
        self._enable_translation()
        self._seed_cache("你是小夕", "You are Xiaoxi")

        body = {
            "model": "claude",
            "system": [
                {"type": "text", "text": "你是小夕"},
                {"type": "text", "text": "保持专业", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [],
        }
        self._seed_cache("保持专业", "Stay professional")
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="anthropic")
        )
        assert result["system"][0]["text"] == "You are Xiaoxi"
        assert result["system"][1]["text"] == "Stay professional"
        # cache_control 保留
        assert result["system"][1].get("cache_control") == {"type": "ephemeral"}

    def test_anthropic_max_history(self):
        """只翻译最近 maxHistoryMessages 条 user 消息。"""
        self._enable_translation()
        config.update(lambda c: c.setdefault("translation", {}).update({
            "maxHistoryMessages": 2,
        }))

        for i in range(5):
            self._seed_cache(f"msg{i}", f"translated{i}")

        messages = []
        for i in range(5):
            messages.append({"role": "user", "content": f"msg{i}"})
            messages.append({"role": "assistant", "content": f"reply{i}"})

        body = {"model": "claude", "messages": messages}
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="anthropic")
        )
        # 最后 2 条 user（idx 6=msg3, idx 8=msg4）应该被翻译
        # 前 3 条 user（idx 0=msg0, idx 2=msg1, idx 4=msg2）不翻译
        assert result["messages"][0]["content"] == "msg0"      # 不翻译
        assert result["messages"][2]["content"] == "msg1"      # 不翻译
        assert result["messages"][4]["content"] == "msg2"      # 不翻译
        assert result["messages"][6]["content"] == "translated3"  # 翻译
        assert result["messages"][8]["content"] == "translated4"  # 翻译

    # ── OpenAI Chat ──

    def test_chat_translates_system_and_user(self):
        self._enable_translation()
        self._seed_cache("你是助手", "You are assistant")
        self._seed_cache("你好啊", "Hello there")

        body = {
            "model": "gpt-5",
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好啊"},
                {"role": "assistant", "content": "Hi!"},
                {"role": "user", "content": "再见"},
            ],
        }
        self._seed_cache("再见", "Goodbye")
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="chat")
        )
        assert result["messages"][0]["content"] == "You are assistant"
        assert result["messages"][1]["content"] == "Hello there"
        assert result["messages"][2]["content"] == "Hi!"  # assistant 不翻译
        assert result["messages"][3]["content"] == "Goodbye"

    def test_chat_developer_role(self):
        """developer role 也应被翻译（等同 system）。"""
        self._enable_translation()
        self._seed_cache("你是开发者助手", "You are a developer assistant")

        body = {
            "model": "gpt-5",
            "messages": [
                {"role": "developer", "content": "你是开发者助手"},
            ],
        }
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="chat")
        )
        assert result["messages"][0]["content"] == "You are a developer assistant"


    def test_chat_system_not_translated_by_default(self):
        self._enable_translation()
        config.update(lambda c: c.setdefault("translation", {}).update({
            "translateSystemMessages": False,
        }))
        self._seed_cache("你是助手", "You are assistant")
        self._seed_cache("你好啊", "Hello there")

        body = {
            "model": "gpt-5",
            "messages": [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "你好啊"},
            ],
        }
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="chat")
        )
        assert result["messages"][0]["content"] == "你是助手"
        assert result["messages"][1]["content"] == "Hello there"

    def test_chat_multimodal_text_blocks_translated_individually(self):
        self._enable_translation()
        self._seed_cache("看这张图", "Look at this image")
        self._seed_cache("重点分析右下角", "Focus on the bottom right")

        body = {
            "model": "gpt-5",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                    {"type": "text", "text": "重点分析右下角"},
                ],
            }],
        }
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="chat")
        )
        content = result["messages"][0]["content"]
        assert content[0]["text"] == "Look at this image"
        assert content[1]["type"] == "image_url"
        assert content[2]["text"] == "Focus on the bottom right"

    # ── OpenAI Responses ──

    def test_responses_translates_instructions_and_input(self):
        self._enable_translation()
        self._seed_cache("你是助手", "You are assistant")
        self._seed_cache("帮我写代码", "Help me write code")

        body = {
            "model": "gpt-5",
            "instructions": "你是助手",
            "input": "帮我写代码",
        }
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="responses")
        )
        assert result["instructions"] == "You are assistant"
        assert result["input"] == "Help me write code"

    def test_responses_input_list(self):
        self._enable_translation()
        self._seed_cache("分析这个", "Analyze this")

        body = {
            "model": "gpt-5",
            "input": [
                {"role": "user", "content": "分析这个"},
                {"role": "assistant", "content": "OK let me analyze"},
                {"role": "user", "content": "再详细点"},
            ],
        }
        self._seed_cache("再详细点", "More details")
        result = asyncio.run(
            translation.translate_body(body, ingress_protocol="responses")
        )
        assert result["input"][0]["content"] == "Analyze this"
        assert result["input"][1]["content"] == "OK let me analyze"  # assistant 不翻译
        assert result["input"][2]["content"] == "More details"

    def test_responses_system_item_respects_switch(self):
        self._enable_translation()
        self._seed_cache("系统提示", "System prompt")
        self._seed_cache("用户问题", "User question")

        config.update(lambda c: c.setdefault("translation", {}).update({
            "translateSystemMessages": False,
        }))
        body = {
            "model": "gpt-5",
            "input": [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "用户问题"},
            ],
        }
        result = asyncio.run(translation.translate_body(body, ingress_protocol="responses"))
        assert result["input"][0]["content"] == "系统提示"
        assert result["input"][1]["content"] == "User question"

        config.update(lambda c: c.setdefault("translation", {}).update({
            "translateSystemMessages": True,
        }))
        result = asyncio.run(translation.translate_body(body, ingress_protocol="responses"))
        assert result["input"][0]["content"] == "System prompt"
        assert result["input"][1]["content"] == "User question"


# ─── 内部调用逻辑 ─────────────────────────────────────────────────

class TestInternalCalls:
    def test_find_channel_nonexistent_model(self):
        """找不到渠道时返回 None。"""
        result = translation._find_channel_for_model("nonexistent-model-xyz")
        assert result is None

    def test_call_model_no_channel(self):
        """模型不存在，_call_model 返回 None。"""
        result = asyncio.run(
            translation._call_model("nonexistent", "hello", "translate", 5.0)
        )
        assert result is None

    def test_translate_single_cache_hit(self):
        """缓存命中时不调模型，直接返回缓存值。"""
        config.update(lambda c: c.setdefault("translation", {}).update({
            "enabled": True,
            "model": "nonexistent-will-fail",
            "targetLanguage": "English",
        }))
        k = translation._cache_key_for_text("English", "缓存测试", translation._get_cfg())
        translation._cache_put(k, "cache test")

        result = asyncio.run(
            translation._translate_single("缓存测试", "English", translation._get_cfg())
        )
        assert result == "cache test"

    def test_translate_single_model_fail_returns_original(self):
        """模型不存在，翻译失败，返回原文。"""
        config.update(lambda c: c.setdefault("translation", {}).update({
            "enabled": True,
            "model": "nonexistent-model",
            "targetLanguage": "English",
        }))
        result = asyncio.run(
            translation._translate_single("翻译失败测试", "English", translation._get_cfg())
        )
        assert result == "翻译失败测试"  # 回退原文


# ─── 失败计数器 ───────────────────────────────────────────────────

class TestFailureCounter:
    def test_success_resets_counter(self):
        translation._consecutive_failures = 5
        translation._record_success()
        assert translation._consecutive_failures == 0

    def test_failure_increments(self):
        translation._consecutive_failures = 0
        cfg = translation._get_cfg()
        translation._record_failure(cfg)
        assert translation._consecutive_failures == 1
        translation._record_failure(cfg)
        assert translation._consecutive_failures == 2


# ─── 默认提示词 ───────────────────────────────────────────────────

class TestDefaultPrompt:
    def test_contains_placeholder(self):
        assert "{target_language}" in translation.DEFAULT_TRANSLATION_PROMPT

    def test_placeholder_replacement(self):
        prompt = translation.DEFAULT_TRANSLATION_PROMPT.replace(
            "{target_language}", "Japanese"
        )
        assert "Japanese" in prompt
        assert "{target_language}" not in prompt

    def test_prompt_treats_source_as_data_with_examples(self):
        prompt = translation.DEFAULT_TRANSLATION_PROMPT
        assert "inert data" in prompt
        assert "<source_text>" in prompt
        assert "Wrong output:" in prompt
        assert "Chinese" in prompt
        assert "HACKED" in prompt


# ─── 响应解析 ─────────────────────────────────────────────────────

class TestExtractFromResponse:
    def test_anthropic_response(self):
        data = {"content": [{"type": "text", "text": "hello world"}]}
        assert translation._extract_text_from_response(data, "anthropic") == "hello world"

    def test_anthropic_multi_block(self):
        data = {"content": [
            {"type": "text", "text": "part1 "},
            {"type": "text", "text": "part2"},
        ]}
        assert translation._extract_text_from_response(data, "anthropic") == "part1 part2"

    def test_anthropic_error(self):
        data = {"type": "error", "error": {"message": "bad"}}
        assert translation._extract_text_from_response(data, "anthropic") is None

    def test_openai_chat_response(self):
        data = {"choices": [{"message": {"content": "translated text"}}]}
        assert translation._extract_text_from_response(data, "openai-chat") == "translated text"

    def test_openai_chat_empty_choices(self):
        data = {"choices": []}
        assert translation._extract_text_from_response(data, "openai-chat") is None

    def test_openai_responses_response(self):
        data = {"output": [
            {"type": "message", "content": [
                {"type": "output_text", "text": "translated"}
            ]}
        ]}
        assert translation._extract_text_from_response(data, "openai-responses") == "translated"

    def test_openai_error(self):
        data = {"error": {"message": "rate limit"}}
        assert translation._extract_text_from_response(data, "openai-chat") is None

    def test_unknown_protocol(self):
        data = {"content": "whatever"}
        assert translation._extract_text_from_response(data, "unknown") is None



    def test_extract_openai_responses_sse_deltas(self):
        raw = (
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","delta":"hel"}\n\n'
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","delta":"lo"}\n\n'
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
        )
        assert translation._extract_text_from_sse(raw, "openai-responses", "gpt") == "hello"

    def test_extract_openai_responses_sse_completed_preferred(self):
        raw = (
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            b'event: response.completed\n'
            b'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"final"}]}]}}\n\n'
        )
        assert translation._extract_text_from_sse(raw, "openai-responses", "gpt") == "final"

    def test_extract_openai_chat_sse_deltas(self):
        raw = (
            b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            b'data: [DONE]\n\n'
        )
        assert translation._extract_text_from_sse(raw, "openai-chat", "gpt") == "hello"


# ─── 生效范围 ───────────────────────────────────────────────────

class TestTranslationScope:
    class _Ch:
        def __init__(self, key):
            self.key = key

    class _Route:
        def __init__(self, key):
            self.candidates = [(TestTranslationScope._Ch(key), "resolved")]
            self.saturated = []

    def test_empty_scope_allows_all(self):
        cfg = translation._get_cfg()
        cfg["scope"] = {"models": [], "channels": []}
        assert translation._translation_scope_allows({"model": "claude-opus-4-8"}, cfg, None) is True

    def test_model_scope_filters_requested_model(self):
        cfg = translation._get_cfg()
        cfg["scope"] = {"models": ["claude-opus-4-8"], "channels": []}
        assert translation._translation_scope_allows({"model": "claude-opus-4-8"}, cfg, None) is True
        assert translation._translation_scope_allows({"model": "gpt-5.5"}, cfg, None) is False

    def test_channel_scope_requires_matching_route(self):
        cfg = translation._get_cfg()
        cfg["scope"] = {"models": [], "channels": ["oauth:claude:a@example.com"]}
        assert translation._translation_scope_allows(
            {"model": "claude-opus-4-8"}, cfg, self._Route("oauth:claude:a@example.com")
        ) is True
        assert translation._translation_scope_allows(
            {"model": "claude-opus-4-8"}, cfg, self._Route("oauth:claude:b@example.com")
        ) is False
        assert translation._translation_scope_allows({"model": "claude-opus-4-8"}, cfg, None) is False


# ─── 翻译结果安全校验 / 输出预算 ────────────────────────────────

class TestTranslationResultValidation:
    def test_rejects_blank_translation(self):
        assert translation._translation_result_problem("hello", "") == "blank"

    def test_does_not_guess_truncation_from_text_shape(self):
        source = (
            "Conversation info (untrusted metadata):\n"
            "```json\n{}\n```\n\n"
            "你检查下网络，看看是什么问题，为什么会连个镜像都拉取不下来"
        )
        truncated = (
            "Conversation info (untrusted metadata):\n"
            "```json\n{\n  \"message_id\": \"1217\""
        )
        # Completeness is decided from explicit upstream finish_reason/stop_reason,
        # not heuristic text-shape guessing.
        assert translation._translation_result_problem(source, truncated) is None

    def test_detects_openai_chat_length_finish(self):
        assert translation._completion_incomplete_reason({
            "choices": [{"finish_reason": "length", "message": {"content": "partial"}}]
        }, "openai-chat") == "finish_reason=length"

    def test_uses_configured_output_limit_as_single_budget(self):
        assert translation._translation_token_budgets("hello", 128000) == [128000]

    def test_override_max_tokens_precedence(self):
        cfg = {"modelOverrides": {"m": {"maxTokens": 64000}}}
        assert translation._override_max_tokens(cfg, "m", "m") == 64000

    def test_body_override_max_tokens_supported(self):
        cfg = {"modelOverrides": {"m": {"body": {"max_tokens": 32000}}}}
        assert translation._override_max_tokens(cfg, "m", "m") == 32000


# ─── build_translation_body 协议适配 ─────────────────────────────

class TestBuildTranslationBody:
    """验证按 channel protocol 构造正确格式的翻译请求 body。"""

    def _make_mock_channel(self, protocol: str):
        class MockCh:
            pass
        ch = MockCh()
        ch.protocol = protocol
        return ch

    def test_anthropic_body(self):
        ch = self._make_mock_channel("anthropic")
        body, ingress = translation._build_translation_body(
            ch, "translate me", "You are a translator", 4096,
        )
        assert ingress == "anthropic"
        assert body["system"] == "You are a translator"
        assert body["messages"][0]["role"] == "user"
        assert body["messages"][0]["content"] == translation._wrap_source_text("translate me")
        assert body["stream"] is False

    def test_openai_chat_body(self):
        ch = self._make_mock_channel("openai-chat")
        body, ingress = translation._build_translation_body(
            ch, "translate me", "You are a translator", 4096,
        )
        assert ingress == "chat"
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == "You are a translator"
        assert body["messages"][1]["role"] == "user"
        assert body["messages"][1]["content"] == translation._wrap_source_text("translate me")

    def test_openai_responses_body(self):
        ch = self._make_mock_channel("openai-responses")
        body, ingress = translation._build_translation_body(
            ch, "translate me", "You are a translator", 4096,
        )
        assert ingress == "responses"
        assert body["instructions"] == "You are a translator"
        assert body["input"] == translation._wrap_source_text("translate me")
        assert body.get("max_output_tokens") == 4096

    def test_configured_translation_model_overrides(self):
        ch = self._make_mock_channel("openai-chat")
        body, _ = translation._build_translation_body(
            ch, "translate me", "You are a translator", 4096,
        )
        cfg = {
            "modelOverrides": {
                "deepseek-v4-flash": {
                    "body": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}
                }
            }
        }
        translation._apply_translation_model_overrides(
            ch, body, cfg, "deepseek-v4-flash", "deepseek-v4-flash",
        )
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "max"
        assert body[translation._PARROT_ALLOW_OPENAI_THINKING_KEY] is True

    def test_no_translation_override_does_not_add_thinking(self):
        ch = self._make_mock_channel("openai-chat")
        body, _ = translation._build_translation_body(
            ch, "translate me", "You are a translator", 4096,
        )
        translation._apply_translation_model_overrides(
            ch, body, {"modelOverrides": {}}, "gpt-5.5", "gpt-5.5",
        )
        assert "thinking" not in body
        assert "reasoning_effort" not in body
        assert translation._PARROT_ALLOW_OPENAI_THINKING_KEY not in body


class TestOpenAIApiChannelThinkingPassthrough:
    def test_internal_thinking_flag_is_passed_to_openai_chat_payload(self):
        from src.openai.channel.api_channel import OpenAIApiChannel

        ch = OpenAIApiChannel({
            "name": "DeepSeek-Test",
            "type": "api",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "sk-test",
            "protocol": "openai-chat",
            "models": [{"real": "deepseek-v4-flash", "alias": "deepseek-v4-flash"}],
            "enabled": True,
        })
        body, _ = translation._build_translation_body(
            ch, "translate me", "You are a translator", 4096,
        )
        body["model"] = "deepseek-v4-flash"
        translation._apply_translation_model_overrides(
            ch, body,
            {"modelOverrides": {"deepseek-v4-flash": {"body": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}}}},
            "deepseek-v4-flash", "deepseek-v4-flash",
        )

        req = asyncio.run(ch.build_upstream_request(
            body, "deepseek-v4-flash", ingress_protocol="chat",
        ))
        payload = json.loads(req.body.decode("utf-8"))
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "max"
        assert translation._PARROT_ALLOW_OPENAI_THINKING_KEY not in payload

    def test_public_chat_passthrough_still_drops_unflagged_thinking(self):
        from src.openai.channel.api_channel import OpenAIApiChannel

        ch = OpenAIApiChannel({
            "name": "OpenAI-Compatible-Test",
            "type": "api",
            "baseUrl": "https://example.test",
            "apiKey": "sk-test",
            "protocol": "openai-chat",
            "models": [{"real": "gpt", "alias": "gpt"}],
            "enabled": True,
        })
        req = asyncio.run(ch.build_upstream_request({
            "model": "gpt",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        }, "gpt", ingress_protocol="chat"))
        payload = json.loads(req.body.decode("utf-8"))
        assert "thinking" not in payload
        # reasoning_effort is a standard OpenAI-compatible chat field and remains passthrough.
        assert payload["reasoning_effort"] == "max"


class TestReadiness:
    def test_validate_ready_requires_model(self):
        cfg = translation._get_cfg()
        cfg["enabled"] = True
        cfg["model"] = ""
        ok, reason = translation.validate_ready(cfg, require_enabled=True)
        assert ok is False
        assert "未设置" in reason

    def test_validate_ready_requires_channel(self):
        cfg = translation._get_cfg()
        cfg["enabled"] = True
        cfg["model"] = "nonexistent-model"
        ok, reason = translation.validate_ready(cfg, require_enabled=True)
        assert ok is False
        assert "不可用" in reason


# ─── 配置 ─────────────────────────────────────────────────────────

class TestConfig:
    def test_default_config_in_place(self):
        from src.config import DEFAULT_CONFIG
        tl = DEFAULT_CONFIG.get("translation")
        assert tl is not None
        assert tl["enabled"] is False
        assert tl["targetLanguage"] == "English"
        assert tl["timeoutSeconds"] == 10
        assert tl["cacheTtlDays"] == 3
        assert tl["memoryCacheMaxMb"] == 100
        assert tl["memoryCacheTtlSeconds"] == 7200
        assert tl["translateSystemMessages"] is False
        assert tl["scope"] == {"models": [], "channels": []}
        assert tl["modelOverrides"] == {}

    def test_get_cfg_merges_defaults(self):
        """即使 config 中翻译段缺字段，_get_cfg 补齐默认值。"""
        config.update(lambda c: c.__setitem__("translation", {"enabled": True}))
        cfg = translation._get_cfg()
        assert cfg["enabled"] is True
        assert cfg["targetLanguage"] == "English"  # 默认补齐
        assert cfg["timeoutSeconds"] == 10


# ─── TG 菜单模块可导入 ───────────────────────────────────────────

class TestMenuImport:
    def test_translation_menu_importable(self):
        from src.telegram.menus import translation_menu
        assert hasattr(translation_menu, "handle_callback")
        assert hasattr(translation_menu, "handle_text_state")
        assert hasattr(translation_menu, "show")
        assert hasattr(translation_menu, "send_new")

    def test_languages_list(self):
        from src.telegram.menus import translation_menu
        langs = [v for v, _ in translation_menu._LANGUAGES]
        assert "English" in langs
        assert "Japanese" in langs
        assert "Chinese" in langs
        assert "Malay" in langs
