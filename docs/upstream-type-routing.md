# 按上游类型选择出站路由

入口：Telegram「网络设置 → 路由规则 → 功能路由」。固定显示 Telegram、OpenAI、Grok、Cursor、Antigravity、WorkBuddy、z.ai / 智谱、Anthropic，不依赖已添加账户。

## 配置与优先级

`network.routing.providers` 的类型键：`openai`、`xai`、`cursor`、`antigravity`、`workbuddy`、`zhipu`、`claude`。目标仍为已有代理名、代理组名或 `direct`。

```json
{
  "default": "direct",
  "telegram": "direct",
  "providers": {"openai": "us", "workbuddy": "direct"}
}
```

以上是 `network.routing` 子树示例，`us` 必须是已配置代理或代理组；不要直接覆盖现有路由配置。

优先级：**账号／渠道 > 模型 > 上游类型 > 旧 purpose／OAuth 规则 > 默认路由**。账号和渠道同一优先级；同时提供且都命中时保留原先账号优先的行为。显式 `direct` 是覆盖规则，不等于未设置。清除类型规则后恢复继承，并非强制直连。

- OAuth 按接入来源识别。WorkBuddy 请求 Claude 模型仍属于 WorkBuddy；智谱的 Anthropic 协议请求仍属于智谱。
- API 渠道仅依据显式 `providerId`；`anthropic` 归到 `claude` 类型。未知／未填写供应商的自定义渠道不根据协议、模型名或 URL 猜测类型，继续走原有规则。
- 登录前请求由提供商专用请求上下文识别，无需提前存在账户。登录、刷新、额度、模型发现、HTTP／WS、媒体与已知渠道的监测请求使用同一类型解析器。Cursor 本机桥接仍保持本机直连，其远端出口由桥接上游链路选择。
- Telegram 独立使用原有 `telegram` 功能规则。
- `directFallback`、代理组顺序及故障回退语义不变。显式代理不可用时，未开启直连兜底便保持失败。

## 旧配置兼容

不自动复制、删除或改写旧规则。未设置类型覆盖时，原请求使用哪个 purpose 就继续使用哪个，随后按原有 `oauth` 通用规则与 `default` 回退。

WorkBuddy、智谱辅助请求的新标识分别为 `oauth_workbuddy`、`oauth_zhipu`，无类型覆盖时仍回退原来的 `oauth_openai`；核心 OpenAI／Anthropic 连接监测分别标识为 `core_openai`、`core_claude`，无类型覆盖时仍回退原来的 `core_monitor`。原有 Cursor、Grok、Antigravity 独立 purpose 保持不变。模型请求的旧协议家族 purpose 也保留为兼容回退，因此不把不同旧路径强行合并。

已有旧规则在功能路由页的「旧规则兼容」区域可继续修改或清除。

## Management API

`GET/PATCH /api/management/v1/proxy-routing` 增加 `providers` 映射，原有 `functions`、`accounts`、`channels`、`models` 字段保持兼容。

```json
{"providers": {"workbuddy": "direct", "openai": "us"}}
```

PATCH 中 `{"providers":{"workbuddy":null}}` 仅清除该类型覆盖；省略的类型不变。供应商类型不要求已创建账户。类型键和代理目标会校验；代理／组重命名、引用冲突检查与 Telegram 删除清理均包含类型规则。

## 网络设置统计

首页只展示当前配置中仍存在的代理和 `direct` 的历史统计，先过滤再取前五项。已删代理的历史日志不删除，代理组的统计聚合仍使用完整统计结果。
