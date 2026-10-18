"""WorkBuddy's stateless, provider-tagged bridge payloads.

Anthropic thinking.signature and Responses reasoning.encrypted_content are the
existing opaque replay carriers. The tagged value below is a Parrot envelope,
NOT an Anthropic/OpenAI ciphertext or a manufactured WorkBuddy signature. Only
this provider unwraps it. No process/global conversation cache is involved.
"""
from __future__ import annotations

import base64
import copy
import json
import uuid

PREFIX = "workbuddy#v1:"
FIELDS = ("reasoning", "reasoning_content", "extra_fields")
CHAT_FIELDS = "_parrot_workbuddy_reasoning_fields"


def capture_chat_fields(body):
    # Chat ingress canonicalizes reasoning for generic providers. Keep the
    # original spelling beside its own message, not in an index/global cache.
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message.pop(CHAT_FIELDS, None)
        if message.get("role") == "assistant" and "reasoning" in message:
            message[CHAT_FIELDS] = fields(message)


def restore_chat_fields(payload):
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        value = message.pop(CHAT_FIELDS, None)
        if isinstance(value, dict):
            for key in FIELDS:
                message.pop(key, None)
            message.update(fields(value))


def merge_extra(old, new):
    """Official mergeExtraFields: one-level object merge; scalars replace."""
    result = copy.deepcopy(old) if isinstance(old, dict) else {}
    for key, value in (new or {}).items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = {**result[key], **copy.deepcopy(value)}
        else:
            result[key] = copy.deepcopy(value)
    return result


def fields(value):
    return {k: copy.deepcopy(value[k]) for k in FIELDS if k in value}


def update_fields(target, source):
    for key, value in fields(source).items():
        target[key] = merge_extra(target.get(key), value) if key == "extra_fields" else value


def accumulate(target, delta):
    for key in ("reasoning", "reasoning_content"):
        if isinstance(delta.get(key), str):
            target[key] = target.get(key, "") + delta[key]
    if isinstance(delta.get("extra_fields"), dict):
        target["extra_fields"] = merge_extra(target.get("extra_fields"), delta["extra_fields"])


