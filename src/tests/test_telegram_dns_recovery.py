from __future__ import annotations

import errno
import os as _ap_os
import sys as _ap_sys

_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(
    _ap_os.path.dirname(_ap_os.path.abspath(__file__))
)))
from src.tests import _isolation

_isolation.isolate()


def _import_modules():
    from src import network
    from src.telegram import ui
    return {"network": network, "ui": ui}


class _FailingSession:
    def get(self, _url):
        try:
            raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
        except OSError as inner:
            try:
                raise RuntimeError("transport connect failed") from inner
            except RuntimeError as middle:
                raise RuntimeError("request failed") from middle


def test_nested_eaddrnotavail_invalidates_telegram_and_rebuilds_session_without_token_log(
    m, monkeypatch, capsys,
):
    ui = m["ui"]
    network = m["network"]
    secret_token = "123456:SUPER-SECRET-TOKEN"
    ui.configure(secret_token, [])

    invalidated: list[str] = []
    rebuilt: list[bool] = []
    monkeypatch.setattr(ui, "_get_session", lambda: _FailingSession())
    monkeypatch.setattr(network, "invalidate_dns_cache", lambda host: invalidated.append(host) or 1)
    monkeypatch.setattr(ui, "rebuild_session", lambda: rebuilt.append(True))

    assert ui.api("getUpdates") is None
    assert invalidated == ["api.telegram.org"]
    assert rebuilt == [True]
    output = capsys.readouterr().out
    assert "DNS cache invalidated and session rebuilt" in output
    assert secret_token not in output
    assert f"bot{secret_token}" not in output
    assert "https://api.telegram.org/" not in output
