"""OAuth transport failures must not permanently empty an otherwise usable pool."""
from __future__ import annotations

import copy
import json
import sqlite3

import httpx
import pytest

from ._isolation import isolate

isolate()

from src import config, cooldown, drain, oauth_manager
from src.protocols import finalize
from src.tests import test_cooldown_success_recovery as cooldown_tests
from src.tests import test_protocol_fake_upstreams as fake

isolated_cooldown = cooldown_tests.isolated_cooldown


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    cfg = copy.deepcopy(config.get())
    cfg.update(oauthAccounts=[], channels=[], modelMapping={"global": {}},
               apiKeys={"fixture": {"key": "ccp-test", "enabled": True}},
               retry={"transient": {"enabled": False}, "recovery": {"oauthRefresh": True}})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_mtime", 0.0)
    monkeypatch.setattr(config, "_reload_callbacks", [])
    config.reload()
    m = fake._import_modules()
    monkeypatch.setattr(m["registry"], "_channels", {})
    monkeypatch.setattr(m["upstream"], "_client", None)
    drain.reset_for_tests()
    fake._setup(m)
    yield m
    fake._setup(m)
    drain.reset_for_tests()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protected", [False, True])
async def test_http_oauth_auth_failure_reaches_healthy_next_account(
    pool_env, monkeypatch, capsys, stream, protected,
):
    m = pool_env
    monkeypatch.setenv("PARROT_NO_REFRESH", "1" if protected else "0")
    config.update(lambda cfg: cfg.update(oauthAccounts=[{
        "provider": "openai", "email": name, "enabled": True,
        "workspace_id": f"ws-{name}", "chatgpt_account_id": f"ws-{name}",
        "models": ["gpt-5"], "access_token": "fixture-only-token",
        "refresh_token": "fixture-only-refresh", "expired": "2099-01-01T00:00:00Z",
        "account_model_catalog": {"schema": 1, "models": [{"id": "gpt-5", "useResponsesLite": False}]},
    } for name in ["expired@example.test", "healthy@example.test"]]))
    bad = fake._make_openai_oauth_channel("expired@example.test")
    good = fake._make_openai_oauth_channel("healthy@example.test")
    fake._install_channels(m, [bad, good])
    # Keep real request assembly, wire parsing, retry/finalization and logging;
    # isolate only token acquisition and the external upstream.
    async def token(ch):
        return "fixture-bad" if ch.key == bad.key else "fixture-good"
    refresh_calls = []
    async def refresh(*args, **kwargs):
        refresh_calls.append(args[0])
        return "fixture-bad"  # A still-rejected credential, not recovery proof.
    monkeypatch.setattr(oauth_manager, "ensure_channel_token", token)
    monkeypatch.setattr(oauth_manager, "force_refresh", refresh)
    from src.scheduler import ScheduleResult
    monkeypatch.setattr(m["scheduler"], "schedule", lambda *a, **kw: ScheduleResult(
        candidates=[(bad, "gpt-5"), (good, "gpt-5")],
        saturated=[], affinity_hit=False, fp_query=None, client_key=None,
    ))
    attempts = []
    def wire(req):
        auth = req.headers.get("authorization")
        attempts.append(auth)
        if auth == "Bearer fixture-bad":
            return httpx.Response(401, json={"error": {"code": "token_expired"}})
        assert auth == "Bearer fixture-good"
        return fake._responses_sse_response("healthy-pool-ok")
    router = fake.MockRouter()
    router.register("https://chatgpt.com", wire)
    response, client = await fake._call_openai_handler(m, router, "responses", {
        "model": "gpt-5", "input": "hello", "stream": stream, "store": False,
    })
    try:
        if hasattr(response, "body_iterator"):
            body = b""
            async for chunk in response.body_iterator:
                body += chunk.encode() if isinstance(chunk, str) else chunk
        else:
            body = response.body
    finally:
        await client.aclose()
    assert response.status_code == 200, body
    assert b"healthy-pool-ok" in body
    assert attempts == ["Bearer fixture-bad"] * (1 if protected else 2) + ["Bearer fixture-good"]
    assert len(refresh_calls) == (0 if protected else 1)
    output = capsys.readouterr().out
    if protected:
        assert "refreshed; retrying same channel" not in output
    row = m["log_db"]._get_conn().execute("SELECT * FROM request_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "success"
    assert row["final_channel_key"] == good.key
    assert row["retry_count"] == (1 if protected else 2)
    assert m["cooldown"].get_state(bad.key, "gpt-5") is None
    assert all(a.get("enabled") and not a.get("disabled_reason") for a in config.get()["oauthAccounts"])


@pytest.mark.parametrize("failure_policy", ["runtime", "post_commit_stream", "cooldown_only"])
@pytest.mark.parametrize("outcome", ["connection_timeout", "connect_timeout", "first_byte_timeout", "idle_timeout", "total_timeout",
    "http_connect_timeout", "pool_timeout", "write_timeout", "read_timeout", "transport_timeout",
    "connect_error", "proxy_connect_error", "transport_error"])
def test_oauth_transport_ladder_is_finite(monkeypatch, isolated_cooldown, outcome, failure_policy):
    entries, _, events = isolated_cooldown
    key = ("oauth:openai:pool", "model")
    monkeypatch.setattr(cooldown, "_windows", lambda: [1, 3, 5, 10, 15, 0])
    monkeypatch.setattr(cooldown, "_grace_count", lambda _: 0)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 10_000_000)
    entries[key] = {"error_count": 30, "cooldown_until": 1,
                    "first_error_at": 1, "last_advance_at": 1}
    scorer = type("Scorer", (), {"record_failure": staticmethod(lambda *a, **kw: None)})()
    finalize.apply_error_health_effects(finalize.error_plan(outcome, failure_policy=failure_policy), scorer=scorer,
        cooldown=cooldown, channel_key=key[0], model=key[1], error_detail="fixture transport failure")
    assert entries[key]["cooldown_until"] == 10_900_000
    assert events == []
    assert cooldown.is_blocked(*key)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 10_900_001)
    assert not cooldown.is_blocked(*key)


