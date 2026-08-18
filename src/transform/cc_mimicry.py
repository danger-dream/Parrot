"""CC 伪装（Claude Code CLI 模拟请求构造）。

⚠⚠⚠ 本模块是从 cc-proxy/server.py 的逐字移植 ⚠⚠⚠

任何修改都可能被 Anthropic 侧检测为异常流量导致账号封禁。允许的变动仅限：
  - 文件头路径 / device_id 文件名（`.cc_proxy_ids.json` → `.anthropic_proxy_ids.json`）
  - `build_metadata()` 接受 email 参数（原来从全局 oauth 读）
  - `transform_request()` 接受 email 参数，透传给 `build_metadata()`
  - 提供 `load_config()` 适配层把 anthropic-proxy 的 `cchMode` / `cchStaticValue`
    翻译成 cc-proxy 原 key 名（`cch_mode` / `cch_static_value`），这样下面所有
    函数体可以保留读 `cch_mode` 的原样写法，无需改动。

其它所有常量、随机种子、hash 算法、字节级边界、函数体逻辑 100% 与 cc-proxy 一致。
对比测试（tests/compare_transform.py）逐字节校验。
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

CC_VERSION = "2.1.156"
FINGERPRINT_SALT = "59cf53e54c78"
CC_ENTRYPOINT = "sdk-cli"
USER_TYPE = "external"

BETAS = [
    "claude-code-20250219",
    "fast-mode-2026-02-01",                 # Claude Fast mode：仅显式 speed=fast / 下游 beta 请求时才进 messages
    "context-1m-2025-08-07",               # 长上下文能力位：显式请求 1M 且模型支持时才进 messages
    "interleaved-thinking-2025-05-14",
    "context-management-2025-06-27",       # 仅最终 payload 含 context_management 时带
    "prompt-caching-scope-2026-01-05",
    "mid-conversation-system-2026-04-07",  # 模型白名单门控
    "effort-2025-11-24",
    "extended-cache-ttl-2025-04-11",       # 与 ttl:"1h" 同生共死
    "oauth-2025-04-20",                    # OAuth 鉴权层；messages 头拼装时过滤掉（§6/§7）
]

FAST_MODE_BETA = "fast-mode-2026-02-01"
CONTEXT_1M_BETA = "context-1m-2025-08-07"
MID_CONVERSATION_SYSTEM_BETA = "mid-conversation-system-2026-04-07"
CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"
OAUTH_BETA = "oauth-2025-04-20"

# server.py 会把下游 HTTP 头里的 beta / 原始模型名折进 body 的私有字段，
# 这样调度 / failover / Channel 抽象不用整体改签名；transform_request 不会透传这些字段。
PARROT_DOWNSTREAM_BETAS_KEY = "_parrot_downstream_betas"
PARROT_ORIGINAL_MODEL_KEY = "_parrot_original_model"
PARROT_WANTS_CONTEXT_1M_KEY = "_parrot_wants_context_1m"
PARROT_WANTS_FAST_MODE_KEY = "_parrot_wants_fast_mode"

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


# ─── load_config 适配层 ──────────────────────────────────────────
# 下方移植的函数（build_system_blocks / sign_body）原版读的是
# cc-proxy 的 cfg["cch_mode"] / cfg["cch_static_value"]；
# 这里提供适配的 load_config() 返回带旧 key 的 dict，
# 保证下方代码体与 cc-proxy 逐字一致、行为不变。

def load_config():
    cfg = _ap_config.get()
    return {
        "cch_mode": cfg.get("cchMode", "disabled"),
        "cch_static_value": cfg.get("cchStaticValue", "00000"),
    }


# ─── Fingerprint ───（与 cc-proxy 一字不改）

def compute_fingerprint(messages):
    # v2.1.156：输入源为「第一个 user message 的最后一个 text content block」
    # （即实际用户 prompt，跳过前面的 system-reminder block），索引 [4,7,18]。
    # salt/sha256 不变。6 组 canonical body 离线复核 6/6 命中。
    prompt_text = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                prompt_text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        prompt_text = block.get("text", "")   # 取最后一个 text block（不 break）
            break
    indices = [4, 7, 18]
    chars = "".join(prompt_text[i] if i < len(prompt_text) else "0" for i in indices)
    return hashlib.sha256(f"{FINGERPRINT_SALT}{chars}{CC_VERSION}".encode()).hexdigest()[:3]


# ─── System prompt ───（与 cc-proxy 一字不改）

def build_system_blocks(messages, *, inject_cache=True):
    fp = compute_fingerprint(messages)
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
        attribution = "x-anthropic-billing-header: " + "; ".join(parts) + ";"
        blocks.append({"type": "text", "text": attribution})
    cc_block = {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
    if inject_cache:
        cc_block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
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

# ─── 缓存断点 ───（与 cc-proxy 一字不改）

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


# ─── Metadata ───（仅签名参数化 email；函数体与 cc-proxy 一致）

def build_metadata(email="", session_id=None):
    account_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, email)) if email else ""
    sid = session_id or str(uuid.uuid4())
    # §8：真实 body metadata.user_id 内含 session_id，且与 header
    # X-Claude-Code-Session-Id 同值（complete-audit §5.2）。
    return {"user_id": json.dumps(
        {"device_id": DEVICE_ID, "account_uuid": account_uuid, "session_id": sid},
        separators=(",", ":"))}


# ─── 工具名重写 ───（与 cc-proxy 一字不改）

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


# ─── 请求转换 ───（仅签名参数化 email；函数体与 cc-proxy 一致）

def transform_request(body, email="", session_id=None):
    explicit_cache_control = cache_hints.has_anthropic_cache_control(body)
    messages = body.get("messages", [])
    user_system = body.get("system")
    messages = inject_user_system_to_messages(messages, user_system)
    messages = _normalize_messages_for_api(messages)
    messages = _strip_assistant_thinking_blocks(messages)
    if not explicit_cache_control:
        messages = _strip_message_cache_control(messages)
    system_blocks = build_system_blocks(messages, inject_cache=False)
    model = body.get("model", "claude-sonnet-4-20250514")

    # 动态工具名映射（tools > 5 时触发）
    dynamic_tool_map = None
    if body.get("tools"):
        raw_tools = body["tools"]
        tool_names = [t.get("name") for t in raw_tools if isinstance(t, dict) and t.get("name")]
        dynamic_tool_map = _build_dynamic_tool_map(tool_names)
        if dynamic_tool_map:
            print(f"  [tool] dynamic mapping {len(dynamic_tool_map)} tools")

    # §15.1：严格按 CC v2.1.156 wire order 构造 payload（sign_body 按插入序序列化，
    # 构造顺序 = wire order）：model, messages, system, tools, metadata,
    # max_tokens, speed, thinking, context_management, output_config, stream。
    # §15.2 B2：cc_mimicry 链路不注入 temperature/top_p/top_k（CC body 无此字段）。
    # §15.2 B3：max_tokens 缺省 64000（CC 默认）。§8 B5：metadata.session_id 与
    # header X-Claude-Code-Session-Id 同源。
    payload = {
        "model": model,
        "messages": messages,
        "system": system_blocks,
    }

    if body.get("tools"):
        tools = _strip_tool_cache_control(
            [dict(t) if isinstance(t, dict) else t for t in body["tools"]],
            preserve_cache_control=explicit_cache_control,
        )
        for t in tools:
            if isinstance(t, dict) and "name" in t:
                t["name"] = _sanitize_tool_name(t["name"], dynamic_tool_map)
        payload["tools"] = tools

    top_cache = cache_hints.top_level_cache_control(body)
    if top_cache:
        payload["cache_control"] = top_cache
    cache_hints.apply_anthropic_block_cache_breakpoints(payload)

    # tool_choice：CC 不"主动加"，但客户端显式传入时必须透传（含工具名混淆），
    # 否则会吞掉下游强制/禁用工具的意图。抓包未含此字段是会话未用到，非协议禁止。
    if "tool_choice" in body:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and "name" in tc:
            tc = dict(tc)
            tc["name"] = _sanitize_tool_name(tc["name"], dynamic_tool_map)
        payload["tool_choice"] = tc

    payload["metadata"] = build_metadata(email, session_id=session_id)
    payload["max_tokens"] = body.get("max_tokens", 64000)

    if request_wants_fast_mode(body):
        payload["speed"] = "fast"

    if "thinking" in body:
        payload["thinking"] = body["thinking"]

    if "context_management" in body:
        payload["context_management"] = body["context_management"]
    elif "thinking" in body:
        t = body["thinking"]
        _thinking_enabled = isinstance(t, dict) and t.get("type") in ("enabled", "adaptive")
        if _thinking_enabled:
            payload["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}

    if "output_config" in body:
        payload["output_config"] = body["output_config"]

    payload["stream"] = body.get("stream", False)

    # apply_opus_adaptive_thinking 会改 thinking 并可能补 output_config，
    # 必须在 output_config 写入之后调用（§15.2）。
    apply_opus_adaptive_thinking(payload, model)

    return payload, dynamic_tool_map


# ─── CCH 签名 ───（与 cc-proxy 一字不改）

CCH_SEED = 0x4D659218E32A3268
CCH_PLACEHOLDER = b"cch=00000"


def sign_body(payload_dict):
    body_bytes = json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    cfg = load_config()
    if _normalize_cch_mode(cfg.get("cch_mode", "dynamic")) != "dynamic":
        return body_bytes
    if CCH_PLACEHOLDER not in body_bytes:
        return body_bytes
    h = xxhash.xxh64(body_bytes, seed=CCH_SEED).intdigest()
    cch = f"{h & 0xFFFFF:05x}"
    return body_bytes.replace(CCH_PLACEHOLDER, f"cch={cch}".encode("ascii"), 1)


# ─── 上游 headers（OAuth 版本）───（§7.1 据 v2.1.156 源码真相重写）

# Stainless SDK 层固定值（@anthropic-ai/sdk 0.94.0 getPlatformProperties 输出，
# 抓包 67/67 印证）。OS/Arch/Runtime-Version 取 Parrot 实际运行环境（Linux/x64/node）。
_STAINLESS_HEADERS = {
    "X-Stainless-Lang": "js",
    "X-Stainless-Package-Version": "0.94.0",
    "X-Stainless-OS": "Linux",
    "X-Stainless-Arch": "x64",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v24.3.0",
    "X-Stainless-Retry-Count": "0",
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
    """Parrot 默认策略：Opus 4.x 默认 1M；Sonnet 4.x 只在显式 1M 时开启。"""
    return _is_opus_4_plus_model(model)


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
    """CC v2.1.156 mid_conversation_system 模型白名单口径。"""
    m = str(model or "").lower()
    if m.startswith("claude-3-"):
        return True
    return m.startswith((
        "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-5",
        "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
        "claude-sonnet-4-0", "claude-sonnet-4-5", "claude-sonnet-4-6",
        "claude-haiku-4-5",
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


def _payload_has_context_management(payload) -> bool:
    return isinstance(payload, dict) and "context_management" in payload and payload.get("context_management") is not None


def _messages_betas_for_request(model=None, betas=None, *, payload=None,
                                downstream_betas=None, original_model=None,
                                wants_context_1m=None, wants_fast_mode=None,
                                allow_any_model_context_1m=False):
    """返回 /v1/messages anthropic-beta 列表。

    规则不再是固定全量 join，而是按最终 payload / 模型 / 下游显式能力请求生成：
      - oauth-2025-04-20 属 token 端点，messages 永远不带；
      - context-1m：Opus 4.x 默认开启；Sonnet 4.5/4.6 仅在下游显式 1M 信号时开启；
        需要强制关闭时可传 wants_context_1m=False；
      - fast-mode：仅在下游显式 speed=fast / fast-mode beta 时开启；
      - mid-conversation-system 按 CC 模型白名单带；
      - context-management 仅最终 payload 含 context_management 时带；
      - extended-cache-ttl 仅最终 payload 含 ttl:"1h" 时带。
    """
    beta_list = BETAS if betas is None else betas
    if wants_fast_mode is None:
        wants_fast_mode = request_wants_fast_mode(
            payload if isinstance(payload, dict) else None,
            downstream_betas=downstream_betas,
        )
    if wants_context_1m is None:
        wants_context_1m = request_wants_context_1m(
            payload if isinstance(payload, dict) else None,
            downstream_betas=downstream_betas,
            original_model=original_model,
            resolved_model=model,
        ) or should_default_context_1m(model)
    allow_context_1m = bool(wants_context_1m) and (
        bool(allow_any_model_context_1m) or model_supports_context_1m(model)
    )
    allow_fast_mode = bool(wants_fast_mode)
    allow_mid_conversation = model_supports_mid_conversation_system(model)
    allow_context_management = True if payload is None else _payload_has_context_management(payload)
    allow_extended_cache_ttl = True if payload is None else _payload_has_ttl_1h(payload)

    out = []
    for b in beta_list:
        if b == OAUTH_BETA:
            continue
        if b == FAST_MODE_BETA and not allow_fast_mode:
            continue
        if b == CONTEXT_1M_BETA and not allow_context_1m:
            continue
        if b == MID_CONVERSATION_SYSTEM_BETA and not allow_mid_conversation:
            continue
        if b == CONTEXT_MANAGEMENT_BETA and not allow_context_management:
            continue
        if b == EXTENDED_CACHE_TTL_BETA and not allow_extended_cache_ttl:
            continue
        out.append(b)
    return out


def build_upstream_headers(access_token, session_id=None, betas=None, *, auth_scheme="bearer",
                           model=None, payload=None, downstream_betas=None,
                           original_model=None, wants_context_1m=None,
                           wants_fast_mode=None, allow_any_model_context_1m=False):
    """构造 messages 出站 header，对齐 CC v2.1.156 抓包恒定头集合（§7.1）。

    - session_id: 与 body.metadata.user_id.session_id 同值（§7.4/§8），调用方传入。
    - betas: 允许调用方传入过滤后的 beta 列表（如 api_channel omit_thinking）；
      缺省用模块 BETAS。无论如何都会剔除 oauth-2025-04-20（那是 token 端点的 beta）。
    - payload/model/downstream_betas/original_model/wants_context_1m/wants_fast_mode:
      用于按最终 body + 模型 + 下游显式能力信号生成 messages beta，避免无条件硬塞能力位。
    - allow_any_model_context_1m: API 兼容渠道可透传/强制未知模型的 1M 标志；
      OAuth 官方渠道仍保留模型能力白名单。
    - auth_scheme: "bearer"(OAuth) 用 Authorization: Bearer；"api_key" 用 x-api-key。
    注意：x-client-request-id 已删（源码实证 CC 从不在请求发此头，抓包 67/67 无）。
    Accept-Encoding 只写 gzip,deflate —— 本机 venv 未装 brotli/zstandard，
    若声明 br/zstd 上游回包会解不开（§7.3）。
    """
    sid = session_id or str(uuid.uuid4())
    beta_str = ",".join(_messages_betas_for_request(
        model=model, betas=betas, payload=payload,
        downstream_betas=downstream_betas, original_model=original_model,
        wants_context_1m=wants_context_1m,
        wants_fast_mode=wants_fast_mode,
        allow_any_model_context_1m=allow_any_model_context_1m,
    ))
    headers = {
        # ── CC 应用层 ──
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": beta_str,
        "x-app": "cli",
        "User-Agent": CLI_USER_AGENT,
        "X-Claude-Code-Session-Id": sid,
    }
    if auth_scheme == "api_key":
        headers["x-api-key"] = access_token
    else:
        headers["Authorization"] = f"Bearer {access_token}"
    # ── Stainless SDK 层 ──
    headers.update(_STAINLESS_HEADERS)
    headers["anthropic-dangerous-direct-browser-access"] = "true"
    # ── 传输层（venv 无 brotli/zstandard，只声明 gzip/deflate）──
    headers["Accept-Encoding"] = "gzip, deflate"
    return headers
