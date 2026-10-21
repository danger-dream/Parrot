"""One-use Qoder refresh journal. Called under the existing account refresh lock.

Intent is durable BEFORE consuming an RT. Candidate is durable BEFORE config
publication. An intent without a candidate is an unknown outcome, not a licence
to replay the old RT (including force). Job/device journals are independent.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from ... import state_db
from . import auth, common


@dataclass
class _RefreshState:
    fingerprint: str
    retry_at: float = 0
    pending_until: float = 0
    pending: dict | None = field(default=None, repr=False)
    candidate_expired: bool = False


_refresh_states: dict[str, _RefreshState] = {}
_device_states: dict[str, _RefreshState] = {}


def credential_fingerprint(account: dict, pair: str = "job") -> str:
    fields = ("device_token", "device_refresh_token") if pair == "device" else ("access_token", "refresh_token")
    # The exact one-use pair stays consumed across generation backfill/restart.
    # Incarnation CAS is enforced separately at publication.
    raw = [auth.identity(account), account.get("machine_id"), pair, *(account.get(k) for k in fields)]
    return hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()


def forget(account_key: str) -> None:
    # Process memory eviction is NOT permission to forget a consumed RT.
    _refresh_states.pop(account_key, None)
    _device_states.pop(account_key, None)


def _journal_key(account, account_key, pair):
    return hashlib.sha256((account_key + ':' + credential_fingerprint(account, pair)).encode()).hexdigest()


def _load(key):
    try:
        return state_db.qoder_refresh_load(key) or {}
    except Exception:
        raise common.QoderError("refresh", kind="refresh_store_unavailable") from None


def _save(key, row):
    try:
        state_db.qoder_refresh_save(key, row)
    except Exception:
        raise common.QoderError("refresh", kind="refresh_store_unavailable") from None


def refresh_status(account, account_key, pair="job"):
    try:
        return _load(_journal_key(account, account_key, pair)).get("phase", "idle")
    except common.QoderError:
        return "store_unavailable"


def _expired(account: dict, threshold=0) -> bool:
    try:
        expiry = datetime.fromisoformat(str(account.get("expired") or "").replace("Z", "+00:00"))
        return expiry.timestamp() <= time.time() + threshold
    except (ValueError, TypeError, OverflowError):
        return True


def refresh_due(account: dict, account_key: str, threshold: int = 600) -> bool:
    phase = refresh_status(account, account_key)
    if phase in {"intent", "unknown", "rejected", "store_unavailable"}:
        return False  # Public status explains recovery; background must not storm.
    return phase == "candidate" or _expired(account, threshold)


def _persist(account, account_key, pair, fingerprint, fields):
    from ... import config, oauth_manager
    try:
        with config.serialized_updates():
            current = oauth_manager.get_account(account_key)
            if (not current or credential_fingerprint(current, pair) != fingerprint
                    or oauth_manager.account_state_key(current) != oauth_manager.account_state_key(account)):
                raise common.QoderError("refresh", kind="stale_generation")
            if not oauth_manager._save_token_fields(account_key, fields,
                    expected_state_key=oauth_manager.account_state_key(current)):
                raise common.QoderError("refresh", kind="stale_generation")
    except common.QoderError:
        raise
    except Exception:
        raise common.QoderError("refresh", kind="save_failed_retry_save") from None


def _rotate(account, account_key, pair):
    states = _device_states if pair == "device" else _refresh_states
    fingerprint = credential_fingerprint(account, pair)
    state = states.get(account_key)
    if state is None or state.fingerprint != fingerprint:
        state = states[account_key] = _RefreshState(fingerprint)
    key = _journal_key(account, account_key, pair)
    row = _load(key)
    if state.pending is not None:
        # A candidate write failure may be recovered in this process, but never
        # published until the candidate has reached durable private storage.
        row = {"phase": "candidate", "candidate": state.pending}
        _save(key, row)
    elif row.get("phase") == "candidate":
        state.pending = row["candidate"]
    elif row.get("phase") in {"intent", "unknown", "rejected"}:
        # PAT is renewable independently, unlike either RT. Never retry the RT.
        if pair == "job" and account.get("qoder_auth_method") == "pat" and account.get("qoder_pat"):
            state.pending = auth._exchange(account, account["qoder_pat"], account_key=account_key)
        else:
            raise common.QoderError("refresh", kind="refresh_outcome_unknown_reauthorize")
    if state.pending is None:
        _save(key, {"phase": "intent", "started_at": time.time()})
        try:
            result = (auth.rotate_device_sync(account, account_key=account_key) if pair == "device"
                      else auth.refresh_sync(account, account_key=account_key))
        except Exception as exc:
            safe_unsent = isinstance(exc, common.QoderError) and exc.kind == "network" and not exc.response_status and exc.transport_kind in {
                "connect", "connect_timeout", "proxy", "proxy_route", "dns", "connection_refused", "pool_timeout"}
            _save(key, {"phase": "idle" if safe_unsent else "unknown", "started_at": time.time()})
            state.retry_at = time.monotonic() + 60
            raise
        state.pending = dict(result, **({"last_refresh": auth.utc_text(time.time())} if pair == "job" else {}))
    _save(key, {"phase": "candidate", "candidate": state.pending})
    fields = state.pending
    _persist(account, account_key, pair, fingerprint, fields)
    state.pending = None
    state.retry_at = 0
    # Keep the journal under the old credential hash. Config rollback cannot
    # make the consumed RT appear unused; the new pair has its own empty key.
    return fields


def refresh_locked(account: dict, account_key: str, force: bool, *, reuse_current: bool = False) -> str:
    common.require_refresh()
    phase = refresh_status(account, account_key)
    state = _refresh_states.get(account_key)
    pending = phase == "candidate" or bool(state and state.fingerprint == credential_fingerprint(account) and state.pending)
    if not pending and phase not in {"intent", "unknown", "rejected", "store_unavailable"}:
        if reuse_current:
            return account["access_token"]
        if not force and not _expired(account, 600):
            return account["access_token"]
        if not force and state and state.retry_at > time.monotonic():
            if not _expired(account):
                return account["access_token"]
            raise common.QoderError("refresh", kind="backoff")
    return _rotate(account, account_key, "job")["access_token"]


def rotate_device(account_key: str, refused_token: str) -> dict:
    from ... import oauth_manager
    common.require_refresh()
    account = oauth_manager.get_account(account_key)
    if not account:
        raise common.QoderError("device_refresh", kind="stale_generation")
    with oauth_manager._get_refresh_lock(oauth_manager.account_state_key(account)):
        account = copy.deepcopy(oauth_manager.get_account(account_key) or {})
        if not account:
            raise common.QoderError("device_refresh", kind="stale_generation")
        if account.get("device_token") and account["device_token"] != refused_token:
            return account
        if account.get("qoder_device_chat"):
            refresh_locked(account, account_key, True)
            return copy.deepcopy(oauth_manager.get_account(account_key) or {})
        return dict(account, **_rotate(account, account_key, "device"))
