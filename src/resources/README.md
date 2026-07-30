# Model pricing snapshot

`models_dev_catalog.json.gz` 是 models.dev 两份公开目录的完整 gzip 组合快照，供远端价格源不可用时兜底：

- 供应商模型 ID 与 Token 价格：<https://models.dev/api.json>
- 规范模型身份与元数据：<https://models.dev/models.json>
- 快照时间：2026-07-30
- 项目主页：<https://models.dev>
- 上游源码：<https://github.com/sst/models.dev>
- 数据许可：MIT，版权与许可原文见 `models.dev.LICENSE`
- 运行时缓存：`$ANTHROPIC_PROXY_DATA_DIR/models_dev_catalog.json.gz`

Parrot 只从 `api.json` 的供应商模型条目读取 USD / 1M Token 价格；`models.json` 不含价格，仅用于确认规范模型 ID，并且只在映射唯一时建立裸模型别名。供应商限定的模型名始终优先，避免把同名模型误套到另一家供应商的价格。

启动后会按 `pricing.sourceUrl`、`pricing.modelsUrl` 和 `pricing.refreshHours` 异步刷新。两份响应必须同时下载、解析并通过规模校验，之后才会写入同一个 gzip 临时文件并原子替换缓存；任一来源失败都保留当前目录，不影响代理请求。
