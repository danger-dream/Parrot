# 09 — Telegram Bot 完整交互树

## 9.1 主菜单

入口命令：`/start`、`/menu`。所有分菜单都有「返回主菜单」按钮。

```
┌────────────────────────────────┐
│  🤖 anthropic-proxy 管理面板     │
├────────────────────────────────┤
│       📊 统计汇总              │
│       📋 最近日志              │
│       🔐 管理 OAuth            │
│       📡 管理渠道              │
│       🔁 模型映射              │
│       ⚖️ 负载均衡              │
│       ⚙  系统设置              │
│       ❓ 帮助                  │
└────────────────────────────────┘
```

命令菜单（setMyCommands）：
- `/start`、`/menu`：打开主菜单
- `/oauth`：OAuth 管理
- `/channels`：渠道管理
- `/keys`：API Key 管理
- `/stats`：统计
- `/logs`：日志
- `/loadbalancing`：负载均衡
- `/settings`：系统设置

## 9.2 管理 OAuth

### 9.2.1 列表视图

```
╔══════════════════════════════╗
║    🔐 OAuth 账户管理          ║
╠══════════════════════════════╣
║ 共 2 个账户，1 个正常、1 个配额禁用

1. ✅ user1@gmail.com
   过期: 2026-04-18 13:26:49 (剩 2h 15m)
   📊 5h: 12% | 7d: 45% | Sonnet 7d: 8% | Opus 7d: 32% | Fable 7d: 6%

2. 🔒 user2@gmail.com [配额禁用]
   过期: 2026-04-19 09:00:00
   📊 5h: 96% | 7d: 72% (预计恢复: 2026-04-18 15:00:00)
╚══════════════════════════════╝
[  user1@gmail.com  ]
[  user2@gmail.com  ]
─────────────────────
[ ➕ 新增账户 ][ 🔄 刷新用量/重置卡 ]
[       ◀ 返回主菜单       ]
```

底部每个账户按钮点进去 → 账户详情页。

### 9.2.2 账户详情

```
╔══════════════════════════════╗
║  user1@gmail.com             ║
╠══════════════════════════════╣
║ 状态: ✅ 正常
║ 过期: 2026-04-18 13:26:49
║ 上次刷新: 2026-04-17 21:26:49
║ 
║ 📊 使用量（刚刷新）
║ ⏱ 5h: 12% (重置: 14:00:00)
║ 📅 7d: 45% (重置: 2026-04-24 08:00:00)
║ 🤖 Sonnet 7d: 8%
║ 🧠 Opus 7d: 32%
║ 📖 Fable 7d: 6%
║    █░░░░░░░░░
║ 💰 额外额度: $0.00 / $50.00
╚══════════════════════════════╝
[ 🔄 刷新 Token  ][ 📊 刷新用量/重置卡 ]
[ 🚫 禁用       ][ 🗑 删除     ]
[ ◀ 返回 OAuth 菜单              ]
```

操作：
- 「刷新 Token」→ 调 `force_refresh(email)` → 显示新过期时间
- 「刷新用量/重置卡」→ 调 `fetch_usage(email)`；OpenAI 账号同时拉取官方重置卡明细 → 更新显示
- 「禁用」→ 弹二次确认 → `set_enabled(email, False, reason="user")`
- 「删除」→ 弹二次确认 → `delete_account(email)`

禁用状态下显示按钮换成「启用」。

### 9.2.3 新增账户

点「➕ 新增账户」→

```
╔════════════════════════╗
║  新增 OAuth 账户        ║
╠════════════════════════╣
║ 请选择添加方式：         ║
╚════════════════════════╝
[ 🌐 登录获取 Token ]
[ 📝 手动设置 JSON ]
[      ◀ 返回      ]
```

#### 登录获取 Token（PKCE）

1. 生成 code_verifier / code_challenge / state
2. 保存 user_state：`{"action": "oa_login_code", "data": {verifier, state}, "ts": now}`
3. 回复：
   ```
   请在浏览器中打开：
   <URL>
   登录完成后，页面会显示一个 authorization code，请复制并发给我。
   ```
4. 用户发送 code → 解析 `code#state` → 调 token endpoint → 拉 profile → 保存
5. 回复成功或错误

