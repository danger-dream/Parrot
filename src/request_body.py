"""Bounded HTTP request body decoding for inference ingress routes.

Supports optional ``Content-Encoding: gzip`` / ``x-gzip`` with streaming
chunk-by-chunk inflation and a strict decompressed byte ceiling so gzip bombs
are rejected immediately without materializing the uncompressed payload.
"""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterable, Iterable, Mapping
from typing import Any

from starlette.requests import Request

MAX_DECOMPRESSED_BODY_BYTES = 32 * 1024 * 1024  # 32 MiB
_GZIP_WBITS = 16 + zlib.MAX_WBITS
_NO_RETRY_HEADERS = {"x-should-retry": "false"}


class RequestBodyDecodeError(Exception):
    """Raised when an encoded HTTP request body is unsupported, corrupt, or oversized."""

    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)
        self.code = code


def parse_content_encodings(headers: Any) -> list[str]:
    """Return normalized non-identity Content-Encoding tokens in application order."""
    raw_values: list[str] = []
    if headers is None:
        return []
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        raw_values.extend(str(v) for v in getlist("content-encoding") if v is not None)
    elif isinstance(headers, Mapping):
        for k, v in headers.items():
            if str(k).lower() == "content-encoding" and v is not None:
                raw_values.append(str(v))
    if not raw_values:
        return []
    tokens: list[str] = []
    for raw in raw_values:
        for part in raw.split(","):
            tok = part.strip().lower()
            if tok and tok != "identity":
                tokens.append("gzip" if tok == "x-gzip" else tok)
    return tokens


class _BoundedGzipDecoder:
    """Incremental gzip decoder enforcing a strict uncompressed byte cap."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max(1, int(max_bytes))
        self._total_out = 0
        self._chunks: list[bytes] = []
        self._saw_input = False
        self._decompressor = zlib.decompressobj(_GZIP_WBITS)

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._saw_input = True
        data = chunk
        while data:
            remaining = (self._max_bytes + 1) - self._total_out
            try:
                piece = self._decompressor.decompress(data, max(1, remaining))
            except zlib.error as exc:
                raise RequestBodyDecodeError(
                    400,
                    f"invalid gzip request body: {exc}",
                    code="invalid_request_encoding",
                ) from exc
            if piece:
                self._total_out += len(piece)
                if self._total_out > self._max_bytes:
                    self._chunks.clear()
                    raise RequestBodyDecodeError(
                        413,
                        f"decompressed request body exceeds limit ({self._max_bytes} bytes)",
                        code="request_too_large",
                    )
                self._chunks.append(piece)
            if self._decompressor.eof:
                unused = self._decompressor.unused_data
                if unused:
                    self._decompressor = zlib.decompressobj(_GZIP_WBITS)
                    data = unused
                else:
                    data = b""
            else:
                tail = self._decompressor.unconsumed_tail
                if not piece and len(tail) == len(data):
                    break
                data = tail

    def finish(self) -> bytes:
        if not self._saw_input:
            raise RequestBodyDecodeError(
                400,
                "invalid gzip request body: empty or truncated stream",
                code="invalid_request_encoding",
            )
        try:
            tail = self._decompressor.flush()
        except zlib.error as exc:
            raise RequestBodyDecodeError(
                400,
                f"invalid gzip request body: {exc}",
                code="invalid_request_encoding",
            ) from exc
        if tail:
            self._total_out += len(tail)
            if self._total_out > self._max_bytes:
                self._chunks.clear()
                raise RequestBodyDecodeError(
                    413,
                    f"decompressed request body exceeds limit ({self._max_bytes} bytes)",
                    code="request_too_large",
                )
            self._chunks.append(tail)
        if not self._decompressor.eof:
            raise RequestBodyDecodeError(
                400,
                "invalid gzip request body: unexpected end of stream",
                code="invalid_request_encoding",
            )
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def decompress_gzip_chunks(
    chunks: Iterable[bytes],
    *,
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BODY_BYTES,
) -> bytes:
    """Decompress an iterable of gzip byte chunks with a strict output cap."""
    decoder = _BoundedGzipDecoder(max_decompressed_bytes)
    for chunk in chunks:
        decoder.feed(chunk)
    return decoder.finish()


async def decompress_gzip_async_chunks(
    chunks: AsyncIterable[bytes],
    *,
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BODY_BYTES,
) -> bytes:
    """Decompress an async stream of gzip byte chunks with a strict output cap."""
    decoder = _BoundedGzipDecoder(max_decompressed_bytes)
    async for chunk in chunks:
        decoder.feed(chunk)
    return decoder.finish()


async def read_decoded_request_body(
    request: Request,
    *,
    max_decompressed_bytes: int = MAX_DECOMPRESSED_BODY_BYTES,
) -> bytes:
    """Read request body bytes, decoding bounded ``gzip`` when declared."""
    encodings = parse_content_encodings(request.headers)
    if not encodings:
        return await request.body()
    if encodings != ["gzip"]:
        joined = ", ".join(encodings)
        raise RequestBodyDecodeError(
            415,
            f"unsupported Content-Encoding: {joined}",
            code="unsupported_content_encoding",
        )
    return await decompress_gzip_async_chunks(
        request.stream(),
        max_decompressed_bytes=max_decompressed_bytes,
    )
