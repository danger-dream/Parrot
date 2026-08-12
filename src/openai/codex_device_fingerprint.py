"""Narrow, pure helpers for configured Codex OAuth device installation identity."""

from __future__ import annotations

import json
import uuid
from typing import Any


INSTALLATION_HEADER = "x-codex-installation-id"
TURN_METADATA_HEADER = "x-codex-turn-metadata"


def new_installation_id() -> str:
    """Create one canonical random installation identity for authoritative persistence."""
    return str(uuid.uuid4())


def canonical_uuid4(value: Any) -> str:
    """Return a canonical RFC 4122 UUIDv4, or ``""`` when no ID is stored."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("codexDeviceInstallationId must be a canonical UUIDv4 string or empty")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("codexDeviceInstallationId must be a canonical UUIDv4 string or empty") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("codexDeviceInstallationId must be a canonical UUIDv4 string or empty")
    return value


def normalize_account_device(account: dict) -> bool:
    """Normalize default-on device state in one OpenAI OAuth config entry.

    Missing enablement means on.  Only the explicit boolean ``False`` opts out;
    a missing or empty installation ID on an enabled workspace is backfilled
    exactly once. Disabled accounts retain any valid UUID, making re-enable
    reversible.
    """
    provider = str(account.get("provider") or account.get("type") or "").lower()
    if provider != "openai":
        return False
    changed = False
    raw_id = account.get("codexDeviceInstallationId")
    explicit_enabled = account.get("codexDeviceConvergenceEnabled")
    if explicit_enabled is not None and not isinstance(explicit_enabled, bool):
        raise ValueError("codexDeviceConvergenceEnabled must be a boolean")
    enabled = explicit_enabled is not False
    installation_id = canonical_uuid4(raw_id)
    workspace_id = str(
        account.get("workspace_id") or account.get("chatgpt_account_id") or ""
    ).strip()
    if installation_id and not workspace_id:
        raise ValueError(
            "codexDeviceInstallationId requires a nonempty OpenAI workspace/chatgpt account ID"
        )
    if enabled and workspace_id and not installation_id:
        account["codexDeviceInstallationId"] = new_installation_id()
        changed = True
    return changed


def _set_existing_header(headers: dict[str, str], name: str, value: str) -> None:
    for key in list(headers):
        if str(key).lower() == name:
            headers[key] = value
            return


def _rewrite_turn_metadata(raw: Any, installation_id: str) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if not isinstance(obj, dict):
        return raw
    obj["installation_id"] = installation_id
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def apply_device_fingerprint(
    headers: dict[str, str],
    payload: dict | None,
    installation_id: str,
    *,
    create_client_metadata: bool,
) -> tuple[dict[str, str], dict | None]:
    """Rewrite only the four authorized Codex installation carriers.

    The caller supplies an already validated account-scoped UUID.  An existing
    non-object ``client_metadata`` is a candidate transformation error.
    """
    if not installation_id:
        return headers, payload

    out_headers = {
        key: value for key, value in headers.items()
        if str(key).lower() != INSTALLATION_HEADER
    }
    out_headers[INSTALLATION_HEADER] = installation_id
    for key, raw in list(out_headers.items()):
        if str(key).lower() == TURN_METADATA_HEADER:
            _set_existing_header(
                out_headers, TURN_METADATA_HEADER,
                _rewrite_turn_metadata(raw, installation_id),
            )
            break

    if payload is None:
        return out_headers, None
    out_payload = dict(payload)
    if "client_metadata" in out_payload and not isinstance(out_payload["client_metadata"], dict):
        raise ValueError("client_metadata must be an object for Codex device fingerprint")
    metadata = out_payload.get("client_metadata")
    if metadata is None:
        if not create_client_metadata:
            return out_headers, out_payload
        metadata = {}
    else:
        metadata = dict(metadata)
    metadata[INSTALLATION_HEADER] = installation_id
    if TURN_METADATA_HEADER in metadata:
        metadata[TURN_METADATA_HEADER] = _rewrite_turn_metadata(
            metadata[TURN_METADATA_HEADER], installation_id,
        )
    out_payload["client_metadata"] = metadata
    return out_headers, out_payload
