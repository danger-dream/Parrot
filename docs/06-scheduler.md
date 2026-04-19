# 06 — 调度器

调度器是请求路由的核心，实现"可用渠道筛选 → 会话亲和 → 智能排序 → 顺序故障转移"四层决策。

## 6.1 调度入口

`src/scheduler.py`：

```python
def schedule(body: dict, api_key_name: str, client_ip: str) -> ScheduleResult:
    """
    同步：纯内存 + sqlite 读，无 I/O 阻塞点。
    返回 ScheduleResult(candidates, fp_query, affinity_hit)。
    调用方（async 的 failover.run_failover）按 candidates 顺序做故障转移。
    candidates 为空时表示 503 no_channels。
    """
```

## 6.2 第一层 — 可用渠道筛选

```python
def _filter_candidates(requested_model: str) -> list[tuple[Channel, str]]:
    out = []
    now_ms = int(time.time() * 1000)
    for ch in registry.all_channels():
        if not ch.enabled:
            continue
        if ch.disabled_reason:   # user / quota / auth_error
            continue
        resolved = ch.supports_model(requested_model)
        if resolved is None:
            continue
        # 冷却检查
        cd = cooldown.get_state(ch.key, resolved)
        if cd and cd["cooldown_until"] and (cd["cooldown_until"] == -1 or cd["cooldown_until"] > now_ms):
            continue
        out.append((ch, resolved))
    return out
```

筛选条件：
1. 渠道 `enabled=True` 且 `disabled_reason is None`
2. 渠道声明支持该模型（OAuth 直接匹配真实名；API 按 alias 查表）
3. `(channel_key, resolved_model)` 不在冷却中（`channel_errors.cooldown_until > now` 或 `-1` 永久）

## 6.3 第二层 — 会话亲和

### 6.3.1 指纹计算

`src/fingerprint.py`：

```python
import hashlib
import json

def _canon(msg_obj) -> str:
    """消息对象的 canonical JSON（稳定 key 排序）。"""
    return json.dumps(msg_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def fingerprint_query(api_key_name: str, client_ip: str, messages: list) -> str | None:
    """
    请求到达时计算查询 key：去掉最后一条（当前 user turn），取剩下的最后两条。
    """
    if not messages:
        return None
    truncated = messages[:-1]
    if len(truncated) < 2:
        return None
    last_two = truncated[-2:]
    raw = f"{api_key_name}|{client_ip}|{_canon(last_two[0])}|{_canon(last_two[1])}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

def fingerprint_write(api_key_name: str, client_ip: str, messages: list, assistant_response: dict) -> str | None:
    """
    响应完成时计算写入 key：把本次产生的 assistant 回复追加到 messages，取最后两条。
    """
    full = messages + [assistant_response]
    if len(full) < 2:
        return None
    last_two = full[-2:]
    raw = f"{api_key_name}|{client_ip}|{_canon(last_two[0])}|{_canon(last_two[1])}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
```

**时序对称保证**：
- 第 N 次请求到达 → `query = hash(api|ip|msg[-3]|msg[-2])` = `hash(api|ip|u_{N-1}|a_{N-1})`
- 第 N-1 次响应写入 → `write = hash(api|ip|u_{N-1}|a_{N-1})`（因为 `u_{N-1}` 是当时的最后一条 user，追加 `a_{N-1}` 后取最后两条）
- 两者相等 ✅

### 6.3.2 assistant_response 的构造

流式路径：在 SSE 解析器中累积 `content_block_start/delta/stop` 事件，组装成一个完整的 `{"role": "assistant", "content": [...blocks]}` 对象。累积逻辑见 `docs/07-failover.md` 的 `SSEAssistantBuilder`。

非流式路径：直接从响应 JSON 的 `content` 字段构造 `{"role": "assistant", "content": response["content"]}`。

### 6.3.3 亲和匹配逻辑

```python
async def _apply_affinity(
    candidates: list[tuple[Channel, str]],
    fp_query: str | None,
    cfg: dict,
) -> tuple[list[tuple[Channel, str]], bool]:
    """
    返回重排后的 candidates 与 affinity_hit 标志。
    """
    if not fp_query or len(candidates) <= 1:
        return candidates, False

    bound = affinity.get(fp_query)
    if not bound:
        return candidates, False

    # 找到候选列表中匹配绑定的索引
    idx = None
    for i, (ch, model) in enumerate(candidates):
        if ch.key == bound["channel_key"] and model == bound["model"]:
            idx = i
            break

    if idx is None:
        # 绑定的渠道:模型已不在候选（可能禁用或冷却），亲和失效但不删除记录
        # 等待渠道恢复时再命中
        return candidates, False

    # 打破检查：绑定 vs 最优 分数
    best_score = scorer.get_score(candidates[0][0].key, candidates[0][1])
    bound_score = scorer.get_score(bound["channel_key"], bound["model"])
    threshold = cfg["affinity"]["threshold"]

    if bound_score > best_score * threshold:
        # 太差，打破绑定
        affinity.delete(fp_query)
        return candidates, False

    # 命中：把绑定渠道顶到首位
    if idx != 0:
        candidates.insert(0, candidates.pop(idx))
    affinity.touch(fp_query)
    return candidates, True
```

## 6.4 第三层 — 智能排序

`src/scorer.py`（完整移植 openai-proxy 的 `scorer.js` 到 Python）：

### 6.4.1 评分公式

