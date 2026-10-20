# Parrot 固定发布入口

日常发布只调用 `scripts/release.py`。模型负责审阅改动、决定版本、编写面向用户的说明、取得必要授权；启动后由程序完成流程与等待。正常执行不逐阶段交给模型判断，不临时追加验证环境。

## 日常流程

1. 预检本机解释器/测试依赖、GitHub仓库与推送权限、明确的变更文件列表。
2. 核对直接运行依赖，更新 `src/__init__.py`，保留提交钩子，提交并冻结候选SHA。
3. 本机实际Python全量隔离回归，与GitHub的Python 3.11全量隔离回归、amd64和arm64原生构建并行。CI固定双分片各执行互不重叠的一半用例，两片均通过且完整收集清单并集无遗漏/重复后才通过quality门禁。两套回归各一份，不创建第三份本机CI容器。
4. 构建任务各自上传非正式候选镜像到GHCR，记录不可变digest。不同架构使用独立的registry构建缓存，可跨候选分支复用。Actions只传递小型JSON凭据，不传完整镜像tar。
5. 所有门禁通过后，核对当前工作区、候选SHA、完整运行资源和本机依赖指纹，先检查远端main祖先关系、候选产物凭据和版本Tag冲突，再按已有授权重启并验证健康、版本、实际服务实例和启动日志；没有授权则暂停。推送时仍再次检查，不能以重启前检查替代推送竞态保护。发布期间不得由其他任务并行修改同一工作区。
6. annotated Tag正文与批准说明逐字核对；更新main并推Tag。正式工作流核实同SHA的候选门禁与未过期产物凭据，按digest检查架构、运行版本和源码身份，再创建正式多架构manifest。不重新回归、构建、下载或上传完整镜像。
7. 镜像通过核验后才创建Release。核对用户说明与自动生成的对比链接，清理本次候选分支，输出耗时报告。

候选镜像和构建缓存不是正式发布；通过门禁前不更新版本标签、latest或Release。正式Release不再有独立手动创建入口，恢复同样经过正式发布工作流。

## 启动与授权

使用实际线上解释器、已经安装的运行和测试依赖、Git及GitHub token。token只通过 `GH_TOKEN` 或 `GITHUB_TOKEN` 环境变量传入，不写入状态文件、命令行或提交。本机token需具有仓库推送、workflow和Actions恢复权限；GHCR上传与演练清理由Actions使用仓库自身的包权限执行，不要求扩大本机token的权限。

说明文件开头讲清用户价值，写明旧版本到新版本跨度，按新功能/优化修复/质量分组。旧版本取 GitHub 上最近一个已公开的正式 Release（跳过草稿、预发布和未发布的本地版本号），与 GitHub 自动生成的对比链接一致。不要手写Full Changelog，由GitHub生成。

```bash
./venv/bin/python scripts/release.py start 0.34.6 \
  --notes /path/to/approved-notes.md \
  --files src/changed_module.py src/tests/test_changed_module.py \
  --approve-restart
```

上例的 `--approve-restart` 仅表示调用方已取得本次目标服务重启授权，绝不是自动授权。未获授权时省略，脚本在门禁处暂停。`start`本身会提交并推候选分支，只有发布已获授权才可执行。重启会中断连接；Tag覆盖另需对准确旧对象的确认。

`src/__init__.py`自动纳入正式候选；未列入 `--files` 的改动会阻止操作。说明原文冻结，不因外部说明文件后续修改而悄悄替换。版本只允许递增。

默认解释器 `venv/bin/python`，可用 `--python /absolute/path` 指定；默认服务 `parrot.service`，默认健康地址来自本机config.json的监听端口。可用 `--service`、`--health-url` 显式指定。每个远端等待阶段默认1800秒，可用 `--timeout` 调整。调用工具的总超时应覆盖完整流程，不要用短超时反复杀掉脚本。

本机发布worker默认最多8且不超过CPU affinity，可显式 `--workers N`；CI使用两个独立runner，每片使用nproc和worksteal。用例按nodeid的固定SHA-256分片，新增用例自动纳入，无需模型挑选。每片上传完整收集/执行清单和失败日志，quality汇总任务拒绝缺片、重复、清单不一致或任何分片失败/取消/跳过。日常隔离测试入口仍默认全量，默认worker数不变。发布时不重复做worker调优。

## 失败与续跑

