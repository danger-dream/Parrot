# 12 — 实施里程碑

按模块独立可验证拆分。每个里程碑完成后应有可运行的增量，避免"全部做完再测"。

## 0 — 贯穿所有里程碑的硬约束：OAuth 不做主动测试

> ⛔ **开发方（Claude）在任何里程碑的验收中，严禁对真实 OAuth 账号（`api.anthropic.com`）发起测试请求。**
>
> 重复的 OAuth 登录与高频同模式调用可能触发 Anthropic 风控。cc-proxy 已在生产环境稳定运行，OAuth 链路行为已被覆盖验证；本项目的 OAuth 相关验证**全部采用离线字节级对比**（见 docs/05 §5.4），不实际调用远端。
>
> 所有里程碑的"验收"部分凡涉及 OAuth 的，都特指：
> - 功能代码完整、逻辑清晰、接口合理
> - 离线 fixture 比对通过
> - 单元测试通过（使用 mock 或 fixture，不触网）
>
> **真实 OAuth 请求的验证由用户（您）在完成交付后自行执行。**
>
> 不受此约束的部分：
> - cc-proxy 自身已经产出的日志 / fixture（只读消费）
> - API 渠道（第三方 Coding Plan 云厂商），可做真实测试请求
> - 纯本地逻辑（fingerprint、scorer、cooldown、affinity、log_db 等）

## M1 — 基础框架与下游入口

**目标**：启动 FastAPI，支持 `/v1/messages`，下游 API Key 验证，请求落库（但不做上游转发）。

**交付**：
- `config.py`：加载/保存/热加载 config.json，默认模板
- `auth.py`：API Key 验证
- `errors.py`：标准错误响应工具
- `state_db.py`：全部表结构 + CRUD 接口
- `log_db.py`：全部表结构 + CRUD 接口（包括 retry_chain）
- `server.py`：FastAPI 骨架、生命周期、/v1/messages 返回 "not implemented" JSON 并写 log
- `deploy.sh`：systemd 安装脚本（复刻 cc-proxy，改名）
- `requirements.txt`：fastapi, uvicorn, httpx, xxhash
- 首次启动生成 `.anthropic_proxy_ids.json`

**验收**：
- `curl -H "x-api-key: ccp-xxx" http://localhost:18082/v1/messages -d '{}'` 返回 501 not_implemented 格式正确
- `state.db` 和 `logs/YYYY-MM.db` 正确创建
- TG Bot 不启动（token 为空时跳过）

## M2 — CC 伪装移植 + 渠道抽象

**目标**：把 cc-proxy 的所有转换代码逐字移植到 `src/transform/cc_mimicry.py`，实现 `Channel` 抽象。

**交付**：
- `src/transform/cc_mimicry.py`：移植清单（docs/05）的全部符号
- `src/transform/standard.py`：`cc_mimicry=False` 的标准转换
- `src/channel/base.py`：`Channel` 抽象类
- `src/channel/oauth_channel.py`：OAuthChannel，依赖 `oauth_manager`
- `src/channel/api_channel.py`：ApiChannel，含 `parse_models_input`
- `src/channel/registry.py`：集中注册 + 重建
- `src/oauth_manager.py`：`ensure_valid_token` / `force_refresh` / `fetch_usage` / `fetch_profile` / `add_account` / `delete_account` / `set_enabled`
- 复制 cc-proxy 的 `oauth.json` 条目到 config（手动；仅用于离线签名验证）
- **离线对比测试**：`tests/compare_transform.py` 用 cc-proxy 历史日志 fixture，验证 OAuthChannel 生成的上游 body 与 cc-proxy 逐字节一致（**不对 api.anthropic.com 发起任何真实请求**）

**验收**（全部在本地离线完成）：
- 对比测试通过（签名/headers/body 全一致）— 纯本地
- `registry.rebuild_from_config()` 能正确构造 2 种渠道类型 — 纯本地
- OAuth token < 5min 过期时触发刷新逻辑分支 — **用 mock 验证 `ensure_valid_token` 的判断路径，不实际调 token endpoint**
- 不启动服务、不发真实流量

## M3 — 调度器（筛选+亲和+评分）

**目标**：能把请求路由到正确的渠道（但还不实际调上游）。

