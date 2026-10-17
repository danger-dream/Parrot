"""Transactional single-recipient operation results; operational mute never hides them."""
from . import subjects, store, content, delivery
from .events import NotificationEvent, Effect, COMPLETED_STAGES


def present(chat_id, message_id, subject, *, operation, stages, saved=True, error=None, result_text=None, continuation=None):
    import time
    from .. import config
    pending = not saved or any(stage["status"] not in COMPLETED_STAGES for stage in stages)
    facts = {"operation": operation, "result": result_text or ("部分完成或仍在处理中" if pending else "各阶段已完成"), "saved": bool(saved), "stages": stages}
    if error is not None:
        from .. import oauth_errors
        info = oauth_errors.describe_oauth_error(error, provider=subject.provider, operation="initialize")
        facts.update(error_code=info.code, error_title=info.title, error_reason=info.reason, advice=info.action)
        pending = True
    event = NotificationEvent("operation.attention" if pending else "operation.completed", subject,
        Effect("confirmed" if saved else "unknown", "saved" if saved else "not_saved"),
        facts,
        scope="operation", source="telegram_operation").record()
    key = None
    try:
        keys = store.record(event, config.get().get("notifications") or {}, [chat_id], transactional=True)
        key = keys[0]
        markup = delivery.keyboard(key, event)
        if continuation:
            # Existing naming/credential-replacement workflow keeps its own nonce,
            # actor-bound plan and input state. Never persist those workflow secrets.
            buttons = [b for row in markup["inline_keyboard"] for b in row]
            markup = {"inline_keyboard": list(continuation) + [[buttons[0], buttons[-1]]]}
        store.update_delivery(key, state="sending", message_id=message_id, attempts=1)
    except Exception:
        # The operation result MUST still be visible when auxiliary persistence
        # fails. No unbound write buttons are synthesized in this fallback.
        markup = {"inline_keyboard": list(continuation or [])}
    payload = {"chat_id": chat_id, "text": content.render(event, detail=True, current_status="操作阶段回执"),
               "parse_mode": "HTML", "reply_markup": markup}
    if message_id is not None:
        payload["message_id"] = message_id
    result = delivery.request("editMessageText" if message_id is not None else "sendMessage", payload)
    if key:
        try:
            accepted = result and result.get("ok") and isinstance(result.get("result"), dict) and result["result"].get("message_id")
            store.update_delivery(key, state="sent" if accepted else "failed" if result and result.get("ok") is False else "unknown",
                message_id=result["result"]["message_id"] if accepted else message_id, updated_at=time.time())
        except Exception:
            pass  # A completed business operation must never be retried for this.
    return event
