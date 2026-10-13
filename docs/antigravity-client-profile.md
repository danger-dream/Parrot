# Antigravity 客户端档案与 Go 传输后端

## 身份与兼容策略

内置身份固定为已分析的 Hub 2.22.0 / CL 995734886 / linux-amd64：

```text
antigravity/hub/2.22.0 (aidev_client; os_type=linux; arch=amd64; cl=995734886)
```

这是模拟的客户端身份，不随 Parrot 宿主架构自动改变，也不单独追踪 updater manifest 中的版本号。客户端版本、CL 与 TLS 档案应一起更新；没有新版本的成套证据时保持现有组合。

升级加载配置时，仅与旧内置 UA `antigravity/hub/2.9.1 darwin/arm64` 和完整旧内置 scope 字符串精确相等的值被更新并持久化；有意固定为这些旧值的配置也会迁移。其他自定义 UA/scope、账户凭证、账户已有 scope、项目、模型选择、回调地址与显式 TLS 开关/profile 均保留。

加载已有配置文件时，若原文件未保存 TLS `profile`（包括仅配置 `enabled:true`），会补齐并持久化旧默认 `chrome131`，不会因 deep-merge 静默切到 Go。首次自动创建配置文件才使用新默认 `antigravity-ls-2.22.0`；两者均不自动开启 TLS 指纹。已有显式 profile 不会被覆盖，选择 LS 时首尾空白按生成面的规则统一去除后用于控制面选择。

OAuth token exchange/refresh 与 userinfo 固定使用 `Go-http-client/1.1`，不再采用自定义 `userAgent`；这与配置值被保留是两件事。Cloud Code 的 load、quota、目录和生成仍采用配置的 `userAgent`，未自定义时使用上面的 Hub UA。模型发现保留现有 prod/daily 合并逻辑，移除该查询的旧 `x-goog-api-client` 头。onboardUser 的官方新形态尚未确认，保留原有字段、轮询、node-client UA 后缀及 `X-Goog-Api-Client`，但 `metadata.ide_version` 随内置版本变为 2.22.0；自定义 `onboardUserAgent` 仍生效。

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

## Go TLS 后端

后端为 `parrot-go-tls-client`，经 httpx transport 接口接入。默认关闭，显式选择：

```json
{
  "antigravityOAuth": {
    "tlsFingerprint": {
      "enabled": false,
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

安装并验证后再将 `enabled` 改为 `true`。TG 的 AG 指纹开关只切换 enabled，不覆盖已经保存的 profile；从旧配置升级时，如仍保存 `chrome131`，需明确选择新 profile。旧 curl profile 仍可使用，其传输路径保留；这不回退上文列出的 OAuth/UA 等协议变化。

新 Go 档案覆盖 Antigravity 文本/图片传输，以及 token、用户信息、项目/配额和模型发现的控制面请求。其他 provider 不受影响。

当前后端仅支持 HTTPS/h2，每次请求独占连接，不跨请求复用；不隐式重定向或重放，包括收到 REFUSED_STREAM 时。响应按块交给 HTTPX 统一解压，业务超时和 SSE 解析继续由 Parrot 管理。这是保证发送边界和取消清理的工程选择，不代表复刻了官方连接池行为。

遵守既有账号/模型/上游类型/用途的代理优先级，AG 控制面也传入账号 key，不再忽略无 `oauth:` 前缀的账号路由。控制面没有生成模型上下文，用途也有差别，因此不宣称不同用途始终使用同一条模型路由。SS2022 使用 Parrot 的认证 CONNECT 桥：桥只运送字节，TLS 由 Go 与目标端点建立。启用后缺少库或代理无法表示时应失败关闭，不能偷偷改用普通 TLS 或直连。

## 证据边界

隔离测试验证的是本地 PKCE 计算/传参、state 校验、会话绑定/一次性、配置迁移与错误处理，不是 Google 新 OAuth 登录实际验收。两枚旧 token 已能查询和生成，也不能证明新增 `aicode` 授权、localhost/官网 redirect、官网页面授权码格式或首次 onboard 已被 Google 接受。

当前未执行真实新授权或服务端 PKCE 负向验收：错误 verifier、跨会话 code、非 PKCE 旧 code 搭配新 verifier 时，Google 是否严格拒绝仍需单独授权的真实验证；不能把 mock 成功写成服务端反降级/绑定已验证。新 UA、目录头与 load 返回 project 的配额语义也存在真实服务端兼容边界。升级不会为了验证这些行为自动撤销授权、重登或操作现有账号。

正确计算的 LS 样本 JA3 为 `03117a8ed39ef02427ebbc39f121275c`。相同 JA3 或初始 HTTP/2 参数不等于所有流量行为与官方完全一致，也不保证免除账号验证。

既有 Cloud Code 样本 `daily-cloudcode-pa.googleapis.com_h2_48676.bin` 和 `_66913.bin` 是 curl 探测，不是官方 LS。不能把其中 `{3:100,4:65536,2:0}` SETTINGS 当作官方档案。当前 TLS spec 与已观测的 LS HTTP/2 初始配置保持成套；官方 Cloud Code 业务链路的动态等价性仍未证明。

客户端是否被服务端接受应以实际业务验证为准。发生 401、403 或账号验证挑战时，不应通过连续重登、撤销授权、切换 IP 或盲目重试来“修复指纹”；参见 [账号验证说明](antigravity-validation-required.md)。
