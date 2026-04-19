# 08 — 多 OAuth 账户管理

## 8.0 开发期约束（硬规则）

> ⛔ **本文档描述的所有 OAuth 远端交互（token 刷新、profile 拉取、usage 拉取、PKCE 换 token），在开发与里程碑验收阶段均不向 `api.anthropic.com` 发起真实调用。**
>
> 重复的 OAuth 登录、连续固定模式的调用会触发 Anthropic 风控，可能导致账号异常。cc-proxy 当前在线上运行并承担真实流量，本项目 **只需把代码设计完善、离线/mock 验证通过即可**。真实 OAuth 联通性由用户（您）在交付后自行验证。
>
> 本文档中所有函数（`ensure_valid_token` / `force_refresh` / `fetch_usage` / `fetch_profile` / `_do_refresh_sync` / PKCE 流程）的实现必须：
> - 封装在 `oauth_manager.py` 中，网络调用入口集中
> - 支持通过环境变量 `DISABLE_OAUTH_NETWORK_CALLS=1` 或 config 中 `oauth.mockMode=true` 切换为"返回 mock 数据"（开发期默认开启）
> - 单元测试完全不依赖网络（用 fixture 或 mock httpx client）

## 8.1 数据模型

OAuth 账户在 `config.json.oauthAccounts` 数组中，每条结构：

```jsonc
{
  "email": "user@gmail.com",
  "access_token": "sk-ant-oat01-...",
  "refresh_token": "sk-ant-ort01-...",
  "expired": "2026-04-18T05:26:49Z",        // ISO UTC
  "last_refresh": "2026-04-17T21:26:49Z",
  "type": "claude",
  "enabled": true,
  "disabled_reason": null,    // null | "user" | "quota" | "auth_error"
  "disabled_until": null,     // quota 模式下为 resets_at（最大）
  "models": [...]             // 空则用 oauthDefaultModels
}
```

账户身份用 `email` 作为唯一键（渠道 `key = "oauth:" + email`）。

## 8.2 oauth_manager 模块

`src/oauth_manager.py`：

```python
import asyncio, threading
from datetime import datetime, timezone, timedelta
import httpx

_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# OAuth 登录 PKCE
_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
_MANUAL_REDIRECT = "https://platform.claude.com/oauth/code/callback"
_SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"

_account_lock = threading.Lock()   # 保护 config 中 oauthAccounts 的读写
_refresh_in_flight = {}            # email -> asyncio.Lock
```

### 8.2.1 读取账户

```python
def get_account(email: str) -> dict | None:
    cfg = config.get()
    for acc in cfg.get("oauthAccounts", []):
        if acc["email"] == email:
            return acc
    return None

def list_accounts() -> list[dict]:
    return list(config.get().get("oauthAccounts", []))
```

### 8.2.2 Token 刷新

```python
async def ensure_valid_token(email: str) -> str:
    """被 OAuthChannel.build_upstream_request 调用，保证 token 可用。
    若 < 5 分钟过期，阻塞刷新；否则直接返回缓存值。"""
    lock = _refresh_in_flight.setdefault(email, asyncio.Lock())
    async with lock:
        acc = get_account(email)
        if not acc:
            raise ValueError(f"unknown email: {email}")
        expired = _parse_iso(acc.get("expired"))
        if expired:
            remaining = (expired - datetime.now(timezone.utc)).total_seconds()
            if remaining >= 300:
                return acc["access_token"]
        return await asyncio.to_thread(_do_refresh_sync, email)

async def force_refresh(email: str) -> str:
    """无视剩余时间，强制刷新（用于 401/403 重试前）。"""
    lock = _refresh_in_flight.setdefault(email, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(_do_refresh_sync, email)

def _do_refresh_sync(email: str) -> str:
    with _account_lock:
        cfg = config.get()
        acc = None
        for a in cfg.get("oauthAccounts", []):
            if a["email"] == email:
                acc = a
                break
        if not acc:
            raise ValueError(f"unknown email: {email}")

    resp = httpx.post(_TOKEN_URL, json={
        "grant_type": "refresh_token",
        "refresh_token": acc["refresh_token"],
        "client_id": _CLIENT_ID,
    }, headers={
        "Content-Type": "application/json",
        "User-Agent": CLI_USER_AGENT,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    new_expired = (datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 28800))).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _account_lock:
        cfg = config.get()
        for a in cfg["oauthAccounts"]:
            if a["email"] == email:
                a["access_token"] = data["access_token"]
                if "refresh_token" in data:
                    a["refresh_token"] = data["refresh_token"]
                a["expired"] = new_expired
                a["last_refresh"] = now_iso
                # 成功刷新 → 清除可能的 auth_error 禁用
                if a.get("disabled_reason") == "auth_error":
                    a["disabled_reason"] = None
                    a["disabled_until"] = None
                break
        config.save()

    return data["access_token"]
```