@pytest.mark.parametrize("channel,message,expected", [
    ("oauth:openai:one", "connection_timeout while opening upstream response", True),
    ("oauth:claude:one", "first_byte_timeout", True),
    ("oauth:openai:one", "HTTP 401: token_expired", False),
    ("oauth:openai:one", "HTTP 429: quota exhausted; connection_timeout", False),
    ("oauth:openai:one", "operator freeze", False),
    ("oauth:openai:one", "unknown timeout", False),
    ("oauth:openai:one", "connection_timeout in vendor-controlled prose", False),
    ("oauth:openai:one", None, False),
    ("api:one", "connection_timeout while opening upstream response", False),
])
def test_legacy_oauth_transport_freezes_get_bounded_retry_window(
    monkeypatch, isolated_cooldown, channel, message, expected,
):
    entries, _, events = isolated_cooldown
    monkeypatch.setattr(cooldown, "_initialized", False)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 1_000_000)
    monkeypatch.setattr(cooldown, "_windows", lambda: [1, 15, 0])
    monkeypatch.setattr(cooldown.state_db, "error_load_all", lambda: [{
        "channel_key": channel, "model": "model", "error_count": 12,
        "cooldown_until": -1, "last_error_message": message,
    }])
    saves = []
    monkeypatch.setattr(cooldown.state_db, "error_save", lambda *args: saves.append(args))
    cooldown.init()
    assert entries[(channel, "model")]["cooldown_until"] == (1_900_000 if expected else -1)
    assert len(saves) == int(expected)
    assert entries[(channel, "model")]["error_count"] == 12
    assert entries[(channel, "model")]["last_error_message"] == message
    assert events == []


