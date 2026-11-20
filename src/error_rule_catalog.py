"""Error rule vocabulary: categories with their recommended handling, presets, built-ins.

Pure data, shared by the rule engine (:mod:`src.error_rules`) and the WebUI.
"""

from __future__ import annotations

import copy
from typing import Any

PRESET_PREFIX = "preset."


# ─── condition and step builders ────────────────────────────────────

def group(mode: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "group", "mode": mode, "items": list(items)}


def cond(field: str, op: str, *, value: Any = None, values: list[str] | None = None, path: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"kind": "cond", "field": field, "op": op}
    if path:
        out["path"] = path
    if value is not None:
        out["value"] = str(value)
    if values is not None:
        out["values"] = list(values)
    return out


def retry(delay: int = 2, times: int = 1) -> dict[str, Any]:
    return {"type": "retry", "delaySeconds": delay, "times": times}


def pause(mode: str = "fixed", *, scope: str = "model", seconds: int = 900, fallback: int = 900,
          boundary: int = 0) -> dict[str, Any]:
    return {"type": "pause", "scope": scope, "mode": mode, "seconds": seconds, "pattern": "",
            "timezone": "+08:00", "boundaryHours": boundary, "fallbackSeconds": fallback}


def notify(repeat: int = 6) -> dict[str, Any]:
    return {"type": "notify", "repeatHours": repeat}


def disable(reason: str = "") -> dict[str, Any]:
    return {"type": "disable", "reason": reason}


DEPRIORITIZE = {"type": "deprioritize"}


def _handling(steps: list[dict[str, Any]], final: str = "next", *, status: int = 0,
              retry_after: bool = False) -> dict[str, Any]:
    return {
        "steps": steps,
        "final": final,
        "thresholds": [],
        "response": {"status": status, "message": "", "retryAfter": retry_after},
    }


CATEGORY_ORDER = (
    "rate_limit", "quota_exhausted", "plan_excluded", "account_blocked",
    "request_invalid", "content_blocked", "upstream_busy", "observe",
)
CATEGORIES: dict[str, dict[str, Any]] = {
    "rate_limit": {
        "label": "短时限流", "description": "请求太频繁，很快就能恢复（每分钟请求数、令牌数之类的限制）。",
        "handling": _handling([pause("retry_after", seconds=60, fallback=60)], status=429, retry_after=True),
    },
    "quota_exhausted": {
        "label": "额度用尽", "description": "套餐或积分用完了，要等到重置时间才会恢复。",
        "handling": _handling([pause("message_time"), notify(6)], status=429, retry_after=True),
    },
    "plan_excluded": {
        "label": "套餐不含此模型", "description": "账号的套餐不支持这个模型，不会自己恢复。",
        "handling": _handling([pause("fixed", seconds=900), notify(24)]),
    },
    "account_blocked": {
        "label": "账号被拒绝", "description": "欠费、套餐到期、被封禁等，需要人工处理后才能恢复。",
        "handling": _handling([disable(), notify(24)]),
    },
    "request_invalid": {
        "label": "请求本身有误", "description": "参数不合法、内容超长等，换哪个渠道都会同样失败。",
        "handling": _handling([], "respond"),
    },
    "content_blocked": {
        "label": "内容被拦截", "description": "上游的安全审核拒绝了这次请求。",
        "handling": _handling([], "respond"),
    },
    "upstream_busy": {
        "label": "上游繁忙", "description": "服务器过载或临时故障，稍后重试通常能成功。",
        "handling": _handling([retry(2, 1), pause("ladder"), DEPRIORITIZE]),
    },
    "observe": {
        "label": "只做标记", "description": "只在日志里标记、按需发通知，处理仍交给后面的规则和默认策略。",
        "handling": _handling([], "pass"),
    },
}

_DEFAULT_MESSAGES = {
    "quota_exhausted": "Upstream quota for {model} is exhausted. Expected to recover at {reset_at} (in {reset_in}).",
    "rate_limit": "Upstream rate limit for {model}. Retry in {reset_in}.",
    "plan_excluded": "The upstream plan does not include {model}.",
    "account_blocked": "The upstream account for {model} is unavailable: {upstream_message}",
}

# Code-owned handlers that stay outside the rule table; shown read-only.
BUILTINS: tuple[dict[str, str], ...] = (
    {"id": "oauth-refresh", "name": "OAuth 凭据自动刷新",
     "description": "OAuth 账号返回 401/403 时先刷新一次凭据再重试；刷新失败才算这个账号出错。",
     "setting": "系统设置 → 重试", "link": "/settings?section=retry"},
    {"id": "transient-retry", "name": "上游繁忙同渠道重试",
     "description": "OpenAI server_error/overloaded、Claude overloaded（529）、xAI 503 等明确的临时错误，在同一渠道按退避间隔重试。",
     "setting": "系统设置 → 重试", "link": "/settings?section=retry"},
    {"id": "antigravity", "name": "Antigravity 额度与积分",
     "description": "识别 Google 的限流、额度和人工验证要求。账号允许用积分时，额度用尽会先用积分重试一次——这一步在规则之前执行，积分也用不了时才交给规则。",
     "setting": "OAuth 账户 → Antigravity", "link": "/oauth"},
    {"id": "codex-usage", "name": "Codex 用量窗口",
     "description": "读取 ChatGPT 账号返回的用量窗口，用尽时按窗口重置时间暂停账号。",
     "setting": "OAuth 账户 → OpenAI", "link": "/oauth"},
    {"id": "claude-unified", "name": "Claude 统一限额",
     "description": "识别 Claude 5 小时 / 7 天统一限额，按限额重置时间暂停账号。",
     "setting": "OAuth 账户 → Claude", "link": "/oauth"},
    {"id": "protocol-recovery", "name": "协议级自动修复",
     "description": "思考历史被拒、encrypted_content 失效、1M 上下文额度不足等情况，同渠道去掉相应内容后重试一次。这一步在规则之前执行。",
     "setting": "系统设置 → 重试", "link": "/settings?section=retry"},
    {"id": "default-ladder", "name": "默认冷却阶梯",
     "description": "以上都没处理的失败：换下一个渠道，并按「错误冷却」的阶梯逐级暂停这个渠道的模型。",
     "setting": "系统设置 → 错误冷却", "link": "/settings?section=cooldown"},
)


