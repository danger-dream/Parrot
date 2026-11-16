# Antigravity 客户端档案与 Go 传输后端

## 身份与兼容策略

内置身份固定为已分析的 Hub 2.32.1 / CL 1021296516 / linux-amd64：

```text
antigravity/hub/2.32.1 (aidev_client; os_type=linux; arch=amd64; cl=1021296516)
```

这是模拟的客户端身份，不随 Parrot 宿主架构自动改变，也不单独追踪 updater manifest 中的版本号。UA/版本/CL 由 `src/antigravity/client_profile.py` 统一维护。2.32.1 观察到的 ClientHello 未要求更换现有 Go TLS 档案，因此保留 `antigravity-ls-2.22.0` 的配置标识；这不代表 HTTP/2 或连接生命周期与官方完全一致。

升级加载配置时，仅与旧内置 UA `antigravity/hub/2.9.1 darwin/arm64`、完整 2.22.0 / CL 995734886 UA 和完整旧内置 scope 字符串精确相等的值被更新并持久化；有意固定为这些旧值的配置也会迁移。其他自定义 UA/scope、账户凭证、账户已有 scope、项目、模型选择、回调地址与显式 TLS 开关/profile 均保留。

加载已有配置文件时，若原文件未保存 TLS `profile`（包括仅配置 `enabled:true`），会补齐并持久化旧默认 `chrome131`，不会因 deep-merge 静默切到 Go。首次自动创建配置文件才使用新默认 `antigravity-ls-2.22.0`；两者均不自动开启 TLS 指纹。已有显式 profile 不会被覆盖，选择 LS 时首尾空白按生成面的规则统一去除后用于控制面选择。

OAuth token exchange/refresh 与 userinfo 固定使用 `Go-http-client/1.1`，不再采用自定义 `userAgent`；这与配置值被保留是两件事。Cloud Code 的 load、quota、目录和生成仍采用配置的 `userAgent`，未自定义时使用上面的 Hub UA。模型发现保留现有 prod/daily 合并逻辑，移除该查询的旧 `x-goog-api-client` 头。onboardUser 的官方新形态尚未确认，保留原有字段、轮询、node-client UA 后缀及 `X-Goog-Api-Client`，但 `metadata.ide_version` 随内置版本更新；自定义 `onboardUserAgent` 仍生效。

关闭 TLS 指纹或保留 `chrome131` **不等于回退整套旧协议行为**：

| 配置 | 控制面传输 | 生成面传输 |
|---|---|---|
| `enabled:false` | 原普通网络路径 | 原默认传输 |
| `enabled:true` + `chrome131` | 原普通网络路径，不使用 curl 指纹 | 保留旧 curl profile |
| `enabled:true` + LS profile | Go helper；失败不得静默回退普通 TLS | LS 传输 |

三种配置都会使用新登录 PKCE、新默认 scope、上述 token/userinfo UA、目录头变化和 quota project body。旧 curl 的兼容保留指的是传输路径，不代表所有请求头/请求体不变。启用 LS 后，自定义 endpoint 还需满足该后端 HTTPS/h2 等传输边界；保留配置不等于保证远端兼容。

生成信封的现有 ID 与会话策略保持不变；不自动添加 `userPromptId`、`enabledCreditTypes` 或 `continuation_token`。额度查询在 loadCodeAssist 返回 project 时携带该 project，不猜测项目值。

## 新登录与存量账户

TG 和管理 API 创建的新 Antigravity 登录会话都使用 PKCE S256，verifier 只保存在服务端待完成会话中。刷新已有 token 的请求不携带新的 scope，不主动重新登录，不撤销授权。

默认仍采用原来的 localhost 回调 URL 粘贴方式，完整校验 state。若明确配置：

```json
{
  "antigravityOAuth": {
    "redirectUri": "https://antigravity.google/oauth-callback"
  }
}
```

新官网回调会话可提交页面显示的裸授权码。该入口仅对绑定了 PKCE verifier 的官网会话开放；管理 API 还必须提交所属用户的 flowId/flowSecret，TG 绑定当前 chat 的一次性会话。完整 URL 提交仍必须携带正确 state，不能用裸 code 兼容入口放宽其他回调。旧登录会话不获得无 state 提交的权限。

新增 `aicode` 仅影响新授权申请，不会自动扩大存量 refresh token 的授权范围；真实 refresh 请求不发送 scope 或 PKCE verifier，仅在服务端返回新 scope 时更新本地授权记录。不要仅因旧 token 没有该 scope 就要求重登。

