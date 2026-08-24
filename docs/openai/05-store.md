# 05 — `previous_response_id` 本地存储

Responses API 是**有状态**的：客户端可以用 `previous_response_id` 续接历史，服务端按 id 拼出完整对话。

我们支持两条路径：

- **同协议（responses → openai-responses 上游）**：透传 `previous_response_id` 给上游即可，上游自己状态化。Proxy 可选也本地存储（做家族内切换时的兜底）。
- **跨变体（responses → openai-chat 上游）**：上游无状态，必须 proxy 侧本地 store 展开历史，翻译成 messages 前缀。

实现：`src/openai/store.py`。

## 5.1 存储表（独立 SQLite）

默认写入 `DATA_DIR/openai_response_store.db`（可用
`openai.store.dbPath` 指定；相对路径仍以 `DATA_DIR` 为根）。它必须与
`stateDbPath` 不同，避免大 history 表的写入和清理阻塞评分、冷却与亲和状态。

```sql
CREATE TABLE IF NOT EXISTS openai_response_store (
  response_id      TEXT PRIMARY KEY,        -- "resp_xxx"
  parent_id        TEXT,                    -- previous_response_id（可空，链头）
  api_key_name     TEXT,                    -- 授权隔离
  model            TEXT,
  channel_key      TEXT,                    -- 本次落地的上游渠道（记录用，不是读写条件）
  created_at       REAL NOT NULL,
  expires_at       REAL NOT NULL,           -- created_at + ttlMinutes*60
  input_items      TEXT NOT NULL,           -- JSON：翻译阶段展开后的完整 input items 列表
  output_items     TEXT NOT NULL            -- JSON：本次响应产生的 output items 列表
);
CREATE INDEX IF NOT EXISTS idx_resp_store_expires ON openai_response_store(expires_at);
CREATE INDEX IF NOT EXISTS idx_resp_store_key     ON openai_response_store(api_key_name);
```

升级不再写 legacy `state.db`。StateStore 启动对完整旧文件集的私有副本完成检查后，OpenAI Store 以该源 revision 为 marker，把旧 `openai_response_store` 执行 `INSERT OR IGNORE` 导入独立库；旧版本回滚导致源指纹变化时会再导入一次。只有完整检查和导入成功才写 marker；旧表缺失或损坏只记录跳过，不阻断独立 Store。`response_id` 的 API Key owner 约束继续生效。

## 5.2 Store 接口

```python
# src/openai/store.py

@dataclass
class StoredResponse:
    response_id: str
    parent_id: str | None
    api_key_name: str
    model: str
    channel_key: str | None
    created_at: float
    expires_at: float
    input_items: list
    output_items: list


class ResponseNotFound(Exception): ...
class ResponseExpired(Exception): ...
class ResponseForbidden(Exception): ...   # key 不一致
class ResponseIdConflict(Exception): ...  # 写入 id 已属于另一个 key

def init() -> None: ...

def save(response_id: str, parent_id: str | None, *,
         api_key_name: str, model: str, channel_key: str | None,
         input_items: list, output_items: list,
         ttl_seconds: int | None = None) -> None: ...

def lookup(response_id: str, *, api_key_name: str) -> StoredResponse: ...
"""查不到抛 ResponseNotFound；过期抛 ResponseExpired；api_key_name 不匹配抛 ResponseForbidden。"""

def expand_history(response_id: str, *, api_key_name: str) -> list[dict]:
    """返回按链条展开的 items：最老的在前，`input_items + output_items` 拼起来。
       内部递归沿 parent_id 向上，直到 None 或命中循环（防御）。"""

def cleanup_expired(now: float | None = None, *,
                    batch_size: int | None = None,
                    max_batches: int | None = None,
                    batch_bytes: int | None = None,
                    time_budget_seconds: float | None = None) -> int: ...
"""返回清理数；每批独立短事务，并受行数、payload 字节数、批数和时间预算限制。"""
```

## 5.3 链展开算法

```python
def expand_history(response_id, *, api_key_name, max_depth=50):
    chain = []
    cur = response_id
    seen = set()
    depth = 0
    while cur and cur not in seen and depth < max_depth:
        seen.add(cur)
        rec = lookup(cur, api_key_name=api_key_name)
        chain.append(rec)
        cur = rec.parent_id
        depth += 1
    chain.reverse()   # 老 → 新
    items: list = []
    for rec in chain:
        items.extend(rec.input_items)
        items.extend(rec.output_items)
    return items
```

## 5.4 写入路径

