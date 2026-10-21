"""Callback payloads kept beyond the process, so buttons on sent messages survive a restart.

Telegram callback_data holds 64 bytes, so menus send a short key and keep the
payload server side. Storing is best effort: without a started state store
(tests, CLI), or for values the codec cannot represent, a payload stays in
memory only and its buttons expire on restart as before.
"""
from __future__ import annotations

import importlib
import json
import math
import threading
import time
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

CODES = "tg_callback_codes"
ACTIONS = "tg_frozen_actions"
CODE_LIMIT = 5000
ACTION_LIMIT = 2000
ACTION_MAX_BYTES = 65536
PRUNE_EVERY_SECONDS = 300
PRUNE_EVERY_WRITES = 500
_prune_lock = threading.Lock()
_since_prune: dict[str, tuple[float, int]] = {}
# Only the bot's own value types are rebuilt from storage.
_TYPE_PACKAGES = ("src.management_control.", "src.telegram.menus.model_center")


def _type_name(cls: type) -> str:
    if not cls.__module__.startswith(_TYPE_PACKAGES):
        raise TypeError(f"unsupported type {cls.__module__}.{cls.__qualname__}")
    return f"{cls.__module__}:{cls.__qualname__}"


def _type(name: str) -> type:
    module, _, qualname = name.partition(":")
    if not module.startswith(_TYPE_PACKAGES):
        raise TypeError(f"unsupported type {name}")
    value: Any = importlib.import_module(module)
    for part in qualname.split("."):
        value = getattr(value, part)
    return value


def encode(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("non-finite float")
        return value
    if isinstance(value, Enum):
        return {"__enum__": _type_name(type(value)), "v": encode(value.value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {"__dc__": _type_name(type(value)),
                "f": {item.name: encode(getattr(value, item.name)) for item in fields(value) if item.init}}
    if isinstance(value, Decimal):
        return {"__dec__": str(value)}
    if isinstance(value, tuple):
        return {"__tuple__": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) and not key.startswith("__") for key in value):
            return {key: encode(item) for key, item in value.items()}
        return {"__map__": [[encode(key), encode(item)] for key, item in value.items()]}
    raise TypeError(f"unsupported type {type(value).__name__}")


def decode(value: Any) -> Any:
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__enum__" in value:
        return _type(value["__enum__"])(decode(value["v"]))
    if "__dc__" in value:
        cls = _type(value["__dc__"])
        if not is_dataclass(cls):
            raise TypeError(f"{value['__dc__']} is not a dataclass")
        return cls(**{key: decode(item) for key, item in value["f"].items()})
    if "__dec__" in value:
        return Decimal(value["__dec__"])
    if "__tuple__" in value:
        return tuple(decode(item) for item in value["__tuple__"])
    if "__map__" in value:
        return {decode(key): decode(item) for key, item in value["__map__"]}
    return {key: decode(item) for key, item in value.items()}


def save_code(short: str, name: str) -> None:
    _save(CODES, short, {"name": name, "at": time.time()}, CODE_LIMIT)


def load_code(short: str) -> str | None:
    row = _load(CODES, short)
    return row.get("name") if isinstance(row, dict) and isinstance(row.get("name"), str) else None


def save_action(token: str, chat_id: int, name: str, data: Any, created: float, ttl: float) -> None:
    try:
        encoded = encode(data)
        if len(json.dumps(encoded, ensure_ascii=False, allow_nan=False)) > ACTION_MAX_BYTES:
            return
    except (TypeError, ValueError):
        return
    _save(ACTIONS, token, {"chat": int(chat_id), "name": name, "data": encoded, "at": created}, ACTION_LIMIT, ttl)


def load_action(token: str) -> tuple[int, str, Any, float] | None:
    row = _load(ACTIONS, token)
    if not isinstance(row, dict):
        return None
    try:
        return int(row["chat"]), str(row["name"]), decode(row["data"]), float(row["at"])
    except (KeyError, TypeError, ValueError, AttributeError, ImportError):
        return None


def _save(domain: str, key: str, row: dict, limit: int, ttl: float | None = None) -> None:
    from .. import state_db
    now = time.time()
    with _prune_lock:
        last, writes = _since_prune.get(domain, (0.0, 0))
        prune = now - last >= PRUNE_EVERY_SECONDS or writes + 1 >= PRUNE_EVERY_WRITES
        _since_prune[domain] = (now, 0) if prune else (last, writes + 1)
    try:
        state_db.tg_callback_save(domain, key, row)
        if prune:
            state_db.tg_callback_prune(domain, limit, now - ttl if ttl is not None else None)
    except Exception as exc:
        if "not started" not in str(exc):
            print(f"[telegram] callback payload not stored ({domain}): {type(exc).__name__}: {exc}")


def _load(domain: str, key: str) -> Any:
    from .. import state_db
    try:
        return state_db.tg_callback_load(domain, key)
    except Exception:
        return None