### 8.2.3 用量拉取

```python
async def fetch_usage(email: str) -> dict:
    """拉取远端 usage，返回原始 dict。"""
    access_token = await ensure_valid_token(email)
    def _do():
        r = httpx.get(_USAGE_URL, headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLI_USER_AGENT,
        }, timeout=15)
        r.raise_for_status()
        return r.json()
    return await asyncio.to_thread(_do)

async def fetch_profile(email: str) -> dict:
    # 与 fetch_usage 同理，用于 PKCE 登录后拉 email
    ...
```

### 8.2.4 添加 / 删除 / 禁用

```python
def add_account(entry: dict):
    """entry 需至少含 email/access_token/refresh_token。"""
    with _account_lock:
        cfg = config.get()
        accounts = cfg.setdefault("oauthAccounts", [])
        if any(a["email"] == entry["email"] for a in accounts):
            raise ValueError(f"Email already exists: {entry['email']}")
        accounts.append(entry)
        config.save()
    registry.rebuild_from_config()

def delete_account(email: str):
    with _account_lock:
        cfg = config.get()
        accounts = cfg.get("oauthAccounts", [])
        cfg["oauthAccounts"] = [a for a in accounts if a["email"] != email]
        config.save()
    # 清理 state.db
    state_db.perf_delete(f"oauth:{email}")
    state_db.error_delete(f"oauth:{email}")
    state_db.affinity_delete_by_channel(f"oauth:{email}")
    state_db.quota_delete(email)
    registry.rebuild_from_config()

def set_enabled(email: str, enabled: bool, reason: str | None = None):
    """
    enabled=False → 禁用（reason 填 "user" / "quota" / "auth_error"）
    enabled=True  → 清除 disabled_reason/until
    """
    ...

def set_disabled_by_quota(email: str, resets_at: str):
    """由 quota_monitor 调用，自动禁用。"""
    ...
```

### 8.2.5 配额监控

```python
async def quota_monitor_loop():
    while True:
        try:
            cfg = config.get()
            threshold = cfg["quotaMonitor"]["disableThresholdPercent"]
            accounts = cfg.get("oauthAccounts", [])
            for acc in accounts:
                # 跳过已被用户禁用的
                if acc.get("disabled_reason") == "user":
                    continue
                # 跳过 auth_error 的
                if acc.get("disabled_reason") == "auth_error":
                    continue
                try:
                    usage = await fetch_usage(acc["email"])
                except Exception as e:
                    print(f"[quota] {acc['email']}: fetch failed: {e}")
                    continue

                # 保存缓存
                state_db.quota_save(acc["email"], _flatten_usage(usage))

                # 提取各项利用率
                utils = _extract_utils(usage)
                any_over = any(u >= threshold for u in utils if u is not None)

                if any_over:
                    # 计算最晚 resets_at（作为 disabled_until）
                    latest_reset = _latest_reset(usage)
                    if acc.get("disabled_reason") != "quota":
                        print(f"[quota] disable {acc['email']} (util >= {threshold}%, reset={latest_reset})")
                        set_disabled_by_quota(acc["email"], latest_reset)
                else:
                    # 若当前因 quota 禁用，且所有 util < threshold → 清除
                    if acc.get("disabled_reason") == "quota":
                        # 额外要求：resets_at 已过
                        if _parse_iso(acc.get("disabled_until")) and _parse_iso(acc["disabled_until"]) <= datetime.now(timezone.utc):
                            print(f"[quota] resume {acc['email']}")
                            set_enabled(acc["email"], True)
        except Exception as e:
            print(f"[quota] loop error: {e}")

        await asyncio.sleep(cfg["quotaMonitor"]["intervalSeconds"])
```

### 8.2.6 Proactive token refresh

```python
async def proactive_refresh_loop():
    """扫所有启用的账号，剩余 < 10min 的主动刷。"""
    while True:
        try:
            cfg = config.get()
            for acc in cfg.get("oauthAccounts", []):
                if not acc.get("enabled", True):
                    continue
                if acc.get("disabled_reason") in ("user",):
                    continue
                expired = _parse_iso(acc.get("expired"))
                if not expired:
                    continue
                remaining = (expired - datetime.now(timezone.utc)).total_seconds()
                if remaining < 600:
                    try:
                        await force_refresh(acc["email"])
                        tgbot.notify_admins(f"✅ Proactive refresh OK: {acc['email']}")
                    except Exception as e:
                        # 标记 auth_error
                        set_enabled(acc["email"], False, reason="auth_error")
                        tgbot.notify_admins(f"⚠️ Refresh failed: {acc['email']}: {e}")
        except Exception as e:
            print(f"[oauth proactive] loop error: {e}")
        await asyncio.sleep(60)
```