#### 手动设置 JSON

1. 保存 user_state：`{"action": "oa_set_json", "ts": now}`
2. 回复：
   ```
   请输入 OAuth JSON（需含 access_token、refresh_token、expired、email）：
   ```
3. 用户发 JSON → 解析 → `add_account(...)` → 回复结果

### 9.2.4 刷新用量/重置卡

对每个 OAuth 账户刷新用量；OpenAI 账号会同时刷新官方重置卡次数与卡片明细，并写入同一份 quota cache。完成后刷新列表视图。

### 9.2.5 媒体设置

「⚙️ 账户设置」中的媒体入口分为两条，避免同名图片接口的配置混淆：

- 「🖼 GPT 图片设置」：管理 GPT/Codex 图片模型、缓存与图片账号禁用列表；
- 「🎨 Grok Imagine」：管理 `xaiOAuth.imageModels`、`videoModels`、`videoJobTtlSeconds` 和 `mediaRequestTimeoutSeconds`。

Grok Imagine 页面同时展示模型路由：已配置的 `grok-imagine-image*` 走 xAI OAuth，其他图片模型仍走 GPT/Codex。修改后通过 `config.update()` 原子保存并热加载。两个设置页均提供「🎞 查看多媒体日志」入口；统计、账号排行和任务详情不再散落在配置页。

## 9.3 渠道管理

### 9.3.1 列表视图

```
📡 渠道管理
共 3 个 | 第 1/1 页

1. 🟢 智谱 Coding Plan Max — 正常
  🏷️ 模型：2 个
  📊 5h：已用 74% · 重置 19:38
  📅 7d：已用 23% · 重置 08-23 10:01
  🛠 MCP 月度：已用 233 / 4,000（5%）· 重置 09-12 10:01
  💎 Parrot 月度：↑ 660.4M · ↓ 1.4M · 缓存 379.4M（57.4%）
  📨 请求：3,692 次 · 成功率 99.6% · 失败 13 次
  ⚡ TPS：平均 78.5 t/s · 峰值 341 t/s · 最低 0.5 t/s
  💵 费用：$495.384

2. 🟢 自定义渠道 — 正常
  🏷️ 模型：5 个
  💎 Parrot 月度：暂无调用

[ 智谱 Coding Plan Max ][ 自定义渠道 ]
─────────────────────────
[ ➕ 添加渠道 ][ 🧹 清除错误 ]
[ 🔗 清空全部亲和绑定          ]
[       ◀ 返回主菜单          ]
```

渠道列表每页 4 个。第一行健康状态来自 scorer/cooldown；上游额度与 Parrot 本地月度统计分行显示，不再把近期成功率、近期样本、月度 TPS、缓存和费用混成同一摘要。本地月度请求、Token、缓存、TPS 与费用均来自同一份当月 `log.db` 共享快照；无本月调用时只显示“暂无调用”。

### 9.3.2 渠道详情

```
╔═══════════════════════════════╗
║  智谱 Coding Plan Max         ║
╠═══════════════════════════════╣
║ URL: https://coding.example.../anthropic
║ Key: sk-xxx***（前 6 + 末 4）
║ CC 伪装: 开启
║ 状态: ✅ 正常
║ 
║ 模型:
║   • glm-5 (GLM-5)      ✅ 可用
║     请求 128 次 / 成功率 98.4%
║     连接 121ms / 首字 812ms / 总 8.7s
║     score: 2340
║ 
║   • glm-5-turbo (GLM-5-Turbo)  ⚠ 退避中
║     冷却剩余 4m 32s (连续失败 2 次)
║     上次错误: HTTP 500 ...
║ 
║ 亲和绑定: 3 个会话
╚═══════════════════════════════╝
[ 🧪 测试模型 ][ ✏ 编辑       ]
[ 🧹 清错误  ][ 🔗 清亲和绑定 ]
[ 🚫 禁用    ][ 🗑 删除       ]
[       ◀ 返回渠道列表         ]
```

