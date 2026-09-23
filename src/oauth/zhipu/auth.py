"""Explicit credential modes; OAuth polling and read-only existing-key resolution."""
from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote, urlparse, parse_qs, urlencode

from . import common as c

ACCOUNT_FIELDS = ("site", "credential_mode", "subject", "label", "model_key", "zcode_token",
                  "organization_id", "project_id", "plan_scope", "entitlement", "management_status")


def normalize_credential(entry):
    entry = copy.deepcopy(entry)
    c.site_of(entry)
    mode = entry.get("credential_mode")
    if mode not in {"api_key", "oauth"}:
        raise ValueError("Zhipu credential_mode must be api_key or oauth")
    scope = entry.get("plan_scope", "personal")
    if scope not in {"personal", "team"}:
        raise ValueError("Zhipu supports personal/team Coding Plan only")
    entry.update(provider="zhipu", type="zhipu", plan_scope=scope)
    for field in (*ACCOUNT_FIELDS, "access_token", "refresh_token", "email"):
        if field in entry and field not in {"entitlement"}:
            entry[field] = c.text(entry[field], field)
    if scope == "team" and not (entry.get("organization_id") and entry.get("project_id")):
        raise ValueError("Zhipu team requires organization_id and project_id")
    if mode == "api_key":
        key = c.text(entry.get("model_key"), "model_key", required=True)
        entry["subject"] = "key-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        for field in ("access_token", "refresh_token", "zcode_token"):
            entry.pop(field, None)
    else:
        c.text(entry.get("subject"), "subject", required=True)
        c.text(entry.get("access_token"), "access_token", required=True)
        c.text(entry.get("zcode_token"), "zcode_token", required=True)
    entry.setdefault("label", entry.get("email") or entry["subject"])
    return entry


def start_login_sync(*, site="bigmodel"):
    c.site_of({"site": site})
    token = secrets.token_hex(32)
    data = c.request(c.PLATFORM_ORIGIN + "/api/v1/oauth/cli/init", method="POST",
                     headers={"Authorization": "Bearer " + token}, body={"provider": site})
    if not isinstance(data, dict):
        raise c.ZhipuError("login", "invalid_data")
    authorize_url = c.text(data.get("authorize_url"), "authorize_url", required=True)
    parsed = urlparse(authorize_url)
    expected_host = "bigmodel.cn" if site == "bigmodel" else "chat.z.ai"
    if parsed.scheme != "https" or parsed.hostname != expected_host or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise c.ZhipuError("login", "invalid_url")
    expires = min(time.time() + 300, float(data.get("expires_at") or 0))
    interval = float(data.get("poll_interval_sec") or 0)
    if not 1 <= interval < expires - time.time():
        raise c.ZhipuError("login", "invalid_interval")
    # This is the headless CLI polling flow, not the desktop deep-link flow.
    # The server callback completes remote_flow_id. Replacing it with zcode://
    # strands the poller even though the provider page says authorization passed.
    return {"site": site, "poll_token": token, "remote_flow_id": c.text(data.get("flow_id"), "flow_id", required=True),
            "auth_url": authorize_url, "expires": expires,
            "interval": interval, "status": "pending"}


CALLBACK_URI = "http://127.0.0.1:1456/auth/callback"


def start_callback_login_sync(*, site="bigmodel"):
    """Headless copy/paste flow; no local listener or server polling required."""
    c.site_of({"site": site})
    state = secrets.token_hex(32)
    if site == "bigmodel":
        url = "https://bigmodel.cn/login?" + urlencode({"appId": "zcode", "redirect": CALLBACK_URI, "state": state})
    else:
        url = "https://chat.z.ai/api/oauth/authorize?" + urlencode({"client_id": "client_P8X5CMWmlaRO9gyO-KSqtg",
            "response_type": "code", "redirect_uri": CALLBACK_URI, "state": state})
    return {"site": site, "auth_url": url, "state": state, "redirect_uri": CALLBACK_URI,
            "status": "awaiting_callback", "expires": time.time() + 300}


