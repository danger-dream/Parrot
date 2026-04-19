"""OAuth→Codex 上游请求体强制改造（移植自 sub2api 的 applyCodexOAuthTransform）。

调用位置：
  `OpenAIOAuthChannel.build_upstream_request` 里，输入已经是 Responses API
  shape（责任方：passthrough 过 common.filter_responses_passthrough；或跨协议
  先过 chat_to_responses.translate_request）。本模块负责把它打成 ChatGPT
  internal codex 端点 (`/backend-api/codex/responses`) 能接受的样子：

  - `store=false` 强制（OAuth 上游对 store=true 报 400）
  - `stream=true` 强制（OAuth 上游仅支持流式 SSE）
  - 删除 Responses API 里上游不支持的字段：max_output_tokens /
    max_completion_tokens / temperature / top_p / frequency_penalty /
    presence_penalty / prompt_cache_retention
  - 模型名规范化为 codex CLI 所认字面：gpt-5 → gpt-5.1 等（映射表下方）
  - `instructions` 空 → 注入默认 "You are a helpful coding assistant."
    （Codex CLI 官方也如此做；完整 117 行 DefaultInstructions 只用于 sub2api
    自测探针，不走代理请求）
  - legacy `functions` / `function_call` → `tools` / `tool_choice`
  - `input` 是字符串 → 包成 [{type:"message", role:"user", content:<str>}]
  - `input[]` 里的 role=system 消息提取到 `instructions`（上游 input 不接受
    system role）

工具调用续链（item_reference / call_* → fc*）这里**暂不搬**——它是 sub2api
为 function call 恢复 call_id 上下文做的兼容层，需要 state_store 保存。Commit 2
目标是跑通单轮请求；续链支持放后续。对应未续链场景，sub2api 也会正常删除
item_reference，等效于我们这里的实现。
"""

from __future__ import annotations

import re
from typing import Any


# ─── codex 模型映射表（完整移植自 sub2api openai_codex_transform.go）─

# 完整表。key 是下游可能发上来的任意 codex 家族别名，value 是上游认识的规范名。
_CODEX_MODEL_MAP: dict[str, str] = {
    # gpt-5.4 家族
    "gpt-5.4":                    "gpt-5.4",
    "gpt-5.4-mini":               "gpt-5.4-mini",
    "gpt-5.4-nano":               "gpt-5.4-nano",
    "gpt-5.4-none":               "gpt-5.4",
    "gpt-5.4-low":                "gpt-5.4",
    "gpt-5.4-medium":             "gpt-5.4",
    "gpt-5.4-high":               "gpt-5.4",
    "gpt-5.4-xhigh":              "gpt-5.4",
    "gpt-5.4-chat-latest":        "gpt-5.4",
    # gpt-5.3 家族（全部映到 codex）
    "gpt-5.3":                    "gpt-5.3-codex",
    "gpt-5.3-none":               "gpt-5.3-codex",
    "gpt-5.3-low":                "gpt-5.3-codex",
    "gpt-5.3-medium":             "gpt-5.3-codex",
    "gpt-5.3-high":               "gpt-5.3-codex",
    "gpt-5.3-xhigh":              "gpt-5.3-codex",
    "gpt-5.3-codex":              "gpt-5.3-codex",
    "gpt-5.3-codex-spark":        "gpt-5.3-codex",
    "gpt-5.3-codex-spark-low":    "gpt-5.3-codex",
    "gpt-5.3-codex-spark-medium": "gpt-5.3-codex",
    "gpt-5.3-codex-spark-high":   "gpt-5.3-codex",
    "gpt-5.3-codex-spark-xhigh":  "gpt-5.3-codex",
    "gpt-5.3-codex-low":          "gpt-5.3-codex",
    "gpt-5.3-codex-medium":       "gpt-5.3-codex",
    "gpt-5.3-codex-high":         "gpt-5.3-codex",
    "gpt-5.3-codex-xhigh":        "gpt-5.3-codex",
    # gpt-5.1 codex 家族
    "gpt-5.1-codex":              "gpt-5.1-codex",
    "gpt-5.1-codex-low":          "gpt-5.1-codex",
    "gpt-5.1-codex-medium":       "gpt-5.1-codex",
    "gpt-5.1-codex-high":         "gpt-5.1-codex",
    "gpt-5.1-codex-max":          "gpt-5.1-codex-max",
    "gpt-5.1-codex-max-low":      "gpt-5.1-codex-max",
    "gpt-5.1-codex-max-medium":   "gpt-5.1-codex-max",
    "gpt-5.1-codex-max-high":     "gpt-5.1-codex-max",
    "gpt-5.1-codex-max-xhigh":    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini":         "gpt-5.1-codex-mini",
    "gpt-5.1-codex-mini-medium":  "gpt-5.1-codex-mini",
    "gpt-5.1-codex-mini-high":    "gpt-5.1-codex-mini",
    # gpt-5.2 家族
    "gpt-5.2":                    "gpt-5.2",
    "gpt-5.2-none":               "gpt-5.2",
    "gpt-5.2-low":                "gpt-5.2",
    "gpt-5.2-medium":             "gpt-5.2",
    "gpt-5.2-high":               "gpt-5.2",
    "gpt-5.2-xhigh":              "gpt-5.2",
    "gpt-5.2-codex":              "gpt-5.2-codex",
    "gpt-5.2-codex-low":          "gpt-5.2-codex",
    "gpt-5.2-codex-medium":       "gpt-5.2-codex",
    "gpt-5.2-codex-high":         "gpt-5.2-codex",
    "gpt-5.2-codex-xhigh":        "gpt-5.2-codex",
    # gpt-5.1 家族
    "gpt-5.1":                    "gpt-5.1",
    "gpt-5.1-none":               "gpt-5.1",
    "gpt-5.1-low":                "gpt-5.1",
    "gpt-5.1-medium":             "gpt-5.1",
    "gpt-5.1-high":               "gpt-5.1",
    "gpt-5.1-chat-latest":        "gpt-5.1",
    # 旧别名
    "gpt-5-codex":                "gpt-5.1-codex",
    "codex-mini-latest":          "gpt-5.1-codex-mini",
    "gpt-5-codex-mini":           "gpt-5.1-codex-mini",
    "gpt-5-codex-mini-medium":    "gpt-5.1-codex-mini",
    "gpt-5-codex-mini-high":      "gpt-5.1-codex-mini",
    "gpt-5":                      "gpt-5.1",
    "gpt-5-mini":                 "gpt-5.1",
    "gpt-5-nano":                 "gpt-5.1",
}


