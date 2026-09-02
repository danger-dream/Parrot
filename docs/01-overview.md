# 01 — 总览与目录结构

## 1.1 项目定位

**anthropic-proxy** 是对 cc-proxy 的完整重构，从"单 OAuth 账号中转"升级为"多渠道（OAuth + 第三方云平台 Coding Plan）智能调度 + 故障转移"的 Anthropic API 代理。

### 与 cc-proxy 的关系

- **cc-proxy 保持不变**，作为历史版本保留，随时可回滚。
- **anthropic-proxy 是新项目**，目录：`/opt/src-space/anthropic-proxy/`。
- **CC 伪装按实证 wire 基线维护**（见文档 05）；当前协议关键行为对齐 Claude Code v2.1.258，旧 cc-proxy 仅作历史回滚参考。
- fresh install 默认端口 `22122`；已有配置显式设置的其他端口保持不变。

## 1.2 功能对比

| 功能 | cc-proxy | anthropic-proxy |
|---|---|---|
| 下游入口 | `/v1/messages` | `/v1/messages` |
| 下游 API Key 验证 | ✅ | ✅ |
| 单 OAuth 账号 | ✅ | ✅（作为一种渠道） |
| 多 OAuth 账号 | ❌ | ✅ |
| 第三方云 Coding Plan | ❌ | ✅ |
| 模型别名映射 | ❌ | ✅（`真实名:别名`） |
| 会话亲和绑定 | ❌ | ✅ |
| 智能评分排序 | ❌ | ✅ |
| 错误冷却阶梯 | ❌ | ✅ |
| 故障转移 | ❌ | ✅ |
| 上游四段超时 | 部分 | 完整（连接/首字/空闲/总） |
| 首包黑名单拦截 | ❌ | ✅ |
| OAuth 配额自动禁用/恢复 | ❌ | ✅ |
| 后台 probe 恢复 | ❌ | ✅ |
| TG Bot 渠道管理 | ❌ | ✅ |
| TG Bot 多维统计 | 基本 | 按渠道/模型/Key |
| 日志重试链 | ❌ | ✅ |
| 配置热加载 | ✅ | ✅ |
| 按月分库日志 | ✅ | ✅（保留） |
| 统一状态存储 | ❌ | ✅（JSON 快照 + 旧库迁移） |

## 1.3 目录结构

```
anthropic-proxy/
├── DESIGN.md                         # 方案索引（本目录根）
├── docs/                             # 分章节设计文档
│   ├── 01-overview.md
│   ├── 02-config-schema.md
│   ├── 03-database.md
│   ├── 04-channel-abstraction.md
│   ├── 05-cc-mimicry.md
│   ├── 06-scheduler.md
│   ├── 07-failover.md
│   ├── 08-oauth-multi.md
│   ├── 09-tgbot.md
│   ├── 10-error-protocol.md
│   ├── 11-background-tasks.md
│   └── 12-milestones.md
│
├── config.json                       # 唯一配置文件（含所有 OAuth 账号、渠道、密钥、调参）
├── runtime-cache.json                # 可重建运行时状态
├── durable-state.json                # 必须持久化状态
├── state-migration.json              # 旧库迁移清单
├── state.db                          # 只读保留的旧版迁移源
├── openai_response_store.db          # previous_response_id history（TTL 清理）
├── logs/
│   ├── 2026-04.db                    # 当月业务日志
│   └── 2026-05.db                    # 跨月自动切换
├── .anthropic_proxy_ids.json         # device_id 持久化（随机 32 字节 hex）
│
├── server.py                         # FastAPI 入口：uvicorn + 生命周期
├── requirements.txt
├── deploy.sh                         # systemd 部署脚本
│
└── src/
    ├── __init__.py
    ├── config.py                     # 配置加载/保存（带 mtime 缓存）
    ├── auth.py                       # 下游 API Key 验证
    ├── errors.py                     # Anthropic 标准错误响应
    │
    ├── state_store.py                # JSON 快照、事务与单实例锁
    ├── state_migration.py            # WAL/hot-journal 安全旧库迁移
    ├── state_db.py                   # 后端中立业务状态接口
    ├── channel_state.py              # 运行期渠道改名的配置/DB/内存协调锁
    ├── sqlite_errors.py              # SQLite 可用性错误分类（不吞编程/Schema 错误）
    ├── log_db.py                     # 按月日志库的所有读写接口
    │
    ├── fingerprint.py                # 会话亲和指纹（query / write 两个函数）
    ├── affinity.py                   # 亲和表（内存 + 持久化）
    ├── scorer.py                     # 滑动窗口 EMA + 评分 + 探索率
    ├── cooldown.py                   # 错误阶梯冷却
    ├── scheduler.py                  # 主调度：筛选→亲和→排序
    │
    ├── transform/
    │   ├── __init__.py
    │   ├── cc_mimicry.py             # v2.1.258 fixture-backed CC wire mimicry
    │   ├── cache_breakpoints.py      # cache_control 统一管理（始终生效）
    │   └── standard.py               # 非 CC 伪装的标准 Anthropic 转换
    │
    ├── channel/
    │   ├── __init__.py
    │   ├── base.py                   # Channel 抽象基类
    │   ├── oauth_channel.py          # OAuth 渠道：token 管理 + CC 伪装链路
    │   ├── api_channel.py            # 第三方 API 渠道：模型别名、cc_mimicry 开关
    │   └── registry.py               # 所有渠道的集中注册与查询
    │
    ├── upstream.py                   # 上游调用：连接/首字/空闲/总超时 + SSE 解析
    ├── failover.py                   # 故障转移主循环
    ├── blacklist.py                  # 首包文本黑名单匹配
    ├── probe.py                      # API 渠道探测（添加渠道测试 + 后台恢复）
    ├── oauth_manager.py              # 多 OAuth 账户 token 刷新、用量拉取、配额监控
    │
    └── telegram/
        ├── __init__.py
        ├── bot.py                    # 长轮询主循环 + 路由分发
        ├── ui.py                     # 公共 UI 组件（按钮、消息、分页）
        ├── states.py                 # 用户输入状态机（TTL 管理）
        └── menus/
            ├── __init__.py
            ├── main.py               # 主菜单
            ├── oauth_menu.py         # 管理 OAuth（含 PKCE 登录）
            ├── channel_menu.py       # 渠道管理（含添加向导 + 测试面板）
            ├── apikey_menu.py        # API Key 管理
            ├── stats_menu.py         # 统计（多维度）
            ├── logs_menu.py          # 日志查看
            └── system_menu.py        # 系统设置（超时/阶梯/黑名单/CCH）
```

