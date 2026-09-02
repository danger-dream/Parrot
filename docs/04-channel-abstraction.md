# 04 — 渠道抽象层

统一 OAuth 账户与第三方 API 渠道为单一 `Channel` 抽象，调度器和故障转移层无需关心下层差异。

## 4.1 类层次

```
Channel (abstract)
├── OAuthChannel   — Anthropic 官方 + OAuth token + CC 伪装（强制 true，不可关）
└── ApiChannel     — 第三方兼容 URL + API Key + 模型别名 + CC 伪装（可切换）
```

## 4.2 Channel 基类（`src/channel/base.py`）

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

@dataclass
class UpstreamRequest:
    url: str                        # 完整 URL（含 query string）
    method: str = "POST"
    headers: dict[str, str]
    body: bytes                     # 已序列化的请求体

@dataclass
class ChannelDisplay:
    """TG Bot 用的展示信息"""
    key: str
    type: str                       # "oauth" | "api"
    display_name: str
    enabled: bool
    disabled_reason: Optional[str]  # None | "user" | "quota" | "auth_error"
    models: list[str]               # 客户端可见的模型名列表（alias 或 real）

class Channel(ABC):
    key: str                        # "oauth:<email>" or "api:<name>"
    type: str                       # "oauth" | "api"
    display_name: str
    enabled: bool
    disabled_reason: Optional[str]
    cc_mimicry: bool

    @abstractmethod
    def supports_model(self, requested_model: str) -> Optional[str]:
        """若支持，返回上游侧的真实模型名；否则 None。"""

    @abstractmethod
    def list_client_models(self) -> list[str]:
        """返回客户端可见的模型名列表。"""

    @abstractmethod
    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str
    ) -> UpstreamRequest:
        """把下游请求体转换为对本渠道上游的请求。"""

    @abstractmethod
    async def restore_response(self, upstream_chunk: bytes) -> bytes:
        """响应内容还原（如工具名还原）。默认直通。"""

    @abstractmethod
    def display(self) -> ChannelDisplay: ...
```

## 4.3 OAuthChannel（`src/channel/oauth_channel.py`）

```python
class OAuthChannel(Channel):
    email: str
    access_token: str
    refresh_token: str
    expired: datetime
    last_refresh: datetime
    models: list[str]               # 真实名，如 ["claude-opus-4-7", ...]
    cc_mimicry: bool = True         # 锁定 true，不从 config 读

    UPSTREAM_BASE = "https://api.anthropic.com"

    def __init__(self, cfg_entry, oauth_defaults):
        self.email = cfg_entry["email"]
        self.key = f"oauth:{self.email}"
        self.type = "oauth"
        self.display_name = self.email
        self.access_token = cfg_entry["access_token"]
        self.refresh_token = cfg_entry["refresh_token"]
        self.expired = parse_iso(cfg_entry["expired"])
        ...
        self.models = cfg_entry.get("models") or oauth_defaults
        self.cc_mimicry = True

    def supports_model(self, requested_model):
        return requested_model if requested_model in self.models else None

    def list_client_models(self):
        return list(self.models)

    async def build_upstream_request(self, body, resolved_model):
        # 1. 确保 token 有效（< 5min 过期则刷新，见 oauth_manager）
        from src.oauth_manager import ensure_valid_token
        access_token = await ensure_valid_token(self.email)

        # 2. 走完整 CC 伪装链路
        from src.transform.cc_mimicry import transform_request, sign_body, build_upstream_headers

        # 替换 body 中的 model 为真实名（OAuth 场景 resolved_model == requested_model）
        body_with_real_model = {**body, "model": resolved_model}

        payload, dynamic_tool_map = transform_request(body_with_real_model)
        self._dynamic_tool_map = dynamic_tool_map
        signed = sign_body(payload)
        headers = build_upstream_headers(access_token)

        return UpstreamRequest(
            url=f"{self.UPSTREAM_BASE}/v1/messages?beta=true",
            headers=headers,
            body=signed,
        )

    async def restore_response(self, chunk):
        from src.transform.cc_mimicry import _restore_tool_names_in_chunk
        return _restore_tool_names_in_chunk(chunk, self._dynamic_tool_map)

    def display(self):
        return ChannelDisplay(
            key=self.key, type="oauth", display_name=self.email,
            enabled=self.enabled, disabled_reason=self.disabled_reason,
            models=self.list_client_models()
        )
