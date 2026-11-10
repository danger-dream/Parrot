"""Process-wide Claude GET coordination; never coordinates or retries POSTs."""
from __future__ import annotations
import copy
import hashlib
import threading
import time
from email.utils import parsedate_to_datetime
from concurrent.futures import Future
import httpx

TTL = 60.0
MIN_INTERVAL = 1.0
_guard = threading.Lock()
_accounts = {}

class CoolingDown(Exception):
    def __init__(self, until):
        self.until = until
        super().__init__("Claude 查询限流冷却中；不会自动重试")

class _Account:
    def __init__(self):
        self.lock = threading.RLock()
        self.flight_lock = threading.Lock()
        self.flights = {}
        self.cache = {}
        self.until = 0
        self.next = 0
        self.failures = 0

def _account(generation):
    with _guard:
        return _accounts.setdefault(generation, _Account())

def _key(token, query):
    return hashlib.sha256(token.encode()).digest(), query

def retry_delay(value, now, failures):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            delay = -1
    if not 0 <= delay < float('inf'):
        return min(300, 30 * 2 ** min(failures, 4))
    return delay

def peek(generation, token, query):
    account = _account(generation)
    with account.lock:
        cached = account.cache.get(_key(token, query))
        return (copy.deepcopy(cached[0]), cached[1], account.until) if cached else (None, None, account.until)

def _read(generation, token, query, fetch, *, fresh=False):
    """Serialize account queries across threads/loops; cache exact semantics.

    Fresh validation bypasses cache, not cooldown. Token rotation invalidates
    values, not the account's upstream cooldown.
    """
    account = _account(generation)
    with account.lock:
        now = time.time()
        if account.until > now:
            raise CoolingDown(account.until)
        key = _key(token, query)
        cached = account.cache.get(key)
        if not fresh and cached and now - cached[1] < TTL:
            return copy.deepcopy(cached[0]), cached[1], True
        time.sleep(max(0, account.next - time.monotonic()))
        try:
            value = fetch()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                account.until = time.time() + retry_delay(exc.response.headers.get('Retry-After'), time.time(), account.failures)
                account.failures += 1
            raise
        finally:
            account.next = time.monotonic() + MIN_INTERVAL
        account.failures = 0
        stamp = time.time()
        account.cache[key] = (copy.deepcopy(value), stamp)
        return copy.deepcopy(value), stamp, False

def check_cooldown(generation):
    account = _account(generation)
    with account.lock:
        if account.until > time.time():
            raise CoolingDown(account.until)


def invalidate(generation):
    account = _account(generation)
    with account.lock:
        account.cache.clear()


def read(generation, token, query, fetch, *, fresh=False):
    account = _account(generation)
    key = (_key(token, query), fresh)
    with account.flight_lock:
        flight = account.flights.get(key)
        leader = flight is None
        if leader:
            flight = account.flights[key] = Future()
    if not leader:
        value, stamp, cached = copy.deepcopy(flight.result())
        # Reused ordinary evidence must not recover a restriction learned after
        # the original reader completed. Redeem still uses its fresh-only flight.
        return value, stamp, cached if fresh else True
    try:
        result = _read(generation, token, query, fetch, fresh=fresh)
        flight.set_result(result)
        return result
    except BaseException as exc:
        flight.set_exception(exc)
        raise
    finally:
        with account.flight_lock:
            account.flights.pop(key, None)
