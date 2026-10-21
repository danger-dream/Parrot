"""Qoder device-flow / PAT login, stored credential normalization and refresh."""
from __future__ import annotations

import base64
import copy
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode

from . import common, device_profile
from ...safe_diagnostics import diagnose

ACCOUNT_FIELDS = (
    "realm", "uid", "nickname", "label", "machine_id", "device_token", "device_refresh_token",
    "qoder_device_chat", "qoder_auth_method", "qoder_pat", "qoder_identity_source",
    "qoder_auto_checkin", "qoder_relogin_required", "qoder_device_profile",
)
DAY = 24 * 60 * 60
PAT_PREFIX = "pt-"


def utc_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def identity(account: dict) -> str:
    realm = common.realm_of(account)
    uid = common.text(account.get("uid"), "uid", required=True)
    return f"{realm}:{quote(uid, safe='')}"


def _date(value) -> float | None:
    """An ISO 8601 or HTTP (RFC 2822) date, as Qoder's client parses it; UTC when it names no zone."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def job_expiry(data: dict, acquired_at: float) -> str:
    """A job token's expires_in is milliseconds; a day when Qoder names none."""
    millis = common.number(data.get("expires_in"))
    return utc_text(acquired_at + (millis / 1000 if millis else DAY))


def device_expiry(data: dict, acquired_at: float) -> str:
    """A device token's expires_at (a date) or expires_in (seconds), as Qoder's CLI reads it."""
    at = _date(data.get("expires_at"))
    if at is not None:
        return utc_text(at)
    seconds = common.number(data.get("expires_in"))
    return utc_text(acquired_at + (seconds if seconds else DAY))


def exchange_expiry(data: dict, acquired_at: float) -> str:
    """A PAT exchange's expire_time/expires_at (a date), else expires_in (milliseconds)."""
    for key in ("expire_time", "expires_at"):
        at = _date(data.get(key))
        if at is not None:
            return utc_text(at)
    millis = common.number(data.get("expires_in"))
    return utc_text(acquired_at + (millis / 1000 if millis else DAY))


def _token(data, *keys) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return common.text(value, "token", required=True, maximum=32768)
    return ""


def normalize_credential(value: dict, *, source: str = "import") -> dict:
    if not isinstance(value, dict):
        raise ValueError("Qoder credential must be an object")
    if value.get("provider", value.get("type", "qoder")) not in ("qoder", None, ""):
        raise ValueError("Credential belongs to another provider")
    method = value.get("qoder_auth_method") or "device"
    if method not in {"device", "pat"}:
        raise ValueError("Unsupported Qoder auth method")
    entry = {
        "provider": "qoder", "type": "qoder",
        "realm": common.realm_of(value),
        "uid": common.text(value.get("uid"), "uid", required=True),
        "email": common.text(value.get("email"), "email"),
        "nickname": common.text(value.get("nickname"), "nickname"),
        "access_token": common.text(value.get("access_token"), "access_token", required=True, maximum=32768),
        "refresh_token": common.text(value.get("refresh_token"), "refresh_token", maximum=32768),
        "expired": common.text(value.get("expired"), "expired", maximum=64),
        "machine_id": common.text(value.get("machine_id") or str(uuid.uuid4()), "machine_id", required=True, maximum=64),
        "device_token": common.text(value.get("device_token"), "device_token", maximum=32768),
        "device_refresh_token": common.text(value.get("device_refresh_token"), "device_refresh_token", maximum=32768),
        "qoder_device_chat": value.get("qoder_device_chat") is True,
        "qoder_auth_method": method,
        "qoder_identity_source": source,
    }
    if value.get("qoder_device_profile") is not None:
        device_profile.validate(value["qoder_device_profile"])
        if entry["machine_id"] != value["qoder_device_profile"]["machine_id"]:
            raise ValueError("Qoder device binding mismatch")
        entry["qoder_device_profile"] = copy.deepcopy(value["qoder_device_profile"])
    if method == "pat":
        if entry["realm"] != "global":
            raise ValueError("Qoder PAT sign-in is international only")
        entry["qoder_pat"] = common.text(value.get("qoder_pat"), "PAT", required=True, maximum=4096)
    # A device sign-in without a refresh token is kept: it serves until it
    # expires, then its refresh is a missing_refresh_token re-login.
    if entry["qoder_device_chat"] and (entry["realm"] != "cn" or entry["device_token"] != entry["access_token"]):
        raise ValueError("Qoder device-token chat is a Qoder CN device credential")
    entry["label"] = common.text(value.get("label"), "label") or entry["email"] or entry["nickname"] or entry["uid"]
    if source == "login":
        entry["last_refresh"] = utc_text(time.time())
    identity(entry)
    return entry


