# 05 — CC 伪装移植清单

> **这是项目的底线。** CC 伪装的所有逻辑必须从 cc-proxy 原样移植，**禁止做"看起来等价的优化"**。任何改动都可能被 Anthropic 侧检测为异常，导致 OAuth 账号封禁。

## 5.1 移植原则

1. 所有函数签名、变量名、注释、常量、随机种子、hash 算法、字节级边界 100% 保留
2. 仅允许把全局常量从 `server.py` 挪到 `src/transform/cc_mimicry.py`，代码体不变
3. 不做"更 Pythonic"的重构（如 `dict.get` 替代 `if ... else`、itertools 替代手动循环等）—— 保持与原始一致
4. 新增必要的 Python type hints 允许，但行为分支不可改

## 5.2 需要原样移植的符号清单

从 `cc-proxy/server.py` 移到 `anthropic-proxy/src/transform/cc_mimicry.py`：

### 常量
```python
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

TOOL_NAME_REWRITES = {"sessions_": "cc_sess_", "session_": "cc_ses_"}
_FAKE_PREFIXES = [
    "analyze_", "compute_", "fetch_", "generate_", "lookup_", "modify_",
    "process_", "query_", "render_", "resolve_", "sync_", "update_",
    "validate_", "convert_", "extract_", "manage_", "monitor_", "parse_",
    "review_", "search_", "transform_", "handle_", "invoke_", "notify_",
]

CCH_SEED = 0x6E52736AC806831E
CCH_PLACEHOLDER = b"cch=00000"

ANTHROPIC_API_BASE = "https://api.anthropic.com"
```

### 函数（原样，按 cc-proxy/server.py 行号）

| cc-proxy 来源 | 移植目标 | 说明 |
|---|---|---|
| `_normalize_cch_mode` | 同名 | 不变 |
| `_normalize_cch_value` | 同名 | 不变 |
| `_load_or_create_device_id` | 迁至 `device_id.py` 或模块级 | 文件路径改为 `.anthropic_proxy_ids.json` |
| `DEVICE_ID` | 模块级常量 | 同上 |
| `compute_fingerprint` | 同名 | 不变 |
| `build_system_blocks` | 同名 | 不变（依赖 `load_config` 读 `cchMode`） |
| `inject_user_system_to_messages` | 同名 | 不变 |
| `_inject_cache_on_msg` | 同名 | 不变 |
| `_msg_has_cache_control` | 同名 | 不变 |
| `_strip_message_cache_control` | 同名 | 不变 |
| `_strip_tool_cache_control` | 同名 | 不变 |
| `add_cache_breakpoints` | 同名 | 不变 |
| `build_metadata` | 同名 | 依赖 `email`（现在来自 OAuth 账户，需改为传参而非全局） |
| `_build_dynamic_tool_map` | 同名 | 不变 |
| `_sanitize_tool_name` | 同名 | 不变 |
| `_restore_tool_names_in_chunk` | 同名 | 不变 |
| `transform_request` | 同名 | 依赖 `build_metadata`（参数化 email） |
| `sign_body` | 同名 | 不变（依赖 `cchMode`） |
| `build_upstream_headers` | 同名 | 不变（参数：access_token） |

### 需要调整的细节（不改行为）

#### `build_metadata(email)`

cc-proxy 里从全局 `_load_oauth_sync()` 读 email。多渠道场景下每个 OAuth 渠道的 email 不同，需改为参数传入：

```python
# cc-proxy 原版
def build_metadata():
    try:
        email = _load_oauth_sync().get("email", "")
        account_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, email)) if email else ""
    except Exception:
        account_uuid = ""
    return {"user_id": json.dumps({"device_id": DEVICE_ID, "account_uuid": account_uuid}, separators=(",", ":"))}

# anthropic-proxy 版
def build_metadata(email: str = ""):
    account_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, email)) if email else ""
    return {"user_id": json.dumps({"device_id": DEVICE_ID, "account_uuid": account_uuid}, separators=(",", ":"))}
```

`transform_request` 签名相应改为：
```python
def transform_request(body: dict, email: str = "") -> tuple[dict, dict | None]:
    ...
    payload = {
        ...
        "metadata": build_metadata(email),
        ...
    }
```

调用方：
- `OAuthChannel.build_upstream_request` → `transform_request(body, email=self.email)`
- `ApiChannel.build_upstream_request`（`cc_mimicry=True`）→ `transform_request(body, email="")`
  （API 渠道 account_uuid 置空是合理的，非 OAuth 场景无需账号身份）

#### `build_upstream_headers(access_token)`

cc-proxy 原版：固定返回 OAuth 头。API 渠道使用 `cc_mimicry=True` 时，不能用 `Bearer` + OAuth-specific 头（会失败）。因此：

- **OAuthChannel**：调用 `build_upstream_headers(access_token)`，完整保留
- **ApiChannel（cc_mimicry=True）**：自建 headers，仅保留 `anthropic-version` + `anthropic-beta` + `Content-Type` + `User-Agent=CLI_USER_AGENT`，auth 头换为 `x-api-key` 或 `Authorization: Bearer <api_key>`（取决于渠道约定）

## 5.3 cache_breakpoints 统一管理（始终启用）

无论 `cc_mimicry` 开关与否，`cache_control` 的统一管理**始终生效**。这是因为客户端（Claude Code、openclaw 等）会在最后一条消息打 cache_control，导致前缀随消息位置变化而失效。由代理剥离再统一打点能显著提升缓存命中率（对所有支持 prefix cache 的提供商都有益）。

### 5.3.1 标准路径（`cc_mimicry=False`）

`src/transform/standard.py`：

