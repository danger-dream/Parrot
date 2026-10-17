"""Exact local management details. No upstream refresh and no legacy chat-state mutation."""
from . import subjects, content, actions, store
from .providers import REGISTRY, quota_metrics
from .catalog import CATALOG


def object_lines(event):
    from .. import state_db, oauth_manager, cooldown
    target = subjects.current(event["subject"])
    if target is None:
        return ["原对象已删除或世代已改变；不会定位同名新对象。"]
    a = target.get("account")
    if a is None:
        return []
    subject = subjects.account(a) if event["subject"]["kind"] == "account" else subjects.channel(target["key"])
    lines = ["<b>当前精确对象管理详情（本地快照）</b>", *content.identity(subject.__dict__)]
    enabled = a.get("enabled", True)
    lines += ["调用开关：" + ("启用" if enabled else "暂停"),
              "暂停原因：" + content.text(a.get("disabled_reason") or "无"),
              "并发上限：" + content.text(a.get("maxConcurrent", a.get("max_concurrent", "使用服务默认"))),
              "凭据：" + ("已配置" if any(a.get(k) for k in ("access_token", "model_key", "project_key_id", "apiKey", "api_key")) else "未取得配置状态")]
    if a.get("disabled_until"):
        lines.append("上游重置/暂停截止：" + content.text(a["disabled_until"]))
    if a.get("expired"):
        lines.append("授权到期：" + content.text(a["expired"]))
    if event["subject"]["kind"] == "account":
        selection = oauth_manager.account_model_selection(a)
        models = list(selection.get("models") or [])
        effective = list(selection.get("effective_models") or [])
        lines.append(f"模型目录：{len(models)}；按当前模型开关可路由：{len(effective)}（不保证上游可用）")
        if a.get("provider") == "openai":
            lines.append("允许积分续用：" + ("是" if a.get("allowCredits") is True else "否"))
        row = state_db.quota_load(target["key"]) or {}
        usage = oauth_manager.usage_from_quota_row(row)
        lines += content.fact_lines({"metrics": quota_metrics(usage)}, detail=True)[:4]
        if not row:
            lines.append("额度快照：尚未取得；查看未发起网络查询。")
    else:
        lines += ["协议：" + content.text(a.get("type", a.get("protocol", "未取得"))),
                  "已配置模型：" + content.text(" / ".join(str(m) for m in a.get("models", [])), 600)]
    return lines


def records(event, actor):
    target = subjects.current(event["subject"])
    if not target:
        return ["原对象已删除；不读取同名新账户账本。"]
    provider = REGISTRY.get(event["subject"]["provider"])
    lines = ["<b>真实操作账本（本地）</b>"]
    if provider and provider.records and actions._control is not None:
        values = provider.records(actions._control, actions._context(actor), target["key"], target["account"])
        for row in values[-10:]:
            lines.append(content.text(row.get("action", "操作")) + "：" + content.text(row.get("status", "unknown"))
                         + ("；业务日期 " + content.text(row["business_date"]) if row.get("business_date") else ""))
    for row in store.items("policy").values():
        if row.get("kind") == "operation" and row.get("generation") == event["subject"]["generation"]:
            lines.append(content.text(row["action"]) + "：" + content.text(row["status"]) + " · #" + content.text(row["id"]))
    if len(lines) == 1:
        lines.append("当前没有保留的业务操作记录；这不表示未发生过操作。")
    lines.append("unknown 表示待核对，不会自动重复提交。")
    return lines


def request_lines(event, actor):
    rid = event["facts"].get("request_id")
    if not rid:
        return ["未取得请求编号；仅保留本通知中的观测证据。"]
    from ..management_control.observability.logs import LogsControl
    try:
        value = LogsControl().detail(actions._context(actor), rid)
    except Exception:
        return ["关联请求记录不存在或已过期；以下为事件时的模型证据。", *content.fact_lines(event["facts"], detail=True)]
    log = value.get("log") or {}
    fields = (("id", "请求编号"), ("status", "状态"), ("httpStatus", "HTTP状态"), ("requestedModel", "请求模型"),
              ("finalModel", "出站模型"), ("upstreamActualModel", "上游报告模型"), ("totalMs", "耗时(ms)"), ("createdAt", "发生时间"))
    lines = ["<b>关联请求实际记录</b>"]
    lines += [label + "：" + content.text(log[key]) for key, label in fields if log.get(key) is not None]
    lines.append("重试记录数量：" + str(len(value.get("attempts") or [])))
    lines += content.fact_lines({k: v for k, v in event["facts"].items() if k in {"signals", "requested_model", "outbound_model", "actual_model"}}, detail=True)
    return lines


