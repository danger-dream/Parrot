"""Opt-in lifecycle isolation for tests that persist temporary config overrides."""
from __future__ import annotations

import copy
from pathlib import Path
import shutil

import pytest


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Keep real config writes private and restore the exact incoming snapshot.

    Do not normalize or replace the caller's settings with test defaults.  A
    copied file preserves reload semantics; writes and backup rotation affect
    only this test.  Restore path/cache/mtime/reload bookkeeping as one scope.
    """
    from src import config

    path = tmp_path / "isolated-config.json"
    if Path(config.CONFIG_PATH).exists():
        shutil.copy2(config.CONFIG_PATH, path)
    monkeypatch.setattr(config, "CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "_cache", copy.deepcopy(config._cache))
    monkeypatch.setattr(config, "_mtime", config._mtime)
    monkeypatch.setattr(config, "_rejected_rewrite_version", None)
    monkeypatch.setattr(config, "_reload_callbacks", list(config._reload_callbacks))
