"""Claude Code messages mimicry for the empirically verified v2.1.258 wire model.

The protocol-critical pieces in this module (fingerprint, billing attribution,
CCH hash view, body profiles and headers) are validated against captured
v2.1.258 fixtures.  Parrot-specific compatibility behaviour remains bounded to
this transform and private ``_parrot_*`` request context never reaches the wire.
"""

import hashlib
import json
import os
import random
import re
import uuid

import xxhash

from .. import cache_hints
from .. import config as _ap_config


# ─── BASE_DIR / device_id 持久化 ──────────────────────────────────
# anthropic-proxy 的包目录层级：<root>/src/transform/cc_mimicry.py
# 所以 BASE_DIR = cc_mimicry.py 所在目录向上两级
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CC_VERSION = "2.1.258"
FINGERPRINT_SALT = "59cf53e54c78"
FINGERPRINT_INDICES = (4, 7, 20)
CC_ENTRYPOINT = "sdk-cli"
USER_TYPE = "external"

FAST_MODE_BETA = "fast-mode-2026-02-01"
CONTEXT_1M_BETA = "context-1m-2025-08-07"
INTERLEAVED_THINKING_BETA = "interleaved-thinking-2025-05-14"
THINKING_TOKEN_COUNT_BETA = "thinking-token-count-2026-05-13"
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
PROMPT_CACHING_SCOPE_BETA = "prompt-caching-scope-2026-01-05"
MID_CONVERSATION_SYSTEM_BETA = "mid-conversation-system-2026-04-07"
ADVISOR_TOOL_BETA = "advisor-tool-2026-03-01"
ADVANCED_TOOL_USE_BETA = "advanced-tool-use-2025-11-20"
EFFORT_BETA = "effort-2025-11-24"
SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_CREDIT_BETA = "fallback-credit-2026-06-01"
STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-12-15"
CACHE_DIAGNOSIS_BETA = "cache-diagnosis-2026-04-07"
EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"
OAUTH_BETA = "oauth-2025-04-20"

# Public compatibility surface: a superset used only when a caller explicitly
# supplies ``betas=``.  Normal requests select one exact profile below.
BETAS = [
    "claude-code-20250219", FAST_MODE_BETA, CONTEXT_1M_BETA,
    INTERLEAVED_THINKING_BETA, THINKING_TOKEN_COUNT_BETA,
    CONTEXT_MANAGEMENT_BETA, PROMPT_CACHING_SCOPE_BETA,
    MID_CONVERSATION_SYSTEM_BETA, ADVISOR_TOOL_BETA,
    ADVANCED_TOOL_USE_BETA, EFFORT_BETA, SERVER_SIDE_FALLBACK_BETA,
    FALLBACK_CREDIT_BETA, STRUCTURED_OUTPUTS_BETA, CACHE_DIAGNOSIS_BETA,
    EXTENDED_CACHE_TTL_BETA,
]

_MAIN_BETAS = [
    "claude-code-20250219", INTERLEAVED_THINKING_BETA,
    THINKING_TOKEN_COUNT_BETA, CONTEXT_MANAGEMENT_BETA,
    PROMPT_CACHING_SCOPE_BETA, MID_CONVERSATION_SYSTEM_BETA,
    ADVANCED_TOOL_USE_BETA, EFFORT_BETA, CACHE_DIAGNOSIS_BETA,
]
_FABLE_API_KEY_BETAS = [
    "claude-code-20250219", INTERLEAVED_THINKING_BETA,
    THINKING_TOKEN_COUNT_BETA, CONTEXT_MANAGEMENT_BETA,
    PROMPT_CACHING_SCOPE_BETA, MID_CONVERSATION_SYSTEM_BETA,
    ADVISOR_TOOL_BETA, ADVANCED_TOOL_USE_BETA, EFFORT_BETA,
    SERVER_SIDE_FALLBACK_BETA, FALLBACK_CREDIT_BETA,
    CACHE_DIAGNOSIS_BETA,
]
_OPUS_5_BETAS = [
    "claude-code-20250219", CONTEXT_1M_BETA,
    INTERLEAVED_THINKING_BETA, THINKING_TOKEN_COUNT_BETA,
    CONTEXT_MANAGEMENT_BETA, PROMPT_CACHING_SCOPE_BETA,
    MID_CONVERSATION_SYSTEM_BETA, ADVISOR_TOOL_BETA,
    ADVANCED_TOOL_USE_BETA, EFFORT_BETA, FALLBACK_CREDIT_BETA,
    CACHE_DIAGNOSIS_BETA,
]
_SIDE_QUERY_BETAS = [
    INTERLEAVED_THINKING_BETA, THINKING_TOKEN_COUNT_BETA,
    CONTEXT_MANAGEMENT_BETA, PROMPT_CACHING_SCOPE_BETA,
    STRUCTURED_OUTPUTS_BETA, CACHE_DIAGNOSIS_BETA,
]

# server.py 会把下游 HTTP 头里的 beta / 原始模型名折进 body 的私有字段，
# 这样调度 / failover / Channel 抽象不用整体改签名；transform_request 不会透传这些字段。
PARROT_DOWNSTREAM_BETAS_KEY = "_parrot_downstream_betas"
PARROT_ORIGINAL_MODEL_KEY = "_parrot_original_model"
PARROT_WANTS_CONTEXT_1M_KEY = "_parrot_wants_context_1m"
PARROT_WANTS_FAST_MODE_KEY = "_parrot_wants_fast_mode"
PARROT_CC_SESSION_ID_KEY = "_parrot_claude_code_session_id"
PARROT_CC_PROMPT_ID_KEY = "_parrot_cc_prompt_id"

CC_REQUEST_CONTEXT_KEYS = frozenset({
    PARROT_CC_SESSION_ID_KEY,
    PARROT_CC_PROMPT_ID_KEY,
})

ONE_M_CONTEXT_TOKENS = 1_000_000

CLI_USER_AGENT = f"claude-cli/{CC_VERSION} ({USER_TYPE}, {CC_ENTRYPOINT})"

ANTHROPIC_API_BASE = "https://api.anthropic.com"


def _normalize_cch_mode(value):
    mode = str(value or "dynamic").strip().lower()
    if mode in ("dynamic", "static", "disabled"):
        return mode
    return "dynamic"


def _normalize_cch_value(value):
    raw = "".join(ch for ch in str(value or "00000").strip().lower() if ch in "0123456789abcdef")
    if not raw:
        return "00000"
    return raw[:5].rjust(5, "0")


# ─── 持久 device_id ───

