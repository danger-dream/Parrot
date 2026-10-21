"""Narrow copy-on-write transaction support for :mod:`src.state_store`.

Published StateStore domains are treated as immutable.  ``CowDomain`` gives a
mutation callback the historical mutable-mapping interface while detaching only
records the callback accesses.  Committing creates a new domain key map and
copies the touched records, so neither old publications nor callback-owned
objects can mutate the new publication afterwards.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Iterator, MutableMapping
from typing import Any


def _same(left: Any, right: Any) -> bool:
    """JSON-identical equality: ``1``, ``1.0`` and ``True`` encode differently."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


class CowDomain(MutableMapping[Any, Any]):
    """A lazy mutable view over one immutable published domain.

    Reading a record through the mutation view detaches that record before it is
    handed to the callback.  Top-level set/delete/clear operations are recorded
    without copying the base key map.  ``commit`` is the only operation that
    creates a new key map, and it deep-copies only records visible through the
    transaction's working set.
    """

    def __init__(self, base: dict[Any, Any]) -> None:
        self._base = base
        self._working: dict[Any, Any] = {}
        self._deleted: set[Any] = set()
        self._cleared = False
        self._key_operations: list[tuple[str, Any | None]] = []

    @property
    def touched(self) -> bool:
        return bool(self._working or self._deleted or self._cleared)

    def __getitem__(self, key: Any) -> Any:
        if key in self._working:
            return self._working[key]
        if self._cleared or key in self._deleted:
            raise KeyError(key)
        value = copy.deepcopy(self._base[key])
        self._working[key] = value
        return value

    def __setitem__(self, key: Any, value: Any) -> None:
        self._working[key] = value
        self._deleted.discard(key)
        self._key_operations.append(("set", key))

    def __delitem__(self, key: Any) -> None:
        if key not in self:
            raise KeyError(key)
        self._working.pop(key, None)
        self._deleted.add(key)
        self._key_operations.append(("delete", key))

    def __contains__(self, key: object) -> bool:
        if key in self._working:
            return True
        return not self._cleared and key not in self._deleted and key in self._base

    def _visible_keys(self) -> dict[Any, None]:
        keys = {} if self._cleared else dict.fromkeys(self._base)
        for operation, key in self._key_operations:
            if operation == "clear":
                keys.clear()
            elif operation == "delete":
                keys.pop(key, None)
            else:
                keys[key] = None
        return keys

    def __iter__(self) -> Iterator[Any]:
        return iter(self._visible_keys())

    def __len__(self) -> int:
        return len(self._visible_keys())

    def peek_items(self) -> list[tuple[Any, Any]]:
        """Visible records without detaching them; callers must not mutate values."""
        return [(key, self._working[key] if key in self._working else self._base[key])
                for key in self._visible_keys()]

    def clear(self) -> None:
        self._working.clear()
        self._deleted.clear()
        self._cleared = True
        self._key_operations.append(("clear", None))

    def copy(self) -> dict[Any, Any]:
        """Keep the callback-facing behavior of a normal ``dict.copy`` call."""
        return dict(self.items())

    @staticmethod
    def _validate_fragment(fragment: dict[Any, Any]) -> None:
        """Reject values that cannot ever be included in a durable snapshot.

        This intentionally encodes only records touched by this transaction.
        Complete payload encoding and checksum construction happen at flush for
        runtime state (and immediately for durable prepare-write-publish).
        """
        if not fragment:
            return
        encoded = json.dumps(
            fragment,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        # write_snapshot verifies a closed file by loading it and comparing the
        # decoded payload. Reject lossy JSON shapes (for example tuple values or
        # non-string mapping keys) now instead of creating a permanently dirty
        # runtime generation that can never pass that verification.
        if json.loads(encoded) != fragment:
            raise TypeError("state mutation is not JSON round-trip safe")

    def commit(self) -> dict[Any, Any]:
        """Return a detached immutable-publication candidate for this domain."""
        if not self.touched:
            return self._base

        visible_keys = self._visible_keys()
        # Reads detach records too; publishing an equal copy would only force a
        # durable rewrite of unchanged state.
        if visible_keys.keys() == self._base.keys() and all(
                _same(value, self._base[key]) for key, value in self._working.items()):
            return self._base
        committed = {
            key: copy.deepcopy(value)
            for key, value in self._working.items()
            if key in visible_keys
        }
        self._validate_fragment(committed)
        return {
            key: committed[key] if key in committed else self._base[key]
            for key in visible_keys
        }
