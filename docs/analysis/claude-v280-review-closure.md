# Claude Code 2.1.280：独立审查闭合（2026-09-23）

范围：以基线 `77c2170` 对照本任务完整附件 `README.md`、`PARROT-DIFF-REPORT.md`，复用已验证的代表抓包与本机2.1.280内置源码。没有重新抓1688份样本，没有真实上游调用。此文区分“报告建议已落实”和“客户端所有能力已克隆”；后者不是目标。

## 一、原报告13项逐项闭合

| # | 报告建议 | 当前结果 / 决定性路径 |
|---|---|---|
| 1 | CC_VERSION、UA、fingerprint版本 | 基线已完成2.1.280；`cc_mimicry.py`、UTF-16固定向量与代表抓包回归。 |
| 2 | CCH三个排除项 | 基线已完成顶层max_tokens/fallbacks及tool_use.input.max_tokens排除；递归model字符串清空、system billing重置正确。本包不改hash-view。新渠道测试用最终wire重算CCH，并验证有/无fallback hash-view相同。 |
| 3 | OAuth beta策略 | 基线已加OAuth标识、按1h TTL加extended-cache-ttl、不注入advisor。本包修正“捕获的Fable fallback策略≠所有代理请求默认意图”：仅显式fallback携带server-side-fallback beta，不再自动跨模型；显式值与fallback-credit beta保留。 |
| 4 | usage/profile UA | 基线已统一CLI_USER_AGENT；usage单OAuth beta，profile无beta且no-cache。现有假网络出站测试覆盖。 |
| 5 | cc_turn_origin/prompt ID | 基线已完成system首块无cache_control；main带sdk origin和request-scoped prompt ID，side query不带。沿用logical-request生命周期，重试不生成新归因。 |
| 6 | plugins scope、顺序 | 基线OAUTH_SCOPES已完成；登录URL与刷新invalid_scope去plugins的一次兼容重试已有测试。scope授权不等于启动插件市场。 |
| 7 | bootstrap UA | 基线已完成专用claude-code/2.1.280 UA；本包补OAuth beta（原报告端点摘要未列，但同版nt→u请求源码明确带Cd）。 |
| 8 | 新beta及Haiku精简 | 基线常量含thinking-binding、mid-tool-changes、per-turn-control、timing、inline-tools。Haiku main五beta已有；本包为旧/未知模型增加能力过滤，不能把Opus画像套给Haiku。per-turn/timing/inline为条件性客户端功能，没有相应实现/意图时不因常量存在全量发送。 |
| 9 | request-class | 基线主请求已带main，Haiku/side按捕获画像不带；API-key/OAuth与HTTP原始头测试覆盖。 |
| 10 | count_tokens透传 | 确实没有路由，不能称已实现。**可选、不在本包新增**：CC的失败路径返回本地估算，不阻断模型调用；准确预估是有价值的增强，但要增加端点权限/调度及非Anthropic渠道策略。直接复用messages变换会错误注入system/max_tokens等，并非一个安全的URL替换。保留当前退路，不伪造精确计数。 |
| 11 | br/zstd | **不做**：仅声明gzip/deflate，与确定可解码能力一致；没有依赖与解码测试不能仿造四编码头。属于传输性能/指纹差异，不是请求协议缺陷。 |
| 12 | refresh总带scope | 基线主平台请求带账号已保存scope，缺省带当前全集；invalid_scope仅去plugins一次。历史legacy/no-scope候选有意保留，不能为逐字模仿破坏既有凭据兼容。不是宣称每个历史兼容重试也带scope。 |
| 13 | grace/overage/slow等响应头 | **未全量解析**，详细边界见下节。不把未解析写成已覆盖；不自动授权额外消费、低优先级等待或重置领取。 |

## 二、此次确认并修复的遗漏

1. **模型能力/默认值**：v280目录的`max_output_tokens`和`Jyr/vCt/L_`分别约束默认thinking、adaptive、effort。Haiku4.5是32000+enabled而非64000+adaptive/high；3.5是8192且不注入thinking/effort，3.7默认32000且不自动开启thinking（显式enabled仍保留）；旧4.x与新模型分别门控。Opus4.5可effort但不可adaptive。未知兼容模型不猜Claude能力，沿用standard 4096安全上限。详情见`docs/05-cc-mimicry.md`表。
2. **显式参数**：CC分支原来丢弃的top_p/top_k/stop_sequences/service_tier/container/mcp_servers和非fast speed现在保留；不再以新注入thinking破坏显式采样参数、强制工具选择或小输出上限。显式thinking/effort/max_tokens/stream及context management原样优先。非CC渠道实现未改。
3. **Fable fallback**：缺省完全不生成fallbacks；用户显式字符串、空列表或模型列表保留，携带能力beta。CCH排除规则独立于wire默认行为。
4. **usage认证恢复**：原Claude `fetch_usage()`在401时直接失败。新增仅Claude `_fetch_claude_usage()`：401→复用并发轮换的新AT或调用既有force_refresh→只重试一次；其它HTTP错误不刷新，二次失败如实抛出。沿用token/profile/scopes写回和账号generation保护；不碰存储格式/备份、WHAM或额度归一化函数。
5. **bootstrap OAuth beta**：源码`nt()`为OAuth选择Authorization+Cd，调用`u()`合并入headers；补足此出站身份头。

