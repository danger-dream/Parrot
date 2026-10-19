"""Isolated nt callback router. Does not read/change legacy conversation input state."""
import threading
import time
from ...notifications import actions, content, delivery, store, subjects
from ...notifications.catalog import CATALOG

_lock = threading.RLock()
_pages = {}
_confirmations = {}  # ephemeral opaque-ref -> control token; NEVER persisted


def _allowed(actor):
    from ... import config
    admins = (config.get().get("telegram") or {}).get("adminIds") or []
    return str(actor) in {str(value) for value in admins}  # empty means deny


def _answer(cb, message):
    from .. import ui
    if not cb.get("_nt_ack_sent"):
        return ui.answer_cb(cb["id"], message, show_alert=True)
    # A callback is acknowledged once, before slow I/O. Successful navigation is
    # visible in the panel, not an unsolicited modal or a second expired answer.
    if message in {"已打开通知面板。", "已打开本人投递记录。", "请确认是否只重发通知。", "仅通知已入队。"}:
        return
    actor = (cb.get("from") or {}).get("id")
    chat = ((cb.get("message") or {}).get("chat") or {}).get("id")
    if actor == chat and _allowed(actor):
        return delivery.request("sendMessage", {"chat_id": chat, "text": content.text(message, 1000), "parse_mode": "HTML"})


def _accepted(result):
    return bool(result and (result.get("ok") is True or
        (result.get("ok") is False and result.get("error_code") == 400 and
         str(result.get("description", "")).lower().startswith("bad request: message is not modified"))))


def _invalid_panel(result):
    # These are explicit rejections, NOT ambiguous transport failures or all 400s.
    return bool(result and result.get("ok") is False and result.get("error_code") == 400 and
        str(result.get("description", "")).lower() in {
            "bad request: message to edit not found", "bad request: message can't be edited"})


def _validate(cb):
    if not cb.get("data", "").startswith("nt:v1:"):
        raise ValueError("无效的通知引用")
    actor = (cb.get("from") or {}).get("id")
    if not _allowed(actor):
        raise ValueError("无权限")
    ref = store.get("refs", cb["data"][6:])
    if not ref or ref["expires_at"] < time.time():
        raise ValueError("通知操作已过期")
    row = store.get("deliveries", ref["delivery_id"])
    message = cb.get("message") or {}
    if not row or int(actor) != row["recipient"] or (message.get("chat") or {}).get("id") != row["chat_id"]:
        raise ValueError("此通知不属于当前收件人")
    if message.get("message_id") != (ref.get("message_id") or row.get("message_id")):
        raise ValueError("消息与通知引用不匹配")
    if ref.get("message_id") and ref["message_id"] not in {row.get("message_id"), row.get("panel_id")}:
        raise ValueError("详情面板已更新，请从原通知重新打开")
    if ref.get("message_id") == row.get("panel_id") and row.get("panel_owner", "notification") != "notification":
        raise ValueError("此面板已转交登录流程，请从原通知重新打开")
    event = store.get("events", row["event_id"])
    if not event:
        raise ValueError("事件记录已过期")
    return int(actor), ref, row, event