def accept_callback_sync(payload, callback_url):
    """Validate the full callback before exchanging its one-use authorization code."""
    parsed = urlparse(str(callback_url).strip())
    expected = urlparse(payload["redirect_uri"])
    query = parse_qs(parsed.query, keep_blank_values=True)
    states = query.get("state") or []
    codes = query.get("code") or query.get("authCode") or []
    if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc or parsed.path != expected.path
            or parsed.fragment or len(states) != 1 or len(codes) != 1 or not codes[0]
            or not hmac.compare_digest(states[0], payload["state"])
            or ("code" in query and "authCode" in query and query["code"] != query["authCode"])):
        raise ValueError("invalid_callback")
    if time.time() >= payload["expires"]:
        raise ValueError("callback_expired")
    if "login_result" not in payload:
        if payload.get("callback_submitted"):
            raise ValueError("callback_already_submitted")
        payload["callback_submitted"] = True
        # Code is never logged/persisted. A lost exchange result must not cause
        # automatic replay of the one-use code; start a fresh authorization.
        data = c.request(c.PLATFORM_ORIGIN + "/api/v1/oauth/token", method="POST", body={
            "provider": payload["site"], "code": codes[0], "redirect_uri": payload["redirect_uri"], "state": payload["state"]})
        payload["login_result"] = data
    payload["credential"] = credential_from_login(payload["login_result"], payload["site"])
    payload.update(entry=copy.deepcopy(payload["credential"]), status="ready")


def credential_from_login(data, site):
    if not isinstance(data, dict):
        raise c.ZhipuError("login", "invalid_data")
    user, tokens = data.get("user") or {}, data.get(site) or {}
    business = c.text(tokens.get("access_token"), "access_token", required=True)
    if site == "zai":
        if not data.get("_business_token"):
            exchanged = c.request(c.BIZ_ORIGINS[site] + "/api/auth/z/login", method="POST", body={"token": business})
            data["_business_token"] = c.text((exchanged or {}).get("access_token"), "access_token", required=True)
        business = data["_business_token"]
    subject = user.get("user_id")
    name = user.get("name") or user.get("email")
    email = user.get("email") or ""
    if not subject or not name:
        url = "https://chat.z.ai/api/oauth/userinfo" if site == "zai" else c.BIZ_ORIGINS[site] + "/api/biz/customer/getCustomerInfo"
        try:
            profile = c.request(url, headers={"Authorization": ("Bearer " if site == "zai" else "") + business},
                                read_attempts=3, stage="profile") or {}
            if not isinstance(profile, dict):
                raise c.ZhipuError("profile", "invalid_data")
        except c.ZhipuError:
            if not subject:
                raise  # Identity is required; a display name is not.
            profile = {}
        subject = subject or (profile.get("sub") or profile.get("id") if site == "zai" else profile.get("customerNumber"))
        name = name or (profile.get("name") or profile.get("preferred_username") or profile.get("email") if site == "zai" else profile.get("customerName") or profile.get("nickName"))
        email = email or profile.get("email") or ""
    return normalize_credential({"site": site, "credential_mode": "oauth", "subject": subject,
        "label": name or email or subject, "email": email, "access_token": business,
        "refresh_token": tokens.get("refresh_token") or "", "zcode_token": data.get("token")})


def poll_login_sync(payload):
    if time.time() >= payload["expires"]:
        payload["status"] = "expired"
        return
    if "login_result" not in payload:
        try:
            data = c.request(c.PLATFORM_ORIGIN + "/api/v1/oauth/cli/poll/" + quote(payload["remote_flow_id"], safe=""),
                             headers={"Authorization": "Bearer " + payload["poll_token"]})
        except c.ZhipuError as exc:
            if 400 <= exc.status_code < 500 and exc.status_code not in (408, 429):
                payload["status"] = "failed"
            raise
        if not isinstance(data, dict) or data.get("status") not in {"pending", "ready", "failed"}:
            raise c.ZhipuError("login", "invalid_data")
        if data["status"] != "ready":
            payload["status"] = data["status"]
            return
        # Preserve the received result before any optional profile/business lookup.
        # Retry normalization from this result, never re-consume a ready poll.
        payload["login_result"] = data
    payload["credential"] = credential_from_login(payload["login_result"], payload["site"])
    payload.update(entry=copy.deepcopy(payload["credential"]), status="ready")