**交付**：
- `src/fingerprint.py`：`fingerprint_query` / `fingerprint_write`
- `src/affinity.py`：内存+state.db 双层
- `src/scorer.py`：完整的 EMA/滑动窗口/评分/排序/探索
- `src/cooldown.py`：错误阶梯冷却
- `src/scheduler.py`：`schedule()` 函数
- **单元测试**：
  - fingerprint 对称性：query(N) == write(N-1)
  - 评分：陈旧衰减、滑动窗口边界
  - 亲和打破：threshold 逻辑

**验收**：
- 单元测试全过
- `schedule(body, ...)` 能返回正确的 (channel, model) 列表
- 探索率生效（20% 的情况下非最优排到首位）

## M4 — 上游调用 + 故障转移

**目标**：能成功代理请求，具备完整故障转移。

**交付**：
- `src/upstream.py`：httpx AsyncClient 管理、四段超时实现
- `src/failover.py`：`run_failover` 主循环
- `src/blacklist.py`：首包黑名单
- `ResponseWriter` 抽象（`src/upstream.py` 或 `failover.py` 内）
- `SSEUsageTracker` + `SSEAssistantBuilder`
- `server.py` 的 `/v1/messages` 完整实现

**验收**（仅通过 API 渠道或 mock 验证，不触 OAuth 远端）：
- 单 API 渠道：cc_mimicry=True / False 都能工作（对真实第三方 Coding Plan 可以测）
- 多 API 渠道场景（全部用 API 渠道搭建，或用本地 mock server 搭建）：
  - 主渠道 down → 自动切到次渠道
  - 主渠道首包返回错误 JSON → 切换
  - 主渠道 idle timeout → 返回流内 error event
  - 亲和绑定生效（同会话两次请求走同一渠道）
- OAuth 渠道的代码分支通过 **mock httpx client** 单元测试覆盖（故障转移的代码路径验证，不发真实请求）
- OAuth 与 API 混合场景的真实验证，**由用户在完成交付后自行进行**

## M5 — 探测、恢复、配额监控

**目标**：后台自动化，不需要人工干预。

**交付**：
- `src/probe.py`：单次 probe + 带进度的 probe
- 后台任务循环（docs/11）：
  - wal_checkpoint_loop
  - stale_pending_cleanup_loop
  - affinity_cleanup_loop
  - oauth_proactive_refresh_loop
  - oauth_quota_monitor_loop
  - cooldown_probe_loop

**验收**：
- 手动把某 API 渠道 baseUrl 改错 → 请求失败 → 进入 cooldown → 改回正确 → 后台 probe 自动清除 cooldown（**可用真实 API 渠道测试**）
- OAuth 配额监控、自动禁用/恢复逻辑：**用 mock 的 `fetch_usage` 返回（含各档位利用率）验证状态机**，不实际调 `/api/oauth/usage`
- OAuth token 主动刷新：**用 mock 验证"剩余 < 10min 时走刷新分支"的判断逻辑**，不真实刷新
- 所有涉及 OAuth 远端的 loop 测试必须可以通过 `DISABLE_OAUTH_NETWORK_CALLS=1` 环境变量禁用，开发期默认禁用

## M6 — TG Bot 基础 + API Key 管理

**目标**：能通过 TG Bot 登入管理面板。

**交付**：
- `src/telegram/bot.py`：长轮询、admin 验证、回调路由
- `src/telegram/ui.py`：公共组件（inline_kb、编辑消息、分页）
- `src/telegram/states.py`：user_state 管理
- `src/telegram/menus/main.py`：主菜单
- `src/telegram/menus/apikey_menu.py`：API Key 管理（加、删、二次确认）

**验收**：
- `/start` 正常显示面板
- 新增/删除 API Key 后立即生效（config 热加载）
- 非 admin 收到拒绝消息

## M7 — TG Bot OAuth 管理

**目标**：能在 TG Bot 中管理多 OAuth。

**交付**：
- `src/telegram/menus/oauth_menu.py`：
  - 列表视图（含用量展示）
  - 账户详情
  - PKCE 登录
  - 手动设置 JSON
  - 刷新 token / 刷新用量 / 启停 / 删除（二次确认）
  - 刷新全部用量

**验收**（开发方仅做代码完整性 + 纯本地/mock 验证，**不运行 PKCE 真实流程**）：
- PKCE URL 构造、state/verifier 生成、code 解析的代码路径覆盖（用 mock 验证）
- 手动设置 JSON 的解析、校验、`add_account` 入 config 的本地流程
- 账户详情渲染（用本地构造的 usage dict 演示）
- 删除账户后 `state_db` 级联清理（perf/error/affinity/quota）
- PKCE 真实登录流程、刷新 token、刷新用量、启停 **由用户在最终部署时自行验证**

