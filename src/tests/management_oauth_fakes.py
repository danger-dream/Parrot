from __future__ import annotations

import copy
import json
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock
from urllib.parse import parse_qs, urlencode, urlparse

from src.management_control.oauth import OAuthBackend, OAuthControl
from src.oauth.openai_import import parse_openai_import_payload


class ImmediateExecutor:
    def submit(self, function, /, *args, **kwargs):
        function(*args, **kwargs)


@dataclass
class CursorLogin:
    login_url: str = "https://cursor.example.test/login"
    uuid: str = "cursor-uuid"
    verifier: str = "cursor-verifier"


@dataclass
class CursorTokens:
    access_token: str = "cursor-access-secret"
    refresh_token: str = "cursor-refresh-secret"
    expires_at_ms: int = 1_800_000_000_000


class InMemoryOAuthBackend(OAuthBackend):
    def __init__(self) -> None:
        self.accounts = [
            {
                "_id": "openai:admin@example.test:workspace-1",
                "provider": "openai",
                "type": "openai",
                "email": "admin@example.test",
                "label": "Primary",
                "workspace_id": "workspace-1",
                "chatgpt_account_id": "workspace-1",
                "access_token": "access-secret-in-storage",
                "refresh_token": "refresh-secret-in-storage",
                "enabled": True,
                "disabled_reason": None,
                "disabled_until": None,
                "maxConcurrent": 2,
                "models": ["gpt-alpha", "gpt-beta"],
                "disabled_models": ["gpt-beta"],
            },
            {
                "_id": "claude:invalid@example.test",
                "provider": "claude",
                "type": "claude",
                "email": "invalid@example.test",
                "access_token": "invalid-access-secret",
                "refresh_token": "invalid-refresh-secret",
                "enabled": False,
                "disabled_reason": "auth_error",
                "disabled_until": None,
                "maxConcurrent": 0,
                "models": ["claude-test"],
            },
        ]
        self.quota = {
            "openai:admin@example.test:workspace-1": {
                "five_hour_util": 12.0,
                "seven_day_util": 34.0,
                "five_hour_reset": "2026-01-01T01:00:00Z",
                "openai_reset_credit_count": 1,
            }
        }
        self.cooldowns = [
            {
                "channel_key": "oauth:openai:admin@example.test:workspace-1",
                "model": "gpt-beta",
                "cooldown_until": -1,
                "last_error_message": "fake failure",
            }
        ]
        self.settings = [False, 60, 95.0, "disabled", False]
        self.preferences = ["used", True]
        self.affinity_cleared = []
        self.last_reset_idempotency_key = None
        self.sync_result = {"action": "updated", "models": 2}
        self.provider_exchange_count = 0
        self.model_sync_started: list[str] = []
        self.usage_fetches: list[str] = []
        self.usage_failures: set[str] = set()
        self.quota_evaluations: list[str] = []
        self._lock = RLock()
        self.interleave_once = None

    def _find_exact(self, account_id: str):
        return next((item for item in self.accounts if self.account_id(item) == account_id), None)

    def _find(self, account_id: str):
        exact = self._find_exact(account_id)
        if exact is not None:
            return exact
        # Mirror production legacy aliases so API tests cannot accidentally pass
        # by using an unrealistically strict fake.
        matches = [
            item for item in self.accounts
            if item.get("email") == account_id
            or f"{self.provider_of(item)}:{item.get('email')}" == account_id
        ]
        return matches[0] if len(matches) == 1 else None

    def _interleave(self):
        callback, self.interleave_once = self.interleave_once, None
        if callback is not None:
            callback()

    def list_accounts(self):
        return self.accounts

    def get_account(self, account_id):
        return self._find(account_id)

    def get_account_exact(self, account_id):
        return self._find_exact(account_id)

    def account_id(self, account):
        if account.get("_id"):
            return str(account["_id"])
        provider = self.provider_of(account)
        if provider == "workbuddy":
            from src.oauth_ids import account_key
            return account_key(account)
        identity = str(account.get("subject") or account.get("email") or "")
        if provider == "openai" and (account.get("workspace_id") or account.get("chatgpt_account_id")):
            return f"openai:{identity}:{account.get('workspace_id') or account.get('chatgpt_account_id')}"
        if provider == "antigravity" and account.get("project_id"):
            return f"antigravity:{identity}:{account['project_id']}"
        return f"{provider}:{identity}"

    def provider_of(self, account_or_id):
        if isinstance(account_or_id, dict):
            return str(account_or_id.get("provider") or account_or_id.get("type") or "claude")
        account = self._find(str(account_or_id))
        return str(account.get("provider")) if account else str(account_or_id).split(":", 1)[0]

    def resolve_account_id(self, value):
        return self.account_id(self._find(value)) if value and self._find(value) else None

    def account_email(self, account_id):
        account = self._find(account_id)
        return str(account.get("email") or "") if account else ""

    def find_exact_identity(self, entry):
        wanted = self.account_id(entry)
        account = self._find(wanted)
        return (wanted, copy.deepcopy(account)) if account else None

    def add_account_if_absent(self, entry):
        account_id = self.account_id(entry)
        if self._find(account_id):
            return {"status": "identity_conflict", "account_key": account_id}
        saved = copy.deepcopy(entry)
        saved["_id"] = account_id
        self.accounts.append(saved)
        return {"status": "added", "account_key": account_id}

    def commit_import_conditional(self, expected_accounts, candidates, choices):
        with self._lock:
            self._interleave()
            if self.accounts != expected_accounts:
                return {
                    "status": "revision_conflict",
                    "added": [],
                    "replaced": [],
                    "skipped": [],
                }
            outcome = {
                "status": "committed",
                "added": [],
                "replaced": [],
                "skipped": [],
            }
            for item in candidates:
                entry = item["entry"]
                candidate_id = item["candidate_id"]
                existing = self.find_exact_identity(entry)
                if existing:
                    if choices[candidate_id] == "keep":
                        outcome["skipped"].append(existing[0])
                        continue
                    result = self.replace_exact_identity(existing[0], entry)
                    if result.get("status") != "replaced":
                        raise RuntimeError("conditional import replace failed")
                    outcome["replaced"].append(existing[0])
                else:
                    result = self.add_account_if_absent(entry)
                    if result.get("status") != "added":
                        raise RuntimeError("conditional import add failed")
                    outcome["added"].append(result["account_key"])
            return outcome

    def replace_exact_identity(self, account_id, entry):
        current = self._find_exact(account_id)
        if current is None or self.account_id(entry) != account_id:
            return {"status": "missing", "account_key": account_id}
        protected = {
            key: copy.deepcopy(current[key])
            for key in ("models", "disabled_models", "maxConcurrent", "enabled", "disabled_reason", "disabled_until")
            if key in current
        }
        current.clear()
        current.update(copy.deepcopy(entry), **protected)
        current["_id"] = account_id
        return {"status": "replaced", "account_key": account_id, "account": copy.deepcopy(current)}

    def replace_exact_identity_conditional(self, account_id, entry, expected_account):
        with self._lock:
            self._interleave()
            current = self._find_exact(account_id)
            if current is None:
                return {"status": "missing"}
            if current != expected_account:
                return {"status": "revision_conflict"}
            return self.replace_exact_identity(account_id, entry)

    def delete_account_conditional(self, account_id, expected_account):
        with self._lock:
            self._interleave()
            current = self._find_exact(account_id)
            if current is None:
                return {"status": "missing"}
            if current != expected_account:
                return {"status": "revision_conflict"}
            self.delete_account(account_id)
            return {"status": "deleted"}

    def delete_invalid_accounts_conditional(self, expected_accounts):
        with self._lock:
            self._interleave()
            for account_id, expected in expected_accounts:
                current = self._find_exact(account_id)
                if current is None:
                    return {"status": "missing"}
                if current != expected:
                    return {"status": "revision_conflict"}
                if current.get("disabled_reason") != "auth_error":
                    return {"status": "state_conflict"}
            for account_id, _expected in expected_accounts:
                self.delete_account(account_id)
            return {"status": "deleted", "count": len(expected_accounts)}

    def update_account_conditional(
        self, account_id, expected_account, *, display_name=None,
        enabled=None, max_concurrent=None,
    ):
        with self._lock:
            self._interleave()
            current = self._find_exact(account_id)
            if current is None:
                return {"status": "missing"}
            if current != expected_account:
                return {"status": "revision_conflict"}
            if display_name is not None:
                current["label"] = display_name
            if enabled is not None:
                current["enabled"] = bool(enabled)
                current["disabled_reason"] = None if enabled else "user"
                current["disabled_until"] = None
                current.pop("quota_observation", None)
            if max_concurrent is not None:
                current["maxConcurrent"] = max(0, int(max_concurrent))
            return {"status": "updated", "account": copy.deepcopy(current)}

    def delete_account(self, account_id):
        account = self._find(account_id)
        if account is None:
            return
        self.accounts.remove(account)
        self.quota.pop(account_id, None)
        self.cooldowns = [item for item in self.cooldowns if item["channel_key"] != f"oauth:{account_id}"]

    def set_enabled(self, account_id, enabled, *, reason=None):
        account = self._find(account_id)
        account["enabled"] = bool(enabled)
        account["disabled_reason"] = None if enabled else (reason or "user")
        account["disabled_until"] = None

    def update_max_concurrent(self, account_id, value):
        self._find(account_id)["maxConcurrent"] = value

    def update_account_display_name(self, account_id, display_name):
        account = self._find(account_id)
        if display_name:
            account["label"] = display_name
        else:
            account.pop("label", None)

    def reorder_accounts_conditional(self, expected_account_ids, account_ids):
        with self._lock:
            self._interleave()
            current = [self.account_id(item) for item in self.accounts]
            if current != list(expected_account_ids):
                return {"status": "revision_conflict"}
            if len(account_ids) != len(set(account_ids)) or set(account_ids) != set(current):
                return {"status": "resource_conflict"}
            self.reorder_accounts(account_ids)
            return {"status": "updated"}

    def reorder_accounts(self, account_ids):
        by_id = {self.account_id(item): item for item in self.accounts}
        self.accounts = [by_id[item] for item in account_ids]

    def reorder_accounts_preserving_unlisted(self, account_ids):
        order = {value: index for index, value in enumerate(account_ids)}
        selected = [item for item in self.accounts if self.account_id(item) in order]
        selected.sort(key=lambda item: order[self.account_id(item)])
        self.accounts = selected + [item for item in self.accounts if self.account_id(item) not in order]

    async def force_refresh(self, account_id):
        self._find(account_id)["access_token"] = "rotated-access-secret"
        return "rotated-access-secret"

    async def fetch_usage(self, account_id):
        return await self.fetch_usage_snapshot(account_id)

    async def fetch_usage_snapshot(self, account_id):
        self.usage_fetches.append(account_id)
        if account_id in self.usage_failures:
            raise RuntimeError("fake usage failure")
        return {"five_hour": {"utilization": 10.0}}

    async def enrich_openai_reset_credit_details(self, account_id, usage):
        return usage

    def flatten_usage(self, usage):
        return {"five_hour_util": 10.0}

    def preserve_antigravity_summary(self, account_id, usage):
        return usage

    def preserve_openai_reset_details(self, account_id, usage):
        return usage

    def evaluate_quota(self, account_id, usage):
        self.quota_evaluations.append(account_id)
        return {"action": "kept_enabled"}

    def quota_load(self, account_id):
        return self.quota.get(account_id)

    def quota_save(self, account_id, usage, *, email=None):
        self.quota[account_id] = dict(usage, email=email)

    def tokens_for_channel(self, channel_key, since):
        return {"total": 3, "input": 100, "output": 20, "cost_ticks": 100_000_000, "costed_success": 3}

    def cooldown_entries(self):
        return self.cooldowns

    def cooldown_state(self, channel_key, model_id):
        return next((item for item in self.cooldowns if item["channel_key"] == channel_key and item.get("model") == model_id), None)

    def clear_errors(self, account_id, model_id=None):
        channel = f"oauth:{account_id}"
        self.cooldowns = [
            item for item in self.cooldowns
            if not (item["channel_key"] == channel and (model_id is None or item.get("model") == model_id))
        ]

    def clear_all_errors(self):
        keys = {item["channel_key"] for item in self.cooldowns if item["channel_key"].startswith("oauth:")}
        self.cooldowns = [item for item in self.cooldowns if not item["channel_key"].startswith("oauth:")]
        return len(keys)

    def clear_affinity(self, account_id):
        self.affinity_cleared.append(account_id)

    def account_model_selection(self, account_or_id):
        account = account_or_id if isinstance(account_or_id, dict) else self._find(account_or_id)
        values = list(account.get("models") or [])
        records = account.get("model_records") or [
            {"id": value, "name": value.upper(), "contextWindow": 128000}
            for value in values
        ]
        return {
            "models": values,
            "records": copy.deepcopy(records),
            "disabled_models": set(account.get("disabled_models") or []),
            "source": "fake",
        }

    def account_disabled_models(self, account_or_id):
        account = account_or_id if isinstance(account_or_id, dict) else self._find(account_or_id)
        return set(account.get("disabled_models") or [])

    def update_account_models_conditional(
        self, account_id, expected_account, *, visible_models, disabled_models,
    ):
        with self._lock:
            self._interleave()
            current = self._find_exact(account_id)
            if current is None:
                return {"status": "missing"}
            if current != expected_account:
                return {"status": "revision_conflict"}
            hidden = set(current.get("disabled_models") or []) - set(visible_models)
            current["disabled_models"] = sorted(hidden | set(disabled_models))
            return {"status": "updated", "account": copy.deepcopy(current)}

    def set_account_disabled_models(self, account_id, models, *, visible_models=None):
        value = set(models)
        self._find(account_id)["disabled_models"] = sorted(value)
        return value

    def set_account_model_disabled(self, account_id, model_id, disabled):
        current = self.account_disabled_models(account_id)
        current.add(model_id) if disabled else current.discard(model_id)
        self._find(account_id)["disabled_models"] = sorted(current)
        return disabled

    def cursor_max_context_default(self, account_or_id, model_id):
        account = account_or_id if isinstance(account_or_id, dict) else self._find(account_or_id)
        return model_id not in set(account.get("cursor_max_context_disabled_models") or [])

    def update_cursor_model_setting_conditional(
        self, account_id, expected_account, *, model_id, enabled,
    ):
        with self._lock:
            self._interleave()
            account = self._find_exact(account_id)
            if account is None:
                return {"status": "missing"}
            if account != expected_account:
                return {"status": "revision_conflict"}
            self.set_cursor_max_context_default(account_id, model_id, enabled)
            return {"status": "updated", "account": copy.deepcopy(account)}

    def set_cursor_max_context_default(self, account_id, model_id, enabled):
        account = self._find(account_id)
        disabled = set(account.get("cursor_max_context_disabled_models") or [])
        disabled.discard(model_id) if enabled else disabled.add(model_id)
        account["cursor_max_context_disabled_models"] = sorted(disabled)
        return enabled

    async def refresh_account_models(self, account_id):
        result = copy.deepcopy(self.sync_result)
        if result.get("action") == "updated":
            self._find(account_id)["last_model_sync"] = "2026-01-01T00:00:00Z"
        return result

    def start_account_model_refresh(self, account_id):
        self.model_sync_started.append(account_id)
        future = Future()
        future.set_result(copy.deepcopy(self.sync_result))
        return future

    def reset_quota(self, account_id):
        self.quota.pop(account_id, None)
        return {"action": "reset", "account_key": account_id}

    async def redeem_openai_reset_credit(self, account_id, idempotency_key):
        self.last_reset_idempotency_key = idempotency_key
        return {"outcome": "reset", "available_count": 0}

    def get_settings(self):
        values = list(self.settings)
        values[3] = values[3] if values[3] in {"disabled", "dynamic"} else "disabled"
        return tuple(values)

    def update_settings_conditional(
        self, expected, *, quota_enabled=None, interval_seconds=None,
        threshold_percent=None, cch_mode=None,
        antigravity_tls_fingerprint_enabled=None,
    ):
        with self._lock:
            self._interleave()
            normalized = list(self.settings)
            normalized[3] = normalized[3] if normalized[3] in {"disabled", "dynamic"} else "disabled"
            if tuple(normalized) != tuple(expected):
                return {"status": "revision_conflict"}
            self.update_settings(
                quota_enabled=quota_enabled,
                interval_seconds=interval_seconds,
                threshold_percent=threshold_percent,
                cch_mode=cch_mode,
                antigravity_tls_fingerprint_enabled=antigravity_tls_fingerprint_enabled,
            )
            return {"status": "updated"}

    def update_settings(
        self, *, quota_enabled=None, interval_seconds=None, threshold_percent=None,
        cch_mode=None, antigravity_tls_fingerprint_enabled=None,
    ):
        if quota_enabled is not None: self.settings[0] = quota_enabled
        if interval_seconds is not None: self.settings[1] = interval_seconds
        if threshold_percent is not None: self.settings[2] = threshold_percent
        if cch_mode is not None: self.settings[3] = cch_mode
        if antigravity_tls_fingerprint_enabled is not None:
            self.settings[4] = antigravity_tls_fingerprint_enabled

    def get_preferences(self):
        values = list(self.preferences)
        values[0] = values[0] if values[0] in {"used", "remaining"} else "used"
        return tuple(values)

    def update_preferences_conditional(
        self, expected, *, usage_display_mode=None, quota_progress_bar=None,
    ):
        with self._lock:
            self._interleave()
            if self.get_preferences() != tuple(expected):
                return {"status": "revision_conflict"}
            self.update_preferences(
                usage_display_mode=usage_display_mode,
                quota_progress_bar=quota_progress_bar,
            )
            return {"status": "updated"}

    def update_preferences(self, *, usage_display_mode=None, quota_progress_bar=None):
        if usage_display_mode is not None: self.preferences[0] = usage_display_mode
        if quota_progress_bar is not None: self.preferences[1] = quota_progress_bar

    def parse_import(self, kind, payload, *, filename=""):
        return parse_openai_import_payload(kind, payload, filename=filename)

    def openai_pkce_generate(self):
        return "verifier", "challenge"

    def openai_build_login_url(self, challenge, state):
        return "https://openai.example.test/authorize?" + urlencode({"state": state})

    def openai_exchange_code(self, code, verifier):
        self.provider_exchange_count += 1
        return {"access_token": "flow-access-secret", "refresh_token": "flow-refresh-secret", "id_token": "flow-id-token", "expires_in": 3600}

    def openai_decode_id_token(self, token):
        if str(token).startswith("{"):
            return json.loads(token)
        return {"email": "flow@example.test", "workspace_id": "flow-workspace"}

    def openai_extract_user_info(self, claims):
        return claims

    def openai_refresh(self, refresh_token, **kwargs):
        email = str(kwargs.get("email") or "import@example.test")
        workspace = f"import-{email.partition('@')[0]}"
        return {
            "access_token": "openai-refreshed-access",
            "refresh_token": refresh_token,
            "id_token": json.dumps({"email": email, "workspace_id": workspace}),
            "workspace_id": workspace,
            "expires_in": 3600,
        }

    def claude_pkce_generate(self):
        return "claude-verifier", "claude-challenge"

    def claude_build_login_url(self, challenge, state):
        return "https://claude.example.test/authorize?" + urlencode({"state": state})

    def claude_exchange_code(self, code, verifier, state):
        return {
            "access_token": "claude-access-secret",
            "refresh_token": "claude-refresh-secret",
            "expires_in": 3600,
        }

    async def claude_fetch_profile(self, access_token):
        return {"account": {"email": "claude-flow@example.test"}}

    def claude_extract_plan(self, profile):
        return {"plan_type": "test"}

    def xai_pkce_generate(self):
        return "xai-verifier", "xai-challenge"

    def xai_discover(self):
        return {
            "authorization_endpoint": "https://xai.example.test/authorize",
            "token_endpoint": "https://xai.example.test/token",
        }

    def xai_authorization_url(self):
        return "https://xai.example.test/authorize"

    def xai_token_url(self):
        return "https://xai.example.test/token"

    def xai_redirect_uri(self):
        return "http://localhost:1455/auth/callback"

    def xai_api_base_url(self):
        return "https://xai.example.test/v1"

    def xai_build_login_url(self, challenge, state, **kwargs):
        return "https://xai.example.test/authorize?" + urlencode({"state": state})

    def xai_exchange_code(self, code, verifier, **kwargs):
        return {
            "access_token": "xai-access-secret",
            "refresh_token": "xai-refresh-secret",
            "id_token": "xai-id-token",
            "expires_in": 3600,
        }

    def xai_refresh(self, refresh_token):
        return {
            "access_token": "xai-refreshed-access",
            "refresh_token": refresh_token,
            "id_token": "xai-id-token",
            "expires_in": 3600,
        }

    def xai_decode_id_token(self, token):
        return {"email": "xai-flow@example.test", "subject": "xai-subject"}

    def xai_extract_user_info(self, claims):
        return claims

    def workbuddy_start_login(self):
        return {"realm": "cn", "state": "fixture-workbuddy-state", "status": "pending",
                "auth_url": "https://www.codebuddy.cn/login"}

    def workbuddy_poll_login(self, payload):
        from src.oauth.workbuddy.auth import normalize_credential
        payload.update(status="ready", entry=normalize_credential({"realm": "cn", "uid": "fixture-workbuddy",
            "nickname": "WorkBuddy Flow", "access_token": "fixture-workbuddy-at", "refresh_token": "fixture-workbuddy-rt"}))

    def cursor_generate_login(self):
        return CursorLogin()

    def cursor_poll_login(self, uuid, verifier):
        return CursorTokens()

    def cursor_subject(self, access_token):
        return "cursor-subject"

    def cursor_profile(self, access_token, **kwargs):
        return {"email": "cursor-flow@example.test", "name": "Cursor Flow", "id": "cursor-profile"}

    def cursor_label(self, subject):
        return f"cursor-{subject}"

    def antigravity_build_login_url(self, state):
        return "https://google.example.test/authorize?" + urlencode({"state": state})

    def antigravity_token_url(self):
        return "https://google.example.test/token"

    def antigravity_redirect_uri(self):
        return "http://localhost:51121/oauth-callback"

    def antigravity_api_base_url(self):
        return "https://antigravity.example.test"

    def antigravity_parse_callback(self, raw):
        values = parse_qs(urlparse(raw).query)
        return {"code": values.get("code", [""])[0], "state": values.get("state", [""])[0]}

    def antigravity_complete_login(self, code, **kwargs):
        return {
            "email": "gravity-flow@example.test",
            "project_id": "gravity-project",
            "access_token": "gravity-access-secret",
            "refresh_token": "gravity-refresh-secret",
            "expires_in": 3600,
        }


def build_control() -> tuple[OAuthControl, InMemoryOAuthBackend]:
    backend = InMemoryOAuthBackend()
    return OAuthControl(backend, executor=ImmediateExecutor()), backend
