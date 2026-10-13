# Parrot 集成（0.2.1）

唯一运行来源是 Parrot 安装的 `vendor/parrot-go-tls-client` 快照。独立包目录用于开发，
不出现在生产 sys.path。`SOURCE.json` 记录版本、逐文件 SHA256 和聚合 hash：

```sh
# 在 Parrot 仓库；仅由维护者明确同步，禁止手改 vendor 后忘记同步
python scripts/sync_go_tls_vendor.py /path/to/parrot-go-tls-client
python scripts/sync_go_tls_vendor.py --check
```

维护者先用 `scripts/build_go_tls.py --source /path/to/package --arch all` 编译 amd64/arm64，
再同步源码和两个预编译库。库的源 SHA 注入 `ParrotBuildInfo`，清单同时保存源、ELF 架构与二进制 hash。
Docker 和源码 pip 安装均只校验、选择和复制预编译库，不执行 Go/C 编译。
release-prepare 的 amd64/arm64 原生 runner 强制验证安装产物身份、Go race 及 Python 传输回环；
缺库不能静默跳过。正式发布仍只提升已验收 digest。
**交叉编译不等于运行验收；arm64 必须实际运行原生 runner。**

源码/systemd 使用目标 Python venv 安装，无需 Go/C；要求 Linux amd64/arm64、glibc >=2.34：

```sh
# 显式安装到指定环境；脚本不重启服务、不修改配置
scripts/install_go_tls.sh /path/to/venv/bin/python
```

仅启用 `antigravity-ls-2.22.0` 时使用新后端，旧 curl profile 保留。工厂仍是
`src.transports.fingerprint.create_impersonated_client(...)`。
同步控制面单独调用 `src.transports.go_tls.request_sync(method, url, *, headers, json, data,
 timeout, proxy_purpose, proxy_channel, proxy_model, proxy_provider)`；由 provider 决定开关，
不全局替换 network，不在 Go 中放账号／协议逻辑。

新 backend 自己不做 DNS：Python 解析直接 socket 对端，复用 network.resolve_host 的
配置／缓存／IP 家族策略，将结果传入 Go；SS/SOCKS 的目标域名仍交给远端。
没有解析结果时失败，不回落到 Go 系统 DNS。SS2022 使用现有一次性认证 CONNECT lease。

本地构建无编译器镜像（仅预编译包安装/加载，不是外部上游验收）：

```sh
docker build -f docker/Dockerfile.build -t parrot-go-tls-local .
```

Parrot 专用测试需要先安装该包：

```sh
PARROT_REQUIRE_GO_TLS=1 python src/tests/isolated_pytest.py -q src/tests/test_go_tls_transport.py \
  src/tests/test_antigravity_tls_fingerprint.py src/tests/test_ss2022_curl_bridge.py
```
