"""Regression coverage for durable, lossless Codex compaction routing."""
import copy
import hashlib
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.tests import _isolation

_isolation.isolate()

from src import affinity, scheduler, state_db
from src.openai import codex_identity, compaction_owner
from src.openai.transform import codex_oauth_transform
from src import failover


class OAuthChannel:
    type = "oauth"
    protocol = "openai-responses"
    provider = "openai"
    upstream_stream_only = True
    disabled_reason = None

    def __init__(self, name, workspace, *, enabled=True):
        self.account_key = f"openai:{name}" + (f":{workspace}" if workspace else "")
        self.key = f"oauth:{self.account_key}"
        self.workspace_id = workspace
        self.chatgpt_account_id = workspace
        self.enabled = enabled

    def supports_model(self, model):
        return model if model == "gpt-5.5" else None


def body(cid="cmp_test", ec="opaque-compaction-ciphertext"):
    return {
        "model": "gpt-5.5",
        "stream": False,
        "input": [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "reasoning-ec"},
            {"type": "compaction", "id": cid, "encrypted_content": ec},
            {"type": "message", "role": "user", "content": "continue"},
        ],
    }


def configure_scheduler(monkeypatch, channels):
    monkeypatch.setattr(scheduler.registry, "all_channels", lambda: list(channels))
    monkeypatch.setattr(
        scheduler.registry, "get_channel",
        lambda key: next((ch for ch in channels if ch.key == key), None),
    )
    monkeypatch.setattr(scheduler.cooldown, "is_blocked", lambda *_: False)
    monkeypatch.setattr(scheduler.concurrency, "is_saturated", lambda *_: False)
    monkeypatch.setattr(scheduler.config, "get", lambda: {"channelSelection": "order"})


def test_single_owner_bootstrap_is_lossless_and_persists(monkeypatch):
    state_db.init()
    owner = OAuthChannel("one@example.com", "ws-one")
    configure_scheduler(monkeypatch, [owner])
    request = body("cmp_bootstrap")
    original = copy.deepcopy(request)

    route = scheduler.schedule(request, "tenant", "192.0.2.1", ingress_protocol="responses")
    assert [ch for ch, _ in route.candidates] == [owner]
    assert route.bound_channel_key == owner.key
    assert route.encrypted_content_count == 0
    upstream_body = failover._attempt_body_for_channel(
        request, owner.key, route.bound_channel_key, portable_body={"bad": "copy"},
    )
    assert upstream_body is request
    assert upstream_body == original
    assert upstream_body["input"][1]["encrypted_content"] == "opaque-compaction-ciphertext"
    assert upstream_body["input"][0]["encrypted_content"] == "reasoning-ec"

    monkeypatch.setattr(scheduler.config, "get", lambda: {
        "openaiOAuth": {
            "codexCliVersion": "0.153.4",
            "codexProtocolProfile": "rust-v0.153.4",
        },
    })
    transformed = codex_oauth_transform.apply_codex_oauth_transform(
        copy.deepcopy(upstream_body), resolved_model="gpt-5.5",
        use_responses_lite=False,
        base_instructions="You are a helpful coding assistant.",
    )
    wire_compaction = next(item for item in transformed["input"] if item.get("type") == "compaction")
    assert wire_compaction == original["input"][1]

    assert compaction_owner.persist_observed(owner, request) == 1
    generated = {
        "output": [{"type": "compaction", "id": "cmp_generated", "encrypted_content": "generated-cipher"}]
    }
    assert compaction_owner.persist_observed(owner, generated) == 1
    generated_ref = compaction_owner.complete_refs(generated)[0]
    assert state_db.compaction_owner_load(generated_ref.compaction_id, generated_ref.content_digest)
    ref = compaction_owner.complete_refs(request)[0]
    stored = state_db.compaction_owner_load(ref.compaction_id, ref.content_digest)
    assert stored["owner_identity"] == compaction_owner.owner_identity(owner)
    assert "opaque-compaction-ciphertext" not in repr(stored)


