"""Single-purpose SQLite state for management auth, approvals and audit."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from collections.abc import Mapping
from typing import Any, Callable


_API_KEY_PROVENANCE_PREFIX = "apiKeyProvenance:"
_API_KEY_PROVENANCE_SOURCES = frozenset({"custom", "generated"})


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    selector TEXT NOT NULL UNIQUE,
    verifier BLOB NOT NULL,
    subject_id TEXT NOT NULL,
    auth_method TEXT NOT NULL,
    roles_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    issued_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    generation INTEGER NOT NULL,
    revoked_at REAL,
    client_name TEXT,
    source_address TEXT,
    device_summary TEXT
);
CREATE INDEX IF NOT EXISTS sessions_selector_idx ON sessions(selector);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    secret_verifier BLOB NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    client_name TEXT NOT NULL,
    source_address TEXT NOT NULL,
    device_summary TEXT,
    request_id TEXT NOT NULL,
    decided_by TEXT,
    decided_at REAL,
    consumed_at REAL,
    confirm_code TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    result TEXT NOT NULL,
    request_id TEXT NOT NULL,
    occurred_at REAL NOT NULL
);
"""


# Additive columns introduced after the first released schema. ``CREATE TABLE
# IF NOT EXISTS`` never alters an existing table, so older databases receive
# them through idempotent ALTERs. Every column is nullable: legacy rows simply
# report "unknown" device facts / no confirmation code.
_ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "sessions": (
        ("client_name", "TEXT"),
        ("source_address", "TEXT"),
        ("device_summary", "TEXT"),
    ),
    "approvals": (("confirm_code", "TEXT"),),
}


class StoreCapacityError(RuntimeError):
    pass