```

**OAuth 的约束**：
- `cc_mimicry` 硬编码 `True`，config 里这个字段对 OAuth 无效
- `supports_model` 直接匹配真实名（无别名映射）
- 失败 401/403 → 尝试刷新 token 一次后重试（见 07-failover）

## 4.4 ApiChannel（`src/channel/api_channel.py`）

```python
class ApiChannel(Channel):
    name: str
    base_url: str
    api_key: str
    models: list[dict]              # [{"real": "GLM-5", "alias": "glm-5"}, ...]
    cc_mimicry: bool                # 可切换

    def __init__(self, cfg_entry):
        self.name = cfg_entry["name"]
        self.key = f"api:{self.name}"
        self.type = "api"
        self.display_name = self.name
        self.base_url = cfg_entry["baseUrl"].rstrip("/")
        self.api_key = cfg_entry["apiKey"]
        self.models = cfg_entry.get("models", [])
        self.cc_mimicry = cfg_entry.get("cc_mimicry", True)
        self.enabled = cfg_entry.get("enabled", True)
        self.disabled_reason = cfg_entry.get("disabled_reason")

    def supports_model(self, requested_model):
        for m in self.models:
            if m["alias"] == requested_model:
                return m["real"]
        return None

    def list_client_models(self):
        return [m["alias"] for m in self.models]

    async def build_upstream_request(self, body, resolved_model):
        # 替换 model 字段为真实名
        body_with_real_model = {**body, "model": resolved_model}

        if self.cc_mimicry:
            # 走 CC 伪装（system block / metadata / CCH / 工具混淆 / cache 断点）
            from src.transform.cc_mimicry import transform_request, sign_body
            payload, dynamic_tool_map = transform_request(body_with_real_model)
            self._dynamic_tool_map = dynamic_tool_map
            signed = sign_body(payload)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": profile_betas,  # v2.1.258 model/auth profile
            }
        else:
            # 仅走"必要"转换：cache_control 统一管理 + 保留用户 system 字段
            from src.transform.standard import standard_transform
            payload = standard_transform(body_with_real_model)
            self._dynamic_tool_map = None
            signed = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }

        return UpstreamRequest(
            url=f"{self.base_url}/v1/messages",
            headers=headers,
            body=signed,
        )

    async def restore_response(self, chunk):
        if self._dynamic_tool_map:
            from src.transform.cc_mimicry import _restore_tool_names_in_chunk
            return _restore_tool_names_in_chunk(chunk, self._dynamic_tool_map)
        return chunk
```

### 4.4.1 `cc_mimicry=True` 路径包含

以 docs/05 的 v2.1.258 fixture-backed wire 规则为准：
1. fingerprint 在注入 downstream `system` 之前从原始 user prompt 计算
2. `system` 字段 → user+assistant("Understood.") 消息对注入，并保留 Parrot cache 管理
3. system billing block（version / entrypoint / CCH / request-scoped prompt ID）
4. `metadata.user_id` 包含稳定 device、空 account UUID、与 header 同值的 session ID
5. 工具名混淆（映射随 `UpstreamRequest` 返回，不存 Channel 实例）
6. CCH v258 独立 hash view 签名，不发送规范化副本
7. 按 ordinary/Fable/Opus-5/side-query 与 auth 选择精确 beta profile
8. 每个实际 HTTP dispatch 刷新 `x-client-request-id`，逻辑 retry 复用 body/session/prompt

官方 Anthropic API key 使用 `x-api-key`；已有第三方兼容渠道保留 Bearer 形态。目标 URL 仍由 `base_url` / `apiPath` 决定。

### 4.4.2 `cc_mimicry=False` 路径包含

仅必要的标准化操作：
1. 不改写 `system` 字段（Anthropic 标准保留）
2. `cache_control` 统一管理（**始终打开**，见 `docs/05-cc-mimicry.md`）
3. **不加** metadata / system_blocks / beta 头 / 工具混淆 / CCH
4. 标准路径保持既有 `x-api-key` 行为；CC 路径的官方/第三方 auth 分界见上节

两条 Anthropic 出站路径都会在最终序列化前按上游校验顺序 `tools → system → messages` 检查混合缓存 TTL。若客户端产生非法的“5m 在前、1h 在后”，Parrot 不重排任何内容，而是把最后一个 1h 之前的所有 5m（包括省略 `ttl` 的默认 5m）提升为 1h；合法的 `1h → 5m` 以及单一 TTL 请求保持原样。CC 路径在此修复之后才计算 CCH。

## 4.5 registry 模块（`src/channel/registry.py`）

```python
# 全局单例，启动时 build 一次；config 热加载后重建
_channels: dict[str, Channel] = {}

def rebuild_from_config():
    cfg = config.get()
    new = {}
    oauth_defaults = cfg["oauthDefaultModels"]
    for entry in cfg["oauthAccounts"]:
        ch = OAuthChannel(entry, oauth_defaults)
        new[ch.key] = ch
    for entry in cfg["channels"]:
        ch = ApiChannel(entry)
        new[ch.key] = ch
    global _channels
    _channels = new

