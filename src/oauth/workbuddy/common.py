"""WorkBuddy CLI wire profile. No credentials or upstream bodies enter errors."""
from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import uuid
from functools import lru_cache
from urllib.parse import urlparse

from ... import network
from ...safe_diagnostics import diagnose, inherit, transport_diagnostic

PROFILE = "cli"
CLI_VERSION = "2.164.0"
USER_AGENT = f"CLI/{CLI_VERSION} CodeBuddy/{CLI_VERSION}"
LEGACY_USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"
GLOBAL_PROFILE = "ide"
WORKBUDDY_CLI_PROFILE = "workbuddy-cli"
WORKBUDDY_CLI_VERSION = "2.168.2"
GLOBAL_IDE_BASE = "https://www.codebuddy.ai"
GLOBAL_CATALOG_VERSION = "4.12.1"
GLOBAL_AUTH_USER_AGENT = f"CodeBuddyIDE/{GLOBAL_CATALOG_VERSION} CodeBuddy/{GLOBAL_CATALOG_VERSION}"
GLOBAL_CHAT_USER_AGENT = GLOBAL_AUTH_USER_AGENT
API_BASES = {"cn": "https://copilot.tencent.com", "global": "https://www.workbuddy.ai"}
BILLING_BASES = {"cn": "https://www.codebuddy.cn", "global": "https://www.workbuddy.ai"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class WorkBuddyError(RuntimeError):
    def __init__(self, stage: str, *, status: int = 0, code=None, kind: str = "upstream",
                 response_status: int = 0, transport_kind: str = "none", transport_exception: str = "none"):
        self.stage = stage
        self.status_code = status
        # Diagnostic-only metadata: do not change retry/auth semantics or retain
        # raw transport messages, request objects, or response bodies here.
        self.response_status = response_status or status
        self.transport_kind = transport_kind
        self.transport_exception = transport_exception
        inherit(self)
        self.code = code if isinstance(code, int) and not isinstance(code, bool) else None
        self.kind = kind
        # Locally losing a rotated credential also requires reauthorization;
        # keep status/code unchanged so it is never presented as upstream 401.
        self.auth_error = status == 401 or self.code == 12153 or kind == "save_candidate_expired_relogin"
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
    if profile not in {PROFILE, GLOBAL_PROFILE, WORKBUDDY_CLI_PROFILE} or (profile in {GLOBAL_PROFILE, WORKBUDDY_CLI_PROFILE} and (declared or inferred) != "global"):
        raise ValueError("Unsupported WorkBuddy realm/client profile")
    return declared or inferred


def profile_of(account: dict) -> str:
    realm_of(account)
    # Existing global CLI credentials retain their original host and wire profile.
    return account.get("workbuddy_client_profile") or PROFILE


def login_profile(realm: str, client_profile: str | None = None) -> str:
    profile = client_profile or (WORKBUDDY_CLI_PROFILE if realm == "global" else PROFILE)
    realm_of({"realm": realm, "workbuddy_client_profile": profile})
    if realm == "global" and profile not in {WORKBUDDY_CLI_PROFILE, GLOBAL_PROFILE}:
        raise ValueError("Unsupported WorkBuddy login profile for region")
    return profile


def route_label(account: dict) -> str:
    profile = profile_of(account)
    if profile == WORKBUDDY_CLI_PROFILE:
        return "WorkBuddy 国际 CLI · workbuddy.ai"
    if profile == GLOBAL_PROFILE:
        return "CodeBuddy IDE · codebuddy.ai"
    return "Legacy Global CLI · workbuddy.ai" if is_legacy(account) else "WorkBuddy 中国 CLI"


def api_base_url(account: dict) -> str:
    realm = realm_of(account)
    return GLOBAL_IDE_BASE if profile_of(account) == GLOBAL_PROFILE else API_BASES[realm]


def billing_base_url(account: dict) -> str:
    realm = realm_of(account)
    return GLOBAL_IDE_BASE if profile_of(account) == GLOBAL_PROFILE else BILLING_BASES[realm]


def is_legacy(account: dict) -> bool:
    return realm_of(account) == "global" and profile_of(account) == PROFILE


@lru_cache(maxsize=1)
def _workbuddy_cli_sdk_headers() -> dict[str, str]:
    """Pin the emulated CLI deployment to this host's installed Node runtime.

    Only process metadata is evaluated, never the official bundle. Like the
    SDK's getPlatformHeaders cache this is stable for the gateway process.
    Missing/unreadable Node is an explicit local prerequisite error, not a
    guessed runtime version. Other profiles never use this probe.
    """
    try:
        raw = subprocess.check_output(["node", "-p",
            "JSON.stringify([process.version,process.platform,process.arch])"],
            text=True, stderr=subprocess.DEVNULL, timeout=2,
            env={**os.environ, "NODE_OPTIONS": ""})
        version, system, arch = json.loads(raw)
        if not all(isinstance(value, str) for value in (version, system, arch)) or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[A-Za-z0-9.+-]+)?", version):
            raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise WorkBuddyError("chat", kind="cli_runtime_unavailable") from None
    platform_name = ("iOS" if "ios" in system.lower() else {
        "android": "Android", "darwin": "MacOS", "win32": "Windows", "freebsd": "FreeBSD",
        "openbsd": "OpenBSD", "linux": "Linux"}.get(system.lower(), "Other:" + system if system else "Unknown"))
    architecture = {"x32": "x32", "x86_64": "x64", "x64": "x64", "arm": "arm",
                    "aarch64": "arm64", "arm64": "arm64"}.get(arch, "other:" + arch if arch else "unknown")
    # Official 2.168.2 embeds openai-node 6.25.0 and sets maxRetries:0.
    # OAuth recovery is a new outer call, not an SDK retry. No per-request SDK
    # timeout exists in Parrot; do not mislabel connect/idle/total timeouts.
    return {"x-stainless-lang": "js", "x-stainless-package-version": "6.25.0",
            "x-stainless-runtime": "node", "x-stainless-runtime-version": version,
            "x-stainless-os": platform_name, "x-stainless-arch": architecture,
            "x-stainless-retry-count": "0"}