def normalize_codex_model(model: str) -> str:
    """下游的任意 codex 家族名 → 上游认识的规范名。"""
    if not model:
        return "gpt-5.1"
    # 去掉 provider 前缀（如 "openai/gpt-5"）
    mid = model.split("/")[-1] if "/" in model else model

    mapped = _CODEX_MODEL_MAP.get(mid)
    if mapped:
        return mapped

    low = mid.lower()
    # 通配兜底，覆盖带变体后缀的未登记模型
    if "gpt-5.4-mini" in low or "gpt 5.4 mini" in low:
        return "gpt-5.4-mini"
    if "gpt-5.4-nano" in low or "gpt 5.4 nano" in low:
        return "gpt-5.4-nano"
    if "gpt-5.4" in low or "gpt 5.4" in low:
        return "gpt-5.4"
    if "gpt-5.2-codex" in low or "gpt 5.2 codex" in low:
        return "gpt-5.2-codex"
    if "gpt-5.2" in low or "gpt 5.2" in low:
        return "gpt-5.2"
    if "gpt-5.3-codex" in low or "gpt 5.3 codex" in low:
        return "gpt-5.3-codex"
    if "gpt-5.3" in low or "gpt 5.3" in low:
        return "gpt-5.3-codex"
    if "gpt-5.1-codex-max" in low or "gpt 5.1 codex max" in low:
        return "gpt-5.1-codex-max"
    if "gpt-5.1-codex-mini" in low or "gpt 5.1 codex mini" in low:
        return "gpt-5.1-codex-mini"
    if ("codex-mini-latest" in low or "gpt-5-codex-mini" in low
            or "gpt 5 codex mini" in low):
        return "codex-mini-latest"
    if "gpt-5.1-codex" in low or "gpt 5.1 codex" in low:
        return "gpt-5.1-codex"
    if "gpt-5.1" in low or "gpt 5.1" in low:
        return "gpt-5.1"
    if "codex" in low:
        return "gpt-5.1-codex"
    if "gpt-5" in low or "gpt 5" in low:
        return "gpt-5.1"
    return "gpt-5.1"


def codex_model_ids() -> list[str]:
    """导出所有 codex 家族**规范**模型名（_CODEX_MODEL_MAP 的 value 集合）。

    只有 ~12 条：gpt-5.1 / gpt-5.1-codex / gpt-5.1-codex-max / ...。
    上游 codex endpoint 最终只认这些规范名，transform 会把别名 normalize 过去。
    """
    return sorted({v for v in _CODEX_MODEL_MAP.values()})


def codex_known_aliases() -> list[str]:
    """导出所有已登记的 codex 模型别名集合（_CODEX_MODEL_MAP 的 key 集合）。

    70+ 条：gpt-5 / gpt-5-codex / gpt-5-codex-mini / gpt-5.3-xhigh / ...。
    给 Channel.list_client_models / supports_model 用，让客户端用任何已知别名
    请求都能命中；真正发给上游时由 transform 的 normalize_codex_model 映射
    到规范名。
    """
    return sorted(_CODEX_MODEL_MAP.keys())


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


def apply_codex_oauth_transform(
    body: dict,
    *,
    resolved_model: str | None = None,
) -> dict:
    """就地改造 body，返回同一对象。

    参数:
      body: Responses API shape（字符串 input 也容忍；见上）
      resolved_model: 调度层已对齐后的模型名（别名→真实名）；调用方
        （Channel）一般会在此之后再调 `normalize_codex_model` 映射到 codex
        规范名并 body["model"] 覆写。为了 transform 独立可测，我们也在这里
        兜底：如果传了 resolved_model 且 body 未设 model，就写进去。
    """
    # 1) 模型名：codex endpoint 识别 gpt-5.1 / gpt-5.1-codex 这类规范名
    if resolved_model and _is_empty_str(body.get("model")):
        body["model"] = resolved_model
    if isinstance(body.get("model"), str):
        body["model"] = normalize_codex_model(body["model"])

    # 2) store / stream 强制
    body["store"] = False
    body["stream"] = True

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

    # 5) input 字符串 → 数组；再把 input 里的 system 消息提到 instructions
    _coerce_input_to_list(body)
    sys_text = _extract_system_messages(body)
    if sys_text:
        orig = body.get("instructions")
        if _is_empty_str(orig):
            body["instructions"] = sys_text
        else:
            body["instructions"] = f"{orig}\n\n{sys_text}"

    # 6) instructions 兜底（sub2api 行为：空 → 一行 fallback）
    if _is_empty_str(body.get("instructions")):
        body["instructions"] = _DEFAULT_INSTRUCTIONS

    return body
