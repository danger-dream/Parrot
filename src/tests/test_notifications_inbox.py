"""Console notification inbox: incident grouping, legacy texts, read state and API."""
from fastapi.testclient import TestClient

from src import notifier, request_events
from src.notifications import emit, inbox, store
from src.notifications.events import Effect, NotificationEvent
from src.tests.test_notifications import env, event  # noqa: F401 - fixture


def unread(page_size=50):
    return inbox.listing("unread", 1, page_size)


def test_repeats_group_into_one_entry_and_recovery_updates_it(env):
    assert emit(event())
    first, total, count = unread()
    assert (total, count) == (1, 1)
    item = first[0]
    assert item["id"].startswith("inc:") and item["status"] == "active" and item["unread"]
    assert item["title"] == "OpenAI · 额度达到暂停线"
    assert item["subject"].startswith("账号：A<b>")
    assert "已暂停调用此账号。" in item["lines"]
    assert item["links"] == [{"kind": "account", "id": "openai:same@example.test:w1", "label": "账户详情"}]

    recovered = NotificationEvent("account.quota.recovered", event().subject, Effect("confirmed", "account_resumed"), {"windows": ["5h"]})
    assert emit(recovered)
    items, total, _ = unread()
    assert total == 1
    assert items[0]["id"] == item["id"] and items[0]["status"] == "recovered" and items[0]["tone"] == "recovered"
    assert items[0]["count"] == 2


def test_transient_and_switched_off_types_stay_out(env):
    cfg = env[0]
    emit(NotificationEvent("account.auth.refreshed", event().subject, Effect("confirmed", "auth_refreshed"), {"expires_at": "2027-01-01T00:00:00Z"}))
    assert unread()[1] == 0
    emit(event())
    assert unread()[1] == 1
    cfg["notifications"] = {"enabled": True, "events": {"account.quota.blocked": False}}
    assert unread()[1] == 0


def test_legacy_text_is_kept_as_plain_text_with_its_page(env):
    assert notifier.notify_event("network_monitor", "⚠️ <b>网络检测失败</b>\n出口 &lt;x&gt; <code>1.2.3.4</code> 超时\n\n重试中") is not None
    items, total, _ = unread()
    assert total == 1
    item = items[0]
    assert item["source"] == "legacy" and item["tone"] == "warning"
    assert item["title"] == "网络检测失败"
    assert item["lines"] == ["出口 <x> 1.2.3.4 超时", "重试中"]
    assert item["links"] == [{"kind": "page", "id": "/network", "label": "网络检测"}]


def test_switched_off_legacy_event_is_not_recorded(env):
    env[0]["notifications"] = {"enabled": True, "events": {"network_monitor": False}}
    notifier.notify_event("network_monitor", "⚠️ <b>网络检测失败</b>")
    assert store.db().values("notification_feed") == []


def test_legacy_feed_is_bounded(env, monkeypatch):
    monkeypatch.setattr(inbox, "MAX_FEED", 3)
    for n in range(5):
        inbox.remember("app_update", f"<b>更新 {n}</b>")
    titles = sorted(row["title"] for row in store.db().values("notification_feed"))
    assert titles == ["更新 2", "更新 3", "更新 4"]


def test_read_marks_and_new_activity(env):
    emit(event())
    inbox.remember("app_update", "<b>有新版本</b>")
    items, _, count = unread()
    assert count == 2
    inbox.mark_read([items[0]["id"]])
    assert unread()[2] == 1
    assert [i["id"] for i in inbox.listing("read", 1, 50)[0]] == [items[0]["id"]]

    inbox.mark_read(everything=True)
    assert unread()[1] == 0 and inbox.listing("read", 1, 50)[1] == 2
    marks = [key for key in store.items("policy") if key.startswith("inbox:")]
    assert marks == ["inbox:*"]

    # A later notification of the same incident is news again.
    emit(event(), recipients=[33])
    assert unread()[1] == 1


def test_paging_reports_total(env):
    for n in range(5):
        inbox.remember("app_update", f"<b>更新 {n}</b>")
    items, total, count = inbox.listing("unread", 2, 2)
    assert (len(items), total, count) == (2, 5, 5)


def test_recorded_notifications_announce_to_live_clients(env):
    seen = []
    unsubscribe = request_events.subscribe(lambda kind, payload: seen.append(kind))
    try:
        emit(event())
        inbox.remember("app_update", "<b>有新版本</b>")
    finally:
        unsubscribe()
    assert seen == ["notification", "notification"]


def test_api_lists_and_marks_read(env, tmp_path):
    from src.tests.management_system_network_support import bearer, build_p6_app, create_session
    from src.management_api.routers.notifications import router
    app, runtime, _fixture = build_p6_app(tmp_path / "api")
    app.include_router(router, prefix="/api/management/v1")
    emit(event())
    with TestClient(app) as client:
        token = create_session(client)
        r = client.get("/api/management/v1/notifications", headers=bearer(token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["data"]["unreadCount"] == 1 and body["meta"]["total"] == 1
        item = body["data"]["items"][0]
        assert item["unread"] and item["status"] == "active" and item["lastAt"].endswith("Z")
        r = client.get("/api/management/v1/notifications", params={"bogus": 1}, headers=bearer(token))
        assert r.status_code == 422
        r = client.post("/api/management/v1/notifications/actions/mark-read", json={"ids": [item["id"]]}, headers=bearer(token))
        assert r.status_code == 200, r.text
        assert r.json()["data"]["unreadCount"] == 0
        r = client.get("/api/management/v1/notifications", params={"view": "read"}, headers=bearer(token))
        assert [i["id"] for i in r.json()["data"]["items"]] == [item["id"]]