def _preset(pid: str, group_name: str, name: str, description: str, when: dict[str, Any],
            category: str, **overrides: Any) -> dict[str, Any]:
    handling = copy.deepcopy(CATEGORIES[category]["handling"])
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(handling.get(key), dict):
            handling[key].update(value)
        else:
            handling[key] = value
    return {
        "id": PRESET_PREFIX + pid, "name": name, "enabled": True, "note": description,
        "group": group_name, "when": when, "category": category, **handling,
    }


def _zhipu(*items: dict[str, Any]) -> dict[str, Any]:
    return group("all", cond("provider", "in", values=["zhipu"]), *items)


def _zhipu_block(pid, name, codes, words, reason):
    return _preset(pid, "智谱", name, "智谱拒绝为这个账号服务，不会自己恢复。停用这个 OAuth 账号，处理好后手动启用。",
                   _zhipu(cond("channelType", "eq", value="oauth"), cond("status", "in", values=["429"]),
                          cond("code", "in", values=codes), cond("body", "contains", values=words)),
                   "account_blocked", steps=[disable(reason), notify(24)])


PRESET_SOURCES: tuple[dict[str, Any], ...] = (
    _preset("zhipu-usage-window", "智谱", "5 小时 / 周期用量用尽",
            "智谱 1308、1316–1321：当前用量窗口已用完，错误里写明了重置时间。暂停这个模型到重置时间。",
            _zhipu(cond("status", "in", values=["429"]), cond("code", "in", values=["1308", "1316-1321"])),
            "quota_exhausted"),
    _preset("zhipu-period-quota", "智谱", "周 / 月额度用尽",
            "智谱 1310：周或月额度用完，错误里写明了重置时间。暂停这个模型到重置时间。",
            _zhipu(cond("status", "in", values=["429"]), cond("code", "in", values=["1310"])), "quota_exhausted"),
    _preset("zhipu-plan-excluded", "智谱", "套餐不含此模型",
            "智谱 1311：当前套餐不包含这个模型。暂停这个模型 15 分钟，套餐开通后会自动恢复。",
            _zhipu(cond("code", "in", values=["1311"])), "plan_excluded"),
    _zhipu_block("zhipu-arrears", "账户欠费", ["1113"], ["欠费", "Insufficient balance", "no resource package"], "arrears"),
    _zhipu_block("zhipu-plan-expired", "套餐已到期或失效", ["1309", "1314"],
                 ["套餐已到期", "package has expired", "企业套餐已失效", "enterprise package has expired"], "plan_expired"),
    _zhipu_block("zhipu-fair-use", "被判定不符合公平使用", ["1313"], ["公平使用", "Fair Usage"], "fair_use_limited"),
    _zhipu_block("zhipu-key-not-allowed", "Key 仅限企业编程套餐", ["1315"],
                 ["仅限企业编程套餐", "limited to enterprise coding package"], "key_not_allowed"),
    _preset("workbuddy-model-usage-limit", "WorkBuddy", "单个模型用量超限",
            "WorkBuddy 6004：这个账号的这个模型用量超限，错误里写明了重置时间。暂停到重置时间，其他账号照常使用。",
            group("all", cond("provider", "in", values=["workbuddy"]), cond("status", "in", values=["429"]),
                  cond("code", "in", values=["6004"])),
            "quota_exhausted", steps=[pause("message_time")]),
    _preset("workbuddy-account-quota", "WorkBuddy", "账户额度用尽",
            "WorkBuddy 14001、14012–14014、14018、14019：这个账号的额度用完。暂停 15 分钟，换其他账号。",
            group("all", cond("provider", "in", values=["workbuddy"]),
                  cond("code", "in", values=["14001", "14012-14014", "14018", "14019"])),
            "quota_exhausted", steps=[pause("fixed", seconds=900)], response={"status": 0, "retryAfter": False}),
    _preset("minimax-token-plan", "MiniMax", "Token Plan 用量上限",
            "MiniMax 返回 HTTP 500 + 2067：Token Plan 当前周期用完。暂停到额度窗口结束（读不到时到下一个 5 小时边界）。",
            group("all", cond("provider", "in", values=["minimax"]), cond("status", "in", values=["500"]),
                  cond("code", "in", values=["2067"])),
            "quota_exhausted", steps=[pause("usage_window", boundary=5), notify(6)]),
)


def default_message(category: str) -> str:
    return _DEFAULT_MESSAGES.get(category) or "{upstream_message}"


def default_handling(category: str) -> dict[str, Any]:
    return copy.deepcopy(CATEGORIES[category]["handling"])
