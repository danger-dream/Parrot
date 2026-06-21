"""OAuth→Codex 上游请求体强制改造。

调用位置：
  `OpenAIOAuthChannel.build_upstream_request` 里，输入已经是 Responses API
  shape（责任方：同协议 passthrough 过 provider adapter target allowlist；
  或跨协议先过 chat_to_responses.translate_request）。本模块负责把它打成
  ChatGPT internal codex 端点 (`/backend-api/codex/responses`) 能接受的样子：

  - `store=false` 强制（OAuth 上游对 store=true 报 400）
  - `stream=true` 强制（OAuth 上游仅支持流式 SSE）
  - 删除 Responses API 里上游不支持的字段：max_output_tokens /
    max_completion_tokens / temperature / top_p / frequency_penalty /
    presence_penalty / prompt_cache_retention / user / metadata /
    safety_identifier / stream_options
  - 模型名：**直接透传 resolved_model**（不做任何别名映射）。
    账号层 `supports_model` 已经用账号 `models` + `defaultModels` 做了白名单
    校验，进到这里的都是合法模型名；上游无论叫 gpt-5.1 / gpt-5.5 / 下个月出的
    gpt-5.6，都原样发出去。新家族只需在 TG 面板或
    `config.oauth.providers.openai.defaultModels` 加一行，代码零改动。
  - `instructions` 空 → 注入默认 "You are a helpful coding assistant."
  - legacy `functions` / `function_call` → `tools` / `tool_choice`
  - `input` 是字符串 → 包成 [{type:"message", role:"user", content:<str>}]
  - `input[]` 里的 role=system 消息提取到 `instructions`（上游 input 不接受
    system role）

工具调用续链现在按 sub2api 的 OAuth transform 做最小兼容：过滤 store=false
上游无法解析的 reasoning/item_reference/id，规范化 call_id 与 tool_choice，避免
ChatGPT internal Codex endpoint 把本地响应 ID 当成持久化引用去查。

历史：早期版本（v0.4.x ~ v0.5.x）从 sub2api 移植了一张 _CODEX_MODEL_MAP 翻译表，
把各种别名（gpt-5 / gpt-5-codex / gpt-5.3-xhigh 等）映射到上游规范名，
并带了"未识别名字 → 降级成 gpt-5.1"的兜底。v0.6.x 起移除：
  1) Parrot 的 channel 层已经用账号 `models` + `defaultModels` 做了白名单，
     进到 transform 的模型名本就是合法的；再翻译纯属画蛇添足。
  2) 兜底降级坑惨——新模型（如 gpt-5.5）未登记就被降成 gpt-5.1，
     导致所有账号都被上游拒绝（gpt-5.1 早就下架）。
移除 commit：见 git log；想回溯旧映射表完整内容也可以去 git history 里查。
"""

from __future__ import annotations

import json
from typing import Any


# ─── 默认 instructions（仅一行，与 sub2api applyInstructions 对齐）──

_DEFAULT_INSTRUCTIONS = "You are a helpful coding assistant."

# 上游 codex endpoint 不认识、必须剥掉的 Responses API 字段。
_STRIP_FIELDS_FOR_CODEX = (
    "max_output_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    # 新版 Responses API 的缓存 TTL；Codex endpoint 拒绝 "Unsupported parameter"
    "prompt_cache_retention",
    # ChatGPT internal Codex endpoint 不接受这些 Responses API 通用字段
    "user",
    "metadata",
    "safety_identifier",
    "stream_options",
    # `background=false` is semantically equivalent to the Codex synchronous
    # stream path; `background=true` is rejected before this transform.
    "background",
    # OAuth Codex 强制 store=false，不能把 Responses 持久化引用直传给上游。
    "previous_response_id",
)