## 三、额度：“结构解析已覆盖”不足以成立

### 已有直接证据

- `oauth_manager._usage_sync`返回原始JSON；`flatten_usage`按0..100解析five_hour/seven_day、seven_day_sonnet/opus和extra_usage，并把完整输入保存在raw_data。Fable由`fable_usage_block`/`_fable_scoped_candidates`解析`limits[]`的weekly_scoped/seven_day_scoped；非active窗口不参与自动禁用。
- v280 SDK `/usage` schema中的`model_scoped[]`是由上游`limits[]`映射的展示结构，不应把它误当作又一个已抓到的上游字段；现有Fable逻辑已有真实limits路径，不能因报告列了model_scoped就新增虚构适配。
- `anthropic/rate_limit_headers.py::parse_rate_limit_headers`仅形成five_hour_util/reset、seven_day_util/reset；`failover._maybe_record_anthropic_snapshot`写被动patch，不替换主动完整usage。
- 新版`*-surpassed-threshold`是数值预警阈值（0.75/0.95等），不是耗尽布尔值。当前`is_window_exceeded`仍兼容旧true，仅数值util≥1才判耗尽；数值预警没有被误判成耗尽。其文档仍是旧布尔说明，不把这一点写成新头族已实现。

### 明确未归一化 / 不跨主控额度所有权修改

- 原报告提到的`seven_day_oauth_apps`、`seven_day_overage_included`不在`flatten_usage`返回列、`extract_utils_percent`、`reset_iso_for_hit_windows`自动处理范围；若上游提供，它们只留在raw_data。响应头`7d-overage-included-*`也不在被动patch内。不能据此宣称主动/被动额度全覆盖。新增专属窗口参与路由/禁用需主控统一判定作用域及reset证据，不可映射成全账号seven_day。
- `grace-{5h,7d}-utilization`/grace-status：源码用于宽限收尾提示与状态；Parrot无该CLI交互，不新增自动宽限继续执行，保持现有quotaMonitor策略。用户自设阈值禁用不会因CC宽限能力被隐式撤销。
- `overage-{status,scope,in-use,utilization,reset,period,disabled-reason,...}`：涉及额外消费授权和费用提示；已有extra_usage主动展示不等价于新头族覆盖。不得因“对齐”自动消费额外额度。
- `slow-{offer,status,budget-utilization,budget-reset,retry-after,max-wait}`：涉及选择低优先级续跑及长等待策略；本包不改请求延迟/调度/用户确认。
- unified status/representative-claim/reset/fallback/upgrade-paths：客户端可据此精细呈现限制来源、升级路径；Parrot现有实际429错误处理不依赖克隆这些提示，但未解析字段若用于新自动禁用，须与窗口scope一致，不能直接扩大账号锁定。

这是**解析覆盖边界与集成提醒**，不是已实现所有新额度路由策略。本包没有覆盖主控的OpenAI reset、原生WS或WHAM修改；收到389行更新报告后，Claude官方重置另按第六节完成。

## 四、登录、刷新和其它出站面的完整核对