def _panel(row, event, text, choices=(), *, force_new=False, _recreate=True):
    """One independent panel per recipient/delivery. Never replace the original event."""
    if len(text) > 3800:
        lines = text.splitlines()
        while len("\n".join(lines)) > 3500 and len(lines) > 6:
            lines.pop(len(lines) // 2)
        text = "\n".join(lines) + "\n内容已摘要；目标 #" + content.text(event["subject"]["short_id"]) + "，请按子页查看。"
    current = store.get("deliveries", row["id"]) or {}
    mid = current.get("panel_id") if not force_new and current.get("panel_owner", "notification") == "notification" else None
    if not mid:
        result = delivery.request("sendMessage", {"chat_id": row["chat_id"], "text": text, "parse_mode": "HTML"})
        if not result or not result.get("ok") or not (result.get("result") or {}).get("message_id"):
            store.update_delivery(row["id"], panel_state="failed" if result else "unknown")
            raise ValueError("Telegram未确认详情面板送达；原通知仍保留。")
        mid = result["result"]["message_id"]
        store.update_delivery(row["id"], panel_id=mid)
    original = store.get("events", row["event_id"])
    if original and event["subject"] != original["subject"]:
        choices = [(label, name, {**payload, "subject": event["subject"]}) for label, name, payload in choices]
    refs = store.action_refs(row["id"], [(action, payload) for label, action, payload in choices], message_id=mid)
    buttons = [{"text": label, "callback_data": ref} for (label, action, payload), ref in zip(choices, refs)]
    result = delivery.request("editMessageText", {"chat_id": row["chat_id"], "message_id": mid, "text": text, "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [buttons[i:i+2] for i in range(0, len(buttons), 2)]}})
    if _invalid_panel(result) and _recreate:
        store.update_delivery(row["id"], panel_id=None, panel_state="failed")
        return _panel(row, event, text, choices, force_new=True, _recreate=False)
    accepted = _accepted(result)
    store.update_delivery(row["id"], panel_state="sent" if accepted else "failed" if result else "unknown",
        panel_owner="notification", panel_version=current.get("panel_version", 0) + 1)
    if not accepted:
        raise ValueError("Telegram未确认详情面板更新；未声称已展示新状态。")
    return True


def _details(event, page, actor=None):
    from ...notifications import views
    target = subjects.current(event["subject"])
    inc = store.get("incidents", event["incident_id"]) or {}
    status = "原账户已删除或世代已改变" if target is None else "独立事件/操作回执，不代表存在持续故障" if CATALOG[event["event_type"]].episodic else "此通知对应的问题已恢复" if inc.get("active") is False else "问题尚未收到恢复确认"
    if target and target.get("account"):
        a = target["account"]
        status += "；账号" + ("启用" if a.get("enabled", True) and not a.get("disabled_reason") else "仍暂停")
    if page == "view" and target and target.get("account"):
        return "\n".join(views.object_lines(event)) + "\n\n" + "历史通知：" + content.text(CATALOG[event["event_type"]].title) + "\n" + content.text(status)
    if page == "models":
        return "\n".join(views.model_lines(event))
    if page == "records":
        return "\n".join(views.records(event, actor))
    if page == "request":
        return "\n".join(views.request_lines(event, actor))
    if page == "storage":
        return "\n".join(views.storage_lines())
    if page == "routes":
        return "\n".join(views.route_lines(event)[0])
    text = content.render(event, detail=True, current_status=status)
    if target and target.get("account"):
        from ... import state_db
        a = target["account"]
        extra = []
        if page == "auth":
            extra = ["当前到期：" + content.text(a.get("expired") or "未取得")]
        elif page == "quota":
            from ...notifications.providers import quota_metrics
            from ... import oauth_manager
            usage = oauth_manager.usage_from_quota_row(state_db.quota_load(target["key"]) or {})
            from ...notifications.providers import REGISTRY
            provider = REGISTRY.get(event["subject"]["provider"])
            metrics = provider.facts(usage) if provider and provider.facts else quota_metrics(usage)
            extra = content.fact_lines({"metrics": metrics}, detail=True) or ["未取得已知额度数值；未发起网络查询。"]
        elif page == "benefits":
            extra = ["仅查看本地快照；查询/使用权益需要明确发起操作并按现有控制层确认。"]
            from ...notifications.providers import REGISTRY
            provider = REGISTRY.get(event["subject"]["provider"])
            if provider and provider.benefit_view:
                from ... import oauth_manager
                if event["subject"]["provider"] == "zhipu":
                    value = {"reset": (a.get("zhipu_reset_status") or {}).get("data") or {}}
                else:
                    value = oauth_manager.usage_from_quota_row(state_db.quota_load(target["key"]) or {})
                lines, _ = provider.benefit_view(value)
                extra += [content.text(line) for line in lines]
        if extra:
            text += "\n\n" + "\n".join(extra)
    return text if len(text) < 3900 else text[:0] + content.render(event, current_status=status) + "\n详细记录较长，请按子页查看。"


def handle_callback(cb):
    cb = dict(cb)
    # Neutral receipt only: not authorization or a claim of successful mutation.
    # Do not wait behind another panel's network/durable I/O for this ack.
    delivery.request("answerCallbackQuery", {"callback_query_id": cb["id"], "text": "正在核对通知…"})
    cb["_nt_ack_sent"] = True
    try:
        with _lock:
            if cb.get("data") == "nt:diagnostics":
                _open_diagnostics(cb)
                return
            actor, ref, row, event = _validate(cb)
            action = ref["action"]
            if ref.get("payload", {}).get("subject"):
                from dataclasses import asdict
                from ...notifications.events import NotificationEvent, Subject, Effect
                navigation = ref["payload"]["subject"]
                target = subjects.current(navigation)
                if target is None:
                    raise ValueError("原渠道/账户已删除，无法打开同名新对象")
                event = NotificationEvent("operation.attention", Subject(**navigation), Effect("unchanged", "not_disabled"),
                    {"operation": "本地精确管理", "result": "只读", "stages": [{"name": "本地详情", "status": "skipped"}]}).record()
                event["incident_id"] = ""

            if action == "diagnostics":
                text, choices = _diagnostics(actor)
                _panel(row, event, text, choices)
                _answer(cb, "已打开本人投递记录。")
                return
            elif action == "resend":
                target = store.get("deliveries", ref["payload"]["delivery"])
                if not target or target["recipient"] != actor or target["state"] not in {"failed", "unknown"}:
                    raise ValueError("不能重发此投递")
                _panel(row, event, "Telegram可能已经收到了原消息；显式重发可能重复。只重发通知，绝不重新执行账号操作。",
                    [("确认仅重发通知", "confirm_resend", {"delivery": target["id"]}), ("取消", "diagnostics", {})])
                _answer(cb, "请确认是否只重发通知。")
                return
            elif action == "confirm_resend":
                key = cb["data"][6:]
                def consume(refs):
                    if refs[key].get("consumed"):
                        raise ValueError("此重发确认已使用")
                    refs[key] = {**refs[key], "consumed": True}
                store.mutate("refs", consume)
                target = store.explicit_resend(ref["payload"]["delivery"], actor)
                from ... import notifier
                if not notifier.enqueue_delivery(target):
                    store.not_submitted(target)
                    raise ValueError("通知队列已满；没有执行任何业务操作")
                _panel(row, event, "仅通知已入队，不等于已送达。", [("刷新投递状态", "diagnostics", {})])
                _answer(cb, "仅通知已入队。")
                return
            elif action == "close":
                # Panel close refs are not generated: close always targets this delivery.
                feedback = delivery.close(row["id"])
                if feedback:
                    _answer(cb, feedback)
                return
            if action in {"mute1", "mute24"}:
                until = time.time() + (3600 if action == "mute1" else 86400)
                key = f"mute:{actor}:{row['incident_id']}"
                store.mutate("policy", lambda rows: rows.__setitem__(key, {"until": until, "at": time.time()}))
                _answer(cb, "已暂停本人此问题重复提醒；未改变账号调度，恢复仍可通知。")
                return
            if action == "mute":
                _panel(row, event, "暂停仅属于你的此问题重复提醒；不是暂停账号。", [("1小时", "mute1", {}), ("24小时", "mute24", {})])
            elif action == "more":
                choices = [(label, name, {}) for name, label in actions.available(event)] + [("本问题家族通知设置", "policy", {})]
                page = int(ref.get("payload", {}).get("page", 0))
                visible = choices[page*4:page*4+4]
                if page:
                    visible.append(("上一页", "more", {"page": page-1}))
                if (page+1)*4 < len(choices):
                    visible.append(("下一页", "more", {"page": page+1}))
                if ref.get("payload", {}).get("subject"):
                    visible = [(label, name, {**payload, "subject": ref["payload"]["subject"]}) for label, name, payload in visible]
                _panel(row, event, _details(event, "view", actor), visible)
            elif action == "policy":
                _panel(row, event, "只影响本人、此对象和此事件家族，不改变账户状态；关闭后也不单独推送恢复。",
                    [("关闭此家族推送", "policy_off", {}), ("恢复此家族推送", "policy_on", {})])
            elif action in {"policy_off", "policy_on"}:
                family = CATALOG[event["event_type"]].family
                key = f"recipient:{actor}:{event['subject']['generation']}:{family}"
                store.mutate("policy", lambda rows: rows.__setitem__(key, {"disabled": action == "policy_off", "at": time.time()}))
                _answer(cb, "已更新本人此对象通知策略；未改变账号状态。")
                return
            elif action == "reauthorize":
                from .. import states
                if states.get_state(row["chat_id"]):
                    raise ValueError("已有输入/登录流程，请先完成或取消；未覆盖原会话")
                _panel(row, event, _details(event, "auth", actor))
                store.update_delivery(row["id"], panel_owner="authorization")
                actions.reauthorize(event, actor, row["chat_id"], store.get("deliveries", row["id"])["panel_id"], cb["id"])
            elif action == "benefit_page":
                value = _pages.get(row["id"])
                if not value or value[0] < time.time():
                    raise ValueError("权益查询页面已过期，请重新查询")
                _benefit_panel(row, event, value[1], value[2], int(ref["payload"].get("page", 0)))
            elif action in actions.READ:
                if action != "view" and action not in dict(actions.available(event)):
                    raise ValueError("此对象/能力已不可用")
                choices = [("⋯ 更多操作", "more", {})]
                if action == "routes":
                    from ...notifications import views
                    _, candidates = views.route_lines(event)
                    choices = [("查看 " + (s.label or s.identity or s.service), "view", {"subject": {**s.__dict__, "scopes": [list(pair) for pair in s.scopes]}}) for s in candidates]
                    page = int(ref.get("payload", {}).get("page", 0))
                    visible = choices[page*4:page*4+4]
                    if page:
                        visible.append(("上一页", "routes", {"page": page-1}))
                    if (page+1)*4 < len(choices):
                        visible.append(("下一页", "routes", {"page": page+1}))
                    choices = visible
                if action == "benefits" and actions._control is not None:
                    choices.insert(0, ("明确查询权益/核对记录", "query_benefits", {}))
                if ref.get("payload", {}).get("subject"):
                    choices = [(label, name, {**payload, "subject": ref["payload"]["subject"]}) for label, name, payload in choices]
                _panel(row, event, _details(event, action, actor), choices)
            elif action in {"stop_credits", "clear_cooldown"}:
                token = actions.prepare(event, action, actor)
                _confirmation(row, event, token, action, _details(event, "view", actor) + "\n\n确认：" + actions.LABELS[action] + "。此操作改变本账号调用/积分策略，不是关闭通知。")
            elif action == "cancel":
                for key, value in list(_confirmations.items()):
                    stored = store.get("refs", key[6:])
                    if stored and stored["delivery_id"] == row["id"]:
                        _confirmations.pop(key, None)
                _panel(row, event, "已取消本次确认；未提交业务操作。", [("返回账户/事件", "view", {})])
            elif action == "confirm":
                pending = _confirmations.pop(cb["data"], None)
                if not pending or pending[2] < time.time():
                    raise ValueError("确认已过期或使用，请重新预览")
                _execute(row, event, pending[1], actor, pending[0])
            elif action in {"refresh_usage", "refresh_token", "sync_models", "query_benefits", "benefit_plan"}:
                _execute(row, event, action, actor, payload=ref.get("payload"))
            else:
                raise ValueError("无效的通知操作")
        _answer(cb, "已打开通知面板。")
    except ValueError as exc:
        _answer(cb, str(exc))
    except Exception:
        _answer(cb, "操作未确认完成或状态保存失败；请重新查看，不会自动重复外部动作。")


def _confirmation(row, event, token, action, text):
    for key, value in list(_confirmations.items()):
        if value[2] < time.time():
            _confirmations.pop(key, None)
    if len(_confirmations) >= 500:
        raise ValueError("确认请求过多，请稍后再试")
    original = store.get("events", row["event_id"])
    navigation = {"subject": event["subject"]} if original and original["subject"] != event["subject"] else {}
    _panel(row, event, text)
    for attempt in range(2):
        current = store.get("deliveries", row["id"])
        mid = current["panel_id"]
        nonce, cancel = store.action_refs(row["id"], [("confirm", navigation), ("cancel", navigation)], ttl=300, message_id=mid)
        result = delivery.request("editMessageReplyMarkup", {"chat_id": row["chat_id"], "message_id": mid,
            "reply_markup": {"inline_keyboard": [[{"text": "确认执行", "callback_data": nonce}, {"text": "取消", "callback_data": cancel}]]}})
        if _invalid_panel(result) and attempt == 0:
            store.update_delivery(row["id"], panel_id=None, panel_state="failed")
            _panel(row, event, text, force_new=True, _recreate=False)
            continue
        accepted = _accepted(result)
        store.update_delivery(row["id"], panel_state="sent" if accepted else "failed" if result else "unknown",
            panel_version=current.get("panel_version", 0) + 1)
        if not accepted:
            raise ValueError("Telegram未确认确认按钮更新；未提交业务操作，请重新预览。")
        # Never authorize an unconfirmed/unknown keyboard. Tokens stay ephemeral.
        _confirmations[nonce] = (token, action, time.time() + 300)
        return


def _benefit_panel(row, event, text, choices, page=0):
    _pages[row["id"]] = (time.time() + 300, text, choices)
    for key, value in list(_pages.items()):
        if value[0] < time.time() or len(_pages) > 500:
            _pages.pop(key, None)
    begin = max(0, page) * 4
    selected = choices[begin:begin+4]
    if begin:
        selected.append(("上一页", "benefit_page", {"page": page-1}))
    if begin + 4 < len(choices):
        selected.append(("下一页", "benefit_page", {"page": page+1}))
    _panel(row, event, text, selected)


def _execute(row, event, action, actor, token=None, payload=None):
    def completed(result):
        with _lock:
            text = _details(event, "view", actor) + "\n\n本次任务：" + content.text(result["status"]) + "\n任务编号：" + content.text(result["id"])
            lines = result.get("lines", [])
            if lines:
                text += "\n\n" + "\n".join(content.text(line) for line in lines[:12])
            current = store.get("deliveries", row["id"]) or {}
            if current.get("panel_owner") != "notification" or current.get("panel_version") != panel_version:
                _panel(row, event, text, force_new=True)
            if result.get("confirmation"):
                _confirmation(row, event, result["confirmation"], "benefit_execute", text)
            elif result.get("choices"):
                _benefit_panel(row, event, text, result["choices"])
            else:
                _panel(row, event, text, [("查看操作记录", "records", {}), ("更多操作", "more", {})])
    # Panel progress is set BEFORE submission: even an immediate fake worker must
    # not have its terminal result overwritten by a late 'queued' UI update.
    _panel(row, event, _details(event, "view", actor) + "\n\n正在提交任务（不等于成功）")
    panel_version = (store.get("deliveries", row["id"]) or {}).get("panel_version")
    actions.execute(event, action, actor, token=token, payload=payload, on_result=completed)


def _diagnostics(actor):
    rows = sorted((r for r in store.items("deliveries").values() if r["recipient"] == actor), key=lambda r:r["created_at"], reverse=True)[:12]
    lines = ["<b>本人最近通知投递状态</b>", "sent仅表示Telegram接受，不代表已读；unknown不会自动重放。"]
    choices = []
    for row in rows:
        event = store.get("events", row["event_id"]) or {}
        if (event.get("subject") or {}).get("exact_id") == "notification_delivery":
            continue
        title = CATALOG[event["event_type"]].title if event.get("event_type") in CATALOG else "过期事件"
        lines.append(content.text(title) + " · #" + row["id"][:6] + "：" + content.text(row["state"]) + "（尝试 " + str(row["attempts"]) + "）")
        if row["state"] in {"failed", "unknown"} and len(choices) < 4:
            choices.append(("预览重发 #" + row["id"][:6], "resend", {"delivery": row["id"]}))
    choices += [("刷新状态", "diagnostics", {}), ("关闭本条", "close", {})]
    return "\n".join(lines), choices


def _open_diagnostics(cb):
    from ... import config
    from ...notifications.events import NotificationEvent, Effect
    actor = (cb.get("from") or {}).get("id")
    if not _allowed(actor) or (cb.get("message") or {}).get("chat", {}).get("id") != actor:
        raise ValueError("请由当前管理员在本人私聊打开投递记录")
    event = NotificationEvent("operation.completed", subjects.system("notification_delivery"), Effect("unchanged", "not_disabled"),
        {"operation": "查看本人投递记录", "result": "只读"}, scope="operation").record()
    keys = store.record(event, config.get().get("notifications") or {}, [actor], transactional=True)
    row = store.get("deliveries", keys[0])
    text, choices = _diagnostics(actor)
    try:
        ok = _panel(row, event, text, choices)
    except ValueError:
        store.update_delivery(row["id"], state="unknown")
        raise
    current = store.get("deliveries", row["id"])
    store.update_delivery(row["id"], state="sent" if ok else "unknown", message_id=current.get("panel_id"))
    _answer(cb, "已打开本人投递记录。" if ok else "Telegram未确认接受诊断面板。")