def _is_empty_str(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return True
    return value.strip() == ""


def _content_to_plain_text(content: Any) -> str:
    """把 Responses API 消息 content（可能是 str / [parts]）拍扁成纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for p in content:
        if isinstance(p, dict):
            # Responses content parts 常见：input_text / output_text / text
            for key in ("text", "input_text", "output_text"):
                v = p.get(key)
                if isinstance(v, str) and v:
                    parts.append(v)
                    break
        elif isinstance(p, str):
            parts.append(p)
    return "\n".join(parts)


def _extract_system_messages(body: dict) -> str | None:
    """从 input[] 里把 role=system 的消息提取并拼到 instructions。

    Codex endpoint 的 input 不接受 system role；这里把它们合并成一条文本
    追加到 instructions（若为空则作为 instructions 正文）。返回合并后的
    system 文本；若没有 system 消息则返回 None。
    """
    items = body.get("input")
    if not isinstance(items, list):
        return None
    keep: list[Any] = []
    sys_texts: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            keep.append(it)
            continue
        typ = it.get("type")
        role = it.get("role")
        if typ == "message" and role == "system":
            txt = _content_to_plain_text(it.get("content", ""))
            if txt:
                sys_texts.append(txt)
            continue
        keep.append(it)
    if not sys_texts:
        return None
    body["input"] = keep
    return "\n\n".join(sys_texts)


def _convert_legacy_tools(body: dict) -> bool:
    """chat completion legacy `functions` / `function_call` → `tools` / `tool_choice`。

    返回是否动过 body。
    """
    modified = False
    if "functions" in body and isinstance(body["functions"], list):
        body["tools"] = [
            {"type": "function", "function": f} for f in body["functions"]
        ]
        del body["functions"]
        modified = True
    if "function_call" in body:
        fc = body["function_call"]
        if isinstance(fc, str):
            body["tool_choice"] = fc  # "auto" / "none"
            modified = True
        elif isinstance(fc, dict):
            name = fc.get("name")
            if isinstance(name, str) and name.strip():
                body["tool_choice"] = {
                    "type": "function",
                    "function": {"name": name.strip()},
                }
                modified = True
        del body["function_call"]
    return modified


def _coerce_input_to_list(body: dict) -> bool:
    """input 是字符串 → 包成 [{type:"message", role:"user", content:<str>}]。

    Codex endpoint 的 input 必须是数组。返回是否动过 body。
    """
    v = body.get("input")
    if isinstance(v, str):
        if v.strip():
            body["input"] = [{
                "type": "message",
                "role": "user",
                "content": v,
            }]
        else:
            body["input"] = []
        return True
    return False


def _normalize_codex_tools(body: dict) -> bool:
    """把 chat-style `{type:"function", function:{name,...}}` 拍平为 Responses-style
    `{type:"function", name, parameters, ...}`（顶层字段）。

    移植自 sub2api openai_codex_transform.go:normalizeCodexTools。原因：codex
    endpoint 走 Responses API 协议，工具定义必须是顶层 name/parameters；若收到
    ChatCompletions 历史格式会 400。本函数在 transform 末尾统一做一次，不管
    下游走哪条 ingress 都兜底。

    返回是否动过 body。副作用：丢弃无效的 function tool（hasFunction 为假且
    顶层无 name 的条目），这与 sub2api 行为一致。
    """
    raw_tools = body.get("tools")
    if not isinstance(raw_tools, list):
        return False

    modified = False
    valid: list = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            # 非 dict 的工具保留（不是我们要处理的）
            valid.append(tool)
            continue
        ttype = str(tool.get("type") or "").strip()
        if ttype != "function":
            valid.append(tool)
            continue
        # 已是 Responses-style（顶层有 name）→ 原样保留
        top_name = tool.get("name")
        if isinstance(top_name, str) and top_name.strip():
            valid.append(tool)
            continue
        # ChatCompletions-style：{type:"function", function:{name, parameters, ...}}
        function_obj = tool.get("function")
        if not isinstance(function_obj, dict):
            # 既无顶层 name 又无 function 对象 → 丢弃（与 sub2api 一致）
            modified = True
            continue
        # 把 function.* 拍平到顶层（不覆盖已有的顶层同名字段）
        for key in ("name", "description", "parameters", "strict"):
            if key in tool:
                continue
            if key in function_obj:
                tool[key] = function_obj[key]
                modified = True
        valid.append(tool)

    if modified:
        body["tools"] = valid
    return modified


def _first_non_empty_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _codex_tools_contain_type(raw_tools: Any, tool_type: str) -> bool:
    if not isinstance(raw_tools, list) or not tool_type:
        return False
    for raw in raw_tools:
        if isinstance(raw, dict) and str(raw.get("type") or "").strip() == tool_type:
            return True
    return False


def _codex_tools_contain_function_name(raw_tools: Any, name: str) -> bool:
    if not isinstance(raw_tools, list) or not name:
        return False
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("type") or "").strip() != "function":
            continue
        tool_name = _first_non_empty_string(raw.get("name"))
        fn = raw.get("function")
        if not tool_name and isinstance(fn, dict):
            tool_name = _first_non_empty_string(fn.get("name"))
        if tool_name == name:
            return True
    return False


def _normalize_codex_tool_choice(body: dict) -> bool:
    """把 tool_choice 规范化成 Codex endpoint 接受的结构。

    function 类型统一为 {type:function, name:<name>}；若指向不存在的工具，
    降级为 "auto"，避免上游 400。
    """
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return False
    choice_type = _first_non_empty_string(choice.get("type"))
    if not choice_type:
        return False
    if choice_type == "function":
        fn = choice.get("function")
        name = _first_non_empty_string(choice.get("name"))
        if not name and isinstance(fn, dict):
            name = _first_non_empty_string(fn.get("name"))
        if not name or not _codex_tools_contain_function_name(body.get("tools"), name):
            body["tool_choice"] = "auto"
            return True
        modified = False
        if choice.get("name") != name:
            choice["name"] = name
            modified = True
        if "function" in choice:
            choice.pop("function", None)
            modified = True
        return modified
    if _codex_tools_contain_type(body.get("tools"), choice_type):
        return False
    body["tool_choice"] = "auto"
    return True


_CODEX_TOOL_CALL_TYPES = {
    "function_call",
    "tool_call",
    "local_shell_call",
    "tool_search_call",
    "custom_tool_call",
    "mcp_tool_call",
    "function_call_output",
    "mcp_tool_call_output",
    "custom_tool_call_output",
    "tool_search_output",
}

_CODEX_INPUT_ITEM_TYPES_REQUIRING_NAME = {
    "function_call",
    "custom_tool_call",
    "mcp_tool_call",
}


def _is_codex_tool_call_item_type(item_type: str) -> bool:
    return item_type in _CODEX_TOOL_CALL_TYPES


def _codex_input_item_requires_name(item_type: str) -> bool:
    return item_type in _CODEX_INPUT_ITEM_TYPES_REQUIRING_NAME


def _has_tools_signal(body: dict) -> bool:
    tools = body.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def _strip_orphaned_tool_fields(body: dict) -> None:
    """当 tools 为空数组或缺失时，清理 tool_choice 和 parallel_tool_calls。

    某些上游（如 xAI、DeepSeek）在收到 tools=[] + tool_choice="auto" 时会 400。
    Codex CLI 在无工具时不发 tool_choice，这里对齐该行为。
    """
    tools = body.get("tools")
    if isinstance(tools, list) and len(tools) > 0:
        return
    # tools 缺失或为空数组 → 清理孤立的 tool_choice / parallel_tool_calls
    body.pop("tool_choice", None)
    body.pop("parallel_tool_calls", None)
    if isinstance(tools, list) and len(tools) == 0:
        body.pop("tools", None)


def _has_tool_choice_signal(body: dict) -> bool:
    choice = body.get("tool_choice")
    if choice is None:
        return False
    if isinstance(choice, str):
        return choice.strip() not in ("", "auto", "none")
    return isinstance(choice, dict) and bool(choice)


def _needs_tool_continuation(body: dict) -> bool:
    if _first_non_empty_string(body.get("previous_response_id")):
        return True
    if _has_tools_signal(body) or _has_tool_choice_signal(body):
        return True
    inp = body.get("input")
    if not isinstance(inp, list):
        return False
    for item in inp:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "")
        if _is_codex_tool_call_item_type(typ) or typ == "item_reference":
            return True
    return False


def _fix_call_id_prefix(call_id: str) -> str:
    if not call_id or call_id.startswith("fc"):
        return call_id
    if call_id.startswith("call_"):
        return "fc" + call_id[len("call_"):]
    return "fc_" + call_id


def _normalize_codex_tool_role_messages(input_items: list[Any]) -> tuple[list[Any], bool]:
    """把 role=tool 的 message 规范成 function_call_output。"""
    modified = False
    out: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        role = str(item.get("role") or "").strip()
        if role != "tool":
            out.append(item)
            continue
        call_id = _first_non_empty_string(item.get("call_id"), item.get("tool_call_id"), item.get("id"))
        if not call_id:
            fallback = dict(item)
            fallback["role"] = "user"
            fallback.pop("tool_call_id", None)
            fallback.pop("call_id", None)
            out.append(fallback)
            modified = True
            continue
        normalized = {
            "type": "function_call_output",
            "call_id": call_id,
            "output": _content_to_plain_text(item.get("content", "")),
        }
        name = _first_non_empty_string(item.get("name"), item.get("tool_name"))
        if name:
            normalized["name"] = name
        out.append(normalized)
        modified = True
    return out, modified


def _stringify_codex_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def _normalize_codex_message_content_text(input_items: list[Any]) -> tuple[list[Any], bool]:
    """Codex endpoint 要求 content parts 的 text 是字符串。"""
    modified = False
    out: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            out.append(item)
            continue
        new_item = item
        new_parts = list(item["content"])
        item_modified = False
        for idx, part in enumerate(new_parts):
            if not isinstance(part, dict) or "text" not in part or isinstance(part.get("text"), str):
                continue
            new_part = dict(part)
            new_part["text"] = _stringify_codex_content_text(part.get("text"))
            new_parts[idx] = new_part
            item_modified = True
        if item_modified:
            new_item = dict(item)
            new_item["content"] = new_parts
            modified = True
        out.append(new_item)
    return out, modified


def _filter_codex_input(input_items: list[Any], *, preserve_references: bool) -> list[Any]:
    filtered: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict):
            filtered.append(item)
            continue
        typ = str(item.get("type") or "")

        # Fix A（透传快路径）：store=false 下，裸 reasoning / rs_* 持久化引用上游
        # 无法读取，仍丢弃；但带合法 encrypted_content 的 reasoning 块要保留透传——
        # 上游 chatgpt.com 通过 encrypted_content 自解密续接推理链（agent 多步工具
        # 链场景必需）。判据：encrypted_content 为非空字符串。对齐 codex 原版
        # history.rs 只保留带 encrypted_content 的 reasoning 块的过滤逻辑。
        if typ == "reasoning":
            enc = item.get("encrypted_content")
            if not (isinstance(enc, str) and enc.strip()):
                continue
            rs = dict(item)
            # store=false 上游不接受持久化 reasoning id / rs_* 引用，剥掉避免上游
            # 按 ID 查持久化存储而 404；encrypted_content 自带续链信息，无需 id。
            rs.pop("id", None)
            filtered.append(rs)
            continue

        if typ == "item_reference":
            if not preserve_references:
                continue
            ref = dict(item)
            ref_id = _first_non_empty_string(ref.get("id"))
            if ref_id.startswith("call_"):
                ref["id"] = _fix_call_id_prefix(ref_id)
            filtered.append(ref)
            continue

        new_item = dict(item)

        if _is_codex_tool_call_item_type(typ):
            call_id = _first_non_empty_string(new_item.get("call_id"))
            if not call_id:
                call_id = _first_non_empty_string(new_item.get("id"))
                if call_id:
                    new_item["call_id"] = call_id
            if call_id:
                fixed = _fix_call_id_prefix(call_id)
                if fixed != call_id:
                    new_item["call_id"] = fixed
        else:
            new_item.pop("call_id", None)

        if _codex_input_item_requires_name(typ) and not _first_non_empty_string(new_item.get("name")):
            name = _first_non_empty_string(new_item.get("tool_name"))
            fn = new_item.get("function")
            if not name and isinstance(fn, dict):
                name = _first_non_empty_string(fn.get("name"))
            new_item["name"] = name or "tool"

        if not preserve_references:
            new_item.pop("id", None)

        filtered.append(new_item)
    return filtered


def _normalize_codex_input(body: dict) -> bool:
    inp = body.get("input")
    if not isinstance(inp, list):
        return False
    modified = False
    normalized, changed = _normalize_codex_tool_role_messages(inp)
    modified = modified or changed
    normalized, changed = _normalize_codex_message_content_text(normalized)
    modified = modified or changed
    filtered = _filter_codex_input(
        normalized,
        preserve_references=_needs_tool_continuation(body),
    )
    if filtered != inp:
        modified = True
    body["input"] = filtered
    return modified


def apply_codex_oauth_transform(
    body: dict,
    *,
    resolved_model: str | None = None,
    session_key: str | None = None,
) -> dict:
    """就地改造 body，返回同一对象。

    参数:
      body: Responses API shape（字符串 input 也容忍；见上）
      resolved_model: 调度层已对齐后的模型名（账号白名单已校验过的合法名字）；
        transform **原样透传**给上游，不做任何别名映射。
      session_key: 历史兼容参数。v0.21.3 起 encrypted_content 只做透明透传，
        不再由 Parrot 维护本地 replay/backfill，因此这里不再使用。
    """
    # 1) 模型名：**直接透传**。resolved_model 已由账号 supports_model 把关；
    #    不做任何别名/兜底映射，避免新模型未登记被错误降级。
    if resolved_model:
        body["model"] = resolved_model
    elif _is_empty_str(body.get("model")):
        # 极端兜底：resolved_model 缺失且 body 里也没 model。正常调用路径
        # （Channel.build_upstream_request）不会走到这里；测试或误用时
        # 给个最保守默认避免上游报缺参，上游会按自己白名单决定是否接受。
        body["model"] = "gpt-5"

    # 2) store / stream 强制
    body["store"] = False
    body["stream"] = True

    # 2.5) 主动注入 include=reasoning.encrypted_content（对齐 codex 原版 client.rs）。
    # store=false 下上游**仅在显式 include 时**才返回 reasoning 的 encrypted_content；
    # v3 自缓存的捕获完全依赖上游返回它，因此绝不能依赖下游带这个开关（不信任下游）。
    # 无条件确保它在 include 列表里。
    _inc = body.get("include")
    if not isinstance(_inc, list):
        _inc = []
    if "reasoning.encrypted_content" not in _inc:
        _inc = list(_inc) + ["reasoning.encrypted_content"]
    body["include"] = _inc

    # 3) 剥不支持字段
    for k in _STRIP_FIELDS_FOR_CODEX:
        body.pop(k, None)

    # 4) legacy functions / function_call → tools / tool_choice
    _convert_legacy_tools(body)

    # 4.5) tools 结构规范化：chat-style {type:function, function:{name,...}}
    #      拍平为 Responses-style {type:function, name, ...}。ingress 无论
    #      是 chat（由 chat_to_responses 翻译后一般已扁平，但防御性再跑一遍）
    #      还是 responses（下游可能直接用 ChatCompletions 格式）都要兜底。
    _normalize_codex_tools(body)
    _normalize_codex_tool_choice(body)
    _strip_orphaned_tool_fields(body)

    # 5) input 字符串 → 数组；再把 input 里的 system 消息提到 instructions
    _coerce_input_to_list(body)
    sys_text = _extract_system_messages(body)
    if sys_text:
        orig = body.get("instructions")
        if _is_empty_str(orig):
            body["instructions"] = sys_text
        else:
            body["instructions"] = f"{orig}\n\n{sys_text}"

    # 5.5) store=false 兼容：过滤 reasoning/item_reference/id，规范化 call_id。
    _normalize_codex_input(body)

    # 6) instructions 兜底（sub2api 行为：空 → 一行 fallback）
    if _is_empty_str(body.get("instructions")):
        body["instructions"] = _DEFAULT_INSTRUCTIONS

    return body
