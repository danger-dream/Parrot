from __future__ import annotations

import copy
import json
import os
import threading

import pytest

from src import config
from src.management_auth import (
    ApprovalError,
    ApprovalService,
    ApprovalStatus,
    AuthMethod,
    AuthenticationRateLimited,
    ManagementStateStore,
    SessionAuthenticationError,
    SessionPolicy,
    SessionService,
)


FAKE_KEY = "pmk_" + "K" * 64
OTHER_FAKE_KEY = "pmk_" + "R" * 64


class Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeNotifier:
    def __init__(self, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.calls = []

    def send(self, admin_ids, notification):
        self.calls.append((admin_ids, notification))
        return self.succeeds


def make_store(tmp_path, clock: Clock, **kwargs) -> ManagementStateStore:
    return ManagementStateStore(
        str(tmp_path / "management-state.db"), clock=clock, **kwargs,
    )


def make_sessions(
    store: ManagementStateStore,
    clock: Clock,
    *,
    key: str = FAKE_KEY,
    idle: int = 3 * 24 * 60 * 60,
    absolute: int = 30 * 24 * 60 * 60,
    touch: int = 300,
    per_source: int = 5,
) -> SessionService:
    return SessionService(
        store,
        management_key=key,
        policy=SessionPolicy(
            idle_timeout_seconds=idle,
            absolute_timeout_seconds=absolute,
            touch_interval_seconds=touch,
        ),
        clock=clock,
        rate_limit_per_source=per_source,
        rate_limit_global=max(per_source, 30),
    )


def make_approvals(
    store,
    clock,
    notifier,
    *,
    admins=(42,),
    configured=True,
    ttl=180,
):
    return ApprovalService(
        store,
        clock=clock,
        ttl_seconds=ttl,
        admin_ids_provider=lambda: tuple(admins),
        telegram_configured_provider=lambda: configured,
        notifier=notifier,
    )


def test_missing_management_key_has_documented_format_and_is_stable(monkeypatch):
    monkeypatch.setattr(config.secrets, "token_urlsafe", lambda size: "x" * 64)
    candidate = {"management": copy.deepcopy(config.DEFAULT_CONFIG["management"])}
    assert config._normalize_management_config(candidate, {}) is True
    assert candidate["management"]["managementKey"] == "pmk_" + "x" * 64
    assert config._normalize_management_config(candidate, candidate) is False
    assert candidate["management"]["managementKey"] == "pmk_" + "x" * 64


def test_management_config_backfill_persists_private_key_without_printing_it(capsys):
    original = config.get()
    old = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "apiKeys": {},
        "oauthAccounts": [],
        "channels": [],
        "telegram": {"botToken": "", "adminIds": []},
    }
    try:
        with open(config.path(), "w", encoding="utf-8") as stream:
            json.dump(old, stream)
        config._cache = None
        config._mtime = 0
        loaded = config.reload()
        key = loaded["management"]["managementKey"]
        assert key.startswith("pmk_") and len(key) == 68
        with open(config.path(), "r", encoding="utf-8") as stream:
            assert json.load(stream)["management"]["managementKey"] == key
        assert key not in capsys.readouterr().out
        assert os.stat(config.path()).st_mode & 0o077 == 0
    finally:
        with open(config.path(), "w", encoding="utf-8") as stream:
            json.dump(original, stream)
        config._cache = None
        config._mtime = 0
        config.reload()


def test_management_settings_validate_security_bounds_and_origins(tmp_path):
    cfg = copy.deepcopy(config.DEFAULT_CONFIG)
    cfg["management"]["managementKey"] = FAKE_KEY
    cfg["management"]["stateDbPath"] = str(tmp_path / "state.db")
    cfg["management"]["allowedOrigins"] = ["https://admin.example.test/"]
    settings = config.management_settings(cfg)
    assert settings["sessionIdleTimeoutSeconds"] == 3 * 24 * 60 * 60
    assert settings["allowedOrigins"] == ("https://admin.example.test",)
    cfg["management"]["allowedOrigins"] = ["*"]
    with pytest.raises(ValueError):
        config.management_settings(cfg)
    cfg["management"]["allowedOrigins"] = []
    cfg["management"]["managementKey"] = "short"
    with pytest.raises(ValueError):
        config.management_settings(cfg)


