# Codex 发布档案：rust-v0.169.0

当前选择正式发布版 `0.169.0`，权威 tag commit：`d32063f96c2dff1dc96af839b3952abb6acfeda4`。本次解决旧客户端版本（`0.159.0`）低于 `0.166.0` 门槛导致认证账户目录未下发 `gpt-6.2-sol` / `gpt-6.2-astra` 的问题，并同步对齐 `0.169.0` 的标准 Responses 指令前缀协议。

## 来源与复核

`src/openai/codex_profiles/rust-v0.169.0.json` 记录模型目录 `codex-rs/models-manager/models.json`、Cargo 版本、请求构建、模型参数解析、metadata 源文件的 SHA-256；每个新档案模型另有规范化源记录 hash 与 instructions 原始 UTF-8 hash。运行时按 hash 校验 instructions。

使用该 tag 的完整本地源码（不会读取运行配置或联网）：

```sh
python3 scripts/codex_release_profile.py /path/to/codex-rust-v0.169.0 --check
```

脚本逐字节重建并比较 profile 与 12 个随档案保存的 instructions 文件（保留既有档案结构和兜底策略，不将 CLI 文案变化当作新模型接入前提）。去掉 `--check` 才会更新仓库档案。源路径和 HEAD 都必须对应该发布版。

发布目录的 12 个模型（含已进入内置目录的 `gpt-6.1-sol` / `gpt-6.1-terra`）逐项提取。以此前 `rust-v0.159.0` 档案作为兼容基线：保留被新内置目录移除的 `gpt-5.4`（来源 `rust-v0.157.0-alpha.10`），并保留 `gpt-5.4-mini` / `gpt-5.2` 的原始 `rust-v0.153.4` 来源，不改写旧文件。基线不是账户授权/模型枚举。`gpt-6.2-sol` 与 `gpt-6.2-astra`（最低客户端版本 `0.166.0`）不在静态发布目录中，由认证账户目录动态发现、保存能力并路由，无需硬编码名单。

## 请求行为

- 模型发现端点、Responses Lite 形状、WS beta 和 reasoning 档位解析与 `gpt-6.2-sol` / `gpt-6.2-astra` 直接兼容。两款模型实时目录提供 Lite、最低客户端 `0.166.0`、默认 `low`、`low/medium/high/xhigh/max/ultra` 及 `multi_agent_reasoning_effort="xhigh"`；具体能力以每个账户的最新目录为准。
- 保留既有档案的 literal `model_messages.instructions_template` 数据结构，不改注入策略：下游显式 instructions/system、已有前缀和 WS continuation 优先；缺省时才执行现有兜底。
- `ultra` 不是原生 wire effort。最新 profile 启用 `ModelInfo::resolve_reasoning_effort` 的回退策略，按账户目录优先的有效模型能力解析：`multi_agent_reasoning_effort` 有效的模型（如 Astra、`gpt-6.1-sol`、`gpt-6.2-sol`）映射为 `xhigh`，缺省该字段的模型回退为 `max`（如有效档位不含 max，则取最后一个非 ultra 档位；均无则 medium）。只做请求归一化，不运行多 Agent orchestrator。
- 账户目录显式 `supportVerbosity=false` 时删除 verbosity，保留 structured output；`supportsReasoningSummaryParameter=false` 时删除 summary。支持 summary 的模型对 `summary=none` 按官方方式省略该字段。旧 pin 中未声明能力仍保持旧透传边界。
- Lite 继续使用 `additional_tools` + developer instructions prefix、确定性 ID、`reasoning.context=all_turns`，顶层省略 instructions/tools，关闭 parallel tool calls。`0.169.0` 起标准（非 Lite）Responses 请求也通过 `protocol.instructionsInInput=true` 对齐上游 `c9253c4977`：将 base instructions 移入 `msg_<uuid5>` developer input 前缀并省略顶层 `instructions`，同时保留顶层 `tools`；旧 pin 档案（`0.153.4`、`0.157.0-alpha.10`、`0.159.0`）保持顶层 `instructions` 原状不变。
- `0.169.0` 默认开启 `incremental_tools`（上游 `6326163b9a`）：工具目录随历史发送，空工具列表不再生成 `additional_tools` 前缀。官方客户端的 Lite 请求因此以 base instructions 消息开头，Parrot 把它与 `additional_tools` 开头同样视为权威前缀，不再补空工具项或重复注入 profile 指令；Parrot 自建前缀在无工具时只有 `msg_` 指令项（`protocol.omitEmptyLiteToolsPrefix`）。旧 pin 仍发送空 `additional_tools`。
- 请求体按官方 serde 声明顺序序列化（上游 `ed0cc1a4ab`，`protocol.routingFieldsFirst`）：HTTP 以 `model`、`stream`、`service_tier` 开头再接 `input`，WS `response.create` 以 `type` 开头；其余未知字段保持原相对顺序排在后面。
- `prompt_cache_options`（上游 `41cbd5b9b2`）在最新 profile 下透传，只保留 `ttl` 为 `30m` / `12h` 的官方形状，其它内容剥掉；旧 pin 没有该字段策略，仍整体剥掉。
- WS 上游超过 1 MiB 的文本消息按不超过 1 MiB 的帧分片发送（上游 `d15bf1deb7`，`protocol.responsesWebsocketMaxFrameBytes`），分片落在 UTF-8 字符边界，消息内容不变；小消息与旧 pin 照旧整帧发送。
- 最新请求 struct 仍不支持 temperature/top_p/penalties/max tokens/cache retention；继续按 profile 剥离。WS beta 仍为 `responses_websockets=2026-02-06`，Lite HTTP header 与 WS metadata key 均未变。HTTP/WS 握手请求头的组成与 `0.159.0` 相同，只有版本号与 User-Agent 变化。
- installation/thread/session/turn/window 核心 metadata 未变。不会凭空生成 analytics/MCP attribution/Cloud/Apps 数据；现有 client_metadata 的非身份字段继续透传。`configuration_update`/persistent 模式不自动注入。

