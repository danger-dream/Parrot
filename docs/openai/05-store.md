# 05 — `previous_response_id` 本地存储

Responses API 是**有状态**的：客户端可以用 `previous_response_id` 续接历史，服务端按 id 拼出完整对话。

我们支持两条路径：

- **同协议（responses → openai-responses 上游）**：透传 `previous_response_id` 给上游即可，上游自己状态化。Proxy 可选也本地存储（做家族内切换时的兜底）。
- **跨变体（responses → openai-chat 上游）**：上游无状态，必须 proxy 侧本地 store 展开历史，翻译成 messages 前缀。

实现：`src/openai/store.py`。

## 5.1 存储表（挂在既有 `state.db` 里，命名空间隔离）

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

为什么挂 `state.db` 而不是新开一个库：
- 状态数据库的运维逻辑（WAL checkpoint、备份）已经成熟
- openai-专属的表名前缀隔离，不污染 Anthropic 表

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

def cleanup_expired(now: float | None = None) -> int: ...
"""返回清理数。每次 ~几十 ms。"""
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

## 5.6 TTL 与清理

- 默认 `openai.store.ttlMinutes = 60`
- 每次 save 时写 `expires_at = now + ttl`
- 后台 `cleanup_expired` 循环每 `openai.store.cleanupIntervalSeconds`（默认 300）跑一次
- 清理任务挂在 `server.py` lifespan 的 `_background_tasks`（见 [07-anthropic-touchpoints.md](./07-anthropic-touchpoints.md)）

## 5.7 并发与隔离

- 写入用 `state_db` 现有的 `_write_lock`（RLock）复用
- 读取无锁（SQLite WAL 模式已够）
- `api_key_name` 字段用来防误读：Key A 看不到 Key B 的 response_id（即使碰撞）

## 5.8 `conversation` 资源

首版**不实现** `conversation` 对象。请求带 `conversation` 字段时：
- 同协议（上游也是 responses）→ 透传
- 跨变体（上游 chat）→ `guard.responses_to_chat` 拒绝 400 `conversation not supported when upstream is chat`

## 5.9 体量

`store.py`：约 180 行（接口 + SQL + 清理）。一张表的 CRUD，复杂度低。