def test_legacy_recovery_save_failure_does_not_publish_unblocked_memory(monkeypatch, isolated_cooldown):
    entries, _, _ = isolated_cooldown
    key = ("oauth:openai:one", "model")
    entries[key] = {"cooldown_until": -1}
    monkeypatch.setattr(cooldown, "_initialized", False)
    monkeypatch.setattr(cooldown.state_db, "error_load_all", lambda: [{
        "channel_key": key[0], "model": key[1], "error_count": 12,
        "cooldown_until": -1, "last_error_message": "connection_timeout while opening upstream response",
    }])
    def fail(*args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(cooldown.state_db, "error_save", fail)
    with pytest.raises(sqlite3.OperationalError):
        cooldown.init()
    assert entries[key]["cooldown_until"] == -1
    assert not cooldown._initialized


@pytest.mark.parametrize("channel,outcome", [
    ("api:one", "connection_timeout"),
    ("oauth:openai:one", "upstream_error_json"),
    ("oauth:openai:one", "unknown_failure"),
])
def test_nontransport_and_api_keep_configured_permanent_policy(monkeypatch, isolated_cooldown, channel, outcome):
    entries, _, events = isolated_cooldown
    key = (channel, "model")
    monkeypatch.setattr(cooldown, "_windows", lambda: [1, 0])
    monkeypatch.setattr(cooldown, "_grace_count", lambda _: 0)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 10_000_000)
    entries[key] = {"error_count": 30, "cooldown_until": 1,
                    "first_error_at": 1, "last_advance_at": 1}
    scorer = type("Scorer", (), {"record_failure": staticmethod(lambda *a, **kw: None)})()
    finalize.apply_error_health_effects(finalize.error_plan(outcome), scorer=scorer,
        cooldown=cooldown, channel_key=channel, model="model",
        error_detail="connection_timeout in vendor-controlled prose")
    assert entries[key]["cooldown_until"] == -1
    assert len(events) == 1


@pytest.mark.parametrize("deadline", [-1, 99_000_000])
def test_explicit_or_existing_freeze_not_overridden_by_inflight_transport(monkeypatch, isolated_cooldown, deadline):
    entries, _, _ = isolated_cooldown
    key = ("oauth:openai:one", "model")
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 1_000_000)
    message = "operator freeze" if deadline == -1 else "HTTP 429: quota exhausted"
    cooldown.record_error(*key, message, cooldown_until=deadline, transient_transport=True)
    assert entries[key]["cooldown_until"] == deadline
    cooldown.record_error(*key, "connection_timeout while opening upstream response", transient_transport=True)
    assert entries[key]["cooldown_until"] == deadline
    if deadline == -1:
        assert entries[key]["last_error_message"] == message
    assert not cooldown.clear_on_success(*key)


@pytest.mark.parametrize("windows,minutes", [([0], 15), ([0, 0], 15), ([1, 30, 0], 30), ([1, 5], 5)])
def test_oauth_transport_finite_ceiling(monkeypatch, isolated_cooldown, windows, minutes):
    entries, _, _ = isolated_cooldown
    key = ("oauth:openai:one", "model")
    monkeypatch.setattr(cooldown, "_windows", lambda: windows)
    monkeypatch.setattr(cooldown, "_grace_count", lambda _: 0)
    monkeypatch.setattr(cooldown, "_now_ms", lambda: 10_000_000)
    entries[key] = {"error_count": 30, "cooldown_until": 1,
                    "first_error_at": 1, "last_advance_at": 1}
    cooldown.record_error(*key, "timeout", transient_transport=True)
    assert entries[key]["cooldown_until"] == 10_000_000 + minutes * 60_000


def test_legacy_recovery_persists_once_and_reenters_real_scheduler(pool_env, monkeypatch):
    m = pool_env
    ch = fake._make_openai_oauth_channel("legacy-timeout@example.test")
    fake._install_channels(m, [ch])
    now = [10_000_000]
    monkeypatch.setattr(cooldown, "_now_ms", lambda: now[0])
    monkeypatch.setattr(cooldown, "_windows", lambda: [1, 15, 0])
    m["state_db"].error_save(ch.key, "gpt-5", 14, -1, "connection_timeout while opening upstream response")
    cooldown._initialized = False
    cooldown.init()
    state = cooldown.get_state(ch.key, "gpt-5")
    assert state["cooldown_until"] == 10_900_000
    saved = next(x for x in m["state_db"].error_load_all() if x["channel_key"] == ch.key)
    assert saved["cooldown_until"] == state["cooldown_until"]
    assert saved["error_count"] == 14
    assert m["scheduler"]._filter_candidates("gpt-5", "responses", {"model": "gpt-5", "input": "hello"})[0] == []
    now[0] += 1_000
    cooldown._initialized = False
    cooldown.init()
    assert cooldown.get_state(ch.key, "gpt-5")["cooldown_until"] == 10_900_000
    now[0] = 10_900_001
    available = m["scheduler"]._filter_candidates("gpt-5", "responses", {"model": "gpt-5", "input": "hello"})[0]
    assert (ch, "gpt-5") in available
    assert cooldown.clear_on_success(ch.key, "gpt-5")
    assert not any(x["channel_key"] == ch.key for x in m["state_db"].error_load_all())