## 升级与 pin

默认配置加载通过 `current.json` 将旧配套版本/profile 迁移并写回。UA/version 与 `/models?client_version=` 在此正式版均使用 `0.169.0`；对于保留的 alpha pin，query 仍按官方 `whole_client_version()` 去除 prerelease/build。已有 installation UUID 和凭证不旋转。显式 `codexProfileAutoUpdate=false` 保留用户选择的配套旧版（包括 `0.153.4`、`0.157.0-alpha.10` 和 `0.159.0`）；旧 profile 与 instructions 不修改。版本门槛比较遵循 SemVer：alpha 不满足同核正式版，但满足较旧正式版。

只有 OpenAI 模型目录成功同步 TTL 为五分钟；Claude/xAI/Cursor/Antigravity/WorkBuddy 保留六小时。OpenAI 目录 schema 2 保存 query 版本；旧 schema 强制一次完整拉取，避免 ETag/304 永久保留遗漏数据，后续继续条件缓存。失败重试、额度缓存、refresh/revoke 不在本档案变更范围。

源码自更新和 Docker 均携带 `src/openai/codex_profiles/`，更新重启后首次加载持久化新配套版本；无需用户手改 config。版本/profile 与旧同步记录不符时立即判为需刷新，并且不发送旧版本 ETag。同步成功后账户目录、渠道可用名单与模型中心会按现有路径更新。升级测试覆盖新旧配置层、默认/自动跟随/显式 pin、凭证和 installation UUID 保留、仍在 TTL 内的旧缓存刷新，以及远端新模型 HTTP/WS 工具调用请求。

## Ultrafast 与 Ultra

`service_tier="ultrafast"` 是速度/服务档位，不是 `reasoning.effort="ultra"`。Parrot 已解析账户 service_tiers、透传 HTTP/WS 请求及 routing hint，并在模型详情和日志显示 Ultrafast；只有账户目录列出的档位才可选。现有 Fast 强制开关仍表示 `priority`，不擅自改成 Ultrafast。对一个账户只返回 priority 的观察不能推广为其他账户也无 Ultrafast。

## 请求/发现复核修正

- Lite 的显式 `none`/`required` 保留字符串约束，`none` 清空可用工具；指定函数编译为“仅保留指定定义 + required”，不再改成 auto。已有 Lite prefix 一并重算工具 ID。带 previous_response_id 的指定函数需重发完整请求：官方遇到 prefix 变化也放弃增量，单凭 delta 不能证明旧工具集撤销；明确拒绝而不静默放宽。
- 认证目录的 `visibility=hide` 模型保留到账号可寻址集合，但不进入已有模型选择/同步预览界面；static profile 仅提供缺省能力，不赋予账号授权。
- discovery 允许持久化 literal instructions 和显式 null/empty defaults，HTTP/WS 都按字段是否存在覆盖 profile。instructions 缺失才回退 profile；显式空字符串禁止回填。`defaultInstructions` 仍是 profile-first 的部署 fallback，不提升成强制覆盖。
- `service_tier=flex` 是官方无需模型目录广告的例外：仅目录真实列出时状态为 `advertised`，未列出（含目录未知）时为 `permitted`，请求 preflight 均允许。不改写 raw catalog，也不在管理 UI 或 metadata override 中虚构可选档位。其他非空 tier 继续校验目录。unsupported max_output_tokens 等继续剥离，不改成报错。

## 最初调查所列的其他请求面

以下既有兼容边界继续保留；数字形式 reasoning effort 可原生透传，新增 response.interrupt 由原生 WS 控制帧路径透传。它们不要求对普通新模型请求添加字段；跨协议/HTTP 回退不能据此宣称支持原生 WS 中断语义：

- **WS prewarm/复用/turn_state**：Parrot 已保留 `generate=false`、previous_response_id delta 和同一 native WS 上的连续 create；已有 turn/owner scoped 的响应 header/event 捕获与回放。这些是代理必须正确保留的协议状态，并非可因“不完整客户端”排除的部分。自动主动预热、跨下游连接建上游池不是最新模型调用的硬门槛，本次不新增。官方 `core/src/client.rs` 明确区分预热、full 与 incremental。
- **HTTP zstd**：Parrot 当前不主动压缩请求。官方 `core/src/client.rs` 按 enable_request_compression、Codex auth、OpenAI provider 三重条件选择 Zstd/None；这是可关闭的传输优化，不是 Lite 或最新模型的强制 wire 门槛，不伪造 content-encoding 或增依赖。
- **metadata mcp_attribution**：官方 `core/src/responses_metadata.rs` 仅在 include_internal 且有 attribution 时写入，并限制 16 KiB。Parrot 没有凭真实 MCP 执行构建 attribution 的产品上下文，不应合成；已有调用方 client_metadata 非身份键在 HTTP/WS 继续透传。缺省无此字段不是最新模型不可调用的证据。
