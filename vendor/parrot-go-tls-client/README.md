# parrot-go-tls-client 0.2.1

Python ctypes + Go/uTLS 的可取消 HTTPS/h2 传输原语。固定 profile：
**`antigravity-ls-2.22.0`**。业务协议、OAuth、SSE 事件解析、路由与业务重试不属于本库。

## 指纹证据边界

保留 LS 2.22.0 实际样本的 cipher/扩展顺序（含 ext50）、曲线组、签名算法、
key_share 组/长度和已验证 h2 起始设置，`MaxReadFrameSize=1048576`。
修正 ext11 长度字段解析后的 JA3 为 **03117a8ed39ef02427ebbc39f121275c**。

基准为 `antigravity-unleash.goog_52006.bin` / `antigravity-unleash.goog_h2_6654.bin`。
**业务 host 等价性、动态 HTTP/2 行为等价性均未确认；Go 同源不表示 100% 一致。**
`daily-cloudcode*48676/66913.bin` 来自人工 curl/8.14.1 探测，不是 LS，不能据此改 profile。
比较脚本只证明列出的初始字段，不证明所有 TLS 字节、会话恢复策略或业务成功。

## 安装与预编译交付

普通使用者只需要 Python >=3.10，不需要安装 Go 或 C 编译器：

```sh
python -m pip install .
```

包内提供维护者预编译的 Linux amd64/arm64 库。安装按机器架构选择、校验 ELF 与 SHA256，
并核对源码身份；产物缺失、不匹配时明确失败，不下载未知库、不现场编译、不回退普通 TLS。
当前产物要求 glibc >=2.34；旧 glibc 主机可使用 Parrot 发布的 Docker 镜像。
wheel 为 `py3-none-linux_<arch>`，不绑定 CPython ABI，不宣称 musl/Windows/macOS 支持。

Parrot 的 `vendor/parrot-go-tls-client/SOURCE.json` 记录源码与产物快照，
`prebuilt/manifest.json` 将每个库绑定到源码 SHA256、架构、Go 版本和最低 glibc。
安装后的 `native_build.json` 与 Go `ParrotBuildInfo` 用于发布前校验实际加载库身份。
只允许这两份受控 `.so` 随仓交付，不复制任意开发目录产物。运行时只导入已安装包。

仅维护者重新构建需要 Go 1.24.x、C 编译器（交叉构建还需对应工具链）：

```sh
# 在 Parrot 仓库执行；先完成独立包修改和测试，再构建与同步
python scripts/build_go_tls.py --source /path/to/parrot-go-tls-client --arch all
python scripts/sync_go_tls_vendor.py /path/to/parrot-go-tls-client
python scripts/sync_go_tls_vendor.py --check
```

`go.mod`/`go.sum` 固定依赖，不要 `go get -u`。交叉编译不等于目标架构运行验收；
两架构 native runner 必须验证随包交付的真实库。同步/验收见 `docker/INTEGRATION.md`。

## 生命周期与流式 API

```python
from parrot_go_tls_client import Client, NativeError

with Client() as client:
    with client.start("GET", "https://example.invalid/", timeout=30) as request:
        while True:
            event = request.poll(1000)  # async adapter uses nonblocking poll(0)
            if event["kind"] == "error":
                raise NativeError(event)
            if event["kind"] == "eof":
                break
            # headers: status + headers_raw byte pairs; data: base64 raw body bytes
```

- `start` 立即返回句柄；事件为 dispatch、uploaded、headers、data、error、eof。
- 有界生命周期事件与 body 队列、32KiB 分块及 h2 栈固定流控窗口；消费慢时背压，不累积无界 SSE。
- `cancel` 立即取消 Go context；`close` 等待 native 请求停止并释放句柄；Client.close 取消所有所属请求。
- 每个请求独占 h2 连接，不跨请求建池。直接调用 `ClientConn.RoundTrip`，**零隐式 HTTP/h2 重试**，
  包括 REFUSED_STREAM/GOAWAY。没有 http.Client 自动重定向。上层仅在确定未发时选择另一条路由。
- dispatch 是 TLS 完成后、应用 HEADERS 可能发出前的保守边界；一旦跨过就永不回退为“未发”。
  uploaded 来自 httptrace.WroteRequest。异常携带 code/phase/dispatched，不靠错误字符串判断安全重试。
- connect/read/write timeout 为请求私有；可选 timeout 是整个 native 请求上限。
  Parrot 的 firstByte/idle/total 仍由其 runtime 管理，并调用 native cancel 终止 I/O。
- 目标证书默认验证；支持附加 CA PEM。insecure 仅显式本地诊断使用，不能为指纹而关闭验证。
- direct、SOCKS5（目标远端解析）、认证 HTTP CONNECT、验证代理证书的 HTTPS CONNECT。
  SS2022 复用 Parrot 的认证/目标锁定/单次 CONNECT 桥，Go 不实现第二套 Shadowsocks。
- `dial_ips` 注入**直接 socket 对端**的解析结果；`require_resolved=True` 禁止 Go 静默 DNS 回退。
  目标 URL/SNI/Host 不改。代理场景只解析代理主机，不本地解析目标域名。
- **原始压缩响应**，不自动添加 Accept-Encoding，也不解压。Parrot HTTPX 统一解码一次。
- 仅 HTTPS/h2。协商 http/1.1 时明确报错，没有假 fallback；保留 LS ALPN 提议顺序。

`Client.request/get/post` 是有限响应便利封装，默认最多128MiB，超限明确报错；**不是 SSE API**。
Parrot 适配入口是 `src/transports/go_tls.py` 的 AsyncBaseTransport/AsyncByteStream。

## 本地验收

```sh
GOTOOLCHAIN=local go test -race -count=1 ./...
python -B test/test_compare.py
# 已有离线指纹样本的比较（不连接上游；差异非零退出）
python test/final_compare.py --ls-ch /path/ls.bin --ls-h2 /path/ls-h2.bin \
  --pg-ch /path/client.bin --pg-h2 /path/client-h2.bin
# 不含编译器的容器安装/加载预编译库（Go源码测试另行执行）
 docker build -f docker/Dockerfile.build -t parrot-go-tls-local .
```

Parrot 传输回归：安装 vendor 包后通过 `src/tests/isolated_pytest.py` 运行
`test_go_tls_transport.py`、`test_antigravity_tls_fingerprint.py`、`test_ss2022_curl_bridge.py`。
这些测试不访问真实账号或外部模型；arm64 必须在对应 runner 验证，不能从 amd64 成功推断。
