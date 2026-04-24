"""OAuth→Codex 上游请求体强制改造。

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

工具调用续链（item_reference / call_* → fc*）这里**暂不搬**——它是 sub2api
为 function call 恢复 call_id 上下文做的兼容层，需要 state_store 保存。Commit 2
目标是跑通单轮请求；续链支持放后续。对应未续链场景，sub2api 也会正常删除
item_reference，等效于我们这里的实现。

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
      resolved_model: 调度层已对齐后的模型名（账号白名单已校验过的合法名字）；
        transform **原样透传**给上游，不做任何别名映射。
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