## 8.3 PKCE 登录流程（复用 cc-proxy 的 tgbot.py 逻辑）

迁移到 `src/oauth_manager.py` + `src/telegram/menus/oauth_menu.py`：

### 步骤
1. TG Bot：用户点「新增账户」→「登录获取 Token」
2. 生成 `code_verifier` + `code_challenge`（S256）
3. 构建 URL：
   ```
   https://claude.com/cai/oauth/authorize?
     code=true&
     client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&
     response_type=code&
     redirect_uri=https://platform.claude.com/oauth/code/callback&
     scope=<OAUTH_SCOPES>&
     code_challenge=<challenge>&
     code_challenge_method=S256&
     state=<random>
   ```
4. TG Bot 发给用户，要求复制回调页面上的 code（格式 `code#state`）
5. 拆分：`code = raw.split("#", 1)[0]`
6. 用 code + code_verifier 换 token：
   ```python
   resp = httpx.post(_TOKEN_URL, json={
       "grant_type": "authorization_code",
       "code": code,
       "redirect_uri": _MANUAL_REDIRECT,
       "client_id": _CLIENT_ID,
       "code_verifier": code_verifier,
       "state": state,
   }, headers={"Content-Type": "application/json", "User-Agent": CLI_USER_AGENT}, timeout=15)
   ```
7. 拿到 `access_token` / `refresh_token` / `expires_in`
8. 用 access_token 拉 profile 得到 email
9. `add_account({...})`
10. 提示用户成功

## 8.4 手动设置 JSON

用户直接粘贴：
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "email": "xxx@gmail.com",
  "expired": "2026-04-18T05:26:49Z"
}
```

验证必填字段 → 若缺 `email`，尝试用 `access_token` 调 profile 补齐 → 调 `add_account`。

## 8.5 device_id 共享策略

**全局唯一**的 `device_id`（持久化在 `.anthropic_proxy_ids.json`），所有 OAuth 账户共享。

理由：
- device_id 模拟"一台机器的 CLI 安装"，一台服务器就是一个设备
- 多账户通过 `account_uuid = uuid5(DNS, email)` 区分，二者组合成 `metadata.user_id` JSON
- 与 Claude Code CLI 实际的行为一致（多账户切换时 device_id 不变）

## 8.6 渠道选择时如何处理多 OAuth

调度器筛选时把每个 OAuth 账户视为独立渠道：
- `key = "oauth:<email>"`
- `supports_model` 用账户的 `models` 字段（或默认列表）
- 性能统计独立（每账户各自 `(channel_key, model)` 一行）
- 冷却独立
- 亲和可以跨账号（同一会话可能被绑到 A 账户，若 A 禁用，下次查亲和失败，重新调度选 B，第二次响应后亲和 key 重新绑到 B——上次的缓存失效但系统仍可用）

## 8.7 多 OAuth 的请求处理示例

```
请求到达 model=claude-opus-4-7, messages=...
→ scheduler 筛选出 [oauth:a@g.com, oauth:b@g.com, api:智谱...]（都支持该模型）
→ 计算指纹 H
→ 查 affinity[H] → 命中 oauth:a@g.com, opus-4-7
  → 打破检查（score a vs 最优）通过 → a 顶到首位
→ failover 顺序尝试 a → b → 智谱
  → a 返回 401（token 过期被 Anthropic 拒）
  → force_refresh(a) → 重新入队 a
  → 第二次 a 请求成功
→ 写亲和：H → oauth:a@g.com, opus-4-7（touch last_used）
→ 记 perf_success(a, opus-4-7)
```

## 8.8 从 cc-proxy 迁移单账户

用户自行操作：

1. 打开 `cc-proxy/oauth.json`，复制内容（4 个字段：access_token / refresh_token / expired / email）
2. 打开 `anthropic-proxy/config.json`，在 `oauthAccounts` 数组里追加一条：
   ```json
   {
     "email": "<从 oauth.json 复制>",
     "access_token": "<复制>",
     "refresh_token": "<复制>",
     "expired": "<复制>",
     "type": "claude",
     "enabled": true,
     "disabled_reason": null,
     "models": []
   }
   ```
3. 热加载（config 有 mtime 检测，无需重启）
4. 用 TG Bot 验证 /oauth 菜单能看到账户与用量

## 8.9 账户删除的二次确认

TG Bot 点「删除账户」→ 弹出「确认删除 xxx@gmail.com？这将清除该账户的所有统计和绑定数据」→ 两个按钮：确认 / 取消。

确认后：
- `oauth_manager.delete_account(email)`
- `state_db` 清理（perf / error / affinity / quota）
- TG 回显 `"✅ 已删除"`
