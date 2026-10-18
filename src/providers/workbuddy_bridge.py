"""Provider-local response bridges; all replay state travels with the response."""
from __future__ import annotations

from . import workbuddy_replay as replay
from ..openai.transform.stream_chat_to_anthropic import StreamTranslator as ChatToAnthropic, _emit as emit_anthropic
from ..openai.transform.stream_c2r import StreamTranslator as ChatToResponses, _emit as emit_responses


class _Replay:
    def _capture(self, event):
        for choice in event.get("choices") or []:
            if choice.get("index", 0) != 0:
                # Both target protocols have only one assistant candidate.
                raise ValueError("WorkBuddy bridge cannot combine multiple choices")
            delta = choice.get("delta") or {}
            replay.accumulate(self._workbuddy_fields, delta)
        # The tagged final carrier owns the complete reasoning. Do not also
        # produce an untagged summary that would duplicate it on replay.
        return {**event, "choices": [{**c, "delta": {k: v for k, v in (c.get("delta") or {}).items()
                    if k not in replay.FIELDS}} for c in event.get("choices") or []]}


class AnthropicStream(_Replay, ChatToAnthropic):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tool_deltas.append_name_deltas = True
        self._workbuddy_fields = {}
        self._workbuddy_block = None

    def _handle_event(self, event):
        yield from super()._handle_event(self._capture(event))

    def close(self):
        if not self.state.terminal_emitted:
            block = replay.thinking_block(self._workbuddy_fields)
            if block:
                yield from self._emit_message_start()
                yield from self._stop_text_if_needed()
                yield from self._stop_all_tools()
                if self.state.terminal_emitted:
                    return
                index = self.state.alloc_index()
                self._workbuddy_block = (index, block)
                yield emit_anthropic("content_block_start", {"type": "content_block_start", "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
                if block["thinking"]:
                    yield emit_anthropic("content_block_delta", {"type": "content_block_delta", "index": index,
                        "delta": {"type": "thinking_delta", "thinking": block["thinking"]}})
                # One final opaque value: native signatures replace, never concatenate.
                yield emit_anthropic("content_block_delta", {"type": "content_block_delta", "index": index,
                    "delta": {"type": "signature_delta", "signature": block["signature"]}})
                yield emit_anthropic("content_block_stop", {"type": "content_block_stop", "index": index})
        yield from super().close()

    def get_downstream_anthropic_assistant(self):
        result = super().get_downstream_anthropic_assistant()
        if self._workbuddy_block:
            result["content"].append(self._workbuddy_block[1])
        return result


class ResponsesStream(_Replay, ChatToResponses):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tool_deltas.append_name_deltas = True
        self._workbuddy_fields = {}

    def _handle_event(self, event):
        yield from super()._handle_event(self._capture(event))

    def _emit_terminal(self):
        item = replay.response_item(self._workbuddy_fields)
        if item:
            index = self.state.allocate_output_index()
            self.state.closed_items.append((index, item))
            for event, value in (("response.output_item.added", {**item, "status": "in_progress"}),
                                 ("response.output_item.done", item)):
                yield emit_responses(event, {"type": event, "sequence_number": self.state.next_seq(),
                    "output_index": index, "item": value})
        yield from super()._emit_terminal()
