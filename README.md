# 🦜 Parrot

[![Docker Image](https://img.shields.io/badge/ghcr.io-parrot-blue?logo=docker)](https://github.com/danger-dream/Parrot/pkgs/container/parrot)
[![Build](https://github.com/danger-dream/Parrot/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/danger-dream/Parrot/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**多家族、多渠道、故障转移的 AI 协议代理**

> 像鹦鹉学舌一样，把下游客户端的请求转发到多个上游，自动挑最快的、故障切到备用。
> 双家族（Anthropic / OpenAI）、三种入口协议、多种上游协议，还能家族内互转。

Parrot 的核心价值：**一个进程管住所有 AI 家族的上游复用**。你手上有一堆 Claude OAuth 账号、ChatGPT Plus OAuth 账号、第三方 GLM / Codex Coding Plan，不想维护 3 套代理 + 3 套统计 + 3 个 TG Bot；Parrot 把它们统一抽象成「渠道」，配上评分调度、故障转移、会话亲和、OAuth 自动刷新、Telegram 图形面板。

---

## 🎯 核心特性

**多家族 · 多入口**

| 入口 | 协议 | 对接客户端 |
|------|------|------|
| `POST /v1/messages` | Anthropic Messages API | Claude Code CLI、OpenClaw、任何 Anthropic SDK |
| `POST /v1/chat/completions` | OpenAI Chat Completions | 大部分 OpenAI SDK / 三方工具 |
| `POST /v1/responses` | OpenAI Responses API | Codex CLI、新版 OpenAI SDK |

**三类上游渠道**

| 渠道 | 类型 | 说明 |
|------|------|------|
| 🅰 Anthropic OAuth | Claude Code 官方账户 | 完整 CC 伪装（指纹 / CCH / 工具名混淆 / cache 断点），与 cc-proxy 同源移植 |
| 🅾 OpenAI OAuth (Codex) | ChatGPT Plus/Pro/Enterprise | 对接 `chatgpt.com/backend-api/codex/responses`，SSE 聚合、rate-limit 头自动解析 |
| 🔀 第三方 API 渠道 | 智谱 / 天翼云 / 京东云 / 讯飞星辰 / 任何 Anthropic 或 OpenAI 兼容服务 | 可开关 CC 伪装；按 `protocol` 决定走哪种请求构造器 |

**家族内互转**：`/v1/chat/completions` 下游请求可以打到 `openai-responses` 上游，反之亦然（SSE 双向状态机 + CapabilityGuard 兜底不兼容字段）。

**运行时保护**

- **四段超时独立**：`connect` / `firstByte` / `idle`（chunk 间）/ `total`（硬上限），任一超时都不会拖死整个请求
- **首包锁**：发任何字节给下游前是"可切换"区；首字节发出后锁渠道，异常转 SSE error 事件收尾
- **故障转移**：按智能排序依次试候选，`upstream_stream_only` 渠道（如 OAuth Codex）对非流式下游自动走 SSE 聚合
- **错误阶梯冷却**：`[1, 3, 5, 10, 15, 0]` 分钟，成功一次清零；OAuth 渠道带宽容次数（`oauthGraceCount: 3`）避免偶发抖动误冷却
- **会话亲和**：指纹 = `hash(api_key | ip | 倒数两条消息)`；30min TTL，缓存命中率显著高于随机调度
- **OAuth 配额监控**：Claude 账户拉 `/api/oauth/usage`；OpenAI 账户解析 Codex `rate-limit` 响应头；阈值自动禁用/恢复
- **评分调度**：滑动窗口 EMA 延迟 + 失败惩罚；带 20% 探索率避免赢家通吃

**Telegram 图形管理面板**

发 `/start` 进主菜单，全图形化配置（文末详述）。

---

## 🚀 快速开始

提供 4 种部署方式，**推荐一键脚本**。

### 方式一：一键脚本（推荐）

```bash
bash <(curl -Ls https://raw.githubusercontent.com/danger-dream/Parrot/main/deploy.sh)
```

脚本会：
1. 显示项目信息 + 检查 / 引导安装 Docker + Docker Compose
2. 交互式收集：安装目录（默认 `/opt/parrot`）/ TG Bot Token / Admin Telegram User ID / 监听端口
3. 生成 `docker-compose.yml` + 最小 `data/config.json`
4. `docker compose pull && up -d`，并验证 `/health` + TG Bot polling

完成后到 Telegram 找你的 bot 发 `/start`，剩下的渠道 / OAuth / API Key 全在图形界面里配。

### 方式二：Docker Compose（手动）

```bash
mkdir -p parrot/data && cd parrot

# 拿 compose 模板
curl -Lo docker-compose.yml https://raw.githubusercontent.com/danger-dream/Parrot/main/docker-compose.yml

# 写最小 config.json（首次启动 server 会自动补全其余默认字段）
cat > data/config.json <<'EOF'
{
  "listen": { "host": "0.0.0.0", "port": 22122 },
  "telegram": {
    "botToken": "<你的 bot token>",
    "adminIds": [<你的 Telegram user id>]
  }
}
EOF

docker compose up -d
docker compose logs -f
```

### 方式三：Docker 直跑（不用 compose）

```bash
mkdir -p ./data
# 先写 ./data/config.json（见方式二）

docker run -d \
  --name parrot \
  --restart unless-stopped \
  -p 22122:22122 \
  -e TZ=Asia/Shanghai \
  -e ANTHROPIC_PROXY_DATA_DIR=/app/data \
  -v "$PWD/data:/app/data" \
  ghcr.io/danger-dream/parrot:latest
```

> `ANTHROPIC_PROXY_DATA_DIR` 是老环境变量名，为向后兼容保留；后续会加 `PARROT_DATA_DIR` 别名。

### 方式四：源码运行（开发用）

```bash
git clone https://github.com/danger-dream/Parrot
cd Parrot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 编辑 config.json（首次启动会自动生成模板）
./venv/bin/python server.py
```

### 下游客户端接入

```bash
# Anthropic 协议入口（Claude 家族）
curl http://<server>:22122/v1/messages \
  -H "x-api-key: ccp-你的Key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [{ "role": "user", "content": "Hello" }]
  }'

# OpenAI Chat 协议入口（GPT 家族）
curl http://<server>:22122/v1/chat/completions \
  -H "Authorization: Bearer ccp-你的Key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4",
    "messages": [{ "role": "user", "content": "Hello" }]
  }'

# OpenAI Responses 协议入口（Codex 原生）
curl http://<server>:22122/v1/responses \
  -H "Authorization: Bearer ccp-你的Key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4",
    "input": [{ "role": "user", "content": "Hello" }]
  }'
```

**官方 SDK 接入**：把 `baseURL` 指向 `http://<server>:22122/v1`，`apiKey` 填 Parrot 生成的下游 Key，即可直接用 `openai` / `anthropic` 官方 Python / Node SDK。

---

## 🏗 架构概览

```
┌────────────────────────────────────────────────────────────────┐
│ 下游客户端（Anthropic SDK / OpenAI SDK / Codex CLI / Claude Code CLI）│
└──────────────┬──────────────┬──────────────┬──────────────────┘
               │              │              │
     POST /v1/messages   /v1/chat/...   /v1/responses
               │              │              │
               ▼              ▼              ▼
  ┌──────────────────────────────────────────────────────────┐
  │               FastAPI 入口 + auth + 日志落盘              │
  └──────────────────────────┬───────────────────────────────┘
                             │ ingress_protocol =
                             │   anthropic | chat | responses
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │ scheduler.schedule                                        │
  │   1. 按 ingress 家族硬过滤（anthropic 家族 ↔ openai 家族）│
  │   2. 筛选 enabled + 非冷却 + 支持模型的渠道              │
  │   3. 会话亲和（fingerprint = key+ip+msg[-2:] 的 hash）   │
  │   4. 评分排序（EMA 延迟 + 失败惩罚 + 20% 探索率）        │
  └──────────────────────────┬───────────────────────────────┘
                             │ candidates: [(channel, model), ...]
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │ failover.run_failover (顺序尝试 + 首包锁)                 │
  │   ingress=anthropic    → AnthropicOAuth / ApiChannel     │
  │   ingress=chat         → OpenAIApiChannel / OpenAIOAuth  │
  │   ingress=responses    → 同上（responses 优先）           │
  │   跨变体时走 chat↔responses 双向 SSE 状态机              │
  │   upstream_stream_only 渠道对非流式请求用 SSE 聚合器兜底 │
  └──────────────────────────┬───────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────────┐
         ▼                   ▼                       ▼
  🅰 Anthropic OAuth    🅾 OpenAI OAuth         🔀 Third-party API
  (Claude Code CC伪装)  (chatgpt.com/codex)     (智谱/天翼云/京东云/讯飞…)
         │                   │                       │
         ▼                   ▼                       ▼
    api.anthropic.com  chatgpt.com/backend-api    third-party endpoints
```

详细设计见 `docs/` 目录（12 篇）和 `docs/openai/` 子目录（10 篇）。

---

## 🌐 HTTP 接口

### `POST /v1/messages`
**完整兼容 Anthropic Messages API**。鉴权通过 `x-api-key` 或 `Authorization: Bearer <key>`。

- 流式（`stream: true`，默认）：SSE
- 非流式：JSON
- 错误：Anthropic 标准错误格式

### `POST /v1/chat/completions`
**完整兼容 OpenAI Chat Completions API**。鉴权通过 `Authorization: Bearer <key>`。

### `POST /v1/responses`
**完整兼容 OpenAI Responses API**。支持 `previous_response_id` 续写（本地 store）、`reasoning.effort`、Codex 工具调用等。

### `GET /v1/models`
返回当前所有启用渠道聚合的可用模型（按 API Key 白名单过滤），Anthropic 标准格式。

### `GET /health`
运维健康检查（无鉴权）：
```json
{
  "status": "ok",          // ok | degraded | error
  "channels": { "total": 13, "enabled": 13, "oauth": 7, "api": 6 },
  "affinity_bound": 64,
  "device_id": "...",
  "version": "parrot"
}
```

---

## 💬 Telegram Bot 管理面板

发 `/start` 进入主菜单（2×4 布局）：

```
[📊 状态总览]   [📋 最近日志]
[📈 统计汇总]   [🔀 渠道管理]
[🔐 管理 OAuth] [🔑 管理 API Key]
[⚙ 系统设置]    [❓ 帮助]
```

### 📊 状态总览（两家族分段）
- 运行时长 · 选路模式 · 亲和绑定数
- 🅰 Anthropic / 🅾 OpenAI 各一行渠道统计（可用 / 冷却 / 永久 / 禁用）
- 今日请求按家族分组展示（成功率 / 首字延迟 / 总耗时 / TPS）
- ⚡ 最快渠道按家族各 Top 5
- 📈 配额预警（≥80%）带家族前缀
- ⚠ 问题渠道清单

### 📈 统计汇总（4×4 时间×维度 + 两家族）
- 时间：今天 / 3 天 / 7 天 / 本月
- 维度：汇总（两家族分段）/ 按渠道 / 按模型 / 按 Key
- **汇总视图**：先 🅰 Anthropic 段（overall + 按渠道 Top3 + 按模型 Top3），后 🅾 OpenAI 段（同上，完整含重试/亲和）；底部跨家族按 Key Top + 最近调用（带家族图标）+ 未命中样本
- **专题视图**：按渠道 / 按模型 Top10，每条前缀 🅰/🅾 家族图标

### 📋 最近日志
15 条最新请求，每条一个 `📄 #N 详情` 按钮点进详情页（完整重试链 + 请求/响应 body）。

### 🔀 渠道管理
添加向导（4 步 + 测试面板）、渠道详情、编辑、测试模型（单/全部）。

### 🔐 管理 OAuth（支持两家族）
- ➕ 新增账户：第一步选 Claude / OpenAI；Claude 支持 PKCE 登录 + 粘贴 JSON；OpenAI 粘贴 refresh_token
- 每条账户显示：状态图标 / 过期时间 / 5h 7d 用量 / 月度统计 / 冷却中的模型
- 详情页：两家族统一布局（提供者 / 计划 / 过期 / 上次刷新 / 使用量 / 月度）
- 操作：刷新 Token / 刷新用量 / 清模型错误 / 清亲和绑定 / 启停 / 删除
- 底部批量：🔄 刷新全部用量 / 🧹 清除所有账户错误（有冷却才显示）

### 🔑 管理 API Key
列表直接显示完整 Key（单击即复制）；每个 Key 可设模型白名单（多选勾选）；删除二次确认。

### ⚙ 系统设置
超时 / 错误阶梯 / 评分参数 / 亲和参数 / CCH 模式 / 渠道选择模式 / 配额监控 / 通知设置 / 首包黑名单。**所有设置均热加载，无需重启。**

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

**节流**：`无可用渠道告警` 同 model 5 分钟内最多发一次。

---

## ⚙ 配置文件

`config.json` 是唯一配置来源，运行时自动持久化（tmp + `os.replace` 原子写 + 3 份备份轮转）。

完整字段说明见 `docs/02-config-schema.md` 和 `docs/openai/02-config-schema.md`。关键字段速查：

```jsonc
{
  "listen":   { "host": "0.0.0.0", "port": 22122 },
  "apiKeys":  { "default": { "key": "ccp-xxx", "allowedModels": [] } },
  "oauthAccounts": [
    { "email": "xxx@example.com", "provider": "claude", "access_token": "...", "refresh_token": "...", "expired": "..." },
    { "email": "yyy@example.com", "provider": "openai", "access_token": "...", "refresh_token": "...", "plan_type": "plus", "chatgpt_account_id": "..." }
  ],
  "channels": [
    { "name": "智谱 Max", "type": "api", "protocol": "anthropic", "baseUrl": "https://...", "apiKey": "...", "models": [{"real": "GLM-5", "alias": "glm-5"}], "cc_mimicry": true, "enabled": true },
    { "name": "OpenAI 3P", "type": "api", "protocol": "openai-responses", "baseUrl": "https://...", "apiKey": "...", "models": [...], "enabled": true }
  ],
  "timeouts":      { "connect": 10, "firstByte": 30, "idle": 120, "total": 600 },
  "errorWindows":  [1, 3, 5, 10, 15, 0],
  "oauthGraceCount": 3,
  "affinity":      { "ttlMinutes": 30, "threshold": 3.0, "cleanupIntervalSeconds": 300 },
  "scoring":       { "emaAlpha": 0.25, "recentWindow": 50, "defaultScore": 3000, "errorPenaltyFactor": 8, "staleMinutes": 15, "staleFullDecayMinutes": 30, "explorationRate": 0.2 },
  "quotaMonitor":  { "enabled": false, "intervalSeconds": 60, "disableThresholdPercent": 95, "resumeThresholdPercent": 95 },
  "accessRefreshThrottleSeconds": 180,
  "providers": {
    "openai": {
      "forceCodexCLI": true,
      "enableTLSFingerprint": false,
      "isolateSessionId": true,
      "defaultModels": ["gpt-5.2", "gpt-5.2-codex", "gpt-5.3-codex", "gpt-5.4"]
    }
  },
  "notifications": { "enabled": true, "events": { ... } },
  "cchMode": "disabled",
  "telegram": { "botToken": "...", "adminIds": [123] }
}
```

> `quotaMonitor.enabled` **默认关闭** —— 启用后每 N 秒拉一次每个 OAuth 账号的 usage（Claude 走 `/api/oauth/usage`，OpenAI 走 Codex 探测头），频繁请求可能被风控盯上。

**不可热加载字段**（改后需重启容器）：`listen.host` / `listen.port` / `stateDbPath` / `logDir` / `telegram.botToken` / `telegram.adminIds`。

---

## 🛠 运维

所有持久化数据集中在 `<安装目录>/data/`：`config.json` / `state.db` / `logs/` / `.anthropic_proxy_ids.json`。

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

> 或重跑一次一键脚本（选 `Upgrade` 模式），等价。

### 日志

```bash
cd <安装目录>
docker compose logs -f                 # 实时
docker compose logs --tail 100         # 最近 100 条
docker compose logs --since 1h         # 最近 1 小时
```

### 业务日志（请求流水）

按月分库在 `data/logs/YYYY-MM.db`（SQLite）。在 TG Bot「📋 最近日志」查看；或宿主机直接 `sqlite3 <安装目录>/data/logs/2026-04.db`。

### 状态数据

`data/state.db`（SQLite）：performance_stats / channel_errors / cache_affinities / oauth_quota_cache / openai_response_store。永久保留。

### 配置备份

每次配置修改都自动轮转 3 份备份（位于 `data/` 目录）：
```
data/config.json
data/config.json.bak.1   (上一版)
data/config.json.bak.2
data/config.json.bak.3   (最老)
```

### 源码 / systemd 部署的运维

如果走「方式四：源码运行」并自己写了 systemd unit，则按该 unit 名管理：

```bash
systemctl start/stop/restart/status <你的unit名>
journalctl -u <你的unit名> -f
```

数据文件默认在源码目录下（不设 `ANTHROPIC_PROXY_DATA_DIR` 时回退到 `BASE_DIR`）。

---

## 📁 目录结构

```
Parrot/
├── README.md                    ← 本文档
├── DESIGN.md                    ← 设计方案总纲
├── docs/                        ← 12 篇 Anthropic 侧设计文档
│   └── openai/                  ← 10 篇 OpenAI 扩展设计文档
├── Dockerfile                   ← 多阶段镜像构建
├── docker-compose.yml           ← 默认 compose 模板（GHCR 镜像）
├── docker-entrypoint.sh         ← root→app 降权入口
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
    ├── errors.py                ← 标准错误响应
    ├── state_db.py              ← state.db 读写
    ├── log_db.py                ← 按月日志库读写 + 跨月聚合（支持 family 过滤）
    ├── public_ip.py
    ├── fingerprint.py           ← 会话亲和指纹（按 Anthropic 标准字段归一化）
    ├── affinity.py
    ├── scorer.py
    ├── cooldown.py              ← OAuth 渠道带 grace count
    ├── scheduler.py             ← 按 ingress 家族过滤 + 亲和 + 评分
    ├── failover.py              ← 故障转移 + upstream_stream_only SSE 聚合
    ├── blacklist.py
    ├── probe.py
    ├── oauth_manager.py         ← 多 OAuth 账户管理（Claude + OpenAI）
    ├── upstream.py              ← httpx client + SSE 工具 + 家族 Builder
    ├── notifier.py
    ├── transform/
    │   ├── cc_mimicry.py        ← Claude CC 伪装（与 cc-proxy 同源）
    │   └── standard.py
    ├── channel/
    │   ├── base.py              ← upstream_stream_only 抽象
    │   ├── oauth_channel.py     ← Anthropic OAuth 渠道
    │   ├── openai_oauth_channel.py ← OpenAI Codex OAuth 渠道
    │   ├── api_channel.py       ← Anthropic 协议第三方 API 渠道
    │   └── registry.py
    ├── oauth/
    │   └── openai.py            ← OpenAI OAuth refresh + 限额头解析
    ├── openai/                  ← OpenAI 协议子树（4700+ 行）
    │   ├── handler.py           ← chat/completions + responses 入口
    │   ├── store.py             ← previous_response_id 本地 store
    │   ├── channel/api_channel.py ← OpenAI 兼容第三方 API 渠道
    │   └── transform/           ← chat↔responses 双向 SSE 状态机 + guard
    └── telegram/
        ├── bot.py
        ├── ui.py                ← 含 family_of / family_tag helpers
        └── menus/
            ├── main.py
            ├── status_menu.py   ← 两家族分段 + 最快渠道 Top 5
            ├── stats_menu.py    ← 家族化汇总 + 专题 + Key 家族拆分
            ├── logs_menu.py
            ├── channel_menu.py
            ├── oauth_menu.py    ← 支持 Claude + OpenAI 双家族管理
            ├── apikey_menu.py
            ├── system_menu.py
            └── help_menu.py
```

---

## 🧪 端到端测试

用官方 `openai` SDK（Python 2.32+ / Node 6.34+）端到端跑的测试矩阵：

| 场景 | Python | Node | 备注 |
|------|--------|------|------|
| chat.completions 非流式 + 逻辑推理 | ✅ | ✅ | 走 SSE 聚合路径 |
| chat.completions 流式 + 编码题 | ✅ | ✅ | 真正 SSE 透传 |
| responses 非流式 + 多轮 function calling | ✅ | ✅ | 2 轮完成 3 个 tool 调用 |
| responses 流式 + reasoning.effort=medium | ✅ | ✅ | 9 种事件类型全齐 |
| messages 流式 + CC 伪装 (Claude) | ✅ | ✅ | 走 cc-proxy 同源伪装 |

测试脚本见本 repo `tests/` 目录。

---

## 🔍 故障排查

### `/health` 显示 `error` 或 `degraded`
- **error**：无启用渠道 → 加至少一个渠道（TG bot「🔀 渠道管理」或「🔐 管理 OAuth」）
- **degraded**：有渠道但全部冷却 → 「🔀 渠道管理」→「🧹 清全部错误」，或 TG bot 的「🔐 管理 OAuth」→「🧹 清除所有账户错误」

### 下游返回 503 `No available channels for model: xxx`
该模型在所有启用渠道里都不存在。检查：
- 模型名拼写
- 渠道是否被禁用 / 配额禁用 / auth_error
- 对 OpenAI OAuth：`gpt-5.2-codex` 对 ChatGPT 账号（Plus/Pro/Enterprise）不支持，会被自动剔除；这种情况返回 404

### 下游返回 403 `Model 'xxx' is not allowed for this API key`
该 Key 设了模型白名单但请求模型不在里面。去 TG bot「🔑 管理 API Key」→ 编辑 Key 的允许模型。

### TG bot 无响应
`docker compose logs --tail 50` 看最近日志：
- `Conflict: terminated by other getUpdates request` → 有多个实例在拉同一 bot
- `Invalid bot token` → 检查 `config.json` 的 `telegram.botToken`

### OAuth 账户被标 `auth_error`
refresh_token 已失效。在 TG bot「🔐 管理 OAuth」→ 点该账户 →「🔄 刷新 Token」；若还是失败则删除后重新添加。

### OpenAI OAuth 请求老是 503 `non-JSON response` ❓
已修复（v0.x 起）。如升级后仍遇到，检查 OAuth 渠道的 `upstream_stream_only` 属性是否为 True（源码部署场景）。

### 查某次请求为什么失败
「📋 最近日志」→ 找到那条 → 点「📄 #N」→ **重试链**会显示每次尝试的渠道 + outcome + 错误原因。

---

## 📜 更名说明

项目原名 `AnthropicProxy`，在支持 OpenAI 之后改名为 **Parrot**（取自「鹦鹉学舌」，贴切于协议代理的本质）。

- 旧仓库 `danger-dream/AnthropicProxy` 已通过 GitHub 自动跳转到本仓库
- 旧镜像 `ghcr.io/danger-dream/anthropicproxy` 暂时与新镜像并存（7-14 天后下线）
- 环境变量 `ANTHROPIC_PROXY_DATA_DIR` 为了向后兼容保持不变；后续会加 `PARROT_DATA_DIR` 别名

---

## 📄 License

MIT — 见 [LICENSE](LICENSE)
