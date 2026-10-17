"""Real JSON transport + StateStore, strict bounded Telegram model (not live acceptance)."""
import copy
import json
from contextlib import nullcontext
from html.parser import HTMLParser
import httpx
import pytest
from src.notifications import delivery, store
from src.telegram import ui
from src.telegram.menus import notification_menu as menu
from src.tests.test_notifications import env, event, controlled
from src.tests.test_notifications_review_security import delivered, click

_REAL_ANSWER = menu._answer


class HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
    def handle_starttag(self, tag, attrs):
        assert tag in {'b', 'i', 'u', 's', 'a', 'code', 'pre', 'strong', 'em', 'ins', 'del', 'span', 'tg-spoiler', 'blockquote'}, tag
        self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag


class TelegramPeer:
    def __init__(self):
        self.messages = {}
        self.calls = []
        self.reject = {}
        self.next_mid = 1000
    def __call__(self, request):
        assert request.method == 'POST'
        assert request.headers['content-type'].startswith('application/json')
        method = request.url.path.rsplit('/', 1)[-1]
        p = json.loads(request.content)
        self.calls.append((method, p))
        if method in self.reject:
            return httpx.Response(400, json=self.reject[method])
        if method == 'answerCallbackQuery':
            assert isinstance(p['callback_query_id'], str)
            return httpx.Response(200, json={'ok': True, 'result': True})
        assert type(p['chat_id']) is int
        if method in {'sendMessage', 'editMessageText'}:
            assert isinstance(p['text'], str) and 0 < len(p['text']) <= 4096
            assert p.get('parse_mode') == 'HTML'
            html = HTML(); html.feed(p['text']); assert not html.stack
        markup = p.get('reply_markup', {'inline_keyboard': []})
        assert isinstance(markup, dict)
        for row in markup['inline_keyboard']:
            assert isinstance(row, list) and row
            for button in row:
                assert button['text'] and 1 <= len(button['callback_data'].encode()) <= 64
        if method == 'sendMessage':
            mid = self.next_mid
            self.next_mid += 1
            self.messages[(p['chat_id'], mid)] = copy.deepcopy(p)
        else:
            assert method in {'editMessageText', 'editMessageReplyMarkup', 'deleteMessage'}
            assert type(p['message_id']) is int
            mid = p['message_id']; key = (p['chat_id'], mid)
            if key not in self.messages:
                return httpx.Response(400, json={'ok': False, 'error_code': 400, 'description': 'Bad Request: message to edit not found'})
            if method == 'deleteMessage':
                del self.messages[key]
                return httpx.Response(200, json={'ok': True, 'result': True})
            old = self.messages[key]
            new = {**old, **{k: v for k, v in p.items() if k != 'message_id'}}
            if old == new:
                return httpx.Response(400, json={'ok': False, 'error_code': 400, 'description': 'Bad Request: message is not modified'})
            self.messages[key] = new
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': mid, 'chat': {'id': p['chat_id']}}})


@pytest.fixture
def protocol(env, monkeypatch):
    peer = TelegramPeer()
    with httpx.Client(transport=httpx.MockTransport(peer)) as client:
        monkeypatch.setattr(ui, '_bot_token', 'fixture-bot-token')
        monkeypatch.setattr(ui, '_session_lease', lambda: nullcontext(client))
        delivery.install(ui.notification_api, recover=False)
        monkeypatch.setattr(menu, '_answer', _REAL_ANSWER)
        monkeypatch.setattr(menu, '_confirmations', {})
        yield peer


@pytest.mark.parametrize('action', ['view', 'quota', 'more'])
def test_existing_panel_fields_round_trip_real_json(env, protocol, action):
    row = delivered(event(), env)[0]
    click(row, action)
    current = store.get('deliveries', row['id'])
    assert current['panel_state'] == 'sent'
    assert current['panel_id'] != row['message_id']
    before = len(protocol.messages)
    click(row, action)
    assert len(protocol.messages) == before
    assert store.get('deliveries', row['id'])['panel_state'] == 'sent'


