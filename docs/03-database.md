# 03 — 数据库设计

三套独立的 SQLite 库：

- **state.db** — 运行时状态，**永久保留**（性能统计、错误冷却、亲和、配额缓存）
- **openai_response_store.db** — OpenAI `previous_response_id` history（TTL 清理）
- **logs/YYYY-MM.db** — 业务日志，**按月分库**（请求流水、重试链、完整 body）

各库均启用 WAL 模式。OpenAI history 与 `state.db` 分库，避免大表清理长时间
占用轻量状态写锁；旧版 `state.db.openai_response_store` 只用于升级后的只读 fallback。

## 3.1 state.db Schema

```sql
-- ─── 性能统计（滑动窗口 EMA） ───────────────────────
CREATE TABLE IF NOT EXISTS performance_stats (
  channel_key         TEXT NOT NULL,     -- "oauth:<email>" 或 "api:<name>"
  model               TEXT NOT NULL,     -- 上游真实模型名
  total_requests      INTEGER DEFAULT 0,
  success_count       INTEGER DEFAULT 0,
  recent_requests     INTEGER DEFAULT 0, -- ≤ recentWindow（50）
  recent_success_count INTEGER DEFAULT 0,
  avg_connect_ms      REAL DEFAULT 0,
  avg_first_byte_ms   REAL DEFAULT 0,
  avg_total_ms        REAL DEFAULT 0,
  last_updated        INTEGER NOT NULL,  -- Unix ms
  PRIMARY KEY (channel_key, model)
);

CREATE INDEX IF NOT EXISTS idx_perf_updated ON performance_stats(last_updated);

-- ─── 错误冷却 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS channel_errors (
  channel_key        TEXT NOT NULL,
  model              TEXT NOT NULL,
  error_count        INTEGER DEFAULT 0,  -- 连续失败次数（成功一次清零）
  cooldown_until     INTEGER,            -- Unix ms；-1 表示永久
  last_error_message TEXT,
  last_error_at      INTEGER,
  PRIMARY KEY (channel_key, model)
);

CREATE INDEX IF NOT EXISTS idx_cooldown ON channel_errors(cooldown_until);

-- ─── 会话亲和 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS cache_affinities (
  fingerprint  TEXT PRIMARY KEY,         -- sha256 hex 前 32 字节
  channel_key  TEXT NOT NULL,
  model        TEXT NOT NULL,
  last_used    INTEGER NOT NULL,         -- Unix ms
  created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_affinity_used ON cache_affinities(last_used);
CREATE INDEX IF NOT EXISTS idx_affinity_channel ON cache_affinities(channel_key);

-- ─── OAuth 配额缓存 ─────────────────────────────────
-- 后台 quota_monitor 每 60s 写入一次；TG Bot 渲染时直接读取（避免频繁拉远端）
CREATE TABLE IF NOT EXISTS oauth_quota_cache (
  email            TEXT PRIMARY KEY,
  fetched_at       INTEGER NOT NULL,   -- Unix ms
  five_hour_util   REAL,
  five_hour_reset  TEXT,               -- ISO
  seven_day_util   REAL,
  seven_day_reset  TEXT,
  sonnet_util      REAL,
  sonnet_reset     TEXT,
  opus_util        REAL,
  opus_reset       TEXT,
  extra_used       REAL,
  extra_limit      REAL,
  extra_util       REAL,
  raw_data         TEXT                -- 原始 JSON 字符串（兜底）
);
```

### state.db 的 API 层接口（`src/state_db.py`）

