"""Downstream stream projection for the managed-search loop.

Network I/O, translations and per-attempt settlement remain in failover. This
module consumes that *ingress-protocol* stream, forwarding visible events while
building the transcript required by the existing search execution loop. Model
round terminals are private; there is only one downstream response lifecycle.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid

from fastapi.responses import JSONResponse, StreamingResponse

from . import local_web_tools as web, upstream
from .protocols.sse import split_sse_events
from .transports.chat_aggregate import ChatAggregateBuilder


def _take_chat_tools(delta, tools):
    for call in delta.pop("tool_calls", []) or []:
        slot = tools.setdefault(call.get("index", 0), {"type": "function"})
        for key, value in call.items():
            if key not in ("index", "function", "custom"):
                slot[key] = copy.deepcopy(value)
        for field in ("function", "custom"):
            if isinstance(call.get(field), dict):
                fn = slot.setdefault(field, {})
                for key, value in call[field].items():
                    fn[key] = fn.get(key, "") + value if isinstance(value, str) else copy.deepcopy(value)
    legacy = delta.pop("function_call", None)
    if isinstance(legacy, dict):
        slot = tools.setdefault("legacy", {})
        for key, value in legacy.items():
            slot[key] = slot.get(key, "") + value if isinstance(value, str) else copy.deepcopy(value)


class ChatToolNames:
    """Before cross-protocol translation, complete only Chat tool fragments.

    Responses/Anthropic tool starts have immutable names. The existing bridges
    cannot emit a partial Chat name as such a start without losing ownership.
    Enabled only for managed rounds; normal text/usage pass through immediately.
    """
    def __init__(self, translator):
        self.translator = translator
        self.buffer = b""
        self.tools = {}

    def __getattr__(self, name):
        return getattr(self.translator, name)

    def feed(self, chunk):
        self.buffer, blocks = split_sse_events(self.buffer + chunk)
        for block in blocks:
            raw = b"\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b"data:"))
            if not raw or raw == b"[DONE]":
                if raw == b"[DONE]" and any(self.tools.values()):
                    raise ValueError("Chat tool stream ended without finish_reason")
                yield from self.translator.feed(block + b"\n\n")
                continue
            data = json.loads(raw)
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                choice["delta"] = delta
                index = choice.get("index", 0)
                tools = self.tools.setdefault(index, {})
                _take_chat_tools(delta, tools)
                if choice.get("finish_reason"):
                    if "legacy" in tools:
                        delta["function_call"] = tools.pop("legacy")
                    if tools:
                        delta["tool_calls"] = [{**call, "index": i} for i, call in tools.items()]
                    self.tools.pop(index, None)
            yield from self.translator.feed(b"data: " + json.dumps(data).encode() + b"\n\n")

    def close(self):
        return self.translator.close()


class Projection:
    def __init__(self, body, protocol, emit):
        self.body, self.protocol, self.emit = body, protocol, emit
        self.plan = {}
        self.usage = {}
        self.base = None
        self.sequence = 0
        self.items = []
        self.ids = set()
        self.chat = {}
        self.round = None
        self.failure_type = None
        self.failure_response = {}
        self.deferred_tools = []
        self.deferred_items = {}
        self.visible_builder = upstream.ResponsesSSEAssistantBuilder() if protocol == "responses" else None

    async def send(self, event, data):
        data = copy.deepcopy(data)
        if self.protocol == "responses":
            data["type"] = event
            data["sequence_number"] = self.sequence
            self.sequence += 1
            self.visible_builder.feed(web._sse(event, data))
        await self.emit(web._sse(event, data) if self.protocol != "chat" else
                        b"data: " + json.dumps(data, ensure_ascii=False).encode() + b"\n\n")

    async def start(self, obj):
        if self.base is not None:
            return
        from .search_tool_policy import _restore_hosted_metadata
        self.base = copy.deepcopy(obj)
        if self.protocol == "responses":
            self.base.setdefault("id", "resp_" + uuid.uuid4().hex)
            _restore_hosted_metadata(self.base, self.plan)
            response = {**self.base, "output": [], "status": "in_progress", "usage": None}
            await self.send("response.created", {"response": response})
            await self.send("response.in_progress", {"response": response})
        elif self.protocol == "anthropic":
            self.base.setdefault("id", "msg_" + uuid.uuid4().hex)
            await self.send("message_start", {"type": "message_start", "message": {
                **self.base, "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {**(self.base.get("usage") or {}), "output_tokens": 0}}})

    def chat_base(self):
        return {**{k: v for k, v in (self.base or {}).items() if k not in ("choices", "usage")},
                "object": "chat.completion.chunk"}

    def projected_items(self):
        return copy.deepcopy(self.items + list(self.deferred_items.values()))

    def store_item(self, index, item):
        if index < 0:
            self.deferred_items[index] = item
        else:
            self.items[index] = item

    def new_item(self, item, *, deferred=False):
        item = copy.deepcopy(item)
        ident = item.get("id")
        if self.protocol == "responses":
            if not ident or ident in self.ids:
                ident = ("fc_" if item.get("type") == "function_call" else "item_") + uuid.uuid4().hex
            item["id"] = ident
            self.ids.add(ident)
        # Tools receive a public index only when committed, after already
        # visible text. Failures can therefore omit them without index holes.
        index = -len(self.deferred_items) - 1 if deferred else len(self.items)
        if deferred:
            self.deferred_items[index] = item
        else:
            self.items.append(item)
        return index, ident

    def owned(self, item):
        return (str(item.get("namespace") or ""), str(item.get("name") or "")) in self.plan

    async def consume(self, response, branch_index=None):
        """Read one real stream; closing/cancelling always closes its owner."""
        self.round = Round(self, branch_index)
        current = self.round
        if hasattr(response, "body_iterator"):
            iterator = response.body_iterator
        else:
            # A caller may already have a JSON result (e.g. transport-level
            # errors). Never re-invoke or replay a stream after visible output.
            if response.status_code >= 400:
                return response
            obj = json.loads(response.body)
            from .search_tool_policy import _chat_sse
            iterator = (web._iter_openai_response_sse(obj) if self.protocol == "responses" else
                        web._iter_anthropic_message_sse(obj) if self.protocol == "anthropic" else _chat_sse(obj))
        buf = b""
        try:
            async for chunk in iterator:
                buf += chunk if isinstance(chunk, bytes) else chunk.encode()
                buf, blocks = split_sse_events(buf)
                for block in blocks:
                    raw = b"\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b"data:"))
                    if raw == b"[DONE]":
                        current.terminal = True
                        continue
                    if not raw:
                        continue
                    data = json.loads(raw)
                    event = data.get("type") or next((line[6:].strip().decode() for line in block.splitlines() if line.startswith(b"event:")), "")
                    if event == "error" or isinstance(data.get("error"), dict) or event in ("response.failed", "response.incomplete"):
                        self.failure_type = event
                        self.failure_response = copy.deepcopy(data.get("response") or {})
                        error = data.get("error") or (data.get("response") or {}).get("error") or {
                            "type": "api_error", "message": "upstream response incomplete",
                            "code": "stream_incomplete"}
                        return JSONResponse({"error": error}, status_code=502)
                    await current.feed(event, data)
            if self.protocol == "chat" and current.chat and all(b._finish_reason for b in current.chat.values()):
                # Existing Chat transports also accept EOF after finish_reason;
                # usage can arrive between that marker and EOF/DONE.
                current.terminal = True
            if not current.terminal:
                return JSONResponse({"error": {"type": "api_error", "message":
                    "managed search upstream stream ended without a terminal event"}}, status_code=502)
            obj = await current.snapshot()
            return JSONResponse(obj, status_code=response.status_code, headers={k: v for k, v in response.headers.items()
                                              if k.lower() not in ("content-length", "content-type")})
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    async def finish_branch(self, obj, branch_index=None):
        """Project the visible transcript used both by terminals and replay."""
        out = copy.deepcopy(obj)
        if self.protocol == "responses":
            from .search_tool_policy import _restore_hosted_metadata
            for key in ("id", "created_at", "object"):
                if key in self.base:
                    out[key] = self.base[key]
            out["output"] = self.projected_items()
            _restore_hosted_metadata(out, self.plan)
        elif self.protocol == "anthropic":
            out["id"] = self.base["id"]
            out["content"] = self.projected_items()
        else:
            choice = out["choices"][0]
            index = branch_index if branch_index is not None else choice.get("index", 0)
            message = copy.deepcopy(choice.get("message") or {})
            tools = {k: message[k] for k in ("tool_calls", "function_call") if message.get(k)}
            if tools:
                delta = copy.deepcopy(tools)
                for i, call in enumerate(delta.get("tool_calls") or []):
                    call["index"] = i
                self.deferred_tools.append(("", {**self.chat_base(), "choices": [{"index": index, "delta": delta, "finish_reason": None}]}))
            message.update(self.chat[index].get_assistant())
            message.update(tools)
            choice["message"] = message
            choice["logprobs"] = copy.deepcopy(self.chat[index].logprobs) or None
            choice["index"] = index
            for key in ("id", "created"):
                if key in self.base:
                    out[key] = self.base[key]
        return out

    async def commit_tools(self):
        # Only after owned effects and replay snapshots are recorded may client
        # tools escape. A later branch/round failure must not dispatch them.
        indices = {}
        for temporary, item in self.deferred_items.items():
            indices[temporary] = len(self.items)
            self.items.append(item)
        for event, data in self.deferred_tools:
            data = copy.deepcopy(data)
            field = "output_index" if self.protocol == "responses" else "index"
            if data.get(field) in indices:
                data[field] = indices[data[field]]
            await self.send(event, data)
        self.deferred_tools.clear()
        self.deferred_items.clear()

    async def complete(self, response):
        obj = json.loads(response.body)
        if response.status_code >= 400:
            await self.fail(obj)
            return
        if self.protocol == "responses":
            await self.send("response.completed", {"response": obj})
        elif self.protocol == "anthropic":
            await self.send("message_delta", {"type": "message_delta", "delta": {
                "stop_reason": obj.get("stop_reason"), "stop_sequence": obj.get("stop_sequence")},
                "usage": obj.get("usage") or {}})
            await self.send("message_stop", {"type": "message_stop"})
        else:
            for choice in obj.get("choices") or []:
                await self.send("", {**self.chat_base(), "choices": [{"index": choice.get("index", 0),
                    "delta": {}, "finish_reason": choice.get("finish_reason"), "logprobs": None}]})
            if (self.body.get("stream_options") or {}).get("include_usage") and "usage" in obj:
                await self.send("", {**self.chat_base(), "choices": [], "usage": obj["usage"]})
            await self.emit(b"data: [DONE]\n\n")

    async def fail(self, obj):
        if self.protocol == "responses":
            await self.start({"object": "response", "model": self.body.get("model")})
            typ = "response.incomplete" if self.failure_type == "response.incomplete" else "response.failed"
            from .search_tool_policy import _restore_hosted_metadata
            failed = {**self.base, **self.failure_response, "id": self.base["id"],
                "output": self.visible_builder.get_output_items(),
                "status": "incomplete" if typ.endswith("incomplete") else "failed", "error": obj.get("error")}
            from .search_tool_policy import _sum_usage
            usage = copy.deepcopy(self.usage)
            partial_usage = self.failure_response.get("usage")
            if isinstance(partial_usage, dict):
                _sum_usage(usage, partial_usage)
            if usage:
                failed["usage"] = usage
            _restore_hosted_metadata(failed, self.plan)
            await self.send(typ, {"response": failed})
        else:
            await self.send("error", {"type": "error", **obj})


class Round:
    def __init__(self, owner, branch_index):
        self.owner, self.protocol, self.branch_index = owner, owner.protocol, branch_index
        self.builder = upstream.ResponsesSSEAssistantBuilder() if self.protocol == "responses" else upstream.SSEAssistantBuilder()
        self.chat = {}
        self.functions = {}
        self.metadata = {}
        self.usage = {}
        self.mapping = {}
        self.hidden = set()
        self.pending = {}
        self.terminal = False
        self.stop = {}
        self.raw_inputs = {}
        self.call_indices = {}
        self.uncertain = set()
        self.deferred_indices = set()
        # Track what was actually emitted, not what the aggregate builder can
        # reconstruct from a later snapshot. Incremental consumers need both.
        self.response_sent = set()
        self.response_text = {}
        self.response_args = {}

    @staticmethod
    def _item_start(item):
        start = copy.deepcopy(item)
        if start.get("type") == "message":
            start["content"] = []
        elif start.get("type") == "function_call":
            start["arguments"] = ""
        return start

    async def _fill_part(self, index, content_index, part):
        typ = part.get("type")
        field = {"output_text": "text", "refusal": "refusal"}.get(typ)
        if field is None:
            return
        ids = {"output_index": index, "content_index": content_index,
               "item_id": self.mapping[index][1]}
        await self._emit_response("response.content_part.added", {
            **ids, "part": {**part, field: ""},
        })
        text = str(part.get(field) or "")
        prior = self.response_text.get((index, content_index, typ), "")
        if not text.startswith(prior):
            if prior.startswith(text):
                return  # sparse/shorter snapshots must not erase live text
            raise ValueError("managed search response snapshot conflicts with emitted text")
        if suffix := text[len(prior):]:
            await self._emit_response(f"response.{typ}.delta", {**ids, "delta": suffix})

    async def _close_part(self, index, content_index, part):
        await self._fill_part(index, content_index, part)
        field = {"output_text": "text", "refusal": "refusal"}.get(part.get("type"))
        ids = {"output_index": index, "content_index": content_index,
               "item_id": self.mapping[index][1]}
        if field:
            await self._emit_response(f"response.{part['type']}.done", {**ids, field: part.get(field, "")})
        await self._emit_response("response.content_part.done", {**ids, "part": part})

    async def _fill_arguments(self, index, arguments):
        arguments = str(arguments or "")
        prior = self.response_args.get(index, "")
        if not arguments.startswith(prior):
            if prior.startswith(arguments):
                return
            raise ValueError("managed search tool snapshot conflicts with emitted arguments")
        if suffix := arguments[len(prior):]:
            await self._emit_response("response.function_call_arguments.delta", {
                "output_index": index, "item_id": self.mapping[index][1], "delta": suffix,
            })

    async def _fill_item(self, index, item, *, close=False):
        if item.get("type") == "message":
            for content_index, part in enumerate(item.get("content") or []):
                if isinstance(part, dict):
                    method = self._close_part if close else self._fill_part
                    await method(index, content_index, part)
        elif item.get("type") == "function_call":
            await self._fill_arguments(index, item.get("arguments"))
            if close:
                await self._emit_response("response.function_call_arguments.done", {
                    "output_index": index, "item_id": self.mapping[index][1],
                    "arguments": item.get("arguments", ""),
                })

    async def _emit_response(self, event, data):
        """Close sparse snapshots before their done event, exactly once.

        Ordinary deltas pass through immediately. Snapshot repair sends only
        their missing suffix; replaying a whole final item would duplicate text
        and tool arguments in SDKs as well as OpenBear's incremental consumers.
        """
        index = data["output_index"]
        key = (event, index, data.get("content_index"), data.get("summary_index"))
        once = event.endswith((".added", ".done"))
        if once and key in self.response_sent:
            return
        if event == "response.output_item.done":
            await self._fill_item(index, data.get("item") or {}, close=True)
        elif event == "response.content_part.done":
            part = data.get("part") or {}
            await self._fill_part(index, data.get("content_index", 0), part)
            field = {"output_text": "text", "refusal": "refusal"}.get(part.get("type"))
            if field:
                await self._emit_response(f"response.{part['type']}.done", {
                    **{k: v for k, v in data.items() if k != "part"}, field: part.get(field, ""),
                })
        elif event in ("response.output_text.done", "response.refusal.done"):
            typ = event.split(".")[1]
            field = "text" if typ == "output_text" else "refusal"
            await self._fill_part(index, data.get("content_index", 0), {"type": typ, field: data.get(field, "")})
        elif event == "response.function_call_arguments.done":
            await self._fill_arguments(index, data.get("arguments"))

        p = self.owner
        mapped, ident = self.mapping[index]
        out = {**data, "output_index": mapped}
        if "item_id" in out:
            out["item_id"] = ident
        if "response_id" in out:
            out["response_id"] = p.base["id"]
        item = data.get("item")
        if isinstance(item, dict):
            outgoing = self._item_start(item) if event.endswith(".added") else item
            out["item"] = {**outgoing, "id": ident}
        if event == "response.content_part.added":
            part = data.get("part") or {}
            field = {"output_text": "text", "refusal": "refusal"}.get(part.get("type"))
            if field:
                out["part"] = {**part, field: ""}
        if index in self.deferred_indices:
            p.deferred_tools.append((event, out))
        else:
            await p.send(event, out)
        if once:
            self.response_sent.add(key)
        if event in ("response.output_text.delta", "response.refusal.delta"):
            part_key = (index, data.get("content_index", 0), event.split(".")[1])
            self.response_text[part_key] = self.response_text.get(part_key, "") + (data.get("delta") or "")
        elif event == "response.function_call_arguments.delta":
            self.response_args[index] = self.response_args.get(index, "") + (data.get("delta") or "")
        elif event == "response.output_item.added" and isinstance(item, dict):
            await self._fill_item(index, item)
        elif event == "response.content_part.added":
            await self._fill_part(index, data.get("content_index", 0), data.get("part") or {})

    def duplicate_call(self, index, item):
        ident = item.get("call_id") or item.get("id")
        if not ident:
            return False
        prior = self.call_indices.setdefault(ident, index)
        return prior != index

    async def feed(self, event, data, *, build=True):
        p = self.owner
        if self.protocol == "chat":
            await self.chat_feed(data)
            return
        if build:
            self.builder.feed(web._sse(event, {**data, "type": event}))
        if self.protocol == "responses":
            if event in ("response.created", "response.in_progress"):
                await p.start(data.get("response") or {})
                return
            if event == "response.completed":
                self.terminal = True
                return
            if "output_index" not in data:
                # Round lifecycle/metadata are not a second client lifecycle.
                return
            index = data["output_index"]
            item = data.get("item")
            if event == "response.output_item.done" and isinstance(item, dict):
                # The native item captured by clients must match the final
                # snapshot used by full-history replay, including merged text
                # and normalized fields such as empty annotations.
                item = self.builder.get_output_item(index) or item
                data = {**data, "item": item}
            if isinstance(item, dict):
                if item.get("type") == "function_call" and not item.get("name"):
                    self.uncertain.add(index)
                    self.pending.setdefault(index, []).append((event, data))
                    return
                if index in self.uncertain:
                    self.uncertain.remove(index)
                    waiting = self.pending.pop(index, [])
                    for old_event, old_data in waiting:
                        if isinstance(old_data.get("item"), dict):
                            old_data = {**old_data, "item": {**old_data["item"], "name": item.get("name"),
                                "namespace": item.get("namespace", "")}}
                        await self.feed(old_event, old_data, build=False)
                if item.get("type") == "function_call" and (p.owned(item) or self.duplicate_call(index, item)):
                    self.hidden.add(index)
                    self.pending.pop(index, None)
                    return
                if item.get("type") == "function_call":
                    self.deferred_indices.add(index)
                if index not in self.mapping:
                    await p.start({"object": "response", "model": p.body.get("model")})
                    self.mapping[index] = p.new_item(item, deferred=index in self.deferred_indices)
                    if event != "response.output_item.added":
                        await self._emit_response("response.output_item.added", {
                            "output_index": index, "item": self._item_start(item),
                        })
            if index in self.hidden:
                return
            if index not in self.mapping:
                self.pending.setdefault(index, []).append((event, data))
                return
            mapped, ident = self.mapping[index]
            if isinstance(item, dict):
                p.store_item(mapped, {**copy.deepcopy(item), "id": ident})
            pending = self.pending.pop(index, [])
            # Buffered deltas must precede an item/part close, but must follow
            # its start when an upstream sends the identifying item late.
            if event != "response.output_item.added":
                for old_event, old_data in pending:
                    await self.feed(old_event, old_data, build=False)
                pending = []
            await self._emit_response(event, data)
            for old_event, old_data in pending:
                await self.feed(old_event, old_data, build=False)
        else:
            if event == "message_start":
                self.metadata = copy.deepcopy(data.get("message") or {})
                self.usage.update(self.metadata.get("usage") or {})
                await p.start(self.metadata)
            elif event == "message_delta":
                self.stop.update(data.get("delta") or {})
                self.usage.update(data.get("usage") or {})
            elif event == "message_stop":
                self.terminal = True
            elif event.startswith("content_block_"):
                index = data.get("index", 0)
                delta = data.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    self.raw_inputs[index] = self.raw_inputs.get(index, "") + (delta.get("partial_json") or "")
                if event == "content_block_start":
                    item = data.get("content_block") or {}
                    if item.get("type") == "tool_use" and (p.owned(item) or self.duplicate_call(index, item)):
                        self.hidden.add(index)
                        return
                    if item.get("type") == "tool_use":
                        self.deferred_indices.add(index)
                    self.mapping[index] = p.new_item(item, deferred=index in self.deferred_indices)
                if index not in self.hidden:
                    if index not in self.mapping:
                        raise ValueError("content block delta without a start")
                    out = {**data, "index": self.mapping[index][0]}
                    if index in self.deferred_indices:
                        p.deferred_tools.append((event, out))
                    else:
                        await p.send(event, out)
            elif event == "ping":
                await p.send(event, data)

    async def chat_feed(self, data):
        p = self.owner
        await p.start(data)
        for key in ("id", "created", "model", "system_fingerprint", "service_tier"):
            if key in data:
                self.metadata[key] = data[key]
        if isinstance(data.get("usage"), dict):
            self.usage = copy.deepcopy(data["usage"])
        for choice in data.get("choices") or []:
            index = choice.get("index", 0)
            builder = self.chat.setdefault(index, ChatAggregateBuilder())
            # The common builder handles text/reasoning/refusal/logprobs. Tool
            # names may be split across Chat chunks, so collect them separately
            # until ownership can be decided, including legacy function_call.
            delta = copy.deepcopy(choice.get("delta") or {})
            tools = self.functions.setdefault(index, {})
            _take_chat_tools(delta, tools)
            clean = {**choice, "delta": delta}
            builder._apply({**data, "choices": [clean]})
            # Keep raw finish reason, even though the text-only builder has no calls.
            builder._finish_reason = choice.get("finish_reason") or builder._finish_reason
            logical = self.branch_index if self.branch_index is not None else index
            visible = p.chat.setdefault(logical, ChatAggregateBuilder())
            visible._apply({**data, "choices": [clean]})
            if delta or choice.get("logprobs"):
                await p.send("", {**p.chat_base(), "choices": [{**clean, "index": logical, "finish_reason": None}]})

    async def snapshot(self):
        p = self.owner
        if self.protocol == "chat":
            choices = []
            for index, builder in self.chat.items():
                if not builder._finish_reason:
                    raise ValueError("Chat stream ended without a choice finish_reason")
                message = builder.get_assistant()
                tools = self.functions[index]
                if "legacy" in tools:
                    message["function_call"] = tools["legacy"]
                calls = [value for key, value in tools.items() if key != "legacy"]
                if calls:
                    message["tool_calls"] = calls
                choices.append({"index": index, "message": message,
                    "finish_reason": builder._finish_reason, "logprobs": builder.logprobs or None})
            return {**self.metadata, "object": "chat.completion", "choices": choices, "usage": self.usage}
        if self.protocol == "responses":
            obj = self.builder.to_full_json(fallback_model=p.body.get("model", ""))
            await p.start(obj)
            items = obj.get("output") or []
        else:
            obj = {**self.metadata, **self.builder.get_assistant(), **self.stop, "usage": self.usage}
            items = obj.get("content") or []
            for index, raw in self.raw_inputs.items():
                if index < len(items):
                    try:
                        items[index]["input"] = json.loads(raw)
                    except ValueError:
                        items[index]["input"] = raw
        for index, item in enumerate(items):
            if index in self.hidden:
                continue
            if item.get("type") in ("function_call", "tool_use") and p.owned(item):
                continue
            if self.protocol == "responses":
                # Reconcile every final item, not just unseen indices. A started
                # message may have only its first delta; opaque reasoning may
                # receive encrypted_content only in this terminal snapshot.
                await self.feed("response.output_item.done", {
                    "output_index": index, "item": item,
                }, build=False)
                if index in self.hidden:
                    continue  # a terminal-only duplicate may just be classified
            elif index not in self.mapping:
                raise ValueError("terminal content without a block start")
            mapped, ident = self.mapping[index]
            p.store_item(mapped, {**copy.deepcopy(item), **({"id": ident} if self.protocol == "responses" else {})})
        return obj


def stream(body, protocol, invoke, *, request_id=None, api_key_name=None):
    """One bounded producer, shared by HTTP SSE and Responses WebSocket."""
    from .search_tool_policy import run

    async def iterate():
        queue = asyncio.Queue(maxsize=1)
        projection = Projection(body, protocol, queue.put)

        async def produce():
            try:
                response = await run(body, protocol, invoke, request_id=request_id,
                                     api_key_name=api_key_name, _stream=projection)
                await projection.complete(response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await projection.fail({"error": {"type": "api_error", "message": str(exc)}})
            await queue.put(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                # wait_for can swallow external cancellation on Python 3.11
                # when its inner get has just completed. Own the get task
                # explicitly so a WS cancel always reaches producer cleanup.
                get_task = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait({get_task}, timeout=5)
                finally:
                    if not get_task.done():
                        get_task.cancel()
                    await asyncio.gather(get_task, return_exceptions=True)
                if not done:
                    if task.done():
                        await task  # do not turn an encoder failure into endless heartbeats
                        break
                    yield b": parrot managed search\n\n"
                    continue
                chunk = get_task.result()
                if chunk is None:
                    break
                yield chunk
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(iterate(), media_type="text/event-stream")
