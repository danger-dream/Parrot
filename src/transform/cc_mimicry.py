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
import uuid

import xxhash

from .. import config as _ap_config


# ─── BASE_DIR / device_id 持久化 ──────────────────────────────────
# anthropic-proxy 的包目录层级：<root>/src/transform/cc_mimicry.py
# 所以 BASE_DIR = cc_mimicry.py 所在目录向上两级
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CC_VERSION = "2.1.92"
FINGERPRINT_SALT = "59cf53e54c78"
CC_ENTRYPOINT = "cli"
USER_TYPE = "external"

BETAS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "prompt-caching-scope-2026-01-05",
    "effort-2025-11-24",
    "redact-thinking-2026-02-12",
    "context-management-2025-06-27",
    "extended-cache-ttl-2025-04-11",
]

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
    ids_file = os.path.join(BASE_DIR, ".anthropic_proxy_ids.json")
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
    first_text = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                first_text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        first_text = block.get("text", "")
                        break
            break
    indices = [4, 7, 20]
    chars = "".join(first_text[i] if i < len(first_text) else "0" for i in indices)
    return hashlib.sha256(f"{FINGERPRINT_SALT}{chars}{CC_VERSION}".encode()).hexdigest()[:3]


# ─── System prompt ───（与 cc-proxy 一字不改）

def build_system_blocks(messages):
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
    blocks.append(
        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    )
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


def _strip_tool_cache_control(tools):
    """移除客户端在 tools 上设置的 cache_control，由代理统一管理。"""
    result = []
    for tool in tools:
        if isinstance(tool, dict) and "cache_control" in tool:
            tool = {k: v for k, v in tool.items() if k != "cache_control"}
        result.append(tool)
    return result


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


# ─── Metadata ───（仅签名参数化 email；函数体与 cc-proxy 一致）

def build_metadata(email=""):
    account_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, email)) if email else ""
    return {"user_id": json.dumps({"device_id": DEVICE_ID, "account_uuid": account_uuid}, separators=(",", ":"))}


# ─── 工具名重写 ───（与 cc-proxy 一字不改）

TOOL_NAME_REWRITES = {"sessions_": "cc_sess_", "session_": "cc_ses_"}  # 静态前缀映射（保留兼容）

# 生成混淆用的可读假名前缀
_FAKE_PREFIXES = [
    "analyze_", "compute_", "fetch_", "generate_", "lookup_", "modify_",
    "process_", "query_", "render_", "resolve_", "sync_", "update_",
    "validate_", "convert_", "extract_", "manage_", "monitor_", "parse_",
    "review_", "search_", "transform_", "handle_", "invoke_", "notify_",
]


def _build_dynamic_tool_map(tool_names, threshold=5):
    """当 tools 数量超过 threshold 时，生成原名→假名的动态映射。
    返回 dict 或 None（无需映射时）。
    """
    if len(tool_names) <= threshold:
        return None
    mapping = {}
    available = list(_FAKE_PREFIXES)
    rng = random.Random(hash(tuple(tool_names)))  # 同进程内同一组 tools 映射稳定，保证缓存命中
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


def _restore_tool_names_in_chunk(chunk_bytes, dynamic_map=None):
    # 动态映射还原（假名→原名），长的先替换避免子串冲突
    if dynamic_map:
        sorted_items = sorted(dynamic_map.items(), key=lambda x: len(x[1]), reverse=True)
        for original, fake in sorted_items:
            chunk_bytes = chunk_bytes.replace(fake.encode(), original.encode())
    # 静态映射还原
    for prefix, replacement in TOOL_NAME_REWRITES.items():
        chunk_bytes = chunk_bytes.replace(replacement.encode(), prefix.encode())
    return chunk_bytes


# ─── 请求转换 ───（仅签名参数化 email；函数体与 cc-proxy 一致）

def transform_request(body, email=""):
    messages = body.get("messages", [])
    user_system = body.get("system")
    messages = inject_user_system_to_messages(messages, user_system)
    messages = _strip_message_cache_control(messages)
    messages = add_cache_breakpoints(messages)
    system_blocks = build_system_blocks(messages)
    model = body.get("model", "claude-sonnet-4-20250514")

    payload = {
        "model": model,
        "messages": messages,
        "system": system_blocks,
        "max_tokens": body.get("max_tokens", 128000),
        "stream": body.get("stream", True),
        "metadata": build_metadata(email),
        "temperature": 1,
    }

    if "temperature" in body:
        payload["temperature"] = body["temperature"]

    if "thinking" in body:
        payload["thinking"] = body["thinking"]

    # 动态工具名映射（tools > 5 时触发）
    dynamic_tool_map = None
    if body.get("tools"):
        tool_names = [t.get("name", "") for t in body["tools"]]
        dynamic_tool_map = _build_dynamic_tool_map(tool_names)
        if dynamic_tool_map:
            print(f"  [tool] dynamic mapping {len(dynamic_tool_map)} tools")

    if body.get("tools"):
        tools = _strip_tool_cache_control([dict(t) for t in body["tools"]])
        for t in tools:
            t["name"] = _sanitize_tool_name(t["name"], dynamic_tool_map)
        tools[-1] = dict(tools[-1])
        tools[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        payload["tools"] = tools

    if "tool_choice" in body:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and "name" in tc:
            tc = dict(tc)
            tc["name"] = _sanitize_tool_name(tc["name"], dynamic_tool_map)
        payload["tool_choice"] = tc

    if "context_management" in body:
        payload["context_management"] = body["context_management"]
    elif "thinking" in body:
        payload["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}

    if "output_config" in body:
        payload["output_config"] = body["output_config"]

    return payload, dynamic_tool_map


# ─── CCH 签名 ───（与 cc-proxy 一字不改）

CCH_SEED = 0x6E52736AC806831E
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


# ─── 上游 headers（OAuth 版本）───（与 cc-proxy 一字不改）

def build_upstream_headers(access_token):
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ",".join(BETAS),
        "x-app": "cli",
        "User-Agent": CLI_USER_AGENT,
        "x-client-request-id": str(uuid.uuid4()),
    }