def _load_or_create_device_id():
    ids_file = os.path.join(_ap_config.DATA_DIR, ".anthropic_proxy_ids.json")
    if os.path.exists(ids_file):
        with open(ids_file) as f:
            return json.load(f).get("device_id", os.urandom(32).hex())
    device_id = os.urandom(32).hex()
    with open(ids_file, "w") as f:
        json.dump({"device_id": device_id}, f)
    return device_id


DEVICE_ID = _load_or_create_device_id()


# ─── load_config compatibility layer ─────────────────────────────
# Keep the internal snake_case keys so dynamic/static/disabled semantics remain
# compatible with existing Parrot configuration and callers.

def load_config():
    cfg = _ap_config.get()
    return {
        "cch_mode": cfg.get("cchMode", "disabled"),
        "cch_static_value": cfg.get("cchStaticValue", "00000"),
    }


# ─── Fingerprint / billing attribution ─────────────────────────────

_SYSTEM_REMINDER_RE = re.compile(
    r"^\s*<system-reminder>[\s\S]*</system-reminder>\s*$"
)


def select_fingerprint_prompt(messages) -> str:
    """Return CC v258's first valid text from the first non-meta user turn.

    Explicit ``isMeta`` flags and complete wire ``<system-reminder>`` blocks do
    not contribute.  A ``<session>...`` side-query block is ordinary text.
    """
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "user" or msg.get("isMeta") is True:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            blocks = ({"type": "text", "text": content},)
        elif isinstance(content, list):
            blocks = content
        else:
            blocks = ()
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text" or block.get("isMeta") is True:
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text:
                continue
            if _SYSTEM_REMINDER_RE.fullmatch(text):
                continue
            return text
    return ""


def _js_utf16_selected_chars(text: str, indices=FINGERPRINT_INDICES) -> str:
    raw = text.encode("utf-16-le", errors="surrogatepass")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    selected = []
    for index in indices:
        if index >= len(units):
            selected.append("0")
            continue
        unit = units[index]
        # Selecting one half of a surrogate pair yields a lone JS surrogate.
        # Node/Bun's UTF-8 encoder hashes that value as U+FFFD.
        selected.append("\ufffd" if 0xD800 <= unit <= 0xDFFF else chr(unit))
    return "".join(selected)


def compute_fingerprint(messages):
    prompt_text = select_fingerprint_prompt(messages)
    chars = _js_utf16_selected_chars(prompt_text)
    material = f"{FINGERPRINT_SALT}{chars}{CC_VERSION}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:3]


def _valid_prompt_id(value) -> str | None:
    text = str(value or "").strip()
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        return None


def ensure_request_context(body: dict) -> dict:
    """Return a shallow request copy with stable logical-request CC context."""
    out = dict(body or {})
    sid = str(out.get(PARROT_CC_SESSION_ID_KEY) or "").strip()
    if not sid:
        sid = str(uuid.uuid4())
    out[PARROT_CC_SESSION_ID_KEY] = sid
    prompt_id = _valid_prompt_id(out.get(PARROT_CC_PROMPT_ID_KEY))
    out[PARROT_CC_PROMPT_ID_KEY] = prompt_id or str(uuid.uuid4())
    return out


def request_context_from(body: dict) -> dict:
    return {
        key: body[key]
        for key in CC_REQUEST_CONTEXT_KEYS
        if isinstance(body, dict) and key in body
    }


def build_system_blocks(
    messages,
    *,
    inject_cache=True,
    fingerprint_value=None,
    prompt_id=None,
    workload=None,
    is_subagent=False,
    prev_req=None,
):
    fp = fingerprint_value or compute_fingerprint(messages)
    version = f"{CC_VERSION}.{fp}"
    cfg = load_config()
    cch_mode = _normalize_cch_mode(cfg.get("cch_mode", "dynamic"))
    blocks = []
    if cch_mode != "disabled":
        parts = [f"cc_version={version}", f"cc_entrypoint={CC_ENTRYPOINT}"]
        if cch_mode == "dynamic":
            parts.append("cch=00000")
        elif cch_mode == "static":
            parts.append(f"cch={_normalize_cch_value(cfg.get('cch_static_value', '00000'))}")
        if isinstance(workload, str) and workload.strip():
            parts.append(f"cc_workload={workload.strip()}")
        if is_subagent is True:
            parts.append("cc_is_subagent=true")
        if isinstance(prev_req, str) and re.fullmatch(r"req_[A-Za-z0-9_-]{1,36}", prev_req):
            parts.append(f"cc_prev_req={prev_req}")
        valid_prompt_id = _valid_prompt_id(prompt_id)
        if valid_prompt_id:
            parts.append(f"cc_prompt_id={valid_prompt_id}")
        attribution = "x-anthropic-billing-header: " + "; ".join(parts) + ";"
        blocks.append({"type": "text", "text": attribution})
    cc_block = {
        "type": "text",
        "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
    }
    if inject_cache:
        cc_block["cache_control"] = {"type": "ephemeral"}
    blocks.append(cc_block)
    return blocks


def inject_user_system_to_messages(messages, user_system):
    if not user_system:
        if messages and messages[0].get("role") != "user":
            messages = list(messages)
            messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "..."}]})
        return messages
    system_text = user_system if isinstance(user_system, str) else ""
    if isinstance(user_system, list):
        parts = []
        for block in user_system:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        system_text = "\n\n".join(parts)
    if not system_text.strip():
        if messages and messages[0].get("role") != "user":
            messages = list(messages)
            messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "..."}]})
        return messages
    messages = list(messages)
    messages.insert(0, {"role": "user", "content": [{"type": "text", "text": system_text}]})
    messages.insert(1, {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]})
    return messages



# ─── Message/content block normalization ───
# Top-level request tools are a tagged union, so unknown `type` values must be
# stripped there. Message content blocks are also tagged by `type`, but unlike
# tools they can contain JSON Schema-like nested payloads (tool input/result
# content, images, documents, citations). Keep normalization shallow: only clean
# known API-bound content block wrappers and never recurse into arbitrary nested
# JSON.

