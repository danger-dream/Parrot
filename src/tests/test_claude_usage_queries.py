"""Isolated production-path Claude usage coordination and reset safety."""
import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate

import httpx
import pytest

from src import oauth_manager as om, state_db
from src.oauth import claude_reset as cr, claude_usage_queries as q
from src.tests.test_claude_reset import env, final_plan


def response(code, value=None, headers=None):
    return httpx.Response(code, json=value or {}, headers=headers, request=httpx.Request('GET', om.OAUTH_USAGE_URL))


def test_background_manual_repeated_threads_share_exact_request(env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def get(url, **kw):
        calls.append(url)
        entered.set()
        assert release.wait(5)
        return env.get(url, **kw)
    monkeypatch.setattr(cr.network, 'get_sync', get)
    with ThreadPoolExecutor(6) as pool:
        first = pool.submit(asyncio.run, om.fetch_usage_snapshot(env.key))
        assert entered.wait(5)
        others = [pool.submit(asyncio.run, om.fetch_usage(env.key)) for _ in range(4)]
        release.set()
        values = [first.result()] + [f.result() for f in others]
    assert len(calls) == 1
    assert all(v['extra_usage'] == env.usage['extra_usage'] for v in values)
    assert not env.posts


def test_status_concurrent_and_query_parameters_remain_distinct(env, monkeypatch):
    with ThreadPoolExecutor(6) as pool:
        values = list(pool.map(lambda _: env.control.refresh_claude_reset_status(env.ctx, env.key), range(6)))
    assert len(env.gets) == 1
    assert all(v['cedar_ember'] == env.usage['cedar_ember'] for v in values)
    env.control.refresh_claude_reset_status(env.ctx, env.key, 'juniper_tide')
    asyncio.run(om.fetch_usage_snapshot(env.key))
    assert [url for url, _ in env.gets] == [om.OAUTH_USAGE_URL + '?cedar_ember=1&skip_spend=1',
        om.OAUTH_USAGE_URL + '?at_wall=1&skip_spend=1', om.OAUTH_USAGE_URL]


@pytest.mark.parametrize('header', ['120', 'date', None, 'invalid', '-2', 'nan'])
def test_real_429_shared_cooldown_expiry_and_no_token_refresh(env, monkeypatch, header):
    clock = [time.time()]
    monkeypatch.setattr(q.time, 'time', lambda: clock[0])
    calls, refreshes = [], []
    if header == 'date':
        header = formatdate(clock[0] + 120, usegmt=True)
    def get(url, **kw):
        calls.append(url)
        return response(429, headers={'Retry-After': header} if header else {})
    monkeypatch.setattr(cr.network, 'get_sync', get)
    async def refresh(*a, **kw):
        refreshes.append(True)
        raise AssertionError('429 must not refresh credentials')
    monkeypatch.setattr(om, 'force_refresh', refresh)
    result = env.control.plan_claude_reset(env.ctx, env.key, 'cedar_ember')
    assert not result['available']
    obs = result['status']['claude_reset_queries']['cedar_ember']
    assert obs['http_status'] == 429 and obs['retry_at'] > clock[0] * 1000
    assert not env.control.plan_claude_reset(env.ctx, env.key, 'juniper_tide')['available']
    with pytest.raises(q.CoolingDown):
        asyncio.run(om.fetch_usage_snapshot(env.key))
    assert len(calls) == 1 and not refreshes and not env.posts
    clock[0] = obs['retry_at'] / 1000 + 1
    monkeypatch.setattr(cr.network, 'get_sync', env.get)
    assert asyncio.run(om.fetch_usage_snapshot(env.key))['five_hour'] == env.usage['five_hour']


def test_failure_preserves_success_data_and_disables_plan(env, monkeypatch):
    first = env.control.plan_claude_reset(env.ctx, env.key, 'cedar_ember')
    stamp = first['status']['claude_reset_queries']['cedar_ember']['fetched_at']
    monkeypatch.setattr(q, 'TTL', 0)
    monkeypatch.setattr(cr.network, 'get_sync', lambda *a, **kw: response(429, headers={'Retry-After': '90'}))
    failed = env.control.plan_claude_reset(env.ctx, env.key, 'cedar_ember')
    assert not failed['available'] and 'plan_token' not in failed
    assert failed['status']['cedar_ember'] == first['status']['cedar_ember']
    assert failed['status']['claude_reset_queries']['cedar_ember']['fetched_at'] == stamp
    assert not env.posts


def test_preconsume_requires_new_status_and_cooldown_blocks_existing_plan(env, monkeypatch):
    token = final_plan(env)
    env.usage['cedar_ember']['eligible'] = False
    result = env.control.execute_claude_reset(env.ctx, env.key, token)
    assert result['result'] == 'ineligible' and not env.posts
    env.usage['cedar_ember']['eligible'] = True
    state_key = om.account_state_key(om.get_account(env.key))
    q.invalidate(state_key)
    token = final_plan(env)
    monkeypatch.setattr(cr.network, 'get_sync', lambda *a, **kw: response(429))
    env.control.refresh_claude_reset_status(env.ctx, env.key, 'juniper_tide')
    # When cache is invalidated (no fresh <60s observation available), cooldown blocks blind dispatch:
    q.invalidate(state_key)
    with pytest.raises(q.CoolingDown):
        env.control.execute_claude_reset(env.ctx, env.key, token)
    assert not env.posts


def test_generation_token_isolation_and_cooldown_survives_refresh(env, monkeypatch):
    a = om.account_state_key(om.get_account(env.key))
    assert q.read(a, 'old', 'usage', lambda: {'n': 1})[0] == {'n': 1}
    assert q.read(a, 'new', 'usage', lambda: {'n': 2})[0] == {'n': 2}
    assert q.read(a + '-replacement', 'old', 'usage', lambda: {'n': 3})[0] == {'n': 3}
    def fail():
        response(429).raise_for_status()
    with pytest.raises(httpx.HTTPStatusError):
        q.read(a, 'new', 'cedar_ember', fail)
    with pytest.raises(q.CoolingDown):
        q.read(a, 'newer', 'usage', lambda: {})


def test_partial_success_never_erases_other_program(env, monkeypatch):
    first = env.control.refresh_claude_reset_status(env.ctx, env.key)
    monkeypatch.setattr(cr.network, 'get_sync', lambda *a, **kw: response(429))
    second = env.control.refresh_claude_reset_status(env.ctx, env.key, 'juniper_tide')
    assert second['claude_reset_queries']['juniper_tide']['state'] == 'error'
    import json
    raw = json.loads(state_db.quota_load(env.key)['raw_data'])
    assert raw['cedar_ember'] == first['cedar_ember']
    assert 'juniper_tide' not in raw


def test_cached_usage_cannot_resume_new_quota_pause(env):
    asyncio.run(om.fetch_usage_snapshot(env.key))
    om.set_disabled_by_quota(env.key, '2030-01-01T00:00:00Z')
    cached = asyncio.run(om.fetch_usage_snapshot(env.key))
    assert cached['claude_usage_query']['cached']
    om.evaluate_and_toggle_by_usage(env.key, cached)
    assert om.get_account(env.key)['disabled_reason'] == 'quota'


def test_bounded_fallback():
    assert q.retry_delay(None, 0, 0) == 30
    assert q.retry_delay('bad', 0, 20) == 300
    assert q.retry_delay('0', 0, 0) == 0


def test_inflight_replacement_and_token_rotation_cannot_publish(env, monkeypatch):
    for replace in (False, True):
        q._accounts.clear()
        def get(url, **kw):
            if replace:
                om.delete_account(env.key)
                om.add_account(env.entry)
            else:
                assert om._save_token_fields(env.key, {"access_token": "rotated"})
            return response(200, copy.deepcopy(env.usage))
        monkeypatch.setattr(cr.network, 'get_sync', get)
        with pytest.raises(ValueError, match='stale_'):
            asyncio.run(cr.status(env.key))
        assert not env.posts


def test_status_401_once_then_success_and_second_401_stops(env, monkeypatch):
    refreshes, calls = [], []
    async def refresh(key, **kw):
        refreshes.append(True)
        om._save_token_fields(key, {"access_token": "new-token"})
        return 'new-token'
    def get(url, **kw):
        calls.append(kw['headers']['Authorization'])
        return response(401) if len(calls) == 1 else response(200, env.usage)
    monkeypatch.setattr(om, 'force_refresh', refresh)
    monkeypatch.setattr(cr.network, 'get_sync', get)
    assert asyncio.run(cr.status(env.key))['cedar_ember']['eligible']
    assert calls == ['Bearer fake-at', 'Bearer new-token'] and len(refreshes) == 1
    q.invalidate(om.account_state_key(om.get_account(env.key)))
    calls.clear()
    monkeypatch.setattr(cr.network, 'get_sync', lambda *a, **kw: response(401))
    result = asyncio.run(cr.status(env.key))
    assert result['claude_reset_queries']['cedar_ember']['http_status'] == 401
    assert len(refreshes) == 2 and not env.posts


def test_serial_account_queries_are_paced(env, monkeypatch):
    monkeypatch.setattr(q, 'MIN_INTERVAL', .03)
    times = []
    def fetch():
        times.append(time.monotonic())
        return {}
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda name: q.read('paced', 'token', name, fetch), ['usage', 'cedar_ember', 'juniper_tide']))
    assert len(times) == 3 and all(b - a >= .025 for a, b in zip(times, times[1:]))
