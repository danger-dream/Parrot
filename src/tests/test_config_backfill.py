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
        "apiKeys": {},
        "oauthAccounts": [],
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

        # 旧路径保留，不删除用户原配置，方便兼容和人工核对。
        assert saved["oauth"]["providers"]["openai"]["defaultModels"] == ["legacy-model"]
    finally:
        with open(config.path(), "w", encoding="utf-8") as f:
            json.dump(original, f, ensure_ascii=False, indent=2)
        config._cache = None
        config._mtime = 0
        config.reload()