_CONTENT_BLOCK_ALLOWED_KEYS = {
    "text": {"type", "text", "citations", "cache_control"},
    "image": {"type", "source", "cache_control"},
    "document": {"type", "source", "title", "context", "citations", "cache_control"},
    "search_result": {"type", "source", "title", "content", "citations", "cache_control"},
    "tool_use": {"type", "id", "name", "input", "cache_control"},
    "tool_result": {"type", "tool_use_id", "content", "is_error", "cache_control", "cache_reference"},
    "thinking": {"type", "thinking", "signature", "cache_control"},
    "redacted_thinking": {"type", "data", "cache_control"},
    "server_tool_use": {"type", "id", "name", "input", "cache_control"},
    "web_search_tool_result": {"type", "tool_use_id", "content", "cache_control"},
    "web_fetch_tool_result": {"type", "tool_use_id", "content", "cache_control"},
    "code_execution_tool_result": {"type", "tool_use_id", "content", "cache_control"},
    "bash_code_execution_tool_result": {"type", "tool_use_id", "content", "cache_control"},
    "text_editor_code_execution_tool_result": {"type", "tool_use_id", "content", "cache_control"},
    "mcp_tool_use": {"type", "id", "name", "input", "server_name", "cache_control"},
    "mcp_tool_result": {"type", "tool_use_id", "content", "cache_control"},
    "container_upload": {"type", "file_id", "cache_control"},
    "tool_search_tool_result": {"type", "tool_use_id", "content", "cache_control"},
    "compaction": {"type", "content", "cache_control"},
}
_MESSAGE_ALLOWED_KEYS = {"role", "content", "name"}


def _normalize_content_block(block):
    if not isinstance(block, dict):
        return block
    btype = block.get("type")
    allowed = _CONTENT_BLOCK_ALLOWED_KEYS.get(btype)
    if allowed is None:
        # Unknown content block tags should pass through. They may be newly added
        # Anthropic beta blocks; stripping fields here would be more dangerous
        # than letting upstream validate them.
        return dict(block)
    out = {k: v for k, v in block.items() if k in allowed}
    if btype == "tool_use" and "caller" in block:
        # Claude Code strips tool-search-only caller unless the tool-search beta
        # is active. Parrot does not currently enable that beta on inbound
        # history, so keep the safe standard API shape.
        out.pop("caller", None)
    if btype in ("tool_use", "server_tool_use", "mcp_tool_use") and isinstance(out.get("input"), str):
        try:
            out["input"] = json.loads(out["input"]) if out["input"] else {}
        except Exception:
            pass
    return out


def _normalize_message_for_api(msg):
    if not isinstance(msg, dict):
        return msg
    out = {k: v for k, v in msg.items() if k in _MESSAGE_ALLOWED_KEYS}
    content = out.get("content")
    if isinstance(content, list):
        out["content"] = [_normalize_content_block(block) for block in content]
    return out


def _normalize_messages_for_api(messages):
    return [_normalize_message_for_api(msg) for msg in (messages or [])]

# ─── Cache breakpoints ────────────────────────────────────────────

def _inject_cache_on_msg(msg):
    msg = dict(msg)
    content = msg.get("content")
    if isinstance(content, list) and content:
        content = list(content)
        last_block = dict(content[-1])
        last_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        content[-1] = last_block
        msg["content"] = content
    elif isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    return msg


def _msg_has_cache_control(msg):
    """检查消息的 content block 中是否已有 cache_control"""
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                return True
    return False


def _strip_message_cache_control(messages):
    """移除客户端在 messages 中设置的所有 cache_control 标记。
    客户端会在最后一条 user message 上设置 cache_control，当下一轮对话中该消息
    不再是最后一条时，标记消失导致内容块变化，使前缀缓存失效。
    由代理统一管理 cache_control 可确保前缀在连续请求间保持稳定。"""
    result = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            changed = False
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    changed = True
                    break
            if changed:
                msg = dict(msg)
                new_content = []
                for block in content:
                    if isinstance(block, dict) and "cache_control" in block:
                        block = {k: v for k, v in block.items() if k != "cache_control"}
                    new_content.append(block)
                msg["content"] = new_content
            result.append(msg)
        else:
            result.append(msg)
    return result


_ANTHROPIC_TOOL_ALLOWED_KEYS = {
    "name",
    "description",
    "input_schema",
    "cache_control",
    "strict",
    "eager_input_streaming",
    "defer_loading",
}
_ANTHROPIC_SERVER_TOOL_TYPE_PREFIXES = (
    "web_search_",
    "web_fetch_",
    "computer_",
    "text_editor_",
    "bash_",
    "memory_",
    "advisor_",
)


def _is_anthropic_server_tool(tool):
    """Return True for Anthropic built-in/server-tool union variants.

    Normal client tools built by Claude Code are plain objects with name,
    description and input_schema; they do not include a top-level type. Some
    SDK/proxy clients accidentally send OpenAI-style or namespace-tagged tools
    (`type: "function"`, `type: "namespace"`) to the Anthropic endpoint.
    Anthropic validates `tools[]` as a tagged union, so an unknown top-level
    `type` triggers: Input tag 'namespace' ... does not match any expected.

    Keep known Anthropic server-tool variants intact; sanitize ordinary client
    tools to Claude Code's outbound shape.
    """
    if not isinstance(tool, dict):
        return False
    t = tool.get("type")
    return isinstance(t, str) and (
        t in {"web_search", "web_search_20250305"}
        or any(t.startswith(prefix) for prefix in _ANTHROPIC_SERVER_TOOL_TYPE_PREFIXES)
    )


def _normalize_anthropic_tool(tool, *, preserve_cache_control=False):
    """Normalize one downstream Anthropic tool to the CC-compatible API shape.

    Mirrors Claude Code's toolToAPISchema choke point: standard custom tools are
    serialized with only name/description/input_schema plus approved optional
    beta/cache fields. Unknown top-level `type` tags are stripped unless the
    tool is a known Anthropic server-tool variant.
    """
    if not isinstance(tool, dict):
        return tool
    if _is_anthropic_server_tool(tool):
        if preserve_cache_control:
            return dict(tool)
        return {k: v for k, v in tool.items() if k != "cache_control"}

    normalized = {
        k: v for k, v in tool.items()
        if k in _ANTHROPIC_TOOL_ALLOWED_KEYS and (preserve_cache_control or k != "cache_control")
    }

    # OpenAI/chat-style compatibility: {type:function,function:{name,description,parameters}}
    fn = tool.get("function")
    if isinstance(fn, dict):
        normalized.setdefault("name", fn.get("name"))
        if "description" in fn:
            normalized.setdefault("description", fn.get("description"))
        if "parameters" in fn and "input_schema" not in normalized:
            normalized["input_schema"] = fn.get("parameters")

    if "input_schema" not in normalized and "parameters" in tool:
        normalized["input_schema"] = tool.get("parameters")

    # Anthropic client tools require an object input schema. If a malformed
    # namespace/function wrapper omitted it, provide the minimal object schema
    # rather than forwarding an unknown union tag that produces a hard 400.
    if not isinstance(normalized.get("input_schema"), dict):
        normalized["input_schema"] = {"type": "object", "properties": {}}

    return normalized


