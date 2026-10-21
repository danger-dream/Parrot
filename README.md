# 🦜 Parrot

[![Docker Image](https://img.shields.io/badge/ghcr.io-parrot-blue?logo=docker)](https://github.com/danger-dream/Parrot/pkgs/container/parrot)
[![Build](https://github.com/danger-dream/Parrot/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/danger-dream/Parrot/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**多家族、多渠道、故障转移的 AI 协议代理**

> 像鹦鹉学舌一样，把下游客户端的请求转发到多个上游，自动挑最快的、故障切到备用。
> 双家族（Anthropic / OpenAI）、三种入口协议、多种上游协议，还能家族内互转。

Parrot 的核心价值：**一个进程管住所有 AI 家族的上游复用**。你手上有一堆 Claude OAuth 账号、ChatGPT Plus OAuth 账号、Cursor 订阅账号、第三方 GLM / Codex Coding Plan，不想维护多套代理、统计和 TG Bot；Parrot 把它们统一抽象成「渠道」，配上评分调度、故障转移、会话亲和、OAuth 自动刷新、Telegram 图形面板。

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
| `POST /mcp` | MCP (Streamable HTTP) | 任意 MCP 客户端：搜索、图片、视频工具 |

**三类上游渠道**

| 渠道 | 类型 | 说明 |
|------|------|------|
| 🅰 Anthropic OAuth | Claude Code 官方账户 | 完整 CC 伪装（指纹 / CCH / 工具名混淆 / cache 断点），与 cc-proxy 同源移植 |
| 🅾 OpenAI OAuth (Codex) | ChatGPT Plus/Pro/Enterprise | 对接 `chatgpt.com/backend-api/codex/responses`，SSE 聚合、rate-limit 头自动解析 |
| 🖱️ Cursor OAuth | Cursor 个人/团队订阅 | 浏览器 PKCE 登录，自动同步真实邮箱/姓名、套餐额度、canonical 模型与 200+ 原生变体；通过私有 AgentService bridge 同时服务 Chat / Responses / Anthropic 入口 |
| 🧩 Qoder OAuth | Qoder 国际（qoder.com）/ Qoder CN（qoder.cn） | 桌面客户端设备授权（国际区另支持 PAT）；按客户端 COSY 签名与报文编码调用其 agent 接口，同时服务 Chat / Responses / Anthropic 入口；同步账号模型目录、积分用量，可开启每日积分自动领取（国际区使用账户绑定的设备档案；无需原生 `runtime-info`） |
| 📡 第三方 API 渠道 | 智谱 / 天翼云 / 京东云 / 讯飞星辰 / 任何 Anthropic 或 OpenAI 兼容服务 | 可开关 CC 伪装；按 `protocol` 决定走哪种请求构造器 |

**家族内互转**：`/v1/chat/completions` 下游请求可以打到 `openai-responses` 上游，反之亦然（SSE 双向状态机 + CapabilityGuard 兜底不兼容字段）。

**图片 / 视频生成**：Parrot 简化图片接口仍走 ChatGPT Codex Responses + `image_generation` tool；标准 `/v1/images/generations`、`/v1/images/edits` 按 `model` 统一分流到 GPT/Codex、xAI Grok Imagine、Antigravity Gemini Image 或已配置的 OpenAI 兼容 API 渠道。Antigravity 编辑会把参考图与 mask 一并发送给 Gemini，并在本地再次执行遮罩边界保护。xAI 视频通过 `/v1/videos/generations|edits|extensions` 创建异步任务，再用 `GET /v1/videos/{request_id}` 查询；查询固定复用创建任务的 OAuth 账号。

**运行时保护**

- **四段超时独立**：`connect` / `firstByte` / `idle`（chunk 间）/ `total`（硬上限），任一超时都不会拖死整个请求
- **首包锁**：发任何字节给下游前是"可切换"区；首字节发出后锁渠道，异常转 SSE error 事件收尾
- **故障转移**：按智能排序依次试候选，`upstream_stream_only` 渠道（如 OAuth Codex）对非流式下游自动走 SSE 聚合
- **错误阶梯冷却**：`[1, 3, 5, 10, 15, 0]` 分钟，成功一次清零；OAuth 渠道带宽容次数（`oauthGraceCount: 3`）避免偶发抖动误冷却；并带两层爆发保护（阶梯推进最小间隔 `cooldownLadderMinIntervalSeconds` + 永久冷却最小累计 `cooldownPermanentMinAgeSeconds`），挡住客户端秒级重试把渠道打穿
- **渠道并发限制**（v0.2.0）：每个渠道可配 `maxConcurrent`，同一时刻只放行这么多个在途请求；其余候选**进 FIFO 排队**直到有位置或 `queueWaitSeconds` 超时（429）；TG 主菜单 overview 实时显示 `在途 / 排队 / 追踪渠道数`
- **会话亲和**：双层指纹 = `hash(api_key | ip | 倒数两条消息)`；session 级 TTL 30min、client 级 soft 回退 TTL 120min（无历史也能命中最近用过的渠道）
- **OAuth 配额监控**：Claude 账户拉 `/api/oauth/usage`；OpenAI 账户解析 Codex `rate-limit` 响应头；Cursor 拉 DashboardService 套餐/额度并区分 Cursor Models 与 Other Models 两个池，按池冷却/恢复对应模型，不粗暴禁用整个账号
- **Cursor 实时用量**：每条请求独立使用 AgentService 返回并由 Parrot 实时记录的 input/output/cache/cost；上游未提供缓存拆分时如实记录为 0，不以延迟 Dashboard event 覆盖
- **评分调度**：滑动窗口 EMA 延迟 + 失败惩罚；带 20% 探索率避免赢家通吃
- **模型映射 & 入口默认模型**：三条入口（anthropic / openai-chat / openai-responses）各自独立维护 `别名 → 真实模型` 表和默认模型；下游客户端发别名、代理改写成真实名再走调度，上游发新模型时**改 TG bot 即生效，无需重启客户端**
- **出站网络设置**：支持在 TG「系统设置 → 网络设置」里配置 DNS 与 SOCKS5。DNS 默认 `8.8.8.8`，首次启动可从系统 DNS 同步一次；DNS 支持普通 IP/域名、DoT（`dot://...`）和 DoH（`https://.../dns-query`），DNS 服务器域名本身用系统 DNS 解析避免套娃。启用 SOCKS5 后所有出站 HTTP 请求走 SOCKS5，代理地址若为域名则使用配置 DNS 解析，保存前会检测并二次确认。内置「网络检测」后台监控，可按间隔检测 DNS / SOCKS5 / 渠道 TCP 连通性 / OpenAI、Claude、Cloudflare 核心上游，并在失败/恢复边沿各通知一次。
- **多媒体日志与媒体缓存**：GPT/Grok/Antigravity 图片及 Grok 视频任务统一写入独立 `image_logs.db`，不污染文本请求日志；视频轮询只更新原任务。开启缓存后，各图片来源与已完成的 Grok 视频共用 `images/` 缓存和清理策略，TG 管理员可在「最近日志 → 多媒体日志」查看仍存在的图片或视频。
- **MCP 服务**：`POST /mcp` 对外提供搜索与媒体工具，供任意 MCP 客户端接入。工具与 HTTP 入口共用同一套实现、同一套开关（`mcp.enabled` / `mcp.tools` / `apiKeys.<name>.allowMcp` / `mcpTools`）和同一份资源 URL 生成逻辑，因此权限、渠道选择、日志与缓存完全一致。工具说明中的可用来源在每次 `tools/list` 时按当前配置实时计算，上游增删来源后模型立即看到最新列表。`web_search` 的 Grok 原生 X Search 默认使用虚拟来源 `x-twitter`；若升级前已有同名真实搜索后端，该后端继续保持普通 Web Search 语义，虚拟来源会自动改用 schema 实时枚举中的兼容 ID（当前为 `x:twitter`），不会抢占既有配置。调用事实独立记录在 `mcp_call_log`（含未触达上游的拒绝），工具返回的内容另存 `mcp_call_detail`（与摘要分表，受「数据留存 → 保存完整请求」控制）。TG 管理员可在「系统设置 → MCP 服务」开关服务、查看接入配置与调用统计，并在调用日志里逐条查看**搜了什么、用的哪个引擎、结果是什么**；工具与访问 Key 在「API Key 管理」按 Key 配置。

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
# 测试必须经隔离入口启动；用系统 python3 启动同一脚本也会自动切换到此 venv
# 默认最多 4 个进程（不超过 CPU affinity）、worksteal 调度、显示最慢 10 项
./venv/bin/python src/tests/isolated_pytest.py src/tests -q
# 排查顺序/共享状态问题，或只跑少量用例时，可显式串行
./venv/bin/python src/tests/isolated_pytest.py src/tests/test_isolation_guard.py -q -n 0

# 编辑 config.json（首次启动会生成基础配置；OpenAI OAuth 默认跟随打包的当前 Codex profile）
./venv/bin/python server.py
```

测试入口尊重显式 `-n` / `--numprocesses`、`--dist`、`--durations`（含 `PYTEST_ADDOPTS`），不会改写用户指定值；未安装 `pytest-xdist` 时会提示并退回串行。每个并行进程仍使用独立测试数据目录并禁止真实外网请求。日常修改先跑受影响测试，最终验收及发布保留全量与跨 Python 版本回归。

### 固定发布入口

发布统一使用 `scripts/release.py start <版本> --notes <说明文件> --files <已审阅文件...>`，失败后使用 `resume <运行ID>` 续跑。脚本固定执行候选检查、并发回归与双架构候选构建、重启授权与验证、推送和发布验收；源码未变时复用已通过结果，源码变化后自动失效。回归只保留本机实际 Python 和 GitHub 干净 CI 各一份，不再额外运行本机 CI 容器；候选构建并行上传GHCR并记录不可变digest，缓存跨候选分支复用；正式工作流仅核验并发布多架构manifest，不重复测试、构建或搬运完整镜像。

本机发布默认最多 8 worker（不超过可用核数，可用 `--workers` 固定），CI 使用实际可用核数；日常测试的默认并行策略不变。重启及失败 Tag 覆盖仍需用户授权。完整入口、错误报告、授权和恢复方法见 [固定发布说明](docs/releasing.md)。`rehearse` 可做不改正式版本、不重启、不更新正式标签或Release的真实验收，并输出阶段耗时；它只用于一次性验证发布工具，不是日常发布前置步骤。

### Codex 协议 profile（OpenAI OAuth）

Codex 的版本、User-Agent、端点、WS beta、模型能力和请求字段策略都来自 `src/openai/codex_profiles/`。Python 源码不保存会漂移的 CLI 版本或模型名单。`src/openai/codex_profiles/current.json` 是发布包的当前 profile 指针；默认 `codexProfileAutoUpdate=true`，升级 Parrot 后首次加载配置会自动把真实 `config.json` 的 `codexCliVersion` / `codexProtocolProfile` 迁移为该 profile 的配套值，既有 OAuth installation UUID 不会旋转。

```json
{
  "openaiOAuth": {
    "codexProfileAutoUpdate": true,
    "codexCliVersion": "0.159.0",
    "codexProtocolProfile": "rust-v0.159.0"
  }
}
```

需要固定已审核旧版时，将 `codexProfileAutoUpdate` 显式设为 `false`，并同时配置完全匹配的 `codexCliVersion` 和 `codexProtocolProfile`（例如 `0.153.4` / `rust-v0.153.4`，旧档案保持不变）。缺失、空值、非法 SemVer、未知 profile 或版本不匹配时，Codex 模型目录、OAuth identity、HTTP 与 WebSocket 请求都会 fail closed。

当前选择正式发布 tag `rust-v0.159.0`。默认自动跟随的现有安装更新并重启后会迁移版本/profile，旧版本模型缓存立即重新同步；无需手动改配置或添加 `gpt-6.1-sol`。实际模型权限、Lite 和思考档位仍以认证账户目录为准，不用内置名单授予权限。`service_tier="ultrafast"` 已支持透传及显示，但须账户模型目录提供该档位；它不同于 `reasoning.effort="ultra"`，也不会由 Fast（priority）开关自动启用。来源哈希、离线复核方式及保留旧模型基线见 [Codex 发布档案](docs/codex-release-profile.md)。

模型策略按“账户认证 `/models` 的显式字段 → 选中 profile 的同名模型记录”解析。两处都没有 `useResponsesLite` 时拒绝该 Codex 模型请求；`ultra` 依据有效模型档位与 `multiAgentReasoningEffort` 映射，最新 profile 按官方算法允许缺省时回退到 `max` / 最后一个非 ultra 档位，旧 pin 保留严格映射边界。目录中的 `defaultReasoningEffort` / `defaultVerbosity` 优先于 profile，且只补调用方未提供的字段。模型 profile 的基础指令优先于用户可选的 `defaultInstructions`；下游显式 instructions、已成形的官方 Lite prefix 和 WebSocket incremental continuation 保持权威。profile 标记为 unsupported 的输出、采样或缓存参数会在发网前剥掉，不会把请求打回客户端；缺失策略仍 fail closed。

模型目录请求携带同一 profile 的版本身份，并使用 ETag / 304；推理响应中的 `X-Models-Etag` 会触发去抖后的目录刷新，`openai-model` / `x-openai-model` 会作为实际模型观测头透传。

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

**官方 SDK 接入**：把 `baseURL` 指向 `http://<server>:22122/v1`，`apiKey` 填 Parrot 生成的下游 Key，即可直接用 `openai` / `anthropic` 官方 Python / Node SDK。图片同时提供 Parrot 简化接口 `/v1/images/generate`、`/v1/images/edit` 和 OpenAI 兼容接口 `/v1/images/generations`、`/v1/images/edits`；后者按模型选择 GPT/Codex、xAI、Antigravity 或 API 渠道。

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
  🅰 Anthropic OAuth    🅾 OpenAI OAuth         📡 Third-party API
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

### 统一模型名（`modelRegistry.enabled`）

开启后，同一个模型不论来自哪个渠道、上游怎么写，对下游只有一个小写名字，所有来源按负载均衡的顺序和策略分流，发给每个来源的仍是它自己的 ID。

- **同一模型合并**：`MiniMax-M3.1-Flash`（MiniMax API）、`minimax-m3.1-flash`（WorkBuddy）、Qoder 的 `mmodel` 都是 `minimax-m3.1-flash`；Cursor 的 `glm-5p3`、Qoder 的 `gmodel` 并入 `glm-5.3`。依据只有官方 ID、官方显示名和 models.dev 目录，不按名字相似猜；判断不了的保持原名。
- **任意写法都能调用**：大小写、版本号写法（`4.6`/`4-6`）不同视为同一个；旧对外名（如 `qoder-glm-5.3`、`kimi-k3-1`、`primary-model`）保留为别名，不再列出。
- **名字只表示模型**：思考强度、思考开关走请求参数；`gemini-4-argon-high`、`claude-opus-5-5-low`、`-thinking`、`-nothinking` 等后缀作为快捷写法始终可用。没传强度时默认 `high`（可用 `modelRegistry.models.<模型>.defaultEffort` 单独设），并按各来源支持的档位就近取；请求自己带了思考设置的照旧。
- **渠道自有产品**统一叫“渠道名-名称”：`qoder-ultimate`、`qoder-sonus`、`workbuddy-primary`、`cursor-auto`、`cursor-composer-2.5`。
- **开关按模型生效**：全局禁用覆盖该模型的所有来源（含以后新加入的）；按来源开关和“全局禁用但某来源例外”照旧。API Key 白名单也按模型匹配，写 `GLM-5.3` 等于写 `glm-5.3`。
- **首次开启**时自动把现有开关改写为按模型记录，只做一次（`modelRegistry.migrated`）。开启前可用 `./venv/bin/python -I scripts/model_registry_report.py --runtime --out report.md` 在配置副本上预览列表和分流的变化，线上数据只读。把 `enabled` 改回 `false` 即退回按渠道命名。
- 人工规则写在 `modelRegistry.rules`，如 `{"type": "merge", "from": "deepseek-flash", "into": "deepseek-v4.1-flash"}`。

### `GET /health`
运维健康检查（无鉴权）：
```json
{
  "status": "ok",          // ok | degraded | error | draining
  "upstream_client": { "state": "ready", "ready": true },
  "channels": { "total": 13, "enabled": 13, "oauth": 7, "api": 6 },
  "affinity_bound": 64,
  "device_id": "...",
  "version": "parrot"
}
```

---

## 网络配置热加载与健康状态

修改 DNS 地址、超时、缓存 TTL 或旧版 SOCKS5 开关/地址后，新请求会在主事件循环中按最新配置重建共享 HTTP 客户端；已开始的请求（包括流式响应和模型目录下载）使用原客户端直到结束，再回收旧连接池。保存相同网络设置不重建，停服后不会由请求重新创建客户端。该调整不改变路由优先级、OAuth 刷新或代理故障切换规则。

`/health` 的 `upstream_client` 只反映本地客户端生命周期，不发送 DNS/上游请求，也不进行付费模型探测。`ready` 表示已就绪；`rebuild_pending` 表示网络变更后的正常按需重建状态，仍可接受请求，并不代表已验证新代理可达。客户端未启动、已关闭或构造失败时，返回 `status: error` 和 HTTP 503；构造失败仍允许后续业务请求重试创建。原有渠道无可用/全部冷却的响应规则及 `draining` 排空语义保持不变，健康响应不包含代理凭据或异常原文。

## OAuth 故障切换与超时恢复

`PARROT_NO_REFRESH=1` 是双实例/测试环境的 **禁止凭据轮换** 开关，不应遗留在负责正式 OAuth 刷新的实例上。它不会让过期 token 重新有效。启用时，HTTP/SSE 和 Responses WebSocket 的 401/403 路径不执行同账号刷新重试，不输出“刷新成功”，直接尝试下一个符合调度条件的候选；也不会仅因禁止刷新而禁用账号。正常刷新流程、WorkBuddy 的鉴权规则、API Key 鉴权和最终错误状态保持不变。启用正式刷新前，应确认没有另一个实例同时轮换同一批账号，然后从部署环境移除该开关并重建容器。

OAuth 的明确传输故障（连接/读写/首包/空闲/总时限超时、连接或代理传输错误）仍按 `errorWindows` 阶梯冷却，但自动阶梯的永久档改用配置中的最大正分钟数；没有正数时使用 15 分钟。分类依据内部 attempt outcome，不依据上游错误文案。到期只表示允许正常调度重试，不代表账号已验证恢复，也不会新增后台付费推理探针。

启动时，旧版已永久冻结的 OAuth 记录只有在错误文本完整匹配已知内部超时诊断（超时标识本身或 `<标识> while opening upstream response`）时，才转换为从本次加载起计算的有限冷却，并持久化后发布。错误次数和原诊断保留，后续重启不重置这个期限。未知错误、鉴权/配额/手动冻结诊断、API 渠道和已有有限冷却不会被此迁移解冻。旧快照没有结构化失败类型，因此仅按上述精确诊断识别；无法识别的历史记录仍需管理员核对。记录新错误时显式指定的冷却期限始终优先，后续在途传输错误也不覆盖已有永久冻结的原因。仍在冷却中的在途成功也不会提前清除冻结。

## 💬 Telegram Bot 管理面板

发 `/start` 进入主菜单：

```
[📈 统计汇总]   [📋 最近日志]
[🔐 管理 OAuth] [📡 管理渠道]
[🔁 模型映射]   [⚖️ 负载均衡]
[⚙ 系统设置]   [❓ 帮助]
```

### 📈 统计汇总（4×4 时间×维度 + 两家族）
- 时间：今天 / 3 天 / 7 天 / 本月
- 维度：汇总（两家族分段）/ 按渠道 / 按模型 / 按 Key
- **汇总视图**：先 🅰 Anthropic 段（overall + 按渠道 Top3 + 按模型 Top3），后 🅾 OpenAI 段（同上，完整含重试/亲和）；底部跨家族按 Key Top + 最近调用（带家族图标）+ 未命中样本
- **专题视图**：按渠道 / 按模型 Top10，每条前缀 🅰/🅾 家族图标
- **模型元数据与金额统计**：普通渠道的客户端可见模型通过默认或 scope 专属 binding 精确绑定到 [models.dev](https://models.dev) `provider/model`；上下文、最大输出、能力与完整价格均来自绑定指向的同一目录记录，专属优先于默认，无绑定不猜测 provider。Cursor OAuth 是明确例外：其账号 scope 永远优先使用该账号实时 `AvailableModels` 返回的上下文、Max Context、推理档位与原生变体，绝不套用同名 models.dev 限制；当前 Cursor bridge 未实现 SelectedImage，因此有效元数据会如实标记图片输入不可用。Telegram 支持 canonical exact 自动同步，以及 OAuth/API scope → 现有模型 → provider → 目录模型的专属绑定流程；压缩模型独立设置并按实际 compact 路由解析相同 binding limits。每次真实上游尝试在 dispatch 时冻结输入 / 输出 / 缓存写入 / 缓存读取 Token 的 USD 费率，支持单档长上下文阶梯、OpenAI Priority 与 Anthropic Fast 完整替换价；xAI 响应若返回可信的 `cost_in_usd_ticks` 则优先采用真实金额；Cursor 使用 bridge 实时返回并由 Parrot 记录的用量，不再以 Dashboard usage event 覆盖。界面区分实际、估算、混合与未计价；无绑定、缺失 usage、无法确认旧 Token 口径、Claude 缓存写入 TTL 不可区分、多档 context tier、缺失 Priority/Fast tariff、未知 service tier，或日志缺少独立 reasoning/audio Token 维度时都会 fail-closed 为“未计价”，不会静默按 `$0` 或错误档位处理。models.dev 与 xAI 实际费用均按 USD 展示，不做实时汇率换算。

### 📋 最近日志
页面可在两类日志之间切换：
- `💬 请求日志`：普通文本 / Responses / Chat / WS 请求，详情包含完整重试链和请求/响应 body；
- `🎞 多媒体日志`：统一展示 GPT/Grok/Antigravity 图片生成与编辑、Grok 视频生成/编辑/延长。视频后续轮询只更新同一任务，详情显示进度、最终状态、OAuth 账号、耗时和 xAI 实际费用。

### 📡 渠道管理
添加向导（4 步 + 测试面板）、渠道详情、编辑、测试模型（单/全部）。

> **Base URL 自适应**：支持上游域名、带版本目录的地址（如 `https://provider.example/v1`）以及完整接口地址。默认按协议追加接口，已有 `/v1` 等版本目录不会重复追加，反代前缀原样保留；完整接口地址在配置文件和管理向导中均可使用。若上游接口挂在非标准路径（如智谱 Coding Plan 的 `https://open.bigmodel.cn/api/coding/paas/v4/chat/completions`），向导会自动拆分为 `baseUrl + apiPath` 存储，协议不匹配时给出交互式选择（采用识别到的协议 / 坚持当前协议清空路径 / 返回修改）。显式 `apiPath` 仍按相对整个 `baseUrl` 追加的方式执行，不自动改写自定义路径。模型发现和图片接口从相同的接口根目录推导。

### 🔐 管理 OAuth
- ➕ 新增账户：支持 Claude / OpenAI / Grok / Cursor；Cursor 显示可点击的浏览器登录链接，完成后点「已登录」即可自动获取并持久化 Token、套餐额度、账号模型和原生变体
- 每条账户显示：状态图标 / 过期时间 / 5h 7d 用量 / 月度统计 / 冷却中的模型
- 详情页：三家族统一布局（提供者 / 计划 / 过期 / 上次刷新 / 使用量 / 月度）
- 操作：刷新 Token / 刷新用量 / 清模型错误 / 清亲和绑定 / 启停 / 删除
- Cursor「模型目录」每页紧凑展示 6 个 canonical 模型及上下文/推理能力，使用编号按钮进入单模型详情；原生变体只保留给内部自动映射，不再铺满列表。所有存在独立 Max Context 档位的模型默认开启，单模型开关按账号持久化关闭例外，下游显式 true/false 仍可逐请求覆盖。目录还支持按账号批量禁用模型；禁用项不会注册为该 Cursor 渠道的候选，也不会出现在该账号的负载均衡顺序或被调度使用，重新进入批量页取消选择即可恢复。
- 底部批量：🔄 刷新全部用量 / 🧹 清除所有账户错误（有冷却才显示）
- OpenAI OAuth Codex 使用完整的 account/thread/turn/window 应用层身份生命周期：每个 canonical workspace 固定一个 UUIDv4 installation（versioned config + durable tombstone），每个 downstream principal+stable anchor 映射到 owner-scoped durable UUIDv7 logical session/thread；显式 native `turn_id` 的 HTTP/WS continuation 复用同一上游 UUIDv7 与 turn-state，新 turn 清空。相同 owner/thread 的上游 turn 细粒度串行，不同 thread 可并行；确认成功且 owner/session/model 匹配的 compaction response 以 durable CAS 推进 window，失败、retry 或普通 history 截断不推进。`response.created` 或可见输出后禁止跨账号重放。缺少 canonical workspace、profile 不匹配或身份冲突均 fail closed；不存在 `codexDeviceConvergenceEnabled` 退出开关。
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
- 路由边界：`grok-imagine-image*` 走 xAI OAuth；`image_models.antigravity` 中的模型走 Antigravity OAuth；其他图片模型按统一模型中心中的 GPT/Codex 或 API 来源路由；
- API Key 详情页分别控制图片、视频权限，模型白名单页用 🖼 / 🎬 标出对应媒体模型；
- 页面可直接进入「🎞 多媒体日志」，查看 GPT/Grok/Antigravity 统一统计、费用和任务状态。

### ⚖️ 负载均衡
- `smart` 智能调度：按滑动窗口评分 + 探索率排序
- `order` 顺序调度：按 config 渠道定义顺序依次尝试
- `priority` 优先级调度：不再按协议家族拆分；TG 提供“按渠道/账户”统一默认顺序和“按模型”canonical 专属顺序，模型专属优先级高于统一渠道优先级，转换层会先过滤不兼容候选
- 按模型页面 6 个/页展示当前有效渠道顺序；支持单模型编辑，以及不分页的一次性全模型多选批量修改。批量渠道并集保存时会按每个模型真实支持范围过滤
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

#### Antigravity TLS/H2 指纹伪装

该功能默认开启，已有配置中显式设置的关闭状态会保留；可在「OAuth 账户」→「⚙️ 设置」点击「🛡 AG指纹」热切换。新 Go 档案覆盖 Antigravity 文本、图片及 OAuth/模型发现/配额控制面；其他渠道及关闭时继续使用原有 transport。对应配置为：

```json
"antigravityOAuth": {
  "tlsFingerprint": {
    "enabled": true,
    "profile": "antigravity-ls-2.22.0"
  }
}
```

`antigravity-ls-2.22.0` 使用自研 `parrot-go-tls-client` 的 Go TLS/HTTP2 后端，通过 httpx 流式适配层保留请求取消、发送状态和代理边界。Linux amd64/arm64 原生库由维护者预编译并校验，Docker 内置，源码安装随 `requirements.txt` 安装；普通用户无需 Go/C 编译器（原生安装要求 glibc >=2.34）。版本/CL/平台身份固定成套，不能仅凭 JA3 或首帧相同宣称与官方全部行为一致。新登录使用 PKCE；存量账号不强制重登。详细配置、官网授权码入口及证据边界见 [Antigravity 客户端档案](docs/antigravity-client-profile.md)。

已保存的 `chrome131` 等旧 profile 不会被开关覆盖，切换 Go 后端需明确修改 profile。旧 profile 继续使用 `curl_cffi` 模拟所选浏览器；direct 与 SOCKS5 路由由 curl 直接处理；内建 SS2022 路由会为每次 attempt 创建仅绑定 loopback、一次性鉴权且锁定当前目标的 HTTP CONNECT bridge，TLS 仍由 curl 端到端建立，不改变 SS2022 出口。适配层关闭自动重定向、按现有超时传递 connect/read、规范化压缩响应，并将发送后状态不明的超时、取消及 transport 异常视为已 dispatch，避免不安全重放。由于 `curl_cffi` 高层流式 API 不提供 httpcore 的 upload-complete/DNS/TCP/TLS trace，启用时 connection/first-byte 分界及这些低层诊断字段不具备与 httpx 相同的精度。

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

完整字段说明见 `docs/02-config-schema.md` 和 `docs/openai/02-config-schema.md`。WorkBuddy 中国区 CLI 的模板兼容规则可通过 `workbuddy.requestRewrite` 配置，默认定向处理已确认的身份句和 `<env>` 主分支模板；支持开关、自定义字面规则及热加载，不修改用户/工具正文。完整示例和匹配边界见 [WorkBuddy 模板适配](docs/02-config-schema.md#workbuddy-模板适配-workbuddyrequestrewrite)。关键字段速查：

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
  "openaiOAuth": {
    "codexProfileAutoUpdate": true,
    "codexCliVersion": "0.159.0",
    "codexProtocolProfile": "rust-v0.159.0",
    "codexIdentity": { "mode": "per-oauth-account", "newIdentityGenerationVersion": 1 },
    "quotaProbe": { "enabled": false, "input": "1", "instructions": "reply ok" }
  },
  "notifications": { "enabled": true, "events": { ... } },
  "channelSelection": "smart",
  "loadBalancing": { "initialized": false, "channelPriorityOrder": [], "modelPriorityOrders": {}, "priorityOrders": { "anthropic": [], "openai": [] } },
  "cchMode": "disabled",
  "telegram": { "botToken": "...", "adminIds": [123] }
}
```

### 统一通知与快捷处理

账号、额度、模型、请求路由和 Responses 历史存储通知使用 `src/notifications/catalog.py` 的同一事件目录。Telegram 可精确查看账户/渠道及本地详情、关闭当前消息、按收件人静音；明确刷新仅针对绑定账户，权益消费/积分策略/清冷却遵守已有权限和确认。查看不隐式刷新，unknown 不等于成功，关闭不等于恢复或停用账户。

`notifications.events` 中显式新事件名优先，否则兼容旧 key。历史逐次授权续期通知及 180 秒自动删除保持；不会在升级时静默关闭。模型目录/权益推送现在受统一开关控制，用户主动操作回执不受运维静音影响。官网状态、应用更新、网络通知及管理登录审批保留原通路。

通知最小记录复用现有 JSON durable state，不保存凭据或确认计划秘密；每个管理员独立记录 Telegram 接受/拒绝/结果未知。无持久世代的旧账户在重启后旧写按钮失效，旧批准不会自动重做。未实现新的 Web 通知中心，也不承诺 Telegram 已读或恰好一次投递。

> `quotaMonitor.enabled` **默认关闭** —— 启用后每 N 秒拉一次每个 OAuth 账号的 usage（Claude 走 `/api/oauth/usage`，OpenAI 走 `wham/usage`）。`openaiOAuth.quotaProbe` 是另一个会实际调用模型的诊断入口，默认禁用，只有配置开启且调用方明确请求时才发网。

> `apiKeys.*.allowImages` **默认关闭** —— 新建或历史 API Key 不会自动获得图片生成 / 编辑能力，必须在 TG「🔑 管理 API Key」里显式开启。

> `apiKeys.*.allowVideos` **默认关闭** —— 视频费用较高，需在 TG「🔑 管理 API Key」里为获准的下游 Key 单独开启，并可配合 `allowedModels` 限定视频模型。

> **旧版本升级无需手工迁移：**启动时会保留既有 API Key、`allowedModels`、`allowImages`、OAuth 账号及全部 token，只为缺失字段补上 Imagine 默认配置，并将历史 Key 的 `allowVideos` 设为 `false`。旧版 `state.db` 中的 `xai_video_jobs` 会只读迁移到 `durable-state.json`；既有 `image_logs.db` 会原地补充多媒体字段，历史 GPT 图片记录自动按 `provider=openai`、`media_type=image` 解释，不删除、不改名、不清空。若要开放视频，升级后再按 Key 显式开启即可。

> API Key 请求排队时，小于 `queuedBodySpoolThresholdBytes` 的待回放 body 保留在内存；超过阈值后自动转入数据目录下固定的 `queued-body-spool/` 私有临时目录。内存预算沿用 `defaultMaxQueuedBodyBytesPerKey` / `maxQueuedBodyBytes`；磁盘临时数据另由 `defaultMaxQueuedBodySpoolBytesPerKey` / `maxQueuedBodySpoolBytes` 约束，成功、失败、取消或断开后都会关闭并删除。

> `images.cacheRetentionDays=0` 表示不按时间清理；`images.cacheMaxBytes=0` 表示不按空间清理。相对 `cachePath` 会落在数据目录下，Parrot 会阻止相对路径逃逸。

**不可热加载字段**（改后需重启容器）：`listen.host` / `listen.port` / `stateDbPath` / `runtimeStatePath` / `durableStatePath` / `openai.store.dbPath` / `logDir` / `telegram.botToken` / `telegram.adminIds`。

---

## 🛠 运维

所有持久化数据集中在可写的 `<安装目录>/data/`：`config.json` / `runtime-cache.json` / `durable-state.json` / `state-migration.json` / 只读保留的旧版 `state.db` / `openai_response_store.db` / `image_logs.db` / `logs/` / `images/` / `.anthropic_proxy_ids.json`。

### 启动 / 停止 / 重启 / 状态（Docker Compose）

```bash
cd <安装目录>
docker compose up -d         # 启动
docker compose stop          # 停止
docker compose restart       # 重启
docker compose ps            # 状态
docker compose down          # 停止 + 删容器（数据保留在 ./data）
```

停止时使用同一个单调时钟请求排空截止（`shutdown.drainTimeoutSeconds` 默认仍为 80 秒），到期取消并等待请求收尾，再停止真实后台 worker、验证并 flush 状态快照；第二次信号不跳过 flush。默认及安装器生成的 Compose 已设置 `stop_grace_period: 150s`，为 worker 和落盘留出余量。已有部署需自行合并此字段；不应依赖 Docker 默认约 10 秒的停止窗口。自定义更长 drain、慢盘或长外部操作时也要扩大外层宽限，不能把不可中断 I/O 或 SIGKILL 当成已完成持久化。

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

GPT/Grok/Antigravity 图片及 Grok 视频任务使用独立日志库 `data/image_logs.db`；历史主调用表原地扩展为统一多媒体任务日志，GPT 图片账号尝试表继续保留。视频创建记一条 `pending`，客户端轮询只更新该记录直至 `success / failed / expired`，不会污染普通文本请求日志。

### 多媒体缓存

缓存默认关闭。开启后，GPT/Grok/Antigravity 图片以及轮询完成的 Grok 视频会保存到 `data/images/`（或 `images.cachePath` 指定的位置），并统一按 `images.cacheRetentionDays` 和 `images.cacheMaxBytes` 自动清理。缓存路径只在服务端内部使用，API 响应不会暴露本地文件路径；管理员可在 TG「📋 最近日志 → 🎞 多媒体日志」的任务详情中查看仍存在的缓存图片或视频。

### 状态数据

`data/runtime-cache.json` 保存可重建的评分、冷却、亲和与配额缓存；`data/durable-state.json` 同步保存 updater、状态通知、视频任务与 compaction owner 等必须持久化状态。两者都是带 schema、单调 generation 和 checksum 的 0600 原子快照，并保留一份已验证 `.bak`。`state-migration.json` 最后写入，记录旧库规范路径、主文件及 WAL/SHM/journal 指纹和成功快照 generation。

`stateDbPath` 仅指向旧版只读迁移源；`runtimeStatePath` / `durableStatePath` 的默认值和相对路径始终位于可写 `DATA_DIR`，与绝对旧库所在目录无关。首次升级会在 0700 私有临时目录复制完整 SQLite 文件集并恢复 WAL/hot journal；源文件不 checkpoint、不改名、不删除。若旧版本回滚后再次写入健康的 `state.db`，下一次升级会发现内容变化、备份当前 JSON、重新导入旧 schema 实际存在的持久域并最后更新 manifest；旧 schema 不认识的 durable 域继续保留。仅 mtime/路径或 SHM 锁元数据变化不会触发重导入；复制一致性仍单独校验完整指纹。若变化后的旧库损坏或不可读，现有 verified JSON 继续权威生效，不会被任何历史备份覆盖，source revision/manifest 保持未推进并在每次重启重新检查，直到源被修复、移除或归档。请在确认不再降级前保留旧库及 sidecar/历史备份。

`data/openai_response_store.db`（SQLite）独立保存 `previous_response_id` history。旧版嵌入 `state.db` 的响应按源指纹执行 `INSERT OR IGNORE`；缺表或损坏只跳过该可选导入，不阻断独立 Store。

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

数据文件默认在源码目录下（不设 `ANTHROPIC_PROXY_DATA_DIR` 时回退到 `BASE_DIR`）。源码代码备份只包含代码产物，不包含配置、状态快照或自定义运行数据路径；回滚不会恢复业务数据。旧 tar 备份恢复时也按代码白名单过滤。

自更新仍是「备份/拉取 → staged → 二次确认」。确认后由备份目录中的一次性、仅依赖 Python 标准库的独立监护程序负责停止/启动、核对目标版本和代码身份；导入失败也能恢复代码和本次依赖安装改变的文件，并再次验证旧版健康。依赖安装前本地复制当前解释器的安装目录（site-packages/scripts），因此需预留对应磁盘空间；不会创建新部署体系或自动提升权限。共享解释器若在 staged 后被其它程序改写，回滚拒绝覆盖冲突并明确报失败，应由管理员处理。

systemd 模式通过 `systemd-run` 一次性 unit 让监护者不受被更新服务 cgroup 停止影响，要求已有运行身份具备对应调度及服务控制权限；权限不足会明确拒绝派发，不自行授权。自行编写的服务建议 `TimeoutStopSec=150s`（按 drain 与实际 worker/磁盘上限加大），不要用第二次信号代替状态收尾。裸进程模式由独立监护者发送 TERM、等待退出后重新拉起，不再绕过 lifespan 使用线程 exec。上线前仍需在自己的 unit/文件系统/解释器环境演练；本仓库隔离测试不等于真实 systemd/Docker 验收。

自更新 sidecar 的健康探针使用实际 `listen.port`，并限制 curl/Engine CLI 单次时间；成功要求目标版本和镜像身份一致。手工改变容器内端口时，还需同步修改 Compose/Dockerfile 固定端口的 healthcheck 与端口映射。

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
├── scripts/release.py           ← 固定发布入口：start / resume / status
├── .github/workflows/
│   ├── release-prepare.yml      ← 候选回归与双架构原生构建并行
│   └── docker-publish.yml       ← 复用候选产物 → GHCR → Release
├── server.py                    ← FastAPI 入口
├── requirements.txt
├── data/                        ← 运行时持久化（容器挂载点；源码模式不存在）
│   ├── config.json              ← 唯一配置文件
│   ├── runtime-cache.json       ← 可重建运行时状态快照
│   ├── durable-state.json       ← 必须持久化状态快照
│   ├── state-migration.json     ← 旧库源指纹与迁移 generation
│   ├── state.db                 ← 只读保留的旧版迁移源（兼容降级）
│   ├── openai_response_store.db ← previous_response_id history（TTL）
│   ├── image_logs.db            ← 图片/视频统一多媒体任务日志（兼容旧图片历史）
│   ├── logs/YYYY-MM.db          ← 按月分库业务日志
│   ├── images/                  ← 图片/视频缓存（开启后，保留兼容目录名）
│   └── .anthropic_proxy_ids.json ← device_id 持久化
└── src/
    ├── config.py                ← 配置加载/保存/热加载
    ├── auth.py                  ← 下游 API Key 验证
    ├── errors.py                ← 标准错误响应
    ├── state_store.py           ← JSON 快照、原子安装与单实例锁
    ├── state_migration.py       ← 旧 SQLite 文件集私有副本迁移
    ├── state_db.py              ← 后端中立业务状态门面
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
    ├── oauth_manager.py         ← 多 OAuth 账户管理（Claude + OpenAI + Grok + Cursor）
    ├── upstream.py              ← httpx client + SSE 工具 + 家族 Builder
    ├── notifier.py
    ├── transform/
    │   ├── cc_mimicry.py        ← Claude CC 伪装（与 cc-proxy 同源）
    │   └── standard.py
    ├── channel/
    │   ├── base.py              ← upstream_stream_only 抽象
    │   ├── oauth_channel.py     ← Anthropic OAuth 渠道
    │   ├── openai_oauth_channel.py ← OpenAI Codex OAuth 渠道
    │   ├── cursor_oauth_channel.py ← Cursor OAuth + canonical→原生变体渠道
    │   ├── api_channel.py       ← Anthropic 协议第三方 API 渠道
    │   └── registry.py
    ├── oauth/
    │   ├── openai.py            ← OpenAI OAuth refresh + 限额头解析
    │   └── cursor.py            ← Cursor 登录轮询、刷新、额度与模型同步
    ├── cursor_bridge/           ← Cursor H2/protobuf AgentService 私有 bridge
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
            ├── oauth_menu.py    ← 支持 Claude + OpenAI + Grok + Cursor OAuth 管理
            ├── apikey_menu.py   ← API Key 模型 / 图片 / 视频权限
            ├── image_menu.py    ← GPT/Codex 图片配置 / 缓存设置
            ├── xai_imagine_menu.py ← Grok Imagine 图片 / 视频设置
            ├── media_logs_menu.py   ← GPT/Grok/Antigravity 统一多媒体日志
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
- **error**：无启用渠道 → 加至少一个渠道（TG bot「📡 渠道管理」或「🔐 管理 OAuth」）
- **degraded**：有渠道但全部冷却 → 「📡 渠道管理」→「🧹 清全部错误」，或 TG bot 的「🔐 管理 OAuth」→「🧹 清除所有账户错误」

### 下游返回 503 `No available channels for model: xxx`
该模型在所有启用渠道里都不存在。检查：
- 模型名拼写
- 渠道是否被禁用 / 配额禁用 / auth_error
- 对 OpenAI OAuth：以该账户最近一次认证 `/models` 目录及选中 Codex profile 为准；源码不维护会过期的型号黑名单

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

### Antigravity 账户返回 403 `Verify your account to continue.`
Google 要求该账户完成账号验证（`VALIDATION_REQUIRED`），TG 配额一行显示「需完成 Google 账号验证」。token 刷新照常成功，不会标 `auth_error`；删号重加、重新授权都解不开。用 `docker exec -i parrot python3 - < scripts/antigravity_validation_link.py` 只读取出该账户最新的验证链接（已补好 `authuser`），在只登录该账号的无痕窗口中打开。完整步骤见 [Antigravity 账号验证](docs/antigravity-validation-required.md)。

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

### Qoder 设备档案与出口

新增账户先选择既有账户代理，再本地生成、预览设备档案；新登录默认 macOS Apple Silicon，平台按钮依次为 macOS / Windows、Linux x86_64 / ARM64，只显示已支持的平台。草稿可选择平台/架构并重生成，确认后才打开设备授权或输入 PAT，已绑定账户的重授权仍沿用原档案。当前可生成 Linux x86_64 / ARM64、macOS Apple Silicon（M 系列）、Windows x64 档案；Intel Mac 与原生 Windows ARM64 仍未适配，不用其他平台模板冒充。国际/CN 设备授权、国际 PAT 均走此流程。档案随账户原子保存，重授权沿用。重复添加已有身份不会覆盖设备，请从原账户「设备档案 / 重授权」进入。旧账户不会自动改 machine_id；先在此入口确认初始化并重授权，原设置和凭据在成功前保留。

最好不要多个账户共用出口，可通过设置代理指定；不同代理名称不等于不同出口。浏览器授权使用浏览器自身网络。Qoder 的已选代理失败不自动直连；保留 legacy SOCKS5 继承与显式 direct 的区别，仅 Qoder 禁用环境代理。一次物理业务请求将同一有效 connector 快照贯穿票据 key、UMID 发送与 HTTP 发送，配置 reload 不改变在途快照；代理组可回退，但必须为新成员重新准备身份。其他 provider 的全局 directFallback 选择不变。设备档案草稿和账户详情不显示出口 IP，也不自动探测公网 IP；保留代理名称及网络使用提示。详情从已保存档案展示型号、系统、架构、已设置容量及完整档案 ID；可选硬件信息仅在有值时显示，未写入的 CPU、主板等字段直接省略，不显示占位文字，也不按平台名称推断。列表只提示已开启的自动签到，不展示签到记录；详情保留签到结果，开关状态只显示在按钮上。套餐的 9999 年占位到期时间不显示。

管理 API：`POST /oauth/login-flows` 的 Qoder 请求用 `loginMethod=device|pat`、可选 `proxyTarget` / `reloginAccountId`，返回 `devicePreview` 和 flow capability，不执行登录。用 flowSecret、预览 revision 调用 `.../{flowId}/qoder-device/regenerate` 或 `.../qoder-device/confirm`；regenerate 可传 `platform=linux-x64|linux-arm64|macos-arm64|windows-x64` 切换平台，省略则保留当前平台，未适配平台会拒绝且不改变草稿。PAT 可在确认后再次向 confirm 提交 pat。之后复用 poll/complete/cancel。旧 start 请求直接带 pat 会被拒绝。所有准备/确认均绑定 actor、过期时间和版本，确认后的档案不能重生成。

Qoder 模型页只展示与下游模型列表一致的名称，不另列“调用名”或内部代号；出站始终使用原 ID 与原 `model_config`，响应里的模型名回显下游请求时用的名字。开启统一模型名后，Qoder 的厂商模型并入共享名字（如 `glm-5.3`、`qwen3.8-max`），自有档位与代号叫 `qoder-ultimate`、`qoder-cantus` 等，内部 ID 不再接受；未开启时仍是 `qoder-cantus`、`qoder-glm-5.3` 这类账户级别名。账户模型页和模型中心展示已同步的免费状态、积分倍率及优惠前倍率；倍率不是每次调用固定扣费积分，费用缺失时不补零或标成免费。账户列表、详情的积分池名称统一显示为“积分”，不改变池内数值。

macOS 档案版本 `macos-generated-v1`：独立生成 MAC、序列号、UUID、主机名和辅助随机状态，不复制采样机器数据；SDK `1.1.2`、`os=7`，签发走同一已选代理链上的 HTTPS，校验服务器证书。四块字段数为 `[2,44,5,3]`，辅助状态与 Linux 分别校验。已离线对照官方组件请求验证编解码，尚未做真实账户在线签发、模型或签到验证。

Windows 档案版本 `windows-generated-v1`：100 字段按 Windows SDK 布局独立生成 MAC、序列号、UUID、主机名等，可选软硬件库存保持空集合，不复制采样机器数据。SDK `1.0.0`、`os=6`，四块字段数 `[2,100,5,3]`；`e=1005` 与档案字段一致，通过同一已选代理链使用明文 HTTP over 443。完整合成档案已在默认入口获得 `ec=200` 并解出 88 字符票据，同一不可变档案再次签发也已验证；无需复用 SDK 的本地 fallback Token 或机器缓存。此验证只覆盖 UMID 签发，不等于真实 Qoder 账户登录、模型或签到接受性验证。

新 Linux 档案版本 `linux-generated-v2`：独立 machine_id、生成的本地管理 MAC/UUID/主机名与中性 Parrot 配置，不包含采样机器私密模板，不承诺防关联或绕过领取限制。结构校验不等于硬件真实性或服务接受性：四块字段数为 `[1,43,5,3]`，block3 的空辅助状态为 `['', '####', '']`，block0 保持原有正确密文常量。旧 v1 标记为需迁移，禁止自动换身份；必须预览确认并成功重授权后，仅修 block3/版本，保留原 machine_id、block1 身份、其余块与原设置，保存成功前原账户不变。UMID 配置 `runtime-info-1.5.0-default-v1` 默认 `pum.m.alibaba.com`；可用 `QODER_UMID_REGION=default|sg|us` 明确选择入口。仅默认入口有既有签发证据，SG/US 和生成档案的 IDE 业务接受性仍待真实账号联调，不做自动在线探测。传输沿用组件的明文 HTTP over 443，经 Parrot 原始字节流代理，不是 HTTPS。

票据与档案分离，按账户、档案及实际 connector 配置绑定到私有 durable state，锁内复用一小时（本地策略）、失败退避。Type/Code 使用 XXHash64 确定性 mark，未检测分支为 90/0，不宣称检测为物理机；票据格式 v2 不复用旧随机三元组。UMID 保存失败时进程内复用同一候选；重启只保留已落盘票据或退避 intent，不保证恢复未落盘票据。普通模型 GET 不带 Encode 参数；对话保持已验证 codec/COSY 路线，设备头来自同一身份 provider；campaigns/usage 账户页继续保留原协议（包括 ClientType=10），不混用 IDE activity；桌面 ClientType=0/Version=1.24.2 用于已验证的普通 COSY 路线。国际 campaigns 仅从统一身份源取原有的 Token/Type 两头，CN campaigns 保留不带设备头行为。新容器不下载或运行 UMID SDK；已有 qoder-device 数据及可选 `scripts/qoder_runtime_info.py` 工具保留。

一次性 job/device refresh 独立记录：消费 RT 前先写私有 durable intent，收到新 pair 后先落 durable candidate 再发布配置。保存失败、进程重启后有候选时仅重试保存；intent/unknown 缺候选时禁止 auto/force 重放旧 RT，管理 API `refreshStatus` / TG 显示结果不明、需重授权，不将其称为已吊销。安全存储失败不消费 RT。PAT 的可重复 exchange 与一次性 RT 区分；登录交换成功后保留 candidate，userinfo 失败只重查身份。准备、确认、identity_pending 均可 poll；10 分钟有效期从准备开始，TG 显示真实截止时间。

SSE 保留 event:error / event:finish 和结构化业务码，三入口流/非流统一分类。官方兼容的干净 EOF（合法完整帧、正常输出）允许完成；损坏残余帧、显式错误、不完整工具、读异常/超时及 forceFinish 不当成功。finish_reason 后继续读取 usage；引用代码中的工具示例保持文本，并行 native tools 按 index 跟踪。105 仅在尚无下游输出时恢复一次，提交后绝不重放。明确 temperature 映射到 `parameters.temperature`；未确认本接口语义的 top_p、stop、schema、required/named tool choice 等候选请求明确拒绝，不以提示词冒充硬约束。

CN jobToken 失败本身不再标 ready：429/明确认证拒绝保留候选与错误；其他兼容情形只有同档案/路由的 device-token 模型 GET 独立成功后才可保存 deviceChat。此代码内正向校验不等于已完成线上 CN 验证。PAT 签到先 ensure token，105 至多恢复一次；campaigns 必须是合法列表，缺字段/业务错误不当 inactive。

以上是离线验证的实现事实，不是上线或生成身份业务接受结论。`./venv/bin/python -I -B scripts/qoder_umid_probe.py` 默认只生成并检查临时档案、绝不联网、不读账户配置。经控制者批准目的地及出口后才可用 `--execute --direct` 单次申请；也可由隔离调用者向 `probe(execute=True, open_stream=...)` 明确提供已批准 connector。无自动重试，仅输出签发状态/长度，不输出票据或设备块。即使单次签发成功，也不证明模型、聊天、签到、长期时间戳或风控接受。
