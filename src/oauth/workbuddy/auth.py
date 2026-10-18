"""WorkBuddy browser login, stored credential normalization and refresh."""
from __future__ import annotations

import copy
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, quote

from . import common
from ...safe_diagnostics import diagnose, report, scope

ACCOUNT_FIELDS = (
    "realm", "uid", "enterprise_id", "domain", "nickname", "label",
    "workbuddy_client_profile", "workbuddy_identity_source", "workbuddy_auto_checkin",
    "refresh_expired", "auth_method", "id_source", "department_full_name",
)


def utc_text(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def identity(account: dict) -> str:
    realm = common.realm_of(account)
    uid = common.text(account.get("uid"), "uid", required=True)
    enterprise = common.text(account.get("enterprise_id"), "enterprise_id")
    scope = "e." + quote(enterprise, safe="") if enterprise else "p"
    identity = f"{realm}:{quote(uid, safe='')}:{scope}"
    # New route gets a new namespace; every pre-existing CN/IDE/legacy key stays
    # byte-for-byte stable. Same UID is not permission to replace an IDE token.
    if common.profile_of(account) == common.WORKBUDDY_CLI_PROFILE:
        identity += ":workbuddy-cli"
    return identity


def _expiry(raw, *, milliseconds: bool = False) -> str:
    if raw is None or raw == "":
        return ""
    try:
        if isinstance(raw, (float, int)) and not isinstance(raw, bool):
            if raw <= 0 and not milliseconds:
                return ""  # Preserve the pre-existing snake-case import contract.
            return utc_text(float(raw) / (1000 if milliseconds else 1))
        if isinstance(raw, str):
            if raw.strip().replace(".", "", 1).isdigit():
                return utc_text(float(raw) / (1000 if milliseconds else 1))
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if value.tzinfo is None:
                raise ValueError("timezone required")
            return utc_text(value.timestamp())
    except (ValueError, OverflowError, OSError):
        pass
    raise ValueError("Invalid WorkBuddy absolute token expiry")


def token_expiries(value: dict, *, acquired_at: float | None = None, strict: bool = True) -> dict:
    """Convert wire epoch-ms / relative-seconds into Parrot UTC fields.

    Existing snake-case absolute fields retain their seconds/ISO import contract.
    Relative lifetimes need an actual acquisition time, never the import time.
    Refresh metadata may be unknown, but must not discard already rotated tokens.
    """
    result = {}
    for field, snake, camel, relative in (
        ("expired", "expires_at", "expiresAt", "expiresIn"),
        ("refresh_expired", "refresh_expires_at", "refreshExpiresAt", "refreshExpiresIn"),
    ):
        for key in (field, snake, camel):
            if value.get(key) is not None and value[key] != "":
                try:
                    result[field] = _expiry(value[key], milliseconds=key == camel)
                except ValueError:
                    if strict:
                        raise
                    result[field] = ""  # Invalid absolute data is not a relative lifetime.
                break
        else:
            if acquired_at is not None:
                seconds = common.number(value.get(relative))
                if seconds is not None:
                    try:
                        result[field] = utc_text(acquired_at + seconds)
                    except (ValueError, OverflowError, OSError):
                        if strict:
                            raise ValueError("Invalid WorkBuddy relative token expiry") from None
                        result[field] = ""
    return result


def normalize_credential(value: dict, *, realm: str | None = None, source: str = "import") -> dict:
    if not isinstance(value, dict):
        raise ValueError("WorkBuddy credential must be an object")
    if value.get("provider", value.get("type", "workbuddy")) not in ("workbuddy", None, ""):
        raise ValueError("Credential belongs to another provider")
    auth = value.get("auth", value)
    account = value.get("account", value)
    if not isinstance(auth, dict) or not isinstance(account, dict):
        raise ValueError("Invalid WorkBuddy credential sections")
    declared = realm or value.get("realm") or auth.get("realm")
    if realm and value.get("realm") and realm != value["realm"]:
        raise ValueError("WorkBuddy import region mismatch")
    entry = {
        "provider": "workbuddy", "type": "workbuddy",
        "access_token": common.text(auth.get("access_token", auth.get("accessToken")), "access_token", required=True, maximum=32768),
        "refresh_token": common.text(auth.get("refresh_token", auth.get("refreshToken")), "refresh_token", required=True, maximum=32768),
        "uid": common.text(account.get("uid"), "uid", required=True),
        "enterprise_id": common.text(account.get("enterprise_id", account.get("enterpriseId")), "enterprise_id"),
        "domain": common.text(auth.get("domain", value.get("domain")), "domain"),
        "realm": declared or "",
        "nickname": common.text(account.get("nickname"), "nickname"),
        "email": common.text(account.get("email"), "email"),
        "workbuddy_client_profile": value.get("workbuddy_client_profile") or common.PROFILE,
        "workbuddy_identity_source": source,
    }
    entry["realm"] = common.realm_of(entry)
    entry["label"] = common.text(value.get("label"), "label") or entry["nickname"] or entry["uid"]
    # Copied files have no acquisition time, even if their source label says login.
    entry.update(token_expiries(auth))
    entry.setdefault("expired", "")
    entry.setdefault("refresh_expired", "")
    for field, raw, maximum in (
        ("auth_method", auth.get("auth_method", auth.get("method")), 256),
        ("id_source", account.get("id_source", account.get("idSource")), 256),
        ("department_full_name", account.get("department_full_name", account.get("departmentFullName")), 2048),
    ):
        if raw is not None:
            entry[field] = common.text(raw, field, maximum=maximum)
    if source == "login":
        entry["last_refresh"] = utc_text(time.time())
    identity(entry)  # Validate every identity component before publication.
    return entry


@diagnose("workbuddy", "login_init", "state_request")
def start_login_sync(*, realm: str = "cn", client_profile: str | None = None) -> dict:
    stage = "profile"
    try:
        profile = common.login_profile(realm, client_profile)
        cookies: dict = {}
        account = {"realm": realm, "workbuddy_client_profile": profile}
        platform = "ide" if profile == common.GLOBAL_PROFILE else "CLI"
        stage = "state_request"
        data = common.request(account, "/v2/plugin/auth/state?" + urlencode({"platform": platform}), body={},
                              kind="login_state", cookies=cookies)
        stage = "state_validation"
        state = common.text(data.get("state"), "state", required=True, maximum=4096)
        stage = "authorization_url_validation"
        auth_url = common.safe_auth_url(data.get("authUrl"), realm=realm)
        return {**account, "state": state, "auth_url": auth_url, "cookies": cookies, "status": "pending"}
    except Exception as exc:
        try:
            if stage in {"state_validation", "authorization_url_validation"}:
                exc.response_status = 200  # common.request returned a validated HTTP-200 envelope.
            report(exc, provider="workbuddy", operation="login_init", stage=stage)
        except Exception:
            pass  # Diagnostics must never replace the original failure or retry IO.
        raise


@diagnose("workbuddy", "login_poll", "token_poll")
def poll_login_sync(payload: dict) -> dict:
    """One poll; retains obtained credentials when account lookup needs a retry.

    Caller serializes access to the flow. The returned entry is secret and must
    stay in the flow/control boundary, not a public response or audit payload.
    """
    if payload.get("status") == "ready":
        return copy.deepcopy(payload["entry"])
    account = {"realm": payload["realm"], "workbuddy_client_profile": common.login_profile(
        payload["realm"], payload.get("workbuddy_client_profile"),
    )}
    query = urlencode({"state": payload["state"]})
    cookies = payload.setdefault("cookies", {})
    if "token" not in payload:
        token = common.request(account, "/v2/plugin/auth/token?" + query, method="GET",
                               kind="login_token", cookies=cookies, pending=True)
        if token is None or not token.get("accessToken"):
            payload["status"] = "pending"
            return {}
        common.text(token.get("accessToken"), "access_token", required=True, maximum=32768)
        token = copy.deepcopy(token)
        payload["token"] = token
        payload["status"] = "identity_pending"
        token.update(token_expiries(token, acquired_at=time.time()))
    token = payload["token"]
    # The account store still requires renewable credentials. Keep a partial
    # token in this flow rather than requesting it again or erasing an old RT.
    common.text(token.get("refreshToken"), "refresh_token", required=True, maximum=32768)
    auth_account = dict(account, access_token=token["accessToken"], domain=token.get("domain") or "")
    common.realm_of(auth_account)  # Never send credentials across regions.
    with scope(stage="profile_fetch"):
        try:
            info = common.request(auth_account, "/v2/plugin/login/account?" + query, method="GET",
                                  kind="login_account", cookies=cookies)
        except common.WorkBuddyError as exc:
            if exc.code != 12151:
                raise
            return {}  # Identity is not ready; never repeat the token exchange.
    entry = normalize_credential({"auth": token, "account": info, **account}, source="login")
    payload.update(status="ready", entry=entry)
    payload.pop("token", None)
    return copy.deepcopy(entry)


@diagnose("workbuddy", "refresh", "token_refresh")
def refresh_sync(refresh_token: str, *, account: dict, account_key: str = "") -> dict:
    common.require_refresh()
    refresh_token = common.text(refresh_token, "refresh_token", maximum=32768)
    if not refresh_token:
        raise common.WorkBuddyError("refresh", kind="missing_refresh_token")
    refresh_expired = token_expiries(account).get("refresh_expired")
    if refresh_expired and datetime.fromisoformat(refresh_expired.replace("Z", "+00:00")).timestamp() <= time.time():
        raise common.WorkBuddyError("refresh", kind="refresh_token_expired")
    # The legacy Global CLI keeps its original empty body and authentication.
    options = {} if common.is_legacy(account) else {"body": {}}
    data = common.request(dict(account, refresh_token=refresh_token), "/v2/plugin/auth/token/refresh",
                          kind="refresh", account_key=account_key, **options)
    result = {"access_token": common.text(data.get("accessToken"), "access_token", required=True, maximum=32768)}
    if data.get("refreshToken"):
        result["refresh_token"] = common.text(data["refreshToken"], "refresh_token", required=True, maximum=32768)
    result.update(token_expiries(data, acquired_at=time.time(), strict=False))
    if "method" in data:
        try:
            result["auth_method"] = common.text(data["method"], "auth_method")
        except ValueError:
            pass  # Optional metadata cannot discard an already rotated AT/RT.
    if data.get("domain"):
        candidate = dict(account, domain=data["domain"])
        common.realm_of(candidate)
        result["domain"] = common.text(data["domain"], "domain")
    return result