```python
from .cc_mimicry import (
    _strip_message_cache_control,
    _strip_tool_cache_control,
    add_cache_breakpoints,
)

def standard_transform(body: dict) -> dict:
    """非 CC 伪装路径：保留 Anthropic 标准字段，但统一管理 cache_control。"""
    messages = body.get("messages", [])
    messages = _strip_message_cache_control(messages)
    messages = add_cache_breakpoints(messages)

    payload = {
        "model": body["model"],
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", True),
    }

    # 保留用户 system 字段原样（不转为 user+assistant）
    if "system" in body:
        payload["system"] = body["system"]
        # 若 system 是 list，也打一个 ephemeral 1h 作为前缀断点
        if isinstance(payload["system"], list) and payload["system"]:
            system = [dict(b) for b in payload["system"]]
            system[-1] = {**system[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}
            payload["system"] = system

    for k in ("temperature", "top_p", "top_k", "stop_sequences", "thinking",
              "context_management", "output_config", "tool_choice", "metadata"):
        if k in body:
            payload[k] = body[k]

    if body.get("tools"):
        tools = _strip_tool_cache_control([dict(t) for t in body["tools"]])
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        payload["tools"] = tools

    return payload
```

打点位置（与 CC 路径一致）：
- system[-1]（若存在）
- tools[-1]（若存在）
- messages[-1]
- messages 中倒数第二个 user turn（若消息数 ≥ 4）

共 4 个 1h ephemeral 断点，Anthropic 上限为 4，正好。

### 5.3.2 CC 路径

不变，使用 cc-proxy 移植过来的 `transform_request`。

## 5.4 移植后的测试（纯离线，不发送真实 OAuth 请求）

> **硬约束**：CC 伪装的验证**严禁**向 `api.anthropic.com` 发送真实请求。
> 重复的 OAuth 登录、连续的相同模式调用会触发 Anthropic 风控，可能导致账号异常甚至封禁。
> cc-proxy 当前在线上运行，真实请求行为已被它覆盖；anthropic-proxy 的 CC 伪装只需证明"字节级输出一致"即可，不需要、也不允许再对远端发测试请求。

### 5.4.1 离线字节级对比测试

在 M2 阶段，编写 `tests/compare_transform.py`：

```python
# 固定输入（fixture）：
#   从 cc-proxy 的 logs/2026-04.db 里抓取若干条真实的 request_body JSON
#   覆盖以下场景：
#     - 无 tools、无 system
#     - 含 string 型 system
#     - 含 list 型 system（多 block）
#     - 工具数 ≤ 5（静态前缀映射）
#     - 工具数 > 5（动态映射触发）
#     - 含 thinking
#     - 含 context_management
#     - 含图片 / 工具调用历史（多模态 content block）
#     - messages 含 tool_result（user 角色）
#
# 对每个 fixture：
#   1. 用 cc-proxy 的 transform_request() 生成 payload_cc
#   2. 用 cc-proxy 的 sign_body() 生成 signed_cc（bytes）
#   3. 用 anthropic-proxy 的 transform_request(body, email=<fixture 对应邮箱>)
#      + sign_body()         生成 signed_ap（bytes）
#   4. assert signed_cc == signed_ap     # 逐字节相等
#
# 同时对比：
#   - build_upstream_headers(<同一 token>)   → headers 逐键相等
#   - _restore_tool_names_in_chunk(<同一 chunk>, <同一 map>)  → 逐字节相等
```

要求：所有 fixture 的输出**逐字节相等**（email 参数与 fixture 来源一致时）。

### 5.4.2 禁止事项清单

M2 验收期间**禁止**的操作：
- ❌ 用 anthropic-proxy 的任一 `OAuthChannel` 实例调用 `api.anthropic.com`
- ❌ 启动 anthropic-proxy 后让它接管真实流量做"A/B 对比"
- ❌ 用新 OAuth token（刚 PKCE 登录的）做联通性测试
- ❌ 在同一账号上同时跑 cc-proxy + anthropic-proxy 发真实请求（会出现 device_id/account 异常模式）

允许的操作：
- ✅ 在本地单元测试里用 fixture 做离线比对
- ✅ 读当前 cc-proxy 的 logs DB 抓 fixture
- ✅ 用已存在的 `oauth.json` token 在离线测试中作为"签名参数"（不实际发 HTTP）

### 5.4.3 真实验证由用户侧进行

cc-proxy 当前在线上跑，真实的 OAuth 请求已经被充分验证过。等 anthropic-proxy 的所有里程碑完成后，由用户（您）自行决定：
- 切多少流量到 anthropic-proxy
- 观察期多久
- 异常时如何回滚（把下游指回 cc-proxy）

开发方（Claude）**不主动发起**任何对真实 OAuth 账号的测试调用。

## 5.5 升级策略

cc-proxy 目前使用 `CC_VERSION = "2.1.92"`。未来 Claude Code CLI 版本升级时：

1. 先在 cc-proxy 中修改 `CC_VERSION`、必要时追加 `BETAS` 项
2. 验证 cc-proxy 稳定后，把修改同步到 anthropic-proxy 的 `src/transform/cc_mimicry.py`
3. 两者版本号同步升级

cc-proxy 依然作为参考基准存在；如果两者出现行为差异，以 cc-proxy 为准。

## 5.6 device_id 处理

`.anthropic_proxy_ids.json` 文件存储：
```json
{"device_id": "<32 字节 hex>"}
```

首次启动时生成随机值，永久复用。**不要复制 cc-proxy 的 `.cc_proxy_ids.json`**——两个服务应使用不同的 device_id（避免 Anthropic 侧同一 device_id 出现跨 account 的异常调用模式）。

首次启动生成时，记得更新目录下的 `.gitignore`（如果有 git 仓）。
