from __future__ import annotations

import os as _ap_os
import socket
import sys as _ap_sys

import pytest

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    from src import network
    return {"network": network}


class _Answer:
    def __init__(self, value: str):
        self._value = value

    def to_text(self) -> str:
        return self._value


class _RRSet(list):
    def __init__(self, rdtype, values: list[str]):
        super().__init__(_Answer(value) for value in values)
        self.rdtype = rdtype


class _Response:
    def __init__(self, network, qtype: str, values: list[str]):
        self.answer = [_RRSet(network.dns.rdatatype.from_text(qtype), values)] if values else []
        self._network = network

    def rcode(self):
        return self._network.dns.rcode.NOERROR


def _install_dns_plan(monkeypatch, network, plan, *, usable_families):
    calls: list[tuple[str, str]] = []

    def query(spec, _host, qtype, _timeout):
        calls.append((spec.raw, qtype))
        outcome = plan[(spec.raw, qtype)]
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(network, qtype, outcome)

    monkeypatch.setattr(network, "_query_with_server", query)
    monkeypatch.setattr(
        network,
        "_is_address_usable",
        lambda ip: (socket.AF_INET6 if ":" in ip else socket.AF_INET) in usable_families,
    )
    monkeypatch.setattr(network, "dns_cache_ttl", lambda: 300)
    network.clear_dns_cache()
    return calls


def test_unspec_partial_aaaa_does_not_stop_later_a_resolver(m, monkeypatch):
    network = m["network"]
    plan = {
        ("1.1.1.1", "A"): TimeoutError("A timeout"),
        ("1.1.1.1", "AAAA"): ["2001:db8::10"],
        ("8.8.8.8", "A"): ["192.0.2.10"],
        ("8.8.8.8", "AAAA"): [],
    }
    calls = _install_dns_plan(
        monkeypatch, network, plan, usable_families={socket.AF_INET},
    )

    assert network.resolve_host("api.telegram.org", servers=["1.1.1.1", "8.8.8.8"]) == ["192.0.2.10"]
    assert ("8.8.8.8", "A") in calls
    assert network.dns_cache_entries()[0]["ips"] == ["192.0.2.10"]


def test_ipv4_only_aaaa_only_is_failure_and_not_positive_cached(m, monkeypatch):
    network = m["network"]
    plan = {
        ("1.1.1.1", "A"): TimeoutError("A timeout"),
        ("1.1.1.1", "AAAA"): ["2001:db8::10"],
        ("8.8.8.8", "A"): TimeoutError("A timeout"),
        ("8.8.8.8", "AAAA"): ["2001:db8::11"],
    }
    calls = _install_dns_plan(
        monkeypatch, network, plan, usable_families={socket.AF_INET},
    )

    for _ in range(2):
        with pytest.raises(OSError, match="DNS resolve failed"):
            network.resolve_host("api.telegram.org", servers=["1.1.1.1", "8.8.8.8"])
    assert len(calls) == 8  # both attempts queried both families on both resolvers
    assert network.dns_cache_entries() == []


def test_ipv6_capable_host_retains_aaaa_only_resolution(m, monkeypatch):
    network = m["network"]
    plan = {
        ("1.1.1.1", "A"): TimeoutError("A timeout"),
        ("1.1.1.1", "AAAA"): ["2001:db8::10"],
        ("8.8.8.8", "A"): TimeoutError("A timeout"),
        ("8.8.8.8", "AAAA"): ["2001:db8::11"],
    }
    _install_dns_plan(
        monkeypatch, network, plan, usable_families={socket.AF_INET, socket.AF_INET6},
    )

    assert network.resolve_host("v6.example", servers=["1.1.1.1", "8.8.8.8"]) == [
        "2001:db8::10", "2001:db8::11",
    ]


def test_normal_dual_stack_still_returns_both_and_stops(m, monkeypatch):
    network = m["network"]
    plan = {
        ("1.1.1.1", "A"): ["192.0.2.10"],
        ("1.1.1.1", "AAAA"): ["2001:db8::10"],
        ("8.8.8.8", "A"): ["192.0.2.11"],
        ("8.8.8.8", "AAAA"): ["2001:db8::11"],
    }
    calls = _install_dns_plan(
        monkeypatch, network, plan, usable_families={socket.AF_INET, socket.AF_INET6},
    )

    assert network.resolve_host("dual.example", servers=["1.1.1.1", "8.8.8.8"]) == [
        "192.0.2.10", "2001:db8::10",
    ]
    assert calls == [("1.1.1.1", "A"), ("1.1.1.1", "AAAA")]


def test_targeted_cache_invalidation_preserves_other_hosts(m):
    network = m["network"]
    network.clear_dns_cache()
    telegram_key = ("api.telegram.org", 0, 0, 0, 0, ("1.1.1.1",))
    other_key = ("api.github.com", 0, 0, 0, 0, ("1.1.1.1",))
    with network._LOCK:
        network._CACHE[telegram_key] = (network.time.time() + 300, ["192.0.2.10"])
        network._CACHE[other_key] = (network.time.time() + 300, ["192.0.2.20"])

    assert network.invalidate_dns_cache("API.TELEGRAM.ORG") == 1
    assert [entry["host"] for entry in network.dns_cache_entries()] == ["api.github.com"]
