"""Capability-driven exact operations; confirmation secrets stay in bounded memory."""
import threading
import time
from . import subjects, store
from .catalog import CATALOG
from .providers import capabilities, REGISTRY

_control = None
_operations = None
_lock = threading.RLock()
_active = {}
_plans = {}
_listeners = {}
LABELS = {"view": "👤 账户详情", "log": "📜 调用日志", "model": "🧬 模型详情", "key": "🔑 Key 详情", "models": "📋 账户模型",
          "records": "📒 操作记录", "storage": "💾 存储状态", "benefits": "🎁 重置方式",
          "refresh_usage": "🔄 更新额度", "sync_models": "🔄 重试模型同步", "refresh_token": "🔄 重试续期", "reauthorize": "🔐 重新授权",
          "stop_credits": "⏸ 暂停积分续用", "clear_cooldown": "🧹 清除冷却"}
# Navigation opens the real management page in a new message; the notification stays as the record.
NAVIGATE = {"view", "log", "model", "key", "models"}
# "event"/"auth"/"request"/"routes"/"quota" only exist on notifications sent by older versions.
READ = NAVIGATE | {"event", "auth", "request", "routes", "quota", "records", "storage", "benefits"}
CONFIRM = {"stop_credits", "clear_cooldown"}


def _label(action, event):
    if action == "view" and event["subject"]["kind"] == "channel":
        return "🔌 渠道详情"
    if action == "key":
        name = event["facts"]["api_key_name"]
        return "🔑 Key：" + (name if len(name) <= 16 else name[:15] + "…")
    return LABELS[action]


def available(event):
    """Buttons in the order the scene declares, limited to what the exact target supports now."""
    s, facts = event["subject"], event["facts"]
    target = subjects.current(s)
    caps = set()
    if target and target.get("account"):
        caps.add("view")
        if s["kind"] == "account":
            caps |= capabilities(target["account"])
    if facts.get("request_id"):
        caps.add("log")
    if facts.get("model") or facts.get("requested_model"):
        caps.add("model")
    if facts.get("api_key_name"):
        caps.add("key")
    if event["event_type"] == "system.responses_store.failed":
        caps.add("storage")
    if _control is None or _operations is None:
        caps &= READ
    inc = store.get("incidents", event.get("incident_id", "")) or {}
    if inc.get("active") is False and not CATALOG[event["event_type"]].episodic:
        caps &= READ
    if facts.get("auth_error") or facts.get("retryable") is False:
        caps.discard("refresh_token")
    if s["kind"] != "account" or event["scope"] != "model" or not event["scope_ref"]:
        caps.discard("clear_cooldown")
    if event["event_type"].startswith("operation.") and facts.get("stages"):
        incomplete = {s["name"] for s in facts["stages"] if s["status"] not in {"succeeded", "skipped", "not_modified"}}
        if not incomplete & {"模型目录", "元数据"}:
            caps.discard("sync_models")
        if "额度快照" not in incomplete:
            caps.discard("refresh_usage")
        if not incomplete & {"模型Key配置", "权益查询"}:
            caps.discard("benefits")
    return [(action, _label(action, event)) for action in CATALOG[event["event_type"]].actions if action in caps]


def _context(actor):
    from ..management_control.oauth.menu_bridge import telegram_context
    return telegram_context(actor)


def _check(event, action, actor):
    from .. import config
    if str(actor) not in {str(a) for a in (config.get().get("telegram") or {}).get("adminIds", [])}:
        raise ValueError("当前已无管理权限")
    target = subjects.current(event["subject"])
    if target is None:
        raise ValueError("原账户已删除或世代已改变")
    if action not in dict(available(event)):
        if action not in {"query_benefits", "benefit_plan", "benefit_execute"} or "benefits" not in dict(available(event)):
            raise ValueError("操作不再可用或受保护模式限制")
    return target


def _provider(target):
    provider = REGISTRY.get(target["account"].get("provider", "claude"))
    if provider is None:
        raise ValueError("未注册服务只允许查看")
    return provider


