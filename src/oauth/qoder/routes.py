"""Qoder physical-route snapshots: one connector for issuer and business IO.

ContextVars propagate through to_thread. A pinned route never re-resolves after
reload; group retries get a new member and prepare that member's own identity.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json

_current = ContextVar('qoder_physical_route', default=None)
_account = ContextVar('qoder_route_account', default='')


@dataclass(frozen=True)
class Route:
    name: str
    connector: object

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps([self.name, self.connector.config_dict()], sort_keys=True).encode()).hexdigest()


def current():
    return _current.get()


@contextmanager
def bind(route):
    token = _current.set(route)
    try:
        yield route
    finally:
        _current.reset(token)


@contextmanager
def account_scope(account_key):
    token = _account.set(account_key)
    from .. import add_context
    try:
        with add_context.bind(None):
            yield
    finally:
        _account.reset(token)


def channel_key(channel):
    return 'oauth:' + _account.get() if _account.get() else channel


def chain(account_key=''):
    from ... import config, network
    from ...proxy import manager
    from ...proxy.connector import DirectConnector, SOCKS5Connector
    pinned = current()
    if pinned is not None:
        return [pinned]
    with config.serialized_updates(), manager._lock:
        selected = network._configured_proxy_chain_or_none(
            proxy_purpose='oauth_qoder', proxy_provider='qoder', proxy_model='',
            proxy_channel=channel_key('oauth:' + account_key if account_key else 'oauth:qoder:login'))
        if selected is None:
            legacy = network.active_socks5_url()
            selected = [('legacy-socks5', SOCKS5Connector('legacy-socks5', legacy))] if legacy else [('direct', DirectConnector())]
        return [Route(name, connector) for name, connector in selected]


def selection(account_key=''):
    return ' → '.join(route.name for route in chain(account_key))
