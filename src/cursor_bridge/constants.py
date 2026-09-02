"""Wire constants copied from schultzp2020/pi-cursor (reject-native tool path)."""

from __future__ import annotations

CURSOR_API_HOST = "api2.cursor.sh"
CURSOR_API_ORIGIN = "https://api2.cursor.sh"
AGENT_RUN_PATH = "/agent.v1.AgentService/Run"
AVAILABLE_MODELS_PATH = "/aiserver.v1.AiService/AvailableModels"
CURSOR_LOGIN_URL = "https://cursor.com/loginDeepControl"
CURSOR_POLL_URL = "https://api2.cursor.sh/auth/poll"
CURSOR_REFRESH_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
CURSOR_USAGE_URL = "https://api2.cursor.sh/auth/usage"
CURSOR_STRIPE_PROFILE_URL = "https://api2.cursor.sh/auth/full_stripe_profile"
CURSOR_WEB_PROFILE_URL = "https://cursor.com/api/auth/me"
CURSOR_PERIOD_USAGE_PATH = "/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
CURSOR_PLAN_INFO_PATH = "/aiserver.v1.DashboardService/GetPlanInfo"

CURSOR_CLIENT_VERSION = "cli-2026.01.09-231024f"
CONNECT_USER_AGENT = "connect-es/1.6.1"
CURSOR_CLIENT_TYPE = "cli"

MCP_SERVER_NAME = "pi"
MCP_TOOL_PREFIX = "mcp_pi_"
MCP_INSTRUCTIONS = (
    "This environment provides tools prefixed with mcp_pi_ (e.g. mcp_pi_read, "
    "mcp_pi_grep, mcp_pi_bash). Always prefer these mcp_pi_* tools over any "
    "built-in native tools."
)

REJECT_REASON = "Tool not available in this environment. Use the MCP tools provided instead."

HEARTBEAT_INTERVAL_S = 30.0
# First token can be slow on a large checkpoint; 30s was causing false hangs.
INACTIVITY_THINKING_S = 90.0
INACTIVITY_STREAMING_S = 30.0
INACTIVITY_FLUSHED_S = 10 * 60.0
CONNECT_TIMEOUT_S = 15.0
UNARY_RPC_TIMEOUT_S = 20.0
REQUEST_TIMEOUT_S = 300.0
# Cursor serializes SSL_read/SSL_write to avoid CPython/OpenSSL data races.  A
# short bounded recv keeps queued H2 writes and close() responsive without
# concurrent access to the same SSLSocket.
SOCKET_RECV_POLL_S = 0.1

MAX_EFFECTIVE_PROMPT_BYTES = 100_000
DEFAULT_CONTEXT_WINDOW = 200_000
DEFAULT_MAX_TOKENS = 64_000

CONNECT_END_STREAM_FLAG = 0b00000010
MAX_CONNECT_FRAME_SIZE = 32 * 1024 * 1024
