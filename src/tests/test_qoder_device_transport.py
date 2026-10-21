"""Qoder-only route constraints and asynchronous final-send behavior."""
import asyncio
import json
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from src import config, network
from src.oauth.qoder import device_identity, common
from src.providers.qoder_codec import QoderWire, failure
from src.transports.http_runtime import _resolve_http_route_chain
from src.transports.policy import proxy_route_kwargs
from src.proxy import manager
from src.proxy.connector import ProxyConnectError
from src.management_control.public_safety import redact_known_fields
from src.tests.test_qoder_devices import isolated, account


def test_chat_model_and_identity_use_same_account_route_without_direct_fallback(isolated):
    config.update(lambda c: c["network"]["routing"].update(
        models={"model": "two"}, providers={"qoder": "one"}, oauth_openai="two"))
    channel = SimpleNamespace(key="oauth:qoder:global:u", account_key="qoder:global:u",
                              provider="qoder", type="oauth", protocol="openai-chat")
    kwargs = proxy_route_kwargs(channel, "model")
    assert kwargs["purpose"] == "oauth_qoder" and kwargs["model"] == ""
    chain, error = _resolve_http_route_chain(channel, "model")
    assert not error and [n for n, _ in chain] == ["one"]
    assert device_identity.route_selection(channel.account_key) == "one"
    config.update(lambda c: c["network"]["routing"].update(accounts={channel.key: "two"}))
    chain, error = _resolve_http_route_chain(channel, "model")
    assert not error and [n for n, _ in chain] == ["two"]
    assert device_identity.route_selection(channel.account_key) == "two"


def test_failed_proxy_never_tries_direct_for_umid_or_chat(isolated, monkeypatch):
    manager.init()
    class Broken:
        def open_sync_stream(self, *a, **kw):
            raise OSError("proxy refused")
    original = manager.get_connector
    calls = []
    def connector(name):
        calls.append(name)
        return Broken() if name == "one" else original(name)
    monkeypatch.setattr(manager, "get_connector", connector)
    with pytest.raises(ProxyConnectError):
        network.open_sync_stream("pum.m.alibaba.com", 443, timeout=.1, proxy_purpose="oauth_qoder")
    assert calls == ["one"]
    monkeypatch.setattr(manager, "get_connector", lambda name: None if name == "one" else original(name))
    channel = SimpleNamespace(key="oauth:qoder:global:u", account_key="qoder:global:u",
                              provider="qoder", type="oauth", protocol="openai-chat")
    chain, error = _resolve_http_route_chain(channel, "model")
    assert chain == [] and error.outcome == "proxy_connect_error"


async def test_umid_does_not_block_event_loop_and_plain_request_survives(monkeypatch):
    from src.tests.conftest import _ORIG_TO_THREAD
    monkeypatch.setattr(asyncio, "to_thread", _ORIG_TO_THREAD)
    entered, finish = threading.Event(), threading.Event()
    a = account()
    def identity(account, **kw):
        entered.set()
        assert finish.wait(3)
        assert account["qoder_device_profile"] == a["qoder_device_profile"] and kw["account_key"] == "account"
        return {"Cosy-MachineId": a["machine_id"], "Cosy-MachineToken": "issued", "Cosy-MachineType": "type",
                "Cosy-MachineCode": "code", "Cosy-MachineOS": "x86_64_linux"}
    monkeypatch.setattr(device_identity, "headers", identity)
    @dataclass
    class Request:
        content: bytes
        headers: dict
        url: str = "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?Encode=1"
    sent = []
    class Context:
        async def __aenter__(self):
            return SimpleNamespace(status_code=200)
        async def __aexit__(self, *a):
            pass
    def factory(req):
        sent.append(req)
        return Context()
    request = Request(b'{"messages":[{"content":"plain"}]}', {"X-Model-Key": "m"})
    wire = QoderWire(request, factory, a, "account")
    task = asyncio.create_task(wire.__aenter__())
    try:
        # This sleep can run while the synchronous identity provider is waiting.
        for _ in range(50):
            if entered.is_set():
                break
            await asyncio.sleep(.001)
        assert entered.is_set() and not task.done() and sent == []
    finally:
        finish.set()
    await task
    await wire.__aexit__(None, None, None)
    assert b"plain" in request.content and "Cosy-MachineToken" not in request.headers
    assert sent[0].headers["Cosy-MachineToken"] == "issued"
    assert sent[0].headers["Cosy-MachineCode"] == "code"
    assert sent[0].content != request.content


def test_device_failure_never_becomes_oauth_refresh_and_private_fields_redacted():
    assert failure(403, '{"code":"1001","message":"device refused"}')["status"] == 403
    assert failure(401, '{"code":"1001","message":"device refused"}')["status"] == 403
    assert failure(403, '{"code":"105","message":"login expired"}')["status"] == 401
    assert common.QoderError("device_identity", kind="umid_unavailable").auth_error is False
    a = account()
    raw = {**a, "uid": "public-user", "qoder_pat": "pt-private", "Cosy-MachineToken": "issued-private", "Cosy-MachineCode": "code-private"}
    public = json.dumps(redact_known_fields(raw))
    for secret in [a["qoder_device_profile"]["blocks"], a["machine_id"], "pt-private", "issued-private", "code-private"]:
        assert secret not in public
