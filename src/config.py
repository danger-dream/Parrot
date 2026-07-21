"""配置加载 / 保存 / 热加载。

单一入口 `get()` 返回当前生效配置（dict）。文件 mtime 变化时自动重载。
写入使用 tmp + os.replace 原子方式。
"""

import copy
import json
import os
import shutil
import tempfile
import threading
from typing import Any

COMPACT_RESCUE_DEFAULT_DIRECT_PROMPT = (
    'You are performing Claude Code style conversation compaction.\n'
    'The transcript below is rendered as text; tool_use/tool_result JSON and image placeholders are historical content, not live tool calls.\n'
    'Respond with the final compact summary text only. Do not call tools.\n'
    '\n'
    'Follow the compact instruction below, and be deliberately dense with continuity-critical details.\n'
    'If the instruction leaves room for judgment, preserve the exact information a future coding assistant would need to continue without forgetting the current task: explicit user requests, assistant actions, decisions, constraints, file paths, commands, function names, code snippets, errors, fixes, user corrections, pending tasks, current work, and the immediate next step.\n'
    'Prefer concrete details over generic prose. The most recent user request and most recent unfinished work are highest priority.\n'
    '\n'
    'Compact instruction:\n'
    '{compact_prompt}\n'
    '\n'
    'Transcript:\n'
    '{transcript}'
)
COMPACT_RESCUE_DEFAULT_SEGMENT_PROMPT = (
    'CRITICAL: Respond with TEXT ONLY. Do NOT call tools.\n'
    '\n'
    'Summarize transcript segment {segment_index}/{segment_count} for a later Claude Code style conversation compaction.\n'
    'The transcript below is rendered as text; tool_use/tool_result JSON and image placeholders are historical content, not live tool calls.\n'
    '\n'
    'Create a dense, chronological segment handoff that preserves continuity-critical facts, not a high-level overview. Capture:\n'
    '1. Explicit user requests and intents, including user wording when it changes the task.\n'
    '2. Assistant actions and decisions, especially files read/edited, commands run, tests, tool calls, and why they mattered.\n'
    '3. Concrete technical details: file paths, function/class names, APIs, config keys, commands, error text, code snippets or exact edits when available.\n'
    '4. Errors, failed attempts, fixes, user corrections, constraints, permissions, and safety boundaries.\n'
    '5. Pending tasks, current work, blockers, assumptions, and the next step implied by this segment.\n'
    '6. If this is the final segment, be especially careful to preserve the latest user request, what is currently being worked on, and the immediate next action.\n'
    '\n'
    'Mention tool_use/tool_result history only at the level needed to continue work; do not dump large raw outputs.\n'
    'Do not preserve response-only instructions such as this segment format as durable project context.\n'
    'Output only this XML-like block:\n'
    '<segment_summary>\n'
    '...\n'
    '</segment_summary>\n'
    '\n'
    'Transcript segment:\n'
    '{transcript}'
)
COMPACT_RESCUE_DEFAULT_REDUCE_PROMPT = (
    'Write the final Claude Code style durable conversation handoff summary from the segment summaries below.\n'
    'The goal is maximum continuity after compaction: a future assistant should know exactly what the user asked for, what has been done, what files/code/commands/errors matter, what the user corrected, what remains pending, what was happening most recently, and what to do next.\n'
    '\n'
    'Original compact instruction, when present, is the style and structure to approximate:\n'
    '{compact_prompt}\n'
    '\n'
    'Important preservation rules:\n'
    '- The latest user request and latest unfinished/current work have highest priority; do not let older segments drown them out.\n'
    '- Preserve concrete paths, commands, function names, config keys, exact error messages, code snippets/edits, test results, decisions, constraints, user corrections, unresolved blockers, and immediate next steps.\n'
    '- Include all user messages that are represented in the segment summaries, especially recent ones and any message that changed requirements.\n'
    '- Do not invent details missing from the segment summaries; state uncertainty or omit instead.\n'
    '- Do not mention this reduction step, segment summaries, compact prompts, or internal formatting instructions as user requests or project context.\n'
    "- Do not preserve response-only instructions such as tool bans, XML formatting requirements, or 'text only' constraints as durable memory.\n"
    '\n'
    'Before providing the final summary, use <analysis> to check chronological coverage, missing current-work details, and whether the next step follows directly from the most recent request.\n'
    'Output exactly two top-level XML-like blocks: <analysis>...</analysis> then <summary>...</summary>.\n'
    'Inside <summary>, use these numbered sections and make each section specific:\n'
    '1. Primary Request and Intent\n'
    '2. Key Technical Concepts\n'
    '3. Files and Code Sections\n'
    '4. Errors and fixes\n'
    '5. Problem Solving\n'
    '6. All user messages\n'
    '7. Pending Tasks\n'
    '8. Current Work\n'
    '9. Optional Next Step\n'
    '\n'
    'Durable context excerpts:\n'
    '{summaries}'
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# DATA_DIR 是所有运行时持久化文件的根目录（config.json / state.db / logs/ / .anthropic_proxy_ids.json）。
# 优先使用环境变量 ANTHROPIC_PROXY_DATA_DIR（容器内通常是 /app/data），不设则回退到 BASE_DIR，
# 保持现有源码安装方式（systemd 直跑）行为完全不变。
DATA_DIR = os.environ.get("ANTHROPIC_PROXY_DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)

# CONFIG_PATH 仍单独支持 ANTHROPIC_PROXY_CONFIG（测试场景用），否则走 DATA_DIR/config.json。
CONFIG_PATH = os.environ.get("ANTHROPIC_PROXY_CONFIG") or os.path.join(DATA_DIR, "config.json")

DEFAULT_CONFIG: dict[str, Any] = {
    "listen": {"host": "0.0.0.0", "port": 18082},
    "apiKeys": {},
    # 下游 API Key 级并发限制默认值；单 Key 可用 apiKeys.<name>.limits 覆盖。
    "apiKeyConcurrency": {
        "enabled": True,
        "defaultMaxConcurrent": 5,
        "defaultMaxQueue": 50,
        "defaultQueueWaitSeconds": 1800,
        # 排队期间 disconnect watcher 会预读并回放请求体；以下单请求字节/事件
        # 上限只约束该 replay 资源。非排队核心 API 保持各协议入口原有契约；图片
        # 入口另按 maxInputImageBytes 和 multipart/JSON 形态执行 endpoint 上限。
        "defaultMaxRequestBodyBytes": 8388608,
        "defaultMaxRequestBodyEvents": 4096,
        "defaultMaxQueuedBodyBytesPerKey": 33554432,
        "maxQueuedBodyBytes": 134217728,
        "queuedBodySpoolThresholdBytes": 1048576,
        "defaultMaxQueuedBodySpoolBytesPerKey": 536870912,
        "maxQueuedBodySpoolBytes": 2147483648,
    },
    "oauthAccounts": [],
    "channels": [],
    "images": {
        "enabled": True,
        "mainModel": "gpt-5.4-mini",
        "toolModel": "gpt-image-2",
        "disabledAccounts": [],
        "cacheEnabled": False,
        "cachePath": "images",
        "cacheRetentionDays": 0,
        "cacheMaxBytes": 1073741824,
        "accountCooldownSeconds": 300,
        "requestTimeoutSeconds": 180,
        "maxPromptChars": 4000,
        "maxInputImageBytes": 20971520,
        "dbPath": "image_logs.db",
    },
    "timeouts": {
        "connect": 10,
        "firstByte": 30,
        "idle": 120,    # chunk 之间最长空闲；上游推理慢需要更宽松
        "total": 600,
    },
    "shutdown": {
        # SIGTERM/SIGINT 后进入 drain，最多等待活跃请求/流式响应完成的秒数。
        # 默认 80s 低于 systemd 默认 TimeoutStopSec=90，避免被 systemd 提前 SIGKILL。
        "drainTimeoutSeconds": 80,
    },
    # ─── 出站网络设置 ─────────────────────────────────────────
    # DNS 默认 8.8.8.8；首次启动时若 bootstrapFromSystem=true 且 bootstrapped=false，
    # 会从系统 /etc/resolv.conf 同步一次并写回 config，之后不再自动覆盖用户设置。
    # SOCKS5 启用后，所有 HTTP 出站请求走 SOCKS5；目标域名交给 SOCKS5 代理端，
    # 只有 SOCKS5 服务器地址本身是域名时才用这里的 DNS 解析。
    "network": {
        "dns": {
            "servers": ["8.8.8.8"],
            "bootstrapFromSystem": True,
            "bootstrapped": False,
            "timeoutSeconds": 3,
            "cacheTtlSeconds": 300,
        },
        "socks5": {
            "enabled": False,
            "url": "",
        },
        # ─── New proxy subsystem (2026-05-27) ─────────────────────
        # proxies: named proxy definitions (socks5 / ss2022)
        # groups: ordered lists of proxy names for failover
        # routing: maps contexts to proxy/group names
        "proxies": {},
        "groups": {},
        "routing": {
            "default": "direct",
            # 仅当已配置的非直连路由无法解析/建立时，才允许自动追加 direct。
            # 未配置任何网络规则时仍按正常默认值直连，不受此开关限制。
            "directFallback": False,
            # "telegram": "direct",
            # "oauth": "direct",
            # "models": {},
            # "channels": {},
        },
        "monitor": {
            "enabled": True,
            "intervalSeconds": 60,   # 最小 5 秒；UI 会校验
            "timeoutSeconds": 5,
            "dns": False,
            "socks5": False,
            "channels": {
                "enabled": False,
                "byKey": {},         # {"api:name": true, "oauth:...": false}
            },
            "core": {
                "openai": False,
                "claude": False,
                "cloudflare": False,
            },
        },
    },
    # 渠道并发限制（2026-04-22 新增）
    # 每个渠道同一时刻最多多少个在途请求；满了则在候选渠道里排队等位。
    # queueWaitSeconds 到了仍无位置 → 客户端收到 429 rate_limit_error。
    "concurrency": {
        "enabled": True,
        "queueWaitSeconds": 30,           # TG Bot 可改，全满排队超时
        "defaultMaxConcurrent": 0,        # 渠道未配 maxConcurrent 时的默认（0=不限）
    },
    "errorWindows": [1, 3, 5, 10, 15, 0],
    # OAuth 渠道宽容次数：前 N 次失败只累计计数不进入冷却（成功一次清零）。
    # 第 N+1 次失败开始按 errorWindows 阶梯。设计目的：避免单 OAuth 账号
    # 因偶发 timeout 立即冷却导致所有 Claude 模型不可用。
    "oauthGraceCount": 3,
    # OAuth 账户用量展示口径：
    #   used      = 展示上游返回的已使用百分比（默认，兼容旧 UI）
    #   remaining = 展示剩余百分比（100 - 已使用百分比）
    "oauthUsageDisplayMode": "used",
    # Ladder throttle（2026-04-21 新增，防客户端/并发爆发把渠道打穿）：
    # 两次阶梯推进最少间隔 N 秒，期间失败仅累计计数、不推进 cooldown_until。
    # 设 0 关闭该保护。默认 30 秒足够挡住客户端秒级重试。
    "cooldownLadderMinIntervalSeconds": 30,
    # 永久冷却门槛：从首次失败（first_error_at）起，至少持续 N 秒仍在失败
    # 才允许进入永久档；不够时回退到倒数第二档。避免短时爆发误判为永久。
    # 与默认 errorWindows=[1,3,5,10,15,0] 配合：正常爬到永久需 1+3+5+10+15=34min，
    # 默认 300s=5min 几乎不影响正常路径，只挡爆发式失败。设 0 关闭该保护。
    "cooldownPermanentMinAgeSeconds": 300,
    "affinity": {
        "ttlMinutes": 30,
        "cleanupIntervalSeconds": 300,
        "clientTtlMinutes": 120,
    },
    "scoring": {
        "emaAlpha": 0.25,
        "recentWindow": 50,
        "defaultScore": 3000,
        "errorPenaltyFactor": 8,
        "staleMinutes": 15,
        "staleFullDecayMinutes": 30,
        "explorationRate": 0.2,
    },
    "cooldownRecovery": {
        "enabled": True,
        "intervalSeconds": 30,
        "timeoutSeconds": 15,
    },
    "quotaMonitor": {
        # 默认关闭：避免每 60s 拉一次 /api/oauth/usage 频繁请求 Anthropic 风控盯上。
        # 用户可在 TG bot「🔐 管理 OAuth」→「⚙️ 账户设置」→「📈 配额监控」按需启用。
        "enabled": False,
        "intervalSeconds": 60,
        "disableThresholdPercent": 95,
        "resumeThresholdPercent": 95,
        # 按访问节流刷新 usage：quotaMonitor.enabled=False 时，TG bot 每次打开
        # 主菜单 / 状态总览 / OAuth 面板 / 详情，若 oauth_quota_cache 已超过该
        # 秒数没刷新，会同步触发一次 fetch_usage（真实 HTTP 限 5s 超时，失败读旧值）。
        # enabled=True 时此节流忽略，刷新由 intervalSeconds 后台循环负责。
        "accessRefreshThrottleSeconds": 180,
    },
    "contentBlacklist": {
        "default": [],
        "byChannel": {},
    },
    # ─── 通知开关（事件级分类） ───────────────────────────────────
    # enabled = 总开关；events 里每个事件可独立开关。
    # notifier.notify_event(key, text) 会同时检查 enabled 和 events[key]。
    "notifications": {
        "enabled": True,
        "events": {
            "channel_permanent": True,    # 渠道/模型连续失败进入永久冷却
            "channel_recovered": True,    # 永久/长冷却被清除（手动 / probe 恢复）
            "quota_disabled": True,       # OAuth 配额到达阈值被自动禁用
            "quota_resumed": True,        # OAuth 配额恢复被自动启用
            "oauth_refreshed": True,      # OAuth Token 自动刷新成功
            "oauth_refresh_failed": True, # OAuth Token 自动刷新失败（标 auth_error）
            "no_channels": True,          # 无可用渠道（503）
            "openai_store_save_failed": True,  # OpenAI previous_response_id Store 写入失败
            "status_alert": True,         # 上游 status page（Claude/OpenAI/Cloudflare）事件
            "app_update": True,           # Parrot 本身的新版本上线提醒
            "network_monitor": True,      # Parrot 自身网络健康检测失败/恢复
        },
    },
    # ─── 上游 status page 监控 ─────────────────────────────────
    # 监控 Claude / OpenAI / Cloudflare 的 statuspage incidents，
    # 出问题/恢复时第一时间通过 TG 推送（事件键: status_alert）。
    "statusMonitor": {
        "enabled": True,
        "intervalSeconds": 60,
        "targets": ["claude", "openai", "cloudflare"],
        "minImpact": "minor",  # none < maintenance < minor < major < critical
    },
    # ─── Parrot 自身版本更新检查 ─────────────────────────────
    # 后台定时拉 GitHub Releases，发现新版本 (semver > 当前) 时通过 TG 推送
    # + 主菜单底部 banner 提示。`ignoredVersions` 由 TG 操作写入。
    "updateChecker": {
        "enabled": True,
        "intervalSeconds": 3600,
        "includePrerelease": True,
        "repo": "danger-dream/Parrot",
        "ignoredVersions": [],
    },
    "translation": {
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
        "translateSystemMessages": False,
        "scope": {"models": [], "channels": []},
        "modelOverrides": {},
    },
    "anysearch": {
        "enabled": True,
        "apiKey": "",
        "endpoint": "https://api.anysearch.com/mcp",
        "timeoutSeconds": 30,
        "maxResults": 8,
        "maxFetchChars": 50000,
        "maxToolRounds": 50,
        "minQueryChars": 2,
        "maxFetchUrlChars": 250,
        "requireKnownUrlForFetch": True,
        # 0=不限并发，保持旧版 asyncio.gather 全并发行为；可手动设 2/3 限流。
        "maxConcurrentToolCalls": 0,
    },
    "cchMode": "disabled",
    "cchStaticValue": "00000",
    "oauthDefaultModels": [
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ],
    # Global alias mapping and model metadata are protocol/account agnostic.
    # modelMapping supports both the new global bucket and legacy per-ingress
    # buckets (anthropic/openai-chat/openai-responses) for backward compatibility.
    "modelMapping": {
        "global": {},
    },
    "ingressDefaultModel": {},
    "modelMetadata": {},
    "protocolBridge": {
        "anthropicToOpenAI": {
            "reasoning": {
                "adaptiveEffort": "xhigh",
                "maxEffort": "xhigh",
                "defaultEnabledEffort": "high",
                "budgetThresholds": [
                    {"lt": 4000, "effort": "low"},
                    {"lt": 16000, "effort": "medium"},
                    {"effort": "high"},
                ],
            },
            "disableParallelToolCallsForLocalWeb": True,
        },
        "serviceTier": {
            "anthropicToOpenAI": {
                "auto": "auto",
                "standard_only": "default",
            },
            "anthropicToCodex": {
                "auto": "priority",
                "standard_only": None,
                "default": None,
            },
            "openaiToAnthropic": {
                "auto": "auto",
                "default": "standard_only",
                "standard_only": "standard_only",
            },
        },
    },
    "compactRescue": {
        "enabled": True,
        # Claude Code compact prompt 识别关键词；全部命中才进入 compact rescue。
        "markers": [
            "critical: respond with text only",
            "create a detailed summary of the conversation so far",
            "after compaction",
            "your summary should include the following sections",
        ],
        # Claude Code compact map-reduce 每段目标 token 数。按 token 算，不按字符数。
        "chunkTargetTokens": 100000,
        # 内部 segment/reduce 输出预算，避免继承客户端超大的 max_tokens 挤爆窗口。
        "reduceMaxTokens": 20000,
        # direct compact / fit 判断保留给最终 summary 的输出预算。
        "summaryReserveTokens": 20000,
        # fit 判断额外安全 buffer。
        "safetyBufferTokens": 20000,
        # segment 并发；0=不限，保持旧版 gather 全并发行为。
        "segmentConcurrency": 0,
        "binaryOmitMinChars": 4096,
        "binarySampleChars": 4096,
        "binaryAsciiRatio": 0.95,
        "prompts": {
            "direct": COMPACT_RESCUE_DEFAULT_DIRECT_PROMPT,
            "segment": COMPACT_RESCUE_DEFAULT_SEGMENT_PROMPT,
            "reduce": COMPACT_RESCUE_DEFAULT_REDUCE_PROMPT,
        },
    },
    "probe": {
        "timeoutSeconds": 60,
        "maxTokens": 50,
        "userMessage": "1+1=?",
    },
    "telegram": {
        "botToken": "",
        "adminIds": [],
        # 统计汇总页各段可见性（仅影响 TG Bot 「📈 统计汇总」汇总视图；
        # 专题视图不受影响）。默认全可见；用户可在「📈 统计汇总」→「⚙ 设置」切换。
        "statsVisibility": {
            "byChannel": True,     # 按渠道 Top（家族段内）
            "byModel": True,       # 按模型 Top（家族段内）
            "byApiKey": True,      # 按 Key Top（跨家族）
            "cacheMisses": True,   # 最近未命中样本
            "recentCalls": True,   # 最近调用
        },
    },
    # Telegram UI 展示增强。providerCustomEmoji 只用于消息正文 HTML；
    # providerBtnEmoji 用于 inline keyboard / 纯文本兜底。
    "telegramUi": {
        "providerCustomEmoji": {
            "openai": "5861557411784957025",
            "claude": "5872779796257184592",
            "xai": "5819115571463068721",
        },
        "providerBtnEmoji": {
            "openai": "🅾️",
            "claude": "🅰️",
            "xai": "𝕏",
        },
    },
    "oauth": {
        "mockMode": False,
        # provider 专属旧配置入口。OpenAI OAuth 新配置请使用顶层 openaiOAuth；
        # 这里保留空 providers 仅用于兼容旧 config.json。
        "providers": {},
    },
    "channelSelection": "smart",  # "smart" | "order" | "priority"
    "loadBalancing": {
        "initialized": False,
        "priorityOrders": {
            "anthropic": [],
            "openai": [],
        },
    },
    "logDir": "logs",
    "stateDbPath": "state.db",
    # OpenAI OAuth/Codex 简化配置。旧版 oauth.providers.openai 仍兼容；加载旧配置时会自动补齐到这里。
    "openaiOAuth": {
        "forceCodexCLI": True,
        "enableTLSFingerprint": False,
        "isolateSessionId": True,
        "defaultModels": [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.2",
            "gpt-5.2-codex",
            "gpt-5.3-codex",
        ],
        "codexUpstreamUrl": "https://chatgpt.com/backend-api/codex/responses",
        "defaultInstructions": "You are a helpful coding assistant.",
        "quotaProbe": {
            "input": "1",
            "instructions": "reply ok",
            "fallbackModel": "gpt-5.2",
        },
    },
    # xAI / Grok OAuth 配置。默认值对齐当前 xAI CLI/Grok OAuth；
    # client/scope/redirect/discovery/apiBase 均可在这里覆盖。
    "xaiOAuth": {
        "issuer": "https://auth.x.ai",
        "discoveryUrl": "https://auth.x.ai/.well-known/openid-configuration",
        "clientId": "b1a00492-073a-47ea-816f-4c329264a828",
        "redirectUri": "http://127.0.0.1:56121/callback",
        "scope": "openid profile email offline_access grok-cli:access api:access",
        "apiBaseUrl": "https://api.x.ai/v1",
        "baseUrl": "https://api.x.ai/v1",
        "cliProxyBaseUrl": "https://cli-chat-proxy.grok.com/v1",
        "cliClientVersion": "0.2.93",
        "responsesPath": "",
        "isolateSessionId": True,
        "userAgent": "parrot/xai-oauth-adapter",
        "defaultModels": [
            "grok-4.5",
        ],
    },
    # OpenAI 支持相关默认值（只在 /v1/chat/completions、/v1/responses 入口或 openai-* 渠道上生效）
    "openai": {
        # previous_response_id 本地 store（跨变体 chat↔responses 必需，同协议可选）
        "store": {
            "enabled": True,
            "ttlMinutes": 60,
            "cleanupIntervalSeconds": 300,
        },
        # reasoning 跨协议桥接："passthrough" = 通过非官方字段 reasoning_content 双向映射；"drop" = 丢弃
        "reasoningBridge": "passthrough",
        # 自动补 OpenAI prompt_cache_key：仅 /v1/chat/completions 与 /v1/responses 生效。
        # 下游显式传入时绝不覆盖；未传时根据亲和链复用会话级 key，
        # 帮 OpenAI/Codex 上游稳定 prompt cache 路由。
        "autoPromptCacheKey": {
            "enabled": True,
            "prefix": "parrot:auto:v1",
        },
        # HTTP /v1/responses 入口是否把 OpenAI OAuth Codex 上游传输切到 WebSocket。
        # 下游真实 WebSocket /v1/responses 入口不受此开关影响，始终可用。
        "responsesUpstreamWsForOAuth": False,
        # 跨变体翻译能力开关
        "translation": {
            "enabled": True,
            "rejectOnBuiltinTools": True,
            "rejectOnMultiCandidate": True,
        },
    },
}


_cache: dict[str, Any] | None = None
_mtime: float = 0.0
# 必须是可重入锁 (RLock)：同一线程内的加载/保存辅助函数可能再次访问配置。
# reload callbacks 始终在锁外执行，避免 callback 跨模块重入造成死锁。
_lock = threading.RLock()
_reload_callbacks: list = []


def _deep_merge_defaults(base: dict, override: dict) -> dict:
    """把 override 合并到 base 的深拷贝上，缺失字段用 base 补齐。"""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_defaults(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _normalize_openai_oauth_config(cfg: dict, raw: dict | None = None) -> bool:
    """把旧版 oauth.providers.openai 自动补齐到新版 openaiOAuth。

    新配置入口更短：openaiOAuth。为了兼容已经部署的旧 config.json，
    当用户没有显式写 openaiOAuth 时，把旧层级的值复制过去并持久化。
    旧层级保留读取兼容，不删除。
    """
    raw = raw if isinstance(raw, dict) else {}
    legacy = (((cfg.get("oauth") or {}).get("providers") or {}).get("openai") or {})
    if not isinstance(legacy, dict):
        legacy = {}
    current = cfg.get("openaiOAuth") if isinstance(cfg.get("openaiOAuth"), dict) else {}
    default = DEFAULT_CONFIG.get("openaiOAuth") if isinstance(DEFAULT_CONFIG.get("openaiOAuth"), dict) else {}
    if isinstance(raw.get("openaiOAuth"), dict):
        # 新入口已经存在：只做默认字段补齐，避免旧层级反向覆盖新配置。
        merged = _deep_merge_defaults(default, current)
    elif legacy:
        # 老配置升级：旧层级覆盖默认值，作为新版 openaiOAuth 初始值。
        merged = _deep_merge_defaults(default, legacy)
    else:
        merged = _deep_merge_defaults(default, current)
    if current != merged:
        cfg["openaiOAuth"] = merged
        return True
    return False


def _normalize_api_keys(cfg: dict) -> bool:
    """把 apiKeys 里的旧式字符串条目升级为 dict 结构（向前兼容）。

    旧格式：`{"name": "ccp-xxx"}`
    新格式：`{"name": {"key": "ccp-xxx", "allowedModels": []}}`

    返回 True 表示做了变更，调用方需要 write 回磁盘。allowedModels 为空列表
    代表"无限制"；非空则是白名单。
    """
    keys = cfg.get("apiKeys") or {}
    if not isinstance(keys, dict):
        return False
    changed = False
    for name, v in list(keys.items()):
        if isinstance(v, str):
            keys[name] = {"key": v, "enabled": True, "allowedModels": []}
            changed = True
        elif isinstance(v, dict):
            if "key" not in v:
                # 无效条目（无 key），丢弃
                del keys[name]
                changed = True
                continue
            if "enabled" not in v:
                # Key 自身可用开关；缺失/空值按启用处理。
                v["enabled"] = True
                changed = True
            if "allowedModels" not in v:
                v["allowedModels"] = []
                changed = True
            if "allowImages" not in v:
                # 新增图片接口权限默认关闭，避免老 Key 自动获得新能力。
                v["allowImages"] = False
                changed = True
        else:
            # 其它类型（list / None 等）视为无效
            del keys[name]
            changed = True
    return changed


def _load_from_disk() -> dict:
    if not os.path.exists(CONFIG_PATH):
        initial = copy.deepcopy(DEFAULT_CONFIG)
        _write_atomic(initial)
        return initial
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    merged = _deep_merge_defaults(DEFAULT_CONFIG, raw)
    # 自动升级旧式配置结构并持久化；同时把新增默认配置项写回磁盘。
    # 这样从旧版本升级的用户不仅运行时能拿到默认值，config.json 里也会
    # 自动出现 compactRescue / protocolBridge / openaiOAuth / anysearch 新字段，
    # 方便后续自行编辑。
    changed = merged != raw
    if changed:
        print("[config] backfilled missing config defaults")
    if _normalize_api_keys(merged):
        changed = True
        print("[config] upgraded legacy apiKeys to new structure")
    if _normalize_openai_oauth_config(merged, raw):
        changed = True
        print("[config] backfilled openaiOAuth from defaults/legacy oauth.providers.openai")
    if changed:
        _write_atomic(merged)
    return merged


_BACKUP_KEEP = 3  # 保留最近 3 份 config 备份


def _rotate_backups() -> None:
    """在覆盖 config.json 前刷新备份链，但不移动当前 live config。

    这样即使后续写 tmp / replace 失败，live config 仍保留在原位。
    """
    if not os.path.exists(CONFIG_PATH):
        return
    # 从大到小移位：.bak.2 → .bak.3；.bak.1 → .bak.2
    for i in range(_BACKUP_KEEP, 1, -1):
        src = CONFIG_PATH + f".bak.{i - 1}"
        dst = CONFIG_PATH + f".bak.{i}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    # 当前 config → .bak.1
    try:
        shutil.copy2(CONFIG_PATH, CONFIG_PATH + ".bak.1")
    except OSError:
        pass


def _write_atomic(data: dict) -> None:
    """Write through a private 0600 temp file, then atomically replace config."""
    parent = os.path.dirname(os.path.abspath(CONFIG_PATH)) or "."
    prefix = f".{os.path.basename(CONFIG_PATH)}."
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=parent, text=True)
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _rotate_backups()
        os.replace(tmp, CONFIG_PATH)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _current_mtime() -> float:
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0.0


def _ensure_loaded(force: bool = False) -> tuple[dict, bool]:
    """返回 (cfg, need_fire_callbacks)。callback 由调用方在锁外触发。"""
    global _cache, _mtime
    mt = _current_mtime()
    need_reload = force or _cache is None or mt != _mtime
    if need_reload:
        new_cache = _load_from_disk()
        _cache = new_cache
        _mtime = _current_mtime()
        return _cache, True
    return _cache, False


def _fire_reload_callbacks(cfg: dict) -> None:
    for cb in list(_reload_callbacks):
        try:
            cb(cfg)
        except Exception as exc:
            print(f"[config] reload callback failed: {exc}")


def get() -> dict:
    """返回当前生效配置（dict）。每次调用检查 mtime，自动热加载。"""
    with _lock:
        cfg, need_fire = _ensure_loaded()
    if need_fire:
        _fire_reload_callbacks(cfg)
    return cfg


def reload() -> dict:
    """强制重载。"""
    with _lock:
        cfg, _ = _ensure_loaded(force=True)
    _fire_reload_callbacks(cfg)
    return cfg


def save() -> None:
    """把内存中当前 cache 写回磁盘。"""
    global _mtime
    with _lock:
        if _cache is None:
            _ensure_loaded()
        _write_atomic(_cache)
        _mtime = _current_mtime()


def update(mutator, *, skip_if_unchanged: bool = False) -> dict:
    """以 mutator(cfg) 的方式原子修改 cfg 并持久化。

    `mutator` 是一个接受当前 cfg dict 的函数，可原地修改；返回值被忽略。
    mutator 只接触当前配置的深拷贝；候选配置持久化成功后才发布为共享
    cache 并触发回调。这样写盘失败不会留下仅当前进程可见的半提交状态。

    **callback 在锁外执行**：避免 callback 内访问 config 接口时被自身锁阻塞，
    也消除其它跨模块 callback 链可能产生的死锁。
    """
    global _cache, _mtime
    with _lock:
        if _cache is None:
            _ensure_loaded()
        candidate = copy.deepcopy(_cache)
        mutator(candidate)
        if skip_if_unchanged and candidate == _cache:
            return _cache
        _write_atomic(candidate)
        _mtime = _current_mtime()
        _cache = candidate
        snapshot = candidate
    _fire_reload_callbacks(snapshot)
    return snapshot


def on_reload(cb) -> None:
    """注册一个回调，每次配置重载（或 update）后被调用。

    回调接受新 cfg dict，不应抛异常。
    """
    _reload_callbacks.append(cb)


def path() -> str:
    return CONFIG_PATH
