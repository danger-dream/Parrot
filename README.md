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
| `POST /v1/images/generate` | Parrot 图片生成 | 用 Parrot API Key 调 ChatGPT/Codex 图片生成 |
| `POST /v1/images/edit` | Parrot 图片编辑 | 单图修改 / 重绘 / 风格化 |
| `POST /v1/images/generations`、`/v1/images/edits` | OpenAI Images API | 按模型分流到 GPT/Codex 或 xAI Grok Imagine |
| `POST /v1/videos/*`、`GET /v1/videos/{request_id}` | xAI Imagine Videos API | 视频生成、编辑、延长与异步结果查询 |

**三类上游渠道**

| 渠道 | 类型 | 说明 |
|------|------|------|
| 🅰 Anthropic OAuth | Claude Code 官方账户 | 完整 CC 伪装（指纹 / CCH / 工具名混淆 / cache 断点），与 cc-proxy 同源移植 |
| 🅾 OpenAI OAuth (Codex) | ChatGPT Plus/Pro/Enterprise | 对接 `chatgpt.com/backend-api/codex/responses`，SSE 聚合、rate-limit 头自动解析 |
| 🔀 第三方 API 渠道 | 智谱 / 天翼云 / 京东云 / 讯飞星辰 / 任何 Anthropic 或 OpenAI 兼容服务 | 可开关 CC 伪装；按 `protocol` 决定走哪种请求构造器 |

**家族内互转**：`/v1/chat/completions` 下游请求可以打到 `openai-responses` 上游，反之亦然（SSE 双向状态机 + CapabilityGuard 兜底不兼容字段）。

**图片 / 视频生成**：Parrot 简化图片接口仍走 ChatGPT Codex Responses + `image_generation` tool；标准 `/v1/images/generations`、`/v1/images/edits` 则按 `model` 分流，`grok-imagine-image*` 使用现有 xAI OAuth，其余图片模型保持原 GPT/Codex 管线。xAI 视频通过 `/v1/videos/generations|edits|extensions` 创建异步任务，再用 `GET /v1/videos/{request_id}` 查询；查询固定复用创建任务的 OAuth 账号。

**运行时保护**

- **四段超时独立**：`connect` / `firstByte` / `idle`（chunk 间）/ `total`（硬上限），任一超时都不会拖死整个请求
- **首包锁**：发任何字节给下游前是"可切换"区；首字节发出后锁渠道，异常转 SSE error 事件收尾
- **故障转移**：按智能排序依次试候选，`upstream_stream_only` 渠道（如 OAuth Codex）对非流式下游自动走 SSE 聚合
- **错误阶梯冷却**：`[1, 3, 5, 10, 15, 0]` 分钟，成功一次清零；OAuth 渠道带宽容次数（`oauthGraceCount: 3`）避免偶发抖动误冷却；并带两层爆发保护（阶梯推进最小间隔 `cooldownLadderMinIntervalSeconds` + 永久冷却最小累计 `cooldownPermanentMinAgeSeconds`），挡住客户端秒级重试把渠道打穿
- **渠道并发限制**（v0.2.0）：每个渠道可配 `maxConcurrent`，同一时刻只放行这么多个在途请求；其余候选**进 FIFO 排队**直到有位置或 `queueWaitSeconds` 超时（429）；TG 主菜单 overview 实时显示 `在途 / 排队 / 追踪渠道数`
- **会话亲和**：双层指纹 = `hash(api_key | ip | 倒数两条消息)`；session 级 TTL 30min、client 级 soft 回退 TTL 120min（无历史也能命中最近用过的渠道）
- **OAuth 配额监控**：Claude 账户拉 `/api/oauth/usage`；OpenAI 账户解析 Codex `rate-limit` 响应头；阈值自动禁用/恢复
- **评分调度**：滑动窗口 EMA 延迟 + 失败惩罚；带 20% 探索率避免赢家通吃
- **模型映射 & 入口默认模型**：三条入口（anthropic / openai-chat / openai-responses）各自独立维护 `别名 → 真实模型` 表和默认模型；下游客户端发别名、代理改写成真实名再走调度，上游发新模型时**改 TG bot 即生效，无需重启客户端**
- **出站网络设置**：支持在 TG「系统设置 → 网络设置」里配置 DNS 与 SOCKS5。DNS 默认 `8.8.8.8`，首次启动可从系统 DNS 同步一次；DNS 支持普通 IP/域名、DoT（`dot://...`）和 DoH（`https://.../dns-query`），DNS 服务器域名本身用系统 DNS 解析避免套娃。启用 SOCKS5 后所有出站 HTTP 请求走 SOCKS5，代理地址若为域名则使用配置 DNS 解析，保存前会检测并二次确认。内置「网络检测」后台监控，可按间隔检测 DNS / SOCKS5 / 渠道 TCP 连通性 / OpenAI、Claude、Cloudflare 核心上游，并在失败/恢复边沿各通知一次。
- **多媒体日志与媒体缓存**：GPT/Grok 图片及 Grok 视频任务统一写入独立 `image_logs.db`，不污染文本请求日志；视频轮询只更新原任务。开启缓存后，GPT/Grok 图片与已完成的 Grok 视频共用 `images/` 缓存和清理策略，TG 管理员可在「最近日志 → 多媒体日志」查看仍存在的图片或视频。

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
# 需要运行测试时再安装开发依赖
./venv/bin/pip install -r requirements-dev.txt

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

