"""Condition trees for error rules: fields, JSON paths, operators, all/any groups.

A tree is ``{"kind": "group", "mode": "all"|"any", "items": [...]}`` whose items
are groups or ``{"kind": "cond", "field", "path", "op", "value", "values"}``.
JSON paths address the parsed upstream error body: ``$.error.code``,
``$['base_resp'].status_code``, ``$.errors[0].message``, ``$.items[*].code``,
``$..message`` (any depth).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Callable

FIELDS = ("status", "code", "message", "body", "provider", "channel", "channelType", "model", "phase", "path")
FIELD_LABELS = {
    "status": "状态码", "code": "错误码", "message": "错误消息", "body": "响应内容", "provider": "提供商",
    "channel": "渠道", "channelType": "账号类型", "model": "模型", "phase": "发生阶段", "path": "",
}
OP_LABELS = {
    "eq": "等于", "ne": "不等于", "in": "是其中之一", "not_in": "不是其中任何一个", "contains": "包含",
    "not_contains": "不包含", "starts_with": "开头是", "regex": "匹配正则", "exists": "存在", "not_exists": "不存在",
    "gt": "大于", "gte": "大于等于", "lt": "小于", "lte": "小于等于",
}
_NUMERIC_OPS = ("gt", "gte", "lt", "lte")
FIELD_OPS: dict[str, tuple[str, ...]] = {
    "status": ("eq", "ne", "in", "not_in", *_NUMERIC_OPS),
    "code": ("eq", "ne", "in", "not_in", "exists", "not_exists"),
    "message": ("contains", "not_contains", "eq", "ne", "starts_with", "regex"),
    "body": ("contains", "not_contains", "regex"),
    "provider": ("in", "not_in"),
    "channel": ("in", "not_in"),
    "channelType": ("eq", "ne"),
    "model": ("in", "not_in", "eq", "ne", "contains", "not_contains"),
    "phase": ("in", "not_in"),
    "path": ("eq", "ne", "in", "not_in", "contains", "not_contains", "starts_with", "regex", "exists",
             "not_exists", *_NUMERIC_OPS),
}
LIST_OPS = frozenset({"in", "not_in", "contains", "not_contains"})
NO_VALUE_OPS = frozenset({"exists", "not_exists"})
NEGATIVE_OPS = frozenset({"ne", "not_in", "not_contains"})
PHASE_VALUES = ("http", "stream", "transport")
PHASE_LABELS = {"http": "上游返回错误", "stream": "输出中报错", "transport": "连接或超时"}
CHANNEL_TYPE_LABELS = {"oauth": "OAuth 账号", "api": "API 渠道"}

MAX_DEPTH = 4
MAX_CONDITIONS = 40
MAX_VALUES = 100
MAX_VALUE_LEN = 300
MAX_PATH_LEN = 200
MAX_REGEX_LEN = 512
_NESTED_QUANTIFIER_RE = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d+,\d*\})")
_RANGE_RE = re.compile(r"^\s*(-?\d+)\s*[-~]\s*(-?\d+)\s*$")
_CLASS_RE = re.compile(r"^([1-5])xx$", re.IGNORECASE)


class ConditionError(ValueError):
    """A condition is invalid; the message is shown to the user."""


# ─── JSON paths ─────────────────────────────────────────────────────

_PATH_TOKEN_RE = re.compile(
    r"\.\.(?P<deep>[A-Za-z0-9_\-$]+|\*)"
    r"|\.(?P<key>[A-Za-z0-9_\-$]+|\*)"
    r"|\[\s*(?P<index>-?\d+|\*)\s*\]"
    r"|\[\s*'(?P<sq>[^']*)'\s*\]"
    r'|\[\s*"(?P<dq>[^"]*)"\s*\]'
)


def parse_path(path: str) -> tuple[tuple[str, Any], ...]:
    """``$.a[0]..b`` → (("key","a"), ("index",0), ("deep","b")); raises ConditionError."""
    text = (path or "").strip()
    if not text.startswith("$"):
        raise ConditionError(f"JSON 路径要以 $ 开头，例如 $.error.code（现在是「{text or '空'}」）")
    if len(text) > MAX_PATH_LEN:
        raise ConditionError(f"JSON 路径不能超过 {MAX_PATH_LEN} 个字符")
    out: list[tuple[str, Any]] = []
    pos = 1
    while pos < len(text):
        m = _PATH_TOKEN_RE.match(text, pos)
        if not m:
            raise ConditionError(f"JSON 路径「{text}」在第 {pos + 1} 个字符处无法识别")
        if m.group("deep") is not None:
            out.append(("deep", m.group("deep")))
        elif m.group("key") is not None:
            out.append(("key", m.group("key")))
        elif m.group("index") is not None:
            idx = m.group("index")
            out.append(("index", "*" if idx == "*" else int(idx)))
        else:
            out.append(("key", m.group("sq") if m.group("sq") is not None else m.group("dq")))
        pos = m.end()
    return tuple(out)


def _walk_deep(node: Any, name: str, out: list[Any], budget: list[int]) -> None:
    if budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(node, dict):
        for key, value in node.items():
            if name == "*" or key == name:
                out.append(value)
            _walk_deep(value, name, out, budget)
    elif isinstance(node, list):
        for value in node:
            if name == "*":
                out.append(value)
            _walk_deep(value, name, out, budget)


def resolve_path(tokens: tuple[tuple[str, Any], ...], data: Any) -> list[Any]:
    """Every value the path reaches; JSON nulls count as absent."""
    nodes = [data]
    for kind, arg in tokens:
        nxt: list[Any] = []
        for node in nodes:
            if kind == "key":
                if isinstance(node, dict):
                    if arg == "*":
                        nxt.extend(node.values())
                    elif arg in node:
                        nxt.append(node[arg])
            elif kind == "index":
                if isinstance(node, list):
                    if arg == "*":
                        nxt.extend(node)
                    elif -len(node) <= arg < len(node):
                        nxt.append(node[arg])
                elif isinstance(node, dict) and arg == "*":
                    nxt.extend(node.values())
            else:
                _walk_deep(node, arg, nxt, [5000])
        nodes = nxt[:500]
        if not nodes:
            break
    return [n for n in nodes if n is not None]


def value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ─── normalization ──────────────────────────────────────────────────


def _check_regex(pattern: str) -> None:
    if len(pattern) > MAX_REGEX_LEN:
        raise ConditionError(f"正则表达式不能超过 {MAX_REGEX_LEN} 个字符")
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise ConditionError("正则表达式含有嵌套重复（如 (a+)+），可能导致匹配卡死，请改写")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConditionError(f"「{pattern}」不是有效的正则表达式：{exc}") from exc


def _number_spec(token: str, field: str) -> None:
    if _RANGE_RE.match(token) or _CLASS_RE.match(token) or re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return
    raise ConditionError(f"{field}只能填数字、区间（如 500-599）或 5xx，无法识别「{token}」")


def _values(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ConditionError("条件的值必须是文本或列表")
    out: list[str] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ConditionError("条件的值只能是文本或数字")
        text = str(item).strip()
        if not text or text in out:
            continue
        if len(text) > MAX_VALUE_LEN:
            raise ConditionError(f"条件的值不能超过 {MAX_VALUE_LEN} 个字符")
        out.append(text)
    if len(out) > MAX_VALUES:
        raise ConditionError(f"一个条件最多 {MAX_VALUES} 个值")
    return out


def _normalize_cond(raw: dict[str, Any]) -> dict[str, Any]:
    field = raw.get("field")
    if field not in FIELDS:
        raise ConditionError("条件的字段无效")
    op = raw.get("op")
    if op not in FIELD_OPS[field]:
        raise ConditionError(f"「{FIELD_LABELS[field] or 'JSON 路径'}」不支持「{OP_LABELS.get(op, op)}」")
    path = ""
    if field == "path":
        path = str(raw.get("path") or "").strip()
        parse_path(path)
    label = FIELD_LABELS[field] or path
    values = _values(raw.get("values"))
    if raw.get("value") not in (None, "") and not values:
        values = _values(raw.get("value"))
    if op in NO_VALUE_OPS:
        values = []
    elif op in LIST_OPS:
        if not values:
            raise ConditionError(f"「{label} {OP_LABELS[op]}」还没有填值")
    else:
        if len(values) != 1:
            raise ConditionError(f"「{label} {OP_LABELS[op]}」需要填一个值")
    if op == "regex":
        _check_regex(values[0])
    if op in _NUMERIC_OPS:
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", values[0]):
            raise ConditionError(f"「{label} {OP_LABELS[op]}」的值要是数字")
    if field == "status":
        for v in values:
            _number_spec(v, "状态码")
    if field == "phase" and any(v not in PHASE_VALUES for v in values):
        raise ConditionError("发生阶段的取值无效")
    if field == "channelType" and any(v not in CHANNEL_TYPE_LABELS for v in values):
        raise ConditionError("账号类型只能是 OAuth 账号或 API 渠道")
    if field == "provider":
        values = [v.lower() for v in values]
    out: dict[str, Any] = {"kind": "cond", "field": field, "op": op, "path": path, "values": values}
    return out


def normalize_tree(raw: Any, *, depth: int = 0, counter: list[int] | None = None) -> dict[str, Any]:
    counter = counter if counter is not None else [0]
    if not isinstance(raw, dict):
        raise ConditionError("条件格式不正确")
    if raw.get("kind", "group") != "group":
        raise ConditionError("最外层必须是条件组")
    if depth >= MAX_DEPTH:
        raise ConditionError(f"条件组最多嵌套 {MAX_DEPTH} 层")
    mode = raw.get("mode", "all")
    if mode not in ("all", "any"):
        raise ConditionError("条件组只能是「全部满足」或「任一满足」")
    items_raw = raw.get("items") or []
    if not isinstance(items_raw, list):
        raise ConditionError("条件组格式不正确")
    items: list[dict[str, Any]] = []
    for item in items_raw:
        if not isinstance(item, dict):
            raise ConditionError("条件格式不正确")
        if item.get("kind") == "group":
            sub = normalize_tree(item, depth=depth + 1, counter=counter)
            if sub["items"]:
                items.append(sub)
        else:
            counter[0] += 1
            if counter[0] > MAX_CONDITIONS:
                raise ConditionError(f"一条规则最多 {MAX_CONDITIONS} 个条件")
            items.append(_normalize_cond(item))
    return {"kind": "group", "mode": mode, "items": items}


def count_conditions(tree: dict[str, Any]) -> int:
    return sum(count_conditions(i) if i["kind"] == "group" else 1 for i in tree["items"])


def fields_used(tree: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for item in tree["items"]:
        out |= fields_used(item) if item["kind"] == "group" else {item["field"]}
    return out


# ─── evaluation ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Subject:
    """What a condition can look at in one failed attempt."""
    status: int | None
    codes: tuple[str, ...]
    message: str
    body: str
    provider: str
    channel: str
    channel_type: str
    model: str
    phase: str
    data: Any


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _in_spec(value: Any, spec: str) -> bool:
    num = _num(value)
    if num is not None:
        m = _RANGE_RE.match(spec)
        if m:
            return int(m.group(1)) <= num <= int(m.group(2))
        m = _CLASS_RE.match(spec)
        if m:
            return int(m.group(1)) * 100 <= num <= int(m.group(1)) * 100 + 99
        target = _num(spec)
        if target is not None:
            return num == target
    return value_text(value).strip().lower() == spec.strip().lower()


def _equals(value: Any, target: str, wildcard: bool) -> bool:
    if wildcard and any(ch in target for ch in "*?["):
        return fnmatchcase(value_text(value).lower(), target.lower())
    return _in_spec(value, target)


def _compiled_regex(cache: dict[str, re.Pattern], pattern: str) -> re.Pattern:
    found = cache.get(pattern)
    if found is None:
        found = cache[pattern] = re.compile(pattern, re.IGNORECASE)
    return found


def field_values(c: dict[str, Any], s: Subject) -> list[Any]:
    field = c["field"]
    if field == "status":
        return [s.status] if s.status is not None else []
    if field == "code":
        return list(s.codes)
    if field == "message":
        return [s.message]
    if field == "body":
        return [s.body]
    if field == "path":
        return resolve_path(parse_path(c["path"]), s.data) if s.data is not None else []
    value = {"provider": s.provider, "channel": s.channel, "channelType": s.channel_type,
             "model": s.model, "phase": s.phase}[field]
    return [value] if value else []


def test_cond(c: dict[str, Any], s: Subject, cache: dict[str, re.Pattern] | None = None) -> tuple[bool, list[Any]]:
    cache = cache if cache is not None else {}
    op = c["op"]
    values = field_values(c, s)
    targets = c["values"]
    wildcard = c["field"] == "model"
    if op == "exists":
        return bool(values), values
    if op == "not_exists":
        return not values, values
    if not values:
        # Nothing to compare: only a negative test on free text (message/body are always present) holds.
        return False, values
    texts = [value_text(v).lower() for v in values]
    if op in ("eq", "in"):
        ok = any(_equals(v, t, wildcard) for v in values for t in targets)
    elif op in ("ne", "not_in"):
        ok = not any(_equals(v, t, wildcard) for v in values for t in targets)
    elif op == "contains":
        ok = any(t.lower() in text for text in texts for t in targets)
    elif op == "not_contains":
        ok = not any(t.lower() in text for text in texts for t in targets)
    elif op == "starts_with":
        ok = any(text.startswith(targets[0].lower()) for text in texts)
    elif op == "regex":
        rx = _compiled_regex(cache, targets[0])
        ok = any(rx.search(value_text(v)) for v in values)
    else:
        target = float(targets[0])
        nums = [n for n in (_num(v) for v in values) if n is not None]
        compare: Callable[[float], bool] = {
            "gt": lambda n: n > target, "gte": lambda n: n >= target,
            "lt": lambda n: n < target, "lte": lambda n: n <= target,
        }[op]
        ok = any(compare(n) for n in nums)
    return ok, values


def matches(tree: dict[str, Any], s: Subject, cache: dict[str, re.Pattern] | None = None) -> bool:
    cache = cache if cache is not None else {}
    results = (matches(i, s, cache) if i["kind"] == "group" else test_cond(i, s, cache)[0] for i in tree["items"])
    if not tree["items"]:
        return False
    return all(results) if tree["mode"] == "all" else any(results)


# ─── explaining ─────────────────────────────────────────────────────

_VALUE_LABELS: dict[str, Callable[[str], str]] = {
    "phase": lambda v: PHASE_LABELS.get(v, v),
    "channelType": lambda v: CHANNEL_TYPE_LABELS.get(v, v),
}


def describe_cond(c: dict[str, Any], *, provider_label: Callable[[str], str] = str,
                  channel_label: Callable[[str], str] = str) -> str:
    field = c["field"]
    label = FIELD_LABELS[field] or c["path"]
    if c["op"] in NO_VALUE_OPS:
        return f"{label} {OP_LABELS[c['op']]}"
    shown = {"provider": provider_label, "channel": channel_label}.get(field) or _VALUE_LABELS.get(field) or str
    joiner = "、" if c["op"] in ("in", "not_in") else " 或 "
    values = [shown(v) for v in c["values"][:4]]
    more = f" 等 {len(c['values'])} 项" if len(c["values"]) > 4 else ""
    quoted = [v if re.fullmatch(r"-?\d+(?:\.\d+)?|\d+-\d+|[1-5]xx", v) else f"“{v}”" for v in values] \
        if field in ("message", "body", "path", "model", "code") else values
    return f"{label} {OP_LABELS[c['op']]} {joiner.join(quoted)}{more}"


def _actual(c: dict[str, Any], values: list[Any], *, provider_label: Callable[[str], str],
            channel_label: Callable[[str], str]) -> str:
    if not values:
        return "取不到这个值" if c["field"] in ("path", "code", "status") else "为空"
    shown = {"provider": provider_label, "channel": channel_label}.get(c["field"]) or _VALUE_LABELS.get(c["field"])
    texts = [shown(str(v)) if shown else value_text(v) for v in values[:3]]
    text = "、".join(t if len(t) <= 60 else t[:60] + "…" for t in texts)
    if c["field"] in ("message", "body"):
        return ""
    return f"实际是 {text}"


def explain_miss(tree: dict[str, Any], s: Subject, *, provider_label: Callable[[str], str] = str,
                 channel_label: Callable[[str], str] = str) -> str | None:
    """None when the tree matches; otherwise why not, in words."""
    cache: dict[str, re.Pattern] = {}

    def walk(node: dict[str, Any]) -> str | None:
        reasons: list[str] = []
        for item in node["items"]:
            if item["kind"] == "group":
                reason = walk(item)
            else:
                ok, values = test_cond(item, s, cache)
                if ok:
                    reason = None
                else:
                    actual = _actual(item, values, provider_label=provider_label, channel_label=channel_label)
                    desc = describe_cond(item, provider_label=provider_label, channel_label=channel_label)
                    reason = f"「{desc}」不成立" + (f"（{actual}）" if actual else "")
            if node["mode"] == "all" and reason:
                return reason
            if node["mode"] == "any":
                if reason is None:
                    return None
                reasons.append(reason)
        if node["mode"] == "any":
            if not reasons:
                return "条件组是空的"
            return reasons[0] if len(reasons) == 1 else "以下条件都不成立：" + "；".join(reasons[:3])
        return None

    return walk(tree)
