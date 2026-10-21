"""Qoder desktop/CLI wire profile. No credentials or upstream bodies enter errors."""
from __future__ import annotations

import json
import math
import os

from ... import network
from ...safe_diagnostics import diagnose, inherit, transport_diagnostic

# The international site is Qoder's desktop client; Qoder CN is its CN CLI
# (@qodercn-ai/qoderclicn), which sends no redirect_uri and may chat on the
# device token itself when qoder.cn refuses a job token.
SITES = {
    "global": {
        "name": "Qoder", "client_id": "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa",
        "web": "https://qoder.com", "openapi": "https://openapi.qoder.sh",
        "api": "https://api3.qoder.sh", "redirect": "qoder-app://", "device_chat": False,
    },
    "cn": {
        "name": "Qoder CN", "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "web": "https://qoder.cn", "openapi": "https://openapi.qoder.com.cn",
        "api": "https://gateway.qoder.com.cn", "redirect": "", "device_chat": True,
    },
}
REALM_LABELS = {"global": "Qoder 国际 · qoder.com", "cn": "Qoder CN · qoder.cn"}

CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
MODELS_PATH = "/algo/api/v2/model/list"
USAGE_PATH = "/sash/api/v2/me/usage"
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
COSY_VERSION = "1.24.2"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class QoderError(RuntimeError):
    def __init__(self, stage: str, *, status: int = 0, code=None, kind: str = "upstream",
                 response_status: int = 0, transport_kind: str = "none", transport_exception: str = "none"):
        self.stage = stage
        self.status_code = status
        self.response_status = response_status or status
        self.transport_kind = transport_kind
        self.transport_exception = transport_exception
        inherit(self)
        self.code = str(code) if isinstance(code, (int, str)) and not isinstance(code, bool) and str(code).isascii() and len(str(code)) <= 16 else None
        self.kind = kind
        # Only a refused refresh (or PAT exchange) proves the sign-in is gone;
        # a refused chat or usage read leaves the account to its own refresh.
        self.auth_error = kind in {"refresh_rejected", "missing_refresh_token", "save_candidate_expired_relogin"}
        self.retryable = kind == "network" or status >= 500 or status == 429
        super().__init__(f"Qoder {stage}: {kind} (HTTP {status}, code {self.code})")


def text(value, field: str, *, required: bool = False, maximum: int = 256) -> str:
    if value is None:
        value = ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(int(value)) if float(value).is_integer() else str(value)
    if not isinstance(value, str):
        raise ValueError(f"Qoder {field} must be text")
    result = value.strip()
    if (required and not result) or len(result) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in result):
        raise ValueError(f"Invalid Qoder {field}")
    return result


def realm_of(account: dict) -> str:
    realm = text(account.get("realm"), "realm").lower()
    if realm not in SITES:
        raise ValueError("Qoder realm must be global or cn")
    return realm


def site_of(account: dict) -> dict:
    return SITES[realm_of(account)]


def route_label(account: dict) -> str:
    return REALM_LABELS[realm_of(account)]


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


def refresh_allowed() -> bool:
    return os.environ.get("PARROT_NO_REFRESH") != "1"


def require_refresh() -> None:
    if not refresh_allowed():
        raise QoderError("protection", kind="disabled")


def _network_disabled() -> bool:
    from ... import config
    return os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1" or bool((config.get().get("oauth") or {}).get("mockMode"))


@diagnose("qoder")
def request(url: str, *, prepare_headers=None, account_key: str = "", **kwargs):
    from . import routes
    choices = routes.chain(account_key)
    for i, route in enumerate(choices):
        try:
            with routes.bind(route):
                physical = dict(kwargs)
                if prepare_headers is not None:
                    physical["headers"] = prepare_headers()
                return _request_once(url, account_key=account_key, **physical)
        except QoderError as exc:
            safe_retry = exc.kind in {"umid_unavailable", "umid_backoff"} or (
                exc.kind == "network" and not exc.response_status and exc.transport_kind in
                {"connect", "connect_timeout", "proxy", "proxy_route", "dns", "connection_refused", "pool_timeout"})
            if not safe_retry or i + 1 == len(choices):
                raise


def _request_once(url: str, *, method: str = "GET", headers: dict | None = None, body=None, content: bytes | None = None,
            account_key: str = "", timeout: float = 20.0, stage: str = "request") -> tuple[int, object]:
    """One bounded request through Parrot's proxy context: (status, JSON or None).

    Non-2xx answers are returned, not raised: Qoder's poll, refresh and job
    token endpoints carry meaning in the status itself. Pre-request connect
    failures are the only automatic retries (network route failover).
    """
    if _network_disabled():
        raise QoderError(stage, kind="disabled")
    sent = {"Accept": "application/json", **(headers or {})}
    kwargs = {}
    if body is not None:
        sent.setdefault("Content-Type", "application/json")
        kwargs["content"] = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    elif content is not None:
        kwargs["content"] = content
    status = 0
    try:
        with network.sync_client(timeout=max(0.001, timeout), follow_redirects=False,
                                 proxy_purpose="oauth_qoder", proxy_provider="qoder",
                                 proxy_channel=f"oauth:{account_key}" if account_key else "oauth:qoder:login") as client:
            with client.stream(method, url, headers=sent, **kwargs) as response:
                status = response.status_code
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise QoderError(stage, kind="too_large", response_status=status)
    except QoderError:
        raise
    except Exception as exc:
        transport_kind, transport_exception = transport_diagnostic(exc)
        raise QoderError(stage, kind="network", response_status=status,
                         transport_kind=transport_kind, transport_exception=transport_exception) from None
    try:
        payload = json.loads(bytes(raw).decode("utf-8-sig")) if raw.strip() else None
    except (ValueError, UnicodeError):
        payload = None
    return status, payload


def openapi(account: dict, path: str, *, method: str = "GET", token: str = "", body=None,
            query: dict | None = None, account_key: str = "", timeout: float = 20.0,
            stage: str = "request", extra_headers: dict | None = None) -> tuple[int, object]:
    from urllib.parse import urlencode
    url = site_of(account)["openapi"] + path + ("?" + urlencode(query) if query else "")
    headers = dict(extra_headers or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    return request(url, method=method, headers=headers, body=body, account_key=account_key, timeout=timeout, stage=stage)


def error_code(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("code")
    if value is None or isinstance(value, bool):
        return None
    value = str(value).strip()
    return value if value.isascii() and value.isdigit() and len(value) <= 12 else None
