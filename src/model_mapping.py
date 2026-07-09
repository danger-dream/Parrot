"""模型别名映射 & 入口默认模型。

新语义: 模型映射按模型名全局生效, 不再按协议家族/入口拆分。
旧配置里三条入口协议各自独立的映射表仍兼容读取:

  - anthropic         (/v1/messages)
  - openai-chat       (/v1/chat/completions)
  - openai-responses  (/v1/responses)

配置结构(config.json 根层):

```
"modelMapping": {
  "global":           {"gpt-5.5": "deepseek-4v-pro"},
  "anthropic":        {"claude-sonnet-4-8": "claude-sonnet-4-5"},
  "openai-chat":      {"gpt-5.5": "gpt-5.4"},
  "openai-responses": {"gpt-5.5-codex": "gpt-5.4"}
},
"ingressDefaultModel": {
  "anthropic":        "claude-sonnet-4-5",
  "openai-chat":      "gpt-5.4",
  "openai-responses": "gpt-5.4"
}
```

语义:
  - **映射**: 下游传 key(别名) → 代理把请求体 body.model 改写成 value(真实名),
    后续白名单 / scheduler / channel / transform 一律按真实名处理。**只解一层**
    (防止 A→B→C 这类递归意图)。新 UI 写入 modelMapping.global；运行期
    读取 global + 旧入口专属映射，global 优先。
  - **默认模型**: body 缺失 model 时兜底填入该入口的默认。
  - **白名单校验**: 采用映射**之后**的真实名(API Key 授权只需列真名)。

对外只暴露两个纯函数, handler 调用时先 `apply_default` 再 `apply_mapping`。
TG bot 菜单通过 `get_ingress_map` / `set_mapping` / `set_default` 等辅助
函数增删查改, 底层写 `config.update(...)` 原子落盘并触发热加载。
"""

from __future__ import annotations

from typing import Optional

from . import config

# ─── 常量 ─────────────────────────────────────────────────────────

#: 合法的 ingress 线。外部调用全部复用该枚举,避免散落字符串。
INGRESS_LINES: tuple[str, ...] = ("anthropic", "openai-chat", "openai-responses")

# 新 UI 只展示/写入这条全局映射线。运行时仍接收三条 ingress line, 用于
# 兼容旧默认模型与旧 per-ingress 映射。
GLOBAL_MAPPING_LINE = "global"
UI_MAPPING_LINES: tuple[str, ...] = (GLOBAL_MAPPING_LINE,)

#: ingress → family (registry.available_models_for_families 认的名)
INGRESS_FAMILY: dict[str, str] = {
    "anthropic":        "anthropic",
    "openai-chat":      "openai",
    "openai-responses": "openai",
}

#: 友好显示名 (TG 菜单用)
INGRESS_LABEL: dict[str, str] = {
    "anthropic":        "Anthropic (/v1/messages)",
    "openai-chat":      "OpenAI & Grok Chat (/v1/chat/completions)",
    "openai-responses": "OpenAI & Grok Responses (/v1/responses)",
    GLOBAL_MAPPING_LINE: "全局模型映射",
}


def _coerce_str_map(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v) for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and k and v
    }


def _root_mapping() -> dict:
    cfg = config.get()
    root = cfg.get("modelMapping") or {}
    return root if isinstance(root, dict) else {}


def _flat_legacy_root(root: dict) -> dict[str, str]:
    """Support the optional flat shape: {alias: real}."""
    if not isinstance(root, dict):
        return {}
    if any(isinstance(v, dict) for v in root.values()):
        return {}
    return _coerce_str_map(root)


# ─── 读 ───────────────────────────────────────────────────────────

def get_ingress_map(ingress: str) -> dict[str, str]:
    """读 alias→real 映射表(返回副本,调用方随便改不影响 cache)。

    - ingress == "global": 仅返回新全局映射；若没有 global, 会把旧三条
      per-ingress 映射合并给 UI 展示。
    - ingress 为运行时入口: 返回 legacy specific + global, global 优先。
    """
    if ingress not in INGRESS_LINES and ingress != GLOBAL_MAPPING_LINE:
        return {}
    root = _root_mapping()
    flat = _flat_legacy_root(root)
    if flat:
        return flat

    global_map = _coerce_str_map(root.get(GLOBAL_MAPPING_LINE))
    if ingress == GLOBAL_MAPPING_LINE:
        if global_map:
            return global_map
        merged: dict[str, str] = {}
        for line in INGRESS_LINES:
            merged.update(_coerce_str_map(root.get(line)))
        return merged

    merged = _coerce_str_map(root.get(ingress))
    merged.update(global_map)
    return merged


def get_default_model(ingress: str) -> Optional[str]:
    """读某条 ingress 的默认模型; 未设置返回 None。"""
    if ingress not in INGRESS_LINES and ingress != GLOBAL_MAPPING_LINE:
        return None
    cfg = config.get()
    root = cfg.get("ingressDefaultModel") or {}
    if not isinstance(root, dict):
        return None
    val = root.get(ingress)
    if not val and ingress in INGRESS_LINES:
        val = root.get(GLOBAL_MAPPING_LINE)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