def headers(account: dict, kind: str = "chat") -> dict[str, str]:
    if is_legacy(account):
        return _legacy_headers(account, kind)
    ide = profile_of(account) == GLOBAL_PROFILE
    client_type = "CodeBuddyIDE" if ide else "CLI"
    workbuddy_cli = profile_of(account) == WORKBUDDY_CLI_PROFILE
    version = GLOBAL_CATALOG_VERSION if ide else WORKBUDDY_CLI_VERSION if workbuddy_cli else CLI_VERSION
    result = {"Content-Type": "application/json", "Accept": "application/json",
              "User-Agent": GLOBAL_AUTH_USER_AGENT if ide else f"CLI/{version} CodeBuddy/{version}",
              "X-IDE-Type": client_type, "X-IDE-Name": client_type, "X-IDE-Version": version,
              "X-Product": "SaaS"}
    if ide:
        result.update({"X-Product-Version": version, "X-Env-ID": "production",
                       "X-Request-Trace-Id": uuid.uuid4().hex})
    if kind in {"billing", "enterprise"}:
        origin = billing_base_url(account)
        result.update({"Origin": origin, "Referer": origin + "/", "X-Requested-With": "XMLHttpRequest"})
    if kind in {"anonymous", "login_state", "login_token"}:
        result.update({name: "true" for name in (
            "X-No-Authorization", "X-No-User-Id", "X-No-Enterprise-Id", "X-No-Department-Info",
        )})
        if kind != "login_token":
            result["X-Domain"] = urlparse(api_base_url(account)).hostname
        return result
    domain = text(account.get("domain"), "domain") or ("www.workbuddy.ai" if workbuddy_cli else "")
    if domain:
        result["X-Domain"] = domain
    if kind == "refresh":
        result.update({"X-Refresh-Token": text(account.get("refresh_token"), "refresh_token", required=True, maximum=32768),
                       "X-Auth-Refresh-Source": "plugin"})
    else:
        result["Authorization"] = "Bearer " + text(account.get("access_token"), "access_token", required=True, maximum=32768)
    uid = text(account.get("uid"), "uid")
    enterprise = text(account.get("enterprise_id"), "enterprise_id")
    if kind == "login_account" or (kind == "account" and not uid):
        result.update({name: "true" for name in ("X-No-User-Id", "X-No-Enterprise-Id", "X-No-Department-Info")})
    else:
        if uid:
            result["X-User-Id"] = uid
        if enterprise:
            result.update({"X-Enterprise-Id": enterprise, "X-Tenant-Id": enterprise})
        department = text(account.get("department_full_name"), "department_full_name", maximum=2048)
        if department:
            result["X-Department-Info"] = department
    if kind == "models" and not ide:
        result["Connection"] = "close"
    if kind == "chat":
        result.update({"X-Agent-Intent": "craft", "X-Private-Data": "true"})
        if workbuddy_cli:
            result.update({"X-Agent-Type": "main", "X-Agent-Runtime": "other"})
            result.update(_workbuddy_cli_sdk_headers())
        if not ide:
            method = text(account.get("auth_method"), "auth_method")
            source = text(account.get("id_source"), "id_source")
            if method:
                result["X-Auth-Method"] = method
            if source:
                result["X-Id-Source"] = source
            if uid and (enterprise or method or source):
                info = {key: value for key, value in (("uin", uid), ("owner_uin", enterprise),
                        ("id_source", source), ("token_source", method)) if value}
                result["X-Userinfo"] = base64.b64encode(json.dumps(info, separators=(",", ":")).encode()).decode()
    if kind == "enterprise":
        result["X-Client-Platform"] = "web"
    return result


