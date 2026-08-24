"""Read-only, WAL/hot-journal-safe migration of retired application state.

Authoritative SQLite files are never opened by SQLite.  The complete source set
is copied into a mode-0700 private directory and only that copy is recovered and
queried with an ordinary SQLite connection.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any

TABLES = {
 "performance_stats": ("channel_key", "model"),
 "channel_errors": ("channel_key", "model"),
 "cache_affinities": ("fingerprint",),
 "client_affinities": ("client_key",),
 "schema_meta": ("key",),
 "oauth_quota_cache": ("account_key",),
 "api_provider_usage_cache": ("account_id",),
 "network_check_status": ("key",),
 "xai_video_jobs": ("request_id",),
 "codex_compaction_owners": ("compaction_id", "content_digest"),
 "app_self_update": ("id",),
 "app_update_state": ("repo",),
 "status_seen_updates": ("provider", "update_id"),
 "status_muted_incidents": ("provider", "incident_id"),
}
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
SQLITE_MAGIC = b"SQLite format 3\x00"
MIGRATION_VERSION = 2


class LegacyUnavailable(RuntimeError):
    """The source could not be inspected; this is not evidence of corruption."""


class LegacyCorrupt(RuntimeError):
    pass


def _key(row: dict[str, Any], columns: tuple[str, ...]) -> str:
    return "\x1f".join(str(row.get(column, "")) for column in columns)


def _regular(path: str) -> bool:
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise LegacyUnavailable(f"legacy source is not a regular file: {path}")
    return True


def source_files(path: str) -> list[str]:
    absolute = os.path.abspath(path)
    return [candidate for candidate in (absolute, *(absolute + suffix for suffix in SIDECAR_SUFFIXES))
            if _regular(candidate)]


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024): digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(path: str) -> dict[str, Any]:
    """Stable identity of main plus all relevant real sidecars."""
    absolute = os.path.abspath(path)
    files = []
    try:
        for candidate in source_files(absolute):
            info = os.stat(candidate)
            files.append({"suffix": candidate[len(absolute):], "size": info.st_size,
                          "mtime_ns": info.st_mtime_ns, "sha256": _file_digest(candidate)})
    except OSError as exc:
        raise LegacyUnavailable(f"cannot fingerprint legacy state {getattr(exc, 'filename', absolute)}: {exc}") from exc
    body = {"canonical_path": os.path.realpath(absolute), "files": files}
    body["revision"] = hashlib.sha256(json.dumps(body, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
    return body


def _copy_source_set(path: str, work: str) -> tuple[str, dict[str, Any]]:
    absolute = os.path.abspath(path)
    before = source_fingerprint(absolute)
    if not before["files"]:
        return os.path.join(work, "state.db"), before
    copied_main = os.path.join(work, "state.db")
    try:
        for entry in before["files"]:
            source = absolute + entry["suffix"]
            destination = copied_main + entry["suffix"]
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o600)
    except (PermissionError, OSError) as exc:
        raise LegacyUnavailable(f"cannot copy legacy state set at {absolute}: {exc}") from exc
    after = source_fingerprint(absolute)
    if before["revision"] != after["revision"]:
        raise LegacyUnavailable(f"legacy state changed while being copied: {absolute}; retry startup")
    # Verify copied bytes against the sealed fingerprint before SQLite sees them.
    for entry in before["files"]:
        copied = copied_main + entry["suffix"]
        if os.path.getsize(copied) != entry["size"] or _file_digest(copied) != entry["sha256"]:
            raise LegacyUnavailable(f"legacy copy verification failed for {absolute + entry['suffix']}")
    return copied_main, before


def _read_copy(copied_main: str) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    if not os.path.exists(copied_main): return {}, []
    with open(copied_main, "rb") as handle:
        header = handle.read(len(SQLITE_MAGIC))
    if header != SQLITE_MAGIC:
        raise LegacyCorrupt(f"invalid SQLite header: {header[:8].hex()}")
    try:
        conn = sqlite3.connect(copied_main, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            quick = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
            if quick != ["ok"]: raise LegacyCorrupt("quick_check: " + "; ".join(quick[:5]))
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]: raise LegacyCorrupt("integrity_check: " + "; ".join(integrity[:5]))
            existing = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            result: dict[str, dict[str, dict[str, Any]]] = {}
            for table, keys in TABLES.items():
                if table not in existing: continue
                rows = [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
                if table == "oauth_quota_cache":
                    for row in rows:
                        if not row.get("account_key"): row["account_key"] = str(row.get("email") or "")
                result[table] = {_key(row, keys): row for row in rows}
            responses = ([dict(row) for row in conn.execute("SELECT * FROM openai_response_store")]
                         if "openai_response_store" in existing else [])
            return result, responses
        finally:
            conn.close()
    except LegacyCorrupt:
        raise
    except sqlite3.DatabaseError as exc:
        lowered = str(exc).lower()
        if any(word in lowered for word in ("malformed", "not a database", "corrupt")):
            raise LegacyCorrupt(str(exc)) from exc
        raise LegacyUnavailable(f"SQLite could not inspect private legacy copy: {exc}") from exc


def inspect_legacy(path: str) -> dict[str, Any]:
    """Inspect one source revision and return state, optional responses and evidence."""
    work = tempfile.mkdtemp(prefix="parrot-state-migration-")
    os.chmod(work, 0o700)
    try:
        copied, fingerprint = _copy_source_set(path, work)
        state, responses = _read_copy(copied)
        return {"source": fingerprint, "state": state, "responses": responses,
                "status": "missing" if not fingerprint["files"] else "healthy"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def backup_candidates(source_path: str, data_dir: str) -> list[str]:
    root = Path(data_dir) / "backups"
    if not root.is_dir(): return []
    candidates: set[str] = set()
    for pattern in ("*.state.db", "state-db-pre-*.db"):
        for path in root.glob(pattern):
            if path.is_file() and os.path.abspath(path) != os.path.abspath(source_path):
                candidates.add(os.path.abspath(path))
    return sorted(candidates, key=lambda candidate: os.path.getmtime(candidate), reverse=True)


def inspect_with_recovery(path: str, data_dir: str) -> dict[str, Any]:
    """Use a healthy historical backup only when the caller is initializing.

    The caller must arbitrate against verified JSON before installing this result.
    Busy, permission and mount/I/O failures remain actionable exceptions.
    """
    try:
        return inspect_legacy(path)
    except LegacyCorrupt as source_error:
        failures = []
        for candidate in backup_candidates(path, data_dir):
            try:
                report = inspect_legacy(candidate)
                backup_source = report["source"]
                report.update(status="backup", corrupt_source=os.path.abspath(path),
                              corrupt_reason=str(source_error), backup=candidate,
                              backup_source=backup_source, source=source_fingerprint(path))
                return report
            except (LegacyCorrupt, LegacyUnavailable) as exc:
                failures.append(f"{candidate}: {exc}")
        fingerprint = source_fingerprint(path)
        return {"source": fingerprint, "state": {}, "responses": [], "status": "rebuilt-empty",
                "corrupt_source": os.path.abspath(path), "corrupt_reason": str(source_error),
                "backup_failures": failures}


def read_legacy_state(path: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Compatibility helper; uses the same safe copied-source inspection."""
    return inspect_legacy(path)["state"]


def read_legacy_response_rows(path: str) -> list[dict[str, Any]]:
    return inspect_legacy(path)["responses"]
