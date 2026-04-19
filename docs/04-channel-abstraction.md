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
                "anthropic-beta": ",".join(BETAS),  # 同 cc-proxy
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

（与 cc-proxy 等价）
1. `system` 字段 → user+assistant("Understood.") 消息对注入
2. `cache_control` 统一管理（strip + 重新打 4 个断点）
3. system_blocks（`cc_version=x.y.fp + cch=?` + "You are Claude Code, ..."）
4. `metadata = {"user_id": JSON({"device_id","account_uuid"})}`
5. 工具名混淆（静态前缀 + 动态映射）
6. CCH 签名（xxhash）
7. `anthropic-beta` 头完整列表

但使用 `Authorization: Bearer <api_key>`（而非 OAuth token），目标 URL 也指向 `base_url`。

### 4.4.2 `cc_mimicry=False` 路径包含

仅必要的标准化操作：
1. 不改写 `system` 字段（Anthropic 标准保留）
2. `cache_control` 统一管理（**始终打开**，见 `docs/05-cc-mimicry.md`）
3. **不加** metadata / system_blocks / beta 头 / 工具混淆 / CCH
4. 使用 `x-api-key`（Anthropic 标准 header）或 `Authorization`（由渠道自己约定，目前统一用 `x-api-key`）

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

添加/编辑/删除渠道时必须同步清理 state.db 相关表：

| 操作 | state.db 需要做 |
|---|---|
| 新增 | 无（新渠道无历史） |
| 重命名（改 name/email） | `perf_rename_channel`、`error_rename_channel`、`affinity_rename_channel` |
| 删除 | `perf_delete(key)`、`error_delete(key)`、`affinity_delete_by_channel(key)` |
| 修改 URL / Key / 模型 | 性能/错误数据可保留（但若模型列表变化，旧模型不再被调度） |
| 禁用 | 不清数据（重新启用可复用） |

在 `registry.rebuild_from_config()` 之后调用 `_sync_state_db_with_channels()`：

```python
def _sync_state_db_with_channels():
    """config 重建后，清理 state.db 中已不存在的 channel_key 记录。"""
    live_keys = set(_channels.keys())
    # perf
    for row in state_db.perf_load_all():
        if row["channel_key"] not in live_keys:
            state_db.perf_delete(row["channel_key"])
    # errors
    for row in state_db.error_load_all():
        if row["channel_key"] not in live_keys:
            state_db.error_delete(row["channel_key"])
    # affinity
    state_db.affinity_delete_stale_channels(live_keys)
```

这样渠道删除后，state.db 不会遗留孤儿数据。
