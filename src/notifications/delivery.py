"""Per-recipient Telegram protocol receipts. Enqueue != delivery != read."""
import threading
import time
from . import store, content
from .catalog import CATALOG

_transport = None
_lock = threading.RLock()


def install(transport, *, recover=True):
    global _transport
    _transport = transport
    if recover:
        from .. import notifier
        for key in store.recover_queue():
            if not notifier.enqueue_delivery(key):
                row = store.get("deliveries", key) or {}
                if row.get("state") == "retry_wait":
                    _schedule_retry(key, max(time.time() + 5, row.get("retry_at", 0)))
                else:
                    store.not_submitted(key)


def transport():
    if _transport is None:
        raise RuntimeError("notification transport unavailable")
    return _transport


def request(method, payload):
    try:
        result = transport()(method, payload)
        return result if isinstance(result, dict) else None
    except Exception:
        # Exception messages can contain bot credentials; retain no raw exception.
        return None


def keyboard(delivery_id, event):
    from .actions import available
    from .subjects import current
    name = {"account": "👤 查看账户", "channel": "🔌 查看渠道"}.get(event["subject"]["kind"], "📋 查看事件详情") if current(event["subject"]) is not None else "📋 查看历史事件"
    choices = [(name, "view")]
    for action, label in available(event):
        if action not in {"view", "close"}:
            choices.append((label, action))
            break
    choices += [("⋯ 更多操作", "more"), ("🔕 暂停重复提醒", "mute"), ("✖ 关闭本条", "close")]
    buttons = [{"text": label, "callback_data": store.action_ref(delivery_id, action)} for label, action in choices]
    return {"inline_keyboard": [buttons[i:i+2] for i in range(0, len(buttons), 2)]}


def deliver(delivery_id):
    # Worker is serial; lock also protects explicit diagnostic/manual retries.
    with _lock:
        row = store.get("deliveries", delivery_id)
        if not row or row["state"] not in {"queued", "retry_wait"}:
            return
        if row["state"] == "retry_wait":
            if row.get("retry_at", 0) > time.time():
                _schedule_retry(delivery_id, row["retry_at"])
                return
            row = store.update_delivery(delivery_id, state="queued")
        event = store.get("events", row["event_id"])
        incident = store.get("incidents", row["incident_id"])
        if not event:
            store.update_delivery(delivery_id, state="failed", reason="event_expired")
            return
        from .. import config
        from .catalog import enabled
        if row.get("transactional") is not True and not enabled(event["event_type"], config.get().get("notifications") or {}):
            store.update_delivery(delivery_id, state="suppressed", reason="policy_changed")
            return
        policy = store.get("policy", f"recipient:{row['recipient']}:{event['subject']['generation']}:{CATALOG[event['event_type']].family}") or {}
        mute = store.get("policy", f"mute:{row['recipient']}:{row['incident_id']}") or {}
        if not row.get("transactional") and (policy.get("disabled") or (mute.get("until", 0) > time.time() and not row.get("bypass_repeat_mute"))):
            store.update_delivery(delivery_id, state="suppressed", reason="recipient_policy_changed")
            return
        # Superseded failure never sends as if still current. The recovery queued
        # behind it carries both history reference and the actual current effect.
        if incident and not incident.get("active") and event["phase"] != "recovered" and not CATALOG[event["event_type"]].episodic:
            store.update_delivery(delivery_id, state="suppressed", reason="recovered_before_send")
            return
        if incident and not CATALOG[event["event_type"]].episodic and (
                (event["phase"] == "recovered" and incident.get("active")) or
                row.get("episode", 0) < incident.get("episode", 0)):
            store.update_delivery(delivery_id, state="suppressed", reason="superseded_before_send")
            return
        try:
            markup = keyboard(delivery_id, event)
            rendered = content.render(event)
            store.update_delivery(delivery_id, state="sending", attempts=row["attempts"] + 1)
        except Exception:
            store.not_submitted(delivery_id, "pretransport_failed")
            return
        result = request("sendMessage", {"chat_id": row["chat_id"], "text": rendered, "parse_mode": "HTML", "reply_markup": markup})
        if result and result.get("ok") and isinstance((result.get("result") or {}).get("message_id"), int):
            mid = result["result"]["message_id"]
            store.update_delivery(delivery_id, state="sent", message_id=mid, reason="telegram_accepted")
            delay = CATALOG[event["event_type"]].auto_delete
            if delay:
                timer = threading.Timer(delay, close, args=(delivery_id,))
                timer.daemon = True
                timer.start()
        elif result and result.get("ok") is False:
            code = result.get("error_code")
            retry_after = (result.get("parameters") or {}).get("retry_after")
            if code == 429 and row["attempts"] < 2 and type(retry_after) in (int, float) and 0 <= retry_after <= 300:
                # Persist the explicit-rejection retry BEFORE any process-local timer.
                retry_at = time.time() + max(1, retry_after)
                store.update_delivery(delivery_id, state="retry_wait", reason="telegram_rejected", error_code=code, retry_at=retry_at)
                _schedule_retry(delivery_id, retry_at)
            else:
                store.update_delivery(delivery_id, state="failed", reason="telegram_rejected", error_code=code)
        else:
            store.update_delivery(delivery_id, state="unknown", reason="no_receipt")


def _schedule_retry(delivery_id, retry_at):
    def retry():
        from .. import notifier
        try:
            row = store.get("deliveries", delivery_id)
            if row and row["state"] == "retry_wait":
                if not notifier.enqueue_delivery(delivery_id):
                    _schedule_retry(delivery_id, time.time() + 5)
        except Exception:
            # Retain the already persisted retry across temporary local failures.
            _schedule_retry(delivery_id, time.time() + 5)
    timer = threading.Timer(max(0.01, retry_at - time.time()), retry)
    timer.daemon = True
    timer.start()


def interrupted(delivery_id):
    """Worker consumed the queue item: never leave a false queued claim."""
    row = store.get("deliveries", delivery_id)
    if not row:
        return
    if row["state"] == "queued":
        store.not_submitted(delivery_id, "pretransport_failed")
    elif row["state"] == "sending":
        store.update_delivery(delivery_id, state="unknown", reason="receipt_persistence_failed")


def close(delivery_id) -> str | None:
    row = store.get("deliveries", delivery_id)
    if not row or not row.get("message_id"):
        return "未取得消息编号，无法确认删除。"
    if row.get("state") == "closed":
        return None
    if row.get("message_owner") == "management":
        return "此消息已转为管理详情页，未删除。"
    result = request("deleteMessage", {"chat_id": row["chat_id"], "message_id": row["message_id"]})
    if result and result.get("ok"):
        store.update_delivery(delivery_id, state="closed")
        return None
    store.update_delivery(delivery_id, close_result="failed" if result else "unknown")
    request("editMessageReplyMarkup", {"chat_id": row["chat_id"], "message_id": row["message_id"], "reply_markup": {"inline_keyboard": []}})
    return "Telegram未允许删除或未返回确认；已尝试收起按钮，未标记已删除。"