`openai/handler.py` 在 failover 成功完成时（stream 全量完成 / 非流式返回后）把：
- 本次展开后的 `input_items`（即送给上游 chat 的翻译前中间态）
- 本次产出的 `output_items`（从响应解析得出）

调用 `store.save(new_resp_id, parent_id=body.get("previous_response_id"), ...)` 一次即可。

对于同协议 responses→responses 路径，proxy 拿不到精确的 `output_items`（因为直接透传），只能从 SSE 流中累积；或者干脆不写入（`openai.store.enabled=true` 但只有跨变体路径真正触发写入）。

**首版简化策略**：只在"跨变体"和"同协议但 `store.alwaysPersist=true`"时写入。默认只跨变体写入。

## 5.5 读入路径

`openai/transform/responses_to_chat.translate_request` 的 `_resolve_input` 里调 `store.expand_history`。

异常映射：
| 异常 | 返回状态 | 错误格式 |
|---|---|---|
| `ResponseNotFound` | 404 | `{error:{message:"response not found", type:"not_found_error"}}` |
| `ResponseExpired` | 410 | `{error:{message:"response expired", ...}}` |
| `ResponseForbidden` | 403 | `{error:{message:"response does not belong to this api key",...}}` |
| legacy/new Store 暂时不可用（BUSY/LOCKED/I/O/FULL） | 503 | `server_error`；不得伪装成 404 |

## 5.6 TTL 与清理

- 默认 `openai.store.ttlMinutes = 60`
- 每次 save 时写 `expires_at = now + ttl`
- 后台 `cleanup_expired` 循环每 `openai.store.cleanupIntervalSeconds`（默认 300）跑一次
- 每批候选最多 `cleanupBatchSize`（默认 100）条，并受
  `cleanupBatchBytes`（默认 8 MiB）约束；单条超大 history 仍会独立删除，
  避免单次事务制造超大 WAL
- 每批立即提交并释放 Store 写锁，让并发 save 有机会插队
- 每轮最多提交 `cleanupMaxBatches`（默认 100）批，并受
  `cleanupTimeBudgetSeconds`（默认 10 秒）约束；默认每轮最多可追赶
  10,000 条小记录，积压由后续轮次继续处理
- 清理任务挂在 `server.py` lifespan 的 `_background_tasks`（见 [07-anthropic-touchpoints.md](./07-anthropic-touchpoints.md)）

## 5.7 并发与隔离

- 独立库使用自己的 `_write_lock`（RLock）与 thread-local 连接，不和 JSON StateStore 锁耦合
- 读取无锁（SQLite WAL 模式已够）
- `response_id` 冲突更新带 owner 条件；只有原 `api_key_name` 才能更新同 id，
  另一个 Key 会得到 `ResponseIdConflict`，不会发生 `INSERT OR REPLACE` 跨租户覆盖
- legacy `state.db` 从不由 SQLite 直接打开；迁移器只对完整文件集的 0700 私有副本执行恢复和查询
- `save()` 失败会 rollback 后重新抛出，由协议收尾层限频告警，不把半开事务留在线程连接中
- `api_key_name` 字段用来防误读：Key A 看不到 Key B 的 response_id（即使碰撞）
- 默认容器入口使用 `umask 077` 并把 data 目录固定为 `0700`；Store 目录必须
  由当前进程 uid 持有且不可被 group/world 写入（源码部署兼容 owner 持有的
  `0755` 仓库根目录），数据库、`-wal`、`-shm` 必须是同 uid 的普通文件且
  mode 不宽于 `0600`。新数据库用显式 `0600` 原子预创建，不依赖调用方
  umask；已有路径若是 symlink、外部 owner 或文件权限过宽，启动会 fail closed
- WAL 配置 `journal_size_limit=64 MiB`，避免突发写入后长期保留无界高水位

升级后 legacy 表不再由当前版本写入或删除。为支持现实降级后再升级，确认不再回滚前应保留旧 `state.db` 及 sidecar；如需回收，必须在维护窗口离线备份并校验，不得在运行中执行 `VACUUM` 或大事务清理。

## 5.8 `conversation` 资源

首版**不实现** `conversation` 对象。请求带 `conversation` 字段时：
- 同协议（上游也是 responses）→ 透传
- 跨变体（上游 chat）→ `guard.responses_to_chat` 拒绝 400 `conversation not supported when upstream is chat`

## 5.9 体量

`store.py`：约 180 行（接口 + SQL + 清理）。一张表的 CRUD，复杂度低。
