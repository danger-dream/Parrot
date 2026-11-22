"""Configurable handling of failed upstream attempts.

One failed attempt becomes an :class:`ErrorObservation`. The first matching
rule -- user rules in list order, then enabled presets -- classifies it and
decides what failover does next: an ordered list of steps (wait and retry,
pause, deprioritize, disable, notify) ending in one destination (next channel,
answer the client, or hand over to later rules). Thresholds add actions once
the same rule has hit N times. When nothing matches, the built-in protocol
handlers and the default retry/cooldown policy run exactly as before.

Rules only choose from a fixed set of actions; they never execute code.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import threading
import time
from typing import Any

from . import error_rule_conditions as conditions
from .error_rule_catalog import (
    BUILTINS, CATEGORIES, CATEGORY_ORDER, PRESET_PREFIX, PRESET_SOURCES, default_handling, default_message,
)
from .error_rule_time import next_boundary, time_from_text, usage_window_end

# ─── vocabulary ─────────────────────────────────────────────────────

PHASES = conditions.PHASE_VALUES
CHANNEL_TYPES = ("oauth", "api")
STEP_TYPES = ("retry", "pause", "deprioritize", "disable", "notify")
SIDE_STEP_TYPES = ("pause", "deprioritize", "disable", "notify")
FINALS = ("next", "respond", "pass")
PAUSE_SCOPES = ("model", "account")
PAUSE_MODES = ("fixed", "message_time", "retry_after", "usage_window", "boundary", "ladder")
THRESHOLD_MODES = ("consecutive", "window")
THRESHOLD_SCOPES = ("model", "account", "rule")
RESPONSE_STATUSES = (0, 400, 402, 403, 404, 429, 500, 502, 503, 504)

MAX_RULES = 200
MAX_NAME_LEN = 120
MAX_NOTE_LEN = 500
MAX_STEPS = 10
MAX_THRESHOLDS = 5
MAX_REGEX_LEN = 512
MAX_TEMPLATE_LEN = 500
MAX_COOLDOWN_SECONDS = 30 * 86400
MAX_RESET_AHEAD_MS = 45 * 86400 * 1000
BODY_EXCERPT = 4096

_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TZ_RE = re.compile(r"^(?:UTC|[+-]\d{2}:\d{2})$")

# Outcomes produced by Parrot itself or by the client never describe the upstream.
LOCAL_OUTCOMES = frozenset({
    "guard_error", "candidate_guard", "transform_error", "client_disconnected",
    "cancelled", "queue_timeout", "blacklist_hit",
})
TRANSPORT_OUTCOMES = frozenset({
    "connection_timeout", "upload_timeout", "first_byte_timeout", "idle_timeout",
    "total_timeout", "http_connect_timeout", "connect_timeout", "pool_timeout",
    "write_timeout", "read_timeout", "transport_timeout", "connect_error",
    "proxy_connect_error", "transport_error", "connection_lifecycle",
    "upstream_closed", "closed_before_first_byte", "stream_truncated",
})
STREAM_OUTCOMES = frozenset({"stream_upstream_error", "upstream_error_json"})


class ErrorRuleValidationError(ValueError):
    """A rule or errorRules settings payload is invalid (message is user-facing)."""


DEFAULT_SETTINGS: dict[str, Any] = {"enabled": True, "rules": [], "presets": {}}

# ─── normalization ──────────────────────────────────────────────────


def _str(raw: Any, field: str, max_len: int, *, default: str = "") -> str:
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise ErrorRuleValidationError(f"{field} 必须是文本")
    value = raw.strip()
    if len(value) > max_len:
        raise ErrorRuleValidationError(f"{field} 不能超过 {max_len} 个字符")
    return value


def _int(raw: Any, field: str, default: int, lo: int, hi: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or (isinstance(raw, float) and not raw.is_integer()):
        raise ErrorRuleValidationError(f"{field} 必须是整数")
    value = int(raw)
    if not lo <= value <= hi:
        raise ErrorRuleValidationError(f"{field} 需要在 {lo}–{hi} 之间")
    return value


def _bool(raw: Any, field: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ErrorRuleValidationError(f"{field} 必须是开关值")
    return raw


def _choice(raw: Any, field: str, choices: tuple, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if raw not in choices:
        raise ErrorRuleValidationError(f"{field} 的取值无效")
    return raw


def _normalize_step(raw: Any, *, allow_retry: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ErrorRuleValidationError("处理步骤格式不正确")
    kind = raw.get("type")
    if kind not in STEP_TYPES or (kind == "retry" and not allow_retry):
        raise ErrorRuleValidationError("处理步骤的类型无效" if kind in STEP_TYPES or kind is None else f"不认识的处理步骤「{kind}」")
    if kind == "retry":
        return {"type": "retry", "delaySeconds": _int(raw.get("delaySeconds"), "重试前等待", 2, 0, 300),
                "times": _int(raw.get("times"), "重试次数", 1, 1, 5)}
    if kind == "pause":
        step = {
            "type": "pause",
            "scope": _choice(raw.get("scope"), "暂停范围", PAUSE_SCOPES, "model"),
            "mode": _choice(raw.get("mode"), "暂停时长方式", PAUSE_MODES, "fixed"),
            "seconds": _int(raw.get("seconds"), "暂停时长", 900, 1, MAX_COOLDOWN_SECONDS),
            "pattern": _str(raw.get("pattern"), "恢复时间提取规则", MAX_REGEX_LEN),
            "timezone": _str(raw.get("timezone"), "时区", 6, default="+08:00") or "+08:00",
            "boundaryHours": _int(raw.get("boundaryHours"), "周期小时数", 0, 0, 168),
            "fallbackSeconds": _int(raw.get("fallbackSeconds"), "保底暂停时长", 900, 0, MAX_COOLDOWN_SECONDS),
        }
        if not _TZ_RE.match(step["timezone"]):
            raise ErrorRuleValidationError("时区格式应为 +08:00 这样的偏移，或 UTC")
        if step["pattern"]:
            try:
                conditions._check_regex(step["pattern"])
            except conditions.ConditionError as exc:
                raise ErrorRuleValidationError(f"恢复时间提取规则：{exc}") from exc
        if step["mode"] == "boundary" and step["boundaryHours"] < 1:
            raise ErrorRuleValidationError("「到下一个周期边界」需要填写周期小时数")
        if step["scope"] == "account":
            # The cooldown ladder is per model; an account pause always needs an end time.
            if step["mode"] == "ladder":
                raise ErrorRuleValidationError("暂停整个账号不能用冷却阶梯，请选一种有明确结束时间的方式")
            open_ended = step["mode"] in ("message_time", "retry_after") or (
                step["mode"] == "usage_window" and not step["boundaryHours"])
            if open_ended and not step["fallbackSeconds"]:
                raise ErrorRuleValidationError("暂停整个账号时，「读不到时间时」要填一个保底时长")
        return step
    if kind == "disable":
        return {"type": "disable", "reason": _str(raw.get("reason"), "停用原因", 40)}
    if kind == "notify":
        return {"type": "notify", "repeatHours": _int(raw.get("repeatHours"), "提醒间隔", 6, 1, 168)}
    return {"type": "deprioritize"}


def _normalize_steps(raw: Any, *, allow_retry: bool, field: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ErrorRuleValidationError(f"{field}必须是列表")
    if len(raw) > MAX_STEPS:
        raise ErrorRuleValidationError(f"{field}最多 {MAX_STEPS} 步")
    return [_normalize_step(s, allow_retry=allow_retry) for s in raw]


def _normalize_threshold(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ErrorRuleValidationError("累计触发格式不正确")
    scope = _choice(raw.get("scope"), "累计范围", THRESHOLD_SCOPES, "model")
    mode = _choice(raw.get("mode"), "累计方式", THRESHOLD_MODES, "consecutive")
    if scope == "rule" and mode == "consecutive":
        raise ErrorRuleValidationError("「所有渠道合计」只能按时间窗口累计（中间的成功无法对应到同一个渠道）")
    steps = _normalize_steps(raw.get("steps"), allow_retry=False, field="累计触发的动作")
    if not steps:
        raise ErrorRuleValidationError("累计触发至少要有一个动作")
    return {
        "count": _int(raw.get("count"), "累计次数", 3, 2, 1000),
        "mode": mode,
        "windowMinutes": _int(raw.get("windowMinutes"), "时间窗口", 10, 1, 7 * 24 * 60),
        "scope": scope,
        "steps": steps,
    }


def normalize_rule(raw: Any, *, preset: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ErrorRuleValidationError("规则格式不正确")
    rid = _str(raw.get("id"), "规则 ID", 64)
    if not rid or not _RULE_ID_RE.match(rid):
        raise ErrorRuleValidationError("规则 ID 只能包含字母、数字、下划线、点和短横线")
    if rid.startswith(PRESET_PREFIX) and not preset:
        raise ErrorRuleValidationError("以 preset. 开头的 ID 保留给预置规则")
    name = _str(raw.get("name"), "规则名称", MAX_NAME_LEN)
    if not name:
        raise ErrorRuleValidationError("请填写规则名称")
    category = _choice(raw.get("category"), "错误类型", CATEGORY_ORDER, None)
    if category is None:
        raise ErrorRuleValidationError("请选择错误类型")
    try:
        when = conditions.normalize_tree(raw.get("when") or {"kind": "group", "mode": "all", "items": []})
    except conditions.ConditionError as exc:
        raise ErrorRuleValidationError(str(exc)) from exc
    if not conditions.count_conditions(when):
        raise ErrorRuleValidationError("请至少设置一个条件，否则这条规则会命中所有错误")
    base = default_handling(category)
    steps = _normalize_steps(raw.get("steps") if "steps" in raw else base["steps"], allow_retry=True, field="处理步骤")
    thresholds_raw = raw.get("thresholds") if "thresholds" in raw else base["thresholds"]
    if not isinstance(thresholds_raw, list) or len(thresholds_raw) > MAX_THRESHOLDS:
        raise ErrorRuleValidationError(f"累计触发最多 {MAX_THRESHOLDS} 条")
    rs_raw = raw.get("response") if isinstance(raw.get("response"), dict) else {}
    rs = {**base["response"], **{k: v for k, v in rs_raw.items() if v is not None}}
    status = _int(rs.get("status"), "返回状态码", 0, 0, 599)
    if status not in RESPONSE_STATUSES:
        raise ErrorRuleValidationError("返回状态码只能选择列表中的值")
    rule = {
        "id": rid,
        "name": name,
        "enabled": _bool(raw.get("enabled"), "启用", True),
        "note": _str(raw.get("note"), "备注", MAX_NOTE_LEN),
        "when": when,
        "category": category,
        "steps": steps,
        "final": _choice(raw.get("final"), "最后的去向", FINALS, base["final"]),
        "thresholds": [_normalize_threshold(t) for t in thresholds_raw],
        "response": {
            "status": status,
            "message": _str(rs.get("message"), "提示文案", MAX_TEMPLATE_LEN),
            "retryAfter": _bool(rs.get("retryAfter"), "附带 Retry-After", False),
        },
    }
    if preset:
        rule["group"] = str(raw.get("group") or "")
    return rule


def normalize_settings(raw: Any, *, tolerant: bool = False) -> dict[str, Any]:
    """Validated settings; ``tolerant`` skips invalid or duplicate rules instead of raising."""
    if raw is None:
        return copy.deepcopy(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        raise ErrorRuleValidationError("errorRules 必须是对象")
    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ErrorRuleValidationError("errorRules.rules 必须是列表")
    if len(rules_raw) > MAX_RULES:
        raise ErrorRuleValidationError(f"最多 {MAX_RULES} 条规则")
    rules, seen = [], set()
    for item in rules_raw:
        try:
            rule = normalize_rule(item)
            if rule["id"] in seen:
                raise ErrorRuleValidationError(f"规则 ID 重复：{rule['id']}")
        except ErrorRuleValidationError as exc:
            if not tolerant:
                name = item.get("name") or item.get("id") if isinstance(item, dict) else None
                raise ErrorRuleValidationError(f"规则「{name or '?'}」：{exc}") from exc
            continue
        seen.add(rule["id"])
        rules.append(rule)
    presets_raw = raw.get("presets") or {}
    if not isinstance(presets_raw, dict):
        raise ErrorRuleValidationError("errorRules.presets 必须是对象")
    presets = {}
    for pid, state in presets_raw.items():
        if pid in PRESET_IDS and isinstance(state, dict) and isinstance(state.get("enabled"), bool):
            presets[pid] = {"enabled": state["enabled"]}
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        if not tolerant:
            raise ErrorRuleValidationError("总开关 必须是开关值")
        enabled = True
    return {"enabled": enabled, "rules": rules, "presets": presets}


def split_settings(raw: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Valid settings, plus each rule the runtime skips as ``{"index", "rule", "error"}``.

    Only the rules are judged one by one; a malformed container still raises.
    """
    if raw is None:
        return copy.deepcopy(DEFAULT_SETTINGS), []
    if not isinstance(raw, dict):
        raise ErrorRuleValidationError("errorRules 必须是对象")
    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ErrorRuleValidationError("errorRules.rules 必须是列表")
    if len(rules_raw) > MAX_RULES:
        raise ErrorRuleValidationError(f"最多 {MAX_RULES} 条规则")
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(rules_raw):
        try:
            rule = normalize_rule(item)
            if rule["id"] in seen:
                raise ErrorRuleValidationError(f"规则 ID 重复：{rule['id']}")
        except ErrorRuleValidationError as exc:
            skipped.append({"index": index, "rule": item, "error": str(exc)})
            continue
        seen.add(rule["id"])
        kept.append(rule)
    return normalize_settings({**raw, "rules": kept}, tolerant=True), skipped


