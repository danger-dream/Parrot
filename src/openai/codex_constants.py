"""Codex CLI 指纹常量 — 统一管理。

所有需要伪装 Codex CLI 指纹的模块统一从这里引用，
修改版本号只需改这一个文件。

版本号来源：@openai/codex npm latest 0.144.0；Codex 模型目录中
GPT-5.6 系列 minimal_client_version=0.144.0。
UA 格式来源：codex-rs/login/src/auth/default_client.rs get_codex_user_agent()：
  "{originator}/{version} ({os} {os_version}; {arch}) {terminal}/{terminal_version}"
"""

# Codex CLI 版本号（与 @openai/codex latest 对齐）
CODEX_CLI_VERSION = "0.144.0"

# Codex originator（codex-rs/login/src/auth/default_client.rs DEFAULT_ORIGINATOR）
CODEX_ORIGINATOR = "codex_cli_rs"

# 完整 User-Agent（模拟 macOS arm64 + iTerm 环境）
CODEX_CLI_USER_AGENT = (
    f"{CODEX_ORIGINATOR}/{CODEX_CLI_VERSION}"
    " (Mac OS 26.5.0; arm64)"
    " iTerm.app/3.6.10"
)

# Responses WebSocket beta header（codex-rs/core/src/client.rs）
RESPONSES_WEBSOCKETS_BETA = "responses_websockets=2026-02-06"

# Responses Lite 标记（官方 models.json: use_responses_lite=true）。
# 保留 gpt-5.6-* 前缀以兼容该系列后续变体；非该前缀的 Lite 模型必须显式登记。
CODEX_RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"
CODEX_RESPONSES_LITE_WS_METADATA_KEY = "ws_request_header_x_openai_internal_codex_responses_lite"
CODEX_RESPONSES_LITE_MODEL_PREFIXES = ("gpt-5.6-",)
CODEX_RESPONSES_LITE_MODELS = frozenset({
    "gpt-daybreak-blue-latest",
    "gpt-daybreak-red-latest",
    "codex-auto-review",
})


def codex_model_uses_responses_lite(model: str | None) -> bool:
    """Return whether official Codex marks this model as Responses Lite."""
    m = str(model or "").strip().lower()
    return m in CODEX_RESPONSES_LITE_MODELS or any(
        m.startswith(prefix) for prefix in CODEX_RESPONSES_LITE_MODEL_PREFIXES
    )
