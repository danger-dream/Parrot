from __future__ import annotations

import asyncio
import ipaddress
import os
from pathlib import Path
import socket


def _validate_import_time_isolation() -> None:
    if os.environ.get("PARROT_TEST_ISOLATED") != "1":
        raise RuntimeError(
            "pytest must be launched via: python3 src/tests/isolated_pytest.py <test args>"
        )
    root_raw = os.environ.get("PARROT_TEST_ROOT") or ""
    root = Path(root_raw)
    if not root.is_absolute():
        raise RuntimeError(f"test isolation root is not absolute: {root_raw!r}")
    names = (
        "ANTHROPIC_PROXY_DATA_DIR",
        "ANTHROPIC_PROXY_CONFIG",
        "PARROT_TEST_STATE_PATH",
        "PARROT_TEST_LOG_DIR",
        "PARROT_TEST_IMAGE_PATH",
    )
    for name in names:
        raw = os.environ.get(name) or ""
        path = Path(raw)
        if not path.is_absolute():
            raise RuntimeError(f"test isolation path {name} is not absolute: {raw!r}")
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise RuntimeError(f"test isolation path escaped root: {name}={path}") from exc
    os.environ["PARROT_TEST_CONFTEST_PROBE"] = "absolute-paths-ok-before-collection"


_validate_import_time_isolation()

import pytest

_ORIG_TO_THREAD = asyncio.to_thread
_ORIG_SOCKET = socket.socket
_ORIG_GETADDRINFO = socket.getaddrinfo
_ORIG_CREATE_CONNECTION = socket.create_connection
_ORIG_HTTPX_MOCK_HANDLE = None


async def _test_inline_to_thread(func, /, *args, **kwargs):
    """测试环境里同步执行 to_thread 任务，避免解释器收尾卡在线程池关闭。"""
    return func(*args, **kwargs)


def _loopback_host(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="strict")
    value = str(host).strip().lower()
    if value == "localhost":
        return True
    if "%" in value:
        value = value.split("%", 1)[0]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _guard_address(address) -> None:
    # AF_UNIX paths and socketpair traffic are local by construction.
    if isinstance(address, (str, bytes)):
        return
    host = address[0] if isinstance(address, tuple) and address else None
    if not _loopback_host(host):
        raise RuntimeError(f"test network blocked non-loopback destination: {host!r}")


class _GuardedSocket(_ORIG_SOCKET):
    def connect(self, address):
        if self.family != socket.AF_UNIX:
            _guard_address(address)
        return super().connect(address)

    def connect_ex(self, address):
        if self.family != socket.AF_UNIX:
            _guard_address(address)
        return super().connect_ex(address)

    def sendto(self, data, *args):
        if self.family != socket.AF_UNIX and args:
            _guard_address(args[-1])
        return super().sendto(data, *args)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _loopback_host(host):
        raise RuntimeError(f"test DNS blocked non-loopback destination: {host!r}")
    return _ORIG_GETADDRINFO(host, *args, **kwargs)


def _guarded_create_connection(address, *args, **kwargs):
    _guard_address(address)
    return _ORIG_CREATE_CONNECTION(address, *args, **kwargs)


@pytest.fixture
def m(request):
    """返回测试模块的模块映射；具体状态初始化由测试显式调用。"""
    module = request.module
    importer = getattr(module, "_import_modules", None)
    if not callable(importer):
        raise RuntimeError(f"{module.__name__} is missing _import_modules()")
    return importer()


@pytest.fixture(autouse=True)
def _restore_telegram_ui_globals():
    """测试间恢复 telegram.ui 的猴补/全局状态，避免跨文件污染。"""
    try:
        from src.telegram import ui
    except Exception:
        ui = None

    if ui is None:
        yield
        return

    orig_api = ui.api
    orig_session = getattr(ui, "_session", None)
    orig_bot_token = getattr(ui, "_bot_token", "")
    orig_admin_ids = set(getattr(ui, "_admin_ids", set()))
    try:
        yield
    finally:
        try:
            ui.close_session()
        except Exception:
            pass
        ui.api = orig_api
        ui._session = orig_session
        ui._bot_token = orig_bot_token
        ui._admin_ids = set(orig_admin_ids)


async def _traced_httpx_mock_handle(self, request):
    """Make MockTransport emulate HTTPcore's authoritative upload trace.

    HTTPX's in-memory transport intentionally bypasses HTTPcore and otherwise
    emits no send-body milestone.  Test fakes own the upload, so they must emit
    that boundary rather than forcing production to invent elapsed timing.
    """
    import httpx

    await request.aread()
    trace = request.extensions.get("trace")
    if trace is not None:
        await trace("http11.send_request_body.started", {})
        await trace("http11.send_request_body.complete", {})
    response = self.handler(request)
    if not isinstance(response, httpx.Response):
        response = await response
    return response


def pytest_configure(config):
    """启用 async 模式、同步to_thread，并在收集前封锁非loopback网络。"""
    global _ORIG_HTTPX_MOCK_HANDLE
    config.option.asyncio_mode = "auto"
    asyncio.to_thread = _test_inline_to_thread
    if os.environ.get("PARROT_TEST_NO_NETWORK") != "1":
        raise RuntimeError("test network guard marker missing")
    socket.socket = _GuardedSocket
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.create_connection = _guarded_create_connection
    os.environ["PARROT_TEST_NETWORK_GUARD"] = "loopback-only"

    import httpx
    _ORIG_HTTPX_MOCK_HANDLE = httpx.MockTransport.handle_async_request
    httpx.MockTransport.handle_async_request = _traced_httpx_mock_handle


def pytest_unconfigure(config):
    asyncio.to_thread = _ORIG_TO_THREAD
    socket.socket = _ORIG_SOCKET
    socket.getaddrinfo = _ORIG_GETADDRINFO
    socket.create_connection = _ORIG_CREATE_CONNECTION
    if _ORIG_HTTPX_MOCK_HANDLE is not None:
        import httpx
        httpx.MockTransport.handle_async_request = _ORIG_HTTPX_MOCK_HANDLE
