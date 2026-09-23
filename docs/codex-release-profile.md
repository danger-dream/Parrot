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
- installation/thread/session/turn/window 核心 metadata 未变。新增的 analytics/MCP attribution/Cloud/Apps 等是产品功能，不伪造、不复制；不新增后台遥测或任务编排。`configuration_update`/persistent 模式并非此代理的会话能力，不自动注入。

## 升级与 pin

默认配置加载通过 `current.json` 将旧配套版本/profile 迁移并写回，UA、version、`/models?client_version=` 同源。已有 installation UUID 和凭证不旋转。显式 `codexProfileAutoUpdate=false` 保留 `0.153.4` / `rust-v0.153.4`；旧 profile 与 instructions 不修改。版本门槛比较遵循 SemVer：alpha 不满足同核正式版，但满足较旧正式版。

只有 OpenAI 模型目录成功同步 TTL 为五分钟；Claude/xAI/Cursor/Antigravity/WorkBuddy 保留六小时。失败重试、额度缓存、refresh/revoke 不在本档案变更范围。