def benefit_choices(event):
    target = subjects.current(event["subject"])
    if not target or "benefits" not in dict(available(event)):
        return []
    caps = capabilities(target["account"])
    if event["event_type"] == "account.action.attention" and event["facts"].get("result") in {"pending", "unknown"}:
        return []
    return [(entry[1], "benefit_plan", {"kind": entry[0]}) for entry in _provider(target).benefit_actions
            if entry[0] in caps and entry[0] != "zhipu_use"]  # card choice needs a current inventory index


def prepare(event, action, actor, *, remote=None):
    from ..management_control.oauth.contracts import revision
    from ..management_control.oauth.plans import OneShotPlanStore
    target = _check(event, action, actor)
    with _lock:
        plans = _plans.setdefault("local", OneShotPlanStore(prefix="notification", ttl_seconds=300))
        token, _ = plans.create(actor_subject_id=str(actor), kind=action,
            revision=revision(target["account"]), payload={"subject": event["subject"], "scope_ref": event["scope_ref"], "remote": remote})
    return token


def reauthorize(event, actor, chat, mid, cb_id):
    from ..telegram import states
    target = _check(event, "reauthorize", actor)
    if states.get_state(chat):
        raise ValueError("已有输入/登录流程，请先完成或取消；未覆盖原会话")
    start = _provider(target).login
    if start is None:
        raise ValueError("此服务没有已注册授权入口")
    return start(chat, mid, cb_id, target["account"])


def _record_operation(event, action, actor, receipt):
    value = {"kind": "operation", "generation": event["subject"]["generation"], "action": action,
             "actor": actor, "id": receipt["id"], "status": receipt["status"], "at": time.time()}
    store.mutate("policy", lambda rows: rows.__setitem__("operation:" + receipt["id"], value))


