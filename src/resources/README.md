# Model pricing snapshot

`model_prices_and_context_window.json` 是 LiteLLM 模型价格目录的本地精简快照，供远端价格源不可用时兜底。

- 上游：<https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json>
- 快照基线：LiteLLM commit [`8447cd3`](https://github.com/BerriAI/litellm/commit/8447cd3ad39817bee0cdf0b3272ff8d2a36f88a8)（2026-07-12），只保留 Parrot 常用模型的原始条目
- 数据许可：沿用 LiteLLM 仓库许可
- 运行时缓存：`$ANTHROPIC_PROXY_DATA_DIR/model_pricing.json`

Parrot 启动后会按 `pricing.sourceUrl` / `pricing.refreshHours` 异步刷新；响应大小限制为 16 MiB，解析、校验和原子落盘在线程中完成。刷新失败不会覆盖本地可用目录，也不会影响代理请求。
内置兜底目录还包含 Parrot 默认的 `xai/grok-4.5` Token 费率。裸模型名
`grok-4.5` 经确认的 xAI 渠道路由时，会先补全 provider 再解析价格；远端目录
刷新失败时也可正常计价。
