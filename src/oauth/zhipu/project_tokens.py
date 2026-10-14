"""Project access tokens: process-only cache, conditional rejection and login CAS.

Only Key locations are persisted. No API here grants permission to create a Key.
"""
from __future__ import annotations

import copy
from concurrent.futures import Future
from contextlib import nullcontext
import hashlib
import json
import math
import random
import threading
import time
from urllib.parse import quote

from . import common as c

_lock = threading.RLock()
_entries = {}
_locations = {}
_business_locks = {}
_business_tokens = {}


def token_fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()


def scope(account):
    return token_fingerprint(json.dumps([c.BIZ_ORIGINS[c.site_of(account)], c.identity(account), account.get("plan_scope"),
        account.get("generationId"), account.get("access_token"), account.get("zhipu_oauth_token"),
        account.get("zcode_token")]))


def assert_live(account, account_key, key_id=None):
    if not account_key:
        return
    from ... import oauth_manager
    current = oauth_manager.get_account(account_key)
    if (not current or scope(current) != scope(account)
            or key_id and current.get("project_key_id") not in (None, "", key_id)):
        raise c.ZhipuError("project_token", "scope_invalidated")


def _save_business(account, account_key, token):
    if account_key:
        from ... import oauth_manager
        result = oauth_manager.mutate_account_if_unchanged(account_key, account,
            lambda value: value.update(access_token=token))
        if result.get("status") != "updated":
            raise c.ZhipuError("business_token", "stale_generation")
    account["access_token"] = token


def business_request(account, url, *, account_key="", header_kind="business", recovery_budget=None, **kwargs):
    """A confirmed biz 401 may exchange the saved OAuth source once, never a PAT/JWT.

    Reads with a stale business token share the saved replacement under a lock.
    Writes are only replayed following a definitive 401, never uncertain results.
    """
    def send():
        headers = c.biz_headers(account)
        token = c.text(account.get("access_token"), "access_token", required=True)
        if token.startswith("Bearer "):
            token = token[7:].strip()
        if header_kind == "customer":
            headers = {"Authorization": token}
        elif header_kind == "keys":
            headers["Authorization"] = ("Bearer " if c.site_of(account) == "zai" and account.get("plan_scope") != "team" else "") + token
        elif header_kind == "pat":
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        return c.request(url, headers=headers, account_key=account_key, **kwargs)

    try:
        return send()
    except c.ZhipuError as exc:
        if exc.status_code != 401 or "/api/biz/" not in url or c.site_of(account) != "zai" or not account.get("zhipu_oauth_token"):
            raise
        if recovery_budget is not None:
            if recovery_budget["remaining"] <= 0:
                raise
            recovery_budget["remaining"] -= 1
    old = copy.deepcopy(account)
    lock_key = (account_key, scope(old))
    with _lock:
        lock = _business_locks.setdefault(lock_key, threading.Lock())
    with lock:
        if account_key:
            from ... import oauth_manager
            current = oauth_manager.get_account(account_key)
            # A peer may have exchanged this same source, but a new login must
            # never be overwritten or borrowed by the old request.
            comparable = dict(current or {}, access_token=old.get("access_token"))
            if not current or scope(comparable) != scope(old):
                raise c.ZhipuError("business_token", "stale_generation")
            if current.get("access_token") != old.get("access_token"):
                if current.get("access_token") != _business_tokens.get(lock_key):
                    raise c.ZhipuError("business_token", "stale_generation")
                # Adopt only the proven credential rotation. In particular,
                # keep the caller's authorization/pause snapshot for its CAS.
                account["access_token"] = current["access_token"]
            else:
                data = c.request(c.BIZ_ORIGINS["zai"] + "/api/auth/z/login", method="POST",
                    body={"token": old["zhipu_oauth_token"]}, account_key=account_key, stage="business_token")
                token = c.text((data or {}).get("access_token"), "access_token", required=True)
                _save_business(account, account_key, token)
                _business_tokens[lock_key] = token
        else:
            token = _business_tokens.get(lock_key)
            if token is None:
                data = c.request(c.BIZ_ORIGINS["zai"] + "/api/auth/z/login", method="POST",
                    body={"token": old["zhipu_oauth_token"]}, stage="business_token")
                token = c.text((data or {}).get("access_token"), "access_token", required=True)
                _business_tokens[lock_key] = token
            _save_business(account, account_key, token)
    return send()  # Exactly one retry; a second 401 propagates.