## 1.4 模块依赖

```
server.py
  ↓
FastAPI /v1/messages
  ↓
auth → fingerprint → scheduler → failover → upstream
                     ↓   ↑         ↑         ↑
             registry ← scorer  cooldown  transform
                          ↓                    ↓
                        state_db          cc_mimicry
                          ↓                    ↓
                     log_db       (channel/base → oauth/api)
```

底层基础模块（被多处依赖）：
- `config.py`：所有其他模块读取配置的单一入口
- `state_store.py` / `state_db.py`：scorer / cooldown / affinity / oauth_manager 的统一状态后端
- `log_db.py`：failover / server.py 的请求日志写入
- `errors.py`：所有出错路径的返回格式构造器

## 1.5 运行时生命周期

启动阶段（`lifespan` 中）：
1. 加载 config.json，验证必填字段
2. 校验状态路径、取得单 writer 锁、加载 JSON，并按 manifest 迁移旧 state.db 文件集
3. 初始化当月 log.db、清理 stale pending
4. 加载内存缓存：perf_stats、error_state、affinity
5. 构造 httpx 共享 AsyncClient
6. 启动后台任务循环：
   - wal_checkpoint_loop（每 5min）
   - stale_pending_cleanup_loop（每 5min）
   - affinity_cleanup_loop（每 5min）
   - oauth_proactive_refresh_loop（每 60s，刷 10min 内即将过期的 token）
   - oauth_quota_monitor_loop（每 60s，查用量，≥ 95% 禁用，恢复自动启用）
   - cooldown_probe_loop（每 30s，对 cooldown 中的 API 渠道模型做 probe）
7. 启动 Telegram Bot（如已配置）

关闭阶段：
1. 取消所有后台任务
2. 关闭 httpx client
3. 严格刷写并关闭 JSON StateStore；随后关闭日志、图片和独立 Store 等数据库连接，并按各自策略完成 checkpoint

## 1.6 与 cc-proxy 的迁移关系

用户自行处理（不做自动迁移）：
1. 启动 anthropic-proxy（首次生成空 `config.json` 模板）
2. 打开 `config.json`，手动将 cc-proxy 的 `oauth.json` 内容作为一条记录追加到 `oauthAccounts` 数组
3. 手动填入下游 API Key（可从 cc-proxy 的 config.json 拷贝）
4. 重启服务

cc-proxy 继续运行直至 anthropic-proxy 验证稳定。