## M8 — TG Bot 渠道管理 + 测试面板

**目标**：能完整管理第三方 API 渠道。

**交付**：
- `src/telegram/menus/channel_menu.py`：
  - 列表视图（含总用量从当月 log.db 聚合）
  - 渠道详情（按模型 per-row 展示状态）
  - 添加向导（4 步）
  - 测试面板（单模型 / 全部 / 跳过，带进度编辑）
  - 编辑子菜单（修改名称/URL/Key/模型/CC伪装）
  - 删除（二次确认 + state.db 级联）
  - 清除错误（渠道级/全局）
  - 清空亲和绑定（渠道级/全局）

**验收**：
- 添加一个真实渠道（如智谱）全流程走通，测试面板进度更新正常
- 重命名渠道后，`state.db` 的 `perf_stats` / `channel_errors` / `cache_affinities` 全部跟着改名
- 删除渠道后 state.db 无遗留

## M9 — TG Bot 统计 + 日志

**目标**：可观测性完备。

**交付**：
- `src/telegram/menus/stats_menu.py`：时间范围 × 分组维度（4×4）
- `src/telegram/menus/logs_menu.py`：列表 + 详情（含重试链）
- `log_db.py` 的跨月聚合查询支持

**验收**：
- 统计汇总的数字与手工 SQL 查询一致
- 按渠道/模型/Key 分组结果正确
- 重试链在详情页清晰展示每次尝试的 outcome

## M10 — TG Bot 系统设置

**目标**：可在不 SSH 改文件的前提下调整所有关键参数。

**交付**：
- `src/telegram/menus/system_menu.py`：
  - 超时设置
  - 错误阶梯
  - 评分参数
  - 亲和参数
  - CCH 模式
  - 首包黑名单（default / byChannel）
  - channel 选择模式（smart / order）

**验收**：
- 改完任一设置 → 下一次请求立即生效（热加载）
- 参数验证：超时非数字、阶梯含负数 → 报错并保留旧值

## 迁移与上线

完成 M4 后即可作为 cc-proxy 的"单渠道平替"跑起来做对比验证：
1. 把 cc-proxy 的 oauth.json 内容作为一条 `oauthAccounts` 放入 anthropic-proxy/config.json
2. 在同一台机器上两个端口并行运行（18081 vs 18082）
3. 部分下游 API Key 流量切到 18082 做验证
4. 稳定 1-2 周后全面切换，cc-proxy 停止

上线后若发现异常回滚：
- 把下游流量指回 18081
- 保留 anthropic-proxy 日志以供分析

## 开发建议

1. **M2 的移植测试是关键点**：即使其他所有代码有 bug，只要 CC 伪装一致，OAuth 链路不会出问题。这一步做扎实。
2. **M4 的故障转移是最复杂的一块**：流式 + 首包锁 + 超时四态，建议用 mock 上游（`pytest-asyncio` + `httpx.MockTransport`）做全面的单元测试。
3. **M3 的 fingerprint 对称性必须有单元测试保证**：这是亲和正确性的基石。
4. 所有涉及 `config.save()` 的路径都要考虑 **写入原子性**（tmp + os.replace），避免部分写入导致 JSON 损坏。
5. TG Bot 开发时先把 menus 的文本模板固定，再接通真实 state.db 数据；避免同时调试两侧。

## 风险项

| 风险 | 缓解 |
|---|---|
| CC 伪装移植走样 → OAuth 账号封禁 | M2 的对比测试逐字节验证；保留 cc-proxy 作为 rollback |
| 亲和指纹算法错误 → 缓存失效成本 | fingerprint 单元测试；上线后观察 `cache_read_tokens` 占比变化 |
| 多渠道并发时的热加载竞态 | `config.save()` 后原子 replace；`registry.rebuild_from_config()` 是幂等的 |
| state.db 长期膨胀 | affinity 定期清理；perf_stats 无永久数据（受滑动窗口限制） |
| TG Bot 长消息被截断 | 所有菜单检查长度并标注"已截断"；复杂数据分页 |
| 首包文本黑名单误杀 | 默认空；用户自行配置；记录到日志便于审查 |