def _strip_tool_cache_control(tools, *, preserve_cache_control=False):
    """移除客户端在 tools 上设置的 cache_control，并规范化普通 client tools。"""
    return [_normalize_anthropic_tool(tool, preserve_cache_control=preserve_cache_control) for tool in tools]


def add_cache_breakpoints(messages):
    """注入缓存断点。断点位置：倒数第二个 user turn + 最后一条消息。
    加上 system + tools 共 4 个断点（上限）。
    注意：调用前应先 _strip_message_cache_control 清除客户端标记。"""
    if not messages:
        return messages
    messages = [dict(m) for m in messages]

    # 1. 最后一条消息
    messages[-1] = _inject_cache_on_msg(messages[-1])

    # 2. 倒数第二个 user turn：缓存多轮对话历史
    #    确保会话前缀在连续请求间可被复用
    if len(messages) >= 4:
        user_count = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                user_count += 1
                if user_count == 2:
                    messages[i] = _inject_cache_on_msg(messages[i])
                    break

    return messages


_THINKING_BLOCK_TYPES = {"thinking", "redacted_thinking"}
_THINKING_REMOVED_TEXT = "[Thinking removed]"


def _strip_assistant_thinking_blocks(messages):
    """移除历史 assistant content 中的 thinking / redacted_thinking block。

    Anthropic 会校验历史 thinking block 的签名、位置与是否可回放。很多下游
    客户端会把上一轮完整 assistant response 原样塞回下一轮，导致
    `messages.N.content.M: thinking or redacted_thinking ... invalid_blocks`。
    Claude Code 遇到签名类 400 也会 strip signed thinking blocks 后重试。

    这里在出站前做确定性清洗：只清理历史 assistant content block，不影响顶层
    request `thinking` 参数；若 assistant 被清到空内容，则补一个文本占位，避免
    空 assistant message 继续触发 invalid_blocks。函数不原地修改入参。
    """
    result = []
    for msg in messages or []:
        if not (isinstance(msg, dict) and msg.get("role") == "assistant"):
            result.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue
        new_content = []
        changed = False
        for block in content:
            if isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES:
                changed = True
                continue
            new_content.append(block)
        if not changed:
            result.append(msg)
            continue
        if not new_content:
            new_content = [{"type": "text", "text": _THINKING_REMOVED_TEXT}]
        new_msg = dict(msg)
        new_msg["content"] = new_content
        result.append(new_msg)
    return result


# ─── Metadata ──────────────────────────────────────────────────────

def build_metadata(email="", session_id=None):
    # v258 wire keeps account_uuid empty even when an OAuth account email exists.
    sid = session_id or str(uuid.uuid4())
    return {"user_id": json.dumps(
        {"device_id": DEVICE_ID, "account_uuid": "", "session_id": sid},
        separators=(",", ":"))}


# ─── Tool-name compatibility ──────────────────────────────────────

TOOL_NAME_REWRITES = {"sessions_": "cc_sess_", "session_": "cc_ses_"}  # 静态前缀映射（保留兼容）

# 生成混淆用的可读假名前缀
_FAKE_PREFIXES = [
    "analyze_", "compute_", "fetch_", "generate_", "lookup_", "modify_",
    "process_", "query_", "render_", "resolve_", "sync_", "update_",
    "validate_", "convert_", "extract_", "manage_", "monitor_", "parse_",
    "review_", "search_", "transform_", "handle_", "invoke_", "notify_",
]


