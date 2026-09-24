"""WorkBuddy CLI wire profile. No credentials or upstream bodies enter errors."""
from __future__ import annotations

import json
import math
import os
from urllib.parse import urlparse

from ... import network

PROFILE = "cli"
USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"
GLOBAL_PROFILE = "ide"
GLOBAL_IDE_BASE = "https://www.codebuddy.ai"
GLOBAL_AUTH_USER_AGENT = "IDE/2.63.2 CodeBuddy/2.63.2"
GLOBAL_CHAT_USER_AGENT = "IDE/2.108.1 CodeBuddy/2.108.1"
API_BASES = {"cn": "https://copilot.tencent.com", "global": "https://www.workbuddy.ai"}
BILLING_BASES = {"cn": "https://www.codebuddy.cn", "global": "https://www.workbuddy.ai"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class WorkBuddyError(RuntimeError):
    def __init__(self, stage: str, *, status: int = 0, code=None, kind: str = "upstream"):
        self.stage = stage
        self.status_code = status
        self.code = code if isinstance(code, int) and not isinstance(code, bool) else None
        self.kind = kind
        self.auth_error = status == 401 or self.code == 12153
        self.retryable = kind == "network" or status >= 500 or status == 429
        # Never interpolate msg, URLs, headers or transport exception text here.
        super().__init__(f"WorkBuddy {stage}: {kind} (HTTP {status}, code {self.code})")


def text(value, field: str, *, required: bool = False, maximum: int = 256) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"WorkBuddy {field} must be text")
    result = value.strip()
    if (required and not result) or len(result) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in result):
        raise ValueError(f"Invalid WorkBuddy {field}")
    return result


def domain_realm(domain: str) -> str | None:
    value = text(domain, "domain").lower()
    if not value:
        return None
    if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-." for char in value) or any(not label or label.startswith("-") or label.endswith("-") for label in value.split(".")):
        raise ValueError("WorkBuddy domain must be a hostname, not a URL")
    if value in {"copilot.tencent.com", "codebuddy.cn", "workbuddy.cn"} or value.endswith((".codebuddy.cn", ".workbuddy.cn")):
        return "cn"
    if value in {"workbuddy.ai", "codebuddy.ai"} or value.endswith((".workbuddy.ai", ".codebuddy.ai")):
        return "global"
    raise ValueError("Unrecognized WorkBuddy domain")


def realm_of(account: dict) -> str:
    declared = text(account.get("realm"), "realm").lower()
    inferred = domain_realm(account.get("domain") or "")
    if declared and declared not in API_BASES:
        raise ValueError("WorkBuddy realm must be cn or global")
    if declared and inferred and declared != inferred:
        raise ValueError("WorkBuddy realm/domain mismatch")
    if not (declared or inferred):
        raise ValueError("WorkBuddy region is required")
    profile = account.get("workbuddy_client_profile") or PROFILE
    if profile not in {PROFILE, GLOBAL_PROFILE} or (profile == GLOBAL_PROFILE and (declared or inferred) != "global"):
        raise ValueError("Unsupported WorkBuddy realm/client profile")
    return declared or inferred


def profile_of(account: dict) -> str:
    realm_of(account)
    # Existing global CLI credentials retain their original host and wire profile.
    return account.get("workbuddy_client_profile") or PROFILE


def login_profile(realm: str, client_profile: str | None = None) -> str:
    expected = GLOBAL_PROFILE if realm == "global" else PROFILE
    profile = client_profile or expected
    realm_of({"realm": realm, "workbuddy_client_profile": profile})
    if profile != expected:
        raise ValueError("Unsupported WorkBuddy login profile for region")
    return profile


def api_base_url(account: dict) -> str:
    realm = realm_of(account)
    return GLOBAL_IDE_BASE if profile_of(account) == GLOBAL_PROFILE else API_BASES[realm]


def billing_base_url(account: dict) -> str:
    realm = realm_of(account)
    return GLOBAL_IDE_BASE if profile_of(account) == GLOBAL_PROFILE else BILLING_BASES[realm]


