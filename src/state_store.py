"""Unified in-memory state with verified, atomic JSON snapshots.

Runtime mutations publish domain/record copy-on-write state and defer complete
payload encoding to the debounced flush. Durable mutations use
prepare-write-publish: memory is published only after the candidate snapshot is
closed, read-verified, installed and directory-synced.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterable

from .state_cow import CowDomain

SCHEMA = "parrot-state"
VERSION = 1
RUNTIME_DOMAINS = (
    "performance_stats", "channel_errors", "cache_affinities",
    "client_affinities", "schema_meta", "oauth_quota_cache",
    "api_provider_usage_cache", "network_check_status", "tg_callback_codes", "tg_frozen_actions",
)
DURABLE_DOMAINS = (
    "xai_video_jobs", "codex_compaction_owners", "codex_identity_tombstones",
    "codex_logical_sessions", "app_self_update", "app_update_state",
    "status_seen_updates", "status_muted_incidents", "workbuddy_actions",
    "model_reroute_mutes", "claude_reset_operations", "zhipu_actions", "qoder_checkins", "qoder_identities", "qoder_refresh", "model_registry",
    "notification_events", "notification_incidents", "notification_deliveries",
    "notification_refs", "notification_policy",
)


class SnapshotError(RuntimeError):
    pass


def _canonical(path: str) -> str:
    absolute = os.path.abspath(path)
    parent = os.path.realpath(os.path.dirname(absolute) or ".")
    return os.path.normcase(os.path.join(parent, os.path.basename(absolute)))


def validate_distinct_paths(named_paths: dict[str, str]) -> None:
    """Reject every unsafe alias before any StateStore artifact is created."""
    canonical: dict[str, str] = {}
    existing: dict[str, os.stat_result] = {}
    for name, supplied in named_paths.items():
        path = os.path.abspath(supplied)
        canonical[name] = _canonical(path)
        if os.path.lexists(path):
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"{name} path must not be a symlink: {path}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"{name} path is not a regular file: {path}")
            existing[name] = info
    names = list(named_paths)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            lp, rp = os.path.abspath(named_paths[left]), os.path.abspath(named_paths[right])
            same = canonical[left] == canonical[right]
            if not same and left in existing and right in existing:
                same = (existing[left].st_dev, existing[left].st_ino) == (existing[right].st_dev, existing[right].st_ino)
            if same:
                raise ValueError(
                    f"conflicting state paths: {left}={lp!r} aliases {right}={rp!r}"
                )
    for name, info in existing.items():
        if info.st_nlink > 1:
            path = os.path.abspath(named_paths[name])
            raise ValueError(f"{name} path has multiple hard links (st_nlink={info.st_nlink}): {path}")


class StateStore:
    def __init__(self, runtime_path: str, durable_path: str, *, manifest_path: str | None = None) -> None:
        self._paths = {"runtime": os.path.abspath(runtime_path),
                       "durable": os.path.abspath(durable_path)}
        self._manifest_path = os.path.abspath(manifest_path or os.path.join(
            os.path.dirname(self._paths["runtime"]), "state-migration.json"))
        validate_distinct_paths(self.artifact_paths(self._paths["runtime"], self._paths["durable"],
                                                    self._manifest_path))
        self._lock = threading.RLock()
        self._install_locks = {"runtime": threading.RLock(), "durable": threading.RLock()}
        self._data = {d: {} for d in (*RUNTIME_DOMAINS, *DURABLE_DOMAINS)}
        self._generation = {"runtime": 0, "durable": 0}
        self._dirty = {"runtime": False, "durable": False}
        self._started = False
        self._closing = False
        self._closed = False
        self._last_error: dict[str, str | None] = {"runtime": None, "durable": None}
        self._runtime_timer: threading.Timer | None = None
        self._debounce_seconds = 1.0
        self._lock_files: list[Any] = []
        self._loaded_from: dict[str, str | None] = {"runtime": None, "durable": None}
        self._encoded: dict[str, dict[str, tuple[Any, bytes]]] = {"runtime": {}, "durable": {}}
        self._installed: dict[str, tuple[int, ...] | None] = {"runtime": None, "durable": None}
        self._unvalidated = {"runtime": False, "durable": False}

    @staticmethod
    def lock_path(target: str) -> str:
        return os.path.abspath(target) + ".lock"

    @classmethod
    def artifact_paths(cls, runtime: str, durable: str, manifest: str) -> dict[str, str]:
        result = {
            "runtimeStatePath": runtime, "runtimeBackup": runtime + ".bak",
            "durableStatePath": durable, "durableBackup": durable + ".bak",
            "migrationManifest": manifest,
        }
        for logical, target in (("runtimeLock", runtime), ("durableLock", durable),
                                ("migrationLock", manifest)):
            result[logical] = cls.lock_path(target)
        return result

    @staticmethod
    def _kind(domain: str) -> str:
        if domain in RUNTIME_DOMAINS:
            return "runtime"
        if domain in DURABLE_DOMAINS:
            return "durable"
        raise KeyError(f"unknown state domain: {domain}")

    @staticmethod
    def _payload_bytes(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")

    @classmethod
    def _envelope(cls, kind: str, generation: int, payload: dict[str, Any]) -> dict[str, Any]:
        body = cls._payload_bytes(payload)
        return {"schema": SCHEMA, "version": VERSION, "kind": kind,
                "generation": generation, "checksum": hashlib.sha256(body).hexdigest(),
                "payload": payload}

    @staticmethod
    def _envelope_bytes(kind: str, generation: int, body: bytes) -> bytes:
        """Byte-identical to the sorted compact dump of ``_envelope`` plus newline."""
        compact = {"ensure_ascii": False, "sort_keys": True, "separators": (",", ":")}
        head = json.dumps({"checksum": hashlib.sha256(body).hexdigest(), "generation": generation,
                           "kind": kind}, **compact)
        tail = json.dumps({"schema": SCHEMA, "version": VERSION}, **compact)
        return (head[:-1] + ',"payload":').encode() + body + ("," + tail[1:] + "\n").encode()

    def _encode(self, kind: str, payload: dict[str, Any]) -> bytes:
        """Payload bytes reusing the encoding of every unchanged published domain."""
        cache = self._encoded[kind]; parts = []
        for name in sorted(payload):
            value = payload[name]; hit = cache.get(name)
            if hit is None or hit[0] is not value:
                hit = cache[name] = (value, self._payload_bytes(value))
            parts.append(json.dumps(name, ensure_ascii=False).encode() + b":" + hit[1])
        return b"{" + b",".join(parts) + b"}"

    @staticmethod
    def _signature(path: str) -> tuple[int, ...]:
        info = os.stat(path)
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    @staticmethod
    def _verify_file(path: str, expected: bytes) -> None:
        with open(path, "rb") as handle:
            if handle.read() != expected:
                raise SnapshotError(f"closed-file verification mismatch: {os.path.basename(path)}")

    @classmethod
    def _validate_obj(cls, obj: Any, expected_kind: str) -> tuple[int, dict[str, Any]]:
        if not isinstance(obj, dict) or obj.get("schema") != SCHEMA or obj.get("version") != VERSION:
            raise SnapshotError("snapshot schema/version mismatch")
        if obj.get("kind") != expected_kind:
            raise SnapshotError("snapshot kind mismatch")
        generation, payload = obj.get("generation"), obj.get("payload")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or not isinstance(payload, dict):
            raise SnapshotError("invalid generation/payload")
        expected = RUNTIME_DOMAINS if expected_kind == "runtime" else DURABLE_DOMAINS
        if any(not isinstance(payload.get(domain, {}), dict) for domain in expected):
            raise SnapshotError("invalid domain payload")
        try:
            checksum = hashlib.sha256(cls._payload_bytes(payload)).hexdigest()
        except (TypeError, ValueError) as exc:
            raise SnapshotError(f"payload is not finite JSON: {exc}") from exc
        if obj.get("checksum") != checksum:
            raise SnapshotError("snapshot checksum mismatch")
        return generation, {d: copy.deepcopy(payload.get(d, {})) for d in expected}

    @classmethod
    def read_snapshot(cls, path: str, kind: str) -> tuple[int, dict[str, Any]]:
        with open(path, "r", encoding="utf-8") as handle:
            return cls._validate_obj(json.load(handle), kind)

    @staticmethod
    def _sync_dir(directory: str) -> None:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def write_snapshot(cls, path: str, kind: str, generation: int,
                       payload: dict[str, Any], *, make_backup: bool = True,
                       body: bytes | None = None,
                       trusted: tuple[int, ...] | None = None) -> tuple[int, ...]:
        """Atomically install main and backup as one recoverable transaction.

        ``body`` is the payload already encoded from records validated at their
        mutation boundary; without it the payload itself must round-trip as JSON.
        ``trusted`` is the signature of the main this process installed or
        verified, so the old main can become the backup without being decoded
        again. Returns the installed main's signature.
        """
        path = os.path.abspath(path); backup = path + ".bak"
        directory = os.path.dirname(path) or "."
        if not os.path.isdir(directory): os.makedirs(directory, mode=0o700, exist_ok=True)
        if body is None:
            body = cls._payload_bytes(payload)
            if json.loads(body) != payload:
                raise SnapshotError("closed-file verification mismatch")
        encoded = cls._envelope_bytes(kind, generation, body)
        token = uuid.uuid4().hex; base = os.path.basename(path)
        candidate = os.path.join(directory, f".{base}.{token}.tmp")
        old_main = os.path.join(directory, f".{base}.{token}.old-main")
        old_backup = os.path.join(directory, f".{base}.{token}.old-bak")
        backup_candidate = os.path.join(directory, f".{base}.{token}.new-bak")
        had_main, had_backup = os.path.exists(path), os.path.exists(backup)
        main_installed = backup_aside = backup_installed = False
        restore_main = old_main

        def write_file(destination: str, data: bytes) -> None:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())

        try:
            write_file(candidate, encoded)
            cls._verify_file(candidate, encoded)
            old_bytes, old_valid = b"", False
            if had_main:
                old_valid = trusted is not None and cls._signature(path) == trusted
                with open(path, "rb") as handle: old_bytes = handle.read()
                if not old_valid:
                    try: cls._validate_obj(json.loads(old_bytes), kind); old_valid = True
                    except Exception: pass
            rotate = make_backup and old_valid
            if had_main:
                # One copy of the old main is both the rollback source and,
                # once rotated, the new backup.
                restore_main = backup_candidate if rotate else old_main
                write_file(restore_main, old_bytes); os.chmod(restore_main, 0o600)
                cls._verify_file(restore_main, old_bytes)
            os.replace(candidate, path); main_installed = True
            os.chmod(path, 0o600); cls._verify_file(path, encoded); cls._sync_dir(directory)
            if rotate:
                if had_backup: os.replace(backup, old_backup); backup_aside = True
                os.replace(backup_candidate, backup); backup_installed = True
                os.chmod(backup, 0o600); cls._verify_file(backup, old_bytes); cls._sync_dir(directory)
            return cls._signature(path)
        except BaseException as install_exc:
            if main_installed:
                try:
                    if not had_main:
                        try: os.unlink(path)
                        except FileNotFoundError: pass
                    elif backup_installed: os.replace(backup, path)
                    else: os.replace(restore_main, path)
                    if backup_aside: os.replace(old_backup, backup)
                    cls._sync_dir(directory)
                except BaseException as rollback_exc:
                    raise SnapshotError(f"snapshot install failed ({install_exc}) and rollback failed: {rollback_exc}") from install_exc
            raise
        finally:
            for temporary in (candidate, old_main, old_backup, backup_candidate):
                try: os.unlink(temporary)
                except FileNotFoundError: pass

    def _write(self, kind: str, generation: int, payload: dict[str, Any], *,
               validated: bool = True, make_backup: bool = True) -> None:
        """Install a snapshot of published, mutation-validated domains."""
        validated = validated and not self._unvalidated[kind]
        self._installed[kind] = self.write_snapshot(
            self._paths[kind], kind, generation, payload, make_backup=make_backup,
            body=self._encode(kind, payload) if validated else None, trusted=self._installed[kind])
        if not validated: self._unvalidated[kind] = False

    def _acquire_process_lock(self) -> None:
        """Acquire deterministic per-target locks, making partial overlaps contend."""
        targets = sorted({_canonical(self._paths["runtime"]), _canonical(self._paths["durable"]),
                          _canonical(self._manifest_path)})
        acquired: list[Any] = []
        try:
            for target in targets:
                lock_path = self.lock_path(target)
                directory = os.path.dirname(lock_path) or "."
                if not os.path.isdir(directory): os.makedirs(directory, mode=0o700, exist_ok=True)
                handle = open(lock_path, "a+b")
                os.chmod(lock_path, 0o600)
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    handle.close()
                    raise RuntimeError(f"another process owns StateStore target: {target} (lock {lock_path})") from exc
                acquired.append(handle)
            self._lock_files = acquired
        except BaseException:
            for handle in reversed(acquired):
                try: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally: handle.close()
            raise

    def start(self, *, migrated: dict[str, dict[str, Any]] | None = None) -> None:
        with self._lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("StateStore is closed")
        self._acquire_process_lock()
        try:
            loaded: dict[str, tuple[int, dict[str, Any]]] = {}
            for kind in ("runtime", "durable"):
                valid: list[tuple[int, dict[str, Any], str]] = []
                failures: list[str] = []
                for candidate in (self._paths[kind], self._paths[kind] + ".bak"):
                    if not os.path.exists(candidate):
                        continue
                    try:
                        generation, payload = self.read_snapshot(candidate, kind)
                        valid.append((generation, payload, candidate))
                    except Exception as exc:
                        failures.append(f"{candidate}: {exc}")
                if valid:
                    generation, payload, selected = max(valid, key=lambda item: item[0])
                    loaded[kind] = generation, payload; self._loaded_from[kind] = selected
                    if selected.endswith(".bak"):
                        print(f"[state_store] recovered {kind} generation {generation} from verified backup {selected}")
                        try:
                            # Do not claim a healthy durable startup while only the
                            # backup is good. Runtime retains the good copy and retries.
                            self._write(kind, generation, payload, make_backup=False)
                        except BaseException as exc:
                            if kind == "durable":
                                raise SnapshotError(f"durable main self-heal from {selected} failed: {exc}") from exc
                            self._last_error[kind] = f"runtime main self-heal from {selected} failed: {exc}"
                            self._dirty[kind] = True
                            print(f"[state_store] {self._last_error[kind]}; will retry")
                    else:
                        self._installed[kind] = self._signature(selected)
                        if len(valid) > 1 and valid[1][0] > valid[0][0]:
                            print(f"[state_store] selected higher verified {kind} generation {generation}")
                elif failures and kind == "durable":
                    raise SnapshotError("durable state exists but no valid snapshot: " + "; ".join(failures))
                elif failures:
                    print("[state_store] invalid runtime snapshots; starting empty: " + "; ".join(failures))
            with self._lock:
                for kind, (generation, payload) in loaded.items():
                    self._generation[kind] = generation; self._data.update(payload)
                    if self._last_error[kind] is not None: self._dirty[kind] = True
                if migrated is not None:
                    for kind in ("runtime", "durable"):
                        if kind not in loaded:
                            domains = RUNTIME_DOMAINS if kind == "runtime" else DURABLE_DOMAINS
                            for domain in domains:
                                self._data[domain] = copy.deepcopy(migrated.get(domain, {}))
                            self._generation[kind] += 1; self._dirty[kind] = True
                            self._unvalidated[kind] = True
                self._started = True
            if migrated is not None:
                if self._dirty["durable"]: self.flush("durable", strict=True)
                if self._dirty["runtime"]: self.flush("runtime", strict=True)
        except BaseException:
            self._release_process_lock()
            raise

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"started": self._started, "closing": self._closing,
                    "generation": dict(self._generation), "dirty": dict(self._dirty),
                    "last_error": dict(self._last_error)}

    def get(self, domain: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data[domain].get(key)
            return copy.deepcopy(value) if value is not None else None

    def values(self, domain: str) -> list[dict[str, Any]]:
        with self._lock: return copy.deepcopy(list(self._data[domain].values()))

    def items(self, domain: str) -> dict[str, dict[str, Any]]:
        with self._lock: return copy.deepcopy(self._data[domain])

    def _assert_mutable(self) -> None:
        if not self._started or self._closing or self._closed:
            raise RuntimeError("StateStore is not accepting mutations")

    def _schedule_runtime_flush(self) -> None:
        with self._lock:
            if self._runtime_timer is not None or not self._started or self._closing:
                return
            timer = threading.Timer(self._debounce_seconds, self._runtime_flush_timer)
            timer.daemon = True; self._runtime_timer = timer; timer.start()

    def _runtime_flush_timer(self) -> None:
        with self._lock:
            self._runtime_timer = None
            if self._closing: return
        if not self.flush("runtime", strict=False): self._schedule_runtime_flush()

    def _candidate_for(self, kind: str) -> dict[str, dict[str, Any]]:
        """Capture one immutable published generation without copying records.

        Every mutation replaces domains and touched records instead of modifying
        a publication in place.  Holding references here is therefore a stable
        flush snapshot even after ``_lock`` is released.
        """
        domains = RUNTIME_DOMAINS if kind == "runtime" else DURABLE_DOMAINS
        return {domain: self._data[domain] for domain in domains}

    def _mutate(self, domain: str, operation: Callable[[dict[str, dict[str, Any]]], Any],
                *, strict: bool | None = None) -> Any:
        kind = self._kind(domain)
        with self._install_locks[kind]:
            with self._lock:
                self._assert_mutable()
                transaction = CowDomain(self._data[domain])
                result = operation(transaction)
                # Commit validates only touched records and detaches callback-owned
                # values. Runtime-wide encoding is deliberately deferred to flush.
                candidate_domain = transaction.commit()
                if candidate_domain is self._data[domain] and not self._dirty[kind]:
                    return copy.deepcopy(result)
                generation = self._generation[kind] + 1
                candidate = self._candidate_for(kind) if kind == "durable" else None
                if candidate is not None:
                    candidate[domain] = candidate_domain
            if kind == "durable":
                try:
                    self._write(kind, generation, candidate)
                except BaseException as exc:
                    with self._lock: self._last_error[kind] = str(exc)
                    raise
                with self._lock:
                    self._data[domain] = candidate_domain
                    self._generation[kind] = generation
                    self._dirty[kind] = False; self._last_error[kind] = None
            else:
                with self._lock:
                    self._data[domain] = candidate_domain
                    self._generation[kind] = generation
                    self._dirty[kind] = True
        if kind == "runtime": self._schedule_runtime_flush()
        return copy.deepcopy(result)

    def _mutate_many(self, domains: Iterable[str], operation: Callable[[dict[str, dict[str, Any]]], Any],
                     *, strict: bool | None = None) -> Any:
        domains = tuple(domains); kinds = {self._kind(domain) for domain in domains}
        if len(kinds) != 1:
            raise ValueError("cross-kind mutation is not supported")
        kind = next(iter(kinds))
        kind_domains = RUNTIME_DOMAINS if kind == "runtime" else DURABLE_DOMAINS
        declared = set(domains)
        with self._install_locks[kind]:
            with self._lock:
                self._assert_mutable()
                # Keep the historical same-kind mapping available to private
                # callbacks, but lazily detach only domains/records they touch.
                transactions = {name: CowDomain(self._data[name]) for name in kind_domains}
                result = operation(transactions)
                candidate_domains = {
                    name: transaction.commit()
                    for name, transaction in transactions.items()
                    if name in declared or transaction.touched
                }
                if not self._dirty[kind] and all(
                        value is self._data[name] for name, value in candidate_domains.items()):
                    return copy.deepcopy(result)
                generation = self._generation[kind] + 1
                candidate = self._candidate_for(kind) if kind == "durable" else None
                if candidate is not None:
                    candidate.update(candidate_domains)
            if kind == "durable":
                self._write(kind, generation, candidate)
            with self._lock:
                self._data.update(candidate_domains); self._generation[kind] = generation
                self._dirty[kind] = kind == "runtime"; self._last_error[kind] = None
        if kind == "runtime": self._schedule_runtime_flush()
        return copy.deepcopy(result)

    def flush(self, kind: str | None = None, *, strict: bool = False) -> bool:
        ok = True
        for current in ((kind,) if kind else ("durable", "runtime")):
            with self._install_locks[current]:
                with self._lock:
                    if not self._dirty[current]: continue
                    generation = self._generation[current]
                    payload = self._candidate_for(current)
                try:
                    self._write(current, generation, payload)
                except BaseException as exc:
                    with self._lock:
                        self._last_error[current] = str(exc); self._dirty[current] = True
                    print(f"[state_store] {current} snapshot failed: {exc}")
                    if strict: raise
                    ok = False
                else:
                    with self._lock:
                        self._last_error[current] = None
                        if self._generation[current] == generation: self._dirty[current] = False
        return ok

    def install_migration(self, migrated: dict[str, dict[str, Any]]) -> dict[str, int]:
        """Replace both kinds from one inspected legacy revision.

        The caller writes its manifest only after this method returns. If the
        process stops between snapshots, an absent/stale manifest makes startup
        repeat both installations.
        """
        with self._install_locks["durable"], self._install_locks["runtime"]:
            with self._lock:
                self._assert_mutable()
                candidates = {
                    "runtime": {d: copy.deepcopy(migrated.get(d, {})) for d in RUNTIME_DOMAINS},
                    # An older schema cannot revoke domains it never owned.
                    "durable": {d: copy.deepcopy(migrated[d] if d in migrated else self._data[d])
                                for d in DURABLE_DOMAINS},
                }
                for payload in candidates.values(): self._payload_bytes(payload)
                generations = {kind: self._generation[kind] + 1 for kind in candidates}
            # Durable first: an interruption remains detectable because the
            # manifest is still stale, and each old main becomes a verified .bak.
            for kind in ("durable", "runtime"):
                self._write(kind, generations[kind], candidates[kind], validated=False)
            with self._lock:
                for kind, payload in candidates.items():
                    self._data.update(payload); self._generation[kind] = generations[kind]
                    self._dirty[kind] = False; self._last_error[kind] = None
            return generations

    def _release_process_lock(self) -> None:
        handles, self._lock_files = self._lock_files, []
        for handle in reversed(handles):
            try: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally: handle.close()

    def close(self) -> bool:
        with self._lock:
            if self._closed: return True
            self._closing = True
            timer, self._runtime_timer = self._runtime_timer, None
        if timer is not None:
            timer.cancel(); timer.join(timeout=max(1.0, self._debounce_seconds + 0.5))
        try:
            self.flush("durable", strict=True)
            result = self.flush("runtime", strict=False)
        finally:
            with self._lock:
                self._started = False; self._closed = True
            self._release_process_lock()
        return result

    @contextmanager
    def optional_write_timeout(self, timeout_ms: int = 100):
        yield