PRESETS: tuple[dict[str, Any], ...] = tuple(normalize_rule(p, preset=True) for p in PRESET_SOURCES)
PRESET_IDS = tuple(p["id"] for p in PRESETS)


def preset_rules(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Presets with the operator's on/off state applied."""
    states = settings.get("presets") or {}
    out = []
    for preset in PRESETS:
        rule = copy.deepcopy(preset)
        rule["enabled"] = bool((states.get(rule["id"]) or {}).get("enabled", True))
        out.append(rule)
    return out


# ─── observation ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ErrorObservation:
    phase: str
    status: int | None
    outcome: str
    provider: str
    channel_key: str
    model: str
    codes: tuple[str, ...]
    message: str
    text: str
    retry_after: float | None = None
    channel_type: str = ""
    data: Any = None


def provider_of(channel: Any) -> str:
    provider = str(getattr(channel, "provider_id", "") or "").strip().lower()
    if provider:
        return provider
    if getattr(channel, "type", "") == "oauth":
        # Claude and OpenAI OAuth channels carry no provider attribute; their key does.
        key = str(getattr(channel, "key", "") or "").split(":")
        fallback = key[1] if len(key) > 2 and key[0] == "oauth" else ""
        return str(getattr(channel, "provider", "") or fallback).strip().lower()
    from .quota_errors import is_zhipu_channel
    if is_zhipu_channel(channel):
        return "zhipu"
    base = str(getattr(channel, "base_url", "") or "").lower()
    if "minimax" in base:
        return "minimax"
    return str(getattr(channel, "provider", "") or "").strip().lower()


_BRACKET_CODE_RE = re.compile(r"[(\[（【]\s*(\d{3,6})\s*[)\]）】]")


_JSON_DECODER = json.JSONDecoder()


def _json_object(text: str) -> dict | None:
    """The first JSON object in ``text``; text around it (prefixes, request ids) is ignored."""
    start = text.find("{")
    for _ in range(8):
        if start < 0:
            return None
        try:
            obj, _end = _JSON_DECODER.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _codes_and_message(obj: dict | None) -> tuple[list[str], str]:
    if not obj:
        return [], ""
    codes: list[str] = []
    message = ""
    layers = [obj]
    error = obj.get("error")
    if isinstance(error, dict):
        layers.append(error)
        data = error.get("data")
        if isinstance(data, dict):
            layers.append(data)
    base_resp = obj.get("base_resp")
    if isinstance(base_resp, dict):
        layers.append({"code": base_resp.get("status_code"), "message": base_resp.get("status_msg")})
    for layer in layers:
        for key in ("code", "type", "status", "errcode", "error_code"):
            value = layer.get(key)
            if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
                continue
            text = str(value).strip()
            if text and text.lower() != "error" and text not in codes:
                codes.append(text)
        if not message:
            for key in ("message", "msg", "error_msg", "detail"):
                value = layer.get(key)
                if isinstance(value, str) and value.strip():
                    message = value.strip()
                    break
    if not message and isinstance(error, str):
        message = error
    # Providers often embed the business code in the text: "...上限 (2067)", "[1308][...]".
    for code in _BRACKET_CODE_RE.findall(message):
        if code not in codes:
            codes.append(code)
    return codes, message


def observation_from_parts(*, phase: str, status: int | None, outcome: str, provider: str,
                           channel_key: str, model: str, body: str, channel_type: str = "",
                           error_code: str | None = None, retry_after: float | None = None) -> ErrorObservation:
    raw = str(body or "")
    if raw.startswith("HTTP ") and ":" in raw[:12]:
        raw = raw.split(":", 1)[1].strip()
    excerpt = raw[:BODY_EXCERPT]
    # raw_decode stops at the end of the first object, so the whole body costs no more than that object.
    obj = _json_object(raw)
    codes, message = _codes_and_message(obj)
    if obj is None:
        # A plain-text error can still carry the business code: "[1311][...]", "... (2067)".
        for code in _BRACKET_CODE_RE.findall(excerpt):
            if code not in codes:
                codes.append(code)
    if error_code and str(error_code) not in codes:
        codes.insert(0, str(error_code))
    decoded = json.dumps(obj, ensure_ascii=False)[:BODY_EXCERPT] if obj else excerpt
    return ErrorObservation(
        phase=phase, status=status, outcome=outcome or "", provider=(provider or "").lower(),
        channel_key=channel_key or "", model=model or "",
        codes=tuple(c.lower() for c in codes),
        message=(message or excerpt[:300]).strip(),
        text=(message + "\n" + decoded) if message else decoded,
        retry_after=retry_after,
        channel_type=channel_type or "",
        data=obj,
    )


def phase_of(outcome: str, status: int | None) -> str:
    """Where a failure happened: no upstream answer, an error inside a 200 stream, or an HTTP error."""
    if outcome in TRANSPORT_OUTCOMES and not status:
        return "transport"
    if outcome in STREAM_OUTCOMES or status == 200:
        return "stream"
    return "http"


def observe(channel: Any, model: str, result: Any) -> ErrorObservation | None:
    """Describe one failed, uncommitted upstream attempt; None for local failures."""
    if getattr(result, "success", False) or getattr(result, "stream_started", False):
        return None
    outcome = str(getattr(result, "outcome", "") or "")
    if outcome in LOCAL_OUTCOMES:
        return None
    status = getattr(result, "http_status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    phase = phase_of(outcome, status)
    body = getattr(result, "full_response_text", None) or getattr(result, "error_detail", None) or ""
    return observation_from_parts(
        phase=phase, status=status, outcome=outcome, provider=provider_of(channel),
        channel_key=str(getattr(channel, "key", "") or ""), model=model, body=str(body),
        channel_type=str(getattr(channel, "type", "") or ""),
        error_code=getattr(result, "error_code", None),
        retry_after=getattr(result, "retry_after_seconds", None),
    )


def subject_of(obs: ErrorObservation) -> conditions.Subject:
    return conditions.Subject(
        status=obs.status, codes=obs.codes, message=obs.message, body=obs.text, provider=obs.provider,
        channel=obs.channel_key, channel_type=obs.channel_type, model=obs.model, phase=obs.phase, data=obs.data,
    )


# ─── matching ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuleMatch:
    rule: dict[str, Any]
    layer: str  # "user" | "preset"

    @property
    def id(self) -> str:
        return self.rule["id"]

    @property
    def category(self) -> str:
        return self.rule["category"]

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.rule["steps"]

    @property
    def final(self) -> str:
        return self.rule["final"]

    @property
    def passthrough(self) -> bool:
        return self.rule["final"] == "pass"

    def pause_steps(self) -> list[dict[str, Any]]:
        """Every pause the rule can record, including those its thresholds fire."""
        steps = list(self.steps)
        for threshold in self.rule["thresholds"]:
            steps += threshold["steps"]
        return [s for s in steps if s["type"] == "pause"]


_PROVIDER_LABELS = {
    "claude": "Claude", "openai": "OpenAI", "xai": "Grok", "cursor": "Cursor", "antigravity": "Antigravity",
    "google-ai-studio": "Google AI Studio", "workbuddy": "WorkBuddy", "zhipu": "智谱", "qoder": "Qoder",
    "kimi": "Kimi", "deepseek": "DeepSeek", "minimax": "MiniMax",
}


def provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider, provider)


def _rule_matches(rule: dict[str, Any], subject: conditions.Subject) -> bool:
    return conditions.matches(rule["when"], subject)


def _compile_settings(settings: dict[str, Any]) -> tuple[RuleMatch, ...]:
    compiled: list[RuleMatch] = []
    for rule in settings.get("rules") or []:
        try:
            norm = normalize_rule(rule)
        except ErrorRuleValidationError as exc:
            print(f"[error_rules] skipping invalid rule {rule.get('id') if isinstance(rule, dict) else '?'}: {exc}")
            continue
        if norm.get("enabled", True):
            compiled.append(RuleMatch(norm, "user"))
    for preset in preset_rules(settings):
        if preset["enabled"]:
            compiled.append(RuleMatch(preset, "preset"))
    return tuple(compiled)


_cache_lock = threading.Lock()
_cache_key: str | None = None
_cache_value: tuple[bool, tuple[RuleMatch, ...]] = (False, ())


def _live() -> tuple[bool, tuple[RuleMatch, ...]]:
    global _cache_key, _cache_value
    from . import config as config_module
    raw = config_module.get().get("errorRules")
    raw = raw if isinstance(raw, dict) else {}
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    with _cache_lock:
        if digest == _cache_key:
            return _cache_value
    enabled = raw.get("enabled", True) is not False
    settings = {"rules": raw.get("rules") if isinstance(raw.get("rules"), list) else [],
                "presets": raw.get("presets") if isinstance(raw.get("presets"), dict) else {}}
    value = (enabled, _compile_settings(settings))
    with _cache_lock:
        _cache_key, _cache_value = digest, value
    return value


def match_chain(obs: ErrorObservation | None) -> tuple[RuleMatch, ...]:
    """Matching rules in order: any pass-through rules, then the one that decides (if any)."""
    if obs is None:
        return ()
    enabled, compiled = _live()
    if not enabled:
        return ()
    subject = subject_of(obs)
    out: list[RuleMatch] = []
    for found in compiled:
        if _rule_matches(found.rule, subject):
            out.append(found)
            if not found.passthrough:
                break
    return tuple(out)


def decisive(chain: tuple[RuleMatch, ...]) -> RuleMatch | None:
    """The rule that handles the attempt; a pass-through rule when nothing else matched."""
    return next((m for m in chain if not m.passthrough), chain[0] if chain else None)


def match(obs: ErrorObservation | None) -> RuleMatch | None:
    return decisive(match_chain(obs))


def find_rule(rule_id: str) -> RuleMatch | None:
    """Current definition of a user rule or preset by id (enabled or not)."""
    from . import config as config_module
    raw = config_module.get().get("errorRules")
    raw = raw if isinstance(raw, dict) else {}
    for rule in raw.get("rules") or []:
        if isinstance(rule, dict) and rule.get("id") == rule_id:
            try:
                return RuleMatch(normalize_rule(rule), "user")
            except ErrorRuleValidationError:
                return None
    for preset in PRESETS:
        if preset["id"] == rule_id:
            return RuleMatch(copy.deepcopy(preset), "preset")
    return None


# ─── recovery time ──────────────────────────────────────────────────


def resolve_until(step: dict[str, Any], obs: ErrorObservation, *, now_ms: int | None = None,
                  channel: Any = None) -> tuple[int | None, str]:
    """End of a pause step in epoch ms and where it came from; (None, "ladder") defers to the ladder."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    mode = step["mode"]

    def valid(ms: int | None) -> int | None:
        return ms if ms is not None and now < ms <= now + MAX_RESET_AHEAD_MS else None

    if mode == "ladder":
        return None, "ladder"
    if mode == "fixed":
        return now + step["seconds"] * 1000, "fixed"
    found: int | None = None
    if mode == "message_time":
        found = valid(time_from_text(obs.text, now_ms=now, pattern=step["pattern"], tz=step["timezone"]))
    elif mode == "retry_after":
        if obs.retry_after is not None and obs.retry_after > 0:
            found = valid(now + int(math.ceil(obs.retry_after * 1000)))
        if found is None:
            found = valid(time_from_text(obs.text, now_ms=now, tz=step["timezone"]))
    elif mode == "usage_window":
        found = valid(usage_window_end(channel, obs.model, now))
    elif mode == "boundary":
        found = next_boundary(now, step["boundaryHours"] or 1, step["timezone"])
    if found is not None:
        return found, mode
    if mode == "usage_window" and step["boundaryHours"]:
        return next_boundary(now, step["boundaryHours"], step["timezone"]), "boundary"
    if step["fallbackSeconds"]:
        return now + step["fallbackSeconds"] * 1000, "fallback"
    return None, "ladder"


# ─── client response ────────────────────────────────────────────────

_BJT = timezone(timedelta(hours=8))


def format_reset_at(until_ms: int | None) -> str:
    if not until_ms:
        return "an unknown time"
    return datetime.fromtimestamp(until_ms / 1000, _BJT).strftime("%Y-%m-%d %H:%M:%S") + " (UTC+8)"


def format_reset_in(until_ms: int | None, now_ms: int | None = None) -> str:
    if not until_ms:
        return "a while"
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    seconds = max(0, (until_ms - now) // 1000)
    if seconds < 90:
        return f"{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


_TEMPLATE_RE = re.compile(r"\{(\$[^{}]{0,200}|[a-z_]{1,40})\}")


def render_message(template: str, *, rule: dict[str, Any], obs: ErrorObservation | None,
                   model: str, until_ms: int | None, now_ms: int | None = None) -> str:
    text = template or default_message(rule["category"])
    values = {
        "model": model or (obs.model if obs else "") or "the model",
        "provider": (obs.provider if obs else "") or "upstream",
        "reset_at": format_reset_at(until_ms),
        "reset_in": format_reset_in(until_ms, now_ms),
        "upstream_message": ((obs.message if obs else "") or "upstream error")[:300],
        "status": str(obs.status) if obs and obs.status is not None else "",
        "code": (obs.codes[0] if obs and obs.codes else ""),
        "rule": rule["name"],
    }

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key.startswith("$"):
            if obs is None or obs.data is None:
                return ""
            try:
                found = conditions.resolve_path(conditions.parse_path(key), obs.data)
            except conditions.ConditionError:
                return m.group(0)
            return conditions.value_text(found[0])[:300] if found else ""
        return values.get(key, m.group(0))

    return _TEMPLATE_RE.sub(sub, text)[:1000]


def client_response(rule: dict[str, Any], *, obs: ErrorObservation | None, model: str,
                    until_ms: int | None, now_ms: int | None = None) -> dict[str, Any] | None:
    """Status/message/Retry-After the client gets when this rule ends the request; None keeps defaults."""
    rs = rule["response"]
    if not rs["status"]:
        return None
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    retry_after = None
    if rs["retryAfter"] and until_ms and until_ms > now:
        retry_after = min(86400 * 7, max(1, math.ceil((until_ms - now) / 1000)))
    return {
        "status": rs["status"],
        "message": render_message(rs["message"], rule=rule, obs=obs, model=model, until_ms=until_ms, now_ms=now),
        "retryAfter": retry_after,
        "ruleId": rule["id"],
    }


def unavailable_response(exclusions: list[dict[str, Any]], model: str,
                         *, now_ms: int | None = None) -> dict[str, Any] | None:
    """When every candidate is out and some were paused by a rule, answer as that rule says."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    best: tuple[int, RuleMatch] | None = None
    for item in exclusions or []:
        if item.get("reason") != "cooldown" or not item.get("rule"):
            continue
        until = item.get("until")
        if not isinstance(until, int) or until <= now:
            continue
        found = find_rule(str(item["rule"]))
        if found is None or not found.rule["response"]["status"]:
            continue
        if best is None or until < best[0]:
            best = (until, found)
    if best is None:
        return None
    return client_response(best[1].rule, obs=None, model=model, until_ms=best[0], now_ms=now)


def response_headers(response: dict[str, Any]) -> dict[str, str] | None:
    if response.get("retryAfter"):
        return {"Retry-After": str(int(response["retryAfter"]))}
    return None


# ─── dry run ────────────────────────────────────────────────────────


def _plan_steps(steps: list[dict[str, Any]], obs: ErrorObservation, now: int, channel: Any) -> tuple[list[dict[str, Any]], int | None]:
    out: list[dict[str, Any]] = []
    latest: int | None = None
    for step in steps:
        item = dict(step)
        if step["type"] == "pause":
            until, source = resolve_until(step, obs, now_ms=now, channel=channel)
            item.update(until=until, source=source, untilText=format_reset_at(until) if until else None)
            if until and (latest is None or until > latest):
                latest = until
        out.append(item)
    return out, latest


def plan(rule: dict[str, Any], obs: ErrorObservation, *, now_ms: int | None = None,
         channel: Any = None) -> dict[str, Any]:
    """What applying ``rule`` to ``obs`` would do, without side effects."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    steps, latest = _plan_steps(rule["steps"], obs, now, channel)
    response = client_response(rule, obs=obs, model=obs.model, until_ms=latest, now_ms=now)
    if response is None and rule["final"] == "respond":
        # Same fallback as failover: relay the upstream status, 502 when there was none.
        response = {"status": obs.status or 502, "message": (obs.message or "upstream error")[:300],
                    "retryAfter": None, "ruleId": rule["id"]}
    thresholds = []
    for t in rule["thresholds"]:
        t_steps, _ = _plan_steps(t["steps"], obs, now, channel)
        thresholds.append({**t, "steps": t_steps})
    return {
        "passthrough": rule["final"] == "pass",
        "steps": steps,
        "final": rule["final"],
        "thresholds": thresholds,
        "response": response,
    }


def explain(obs: ErrorObservation, settings: dict[str, Any], *, draft: dict[str, Any] | None = None,
            now_ms: int | None = None, channel: Any = None,
            channel_label: Any = str, progress: Any = None) -> dict[str, Any]:
    """Layer-by-layer trace for the tester. ``draft`` is evaluated first, as an unsaved user rule."""
    trace: list[dict[str, Any]] = []
    chosen: RuleMatch | None = None
    subject = subject_of(obs)
    entries: list[tuple[dict[str, Any], str]] = []
    if draft is not None:
        entries.append((draft, "draft"))
    entries += [(r, "user") for r in settings.get("rules") or []]
    entries += [(r, "preset") for r in preset_rules(settings)]
    for rule, layer in entries:
        row = {"layer": layer, "id": rule["id"], "name": rule["name"], "category": rule["category"]}
        if not settings.get("enabled", True) and layer != "draft":
            row.update(status="skipped", reason="错误处理规则总开关已关闭")
        elif not rule.get("enabled", True):
            row.update(status="skipped", reason="规则已停用")
        elif chosen is not None and not chosen.passthrough:
            row.update(status="skipped", reason="前面的规则已经处理")
        else:
            reason = conditions.explain_miss(rule["when"], subject, provider_label=provider_label,
                                             channel_label=channel_label)
            if reason is None:
                row.update(status="matched", reason="")
                if chosen is None or chosen.passthrough:
                    chosen = RuleMatch(rule, layer)
            else:
                row.update(status="missed", reason=reason)
        trace.append(row)
    result = {
        "observation": {
            "phase": obs.phase, "status": obs.status, "provider": obs.provider, "model": obs.model,
            "codes": list(obs.codes), "message": obs.message,
            "data": obs.data if obs.data is not None and len(json.dumps(obs.data, ensure_ascii=False)) <= 20000 else None,
        },
        "trace": trace,
        "matched": None,
        "plan": None,
    }
    if chosen is not None:
        result["matched"] = {"layer": chosen.layer, "id": chosen.id, "name": chosen.rule["name"],
                             "category": chosen.category}
        result["plan"] = plan(chosen.rule, obs, now_ms=now_ms, channel=channel)
        if progress is not None:
            for item, current in zip(result["plan"]["thresholds"], progress(chosen)):
                item["current"] = current
    return result


def categories_public() -> list[dict[str, Any]]:
    return [{"id": cid, "label": CATEGORIES[cid]["label"], "description": CATEGORIES[cid]["description"],
             "handling": default_handling(cid)} for cid in CATEGORY_ORDER]