def _stable_tool_names_seed(tool_names) -> int:
    """Return a process-independent seed for dynamic tool-name mapping.

    Python's built-in hash() is salted per process. Using it here made OAuth
    tool aliases change after every Parrot restart, which changes the prompt
    cache key for long Claude Code requests and can force a full cache rewrite.
    """
    raw = json.dumps(list(tool_names), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _build_dynamic_tool_map(tool_names, threshold=5):
    """当 tools 数量超过 threshold 时，生成原名→假名的动态映射。
    返回 dict 或 None（无需映射时）。
    """
    if len(tool_names) <= threshold:
        return None
    mapping = {}
    available = list(_FAKE_PREFIXES)
    rng = random.Random(_stable_tool_names_seed(tool_names))  # 跨进程稳定，避免重启打碎 prompt cache
    rng.shuffle(available)
    for i, name in enumerate(tool_names):
        prefix = available[i % len(available)]
        fake = f"{prefix}{name[:3]}{i:02d}"
        mapping[name] = fake
    return mapping


def _sanitize_tool_name(name, dynamic_map=None):
    # 先尝试动态映射
    if dynamic_map and name in dynamic_map:
        return dynamic_map[name]
    # 兜底：静态前缀映射
    for prefix, replacement in TOOL_NAME_REWRITES.items():
        if name.startswith(prefix):
            return replacement + name[len(prefix):]
    return name


def _restore_tool_name_value(name, dynamic_map=None):
    """只还原协议里的工具名值，避免全 chunk 替换误伤正文文本。"""
    if not isinstance(name, str):
        return name
    if dynamic_map:
        for original, fake in dynamic_map.items():
            if name == fake:
                return original
    for prefix, replacement in TOOL_NAME_REWRITES.items():
        if name.startswith(replacement):
            return prefix + name[len(replacement):]
    return name


def _restore_tool_names_in_obj(obj, dynamic_map=None):
    """递归处理 JSON 对象，但只改 tool_use/server_tool_use 的 name 字段。"""
    if isinstance(obj, list):
        return [_restore_tool_names_in_obj(x, dynamic_map) for x in obj]
    if not isinstance(obj, dict):
        return obj

    out = {k: _restore_tool_names_in_obj(v, dynamic_map) for k, v in obj.items()}
    if out.get("type") in ("tool_use", "server_tool_use") and "name" in out:
        out["name"] = _restore_tool_name_value(out.get("name"), dynamic_map)
    return out


def _restore_tool_names_in_name_fields_bytes(data, dynamic_map=None):
    """JSON 不完整时的兜底：只替换原始字节里的 "name":"..." 字段值。"""
    if not isinstance(data, (bytes, bytearray)):
        return data

    def repl(match):
        value = match.group(3).decode("utf-8", errors="replace")
        restored = _restore_tool_name_value(value, dynamic_map)
        if restored == value:
            return match.group(0)
        quote = match.group(2)
        return b'"name"' + match.group(1) + quote + restored.encode("utf-8") + quote

    # 只匹配未转义的 JSON name 字段，不碰 text_delta 里的普通正文。
    return re.sub(rb'"name"(\s*:\s*)(["\'])([^"\']*)\2', repl, bytes(data))


def _restore_tool_names_in_json_bytes(data, dynamic_map=None):
    try:
        obj = json.loads(data)
    except Exception:
        return _restore_tool_names_in_name_fields_bytes(data, dynamic_map)
    obj = _restore_tool_names_in_obj(obj, dynamic_map)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _restore_tool_names_in_sse_chunk(chunk_bytes, dynamic_map=None):
    try:
        text = chunk_bytes.decode("utf-8")
    except Exception:
        return chunk_bytes

    out = []
    changed = False
    for line in text.splitlines(keepends=True):
        line_body = line.rstrip("\r\n")
        newline = line[len(line_body):]
        if not line_body.startswith("data:"):
            out.append(line)
            continue
        data = line_body[5:].strip()
        if not data or data == "[DONE]":
            out.append(line)
            continue
        restored = _restore_tool_names_in_json_bytes(data.encode("utf-8"), dynamic_map)
        if restored != data.encode("utf-8"):
            changed = True
            out.append("data: " + restored.decode("utf-8") + newline)
        else:
            out.append(line)
    if not changed:
        return chunk_bytes
    return "".join(out).encode("utf-8")


def _restore_tool_names_in_chunk(chunk_bytes, dynamic_map=None):
    # SSE：只处理 data 行里的 JSON，不碰 event 行 / 正文里的普通文本。
    if b"data:" in chunk_bytes:
        return _restore_tool_names_in_sse_chunk(chunk_bytes, dynamic_map)
    # 非流式 JSON：只处理 tool_use/server_tool_use.name。
    return _restore_tool_names_in_json_bytes(chunk_bytes, dynamic_map)


# ─── Opus 4.7/4.8 adaptive thinking 适配 ───
# 旧客户端发的是 thinking.type=enabled + budget_tokens，无法表达 Opus 4.7/4.8
# 的 effort 档位。这里仅对 opus-4-7 / opus-4-8 把旧式写法升级为 adaptive + effort，
# disabled（明确不思考）与客户端已自带 effort 的请求都原样保留。

_OPUS_ADAPTIVE_MODELS = ("claude-opus-4-7", "claude-opus-4-8")


def _budget_to_effort(budget):
    try:
        b = int(budget)
    except (TypeError, ValueError):
        return "max"
    if b >= 16384:
        return "max"
    if b >= 8192:
        return "xhigh"
    if b >= 2048:
        return "high"
    return "medium"


def apply_opus_adaptive_thinking(payload, model):
    """就地把 Opus 4.7/4.8 的旧式 thinking.enabled+budget_tokens 升级为
    thinking.type=adaptive + output_config.effort。返回 payload 本身。"""
    if not isinstance(model, str) or not any(model.startswith(m) for m in _OPUS_ADAPTIVE_MODELS):
        return payload
    t = payload.get("thinking")
    if not isinstance(t, dict):
        return payload
    ttype = t.get("type")
    if ttype not in ("enabled", "adaptive"):
        # disabled / 其它：明确不思考，保持原样
        return payload
    effort = _budget_to_effort(t.get("budget_tokens"))
    payload["thinking"] = {"type": "adaptive"}
    oc = payload.get("output_config")
    if not (isinstance(oc, dict) and oc.get("effort")):
        # 客户端未显式指定 effort 时才注入
        payload["output_config"] = {**(oc if isinstance(oc, dict) else {}), "effort": effort}
    return payload


# ─── Request body profiles ─────────────────────────────────────────

_SIDE_QUERY_OUTPUT_CONFIG = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        },
    },
}


def _is_fable_model(model) -> bool:
    return str(model or "").lower() == "claude-fable-5"


def _is_opus_5_model(model) -> bool:
    value = str(model or "").lower()
    return value == "claude-opus-5" or value.startswith("claude-opus-5-")


def _is_side_query_request(body: dict, model=None, *, messages=None) -> bool:
    if not str(model or body.get("model") or "").lower().startswith("claude-haiku-"):
        return False
    output_config = body.get("output_config")
    if isinstance(output_config, dict) and isinstance(output_config.get("format"), dict):
        return True
    prompt = select_fingerprint_prompt(messages if messages is not None else body.get("messages", []))
    return prompt.lstrip().startswith("<session>")


def transform_request(body, email="", session_id=None, *, auth_mode="api_key"):
    explicit_cache_control = cache_hints.has_anthropic_cache_control(body)
    original_messages = body.get("messages", [])
    fingerprint_value = compute_fingerprint(original_messages)
    model = body.get("model", "claude-sonnet-4-20250514")
    side_query = _is_side_query_request(body, model, messages=original_messages)
    fable_main = _is_fable_model(model) and not side_query
    # No authoritative OAuth Fable-main body exists.  Preserve explicit fields
    # there and apply only the observed auth/header differences.
    auto_profile = not (fable_main and auth_mode == "oauth")

    sid = str(session_id or body.get(PARROT_CC_SESSION_ID_KEY) or "").strip() or str(uuid.uuid4())
    prompt_id = None if side_query else body.get(PARROT_CC_PROMPT_ID_KEY)
    if prompt_id is None and not side_query:
        prompt_id = str(uuid.uuid4())

    messages = inject_user_system_to_messages(original_messages, body.get("system"))
    messages = _normalize_messages_for_api(messages)
    messages = _strip_assistant_thinking_blocks(messages)
    if not explicit_cache_control:
        messages = _strip_message_cache_control(messages)
    system_blocks = build_system_blocks(
        original_messages,
        inject_cache=False,
        fingerprint_value=fingerprint_value,
        prompt_id=prompt_id,
    )

    dynamic_tool_map = None
    if body.get("tools"):
        raw_tools = body["tools"]
        tool_names = [t.get("name") for t in raw_tools if isinstance(t, dict) and t.get("name")]
        dynamic_tool_map = _build_dynamic_tool_map(tool_names)
        if dynamic_tool_map:
            print(f"  [tool] dynamic mapping {len(dynamic_tool_map)} tools")

    # v258 insertion order is the wire order.  Optional compatibility fields are
    # inserted adjacent to their native section and all private fields are consumed.
    payload = {"model": model, "messages": messages, "system": system_blocks}

    if body.get("tools"):
        tools = _strip_tool_cache_control(
            [dict(t) if isinstance(t, dict) else t for t in body["tools"]],
            preserve_cache_control=explicit_cache_control,
        )
        for tool in tools:
            if isinstance(tool, dict) and "name" in tool:
                tool["name"] = _sanitize_tool_name(tool["name"], dynamic_tool_map)
        payload["tools"] = tools

    top_cache = cache_hints.top_level_cache_control(body)
    if top_cache:
        payload["cache_control"] = top_cache
    if not side_query or explicit_cache_control:
        cache_hints.apply_anthropic_block_cache_breakpoints(
            payload,
            default_cache_control={"type": "ephemeral"},
        )

    if "tool_choice" in body:
        tool_choice = body["tool_choice"]
        if isinstance(tool_choice, dict) and "name" in tool_choice:
            tool_choice = dict(tool_choice)
            tool_choice["name"] = _sanitize_tool_name(tool_choice["name"], dynamic_tool_map)
        payload["tool_choice"] = tool_choice

    payload["metadata"] = build_metadata(email, session_id=sid)
    payload["max_tokens"] = body.get("max_tokens", 32000 if side_query else 64000)

    if request_wants_fast_mode(body):
        payload["speed"] = "fast"

    if "thinking" in body:
        payload["thinking"] = body["thinking"]
    elif side_query:
        payload["thinking"] = {"type": "disabled"}
    elif auto_profile:
        payload["thinking"] = {"type": "adaptive", "display": "omitted"}

    if "context_management" in body:
        payload["context_management"] = body["context_management"]
    elif not side_query and auto_profile:
        thinking = payload.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"):
            payload["context_management"] = {
                "edits": [{"type": "clear_thinking_20251015", "keep": "all"}],
            }

    if "temperature" in body:
        payload["temperature"] = body["temperature"]
    elif side_query:
        payload["temperature"] = 1

    if "fallbacks" in body:
        payload["fallbacks"] = body["fallbacks"]
    elif fable_main and auto_profile:
        payload["fallbacks"] = "default"

    if "output_config" in body:
        payload["output_config"] = body["output_config"]
    elif side_query:
        payload["output_config"] = json.loads(json.dumps(_SIDE_QUERY_OUTPUT_CONFIG))
    elif auto_profile:
        payload["output_config"] = {"effort": "high"}

    if "diagnostics" in body:
        payload["diagnostics"] = body["diagnostics"]
    elif not side_query and auto_profile:
        payload["diagnostics"] = {"previous_message_id": None}

    payload["stream"] = body.get("stream", False)
    return payload, dynamic_tool_map