```python
# perf_stats
def perf_load_all() -> list[Row]
def perf_save(channel_key, model, stats: dict)
def perf_delete(channel_key=None, model=None)   # None 通配
def perf_rename_channel(old_key, new_key)

# errors (cooldown)
def error_load_all() -> list[Row]
def error_save(channel_key, model, error_count, cooldown_until, msg)
def error_delete(channel_key=None, model=None)
def error_rename_channel(old_key, new_key)

# affinity
def affinity_load_all() -> list[Row]
def affinity_upsert(fingerprint, channel_key, model, last_used)
def affinity_touch(fingerprint, last_used)
def affinity_delete(fingerprint=None)
def affinity_delete_by_channel(channel_key)
def affinity_rename_channel(old_key, new_key)
def affinity_cleanup(ttl_ms)

# oauth quota
def quota_save(email, data: dict)
def quota_load(email) -> Row | None
def quota_load_all() -> list[Row]
def quota_delete(email)

# coordinated live rename (performance/errors/fp/client/quota in one transaction)
def rename_runtime_channel_state(old_channel_key, new_channel_key, ...)
```

所有 state.db 写操作由单一 `_write_lock`（`threading.RLock`）序列化。运行期渠道改名不直接逐表调用这些接口，而由 `src/channel_state.py` 协调：配置写入和 reload、单个 SQLite 事务、scorer/cooldown/两类 affinity 内存发布共用同一生命周期锁；失败时在释放过渡 key 前恢复旧配置。这样 reload cleanup 不会把改名中的 old/new 状态误判为 stale，也不会留下只更新 DB 或只更新内存的中间态。

## 3.2 logs/YYYY-MM.db Schema

每月自动切换一个 DB 文件，文件名 `logs/YYYY-MM.db`（例如 `logs/2026-04.db`），使用北京时间（UTC+8）判断月份。

```sql
-- ─── 请求摘要 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS request_log (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id              TEXT UNIQUE NOT NULL,
  created_at              REAL NOT NULL,
  finished_at             REAL,
  client_ip               TEXT,
  api_key_name            TEXT,

  requested_model         TEXT,         -- 下游请求的 model（可能是 alias）
  final_channel_key       TEXT,         -- 最终成功的渠道；失败时为最后尝试的渠道
  final_channel_type      TEXT,         -- "oauth" | "api"
  final_model             TEXT,         -- 上游侧真实模型名

  status                  TEXT DEFAULT 'pending',  -- pending | success | error
  http_status             INTEGER,
  error_message           TEXT,         -- 完整错误信息（不截断）

  is_stream               INTEGER DEFAULT 1,
  msg_count               INTEGER DEFAULT 0,
  tool_count              INTEGER DEFAULT 0,

  input_tokens            INTEGER DEFAULT 0,
  output_tokens           INTEGER DEFAULT 0,
  cache_creation_tokens   INTEGER DEFAULT 0,
  cache_read_tokens       INTEGER DEFAULT 0,

  connect_time_ms         INTEGER,      -- 最终成功渠道的连接时长
  first_token_time_ms     INTEGER,      -- 最终成功渠道的首字时长
  total_time_ms           INTEGER,

  retry_count             INTEGER DEFAULT 0,
  affinity_hit            INTEGER DEFAULT 0,   -- 0/1
  fingerprint             TEXT          -- 本次请求的亲和指纹（前 16 字符即可）
);

CREATE INDEX IF NOT EXISTS idx_log_created ON request_log(created_at);
CREATE INDEX IF NOT EXISTS idx_log_status ON request_log(status);
CREATE INDEX IF NOT EXISTS idx_log_apikey ON request_log(api_key_name);
CREATE INDEX IF NOT EXISTS idx_log_channel ON request_log(final_channel_key);
CREATE INDEX IF NOT EXISTS idx_log_model ON request_log(requested_model);

-- ─── 请求/响应详情 ─────────────────────────────────
-- 大字段独立表，避免统计查询读入大文本
CREATE TABLE IF NOT EXISTS request_detail (
  request_id       TEXT PRIMARY KEY,
  request_headers  TEXT,                -- JSON（敏感头 *** 脱敏）
  request_body     TEXT,                -- JSON 完整（含 messages/tools）
  response_body    TEXT,                -- 完整 SSE 文本或 JSON 完整响应
  FOREIGN KEY (request_id) REFERENCES request_log(request_id)
);

-- ─── 重试渠道链 ────────────────────────────────────
CREATE TABLE IF NOT EXISTS retry_chain (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id      TEXT NOT NULL,
  attempt_order   INTEGER NOT NULL,     -- 1-based
  channel_key     TEXT NOT NULL,
  channel_type    TEXT NOT NULL,
  model           TEXT NOT NULL,
  started_at      REAL NOT NULL,
  connect_ms      INTEGER,
  first_byte_ms   INTEGER,
  ended_at        REAL,
  outcome         TEXT,                 -- success | http_error | connect_timeout |
                                        -- first_byte_timeout | idle_timeout | total_timeout |
                                        -- blacklist_hit | upstream_error_json |
                                        -- transport_error | closed_before_first_byte
  error_detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_retry_req ON retry_chain(request_id);
```