def headers(account: dict, kind: str = "chat") -> dict[str, str]:
    realm = realm_of(account)
    ide = profile_of(account) == GLOBAL_PROFILE
    origin = billing_base_url(account)
    client_type = "IDE" if ide else "CLI"
    version = "2.108.1" if ide and kind == "chat" else "2.63.2"
    result = {"Content-Type": "application/json", "Accept": "application/json",
              "X-Requested-With": "XMLHttpRequest", "Origin": origin,
              "Referer": origin + "/", "User-Agent": USER_AGENT,
              "X-IDE-Type": client_type, "X-IDE-Name": client_type, "X-IDE-Version": version}
    if ide:
        result.update({"User-Agent": GLOBAL_CHAT_USER_AGENT if kind == "chat" else GLOBAL_AUTH_USER_AGENT,
                       "X-Domain": "www.codebuddy.ai", "X-Product": "SaaS"})
        if kind == "chat":
            result["x-codebuddy-request"] = "1"
    elif kind == "chat":
        result["X-Agent-Intent"] = "craft"
    enterprise = text(account.get("enterprise_id"), "enterprise_id")
    uid = text(account.get("uid"), "uid")
    domain = text(account.get("domain"), "domain")
    if kind == "refresh":
        result.update({"X-Refresh-Token": text(account.get("refresh_token"), "refresh_token", required=True, maximum=32768),
                       "X-Auth-Refresh-Source": "plugin" if ide else "workbuddy"})
        if enterprise:
            result["X-Enterprise-Id"] = enterprise
        return result
    if kind == "anonymous":
        if ide:
            result.update({name: "true" for name in (
                "X-No-Authorization", "X-No-User-Id", "X-No-Enterprise-Id", "X-No-Department-Info",
            )})
        return result
    result["Authorization"] = "Bearer " + text(account.get("access_token"), "access_token", required=True, maximum=32768)
    for name, value, missing in (("X-User-Id", uid, "X-No-User-Id"),
                                 ("X-Enterprise-Id", enterprise, "X-No-Enterprise-Id"),
                                 ("X-Domain", domain, "X-No-Department-Info")):
        if value:
            result[name] = value
        elif kind == "chat" and not (ide and name == "X-Domain"):
            result[missing] = "true" if ide else "1"
    if kind == "chat":
        result["X-Product"] = "SaaS"
    if kind in {"billing", "enterprise"} and enterprise:
        result["X-Tenant-Id"] = enterprise
    if kind == "enterprise":
        result["X-Client-Platform"] = "web"
    return result


def safe_auth_url(value, *, realm: str = "cn") -> str:
    value = text(value, "authUrl", required=True, maximum=8192)
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if realm == "cn":
        allowed = host in {"tencent.com", "codebuddy.cn", "workbuddy.cn"} or host.endswith((".tencent.com", ".codebuddy.cn", ".workbuddy.cn"))
    elif realm == "global":
        allowed = host in {"codebuddy.ai", "workbuddy.ai"} or host.endswith((".codebuddy.ai", ".workbuddy.ai"))
    else:
        allowed = False
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or not allowed:
        raise ValueError("Unrecognized WorkBuddy authorization URL")
    return value


def number(value) -> float | int | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or result < 0 or result > 2**53:
        return None
    return int(result) if result.is_integer() else result


def effects_allowed() -> bool:
    """Compatibility capability: activities are not controlled by token refresh.

    Region, account state, explicit confirmation and deduplication still decide
    whether an individual check-in/trial action can be submitted.
    """
    return True


def refresh_allowed() -> bool:
    return os.environ.get("PARROT_NO_REFRESH") != "1"


def require_refresh() -> None:
    if not refresh_allowed():
        raise WorkBuddyError("protection", kind="disabled")


def request(account: dict, path: str, *, method: str = "POST", body=None,
            kind: str = "billing", account_key: str = "", timeout: float = 15.0,
            cookies: dict | None = None, pending: bool = False, allow_empty: bool = False):
    """Single bounded JSON request through Parrot's configured proxy context.

    network's route failover retries only pre-request ConnectError/ConnectTimeout;
    read/write timeouts and application failures are never automatically replayed.
    Login cookies are short-lived flow-owned data, never account configuration.
    """
    from ... import config
    if os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1" or (config.get().get("oauth") or {}).get("mockMode"):
        raise WorkBuddyError("network", kind="disabled")
    if kind == "refresh":
        require_refresh()
    base = billing_base_url(account) if kind in {"billing", "enterprise"} else api_base_url(account)
    kwargs = {} if body is None else {"json": body}
    try:
        with network.sync_client(timeout=max(0.001, timeout), follow_redirects=False,
                                 proxy_purpose="oauth_workbuddy",
                                 proxy_channel=f"oauth:{account_key}" if account_key else "oauth:workbuddy:login",
                                 cookies=cookies) as client:
            with client.stream(method, base + path, headers=headers(account, kind), **kwargs) as response:
                status = response.status_code
                if not 200 <= status < 300:
                    raise WorkBuddyError("request", status=status)
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise WorkBuddyError("response", kind="too_large")
                if cookies is not None:
                    cookies.update(dict(client.cookies.items()))
    except WorkBuddyError:
        raise
    except Exception:
        raise WorkBuddyError("request", kind="network") from None
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError):
        raise WorkBuddyError("response", kind="invalid_json") from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("code"), int) or isinstance(envelope.get("code"), bool):
        raise WorkBuddyError("response", kind="invalid_envelope")
    if envelope["code"] != 0:
        # "login ing" is explicitly documented by both reference implementations.
        # Do not turn arbitrary 4xx/business errors into endless pending.
        if pending and (envelope["code"] == 11217 or str(envelope.get("msg") or "").strip().lower() == "login ing"):
            return None
        raise WorkBuddyError("request", code=envelope["code"])
    data = envelope.get("data")
    if pending and data is None:
        return None
    if allow_empty and data is None:
        return {}
    if not isinstance(data, dict):
        raise WorkBuddyError("response", kind="invalid_data")
    return data
