# 01 — 架构总览

## 1.1 路由

所有路径**同根挂载**，共享 `/health` 与 `/v1/models`：

| 路径 | 方法 | 家族 / 协议 | 说明 |
|---|---|---|---|
| `/v1/messages` | POST | anthropic | 现状，零改动 |
| `/v1/chat/completions` | POST | openai-chat（ingress）| 新增 |
| `/v1/responses` | POST | openai-responses（ingress）| 新增 |
| `/v1/models` | GET | 共享 | 按 API Key 的 `allowedProtocols` 过滤返回相应家族的模型 |
| `/health` | GET | 共享 | 零改动 |

`/v1/models` 的返回结构保持 Anthropic 风格（`data[]` + `type:"model"` 等字段）。OpenAI SDK 虽然期望 `object:"list"` + `data[].object:"model"`，但实际 OpenAI 客户端通常不严格校验，多数 SDK 也支持当前结构；如确实要做兼容，可在查询时按 `Accept` header 或 query param `?style=openai` 选择回复风格——这是第 V 阶段的锦上添花，首版不做。

## 1.2 上游协议与家族

```
Channel.protocol ∈ { "anthropic", "openai-chat", "openai-responses" }

family(p) = "anthropic"  if p == "anthropic"
          = "openai"     otherwise

ingress_protocol = "anthropic" | "chat" | "responses"  (由入口决定)

family(ingress) 必须 == family(channel.protocol)   → 不支持跨家族
```

## 1.3 调用流程图

```
┌──────────────────────┬─────────────────────────┬────────────────────┐
│  /v1/messages (原)   │  /v1/chat/completions   │  /v1/responses     │
│  ingress=anthropic   │  ingress=chat            │  ingress=responses  │
└──────────┬───────────┴──────────┬──────────────┴─────────┬──────────┘
           │                      │                        │
           │               ┌──────┴────────────────────────┴──────┐
           │               │  auth + auth_ex（补 allowed_protocols）│
           │               │  body 解析 / CapabilityGuard         │
           │               │  fingerprint_query（按 ingress 选）  │
           │               └──────┬────────────────────────┬──────┘
           │                      │                        │
           ▼                      ▼                        ▼
    ┌──────────────────────────────────────────────────────────────┐
    │  scheduler.schedule(body, key, ip, ingress_protocol="...")    │
    │     按 family(ingress) 筛候选 + 亲和 + 评分排序               │
    └──────────────┬────────────────────────────────────────────────┘
                   ▼
    ┌──────────────────────────────────────────────────────────────┐
    │  failover.run_failover(..., ingress_protocol)                 │
    │    • ch.build_upstream_request(body, model, ingress=...)      │
    │       - 同协议 → 透传（参数白名单过滤）                        │
    │       - 跨变体（chat↔responses）→ 请求翻译                      │
    │    • 首包安全检查（按 ch.protocol 选 parse_first_*_event）    │
    │    • SSE 消费（按 ch.protocol 选 Tracker/Builder）             │
    │    • 若 ingress != ch.protocol → 流经 StreamTranslator 回转   │
    │    • 错误协议（JSON / SSE）按 ingress 选格式                   │
    └───────────────────────────────────────────────────────────────┘
```

## 1.4 九种组合的处理矩阵

```
                       channel.protocol
                       ├─ anthropic ─┼─ openai-chat ─┼─ openai-responses
ingress=anthropic    │   透传/CC伪装 │     拒绝      │     拒绝
ingress=chat         │    拒绝      │     透传      │   chat→responses
ingress=responses    │    拒绝      │  responses→chat │     透传
```

"拒绝"由 scheduler 在候选筛选阶段自动实现（family 不匹配直接不入候选），最终落到"无可用渠道"的现有错误路径。

## 1.5 复用 vs 新建的决策准则

| 类型 | 决策 | 举例 |
|---|---|---|
| 协议无关基础设施 | 直接共享 | scorer / cooldown / affinity / state_db / log_db / notifier / oauth_manager / public_ip / config |
| 协议相关但"每种协议不同函数"好写 | 在原文件追加函数（Anthropic 侧函数不动） | `errors.py`、`upstream.py`、`fingerprint.py` |
| 签名里加新参数就能兼容（默认值保留原行为）| 原地扩展 | `scheduler.schedule`、`failover.run_failover`、`probe.probe_channel` |
| 逻辑形态差异太大 | 完全分流到 `src/openai/` 子树 | 请求/响应 Translator、Store、CapabilityGuard |
| 注册/路由类扩展点 | 增加 factory，默认回原路径 | `channel/registry.py` |

## 1.6 文件分工（详细见 07 / 08）

```
src/
├── server.py             ← 只追加两个 route 函数
├── auth.py               ← 追加 allowedProtocols 读取函数
├── errors.py             ← 追加 OpenAI 两种错误格式函数
├── fingerprint.py        ← 追加 chat / responses 两套归一化 fp
├── upstream.py           ← 追加 OpenAI 两套 SSE 工具类 & 首包解析
├── scheduler.py          ← 添加 ingress_protocol 默认参数 + family 过滤
├── failover.py           ← 添加 ingress_protocol + SSE toolkit 分派 + 流翻译器接入
├── probe.py              ← 按 ch.protocol 分派 probe payload
├── channel/
│   ├── base.py           ← Channel 基类加 protocol 字段（默认 "anthropic"）
│   └── registry.py       ← 追加 factory 扩展点
├── telegram/menus/
│   ├── channel_menu.py   ← 添加"协议"向导步骤 + 编辑入口
│   └── apikey_menu.py    ← 添加"允许协议"按钮
│
└── openai/               ← 新建子树，见 08
    ├── handler.py         # /v1/chat/completions + /v1/responses 主流程
    ├── auth_ex.py         # allowed_protocols 补丁
    ├── store.py           # previous_response_id 本地存储
    ├── channel/           # OpenAIApiChannel + factory 注册
    └── transform/         # 请求 + SSE 双向翻译器 + Guard
```

## 1.7 关键设计决策汇总

| 决策 | 选择 | 理由 |
|---|---|---|
| 路由前缀 | 无前缀，全部根路径挂载 | 用户要求 `/v1/models` `/health` 共享 |
| /v1/models 冲突 | 按 API Key 的 `allowedProtocols` 过滤 | 简单、对客户端透明 |
| 跨家族互转 | 不做 | 用户明确划定范围；scheduler family 过滤兜底 |
| `previous_response_id` | 本地 Store 支持 | 用户明确要求（见 05） |
| reasoning 信息 | 默认 passthrough（映射到 `reasoning_content`/`summary_text`）| 可转（见 06） |
| CC 伪装 | 仅对 anthropic 家族，OpenAI 家族永远不用 | 与 OpenAI 无关 |
| auth 扩展 | 新增 `allowed_protocols_for(key_name)` 辅助函数 | 保持 `auth.validate` 签名不变 |
| Channel 基类 | 加 `protocol: str = "anthropic"` 类属性默认值 | 向后兼容 |