def _userinfo(account: dict, token: str, *, strict=False) -> dict:
    try:
        status, data = common.openapi(account, "/api/v1/userinfo", token=token, stage="userinfo", timeout=15)
    except common.QoderError:
        if strict:
            raise
        return {}
    if strict and (status != 200 or not isinstance(data, dict) or not any(data.get(k) for k in ("id", "user_id", "uid"))):
        raise common.QoderError("userinfo", status=status, code=common.error_code(data), kind="identity_unavailable")
    return data if 200 <= status < 300 and isinstance(data, dict) else {}


@diagnose("qoder", "login_init", "authorize_url")
def start_login_sync(*, realm: str = "global", profile: dict) -> dict:
    """A PKCE device flow on Qoder's account page; nothing is sent until the poll."""
    site = common.SITES[common.realm_of({"realm": realm})]
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    device_profile.validate(profile)
    nonce, machine_id = str(uuid.uuid4()), profile["machine_id"]
    query = {"challenge": challenge, "challenge_method": "S256", "nonce": nonce,
             "machine_id": machine_id, "client_id": site["client_id"]}
    if site["redirect"]:
        query["redirect_uri"] = site["redirect"]
    return {"realm": realm, "verifier": verifier, "nonce": nonce, "machine_id": machine_id,
            "qoder_device_profile": copy.deepcopy(profile),
            "auth_url": f"{site['web']}/device/selectAccounts?{urlencode(query)}", "status": "pending"}


@diagnose("qoder", "login_poll", "device_poll")
def poll_login_sync(payload: dict) -> dict:
    """One poll; keeps what it obtained so a later step's retry never repeats an exchange.

    Caller serializes access to the flow. The returned entry is secret and must
    stay in the flow/control boundary.
    """
    if payload.get("status") == "ready":
        return copy.deepcopy(payload["entry"])
    account = {"realm": payload["realm"]}
    site = common.site_of(account)
    if "device" not in payload:
        status, data = common.openapi(account, "/api/v1/deviceToken/poll", stage="device_poll",
                                      query={"nonce": payload["nonce"], "verifier": payload["verifier"], "challenge_method": "S256"})
        token = _token(data, "token") if status == 200 else ""
        if not token:
            payload["status"] = "pending"
            return {}
        payload["device"] = {"token": token, "refresh": _token(data, "refresh_token"),
                             "expired": device_expiry(data, time.time()),
                             "uid": common.text(data.get("user_id"), "uid"), "name": common.text(data.get("user_name"), "nickname")}
        payload["status"] = "identity_pending"
    device = payload["device"]
    if "chat" not in payload:
        status, data = common.openapi(account, "/api/v1/me/jobToken", method="POST", token=device["token"],
                                      body={"clientId": site["client_id"]}, stage="job_token")
        token = _token(data, "token") if status == 200 else ""
        if token:
            payload["chat"] = {"access_token": token, "refresh_token": _token(data, "refresh_token"),
                               "expired": job_expiry(data, time.time())}
        elif (site["device_chat"] and status not in (401, 429)
              and common.error_code(data) != "105"
              and str((data or {}).get("message", "") if isinstance(data, dict) else "").strip().lower() != "login expired"):
            # A jobToken failure is never a success signal. Retain the already
            # authorized device candidate and require independent model access.
            from . import catalog
            candidate = {"realm": payload["realm"], "access_token": device["token"],
                         "machine_id": payload["machine_id"], "qoder_device_profile": payload["qoder_device_profile"]}
            catalog.fetch_models_sync(candidate)
            payload["chat"] = {"access_token": device["token"], "refresh_token": device["refresh"],
                               "expired": device["expired"], "qoder_device_chat": True}
        else:
            raise common.QoderError("job_token", status=status, code=common.error_code(data),
                                    kind="invalid_response" if status == 200 else "upstream")
    info = _userinfo(account, device["token"])
    entry = normalize_credential({
        **payload["chat"], "realm": payload["realm"], "machine_id": payload["machine_id"],
        "qoder_device_profile": payload["qoder_device_profile"],
        "uid": device["uid"] or str(info.get("id") or info.get("user_id") or info.get("uid") or ""),
        "email": str(info.get("email") or ""), "nickname": str(info.get("name") or device["name"] or ""),
        "device_token": device["token"], "device_refresh_token": device["refresh"], "qoder_auth_method": "device",
    }, source="login")
    payload.update(status="ready", entry=entry)
    payload.pop("device", None)
    payload.pop("chat", None)
    return copy.deepcopy(entry)


def _exchange(account: dict, pat: str, *, account_key: str = "") -> dict:
    status, data = common.openapi(account, "/api/v1/jobToken/exchange", method="POST",
                                  body={"personal_token": pat}, account_key=account_key, stage="pat_exchange")
    if status in (400, 401, 403):
        raise common.QoderError("pat_exchange", status=status, code=common.error_code(data), kind="refresh_rejected")
    token = _token(data, "token", "device_token", "access_token") if status == 200 else ""
    if not token:
        raise common.QoderError("pat_exchange", status=status, code=common.error_code(data),
                                kind="invalid_response" if status == 200 else "upstream")
    return {"access_token": token, "refresh_token": _token(data, "refresh_token", "refreshToken"),
            "expired": exchange_expiry(data, time.time())}


