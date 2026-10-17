"""Network-free HTML rendering. Bound text before escaping, never slice HTML."""
from datetime import datetime, timezone, timedelta
from html import escape
from .catalog import CATALOG
from .events import EFFECTS

LABELS = {"expires_at": "授权到期", "reset_at": "上游给出的重置时间", "retry_at": "预计下次尝试", "pool": "额度池",
          "windows": "额度窗口", "models": "涉及模型", "count": "模型数量", "threshold": "暂停阈值(%)",
          "error_code": "详情码", "error_title": "错误类别", "error_reason": "原因", "advice": "建议",
          "model": "模型", "requested_model": "请求模型", "outbound_model": "出站模型", "actual_model": "上游报告模型",
          "api_key_name": "下游 Key 名称（非凭据）", "request_id": "请求编号", "response_id": "响应编号",
          "ingress": "入口协议", "operation": "操作", "result": "结果", "business_date": "业务日期", "previous": "此前限制"}


def text(value, limit=180):
    value = str(value)
    return escape(value[:limit] + ("…" if len(value) > limit else ""), quote=False)


def identity(s):
    lines = []
    if s.get("label"):
        lines.append("账户：" + text(s["label"]))
    if s.get("identity") and s.get("identity") != s.get("label"):
        lines.append("账号：" + text(s["identity"]))
    if not s.get("identity") and not s.get("label"):
        lines.append("未命名账户")
    lines.append("本地编号：#" + text(s["short_id"]))
    for label, value in s.get("scopes") or []:
        lines.append(text(label) + "：" + text(value))
    return lines


def fact_lines(facts, *, detail=False, labels=None):
    labels = {**LABELS, **dict(labels or ())}
    lines = []
    for key, value in facts.items():
        if key in {"retryable", "auth_error", "saved"}:
            continue
        if key == "metrics":
            for m in value:
                parts = [text(m.get("display_name") or m.get("metric_id") or "额度")]
                for field, label in (("used", "已用"), ("remaining", "剩余"), ("total", "总额"), ("threshold", "阈值")):
                    if field in m:
                        parts.append(f'{label} {text(m[field])}{text(m.get("unit", ""))}')
                if len(parts) == 1:
                    parts.append("数值未取得")
                if m.get("reset_at"):
                    certainty = m.get("reset_certainty", "unknown")
                    label = {"upstream": "上游给出的重置时间", "estimated": "预计重置时间", "unknown": "重置时间（确定性未取得）"}[certainty]
                    value = m["reset_at"]
                    try:
                        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        if dt.tzinfo is not None:
                            value = dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S 北京时间")
                    except (ValueError, TypeError):
                        pass
                    parts.append(label + "：" + text(value))
                lines.append(" · ".join(parts))
        elif key in {"added", "removed", "models", "windows"}:
            label = {"added": "新增模型", "removed": "移除模型"}.get(key, labels.get(key, key))
            if key == "windows" and not value:
                lines.append("具体额度窗口未取得")
            else:
                lines.append(f'{label}（{len(value)}）：' + text(" / ".join(value), 650 if detail else 180))
        elif key in {"signals", "exclusions", "benefits", "inventory", "stages"}:
            for item in value[:12 if detail else 3]:
                if key == "signals":
                    line = str(item.get("source", "信号")) + "：" + " → ".join(item.get("models") or [])
                elif key == "exclusions":
                    line = str(item.get("subject", "候选")) + "：" + str(item.get("reason", "未知"))
                    if detail and item.get("detail"):
                        line += "（" + str(item["detail"]) + "）"
                else:
                    status = item.get("status", item.get("count", "未核实"))
                    if key == "stages":
                        status = {"succeeded": "已完成", "failed": "未完成", "unknown": "结果未核实", "running": "仍在后台进行", "skipped": "按配置跳过", "partial_failed": "部分完成"}.get(status, status)
                    line = str(item.get("name", "阶段")) + "：" + str(status)
                    if key == "stages" and "count" in item:
                        line += "（" + str(item["count"]) + "）"
                    if item.get("expires_at"):
                        line += "；到期 " + str(item["expires_at"])
                lines.append(text(line, 400 if detail else 180))
            if len(value) > (12 if detail else 3):
                lines.append("其余内容见对应对象详情。")
        elif key in LABELS:
            if key in {"expires_at", "reset_at", "retry_at"}:
                try:
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if dt.tzinfo is not None:
                        value = dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S 北京时间")
                except (ValueError, TypeError):
                    pass
            lines.append(labels[key] + "：" + text(value, 650 if detail else 180))
    return lines


def effect_text(effect):
    if effect["status"] in {"unknown", "failed"} and effect["code"] not in {"disable_failed", "action_unknown", "action_failed", "history_unavailable", "not_saved"}:
        return "当前影响尚未确认；不能据此断言已恢复或已禁用。"
    return EFFECTS[effect["code"]]


def render(event, *, detail=False, current_status=None):
    spec = CATALOG[event["event_type"]]
    s = event["subject"]
    icon = "✅" if event["phase"] == "recovered" else {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}[event["severity"]]
    title = spec.title if event["effect"]["status"] not in {"failed", "unknown"} else spec.title.replace("已暂停", "限制待核实").replace("永久冻结", "冻结状态待核实")
    lines = [f'{icon} <b>{text(s["service"])} · {text(title)}</b>' , *identity(s)]
    if s.get("plan") and spec.family in {"quota", "model.quota", "entitlement", "credits", "benefit"}:
        lines.append("套餐：" + text(s["plan"]))
    facts = fact_lines({key: event["facts"][key] for key in spec.fields if key in event["facts"]}, detail=detail, labels=spec.labels)
    lines += ["", *(facts if detail else facts[:4])]
    if not detail and len(facts) > 4:
        lines.append("更多事实见详情。")
    if event.get("previous_event") and event["phase"] == "recovered":
        lines.append("此前出现的限制已解除；保留故障历史，当前状态见详情。")
    effect = event["effect"]
    lines.append(effect_text(effect))
    lines.append(spec.next_step)
    when = datetime.fromtimestamp(event["occurred_at"], timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    lines.append("发生时间：" + when + "（北京时间）")
    if detail:
        lines += ["来源：" + text(event["source"]), "当前状态：" + text(current_status or "未查询")]
    if spec.auto_delete and not detail:
        lines.append(f"⏱ 本消息将在 {spec.auto_delete} 秒后自动删除")
    # Individual lines were escaped after truncation. Remove whole fact lines,
    # never cut entities/tags. A short identity/effect always remains.
    while len("\n".join(lines)) > 3600 and facts:
        candidate = facts.pop()
        if candidate in lines:
            lines.remove(candidate)
    if len("\n".join(lines)) > 3900:
        lines = lines[:2] + ["详细内容请查看事件记录。", effect_text(effect), spec.next_step, "发生时间：" + when]
    return "\n".join(lines)
