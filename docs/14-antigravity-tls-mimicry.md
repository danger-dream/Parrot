# 14 — Antigravity TLS/H2 指纹伪装

`docs/05-cc-mimicry.md` 解决的是 Anthropic 家族的 **协议层** 伪装（body profile、
CCH、fingerprint salt）。本文件解决另一条正交的链路：**传输层画像**，即出口
connection 的 TLS ClientHello、ALPN 与 header 序。

## 14.1 为什么需要独立一层

CC mimicry 之所以有效，是因为 Anthropic 前端校验的是 body 结构与签名。Cloud
Code / Antigravity 家族（`v1internal:generateContent`）多了一道前置判据：GFE
在 TLS 握手和 HTTP/2 帧层面就能把请求分成「IDE 进程」与「脚本直连」两类，且这
一分类发生在应用层校验之前。协议层再准，ClientHello 是 Python/OpenSSL 画像时
依然命中第二类。

实测与社区结论一致（CLIProxyAPI、zerogravity、agent-vibes go-worker 三种独立
实现殊途同归）：

| 观测点 | 默认 httpx | 真实 hub |
|---|---|---|
| TLS 库 | OpenSSL（Python 默认 suites / extensions） | BoringSSL（Chrome 画像） |
| JA3/JA4 | Python 指纹，易聚类 | 与 Chrome 同族 |
| ALPN | 不声明 h2（`http2=False`），退 HTTP/1.1 | h2 |
| header 序 | httpx 字典序 | Electron 原始插入序 |
| UA | 需显式设置 | `antigravity/hub/<ver> <platform>` |

其中 ALPN 一项影响最直接：Go/Python 客户端不显式声明 h2 时，GFE 侧的 "direct
HTTP" 判定会把请求从 IDE 流量里摘出去。

## 14.2 启用边界

- 只有**渠道显式声明** `tls_fingerprint` 时才生效。当前仅 Antigravity OAuth
  channel 读取 `antigravityOAuth.tlsFingerprint`；其他渠道继续共享 httpx 连接池，
  行为零变化。
- 走 **direct route** 才生效。new-proxy chain（connector 自建 client）路径不叠加
  指纹，出口语义优先。
- **legacy SOCKS5 代理仍然生效**：httpx 在传入自定义 transport 时会忽略 client
  级 `proxy` 参数，所以代理会被透传给 curl_cffi session，不会静默绕过。
- `internal_loopback` 渠道（Cursor bridge 一类进程内回环）不受影响，仍走短生命
  周期直连 client。
- 置空串（`"tlsFingerprint": ""`）可显式关闭，退回共享池。

## 14.3 实现

新增 `src/transports/fingerprint.py`：

- `ImpersonatedTransport(httpx.AsyncBaseTransport)`：把 httpx 请求改由 curl_cffi
  的 `AsyncSession` 发出。一次 attempt 一个 session，随 `client.aclose()` 释放，
  避免跨 event loop 复用。
- 响应侧 `_CurlResponseStream(httpx.AsyncByteStream)` 只提供 `__aiter__` +
  `aclose`，failover 现有的 `aiter_bytes()` / `aread()` 消费方式不变。
- `impersonation_transport(profile)` 工厂：backend 缺失时告警并返回 `None`。

接线点：

1. `src/network.py` 的 `async_client(..., impersonate=None)` —— 非空时换 transport
   并 `pop("http2")`（ALPN 交由 BoringSSL 协商，同时传 http2 会被 httpx 拒绝）。
2. `src/channel/base.py` 增加类属性 `tls_fingerprint: Optional[str] = None`。
3. `src/channel/antigravity_oauth_channel.py` 从 `_provider_cfg()` 读取该字段。
4. `src/transports/http_runtime.py` 在每个 attempt 的 route 解析处增加分支：
   渠道声明了指纹时建独立的 impersonation client，复用已有的
   `owner.proxy_client` 释放路径。

`timeout` 从 `request.extensions["timeout"]` 透传给 curl_cffi，因此
connect / first_byte / idle / total 四段超时语义（见 `docs/02-config-schema.md`）
不变。

## 14.4 已知取舍

- **trace extension 不模拟**：`http_runtime` 传入的 `extensions={"trace": ...}`
  是 httpcore 的细粒度事件钩子，curl_cffi 无对应物。跳过只会让该 attempt 的
  dispatch 计时字段缺失，不影响请求正确性与错误分类。
- **fail-open**：未安装 `curl_cffi` 时打一次告警并退回普通 httpx，不阻断主链路。
  `requirements.txt` 已声明依赖；Docker runtime 与 CI 均能取到 linux manylinux
  wheel。
- **响应头剥字段**：libcurl 按浏览器画像的 `accept_encoding` 自动解压，但保留
  `Content-Encoding` / `Content-Length`；若原样交给 httpx 会二次解压（实测报
  `DecodingError: incorrect header check`）。因此响应侧剥掉这两个字段，使 body
  与头部一致。只动响应侧，请求方向指纹不受影响。
- **profile 选择**：默认 `chrome131`，与 UA `antigravity/hub/2.9.1 darwin/arm64`
  所暗示的 Electron/Chromium 画像同族。curl_cffi 升级后可按需切换（如
  `chrome136`、`safari18_0`）。Antigravity 侧的 UA 版本下限由官方 updater
  manifest 决定（当前 hub 2.9.1），UA 与 profile 应一起调整，不要只改一边。
- **不做生命周期伪装**：warmup RPC、心跳、请求间隔 jitter 属于客户端行为，
  代理侧复刻反而制造更可识别的规律性。封控的决定性变量在出口 IP 与账号节奏，
  不在本层。
