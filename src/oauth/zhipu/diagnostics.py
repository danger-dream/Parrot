"""Credential-free facts from the actual management request, not guessed routes."""
from __future__ import annotations

from urllib.parse import urlsplit
import re

import httpx

FIELDS = ("request_not_sent", "network_phase", "target_host", "proxy_route", "fallback_used")


class RequestTrace:
    def __init__(self, url):
        self.host = urlsplit(url).hostname or ""
        self.routes = []
        self.phase = ""
        self.send_started = False

    def route(self, name):
        # Route names, never proxy URLs/passwords or full target URLs.
        self.routes.append(str(name)[:80])
        self.phase = "connect"

    def trace(self, event, info):
        if event.endswith("connect_tcp.started"):
            self.phase = "connect"
        elif event.endswith("start_tls.started"):
            self.phase = "tls"
        elif "send_request_headers.started" in event or "send_request_body.started" in event:
            self.phase = "write"
            self.send_started = True
        elif "receive_response" in event and event.endswith(".started"):
            self.phase = "read"

    async def trace_async(self, event, info):
        self.trace(event, info)

    def extensions(self, *, asynchronous=False):
        return {"trace": self.trace_async if asynchronous else self.trace,
                "parrot_route_observer": self.route}

    def facts(self, exc=None):
        # httpx connect/pool errors are raised before application data is sent.
        # Never infer this from a generic timeout or the absence of trace events.
        not_sent = isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)) and not self.send_started
        return {"request_not_sent": not_sent, "network_phase": self.phase,
                "target_host": self.host, "proxy_route": self.routes[-1] if self.routes else "unobserved",
                "fallback_used": len(self.routes) > 1}


def error_facts(exc):
    host = getattr(exc, "target_host", "")
    route = getattr(exc, "proxy_route", "")
    phase = getattr(exc, "network_phase", "")
    return {"request_not_sent": getattr(exc, "request_not_sent", False) is True,
            "fallback_used": getattr(exc, "fallback_used", False) is True,
            "network_phase": phase if isinstance(phase, str) and phase in {"connect", "tls", "write", "read"} else "",
            "target_host": host if isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) else "",
            "proxy_route": route if isinstance(route, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", route) else "unobserved"}