# ─── 运行期应用 ────────────────────────────────────────────────────

def apply_default(body: dict, ingress: str) -> None:
    """body.model 缺失时,按 ingress 的默认填入。就地修改。

    仅在 `body.get("model")` 为假值(None/"")时生效; 显式传了模型名不覆盖。
    无默认或 ingress 非法时不动。
    """
    if not isinstance(body, dict):
        return
    current = body.get("model")
    if isinstance(current, str) and current.strip():
        return
    default = get_default_model(ingress)
    if default:
        body["model"] = default


def apply_mapping(body: dict, ingress: str) -> Optional[tuple[str, str]]:
    """按 ingress 的映射把 body.model 从别名改写为真实名(**只解一层**)。

    返回:
      - 命中并改写: (alias, real)
      - 没命中: None

    就地修改 body; 非 dict / 无 model / ingress 非法一律直接返回 None 不抛。
    """
    if not isinstance(body, dict):
        return None
    if ingress not in INGRESS_LINES and ingress != GLOBAL_MAPPING_LINE:
        return None
    alias = body.get("model")
    if not (isinstance(alias, str) and alias.strip()):
        return None
    mapping = get_ingress_map(ingress)
    real = mapping.get(alias)
    if not real or real == alias:
        return None
    body["model"] = real
    return (alias, real)


# ─── 写 (给 TG bot 菜单用) ────────────────────────────────────────

def set_mapping(ingress: str, alias: str, real: str) -> None:
    """新增或覆盖一条别名→真实名映射。"""
    if ingress not in INGRESS_LINES and ingress != GLOBAL_MAPPING_LINE:
        raise ValueError(f"invalid ingress line: {ingress!r}")
    alias = (alias or "").strip()
    real = (real or "").strip()
    if not alias or not real:
        raise ValueError("alias and real must be non-empty")
    if alias == real:
        raise ValueError("alias must differ from real model name")

    def _mutate(cfg: dict) -> None:
        root = cfg.setdefault("modelMapping", {})
        if not isinstance(root, dict):
            root = {}
            cfg["modelMapping"] = root
        line_key = GLOBAL_MAPPING_LINE if ingress == GLOBAL_MAPPING_LINE else ingress
        line = root.setdefault(line_key, {})
        if not isinstance(line, dict):
            line = {}
            root[line_key] = line
        line[alias] = real
    config.update(_mutate)


def remove_mapping(ingress: str, alias: str) -> bool:
    """删一条映射; 返回 True 表示确实删了。"""
    if ingress not in INGRESS_LINES and ingress != GLOBAL_MAPPING_LINE:
        return False
    alias = (alias or "").strip()
    if not alias:
        return False
    removed = [False]

    def _mutate(cfg: dict) -> None:
        root = cfg.get("modelMapping") or {}
        line_key = GLOBAL_MAPPING_LINE if ingress == GLOBAL_MAPPING_LINE else ingress
        line = root.get(line_key) if isinstance(root, dict) else None
        if isinstance(line, dict) and alias in line:
            del line[alias]
            removed[0] = True
    config.update(_mutate)
    return removed[0]


def set_default(ingress: str, real: str) -> None:
    """设置某条 ingress 的默认模型。"""
    if ingress not in INGRESS_LINES and ingress != GLOBAL_MAPPING_LINE:
        raise ValueError(f"invalid ingress line: {ingress!r}")
    real = (real or "").strip()
    if not real:
        raise ValueError("default model must be non-empty")

    def _mutate(cfg: dict) -> None:
        root = cfg.setdefault("ingressDefaultModel", {})
        if not isinstance(root, dict):
            root = {}
            cfg["ingressDefaultModel"] = root
        root[ingress] = real
    config.update(_mutate)


def clear_default(ingress: str) -> bool:
    """清除某条 ingress 的默认模型。返回 True 表示确实清了。"""
    if ingress not in INGRESS_LINES and ingress != GLOBAL_MAPPING_LINE:
        return False
    cleared = [False]

    def _mutate(cfg: dict) -> None:
        root = cfg.get("ingressDefaultModel") or {}
        if isinstance(root, dict) and ingress in root:
            del root[ingress]
            cleared[0] = True
    config.update(_mutate)
    return cleared[0]


def list_available_models_for(ingress: str) -> list[str]:
    """列出该 ingress 下「可选的真实模型名」(供 TG 菜单按钮列表用)。

    全局映射不再按家族过滤；旧入口专属 UI/调用仍保留原过滤行为。
    调用 registry 时放在函数内 import, 避免循环。
    """
    if ingress == GLOBAL_MAPPING_LINE:
        from .channel import registry
        return registry.available_models()
    if ingress not in INGRESS_LINES:
        return []
    from .channel import registry
    fam = INGRESS_FAMILY[ingress]
    return registry.available_models_for_families({fam})
