"""Cross-module coordination for live channel state mutations.

Scorer, cooldown, affinity, and registry cleanup all mirror channel-keyed state.
A single re-entrant lock keeps runtime channel renames from interleaving with
normal mutations or stale-channel cleanup.  Transition keys remain temporarily
live while config reload callbacks observe a rename in progress.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator


mutation_lock = threading.RLock()
_transition_keys: set[str] = set()
_aliases: dict[str, str] = {}
_deleted_keys: set[str] = set()
# API channels keep their public/logical key (``api:<name>``), while request
# attempts carry an opaque generation key.  Generation keys resolve to the
# logical key only while their generation is live; a deleted generation is
# tombstoned independently, so a same-name replacement cannot accept its late
# writes.
_generation_targets: dict[str, str] = {}
_legacy_api_generations: dict[str, str] = {}
_GENERATION_PREFIX = "api-generation:"


def register_api_generation(channel_key: str, generation_id: str | None) -> str:
    """Return a stable process-local attempt identity for one API config entry.

    New entries persist ``generationId``.  Legacy entries without it receive a
    stable identity for this process (ordinary registry rebuilds reuse it).
    """
    import uuid

    with mutation_lock:
        generation = str(generation_id or "").strip()
        if not generation:
            generation = _legacy_api_generations.setdefault(
                channel_key, uuid.uuid4().hex,
            )
        generation_key = f"{_GENERATION_PREFIX}{generation}"
        existing = _generation_targets.get(generation_key)
        if (
            existing is not None
            and existing != channel_key
            and resolve(existing) != channel_key
        ):
            raise ValueError(f"duplicate API channel generationId: {generation}")
        _generation_targets[generation_key] = channel_key
        return generation_key


def effect_key(channel) -> str:
    """Identity used by attempt-scoped health, affinity and concurrency writes."""
    return str(getattr(channel, "state_key", None) or channel.key)


def _direct_target(channel_key: str) -> str:
    return _generation_targets.get(channel_key, channel_key)


def generation_id(channel_key: str) -> str | None:
    """Extract the persisted opaque id from an API attempt identity."""
    if channel_key.startswith(_GENERATION_PREFIX):
        return channel_key[len(_GENERATION_PREFIX):] or None
    return None


@contextmanager
def transition(old_key: str, new_key: str) -> Iterator[None]:
    """Serialize one rename and protect both keys from reload cleanup."""
    with mutation_lock:
        keys = {key for key in (old_key, new_key) if key}
        _transition_keys.update(keys)
        try:
            yield
        finally:
            _transition_keys.difference_update(keys)


def include_transitions(live_keys: set[str]) -> set[str]:
    """Return a live-key snapshot including in-progress rename endpoints."""
    with mutation_lock:
        return set(live_keys) | set(_transition_keys)


def resolve(channel_key: str) -> str:
    """Map late writes from an in-flight old channel to its renamed key."""
    with mutation_lock:
        current = channel_key
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            targeted = _direct_target(current)
            if targeted != current:
                current = targeted
                continue
            if current in _aliases:
                current = _aliases[current]
                continue
            break
        return current


def alias_sources(channel_key: str) -> set[str]:
    """Return every retired source generation that resolves to channel_key."""
    with mutation_lock:
        target = resolve(channel_key)
        return {
            source for source in _aliases
            if resolve(source) == target
        }


def _install_alias(old_key: str, new_key: str) -> None:
    target = resolve(new_key)
    _aliases[old_key] = target
    for source, current in list(_aliases.items()):
        if current == old_key:
            _aliases[source] = target


def is_retired_source(channel_key: str) -> bool:
    with mutation_lock:
        return channel_key in _aliases or channel_key in _deleted_keys


def is_deleted(channel_key: str) -> bool:
    """Return whether a channel generation was deleted in this process."""
    with mutation_lock:
        return channel_key in _deleted_keys


def retire_deleted(channel_key: str) -> None:
    """Tombstone a deleted generation so late request side effects are dropped."""
    with mutation_lock:
        _deleted_keys.add(channel_key)


def restore_deleted(channel_key: str) -> None:
    """Undo a tombstone only when the matching config deletion did not commit."""
    with mutation_lock:
        _deleted_keys.discard(channel_key)


def assert_reusable(channel_key: str) -> None:
    if is_retired_source(channel_key):
        raise ValueError(
            f"channel key {channel_key!r} belongs to a retired generation; "
            "restart before reusing the key"
        )


def rename_with_config(*, old_channel_key: str, new_channel_key: str,
                       config_mutator: Callable[[dict], None],
                       rollback_mutator: Callable[[dict], None],
                       old_account_key: str | None = None,
                       new_account_key: str | None = None,
                       email: str | None = None) -> None:
    """Publish config + every persisted/in-memory channel mirror as one lifecycle.

    Config is written first while both rename endpoints are protected from the
    synchronous reload cleanup.  Routing and mirrored mutations share this
    lock, so no request can observe the short config/state transition.  A
    failed SQLite transaction restores the previous config before releasing
    the transition.
    """
    if old_channel_key == new_channel_key:
        from . import config
        config.update(config_mutator)
        return

    from . import affinity, concurrency, config, cooldown, scorer, state_db

    # Global order is config lifecycle → channel-state lifecycle. Normal
    # config.update callbacks take the same order, avoiding cross-thread
    # config/reload races and lock inversion.
    with config.serialized_updates(), transition(old_channel_key, new_channel_key):
        frozen_concurrency_max = concurrency.capture_rename_limit(old_channel_key)
        config.update(config_mutator)
        try:
            with state_db.optional_write_timeout():
                state_db.rename_runtime_channel_state(
                    old_channel_key,
                    new_channel_key,
                    old_account_key=old_account_key,
                    new_account_key=new_account_key,
                    email=email,
                )
        except BaseException:
            # The single SQLite transaction has already rolled back; restore
            # config before exposing routing again.
            config.update(rollback_mutator)
            raise

        # The state.db transaction above is the only persistence commit.
        # Install generation routing first, then attempt every deterministic
        # memory publication.  A broken publisher stays loud but cannot let
        # late old-generation writes recreate old state or lose concurrency
        # release accounting.
        _install_alias(old_channel_key, new_channel_key)
        concurrency.rename_channel(
            old_channel_key, new_channel_key,
            frozen_max=frozen_concurrency_max,
        )
        publication_errors: list[Exception] = []
        publishers = (
            lambda: scorer.rename_channel(
                old_channel_key, new_channel_key, persist=False,
            ),
            lambda: cooldown.rename_channel(
                old_channel_key, new_channel_key, persist=False,
            ),
            lambda: affinity.rename_channel(
                old_channel_key, new_channel_key, persist=False,
            ),
            lambda: affinity.client_rename_channel(
                old_channel_key, new_channel_key, persist=False,
            ),
        )
        for publish in publishers:
            try:
                publish()
            except Exception as exc:
                publication_errors.append(exc)
        if publication_errors:
            raise ExceptionGroup(
                "channel state committed but memory publication failed",
                publication_errors,
            )
