"""Standalone Python Cursor client with OpenAI-style tool_calls."""

from .auth import (
    CursorAuthParams,
    CursorAuthPending,
    CursorTokens,
    generate_auth_params,
    poll_cursor_auth,
    poll_cursor_auth_once,
    refresh_cursor_token,
)
from .client import CursorClient
from .errors import (
    CursorAuthError,
    CursorError,
    CursorOverloadError,
    CursorRateLimitError,
    CursorTimeoutError,
    CursorToolActivityError,
)
from .models import CursorModel
from .usage import CursorUsage

__all__ = [
    "CursorAuthError",
    "CursorAuthParams",
    "CursorAuthPending",
    "CursorClient",
    "CursorError",
    "CursorModel",
    "CursorOverloadError",
    "CursorRateLimitError",
    "CursorTimeoutError",
    "CursorTokens",
    "CursorToolActivityError",
    "CursorUsage",
    "generate_auth_params",
    "poll_cursor_auth",
    "poll_cursor_auth_once",
    "refresh_cursor_token",
]

__version__ = "0.1.1"