操作：
- 「测试模型」→ 打开测试面板（见 9.3.4）
- 「编辑」→ 修改名称/URL/Key/模型/CC 伪装开关（见 9.3.5）
- 「清错误」→ `cooldown.clear(channel_key, None)` 清除该渠道所有模型冷却
- 「清亲和绑定」→ `affinity.delete_by_channel(channel_key)`
- 「禁用/启用」→ 切换 `enabled`，`disabled_reason = "user"`（或 null）
- 「删除」→ 二次确认 → `delete_channel(name)` + state_db 清理

### 9.3.3 添加渠道（5 步向导）

1. **名称（1/5）**：非空、最长 64 字符且不得与已有渠道重名。
2. **URL / 提供商（2/5）**：同页既可输入 custom Base URL，也可从品牌（每页 10 个）→ preset 两级目录选择模板；单 preset 品牌直接应用。preset 保存其身份，但同时立即把所选完整 request endpoint 拆为 resolved `baseUrl/apiPath`。
3. **协议（3/5）**：custom 可选 Anthropic、OpenAI Chat、OpenAI Responses，并保留完整路径冲突确认；preset 只显示自身声明的协议，单协议直接跳过此页。
4. **API Key（4/5）**：校验后进入独立 loading 状态，异步请求模型列表。请求禁用重定向、限制响应体与时长，不向 UI/日志回显 Key 或上游响应正文；取消、重试、返回 Key 或新会话会使迟到结果失效。
5. **模型（5/5）**：发现结果稳定去重并每页显示 10 个，支持跨页多选、完整结果全选/反选；至少选择一项才能确认。发现失败或空结果可重试、返回修改 Key 或复用 `parse_models_input()` 手填。preset 精确使用目录 `models_url`，缺失/失败且存在 `static_models` 时改用静态列表；custom 按同源 `/v1/models` 规则推导。

模型确认后仍进入原测试/保存面板。该面板的单模型按钮也每页最多 10 个；“测试全部”“跳过测试”“保存”和“返回模型选择/手填”语义不变。

#### 完成 → 渠道测试面板

向导完成第 5 步模型选择/手填后，**暂不保存**，先弹测试面板：

```
╔══════════════════════════════╗
║  🧪 渠道测试                  ║
╠══════════════════════════════╣
║ 请选择模型进行联通性测试，
║ 帮助系统了解渠道状态。
║ 至少需要一个模型测试成功才能保存渠道。
╚══════════════════════════════╝
[ glm-5        ] [ glm-5-turbo ]
[     🧪 测试全部模型            ]
[ ⏭ 跳过测试（全部标记可用）   ]
[     ◀ 返回上一步              ]
[     ✕ 取消添加               ]
```

- 点单个模型 → 执行单次测试
- 点「测试全部模型」→ 顺序测试所有模型
- 点「跳过」→ 直接保存渠道，所有模型初始标记为"可用"（不触发 cooldown 初始化）
- 点「返回模型选择/手填」→ 回到第 5 步的发现结果选择页或手填页
- 点「取消添加」→ 丢弃向导数据

测试进行中，**在同一条 TG 消息上持续编辑**（避免刷屏）：

```
🧪 正在测试[智谱Coding Plan Max]渠道 glm-5 模型...
```

每 10s 追加一行（只要还没结束）：
```
🧪 正在测试[智谱Coding Plan Max]渠道 glm-5 模型...
⏳ 调用时长超过 10s...
```

成功：
```
🧪 正在测试[智谱Coding Plan Max]渠道 glm-5 模型...
⏳ 调用时长超过 10s...
✅ 模型测试成功，耗时 17101ms
```

失败：
```
🧪 正在测试[智谱Coding Plan Max]渠道 glm-5 模型...
❌ 模型测试失败，失败原因：模型调用超时 (60s)
```

测试全部：按模型顺序执行，每个模型测试完成后追加到同一条消息末尾，形成累积日志。

测试完成后的弹出选项：

```
╔══════════════════════════════╗
║ 测试完成                      ║
║                              ║
║ 通过 2 / 3 个模型:           ║
║   ✅ glm-5                    ║
║   ✅ glm-5-turbo              ║
║   ❌ gpt-5-dummy（已标为不可用）║
║                              ║
║ 至少有一个模型可用，可保存。  ║
╚══════════════════════════════╝
[ ✅ 保存渠道 ]
[ 🧪 重新测试 ]
[ ✕ 取消     ]
```