def test_sessions_are_multi_device_hashed_touch_throttled_and_revocable(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    sessions = make_sessions(store, clock, idle=100, absolute=500, touch=60)
    first = sessions.create_from_management_key(
        FAKE_KEY, source="198.51.100.1", request_id="req-first",
    )
    second = sessions.create_from_management_key(
        FAKE_KEY, source="198.51.100.2", request_id="req-second",
    )
    assert first.credential != second.credential
    assert sessions.verify(first.credential).principal.session_id == first.principal.session_id
    selector = first.credential[4:].split(".", 1)[0]
    stored = store.get_session(selector)
    assert stored is not None
    assert bytes(stored["verifier"]) != first.credential.encode()
    initial_seen = stored["last_seen_at"]

    clock.advance(30)
    sessions.verify(first.credential)
    assert store.get_session(selector)["last_seen_at"] == initial_seen
    clock.advance(31)
    touched = sessions.verify(first.credential)
    assert store.get_session(selector)["last_seen_at"] == clock.value
    assert touched.idle_expires_at.timestamp() == clock.value + 100

    sessions.revoke_current(first.credential, request_id="req-revoke")
    with pytest.raises(SessionAuthenticationError):
        sessions.verify(first.credential)
    assert sessions.verify(second.credential).principal.session_id == second.principal.session_id
    audit_text = repr(store.audit_snapshot())
    assert first.credential not in audit_text and FAKE_KEY not in audit_text
    store.close()
    db_bytes = (tmp_path / "management-state.db").read_bytes()
    assert first.credential.encode() not in db_bytes
    assert second.credential.encode() not in db_bytes
    assert os.stat(tmp_path / "management-state.db").st_mode & 0o077 == 0


def test_session_idle_and_absolute_expiry_are_distinct(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    sessions = make_sessions(store, clock, idle=60, absolute=150, touch=10)
    idle = sessions.create_from_management_key(
        FAKE_KEY, source="idle", request_id="req-idle",
    )
    clock.advance(60)
    with pytest.raises(SessionAuthenticationError) as expired:
        sessions.verify(idle.credential)
    assert expired.value.reason == "expired"

    absolute = sessions.create_from_management_key(
        FAKE_KEY, source="absolute", request_id="req-absolute",
    )
    for _ in range(4):
        clock.advance(30)
        sessions.verify(absolute.credential)
    clock.advance(30)
    with pytest.raises(SessionAuthenticationError) as expired_absolute:
        sessions.verify(absolute.credential)
    assert expired_absolute.value.reason == "expired"


def test_revoke_all_and_key_rotation_change_generation(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    sessions = make_sessions(store, clock)
    one = sessions.create_from_management_key(FAKE_KEY, source="one", request_id="r1")
    two = sessions.create_from_management_key(FAKE_KEY, source="two", request_id="r2")
    assert sessions.revoke_all(one.principal, request_id="revoke-all") == 2
    for issued in (one, two):
        with pytest.raises(SessionAuthenticationError):
            sessions.verify(issued.credential)
    fresh = sessions.create_from_management_key(FAKE_KEY, source="fresh", request_id="r3")
    rotated = make_sessions(store, clock, key=OTHER_FAKE_KEY)
    with pytest.raises(SessionAuthenticationError):
        rotated.verify(fresh.credential)
    with pytest.raises(SessionAuthenticationError):
        rotated.create_from_management_key(FAKE_KEY, source="old", request_id="r4")
    assert rotated.create_from_management_key(
        OTHER_FAKE_KEY, source="new", request_id="r5",
    ).principal.auth_method is AuthMethod.MANAGEMENT_KEY


def test_reset_key_signs_out_only_key_sessions_and_survives_restart(tmp_path):
    from src.management_auth.principal import ADMINISTRATOR_CAPABILITIES, Role
    clock = Clock()
    store = make_store(tmp_path, clock)
    sessions = make_sessions(store, clock)
    keyed = sessions.create_from_management_key(FAKE_KEY, source="key", request_id="r1")
    approved = sessions.issue_for_principal(
        subject_id="telegram:42", auth_method=AuthMethod.TELEGRAM_APPROVAL,
        roles=(Role.ADMINISTRATOR,), capabilities=ADMINISTRATOR_CAPABILITIES,
    )
    assert sessions.rotate_management_key(OTHER_FAKE_KEY) == 1
    with pytest.raises(SessionAuthenticationError):
        sessions.verify(keyed.credential)
    assert sessions.verify(approved.credential)
    with pytest.raises(SessionAuthenticationError):
        sessions.create_from_management_key(FAKE_KEY, source="old", request_id="r2")
    assert sessions.create_from_management_key(OTHER_FAKE_KEY, source="new", request_id="r3")
    # A restart with the reset key finds the same fingerprint and keeps every session.
    restarted = make_sessions(store, clock, key=OTHER_FAKE_KEY)
    assert restarted.verify(approved.credential)


def test_management_key_attempts_are_rate_limited_without_reason_or_secret(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    sessions = make_sessions(store, clock, per_source=2)
    for value in ("wrong-one", "wrong-two"):
        with pytest.raises(SessionAuthenticationError):
            sessions.create_from_management_key(value, source="same", request_id="rate")
    with pytest.raises(AuthenticationRateLimited):
        sessions.create_from_management_key(FAKE_KEY, source="same", request_id="rate")
    assert FAKE_KEY not in repr(store.audit_snapshot())
    clock.advance(61)
    assert sessions.create_from_management_key(
        FAKE_KEY, source="same", request_id="after-window",
    )


def test_telegram_approval_fails_closed_and_never_notifies_with_secrets(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    notifier = FakeNotifier()
    for admins, configured in (((42,), False), ((), True)):
        service = make_approvals(
            store, clock, notifier, admins=admins, configured=configured,
        )
        with pytest.raises(ApprovalError) as unavailable:
            service.create(
                client_name="browser",
                source_address="203.0.113.4",
                device_summary="test-device",
                request_id="closed",
            )
        assert unavailable.value.reason == "unavailable"
    assert notifier.calls == []


def test_telegram_approval_is_browser_bound_one_time_and_atomic(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    notifier = FakeNotifier()
    approvals = make_approvals(store, clock, notifier)
    sessions = make_sessions(store, clock)
    issued = approvals.create(
        client_name="test-browser",
        source_address="203.0.113.8",
        device_summary="fake-device",
        request_id="approval-create",
    )
    assert issued.expires_at.timestamp() - clock.value == 180
    assert approvals.get(issued.approval_id, issued.exchange_secret).status is ApprovalStatus.PENDING
    assert len(notifier.calls) == 1
    admin_ids, notification = notifier.calls[0]
    assert admin_ids == (42,)
    assert notification.approve_callback.startswith("mauth:a:map_")
    assert len(notification.approve_callback.encode()) <= 64
    assert issued.exchange_secret not in repr(notification)
    assert FAKE_KEY not in repr(notification)

    with pytest.raises(ApprovalError) as wrong_admin:
        approvals.decide(issued.approval_id, telegram_user_id=99, approved=True)
    assert wrong_admin.value.reason == "forbidden"
    assert approvals.decide(
        issued.approval_id, telegram_user_id=42, approved=True,
    ) is ApprovalStatus.APPROVED
    with pytest.raises(ApprovalError) as repeated_decision:
        approvals.decide(
            issued.approval_id, telegram_user_id=42, approved=False,
        )
    assert repeated_decision.value.reason == "alreadyDecided"
    assert approvals.get(
        issued.approval_id, issued.exchange_secret,
    ).status is ApprovalStatus.APPROVED

    successes = []
    failures = []

    def exchange():
        try:
            successes.append(sessions.create_from_telegram_approval(
                approval_id=issued.approval_id,
                exchange_secret=issued.exchange_secret,
                request_id="approval-exchange",
            ))
        except SessionAuthenticationError as exc:
            failures.append(exc.reason)

    threads = [threading.Thread(target=exchange) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1 and failures == ["consumed"]
    assert successes[0].principal.auth_method is AuthMethod.TELEGRAM_APPROVAL
    assert approvals.get(
        issued.approval_id, issued.exchange_secret,
    ).status is ApprovalStatus.CONSUMED
    assert sessions.verify(successes[0].credential).principal.session_id
    audit = repr(store.audit_snapshot())
    assert issued.exchange_secret not in audit
    assert successes[0].credential not in audit


def test_approval_denial_expiry_cross_challenge_and_secret_replay(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    notifier = FakeNotifier()
    approvals = make_approvals(store, clock, notifier, ttl=180)
    sessions = make_sessions(store, clock)
    denied = approvals.create(
        client_name="denied",
        source_address="source-a",
        device_summary=None,
        request_id="denied",
    )
    approvals.decide(denied.approval_id, telegram_user_id=42, approved=False)
    with pytest.raises(ApprovalError) as repeated_denial:
        approvals.decide(denied.approval_id, telegram_user_id=42, approved=False)
    assert repeated_denial.value.reason == "alreadyDecided"
    with pytest.raises(SessionAuthenticationError) as denied_exchange:
        sessions.create_from_telegram_approval(
            approval_id=denied.approval_id,
            exchange_secret=denied.exchange_secret,
            request_id="denied-exchange",
        )
    assert denied_exchange.value.reason == "denied"

    pending = approvals.create(
        client_name="expires",
        source_address="source-b",
        device_summary=None,
        request_id="expires",
    )
    clock.advance(181)
    assert approvals.get(pending.approval_id, pending.exchange_secret).status is ApprovalStatus.EXPIRED

    first = approvals.create(
        client_name="first",
        source_address="source-c",
        device_summary=None,
        request_id="cross-1",
    )
    second = approvals.create(
        client_name="second",
        source_address="source-d",
        device_summary=None,
        request_id="cross-2",
    )
    approvals.decide(first.approval_id, telegram_user_id=42, approved=True)
    with pytest.raises(SessionAuthenticationError) as cross:
        sessions.create_from_telegram_approval(
            approval_id=first.approval_id,
            exchange_secret=second.exchange_secret,
            request_id="cross",
        )
    assert cross.value.reason == "failed"
    with pytest.raises(ApprovalError):
        approvals.get(first.approval_id, "wrong-browser-secret")
