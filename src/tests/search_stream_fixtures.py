"""Fake provider SSE, with actual incremental text and fragmented tool names."""
import copy
import json

from src import local_web_tools as web


def events(obj, protocol):
    obj = copy.deepcopy(obj)
    if protocol == "responses":
        yield "response.created", {"type": "response.created", "response": {**obj, "output": [], "status": "in_progress"}}
        for index, item in enumerate(obj.get("output") or []):
            item.setdefault("id", "item-" + str(index))
            start = copy.deepcopy(item)
            if item["type"] == "message":
                start["content"] = []
            elif item["type"] == "function_call":
                start["arguments"] = ""
            start["status"] = "in_progress"
            yield "response.output_item.added", {"type": "response.output_item.added", "output_index": index, "item": start}
            if item["type"] == "message":
                for ci, part in enumerate(item["content"]):
                    ids = {"output_index": index, "item_id": item["id"], "content_index": ci}
                    yield "response.content_part.added", {"type": "response.content_part.added", **ids, "part": {**part, "text": ""}}
                    for text in part.get("text") or "":
                        yield "response.output_text.delta", {"type": "response.output_text.delta", **ids, "delta": text}
                    yield "response.output_text.done", {"type": "response.output_text.done", **ids, "text": part["text"]}
                    yield "response.content_part.done", {"type": "response.content_part.done", **ids, "part": part}
            elif item["type"] == "function_call":
                for args in (item["arguments"][:3], item["arguments"][3:]):
                    yield "response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "output_index": index, "item_id": item["id"], "delta": args}
                yield "response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "output_index": index, "item_id": item["id"], "arguments": item["arguments"]}
            yield "response.output_item.done", {"type": "response.output_item.done", "output_index": index, "item": item}
        yield "response.completed", {"type": "response.completed", "response": obj}
    elif protocol == "anthropic":
        yield "message_start", {"type": "message_start", "message": {**obj, "content": [], "stop_reason": None, "usage": {**obj.get("usage", {}), "output_tokens": 0}}}
        for index, block in enumerate(obj.get("content") or []):
            start = {**block, "text": ""} if block["type"] == "text" else {**block, "input": {}}
            yield "content_block_start", {"type": "content_block_start", "index": index, "content_block": start}
            if block["type"] == "text":
                for text in block["text"]:
                    yield "content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}
            else:
                yield "content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}
            yield "content_block_stop", {"type": "content_block_stop", "index": index}
        yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": obj["stop_reason"]}, "usage": {"output_tokens": obj["usage"]["output_tokens"]}}
        yield "message_stop", {"type": "message_stop"}
    else:
        base = {k: v for k, v in obj.items() if k not in ("choices", "usage")}
        base["object"] = "chat.completion.chunk"
        for choice in obj["choices"]:
            def chunk(delta, reason=None):
                return "", {**base, "choices": [{"index": choice["index"], "delta": delta, "finish_reason": reason}]}
            yield chunk({"role": "assistant"})
            msg = choice["message"]
            for text in msg.get("content") or "":
                yield chunk({"content": text})
            for index, call in enumerate(msg.get("tool_calls") or []):
                fn = call["function"]
                yield chunk({"tool_calls": [{"index": index, "id": call["id"], "type": "function", "function": {"name": fn["name"][:3], "arguments": ""}}]})
                yield chunk({"tool_calls": [{"index": index, "function": {"name": fn["name"][3:], "arguments": fn["arguments"]}}]})
            if msg.get("function_call"):
                fn = msg["function_call"]
                yield chunk({"function_call": {"name": fn["name"][:3], "arguments": ""}})
                yield chunk({"function_call": {"name": fn["name"][3:], "arguments": fn["arguments"]}})
            yield chunk({}, choice["finish_reason"])
        if "usage" in obj:
            yield "", {**base, "choices": [], "usage": obj["usage"]}
        yield "", None


def encode(event, data):
    if data is None:
        return b"data: [DONE]\n\n"
    return web._sse(event, data) if event else b"data: " + json.dumps(data).encode() + b"\n\n"


def wire(obj, protocol):
    return b"".join(encode(event, data) for event, data in events(obj, protocol))


def text_delta(data, protocol):
    if protocol == "responses" and data.get("type") == "response.output_text.delta":
        return data.get("delta") or ""
    if protocol == "anthropic" and (data.get("delta") or {}).get("type") == "text_delta":
        return data["delta"]["text"]
    if protocol == "chat":
        return "".join((c.get("delta") or {}).get("content") or "" for c in data.get("choices") or [])
    return ""


def decode(raw):
    return [json.loads(line[5:]) for line in raw.splitlines() if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"]