- 保存 → `registry.add_channel(...)`，初始化 StateStore 状态；失败的模型写入 `channel_errors`（冷却到永久）或简单地不初始化（保持 ok 状态但排名靠后，交给 recovery probe 后续处理）—— 建议**失败的模型默认不拉黑**，让 recovery probe 机制自然发现是否可用；TG Bot UI 仅在描述中标注"上次测试失败"
- 全部失败时禁止保存，按钮换为「返回模型列表修改」

### 9.3.4 测试模型（已存在渠道）

同上的测试面板，但列表只显示该渠道已配置的模型。测试结果**不修改** cooldown 状态，只反映当时联通性，日志输出到 TG 消息。

### 9.3.5 编辑渠道

进入编辑子菜单：

```
╔══════════════════════════╗
║ ✏ 编辑渠道                ║
╠══════════════════════════╣
║ 智谱 Coding Plan Max
║ URL: https://...
║ Key: sk-xxx***
║ CC 伪装: 开启
║ 模型: 2 个
╚══════════════════════════╝
[ ✏ 修改名称 ][ ✏ 修改 URL    ]
[ ✏ 修改 Key ][ ✏ 修改模型列表 ]
[ 🎭 切换 CC 伪装（当前:开）   ]
[      ◀ 返回渠道详情         ]
```

每项"修改"走文本输入状态机，改完保存 `config.json` 热加载。

注意：
- 修改名称 → StateStore 级联 rename
- 修改模型列表 → 新增模型无历史（默认分）；旧模型若已不在列表，统计保留但不再被调度

### 9.3.6 清除错误（渠道管理根菜单）

```
确认清除所有渠道的所有模型冷却状态？
[ ✅ 确认 ][ ✕ 取消 ]
```

`cooldown.clear_all()`。

### 9.3.7 清空全部亲和绑定

同上弹二次确认，`affinity.delete_all()`。

## 9.4 管理 API Key

（保留 cc-proxy 的 UI，仅补全二次确认）

```
╔═══════════════════════╗
║ 🔑 API Key 管理        ║
╠═══════════════════════╣
║ 当前: 2 个
║   • default
║   • staging
╚═══════════════════════╝
[ ➕ 添加 ][ 🗑 删除 ]
[     ◀ 返回       ]
```

### 添加

1. 输入名称（禁空格）
2. 选择生成方式：
   - 「🎲 自动生成」：生成 `ccp-<48 hex>` → 回显（一次性显示，建议用户保存）
   - 「✏ 自定义输入」：输入 8-256 位自定义 key，允许可见 ASCII 字母数字和 `-_.~+/=`，不允许空格、换行或控制字符；会检查是否与现有 key 重复

### 媒体权限与模型白名单

API Key 详情页提供两个互相独立的媒体开关：

- `allowImages`：「🖼 允许/禁用图片接口」；
- `allowVideos`：「🎬 允许/禁用视频接口」，新建和历史 Key 均默认禁止。

模型白名单页会把 `xaiOAuth.imageModels` / `videoModels` 合并到普通渠道模型列表，并分别用 🖼 / 🎬 标识。媒体调用必须同时满足接口权限开关与 `allowedModels` 白名单；白名单为空仍表示不限制模型，但不会绕过图片/视频权限开关。

### 自定义 key

API Key 详情页可直接改密钥：

- 「🔁 重新生成 key」：二次确认后覆盖为新的 `ccp-<48 hex>`
- 「✏ 自定义新 key」：输入并校验后覆盖为自定义字符串

改 key 后，所有使用旧 key 的下游客户端都需要同步更新。

### 删除

1. 列出所有 key name → 点按钮选一个
2. 二次确认 → 从 config.apiKeys 删除

## 9.5 统计汇总

### 9.5.1 时间范围选择

主菜单点「📊 统计汇总」→

```
╔══════════════════════════╗
║  📊 统计汇总              ║
╠══════════════════════════╣
║ 选择时间范围与维度：       ║
╚══════════════════════════╝
[ 今天 ][ 3天 ][ 7天 ][ 本月 ]
─────────────────────────────
[ 汇总 ][ 按渠道 ][ 按模型 ][ 按 Key ]
[       ◀ 返回主菜单         ]
```

