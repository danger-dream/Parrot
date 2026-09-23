"""Search management: one sparse, secret-preserving config boundary for API/TG."""
from __future__ import annotations

import asyncio
import copy
import math
import re
import uuid
from urllib.parse import urlsplit

from src import config, oauth_manager, search_service
from src.management_auth import Capability
from src.oauth_ids import account_key
from .errors import ManagementError, ManagementErrorCode
from .models.common import DomainControl, stable_revision


INTEGER_LIMITS = {
    "maxAttempts": (1, 10), "maxResults": (1, 20), "maxToolRounds": (1, 1000),
    "maxFetchChars": (1, 10_000_000), "minQueryChars": (1, 1000),
    "maxFetchUrlChars": (1, 100_000), "maxConcurrentToolCalls": (0, 1000),
}
SETTING_FIELDS = (*INTEGER_LIMITS, "functionMode", "hostedMode", "timeoutSeconds",
                  "requireKnownUrlForFetch", "language", "country", "freshness")
BACKEND_FIELDS = ("name", "enabled", "endpoint", "model", "accountIds", "allowDisabledAccounts")
KEY_FIELDS = ("apiKeys", "addApiKeys", "removeKeyIndices")
API_TYPES = search_service.KEY_TYPES
ACCOUNT_TYPES = search_service.ACCOUNT_TYPES


