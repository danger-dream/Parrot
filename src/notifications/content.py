"""Network-free HTML rendering. Bound text before escaping, never slice HTML."""
from datetime import datetime, timezone, timedelta
from html import escape
from .catalog import CATALOG
from .events import EFFECTS

_BEIJING = timezone(timedelta(hours=8))
LABELS = {"expires_at": "授权到期", "reset_at": "重置时间", "retry_at": "下次尝试", "pool": "额度池",
          "windows": "触发窗口", "models": "涉及模型", "count": "模型数量", "threshold": "暂停阈值",
          "error_code": "错误代码", "error_reason": "原因",
          "model": "模型", "requested_model": "请求模型", "outbound_model": "发出模型", "actual_model": "上游返回",
          "api_key_name": "来源 Key", "request_id": "请求编号", "response_id": "响应编号",
          "ingress": "入口", "operation": "操作", "result": "结果", "business_date": "业务日期", "previous": "此前"}
INGRESS = {"responses": "Responses API", "responses_ws": "Responses API（WebSocket）",
           "chat": "Chat Completions", "anthropic": "Anthropic Messages"}
WINDOWS = {"5h": "5 小时", "7d": "7 天", "primary": "主窗口", "secondary": "次窗口"}
# An empty translation means the code only repeats another line (e.g. the result).
ERROR_CODES = {"repeated_upstream_failure": "连续多次上游失败", "result_unknown": ""}
RESULTS = {"unknown": "未知", "pending": "处理中", "failed": "失败", "succeeded": "成功"}
STAGES = {"succeeded": "已完成", "failed": "未完成", "unknown": "结果未核实", "running": "仍在后台进行",
          "skipped": "按配置跳过", "partial_failed": "部分完成"}
# Scheduler exclusion codes (scheduler._filter_candidates / model_registry Switches.blocked).
EXCLUSIONS = {"global_model_disabled": "模型已在模型中心全局停用", "source_model_disabled": "此渠道未启用该模型",
              "resolved_model_disabled": "映射后的目标模型已停用", "api_key_channel_denied": "该 Key 无权使用此渠道",
              "disabled": "渠道或账号已停用", "disabled_reason": "账号已暂停", "cooldown": "冷却中",
              "protocol_guard": "渠道不支持这次请求用到的能力", "saturated": "并发已满"}
PAUSE_REASONS = {"quota": "额度用尽", "auth_error": "授权失效", "user": "手动停用"}
ROUTE_ADVICE = (
    ({"global_model_disabled", "source_model_disabled", "resolved_model_disabled"}, "在模型详情中启用该模型，或让 {key} 改用其他模型。"),
    ({"api_key_channel_denied"}, "在 Key 详情中放开 {key} 的渠道权限。"),
    ({"cooldown"}, "等冷却结束，或在模型详情中清除故障。"),
    ({"disabled", "disabled_reason"}, "到对应账户或渠道详情中恢复启用。"),
    ({"protocol_guard"}, "请求用到了这些渠道不支持的能力，详见调用日志。"),
    ({"saturated"}, "渠道并发已满，稍后重试。"),
)
PLAN_FAMILIES = {"quota", "model.quota", "entitlement", "credits", "benefit"}


def text(value, limit=180):
    value = str(value)
    return escape(value[:limit] + ("…" if len(value) > limit else ""), quote=False)


def moment(value, *, seconds=False):
    """ISO text or epoch seconds/milliseconds as Beijing time; anything else is shown as given."""
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            number = float(value)
            dt = datetime.fromtimestamp(number / 1000 if number > 1e12 else number, _BEIJING)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return str(value)
            dt = dt.astimezone(_BEIJING)
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def _number(value):
    return f"{value:g}" if isinstance(value, float) else str(value)


def identity(s, *, plan=False):
    """One line naming the exact account/channel; route and system subjects have none."""
    if s["kind"] not in {"account", "channel"}:
        return []
    primary = s.get("label") or s.get("identity")
    if not primary:
        return [("渠道" if s["kind"] == "channel" else "账号") + "：未命名（#" + text(s["short_id"]) + "）"]
    extras, seen = [], {primary.casefold()}
    if s.get("identity") and s["identity"].casefold() not in seen:
        extras.append(s["identity"])
        seen.add(s["identity"].casefold())
    for label, value in s.get("scopes") or []:
        # Unnamed scopes are local hashes: they identify nothing for a reader.
        if value.startswith("未命名") or value.casefold() in seen:
            continue
        extras.append(label + " " + value)
        seen.add(value.casefold())
    if plan and s.get("plan"):
        extras.append("套餐 " + s["plan"])
    line = ("渠道" if s["kind"] == "channel" else "账号") + "：" + text(primary, 120)
    return [line + ("（" + text(" · ".join(extras), 200) + "）" if extras else "")]