顶部两排按钮：时间范围 × 分组维度。当前选中的按钮标记 ✅。

### 9.5.2 汇总视图（全局）

```
📊 统计 — 今天

Tokens:
↑ 45.2M | ↓ 1.3M | cache 43.0M (95.1%)

请求:
共 1,234 次 | ✅ 1,201 | ❌ 28 | ⏳ 5
成功率 97.3%

缓存:
命中请求 1,098/1,201 (91.4%) | 写入 183/1,201 (15.2%)
读缓存 43.0M (95.1%) | 写缓存 1.2M (2.7%)

耗时（平均）:
连接 180ms | 首字 720ms | 总 4.8s

重试:
共 42 次 | 命中 35 个请求 (2.8%)

亲和:
命中率 82.3%
```

### 9.5.3 按渠道分组

```
📊 按渠道 — 今天

🔐 oauth:user1@gmail.com
  请求 890 | 成功率 98.8% | 命中 812 (92.0%)
  ↑ 30M | ↓ 950K | cache 29M

📡 api:智谱 Coding Plan Max
  请求 310 | 成功率 95.8% | 命中 285 (95.9%)
  ↑ 14M | ↓ 300K | cache 13.5M

📡 api:百度 Coding Plan
  请求 34 | 成功率 100% | 命中 1 (2.9%)
  ↑ 1.2M | ↓ 50K | cache 500K
```

### 9.5.4 按模型 / 按 Key

类似结构，分组字段换成 `requested_model` / `api_key_name`。

## 9.6 最近日志

「📋 最近日志」是日志总入口，顶部可切换：

```text
[✅ 💬 请求日志] [🎞 多媒体日志]
```

### 9.6.1 请求日志

保持原有普通 API 请求流水：分页展示模型、渠道、Token、缓存、连接/首字/总耗时和错误摘要。点 `📄 #N` 进入详情后可查看完整重试链、代理轮次、请求 body 与响应；长内容继续按现有检查器分页或导出。

### 9.6.2 多媒体日志

统一展示以下“一次生成任务一条记录”的业务日志：

- GPT 图片生成 / 编辑；
- Grok 图片生成 / 编辑；
- Grok 视频生成 / 编辑 / 延长。

页面顶部显示 GPT 图片、Grok 图片、视频、成功/失败/进行中/过期数量、已记录 xAI 实际费用及 OAuth 账号排行。OAuth Top 3 只统计当前配置中仍存在的账号，已移除账号的历史任务不会继续占据排行。列表显示模型、数量或视频规格、进度、耗时、API Key 和错误摘要。

视频创建后先记为 `pending`；客户端每次 `GET /v1/videos/{request_id}` 只更新原记录的 `progress / upstream_status / last_polled_at / usage / cost`，不会为轮询新增日志。终态映射为 `success / failed / expired / cancelled`。

详情页包含 API Key、OAuth 账号、模型、尺寸、图片数量或视频时长、创建请求耗时、最终生成耗时、`request_id`、进度、费用与错误。只保存提示词摘要和哈希，不保存完整请求体或 Base64；启用媒体缓存后，GPT/Grok 图片和已完成的 Grok 视频会按统一保留策略落盘，文件仍存在时可从详情页发回 Telegram。详情页只返回多媒体日志，不提供跨到请求日志的按钮。

物理存储继续使用历史 `image_logs.db` / `image_call_logs`。启动迁移只幂等新增多媒体字段；旧 GPT 图片记录自动按 `provider=openai`、`media_type=image` 解释，不删表、不改名、不清空。

## 9.7 系统设置

```
╔═══════════════════════╗
║  ⚙ 系统设置           ║
╠═══════════════════════╣
║ 超时设置
║ 错误冷却阶梯
║ 评分参数
║ 亲和参数
║ CCH 模式
║ 负载均衡入口
║ 首包黑名单
║ 请求日志数据留存
╚═══════════════════════╝
[ 各项设置按钮 ]
[ ◀ 返回主菜单 ]
```

### 9.7.1 超时设置

```
╔═══════════════════════╗
║  ⚙ 超时设置           ║
╠═══════════════════════╣
║ 当前配置：
║   连接:   10s
║   首字:   30s
║   空闲:   30s
║   总:    600s
╚═══════════════════════╝
[ ✏ 修改配置 ]
[ ◀ 返回    ]
```

