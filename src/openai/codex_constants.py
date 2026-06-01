"""Codex CLI 指纹常量 — 统一管理。

所有需要伪装 Codex CLI 指纹的模块统一从这里引用，
修改版本号只需改这一个文件。

版本号来源：Codex 源码 tag rust-v0.135.0-alpha.2（当前线上稳定版）。
UA 格式来源：codex-rs/login/src/auth/default_client.rs get_codex_user_agent()：
  "{originator}/{version} ({os} {os_version}; {arch}) {terminal}/{terminal_version}"
"""

# Codex CLI 版本号（与 CLIProxyAPI 对齐）
CODEX_CLI_VERSION = "0.135.0"

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
