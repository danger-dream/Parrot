"""Per-candidate outbound prompt rewrite rules engine.

Applies user-configured, deterministic text rewrites (replace, wrap, prepend/append
system prompt) to outbound request copies right before channel request building.
Only text content fields are ever rewritten; tool-call arguments, media base64,
and opaque reasoning signatures/encrypted_content are strictly untouched.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import json
import re
import threading
from typing import Any

DEFAULT_PROMPT_RULES: dict[str, Any] = {
    "enabled": True,
    "rules": [],
}

ALL_TARGET = "all"
CONCRETE_TARGETS: tuple[str, ...] = (
    "system",
    "user_text",
    "assistant_text",
    "tool_description",
    "tool_result_text",
)
VALID_TARGETS = frozenset({ALL_TARGET, *CONCRETE_TARGETS})
VALID_PROTOCOLS = frozenset({"anthropic", "chat", "responses"})
VALID_ACTION_TYPES = frozenset({"replace", "wrap", "prepend_system", "append_system"})
VALID_MODES = frozenset({"literal", "regex"})

MAX_RULES = 200
MAX_PATTERNS_PER_RULE = 100
MAX_PATTERN_LEN = 1024
MAX_TEXT_LEN = 16384
MAX_WRAP_AFFIX_LEN = 256
MAX_ID_LEN = 64
MAX_NAME_LEN = 120
MAX_REWRITTEN_TEXT_LEN = 4 * 1024 * 1024  # 4 MiB hard cap per rewritten text field
MAX_EXPANSION_SLACK = 256 * 1024  # Allow up to max(4x, +256 KiB) expansion per field
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Reject common nested-quantifier shapes that can cause catastrophic backtracking.
_NESTED_QUANTIFIER_RE = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d+,\d*\})")


class PromptRuleValidationError(ValueError):
    """Raised when promptRules configuration or a rule payload is invalid."""


@dataclass(frozen=True, slots=True)
class RuleHit:
    rule_id: str
    rule_name: str
    action_type: str
    replacements: int
    targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RewriteReport:
    changed: bool
    total_replacements: int
    hits: tuple[RuleHit, ...]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "id": h.rule_id,
                "name": h.rule_name,
                "actionType": h.action_type,
                "replacements": h.replacements,
                "targets": list(h.targets),
            }
            for h in self.hits
        ]


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    id: str
    name: str
    enabled: bool
    models: tuple[str, ...]
    channels: tuple[str, ...]
    providers: tuple[str, ...]
    api_keys: tuple[str, ...]
    protocols: tuple[str, ...]
    targets: frozenset[str]
    action_type: str
    mode: str
    pattern: str
    patterns: tuple[str, ...]
    replacement: str
    prefix: str
    suffix: str
    skip_if_wrapped: bool
    text: str
    case_sensitive: bool
    stop_on_match: bool
    compiled_regex: re.Pattern[str] | None
    literal_terms: tuple[str, ...] = ()
    literal_lower_terms: tuple[str, ...] = ()
    literal_caseless: bool = False


_cache_lock = threading.RLock()
_compiled_cache_raw_ref: dict[str, Any] | None = None
_compiled_cache_key: str = ""
_compiled_cache_value: tuple[bool, tuple[_CompiledRule, ...]] = (True, ())


def _clean_str_list(raw: Any, field_name: str, *, max_items: int = 200, max_len: int = 256) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PromptRuleValidationError(f"{field_name} must be an array of strings")
    if len(raw) > max_items:
        raise PromptRuleValidationError(f"{field_name} exceeds maximum of {max_items} entries")
    out: list[str] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            raise PromptRuleValidationError(f"{field_name}[{idx}] must be a string")
        val = item.strip()
        if not val:
            continue
        if len(val) > max_len:
            raise PromptRuleValidationError(f"{field_name}[{idx}] exceeds {max_len} characters")
        if val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _validate_regex(pattern: str, field_name: str, *, replacement: str | None = None) -> None:
    if _NESTED_QUANTIFIER_RE.search(pattern):
        raise PromptRuleValidationError(f"{field_name} contains unsafe nested quantifiers")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise PromptRuleValidationError(f"{field_name} is not a valid regex: {exc}") from exc
    if compiled.search("") is not None and compiled.pattern == "":
        raise PromptRuleValidationError(f"{field_name} cannot be empty")
    if replacement is not None and "\\" in replacement:
        parser = getattr(re, "_parser", None)
        parse_tpl = getattr(parser, "parse_template", None) if parser is not None else None
        if callable(parse_tpl):
            try:
                parse_tpl(replacement, compiled)
            except (re.error, IndexError) as exc:
                raise PromptRuleValidationError(f"{field_name} replacement template is invalid: {exc}") from exc


def _opt_bool(raw: dict[str, Any], key: str, default: bool, field_name: str) -> bool:
    val = raw.get(key, default)
    if not isinstance(val, bool):
        raise PromptRuleValidationError(f"{field_name} must be boolean")
    return val


def _opt_str(raw: dict[str, Any], key: str, max_len: int, field_name: str) -> str:
    val = raw.get(key, "")
    if val is None:
        return ""
    if not isinstance(val, str) or len(val) > max_len:
        raise PromptRuleValidationError(f"{field_name} must be a string up to {max_len} characters")
    return val


def normalize_rule(raw: Any, *, index: int | None = None) -> dict[str, Any]:
    prefix = f"promptRules.rules[{index}]" if index is not None else "rule"
    if not isinstance(raw, dict):
        raise PromptRuleValidationError(f"{prefix} must be an object")

    rule_id = str(raw.get("id") or "").strip()
    if not rule_id or len(rule_id) > MAX_ID_LEN or not _RULE_ID_RE.fullmatch(rule_id):
        raise PromptRuleValidationError(
            f"{prefix}.id must be 1-{MAX_ID_LEN} ASCII letters/digits/._- starting with a letter or digit"
        )
    name = str(raw.get("name") or "").strip() or rule_id
    if len(name) > MAX_NAME_LEN:
        raise PromptRuleValidationError(f"{prefix}.name must be at most {MAX_NAME_LEN} characters")
    enabled = _opt_bool(raw, "enabled", True, f"{prefix}.enabled")
    stop_on_match = _opt_bool(raw, "stopOnMatch", False, f"{prefix}.stopOnMatch")

    raw_match = raw.get("match") if raw.get("match") is not None else {}
    if not isinstance(raw_match, dict):
        raise PromptRuleValidationError(f"{prefix}.match must be an object")
    models = _clean_str_list(raw_match.get("models"), f"{prefix}.match.models")
    channels = _clean_str_list(raw_match.get("channels"), f"{prefix}.match.channels", max_len=512)
    providers = [p.lower() for p in _clean_str_list(raw_match.get("providers"), f"{prefix}.match.providers")]
    api_keys = _clean_str_list(raw_match.get("apiKeys"), f"{prefix}.match.apiKeys")
    protocols = [p.lower() for p in _clean_str_list(raw_match.get("protocols"), f"{prefix}.match.protocols")]
    for proto in protocols:
        if proto not in VALID_PROTOCOLS:
            raise PromptRuleValidationError(f"{prefix}.match.protocols contains unsupported protocol {proto!r}")

    raw_targets = raw.get("targets")
    if raw_targets is None:
        targets = [ALL_TARGET]
    else:
        targets = _clean_str_list(raw_targets, f"{prefix}.targets", max_items=16, max_len=32)
        if not targets:
            raise PromptRuleValidationError(f"{prefix}.targets must select at least one target")
        for t in targets:
            if t not in VALID_TARGETS:
                raise PromptRuleValidationError(f"{prefix}.targets contains unsupported target {t!r}")
        if ALL_TARGET in targets:
            targets = [ALL_TARGET]

    raw_action = raw.get("action")
    if not isinstance(raw_action, dict):
        raise PromptRuleValidationError(f"{prefix}.action must be an object")
    action_type = str(raw_action.get("type") or "").strip()
    if action_type not in VALID_ACTION_TYPES:
        raise PromptRuleValidationError(f"{prefix}.action.type must be one of {sorted(VALID_ACTION_TYPES)}")
    mode = str(raw_action.get("mode") or "literal").strip()
    if mode not in VALID_MODES:
        raise PromptRuleValidationError(f"{prefix}.action.mode must be 'literal' or 'regex'")
    case_sensitive = _opt_bool(raw_action, "caseSensitive", False, f"{prefix}.action.caseSensitive")
    skip_if_wrapped = _opt_bool(raw_action, "skipIfWrapped", True, f"{prefix}.action.skipIfWrapped")
    pattern = _opt_str(raw_action, "pattern", MAX_PATTERN_LEN, f"{prefix}.action.pattern")
    replacement = _opt_str(raw_action, "replacement", MAX_TEXT_LEN, f"{prefix}.action.replacement")
    affix_prefix = _opt_str(raw_action, "prefix", MAX_WRAP_AFFIX_LEN, f"{prefix}.action.prefix")
    affix_suffix = _opt_str(raw_action, "suffix", MAX_WRAP_AFFIX_LEN, f"{prefix}.action.suffix")
    text = _opt_str(raw_action, "text", MAX_TEXT_LEN, f"{prefix}.action.text")

    raw_patterns = raw_action.get("patterns")
    patterns: list[str] = []
    if raw_patterns is not None:
        if not isinstance(raw_patterns, list) or len(raw_patterns) > MAX_PATTERNS_PER_RULE:
            raise PromptRuleValidationError(f"{prefix}.action.patterns must be an array of at most {MAX_PATTERNS_PER_RULE} strings")
        seen_p: set[str] = set()
        for p_idx, p_val in enumerate(raw_patterns):
            if not isinstance(p_val, str) or len(p_val) > MAX_PATTERN_LEN:
                raise PromptRuleValidationError(f"{prefix}.action.patterns[{p_idx}] must be a string up to {MAX_PATTERN_LEN} characters")
            if p_val and p_val not in seen_p:
                seen_p.add(p_val)
                patterns.append(p_val)

    if action_type in {"replace", "wrap"}:
        if mode == "regex":
            if not pattern:
                raise PromptRuleValidationError(f"{prefix}.action.pattern is required for regex mode")
            _validate_regex(
                pattern,
                f"{prefix}.action.pattern",
                replacement=replacement if action_type == "replace" else None,
            )
            patterns = []
        elif not pattern and not patterns:
            raise PromptRuleValidationError(f"{prefix}.action requires pattern or patterns")
        if action_type == "wrap" and not affix_prefix and not affix_suffix:
            raise PromptRuleValidationError(f"{prefix}.action requires prefix or suffix for wrap action")
    else:
        if not text:
            raise PromptRuleValidationError(f"{prefix}.action.text is required for {action_type}")
        targets = ["system"]

    return {
        "id": rule_id, "name": name, "enabled": enabled, "stopOnMatch": stop_on_match,
        "match": {"models": models, "channels": channels, "providers": providers, "apiKeys": api_keys, "protocols": protocols},
        "targets": targets,
        "action": {
            "type": action_type, "mode": mode, "pattern": pattern, "patterns": patterns,
            "replacement": replacement, "prefix": affix_prefix, "suffix": affix_suffix,
            "skipIfWrapped": skip_if_wrapped, "text": text, "caseSensitive": case_sensitive,
        },
    }


def normalize_settings(raw: Any) -> dict[str, Any]:
    if raw is None:
        return copy.deepcopy(DEFAULT_PROMPT_RULES)
    if not isinstance(raw, dict):
        raise PromptRuleValidationError("promptRules must be an object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PromptRuleValidationError("promptRules.enabled must be boolean")
    raw_rules = raw.get("rules", [])
    if not isinstance(raw_rules, list):
        raise PromptRuleValidationError("promptRules.rules must be an array")
    if len(raw_rules) > MAX_RULES:
        raise PromptRuleValidationError(f"promptRules.rules exceeds maximum of {MAX_RULES} rules")
    rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_rules):
        rule = normalize_rule(item, index=idx)
        if rule["id"] in seen_ids:
            raise PromptRuleValidationError(f"Duplicate prompt rule id: {rule['id']}")
        seen_ids.add(rule["id"])
        rules.append(rule)
    return {"enabled": enabled, "rules": rules}


def _compile_rule(rule: dict[str, Any]) -> _CompiledRule:
    action = rule["action"]
    action_type = action["type"]
    mode = action["mode"]
    flags = 0 if action["caseSensitive"] else re.IGNORECASE
    compiled_regex: re.Pattern[str] | None = None
    literal_terms: tuple[str, ...] = ()
    literal_lower_terms: tuple[str, ...] = ()
    literal_caseless = False
    if action_type in {"replace", "wrap"}:
        if mode == "regex":
            compiled_regex = re.compile(action["pattern"], flags)
        else:
            terms: list[str] = []
            if action["pattern"]:
                terms.append(action["pattern"])
            for p in action["patterns"]:
                if p not in terms:
                    terms.append(p)
            # Match longer literal terms first so shorter substrings do not split longer keywords.
            terms.sort(key=len, reverse=True)
            literal_terms = tuple(terms)
            literal_lower_terms = tuple(t.lower() for t in terms)
            # Terms without case variants (e.g. CJK/digits/symbols) or case-sensitive rules can use direct C-level substring search.
            literal_caseless = bool(action["caseSensitive"]) or all(
                t.lower() == t.upper() and t.casefold() == t for t in terms
            )
            eff_flags = 0 if literal_caseless else flags
            compiled_regex = re.compile("|".join(re.escape(t) for t in terms), eff_flags)

    raw_targets = rule["targets"]
    expanded = frozenset(CONCRETE_TARGETS) if ALL_TARGET in raw_targets else frozenset(raw_targets)
    match = rule["match"]
    return _CompiledRule(
        id=rule["id"], name=rule["name"], enabled=rule["enabled"],
        models=tuple(match["models"]), channels=tuple(match["channels"]),
        providers=tuple(match["providers"]), api_keys=tuple(match["apiKeys"]),
        protocols=tuple(match["protocols"]), targets=expanded,
        action_type=action_type, mode=mode, pattern=action["pattern"],
        patterns=tuple(action["patterns"]), replacement=action["replacement"],
        prefix=action["prefix"], suffix=action["suffix"],
        skip_if_wrapped=action["skipIfWrapped"], text=action["text"],
        case_sensitive=action["caseSensitive"], stop_on_match=rule["stopOnMatch"],
        compiled_regex=compiled_regex, literal_terms=literal_terms,
        literal_lower_terms=literal_lower_terms, literal_caseless=literal_caseless,
    )


def _compiled_from_settings(settings: dict[str, Any]) -> tuple[bool, tuple[_CompiledRule, ...]]:
    normalized = normalize_settings(settings)
    compiled = tuple(_compile_rule(rule) for rule in normalized["rules"])
    return normalized["enabled"], compiled


def _compile_live_tolerant(raw: dict[str, Any]) -> tuple[bool, tuple[_CompiledRule, ...]]:
    """Compile live config tolerantly: skip single bad hand-edited rules instead of failing all."""
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        print("[prompt_rules] invalid promptRules.enabled in config; rules disabled")
        return False, ()
    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list):
        print("[prompt_rules] invalid promptRules.rules in config; rules disabled")
        return False, ()
    compiled: list[_CompiledRule] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_rules[:MAX_RULES]):
        try:
            norm = normalize_rule(item, index=idx)
            if norm["id"] in seen_ids:
                raise PromptRuleValidationError(f"Duplicate prompt rule id: {norm['id']}")
            seen_ids.add(norm["id"])
            compiled.append(_compile_rule(norm))
        except PromptRuleValidationError as exc:
            print(f"[prompt_rules] skipping invalid rule[{idx}] from config: {exc}")
    return enabled, tuple(compiled)


def _get_live_compiled() -> tuple[bool, tuple[_CompiledRule, ...]]:
    global _compiled_cache_raw_ref, _compiled_cache_key, _compiled_cache_value
    from . import config as config_module
    raw = config_module.get().get("promptRules")
    if not raw or not isinstance(raw, dict):
        return False, ()
    if not raw.get("enabled", True) or not raw.get("rules"):
        return False, ()
    with _cache_lock:
        if raw is _compiled_cache_raw_ref:
            return _compiled_cache_value
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with _cache_lock:
        if digest == _compiled_cache_key:
            _compiled_cache_raw_ref = raw
            return _compiled_cache_value
    value = _compile_live_tolerant(raw)
    with _cache_lock:
        _compiled_cache_raw_ref = raw
        _compiled_cache_key = digest
        _compiled_cache_value = value
    return value


def _pattern_matches(patterns: tuple[str, ...], candidates: tuple[str, ...], *, case_insensitive: bool = True) -> bool:
    if not patterns:
        return True
    if "*" in patterns:
        return True
    for pat in patterns:
        p = pat.lower() if case_insensitive else pat
        for cand in candidates:
            if not cand:
                continue
            c = cand.lower() if case_insensitive else cand
            if fnmatchcase(c, p):
                return True
    return False


def _rule_matches_context(
    rule: _CompiledRule,
    *,
    requested_model: str = "",
    resolved_model: str = "",
    channel_key: str = "",
    channel_name: str = "",
    provider: str = "",
    api_key_name: str = "",
    ingress_protocol: str = "",
) -> bool:
    if not rule.enabled:
        return False
    if not _pattern_matches(rule.models, (requested_model, resolved_model), case_insensitive=True):
        return False
    if not _pattern_matches(rule.channels, (channel_key, channel_name), case_insensitive=False):
        return False
    if not _pattern_matches(rule.providers, (provider.lower(),), case_insensitive=True):
        return False
    if not _pattern_matches(rule.api_keys, (api_key_name,), case_insensitive=False):
        return False
    if not _pattern_matches(rule.protocols, (ingress_protocol.lower(),), case_insensitive=True):
        return False
    return True


def _within_expansion_limit(original_len: int, new_len: int) -> bool:
    if new_len <= original_len:
        return True
    cap = min(MAX_REWRITTEN_TEXT_LEN, max(original_len * 4, original_len + MAX_EXPANSION_SLACK))
    return new_len <= cap


def _warn_once(warned: set[str] | None, rule_id: str, msg: str) -> None:
    if warned is not None:
        if rule_id in warned:
            return
        warned.add(rule_id)
    print(msg)


def _apply_rule_to_text(
    rule: _CompiledRule, text: str, *, warned: set[str] | None = None,
) -> tuple[str, int]:
    if not text or rule.compiled_regex is None:
        return text, 0
    if rule.mode == "literal":
        if rule.literal_caseless:
            if len(rule.literal_terms) == 1:
                term = rule.literal_terms[0]
                if term not in text:
                    return text, 0
                if rule.action_type == "replace":
                    count = text.count(term)
                    new_text = text.replace(term, rule.replacement)
                    if not _within_expansion_limit(len(text), len(new_text)):
                        _warn_once(warned, rule.id, f"[prompt_rules] rule {rule.id!r} expansion cap exceeded ({len(text)} -> {len(new_text)}), skipped")
                        return text, 0
                    return new_text, count
            elif not any(t in text for t in rule.literal_terms):
                return text, 0
        else:
            lower_text = text.lower()
            if not any(t in lower_text for t in rule.literal_lower_terms):
                return text, 0

    try:
        if rule.action_type == "replace":
            if rule.mode == "literal":
                repl = rule.replacement
                count = 0

                def _lit_sub(_match: re.Match[str]) -> str:
                    nonlocal count
                    count += 1
                    return repl

                new_text = rule.compiled_regex.sub(_lit_sub, text)
            else:
                new_text, count = rule.compiled_regex.subn(rule.replacement, text)
        elif rule.action_type == "wrap":
            prefix, suffix, skip = rule.prefix, rule.suffix, rule.skip_if_wrapped
            count = 0

            def _wrap_sub(match: re.Match[str]) -> str:
                nonlocal count
                start, end = match.span()
                matched = match.group(0)
                if not matched:
                    return matched
                if skip and ((not prefix) or text.endswith(prefix, 0, start)) and ((not suffix) or text.startswith(suffix, end)):
                    return matched
                count += 1
                return f"{prefix}{matched}{suffix}"

            new_text = rule.compiled_regex.sub(_wrap_sub, text)
        else:
            return text, 0
    except re.error as exc:
        _warn_once(warned, rule.id, f"[prompt_rules] rule {rule.id!r} regex error skipped: {exc}")
        return text, 0

    if count and not _within_expansion_limit(len(text), len(new_text)):
        _warn_once(warned, rule.id, f"[prompt_rules] rule {rule.id!r} expansion cap exceeded ({len(text)} -> {len(new_text)}), skipped")
        return text, 0
    return new_text, count


def _apply_system_affix(text: str, *, affix: str, position: str) -> tuple[str, int]:
    if not affix:
        return text, 0
    if position == "prepend_system":
        if text.startswith(affix):
            return text, 0
        sep = "\n\n" if text and not affix.endswith("\n") and not text.startswith("\n") else ""
        return f"{affix}{sep}{text}", 1
    if text.endswith(affix):
        return text, 0
    sep = "\n\n" if text and not text.endswith("\n") and not affix.startswith("\n") else ""
    return f"{text}{sep}{affix}", 1


def _prune_emptied_anthropic_blocks(blocks: list[Any], emptied_indices: set[int], *, allow_empty_list: bool) -> list[Any]:
    if not emptied_indices:
        return blocks
    surviving = [b for i, b in enumerate(blocks) if i not in emptied_indices]
    if not surviving and not allow_empty_list:
        return blocks
    # Preserve cache_control on a preceding surviving block if a cached text block was emptied.
    if surviving:
        out: list[Any] = []
        for idx, block in enumerate(blocks):
            if idx in emptied_indices:
                cc = block.get("cache_control") if isinstance(block, dict) else None
                if cc and out and isinstance(out[-1], dict) and "cache_control" not in out[-1]:
                    out[-1] = {**out[-1], "cache_control": cc}
                continue
            out.append(block)
        return out
    return surviving


def _rewrite_content_blocks(
    content: Any, rule: _CompiledRule, *, allowed_block_types: frozenset[str],
    warned: set[str] | None = None, prune_emptied: bool = False, allow_empty_list: bool = False,
) -> tuple[Any, int]:
    if isinstance(content, str):
        return _apply_rule_to_text(rule, content, warned=warned)
    if not isinstance(content, list):
        return content, 0
    total = 0
    out_list: list[Any] | None = None
    emptied_indices: set[int] = set()
    for idx, block in enumerate(content):
        if isinstance(block, str):
            new_text, count = _apply_rule_to_text(rule, block, warned=warned)
            if count:
                if out_list is None:
                    out_list = list(content)
                out_list[idx] = new_text
                total += count
            continue
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in allowed_block_types and isinstance(block.get("text"), str):
            old_text = block["text"]
            new_text, count = _apply_rule_to_text(rule, old_text, warned=warned)
            if count:
                if out_list is None:
                    out_list = list(content)
                new_block = dict(block)
                new_block["text"] = new_text
                out_list[idx] = new_block
                if prune_emptied and old_text.strip() and not new_text.strip():
                    emptied_indices.add(idx)
                total += count
    if out_list is not None and emptied_indices:
        out_list = _prune_emptied_anthropic_blocks(out_list, emptied_indices, allow_empty_list=allow_empty_list)
    return (out_list if out_list is not None else content), total


def _affix_text_blocks(
    blocks: list[Any], *, affix: str, position: str, allowed_types: frozenset[str],
) -> tuple[list[Any], int] | None:
    text_indices = [
        i for i, b in enumerate(blocks)
        if isinstance(b, dict) and b.get("type") in allowed_types and isinstance(b.get("text"), str)
    ]
    if not text_indices:
        return None
    bi = text_indices[0] if position == "prepend_system" else text_indices[-1]
    new_text, count = _apply_system_affix(blocks[bi]["text"], affix=affix, position=position)
    if not count:
        return blocks, 0
    new_blocks = list(blocks)
    new_block = dict(new_blocks[bi])
    new_block["text"] = new_text
    new_blocks[bi] = new_block
    return new_blocks, count


def _affix_message_entry(
    entries: list[Any], target_idx: int, *, affix: str, position: str, allowed_types: frozenset[str],
) -> tuple[list[Any], int] | None:
    entry = entries[target_idx]
    content = entry.get("content")
    if isinstance(content, str):
        new_text, count = _apply_system_affix(content, affix=affix, position=position)
        if not count:
            return entries, 0
        new_entries = list(entries)
        new_entries[target_idx] = {**entry, "content": new_text}
        return new_entries, count
    if isinstance(content, list):
        res = _affix_text_blocks(content, affix=affix, position=position, allowed_types=allowed_types)
        if res is None:
            return None
        new_blocks, count = res
        if not count:
            return entries, 0
        new_entries = list(entries)
        new_entries[target_idx] = {**entry, "content": new_blocks}
        return new_entries, count
    return None


def _apply_system_insert_to_body(body: dict[str, Any], ingress_protocol: str, rule: _CompiledRule) -> int:
    affix, pos = rule.text, rule.action_type
    if ingress_protocol == "anthropic":
        sys_val = body.get("system")
        if isinstance(sys_val, str):
            new_text, count = _apply_system_affix(sys_val, affix=affix, position=pos)
            if count:
                body["system"] = new_text
            return count
        if isinstance(sys_val, list):
            res = _affix_text_blocks(sys_val, affix=affix, position=pos, allowed_types=frozenset({"text"}))
            if res is not None:
                new_list, count = res
                if count:
                    body["system"] = new_list
                return count
            new_list = list(sys_val)
            new_block = {"type": "text", "text": affix}
            new_list.insert(0, new_block) if pos == "prepend_system" else new_list.append(new_block)
            body["system"] = new_list
            return 1
        body["system"] = affix
        return 1

    if ingress_protocol == "chat":
        messages = body.get("messages")
        if not isinstance(messages, list):
            return 0
        sys_indices = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") in ("system", "developer")]
        if sys_indices:
            idx = sys_indices[0] if pos == "prepend_system" else sys_indices[-1]
            res = _affix_message_entry(messages, idx, affix=affix, position=pos, allowed_types=frozenset({"text"}))
            if res is not None:
                new_msgs, count = res
                if count:
                    body["messages"] = new_msgs
                return count
        body["messages"] = [{"role": "system", "content": affix}, *messages]
        return 1

    instructions = body.get("instructions")
    if isinstance(instructions, str):
        new_text, count = _apply_system_affix(instructions, affix=affix, position=pos)
        if count:
            body["instructions"] = new_text
        return count
    raw_input = body.get("input")
    if isinstance(raw_input, list):
        sys_indices = [
            i for i, item in enumerate(raw_input)
            if isinstance(item, dict) and item.get("role") in ("system", "developer") and item.get("type") in (None, "", "message")
        ]
        if sys_indices:
            idx = sys_indices[0] if pos == "prepend_system" else sys_indices[-1]
            res = _affix_message_entry(raw_input, idx, affix=affix, position=pos, allowed_types=frozenset({"input_text", "text"}))
            if res is not None:
                new_items, count = res
                if count:
                    body["input"] = new_items
                return count
    body["instructions"] = affix
    return 1


def _rewrite_tools_descriptions(tools: Any, rule: _CompiledRule, *, warned: set[str] | None = None) -> tuple[Any, int]:
    if not isinstance(tools, list):
        return tools, 0
    total = 0
    out_tools: list[Any] | None = None
    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            continue
        new_tool: dict[str, Any] | None = None
        if isinstance(tool.get("description"), str):
            new_desc, count = _apply_rule_to_text(rule, tool["description"], warned=warned)
            if count:
                new_tool = dict(tool)
                new_tool["description"] = new_desc
                total += count
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("description"), str):
            new_desc, count = _apply_rule_to_text(rule, fn["description"], warned=warned)
            if count:
                new_tool = dict(new_tool or tool)
                new_fn = dict(fn)
                new_fn["description"] = new_desc
                new_tool["function"] = new_fn
                total += count
        if tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
            new_children, count = _rewrite_tools_descriptions(tool["tools"], rule, warned=warned)
            if count:
                new_tool = dict(new_tool or tool)
                new_tool["tools"] = new_children
                total += count
        if new_tool is not None:
            if out_tools is None:
                out_tools = list(tools)
            out_tools[idx] = new_tool
    return (out_tools if out_tools is not None else tools), total


_ROLE_TO_TARGET = {"system": "system", "developer": "system", "user": "user_text", "assistant": "assistant_text", "tool": "tool_result_text"}
_TEXT_ONLY = frozenset({"text"})
_RESP_USER_TYPES = frozenset({"input_text", "text"})
_RESP_ASSISTANT_TYPES = frozenset({"output_text", "text"})
_RESP_TOOL_TYPES = frozenset({"input_text", "output_text", "text"})


def _apply_one_rule_to_body(
    body: dict[str, Any], ingress_protocol: str, rule: _CompiledRule, *, warned: set[str] | None = None,
) -> tuple[int, list[str]]:
    if rule.action_type in {"prepend_system", "append_system"}:
        count = _apply_system_insert_to_body(body, ingress_protocol, rule)
        return count, (["system"] if count else [])

    total = 0
    hit_targets: list[str] = []

    def _record(target: str, count: int) -> None:
        nonlocal total
        if count > 0:
            total += count
            if target not in hit_targets:
                hit_targets.append(target)

    if ingress_protocol == "anthropic":
        if "system" in rule.targets and "system" in body:
            new_sys, c = _rewrite_content_blocks(body["system"], rule, allowed_block_types=_TEXT_ONLY, warned=warned, prune_emptied=True, allow_empty_list=True)
            if c:
                body.pop("system", None) if (isinstance(new_sys, list) and not new_sys) else body.__setitem__("system", new_sys)
                _record("system", c)

        messages = body.get("messages")
        if isinstance(messages, list) and (rule.targets & {"user_text", "assistant_text", "tool_result_text"}):
            new_msgs: list[Any] | None = None
            for idx, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                role, content = str(msg.get("role") or ""), msg.get("content")
                if role == "user":
                    updated_content, msg_changed = content, False
                    if "user_text" in rule.targets:
                        updated_content, c = _rewrite_content_blocks(updated_content, rule, allowed_block_types=_TEXT_ONLY, warned=warned, prune_emptied=True, allow_empty_list=False)
                        if c:
                            msg_changed = True
                            _record("user_text", c)
                    if "tool_result_text" in rule.targets and isinstance(updated_content, list):
                        new_blocks: list[Any] | None = None
                        for b_idx, block in enumerate(updated_content):
                            if not isinstance(block, dict) or block.get("type") != "tool_result":
                                continue
                            new_tr, c = _rewrite_content_blocks(block.get("content"), rule, allowed_block_types=_TEXT_ONLY, warned=warned, prune_emptied=True, allow_empty_list=True)
                            if c:
                                new_blocks = list(updated_content) if new_blocks is None else new_blocks
                                new_blocks[b_idx] = {**block, "content": new_tr}
                                _record("tool_result_text", c)
                        if new_blocks is not None:
                            updated_content, msg_changed = new_blocks, True
                    if msg_changed:
                        new_msgs = list(messages) if new_msgs is None else new_msgs
                        new_msgs[idx] = {**msg, "content": updated_content}
                elif role == "assistant" and "assistant_text" in rule.targets:
                    updated_content, c = _rewrite_content_blocks(content, rule, allowed_block_types=_TEXT_ONLY, warned=warned, prune_emptied=True, allow_empty_list=False)
                    if c:
                        new_msgs = list(messages) if new_msgs is None else new_msgs
                        new_msgs[idx] = {**msg, "content": updated_content}
                        _record("assistant_text", c)
            if new_msgs is not None:
                body["messages"] = new_msgs

    elif ingress_protocol == "chat":
        messages = body.get("messages")
        if isinstance(messages, list) and (rule.targets & {"system", "user_text", "assistant_text", "tool_result_text"}):
            new_msgs = None
            for idx, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                target = _ROLE_TO_TARGET.get(str(msg.get("role") or ""), "")
                if not target or target not in rule.targets:
                    continue
                new_content, c = _rewrite_content_blocks(msg.get("content"), rule, allowed_block_types=_TEXT_ONLY, warned=warned)
                if c:
                    new_msgs = list(messages) if new_msgs is None else new_msgs
                    new_msgs[idx] = {**msg, "content": new_content}
                    _record(target, c)
            if new_msgs is not None:
                body["messages"] = new_msgs

    else:
        if "system" in rule.targets and isinstance(body.get("instructions"), str):
            new_ins, c = _apply_rule_to_text(rule, body["instructions"], warned=warned)
            if c:
                body["instructions"] = new_ins
                _record("system", c)

        raw_input = body.get("input")
        if isinstance(raw_input, str) and "user_text" in rule.targets:
            new_inp, c = _apply_rule_to_text(rule, raw_input, warned=warned)
            if c:
                body["input"] = new_inp
                _record("user_text", c)
        elif isinstance(raw_input, list) and (rule.targets & {"system", "user_text", "assistant_text", "tool_result_text"}):
            new_items: list[Any] | None = None
            for idx, item in enumerate(raw_input):
                if not isinstance(item, dict):
                    continue
                typ, role = str(item.get("type") or ""), str(item.get("role") or "")
                if typ in ("function_call_output", "custom_tool_call_output"):
                    if "tool_result_text" in rule.targets:
                        new_out, c = _rewrite_content_blocks(item.get("output"), rule, allowed_block_types=_RESP_TOOL_TYPES, warned=warned)
                        if c:
                            new_items = list(raw_input) if new_items is None else new_items
                            new_items[idx] = {**item, "output": new_out}
                            _record("tool_result_text", c)
                    continue
                if typ and typ != "message":
                    continue
                target = _ROLE_TO_TARGET.get(role, "") if role != "tool" else ""
                if not target or target not in rule.targets:
                    continue
                allowed = _RESP_ASSISTANT_TYPES if target == "assistant_text" else _RESP_USER_TYPES
                new_content, c = _rewrite_content_blocks(item.get("content"), rule, allowed_block_types=allowed, warned=warned)
                if c:
                    new_items = list(raw_input) if new_items is None else new_items
                    new_items[idx] = {**item, "content": new_content}
                    _record(target, c)
            if new_items is not None:
                body["input"] = new_items

    if "tool_description" in rule.targets and isinstance(body.get("tools"), list):
        new_tools, c = _rewrite_tools_descriptions(body["tools"], rule, warned=warned)
        if c:
            body["tools"] = new_tools
            _record("tool_description", c)

    return total, hit_targets


def _shallow_request_copy(body: dict[str, Any]) -> dict[str, Any]:
    return dict(body)


def apply_to_request(
    body: dict[str, Any], *, ingress_protocol: str, requested_model: str = "",
    resolved_model: str = "", channel_key: str = "", channel_name: str = "",
    provider: str = "", api_key_name: str = "", settings_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], RewriteReport]:
    empty = RewriteReport(changed=False, total_replacements=0, hits=())
    if not isinstance(body, dict):
        return body, empty
    enabled, compiled_rules = _compiled_from_settings(settings_override) if settings_override is not None else _get_live_compiled()
    if not enabled or not compiled_rules:
        return body, empty

    req_model = requested_model or str(body.get("model") or "")
    key_name = api_key_name or str(body.get("_api_key_name") or body.get("_parrot_api_key_name") or "")
    matched_rules = [
        rule for rule in compiled_rules
        if _rule_matches_context(
            rule, requested_model=req_model, resolved_model=resolved_model,
            channel_key=channel_key, channel_name=channel_name, provider=provider,
            api_key_name=key_name, ingress_protocol=ingress_protocol,
        )
    ]
    if not matched_rules:
        return body, empty

    working = _shallow_request_copy(body)
    hits: list[RuleHit] = []
    warned: set[str] = set()
    total = 0
    for rule in matched_rules:
        try:
            count, targets = _apply_one_rule_to_body(working, ingress_protocol, rule, warned=warned)
        except Exception as exc:
            print(f"[prompt_rules] rule {rule.id!r} failed on request, skipped: {exc}")
            continue
        if count > 0:
            total += count
            hits.append(RuleHit(rule.id, rule.name, rule.action_type, count, tuple(targets)))
            if rule.stop_on_match:
                break

    if not hits:
        return body, empty
    return working, RewriteReport(changed=True, total_replacements=total, hits=tuple(hits))


def preview_rules(
    *, samples: dict[str, str], requested_model: str = "", resolved_model: str = "",
    channel_key: str = "", channel_name: str = "", provider: str = "",
    api_key_name: str = "", ingress_protocol: str = "responses",
    settings_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dry-run prompt rules against sample text fields without dispatching upstream."""
    enabled, compiled_rules = _compiled_from_settings(settings_override) if settings_override is not None else _get_live_compiled()
    before = {k: str(samples.get(k) or "") for k in CONCRETE_TARGETS}
    after = dict(before)
    if not enabled or not compiled_rules:
        return {"enabled": enabled, "changed": False, "totalReplacements": 0, "matchedRuleIds": [], "hits": [], "before": before, "after": after}

    matched_rules = [
        rule for rule in compiled_rules
        if _rule_matches_context(
            rule, requested_model=requested_model, resolved_model=resolved_model or requested_model,
            channel_key=channel_key, channel_name=channel_name, provider=provider,
            api_key_name=api_key_name, ingress_protocol=ingress_protocol,
        )
    ]
    hits: list[RuleHit] = []
    total = 0
    for rule in matched_rules:
        rule_count = 0
        rule_targets: list[str] = []
        if rule.action_type in {"prepend_system", "append_system"}:
            new_sys, c = _apply_system_affix(after["system"], affix=rule.text, position=rule.action_type)
            if c:
                after["system"] = new_sys
                rule_count += c
                rule_targets.append("system")
        else:
            for target in CONCRETE_TARGETS:
                if target not in rule.targets or not after[target]:
                    continue
                new_val, c = _apply_rule_to_text(rule, after[target])
                if c:
                    after[target] = new_val
                    rule_count += c
                    rule_targets.append(target)
        if rule_count > 0:
            total += rule_count
            hits.append(RuleHit(rule.id, rule.name, rule.action_type, rule_count, tuple(rule_targets)))
            if rule.stop_on_match:
                break

    report = RewriteReport(changed=bool(hits), total_replacements=total, hits=tuple(hits))
    return {
        "enabled": enabled, "changed": report.changed, "totalReplacements": total,
        "matchedRuleIds": [r.id for r in matched_rules], "hits": report.as_dicts(),
        "before": before, "after": after,
    }