旧 pending 会话不含 verifier 时，仍只按旧流程接受带正确 state 的完整回调 URL，不补发 verifier，也不开放官网裸 code 豁免。TG 对 Unicode 等非法 state 返回不匹配，不会因此放宽校验；其原有一次性消费语义不变。

## 2.32.1 模型目录与参数

`disabled:true` 与 `*-disabled` 告知项不进入可路由模型；目录仍保留必要诊断字段。旧目录中的禁用项同样在渠道构建、模型选择与发送前校验中拒绝。一次成功的空目录表示当前没有可用文本模型，会撤销旧的可用清单，保留用户 `disabledModels` 等偏好，并遵守正常同步间隔；网络失败或损坏的目录不会替换上次成功结果。

目录元数据按账号、实际生成端点和精确模型键保存。`modelEnum` 来自上游 `model`，`thinkingLevel`、`maxOutputTokens`、`minThinkingBudget` 也保留在源端点观察中。不得从 prod 借用字段填 daily 请求；缺失字段保持未知。旧目录缺少完整标记时在正常同步流程中刷新，不在每次生成时额外请求目录。

Carbon 与 Hydrogen 使用精确目录键选择档位，冲突 effort 或无法映射的数字预算显式报错。生成侧采用有效端点的目录 thinkingLevel：已知枚举 1/2/3 对应 `LOW`/`MEDIUM`/`HIGH`，Hydrogen xhigh 的目录值 `100` 保持数值，不转为 high 或字符串 XHIGH。存在 level 时不同时补造 thinkingBudget；无目录元数据的手工模型不猜 level/budget。AG 文本渠道强制采用有效端点目录的 `maxOutputTokens`，覆盖 Responses `max_output_tokens`、Chat `max_completion_tokens` / `max_tokens` 和 Anthropic `max_tokens`，不论调用方值更小或更大。目录缺失或字段不是正整数时，采用官方 LS 已观察到的默认值 `16384`，不借用其他账号或端点的上限，也不根据模型名称猜值。此规则在文本三入口统一出站处生效，涵盖 Carbon、Hydrogen、Argon、Claude 及其他可用文本模型。摘要开关和档位选择保留；Claude 数值思考预算按强制后的输出上限执行既有的 `budget < maxOutputTokens` 约束，不再被调用方原来的小输出值提前削减。图片生成/编辑接口不应用这两项文本规则。

2026-11-16 有效 daily 目录中，Carbon Low/Medium/High 的 `maxOutputTokens` 均为 `262144`，`contextWindow` 均为 `1048576`。这是目录声明的能力，不是一次生成 262144 tokens 的实测保证。调用方传入 `max_output_tokens=32768` 时，这些模型实际发送强制的目录值 262144。较早版本曾保留调用方的32768限制，那些请求的 `MAX_TOKENS` 不能解释为 Carbon 仅支持32K输出。目录 `contextWindow` 只作能力元数据，不作为上下文大小声明传给上游。

这些参数证据来自官方 LS 在完整目录 mock 下实际构造的请求，不是 Google 服务端对本实现的真实验收。缺字段 mock 曾产生 16384/budget:0 的默认值，不能据此认为所有模型都使用该组合。

## 文本系统提示词前缀

所有 AG 文本入口在最终 `systemInstruction` 前强制添加同一段343字符短身份：

```text
<identity>
You are Antigravity, a powerful agentic AI coding assistant designed by the Google Deepmind team working on Advanced Agentic Coding.
You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.
</identity>
```

原请求系统提示词位于闭合的 `</identity>` 之后，以两个换行分隔；保留既有入口转换后的 system/developer 文本内容和顺序，不删除 OpenBear 等调用方身份，不把规则迁移到 user，不加入 `<USER_REQUEST>` 包装要求。原请求没有系统提示词时仅补短身份；首段已以前述完整短身份开头时不重复前置。改写只发生在新构造的出站对象，不写回调用方请求或会话存储，重试/续轮不会累积前缀。用户消息、工具定义与工具历史仍使用原转换路径。

图片接口保持原样，不添加编程助手身份，不强制文本输出预算；其他 provider 不受影响。

## 请求关联与 labels

文本生成使用 `agent/{cascade}/{epoch_ms}/{trajectory}/{step}` 格式的 requestId。生成轮次与步骤计数分开：新用户轮或工具续轮增加 round；同一逻辑请求的 HTTP 重试保留 round，每次构建上游尝试增加 step 并更新时间。`last_step_index = step - 1`。完成响应中的每个工具调用各占一个轮间步骤，普通文本完成占一个；双工具、文本、双工具的连续请求步骤为 `1→4→6→9`，不是固定 `2k+1`。

