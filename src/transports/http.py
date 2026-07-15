"""HTTP transport helpers.

Phase 6.2 extracts the construction of the httpx stream context from failover.
The returned object is still the original httpx async context manager; no retry,
proxy, parsing, or timeout semantics change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx


@dataclass(frozen=True)
class HttpStreamRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    content: bytes
    connect_timeout: float
    read_timeout: float
    write_timeout: float = 30.0
    pool_timeout: float | None = None
    extensions: Mapping[str, Any] | None = None

    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.write_timeout,
            pool=self.pool_timeout if self.pool_timeout is not None else self.connect_timeout,
        )


def open_stream(client, request: HttpStreamRequest):
    """Open an HTTP stream using the exact parameters Parrot used in failover."""
    return client.stream(
        request.method,
        request.url,
        headers=dict(request.headers),
        content=request.content,
        timeout=request.timeout(),
        extensions=dict(request.extensions or {}),
    )