运行记录位于Git忽略的 `.release-runs/<运行ID>/`。保留记录，不通过删状态、手动Tag或临时拼命令绕开门禁。

```bash
./venv/bin/python scripts/release.py status <运行ID>
./venv/bin/python scripts/release.py resume <运行ID>
```

- 候选与解释器/依赖未变，复用已通过的本机回归和云端产物。外部推送/Release失败不从头回归或构建。
- 修复源码后生成新候选，使旧候选验证失效；新增文件通过 `resume <运行ID> --files path/to/fix.py` 显式加入。
- 同一候选的CI故障用 `--retry-ci`，正式工作流故障用 `--retry-publish`，只请求失败job重跑。不移动已有Tag来掩盖上传故障。必要时从Actions手动恢复，运行ref必须选择输入的同一个目标Tag，不能选择main；不匹配会在发布副作用之前拒绝。
- GitHub明确拒绝请求时不会保留虚假的等待意图；结果未知时保留意图并核实远端。长时间没有新attempt会停止说明，不空等完整30分钟。
- 重启前与健康验证后核对候选；重启意图及成功回执绑定完整运行资源、Python/依赖版本指纹及PID/进程启动标识。JSON和TXT运行资源同样参与指纹。依赖变化后即使重新通过回归，也不能复用旧进程的重启回执；缺少依赖指纹的旧回执同样不能直接复用，不确定重启仍须先核实。
- 重启结果未知或修复改变了运行内容时，先检查服务状态，再使用 `--reconcile-restart --approve-restart`。脚本确认systemd没有待执行job后才允许新的明确重启动作；不能盲目添加参数。
- 覆盖失败Tag使用 `--approve-tag-rewrite <准确旧对象SHA>`，已有Release的版本不可覆盖。固定入口的annotated Tag正文原样传递到Release，不删除版本号开头的首行。同名Release草稿会明确阻断创建步骤，需要先处理草稿，脚本不会将其冒充公开版本或擅自公开。
- 退出码0为完成，1为错误，2为需要授权或副作用核实。错误中保存阶段、适用的命令/退出码、失败测试/job、日志和恢复命令。

脚本退出会终止本次测试子进程，清理源码副本、隔离数据和临时认证文件。异常退出后续跑先核实进程身份再回收自己的快照；不清理生产配置、数据、其他任务资源或全局缓存。

## 一次性真实演练（不是日常发布前置步骤）

仅用于验收发布工具与测量耗时，日常发布不要先做演练。

```bash
./venv/bin/python scripts/release.py rehearse \
  --files scripts/release.py scripts/release_cli.py .github/workflows/release-prepare.yml \
  .github/workflows/docker-publish.yml .github/workflows/release.yml \
  src/tests/test_release_entrypoint.py src/tests/test_release_recovery.py \
  src/tests/test_release_formal_paths.py src/tests/test_release_workflow_order.py scripts/test_shards.py \
  src/tests/sharding.py src/tests/test_test_sharding.py .gitignore README.md docs/releasing.md
```

演练是真实的本机/CI全仓回归、双架构构建、候选上传、digest核验与多架构manifest发布，只允许 `rehearsal-<SHA>` 标签。使用独立Git index和候选提交，不改本机HEAD、暂存区、正式版本号、远端main、正式Tag、latest或Release，绝不重启服务。需要提前授权临时候选分支、Actions和候选镜像上传/清理。

完成后删除本次临时分支及明确归属本次、没有任何正式标签引用的演练镜像版本；保留跨发布构建缓存。若发现非演练标签共同引用则拒绝删除。失败时保留候选与证据供续跑。

冷/热缓存比较应启动两个不同的演练运行，第二次仍执行完整门禁，才能验证跨候选复用。不要把同一候选的resume当成热缓存测试。

演练不能证明真实线上重启、副作用权限和正式Tag/Release写入已成功；这些由隔离测试覆盖，并在首次授权正式发布时完成实测。演练耗时中不含人工授权等待与真实线上重启，不伪装成正式完整发布耗时。

## 计时与证据

`state.json`保存有效阶段凭据、错误和Actions job/step原始时间；`timings.json`保存候选、运行模式、并发阶段、总墙钟时间、脚本活跃时间与进程外间隔（可能是人工等待，也可能是中断）。不把并发job耗时简单相加。

日志位于运行目录的 `logs/`。成功、失败均保留错误证据及耗时；模型直接利用这些数据解释瓶颈，不重新调查整场发布。
