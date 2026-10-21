"""Durable snapshot writes stay proportional to real state changes."""
import json

from src import state_db
from src.notifications import emit, store
from src.state_store import StateStore
from src.tests.test_notifications import env, event  # noqa: F401  (fixture)


def _store(tmp_path):
    db = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"))
    db.start()
    return db


def _writes(monkeypatch, db):
    calls = []
    real = db.write_snapshot
    def counted(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)
    monkeypatch.setattr(db, "write_snapshot", counted)
    return calls


def test_encoded_snapshot_is_byte_identical_to_full_dump(tmp_path):
    db = _store(tmp_path)
    db._mutate("xai_video_jobs", lambda d: d.update({"b": {"名称": "视频\n", "n": 1.5, "flag": True},
                                                     "a": {"list": [1, None, "x"]}}))
    payload = db._candidate_for("durable")
    legacy = (json.dumps(StateStore._envelope("durable", 7, payload), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False) + "\n").encode()
    assert StateStore._envelope_bytes("durable", 7, db._encode("durable", payload)) == legacy
    db.close()


def test_unchanged_mutations_do_not_write_or_advance_generation(tmp_path, monkeypatch):
    db = _store(tmp_path)
    db._mutate("xai_video_jobs", lambda d: d.__setitem__("j", {"v": 1}))
    writes = _writes(monkeypatch, db)
    before = db.health()["generation"]
    assert db._mutate("xai_video_jobs", lambda d: d.get("j")) == {"v": 1}
    db._mutate("xai_video_jobs", lambda d: d.__setitem__("j", {"v": 1}))
    db._mutate_many(("xai_video_jobs", "zhipu_actions"), lambda t: dict(t["xai_video_jobs"]))
    assert writes == [] and db.health()["generation"] == before
    # JSON-visible type changes are real changes even when Python compares equal.
    db._mutate("xai_video_jobs", lambda d: d.__setitem__("j", {"v": True}))
    assert writes == ["durable"] and db.get("xai_video_jobs", "j") == {"v": True}
    db.close()


def test_write_reencodes_only_touched_domains_and_trusts_its_own_main(tmp_path, monkeypatch):
    db = _store(tmp_path)
    db._mutate("xai_video_jobs", lambda d: d.__setitem__("big", {"v": "x" * 1000}))
    db._mutate("zhipu_actions", lambda d: d.__setitem__("a", {"v": 1}))
    encoded, decoded = [], []
    real_bytes, real_validate = StateStore._payload_bytes, StateStore._validate_obj
    monkeypatch.setattr(StateStore, "_payload_bytes", staticmethod(lambda p: encoded.append(p) or real_bytes(p)))
    monkeypatch.setattr(StateStore, "_validate_obj",
                        classmethod(lambda cls, obj, kind: decoded.append(kind) or real_validate(obj, kind)))
    db._mutate("zhipu_actions", lambda d: d.__setitem__("a", {"v": 2}))
    assert encoded == [{"a": {"v": 2}}] and decoded == []
    db.close()
    monkeypatch.undo()
    restored = _store(tmp_path)
    assert restored.get("xai_video_jobs", "big") == {"v": "x" * 1000}
    assert restored.get("zhipu_actions", "a") == {"v": 2}
    restored.close()


def test_open_incident_repeats_that_notify_nobody_are_not_persisted(env, monkeypatch):
    cfg, db, queue, _ = env
    cfg["notifications"]["events"]["quota_disabled"] = False
    writes = _writes(monkeypatch, db)
    assert not emit(event())
    assert len(writes) == 1  # the episode opening is kept for recovery bookkeeping
    for _ in range(5):
        assert not emit(event())
    assert len(writes) == 1 and len(store.items("events")) == 1
    # A switched-off episode is not consumed: enabling it still notifies once.
    cfg["notifications"]["events"]["quota_disabled"] = True
    assert emit(event()) and len(queue) == 2
    for _ in range(5):
        assert not emit(event())
    assert len(writes) == 2 and len(queue) == 2
    emit(event("account.quota.recovered"))
    assert len(writes) == 3
    assert next(iter(store.items("incidents").values()))["active"] is False


def test_capture_versions_need_no_write_and_stay_ordered_after_restart(env, tmp_path, monkeypatch):
    _, db, _, _ = env
    subject = event().subject
    writes = _writes(monkeypatch, db)
    versions = [store.next_version(subject) for _ in range(3)]
    assert writes == [] and versions == sorted(set(versions))
    emit(event())
    recorded = next(iter(store.items("incidents").values()))["version"]
    db.close()
    restarted = StateStore(str(tmp_path / "runtime.json"), str(tmp_path / "durable.json"))
    restarted.start()
    monkeypatch.setattr(state_db, "_store", restarted)
    assert store.next_version(subject) > recorded
    restarted.close()


def test_codex_usage_stamps_do_not_rewrite_durable_state_per_request(tmp_path, monkeypatch):
    db = _store(tmp_path)
    monkeypatch.setattr(state_db, "_store", db)
    digest = lambda char: "sha256:" + char * 64
    candidate = {"owner_digest": digest("a"), "downstream_principal_digest": digest("b"),
                 "downstream_anchor_digest": digest("c"), "session_id": "s", "window_number": 0}
    writes = _writes(monkeypatch, db)
    first = state_db.codex_logical_session_resolve(candidate)
    assert state_db.codex_logical_session_resolve(candidate) == first and len(writes) == 1
    state_db.compaction_owner_upsert("cmp", "content", "channel", "owner")
    state_db.compaction_owner_upsert("cmp", "content", "channel", "owner")
    assert len(writes) == 2
    state_db.compaction_owner_upsert("cmp", "content", "other-channel", "owner")
    assert len(writes) == 3
    later = first["last_used_at"] + state_db._TOUCH_INTERVAL_MS
    monkeypatch.setattr(state_db, "now_ms", lambda: later)
    assert state_db.codex_logical_session_resolve(candidate)["last_used_at"] == later and len(writes) == 4
    db.close()