@diagnose("qoder", "login_pat", "pat_exchange")
def pat_login_sync(pat: str, *, realm: str = "global", profile: dict, pending: dict | None = None) -> dict:
    pat = common.text(pat, "PAT", required=True, maximum=4096)
    if realm != "global":
        raise ValueError("Qoder PAT sign-in is international only")
    if not pat.startswith(PAT_PREFIX):
        raise ValueError("Qoder PAT must start with pt-")
    device_profile.validate(profile)
    account = {"realm": realm, "machine_id": profile["machine_id"], "qoder_device_profile": profile}
    pending = pending if pending is not None else {}
    if "pat_candidate" not in pending:
        pending["pat_candidate"] = {"tokens": _exchange(account, pat), "pat": pat}
        pending["status"] = "identity_pending"
    candidate = pending["pat_candidate"]
    if candidate["pat"] != pat:
        raise ValueError("PAT differs from the confirmed exchange")
    tokens = candidate["tokens"]
    pending["status"] = "identity_pending"
    info = _userinfo(account, tokens["access_token"], strict=True)
    return normalize_credential({
        **tokens, **account, "uid": str(info.get("id") or info.get("user_id") or info.get("uid") or ""),
        "email": str(info.get("email") or ""), "nickname": str(info.get("name") or ""),
        "qoder_auth_method": "pat", "qoder_pat": pat,
    }, source="login")


def _device_pair(account: dict, refresh: str, *, account_key: str, stage: str) -> dict:
    status, data = common.openapi(account, "/api/v1/deviceToken/refresh", method="POST",
                                  body={"refresh_token": refresh}, account_key=account_key, stage=stage)
    if status in (401, 403):
        raise common.QoderError(stage, status=status, code=common.error_code(data),
                                kind="refresh_rejected" if account.get("qoder_device_chat") else "device_rejected")
    if status != 200:
        raise common.QoderError(stage, status=status, code=common.error_code(data))
    token = _token(data, "token", "device_token")
    new_refresh = _token(data, "refresh_token")
    if not token or not new_refresh:
        raise common.QoderError(stage, status=status, kind="invalid_response")
    return {"device_token": token, "device_refresh_token": new_refresh, "expired": device_expiry(data, time.time())}


@diagnose("qoder", "refresh", "token_refresh")
def refresh_sync(account: dict, *, account_key: str = "") -> dict:
    """Spend the chat refresh token once; the caller must persist the result immediately."""
    common.require_refresh()
    refresh = common.text(account.get("refresh_token"), "refresh_token", maximum=32768)
    if account.get("qoder_device_chat"):
        if not refresh:
            raise common.QoderError("refresh", kind="missing_refresh_token")
        pair = _device_pair(account, refresh, account_key=account_key, stage="refresh")
        return {"access_token": pair["device_token"], "refresh_token": pair["device_refresh_token"],
                "expired": pair["expired"], "device_token": pair["device_token"],
                "device_refresh_token": pair["device_refresh_token"]}
    pat = account.get("qoder_pat") if account.get("qoder_auth_method") == "pat" else ""
    if refresh:
        status, data = common.openapi(account, "/api/v1/jobToken/refresh", method="POST",
                                      body={"refresh_token": refresh}, account_key=account_key, stage="refresh")
        if status == 200:
            token, new_refresh = _token(data, "token"), _token(data, "refresh_token")
            if not token or not new_refresh:
                raise common.QoderError("refresh", status=status, kind="invalid_response")
            return {"access_token": token, "refresh_token": new_refresh, "expired": job_expiry(data, time.time())}
        if status not in (401, 403) or not pat:
            raise common.QoderError("refresh", status=status, code=common.error_code(data),
                                    kind="refresh_rejected" if status in (401, 403) else "upstream")
    elif not pat:
        raise common.QoderError("refresh", kind="missing_refresh_token")
    # A PAT lasts up to a year, the job refresh two days: exchange it again.
    return _exchange(account, pat, account_key=account_key)


@diagnose("qoder", "device_refresh", "device_refresh")
def rotate_device_sync(account: dict, *, account_key: str = "") -> dict:
    """Rotate the device token Qoder refused; on a device-chat account it is the chat pair too."""
    common.require_refresh()
    refresh = common.text(account.get("device_refresh_token"), "device_refresh_token", maximum=32768)
    if not refresh:
        raise common.QoderError("device_refresh", kind="missing_refresh_token" if account.get("qoder_device_chat") else "device_rejected")
    pair = _device_pair(account, refresh, account_key=account_key, stage="device_refresh")
    fields = {"device_token": pair["device_token"], "device_refresh_token": pair["device_refresh_token"]}
    if account.get("qoder_device_chat"):
        fields.update(access_token=pair["device_token"], refresh_token=pair["device_refresh_token"], expired=pair["expired"])
    return fields
