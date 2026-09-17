"""Lossless search evidence envelope for native cross-protocol tool history.

Hosted calls stay server-side calls. The native source is retained in an explicit
extension so replay can reconstruct opaque provider fields rather than synthesize
client function arguments or tool results. Only search blocks use this codec.
"""
from __future__ import annotations
import copy

SOURCE = "parrot_native_search"


def responses_to_anthropic(item: dict) -> list[dict]:
    if item.get("type") != "web_search_call":
        return []
    saved = item.get(SOURCE)
    if isinstance(saved, list):
        return copy.deepcopy(saved)
    call_id = item.get("id") or item.get("call_id")
    if not call_id:
        return []
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    inputs = {k: copy.deepcopy(v) for k, v in action.items() if k not in ("type", "sources")}
    use = {"type": "server_tool_use", "id": call_id, "name": "web_search", "input": inputs, SOURCE: copy.deepcopy(item)}
    sources = []
    for source in action.get("sources") or []:
        if isinstance(source, dict) and source.get("url"):
            sources.append({**copy.deepcopy(source), "type": "web_search_result"})
    result = {"type": "web_search_tool_result", "tool_use_id": call_id, "content": sources}
    if item.get("status") == "failed" or item.get("error"):
        result["content"] = {"type": "web_search_tool_result_error", **copy.deepcopy(item.get("error") or {})}
    return [use, result]


def anthropic_to_responses(blocks: list) -> list[dict]:
    out = []
    by_id = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        typ = block.get("type")
        if typ == "server_tool_use" and block.get("name") == "web_search":
            if isinstance(block.get(SOURCE), dict):
                item = copy.deepcopy(block[SOURCE])
                item.pop(SOURCE, None)
            else:
                item = {"type": "web_search_call", "id": block.get("id"), "status": "in_progress", "action": {"type": "search", **copy.deepcopy(block.get("input") or {})}, SOURCE: [copy.deepcopy(block)]}
            by_id[block.get("id")] = item
            out.append(item)
        elif typ == "web_search_tool_result":
            item = by_id.get(block.get("tool_use_id"))
            if item is None:
                continue
            if SOURCE in item:
                item[SOURCE].append(copy.deepcopy(block))
            content = block.get("content")
            if isinstance(content, dict) and content.get("type") == "web_search_tool_result_error":
                item["status"] = "failed"
                item["error"] = copy.deepcopy(content)
            else:
                item["status"] = "completed"
                if SOURCE in item:
                    item["action"]["sources"] = [{**copy.deepcopy(s), "type": "url"} for s in content or [] if isinstance(s, dict) and s.get("url")]
    return out


def is_anthropic_search(block):
    return isinstance(block, dict) and (block.get("type") == "web_search_tool_result" or (block.get("type") == "server_tool_use" and block.get("name") == "web_search"))