def test_durable_owner_survives_affinity_expiry_and_connection_restart(monkeypatch):
    state_db.init()
    owner = OAuthChannel("one@example.com", "ws-durable")
    other = OAuthChannel("two@example.com", "ws-other")
    request = body("cmp_durable", "cipher-durable")
    compaction_owner.persist_observed(owner, request)

    real_now_ms = state_db.now_ms
    monkeypatch.setattr(state_db, "now_ms", lambda: 1)
    affinity.upsert("expired-session", other.key, "gpt-5.5")
    monkeypatch.setattr(state_db, "now_ms", lambda: 30 * 60 * 1000 + 2)
    affinity.cleanup(30 * 60 * 1000)
    monkeypatch.setattr(state_db, "now_ms", real_now_ms)
    assert affinity.get("expired-session") is None

    # Simulate a process restart through the public lifecycle only.
    state_db.close()
    state_db.init()

    configure_scheduler(monkeypatch, [other, owner])
    route = scheduler.schedule(
        request, "tenant", "192.0.2.2", ingress_protocol="responses",
        fp_query="expired-session",
    )
    assert [ch for ch, _ in route.candidates] == [owner]
    assert route.bound_channel_key == owner.key


def test_unknown_multiple_owners_and_unavailable_owner_fail_without_mutation(monkeypatch):
    state_db.init()
    one = OAuthChannel("one@example.com", "ws-u1")
    two = OAuthChannel("two@example.com", "ws-u2")
    unknown = body("cmp_unknown_multi", "unknown-cipher")
    before = copy.deepcopy(unknown)
    configure_scheduler(monkeypatch, [one, two])

    route = scheduler.schedule(unknown, "tenant", "192.0.2.3", ingress_protocol="responses")
    assert not route
    assert route.guard_error.startswith("compaction_owner_unknown:")
    assert unknown == before

    known = body("cmp_removed_owner", "removed-cipher")
    compaction_owner.persist_observed(one, known)
    configure_scheduler(monkeypatch, [two])
    unavailable = scheduler.schedule(
        known, "tenant", "192.0.2.4", ingress_protocol="responses",
    )
    assert not unavailable
    assert unavailable.guard_error.startswith("compaction_owner_unavailable:")
    assert known["input"][1]["encrypted_content"] == "removed-cipher"


def test_conflicting_known_owners_are_rejected(monkeypatch):
    state_db.init()
    one = OAuthChannel("one@example.com", "ws-c1")
    two = OAuthChannel("two@example.com", "ws-c2")
    first = body("cmp_conflict_one", "cipher-one")
    second = body("cmp_conflict_two", "cipher-two")
    compaction_owner.persist_observed(one, first)
    compaction_owner.persist_observed(two, second)
    combined = body("cmp_conflict_one", "cipher-one")
    combined["input"].insert(2, second["input"][1])
    before = copy.deepcopy(combined)
    configure_scheduler(monkeypatch, [one, two])

    route = scheduler.schedule(combined, "tenant", "192.0.2.9", ingress_protocol="responses")
    assert not route
    assert route.guard_error.startswith("compaction_owner_conflict:")
    assert combined == before


def test_exact_affinity_bootstraps_one_of_multiple_owners(monkeypatch):
    state_db.init()
    one = OAuthChannel("one@example.com", "ws-a1")
    two = OAuthChannel("two@example.com", "ws-a2")
    configure_scheduler(monkeypatch, [two, one])
    affinity.upsert("exact-owner", one.key, "gpt-5.5")
    request = body("cmp_exact_bootstrap", "exact-cipher")

    route = scheduler.schedule(
        request, "tenant", "192.0.2.5", ingress_protocol="responses",
        fp_query="exact-owner",
    )
    assert [ch for ch, _ in route.candidates] == [one]
    compaction_owner.persist_observed(one, request)
    ref = compaction_owner.complete_refs(request)[0]
    assert state_db.compaction_owner_load(ref.compaction_id, ref.content_digest)


