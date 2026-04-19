# 11 — 后台定时任务

所有后台任务在 FastAPI `lifespan` 中启动，统一用 `asyncio.create_task` 管理，`finally` 中 cancel。

## 11.1 任务清单

| 任务 | 周期 | 模块 | 作用 |
|---|---|---|---|
| WAL checkpoint | 300s | `server.py` / `state_db` / `log_db` | 防 WAL 文件膨胀 |
| Stale pending 清理 | 300s | `log_db` | 清理进程崩溃遗留的 pending 记录 |
| Affinity 过期清理 | 300s | `affinity` | 清理 30min 以上未使用的亲和记录 |
| OAuth token 主动刷新 | 60s | `oauth_manager` | 剩余 < 10min 的 token 提前刷新 |
| OAuth 配额监控 | 60s | `oauth_manager` | 拉 usage，≥ 95% 禁用；恢复则启用 |
| Cooldown probe（API） | 30s | `probe` | 冷却中的 API 渠道模型做探测，成功则清除 |

## 11.2 WAL checkpoint

```python
async def wal_checkpoint_loop():
    while True:
        await asyncio.sleep(300)
        try:
            state_db.checkpoint()
        except Exception as e:
            print(f"[state_db] checkpoint failed: {e}")
        try:
            log_db.checkpoint()
        except Exception as e:
            print(f"[log_db] checkpoint failed: {e}")
```

`checkpoint()` 内部：`conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")`。

## 11.3 Stale pending 清理

```python
async def stale_pending_cleanup_loop():
    while True:
        await asyncio.sleep(300)
        try:
            await asyncio.to_thread(log_db.cleanup_stale_pending, 1800)  # 30min
        except Exception as e:
            print(f"[log_db] cleanup stale failed: {e}")
```

`cleanup_stale_pending(timeout_seconds)` SQL：
```sql
UPDATE request_log
SET status='error',
    error_message='process crashed (stale pending)',
    finished_at = unixepoch()
WHERE status='pending' AND created_at < (unixepoch() - ?)
```

## 11.4 Affinity 过期清理

```python
async def affinity_cleanup_loop():
    while True:
        await asyncio.sleep(300)
        try:
            cfg = config.get()
            ttl_ms = cfg["affinity"]["ttlMinutes"] * 60 * 1000
            affinity.cleanup(ttl_ms)
        except Exception as e:
            print(f"[affinity] cleanup failed: {e}")
```

内存 + state.db 同步清理（`affinity.py` 同时维护两者）。

## 11.5 OAuth token 主动刷新

已在 `docs/08-oauth-multi.md` §8.2.6 描述，简略重申：

```python
async def oauth_proactive_refresh_loop():
    await asyncio.sleep(30)  # 启动后等 30s
    while True:
        try:
            cfg = config.get()
            for acc in cfg.get("oauthAccounts", []):
                if not acc.get("enabled", True):
                    continue
                if acc.get("disabled_reason") == "user":
                    continue
                expired = _parse_iso(acc.get("expired"))
                if not expired:
                    continue
                remaining = (expired - datetime.now(timezone.utc)).total_seconds()
                if remaining < 600:
                    try:
                        await oauth_manager.force_refresh(acc["email"])
                        # 成功后拉一次 usage 一并通知 TG
                        usage = await oauth_manager.fetch_usage(acc["email"])
                        tgbot.notify_admins(
                            f"✅ Token refreshed: {acc['email']}\n"
                            f"New expired: {acc['expired']}\n\n"
                            f"{tgbot.format_usage(usage)}"
                        )
                    except Exception as e:
                        oauth_manager.set_enabled(acc["email"], False, reason="auth_error")
                        tgbot.notify_admins(f"⚠ OAuth refresh failed: {acc['email']}: {e}")
        except Exception as e:
            print(f"[oauth proactive] loop error: {e}")
        await asyncio.sleep(60)
```

失败重试：`force_refresh` 内部已是一次性调用；如需更健壮，可在循环里给 `force_refresh` 外套 3 次重试（10s、20s 间隔），与 cc-proxy 的行为一致。

## 11.6 OAuth 配额监控

```python
async def oauth_quota_monitor_loop():
    await asyncio.sleep(45)  # 启动后等 45s（避开主动刷新的第一轮）
    while True:
        try:
            cfg = config.get()
            if not cfg["quotaMonitor"]["enabled"]:
                await asyncio.sleep(60)
                continue
            threshold = cfg["quotaMonitor"]["disableThresholdPercent"]

            for acc in cfg.get("oauthAccounts", []):
                if acc.get("disabled_reason") == "user":
                    continue
                if acc.get("disabled_reason") == "auth_error":
                    continue
                try:
                    usage = await oauth_manager.fetch_usage(acc["email"])
                except Exception as e:
                    print(f"[quota] {acc['email']}: fetch failed: {e}")
                    continue

                state_db.quota_save(acc["email"], _flatten_usage(usage))

                utils = _extract_utils_percent(usage)  # list[float|None]
                any_over = any(u is not None and u >= threshold for u in utils)

                if any_over:
                    latest_reset = _latest_reset_iso(usage)
                    if acc.get("disabled_reason") != "quota":
                        oauth_manager.set_disabled_by_quota(acc["email"], latest_reset)
                        tgbot.notify_admins(
                            f"⚠ OAuth quota disabled: {acc['email']}\n"
                            f"resets_at: {latest_reset}"
                        )
                else:
                    if acc.get("disabled_reason") == "quota":
                        du = acc.get("disabled_until")
                        if du:
                            du_dt = _parse_iso(du)
                            if du_dt and du_dt > datetime.now(timezone.utc):
                                # 还没到 resets_at，保持禁用
                                continue
                        oauth_manager.set_enabled(acc["email"], True)
                        tgbot.notify_admins(f"✅ OAuth quota resumed: {acc['email']}")
        except Exception as e:
            print(f"[quota monitor] loop error: {e}")
        await asyncio.sleep(cfg["quotaMonitor"]["intervalSeconds"])
```