### log_db 的 API 层接口（`src/log_db.py`）

```python
# 写入
def insert_pending(request_id, client_ip, api_key_name, requested_model,
                   is_stream, msg_count, tool_count, fingerprint,
                   request_headers, request_body)

def record_retry_attempt(request_id, attempt_order, channel_key, channel_type, model,
                         started_at, connect_ms=None, first_byte_ms=None,
                         ended_at=None, outcome=None, error_detail=None)

def finish_success(request_id, final_channel_key, final_channel_type, final_model,
                   input_tokens, output_tokens, cache_creation, cache_read,
                   connect_ms, first_token_ms, total_ms,
                   retry_count, affinity_hit, response_body, http_status=200)

def finish_error(request_id, error_message, retry_count,
                 final_channel_key=None, final_channel_type=None, final_model=None,
                 connect_ms=None, first_token_ms=None, total_ms=None,
                 http_status=None, response_body=None)

# 维护
def cleanup_stale_pending(timeout_seconds=1800)
def checkpoint()

# 查询（TG Bot 使用）
def recent_logs(limit=20, channel_key=None, model=None, status=None)
def stats_summary(since_ts, group_by=None)   # group_by: None|"channel"|"model"|"apikey"
def cache_stats_by_channel(since_ts, limit=10)
def cache_stats_by_model(since_ts, limit=10)
def cache_stats_by_apikey(since_ts, limit=10)
def recent_cache_misses(since_ts, limit=10)
def retry_chain_of(request_id) -> list[Row]
```

## 3.3 按月切库的实现

`log_db.py` 维护：
- `_current_month`：当前月份 "YYYY-MM"
- `_local.conn`：thread-local 连接
- `_local.month`：该连接对应的月份

每次 `_get_conn()` 调用检查当前北京时间的月份是否变化：
- 未变化：返回已打开连接
- 变化了：关闭旧连接，打开新月份 DB，重建 schema（`CREATE IF NOT EXISTS`）

新请求在 `insert_pending()` 时绑定当时的月库，后续 request/retry/proxy/local-web/
attempt-usage 写入都携带同一个 `RowLogHandle`。因此跨过北京时间月界的长请求仍完整
落在开始月份，不会出现摘要在旧库、结算在新库的拆分；月界之后新建的请求才进入新库。

## 3.4 跨库数据聚合（TG Bot 统计）

默认只查询当月库。若用户选择更长时间范围（超过当月），`stats_summary(since_ts)`：

1. 计算起始月份到当前月份的所有 DB 文件名
2. 逐个打开（只读）查询
3. Python 侧聚合结果

目前 TG Bot 的时间范围选项是"今天/3天/7天/本月"，最多跨 1 个月边界（7 天可能跨月），按此规则支持即可。

## 3.5 状态数据 vs 业务日志的区分

