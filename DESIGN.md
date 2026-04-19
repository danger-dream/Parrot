# anthropic-proxy — 设计方案

> 多渠道、智能调度、故障转移的 Anthropic API 代理。
> 由 cc-proxy（单 OAuth 账号）演化而来，完整复用 CC 伪装逻辑，并引入多渠道抽象、亲和绑定、智能排序、故障转移与 OAuth 多账户支持。

## 文档索引

本方案拆分为 12 个子文档，建议按顺序阅读：

| # | 文档 | 内容 |
|---|---|---|
| 01 | [overview](docs/01-overview.md) | 项目总览、与 cc-proxy 关系、目录结构、核心原则 |
| 02 | [config-schema](docs/0
2-config-schema.md) | `config.json` 完整 schema 与所有字段默认值 |
| 03 | [database](docs/03-database.md) | `state.db` 与按月分库 `logs/YYYY-MM.db` 的 DDL |
| 04 | [channel-abstraction](docs/04-channel-abstraction.md) | Channel 基类 / OAuthChannel / ApiChannel |
| 05 | [cc-mimicry](docs/05-cc-mimicry.md) | CC 伪装从 cc-proxy 的移植清单（禁止变动） |
| 06 | [scheduler](docs/06-scheduler.md) | 调度器：筛选→亲和→智能排序→顺序重试 |
| 07 | [failover](docs/07-failover.md) | 故障转移：首包锁、黑名单、超时四态处理 |
| 08 | [oauth-multi](docs/08-oauth-multi.md) | 多 OAuth 账户、PKCE 登录、配额监控、自动恢复 |
| 09 | [tgbot](docs/09-tgbot.md) | Telegram Bot 完整交互树（每个菜单逐一定义） |
| 10 | [error-protocol](docs/10-error-protocol.md) | Anthropic 标准错误格式，所有错误路径统一 |
| 11 | [background-tasks](docs/11-background-tasks.md) | 所有后台循环任务清单 |
| 12 | [milestones](docs/12-milestones.md) | 实施里程碑（M1–M10）与迁移说明 |

## 核心原则（不可动摇）

1. **OAuth 零风险** — cc-proxy 的请求伪装逻辑（指纹、system block、metadata、CCH 签名、工具名混淆、beta 列表、cache 断点）**一字不改**地移植到 `src/transform/cc_mimicry.py`，OAuth 渠道请求构建路径直接调用它。
2. **OAuth 不做主动测试（严格执行）** — 开发期、里程碑验收、对比测试**一律不发起任何真实的 OAuth 上游请求**。重复的 OAuth 登录、连续的相同模式调用可能触发 Anthropic 的风控检测，导致账号异常。所有 OAuth 链路的验证都采用"离线 + 字节级对比"方式：用 cc-proxy 线上抓取的真实请求 body 作为 fixture，anthropic-proxy 的 `OAuthChannel.build_upstream_request` 在本地生成对应字节流，与 fixture 逐字节比对；**不实际发送到 api.anthropic.com**。真实验证在用户侧、用生产 cc-proxy 的自然流量镜像/切流方式进行。
3. **故障转移安全边界** — 向下游发送任何字节（HTTP 头或 SSE event）之前为"可切换"区；一旦发出首字节，锁定当前渠道，异常只能以 Anthropic 标准错误事件收尾。
4. **会话亲和** — 同一会话必须绑定到同一渠道（否则缓存失效，token 成本激增）。指纹 = `hash(api_key_name | client_ip | canonical(倒数第二条) | canonical(倒数第一条))`，查询时去掉当前 user turn 再取最后两条；写入时追加本次 assistant 回复后取最后两条——两个时刻 hash 必然相等。
5. **错误协议统一** — 任何出错路径（未发首包 / 已发首包）都返回 Anthropic 规范格式，客户端无需做分叉处理。
6. **配置单源** — 所有可变配置集中在 `config.json`，运行时状态集中在 `state.db`，业务日志按月分库在 `logs/`。禁止再出现散落的 `oauth.json`、`.ids.json`。

## 技术栈

- Python 3.11+
- FastAPI + uvicorn（HTTP 层）
- httpx（上游客户端，连接池）
- xxhash（CCH 签名）
- sqlite3（state + logs，WAL 模式）
- 原生 stdlib 实现 TG 长轮询（httpx.Client）

## 监听与部署

- 默认端口 `18082`（cc-proxy 是 18081，避免冲突，两者可并存）
- 部署脚本 `deploy.sh`（systemd，模仿 cc-proxy 风格）
- 迁移期建议 cc-proxy 保持运行，anthropic-proxy 并行上线，验证稳定后切换下游。