def test_rejected_update_is_diagnosable_without_payload_or_bot_token(env, protocol, capsys):
    row = delivered(event(), env)[0]
    protocol.reject['editMessageText'] = {'ok': False, 'error_code': 400, 'description': 'Bad Request: BUTTON_DATA_INVALID'}
    click(row, 'view')
    assert store.get('deliveries', row['id'])['panel_state'] == 'failed'
    output = capsys.readouterr().out
    records = [json.loads(line.split('[tg-notification] ', 1)[1]) for line in output.splitlines() if '[tg-notification] ' in line]
    failure = next(r for r in records if r['method'] == 'editMessageText')
    assert failure['error_code'] == 400 and failure['http_status'] == 400
    assert failure['description'] == 'Bad Request: BUTTON_DATA_INVALID'
    assert failure['outcome'] == 'rejected' and failure['elapsed_ms'] >= 0
    assert failure['message_id'] == store.get('deliveries', row['id'])['panel_id']
    assert 'fixture-bot-token' not in output and 'same@example.test' not in output and 'nt:v1:' not in output


def test_unknown_transport_is_logged_once_and_never_replayed(env, protocol, monkeypatch, capsys):
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout('https://api.telegram.org/botfixture-bot-token/secret')
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        monkeypatch.setattr(ui, '_session_lease', lambda: nullcontext(client))
        assert delivery.request('sendMessage', {'chat_id': 11, 'text': 'private-body', 'parse_mode': 'HTML'}) is None
    assert len(calls) == 1
    output = capsys.readouterr().out
    assert '"outcome": "unknown"' in output and 'ReadTimeout' in output
    assert 'fixture-bot-token' not in output and 'private-body' not in output


def test_error_echo_cannot_leak_credentials_into_diagnostics(env, protocol, capsys):
    protocol.reject['sendMessage'] = {'ok': False, 'error_code': 400,
        'description': 'Bad Request: unsupported private-token-value https://private.test/?token=secret'}
    delivery.request('sendMessage', {'chat_id': 11, 'text': 'private-body', 'parse_mode': 'HTML'})
    output = capsys.readouterr().out
    assert 'private-' not in output and 'token=secret' not in output
    assert 'description_sha256' in output


@pytest.mark.parametrize('action', ['view', 'quota', 'more'])
def test_missing_existing_panel_recreated_once_and_old_refs_fail_closed(env, protocol, action):
    from src.telegram import bot
    from src.tests.test_notifications import callback
    row = delivered(event(), env)[0]
    click(row, 'view')
    old_mid = store.get('deliveries', row['id'])['panel_id']
    old_ref = next('nt:v1:' + k for k, r in store.items('refs').items() if r['message_id'] == old_mid)
    del protocol.messages[(row['chat_id'], old_mid)]
    before = len(protocol.calls)
    click(row, action)
    current = store.get('deliveries', row['id'])
    assert current['panel_state'] == 'sent' and current['panel_id'] != old_mid
    assert sum(m == 'sendMessage' for m, p in protocol.calls[before:]) == 1
    assert (row['chat_id'], row['message_id']) in protocol.messages
    before = len(protocol.calls)
    bot._handle_callback(callback({**row, 'message_id': old_mid}, old_ref))
    assert not any(m.startswith('editMessage') for m, p in protocol.calls[before:])
    assert store.get('deliveries', row['id'])['panel_id'] == current['panel_id']


def test_ack_precedes_panel_io_and_never_claims_success(env, protocol):
    row = delivered(event(), env)[0]
    before = len(protocol.calls)
    click(row, 'more')
    calls = protocol.calls[before:]
    assert calls[0][0] == 'answerCallbackQuery'
    assert not calls[0][1].get('show_alert')
    assert '已打开' not in calls[0][1].get('text', '')
    assert sum(m == 'answerCallbackQuery' for m, p in calls) == 1


@pytest.mark.parametrize('code,description', [(400, 'Bad Request: BUTTON_DATA_INVALID'),
    (400, "Bad Request: can't parse entities"), (403, 'Forbidden: bot was blocked by the user'),
    (429, 'Too Many Requests: retry after 1'), (None, None)])