# ─── CCH v258 signature ────────────────────────────────────────────

CCH_SEED = 0x4D659218E32A3268
CCH_PLACEHOLDER = b"cch=00000"
_BILLING_PREFIX = "x-anthropic-billing-header: "
_CCH_IN_BILLING_RE = re.compile(r"(?<=; )cch=[0-9a-f]{5}(?=;)")


def _generated_billing_block(payload_dict):
    system = payload_dict.get("system") if isinstance(payload_dict, dict) else None
    if not isinstance(system, list) or not system or not isinstance(system[0], dict):
        return None
    block = system[0]
    text = block.get("text")
    if block.get("type") != "text" or not isinstance(text, str) or not text.startswith(_BILLING_PREFIX):
        return None
    return block


def _replace_generated_billing_cch(payload_dict, replacement: str):
    """Copy the payload while changing only Parrot's generated billing block."""
    billing = _generated_billing_block(payload_dict)
    if billing is None:
        return payload_dict
    text = billing.get("text", "")
    updated = _CCH_IN_BILLING_RE.sub(f"cch={replacement}", text, count=1)
    if updated == text:
        return payload_dict
    out = dict(payload_dict)
    system = list(payload_dict["system"])
    system[0] = {**billing, "text": updated}
    out["system"] = system
    return out


def _cch_hash_value(value, *, top_level=False):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if top_level and key == "max_tokens":
                continue
            if key == "model" and isinstance(item, str):
                out[key] = ""
            else:
                out[key] = _cch_hash_value(item)
        return out
    if isinstance(value, list):
        return [_cch_hash_value(item) for item in value]
    return value