def location_id(account):
    with _lock:
        return _locations.get(scope(account), account.get("project_key_id"))


def _drop_location(account, account_key, key):
    with _lock:
        _locations[key] = None  # Tombstone also defeats an old caller's snapshot.
    if account_key:
        from ... import oauth_manager
        current = copy.deepcopy(oauth_manager.get_account(account_key))
        if current and scope(current) == scope(account) and current.get("project_key_id"):
            result = oauth_manager.mutate_account_if_unchanged(account_key, current,
                lambda value: value.pop("project_key_id", None))
            if result.get("status") == "updated":
                account.pop("project_key_id", None)


def _material(data, started):
    if not isinstance(data, dict) or data.get("success") is False or type(data.get("code")) not in (int, str) or str(data.get("code")) not in {"0", "200"}:
        raise c.ZhipuError("project_token", "invalid_response")
    value = data.get("data")
    if not isinstance(value, dict):
        raise c.ZhipuError("project_token", "invalid_response")
    if value.get("enable", True) is False:
        raise c.ZhipuError("project_token", "disabled")
    if value.get("enable", True) is not True or value.get("tokenType") != "Bearer":
        raise c.ZhipuError("project_token", "invalid_response")
    try:
        token = c.text(value.get("accessToken"), "accessToken", required=True)
        for field in ("expiresIn", "expiresAt"):
            v = value.get(field)
            if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
                raise ValueError()
        expires = min(value["expiresAt"] * 1000, started + value["expiresIn"] * 1000)
        if expires <= time.time() * 1000:
            raise ValueError()
    except ValueError:
        raise c.ZhipuError("project_token", "invalid_response") from None
    return {"token": token, "fingerprint": token_fingerprint(token), "expires": expires,
            "refresh": expires - 120000 - random.random() * 60000}


def _flight_token(account, account_key, entry, result, rejected_fingerprint):
    """Consume a completed flight without borrowing a replacement login/state.

    The leader supplies the exact resulting credential scope, not a new account
    snapshot. A newer PAT in that same entry is usable; an invalidated/expired
    result fails closed without recursively starting another issuance.
    """
    from ... import config
    candidate = dict(account, access_token=result["access_token"])
    if scope(candidate) != result["scope"]:
        raise c.ZhipuError("project_token", "scope_invalidated")
    with config.serialized_updates() if account_key else nullcontext():
        assert_live(candidate, account_key, result["key_id"])
        with _lock:
            if _entries.get(result["scope"]) is not entry:
                raise c.ZhipuError("project_token", "scope_invalidated")
            value = entry.get("value")
            if value and value["fingerprint"] == rejected_fingerprint:
                entry.pop("value", None)
                value = None
            if not value or time.time() * 1000 >= value["expires"]:
                raise c.ZhipuError("project_token", "scope_invalidated")
            account["access_token"] = result["access_token"]
            return value["token"]