# 图片生成（需要该 API Key 开启 allowImages）
curl http://<server>:22122/v1/images/generate \
  -H "Authorization: Bearer ccp-你的Key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只赛博朋克鹦鹉站在霓虹灯牌前，电影感海报",
    "size": "1024x1024"
  }'

# 图片编辑 / 修改（单图，image 可传 data URL、裸 base64 或 http(s) URL）
curl http://<server>:22122/v1/images/edit \
  -H "Authorization: Bearer ccp-你的Key" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "把画面改成蓝紫色赛博朋克风格，保持主体不变",
    "image": "data:image/png;base64,...",
    "size": "1024x1024"
  }'
```

**官方 SDK 接入**：把 `baseURL` 指向 `http://<server>:22122/v1`，`apiKey` 填 Parrot 生成的下游 Key，即可直接用 `openai` / `anthropic` 官方 Python / Node SDK。图片接口是 Parrot 简化接口，不是 OpenAI 标准 Images API；请直接请求 `/v1/images/generate` / `/v1/images/edit`。

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

### `POST /v1/images/generate`
**Parrot 简化图片生成接口**。鉴权通过 `Authorization: Bearer <key>` 或 `x-api-key`；该 API Key 必须开启 `allowImages`。

请求体：
```json
{
  "prompt": "要生成的图片描述",
  "size": "1024x1024"
}
```

字段说明：
- `prompt`：必填，图片提示词。
- `size`：选填；不传时 Parrot 不会给上游传 `size`，由上游默认决定。Parrot 不做固定白名单，按上游能力处理。

### `POST /v1/images/edit`
**Parrot 简化图片编辑接口**。用于单张图片修改 / 重绘 / 风格化；同样需要 API Key 开启 `allowImages`。

JSON 请求体：
```json
{
  "prompt": "如何修改这张图片",
  "image": "data:image/png;base64,...",
  "size": "1024x1024"
}
```

也支持 `multipart/form-data`：
- `prompt`：必填。
- `image`：上传文件，或字符串形式的 data URL / 裸 base64 / http(s) URL。
- `size`：选填。

当前编辑接口定位是**单图编辑**；多图拼接 / 复杂参考图组合不作为第一版兼容目标。

图片接口成功响应示例：
```json
{
  "id": "...",
  "object": "parrot.image.generate",
  "created": 1777049201,
  "action": "generate",
  "model": "gpt-5.4-mini",
  "image_model": "gpt-image-2",
  "account": "openai-account@example.com",
  "data": [
    {
      "b64_json": "...",
      "revised_prompt": "...",
      "output_format": "png",
      "size": "1024x1024",
      "bytes": 391542
    }
  ],
  "usage": { "input_tokens": 123, "output_tokens": 45 },
  "cached": true,
  "duration_ms": 80692
}
```

注意：
- 返回图片放在 `data[].b64_json`，下游需要自行保存为文件或转发给用户。
- 即使开启缓存，API 响应也不会返回服务器本地缓存路径，避免泄露部署路径。
- 上游已经成功生成图片后，如果本地缓存写入失败，请求仍按成功返回，只是 `cached=false`。

