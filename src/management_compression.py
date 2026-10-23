"""Gzip for the Management API and the WebUI build, nothing else.

Model listings and request logs are hundreds of kilobytes of JSON that shrink
15–40 times, and the WebUI bundle about three times; on a phone that is most of
the page load. The inference endpoints are deliberately untouched: they stream
upstream responses to API clients and must keep their exact framing.

Only textual bodies are compressed. Media, archives, partial (Range) responses,
bodiless responses (204 / 304) and anything already encoded pass through
unchanged. The decision is made here, on the response start, rather than by
subclassing Starlette's GZip responder, whose internals change between releases.
"""

from __future__ import annotations

import zlib
from typing import Any

import anyio.to_thread
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PREFIXES = ("/api/management/", "/ui/")
_TEXTUAL = ("application/json", "application/javascript", "text/", "image/svg+xml", "application/manifest+json")
# Event streams are textual but must reach the client event by event, uncompressed.
_STREAMED = ("text/event-stream",)
# Larger chunks are compressed in a worker thread so the event loop keeps serving proxy traffic.
_THREAD_MINIMUM = 128 * 1024


class _Responder:
    def __init__(self, send: Send, minimum_size: int, compresslevel: int) -> None:
        self.send = send
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.start: Message | None = None
        self.mode: str | None = None  # "plain" or "gzip", decided by the first body chunk
        self.compressor: Any = None

    async def __call__(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.start = message
            headers = Headers(raw=message["headers"])
            content_type = headers.get("content-type", "").lower()
            if (message["status"] in (204, 206, 304) or "content-encoding" in headers
                    or not content_type.startswith(_TEXTUAL) or content_type.startswith(_STREAMED)):
                self.mode = "plain"
                await self.send(message)
            return
        if self.mode == "plain":
            await self.send(message)
            return
        if kind != "http.response.body":
            # Trailers / pathsend before any body: nothing to compress.
            if self.mode is None and self.start is not None:
                self.mode = "plain"
                await self.send(self.start)
            await self.send(message)
            return
        body = message.get("body", b"")
        more = message.get("more_body", False)
        if self.mode is None:
            if not more and len(body) < self.minimum_size:
                self.mode = "plain"
                await self.send(self.start)
                await self.send(message)
                return
            self.mode = "gzip"
            self.compressor = zlib.compressobj(self.compresslevel, zlib.DEFLATED, 31)
            data = await self._compress(body, more)
            headers = MutableHeaders(raw=self.start["headers"])
            headers["Content-Encoding"] = "gzip"
            headers.add_vary_header("Accept-Encoding")
            if "content-length" in headers:
                del headers["content-length"]
            if not more:
                headers["Content-Length"] = str(len(data))
            await self.send(self.start)
            await self.send({"type": "http.response.body", "body": data, "more_body": more})
            return
        data = await self._compress(body, more)
        await self.send({"type": "http.response.body", "body": data, "more_body": more})

    async def _compress(self, body: bytes, more: bool) -> bytes:
        compressor = self.compressor

        def work() -> bytes:
            out = compressor.compress(body)
            return out + (compressor.flush(zlib.Z_SYNC_FLUSH) if more else compressor.flush())

        if len(body) >= _THREAD_MINIMUM:
            return await anyio.to_thread.run_sync(work)
        return work()


class ManagementCompressionMiddleware:
    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(PREFIXES):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if "gzip" not in headers.get("accept-encoding", "") or "range" in headers:
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, _Responder(send, self.minimum_size, self.compresslevel))
