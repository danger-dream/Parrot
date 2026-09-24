"""ZCode Coding Plan wire identities and bounded, credential-safe management IO."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import time
from urllib.parse import quote

import httpx
from ...proxy.connector import ProxyConnectError, UpstreamConnectError

from ... import network
from .diagnostics import RequestTrace, error_facts

VERSION = "3.14.3"
PLATFORM_ORIGIN = "https://zcode.z.ai"
BIZ_ORIGINS = {"bigmodel": "https://bigmodel.cn", "zai": "https://api.z.ai"}
MODEL_ORIGINS = {"bigmodel": "https://open.bigmodel.cn", "zai": "https://api.z.ai"}
MAX_BYTES = 10 * 1024 * 1024
READ_TOTAL_TIMEOUT = 180
READ_TIMEOUT = httpx.Timeout(connect=20, read=60, write=20, pool=20)


class ZhipuError(RuntimeError):
    def __init__(self, stage, kind="upstream", *, status=0, code=None, fail_open=False, timeout_phase="",
                 request_not_sent=False, network_phase="", target_host="", proxy_route="", fallback_used=False):
        self.stage, self.kind, self.status_code = stage, kind, status
        self.timeout_phase = timeout_phase
        self.request_not_sent, self.network_phase = request_not_sent, network_phase
        self.target_host, self.proxy_route, self.fallback_used = target_host, proxy_route, fallback_used
        self.code = (code if type(code) is int else
                     int(code) if isinstance(code, str) and code.isascii() and code.isdecimal() and len(code) <= 12 else None)
        self.fail_open_eligible = fail_open
        self.auth_error = status in (401, 403)
        self.retryable = kind in {"network", "timeout"} or status in (408, 429) or status >= 500
        super().__init__(f"Zhipu {stage}: {kind} (HTTP {status}, code {self.code})")


def text(value, field, *, required=False, maximum=32768):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"Invalid Zhipu {field}")
    value = value.strip()
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"Invalid Zhipu {field}")
    return value


def site_of(account):
    site = account.get("site")
    if site not in BIZ_ORIGINS:
        raise ValueError("Zhipu site must be bigmodel or zai")
    return site


def identity(account):
    # Escaping prevents user/org/project separators from aliasing another scope.
    parts = [site_of(account), account.get("credential_mode"), account.get("subject"),
             account.get("organization_id") or "", account.get("project_id") or ""]
    return ":".join(quote(str(v or ""), safe="") for v in parts)


def fingerprint(account):
    values = [identity(account), account.get("generationId"), account.get("model_key"),
              account.get("access_token"), account.get("zcode_token")]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def identity_headers():
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    return {"User-Agent": f"ZCode/{VERSION}", "HTTP-Referer": PLATFORM_ORIGIN,
            "X-Title": "Z Code@cli", "X-ZCode-Agent": "glm", "X-ZCode-App-Version": VERSION,
            "X-Release-Channel": "production", "X-Client-Language": "zh-CN",
            "X-Client-Timezone": "Asia/Shanghai", "X-Platform": "linux-" + arch,
            "X-Os-Category": "linux", "X-Os-Version": platform.release()}


def model_headers():
    # Observed 3.14.3 main-request profile. Management/signing endpoints do not
    # pass through AI SDK provider-utils and keep the plain identity UA.
    return {**identity_headers(), "User-Agent":
            f"ZCode/{VERSION} ai-sdk/provider-utils/4.0.27 runtime/node.js/24"}


def scope_headers(account):
    result = {"Bigmodel-Target-Type": "TEAM" if account.get("plan_scope") == "team" else "PERSONAL"}
    if account.get("plan_scope") == "team":
        result.update({"bigmodel-organization": account["organization_id"],
                       "bigmodel-project": account["project_id"]})
    return result


def biz_headers(account):
    if account.get("credential_mode") != "oauth":
        raise ZhipuError("management", "oauth_required")
    token = text(account.get("access_token"), "access_token", required=True)
    if site_of(account) == "bigmodel" and token == account.get("zcode_token"):
        raise ZhipuError("management", "oauth_required")
    result = {"Authorization": ("Bearer " if site_of(account) == "zai" else "") + token}
    if account.get("plan_scope") == "team":
        result.update(scope_headers(account))
    return result


def reset_headers(account):
    biz_headers(account)
    return {**scope_headers(account), "Authorization": "Bearer " + text(account.get("zcode_token"), "zcode_token", required=True),
            "X-Bigmodel-Authorization": account["access_token"]}


def require_network():
    from ... import config
    if os.environ.get("DISABLE_OAUTH_NETWORK_CALLS") == "1" or (config.get().get("oauth") or {}).get("mockMode"):
        raise ZhipuError("network", "disabled")


def request(url, *, headers=None, method="GET", body=None, account_key="", timeout=None, envelope=True,
            read_attempts=1, stage=None):
    try:
        return _request(url, headers=headers, method=method, body=body, account_key=account_key,
                        timeout=timeout, envelope=envelope, read_attempts=read_attempts, stage=stage)
    except ZhipuError as exc:
        if stage:
            exc.stage = stage
        print("[zhipu-request] " + json.dumps({"stage": exc.stage, "kind": exc.kind,
            "http_status": exc.status_code, "code": exc.code, "timeout_phase": exc.timeout_phase,
            **error_facts(exc)}, ensure_ascii=False), flush=True)
        raise


def _request(url, *, headers, method, body, account_key, timeout, envelope, read_attempts, stage):
    # Only explicitly opted-in reads use the longer timeout/retry policy.
    # Writes, CLI polling and catalog deadlines retain their existing owner.
    if method.upper() != "GET" or read_attempts <= 1:
        return _request_once(url, headers=headers, method=method, body=body,
                             account_key=account_key, timeout=20 if timeout is None else timeout, envelope=envelope)

    trace = RequestTrace(url)
    async def read():
        try:
            return await asyncio.wait_for(_read_attempts(url, headers=headers, account_key=account_key,
                timeout=READ_TIMEOUT if timeout is None else timeout, envelope=envelope,
                attempts=min(3, read_attempts), stage=stage, trace=trace), timeout=READ_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            # Cancels and closes the active request, including proxy fallback,
            # body streaming and retry backoff; no abandoned network worker.
            raise ZhipuError(stage or "request", "timeout", timeout_phase="total", **trace.facts()) from None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(read())
    # Keep the synchronous API usable by existing callers under an event loop.
    # This thread is joined; the coroutine owns cancellation and its client.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="zhipu-read") as executor:
        return executor.submit(lambda: asyncio.run(read())).result()


async def _read_attempts(url, *, headers, account_key, timeout, envelope, attempts, stage, trace):
    for attempt in range(attempts):
        trace.routes.clear()
        trace.phase, trace.send_started = "", False
        try:
            return await _read_once(url, headers=headers, account_key=account_key,
                                    timeout=timeout, envelope=envelope, trace=trace)
        except ZhipuError as exc:
            if stage:
                exc.stage = stage
            if not exc.retryable or attempt + 1 == attempts:
                raise
            delay = max(0.5 * 2 ** attempt, getattr(exc, "retry_after", 0))
            if delay > 5:
                raise
            print(f"[zhipu] {stage or 'read'} retry={attempt + 2}/{attempts} kind={exc.kind} phase={exc.timeout_phase or '-'} http={exc.status_code}")
            await asyncio.sleep(delay)


def _timeout_phase(exc):
    return next((name for cls, name in ((httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool")) if isinstance(exc, cls)), "")


def _check_status(response):
    if not 200 <= response.status_code < 300:
        error = ZhipuError("request", status=response.status_code)
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isascii() and retry_after.isdecimal():
            error.retry_after = min(int(retry_after[:8]), 86400)
        raise error


def _decode_response(raw, status, envelope):
    value = json.loads(raw)
    if not envelope:
        return value
    if not isinstance(value, dict) or value.get("success") is False or str(value.get("code", 0)) not in {"0", "200"}:
        raise ZhipuError("response", "business", status=status,
                         code=value.get("code") if isinstance(value, dict) else None)
    return value.get("data")


async def _read_once(url, *, headers, account_key, timeout, envelope, trace=None):
    require_network()
    trace = trace or RequestTrace(url)
    try:
        async with network.async_client(timeout=timeout, follow_redirects=False, proxy_purpose="oauth_zhipu",
                proxy_channel="oauth:" + account_key if account_key else "oauth:zhipu:login") as client:
            async with client.stream("GET", url, headers=headers or {}, extensions=trace.extensions(asynchronous=True)) as response:
                _check_status(response)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ZhipuError("response", "too_large")
        return _decode_response(raw, response.status_code, envelope)
    except ZhipuError as exc:
        for name, value in trace.facts().items():
            setattr(exc, name, value)
        raise
    except (ValueError, UnicodeError):
        raise ZhipuError("response", "invalid_json", **trace.facts()) from None
    except httpx.TimeoutException as exc:
        raise ZhipuError("request", "timeout", timeout_phase=_timeout_phase(exc), **trace.facts(exc)) from None
    except (httpx.TransportError, ProxyConnectError, UpstreamConnectError, OSError) as exc:
        raise ZhipuError("request", "network", **trace.facts(exc)) from None


def _request_once(url, *, headers, method, body, account_key, timeout, envelope):
    require_network()
    trace = RequestTrace(url)
    try:
        with network.sync_client(timeout=timeout, follow_redirects=False, proxy_purpose="oauth_zhipu",
                                 proxy_channel="oauth:" + account_key if account_key else "oauth:zhipu:login") as client:
            with client.stream(method, url, headers=headers or {}, extensions=trace.extensions(),
                               **({"json": body} if body is not None else {})) as response:
                _check_status(response)
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ZhipuError("response", "too_large")
        return _decode_response(raw, response.status_code, envelope)
    except ZhipuError as exc:
        for name, value in trace.facts().items():
            setattr(exc, name, value)
        raise
    except (ValueError, UnicodeError):
        raise ZhipuError("response", "invalid_json", **trace.facts()) from None
    except httpx.TimeoutException as exc:
        raise ZhipuError("request", "timeout", timeout_phase=_timeout_phase(exc), **trace.facts(exc)) from None
    except (httpx.TransportError, ProxyConnectError, UpstreamConnectError, OSError) as exc:
        raise ZhipuError("request", "network", **trace.facts(exc)) from None