修改：

```
请输入新的超时配置，格式：
  <连接>,<首字>,<空闲>,<总>
如：10,30,30,600
（单位：秒，总 ≥ 其他三项之和）
```

验证 → 写入 config.timeouts → 回显。

### 9.7.2 错误冷却阶梯

```
当前: [1, 3, 5, 10, 15, 0]  分钟
说明: 连续第 N 次失败进入该阶梯冷却；0 = 永久。
```

修改：

```
请输入新的错误阶梯（分钟，以逗号分隔，末位可为0表示永久）：
如: 1,3,5,10,15,0
```

验证：非负整数数组，长度 ≥ 1。

### 9.7.3 评分参数 / 亲和参数

分别暴露几个关键项，形式同上（当前显示 + 修改）：
- 评分：emaAlpha、recentWindow、errorPenaltyFactor、explorationRate
- 亲和：ttlMinutes

### 9.7.4 CCH 模式

```
当前: disabled
说明: CCH 是 Claude Code 头部签名。disabled = 不加；
      dynamic = 基于 body xxhash 计算。
```

按钮：切换 disabled/dynamic。

### 9.7.5 首包黑名单

```
╔═══════════════════════╗
║  首包黑名单            ║
╠═══════════════════════╣
║ default（所有渠道）:
║   content_policy_violation
║   quota_exceeded
║ 
║ byChannel:
║   智谱 Coding Plan Max:
║     - 特定关键词
╚═══════════════════════╝
[ ➕ 添加 default ][ ➕ 添加渠道特定 ]
[ 🗑 删除         ]
[      ◀ 返回     ]
```

添加操作走文本输入；删除走列表选择。

### 9.7.6 请求日志数据留存

入口：`⚙ 系统设置` → `🗃 数据留存`。

- 留存模式与天数独立显示：默认 `全部保留`；切换到 `按天留存` 后可随时修改 N 天（整数，最少 1 天，无业务上限）。
- 已处于按天留存时，增大天数（如 3 → 5）只更新天数、不触发即时清理；缩短天数（如 5 → 3）会扩大删除范围，必须走两次确认。
- 第一次确认只警告并开始扫描；扫描会逐项列出完整过期月份将删除的 DB 文件、边界月份需删除的记录数与当前文件大小。
- 第二次确认前不写入配置、不删除数据。最终确认后才保存策略并执行清理；计划短期有效，执行前会重新验证数据范围与磁盘余量。
- 完整过期月库删除 `YYYY-MM.db` 及 WAL/SHM sidecar；边界月删除请求摘要、原始请求/响应、重试/代理/本地 Web 关联明细，然后压缩 SQLite 以实际释放空间。
- 不影响 StateStore JSON snapshots、多媒体日志/图片缓存和翻译缓存。清理期间会在同一条 TG 消息中回填进度和实际释放空间。

### 9.7.7 负载均衡

```
当前调度算法:
✅ 智能调度（按滑动窗口评分 + 20% 探索率排序）
顺序调度（按配置顺序依次尝试）
优先级调度（按用户自行设定的优先级）
```

按钮：`smart` / `order` / `priority`。

选择 `priority` 后可分别进入 Anthropic / OpenAI & Grok 协议优先级编辑页，使用序号勾选、置顶/置底/上移/下移和批量设置调整队列；选中按钮显示为 `3 ✅`。

## 9.8 OAuth 重复身份覆盖确认

所有生产 OAuth 新增入口（Claude、OpenAI/Codex、xAI/Grok、Cursor）在完整解析 token/profile 后按 canonical identity 检查重复。不同 identity 直接新增；相同 identity 不再自动跳过、刷新旧 token 或覆盖，而是显示“覆盖 / 取消”确认。

