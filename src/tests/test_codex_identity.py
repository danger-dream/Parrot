"""Acceptance matrix for per-OAuth Codex installation/session/turn identity."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest


def _import_modules():
    from src import state_db
    from src.openai import codex_identity as identity
    from src.openai import codex_identity_mapper as mapper
    from src.state_store import StateStore
    return {"state_db": state_db, "identity": identity, "mapper": mapper, "StateStore": StateStore}


@pytest.fixture
def identity_store(tmp_path, monkeypatch, m):
    state_db = m["state_db"]
    original = state_db._store
    store = m["StateStore"](
        str(tmp_path / "runtime.json"),
        str(tmp_path / "durable.json"),
        manifest_path=str(tmp_path / "manifest.json"),
    )
    store.start()
    monkeypatch.setattr(state_db, "_store", store)
    m["identity"].clear_turn_mappings_for_tests()
    try:
        yield store, tmp_path
    finally:
        m["identity"].clear_turn_mappings_for_tests()
        store.close()
        monkeypatch.setattr(state_db, "_store", original)


def _account(workspace: str, email: str = "same@example.test", **extra):
    return {
        "provider": "openai",
        "email": email,
        "workspace_id": workspace,
        "chatgpt_account_id": workspace,
        **extra,
    }


def _version(value: str) -> int:
    return uuid.UUID(value).version


def test_account_identity_stable_across_relogin_profile_and_downstream(identity_store, m):
    identity = m["identity"]
    account = _account("workspace-a")
    assert identity.normalize_account_identity(account, protocol_profile="rust-v0.153.4")
    first = identity.account_identity_from_account(account)
    identity.register_account_identity(account)
    assert first is not None and _version(first.installation_id) == 4

    relogin = _account(
        "workspace-a",
        email="renamed@example.test",
        codexIdentity=copy.deepcopy(account["codexIdentity"]),
        codexDeviceInstallationId=account["codexDeviceInstallationId"],
    )
    assert not identity.normalize_account_identity(
        relogin,
        protocol_profile="future-profile",
        new_identity_generation_version=99,
    )
    second = identity.account_identity_from_account(relogin)
    assert second == first

    for principal, anchor in (("key-a", "session-a"), ("key-b", "session-b")):
        body = {
            "_api_key_name": principal,
            "prompt_cache_key": anchor,
            "_client_body_fields": ["prompt_cache_key"],
        }
        context = identity.resolve_request_identity_context(relogin, body)
        assert context.account_identity.installation_id == first.installation_id


def test_unsupported_generation_fails_only_when_creating_new_identity(identity_store, m):
    identity = m["identity"]
    with pytest.raises(ValueError, match="unsupported.*newIdentityGenerationVersion"):
        identity.normalize_account_identity(
            _account("workspace-new-generation"),
            new_identity_generation_version=99,
        )


def test_different_workspaces_never_share_installation_and_reject_copy(identity_store, m):
    identity = m["identity"]
    first = _account("workspace-a")
    second = _account("workspace-b")
    identity.normalize_account_identity(first)
    identity.normalize_account_identity(second)
    identity.register_account_identity(first)
    identity.register_account_identity(second)
    assert first["codexDeviceInstallationId"] != second["codexDeviceInstallationId"]

    copied = _account(
        "workspace-c",
        codexDeviceInstallationId=first["codexDeviceInstallationId"],
    )
    identity.normalize_account_identity(copied)
    with pytest.raises(ValueError, match="another owner"):
        identity.register_account_identity(copied)


def test_duplicate_canonical_owner_converges_or_rejects_conflict(identity_store, m):
    identity = m["identity"]
    accounts = [
        _account("workspace-shared", email="first@example.test"),
        _account("workspace-shared", email="second@example.test"),
    ]
    assert identity.normalize_account_identities(accounts)
    assert accounts[0]["codexIdentity"] == accounts[1]["codexIdentity"]

    conflicting = copy.deepcopy(accounts)
    conflicting[1]["codexIdentity"]["installationId"] = str(uuid.uuid4())
    conflicting[1]["codexDeviceInstallationId"] = conflicting[1]["codexIdentity"][
        "installationId"
    ]
    with pytest.raises(ValueError, match="duplicate canonical OpenAI owner"):
        identity.normalize_account_identities(conflicting)


def test_unknown_workspace_has_no_fallback_and_fails_closed(identity_store, m):
    identity = m["identity"]
    account = {"provider": "openai", "email": "legacy@example.test"}
    assert not identity.normalize_account_identity(account)
    assert "codexIdentity" not in account
    with pytest.raises(ValueError, match="unknown|requires"):
        identity.account_identity_from_account(account)


def test_logical_session_lifecycle_uuid_versions_and_window_advance(identity_store, m):
    identity = m["identity"]
    account = _account("workspace-a")
    identity.normalize_account_identity(account)
    identity.register_account_identity(account)

    def resolve(principal: str, anchor: str):
        body = {
            "_api_key_name": principal,
            "prompt_cache_key": anchor,
            "_client_body_fields": ["prompt_cache_key"],
        }
        return identity.resolve_request_identity_context(account, body)

    u1 = resolve("key-a", "anchor-a")
    u1_retry = resolve("key-a", "anchor-a")
    u2 = resolve("key-b", "anchor-a")
    assert u1.logical_session.session_id == u1_retry.logical_session.session_id
    assert u1.logical_session.session_id != u2.logical_session.session_id
    assert u1.logical_session.session_id == u1.logical_session.root_thread_id
    assert u1.logical_session.upstream_prompt_cache_key == u1.logical_session.session_id
    assert _version(u1.logical_session.session_id) == 7
    assert _version(u1.logical_session.context_window_id) == 7
    assert _version(u1.turn.turn_id) == 7

    turn2 = identity.next_turn_context(u1)
    assert turn2.logical_session.session_id == u1.logical_session.session_id
    assert turn2.turn.turn_id != u1.turn.turn_id
    advanced = identity.advance_context_window(
        u1.logical_session, expected_window_number=0
    )
    assert advanced.window_number == 1
    assert advanced.context_window_id != u1.logical_session.context_window_id
    with pytest.raises(ValueError, match="CAS conflict"):
        identity.advance_context_window(
            u1.logical_session, expected_window_number=0
        )


def test_atomic_100_concurrent_first_create_has_one_uuid(identity_store, m):
    identity = m["identity"]
    owner = identity.owner_digest_for_workspace("workspace-concurrent")
    principal = identity.scoped_digest("principal", "key")
    anchor = identity.scoped_digest("anchor:prompt-cache-key", "anchor")

    def resolve(_):
        return identity.resolve_logical_session(
            owner, principal, anchor, durable=True
        ).session_id

    with ThreadPoolExecutor(max_workers=20) as pool:
        observed = list(pool.map(resolve, range(100)))
    assert len(set(observed)) == 1
    rows = [
        row for row in m["state_db"].codex_logical_session_load_all()
        if row["owner_digest"] == owner
    ]
    assert len(rows) == 1


def test_snapshot_http_ws_parity_no_raw_id_leak_and_official_shapes(identity_store, m):
    identity = m["identity"]
    account = _account("workspace-a")
    identity.normalize_account_identity(account)
    identity.register_account_identity(account)
    raw_key = "downstream-key-name"
    raw_anchor = "downstream-session-id"
    raw_installation = "downstream-installation-id"
    body = {
        "_api_key_name": raw_key,
        "prompt_cache_key": raw_anchor,
        "_client_body_fields": ["prompt_cache_key"],
        "client_metadata": {
            "x-codex-installation-id": raw_installation,
            "session_id": raw_anchor,
        },
    }
    context = identity.resolve_request_identity_context(account, body)
    snapshot = context.snapshot()
    http_headers, http_body = identity.project_snapshot(snapshot, {}, body)
    ws_headers, ws_frame = identity.project_snapshot(snapshot, {}, body)
    assert http_headers == ws_headers
    assert http_body == ws_frame
    assert "x-codex-installation-id" not in http_headers
    assert http_headers["session-id"] == snapshot.session_id
    assert http_headers["thread-id"] == snapshot.thread_id
    assert http_headers["x-client-request-id"] == snapshot.thread_id
    assert http_headers["x-codex-window-id"] == f"{snapshot.thread_id}:0"
    assert "session_id" not in http_headers
    metadata = json.loads(http_headers["x-codex-turn-metadata"])
    nested = json.loads(http_body["client_metadata"]["x-codex-turn-metadata"])
    assert metadata == nested == snapshot.turn_metadata()
    assert "prompt_cache_key" not in metadata
    wire = json.dumps({"headers": http_headers, "body": http_body})
    for raw in (raw_key, raw_anchor, raw_installation):
        assert raw not in wire
    for value in (
        snapshot.session_id,
        snapshot.thread_id,
        snapshot.turn_id,
        snapshot.context_window_id,
    ):
        assert _version(value) == 7


def test_retry_turn_state_scope_and_failover_owner_isolation(identity_store, m):
    identity = m["identity"]
    a = _account("workspace-a")
    b = _account("workspace-b")
    identity.normalize_account_identity(a)
    identity.normalize_account_identity(b)
    shared_body = {
        "_api_key_name": "key",
        "prompt_cache_key": "same-anchor",
        "_client_body_fields": ["prompt_cache_key"],
    }
    ctx_a = identity.resolve_request_identity_context(a, shared_body)
    first_a = ctx_a.snapshot()
    assert ctx_a.turn.capture_turn_state(
        "opaque-a", owner_digest=first_a.owner_digest, turn_id=first_a.turn_id
    )
    retry_a = ctx_a.snapshot()
    assert retry_a.turn_id == first_a.turn_id
    assert retry_a.turn_state == "opaque-a"
    assert not ctx_a.turn.capture_turn_state(
        "wrong", owner_digest=identity.owner_digest_for_workspace("workspace-b"),
        turn_id=first_a.turn_id,
    )

    ctx_b = identity.resolve_request_identity_context(b, shared_body)
    first_b = ctx_b.snapshot()
    assert first_b.owner_digest != first_a.owner_digest
    assert first_b.installation_id != first_a.installation_id
    assert first_b.session_id != first_a.session_id
    assert first_b.turn_state is None
    next_a = identity.next_turn_context(ctx_a).snapshot()
    assert next_a.turn_id != first_a.turn_id
    assert next_a.turn_state is None


def test_explicit_turn_mapping_continuation_new_turn_ttl_and_owner_isolation(
    identity_store, m,
):
    identity = m["identity"]
    first_account = _account("workspace-turn-a")
    second_account = _account("workspace-turn-b")
    identity.normalize_account_identity(first_account)
    identity.normalize_account_identity(second_account)

    http_body = {
        "_api_key_name": "key",
        "client_metadata": {"session_id": "native-session", "turn_id": "native-turn"},
    }
    first = identity.resolve_request_identity_context(first_account, http_body)
    ws_body = {
        "_api_key_name": "key",
        "_codex_native_identity": {
            "client_metadata": {
                "session_id": "native-session",
                "x-codex-turn-metadata": json.dumps({"turn_id": "native-turn"}),
            },
            "headers": {},
        },
    }
    continuation = identity.resolve_request_identity_context(first_account, ws_body)
    assert continuation.logical_session.session_id == first.logical_session.session_id
    assert continuation.turn is first.turn
    assert continuation.turn.turn_id != "native-turn"
    assert _version(continuation.turn.turn_id) == 7

    first.turn.capture_turn_state(
        "sticky", owner_digest=first.account_identity.owner_digest,
        turn_id=first.turn.turn_id,
    )
    new_turn_body = {
        "_api_key_name": "key",
        "client_metadata": {"session_id": "native-session", "turn_id": "native-turn-2"},
    }
    new_turn = identity.resolve_request_identity_context(first_account, new_turn_body)
    assert new_turn.turn.turn_id != first.turn.turn_id
    assert new_turn.turn.turn_state is None

    other_owner = identity.resolve_request_identity_context(second_account, http_body.copy())
    assert other_owner.turn.turn_id != first.turn.turn_id
    assert other_owner.turn.turn_state is None

    identity.clear_turn_mappings_for_tests()
    logical = first.logical_session
    ttl_payload = {"client_metadata": {"turn_id": "ttl-turn"}}
    before_expiry = identity.resolve_turn_context(logical, ttl_payload, now_monotonic=10)
    same = identity.resolve_turn_context(logical, ttl_payload, now_monotonic=11)
    expired = identity.resolve_turn_context(
        logical, ttl_payload,
        now_monotonic=11 + identity.TURN_MAPPING_TTL_SECONDS,
    )
    assert same is before_expiry
    assert expired.turn_id != before_expiry.turn_id


@pytest.mark.asyncio
async def test_thread_turn_serialization_parallelism_cancellation_and_reclaim(
    identity_store, m,
):
    identity = m["identity"]
    account = _account("workspace-queue")
    identity.normalize_account_identity(account)
    first = identity.resolve_request_identity_context(account, {
        "_api_key_name": "key", "prompt_cache_key": "thread-a",
        "_client_body_fields": ["prompt_cache_key"],
    })
    same_thread = identity.next_turn_context(first)
    other_thread = identity.resolve_request_identity_context(account, {
        "_api_key_name": "key", "prompt_cache_key": "thread-b",
        "_client_body_fields": ["prompt_cache_key"],
    })

    first_body: dict = {}
    first_lease = await identity.acquire_request_turn_serialization(first_body, first)
    waiter_body: dict = {}
    waiter = asyncio.create_task(
        identity.acquire_request_turn_serialization(waiter_body, same_thread)
    )
    await asyncio.sleep(0)
    assert not waiter.done()

    other_body: dict = {}
    other_lease = await asyncio.wait_for(
        identity.acquire_request_turn_serialization(other_body, other_thread),
        timeout=0.2,
    )
    other_lease.release()
    identity.release_request_turn_serialization(other_body)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    first_lease.release()
    identity.release_request_turn_serialization(first_body)

    final_body: dict = {}
    final_lease = await asyncio.wait_for(
        identity.acquire_request_turn_serialization(final_body, same_thread),
        timeout=0.2,
    )
    final_lease.release()
    identity.release_request_turn_serialization(final_body)
    assert identity.active_thread_turn_queue_count() == 0


def test_event_level_turn_state_is_exact_turn_and_owner_scoped(identity_store, m):
    identity = m["identity"]
    account = _account("workspace-event-state")
    identity.normalize_account_identity(account)
    context = identity.resolve_request_identity_context(account, {
        "_api_key_name": "key", "client_metadata": {
            "session_id": "session", "turn_id": "turn",
        },
    })
    translator_ctx = {
        "codex_identity_context": context,
        "codex_identity_snapshot": context.snapshot(),
    }
    assert identity.capture_turn_state_event(translator_ctx, json.dumps({
        "type": "response.metadata",
        "headers": {"X-Codex-Turn-State": "event-token"},
    }))
    assert context.snapshot().turn_state == "event-token"
    assert not identity.capture_turn_state_event(translator_ctx, {
        "type": "codex.response.metadata",
        "headers": {"x-codex-turn-state": "foreign"},
    })
    next_context = identity.next_turn_context(context)
    assert next_context.snapshot().turn_state is None


def test_tombstone_restart_reimport_delete_and_explicit_forget(identity_store, monkeypatch, m):
    store, tmp_path = identity_store
    identity = m["identity"]
    state_db = m["state_db"]
    account = _account("workspace-life")
    identity.normalize_account_identity(account)
    first = identity.register_account_identity(account)
    body = {
        "_api_key_name": "key",
        "prompt_cache_key": "anchor",
        "_client_body_fields": ["prompt_cache_key"],
    }
    identity.resolve_request_identity_context(account, body)
    assert state_db.codex_logical_session_load_all()

    # Ordinary credential deletion semantics: clear session state, retain tombstone.
    state_db.codex_logical_session_delete_owner(first.owner_digest)
    assert not state_db.codex_logical_session_load_all()
    assert state_db.codex_identity_tombstone_load(first.owner_digest)

    # Reimport with credentials only restores the same UUIDv4 from the tombstone.
    imported = _account("workspace-life", email="new-label@example.test")
    identity.normalize_account_identity(imported)
    assert imported["codexDeviceInstallationId"] == first.installation_id

    # A complete durable-state copy preserves the owner installation across restart.
    store.close()
    copied = tmp_path / "copied-durable.json"
    shutil.copy2(tmp_path / "durable.json", copied)
    restarted = m["StateStore"](
        str(tmp_path / "runtime-restart.json"),
        str(copied),
        manifest_path=str(tmp_path / "manifest-restart.json"),
    )
    restarted.start()
    monkeypatch.setattr(state_db, "_store", restarted)
    try:
        assert state_db.codex_identity_tombstone_load(first.owner_digest)["installation_id"] == first.installation_id
        assert identity.forget_owner_identity(first.owner_digest)
        forgotten = _account("workspace-life")
        identity.normalize_account_identity(forgotten)
        assert forgotten["codexDeviceInstallationId"] != first.installation_id
        assert _version(forgotten["codexDeviceInstallationId"]) == 4
    finally:
        restarted.close()
        # Keep fixture finalizer idempotent after its original store was closed.
        monkeypatch.setattr(state_db, "_store", store)


def test_structured_response_mapper_never_rewrites_plain_text_uuid(m):
    mapper = m["mapper"]
    state = mapper.ProtocolIdentityMap()
    state.register("downstream-turn", "018f47c2-1234-7abc-8def-123456789abc")
    upstream = "018f47c2-1234-7abc-8def-123456789abc"
    payload = json.dumps({
        "turn_id": upstream,
        "output_text": f"ordinary text contains {upstream}",
        "nested": {"content": upstream},
    }).encode()
    mapped = json.loads(mapper.expose_response_payload(payload, state))
    assert mapped["turn_id"] == "downstream-turn"
    assert mapped["output_text"].endswith(upstream)
    assert mapped["nested"]["content"] == upstream