- **authorize/exchange**：CAI authorize、platform token/manual callback、client_id/PKCE/state已对齐；orgUUID/login_hint/login_method是可选登录引导参数，现有登录流程不需要额外猜组织。exchange多带CLI UA不影响认证；没有复制SDK库默认头来伪造网络栈。
- **刷新响应/失败分类**：现有链处理access_token/refresh_token/expires_in/scope并拉profile写套餐；invalid_scope不直接误禁用，invalid_grant走既有失效分类。account_on_hold仍通过HTTP错误链报告，不加自动解封或新的TG交互。30s超时和legacy候选是代理网络容错，不必复制CLI全部短超时。
- **usage/profile/bootstrap**：身份和usage401见上述修复。报告中的at_wall/cedar_ember/skip_spend、bootstrap model/entrypoint等参数来自具体客户端交互；普通账号用量/套餐拉取不能凭空宣称已经到额度墙或选择某模型，也不能默认skip_spend漏费用数据。5s/10s/30s差异是既有请求容错，不是身份协议错误。
- **validate/revoke**：validate可由现有真实usage/profile/刷新验证有效性代替；新增一次空POST只会增加请求。revoke会影响被其它客户端共用的授权，删除本地账号不等于用户授权撤销上游token；不自动新增该破坏性操作。
- **模型目录**：`oauth_model_discovery.discover_claude`已有OAuth `/v1/models?limit=1000`分页、版本/beta及能力读取；不能拿下载的静态签名目录替代账号可见权限。本包能力表仅选wire默认值，不向账号注入模型。
- **reset_rate_limits**：旧266行报告的“客户端专属不需要”分类不再适用。更新389行报告3.7及主控确认明确它属于现有账号管理职责；本包已补Claude双机制的状态读取、详情展示、两阶段确认和手动执行，见第六节。
- **遥测**event_logging/statsig/staging beacon：不复制采集、gate服务或外传。
- **events WS/SSE、local_pairing、desktop下载**：属于云会话/设备配对/分发产品，没有Parrot相应会话生命周期；不是模型转发必需出站。
- **file_upload/files、远程MCP、插件市场**：scope存在不等于要求代理实现存储、连接器或市场；现有下游请求/工具内容保持，新增上传及MCP会扩展访问和数据生命周期，明确不克隆。
- **/api/hello、device_authorization限流配置**：前者是客户端隐私/连通性预检，后者是设备登录本地策略；现有代理网络处理/授权方式不依赖它们，不新增无业务请求。

## 五、验证与合并边界

- 正式渠道回归：`test_cc_model_profiles.py`；原v280代表抓包/CCH/头/auth：`test_cc_v2_1_280_upgrade.py`；usage真实本地写回与fake网络：`test_claude_usage_auth_recovery.py`。
- 定向执行统一使用`/opt/src-space/parrot/venv/bin/python src/tests/isolated_pytest.py`，config/state/logs/身份文件只写隔离临时目录；不跑全量、不真实请求。
- 首次提交`18b0ce4`的`oauth_manager.py`合并点为Claude helper、Claude分派及bootstrap OAuth beta；后续Claude重置提交仅在本文件补organization.uuid提取及账号规范化保留，其业务放入`oauth/claude_reset.py`。不修改OpenAI reset/WHAM或通用quota evaluator。
- 首次提交定向结果：**234 passed**（2.98s），`git diff --check`通过。命令：

```bash
/opt/src-space/parrot/venv/bin/python src/tests/isolated_pytest.py -q \
  src/tests/test_cc_model_profiles.py src/tests/test_cc_v2_1_280_upgrade.py \
  src/tests/test_claude_usage_auth_recovery.py src/tests/test_channel_compatibility.py \
  src/tests/test_m5.py src/tests/test_anthropic_passive_sampling.py \
  src/tests/test_routing_review_fixes.py \
  src/tests/test_protocol_fake_upstreams.py::test_cc_v258_529_reuses_body_context_and_isolates_concurrent_requests
```
## 六、389行更新报告：Claude双官方重置已补齐

这是收到新材料后的追加实现，替代旧3.7的排除分类。关键条件再对照本机同版二进制：cedar `Ge` 的program/grant_id/request_id POST、`Qn=600000`/`Gt`未决键选择、`$t`的next grant/usable/paused/expiry；juniper `an`只传program，`kr/Cj`判断eligible/arm/available。未进行真实账号消费验证。

### 状态、身份与展示

- `profile.organization.uuid`保存为专用`claude_organization_uuid`，登录/导入规范化保留；旧账号进入重置状态查询时补拉profile。绝不回退到account.uuid。
- 普通usage不加skip_spend，两个块已存在raw_data；本包补TG读取/展示，不新增冗余quota列。强一致卡状态使用`?cedar_ember=1&skip_spend=1`或`?at_wall=1&skip_spend=1`，只patch这两个raw块，不覆盖费用、窗口数值或完整usage的fetched_at。
- 账号详情显示两种状态、卡数量/剩余/有效期/暂停/usable/窗口和限制条件、next_grant、cooldown、下次可用时间及每周次数。新增“官方额度重置/状态”；原“清本地配额禁用”的按钮和本地含义不变。

### 消费安全与反馈

