"""外网 IPv4 获取（启动时一次性，后台线程，不阻塞 lifespan）。

获取失败时返回 None；菜单不显示公网地址。
"""

from __future__ import annotations

import threading
from typing import Optional

import httpx


_PUBLIC_IP: Optional[str] = None
_fetched = False
_lock = threading.Lock()

# 多个 fallback 端点。优先 ip.sb（速度通常最快），失败回退到 icanhazip.com。
_ENDPOINTS = (
    "https://api.ip.sb/ip",
    "https://4.icanhazip.com",
    "https://ipv4.icanhazip.com",
)
_HTTP_TIMEOUT = 8.0
_UA = "curl/8.4.0"


def _do_fetch() -> None:
    global _PUBLIC_IP, _fetched
    for url in _ENDPOINTS:
        try:
            resp = httpx.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": _UA})
            if resp.status_code != 200:
                continue
            ip = resp.text.strip()
            # 简单校验：IPv4 格式（4 段 0-255）
            parts = ip.split(".")
            if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                with _lock:
                    _PUBLIC_IP = ip
                    _fetched = True
                print(f"[public_ip] resolved {ip} (via {url})")
                return
        except Exception as exc:
            print(f"[public_ip] {url} failed: {exc}")
    with _lock:
        _fetched = True
    print("[public_ip] all endpoints failed; public address will be hidden in menus")


def fetch_async() -> None:
    """启动后台线程获取一次。已获取过则不重复。"""
    with _lock:
        if _fetched:
            return
    threading.Thread(target=_do_fetch, daemon=True, name="public-ip-fetch").start()


def get() -> Optional[str]:
    """返回缓存的公网 IPv4，未获取或失败时为 None。"""
    with _lock:
        return _PUBLIC_IP
