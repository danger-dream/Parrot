"""Management-only OAuth contract helpers.

These helpers deliberately do not alter Telegram/raw domain values.  They are used
when typed Management API DTOs, plans, operations, and audit records are built.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.management_control.public_safety import (
    is_known_sensitive_field,
    redact_known_fields,
)


_VOLATILE_CANDIDATE_FIELDS = {
    "last_refresh",
    "last_model_sync",
    "last_model_sync_attempt",
    "last_model_sync_error",
    "last_model_sync_source",
}


def revision(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact candidate minus refresh/sync timestamps that naturally drift."""
    return {
        str(key): copy.deepcopy(item)
        for key, item in value.items()
        if str(key) not in _VOLATILE_CANDIDATE_FIELDS
    }


def candidate_revision(value: Mapping[str, Any]) -> str:
    return revision(stable_candidate(value))


def credential_fingerprint(value: Mapping[str, Any]) -> str:
    """Bind a plan to credentials without retaining/returning a derived hint."""
    selected = {
        str(key): copy.deepcopy(item)
        for key, item in value.items()
        if is_sensitive_key(key)
        or str(key) in {
            "provider", "type", "email", "subject", "sub", "workspace_id",
            "chatgpt_account_id", "project_id", "organization_id", "expired",
        }
    }
    return revision(selected)


def utc_datetime(value: Any) -> datetime | None:
    """Normalize persisted epoch/ISO values for RFC3339 UTC API output only."""
    if value is None or value == "" or value == -1 or value == "-1":
        return None
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
        if abs(epoch) >= 100_000_000_000:
            epoch /= 1000.0
        parsed = datetime.fromtimestamp(epoch, tz=timezone.utc)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            epoch = float(raw)
        except ValueError:
            normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
        else:
            if abs(epoch) >= 100_000_000_000:
                epoch /= 1000.0
            parsed = datetime.fromtimestamp(epoch, tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_sensitive_key(key: object) -> bool:
    """Compatibility wrapper for the shared exact sensitive-field vocabulary."""
    return is_known_sensitive_field(key)


def sanitize_text(value: object) -> str:
    """Return ordinary business text unchanged; do not infer embedded secrets."""
    return str(value)


def _camel_key(key: object) -> str:
    raw = str(key)
    if raw == "account_key":
        return "accountId"
    head, *tail = raw.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _project_public(value: Any, *, camel_case_keys: bool) -> Any:
    if isinstance(value, Mapping):
        return {
            _camel_key(key) if camel_case_keys else str(key): _project_public(
                item, camel_case_keys=camel_case_keys,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_project_public(item, camel_case_keys=camel_case_keys) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return utc_datetime(value)
    return str(value)


def public_value(value: Any, *, camel_case_keys: bool = False) -> Any:
    """Project OAuth payloads after exact structured-field redaction."""
    return _project_public(
        redact_known_fields(value), camel_case_keys=camel_case_keys,
    )


def audit_failures(action: str, *, target_arg: str | None = None, target: str = "oauth"):
    """Record a stable failed domain audit without exposing exception text."""
    def decorate(method):
        signature = inspect.signature(method)

        @functools.wraps(method)
        def wrapped(self, *args, **kwargs):
            context = args[0] if args else kwargs.get("context")
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                audit_target = target
                if target_arg is not None:
                    try:
                        bound = signature.bind(self, *args, **kwargs)
                        value = bound.arguments.get(target_arg, target)
                        audit_target = getattr(value, "value", value)
                    except Exception:
                        audit_target = target
                if context is not None:
                    self._audit(context, action, str(audit_target), "failed")
                raise

        return wrapped
    return decorate


def invalid_account(account: Mapping[str, Any]) -> bool:
    """Frozen TG invalid-list predicate: email plus auth_error."""
    return bool(account.get("email") or account.get("provider") == "zhipu") and account.get("disabled_reason") == "auth_error"