def cch_hash_view(payload_dict) -> bytes:
    placeholder_payload = _replace_generated_billing_cch(payload_dict, "00000")
    view = _cch_hash_value(placeholder_payload, top_level=True)
    return json.dumps(view, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def compute_cch(payload_dict) -> str:
    digest = xxhash.xxh64(cch_hash_view(payload_dict), seed=CCH_SEED).intdigest()
    return f"{digest & 0xFFFFF:05x}"


def _generated_cch_offset(body_bytes: bytes, payload_dict) -> int | None:
    billing = _generated_billing_block(payload_dict)
    system = payload_dict.get("system") if isinstance(payload_dict, dict) else None
    if billing is None or not isinstance(system, list):
        return None
    system_bytes = json.dumps(
        system, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    field_bytes = b'"system":' + system_bytes
    field_start = body_bytes.find(field_bytes)
    marker_offset = field_bytes.find(CCH_PLACEHOLDER)
    if field_start < 0 or marker_offset < 0:
        return None
    return field_start + marker_offset


def sign_body(payload_dict):
    # Serialize the final wire body exactly once.  Hash normalization is a
    # separate view and never becomes the transmitted payload.
    body_bytes = json.dumps(
        payload_dict, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    cfg = load_config()
    if _normalize_cch_mode(cfg.get("cch_mode", "dynamic")) != "dynamic":
        return body_bytes
    offset = _generated_cch_offset(body_bytes, payload_dict)
    if offset is None:
        return body_bytes
    cch = compute_cch(payload_dict).encode("ascii")
    start = offset + len(b"cch=")
    # Patch only the located generated block; user message text is untouched and
    # no post-signature JSON serialization can reorder the wire payload.
    return body_bytes[:start] + cch + body_bytes[start + 5:]


# ─── v258 upstream headers ─────────────────────────────────────────

_STAINLESS_HEADERS = {
    "X-Stainless-Arch": "x64",
    "X-Stainless-Lang": "js",
    "X-Stainless-OS": "Linux",
    "X-Stainless-Package-Version": "0.112.1",
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v26.3.0",
    "X-Stainless-Timeout": "600",
}


_CONTEXT_1M_MODEL_MARKER_RE = re.compile(
    # Claude Code 官方模型选择器使用 `sonnet[1m]` / `opus[1m]`，并在出站前
    # 把 bracket marker 从 body.model 剥掉；`-1m` / `context-1m` / Cursor
    # sidecar 的 `~1000000` 都是 Parrot 兼容扩展。
    r"\[(?:1|2)m\]|"
    r"(^|[~\-_./:\s])(?:1m|1000k|1000000)(?:$|[~\-_./:\s])|"
    r"1m[-_\s]?context|context[-_\s]?1m",
    re.I,
)

_CONTEXT_1M_MODEL_SUFFIX_RE = re.compile(
    r"(?:\[(?:1|2)m\]|"
    r"[-_./:\s]context[-_\s]?1m|"
    r"[-_./:\s]1m[-_\s]?context|"
    r"[~\-_./:\s](?:1m|1000k|1000000))$",
    re.I,
)

_CONTEXT_WINDOW_FIELDS = (
    "max_context", "max_context_tokens", "maxContext", "maxContextTokens",
    "context_window", "context_window_tokens", "contextWindow", "contextWindowTokens",
    "max_context_window", "max_context_window_tokens",
    "model_context_window", "model_context_window_tokens",
    "context_length", "context_length_tokens", "max_input_tokens", "maxInputTokens",
)

_CONTEXT_1M_FLAG_FIELDS = (
    "context_1m", "use_1m_context", "use1mContext", "long_context", "longContext",
    "enable_long_context", "enableLongContext",
)


def parse_beta_header(value) -> list[str]:
    """把下游 anthropic-beta / betas 表达解析为 beta 字符串列表。"""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            raw_items.extend(parse_beta_header(item))
        return raw_items
    return [x.strip() for x in str(value).split(",") if x.strip()]


def _has_beta(beta_list, beta: str) -> bool:
    return beta in set(parse_beta_header(beta_list))


def request_wants_fast_mode(body=None, *, downstream_betas=None) -> bool:
    """下游是否显式要求 Claude Fast mode。

    Anthropic Fast mode 的 wire 形态是二者同时出现：
      - `anthropic-beta: fast-mode-2026-02-01`
      - body `speed: "fast"`

    下游可能通过 HTTP header、SDK 风格的 betas 字段，或 Parrot 内部
    `_parrot_wants_fast_mode` 信号表达同一个意图；这里统一折成布尔值。
    """
    if _has_beta(downstream_betas, FAST_MODE_BETA):
        return True

    if isinstance(body, dict):
        if body.get(PARROT_WANTS_FAST_MODE_KEY) is True:
            return True
        if str(body.get("speed") or "").strip().lower() == "fast":
            return True
        for key in ("betas", "anthropic_beta", "anthropic-beta", "anthropic_betas"):
            if _has_beta(body.get(key), FAST_MODE_BETA):
                return True
    return False


def _value_requests_1m_context(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value >= ONE_M_CONTEXT_TOKENS
    text = str(value or "").strip().lower().replace(",", "")
    if not text:
        return False
    if text.isdigit():
        try:
            return int(text) >= ONE_M_CONTEXT_TOKENS
        except Exception:
            return False
    return bool(_CONTEXT_1M_MODEL_MARKER_RE.search(text))


def _value_disables_1m_context(value) -> bool:
    """Whether an explicit boolean-style downstream control disables 1M."""
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value <= 0
    text = str(value or "").strip().lower()
    return text in {"0", "false", "off", "no", "disable", "disabled", "normal", "standard"}


def _model_name_requests_context_1m(model) -> bool:
    return bool(_CONTEXT_1M_MODEL_MARKER_RE.search(str(model or "")))


def strip_context_1m_model_marker(model):
    """把显式 1M 模型别名还原成真实上游 model。

    例：`claude-sonnet-4-6[1m]` / `claude-sonnet-4-6-1m` / `...-context-1m`
    都在 Parrot 内部归一成 `claude-sonnet-4-6`，显式 1M 意图由
    `request_wants_context_1m()` 单独记录，不把 marker 透传给调度/上游。
    """
    if not isinstance(model, str):
        return model
    raw = model.strip()
    if not raw:
        return model
    stripped = _CONTEXT_1M_MODEL_SUFFIX_RE.sub("", raw).strip()
    return stripped or raw


def _iter_context_window_values(body: dict):
    if not isinstance(body, dict):
        return
    for key in _CONTEXT_WINDOW_FIELDS:
        if key in body:
            yield body[key]
    for nested_key in ("extra_body", "extraBody", "metadata"):
        nested = body.get(nested_key)
        if isinstance(nested, dict):
            for key in _CONTEXT_WINDOW_FIELDS:
                if key in nested:
                    yield nested[key]


def request_wants_context_1m(body=None, *, downstream_betas=None,
                             original_model=None, resolved_model=None) -> bool:
    """下游是否显式要求 1M context。

    只把明确意图当成 true：
      - anthropic-beta / body.betas 含 context-1m；
      - 原始模型名/别名含 1m/context-1m 标记；
      - 下游显式传 context window/max context 约 1,000,000；
      - Parrot 扩展布尔开关 long_context / enableLongContext 等。
    注意：`max_tokens` 是输出上限，不是上下文窗口，故不参与判断。
    """
    if _has_beta(downstream_betas, CONTEXT_1M_BETA):
        return True

    if isinstance(body, dict):
        for key in ("betas", "anthropic_beta", "anthropic-beta", "anthropic_betas"):
            if _has_beta(body.get(key), CONTEXT_1M_BETA):
                return True
        for key in _CONTEXT_1M_FLAG_FIELDS:
            if key in body and _value_requests_1m_context(body[key]):
                return True
        for value in _iter_context_window_values(body) or []:
            if _value_requests_1m_context(value):
                return True

    for candidate in (original_model, resolved_model, body.get("model") if isinstance(body, dict) else None):
        if _model_name_requests_context_1m(candidate):
            return True
    return False


def request_context_1m_override(body=None, *, downstream_betas=None,
                                original_model=None, resolved_model=None) -> bool | None:
    """Return an explicit per-request 1M override, or ``None`` when absent.

    Positive model/header/window signals enable 1M.  A boolean-style body flag
    such as ``long_context=false`` explicitly disables an account/model default.
    The private Parrot field is authoritative on retries and translated rounds.
    """
    if isinstance(body, dict):
        internal = body.get(PARROT_WANTS_CONTEXT_1M_KEY)
        if isinstance(internal, bool):
            return internal
    if request_wants_context_1m(
        body,
        downstream_betas=downstream_betas,
        original_model=original_model,
        resolved_model=resolved_model,
    ):
        return True
    if isinstance(body, dict):
        for key in _CONTEXT_1M_FLAG_FIELDS:
            if key in body and _value_disables_1m_context(body[key]):
                return False
    return None


def should_default_context_1m(model) -> bool:
    """v258 capture default: Opus 5 carries context-1m; Opus 4.8 does not."""
    return _is_opus_5_model(model)


def _is_opus_4_plus_model(model) -> bool:
    m = str(model or "").lower()
    return bool(re.match(r"^claude-opus-(?:[4-9]|[1-9]\d)(?:[-.]|$)", m))


def model_supports_context_1m(model) -> bool:
    """CC 1M context 能力口径：Opus 4+ 默认可开；Sonnet 4.5/4.6 仅显式可开。"""
    m = str(model or "").lower()
    if _is_opus_4_plus_model(m):
        return True
    return m.startswith(("claude-sonnet-4-5", "claude-sonnet-4-6"))


def model_supports_mid_conversation_system(model) -> bool:
    """CC v2.1.258 mid-conversation-system compatibility model gate."""
    m = str(model or "").lower()
    if m.startswith("claude-3-"):
        return True
    return m.startswith((
        "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-5",
        "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
        "claude-sonnet-4-0", "claude-sonnet-4-5", "claude-sonnet-4-6",
        "claude-haiku-4-5", "claude-fable-5",
    ))


def _payload_has_ttl_1h(obj) -> bool:
    if isinstance(obj, dict):
        cc = obj.get("cache_control")
        if isinstance(cc, dict) and str(cc.get("ttl", "")).lower() == "1h":
            return True
        return any(_payload_has_ttl_1h(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_payload_has_ttl_1h(v) for v in obj)
    return False


def _insert_beta_before(out: list[str], beta: str, before: str) -> None:
    if beta in out:
        return
    try:
        index = out.index(before)
    except ValueError:
        out.append(beta)
    else:
        out.insert(index, beta)


def _insert_beta_after(out: list[str], beta: str, after: str) -> None:
    if beta in out:
        return
    try:
        index = out.index(after) + 1
    except ValueError:
        index = 0
    out.insert(index, beta)


def _wire_beta_profile(model=None, payload=None, *, auth_mode="api_key") -> list[str]:
    side_query = _is_side_query_request(
        payload or {}, model, messages=(payload or {}).get("messages", []),
    )
    if side_query:
        out = list(_SIDE_QUERY_BETAS)
    elif _is_fable_model(model):
        out = list(_FABLE_API_KEY_BETAS)
    elif _is_opus_5_model(model):
        out = list(_OPUS_5_BETAS)
    else:
        out = list(_MAIN_BETAS)

    if auth_mode == "oauth":
        # Observed auth difference: advisor remains while API-key-only fallback
        # betas disappear.  No unknown OAuth Fable body combination is invented.
        out = [b for b in out if b not in {
            SERVER_SIDE_FALLBACK_BETA, FALLBACK_CREDIT_BETA,
        }]
        _insert_beta_before(
            out,
            ADVISOR_TOOL_BETA,
            STRUCTURED_OUTPUTS_BETA if side_query else ADVANCED_TOOL_USE_BETA,
        )
    return out


def _messages_betas_for_request(model=None, betas=None, *, payload=None,
                                downstream_betas=None, original_model=None,
                                wants_context_1m=None, wants_fast_mode=None,
                                allow_any_model_context_1m=False,
                                auth_mode="api_key"):
    out = _wire_beta_profile(model, payload, auth_mode=auth_mode)
    if betas is not None:
        allowed = [b for b in parse_beta_header(betas) if b != OAUTH_BETA]
        known = set(BETAS)
        out = [b for b in out if b in allowed]
        out.extend(b for b in allowed if b not in known and b not in out)

    if wants_fast_mode is None:
        wants_fast_mode = request_wants_fast_mode(
            payload if isinstance(payload, dict) else None,
            downstream_betas=downstream_betas,
        )
    if wants_context_1m is None:
        explicit_context = request_wants_context_1m(
            payload if isinstance(payload, dict) else None,
            downstream_betas=downstream_betas,
            original_model=original_model,
            resolved_model=model,
        )
        wants_context_1m = explicit_context or should_default_context_1m(model)

    allow_context_1m = bool(wants_context_1m) and (
        bool(allow_any_model_context_1m) or model_supports_context_1m(model)
    )
    if not allow_context_1m:
        out = [b for b in out if b != CONTEXT_1M_BETA]
    elif betas is None or CONTEXT_1M_BETA in parse_beta_header(betas):
        _insert_beta_after(out, CONTEXT_1M_BETA, "claude-code-20250219")

    if wants_fast_mode and (betas is None or FAST_MODE_BETA in parse_beta_header(betas)):
        _insert_beta_after(out, FAST_MODE_BETA, "claude-code-20250219")
    else:
        out = [b for b in out if b != FAST_MODE_BETA]

    if isinstance(payload, dict) and _payload_has_ttl_1h(payload):
        if betas is None or EXTENDED_CACHE_TTL_BETA in parse_beta_header(betas):
            _insert_beta_before(out, EXTENDED_CACHE_TTL_BETA, CACHE_DIAGNOSIS_BETA)
    else:
        out = [b for b in out if b != EXTENDED_CACHE_TTL_BETA]
    return list(dict.fromkeys(out))


def build_upstream_headers(access_token, session_id=None, betas=None, *, auth_scheme="bearer",
                           auth_mode=None, model=None, payload=None, downstream_betas=None,
                           original_model=None, wants_context_1m=None,
                           wants_fast_mode=None, allow_any_model_context_1m=False):
    """Build the ordered v258 application headers for one Messages attempt.

    ``auth_mode`` controls only evidence-backed beta differences; third-party
    Bearer providers therefore do not silently acquire OAuth-specific behaviour.
    The initial request UUID is refreshed by ``http_runtime`` at every physical
    dispatch.  Brotli/zstd are deliberately not advertised without decoders.
    """
    sid = session_id or str(uuid.uuid4())
    effective_auth_mode = auth_mode or ("oauth" if auth_scheme == "bearer" else "api_key")
    beta_str = ",".join(_messages_betas_for_request(
        model=model, betas=betas, payload=payload,
        downstream_betas=downstream_betas, original_model=original_model,
        wants_context_1m=wants_context_1m,
        wants_fast_mode=wants_fast_mode,
        allow_any_model_context_1m=allow_any_model_context_1m,
        auth_mode=effective_auth_mode,
    ))

    headers = {"Accept": "application/json"}
    if auth_scheme != "api_key":
        headers["Authorization"] = f"Bearer {access_token}"
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = CLI_USER_AGENT
    headers["X-Claude-Code-Session-Id"] = sid
    headers.update(_STAINLESS_HEADERS)
    headers["anthropic-beta"] = beta_str
    headers["anthropic-dangerous-direct-browser-access"] = "true"
    headers["anthropic-version"] = "2023-06-01"
    if auth_scheme == "api_key":
        headers["x-api-key"] = access_token
    headers["x-app"] = "cli"
    headers["x-client-request-id"] = str(uuid.uuid4())
    headers["Accept-Encoding"] = "gzip, deflate"
    return headers