def test_other_rejections_and_unknown_never_recreate_or_retry_panel(env, protocol, monkeypatch, code, description):
    row = delivered(event(), env)[0]; click(row, 'view')
    mid = store.get('deliveries', row['id'])['panel_id']
    original = ui.notification_api
    attempts = []
    def fault(method, payload):
        if method == 'editMessageText':
            attempts.append(payload)
            return None if code is None else {'ok': False, 'error_code': code, 'description': description}
        return original(method, payload)
    delivery.install(fault, recover=False)
    before = len(protocol.calls)
    click(row, 'quota')
    current = store.get('deliveries', row['id'])
    assert current['panel_id'] == mid and current['panel_state'] == ('unknown' if code is None else 'failed')
    assert len(attempts) == 1
    # A separate failure receipt is permitted, never a duplicate detail/operation.
    sends = [p for m, p in protocol.calls[before:] if m == 'sendMessage']
    assert all('未确认详情面板更新' in p['text'] for p in sends)


def test_uneditable_existing_panel_is_replaced_once_only(env, protocol):
    row = delivered(event(), env)[0]; click(row, 'view')
    old = store.get('deliveries', row['id'])['panel_id']
    original = ui.notification_api
    attempts = []
    def fault(method, payload):
        if method == 'editMessageText' and payload['message_id'] == old:
            attempts.append(payload)
            return {'ok': False, 'error_code': 400, 'description': "Bad Request: message can't be edited"}
        return original(method, payload)
    delivery.install(fault, recover=False)
    click(row, 'quota')
    current = store.get('deliveries', row['id'])
    assert current['panel_state'] == 'sent' and current['panel_id'] != old and len(attempts) == 1
    assert (row['chat_id'], old) in protocol.messages  # never delete the old message


@pytest.mark.parametrize('unknown', [False, True])
def test_rebuild_send_failure_retains_failure_and_never_retries(env, protocol, unknown):
    row = delivered(event(), env)[0]; click(row, 'view')
    old = store.get('deliveries', row['id'])['panel_id']
    del protocol.messages[(row['chat_id'], old)]
    original = ui.notification_api
    attempts = []
    def fault(method, payload):
        if method == 'sendMessage':
            attempts.append(payload)
            return None if unknown else {'ok': False, 'error_code': 403}
        return original(method, payload)
    delivery.install(fault, recover=False)
    click(row, 'view')
    current = store.get('deliveries', row['id'])
    assert current['panel_id'] is None and current['panel_state'] == ('unknown' if unknown else 'failed')
    assert sum('当前精确对象管理详情' in p['text'] for p in attempts) == 1
    assert (row['chat_id'], row['message_id']) in protocol.messages


def credits_event():
    from src.notifications.events import NotificationEvent, Effect
    return NotificationEvent("account.credits.started", event().subject, Effect("confirmed", "credits_allowed"), {"metrics": []})


def confirmation_click(row, peer, index):
    from src.telegram import bot
    from src.tests.test_notifications import callback
    p = next(p for m, p in reversed(peer.calls) if m == 'editMessageReplyMarkup')
    ref = p['reply_markup']['inline_keyboard'][0][index]['callback_data']
    bot._handle_callback(callback({**row, 'message_id': p['message_id']}, ref))
    return p, ref


@pytest.mark.parametrize('cancel', [False, True])
def test_confirmation_and_cancel_use_bound_panel_and_single_consumption(env, protocol, controlled, cancel):
    from src.telegram import bot
    from src.tests.test_notifications import callback
    row = delivered(credits_event(), env)[0]
    click(row, 'stop_credits')
    p, ref = confirmation_click(row, protocol, int(cancel))
    assert store.get('deliveries', row['id'])['panel_state'] == 'sent'
    bot._handle_callback(callback({**row, 'message_id': p['message_id']}, ref))
    assert len(controlled[3].jobs) == (0 if cancel else 1)
    if not cancel:
        controlled[3].jobs[0]()
        assert len(controlled[2]) == 1


@pytest.mark.parametrize('unknown', [False, True])
def test_confirmation_markup_failure_cannot_authorize_business_action(env, protocol, controlled, unknown):
    from src.telegram import bot
    from src.tests.test_notifications import callback
    row = delivered(credits_event(), env)[0]
    original = ui.notification_api
    attempted = []
    def fault(method, payload):
        if method == 'editMessageReplyMarkup':
            attempted.append(payload)
            return None if unknown else {'ok': False, 'error_code': 400, 'description': 'Bad Request: BUTTON_DATA_INVALID'}
        return original(method, payload)
    delivery.install(fault, recover=False)
    click(row, 'stop_credits')
    assert len(attempted) == 1
    assert store.get('deliveries', row['id'])['panel_state'] == ('unknown' if unknown else 'failed')
    p = attempted[0]
    bot._handle_callback(callback({**row, 'message_id': p['message_id']}, p['reply_markup']['inline_keyboard'][0][0]['callback_data']))
    assert not controlled[3].jobs and not menu._confirmations