def all_channels() -> list[Channel]: ...
def get_channel(key) -> Channel | None: ...
def enabled_channels() -> list[Channel]: ...
def find_by_display_name(name) -> Channel | None: ...
```

配置热加载（`config.py` 检测到 mtime 变更）时，调用 `rebuild_from_config()`。

## 4.6 模型别名语法解析

放在 `src/channel/api_channel.py` 模块级工具函数：

```python
import re

_SEP_PATTERN = re.compile(r"[,，;；\s]+")
_COLON_PATTERN = re.compile(r"[:：]")

def parse_models_input(raw: str) -> list[dict]:
    """
    解析用户在 TG Bot 中输入的模型列表：
      "GLM-5:glm-5, GLM-5-Turbo:glm-5-turbo ; gpt-5.4"
    返回 [{"real":"GLM-5","alias":"glm-5"}, ...]

    抛 ValueError 用于前端显示错误。
    """
    items = [x for x in _SEP_PATTERN.split(raw.strip()) if x]
    if not items:
        raise ValueError("模型列表不能为空")
    out, seen_aliases = [], set()
    for item in items:
        parts = _COLON_PATTERN.split(item)
        if len(parts) == 1:
            real = alias = parts[0].strip()
        elif len(parts) == 2:
            real = parts[0].strip()
            alias = parts[1].strip()
        else:
            raise ValueError(f"模型项格式错误：{item}")
        if not real or not alias:
            raise ValueError(f"模型项不能为空：{item}")
        if alias in seen_aliases:
            raise ValueError(f"别名重复：{alias}")
        seen_aliases.add(alias)
        out.append({"real": real, "alias": alias})
    return out
```

## 4.7 渠道增删改查的一致性

添加/编辑/删除渠道时必须同步清理 StateStore 相关域：

| 操作 | StateStore 需要做 |
|---|---|
| 新增 | 无（新渠道无历史） |
| 重命名（改 name/email） | 通过 `channel_state.rename_with_config()` 串行配置/reload、单个 DB 事务和内存镜像发布 |
| 删除 | 原子发布 config + priorityOrders 删除，给在途 generation 加 tombstone，再通过各运行时模块同步删除 DB 与内存镜像 |
| 修改 URL / Key / 模型 | 性能/错误数据可保留（但若模型列表变化，旧模型不再被调度） |
| 禁用 | 不清数据（重新启用可复用） |

在 `registry.rebuild_from_config()` 之后调用 `_sync_state_db_with_channels()`：

```python
def _sync_state_db_with_channels():
    """config 重建后，清理 StateStore 中已不存在的 channel_key 记录。"""
    live_keys = channel_state.include_transitions(set(_channels.keys()))
    # 所有带内存镜像的状态都通过模块层清理。scorer 在可选 DB 写
    # 不可用时会保留 memory-only 评分，因此 stale 集取 DB∪内存。
    stale_perf = {r["channel_key"] for r in state_db.perf_load_all()
                  if r["channel_key"] not in live_keys}
    stale_perf |= {key for key in scorer.channel_keys()
                   if key not in live_keys}
    stale_errors = {r["channel_key"] for r in state_db.error_load_all()
                    if r["channel_key"] not in live_keys}
    for channel_key in stale_perf:
        scorer.clear_stats(channel_key)
    for channel_key in stale_errors:
        cooldown.clear(channel_key, notify_recovered=False)
    # affinity
    affinity.delete_stale_channels(live_keys)
    affinity.client_delete_stale_channels(live_keys)
```

这样渠道删除后，runtime JSON snapshot 不会遗留孤儿数据。

运行期改名后，旧 key 会在当前进程内保留为 late-write alias：改名前已经在途的请求仍把评分、冷却和两类 affinity 写到新 key。旧 concurrency slot 按旧 key 原地排空，并冻结改名前的 `maxConcurrent`；即使 slot 先清除、稍后才有旧请求进入，也继续使用冻结值。为避免字符串 key 无法区分旧/新 generation，当前进程内禁止立刻重新创建同名旧 key；重启后不存在旧在途请求和 alias，才可安全复用。

删除渠道时，目标 key 会在 config 删除前进入进程级 tombstone。已经 acquire 的请求可按旧 generation 收尾，但 FIFO 中尚未 acquire 的 waiter 会被取消；它被唤醒后必须重查 tombstone，不能再使用已删除凭据。`try_acquire()` 在检查“并发限制已关闭”捷径之前也必须先拒绝 deleted generation。旧请求的 scorer/cooldown/两类 affinity 与 OAuth quota 副作用都会被丢弃；即使没有 concurrency slot，也可能存在“已选中渠道但尚未 acquire”或关闭并发限制的旧请求，因此 tombstone 必须保留到进程重启，不能仅凭 slot 排空解除。同名渠道只能在重启后安全复用；配置删除失败则立即撤销 tombstone，不改变原渠道。
