"""Qoder account pages: credits usage and the daily credits campaign, asked on the device token."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import common, device_identity, device_profile
from ...safe_diagnostics import diagnose

# campaigns/usage retain their existing account-page protocol, not IDE activity.
_ACCOUNT_HEADERS = {"Cosy-ClientType": "10", "User-Agent": "Qoder"}
_IDENTITY_TTL = 3600  # Qoder's desktop client renews its native identity hourly.
_identity_cache: dict[str, tuple[float, dict]] = {}
_identity_lock = threading.Lock()


def _account_token(account: dict) -> str:
    # A PAT account has no device token: its job token reads the account pages.
    if account.get("device_token"):
        return account["device_token"]
    if account.get("qoder_auth_method") == "pat":
        return account.get("access_token") or ""
    return ""


def _call(account: dict, url: str, *, method: str = "GET", body=None, account_key: str = "",
          extra: dict | None = None, stage: str) -> tuple[int, object, dict]:
    """Ask url as the account; a refused device token is rotated once and asked again."""
    from . import runtime
    token = _account_token(account)
    if not token:
        raise common.QoderError(stage, kind="device_token_missing")
    headers = {**_ACCOUNT_HEADERS, **(extra or {}), "Authorization": "Bearer " + token}
    def prepare():
        result = dict(headers)
        if stage.startswith("checkin") and common.realm_of(account) == "global":
            device = device_identity.headers(account, account_key=account_key)
            result.update({k: device[k] for k in ("Cosy-MachineToken", "Cosy-MachineType")})
        return result
    status, data = common.request(url, method=method, headers=headers, prepare_headers=prepare,
                                body=body, account_key=account_key, stage=stage)
    code = common.error_code(data)
    if (code == "105" or (status == 401 and not code)) and account_key:
        if account.get("device_token"):
            account = runtime.rotate_device(account_key, token)
        elif account.get("qoder_auth_method") == "pat":
            from ... import oauth_manager
            current = oauth_manager.get_account(account_key)
            if current.get("access_token") == token:
                oauth_manager._refresh_sync_locked(account_key, True, observation=oauth_manager.refresh_observation(current))
            account = oauth_manager.get_account(account_key)
        else:
            return status, data, account
        headers["Authorization"] = "Bearer " + _account_token(account)
        status, data = common.request(url, method=method, headers=headers, prepare_headers=prepare, body=body, account_key=account_key, stage=stage)
    return status, data, account


def _when(value) -> str | None:
    """An RFC 3339 time with its zone, else seconds or milliseconds since the epoch."""
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    number = common.number(value.strip() if isinstance(value, str) else value)
    if not number:
        return None
    seconds = number / 1000 if number >= 1e12 else number
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def parse_usage(envelope) -> dict:
    """The plan and each pool of credits the usage envelope tells."""
    if not isinstance(envelope, dict) or envelope.get("displayMode") not in {"qoder", "enterprise"}:
        raise common.QoderError("usage", kind="invalid_response")
    if envelope["displayMode"] == "enterprise":
        return {"plan": "Enterprise", "pools": [], "complete": True}
    usage = envelope.get("qoderUsage")
    if not isinstance(usage, dict):
        raise common.QoderError("usage", kind="invalid_response")

    def field(camel, snake):
        return usage.get(camel) if camel in usage else usage.get(snake)

    result = {"pools": [], "complete": True}
    if isinstance(field("userType", "user_type"), str):
        result["plan"] = common.text(field("userType", "user_type"), "plan", maximum=64)
    until = _when(field("expiresAt", "expires_at"))
    if until:
        result["until"] = until

    def add(block, kind, name):
        if not isinstance(block, dict):
            return
        values = {key: block.get(key) for key in ("total", "cap", "used", "remaining")}
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))) for v in values.values()):
            return
        total = values["total"] if values["total"] is not None else values["cap"]
        if total is None or total <= 0 or (values["used"] is None and values["remaining"] is None):
            return
        used = values["used"] if values["used"] is not None else total - values["remaining"]
        if used < 0:
            return
        remaining = values["remaining"] if values["remaining"] is not None else max(0, total - used)
        label = block.get("name") if isinstance(block.get("name"), str) and block.get("name").strip() else name
        result["pools"].append({"kind": kind, "name": label.strip()[:64], "total": total, "used": used,
                                "remaining": remaining, "used_percent": min(100.0, round(100 * used / total, 2)),
                                "unit": block.get("unit") if isinstance(block.get("unit"), str) and block.get("unit") else "credits"})

    add(field("userQuota", "user_quota"), "user", "Credits")
    add(field("addOnQuota", "add_on_quota"), "add_on", "Add-on credits")
    add(field("orgResourcePackage", "org_resource_package"), "shared", "Shared credits")
    dedicated = field("dedicatedResourcePackages", "dedicated_resource_packages")
    for block in dedicated if isinstance(dedicated, list) else []:
        add(block, "dedicated", "Dedicated credits")
    return result


@diagnose("qoder", "fetch_usage", "usage")
def fetch_usage_sync(account: dict, *, account_key: str = "") -> dict:
    url = common.site_of(account)["openapi"] + common.USAGE_PATH
    status, data, _account = _call(account, url, account_key=account_key, stage="usage")
    if status != 200:
        raise common.QoderError("usage", status=status, code=common.error_code(data))
    block = parse_usage(data)
    block.update(realm=common.realm_of(account), source="qoder:usage", fetched_at=int(time.time() * 1000), status="ok")
    return {"qoder": block}


# ---- the daily credits campaign ---------------------------------------------------

def runtime_info_path(*, env=None, home: str | None = None, system: str | None = None, arch: str | None = None) -> str:
    """Qoder's native device-identity helper, installed by its desktop app or cached by its CLI."""
    env = os.environ if env is None else env
    if env.get("QODER_RUNTIME_INFO"):
        configured = Path(env["QODER_RUNTIME_INFO"]).expanduser().resolve()
        if not configured.is_file():
            raise common.QoderError("checkin", kind="runtime_info_missing")
        return str(configured)
    home = home or str(Path.home())
    system = system or {"darwin": "darwin", "windows": "win32"}.get(platform.system().lower(), platform.system().lower())
    machine = (arch or platform.machine()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64",
            "i386": "ia32", "i686": "ia32", "x86": "ia32"}.get(machine, machine)
    name = "runtime-info.exe" if system == "win32" else "runtime-info"
    candidates = []
    if system == "win32":
        local = env.get("LOCALAPPDATA") or str(Path(home) / "AppData" / "Local")
        candidates.append(Path(local) / "Programs" / "Qoder" / "resources" / "umid" / name)
    elif system == "darwin":
        for root in (Path("/Applications"), Path(home) / "Applications"):
            candidates.append(root / "Qoder.app" / "Contents" / "Resources" / "umid" / name)
    elif system == "linux":
        candidates.append(Path("/opt/Qoder/resources/umid") / name)
    for path in candidates:
        if path.is_file():
            return str(path)
    config = env.get("QODER_CONFIG_DIR") or str(Path(env.get("QODER_CLI_HOME") or env.get("GEMINI_CLI_HOME") or home)
                                                 / (env.get("QODER_CONFIG_DIR_NAME") or ".qoder"))
    cache = Path(config) / ".bin"
    found = []
    try:
        entries = list(cache.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if system == "win32":
            path = entry / name if entry.is_dir() and entry.name.startswith(f"umid-win32-{arch}-") else None
        else:
            path = entry if entry.is_file() and entry.name.startswith(f"runtime-info-{system}-{arch}-") and not entry.name.endswith(".tmp") else None
        if path is not None and path.is_file():
            found.append((path.stat().st_mtime, str(path)))
    if found:
        return max(found)[1]
    raise common.QoderError("checkin", kind="runtime_info_missing")


def native_identity(uid: str) -> dict:
    """Cosy-MachineToken/Type from Qoder's own helper; without them the international claim is hidden."""
    uid = common.text(uid, "uid", required=True)
    now = time.monotonic()
    with _identity_lock:
        cached = _identity_cache.get(uid)
        if cached and now - cached[0] < _IDENTITY_TTL:
            return dict(cached[1])
    path = runtime_info_path()
    linux = platform.system().lower() == "linux"
    try:
        completed = subprocess.run([path, "3"] if linux else [path, "3", "--account-stdin"],
                                   input=None if linux else json.dumps({"account": uid}) + "\n",
                                   stdin=subprocess.DEVNULL if linux else None,
                                   capture_output=True, text=True, timeout=20, check=True)
        identity = json.loads(completed.stdout.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        raise common.QoderError("checkin", kind="runtime_info_failed") from None
    headers = {}
    for header, key in (("Cosy-MachineToken", "machineToken"), ("Cosy-MachineType", "machineType")):
        value = identity.get(key) if isinstance(identity, dict) else None
        value = value.strip() if isinstance(value, str) else ""
        if not value or len(value) > 4096 or not all(32 <= ord(c) <= 126 for c in value):
            raise common.QoderError("checkin", kind="runtime_info_invalid")
        headers[header] = value
    with _identity_lock:
        _identity_cache[uid] = (now, headers)
    return dict(headers)


def checkin_supported(account: dict) -> tuple[bool, str]:
    """Whether the account has explicitly bound its international device profile."""
    if not _account_token(account):
        return False, "device_token_missing"
    if common.realm_of(account) == "global":
        if device_profile.summary(account)["status"] != "bound":
            return False, "device_initialization_required"
    return True, ""


def _json(status, data, stage):
    if status != 200 or common.error_code(data) not in (None, "", "0", "200"):
        raise common.QoderError(stage, status=status, code=common.error_code(data))
    if not isinstance(data, dict):
        raise common.QoderError(stage, status=status, kind="invalid_response")
    return data


@diagnose("qoder", "checkin", "campaign")
def checkin_sync(account: dict, *, account_key: str = "") -> dict:
    """Claim today's credits: claimed, already_done, not_offered (listed but not claimable yet) or inactive."""
    base = common.site_of(account)["openapi"] + common.CAMPAIGNS_PATH
    # Qoder's client sends the campaigns pages as JSON, GET and POST alike.
    machine = {"Content-Type": "application/json"}
    status, data, account = _call(account, base, account_key=account_key, extra=machine, stage="checkin_list")
    listing = _json(status, data, "checkin_list")
    if not isinstance(listing.get("campaigns"), list) or any(not isinstance(v, dict) for v in listing["campaigns"]):
        raise common.QoderError("checkin_list", kind="invalid_response")
    now = time.time()
    campaigns = [item for item in listing.get("campaigns") or [] if isinstance(item, dict) and item.get("actionType") == "CLAIM_BENEFIT"]

    def amount(*values):
        for value in values:
            number = common.number(value)
            if number:
                return number
        return None

    def active(item):
        start, end = common.number(item.get("startAt")), common.number(item.get("endAt"))
        return (not start or start <= now) and (not end or end > now)

    claimable = next((item for item in campaigns if item.get("claimStatus") == "CLAIMABLE"), None)
    if claimable is None:
        done = next((item for item in campaigns if item.get("claimStatus") == "CLAIMED" and active(item)), None)
        if done is not None:
            return {"status": "already_done", "credit": amount((done.get("benefit") or {}).get("amount")),
                    "until": common.number(done.get("endAt"))}
        return {"status": "not_offered" if campaigns else "inactive"}
    campaign_id = common.text(claimable.get("campaignId"), "campaign id", required=True)
    from urllib.parse import quote
    status, data, _account = _call(account, f"{base}/{quote(campaign_id, safe='')}/claim", method="POST", body={},
                                   account_key=account_key, extra=machine, stage="checkin_claim")
    claim = _json(status, data, "checkin_claim")
    inner = claim.get("data") if isinstance(claim.get("data"), dict) else {}
    if (claim.get("status") or inner.get("status")) != "CLAIMED":
        raise common.QoderError("checkin_claim", status=status, kind="claim_unconfirmed")
    benefit = claim.get("benefit") or inner.get("benefit") or {}
    return {"status": "claimed", "credit": amount(benefit.get("amount") if isinstance(benefit, dict) else None,
                                                  (claimable.get("benefit") or {}).get("amount")),
            "until": common.number(claimable.get("endAt"))}
