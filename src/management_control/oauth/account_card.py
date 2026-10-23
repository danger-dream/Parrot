"""Structured OAuth list card: everything Telegram's account block shows.

Round 3. ``oauth_menu._format_account_block`` renders, per account: status
tag, plan line (provider specific), usage windows / credits, local period
usage (tokens, cache, TPS, amount, weekly projection) and cooldown counts.
This module returns the same facts as fields so the Web list never has to
call the detail endpoint per card. Everything reads cached state only; the
local-statistics aggregates use the shared non-blocking statistics cache
(``statsLoading`` mirrors TG's 「统计初始化中」).
"""

from __future__ import annotations

import calendar
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from src import quota_errors
from src.cooldown import ACCOUNT_SCOPE
from src.oauth.plan_labels import openai_plan_label, xai_plan_label

from .usage_sections import _time, build_usage_sections

_BJT = timezone(timedelta(hours=8))
CARD_STATS_MAX_STALE = 6 * 3600.0


def _raw(row: dict | None) -> dict:
    try:
        value = json.loads((row or {}).get("raw_data") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _shift_month(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def local_period(provider: str, account: dict, row: dict | None, now: datetime | None = None) -> dict:
    """TG ``_oauth_local_period``: provider period when known, else natural month."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start, source = None, ""
    if provider == "openai":
        expiry = _parse(account.get("subscription_expires_at"))
        if expiry is not None:
            candidate = _shift_month(expiry, -1)
            if candidate <= now <= expiry + timedelta(minutes=5):
                start, source = candidate, "openai_inferred_subscription"
    elif provider == "claude":
        anchor = _parse(account.get("subscription_created_at"))
        if anchor is not None and anchor <= now:
            months = (now.year - anchor.year) * 12 + now.month - anchor.month
            candidate = _shift_month(anchor, months)
            if candidate > now:
                candidate = _shift_month(anchor, months - 1)
            start, source = candidate, "claude_inferred_subscription"
    elif provider == "xai":
        billing = ((_raw(row).get("xai") or {}).get("billing") or {})
        begin, end = _parse(billing.get("period_start")), _parse(billing.get("period_end"))
        if begin is not None and (end is not None and end > begin
                                  or billing.get("period_type") == "USAGE_PERIOD_TYPE_WEEKLY"):
            start, source = begin, "xai_official_week"
    elif provider == "cursor":
        begin = _parse(account.get("billing_cycle_start") or (_raw(row).get("cursor") or {}).get("billing_cycle_start"))
        if begin is not None:
            start, source = begin, "cursor_official_billing"
    if start is None:
        month = now.astimezone(_BJT).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {"source": "natural_month", "label": "本地自然月", "money_label": "自然月",
                "since": month.timestamp()}
    labels = {"openai": ("套餐本期", "本期"), "claude": ("套餐本期", "本期"),
              "xai": ("本周", "本周"), "cursor": ("账单本期", "账单本期")}
    label, money = labels[provider]
    return {"source": source, "label": label, "money_label": money, "since": start.timestamp()}


def window_since(reset_iso: Any, window_seconds: int, now_ts: float) -> float:
    """TG ``_quota_window_since_ts``."""
    fallback = now_ts - window_seconds
    reset = _parse(reset_iso)
    if reset is None:
        return fallback
    reset_ts = reset.timestamp()
    if reset_ts <= now_ts - 60 or reset_ts > now_ts + window_seconds + 300:
        return fallback
    return reset_ts - window_seconds


def card_stats_key(account_id: str, since: float, rolling_seconds: int | None = None,
                   scope: str | None = None) -> tuple:
    """Statistics-cache key of one card figure.

    A window without a usable reset time (Claude sends none while the 5h window is unused) rolls
    with the clock, so it gets one stable key instead of a new one every minute that would never
    finish loading. Anchored windows use minute granularity: re-read reset times jitter by seconds.
    """
    head = ("oauth-card", account_id) + ((scope,) if scope else ())
    if rolling_seconds:
        return head + ("rolling", int(rolling_seconds))
    return head + (int(since // 60),)


def stats_view(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    number = lambda key: int(raw.get(key) or 0)
    prompt = number("input") + number("cache_creation") + number("cache_read")
    return {
        "total": number("total"),
        "success_count": number("success_count"),
        "error_count": number("error_count"),
        "input_tokens": number("input"),
        "output_tokens": number("output"),
        "cache_creation_tokens": number("cache_creation"),
        "cache_read_tokens": number("cache_read"),
        "prompt_tokens": prompt,
        "cache_read_ratio": (number("cache_read") / prompt) if prompt > 0 else None,
        "average_tokens_per_second": raw.get("avg_tps"),
        "maximum_tokens_per_second": raw.get("max_tps"),
        "minimum_tokens_per_second": raw.get("min_tps"),
        "cost_ticks": number("cost_ticks"),
        "actual_cost_ticks": number("actual_cost_ticks"),
        "estimated_cost_ticks": number("estimated_cost_ticks"),
        "actual_costed_success": number("actual_costed_success"),
        "costed_success": number("costed_success"),
        "unpriced_success": number("unpriced_success"),
    }


def weekly_projection(provider: str, row: dict | None, stats_7d: dict | None) -> dict | None:
    """TG ``_weekly_quota_projection``: one full weekly quota from this week's usage."""
    if provider not in {"claude", "openai", "xai", "zhipu"} or not isinstance(row, dict) or not stats_7d:
        return None
    try:
        used = float(row.get("seven_day_util"))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(used) or used <= 0 or used > 100:
        return None
    tokens = stats_7d["prompt_tokens"] + stats_7d["output_tokens"]
    if tokens <= 0:
        return None
    denominator = Decimal(str(used))
    project = lambda value: int((Decimal(value) * 100 / denominator).to_integral_value(rounding=ROUND_HALF_UP))
    costed = stats_7d["cost_ticks"] > 0 or stats_7d["costed_success"] > 0
    return {"tokens": project(tokens), "cost_ticks": project(stats_7d["cost_ticks"]) if costed else None}


_WINDOW_LABELS = {"5 小时": "fiveHour", "7 天": "sevenDay", "Fable 7 天": "fable"}


def _with_window_local(sections, windows: dict) -> tuple:
    """TG prints the local Parrot usage under each 5h / 7d bar: attach it as ``local``."""
    out = []
    for section in sections:
        if section.get("id") != "windows":
            out.append(section)
            continue
        items = []
        for entry in section["items"]:
            window = windows.get(_WINDOW_LABELS.get(entry.get("label"), ""))
            items.append({**entry, "local": window.get("stats") if window else None})
        out.append({**section, "items": items})
    return tuple(out)


def runtime_errors(provider: str, account_id: str, account: dict, entries: list, backend) -> list[dict]:
    """TG 冷却中的模型 / 配额暂停 / 供应商限制, with kind, pool and error count."""
    key = f"oauth:{account_id}"
    selected = {str(model) for model in account.get("models") or []}
    disabled = set(backend.cursor_disabled_models(account)) if provider == "cursor" else set()
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("channel_key") != key:
            continue
        message = entry.get("last_error_message")
        kind, pool = "fault", None
        if entry.get("model") == ACCOUNT_SCOPE:
            kind = "accountRestriction"
        elif provider == "cursor" and quota_errors.is_cursor_pool_quota_message(message):
            kind = "quotaPause"
            try:
                pool = (json.loads(message).get("error") or {}).get("pool")
            except (TypeError, ValueError, AttributeError):
                pool = None
            pool = pool if pool in ("cursor_models", "other_models") else "unknown"
            if entry.get("model") not in selected or entry.get("model") in disabled:
                kind = "quotaPauseHidden"  # TG omits hidden/disabled models from the group
        out.append({"model": entry.get("model"), "kind": kind, "pool": pool,
                    "error_count": int(entry.get("error_count") or 0)})
    return out


class OAuthAccountCardMixin:
    """Adds ``card`` to summaries (list page items and the detail's account)."""

    def _with_cards(self, summaries, context=None, *, blocking: bool = False):
        from src.management_auth import AuthMethod
        if context is not None and context.actor.auth_method is AuthMethod.TELEGRAM_ADMIN:
            return list(summaries)  # Telegram renders from its own caches
        try:
            cooldowns = list(self.backend.cooldown_entries())
        except Exception:
            cooldowns = []
        out = []
        for summary in summaries:
            try:
                account = self.backend.get_account_exact(summary.account_id) or {}
                card = self._account_card(summary.account_id, summary.provider.value, account, cooldowns,
                                          blocking=blocking)
            except Exception:
                card = None  # presentation only; never fail the list
            out.append(replace(summary, card=card))
        return out

    def warm_card_stats(self) -> None:
        """Compute every account's list-card statistics once, off the request path."""
        try:
            cooldowns = list(self.backend.cooldown_entries())
        except Exception:
            cooldowns = []
        for account in self.backend.list_accounts():
            try:
                account_id = self.backend.account_id(account)
                provider = self.backend.provider_of(account)
                self._account_card(account_id, provider, account, cooldowns, blocking=True)
            except Exception:
                continue

    def _now_utc(self) -> datetime:
        clock = getattr(self, "_clock", None)
        try:
            value = clock() if clock is not None else None
        except Exception:
            value = None
        return value if isinstance(value, datetime) and value.tzinfo else datetime.now(timezone.utc)

    def _local_stats(self, account_id: str, since: float, blocking: bool = False, *,
                     rolling_seconds: int | None = None, models: tuple[str, ...] | None = None,
                     scope: str | None = None) -> tuple[dict | None, bool]:
        """``models`` limits the figure to those models (TG's Fable 7d line); ``scope`` names it in the key."""
        from src.management_control.stats_cache import STATS_CACHE, cached
        from .backend import OAuthBackend

        channel = f"oauth:{account_id}"
        if models is None:
            loader = lambda: self.backend.tokens_for_channel(channel, since) or {}
        else:
            loader = lambda: self.backend.tokens_for_channel_models(channel, list(models), since) or {}
        key = card_stats_key(account_id, since, rolling_seconds, scope)
        if type(self.backend) is OAuthBackend and blocking:
            value, loading = cached(key, loader), False  # single-account detail
        elif type(self.backend) is OAuthBackend:
            # Each figure costs a scan of the account's attempts; showing the last value (refreshed
            # in the background) beats an empty card whenever the page was not opened for a while.
            value, loading = STATS_CACHE.peek_or_refresh(key, loader, max_stale_seconds=CARD_STATS_MAX_STALE)
        else:
            value, loading = loader(), False
        return stats_view(value), loading

    def _fable_window(self, account_id: str, account: dict, row: dict, now_ts: float, blocking: bool) -> dict:
        """Local usage of the account's Fable models inside the Fable 7d window (TG ``fable-7d``)."""
        try:
            util, reset = self.backend.fable_display_from_quota_row(row)
            models = tuple(self.backend.claude_fable_models(account))
        except Exception:
            return {}
        if util is None:
            return {}
        seconds = 7 * 86400
        since = window_since(reset, seconds, now_ts)
        rolling = seconds if since == now_ts - seconds else None
        stats, loading = self._local_stats(account_id, since, blocking, rolling_seconds=rolling,
                                           models=models, scope="fable")
        return {"fable": {"since": datetime.fromtimestamp(since, timezone.utc), "stats": stats,
                          "stats_loading": loading}}

    def _plan_facts(self, provider: str, account: dict, row: dict) -> dict:
        raw = _raw(row)
        facts: dict[str, Any] = {}
        if provider == "openai":
            plan = openai_plan_label(account.get("plan_type"))
            email = str(account.get("email") or "")
            same = sum(1 for item in self.backend.list_accounts()
                       if self.backend.provider_of(item) == "openai" and str(item.get("email") or "") == email)
            name, wtype = str(account.get("workspace_name") or "").strip(), str(account.get("workspace_type") or "").strip()
            label = None
            if same > 1:
                label = (name if name and not (name.lower() == "personal" and "team" in f"{wtype} {account.get('plan_type') or ''}".lower())
                         else wtype or "workspace")
            summary = ((raw.get("openai") or {}).get("rate_limit_reset_credits")
                       or raw.get("rate_limit_reset_credits") or {})
            try:
                resets = int(summary.get("available_count")) if isinstance(summary, dict) else None
            except (TypeError, ValueError):
                resets = None
            facts.update(label=plan or None, workspace_label=label, reset_credit_count=resets,
                         expires_at=_time(account.get("subscription_expires_at")),
                         credits=self._openai_credit_facts(row))
        elif provider == "claude":
            facts.update(label=self.backend.claude_plan_label(account) or None,
                         status=account.get("subscription_status") or None,
                         billing_type=account.get("billing_type") or None,
                         started_at=_time(account.get("subscription_created_at")))
        elif provider == "xai":
            xai = raw.get("xai") if isinstance(raw.get("xai"), dict) else {}
            labels = []
            for value in ((xai.get("user") or {}).get("subscription_tier"),
                          (xai.get("settings") or {}).get("subscription_tier_display")):
                text = xai_plan_label(value)
                if text and text not in labels:
                    labels.append(text)
            user = xai.get("user") if isinstance(xai.get("user"), dict) else {}
            settings = xai.get("settings") if isinstance(xai.get("settings"), dict) else {}
            blocked = bool(user.get("blocked") or user.get("user_blocked_reason") or user.get("team_blocked_reasons"))
            state = ("blocked" if blocked else "limited" if settings.get("allow_access") is False
                     else "ok" if settings.get("allow_access") is True or user.get("has_grok_code_access") is True else None)
            code = user.get("has_grok_code_access")
            facts.update(label=" / ".join(labels) or None, tier=" / ".join(labels) or None,
                         access={"state": state, "grok_code": code if isinstance(code, bool) else None}
                         if state or isinstance(code, bool) else None)
        elif provider == "cursor":
            records = ((account.get("cursor_model_catalog") or {}).get("models") or [])
            facts.update(label=str(account.get("plan_type") or "Cursor"),
                         variant_count=sum(len(item.get("legacy_slugs") or []) for item in records if isinstance(item, dict)))
        elif provider == "antigravity":
            from src.oauth import antigravity
            block = raw.get("antigravity") if isinstance(raw.get("antigravity"), dict) else {}
            tier = antigravity.credits_tier_label(block) or None
            facts.update(label=tier, tier=tier,
                         text_model_count=len([m for m in account.get("models") or [] if str(m).strip()]),
                         image_model_count=0)  # TG _antigravity_catalog_counts: images not counted
        elif provider == "zhipu":
            from src.model_state import state_snapshot
            selection = self.backend.account_model_selection(account)
            models = set(selection.get("models") or [])
            try:
                global_disabled = len(models & set(state_snapshot(self.backend.config_snapshot())["disabledModels"]))
            except Exception:
                global_disabled = None
            facts.update(label=("团队" if account.get("plan_scope") == "team" else "个人") + " Coding Plan",
                         global_disabled_model_count=global_disabled,
                         organization_id=account.get("organization_id") or None,
                         project_id=account.get("project_id") or None,
                         site=account.get("site") or None,
                         credential_mode="api_key" if account.get("credential_mode") == "api_key" else "oauth",
                         entitlement=account.get("entitlement") or None,
                         management_status=account.get("management_status") or None,
                         setup_incomplete=not (account.get("model_key") or account.get("project_key_id")))
        elif provider == "workbuddy":
            kind = account.get("workbuddy_account_type") or ""
            scope = {"personal": "个人", "pro": "个人 Pro", "ultimate": "企业版", "exclusive": "专享版",
                     "premise": "私有部署"}.get(kind) or ("类型未知" if kind else "企业" if account.get("enterprise_id") else "个人")
            facts.update(label=scope, region="cn" if account.get("realm") == "cn" else "global",
                         account_type=kind or None)
        elif provider == "qoder":
            facts.update(auth_method="pat" if account.get("qoder_auth_method") == "pat" else "device",
                         device_chat=bool(account.get("qoder_device_chat")), region=account.get("realm") or None)
        return facts

    def _openai_credit_facts(self, row: dict) -> dict | None:
        """TG ``_format_openai_credits`` observation facts (observedAt / source / stale)."""
        codex = (_raw(row).get("openai") or {})
        credits = codex.get("credits") if isinstance(codex.get("credits"), dict) else {}
        if not credits:
            return None
        key = ("unlimited" if credits.get("unlimited") is True
               else "balance" if credits.get("balance") not in (None, "", 0, "0") else "has_credits")
        observed = int((codex.get("credits_observed_at") or {}).get(key) or 0)
        failed = int((row or {}).get("codex_usage_failed_at") or 0)
        source = (codex.get("credits_sources") or {}).get(key)
        return {"observed_at": _time(observed) if observed else None,
                "source": ("official_usage" if source == "wham_usage" else "response_sample") if source else None,
                "stale": failed > 0 and failed >= observed}

    def _cooldown_facts(self, provider: str, account_id: str, account: dict, cooldowns: list) -> dict:
        key = f"oauth:{account_id}"
        entries = [e for e in cooldowns if isinstance(e, dict) and e.get("channel_key") == key]
        paused = any(e.get("model") == ACCOUNT_SCOPE for e in entries)
        entries = [e for e in entries if e.get("model") != ACCOUNT_SCOPE]
        quota_paused = 0
        if provider == "cursor":
            selected = {str(model) for model in account.get("models") or []}
            disabled = set(self.backend.cursor_disabled_models(account))
            faults = []
            for entry in entries:
                if quota_errors.is_cursor_pool_quota_message(entry.get("last_error_message")):
                    if entry.get("model") in selected and entry.get("model") not in disabled:
                        quota_paused += 1
                    continue
                faults.append(entry)
            entries = faults
        permanent = sum(1 for e in entries if e.get("cooldown_until") == -1)
        return {"account_paused": paused, "quota_paused_models": quota_paused,
                "permanent_models": permanent, "cooling_models": len(entries) - permanent}

    def _account_card(self, account_id: str, provider: str, account: dict, cooldowns: list, *,
                      light: bool = False, blocking: bool = False) -> dict:
        """``light`` skips local statistics (used per source on the model detail)."""
        row = self.backend.quota_load(account_id) or {}
        reason = account.get("disabled_reason")
        status = ("userDisabled" if reason == "user" else "quotaDisabled" if reason == "quota"
                  else "authFailed" if reason == "auth_error" else None)
        cooldown = self._cooldown_facts(provider, account_id, account, cooldowns)
        if status is None and provider == "workbuddy" and cooldown["account_paused"]:
            status = "providerRestricted"
        snapshot = (self.backend.workbuddy_snapshot(account_id) if provider == "workbuddy"
                    else self.backend.qoder_snapshot(account_id) if provider == "qoder" else None)
        zhipu = self.backend.zhipu_snapshot(account_id) if provider == "zhipu" else None
        sections = build_usage_sections(provider, row, self.backend, zhipu_snapshot=zhipu, provider_snapshot=snapshot)
        if light:
            return {"status_tag": status, "plan": self._plan_facts(provider, account, row),
                    "usage_sections": sections, "usage_fetched_at": _time(row.get("fetched_at")),
                    "cooldown": cooldown}
        now = self._now_utc()
        period = local_period(provider, account, row, now)
        period_stats, loading = self._local_stats(account_id, period["since"], blocking)
        now_ts = now.timestamp()
        windows = {}
        if provider in {"claude", "openai", "zhipu", "xai"}:
            for name, util_key, reset_key, seconds in (("fiveHour", "five_hour_util", "five_hour_reset", 5 * 3600),
                                                       ("sevenDay", "seven_day_util", "seven_day_reset", 7 * 86400)):
                if row.get(util_key) is None or (provider == "xai" and name == "fiveHour"):
                    continue
                since = window_since(row.get(reset_key), seconds, now_ts)
                rolling = seconds if since == now_ts - seconds else None
                stats, window_loading = self._local_stats(account_id, since, blocking, rolling_seconds=rolling)
                windows[name] = {"since": datetime.fromtimestamp(since, timezone.utc), "stats": stats,
                                 "stats_loading": window_loading}
        if provider == "claude":
            windows.update(self._fable_window(account_id, account, row, now_ts, blocking))
        projection = weekly_projection(provider, row, (windows.get("sevenDay") or {}).get("stats"))
        sections = _with_window_local(sections, windows)
        return {
            "status_tag": status,
            "plan": self._plan_facts(provider, account, row),
            "usage_sections": sections,
            "usage_fetched_at": _time(row.get("fetched_at")),
            "local_period": {
                "source": period["source"], "label": period["label"], "money_label": period["money_label"],
                "since": datetime.fromtimestamp(period["since"], timezone.utc),
                "stats": period_stats, "stats_loading": loading,
            },
            "window_stats": windows,
            "weekly_projection": projection,
            "cooldown": cooldown,
        }


    def _enrich_detail(self, detail, account: dict):
        """Round 3: detail = the TG detail facts (plan, honest-period stats,
        window local usage, weekly projection, typed runtime errors)."""
        from .models import OAuthLocalStats
        card = detail.account.card or {}
        period = card.get("local_period") or {}
        stats = period.get("stats")
        facts = runtime_errors(detail.account.provider.value, detail.account.account_id, account,
                               list(self.backend.cooldown_entries()), self.backend)
        errors = tuple(replace(error, kind=fact["kind"], pool=fact["pool"], error_count=fact["error_count"])
                       for error, fact in zip(detail.runtime_errors, facts))
        local = detail.local_stats
        if stats:
            from src.model_pricing import TICKS_PER_USD
            local = OAuthLocalStats(
                request_count=stats["total"], input_tokens=stats["input_tokens"], output_tokens=stats["output_tokens"],
                cost_usd=stats["cost_ticks"] / TICKS_PER_USD if stats["costed_success"] else None,
                details={**stats, "period_label": period.get("label"), "period_source": period.get("source"),
                         "money_label": period.get("money_label"), "since": period.get("since")},
            )
        return replace(
            detail, runtime_errors=errors, local_stats=local,
            usage_sections=card.get("usage_sections", detail.usage_sections),
            plan=card.get("plan"), weekly_projection=card.get("weekly_projection"),
            window_stats=card.get("window_stats"), last_refresh_at=_time(account.get("last_refresh")),
        )
