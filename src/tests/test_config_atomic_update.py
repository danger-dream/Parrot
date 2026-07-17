"""config.update 持久化故障下的原子可见性回归。"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config

    return {"config": config}


def _install_private_config(config, tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    old = copy.deepcopy(config.DEFAULT_CONFIG)
    old["atomicUpdateProbe"] = {"value": "old"}
    path.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", copy.deepcopy(old))
    monkeypatch.setattr(config, "_mtime", os.path.getmtime(path))
    monkeypatch.setattr(config, "_reload_callbacks", [])
    return path, old


@pytest.mark.parametrize("failure_phase", ["json_dump", "fsync", "replace"])
def test_update_write_failure_keeps_live_disk_and_callbacks_atomic(
    m, tmp_path, monkeypatch, failure_phase,
):
    config = m["config"]
    path, old = _install_private_config(config, tmp_path, monkeypatch)
    callback_values = []
    config.on_reload(lambda cfg: callback_values.append(cfg))
    held_old_cache = config.get()
    assert held_old_cache == old

    with monkeypatch.context() as fault:
        if failure_phase == "json_dump":
            def fail_dump(_data, fp, *_args, **_kwargs):
                fp.write('{"partial":')
                raise OSError("synthetic json dump failure")

            fault.setattr(config.json, "dump", fail_dump)
        elif failure_phase == "fsync":
            fault.setattr(
                config.os,
                "fsync",
                lambda _fd: (_ for _ in ()).throw(OSError("synthetic fsync failure")),
            )
        else:
            original_replace = config.os.replace

            def fail_live_replace(src, dst):
                if src == str(path) + ".tmp" and dst == str(path):
                    raise OSError("synthetic live replace failure")
                return original_replace(src, dst)

            fault.setattr(config.os, "replace", fail_live_replace)

        with pytest.raises(OSError, match="synthetic"):
            config.update(
                lambda cfg: cfg["atomicUpdateProbe"].__setitem__("value", "failed")
            )

    assert config._cache is held_old_cache
    assert config.get() is held_old_cache
    assert held_old_cache == old
    assert json.loads(path.read_text(encoding="utf-8")) == old
    assert callback_values == []

    result = config.update(
        lambda cfg: cfg["atomicUpdateProbe"].__setitem__("value", "committed")
    )
    assert result is config._cache
    assert result is not held_old_cache
    assert held_old_cache == old
    assert result["atomicUpdateProbe"]["value"] == "committed"
    assert json.loads(path.read_text(encoding="utf-8")) == result
    assert callback_values == [result]

    reloaded = config.reload()
    assert reloaded["atomicUpdateProbe"]["value"] == "committed"
    assert json.loads(path.read_text(encoding="utf-8")) == reloaded
