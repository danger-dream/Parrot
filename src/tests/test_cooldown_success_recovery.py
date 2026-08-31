"""Cooldown success/recovery semantics under concurrent in-flight requests."""

from __future__ import annotations

from contextlib import nullcontext

import pytest

from src import cooldown


@pytest.fixture
def isolated_cooldown(monkeypatch):
    entries: dict[tuple[str, str], dict] = {}
    deletes: list[tuple[str | None, str | None]] = []
    events: list[tuple[str, str]] = []

    monkeypatch.setattr(cooldown, "_entries", entries)
    monkeypatch.setattr(cooldown.channel_state, "resolve", lambda key: key)
    monkeypatch.setattr(cooldown.channel_state, "is_deleted", lambda _key: False)
    monkeypatch.setattr(
        cooldown.state_db,
        "optional_write_timeout",
        lambda: nullcontext(),
    )
    monkeypatch.setattr(
        cooldown.state_db,
        "error_save",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cooldown.state_db,
        "error_delete",
        lambda channel_key, model: deletes.append((channel_key, model)),
    )
    monkeypatch.setattr(
        cooldown.notifier,
        "notify_event",
        lambda event_key, text, **_kwargs: events.append((event_key, text)),
    )
    return entries, deletes, events


def _state(cooldown_until):
    return {
        "error_count": 4,
        "cooldown_until": cooldown_until,
        "last_error_message": 'HTTP 429: {"detail":"Rate limit exceeded"}',
        "first_error_at": 1_000,
        "last_advance_at": 1_000,
    }


def test_inflight_success_does_not_reopen_active_cooldown_or_notify(
    monkeypatch, isolated_cooldown,
):
    entries, deletes, events = isolated_cooldown
    key = ("oauth:openai:test", "gpt-5.6-terra")
    entries[key] = _state(6_000)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 1_000)

    # Multiple successes from requests dispatched before the 429 must not create
    # cooldown -> recovered -> cooldown oscillation.
    assert cooldown.clear_on_success(*key) is False
    assert cooldown.clear_on_success(*key) is False
    assert cooldown.clear_on_success(*key) is False

    assert entries[key]["cooldown_until"] == 6_000
    assert deletes == []
    assert events == []


def test_explicit_429_then_inflight_success_preserves_deadline_until_expiry(
    monkeypatch, isolated_cooldown,
):
    entries, deletes, events = isolated_cooldown
    key = ("oauth:openai:test", "gpt-5.6-terra")
    now = [1_000]
    monkeypatch.setattr(cooldown, "_now_ms", lambda: now[0])
    monkeypatch.setattr(cooldown, "_grace_count", lambda _key: 3)

    cooldown.record_error(
        *key,
        'HTTP 429: {"detail":"Rate limit exceeded"}',
        cooldown_until=6_000,
    )
    assert entries[key]["cooldown_until"] == 6_000

    assert cooldown.clear_on_success(*key) is False
    assert key in entries
    assert deletes == []
    assert events == []

    now[0] = 6_001
    assert cooldown.clear_on_success(*key) is True
    assert key not in entries
    assert deletes == [key]
    assert events == []


def test_success_silently_clears_grace_or_naturally_expired_state(
    monkeypatch, isolated_cooldown,
):
    entries, deletes, events = isolated_cooldown
    grace_key = ("oauth:openai:test", "grace-model")
    expired_key = ("oauth:openai:test", "expired-model")
    entries[grace_key] = _state(None)
    entries[expired_key] = _state(999)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 1_000)

    assert cooldown.clear_on_success(*grace_key) is True
    assert cooldown.clear_on_success(*expired_key) is True

    assert entries == {}
    assert deletes == [grace_key, expired_key]
    assert events == []


def test_explicit_recovery_can_clear_active_cooldown_and_notifies_once(
    monkeypatch, isolated_cooldown,
):
    entries, deletes, events = isolated_cooldown
    key = ("api:verified-recovery", "model")
    entries[key] = _state(6_000)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 1_000)

    cooldown.clear(*key)
    cooldown.clear(*key)

    assert entries == {}
    assert deletes == [key, key]
    assert len(events) == 1
    assert events[0][0] == "channel_recovered"
    assert "渠道恢复" in events[0][1]