def origin_lines(facts):
    lines = []
    if facts.get("api_key_name"):
        lines.append("来源 Key：" + text(facts["api_key_name"], 80))
    if facts.get("ingress"):
        lines.append("入口：" + text(INGRESS.get(facts["ingress"], facts["ingress"]), 80))
    return lines


def _metric(m):
    unit = str(m.get("unit", ""))
    gap = "" if unit in {"", "%"} else " "
    parts = [f"{label} {_number(m[field])}{gap}{unit}" for field, label in
             (("used", "已用"), ("remaining", "剩余"), ("total", "总额"), ("threshold", "阈值")) if field in m]
    if not parts:
        parts.append("数值未取得")
    if m.get("reset_at"):
        when = moment(m["reset_at"])
        parts.append({"upstream": when + " 重置", "estimated": "预计 " + when + " 重置"}.get(
            m.get("reset_certainty", "unknown"), when + " 重置（未确认）"))
    return text(m.get("display_name") or m.get("metric_id") or "额度") + "：" + text("，".join(parts), 300)


def _exclusion_note(reason, detail):
    detail = str(detail or "")
    if not detail:
        return ""
    if reason == "cooldown":
        if detail == "permanent":
            return "永久冻结"
        until = detail.rsplit("until=", 1)[-1] if "until=" in detail else ""
        parts = ["套餐不含此模型"] if detail.startswith("plan-excluded") else []
        if until.isdigit():
            parts.append("至 " + moment(int(until)))
        return "，".join(parts)
    if reason == "disabled_reason":
        return PAUSE_REASONS.get(detail, detail)
    return detail


def _exclusion_lines(items, detail):
    if not items:
        return ["没有任何渠道提供此模型"]
    groups = {}
    for item in items:
        groups.setdefault(item.get("reason") or "unknown", []).append(item)
    limit = 12 if detail else 5
    lines = []
    # Largest cause first; equal sizes keep the scheduler's order.
    for reason, members in sorted(groups.items(), key=lambda pair: -len(pair[1])):
        lines.append(text(EXCLUSIONS.get(reason) or "其他原因（" + reason + "）") + f"（{len(members)} 个渠道）")
        for item in members[:limit]:
            note = _exclusion_note(reason, item.get("detail"))
            lines.append("· " + text(item.get("subject") or "未知渠道", 120) + ("（" + text(note, 120) + "）" if note else ""))
        if len(members) > limit:
            lines.append(f"· …另有 {len(members) - limit} 个")
    return lines


def _item_lines(key, value, detail):
    limit = 12 if detail else 6
    lines = []
    for item in value[:limit]:
        if key == "signals":
            line = str(item.get("source", "信号")) + "：" + " / ".join(item.get("models") or [])
        else:
            status = item.get("status", item.get("count", "未核实"))
            if key == "stages":
                status = STAGES.get(status, status)
            line = str(item.get("name", "阶段")) + "：" + str(status)
            if key == "stages" and "count" in item:
                line += "（" + str(item["count"]) + "）"
            if item.get("expires_at"):
                line += "，到期 " + moment(item["expires_at"])
        lines.append(text(line, 400 if detail else 200))
    if len(value) > limit:
        lines.append(f"…另有 {len(value) - limit} 项")
    return lines


