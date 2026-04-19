# AnthropicProxy

[![Docker Image](https://img.shields.io/badge/ghcr.io-anthropicproxy-blue?logo=docker)](https://github.com/danger-dream/AnthropicProxy/pkgs/container/anthropicproxy)
[![Build](https://github.com/danger-dream/AnthropicProxy/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/danger-dream/AnthropicProxy/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**多渠道、智能调度、故障转移的 Anthropic 协议代理**

一个兼容 Anthropic `/v1/messages` 协议的反向代理工具，支持把下游客户端（Claude Code CLI、openclaw、任何 Anthropic SDK 应用）的请求透明地路由到多个上游（官方 OAuth 账户 + 第三方 Anthropic 兼容云服务），具备：

- **多渠道聚合**：Claude Code OAuth 账户 + 第三方 Coding Plan（智谱 / 天翼云 / 京东云 / 讯飞星辰 等）
- **智能调度**：基于滑动窗口评分（延迟 + 失败惩罚）+ 会话亲和绑定 + 20% 探索率
- **故障转移**：未发首包 → 自动切下一候选；已发首包 → 流内 SSE error 收尾
- **完整 CC 伪装**：与 cc-proxy 同源的请求签名逻辑（指纹 / CCH / 工具名混淆 / cache 断点）
- **Telegram Bot 管理面板**：渠道管理 / OAuth 账户 / API Key / 统计 / 日志 / 系统设置 全图形化
- **运行时保护**：四段超时独立 + 错误阶梯冷却 + 首包文本黑名单 + OAuth 配额自动禁用/恢复

---

## 目录

- [快速开始](#快速开始)
- [架构概览](#架构概览)
- [HTTP 接口](#http-接口)
- [Telegram Bot 管理面板](#telegram-bot-管理面板)
- [📢 通知系统](#-通知系统)
- [配置文件](#配置文件)
- [运维](#运维)
- [目录结构](#目录结构)
- [故障排查](#故障排查)

---

## 快速开始

提供三种部署方式，**推荐用一键脚本**。

### 方式一：一键脚本（推荐）

```bash
bash <(curl -Ls https://raw.githubusercontent.com/danger-dream/AnthropicProxy/main/deploy.sh)
```

脚本会：
1. 显示项目信息
2. 检查 / 引导安装 Docker + Docker Compose
3. 交互式收集：安装目录 / TG Bot Token / Admin Telegram User ID / 监听端口
4. 生成 `docker-compose.yml` + 最小 `data/config.json`
5. `docker compose pull && up -d`
6. 自动验证 `/health` 和 TG Bot polling

完成后到 Telegram 找你的 bot 发 `/start`，剩下的渠道 / OAuth / API Key 全在 TG 图形界面里配。

### 方式二：Docker Compose（手动）

```bash
mkdir -p anthropic-proxy/data && cd anthropic-proxy

# 拿 compose 模板
curl -Lo docker-compose.yml https://raw.githubusercontent.com/danger-dream/AnthropicProxy/main/docker-compose.yml

# 写最小 config.json（首次启动 server 会自动补全其余默认字段）
cat > data/config.json <<'EOF_CFG'
{
  "listen": { "host": "0.0.0.0", "port": 22122 },
  "telegram": {
    "botToken": "<你的 bot token>",
    "adminIds": [<你的 Telegram user id>]
  }
}
EOF_CFG

docker compose up -d
docker compose logs -f
```

### 方式三：Docker 直跑（不用 compose）

```bash
mkdir -p ./data
# 同样要先写 ./data/config.json（见上）

docker run -d \
  --name anthropic-proxy \
  --restart unless-stopped \
  -p 22122:22122 \
  -e TZ=Asia/Shanghai \
  -e ANTHROPIC_PROXY_DATA_DIR=/app/data \
  -v "$PWD/data:/app/data" \
  ghcr.io/danger-dream/anthropicproxy:latest
```

### 方式四：源码运行（开发用）

```bash
git clone https://github.com/danger-dream/AnthropicProxy
cd AnthropicProxy
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 编辑 config.json（首次启动会自动生成模板）
./venv/bin/python server.py
```

### 下游客户端接入

```bash
curl http://<server>:22122/v1/messages \
  -H "x-api-key: ccp-你的Key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [{ "role": "user", "content": "Hello" }]
  }'
```

> **配置 / OAuth / 渠道全部在 TG Bot 图形界面里完成**：
> - 🔀 渠道管理：添加第三方 API 渠道（含测试向导）
> - 🔐 管理 OAuth：PKCE 登录或粘贴已有 OAuth JSON 添加 Claude 官方账户
> - 🔑 管理 API Key：创建下游调用用的 Key（可设模型白名单）

---

## 架构概览

```
┌────────────────┐
│ 下游客户端      │  (Claude Code CLI / openclaw / SDK)
└────────┬───────┘
         │ x-api-key
         ▼
┌────────────────────────────────────────────────────────┐
│                   FastAPI 入口                          │
│  auth.validate → allowed_models 检查                    │
└────────┬───────────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────────────────────┐
│  scheduler.schedule                                     │
│    1. 筛选可用渠道（enabled + 非冷却 + 支持模型）        │
│    2. 会话亲和（fingerprint = key+ip+msg[-2:] 的 hash）│
│    3. 评分排序（EMA 延迟 + 失败惩罚 + 20% 探索率）      │
└────────┬───────────────────────────────────────────────┘
         │ candidates: [(channel, resolved_model), ...]
         ▼
┌────────────────────────────────────────────────────────┐
│  failover.run_failover (顺序尝试)                       │
│    • _try_channel                                       │
│        build_upstream_request →                         │
│        httpx.stream (连接/首字/读 硬超时)               │
│        首包安全检查（黑名单 / upstream error JSON）     │
│    • 未发首包失败 → 切下一候选                          │
│    • 已发首包失败 → 流内 SSE error 收尾                 │
└────────┬───────────────────────────────────────────────┘
         │
         ▼
  ┌──────────────────┬──────────────────┐
  │ OAuthChannel     │ ApiChannel        │
  │ (官方+CC伪装)    │ (第三方云厂商)    │
  └──────────────────┴──────────────────┘
         │                   │
         ▼                   ▼
   api.anthropic.com      第三方 Anthropic 兼容服务
```

**核心特性：**

| 维度 | 说明 |
|---|---|
| **首包锁** | 向下游发任何字节前为"可切换"区；首字节发出后锁定渠道 |
| **四段超时** | `connect` / `firstByte` / `idle`（chunk 间）/ `total`（硬上限） |
| **会话亲和** | 指纹 = `hash(key \| ip \| 倒数两条消息 canonical JSON)`，30min TTL |
| **错误冷却** | 阶梯 `[1, 3, 5, 10, 15, 0]` 分钟，`0` = 永久；成功一次清零 |
| **CC 伪装** | 完整移植 Claude Code，OAuth 渠道强制启用；API 渠道可选 |
| **OAuth 配额监控** | 每 60s 拉 usage，≥95% 自动禁用，resets_at 过后自动恢复 |

详见 `docs/` 目录下 12 篇设计文档。

---

## HTTP 接口

### `POST /v1/messages`
**完整兼容 Anthropic Messages API**。鉴权通过 `x-api-key` 或 `Authorization: Bearer <key>`。

- 流式（`stream: true`，默认）：SSE 流
- 非流式：JSON 响应
- 错误：标准 Anthropic 错误格式 `{"type": "error", "error": {"type": "...", "message": "..."}}`

### `GET /v1/models`
**Anthropic 标准模型列表**。返回当前所有启用渠道聚合的可用模型（按 API Key 白名单过滤）：

```bash
curl http://<server>:22122/v1/models -H "x-api-key: ccp-xxx"
```

```json
{
  "data": [
    { "type": "model", "id": "claude-opus-4-7", "display_name": "claude-opus-4-7", "created_at": "2025-01-01T00:00:00Z" },
    { "type": "model", "id": "glm-5", "display_name": "glm-5", "created_at": "2025-01-01T00:00:00Z" }
  ],
  "first_id": "claude-opus-4-7",
  "last_id": "glm-5",
  "has_more": false
}
```

### `GET /health`
运维健康检查（无鉴权）：
```json
{
  "status": "ok",          // ok | degraded | error
  "channels": { "total": 5, "enabled": 5, "oauth": 0, "api": 5 },
  "affinity_bound": 3,
  "device_id": "...",
  "version": "anthropic-proxy"
}
```

---

## Telegram Bot 管理面板

发 `/start` 进入主菜单（2×4 布局）：

```
[📊 状态总览]  [📋 最近日志]
[📈 统计汇总]  [🔀 渠道管理]
[🔐 管理 OAuth] [🔑 管理 API Key]
[⚙ 系统设置]   [❓ 帮助]
```

### 📊 状态总览
运行时长 · 渠道状态 · 今日请求 · ⚡ 最快渠道 Top5 · ⚠ 问题渠道清单 · 📈 配额预警（≥80%）

### 📈 统计汇总（4×4 时间×维度）
- 时间：今天 / 3 天 / 7 天 / 本月
- 维度：汇总（一屏全展）/ 按渠道 / 按模型 / 按 Key
- 汇总视图：Tokens / 请求 / 缓存 / 耗时 / 重试 / 亲和 / 三维 Top3 / 最近未命中 / 最近调用

### 📋 最近日志
15 条最新请求，每条一个 `📄 #N 详情` 按钮点进详情页（完整重试链 + 请求/响应 body）。

### 🔀 渠道管理
- 添加向导（4 步 + 测试面板）：名称 → URL → API Key → 模型列表 → 测试
- 渠道详情：模型状态、健康图标（🟢🟡🟠🔴）、统计、冷却、亲和数
- 编辑：名称 / URL / Key / 模型 / CC 伪装切换
- 测试模型：单/全部；成功 8s / 失败 30s 自动删除进度消息

### 🔐 管理 OAuth
- PKCE 登录（浏览器授权 + 粘贴 code）
- 手动粘贴 OAuth JSON
- 配额查看（5h / 7d / Sonnet 7d / Opus 7d，含 reset 倒计时）
- 刷新 Token / 刷新用量 / 启停 / 删除

### 🔑 管理 API Key
- 列表直接显示完整 Key（单击即复制）
- **每个 Key 可设模型白名单**（多选勾选界面；空 = 无限制）
- 删除二次确认（含 Key 末 8 字符防误删）

### ⚙ 系统设置
- 超时（连接 / 首字 / 空闲 / 总）
- 错误阶梯
- 评分参数（emaAlpha / 窗口 / 失败惩罚 / 探索率）
- 亲和参数（TTL / 打破阈值）
- CCH 模式（disabled / dynamic / static）
- 渠道选择模式（smart / order）
- **📈 配额监控**：开关 / 检查间隔 / 禁用阈值（**默认关闭** —— 频繁拉 `/api/oauth/usage` 可能被风控）
- **🔔 通知设置**：总开关 + 每个事件单独开关
- 首包黑名单（default + byChannel）

**所有设置均热加载，无需重启。**

---

## 📢 通知系统

通过 Telegram 主动告知运维关键事件。所有事件可在「⚙ 系统设置」→「🔔 通知设置」单独开关。

| 事件 | 触发条件 | 默认 |
|---|---|---|
| 🔴 渠道永久冻结 | 某 (渠道,模型) 连续失败到永久冷却（首次）| ✅ |
| ✅ 渠道恢复 | 永久 / 长冷却被清除（手动 / probe / 成功一次）| ✅ |
| ⚠ 配额禁用 | OAuth 任一指标 ≥ 阈值被自动禁用 | ✅ |
| ✅ 配额恢复 | 全部指标 < 阈值且 resets_at 已过，自动启用 | ✅ |
| 🔄 OAuth Token 刷新成功 | 后台主动刷新 / 手动触发 | ✅ |
| ❌ OAuth Token 刷新失败 | refresh_token 失效，标 auth_error | ✅ |
| 🚨 无可用渠道告警 | 请求模型在所有渠道都不可用（503）| ✅ |

**防误报**：
- "永久冻结"只在状态变化时（普通冷却 → 永久）通知一次，不会重复打扰
- "渠道恢复"只在被清除前真正在冷却中（非"成功一次清错误计数"）才通知

**节流**：`无可用渠道告警` 同 model 5 分钟内最多发一次。

**实现细节**：`notifier.notify_event(key, text)` 异步入队（不阻塞调用方），由 worker 线程消费 → TG admin 广播。

---

## 配置文件

`config.json` 是唯一配置来源，运行时自动持久化（tmp + `os.replace` 原子写 + 3 份备份轮转）。

完整字段说明见 `docs/02-config-schema.md`。关键字段速查：

```jsonc
{
  "listen":   { "host": "0.0.0.0", "port": 22122 },
  "apiKeys":  { "default": { "key": "ccp-xxx", "allowedModels": [] } },
  "oauthAccounts": [ { "email": "...", "access_token": "...", "refresh_token": "...", "expired": "...", "enabled": true, "disabled_reason": null, "models": [] } ],
  "channels": [ { "name": "智谱", "type": "api", "baseUrl": "https://...", "apiKey": "...", "models": [{"real": "GLM-5", "alias": "glm-5"}], "cc_mimicry": true, "enabled": true } ],
  "timeouts": { "connect": 10, "firstByte": 30, "idle": 30, "total": 600 },
  "errorWindows": [1, 3, 5, 10, 15, 0],
  "affinity":  { "ttlMinutes": 30, "threshold": 3.0, "cleanupIntervalSeconds": 300 },
  "scoring":   { "emaAlpha": 0.25, "recentWindow": 50, "defaultScore": 3000, "errorPenaltyFactor": 8, "staleMinutes": 15, "staleFullDecayMinutes": 30, "explorationRate": 0.2 },
  "quotaMonitor":  { "enabled": false, "intervalSeconds": 60, "disableThresholdPercent": 95, "resumeThresholdPercent": 95 },
  "notifications": {
    "enabled": true,
    "events": {
      "channel_permanent": true, "channel_recovered": true,
      "quota_disabled": true,    "quota_resumed":   true,
      "oauth_refreshed": true,   "oauth_refresh_failed": true,
      "no_channels":     true
    }
  },
  "cchMode": "disabled",
  "channelSelection": "smart",
  "telegram": { "botToken": "...", "adminIds": [123] },
  "oauth": { "mockMode": false }
}
```

> `quotaMonitor.enabled` **默认关闭** —— 启用后每 N 秒拉一次每个 OAuth 账号的 `/api/oauth/usage`，频繁请求可能被 Anthropic 风控盯上。需要时在「⚙ 系统设置」→「📈 配额监控」打开。

> `notifications.events.<key>` 控制每个事件是否推送给 TG admin，关掉的事件只打到 journal 不发 TG。

**不可热加载字段**（改后需重启）：`listen.host` / `listen.port` / `stateDbPath` / `logDir` / `telegram.botToken` / `telegram.adminIds`。

---

## 运维

所有持久化数据集中在 `<安装目录>/data/`，包括 `config.json` / `state.db` / `logs/` / `.anthropic_proxy_ids.json`。

### 启动 / 停止 / 重启 / 状态（Docker Compose）

```bash
cd <安装目录>
docker compose up -d         # 启动
docker compose stop          # 停止
docker compose restart       # 重启
docker compose ps            # 状态
docker compose down          # 停止 + 删容器（数据保留在 ./data）
```

### 升级到最新镜像

```bash
cd <安装目录>
docker compose pull
docker compose up -d
```

> 也可以重新跑一次一键脚本（选 `Upgrade` 模式），等价。

### 日志

```bash
cd <安装目录>
docker compose logs -f                       # 实时
docker compose logs --tail 100               # 最近 100 条
docker compose logs --since 1h               # 最近 1 小时
```

### 业务日志（请求流水）

按月分库在 `data/logs/YYYY-MM.db`（SQLite）。在 TG Bot「📋 最近日志」查看；或宿主机直接 `sqlite3 <安装目录>/data/logs/2026-04.db`。

### 状态数据

`data/state.db`（SQLite）：performance_stats / channel_errors / cache_affinities / oauth_quota_cache。永久保留，记录渠道性能、冷却、亲和绑定、OAuth 配额缓存。

### 配置备份

每次配置修改都自动轮转 3 份备份（位于 `data/` 目录）：
```
data/config.json
data/config.json.bak.1   (上一版)
data/config.json.bak.2
data/config.json.bak.3   (最老)
```

### 源码 / systemd 部署的运维

如果走「方式四：源码运行」并自己写了 systemd unit，则：

```bash
systemctl start/stop/restart/status anthropic-proxy
journalctl -u anthropic-proxy -f
```

数据文件默认在源码目录下（不设 `ANTHROPIC_PROXY_DATA_DIR` 时回退到 `BASE_DIR`）。

---

## 目录结构

```
AnthropicProxy/
├── README.md                    ← 本文档
├── DESIGN.md                    ← 设计方案总纲
├── docs/                        ← 12 篇分章节设计文档
├── Dockerfile                   ← 多阶段镜像构建
├── docker-compose.yml           ← 默认 compose 模板（GHCR 镜像）
├── .dockerignore
├── deploy.sh                    ← 一键部署脚本（交互式）
├── .github/workflows/
│   └── docker-publish.yml       ← GitHub Actions：push → 构建多架构镜像 → GHCR
├── server.py                    ← FastAPI 入口
├── requirements.txt
├── data/                        ← 运行时持久化（容器挂载点；源码模式不存在）
│   ├── config.json              ← 唯一配置文件
│   ├── state.db                 ← 运行时状态（永久）
│   ├── logs/YYYY-MM.db          ← 按月分库业务日志
│   └── .anthropic_proxy_ids.json ← device_id 持久化
└── src/
    ├── config.py                ← 配置加载/保存/热加载
    ├── auth.py                  ← 下游 API Key 验证
    ├── errors.py                ← Anthropic 标准错误响应
    ├── state_db.py              ← state.db 读写接口
    ├── log_db.py                ← 按月日志库读写 + 跨月聚合
    ├── public_ip.py             ← 启动后台获取外网 IPv4
    ├── fingerprint.py           ← 会话亲和指纹
    ├── affinity.py              ← 亲和表（内存 + 持久化）
    ├── scorer.py                ← 滑动窗口 EMA 评分 + 探索
    ├── cooldown.py              ← 错误阶梯冷却
    ├── scheduler.py             ← 主调度入口
    ├── failover.py              ← 故障转移主循环
    ├── blacklist.py             ← 首包文本黑名单
    ├── probe.py                 ← API 渠道探测
    ├── oauth_manager.py         ← 多 OAuth 账户管理
    ├── upstream.py              ← 上游 httpx client + SSE 工具
    ├── notifier.py              ← 管理员通知（异步队列 + worker）
    ├── transform/
    │   ├── cc_mimicry.py        ← CC 伪装（与 cc-proxy 同源）
    │   └── standard.py          ← 非 CC 伪装的标准转换
    ├── channel/
    │   ├── base.py              ← Channel 抽象基类
    │   ├── oauth_channel.py     ← OAuth 渠道
    │   ├── api_channel.py       ← 第三方 API 渠道
    │   └── registry.py          ← 渠道注册表
    ├── telegram/
    │   ├── bot.py               ← 长轮询主循环
    │   ├── ui.py                ← UI 工具（消息发送/HTML/按钮/通知）
    │   ├── states.py            ← 用户输入状态机
    │   └── menus/
    │       ├── main.py          ← 主菜单
    │       ├── status_menu.py   ← 状态总览
    │       ├── stats_menu.py    ← 统计汇总（cc-proxy 风格一屏全展）
    │       ├── logs_menu.py     ← 最近日志
    │       ├── channel_menu.py  ← 渠道管理（含测试向导）
    │       ├── oauth_menu.py    ← OAuth 账户管理
    │       ├── apikey_menu.py   ← API Key 管理（含模型白名单）
    │       ├── system_menu.py   ← 系统设置
    │       └── help_menu.py     ← 帮助页
    └── tests/
        ├── _isolation.py        ← 测试隔离工具（tmpdir）
        └── test_m*.py           ← 按里程碑组织的集成测试（86 条）
```

---

## 故障排查

### `/health` 显示 `error` 或 `degraded`
- **error**：无启用渠道 → 加至少一个渠道（TG bot「🔀 渠道管理」或「🔐 管理 OAuth」）
- **degraded**：有渠道但全部冷却 → 「🔀 渠道管理」→「🧹 清全部错误」，或等 `errorWindows` 时间过去

### 下游返回 503 `No available channels for model: xxx`
该模型在所有启用渠道里都不存在。检查：
- 模型名拼写（OAuth 是真实名；API 渠道按渠道的 `alias` 匹配）
- 渠道是否被禁用 / 配额禁用 / auth_error

### 下游返回 403 `Model 'xxx' is not allowed for this API key`
该 Key 设了模型白名单但请求模型不在里面。去 TG bot「🔑 管理 API Key」→ 点 Key 名字 →「🎯 编辑允许模型」调整，或清空白名单。

### TG bot 无响应
`journalctl -u anthropic-proxy -n 50` 看最近日志：
- 如看到 `Conflict: terminated by other getUpdates request` → 有多个实例在拉同一 bot，杀掉多余的
- 如看到 `Invalid bot token` → 检查 `config.json` 的 `telegram.botToken`
- 其它错误按 traceback 定位

### OAuth 账户被标 `auth_error`
refresh_token 已失效。在 TG bot「🔐 管理 OAuth」→ 点该账户 →「🔄 刷新 Token」；若还是失败则删除后用「➕ 新增账户」→「🌐 登录获取 Token」走 PKCE 重新登录。

### TG 通知太吵 / 太静
- 太吵：「⚙ 系统设置」→「🔔 通知设置」可关闭单个事件，或一键关总开关
- 太静：检查总开关是否开启；检查具体事件是否被关；查 journal 里是否有 `[notify:<key>:off]` 之类的字样
- 永远收不到："已禁用" 不会推、`adminIds` 没配或填错（注意是 int 不是 string）

### 配额监控该开吗？
默认关闭。**短期建议**：试用阶段先开几天观察，OAuth 账号触达阈值前就被禁用是个好功能；长期跑可以关掉减少 `/api/oauth/usage` 调用频次以避免风控。手动判断额度可以随时点「🔐 管理 OAuth」→ 选某账号 →「📊 刷新用量」。

### 看某次请求为什么失败
「📋 最近日志」→ 找到那条 → 点「📄 #N」进详情 → **重试链**会显示每次尝试的渠道 + outcome + 错误原因。
