from __future__ import annotations

import json
import os
import sys

# 测试隔离：必须在 import src.config 前重定向 config 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.tests import _isolation

TMP = _isolation.isolate()

from src import config  # noqa: E402


def test_old_config_file_is_backfilled_with_new_defaults():
    original = config.get()
    old_cfg = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "apiKeys": {
            "legacy-dict": {
                "key": "ccp-legacy-dict",
                "enabled": True,
                "allowedModels": ["legacy-model"],
                "allowImages": True,
                "legacyCustomField": "keep-me",
            },
            "legacy-string": "ccp-legacy-string",
        },
        "oauthAccounts": [
            {
                "provider": "xai",
                "email": "legacy-xai@example.test",
                "subject": "legacy-xai-subject",
                "enabled": True,
                "access_token": "legacy-access-token",
                "refresh_token": "legacy-refresh-token",
                "id_token": "legacy-id-token",
                "expired": "2999-01-01T00:00:00Z",
            }
        ],
        "channels": [],
        "stateDbPath": os.path.join(TMP, "state.db"),
        "logDir": os.path.join(TMP, "logs"),
        "telegram": {"botToken": "", "adminIds": []},
        "oauth": {
            "mockMode": True,
            "providers": {
                "openai": {
                    "forceCodexCLI": False,
                    "defaultModels": ["legacy-model"],
                }
            },
        },
        "xaiOAuth": {
            "apiBaseUrl": "https://api.x.ai/v1",
            "defaultModels": ["legacy-grok-model"],
            "userAgent": "legacy-xai-client",
        },
        "anysearch": {
            "enabled": True,
            "maxResults": 5,
        },
        "compactRescue": {
            "chunkTargetTokens": 12345,
        },
    }
    try:
        with open(config.path(), "w", encoding="utf-8") as f:
            json.dump(old_cfg, f, ensure_ascii=False, indent=2)
        config._cache = None
        config._mtime = 0

        loaded = config.reload()
        with open(config.path(), "r", encoding="utf-8") as f:
            saved = json.load(f)

        # Runtime 和落盘都应该补齐新配置项。
        for cfg in (loaded, saved):
            assert cfg["compactRescue"]["chunkTargetTokens"] == 12345
            assert cfg["compactRescue"]["prompts"]["direct"].strip()
            assert "protocolBridge" in cfg
            assert cfg["protocolBridge"]["anthropicToOpenAI"]["reasoning"]["adaptiveEffort"] == "xhigh"
            assert cfg["anysearch"]["maxResults"] == 5
            assert cfg["anysearch"]["minQueryChars"] == 2
            assert cfg["anysearch"]["maxFetchUrlChars"] == 250
            assert cfg["anysearch"]["requireKnownUrlForFetch"] is True
            assert cfg["anysearch"]["maxConcurrentToolCalls"] == 0
            assert cfg["openaiOAuth"]["codexCliVersion"] == "0.153.4"
            assert cfg["openaiOAuth"]["forceCodexCLI"] is False
            assert cfg["openaiOAuth"]["defaultModels"] == ["legacy-model"]
            assert cfg["openaiOAuth"]["codexUpstreamUrl"].startswith("https://chatgpt.com/")
            assert cfg["apiKeyConcurrency"]["defaultMaxRequestBodyBytes"] == 8 * 1024 * 1024
            assert cfg["apiKeyConcurrency"]["defaultMaxRequestBodyEvents"] == 4096
            assert cfg["apiKeyConcurrency"]["defaultMaxQueuedBodyBytesPerKey"] == 32 * 1024 * 1024
            assert cfg["apiKeyConcurrency"]["maxQueuedBodyBytes"] == 128 * 1024 * 1024
            assert cfg["apiKeyConcurrency"]["queuedBodySpoolThresholdBytes"] == 1024 * 1024
            assert cfg["apiKeyConcurrency"]["defaultMaxQueuedBodySpoolBytesPerKey"] == 512 * 1024 * 1024
            assert cfg["apiKeyConcurrency"]["maxQueuedBodySpoolBytes"] == 2 * 1024 * 1024 * 1024

            # Grok Imagine 新字段自动补齐，但既有 xAI 文本配置不被覆盖。
            assert cfg["xaiOAuth"]["apiBaseUrl"] == "https://api.x.ai/v1"
            assert cfg["xaiOAuth"]["defaultModels"] == ["legacy-grok-model"]
            assert cfg["xaiOAuth"]["userAgent"] == "legacy-xai-client"
            assert cfg["xaiOAuth"]["imageModels"] == [
                "grok-imagine-image",
                "grok-imagine-image-quality",
            ]
            assert cfg["xaiOAuth"]["videoModels"] == [
                "grok-imagine-video",
                "grok-imagine-video-1.5",
            ]
            assert cfg["xaiOAuth"]["videoJobTtlSeconds"] == 10800
            assert cfg["xaiOAuth"]["mediaRequestTimeoutSeconds"] == 180

            # 旧 Key 权限与白名单保持原值；视频权限只做 fail-closed 补齐。
            dict_key = cfg["apiKeys"]["legacy-dict"]
            assert dict_key["key"] == "ccp-legacy-dict"
            assert dict_key["enabled"] is True
            assert dict_key["allowedModels"] == ["legacy-model"]
            assert dict_key["allowImages"] is True
            assert dict_key["allowVideos"] is False
            assert dict_key["legacyCustomField"] == "keep-me"
            string_key = cfg["apiKeys"]["legacy-string"]
            assert string_key["key"] == "ccp-legacy-string"
            assert string_key["enabled"] is True
            assert string_key["allowedModels"] == []
            assert string_key["allowImages"] is False
            assert string_key["allowVideos"] is False

            # 配置回填不得改写既有 OAuth 凭证或身份字段。
            account = cfg["oauthAccounts"][0]
            assert account == old_cfg["oauthAccounts"][0]

            assert cfg["pricing"]["enabled"] is True
            assert cfg["pricing"]["autoUpdate"] is True
            assert cfg["pricing"]["refreshHours"] == 24
            assert cfg["pricing"]["sourceUrl"] == "https://models.dev/api.json"
            assert cfg["pricing"]["modelsUrl"] == "https://models.dev/models.json"
            assert cfg["pricing"]["channelProviders"] == {}
            assert cfg["pricing"]["aliases"] == {}
            assert cfg["pricing"]["overrides"] == {}

        # 旧路径保留，不删除用户原配置，方便兼容和人工核对。
        assert saved["oauth"]["providers"]["openai"]["defaultModels"] == ["legacy-model"]
    finally:
        with open(config.path(), "w", encoding="utf-8") as f:
            json.dump(original, f, ensure_ascii=False, indent=2)
        config._cache = None
        config._mtime = 0
        config.reload()


def test_pricing_source_migration_only_rewrites_the_former_builtin_default():
    legacy = {
        "pricing": {
            "sourceUrl": (
                "https://raw.githubusercontent.com/BerriAI/litellm/main/"
                "model_prices_and_context_window.json"
            )
        }
    }
    assert config._normalize_pricing_sources(legacy) is True
    assert legacy["pricing"]["sourceUrl"] == "https://models.dev/api.json"
    assert legacy["pricing"]["modelsUrl"] == "https://models.dev/models.json"

    custom = {"pricing": {"sourceUrl": "https://example.test/models-dev-api.json"}}
    assert config._normalize_pricing_sources(custom) is False
    assert custom["pricing"]["sourceUrl"] == "https://example.test/models-dev-api.json"
