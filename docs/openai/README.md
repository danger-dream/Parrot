# OpenAI 协议支持 —— 扩展设计

本目录是"在 anthropic-proxy 里追加 OpenAI 两套对话接口（`/v1/chat/completions`、`/v1/responses`）"的设计文档集合。

## 目标

1. **两个新入口**：`/v1/chat/completions`、`/v1/responses`；现有 `/v1/messages`、`/v1/models`、`/health` 保持原路径与原行为不变
2. **三种上游协议**：`anthropic`（现状）、`openai-chat`、`openai-responses`
3. **家族内互通**：`/v1/chat/completions` 入口可以打到 `openai-responses` 上游，反之亦然（下方 §3）
4. **共用一个服务 / 一个配置 / 一个 Telegram 管理面板**
5. **Anthropic 原有功能全部保留**：OAuth 账户、CC 伪装、配额监控、亲和、评分、冷却、TG 菜单——全部不动

## 核心约束（底线）

**Anthropic 侧业务逻辑不得发生行为改变**。允许的只有：
- 在协议无关的共享模块（`auth` / `errors` / `fingerprint` / `upstream` / `scheduler` / `failover` / `probe` 等）里**追加**新符号、或在签名里加带默认值的参数（默认值走原路径）
- 在共享扩展点（如 `channel/registry`）里**增加**钩子（默认行为不变）
- 在 TG 菜单里**追加**新按钮/向导步骤（不修改既有按钮）

不允许的：
- 修改 Anthropic 现有函数的函数体让它产出不同结果
- 修改 Anthropic 既有测试预期
- 在 Anthropic 调用链里插入 OpenAI 专属分支

## 跨家族不做

本次范围**不支持** `/v1/messages` → `openai-*` 上游，也不支持 `/v1/chat/completions`、`/v1/responses` → `anthropic` 上游。调度器按 ingress family 硬过滤候选渠道，避免误路由。

## 文档目录

| 文件 | 内容 |
|---|---|
| [01-architecture.md](./01-architecture.md) | 总体架构、路由、调度、转换流程图、关键决策 |
| [02-config-schema.md](./02-config-schema.md) | `config.json` 字段变更（仅追加） |
| [03-protocol-diff.md](./03-protocol-diff.md) | OpenAI chat vs responses 规范对比、互转死角清单 |
| [04-transform.md](./04-transform.md) | 请求转换器、SSE 双向状态机、CapabilityGuard |
| [05-store.md](./05-store.md) | `previous_response_id` / `conversation` 本地存储 |
| [06-reasoning-bridge.md](./06-reasoning-bridge.md) | reasoning 跨协议映射 |
| [07-anthropic-touchpoints.md](./07-anthropic-touchpoints.md) | Anthropic 侧逐文件的"纯追加"改动清单 |
| [08-openai-tree.md](./08-openai-tree.md) | `src/openai/` 子树文件清单 + 每个文件的公开接口 |
| [09-milestones.md](./09-milestones.md) | 分阶段实施计划 |

## 工作量一览

- Anthropic 侧：约 **+180 行纯追加**（不改行为）
- `src/openai/` 子树：约 **+4700 行新增**
- 工期：专注 **2.5–3 周**
