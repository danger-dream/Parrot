# Model pricing snapshot

`model_prices_and_context_window.json` 是 LiteLLM 模型价格目录的本地精简快照，供远端价格源不可用时兜底。

- 上游：<https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json>
- 数据许可：沿用 LiteLLM 仓库许可
- 运行时缓存：`$ANTHROPIC_PROXY_DATA_DIR/model_pricing.json`

Parrot 启动后会按 `pricing.sourceUrl` / `pricing.refreshHours` 异步刷新；刷新失败不会覆盖本地可用目录，也不会影响代理请求。