### `GET /v1/models`
返回当前所有启用渠道聚合的可用模型（按 API Key 白名单过滤），Anthropic 标准格式。
**配置了模型映射的别名也会在这里一同列出**（条件：别名所在入口的家族对该 Key 放行，且别名指向的真实模型本身对该 Key 可见），这样下游客户端直接拉列表就能看到最新别名。

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

发 `/start` 进入主菜单：

```
[📈 统计汇总]   [📋 最近日志]
[🔐 管理 OAuth] [🔀 管理渠道]
[🔁 模型映射]   [⚖️ 负载均衡]
[⚙ 系统设置]   [❓ 帮助]
```

### 📈 统计汇总（4×4 时间×维度 + 两家族）
- 时间：今天 / 3 天 / 7 天 / 本月
- 维度：汇总（两家族分段）/ 按渠道 / 按模型 / 按 Key
- **汇总视图**：先 🅰 Anthropic 段（overall + 按渠道 Top3 + 按模型 Top3），后 🅾 OpenAI 段（同上，完整含重试/亲和）；底部跨家族按 Key Top + 最近调用（带家族图标）+ 未命中样本
- **专题视图**：按渠道 / 按模型 Top10，每条前缀 🅰/🅾 家族图标
- **金额统计**：根据 [models.dev](https://models.dev) 的供应商价格目录，按每次真实上游尝试冻结输入 / 输出 / 缓存写入 / 缓存读取 Token 的 USD 金额，并在 Telegram 中所有展示 Token 的请求、账户、渠道、模型和 API Key 位置同步显示；请求数仍描述下游调用，发生 failover 时会另标“上游 N 次”。支持单档长上下文阶梯、OpenAI Priority 与 Anthropic Fast 完整替换价；Anthropic 的容量 `service_tier` 不会覆盖出站 `speed=fast` 证据。`models.json` 只校验规范模型身份，不提供价格；第三方 API 渠道需通过 `pricing.channelProviders` 明确供应商，避免同名模型套错价格。xAI 响应若返回可信的 `cost_in_usd_ticks` 则优先采用真实金额；界面区分实际、估算、混合与未计价。金额固定两位小数且不加约等号；未知/歧义模型、缺失或只有单侧的 partial usage、无法确认旧 Token 口径、Claude 缓存写入 TTL 不可区分、多档 context tier、缺失 Priority/Fast tariff、未知 service tier，或日志缺少独立 reasoning/audio Token 维度时都会 fail-closed 为“未计价”，不会静默按 `$0` 或错误档位处理。models.dev 与 xAI 实际费用均为 USD，不做实时汇率换算。

### 📋 最近日志
页面可在两类日志之间切换：
- `💬 请求日志`：普通文本 / Responses / Chat / WS 请求，详情包含完整重试链和请求/响应 body；
- `🎞 多媒体日志`：统一展示 GPT/Grok 图片生成与编辑、Grok 视频生成/编辑/延长。视频后续轮询只更新同一任务，详情显示进度、最终状态、OAuth 账号、耗时和 xAI 实际费用。

### 🔀 渠道管理
添加向导（4 步 + 测试面板）、渠道详情、编辑、测试模型（单/全部）。

> **Base URL 自适应**（v0.5.0+）：默认填上游域名即可，代理按协议追加 `/v1/messages`、`/v1/chat/completions`、`/v1/responses`。若上游接口挂在非标准路径（如智谱 Coding Plan 的 `https://open.bigmodel.cn/api/coding/paas/v4/chat/completions`），直接把**完整调用路径**贴进来，向导会自动拆分为 `baseUrl + apiPath` 存储，协议不匹配时给出交互式选择（采用识别到的协议 / 坚持当前协议清空路径 / 返回修改）。

### 🔐 管理 OAuth
- ➕ 新增账户：支持 Claude / OpenAI / Grok；各家按现有登录或 refresh_token 流程接入
- 每条账户显示：状态图标 / 过期时间 / 5h 7d 用量 / 月度统计 / 冷却中的模型
- 详情页：三家族统一布局（提供者 / 计划 / 过期 / 上次刷新 / 使用量 / 月度）
- 操作：刷新 Token / 刷新用量 / 清模型错误 / 清亲和绑定 / 启停 / 删除
- 底部批量：🔄 刷新全部用量 / 🧹 清除所有账户错误（有冷却才显示）
- 账户设置中的媒体入口明确拆分：
  - 「🖼 GPT 图片设置」管理 GPT/Codex 图片模型、缓存和图片专用账号禁用列表；
  - 「🎨 Grok Imagine」管理 xAI 图片/视频模型、视频任务绑定时长和媒体请求超时，并显示图片模型路由边界；
  - 两个设置页都可跳转到统一的「🎞 多媒体日志」。

### 🔑 API Key
发送 `/keys` 管理下游代理 Key。列表直接显示完整 Key（单击即复制）；每个 Key 可设模型白名单（多选勾选，包含已配置的 Grok 图片/视频模型）；删除二次确认。图片权限由 `allowImages` 单独控制，视频权限由 `allowVideos` 单独控制，二者默认关闭，可在 Key 详情页分别点击「🖼 允许图片接口」和「🎬 允许视频接口」。

API Key 还支持启用/停用与单 Key 请求限流：全局默认在「⚙ 系统设置 → 🔑 API Key 限流」里设置，默认单 Key 5 并发、50 队列、最长等待 30 分钟；单个 Key 可在详情页「🚦 请求限流」覆盖 `enabled / maxConcurrent / maxQueue / queueWaitSeconds`，其中 `limits.enabled` 优先级高于全局开关。

### 🖼 GPT / Codex 图片设置
从「🔐 管理 OAuth → ⚙️ 账户设置」进入，用于管理 GPT/Codex 图片接口的运行参数：
- 功能开关、主模型 `mainModel`、图片工具模型 `toolModel`；
- 图片专用账号禁用列表，只影响图片生成 / 编辑，不影响普通 OpenAI OAuth 文本请求；
- 图片缓存开关、缓存路径、保留天数、缓存空间上限；
- 统计、账号排行、最近任务和缓存图片查看统一移到「🎞 多媒体日志」。

### 🎨 Grok Imagine 设置
从「🔐 管理 OAuth → ⚙️ 账户设置」进入：
- 编辑 `xaiOAuth.imageModels` 与 `xaiOAuth.videoModels`；
- 编辑视频任务绑定时长 `videoJobTtlSeconds` 与媒体请求超时 `mediaRequestTimeoutSeconds`；
- 页面明确展示：配置的 `grok-imagine-image*` 走 xAI OAuth，其他图片模型继续走 GPT/Codex；
- API Key 详情页分别控制图片、视频权限，模型白名单页用 🖼 / 🎬 标出对应媒体模型；
- 页面可直接进入「🎞 多媒体日志」，查看 GPT/Grok 统一统计、费用和任务状态。

### ⚖️ 负载均衡
- `smart` 智能调度：按滑动窗口评分 + 探索率排序
- `order` 顺序调度：按 config 渠道定义顺序依次尝试
- `priority` 优先级调度：按用户在 TG 菜单保存的 Anthropic / OpenAI 协议队列依次尝试
- 亲和优先于负载均衡：绑定目标仍可用就继续用；不可用时才由当前算法选接班渠道。

### 🔁 模型映射 & 默认模型

三条入口（anthropic / openai-chat / openai-responses）各自独立维护：
- **默认模型**：body 缺失 `model` 字段时的兜底；点「✏ 设置默认」直接弹真实模型按钮列表选一下即可
- **别名映射**：下游传别名 → 代理改写成真实模型名再走调度（只解一层，无递归）
- **新增映射**：先输入别名，再从真实模型按钮列表里选（家族过滤 + 分页 10/页）；校验不能与真实模型重名、不能与已有别名冲突
- **条目详情页**：点任一已有映射可进入详情页，支持「🏷 修改别名 / 🎯 修改真实 / 🗑 删除」三种操作，修改别名用原子替换保证中间状态不影响线上流量

白名单校验和 `/v1/models` 列表**采用映射后的真实名**——API Key 只需授权真模型即可，别名自动跟随。

### ⚙ 系统设置
超时 / 错误阶梯（含阶梯推进最小间隔、永久最小累计两项爆发保护）/ 评分参数 / 亲和参数 / CCH 模式 / 配额监控 / 通知设置 / 首包黑名单 / ⚡ 渠道并发限制。**所有设置均热加载，无需重启。**

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
  "apiKeys":  { "default": { "key": "ccp-xxx", "allowedModels": [], "allowImages": false, "allowVideos": false } },
  "oauthAccounts": [
    { "email": "xxx@example.com", "provider": "claude", "access_token": "...", "refresh_token": "...", "expired": "..." },
    { "email": "yyy@example.com", "provider": "openai", "access_token": "...", "refresh_token": "...", "plan_type": "plus", "chatgpt_account_id": "..." }
  ],
  "channels": [
    { "name": "智谱 Max", "type": "api", "protocol": "anthropic", "baseUrl": "https://...", "apiKey": "...", "models": [{"real": "GLM-5", "alias": "glm-5"}], "cc_mimicry": true, "enabled": true },
    { "name": "OpenAI 3P", "type": "api", "protocol": "openai-responses", "baseUrl": "https://...", "apiKey": "...", "models": [...], "enabled": true },
    // 非标准路径上游：baseUrl 只留主机，apiPath 放完整调用路径，运行时 = baseUrl + apiPath
    { "name": "智谱 Coding", "type": "api", "protocol": "openai-chat", "baseUrl": "https://open.bigmodel.cn", "apiPath": "/api/coding/paas/v4/chat/completions", "apiKey": "...", "models": [...], "enabled": true }
  ],
  "images": {
    "enabled": true,
    "mainModel": "gpt-5.4-mini",
    "toolModel": "gpt-image-2",
    "disabledAccounts": [],
    "cacheEnabled": false,
    "cachePath": "images",
    "cacheRetentionDays": 0,
    "cacheMaxBytes": 1073741824,
    "accountCooldownSeconds": 300,
    "requestTimeoutSeconds": 180,
    "maxPromptChars": 4000,
    "maxInputImageBytes": 20971520,
    "dbPath": "image_logs.db"
  },
  "timeouts":      { "connect": 10, "firstByte": 30, "idle": 120, "total": 600 },
  "apiKeyConcurrency": {
    "enabled": true,
    "defaultMaxConcurrent": 5,
    "defaultMaxQueue": 50,
    "defaultQueueWaitSeconds": 1800,
    "defaultMaxRequestBodyBytes": 8388608,
    "defaultMaxRequestBodyEvents": 4096,
    "defaultMaxQueuedBodyBytesPerKey": 33554432,
    "maxQueuedBodyBytes": 134217728,
    "queuedBodySpoolThresholdBytes": 1048576,
    "defaultMaxQueuedBodySpoolBytesPerKey": 536870912,
    "maxQueuedBodySpoolBytes": 2147483648
  },
  "concurrency":   { "enabled": true, "queueWaitSeconds": 30, "defaultMaxConcurrent": 0 },
  "errorWindows":  [1, 3, 5, 10, 15, 0],
  "oauthGraceCount": 3,
  "cooldownLadderMinIntervalSeconds": 30,     // 阶梯推进最小间隔；防秒级重试把渠道打穿
  "cooldownPermanentMinAgeSeconds": 300,      // 永久冷却最小累计；防爆发式失败误判为永久
  "affinity":      { "ttlMinutes": 30, "cleanupIntervalSeconds": 300, "clientTtlMinutes": 120 },
  "scoring":       { "emaAlpha": 0.25, "recentWindow": 50, "defaultScore": 3000, "errorPenaltyFactor": 8, "staleMinutes": 15, "staleFullDecayMinutes": 30, "explorationRate": 0.2 },
  "quotaMonitor":  { "enabled": false, "intervalSeconds": 60, "disableThresholdPercent": 95, "resumeThresholdPercent": 95 },
  "accessRefreshThrottleSeconds": 180,
  "modelMapping": {
    "anthropic":        { "claude-new-alias": "claude-sonnet-4-6" },
    "openai-chat":      { "gpt-5.5": "gpt-5.4" },
    "openai-responses": { "gpt-5.5-codex": "gpt-5.4" }
  },
  "ingressDefaultModel": {
    "anthropic":        "claude-sonnet-4-6",
    "openai-chat":      "gpt-5.4",
    "openai-responses": "gpt-5.4"
  },
  "providers": {
    "openai": {
      "forceCodexCLI": true,
      "enableTLSFingerprint": false,
      "isolateSessionId": true,
      "defaultModels": ["gpt-5.2", "gpt-5.2-codex", "gpt-5.3-codex", "gpt-5.4", "gpt-5.5"]
    }
  },
  "notifications": { "enabled": true, "events": { ... } },
  "channelSelection": "smart",
  "loadBalancing": { "initialized": false, "priorityOrders": { "anthropic": [], "openai": [] } },
  "cchMode": "disabled",
  "telegram": { "botToken": "...", "adminIds": [123] }
}
```

> `quotaMonitor.enabled` **默认关闭** —— 启用后每 N 秒拉一次每个 OAuth 账号的 usage（Claude 走 `/api/oauth/usage`，OpenAI 走 Codex 探测头），频繁请求可能被风控盯上。

> `apiKeys.*.allowImages` **默认关闭** —— 新建或历史 API Key 不会自动获得图片生成 / 编辑能力，必须在 TG「🔑 管理 API Key」里显式开启。

> `apiKeys.*.allowVideos` **默认关闭** —— 视频费用较高，需在 TG「🔑 管理 API Key」里为获准的下游 Key 单独开启，并可配合 `allowedModels` 限定视频模型。

> **旧版本升级无需手工迁移：**启动时会保留既有 API Key、`allowedModels`、`allowImages`、OAuth 账号及全部 token，只为缺失字段补上 Imagine 默认配置，并将历史 Key 的 `allowVideos` 设为 `false`。`state.db` 会幂等新增 `xai_video_jobs`；既有 `image_logs.db` 会原地补充多媒体字段，历史 GPT 图片记录自动按 `provider=openai`、`media_type=image` 解释，不删除、不改名、不清空。若要开放视频，升级后再按 Key 显式开启即可。

> API Key 请求排队时，小于 `queuedBodySpoolThresholdBytes` 的待回放 body 保留在内存；超过阈值后自动转入数据目录下固定的 `queued-body-spool/` 私有临时目录。内存预算沿用 `defaultMaxQueuedBodyBytesPerKey` / `maxQueuedBodyBytes`；磁盘临时数据另由 `defaultMaxQueuedBodySpoolBytesPerKey` / `maxQueuedBodySpoolBytes` 约束，成功、失败、取消或断开后都会关闭并删除。

> `images.cacheRetentionDays=0` 表示不按时间清理；`images.cacheMaxBytes=0` 表示不按空间清理。相对 `cachePath` 会落在数据目录下，Parrot 会阻止相对路径逃逸。

**不可热加载字段**（改后需重启容器）：`listen.host` / `listen.port` / `stateDbPath` / `openai.store.dbPath` / `logDir` / `telegram.botToken` / `telegram.adminIds`。

---

## 🛠 运维

所有持久化数据集中在 `<安装目录>/data/`：`config.json` / `state.db` / `openai_response_store.db` / `image_logs.db` / `logs/` / `images/` / `.anthropic_proxy_ids.json`。

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

GPT/Grok 图片及 Grok 视频任务使用独立日志库 `data/image_logs.db`；历史主调用表原地扩展为统一多媒体任务日志，GPT 图片账号尝试表继续保留。视频创建记一条 `pending`，客户端轮询只更新该记录直至 `success / failed / expired`，不会污染普通文本请求日志。

### 多媒体缓存

缓存默认关闭。开启后，GPT/Grok 图片以及轮询完成的 Grok 视频会保存到 `data/images/`（或 `images.cachePath` 指定的位置），并统一按 `images.cacheRetentionDays` 和 `images.cacheMaxBytes` 自动清理。缓存路径只在服务端内部使用，API 响应不会暴露本地文件路径；管理员可在 TG「📋 最近日志 → 🎞 多媒体日志」的任务详情中查看仍存在的缓存图片或视频。

### 状态数据

`data/state.db`（SQLite）：performance_stats / channel_errors / cache_affinities / oauth_quota_cache 等轻量运行时状态，永久保留。

`data/openai_response_store.db`（SQLite）：`previous_response_id` history 表
`openai_response_store`。升级自旧版本时不在线迁移旧表；新库 miss 会只读回退
`state.db`，让旧 id 在原 TTL 内继续可用。

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
│   ├── openai_response_store.db ← previous_response_id history（TTL）
│   ├── image_logs.db            ← 图片/视频统一多媒体任务日志（兼容旧图片历史）
│   ├── logs/YYYY-MM.db          ← 按月分库业务日志
│   ├── images/                  ← 图片/视频缓存（开启后，保留兼容目录名）
│   └── .anthropic_proxy_ids.json ← device_id 持久化
└── src/
    ├── config.py                ← 配置加载/保存/热加载
    ├── auth.py                  ← 下游 API Key 验证
    ├── errors.py                ← 标准错误响应
    ├── state_db.py              ← state.db 读写
    ├── channel_state.py         ← 运行期渠道改名的配置/DB/内存原子协调
    ├── sqlite_errors.py         ← SQLite 可用性错误精确分类
    ├── log_db.py                ← 按月日志库读写 + 跨月聚合（支持 family 过滤）
    ├── image_db.py              ← 兼容旧库的多媒体日志底层 + GPT 图片尝试统计
    ├── media_db.py              ← 统一图片/视频日志门面
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
    │   ├── images_simple.py     ← Parrot 简化图片生成/编辑接口
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
            ├── oauth_menu.py    ← 支持 Claude + OpenAI + Grok OAuth 管理
            ├── apikey_menu.py   ← API Key 模型 / 图片 / 视频权限
            ├── image_menu.py    ← GPT/Codex 图片配置 / 缓存设置
            ├── xai_imagine_menu.py ← Grok Imagine 图片 / 视频设置
            ├── media_logs_menu.py   ← GPT/Grok 统一多媒体日志
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
| images/generate 真实生成 | ✅ | - | 走 ChatGPT/Codex image_generation tool |
| images/edit 单图编辑 | ✅ | - | data URL 输入图 + 图片缓存 |

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

### 图片接口返回 403 `Image generation is not allowed for this API key`
该 API Key 没有开启图片权限。去 TG bot「🔑 管理 API Key」→ 点进该 Key →「🖼 允许图片接口」。

### 图片接口返回 503 `no available OpenAI OAuth account for images`
没有可用于图片生成的 OpenAI OAuth 账号。检查：
- 是否至少添加了一个 OpenAI OAuth 账号，且账号有 `chatgpt_account_id`。
- 账号是否被停用、认证失败、配额禁用，或在「🖼 图片生成」里被图片模块单独禁用。
- 账号是否处于图片模块独立冷却中。

### 图片或视频生成成功但 TG 看不到查看按钮
「📋 最近日志 → 🎞 多媒体日志 → 任务详情」中的查看按钮只在以下条件同时满足时显示：
- 多媒体缓存已开启；
- 图片调用成功，或 Grok 视频已轮询到完成状态；
- `image_logs.db` 里记录了缓存路径；
- 本地缓存文件还没有被保留天数 / 空间上限清理掉。

### TG bot 无响应
`docker compose logs --tail 50` 看最近日志：
- `Conflict: terminated by other getUpdates request` → 有多个实例在拉同一 bot
- `Invalid bot token` → 检查 `config.json` 的 `telegram.botToken`

### OAuth 账户被标 `auth_error`
refresh_token 已失效。在 TG bot「🔐 管理 OAuth」→ 点该账户 →「🔄 刷新 Token」；若还是失败则删除后重新添加。

### OpenAI OAuth 请求老是 503 `non-JSON response` ❓
已修复（v0.x 起）。如升级后仍遇到，检查 OAuth 渠道的 `upstream_stream_only` 属性是否为 True（源码部署场景）。

### 查某次请求为什么失败
- 普通请求：「📋 最近日志 → 💬 请求日志」→ 点「📄 #N」，重试链会显示每次渠道尝试和错误原因；
- 图片 / 视频：「📋 最近日志 → 🎞 多媒体日志」→ 点「📄 #N」，查看模型、OAuth 账号、任务进度、耗时、费用和最终错误。

---

## 📜 更名说明

项目原名 `AnthropicProxy`，在支持 OpenAI 之后改名为 **Parrot**（取自「鹦鹉学舌」，贴切于协议代理的本质）。

- 旧仓库 `danger-dream/AnthropicProxy` 已通过 GitHub 自动跳转到本仓库
- 旧镜像 `ghcr.io/danger-dream/anthropicproxy` 暂时与新镜像并存（7-14 天后下线）
- 环境变量 `ANTHROPIC_PROXY_DATA_DIR` 为了向后兼容保持不变；后续会加 `PARROT_DATA_DIR` 别名

---

## 📄 License

MIT — 见 [LICENSE](LICENSE)
