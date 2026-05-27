from __future__ import annotations

import os as _ap_os
import sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation
_isolation.isolate()


def _import_modules():
    root = _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__))))
    if root not in _ap_sys.path:
        _ap_sys.path.insert(0, root)
    from src import config, network
    return {"config": config, "network": network}


def test_parse_dns_input(m):
    n = m["network"]
    assert n.parse_dns_input("1.1.1.1, 8.8.8.8") == ["1.1.1.1", "8.8.8.8"]
    assert n.parse_dns_input("1.1.1.1 8.8.8.8") == ["1.1.1.1", "8.8.8.8"]
    assert n.parse_dns_input("dns.google") == ["dns.google"]
    assert n.parse_dns_input("dot://1.1.1.1:853?hostname=cloudflare-dns.com") == ["dot://1.1.1.1:853?hostname=cloudflare-dns.com"]
    assert n.parse_dns_input("https://dns.google/dns-query") == ["https://dns.google/dns-query"]
    try:
        n.parse_dns_input("ftp://dns.example.com")
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported DNS scheme should fail")


def test_normalize_socks5_url(m):
    n = m["network"]
    assert n.normalize_socks5_url("127.0.0.1:1080").url == "socks5://127.0.0.1:1080"
    assert n.normalize_socks5_url("tcp://127.0.0.1:1080").url == "socks5://127.0.0.1:1080"
    assert n.normalize_socks5_url("socks5h://example.com:1080").url == "socks5://example.com:1080"
    got = n.normalize_socks5_url("socks5://user:pass@example.com:1080")
    assert got.display_url == "socks5://user:***@example.com:1080"
    try:
        n.normalize_socks5_url("http://127.0.0.1:8080")
    except ValueError:
        pass
    else:
        raise AssertionError("http proxy should not be accepted as SOCKS5")


def test_save_network_config(m):
    cfg = m["config"]
    n = m["network"]
    n.save_dns_servers(["1.1.1.1"])
    assert cfg.get()["network"]["dns"]["servers"] == ["1.1.1.1"]
    assert cfg.get()["network"]["dns"]["bootstrapped"] is True
    saved = n.save_socks5("127.0.0.1:1080", enabled=True)
    assert saved == "socks5://127.0.0.1:1080"
    assert cfg.get()["network"]["socks5"]["enabled"] is True
    n.set_socks5_enabled(False)
    assert cfg.get()["network"]["socks5"]["enabled"] is False



def test_parse_ss2022_url_accepts_urlsafe_unpadded_key(m):
    import base64
    from src.proxy.connector import parse_proxy_url, connector_from_config
    key = bytes(range(32))
    password = base64.urlsafe_b64encode(key).decode().rstrip("=")
    userinfo = base64.urlsafe_b64encode(f"2022-blake3-aes-256-gcm:{password}".encode()).decode().rstrip("=")
    cfg = parse_proxy_url(f"ss://{userinfo}@127.0.0.1:8388#jp-main")
    assert cfg["type"] == "ss2022"
    assert cfg["name"] == "jp-main"
    conn = connector_from_config("jp-main", cfg)
    assert conn.type == "ss2022"
    assert conn.server == "127.0.0.1"
    assert conn.port == 8388
