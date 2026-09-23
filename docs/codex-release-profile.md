# Codex 发布档案：rust-v0.157.0-alpha.10

本次按“最新已发布版本（含 alpha）”选择 `0.157.0-alpha.10`，不是 npm stable `0.156.0`，也不是 main 的占位版本。权威 tag commit：`2170d8b3c77883dbe743078fb8bbb017f27caa9c`。

## 来源与复核

`src/openai/codex_profiles/rust-v0.157.0-alpha.10.json` 记录模型目录 `codex-rs/models-manager/models.json`、Cargo 版本、请求构建、模型参数解析、metadata 源文件的 SHA-256；每个新档案模型另有规范化源记录 hash 与 instructions 原始 UTF-8 hash。运行时按 hash 校验 instructions。

使用该 tag 的完整本地源码（不会读取运行配置或联网）：

```sh
python3 scripts/codex_release_profile.py /path/to/codex-rust-v0.157.0-alpha.10 --check
```

脚本逐字节重建并比较 profile 与 11 个 instructions 文件。去掉 `--check` 才会更新仓库档案。源路径和 HEAD 都必须对应该发布版。

最新目录的 11 个模型全部逐项提取；旧目录独有的 `gpt-5.4-mini` / `gpt-5.2` 保留 `rust-v0.153.4` 明示基线（来源 tag 与基线文件 hash 单独记录），不冒充最新版目录内容。基线不是账户授权/模型枚举；实际可用名单仍来自认证账户目录。

## 请求行为

- 新增 `gpt-6-sol` / `gpt-6-luna`：最低客户端 `0.155.0`，Lite，默认 `medium` reasoning / `low` verbosity。Sol 支持 `ultra`，Luna 最高 `max`。
- 11 个模型使用发布目录的 literal `model_messages.instructions_template`，不做已被上游废弃的 personality 插值；Astra instructions 经复核与旧 tag 逐字节相同，其余模型补齐此前缺失的档案 instructions。
- `ultra` 不是原生 wire effort。最新 profile 启用 `ModelInfo::resolve_reasoning_effort` 的回退策略，按账户目录优先的有效模型能力解析：Astra 为 `xhigh`，缺省 multi-agent effort 的 Sol 等为 `max`（如有效档位不含 max，则取最后一个非 ultra 档位；均无则 medium）。只做请求归一化，不运行多 Agent orchestrator。
- 账户目录显式 `supportVerbosity=false` 时删除 verbosity，保留 structured output；`supportsReasoningSummaryParameter=false` 时删除 summary。支持 summary 的模型对 `summary=none` 按官方方式省略该字段。旧 pin 中未声明能力仍保持旧透传边界。
- Lite 继续使用 `additional_tools` + developer instructions prefix、确定性 ID、`reasoning.context=all_turns`，顶层省略 instructions/tools，关闭 parallel tool calls；非 Lite 保留传统形状。下游显式 instructions 与官方已有 prefix/WS continuation 优先。
- 最新请求 struct 仍不支持 temperature/top_p/penalties/max tokens/cache retention；继续按 profile 剥离。WS beta 仍为 `responses_websockets=2026-02-06`，Lite HTTP header 与 WS metadata key 均未变。
- installation/thread/session/turn/window 核心 metadata 未变。不会凭空生成 analytics/MCP attribution/Cloud/Apps 数据；现有 client_metadata 的非身份字段继续透传。`configuration_update`/persistent 模式不自动注入。

## 升级与 pin

默认配置加载通过 `current.json` 将旧配套版本/profile 迁移并写回。UA/version 保留完整 `0.157.0-alpha.10` 发布身份；`/models?client_version=` 按官方 `whole_client_version()` 使用 `0.157.0`（去 prerelease/build），不是另一种发布身份。已有 installation UUID 和凭证不旋转。显式 `codexProfileAutoUpdate=false` 保留 `0.153.4` / `rust-v0.153.4`；旧 profile 与 instructions 不修改。版本门槛比较遵循 SemVer：alpha 不满足同核正式版，但满足较旧正式版。

只有 OpenAI 模型目录成功同步 TTL 为五分钟；Claude/xAI/Cursor/Antigravity/WorkBuddy 保留六小时。OpenAI 目录 schema 2 保存 query 版本；旧 schema 强制一次完整拉取，避免 ETag/304 永久保留遗漏数据，后续继续条件缓存。失败重试、额度缓存、refresh/revoke 不在本档案变更范围。

## 请求/发现复核修正

- Lite 的显式 `none`/`required` 保留字符串约束，`none` 清空可用工具；指定函数编译为“仅保留指定定义 + required”，不再改成 auto。已有 Lite prefix 一并重算工具 ID。带 previous_response_id 的指定函数需重发完整请求：官方遇到 prefix 变化也放弃增量，单凭 delta 不能证明旧工具集撤销；明确拒绝而不静默放宽。
- 认证目录的 `visibility=hide` 模型保留到账号可寻址集合，但不进入已有模型选择/同步预览界面；static profile 仅提供缺省能力，不赋予账号授权。
- discovery 允许持久化 literal instructions 和显式 null/empty defaults，HTTP/WS 都按字段是否存在覆盖 profile。instructions 缺失才回退 profile；显式空字符串禁止回填。`defaultInstructions` 仍是 profile-first 的部署 fallback，不提升成强制覆盖。
- `service_tier=flex` 是官方无需模型目录广告的例外：仅目录真实列出时状态为 `advertised`，未列出（含目录未知）时为 `permitted`，请求 preflight 均允许。不改写 raw catalog，也不在管理 UI 或 metadata override 中虚构可选档位。其他非空 tier 继续校验目录。unsupported max_output_tokens 等继续剥离，不改成报错。

## 最初调查所列的其他请求面

依据同一已验证 tag 的本地源码，不再次联网选版本：

- **WS prewarm/复用/turn_state**：Parrot 已保留 `generate=false`、previous_response_id delta 和同一 native WS 上的连续 create；已有 turn/owner scoped 的响应 header/event 捕获与回放。这些是代理必须正确保留的协议状态，并非可因“不完整客户端”排除的部分。自动主动预热、跨下游连接建上游池不是最新模型调用的硬门槛，本次不新增。官方 `core/src/client.rs:1381–1413,1931–1971` 明确区分预热、full 与 incremental。
- **HTTP zstd**：Parrot 当前不主动压缩请求。官方 `core/src/client.rs:1581–1589` 按 enable_request_compression、Codex auth、OpenAI provider 三重条件选择 Zstd/None；这是可关闭的传输优化，不是 Lite 或最新模型的强制 wire 门槛，不伪造 content-encoding 或增依赖。
- **metadata mcp_attribution**：官方 `core/src/responses_metadata.rs:360–363` 仅在 include_internal 且有 attribution 时写入，并限制 16 KiB。Parrot 没有凭真实 MCP 执行构建 attribution 的产品上下文，不应合成；已有调用方 client_metadata 非身份键在 HTTP/WS 继续透传。缺省无此字段不是最新模型不可调用的证据。