def storage_lines():
    from .. import config
    from ..openai import store as response_store
    from pathlib import Path
    import stat
    cfg = (config.get().get("openai") or {}).get("store") or {}
    lines = ["<b>Responses 历史存储当前状态（只读）</b>", "配置启用：" + str(cfg.get("enabled", True)),
             "TTL(秒)：" + content.text(cfg.get("ttlSeconds", "使用默认")),
             "本进程已初始化：" + str(response_store._initialized)]
    raw = response_store._db_path
    if not raw:
        lines.append("尚未取得已初始化的存储路径；没有创建或重建数据库。")
    else:
        try:
            info = Path(raw).stat()
            lines += ["已初始化文件：" + content.text(Path(raw).name), "文件大小：" + str(info.st_size) + " 字节",
                      "权限：" + oct(stat.S_IMODE(info.st_mode))]
        except OSError:
            lines.append("已初始化文件当前不可读取；未执行写入/修复。")
    return lines


def route_lines(event):
    from .. import config, oauth_manager, oauth_ids
    model = event["facts"].get("model", "")
    lines = ["<b>该模型的筛选与渠道详情</b>", "模型：" + content.text(model), "入口：" + content.text(event["facts"].get("ingress", "未取得")),
             "当时排除原因：", *content.fact_lines({"exclusions": event["facts"].get("exclusions", [])}, detail=True), "当前本地已配置的模型候选（未发探测）："]
    choices = []
    for account in config.get().get("oauthAccounts", []):
        selection = oauth_manager.account_model_selection(account)
        if model not in selection.get("models", []):
            continue
        subject = subjects.account(account)
        lines.append(content.text(subject.service + " · " + (subject.label or subject.identity or subject.short_id)) + "：" + ("启用" if account.get("enabled", True) else "暂停"))
        choices.append(subject)
    for channel in config.get().get("channels", []):
        if model not in [m if isinstance(m, str) else m.get("alias", m.get("real")) for m in channel.get("models", [])]:
            continue
        subject = subjects.channel("api:" + channel["name"])
        lines.append(content.text(subject.label) + "：" + ("启用" if channel.get("enabled", True) else "暂停"))
        choices.append(subject)
    if not choices:
        lines.append("当前配置未找到显式包含此模型的渠道；别名/动态规则以现有路由规则为准。")
    return lines, choices


def model_lines(event):
    from .. import oauth_manager, cooldown
    target = subjects.current(event["subject"])
    if not target:
        return ["原对象已删除；仅能查看历史模型证据。"]
    a = target["account"]
    if event["subject"]["kind"] == "account":
        selection = oauth_manager.account_model_selection(a)
        models = list(selection.get("models") or [])
        disabled = set(selection.get("disabled_models") or [])
        channel = "oauth:" + target["key"]
    else:
        models = [m if isinstance(m, str) else str(m.get("alias") or m.get("real") or "") for m in a.get("models", [])]
        disabled = set(a.get("disabledModels") or [])
        channel = target["key"]
    selected = [event["scope_ref"]] if event["scope"] == "model" else event["facts"].get("models", models) if event["scope"] == "pool" else models
    lines = ["<b>指定模型/额度池当前本地状态</b>"]
    for model in selected[:40]:
        state = cooldown.get_state(channel, model) or {}
        status = "不在当前目录" if model not in models else "被模型开关暂停" if model in disabled else "模型开关启用"
        if cooldown.is_blocked(channel, model):
            status += "；永久冻结" if state.get("cooldown_until") == -1 else "；仍在本地冷却"
        else:
            status += "；无本地冷却"
        lines.append(content.text(model) + "：" + content.text(status))
    if not selected:
        lines.append("当前没有已知模型；未请求上游目录。")
    if len(selected) > 40:
        lines.append("当前页显示前40项；通知中的新增/移除摘要保留在事件详情。")
    lines.append("账户开关和其他路由限制独立生效；这里不保证上游调用成功。")
    return lines