def execute(event, action, actor, *, token=None, payload=None, on_result=None):
    """Single-flight per exact object/action, including async plan/read operations."""
    import copy
    from ..management_control.oauth.contracts import revision
    target = _check(event, action, actor)
    expected = copy.deepcopy(target["account"])
    context = _context(actor)
    import json
    stages = event["facts"].get("stages") if action == "sync_models" and event["event_type"].startswith("operation.") else None
    detail = (str(actor), json.dumps(payload or {}, sort_keys=True)) if action == "benefit_plan" else (json.dumps(stages, sort_keys=True),) if stages is not None else ()
    remote = None
    with _lock:
        if action in CONFIRM | {"benefit_execute"}:
            plans = _plans.get("local")
            if plans is None:
                raise ValueError("确认已过期，请重新预览")
            plan = plans.consume(token, actor_subject_id=str(actor), kind=action)
            if plan.payload["subject"] != event["subject"] or plan.revision != revision(expected):
                raise ValueError("目标已变化，请重新预览")
            remote = plan.payload.get("remote")
            if action == "benefit_execute":
                # Each actor-bound, one-shot intent is distinct. Provider ledgers
                # arbitrate concurrent consumption, not this generic refresh cache.
                detail = (actor, plan.plan_id)
        key = (event["subject"]["generation"], action, event["scope_ref"], detail)
        existing = _active.get(key)
        if existing and existing["status"] in {"queued", "running"}:
            if on_result:
                _listeners.setdefault(existing["id"], []).append((actor, on_result))
            return dict(existing)
        operation = _operations.create(context, kind="notification." + action, cancellable=False)
        receipt = {"id": operation.id, "status": "queued"}
        _record_operation(event, action, actor, receipt)
        _active[key] = receipt
        _listeners[operation.id] = [(actor, on_result)] if on_result else []
        for old in list(_active):
            if len(_active) <= 400:
                break
            if _active[old]["status"] not in {"queued", "running"}:
                del _active[old]
    def run():
        from .. import oauth_manager
        from ..management_control.errors import ManagementErrorCode
        output = {}
        try:
            current = _check(event, action, actor)
            if revision(current["account"]) != revision(expected):
                raise ValueError("目标已变化")
            _operations.mark_running(operation.id)
            receipt["status"] = "running"
            _record_operation(event, action, actor, receipt)
            with oauth_manager.exact_operation_target(current["key"], event["subject"]["generation"]):
                if action in {"refresh_usage", "sync_models", "refresh_token"}:
                    result = _control.notification_operation(context, current["key"], expected, action, **({"stages": stages} if stages is not None else {}))
                    if result.get("stages"):
                        from .content import fact_lines
                        # Stage names/statuses are the control contract, not upstream prose.
                        output["lines"] = fact_lines({"stages": result["stages"]}, detail=True)
                elif action == "stop_credits":
                    result = _control.set_notification_policy(context, current["key"], expected, allow_credits=False)
                    result = {"status": "succeeded" if result.get("allowed") is False else "unknown"}
                elif action == "clear_cooldown":
                    result = _control.clear_notification_cooldown(context, current["key"], expected, event["scope_ref"])
                    result = {"status": "succeeded" if result.get("cleared") is True else "unknown"}
                elif action == "query_benefits":
                    provider = _provider(current)
                    value = provider.benefit_query(_control, context, current["key"])
                    lines, choices = provider.benefit_view(value)
                    output.update(lines=lines, choices=choices + benefit_choices(event))
                    result = {"status": "succeeded"}
                elif action == "benefit_plan":
                    if event["event_type"] == "account.action.attention" and event["facts"].get("result") in {"pending", "unknown"}:
                        raise ValueError("此前结果未知，请先核对记录")
                    provider = _provider(current)
                    name = (payload or {}).get("kind")
                    entry = next((entry for entry in provider.benefit_actions if entry[0] == name), None)
                    if not entry or name not in capabilities(current["account"]):
                        raise ValueError("权益能力不可用")
                    preview = entry[2](_control, context, current["key"], payload or {})
                    if preview.get("available") is False:
                        output["lines"] = ["当前不能执行：" + str(preview.get("reason") or "上游未确认资格")]
                    else:
                        token = prepare(event, "benefit_execute", actor, remote=(name, preview["plan_token"], "preview"))
                        output.update(confirmation=token, lines=[entry[1], "确认后可能消耗权益/次数，无法撤销；只执行当前选定目标。"])
                        for field in ("reset_type", "card_index", "expire_at", "business_date", "program", "observed_status"):
                            if preview.get(field) is not None:
                                output["lines"].append(field + "：" + str(preview[field]))
                    result = {"status": "succeeded"}
                elif action == "benefit_execute":
                    entry = next(e for e in _provider(current).benefit_actions if e[0] == remote[0])
                    if len(entry) > 4 and (len(remote) < 3 or remote[2] != "final"):
                        final = entry[4](_control, context, current["key"], remote[1])
                        token = prepare(event, "benefit_execute", actor, remote=(remote[0], final["plan_token"], "final"))
                        output.update(confirmation=token, lines=["最终确认：" + entry[1],
                            "提交不可撤销；成功后仍按实际用量检查，不强制启用。超时只核对，不自动补发。",
                            "权益编号：" + str(final.get("grant_id") or "服务端当前权益")])
                        result = {"status": "succeeded"}
                    else:
                        result = entry[3](_control, context, current["key"], remote[1])
                else:
                    raise ValueError("unsupported action")
            status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            receipt["status"] = "succeeded" if status in {"succeeded", "already_done"} else "unknown" if status in {"pending", "unknown"} else "failed"
            if receipt["status"] == "succeeded":
                _operations.succeed(operation.id, {"status": receipt["status"]})
            else:
                _operations.fail(operation.id, code=ManagementErrorCode.UPSTREAM_ERROR, message="结果未确认成功，请核对记录", retryable=False)
        except Exception as exc:
            receipt["status"] = "unknown" if isinstance(exc, (TimeoutError, ConnectionError)) else "failed"
            _operations.fail(operation.id, code=ManagementErrorCode.UPSTREAM_ERROR, message="操作未确认完成", retryable=False)
        finally:
            try:
                _record_operation(event, action, actor, receipt)
            except Exception:
                print("[notification] operation receipt persistence failed")
            with _lock:
                listeners = _listeners.pop(operation.id, [])
            for listener_actor, callback in listeners:
                result_view = {**receipt, **output}
                if listener_actor != actor:
                    result_view.pop("confirmation", None)
                try:
                    callback(result_view)
                except Exception:
                    print("[notification] operation panel not confirmed")
    try:
        _operations.submit(operation.id, run)
    except Exception:
        receipt["status"] = "failed"
        _record_operation(event, action, actor, receipt)
        with _lock:
            _listeners.pop(operation.id, None)
        raise ValueError("任务未提交；未执行业务动作") from None
    return dict(receipt)