- callback 只携带固定动作和短随机 nonce，不携带 token、email 或 profile 文本；候选凭据只暂存在当前 chat 的 10 分钟内存 state。
- 取消、过期、旧按钮和重复点击均不修改配置、quota、负载均衡或历史统计。
- 确认会先消费 state，再原子校验 expected/current/incoming 三个 canonical key 完全一致，并原地更新原数组项；目标消失或 identity 改变时要求重新登录。
- 覆盖保留 `oauth:<account_key>`、数组位置、优先级、并发/启停设置及 provider 本地偏好；只有确认成功后的新用量 observation 才更新 quota。
- Sub2API / CPA 批量导入在首层文件确认后仅刷新并内存分组；存在重复时追加“新增 N / 覆盖 M / 失败 K”整批确认，取消整批零写入。

## 9.9 权限与状态管理

### Admin 检查

每次 update 进来先走：
```python
if not _is_admin(chat_id):
    send(chat_id, f"⛔ 无权限。你的 Chat ID: {chat_id}")
    return
```
`_is_admin` 从 `config.telegram.adminIds` 取。

### user_state TTL

所有"等待用户输入"的状态写入 `_user_states`（dict），TTL 600s，每 50 轮 poll 清理一次。

### 长消息截断

TG 单消息 4096 字符限制。所有菜单消息末尾加：
```python
if len(text) > 3900:
    text = text[:3900] + "\n\n... (已截断)"
```

### 按钮回调数据格式

统一 `<action>:<param1>:<param2>`，长度 ≤ 64 字节。常见：
- `menu_main`
- `menu_oauth`、`menu_channel`、`menu_apikey`、`menu_stats`、`menu_logs`、`menu_settings`
- `oa_view:<email>`、`oa_refresh:<email>`、`oa_del:<email>`、`oa_del_confirm:<email>`、`oa_login`、`oa_set_json`
- `ch_view:<name>`、`ch_edit:<name>`、`ch_del:<name>`、`ch_test:<name>`、`ch_test_model:<name>:<model>`、`ch_test_all:<name>`
- `wiz_chan_start`、`wiz_chan_step2`、`wiz_chan_step3`、`wiz_chan_step4`、`wiz_chan_test_model:<model>`、`wiz_chan_test_all`、`wiz_chan_skip_test`、`wiz_chan_save`、`wiz_chan_cancel`、`wiz_chan_back`
- `ak_add`、`ak_del`、`ak_del_confirm:<name>`、`ak_del_exec:<name>`
- `stats:<period>:<dim>`（period: 0/3/7/month；dim: all/channel/model/apikey）
- `logs:recent`、`logs_detail:<req_id>`
- `sys_timeouts`、`sys_errwin`、`sys_scoring`、`sys_affinity`、`sys_cch`、`sys_blacklist`、`sys_chansel`
- `back_main`

当名称/模型/email 含冒号或长度超标时，callback_data 中使用 hash 短码（4 字节），服务端用一个短码表映射回实际名。避免直接拼名称导致截断或歧义。

### 🌡 剔除 temperature

渠道编辑页新增「🌡 切换剔除 temperature」开关，默认关闭。

- 开启后，向该渠道上游发送请求前会从 payload 中删除 `temperature` 字段（包括 CC 伪装路径硬编码的 `temperature: 1` 与标准透传路径来自客户端的值）。
- 适用于：第三方 Claude 中转对新模型废弃 `temperature` 参数（典型报错 `temperature is deprecated for this model`）。
- 只对当前渠道生效，其他渠道仍按原行为透传 temperature。


## API 渠道上游额度/用量

渠道列表先显示独立缓存的一行摘要，详情将「上游账户额度/用量」与「Parrot 本地统计」分区。仅 `providerId + providerPresetId` 命中固定 adapter registry 时支持查询；旧渠道和自定义渠道不会按名称或 URL 猜测。首版固定支持 13 个 preset：智谱 `coding-cn/coding-global`，Kimi `code/api-cn/api-global`，DeepSeek `standard`，OpenRouter `standard`，MiniMax `api-cn/api-global/token-cn/token-global`，SiliconFlow `api-cn/api-global`。进入列表/详情及“刷新上游用量”回调都只渲染已有数据并异步请求更新，Telegram handler 不等待 Provider 网络。已有数据时不向用户暴露缓存过期、后台刷新等实现状态；尚无数据时显示中性的“上游用量尚未获取”，保留旧值时仍可提示最近一次更新失败。智谱模型与工具统计按实际查询区间统一标为“近 24 小时”。回调只携带渠道短码和页码。