class SearchControl(DomainControl):
    def __init__(self, *, audit_sink=None, operations=None):
        super().__init__(audit_sink=audit_sink)
        self.operations = operations
        # Separate runtime-owned ledger; only hashes and operation IDs are retained.
        self._idempotency = self._idempotency.__class__()

    @staticmethod
    def _invalid(field, message="Invalid search setting"):
        return DomainControl._validation(field, "invalid_value", message)

    @staticmethod
    def _backend(cfg, backend_id):
        for backend in cfg.get("backends", []):
            if backend.get("id") == backend_id:
                return backend
        raise ManagementError(ManagementErrorCode.RESOURCE_NOT_FOUND)

    def get(self, context):
        self._read(context)
        return self._settings_view()

    @staticmethod
    def _settings_view():
        """Secret-free projection shared by authorized reads and write receipts."""
        cfg = search_service.settings()
        statuses = {row["id"]: row for row in search_service.backend_statuses()}
        result = {key: copy.deepcopy(cfg[key]) for key in SETTING_FIELDS}
        result["backends"] = []
        for backend in cfg.get("backends", []):
            if backend.get("id") not in statuses:
                continue
            row = {key: copy.deepcopy(backend.get(key, search_service.default_backend(backend["type"])[key]))
                   for key in BACKEND_FIELDS}
            row.update(statuses[backend["id"]])
            # Legacy endpoints may predate validation. Do not reveal embedded
            # URL credentials/query secrets and never rewrite them on a read.
            try:
                parsed = urlsplit(row["endpoint"])
                if parsed.username or parsed.password or parsed.query or parsed.fragment:
                    row["endpoint"] = ""
            except ValueError:
                row["endpoint"] = ""
            result["backends"].append(row)
        result["revision"] = stable_revision(cfg)
        return result

    @staticmethod
    def _is_configured(row) -> bool:
        """Whether a source has anything of its own to manage.

        The backend list is seeded with one placeholder per backend type, so an
        untouched entry is not a real source. It counts as configured once it has
        a credential, or differs from its own default template (renamed, custom
        endpoint/id, account selection, model, manual disable) — that keeps a
        freshly added source visible before its first key or account arrives.
        """
        if row.get("keyCount") or row.get("accountCount"):
            return True
        # An explicitly added source keeps a generated id; a seeded placeholder
        # reuses its type as id. This must be checked first so a just-added
        # source stays reachable while its key or account selection is pending.
        if str(row.get("id") or "") != str(row.get("type") or ""):
            return True
        # A provider may have accounts that are all currently disabled or
        # missing credentials: accountCount is then 0, yet the source must stay
        # reachable so its opt-in/account selection can still be managed.
        if row["type"] in ACCOUNT_TYPES and SearchControl._has_provider_accounts(row["type"]):
            return True
        template = search_service.default_backend(row["type"])
        for key in BACKEND_FIELDS:
            if row.get(key) != template.get(key):
                return True
        return False

    @staticmethod
    def _has_provider_accounts(backend_type: str) -> bool:
        """Whether any OAuth account of this provider exists, in any state."""
        provider = "claude" if backend_type == "anthropic" else backend_type
        return any(
            (account.get("provider") or "claude") == provider
            for account in config.get().get("oauthAccounts") or []
        )

    def visible_backends(self, context):
        """Configured sources only, in priority order (what the UI should list)."""
        value = self.get(context)
        value["backends"] = [row for row in value["backends"] if self._is_configured(row)]
        return value

    def accounts(self, context, backend_id):
        """Only public full identities; never return OAuth credentials."""
        self._read(context)
        backend = self._backend(search_service.settings(), backend_id)
        kind = backend["type"]
        provider = "claude" if kind == "anthropic" else kind
        if kind not in ACCOUNT_TYPES:
            return []
        rows = []
        accounts = [acc for acc in config.get().get("oauthAccounts") or []
                    if (acc.get("provider") or "claude") == provider]
        for account in accounts:
            name = str(account.get("label") or account.get("email") or account.get("name") or provider)
            if provider == "openai" and sum(acc.get("email") == account.get("email") for acc in accounts) > 1:
                # Match OAuth menus: curated labels first, human workspace
                # disambiguation only; never display internal workspace IDs.
                workspace = str(account.get("workspace_name") or "").strip()
                kind = str(account.get("workspace_type") or "").strip()
                plan = str(account.get("plan_type") or "").strip()
                if not workspace or (workspace.lower() == "personal" and "team" in f"{kind} {plan}".lower()):
                    workspace = kind or plan or "workspace"
                name += " · " + workspace
            rows.append({"id": account_key(account),
                         "name": name,
                         "enabled": account.get("enabled", True) is not False and not bool(account.get("disabled_reason")),
                         "credentialConfigured": bool(account.get("model_key" if provider == "zhipu" else "access_token"))})
        return rows

    def _commit(self, context, action, mutate, expected_revision=None, *, read_back=True):
        self._write(context)
        try:
            def apply(root):
                # Called within config.update's serialized/reentrant lock. Never use
                # the public DTO as a base: it intentionally omits all API keys.
                effective = copy.deepcopy(search_service.settings())
                self._check_revision(expected_revision, stable_revision(effective))
                mutate(effective)
                root["search"] = effective
            config.update(apply)
        except ManagementError:
            self._audit(context, action, "search", "failed")
            raise
        except Exception:
            self._audit(context, action, "search", "failed")
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE) from None
        self._audit(context, action, "search", "succeeded")
        # WRITE authorizes the mutation receipt; it does not grant an independent
        # GET. Do not perform a second READ permission check after committing.
        return self._settings_view() if read_back else None

    def patch(self, context, patch, *, expected_revision=None):
        self._write(context)
        if not isinstance(patch, dict) or not patch or set(patch) - set(SETTING_FIELDS):
            raise self._invalid("body")
        for key, value in patch.items():
            if key in INTEGER_LIMITS:
                low, high = INTEGER_LIMITS[key]
                if type(value) is not int or not low <= value <= high:
                    raise self._invalid(key, f"Expected integer in {low}..{high}")
            elif key in ("functionMode", "hostedMode"):
                if value not in search_service.MODES:
                    raise self._invalid(key)
            elif key == "timeoutSeconds":
                if type(value) not in (float, int) or not math.isfinite(value) or not 0.1 <= value <= 600:
                    raise self._invalid(key, "Expected seconds in 0.1..600")
            elif key == "requireKnownUrlForFetch":
                if type(value) is not bool:
                    raise self._invalid(key)
            elif not isinstance(value, str) or len(value) > 64:
                raise self._invalid(key)
            elif key == "freshness" and value not in ("", "day", "week", "month", "year"):
                raise self._invalid(key)
        return self._commit(context, "search.settings.update", lambda cfg: cfg.update(copy.deepcopy(patch)), expected_revision)

    def _backend_patch(self, context, backend, patch):
        if not isinstance(patch, dict) or not patch or set(patch) - set((*BACKEND_FIELDS, *KEY_FIELDS)):
            raise self._invalid("body")
        key_ops = set(patch) & set(KEY_FIELDS)
        if key_ops:
            self._write(context, Capability.SECRETS_WRITE)
            if backend["type"] not in API_TYPES or len(key_ops) != 1:
                raise self._invalid("apiKeys")
        for key, value in patch.items():
            if key in ("enabled", "allowDisabledAccounts"):
                if type(value) is not bool:
                    raise self._invalid(key)
                if key == "allowDisabledAccounts" and backend["type"] not in ACCOUNT_TYPES:
                    raise self._invalid(key)
            elif key in ("name", "model", "endpoint"):
                if not isinstance(value, str) or len(value) > (2048 if key == "endpoint" else 200):
                    raise self._invalid(key)
                if key == "name" and not value.strip():
                    raise self._invalid(key)
                if key == "model" and backend["type"] in API_TYPES and value:
                    raise self._invalid(key, "Only OAuth search backends support model settings")
                if key == "endpoint" and value:
                    try:
                        parsed = urlsplit(value)
                        valid = (parsed.scheme in ("https", "http") and parsed.hostname and
                                 not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
                    except ValueError:
                        valid = False
                    if not valid or backend["type"] not in API_TYPES:
                        raise self._invalid(key, "Expected an HTTP(S) endpoint without embedded credentials or query")
                    if backend["type"] == "zhipu":
                        from src.oauth.zhipu.common import MODEL_ORIGINS
                        if value.rstrip("/") not in MODEL_ORIGINS.values():
                            raise self._invalid(key, "Use https://open.bigmodel.cn or https://api.z.ai for Coding Plan keys")
            elif key == "accountIds":
                if backend["type"] not in ACCOUNT_TYPES or not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                    raise self._invalid(key)
                provider = "claude" if backend["type"] == "anthropic" else backend["type"]
                known = {account_key(acc) for acc in config.get().get("oauthAccounts", [])
                         if (acc.get("provider") or "claude") == provider}
                if len(value) != len(set(value)) or any(item not in known for item in value):
                    raise self._invalid(key, "Use complete public account IDs from this provider")
            elif key in ("apiKeys", "addApiKeys"):
                if not isinstance(value, list) or len(value) > 100 or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 8192 for v in value
                ):
                    raise self._invalid("apiKeys", "Expected an array of non-empty keys (maximum 100)")
            elif key == "removeKeyIndices":
                keys = self._keys(backend)
                if not isinstance(value, list) or not value or any(type(v) is not int or not 0 <= v < len(keys) for v in value):
                    raise self._invalid(key)
        for key, value in patch.items():
            if key in ("apiKeys", "addApiKeys"):
                values = (self._keys(backend) if key == "addApiKeys" else []) + [v.strip() for v in value]
                values = list(dict.fromkeys(values))
                if len(values) > 100:
                    raise self._invalid("apiKeys")
                backend["apiKeys"] = values
            elif key == "removeKeyIndices":
                backend["apiKeys"] = [v for i, v in enumerate(self._keys(backend)) if i not in value]
            else:
                backend[key] = copy.deepcopy(value)

    @staticmethod
    def _keys(backend):
        values = backend.get("apiKeys") or []
        if isinstance(values, str):
            values = [values]
        return list(dict.fromkeys(str(v).strip() for v in values if str(v).strip()))

    def add_backend(self, context, body, *, expected_revision=None):
        self._write(context)
        body = copy.deepcopy(body)
        kind = body.pop("type", None)
        backend_id = body.pop("id", None) or f"{kind}-{uuid.uuid4().hex[:12]}"
        if kind not in search_service.BACKEND_TYPES or not isinstance(backend_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", backend_id):
            raise self._invalid("type/id")
        def mutate(cfg):
            if any(row["id"] == backend_id for row in cfg["backends"]):
                raise ManagementError(ManagementErrorCode.RESOURCE_CONFLICT)
            backend = search_service.default_backend(kind)
            backend["id"] = backend_id
            if body:
                self._backend_patch(context, backend, body)
            cfg["backends"].append(backend)
        return self._commit(context, "search.backend.create", mutate, expected_revision)

    def patch_backend(self, context, backend_id, patch, *, expected_revision=None):
        return self._commit(context, "search.backend.update", lambda cfg: self._backend_patch(
            context, self._backend(cfg, backend_id), patch), expected_revision)

    def delete_backend(self, context, backend_id, *, expected_revision=None):
        def mutate(cfg):
            self._check_revision(expected_revision, stable_revision(cfg), required=True)
            backend = self._backend(cfg, backend_id)
            cfg["backends"].remove(backend)
            # Keep even an explicit empty list: legacy defaults must not return.
        return self._commit(context, "search.backend.delete", mutate, expected_revision, read_back=False)

    def priority(self, context, backend_ids, *, expected_revision=None):
        def mutate(cfg):
            current = {row["id"]: row for row in cfg["backends"]}
            if not isinstance(backend_ids, list) or any(not isinstance(v, str) for v in backend_ids) or len(backend_ids) != len(current) or set(backend_ids) != set(current):
                raise self._invalid("backendIds", "Provide every backend ID exactly once")
            cfg["backends"] = [current[item] for item in backend_ids]
        return self._commit(context, "search.priority.update", mutate, expected_revision)

    # OAuth search backends execute a real model call, so the model must come
    # from the account's own catalog. The list is only ever offered as choices;
    # it is never silently substituted for a stored value.
    _MODEL_FALLBACK = {"openai": "gpt-5.5", "xai": "grok-4.6", "anthropic": "claude-sonnet-4-6"}

    def models(self, context, backend_id):
        """Selectable models for one OAuth search source.

        Returns the union of the eligible accounts' own model catalogs, with the
        currently configured value first when it is one of them. API-key sources
        have no model dimension and return an empty list.
        """
        self._read(context)
        backend = self._backend(search_service.settings(), backend_id)
        if backend["type"] in API_TYPES:
            return {"backendId": backend_id, "supported": False, "selected": "",
                    "default": "", "models": []}
        try:
            available = search_service._accounts(backend)
        except Exception:
            available = []
        known: list[str] = []
        for account in available:
            try:
                selection = oauth_manager.account_model_selection(
                    account_key(account) if isinstance(account, dict) else account
                )
            except Exception:
                selection = {}
            for model in selection.get("models") or []:
                name = str(model or "").strip()
                if name and name not in known:
                    known.append(name)
        selected = str(backend.get("model") or "")
        return {
            "backendId": backend_id,
            "supported": True,
            "selected": selected,
            "default": self._MODEL_FALLBACK.get(backend["type"], ""),
            "models": known,
        }

    @staticmethod
    def _search_log_db():
        from src import log_db
        return log_db

    @staticmethod
    def _since(period) -> float:
        """Window start in the same Beijing-time convention as the monthly log DB.

        Computed here rather than in the transport layer: the Management API may
        only depend on the control layer, never on a Telegram module.
        """
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone(timedelta(hours=8)))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period != "today":
            start = start.replace(day=1)
        return start.timestamp()

    @staticmethod
    def _cost_usd(ticks) -> float:
        from src import model_pricing
        return round(int(ticks or 0) / model_pricing.TICKS_PER_USD, 6)

    def logs(self, context, *, since_ts, source_id=None, limit=50, offset=0):
        """Recent search calls, independent of any request parent.

        Read-only view over the dedicated search log. ``account_key`` is a public
        account identity (the same value ``getSearchBackendAccounts`` returns) and
        ``credential_label`` is an ordinal such as ``Key #1``: neither carries a
        credential value, so both stay as statistics dimensions.
        """
        self._read(context)
        log_db = self._search_log_db()
        entries = log_db.search_call_entries(
            since_ts, source_id=source_id or None, limit=limit, offset=offset,
        )
        # Display labels come from the live config so a renamed source is not
        # frozen into historical rows; history keeps the recorded snapshot only
        # when the source no longer exists.
        known = {}
        try:
            for row in search_service.settings().get("backends") or []:
                known[str(row.get("id") or "")] = str(row.get("name") or "")
        except Exception:
            known = {}
        result = []
        for entry in entries:
            source = str(entry.get("source_id") or "")
            result.append({
                "id": int(entry.get("id") or 0),
                "callId": str(entry.get("call_id") or ""),
                "attemptNo": int(entry.get("attempt_no") or 0),
                "origin": str(entry.get("origin") or ""),
                "requestId": entry.get("request_id"),
                "roundNo": int(entry.get("round_no") or 0),
                "sourceId": source,
                "sourceType": str(entry.get("source_type") or ""),
                "sourceName": known.get(source) or str(entry.get("source_name") or "") or source,
                "operation": str(entry.get("operation") or "search"),
                "credentialKind": str(entry.get("credential_kind") or ""),
                "credentialLabel": str(entry.get("credential_label") or ""),
                "accountKey": str(entry.get("account_key") or ""),
                "credentialIndex": entry.get("credential_index"),
                "model": str(entry.get("model") or ""),
                "query": entry.get("query"),
                "url": entry.get("url"),
                "startedAt": float(entry.get("started_at") or 0.0),
                "endedAt": entry.get("ended_at"),
                "status": str(entry.get("status") or "running"),
                "errorCode": entry.get("error_code"),
                "elapsedMs": entry.get("elapsed_ms"),
                "resultCount": int(entry.get("result_count") or 0),
                "contentChars": int(entry.get("content_chars") or 0),
                "inputTokens": int(entry.get("input_tokens") or 0),
                "outputTokens": int(entry.get("output_tokens") or 0),
                "cacheCreationTokens": int(entry.get("cache_creation_tokens") or 0),
                "cacheReadTokens": int(entry.get("cache_read_tokens") or 0),
                "usageObserved": bool(entry.get("usage_observed")),
                "pricingModel": entry.get("pricing_model"),
                "costSource": str(entry.get("cost_source") or "unpriced"),
                "costUsd": self._cost_usd(entry.get("cost_ticks")),
                "settledAt": entry.get("settled_at"),
            })
        return result

    def stats(self, context, *, since_ts, source_id=None):
        """Per-source search statistics; the source-facing complement of logs()."""
        self._read(context)
        rows = self._search_log_db().search_call_stats(since_ts, source_id=source_id or None)
        # Live names take precedence, but a source deleted after the fact must
        # still be identifiable rather than collapsing to its raw ID.
        known = {}
        try:
            for row in search_service.settings().get("backends") or []:
                known[str(row.get("id") or "")] = str(row.get("name") or "")
        except Exception:
            known = {}
        result = []
        for row in rows:
            elapsed_n = int(row.get("elapsed_n") or 0)
            source = str(row.get("source_id") or "")
            result.append({
                "sourceId": source,
                "sourceType": str(row.get("source_type") or ""),
                "sourceName": known.get(source) or str(row.get("source_name") or "") or source,
                "attempts": int(row.get("attempts") or 0),
                "success": int(row.get("success") or 0),
                "failed": int(row.get("failed") or 0),
                "running": int(row.get("running") or 0),
                "averageMs": (round(int(row.get("elapsed_sum") or 0) / elapsed_n) if elapsed_n else None),
                "resultCount": int(row.get("result_count") or 0),
                "inputTokens": int(row.get("input_tokens") or 0),
                "outputTokens": int(row.get("output_tokens") or 0),
                "cacheCreationTokens": int(row.get("cache_creation_tokens") or 0),
                "cacheReadTokens": int(row.get("cache_read_tokens") or 0),
                "costUsd": self._cost_usd(row.get("cost_ticks")),
                "usageObserved": int(row.get("usage_observed") or 0),
                "lastAt": float(row.get("last_at") or 0.0),
            })
        return result

    async def test(self, context, backend_id, *, operation="search", query=None, url=None):
        self._write(context)
        self._backend(search_service.settings(), backend_id)
        if operation not in ("search", "extract"):
            raise self._invalid("operation")
        if (operation == "search" and (not isinstance(query, str) or not query.strip())) or (
            operation == "extract" and (not isinstance(url, str) or not url.strip())
        ):
            raise self._invalid("query/url")
        try:
            call = search_service.search if operation == "search" else search_service.extract
            result = await call({"query": query} if operation == "search" else {"url": url},
                                request_id=context.request_id, backend_id=backend_id,
                                origin="management_test")
        except search_service.SearchError as exc:
            self._audit(context, "search.test", "search", "failed")
            code = (ManagementErrorCode.VALIDATION_FAILED if exc.status_code < 500
                    else ManagementErrorCode.UPSTREAM_ERROR)
            raise ManagementError(code, exc.message, retryable=exc.retryable) from None
        except Exception:
            self._audit(context, "search.test", "search", "failed")
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE) from None
        self._audit(context, "search.test", "search", "succeeded")
        # This is a management probe, not a second public search endpoint. No
        # upstream body, query, result URLs or credential-bearing config in audit/DTO.
        return {"backendId": backend_id, "operation": operation, "succeeded": True,
                "resultCount": len(result.get("results") or []),
                "contentChars": len(result.get("content") or ""),
                "attemptCount": len(result.get("attempts") or [])}

    def start_test(self, context, backend_id, *, operation="search", query=None, url=None):
        self._write(context)
        self._backend(search_service.settings(), backend_id)
        if operation not in ("search", "extract") or (
            operation == "search" and (not isinstance(query, str) or not query.strip())
        ) or (operation == "extract" and (not isinstance(url, str) or not url.strip())):
            raise self._invalid("query/url")
        store = self.operations
        if store is None:
            raise ManagementError(ManagementErrorCode.SERVICE_NOT_READY)
        fingerprint = stable_revision((backend_id, operation, query, url))
        # Serialize replay + allocation so simultaneous retries cannot dispatch twice.
        with self._idempotency_lock:
            key = (context.actor.session_id or context.actor.subject_id, context.idempotency_key)
            if context.idempotency_key and key in self._idempotency:
                known, operation_id = self._idempotency[key]
                if known != fingerprint:
                    raise ManagementError(ManagementErrorCode.STATE_CONFLICT)
                return store.get(context, operation_id)
            item = store.create(context, kind="search.test", cancellable=False)
            def worker():
                store.mark_running(item.id)
                try:
                    result = asyncio.run(self.test(context, backend_id, operation=operation, query=query, url=url))
                    store.succeed(item.id, result)
                except ManagementError as exc:
                    store.fail_if_active(item.id, code=exc.code, retryable=exc.retryable)
            store.submit(item.id, worker)
            if context.idempotency_key:
                self._idempotency[key] = (fingerprint, item.id)
                while len(self._idempotency) > self._idempotency_limit:
                    self._idempotency.popitem(last=False)
            return item


DEFAULT_SEARCH_CONTROL = SearchControl()