class ManagementStateStore:
    """Owns management security state and non-secret provenance metadata."""

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float],
        max_audit_records: int = 5_000,
        max_approvals: int = 2_000,
    ) -> None:
        if max_audit_records < 1 or max_approvals < 1:
            raise ValueError("management store bounds must be positive")
        self.path = path
        self._clock = clock
        self._max_audit_records = max_audit_records
        self._max_approvals = max_approvals
        self._lock = threading.RLock()
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path)) or "."
            os.makedirs(parent, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate_additive_columns()

    def _migrate_additive_columns(self) -> None:
        with self._conn:
            for table, columns in _ADDITIVE_COLUMNS.items():
                existing = {
                    str(row[1])
                    for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, declaration in columns:
                    if name not in existing:
                        self._conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def synchronize_credential(self, fingerprint: str) -> int:
        """Increment generation and revoke sessions when the fixed key changes."""
        now = self._clock()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key='credentialFingerprint'"
            ).fetchone()
            generation_row = self._conn.execute(
                "SELECT value FROM metadata WHERE key='credentialGeneration'"
            ).fetchone()
            generation = int(generation_row["value"]) if generation_row else 1
            if row is None:
                self._conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('credentialFingerprint',?)",
                    (fingerprint,),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES('credentialGeneration',?)",
                    (str(generation),),
                )
            elif not hmac.compare_digest(str(row["value"]), fingerprint):
                generation += 1
                self._conn.execute(
                    "UPDATE metadata SET value=? WHERE key='credentialFingerprint'",
                    (fingerprint,),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO metadata(key,value) VALUES('credentialGeneration',?)",
                    (str(generation),),
                )
                self._conn.execute(
                    "UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL",
                    (now,),
                )
                self._conn.execute(
                    "UPDATE approvals SET status='expired' "
                    "WHERE status IN ('pending','approved')",
                )
            return generation

    def credential_generation(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key='credentialGeneration'"
            ).fetchone()
            return int(row["value"]) if row else 1

    @staticmethod
    def _api_key_provenance_key(key_id: str) -> str:
        identity = hashlib.sha256(str(key_id).encode("utf-8")).hexdigest()
        return _API_KEY_PROVENANCE_PREFIX + identity

    def set_api_key_provenance(
        self, *, key_id: str, secret_fingerprint: str, source: str,
    ) -> None:
        """Record a known source after the authoritative config write succeeds."""
        if source not in _API_KEY_PROVENANCE_SOURCES:
            raise ValueError("unsupported API key provenance source")
        value = json.dumps({"fingerprint": secret_fingerprint, "source": source})
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                (self._api_key_provenance_key(key_id), value),
            )

    def get_api_key_provenance(
        self, *, key_id: str, secret_fingerprint: str,
    ) -> str | None:
        """Read a source only for the current secret; never repair records on GET."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key=?",
                (self._api_key_provenance_key(key_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row["value"])
        except (TypeError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("fingerprint") != secret_fingerprint:
            return None
        source = record.get("source")
        return source if source in _API_KEY_PROVENANCE_SOURCES else None

    def forget_api_key_provenance(self, *, key_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM metadata WHERE key=?",
                (self._api_key_provenance_key(key_id),),
            )

    @staticmethod
    def _session_values(values: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            values["session_id"],
            values["selector"],
            values["verifier"],
            values["subject_id"],
            values["auth_method"],
            json.dumps(list(values["roles"]), separators=(",", ":")),
            json.dumps(list(values["capabilities"]), separators=(",", ":")),
            values["issued_at"],
            values["last_seen_at"],
            values["expires_at"],
            values["generation"],
            values.get("client_name"),
            values.get("source_address"),
            values.get("device_summary"),
        )

    def _insert_session(self, values: Mapping[str, Any]) -> None:
        self._conn.execute(
            """INSERT INTO sessions(
                session_id,selector,verifier,subject_id,auth_method,roles_json,
                capabilities_json,issued_at,last_seen_at,expires_at,generation,
                client_name,source_address,device_summary
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            self._session_values(values),
        )

    def insert_session(self, values: Mapping[str, Any]) -> None:
        with self._lock, self._conn:
            self._insert_session(values)

    def get_session(self, selector: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE selector=?", (selector,)
            ).fetchone()
            return dict(row) if row else None

    def touch_session(self, selector: str, *, previous_seen_at: float, now: float) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET last_seen_at=? "
                "WHERE selector=? AND last_seen_at=? AND revoked_at IS NULL",
                (now, selector, previous_seen_at),
            )
            return cursor.rowcount == 1

    def revoke_session(self, session_id: str) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE session_id=? AND revoked_at IS NULL",
                (self._clock(), session_id),
            )
            return cursor.rowcount == 1

    def list_active_sessions(self, *, now: float, idle_timeout_seconds: float) -> list[dict[str, Any]]:
        """Unrevoked, current-generation sessions that are neither absolutely
        nor idly expired at ``now``; most recently used first."""
        with self._lock:
            generation = self.credential_generation()
            rows = self._conn.execute(
                "SELECT session_id,auth_method,issued_at,last_seen_at,expires_at,"
                "client_name,source_address,device_summary FROM sessions "
                "WHERE revoked_at IS NULL AND generation=? AND expires_at>? "
                "AND last_seen_at>? ORDER BY last_seen_at DESC, issued_at DESC",
                (generation, now, now - idle_timeout_seconds),
            ).fetchall()
            return [dict(row) for row in rows]

    def revoke_other_sessions(self, keep_session_id: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE sessions SET revoked_at=? "
                "WHERE revoked_at IS NULL AND session_id<>?",
                (self._clock(), keep_session_id),
            )
            return cursor.rowcount

    def revoke_all_sessions(self) -> int:
        now = self._clock()
        with self._lock, self._conn:
            generation = self.credential_generation() + 1
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata(key,value) VALUES('credentialGeneration',?)",
                (str(generation),),
            )
            cursor = self._conn.execute(
                "UPDATE sessions SET revoked_at=? WHERE revoked_at IS NULL", (now,)
            )
            return cursor.rowcount

    def insert_approval(self, values: Mapping[str, Any]) -> None:
        with self._lock, self._conn:
            now = self._clock()
            self._conn.execute(
                "UPDATE approvals SET status='expired' "
                "WHERE status IN ('pending','approved') AND expires_at<=?",
                (now,),
            )
            count = int(self._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])
            if count >= self._max_approvals:
                remove = count - self._max_approvals + 1
                self._conn.execute(
                    "DELETE FROM approvals WHERE approval_id IN ("
                    "SELECT approval_id FROM approvals "
                    "WHERE status IN ('expired','denied','consumed') "
                    "ORDER BY created_at LIMIT ?)",
                    (remove,),
                )
                count = int(self._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0])
                if count >= self._max_approvals:
                    raise StoreCapacityError("approval capacity is full")
            self._conn.execute(
                """INSERT INTO approvals(
                    approval_id,secret_verifier,status,created_at,expires_at,
                    client_name,source_address,device_summary,request_id,
                    confirm_code
                ) VALUES(?,?,'pending',?,?,?,?,?,?,?)""",
                (
                    values["approval_id"],
                    values["secret_verifier"],
                    values["created_at"],
                    values["expires_at"],
                    values["client_name"],
                    values["source_address"],
                    values.get("device_summary"),
                    values["request_id"],
                    values.get("confirm_code"),
                ),
            )

    def _approval_row(self, approval_id: str, now: float) -> sqlite3.Row | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row and row["status"] in {"pending", "approved"} and now >= row["expires_at"]:
            self._conn.execute(
                "UPDATE approvals SET status='expired' "
                "WHERE approval_id=? AND status IN ('pending','approved')",
                (approval_id,),
            )
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return row

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn:
            row = self._approval_row(approval_id, self._clock())
            return dict(row) if row else None

    def decide_approval(self, approval_id: str, *, approved: bool, actor_id: int) -> str:
        now = self._clock()
        target = "approved" if approved else "denied"
        with self._lock, self._conn:
            row = self._approval_row(approval_id, now)
            if row is None:
                return "notFound"
            if row["status"] != "pending":
                status = str(row["status"])
                return "alreadyDecided" if status in {"approved", "denied"} else status
            cursor = self._conn.execute(
                "UPDATE approvals SET status=?,decided_by=?,decided_at=? "
                "WHERE approval_id=? AND status='pending' AND expires_at>?",
                (target, str(actor_id), now, approval_id, now),
            )
            if cursor.rowcount != 1:
                row = self._approval_row(approval_id, now)
                if row is None:
                    return "notFound"
                status = str(row["status"])
                return "alreadyDecided" if status in {"approved", "denied"} else status
            return target

    def deny_pending_approval(self, approval_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE approvals SET status='denied',decided_at=? "
                "WHERE approval_id=? AND status='pending'",
                (self._clock(), approval_id),
            )

    def consume_approval_and_insert_session(
        self,
        approval_id: str,
        *,
        secret_verifier: bytes,
        session_values: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        now = self._clock()
        with self._lock, self._conn:
            row = self._approval_row(approval_id, now)
            if row is None:
                return "notFound", None
            if not hmac.compare_digest(bytes(row["secret_verifier"]), secret_verifier):
                return "authenticationFailed", None
            if row["status"] != "approved":
                status = "alreadyConsumed" if row["status"] == "consumed" else str(row["status"])
                return status, None
            cursor = self._conn.execute(
                "UPDATE approvals SET status='consumed',consumed_at=? "
                "WHERE approval_id=? AND status='approved' AND expires_at>?",
                (now, approval_id, now),
            )
            if cursor.rowcount != 1:
                row = self._approval_row(approval_id, now)
                if row and row["status"] == "consumed":
                    return "alreadyConsumed", None
                return str(row["status"]) if row else "notFound", None
            # The browser that created the approval is the device that owns the
            # session; copy its non-secret description for the device list.
            self._insert_session({
                **session_values,
                "client_name": row["client_name"],
                "source_address": row["source_address"],
                "device_summary": row["device_summary"],
            })
            return "consumed", dict(row)

    def record_audit(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        result: str,
        request_id: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit(actor,action,target,result,request_id,occurred_at) "
                "VALUES(?,?,?,?,?,?)",
                (actor, action, target, result, request_id, self._clock()),
            )
            self._conn.execute(
                "DELETE FROM audit WHERE sequence NOT IN "
                "(SELECT sequence FROM audit ORDER BY sequence DESC LIMIT ?)",
                (self._max_audit_records,),
            )

    def audit_snapshot(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT actor,action,target,result,request_id,occurred_at "
                "FROM audit ORDER BY sequence"
            ).fetchall()
            return tuple(dict(row) for row in rows)