def _legacy_headers(account: dict, kind: str = "chat") -> dict[str, str]:
    realm = realm_of(account)
    ide = profile_of(account) == GLOBAL_PROFILE
    origin = billing_base_url(account)
    client_type = "IDE" if ide else "CLI"
    version = "2.108.1" if ide and kind == "chat" else "2.63.2"
    result = {"Content-Type": "application/json", "Accept": "application/json",
              "X-Requested-With": "XMLHttpRequest", "Origin": origin,
              "Referer": origin + "/", "User-Agent": LEGACY_USER_AGENT,
              "X-IDE-Type": client_type, "X-IDE-Name": client_type, "X-IDE-Version": version}
    if ide:
        result.update({"User-Agent": GLOBAL_CHAT_USER_AGENT if kind == "chat" else GLOBAL_AUTH_USER_AGENT,
                       "X-Domain": "www.codebuddy.ai", "X-Product": "SaaS"})
        if kind == "models":
            result.update({"User-Agent": f"CodeBuddyIDE/{GLOBAL_CATALOG_VERSION}",
                           "X-IDE-Type": "CodeBuddyIDE", "X-IDE-Name": "CodeBuddyIDE",
                           "X-IDE-Version": GLOBAL_CATALOG_VERSION})
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


@diagnose("workbuddy")
def request(account: dict, path: str, *, method: str = "POST", body=None,
            kind: str = "billing", account_key: str = "", timeout: float = 15.0,
            cookies: dict | None = None, pending: bool = False, allow_empty: bool = False,
            allow_list: bool = False):
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
    status = 0
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
                        raise WorkBuddyError("response", kind="too_large", response_status=status)
                if cookies is not None:
                    cookies.update(dict(client.cookies.items()))
    except WorkBuddyError:
        raise
    except Exception as exc:
        transport_kind, transport_exception = transport_diagnostic(exc)
        raise WorkBuddyError("request", kind="network", response_status=status,
                             transport_kind=transport_kind, transport_exception=transport_exception) from None
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError):
        raise WorkBuddyError("response", kind="invalid_json", response_status=status) from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("code"), int) or isinstance(envelope.get("code"), bool):
        raise WorkBuddyError("response", kind="invalid_envelope", response_status=status)
    if envelope["code"] != 0:
        # "login ing" is explicitly documented by both reference implementations.
        # Do not turn arbitrary 4xx/business errors into endless pending.
        if pending and (envelope["code"] == 11217 or str(envelope.get("msg") or "").strip().lower() == "login ing"):
            return None
        raise WorkBuddyError("request", code=envelope["code"], response_status=status)
    data = envelope.get("data")
    if pending and data is None:
        return None
    if allow_empty and data is None:
        return {}
    if not isinstance(data, dict) and not (allow_list and isinstance(data, list)):
        raise WorkBuddyError("response", kind="invalid_data", response_status=status)
    return data