有可信枚举时，发送九个基础 labels；`model_enum` 只取当前账号、生成端点和精确模型的目录字段。枚举未知时不伪造完整 labels，但仍使用独立请求关联。`used_*` 三值保留受控顶层 Hub 样本观察到的 `false/false/true`，不将其解释为模型家族分类；不伪造样本偶见、只有官方 LS 执行上下文才能确定的 `last_execution_id`。

会话身份在缓存转换和其他渠道内部身份注入之前快照：明确的客户端 session_id / Claude Code session 优先；Responses 的 previous_response_id 仅在本地 Store 租户校验通过且响应确实保存后关联；没有这些身份时，调用方显式 prompt_cache_key 可作后备锚点。自动按内容生成或入口标记为注入的缓存键只用于缓存路由，不能合并会话，也不能覆盖明确的 session_id 或 previous_response_id。锚点组合 API key 身份、上游账号及端点后哈希隔离。没有可信租户或锚点时每请求新建会话，绝不以首句文本合并不同调用者；模型切换建立新的顶层关联，避免猜测跨模型 used_* 状态。重试复用请求级内部上下文，不能由客户端 JSON 伪造。内存缓存上限 4096 会话、8192 响应关联，空闲 TTL 一小时；重启、多进程间不恢复关联，历史内容仍由现有协议层处理。并发分配受锁保护，晚到的旧轮响应不覆盖新轮工具计数。

证据为官方 LS 接受正确 wrapped SSE 后的普通轮、单工具、双工具与 429→503→成功样本。早期裸 candidates mock 导致的空响应修复循环已剔除。超过两个并行工具按逐工具计数实现，但没有官方独立样本；子代理层级及跨模型内部状态不模拟。没有元数据、响应未完成或缓存失效时不声明与官方内部轨迹完全一致。图片继续使用原有 image_gen/requestType 格式，不挂文本 labels；文本系统提示词仅增加上文的短身份前缀，工具定义、工具历史和传输栈保持原策略。

## 拒绝与诊断

生成侧只以结构化错误证据区分配额、限流、验证挑战与请求拒绝。`VALIDATION_REQUIRED` 不进行 token 刷新或积分补救；只影响当前账号模型的有界冷却，保留其他账号可用性。明确的请求协议错误不继续在账号池重放。裸 HTTP400/422、错误体中的数字 code=400/422 与未知429均不能独立证明全池请求拒绝或封号；未知429也不触发积分补救。

HTTP200 JSON/SSE 中的 Google 错误由 decoder 保存为进程内受信观察，拒绝/验证/配额分类跨转换保留；不从客户端诊断字段或转换后的 SSE 文本反推分类。文本最终错误、已提交流的终态和图片错误出口使用同一套安全诊断：HTTP 状态、Google status、ErrorInfo reason/domain、RetryInfo，以及白名单内的标准生成失败码或服务端本地错误码。不复制提示词、验证链接、原始 message、任意错误 code 或元数据，空诊断字段不展示；内部受控响应记录保留。HTTP200 内的业务错误不等于请求成功，未收到 HTTP 响应也不能凭 decoder 存在推断为 200。

仅明确的 `INVALID_ARGUMENT` / `OUT_OF_RANGE` 称为请求参数拒绝。验证挑战、认证权限失败、配额耗尽、限流分别说明；未知 HTTP 错误不猜封号原因。本地 `upstream_stream_incomplete` 表示流结束但缺少完成原因，`timeout` / `transport_error` 表示超时或连接失败、结果未知，JSON/UTF-8/解码错误与本地请求准备错误分别说明，不再一概称为“Antigravity rejected the request”。这些展示分类不新增账号、配额或重试动作；已提交的流不自动重放。`MAX_TOKENS` 仍是 `response.incomplete` / `max_output_tokens`，表示达到本次请求的输出上限，不改成拒绝或成功。

短身份前缀和强制输出预算是 AG 文本渠道的兼容策略，不是“防封”保证。不会改写原调用方提示词内容、工具定义或用户消息，不模拟 Clearcut/Unleash 遥测，也不新增插件、语音或远程 Agent API。当前没有新的 ToS 封禁错误样本，不保证任何拟真字段能够避免账号限制。2026-11-16 同一长历史在 Low/32768 加短身份后仍缺 finishReason，Medium/262144 单次直接调用则取得完整工具调用与 STOP；变量不止一个，不能把成功单独归因于身份段或更大输出上限。

## Go TLS 后端

后端为 `parrot-go-tls-client`，经 httpx transport 接口接入。TLS 伪装默认开启，新建配置使用以下 Go 档案；已有配置显式设置的 `enabled: false` 会保留：