def resolve(account, *, account_key, location, on_stage=None, on_key=None,
            rejected_fingerprint=None, force=False, known_key_id=None, recovery_budget=None):
    from ... import config
    with config.serialized_updates() if account_key else nullcontext():
        assert_live(account, account_key)
        key = scope(account)
        with _lock:
            if account_key:
                for previous, cached in list(_entries.items()):
                    if previous != key and cached.get("account_key") == account_key:
                        _entries.pop(previous, None)
                        _locations.pop(previous, None)
            existing = _entries.get(key)
            persisted = account.get("project_key_id")
            if existing and persisted and existing.get("key_id") and existing["key_id"] != persisted:
                _entries.pop(key, None)
                _locations[key] = persisted
            entry = _entries.setdefault(key, {"account_key": account_key})
            old = entry.get("value")
            if old and rejected_fingerprint == old["fingerprint"]:
                entry.pop("value", None)
                old = None
            pending = entry.get("pending")
            if pending is None and old and not force and time.time() * 1000 < min(old["refresh"], old["expires"]):
                if on_key:
                    on_key(location_id(account), False)
                return old["token"]
            leader = pending is None
            if leader:
                pending = entry["pending"] = Future()
    if not leader:
        result = pending.result()
        return _flight_token(account, account_key, entry, result, rejected_fingerprint)
    try:
        with _lock:
            invalidated_location = key in _locations and _locations[key] is None
        key_id = (None if invalidated_location else known_key_id) or location_id(account)
        if not key_id:
            key_id = location()
        elif on_key:
            on_key(key_id, False)
        with _lock:
            _locations[key] = key_id
            entry["key_id"] = key_id
        if on_stage:
            on_stage("project_token")
        from .auth import key_path
        url = c.BIZ_ORIGINS[c.site_of(account)] + key_path(account) + "/" + quote(key_id, safe="") + "/access_tokens"
        started = time.time() * 1000
        try:
            data = business_request(account, url, account_key=account_key, header_kind="pat", method="POST",
                body={"clientType": "zcode", "clientVersion": c.VERSION}, envelope=False, stage="project_token",
                recovery_budget=recovery_budget)
        except c.ZhipuError as exc:
            if exc.status_code == 404:
                _drop_location(account, account_key, key)
            raise
        value = _material(data, started)
        if value["fingerprint"] == rejected_fingerprint:
            raise c.ZhipuError("project_token", "scope_invalidated")
        with config.serialized_updates() if account_key else nullcontext():
            assert_live(account, account_key, key_id)
            with _lock:
                if _entries.get(key) is not entry:
                    raise c.ZhipuError("project_token", "scope_invalidated")
                # The business exchange may have advanced the login token under CAS.
                current_key = scope(account)
                if current_key != key:
                    if current_key in _entries and _entries[current_key] is not entry:
                        raise c.ZhipuError("project_token", "scope_invalidated")
                    _entries.pop(key, None)
                    _entries[current_key] = entry
                    _locations.pop(key, None)
                    _locations[current_key] = key_id
                entry["value"] = value
                result = {"scope": current_key, "access_token": account["access_token"], "key_id": key_id}
    except BaseException as exc:
        try:
            assert_live(account, account_key)
        except c.ZhipuError as stale:
            exc = stale
        with _lock:
            if _entries.get(key) is not entry:
                exc = c.ZhipuError("project_token", "scope_invalidated")
            transient = isinstance(exc, c.ZhipuError) and (exc.kind in {"network", "timeout"} or exc.status_code == 429 or exc.status_code >= 500)
            now = time.time() * 1000
            # A peer can reject the old token while this refresh is in flight.
            deferred = transient and old and scope(account) == key and entry.get("value") is old and now < old["expires"]
            if deferred:
                old["refresh"] = min(now + 5000, old["expires"])
                result = {"scope": key, "access_token": account["access_token"], "key_id": entry["key_id"]}
            elif _entries.get(key) is entry:
                _entries.pop(key, None)
        if not deferred:
            pending.set_exception(exc)
            raise exc
    finally:
        with _lock:
            # Detach before waking consumers. A rejection can immediately start
            # a new flight, which this completion must never clear or reuse.
            if entry.get("pending") is pending:
                entry.pop("pending", None)
    pending.set_result(result)
    return _flight_token(account, account_key, entry, result, rejected_fingerprint)


def cached_token(account):
    """Read-only use for the explicit no-refresh operator switch; never legacy Key."""
    with _lock:
        value = (_entries.get(scope(account)) or {}).get("value")
        return value["token"] if value and time.time() * 1000 < value["expires"] else None


def forget(account_key):
    with _lock:
        for key, entry in list(_entries.items()):
            if entry.get("account_key") == account_key:
                _entries.pop(key, None)
                _locations.pop(key, None)
        for key in list(_business_locks):
            if key[0] == account_key:
                _business_locks.pop(key, None)
                _business_tokens.pop(key, None)