- 统一`OAuthBackend`→`ClaudeResetControlMixin`契约，需DESTRUCTIVE权限；说明计划→最终确认计划→执行。计划绑定actor、精确账号revision、generation、组织和所选next grant，执行前一次性consume；TG同样经过这个服务端门禁，而非只靠callback字符串的stage。
- 执行前强一致重查eligible、next_grant、paused、有效期、usable_now、blocking、use_requires_limit、cooldown；不悄悄替换卡。Juniper要求实验reset组且available，明确“消耗周额度份额、重置5h、每周次数”。不克隆statsig，不隐式自动续跑。
- 25s POST使用OAuth Bearer、JSON和当前CLI UA。Cedar恰为`{program,grant_id,request_id}`；Juniper恰为`{program}`，不编造幂等头/字段。状态GET的401允许刷新后重读；消费POST仅对明确401复用并发更新的Token或刷新一次，保持同一body/request_id再试一次，并重新检查账号generation、组织和禁用状态。第二次401、403不再重试；超时/断连等结果不确定时不重发消费。429返回rate_limited。
- 新增`claude_reset_operations`独立durable域，键绑定generation+program+组织，只保留非秘密操作元数据。先持久化pending再POST；写入失败不会消费。旧账号无持久generation时先固化现有同一generation，避免重启失联。Token/config存储与备份格式未改变。
- Cedar同一未决操作600秒内经再次确认时复用同一request_id；同一grant超出600秒且仍无结论则阻止盲目重发。服务端明确提供不同的next_grant且用户重新确认新卡时，允许独立新操作，不把旧卡未决状态扩大为永久账号级封锁，也不据此宣称旧操作成功。Juniper未决同一周**即使状态仍available，也绝不再次POST**；重启仍有效。查到额度已变化只报告“无法归因但状态已变化”，不谎称本次成功；下一自然周有新周期证据且用户重新确认才允许新操作。
- result/reason/resets_left/cleared/cooldown_until/next_available_at/weekly_resets_at原样进入反馈。未知、超时、stamp_indeterminate/reset_unconfirmed只补GET核对，不自动重试消费、不强制启用。

### 重置后恢复与scope

- reset/already_used之后单独拉普通完整usage（含费用）。只有5h/7d及先前已知模型窗口都有有效新证据，才调用现有scope evaluator；缺失/失败不作为恢复证明。
- 从POST前到刷新结束，比较账号generation、quota观察代次、disabled状态、被动头观察时间和模型限制记录；任何新限制到达都保留，不删除quota整行，不全量清模型冷却。删除重建的晚结果不能写入或启用新账号。
- 确认完整新usage低于阈值且没有并发新限制后，仅清除HTTP 429/rate_limit_error中期限与旧账号配额期限匹配的模型冷却，先完成持久化删除再启用账号；删除失败不回报恢复。无关错误、不同期限及仍超限/未知的Fable限制保留，不全量清空冷却。
- 后台/手动恢复也要求有效新额度数据：OpenAI和Claude的缺失窗口保留旧值与期限，并以原始未合并响应判定本轮到底观察到了什么；未覆盖原超限窗口时不得恢复。spend_control=false不能替代缺失周额度，Claude空usage也不能恢复。Fable专属指标不能单独证明整个账号额度恢复。
- **证据边界**：更新报告仅证明`clears`枚举允许oauth_apps/overage/cowork/omelette等窗口，没有给出“普通代理messages必然消费该窗口”的模型/产品归属映射，也无真实Claude账号验证。因此保留原始字段、展示服务端cleared，不将它们并入账号seven_day，不新增overage授权。后续若要自动路由这些窗口，需要模型/产品scope映射的权威证据，而非从名字猜测。

### 追加修改面与回归

新增`oauth/claude_reset.py`、管理控制mixin和TG子菜单；`oauth_manager.py`只加组织字段。`state_db.py`新增三项Claude接口，`state_store.py`只增加一个durable域。原OpenAI reset/WHAM/WS不重做。后续恢复修正集中在`claude_reset.py`、通用额度评估和`quota_save`的缺失窗口保留，不改变其他provider的保存策略；`test_quota_recovery_review_fixes.py`覆盖实际调度、持久化、部分/空额度、旧卡与新卡隔离以及有限401恢复。

`test_claude_reset.py`覆盖50个正式用例：eligibility与next grant、两种精确wire、同/跨按钮并发、未决ID、真实state重载、权限及两阶段服务端确认、无组织、账号删除重建、回包/刷新期间新限制、Fable scope、成功后仍超限/刷新失败、TG实际callback链和两类按钮共存。原TG严格快照只在当前overlay的六个Claude详情case补明确新增区块/按钮；历史v0.31.13和其它provider内容未重录。

追加最终定向验证：**300 passed（8.72s）**，`git diff --check`通过。执行命令：

```bash
/opt/src-space/parrot/venv/bin/python src/tests/isolated_pytest.py -q \
  src/tests/test_claude_reset.py src/tests/test_claude_usage_auth_recovery.py \
  src/tests/test_management_oauth_contract_regressions.py src/tests/test_management_oauth_parity_fixes.py \
  src/tests/test_m7_oauth_menu.py src/tests/test_state_store_json.py src/tests/test_state_store_boundaries.py \
  src/tests/test_tg_contract_oauth_01_03.py src/tests/test_tg_contract_oauth_04_06.py \
  src/tests/test_m5.py src/tests/test_anthropic_passive_sampling.py
```