def fact_lines(facts, *, detail=False, labels=None, show=None):
    labels = {**LABELS, **dict(labels or ())}
    metrics = facts.get("metrics") or []
    lines = []
    for key in [k for k in (show or facts) if k in facts]:
        value = facts[key]
        if key in {"retryable", "auth_error", "saved", "advice", "api_key_name", "ingress", "request_id"}:
            continue
        if key == "metrics":
            lines += [_metric(m) for m in value]
        elif key == "windows":
            if value and not metrics:
                lines.append(labels["windows"] + "：" + text(" / ".join(WINDOWS.get(w, w) for w in value)))
        elif key in {"added", "removed", "models"}:
            label = {"added": "新增模型", "removed": "移除模型"}.get(key, labels["models"])
            lines.append(f'{label}（{len(value)}）：' + text(" / ".join(value), 650 if detail else 300))
        elif key == "exclusions":
            lines += _exclusion_lines(value, detail)
        elif key in {"signals", "benefits", "inventory", "stages"}:
            lines += _item_lines(key, value, detail)
        elif key == "error_title":
            reason = facts.get("error_reason")
            lines.append(text(value) + ("：" + text(reason, 400) if reason else ""))
        elif key == "error_reason":
            if not facts.get("error_title"):
                lines.append(labels["error_reason"] + "：" + text(value, 400))
        elif key == "error_code":
            if not facts.get("error_title") and ERROR_CODES.get(value) != "":
                lines.append(text(ERROR_CODES.get(value) or labels["error_code"] + "：" + value))
        elif key == "threshold":
            lines.append(labels["threshold"] + "：" + _number(value) + "%")
        elif key in {"expires_at", "reset_at", "retry_at"}:
            if not (key == "reset_at" and any(m.get("reset_at") for m in metrics)):
                lines.append(labels[key] + "：" + text(moment(value)))
        elif key == "result":
            lines.append(labels["result"] + "：" + text(RESULTS.get(value, value), 300))
        elif key in labels:
            lines.append(labels[key] + "：" + text(value, 650 if detail else 300))
    return lines


def effect_text(effect):
    if effect["status"] in {"unknown", "failed"} and effect["code"] not in {"disable_failed", "action_unknown", "action_failed", "history_unavailable", "not_saved"}:
        return "实际影响尚未确认，请打开详情核对当前状态。"
    return EFFECTS[effect["code"]]


def _route_advice(facts):
    reasons = [item.get("reason") for item in facts.get("exclusions") or []]
    if not reasons:
        return "检查模型名是否正确，或在模型中心为它添加来源。"
    key = facts.get("api_key_name") or "客户端"
    ranked = sorted(((sum(r in codes for r in reasons), tip) for codes, tip in ROUTE_ADVICE), key=lambda pair: -pair[0])
    return "".join(tip.format(key=key) for count, tip in ranked[:2] if count)


def advice_text(event):
    spec, facts = CATALOG[event["event_type"]], event["facts"]
    if event["event_type"] == "routing.unavailable":
        tip = _route_advice(facts)
    elif facts.get("advice"):
        tip = facts["advice"]
    elif event["event_type"] == "account.auth.refresh_failed" and (facts.get("auth_error") or facts.get("retryable") is False):
        tip = "点「重新授权」重新登录。"
    else:
        tip = spec.next_step
    return "处理：" + text(tip, 300) if tip else ""


def headline(event):
    spec, s, facts = CATALOG[event["event_type"]], event["subject"], event["facts"]
    title = spec.title
    if event["effect"]["status"] in {"failed", "unknown"} and event["effect"]["code"] in {"account_paused", "model_paused"}:
        title += "（待核实）"
    if s["kind"] in {"account", "channel"}:
        return s["service"] + " · " + title
    if s["kind"] == "route" and facts.get("model"):
        return title + " · " + facts["model"]
    return title


def render(event, *, detail=False, current_status=None):
    spec = CATALOG[event["event_type"]]
    s, facts = event["subject"], event["facts"]
    icon = "✅" if event["phase"] == "recovered" else {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}[event["severity"]]
    head = [f'{icon} <b>{text(headline(event), 200)}</b>', *identity(s, plan=spec.family in PLAN_FAMILIES), *origin_lines(facts)]
    body = fact_lines({key: facts[key] for key in spec.fields if key in facts}, detail=detail, labels=spec.labels, show=spec.show)
    closing = [line for line in (effect_text(event["effect"]), advice_text(event)) if line]
    when = datetime.fromtimestamp(event["occurred_at"], _BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    closing.append(when)
    if current_status:
        closing.append("当前状态：" + text(current_status))
    if spec.auto_delete and not detail:
        closing.append(f"⏱ 本消息将在 {spec.auto_delete} 秒后自动删除")

    def compose():
        return "\n".join([*head, *([""] + body if body else []), "", *closing])
    # Individual lines were escaped after truncation. Remove whole fact lines,
    # never cut entities/tags. The headline, effect and advice always remain.
    while len(compose()) > 3600 and body:
        body.pop()
    if len(compose()) > 3900:
        head, body = head[:1], []
    return compose()
