import json
import os
from pathlib import Path

import pytest

from src import config, state_db
from src.state_store import StateStore


def _seed_runtime(tmp_path):
    runtime=str(tmp_path/"runtime-cache.json");durable=str(tmp_path/"durable-state.json")
    store=StateStore(runtime,durable);store.start()
    store._mutate("performance_stats",lambda d:d.__setitem__("healthy",{"v":1}));store.flush("runtime",strict=True)
    return store,runtime,durable


@pytest.mark.parametrize("fault", ["fsync", "closed_verify"])
def test_atomic_fault_before_replace_preserves_last_healthy(tmp_path, monkeypatch, fault):
    store,runtime,_=_seed_runtime(tmp_path)
    store._mutate("performance_stats",lambda d:d.__setitem__("new",{"v":2}))
    if fault=="fsync":
        monkeypatch.setattr(os,"fsync",lambda _fd: (_ for _ in ()).throw(OSError("fsync fault")))
    else:
        real=StateStore.read_snapshot
        def fail_temp(path,kind):
            if str(path).endswith(".tmp"):raise OSError("read-after-close fault")
            return real(path,kind)
        monkeypatch.setattr(StateStore,"read_snapshot",fail_temp)
    assert store.flush("runtime",strict=False) is False
    generation,payload=StateStore.read_snapshot(runtime,"runtime")
    assert payload["performance_stats"]=={"healthy":{"v":1}}


@pytest.mark.parametrize("field,value", [("schema","wrong"),("version",999),("checksum","0"*64)])
def test_schema_version_checksum_damage_uses_verified_backup(tmp_path, field, value):
    store,runtime,_=_seed_runtime(tmp_path)
    store._mutate("performance_stats",lambda d:d.__setitem__("second",{"v":2}));store.flush("runtime",strict=True)
    store.close()
    obj=json.loads(Path(runtime).read_text());obj[field]=value;Path(runtime).write_text(json.dumps(obj))
    restored=StateStore(runtime,str(tmp_path/"durable-state.json"));restored.start()
    assert restored.get("performance_stats","healthy")=={"v":1}
    assert restored.get("performance_stats","second") is None


def test_normal_start_does_not_create_legacy_state_db(tmp_path, monkeypatch):
    cfg={"stateDbPath":"state.db","runtimeStatePath":"runtime-cache.json","durableStatePath":"durable-state.json"}
    state_db.close()
    with monkeypatch.context() as patch:
        patch.setattr(config,"DATA_DIR",str(tmp_path));patch.setattr(config,"get",lambda:cfg)
        state_db.init();state_db.close()
    assert not (tmp_path/"state.db").exists()
    assert (tmp_path/"runtime-cache.json").exists()
    assert (tmp_path/"durable-state.json").exists()
    state_db.init()