def is_carrier(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encode(value):
    raw = json.dumps(fields(value), ensure_ascii=False, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode()


def decode(value):
    if not is_carrier(value):
        return None
    try:
        obj = json.loads(base64.b64decode(value[len(PREFIX):], altchars=b"-_", validate=True))
        if not isinstance(obj, dict) or set(obj) - set(FIELDS):
            raise ValueError()
        if any(k in obj and not isinstance(obj[k], str) for k in FIELDS[:2]):
            raise ValueError()
        if "extra_fields" in obj and not isinstance(obj["extra_fields"], dict):
            raise ValueError()
        return obj
    except (ValueError, TypeError, UnicodeError) as exc:
        from ..openai.transform.guard import GuardError
        raise GuardError(400, "invalid_request_error", "Invalid WorkBuddy reasoning replay carrier", param="input") from exc


def thinking_block(value):
    data = fields(value)
    if not data:
        return None
    return {"type": "thinking", "thinking": data.get("reasoning_content", data.get("reasoning", "")),
            "signature": encode(data)}


def response_item(value):
    data = fields(value)
    if not data:
        return None
    # This final item belongs to the preceding assistant turn (including a
    # tool-only turn). It is not pending reasoning for the following turn.
    text = data.get("reasoning_content", data.get("reasoning", ""))
    return {"type": "reasoning", "id": "rs_" + uuid.uuid4().hex[:24], "status": "completed",
            "summary": [{"type": "summary_text", "text": text}] if text else [],
            "encrypted_content": encode(data)}


def anthropic_messages(messages):
    from ..openai.transform.anthropic_to_chat import _convert_messages
    out = []
    for msg in messages:
        converted = _convert_messages([msg])
        replay = {}
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if not isinstance(block, dict):
                    continue
                data = decode(block.get("signature") if block.get("type") == "thinking" else block.get("data"))
                if data is not None:
                    update_fields(replay, data)
        if replay:
            if not converted:
                converted = [{"role": "assistant", "content": []}]
            converted[-1].update(replay)
        out.extend(converted)
    return out


def responses_messages(items):
    from ..openai.transform.responses_to_chat import _input_items_to_messages
    out, group, replay = [], [], {}

    def flush():
        nonlocal group, replay
        if not group and not replay:
            return
        converted = _input_items_to_messages(group)
        if replay:
            assistant = next((m for m in converted if m.get("role") == "assistant"), None)
            if assistant is None:
                assistant = {"role": "assistant", "content": []}
                converted.insert(0, assistant)
            # Do not retain the generic bridge's invented empty mirror.
            for key in FIELDS:
                assistant.pop(key, None)
            assistant.update(copy.deepcopy(replay))
        out.extend(converted)
        group, replay = [], {}

    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        data = decode(item.get("encrypted_content")) if kind == "reasoning" else None
        if data is not None:
            update_fields(replay, data)
            flush()
        elif kind == "reasoning":
            if any(i.get("type") in ("function_call", "custom_tool_call") or i.get("role") == "assistant" for i in group):
                flush()
            # Official agents-framework items may carry rawContent/providerData.
            pd = item.get("providerData") or {}
            update_fields(replay, pd)
            raw = item.get("rawContent") or item.get("content") or item.get("summary") or []
            text = "".join(p.get("text", "") for p in raw if isinstance(p, dict) and isinstance(p.get("text"), str))
            if text:
                replay["reasoning"] = text
        elif kind in ("function_call", "custom_tool_call") or item.get("role") == "assistant":
            if item.get("role") == "assistant" and any(i.get("role") == "assistant" for i in group):
                flush()
            group.append(item)
            update_fields(replay, item)
            pd = item.get("providerData")
            if isinstance(pd, dict):
                update_fields(replay, pd)
        else:
            flush()
            out.extend(_input_items_to_messages([item]))
    flush()
    return out


def strip_foreign(payload, protocol):
    """Never send our opaque carrier to another provider as a native signature."""
    # The fast path preserves other providers' ordinary payload objects.
    items = payload.get("input") if isinstance(payload.get("input"), list) else []
    messages = payload.get("messages") or []
    if not (any(isinstance(i, dict) and is_carrier(i.get("encrypted_content")) for i in items)
            or any(isinstance(m, dict) and (CHAT_FIELDS in m or (protocol == "openai-chat" and "extra_fields" in m) or any(isinstance(b, dict) and (
                is_carrier(b.get("signature")) or is_carrier(b.get("data")))
                for b in (m.get("content") if isinstance(m.get("content"), list) else []))) for m in messages)):
        return payload
    out = copy.deepcopy(payload)
    if protocol == "openai-responses" and isinstance(out.get("input"), list):
        removed = {i["id"] for i in out["input"] if isinstance(i, dict) and isinstance(i.get("id"), str) and is_carrier(i.get("encrypted_content"))}
        retained = {i.get("id") for i in out["input"] if isinstance(i, dict) and i.get("type") != "item_reference" and not is_carrier(i.get("encrypted_content"))}
        removed -= retained
        out["input"] = [i for i in out["input"] if not isinstance(i, dict) or not (
            is_carrier(i.get("encrypted_content")) or (i.get("type") == "item_reference" and i.get("id") in removed))]
    kept_messages = []
    for msg in out.get("messages") or []:
        if not isinstance(msg, dict):
            kept_messages.append(msg)
            continue
        msg.pop(CHAT_FIELDS, None)
        if protocol == "openai-chat":
            msg.pop("extra_fields", None)  # WorkBuddy message extension, not a global OpenAI field.
        if isinstance(msg.get("content"), list):
            old = msg["content"]
            msg["content"] = [b for b in old if not isinstance(b, dict) or not (
                is_carrier(b.get("signature")) or is_carrier(b.get("data")))]
            if old and not msg["content"] and msg.get("role") == "assistant" and not msg.get("tool_calls"):
                continue
        kept_messages.append(msg)
    if "messages" in out:
        out["messages"] = kept_messages
    return out
