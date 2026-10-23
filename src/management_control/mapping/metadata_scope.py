"""Public scope of a listed metadata binding (listModelMetadata)."""
from __future__ import annotations

import dataclasses

from src.management_control.routing_account_ids import oauth_account_id_from_channel_key

from .contracts import MetadataRecord


def listed_scope(record: MetadataRecord, listed) -> MetadataRecord:
    """Report the scope a listed binding belongs to.

    Account-native bindings (e.g. Cursor's live per-account catalog) resolve
    to a record without a scope key; the list must still say which account
    it is, otherwise several rows look like conflicting ``global`` values.
    """
    scope_key = getattr(listed, "scope_key", None)
    if scope_key is None or record.scope_id is not None:
        return record
    scope = "oauth" if scope_key.startswith("oauth:") else "api"
    public_id = oauth_account_id_from_channel_key(scope_key) if scope == "oauth" else scope_key
    return dataclasses.replace(
        record, scope=scope, scope_id=public_id,
        outbound_model=record.outbound_model or getattr(listed, "outbound_model", None),
    )
