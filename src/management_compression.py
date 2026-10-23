"""Gzip for the Management API and the WebUI build, nothing else.

Model listings and request logs are hundreds of kilobytes of JSON that shrink
15–40 times, and the WebUI bundle about three times; on a phone that is most of
the page load. The inference endpoints are deliberately untouched: they stream
upstream responses to API clients and must keep their exact framing.

Only textual bodies are compressed. Media, archives, partial (Range) responses
and anything already encoded pass through unchanged.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PREFIXES = ("/api/management/", "/ui/")
_TEXTUAL = ("application/json", "application/javascript", "text/", "image/svg+xml", "application/manifest+json")


class _Responder(GZipResponder):
    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            await super().send_with_compression(message)
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            if message.get("status") in (206, 304) or not content_type.startswith(_TEXTUAL):
                self.content_type_is_excluded = True
            return
        await super().send_with_compression(message)


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
        await _Responder(self.app, self.minimum_size, compresslevel=self.compresslevel)(scope, receive, send)
