"""SQLite error classification shared by optional persistence side effects."""

from __future__ import annotations

import sqlite3


_AVAILABILITY_PRIMARY_CODES = {
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
    sqlite3.SQLITE_NOMEM,
    sqlite3.SQLITE_READONLY,
    sqlite3.SQLITE_IOERR,
    sqlite3.SQLITE_FULL,
    sqlite3.SQLITE_CANTOPEN,
    sqlite3.SQLITE_PROTOCOL,
}
_AVAILABILITY_MESSAGES = (
    "database is locked",
    "database table is locked",
    "disk i/o error",
    "database or disk is full",
    "attempt to write a readonly database",
    "unable to open database file",
    "out of memory",
)


def is_availability_error(exc: BaseException) -> bool:
    """Return whether a SQLite failure is environmental and safe to degrade.

    Programming/schema/data-contract failures must remain visible. Only
    lock/resource/I/O availability failures may skip an optional persistence
    side effect while preserving the primary model response. Extended SQLite
    result codes are reduced to their primary code.
    """
    if not isinstance(exc, sqlite3.Error):
        return False
    if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.IntegrityError,
                        sqlite3.DataError, sqlite3.NotSupportedError,
                        sqlite3.InterfaceError)):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in _AVAILABILITY_PRIMARY_CODES
    message = str(exc).lower()
    return any(fragment in message for fragment in _AVAILABILITY_MESSAGES)
