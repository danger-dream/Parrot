"""Sanitize JSON schemas before they reach Antigravity / Cloud Code.

CPA's cleaner is large because it hit production 400s from Claude VALIDATED
mode and proto-JSON. This is the subset that actually prevents those failures:

- drop keywords Gemini rejects (title/const/$ref/anyOf leftovers, constraints)
- flatten anyOf/oneOf and merge allOf
- turn const into a description hint
- drop enums on tool schemas (Antigravity does not enforce them)
- Claude VALIDATED empty objects get a required placeholder

Only call this on a schema document, never on a whole request. Running it over
functionCall arguments rewrites ordinary data keys such as ``title``.
"""

from __future__ import annotations

from typing import Any


PLACEHOLDER_REASON = "Brief explanation of why you are calling this tool"

_UNSUPPORTED = {
    "$schema",
    "$defs",
    "definitions",
    "const",
    "$ref",
    "$id",
    "additionalProperties",
    "propertyNames",
    "patternProperties",
    "if",
    "then",
    "else",
    "not",
    "$comment",
    "enumDescriptions",
    "enumTitles",
    "prefill",
    "deprecated",
    "encrypted",
    "minLength",
    "maxLength",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "format",
    "default",
    "examples",
    "minimum",
    "maximum",
    "multipleOf",
}

_KEEP = {
    "type",
    "properties",
    "items",
    "required",
    "description",
    "enum",
    "nullable",
    "anyOf",
    "oneOf",
    "allOf",
    "title",
}


def uses_antigravity_schema(model: str) -> bool:
    name = str(model or "").lower()
    return "claude" in name or "gemini-3-pro" in name or "gemini-3.1-pro" in name


def clean_tool_schema(schema: Any, *, require_placeholder: bool = False) -> Any:
    cleaned = _clean(schema, drop_enums=True, require_placeholder=require_placeholder, top=True)
    return cleaned if isinstance(cleaned, dict) else {"type": "object"}


def clean_response_schema(schema: Any) -> Any:
    return _clean(schema, drop_enums=False, require_placeholder=False, top=True)


def _clean(node: Any, *, drop_enums: bool, require_placeholder: bool, top: bool) -> Any:
    if not isinstance(node, dict):
        return node
    node = dict(node)

    if isinstance(node.get("allOf"), list):
        node = _merge_all_of(node, drop_enums=drop_enums, require_placeholder=require_placeholder)
    if isinstance(node.get("anyOf"), list) or isinstance(node.get("oneOf"), list):
        node = _flatten_union(node, drop_enums=drop_enums, require_placeholder=require_placeholder)

    const = node.pop("const", None)
    if const is not None:
        node["description"] = _join_hint(node.get("description"), f"const: {const}")

    props = node.get("properties")
    if isinstance(props, dict):
        node["properties"] = {
            key: _clean(value, drop_enums=drop_enums, require_placeholder=require_placeholder, top=False)
            for key, value in props.items()
        }

    items = node.get("items")
    if isinstance(items, dict):
        node["items"] = _clean(items, drop_enums=drop_enums, require_placeholder=require_placeholder, top=False)
    elif isinstance(items, list):
        node["items"] = [
            _clean(item, drop_enums=drop_enums, require_placeholder=require_placeholder, top=False)
            for item in items
        ]

    hints: list[str] = []
    for key in list(node.keys()):
        if key in _KEEP:
            continue
        if key.startswith("x-") or key in _UNSUPPORTED:
            value = node.pop(key)
            if key in {"minLength", "maxLength", "pattern", "format", "minimum", "maximum", "default"}:
                hints.append(f"{key}: {value}")
    if hints:
        node["description"] = _join_hint(node.get("description"), "; ".join(hints))

    if drop_enums and isinstance(node.get("enum"), list):
        values = [str(item) for item in node.pop("enum")]
        if values:
            node["description"] = _join_hint(node.get("description"), "Allowed: " + ", ".join(values))
    elif (not drop_enums) and node.get("type") == "boolean":
        node.pop("enum", None)

    required = node.get("required")
    if isinstance(required, list) and isinstance(node.get("properties"), dict):
        valid = [name for name in required if isinstance(name, str) and name in node["properties"]]
        if valid:
            node["required"] = valid
        else:
            node.pop("required", None)

    if require_placeholder:
        node = _ensure_placeholder(node, top=top)
    return node


def _merge_all_of(node: dict, *, drop_enums: bool, require_placeholder: bool) -> dict:
    merged = dict(node)
    branches = merged.pop("allOf")
    properties: dict[str, Any] = dict(merged.get("properties") or {})
    required: list[str] = list(merged.get("required") or [])
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        cleaned = _clean(branch, drop_enums=drop_enums, require_placeholder=False, top=False)
        if not isinstance(cleaned, dict):
            continue
        child_props = cleaned.get("properties")
        if isinstance(child_props, dict):
            properties.update(child_props)
        child_required = cleaned.get("required")
        if isinstance(child_required, list):
            for name in child_required:
                if isinstance(name, str) and name not in required:
                    required.append(name)
        if cleaned.get("description"):
            merged["description"] = _join_hint(merged.get("description"), cleaned["description"])
        if "type" not in merged and cleaned.get("type"):
            merged["type"] = cleaned["type"]
    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = required
    return merged


def _flatten_union(node: dict, *, drop_enums: bool, require_placeholder: bool) -> dict:
    key = "anyOf" if isinstance(node.get("anyOf"), list) else "oneOf"
    branches = list(node.get(key) or [])
    chosen: dict[str, Any] | None = None
    nullable = False
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        if branch.get("type") == "null":
            nullable = True
            continue
        chosen = _clean(branch, drop_enums=drop_enums, require_placeholder=False, top=False)
        if isinstance(chosen, dict):
            break
        chosen = None
    merged = {k: v for k, v in node.items() if k not in {"anyOf", "oneOf"}}
    if isinstance(chosen, dict):
        for field, value in chosen.items():
            merged.setdefault(field, value)
    if nullable:
        merged["nullable"] = True
    return merged


def _ensure_placeholder(node: dict, *, top: bool) -> dict:
    typ = node.get("type")
    if typ not in (None, "object"):
        return node
    props = node.get("properties")
    if not isinstance(props, dict):
        props = {}
    if not props:
        node["type"] = "object"
        node["properties"] = {
            "reason": {"type": "string", "description": PLACEHOLDER_REASON},
        }
        node["required"] = ["reason"]
        return node
    required = node.get("required")
    if isinstance(required, list) and required:
        return node
    if top:
        first = next(iter(props), None)
        if first:
            node["required"] = [first]
        return node
    if "_" not in props:
        props = dict(props)
        props["_"] = {"type": "boolean"}
        node["properties"] = props
    node["required"] = ["_"]
    return node


def _join_hint(existing: Any, hint: str) -> str:
    text = str(existing or "").strip()
    if not text:
        return hint
    if hint in text:
        return text
    return f"{text} ({hint})"
