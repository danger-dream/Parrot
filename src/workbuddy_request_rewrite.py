"""Configurable, literal WorkBuddy template adaptation; no runtime/config imports.

Only standalone system prefix lines and label lines inside a complete, unquoted
<env> block are eligible. Never walk arbitrary JSON strings or business roles.
"""
from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser
from typing import Any

DEFAULT_REQUEST_REWRITE = {
    "enabled": True,
    "rules": [
        {
            "id": "cc-identity",
            "enabled": True,
            "scope": "system_prefix_line",
            "match": "You are Claude Code, Anthropic's official CLI for Claude.",
            "replace": "You are a coding agent.",
        },
        {
            "id": "cc-env-main-branch",
            "enabled": True,
            "scope": "system_env_line",
            "match": "Main branch (you will usually use this for PRs)",
            "replace": "Main branch (normally used for pull requests)",
        },
    ],
}
SCOPES = frozenset({"system_prefix_line", "system_env_line"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_FENCE = re.compile(r"(`{3,}|~{3,})(.*)\Z")


class RewriteConfigError(ValueError):
    """Messages identify fields/indices, never configured match/replacement text."""


def settings_from_config(cfg: dict) -> dict:
    workbuddy = cfg.get("workbuddy", {})
    if not isinstance(workbuddy, dict):
        raise RewriteConfigError("workbuddy must be an object")
    settings = workbuddy.get("requestRewrite", DEFAULT_REQUEST_REWRITE)
    validate_settings(settings)
    return settings


def validate_settings(settings: Any) -> None:
    if not isinstance(settings, dict):
        raise RewriteConfigError("workbuddy.requestRewrite must be an object")
    if set(settings) - {"enabled", "rules"}:
        raise RewriteConfigError("workbuddy.requestRewrite has unknown fields")
    if not isinstance(settings.get("enabled", True), bool):
        raise RewriteConfigError("workbuddy.requestRewrite.enabled must be boolean")
    rules = settings.get("rules", DEFAULT_REQUEST_REWRITE["rules"])
    if not isinstance(rules, list) or len(rules) > 64:
        raise RewriteConfigError("workbuddy.requestRewrite.rules must be an array of at most 64 rules")
    ids = set()
    for i, rule in enumerate(rules):
        prefix = f"workbuddy.requestRewrite.rules[{i}]"
        if not isinstance(rule, dict) or set(rule) - {"id", "enabled", "scope", "match", "replace"}:
            raise RewriteConfigError(f"{prefix} must be a rule object with known fields")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not _ID.fullmatch(rule_id) or rule_id in ids:
            raise RewriteConfigError(f"{prefix}.id must be unique, 1-64 ASCII letters/digits/._- (starting with a letter/digit)")
        ids.add(rule_id)
        if not isinstance(rule.get("enabled", True), bool):
            raise RewriteConfigError(f"{prefix}.enabled must be boolean")
        if not isinstance(rule.get("scope"), str) or rule["scope"] not in SCOPES:
            raise RewriteConfigError(f"{prefix}.scope must be system_prefix_line or system_env_line")
        for name, minimum, maximum in (("match", 1, 2048), ("replace", 0, 4096)):
            value = rule.get(name)
            if (not isinstance(value, str) or not minimum <= len(value) <= maximum
                    or any(ord(c) < 32 or ord(c) == 127 or c in "\u0085\u2028\u2029" for c in value)):
                raise RewriteConfigError(f"{prefix}.{name} must be a single-line string of {minimum}-{maximum} characters without control characters")


class _ScopeMarkup(HTMLParser):
    """Use the stdlib tokenizer for inline tags, attributes, comments and CDATA.

    This is exclusion bookkeeping, not HTML repair: only a complete, exact bare
    root env grants scope. Nested/opaque/malformed markup never grants it.
    """

    def __init__(self, lines: list[str], blocked: set[int]):
        super().__init__(convert_charrefs=False)
        self.lines = lines
        self.blocked = blocked
        self.stack: list[tuple[str, int, bool]] = []
        self.env_lines: set[int] = set()
        self.uncertain = False

    def _block_token(self, text: str) -> None:
        start = self.getpos()[0] - 1
        self.blocked.update(range(start, start + text.count("\n") + 1))

    def handle_starttag(self, tag, attrs):
        text = self.get_starttag_text()
        self._block_token(text)
        start = self.getpos()[0] - 1
        bare = not self.stack and text == "<env>" and self.lines[start].strip() == "<env>"
        self.stack.append((tag, start, bare))

    def handle_startendtag(self, tag, attrs):
        # In particular, attributes/whitespace cannot hide the closing slash.
        self._block_token(self.get_starttag_text())

    def handle_endtag(self, tag):
        end = self.getpos()[0] - 1
        self.blocked.add(end)
        if not self.stack or self.stack[-1][0] != tag:
            self.uncertain = True
            return
        _, start, bare = self.stack.pop()
        if bare and not self.stack and self.lines[end].strip() == "</env>":
            self.env_lines.update(range(start + 1, end))
        else:
            self.blocked.update(range(start, end + 1))

    handle_comment = _block_token
    handle_decl = _block_token
    handle_pi = _block_token
    unknown_decl = _block_token


def _line_scopes(lines: list[str]) -> dict[int, str]:
    """Locate complete bare env blocks; exclude quoted markup and code fences.

    Syntax is deliberately conservative, not an inference of a client's identity.
    A prose quote not marked as such is indistinguishable from a real template;
    only the explicit line-level scope and exact configured match authorize edits.
    """
    # Normalize only the tokenizer view, retaining every original byte/part for
    # rewriting. Mask fences before parsing so quoted fake tags cannot affect it.
    normalized = [line.rstrip("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029") for line in lines]
    blocked: set[int] = set()
    fence: tuple[str, int] | None = None
    for i, line in enumerate(normalized):
        marker = _FENCE.fullmatch(line.strip())
        if fence:
            blocked.add(i)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
        elif marker:
            blocked.add(i)
            fence = (marker[1][0], len(marker[1]))
    parser = _ScopeMarkup(normalized, blocked)
    try:
        parser.feed("\n".join("" if i in blocked else line for i, line in enumerate(normalized)))
        parser.close()
    except (AssertionError, ValueError):
        return {}  # Malformed declarations are untrusted structure, not an error.
    if parser.uncertain:
        return {}
    for _, start, _ in parser.stack:
        blocked.update(range(start, len(lines)))
    scopes = {i: "system_env_line" for i in parser.env_lines - blocked}
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and first not in blocked:
        scopes[first] = "system_prefix_line"
    return scopes


def _rewrite_texts(texts: list[str], rules: list[dict]) -> tuple[list[str], dict[str, int]]:
    # Keep text-part boundaries and every original line ending. Scopes can span
    # adjacent text parts, but matches cannot splice unrelated parts together.
    parts = [text.splitlines(keepends=True) for text in texts]
    lines = [line for part in parts for line in part]
    scopes = _line_scopes(lines)
    hits: Counter[str] = Counter()
    for i, scope in scopes.items():
        line = lines[i]
        clean = line.strip()
        leading = len(line) - len(line.lstrip())
        for rule in rules:
            if rule["scope"] != scope:
                continue
            needle = rule["match"]
            matches = clean == needle
            if scope == "system_env_line":
                matches |= clean.startswith(needle + ":")
            if not matches:
                continue
            replacement = rule["replace"]
            if replacement != needle:
                lines[i] = line[:leading] + replacement + line[leading + len(needle):]
                hits[rule["id"]] += 1
            # First matching rule wins against the ORIGINAL line: no cascading
            # rules or repeated replace-until-clean loops.
            break
    rewritten, offset = [], 0
    for part in parts:
        rewritten.append("".join(lines[offset:offset + len(part)]))
        offset += len(part)
    return rewritten, dict(hits)


def rewrite_payload(payload: dict, settings: dict) -> tuple[dict, dict[str, int]]:
    """Return a copy-on-write payload and hit counts; never mutate the input.

    The channel caller enforces the provider/realm/profile boundary. Only system
    messages and their string/text parts are visited, never developer/user/tool
    roles, tool schemas, tool arguments, images, or unknown content part types.
    """
    validate_settings(settings)
    if not settings.get("enabled", True):
        return payload, {}
    rules = [rule for rule in settings.get("rules", DEFAULT_REQUEST_REWRITE["rules"]) if rule.get("enabled", True)]
    if not rules:
        return payload, {}
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, {}
    changed_messages = list(messages)
    hits: Counter[str] = Counter()
    for i, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts, counts = _rewrite_texts([content], rules)
            changed_content = texts[0]
        elif isinstance(content, list):
            # Unknown/non-text parts are structural barriers, not hidden text.
            # Skip such a system message rather than infer continuity across them.
            if any(not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str) for part in content):
                continue
            texts, counts = _rewrite_texts([part["text"] for part in content], rules)
            changed_content = [dict(part, text=text) if text != part["text"] else part for part, text in zip(content, texts)]
        else:
            continue
        if counts:
            changed_messages[i] = dict(message, content=changed_content)
            hits.update(counts)
    return (dict(payload, messages=changed_messages), dict(hits)) if hits else (payload, {})
