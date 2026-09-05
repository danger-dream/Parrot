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


def normalize_account_device(account: dict, *, protocol_profile: str) -> bool:
    """Compatibility entry point for the versioned per-workspace identity migration."""
    from .codex_identity import normalize_account_identity

    return normalize_account_identity(
        account, protocol_profile=protocol_profile,
    )


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
    direct_installation_header: bool = False,
) -> tuple[dict[str, str], dict | None]:
    """Project an account UUID to metadata and, for compact/realtime, a header.

    Ordinary Responses uses metadata only in the selected protocol profile. The
    direct header is opt-in for endpoint profiles that define that carrier.
    """
    if not installation_id:
        return headers, payload

    out_headers = {
        key: value for key, value in headers.items()
        if str(key).lower() != INSTALLATION_HEADER
    }
    if direct_installation_header:
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