| 数据 | 库 | 是否按月分片 | 生命周期 |
|---|---|---|---|
| 渠道性能统计 | state.db | 否 | 永久累积（滑动窗口） |
| 错误冷却 | state.db | 否 | 临时（cooldown 到期清除） |
| 亲和绑定 | state.db | 否 | TTL 30min |
| OAuth 配额缓存 | state.db | 否 | 实时覆盖写 |
| OpenAI response history | openai_response_store.db | 否 | TTL 60min（默认） |
| 请求流水 | logs/YYYY-MM.db | 是 | 默认永久保留；可由 `logRetention` 按天清理 |
| 重试链 | logs/YYYY-MM.db | 是 | 与所属请求流水同生命周期 |
| 上游尝试结算 | logs/YYYY-MM.db | 是 | 与所属请求流水同生命周期 |
| 请求/响应 body | logs/YYYY-MM.db | 是 | 与所属请求流水同生命周期 |

原则：**轻量状态数据需要快速读写且重启恢复**，放 state.db；体积可能很大的
OpenAI history 独立分库；**业务日志写多读少且数据量大**，按月分库便于归档与迁移。

当 `logRetention.mode="days"` 时，完整过期月份直接删除整库；留存临界所在月份会删除过期请求及 `request_detail` / retry / proxy / local-web / attempt-usage 关联行，再压缩 SQLite 以实际回收磁盘空间。TG Bot 必须经两次确认后才会保存该策略并执行首次清理；之后由后台维护循环每天最多检查一次。

## 按上游尝试结算

`request_log` 继续作为向后兼容的下游请求摘要：Token 字段只描述 Parrot 对下游
暴露的用量，不会改写为包含重试或故障转移的用量。费用单独结算到
`upstream_attempt_usage`；每次真实上游 dispatch 对应一条不可变、可幂等 finalize
的记录。dispatch 前发生的转换/guard 错误只留在 `retry_chain` 供排障，不作为账单事实。
`retry_chain.dispatched_at` 在传输层开始发送时立即落盘：若进程随后在 finalize 前退出，
聚合会把缺失结算的该次 dispatch 明确计为 `unpriced`，而不是回退请求摘要后漏算整次尝试。
HTTP/WS 代理链只允许在尚未开始发送时切换下一条 route；一旦请求可能离开 Parrot 就不在
同一 retry 行内重放，避免生成两次上游账单却只留下一个结算事实。

每条尝试结算保存规范化 Token、是否真实观察到完整 Token usage、该次尝试的上游协议、
响应/出站 service tier、dispatch 确定性、补全 provider 的模型、冻结后的实际费率快照及
版本，以及费用来源（`actual`、`estimated`、`unpriced`）。OpenAI 的 tier 优先级为
“响应实际 tier > 出站请求 tier > 未知”；Anthropic 的容量 `service_tier` 与 Claude
`speed=fast` 是两个维度，Fast 只认 dispatch 前冻结的出站 `speed=fast`。下游意图不是
账单事实。显式完整的全零 Token 字段属于已观察用量，估价为 0；只有 input 或 output
一侧的 partial usage、缺失 usage 或非法数值均不得把另一侧默认为 0。xAI 上游返回的
实际费用可以在 Token usage 缺失时结算；无法确认是否已发出的传输错误必须标为未计价。

重试、failover、本地 WebSearch 多轮和 Compact map/reduce 子调用分别结算。Compact
子调用 ID 只在聚合时映射回原下游请求。请求数仍描述下游请求，Token、金额与 family
归属描述每次真实上游尝试；两者数量不同时界面会明确显示“上游 N 次”。跨协议 failover
按每条尝试自己的协议归属 family，不能继承根请求最终成功协议。聚合以不可变尝试事实
为准；partial ledger 缺少最终成功尝试时保留 `request_log` 中唯一可用的最终 Token，
同时将缺失的 dispatch 费用标为未计价，避免 Token 被减成负数或静默漏账。
Responses WebSocket 可在同一物理连接中顺序发送多个 `response.create`；每个 create
仍是独立下游请求，分别创建 request、retry 和 attempt-usage 记录，连接复用不会把多轮
调用压成一次请求或一条账单。

月度数据库使用增量迁移：旧请求记录继续可读；新增请求摘要/尝试账本字段和索引时，
不会改写历史 Token 语义。