def test_refresh_workspace_and_registry_rebuild_keep_identity_and_adopt_old_row():
    state_db.init()
    pre_refresh = OAuthChannel("legacy@example.com", "")
    request = body("cmp_identity_migration", "identity-cipher")

    # Simulate the exact identity hash emitted by the initial implementation.
    old_raw = json.dumps({
        "provider": "openai",
        "account_key": pre_refresh.account_key,
        "workspace": "ws-stable",
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    old_identity = hashlib.sha256(old_raw.encode("utf-8")).hexdigest()
    ref = compaction_owner.complete_refs(request)[0]
    state_db.compaction_owner_upsert(
        ref.compaction_id, ref.content_digest, pre_refresh.key, old_identity,
    )

    # Refresh updates workspace on the old live channel but not its account_key;
    # registry rebuild creates the canonical composite key.
    pre_refresh.workspace_id = pre_refresh.chatgpt_account_id = "ws-stable"
    rebuilt = OAuthChannel("legacy@example.com", "ws-stable")
    assert compaction_owner.owner_identity(pre_refresh) == compaction_owner.owner_identity(rebuilt)
    selected = compaction_owner.select_owner([ref], [rebuilt], live_channels=[rebuilt])
    assert selected is rebuilt
    migrated = state_db.compaction_owner_load(ref.compaction_id, ref.content_digest)
    assert migrated["owner_identity"] == compaction_owner.owner_identity(rebuilt)
    assert migrated["owner_key"] == rebuilt.key


def test_unknown_bootstrap_uses_all_live_identities_not_only_eligible(monkeypatch):
    state_db.init()
    eligible = OAuthChannel("eligible@example.com", "ws-eligible")
    ineligible = OAuthChannel("ineligible@example.com", "ws-ineligible")
    monkeypatch.setattr(ineligible, "supports_model", lambda _model: None)
    configure_scheduler(monkeypatch, [eligible, ineligible])

    route = scheduler.schedule(
        body("cmp_one_eligible", "one-eligible-cipher"),
        "tenant", "192.0.2.20", ingress_protocol="responses",
    )
    assert not route
    assert route.guard_error.startswith("compaction_owner_unknown:")


def test_known_disabled_owner_is_unavailable_and_never_switches(monkeypatch):
    state_db.init()
    owner = OAuthChannel("disabled@example.com", "ws-disabled", enabled=False)
    other = OAuthChannel("other@example.com", "ws-other")
    request = body("cmp_disabled_owner", "disabled-cipher")
    compaction_owner.persist_observed(owner, request)
    configure_scheduler(monkeypatch, [owner, other])

    route = scheduler.schedule(request, "tenant", "192.0.2.21", ingress_protocol="responses")
    assert not route
    assert route.guard_error.startswith("compaction_owner_unavailable:")


def _scoped_compaction_request(owner, *, anchor="window-anchor"):
    owner_digest = compaction_owner.owner_identity(owner)
    logical = codex_identity.resolve_logical_session(
        owner_digest,
        codex_identity.scoped_digest("principal", "tenant"),
        codex_identity.scoped_digest("anchor:prompt-cache-key", anchor),
        durable=True,
    )
    identity = codex_identity.AccountIdentity(
        schema_version=1,
        id_generation_version=1,
        protocol_profile="rust-v0.153.4",
        owner_kind=codex_identity.OWNER_KIND,
        owner_digest=owner_digest,
        installation_id=str(uuid.uuid4()),
        created_at="2026-01-01T00:00:00Z",
    )
    context = codex_identity.RequestIdentityContext(
        identity, logical, codex_identity.TurnContext.new(logical),
    )
    request = {
        "model": "gpt-5.5",
        "input": [{"type": "message", "role": "user", "content": "compact"}],
        "_codex_identity_contexts": {owner_digest: context},
    }
    return request, context


def test_confirmed_compaction_advances_window_once_and_history_or_failure_does_not():
    state_db.init()
    owner = OAuthChannel("window@example.com", "ws-window")
    request, context = _scoped_compaction_request(owner)
    old_snapshot = context.snapshot()
    history_only = body("cmp_history_only", "history-cipher")
    history_only["_codex_identity_contexts"] = request["_codex_identity_contexts"]

    # A compaction in request history is not a successful compaction response.
    compaction_owner.persist_observed(owner, history_only, {"output": []})
    assert context.logical_session.window_number == 0
    compaction_owner.persist_observed(owner, request, {
        "error": {"type": "server_error"},
    })
    assert context.logical_session.window_number == 0

    response = {
        "model": "gpt-5.5",
        "output": [{
            "type": "compaction", "id": "cmp_confirmed",
            "encrypted_content": "confirmed-cipher",
        }],
    }
    compaction_owner.persist_observed(owner, request, response)
    assert context.logical_session.window_number == 1
    assert context.snapshot().window_id == f"{old_snapshot.thread_id}:1"
    assert context.snapshot().context_window_id != old_snapshot.context_window_id
    assert uuid.UUID(context.snapshot().context_window_id).version == 7

    compaction_owner.persist_observed(owner, request, response)
    assert context.logical_session.window_number == 1
    ref = compaction_owner.complete_refs(response)[0]
    marker = state_db.compaction_owner_load(ref.compaction_id, ref.content_digest)
    assert marker["window_advanced_from"] == 0
    assert marker["window_advanced_to"] == 1


def test_confirmed_compaction_concurrent_atomic_cas_advances_only_once():
    state_db.init()
    owner = OAuthChannel("window-race@example.com", "ws-window-race")
    request, context = _scoped_compaction_request(owner, anchor="race-anchor")
    response = {
        "model": "gpt-5.5",
        "output": [{
            "type": "compaction", "id": "cmp_race",
            "encrypted_content": "race-cipher",
        }],
    }

    def finalize(_):
        local_request = dict(request)
        local_context = codex_identity.RequestIdentityContext(
            context.account_identity,
            context.logical_session,
            codex_identity.TurnContext.new(context.logical_session),
        )
        local_request["_codex_identity_contexts"] = {
            compaction_owner.owner_identity(owner): local_context,
        }
        compaction_owner.persist_observed(owner, local_request, response)
        return local_context.logical_session.window_number

    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = list(pool.map(finalize, range(32)))
    row = state_db.codex_logical_session_load(
        context.logical_session.owner_digest,
        context.logical_session.downstream_principal_digest,
        context.logical_session.downstream_anchor_digest,
    )
    assert row["window_number"] == 1
    assert set(observed) == {1}


def test_compaction_response_model_mismatch_does_not_advance():
    state_db.init()
    owner = OAuthChannel("window-model@example.com", "ws-window-model")
    request, context = _scoped_compaction_request(owner, anchor="model-anchor")
    response = {
        "model": "wrong-model",
        "output": [{
            "type": "compaction", "id": "cmp_wrong_model",
            "encrypted_content": "wrong-model-cipher",
        }],
    }
    compaction_owner.persist_observed(owner, request, response)
    assert context.logical_session.window_number == 0


def test_persistence_failure_is_structured_warning_and_non_throwing(caplog, monkeypatch):
    owner = OAuthChannel("warn@example.com", "ws-warn")
    monkeypatch.setattr(
        state_db, "compaction_owner_upsert",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with caplog.at_level("WARNING", logger="src.openai.compaction_owner"):
        ok = compaction_owner.persist_observed_safe(
            owner, body("cmp_warn", "warn-cipher"), path="test_non_stream",
        )
    assert ok is False
    record = next(r for r in caplog.records if r.message == "codex_compaction_owner_persist_failed")
    assert record.event == "codex_compaction_owner_persist_failed"
    assert record.path == "test_non_stream"
    assert record.channel_key == owner.key


def test_failover_defense_rejects_cross_owner_and_compaction_strip_retry_is_disabled():
    request = body("cmp_defense", "defense-cipher")
    with pytest.raises(compaction_owner.CompactionRouteError) as exc:
        failover._attempt_body_for_channel(request, "oauth:other", "oauth:owner", {})
    assert exc.value.code == "compaction_owner_mismatch"
    assert request["input"][1]["encrypted_content"] == "defense-cipher"

    # The production invalid-EC gate is intentionally request-level and leaves
    # reasoning-only requests on the existing retry path.
    cfg = {"retry": {"recovery": {"invalidEncryptedContent": True}}}
    invalid = "invalid_encrypted_content: ciphertext could not be verified"
    assert failover._invalid_ec_cleanup_retry_allowed(request, invalid, cfg, False) is False
    reasoning_only = copy.deepcopy(request)
    reasoning_only["input"] = [reasoning_only["input"][0]]
    assert failover._invalid_ec_cleanup_retry_allowed(reasoning_only, invalid, cfg, False) is True
    assert failover._invalid_ec_cleanup_retry_allowed(reasoning_only, invalid, cfg, True) is False