```python
def calculate_score(stats: dict) -> float:
    if not stats or stats["total_requests"] == 0:
        return DEFAULT_SCORE  # 3000

    recent_total = min(stats["recent_requests"], RECENT_WINDOW)  # 50
    if recent_total > 0:
        recent_success_rate = min(stats["recent_success_count"], recent_total) / recent_total
    else:
        recent_success_rate = stats["success_count"] / stats["total_requests"]

    latency = stats["avg_connect_ms"] + stats["avg_first_byte_ms"]
    error_penalty = 1 + (1 - recent_success_rate) * ERROR_PENALTY_FACTOR  # 8
    score = latency * error_penalty

    # 陈旧衰减（15 min 开始 → 30 min 完全回归 3000）
    stale_ms = now_ms() - stats["last_updated"]
    stale_min = stale_ms / 60000
    if stale_min > STALE_MINUTES:
        progress = min((stale_min - STALE_MINUTES) / (STALE_FULL_DECAY_MINUTES - STALE_MINUTES), 1.0)
        score = score * (1 - progress) + DEFAULT_SCORE * progress

    return score
```

### 6.4.2 排序 + 探索

```python
def sort_by_score(candidates, exploration_rate) -> list:
    if len(candidates) <= 1:
        return candidates
    sorted_list = sorted(
        candidates,
        key=lambda ca: get_score(ca[0].key, ca[1]),
    )
    if exploration_rate > 0 and random.random() < exploration_rate:
        target = _pick_explore_target(sorted_list)
        if target is not None and target != 0:
            item = sorted_list.pop(target)
            sorted_list.insert(0, item)
    return sorted_list

def _pick_explore_target(sorted_list):
    """
    优先级：
      - 未测过 → 最高（priority = 1_000_000）
      - 最近窗口次数少且陈旧 → (stale_min + 1) / (recent_count + 1)
    """
    best_idx, best_priority = None, -1
    for i, (ch, model) in enumerate(sorted_list):
        stats = get_stats(ch.key, model)
        if not stats or stats["total_requests"] == 0:
            priority = 1_000_000
        else:
            recent_count = min(stats["recent_requests"], RECENT_WINDOW)
            stale_min = (now_ms() - stats["last_updated"]) / 60000
            priority = (stale_min + 1) / (recent_count + 1)
        if priority > best_priority:
            best_priority, best_idx = priority, i
    return best_idx
```

### 6.4.3 记录（成功/失败）

```python
def record_success(channel_key, model, connect_ms, first_byte_ms, total_ms):
    # 1. totalRequests++ / successCount++
    # 2. 滑动窗口：未满则 recent_requests / recent_success_count++；
    #    满了则用"滑出旧平均成功率 + 滑入一次成功"等效 EMA
    # 3. EMA 更新 avg_connect_ms / avg_first_byte_ms / avg_total_ms（α=0.25）
    # 4. lastUpdated = now_ms()
    # 5. state_db.perf_save

def record_failure(channel_key, model, connect_ms):
    # 1. totalRequests++
    # 2. 滑动窗口：未满则 recent_requests++ 但 recent_success_count 不变；
    #    满了则用"滑出旧平均成功率 + 滑入一次失败"
    # 3. 仅当 totalRequests>1 且 connect_ms 非 None 时更新 avg_connect_ms
    # 4. lastUpdated = now_ms()
```

## 6.5 第四层 — 顺序故障转移

```python
def schedule(body: dict, api_key_name: str, client_ip: str) -> ScheduleResult:
    """同步函数：纯内存 + state.db 读，无 I/O 阻塞点。"""
    requested_model = body.get("model")
    if not requested_model:
        return ScheduleResult([], None, False)

    candidates = _filter_candidates(requested_model)
    if not candidates:
        return ScheduleResult([], None, False)

    fp_query = fingerprint.fingerprint_query(api_key_name, client_ip, body.get("messages") or [])

    cfg = config.get()
    mode = (cfg.get("channelSelection") or "smart").lower()
    if mode == "smart":
        candidates = scorer.sort_by_score(candidates)
    # "order" 模式：按 registry 注册顺序（即 config 中定义顺序）

    candidates, affinity_hit = _apply_affinity(candidates, fp_query, cfg)
    return ScheduleResult(candidates, fp_query, affinity_hit)
```

server.py 调用方后续单独记 `log_db.update_pending(request_id, affinity_hit=...)`。
调度器返回的 ScheduleResult 交给 `failover.run_failover`（async）顺序尝试。

## 6.6 关键参数一览（来自 config）

| 参数 | 默认 | 作用 |
|---|---|---|
| `scoring.emaAlpha` | 0.25 | 延迟 EMA 平滑 |
| `scoring.recentWindow` | 50 | 滑动窗口大小 |
| `scoring.defaultScore` | 3000 | 未测/陈旧 默认分 |
| `scoring.errorPenaltyFactor` | 8 | 失败率惩罚倍数 |
| `scoring.staleMinutes` | 15 | 开始衰减的阈值 |
| `scoring.staleFullDecayMinutes` | 30 | 完全回归默认分 |
| `scoring.explorationRate` | 0.2 | 探索率 |
| `affinity.ttlMinutes` | 30 | 亲和 TTL |
| `affinity.threshold` | 3.0 | 打破绑定的分数倍数 |
| `errorWindows` | [1,3,5,10,15,0] | 错误阶梯（分钟） |

## 6.7 TG Bot 可干预的点

- **清空亲和绑定**：`affinity.delete_all()` 或 `affinity.delete_by_channel(key)`
- **清除错误**：`cooldown.clear(channel_key, model=None)`
- **重置性能统计**（不暴露于 UI，但作为 CLI 工具保留）：`scorer.clear_stats(...)`
- **切换 channelSelection**：`order` / `smart` （写入 config.channelSelection）