```json
{
  "antigravityOAuth": {
    "tlsFingerprint": {
      "enabled": true,
      "profile": "antigravity-ls-2.22.0"
    }
  }
}
```

普通使用者**不需要 Go 或 C 编译器**。仓库内 `vendor/parrot-go-tls-client` 附带维护者编译的 Linux amd64/arm64 库；Docker 内置同一受控产物，源码安装随正常依赖安装自动选择本机架构：

```sh
./venv/bin/pip install -r requirements.txt
./venv/bin/python scripts/check_go_tls.py --load
```

若只需重新安装原生包，可执行 `./scripts/install_go_tls.sh ./venv/bin/python`。安装只校验、复制预编译库，不在用户机器编译，不重启或修改配置。每个库通过源码 SHA256、二进制 SHA256、ELF 架构及 Go `ParrotBuildInfo` 绑定；不匹配即失败。

当前产物支持 Linux amd64/arm64、glibc >=2.34；旧 glibc 系统可用发布的 Docker 镜像，不宣称 musl、Windows 或 macOS 原生支持。维护者构建脚本是 `scripts/build_go_tls.py`，普通安装不会调用它。交叉编译不是目标平台运行验收，正式候选在两架构 native runner 强制测试，缺库不能跳过。

正常安装会带上原生库；如已有配置显式关闭，可在安装验证后将 `enabled` 改为 `true`。缺少开关时补为开启；旧配置缺少 profile 时仍沿用 `chrome131`，不随开关默认值变化而切换后端。TG 的 AG 指纹开关只切换 enabled，不覆盖已经保存的 profile；从旧配置升级时，如仍保存 `chrome131`，需明确选择新 profile。旧 curl profile 仍可使用，其传输路径保留；这不回退上文列出的 OAuth/UA 等协议变化。

新 Go 档案覆盖 Antigravity 文本/图片传输，以及 token、用户信息、项目/配额和模型发现的控制面请求。其他 provider 不受影响。

当前后端仅支持 HTTPS/h2，每次请求独占连接，不跨请求复用；不隐式重定向或重放，包括收到 REFUSED_STREAM 时。响应按块交给 HTTPX 统一解压，业务超时和 SSE 解析继续由 Parrot 管理。这是保证发送边界和取消清理的工程选择，不代表复刻了官方连接池行为。

遵守既有账号/模型/上游类型/用途的代理优先级，AG 控制面也传入账号 key，不再忽略无 `oauth:` 前缀的账号路由。控制面没有生成模型上下文，用途也有差别，因此不宣称不同用途始终使用同一条模型路由。SS2022 使用 Parrot 的认证 CONNECT 桥：桥只运送字节，TLS 由 Go 与目标端点建立。启用后缺少库或代理无法表示时应失败关闭，不能偷偷改用普通 TLS 或直连。

## 证据边界

隔离测试验证的是本地 PKCE 计算/传参、state 校验、会话绑定/一次性、配置迁移与错误处理，不是 Google 新 OAuth 登录实际验收。两枚旧 token 已能查询和生成，也不能证明新增 `aicode` 授权、localhost/官网 redirect、官网页面授权码格式或首次 onboard 已被 Google 接受。

当前未执行真实新授权或服务端 PKCE 负向验收：错误 verifier、跨会话 code、非 PKCE 旧 code 搭配新 verifier 时，Google 是否严格拒绝仍需单独授权的真实验证；不能把 mock 成功写成服务端反降级/绑定已验证。新 UA、目录头与 load 返回 project 的配额语义也存在真实服务端兼容边界。升级不会为了验证这些行为自动撤销授权、重登或操作现有账号。

正确计算的 LS 样本 JA3 为 `03117a8ed39ef02427ebbc39f121275c`。相同 JA3 或初始 HTTP/2 参数不等于所有流量行为与官方完全一致，也不保证免除账号验证。

既有 Cloud Code 样本 `daily-cloudcode-pa.googleapis.com_h2_48676.bin` 和 `_66913.bin` 是 curl 探测，不是官方 LS。不能把其中 `{3:100,4:65536,2:0}` SETTINGS 当作官方档案。当前 TLS spec 与已观测的 LS HTTP/2 初始配置保持成套；官方 Cloud Code 业务链路的动态等价性仍未证明。

客户端是否被服务端接受应以实际业务验证为准。发生 401、403 或账号验证挑战时，不应通过连续重登、撤销授权、切换 IP 或盲目重试来“修复指纹”；参见 [账号验证说明](antigravity-validation-required.md)。
