"""Bounded HTTPS image retrieval for Antigravity Chat/Responses inputs.

The downloader deliberately does not trust redirects, DNS answers, MIME headers, or
Content-Length.  Every hop is resolved and checked before any request is sent and
the streamed body is capped independently.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import ipaddress
import socket
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

MAX_IMAGE_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 3
ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


class RemoteImageError(ValueError):
    """Invalid/unsafe image input or response content."""


class RemoteImageDownloadError(RemoteImageError):
    """External image service/DNS failure, not a model account failure."""


def _safe_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return bool(addr.is_global) and not any((
        addr.is_private, addr.is_loopback, addr.is_link_local, addr.is_multicast,
        addr.is_reserved, addr.is_unspecified,
    ))


async def _validate_url(url: str, resolver: Callable[..., Any] | None = None) -> tuple[str, int, tuple[str, ...], str]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise RemoteImageError("input_image remote URL has an invalid authority") from exc
    if parsed.scheme.lower() != "https":
        raise RemoteImageError("input_image remote URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise RemoteImageError("input_image remote URL has an invalid authority")
    resolve = resolver or socket.getaddrinfo
    try:
        rows = await asyncio.to_thread(resolve, parsed.hostname, port, 0, socket.SOCK_STREAM)
    except Exception as exc:
        raise RemoteImageDownloadError("input_image hostname could not be resolved") from exc
    addresses = {str(row[4][0]) for row in rows if row and len(row) >= 5 and row[4]}
    if not addresses or not all(_safe_ip(value) for value in addresses):
        raise RemoteImageError("input_image URL resolves to a non-public address")
    ordered = tuple(sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value)))
    authority = parsed.netloc
    return parsed.hostname, port, ordered, authority


def _pinned_url(original: str, address: str, port: int) -> str:
    parsed = urlsplit(original)
    literal = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
    netloc = literal if port == 443 else f"{literal}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))


def _magic_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


async def download_https_image(
    url: str, *, max_bytes: int = MAX_IMAGE_BYTES, max_redirects: int = MAX_REDIRECTS,
    resolver: Callable[..., Any] | None = None, client: Any | None = None,
) -> tuple[bytes, str]:
    current = str(url).strip()
    owned = client is None
    if owned:
        # Never inherit environment/application proxies here: a proxy receiving
        # the original hostname would perform an unvalidated DNS lookup outside
        # this SSRF boundary.  The URL host below is the validated IP itself.
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=8.0, read=20.0),
            follow_redirects=False,
            trust_env=False,
        )
    cm = client if owned else _NullAsyncContext(client)
    async with cm as active:
        for hop in range(max_redirects + 1):
            hostname, port, addresses, authority = await _validate_url(current, resolver)
            # A fresh hop is resolved exactly once.  Connect to one member of that
            # validated all-public answer set while preserving HTTP authority and
            # TLS identity through Host and httpcore's sni_hostname extension.
            pinned = _pinned_url(current, addresses[0], port)
            async with active.stream(
                "GET",
                pinned,
                headers={"Accept": ", ".join(sorted(ALLOWED_MIME)), "Host": authority},
                extensions={"sni_hostname": hostname},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop >= max_redirects:
                        raise RemoteImageError("input_image redirect limit exceeded")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise RemoteImageDownloadError(f"input_image download returned HTTP {response.status_code}")
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime not in ALLOWED_MIME:
                    raise RemoteImageError(f"input_image Content-Type {mime or 'missing'} is not supported")
                length = response.headers.get("content-length")
                if length:
                    try:
                        declared = int(length)
                    except ValueError as exc:
                        raise RemoteImageError("input_image has invalid Content-Length") from exc
                    if declared > max_bytes:
                        raise RemoteImageError(f"input_image exceeds {max_bytes} bytes")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise RemoteImageError(f"input_image exceeds {max_bytes} bytes")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                detected = _magic_mime(raw)
                if detected is None or detected != mime:
                    raise RemoteImageError("input_image MIME does not match image content")
                return raw, mime
    raise RemoteImageError("input_image redirect limit exceeded")


class _NullAsyncContext:
    def __init__(self, value: Any): self.value = value
    async def __aenter__(self) -> Any: return self.value
    async def __aexit__(self, *_: Any) -> None: return None


async def inline_remote_images(body: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with Chat/Responses HTTPS image URLs replaced by data URLs."""
    out = copy.deepcopy(body)

    # Images are protocol content parts, never arbitrary similarly shaped JSON
    # in tools/schema/examples, metadata, tool arguments or tool result bodies.
    for container in ("messages", "input"):
        messages = out.get(container)
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("type", "message") != "message":
                continue
            if message.get("role", "user") not in {"system", "developer", "user", "assistant"}:
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {"input_image", "image_url"}:
                    continue
                holder = part.get("image_url")
                url = holder.get("url") if isinstance(holder, dict) else holder
                if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
                    raw, mime = await download_https_image(url)
                    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
                    if isinstance(holder, dict):
                        holder["url"] = data_url
                    else:
                        part["image_url"] = data_url
    return out


async def prepare_antigravity_images(payload: dict, *, tool_outputs: bool = False) -> dict:
    """AG boundary classification; the shared downloader keeps its safety policy.

    Input failures are 400; external fetch failures are 502. Neither is evidence
    about a model account's health, and retries on another model cannot fix them.
    """
    from ..openai.transform.guard import GuardError
    try:
        return await (inline_tool_output_images(payload) if tool_outputs else inline_remote_images(payload))
    except (RemoteImageDownloadError, httpx.RequestError) as exc:
        raise GuardError(502, "api_error", "Remote input image download failed", param="input", scope="request") from exc
    except RemoteImageError as exc:
        raise GuardError(400, "invalid_request_error", str(exc), param="input", scope="request") from exc


def validate_inline_tool_image(inline: dict) -> None:
    """Validate new tool-image support without altering ordinary user images."""
    from ..openai.transform.guard import GuardError
    mime, data = inline.get("mimeType"), inline.get("data")
    if mime not in ALLOWED_MIME or not isinstance(data, str) or not data:
        raise GuardError(400, "invalid_request_error", "Unsupported Antigravity tool image MIME or empty data", scope="candidate")
    if len(data) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise GuardError(400, "invalid_request_error", "Antigravity tool image exceeds image size limit", scope="candidate")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise GuardError(400, "invalid_request_error", "Invalid base64 tool image", scope="candidate") from exc
    if len(decoded) > MAX_IMAGE_BYTES:
        raise GuardError(400, "invalid_request_error", "Antigravity tool image exceeds image size limit", scope="candidate")


async def inline_tool_output_images(payload: dict[str, Any]) -> dict[str, Any]:
    """AG-only: operate on normalized typed tool output lists, not arbitrary JSON."""
    out = copy.deepcopy(payload)
    items = out.get("input")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        parts = item.get("output")
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict) or part.get("type") not in {"input_image", "image_url"}:
                continue
            holder = part.get("image_url")
            url = holder.get("url") if isinstance(holder, dict) else holder
            if isinstance(url, str) and url.lower().startswith(("http://", "https://")):
                raw, mime = await download_https_image(url)
                data_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
                if isinstance(holder, dict):
                    holder["url"] = data_url
                else:
                    part["image_url"] = data_url
    return out