> 注：`_latest_reset_iso` 返回 `usage` 中各个窗口 `resets_at` 的最大值（ISO 字符串）。这样"disabled_until"保守地等到所有窗口都过期。

## 11.7 Cooldown probe（API 渠道自动恢复）

```python
async def cooldown_probe_loop():
    while True:
        try:
            cfg = config.get()
            if not cfg["cooldownRecovery"]["enabled"]:
                await asyncio.sleep(30)
                continue
            cooldowns = cooldown.get_active_entries()
            for entry in cooldowns:
                ch = registry.get_channel(entry["channel_key"])
                # 只探测 API 渠道
                if not ch or ch.type != "api" or not ch.enabled:
                    continue
                ok = await probe.probe_channel_model(
                    ch, entry["model"],
                    timeout_s=cfg["cooldownRecovery"]["timeoutSeconds"],
                )
                if ok:
                    cooldown.clear(ch.key, entry["model"])
                    print(f"[probe] cleared cooldown for {ch.key}:{entry['model']}")
        except Exception as e:
            print(f"[probe] loop error: {e}")
        await asyncio.sleep(cfg["cooldownRecovery"]["intervalSeconds"])
```

`probe.probe_channel_model` 构造如 `docs/02` 中的 `probe.request`：
```python
async def probe_channel_model(ch: ApiChannel, model: str, timeout_s: int) -> bool:
    cfg = config.get()
    body = {
        "model": None,  # 会由 build_upstream_request 替换
        "max_tokens": cfg["probe"]["maxTokens"],
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "user", "content": cfg["probe"]["userMessage"]}],
    }
    try:
        upstream_req = await ch.build_upstream_request(body, model)
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                upstream_req.url, headers=upstream_req.headers, content=upstream_req.body,
            )
            if resp.status_code != 200:
                return False
            obj = resp.json()
            if obj.get("type") == "error":
                return False
            return True
    except Exception:
        return False
```

`probe` **仅用于 API 渠道**；OAuth 渠道**永远不做** probe。

理由有两点：
1. **避免误消耗配额**：OAuth 账户的配额是与真实用量绑定的（5h/7d/Sonnet/Opus 利用率），probe 流量会计入配额。
2. **风控风险**：重复的、小 token、固定模式的探测请求可能被 Anthropic 识别为异常流量。cc-proxy 当前线上运行没有做 probe，anthropic-proxy 也必须保持这个行为。

OAuth 的"可用性恢复"路径完全依赖：
- `oauth_quota_monitor_loop`（见 §11.6）读取真实 usage 判断是否解除 quota 禁用
- `oauth_proactive_refresh_loop`（见 §11.5）刷新 token（这是**必要**的远端调用，但频率可控，且每个账号每 8h 才一次）

> **注**：开发方（Claude）在实现与验收阶段，对 OAuth 所有远端端点（`/v1/oauth/token`、`/api/oauth/profile`、`/api/oauth/usage`）的调用**一律用 mock 代替**，不向 `api.anthropic.com` 发送真实请求。真实联通性验证由用户在交付后自行进行。

## 11.8 添加渠道时的测试

也使用 `probe.probe_channel_model`，但超时用 `config.probe.timeoutSeconds`（默认 60s），结果用于 TG Bot 的测试面板展示。

进度与错误：
```python
async def probe_with_progress(ch, model, timeout_s, progress_callback):
    """
    每 10s 调用一次 progress_callback("调用时长超过 Xs...")；
    完成时 callback 结果。
    """
    t0 = time.time()
    progress_task = asyncio.create_task(_progress_tick(t0, progress_callback))
    try:
        ok = await probe_channel_model(ch, model, timeout_s)
        elapsed = int((time.time() - t0) * 1000)
        return ok, elapsed
    finally:
        progress_task.cancel()

async def _progress_tick(t0, cb):
    seconds = 10
    while True:
        await asyncio.sleep(seconds)
        cb(f"调用时长超过 {seconds}s...")
        seconds += 10
```

`progress_callback` 通过 TG editMessage 编辑原消息追加文本。注意 TG 有 editMessage 的频率限制，多个消息时相差 ≥ 1s 避免被限流。

## 11.9 任务启动代码

`server.py` `lifespan`：

```python
_tasks = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化
    db.init(...)
    ...

    # 启动所有后台任务
    loops = [
        wal_checkpoint_loop,
        stale_pending_cleanup_loop,
        affinity_cleanup_loop,
        oauth_proactive_refresh_loop,
        oauth_quota_monitor_loop,
        cooldown_probe_loop,
    ]
    for loop_fn in loops:
        _tasks.append(asyncio.create_task(loop_fn()))

    yield

    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    await _http_client.aclose()
```

所有循环都有 `try/except Exception` 包着，一个任务挂了不影响其他；但 `asyncio.CancelledError` 不捕获（允许 cancel 正常退出）。
