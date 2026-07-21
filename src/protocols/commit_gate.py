"""Commit-boundary helpers for protocol streams.

The first Protocol Runtime extraction keeps the existing HTTP/SSE failover
semantics intact while moving the boundary logic out of ``failover.py``:
metadata/control events do not commit an attempt, pre-commit stream errors remain
retryable, and same-protocol Responses metadata is replayed only after a real
visible event.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from .. import upstream


class StreamTranslator(Protocol):
    def feed(self, chunk: bytes) -> Iterable[bytes]: ...


@dataclass(frozen=True)
class CommitGateFeedResult:
    downstream_chunks: list[bytes]
    error_event: dict[str, Any] | None = None


class SseCommitGate:
    """Buffer SSE events until the first downstream-visible bytes.

    The gate is intentionally byte-preserving: complete SSE event blocks are
    replayed with their original bytes, and partial trailing bytes are only
    forwarded after the commit boundary has already been crossed.
    """

    def __init__(self, *, protocol: str, stream_translator: StreamTranslator | None = None) -> None:
        self.protocol = protocol
        self.stream_translator = stream_translator
        self._pending = b""
        self._buffered_downstream_chunks: list[bytes] = []
        # Persist the commit state as a defensive guarantee for callers that
        # feed multiple network batches before handing chunks downstream.
        self._downstream_committed = False

    def feed(self, restored: bytes) -> CommitGateFeedResult:
        self._pending += restored
        self._pending, events = upstream.split_sse_events(self._pending)
        downstream_chunks: list[bytes] = []
        downstream_started = self._downstream_committed

        for block in events:
            event_name, data = upstream.parse_sse_event_bytes(block)
            event_bytes = block + b"\n\n"
            if upstream.is_stream_error_event(event_name, data):
                if downstream_started:
                    # A single network chunk can contain multiple complete SSE
                    # events.  If an earlier event in this same feed already
                    # crossed the downstream-visible boundary, the attempt is
                    # committed; a following error must be forwarded/logged as
                    # a post-commit stream error, not converted back into a
                    # retryable pre-visible error.
                    downstream_chunks.extend(self._feed_downstream_event(event_bytes))
                    continue
                error_obj = dict(data or {})
                error_obj["_event_name"] = event_name or ""
                return CommitGateFeedResult([], error_obj)

            outs = self._feed_downstream_event(event_bytes)
            if outs:
                if not downstream_started and not chunks_have_downstream_commit_event(outs, self.protocol):
                    # Translator output can itself be downstream metadata.  In
                    # particular Anthropic→Responses emits response.created /
                    # response.in_progress on Anthropic message_start.  Buffer
                    # those until a real downstream-visible Responses event
                    # arrives, otherwise failover would commit on metadata.
                    self._buffered_downstream_chunks.extend(outs)
                    continue
                if not downstream_started and self._buffered_downstream_chunks:
                    downstream_chunks.extend(self._buffered_downstream_chunks)
                    self._buffered_downstream_chunks.clear()
                downstream_started = True
                self._downstream_committed = True
                downstream_chunks.extend(outs)

        if downstream_started:
            if self._pending:
                # Partial trailing bytes only happen when a chunk contains the
                # start of a following SSE block after the first downstream event.
                # It is now safe to pass through/feed them; subsequent reads will
                # continue from the network iterator.
                if self.stream_translator is not None:
                    downstream_chunks.extend(self.stream_translator.feed(self._pending))
                else:
                    downstream_chunks.append(self._pending)
                self._pending = b""
            return CommitGateFeedResult(downstream_chunks, None)
        return CommitGateFeedResult([], None)

    def _feed_downstream_event(self, event_bytes: bytes) -> list[bytes]:
        if self.stream_translator is not None:
            return list(self.stream_translator.feed(event_bytes))
        return [event_bytes]


def is_responses_visible_event_type(event_type: str | None) -> bool:
    """Return whether a Responses event type carries downstream-visible output."""
    return bool(event_type) and event_type in upstream.RESPONSES_VISIBLE_EVENTS


def is_sse_downstream_visible_event(event_name: str | None, data: dict | None, protocol: str) -> bool:
    """Whether an upstream SSE event should cross the commit boundary."""
    if protocol == "openai-responses":
        return is_responses_visible_event_type(event_name)
    return data is not None and not upstream.is_stream_error_event(event_name, data)


def is_sse_downstream_normal_terminal_event(
    event_name: str | None, data: dict | None, protocol: str,
) -> bool:
    """Whether a downstream frame is a valid empty Responses completion.

    A completed (or non-error incomplete) native Responses stream need not carry
    text/tool output.  It must still cross the pre-commit boundary so callers see
    a legitimate terminal response rather than a fabricated 503.
    """
    return (
        protocol == "openai-responses"
        and isinstance(data, dict)
        and event_name in ("response.completed", "response.incomplete")
    )


def is_sse_downstream_commit_event(event_name: str | None, data: dict | None, protocol: str) -> bool:
    return (
        is_sse_downstream_visible_event(event_name, data, protocol)
        or is_sse_downstream_normal_terminal_event(event_name, data, protocol)
    )


def _chunks_have_downstream_event(chunks: Iterable[bytes], protocol: str, *, include_normal_terminal: bool) -> bool:
    for chunk in chunks:
        if not chunk:
            continue
        pending = bytes(chunk)
        rest, events = upstream.split_sse_events(pending)
        if not events:
            # Non-SSE bytes should only exist after a translator deliberately
            # emits protocol data.  For Responses be conservative because
            # metadata visibility is event-type based.
            if protocol != "openai-responses":
                return True
            continue
        for block in events:
            event_name, data = upstream.parse_sse_event_bytes(block)
            if is_sse_downstream_visible_event(event_name, data, protocol):
                return True
            if include_normal_terminal and is_sse_downstream_normal_terminal_event(event_name, data, protocol):
                return True
        if rest and protocol != "openai-responses":
            return True
    return False


def chunks_have_downstream_visible_event(chunks: Iterable[bytes], protocol: str) -> bool:
    """Whether translated downstream chunks contain client-visible output."""
    return _chunks_have_downstream_event(chunks, protocol, include_normal_terminal=False)


def chunks_have_downstream_commit_event(chunks: Iterable[bytes], protocol: str) -> bool:
    """Whether chunks are enough to commit, including a valid empty completion."""
    return _chunks_have_downstream_event(chunks, protocol, include_normal_terminal=True)