def test_confirmation_message_disappearing_between_text_and_buttons_rebinds(env, protocol, controlled):
    row = delivered(credits_event(), env)[0]
    original = ui.notification_api
    attempted = []
    def fault(method, payload):
        if method == 'editMessageReplyMarkup':
            attempted.append(payload)
            if len(attempted) == 1:
                del protocol.messages[(payload['chat_id'], payload['message_id'])]
        return original(method, payload)
    delivery.install(fault, recover=False)
    click(row, 'stop_credits')
    assert len(attempted) == 2 and attempted[0]['message_id'] != attempted[1]['message_id']
    assert store.get('deliveries', row['id'])['panel_state'] == 'sent'
    assert attempted[0]['reply_markup']['inline_keyboard'][0][0]['callback_data'] not in menu._confirmations
    confirmation_click(row, protocol, 0)
    assert len(controlled[3].jobs) == 1


def test_async_terminal_receipt_recreates_missing_progress_panel_without_reexecuting(env, protocol, controlled):
    row = delivered(event(), env)[0]
    click(row, 'refresh_usage')
    old = store.get('deliveries', row['id'])['panel_id']
    del protocol.messages[(row['chat_id'], old)]
    assert len(controlled[3].jobs) == 1
    controlled[3].jobs[0]()
    current = store.get('deliveries', row['id'])
    assert current['panel_state'] == 'sent' and current['panel_id'] != old
    assert 'succeeded' in protocol.messages[(row['chat_id'], current['panel_id'])]['text']
    assert len(controlled[2]) == 1


def test_ack_does_not_wait_for_another_callback_panel_lock(env, protocol):
    import threading
    from src.telegram import bot
    from src.tests.test_notifications import callback
    row = delivered(event(), env)[0]
    ref = store.action_ref(row['id'], 'view')
    ack = threading.Event()
    original = ui.notification_api
    def observed(method, payload):
        result = original(method, payload)
        if method == 'answerCallbackQuery': ack.set()
        return result
    delivery.install(observed, recover=False)
    with menu._lock:
        thread = threading.Thread(target=bot._handle_callback, args=(callback(row, ref),))
        thread.start()
        assert ack.wait(2), 'ack blocked behind another panel I/O'
    thread.join(5)
    assert not thread.is_alive()


def test_eleven_megabyte_synthetic_durable_acks_before_write_and_batches_refs(env, protocol, monkeypatch, capsys):
    import time
    row = delivered(event(), env)[0]
    # All invented data; never read or copy a production snapshot.
    fixture = {str(i): {'fixture': 'x' * 850, 'ordinal': i} for i in range(13258)}
    env[1]._mutate('codex_logical_sessions', lambda rows: rows.update(fixture), strict=True)
    size = __import__('os').path.getsize(env[1]._paths['durable'])
    assert size >= 11_000_000
    ref = store.action_ref(row['id'], 'more')
    from src.telegram import bot
    from src.tests.test_notifications import callback
    writes = []
    write = env[1].write_snapshot
    before = len(protocol.calls)
    def measured(*a, **kw):
        assert any(m == 'answerCallbackQuery' for m, p in protocol.calls[before:]), 'durable I/O delayed ack'
        started = time.monotonic()
        result = write(*a, **kw)
        writes.append(time.monotonic() - started)
        return result
    monkeypatch.setattr(env[1], 'write_snapshot', measured)
    started = time.monotonic()
    bot._handle_callback(callback(row, ref))
    total = time.monotonic() - started
    assert store.get('deliveries', row['id'])['panel_state'] == 'sent'
    assert len(writes) == 3  # panel-id, all button refs together, terminal state
    assert protocol.calls[before][0] == 'answerCallbackQuery'
    print(json.dumps({'synthetic_durable_bytes': size, 'panel_writes': len(writes), 'write_seconds': writes, 'callback_seconds': total}))