def project_choices(account, *, account_key=""):
    site = c.site_of(account)
    data = c.request(c.BIZ_ORIGINS[site] + "/api/biz/customer/getCustomerInfo",
                     headers=c.biz_headers(account), account_key=account_key, read_attempts=3, stage="projects")
    choices = []
    for org in (data or {}).get("organizations") or []:
        for project in org.get("projects") or []:
            if not org.get("organizationId") or not project.get("projectId"):
                continue
            choices.append({"organization_id": str(org["organizationId"]), "project_id": str(project["projectId"]),
                "organization_name": str(org.get("organizationName") or ""), "project_name": str(project.get("projectName") or ""),
                "plan_scope": "team" if str(project.get("projectType")) == "2" else "personal"})
    return choices


def default_personal_project(choices):
    """ZCode pickOrgAndProject: default personal org/project, never a team."""
    personal = [choice for choice in choices if choice.get("plan_scope") == "personal"]
    if not personal:
        raise c.ZhipuError("projects", "personal_project_missing")
    first = next((choice for choice in personal if "默认机构" in choice.get("organization_name", "")), personal[0])
    projects = [choice for choice in personal if choice["organization_id"] == first["organization_id"]]
    return next((choice for choice in projects if "默认项目" in choice.get("project_name", "")), projects[0])


def entitlement(account, *, account_key=""):
    host = c.BIZ_ORIGINS[c.site_of(account)]
    team = account.get("plan_scope") == "team"
    path = "/api/biz/team/subscribe/product/querySubscribeDetail" if team else "/api/biz/subscription/list"
    data = c.request(host + path, headers=c.biz_headers(account), account_key=account_key,
                     read_attempts=3, stage="subscription")
    if team:
        if not isinstance(data, dict):
            return "unknown"
        if data.get("hasSubscription") is False:
            return "unavailable"
        if data.get("hasSubscription") is True:
            if data.get("status") == "EXPIRED":
                return "expired"
            if data.get("status") == "EFFECTIVE":
                return {"VALID": "available", "UNASSIGNED": "unassigned"}.get(data.get("memberGrantStatus"), "unknown")
        return "unknown"
    if not isinstance(data, list):
        return "unknown"
    if any(isinstance(row, dict) and "coding" in (str(row.get("productId")) + str(row.get("productName"))).lower()
           and row.get("status") == "VALID" and row.get("inCurrentPeriod") is True for row in data):
        return "available"
    return "unavailable"


def key_path(account):
    return "/api/biz/v1/organization/" + quote(account["organization_id"], safe="") + "/projects/" + quote(account["project_id"], safe="") + "/api_keys"


def resolve_model_key(account, *, account_key="", create=False, before_create=None, on_stage=None,
                      known_key_id=None, on_key=None):
    """Creation is owned by durable actions, including automatic login setup."""
    if not account.get("organization_id") or not account.get("project_id"):
        raise c.ZhipuError("model_key", "project_required")
    base = c.BIZ_ORIGINS[c.site_of(account)] + key_path(account)
    headers = c.biz_headers(account)
    key = {"apiKey": c.text(known_key_id, "apiKey", required=True)} if known_key_id else None
    created = False
    if key is None:
        if on_stage is not None:
            on_stage("key_list")
        keys = c.request(base, headers=headers, account_key=account_key, read_attempts=3, stage="key_list")
        if not isinstance(keys, list):
            raise c.ZhipuError("model_key", "invalid_data")
        key = next((row for row in keys if isinstance(row, dict) and row.get("name") == "zcode-api-key"), None)
        if key is None:
            if not create:
                raise c.ZhipuError("model_key", "creation_confirmation_required")
            if before_create is not None:
                before_create()
            key = c.request(base, method="POST", headers=headers, body={"name": "zcode-api-key"},
                            account_key=account_key, stage="key_create")
            created = True
    key_id = c.text((key or {}).get("apiKey"), "apiKey", required=True)
    if on_key is not None:
        on_key(key_id, created)
    if on_stage is not None:
        on_stage("key_copy")
    data = c.request(base + "/copy/" + quote(key_id, safe=""), headers=headers, account_key=account_key,
                     read_attempts=3, stage="key_copy")
    secret = c.text((data or {}).get("secretKey"), "secretKey")
    if not secret and c.site_of(account) == "zai":
        raise c.ZhipuError("model_key", "secret_missing")
    return key_id + "." + secret if secret else key_id
