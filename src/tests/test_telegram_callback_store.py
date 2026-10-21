"""Telegram buttons keep working across a restart: payloads are stored and rebuilt."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

import pytest

from src.tests import _isolation

_isolation.isolate()

from src import state_db  # noqa: E402
from src.management_control.models import (  # noqa: E402
    ModelFilters,
    ModelKind,
    ModelSelection,
    ModelSelectionMode,
    ModelSourceRef,
    ModelSourceType,
    ModelStateField,
    ModelStateTarget,
    ModelStatus,
)
from src.telegram import callback_store, ui  # noqa: E402
from src.telegram.menus import model_center_menu as menu  # noqa: E402


@pytest.fixture
def store(request):
    state_db.init()
    request.addfinalizer(state_db.close)


def _restart():
    """Flush and reopen the store from disk, and forget every in-memory payload."""
    state_db.close()
    state_db.init()
    menu.reset_for_tests()
    with ui._code_lock:
        ui._code_to_name.clear()


def test_codec_round_trips_the_menu_value_types_and_refuses_anything_else():
    value = {
        "selection": ModelSelection(mode=ModelSelectionMode.FILTER, model_ids=("a", "b"),
                                    filters=ModelFilters(kinds=(ModelKind.CHAT,), statuses=(ModelStatus.ENABLED,))),
        "scope": ModelSourceRef(ModelSourceType.OAUTH, "workbuddy:cn:x"),
        "target": ModelStateTarget(ModelStateField.ENABLED, False),
        "price": Decimal("0.125"), "pages": ("one", "two"), "keyed": {1: "x"}, "plain": [1, 2.5, None, True],
    }
    assert callback_store.decode(callback_store.encode(value)) == value
    session = menu._session(42)
    session.source, session.selected = ModelSourceRef(ModelSourceType.API, "api:DeepSeek"), ["glm-5.3"]
    back = menu._list_context(session)
    assert callback_store.decode(callback_store.encode({"back": back})) == {"back": back}
    menu.reset_for_tests()

    @dataclass
    class Foreign:
        x: int

    with pytest.raises(TypeError):
        callback_store.encode({"x": Foreign(1)})
    with pytest.raises(TypeError):
        callback_store.encode({"x": math.nan})
    with pytest.raises(TypeError):
        callback_store.decode({"__dc__": "os:PathLike", "f": {}})


def test_model_center_buttons_survive_a_restart(store):
    target = ModelStateTarget(ModelStateField.VISIBLE, True)
    callback = menu._freeze(42, "set_state", target=target, revision="r1", selection=ModelSelection(
        mode=ModelSelectionMode.IDS, model_ids=("glm-5.3",)))
    token = callback.rsplit(":", 1)[1]
    _restart()
    action = menu._thaw(42, token)
    assert action is not None and action.name == "set_state"
    assert action.data["target"] == target and action.data["selection"].model_ids == ("glm-5.3",)
    assert menu._thaw(7, token) is None


def test_unstorable_or_expired_buttons_still_expire(store, monkeypatch):
    memory_only = menu._freeze(42, "detail", resource_key=object()).rsplit(":", 1)[1]
    old = menu._freeze(42, "detail", resource_key="k").rsplit(":", 1)[1]
    _restart()
    assert menu._thaw(42, memory_only) is None
    later = menu.time.time() + menu._ACTION_TTL + 1
    monkeypatch.setattr(menu.time, "time", lambda: later)
    assert menu._thaw(42, old) is None


def test_short_codes_survive_a_restart(store):
    short = ui.register_code("oauth:workbuddy:cn:account-1")
    _restart()
    assert ui.resolve_code(short) == "oauth:workbuddy:cn:account-1"
    assert ui.resolve_code("00000000") is None


def test_stored_payloads_stay_bounded(store, monkeypatch):
    state_db._mut("tg_frozen_actions", lambda data: data.clear())
    now = menu.time.time()
    for index in range(30):
        state_db.tg_callback_save("tg_frozen_actions", f"t{index}", {"at": now - 4000 + index})
    state_db.tg_callback_prune("tg_frozen_actions", 20)
    assert sorted(row["at"] for row in state_db._all("tg_frozen_actions")) == [now - 4000 + i for i in range(10, 30)]
    state_db.tg_callback_save("tg_frozen_actions", "fresh", {"at": now})
    state_db.tg_callback_prune("tg_frozen_actions", 20, now - 1800)
    assert [row["at"] for row in state_db._all("tg_frozen_actions")] == [now]
    # Saving prunes on a schedule, not on every write.
    monkeypatch.setattr(callback_store, "_since_prune", {})
    calls = []
    monkeypatch.setattr(state_db, "tg_callback_prune", lambda *args: calls.append(args))
    for index in range(callback_store.PRUNE_EVERY_WRITES + 1):
        callback_store.save_code(f"c{index}", "name")
    assert len(calls) == 2


def test_page_turn_buttons_stay_in_memory_only(store):
    token = menu._freeze(42, "content_page", pages=("one", "two"), keyboard={}, page=1).rsplit(":", 1)[1]
    assert menu._thaw(42, token) is not None
    _restart()
    assert menu._thaw(42, token) is None
