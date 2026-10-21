"""Account device identity provider shared by model/chat and account operations.

Profile bindings are config data. UMID tickets are private durable state, keyed
by profile AND selected route configuration, never by a host-wide identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import ssl
import threading
import time
from collections import OrderedDict

from ... import config, network, state_db
from .. import add_context
from . import common, device_profile, device_platforms, device_marks, umid_protocol, umid_http, routes

_LOCKS = [threading.Lock() for _ in range(64)]
_pending: OrderedDict[str, dict] = OrderedDict()
_guard = threading.Lock()
TTL = 3600  # Local reuse policy, not an assertion about upstream validity.
BACKOFF = 60
HOSTS = {"default": "pum.m.alibaba.com", "sg": "sg-pum.alibabachengdun.com",
         "us": "us-pum.alibabachengdun.com"}


def route_selection(account_key: str = ""):
    return routes.selection(account_key)


def _binding(account, account_key):
    profile = account.get("qoder_device_profile")
    if not profile:
        raise common.QoderError("device_identity", kind="device_initialization_required")
    try:
        digest = device_profile.fingerprint(profile)
        if profile["version"] not in device_profile.CURRENT_VERSIONS:
            raise common.QoderError("device_identity", kind="device_migration_required")
        if profile["machine_id"] != account.get("machine_id"):
            raise ValueError("Inconsistent machine id")
        region = os.environ.get("QODER_UMID_REGION", "default")
        host = HOSTS[region]
        selected = routes.current() or routes.chain(account_key)[0]
        route = selected.fingerprint + ":" + region
        owner = account_key or (str(account.get("realm") or "") + ":" + str(account.get("uid") or "draft"))
        key = hashlib.sha256((owner + ":" + digest + ":" + route + ":marks-v2").encode()).hexdigest()
        return key, host, device_profile.validate(profile)
    except (ValueError, KeyError, TypeError):
        raise common.QoderError("device_identity", kind="invalid_device_profile") from None


def _fetch(host, blocks, account_key):
    if common._network_disabled():
        raise common.QoderError("device_identity", kind="disabled")
    deadline = time.monotonic() + 15
    stream = None
    try:
        body = umid_protocol.build_payload(blocks)
        stream = network.open_sync_stream(host, 443, timeout=15, proxy_purpose="oauth_qoder",
                                          proxy_provider="qoder",
                                          proxy_channel="oauth:" + account_key if account_key else "oauth:qoder:login")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        stream.settimeout(remaining)
        process = umid_protocol.decode_blocks(blocks)[1][8].decode("ascii")
        if device_platforms.for_process(process)["tls"]:
            stream = stream.start_tls(ssl.create_default_context(), server_hostname=host)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            stream.settimeout(remaining)
        stream.sendall(umid_protocol.request_bytes(host, body, process=process))
        return umid_protocol.parse_response(umid_http.read_response(stream, deadline))
    except common.QoderError:
        raise
    except Exception:
        # No raw response, transport URL, device block or ticket in diagnostics.
        raise common.QoderError("device_identity", kind="umid_unavailable") from None
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass  # Closing cannot disclose transport credentials or replace the result.


def summary(account: dict, *, account_key: str = "") -> dict:
    result = device_profile.summary(account)
    if result["status"] != "bound":
        return result
    try:
        key, _host, _blocks = _binding(account, account_key)
        row = state_db.qoder_identity_load(key) or {}
        result["ticket_status"] = ("cached" if row.get("expires", 0) > time.time() else
                                   "backoff" if row.get("retry_at", 0) > time.time() else "not_cached")
    except Exception:
        result["ticket_status"] = "unavailable"
    return result


def headers(account: dict, *, account_key: str = "") -> dict:
    if routes.current() is None:
        # Standalone callers select one member; a business operation owns group retry.
        with routes.bind(routes.chain(account_key)[0]):
            return headers(account, account_key=account_key)
    key, host, blocks = _binding(account, account_key)
    with _LOCKS[int(key[:8], 16) % len(_LOCKS)]:
        now = time.time()
        with _guard:
            row = _pending.get(key)
        needs_save = bool(row and row.get("token"))
        try:
            row = row or state_db.qoder_identity_load(key) or {}
            if row.get("token") and row.get("format_version") != device_marks.VERSION:
                row = {}  # Never reuse pre-fix random triples.
            if any(type(row.get(k, 0)) not in (int, float) for k in ("expires", "retry_at")):
                raise ValueError("Invalid private ticket record")
            if row.get("expires", 0) > now and any(
                    not isinstance(row.get(k), str) or not 1 <= len(row[k]) <= 4096
                    or not all(33 <= ord(c) <= 126 for c in row[k]) for k in ("token", "type", "code")):
                raise ValueError("Invalid private ticket record")
        except Exception:
            raise common.QoderError("device_identity", kind="identity_store_unavailable") from None
        if row.get("expires", 0) <= now:
            if row.get("retry_at", 0) > now:
                raise common.QoderError("device_identity", kind="umid_backoff")
            row = {"retry_at": now + BACKOFF}
            # Persist the intent BEFORE contacting the issuer; failure cannot start a storm.
            try:
                state_db.qoder_identity_save(key, row)
            except Exception:
                raise common.QoderError("device_identity", kind="identity_store_unavailable") from None
            with _guard:
                _pending[key] = row
                while len(_pending) > 512:
                    _pending.popitem(last=False)
            token = _fetch(host, blocks, account_key)
            row = {"token": token, **device_marks.missing_detection(token),
                   "expires": time.time() + TTL, "retry_at": now + BACKOFF}
            with _guard:
                _pending[key] = row
            needs_save = True
        if needs_save:
            try:
                # A failed save is retried with this same issued candidate, never reissued.
                state_db.qoder_identity_save(key, row)
            except Exception:
                raise common.QoderError("device_identity", kind="identity_store_unavailable") from None
            with _guard:
                _pending.pop(key, None)
        return {"Cosy-MachineId": account["machine_id"], "Cosy-MachineToken": row["token"],
                "Cosy-MachineType": row["type"], "Cosy-MachineCode": row["code"],
                "Cosy-MachineOS": account["qoder_device_profile"]["machine_os"]}
