from __future__ import annotations

import asyncio
import ipaddress
import os
from pathlib import Path
import socket


def _worker_data_root() -> None:
    """给每个 xdist worker 分配独立的数据目录。

    并行时所有 worker 由同一份 bootstrap 环境派生，最初的
    ``ANTHROPIC_PROXY_DATA_DIR`` / ``ANTHROPIC_PROXY_CONFIG`` 等是**同一条路径**。
    而 StateStore 是对目标文件加进程独占锁的，config.json 也会被各 worker 覆写，
    于是并行会互相抢锁、互相改配置。这里按 worker 名派生独立子目录后再交给
    conftest 的既有校验，使并行与串行都满足"每个测试进程独占自己的数据根"。

    必须在任何 ``src`` 导入之前执行：``src/config.py`` 在导入时就读这些环境变量
    决定 DATA_DIR / CONFIG_PATH。conftest 的导入早于测试模块与 src，因此这里安全。
    """
    name = os.environ.get("PYTEST_XDIST_WORKER") or ""
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
    if not safe:
        # 串行（或 xdist 控制器）：沿用 bootstrap 已经建好的单一数据根。
        return
    root = Path(os.environ["PARROT_TEST_ROOT"]).resolve()
    data_dir = (root / f"data-{safe}").resolve()
    log_dir = (data_dir / "logs").resolve()
    for path in (data_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    os.environ["ANTHROPIC_PROXY_DATA_DIR"] = str(data_dir)
    os.environ["ANTHROPIC_PROXY_CONFIG"] = str((data_dir / "config.json").resolve())
    os.environ["PARROT_TEST_STATE_PATH"] = str((data_dir / "state.db").resolve())
    os.environ["PARROT_TEST_LOG_DIR"] = str(log_dir)
    os.environ["PARROT_TEST_IMAGE_PATH"] = str((data_dir / "image_logs.db").resolve())

    # 这份 config 必须**先于**任何 config.get() 存在：否则 _load_from_disk()
    # 会退回 DEFAULT_CONFIG（oauth.mockMode=False）并把默认值写回磁盘，
    # 使本 worker 的 OAuth 测试真的发起网络请求。内容与 bootstrap 同源。
    from . import _isolation

    _isolation.write_minimal_config(os.environ["ANTHROPIC_PROXY_CONFIG"])


def _validate_import_time_isolation() -> None:
    if os.environ.get("PARROT_TEST_ISOLATED") != "1":
        raise RuntimeError(
            "pytest must be launched via: "
            "./venv/bin/python src/tests/isolated_pytest.py <test args>"
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


_worker_data_root()
_validate_import_time_isolation()

import pytest


def _normalize_loaded_auto_codex_profile() -> None:
    """Keep synthetic loaded configs on the production current-profile path.

    Some domain fixtures replace ``config._cache`` with a deepcopy of
    ``DEFAULT_CONFIG`` after pytest fixture setup.  The Python defaults
    intentionally omit mutable Codex version/profile values, because production
    fills them via ``_normalize_openai_oauth_config`` while loading config.json.
    Re-run that same normalizer before the test call when such a synthetic cache
    still has auto-update enabled; pinned/fail-closed fixtures remain untouched.
    """
    import sys

    config_module = sys.modules.get("src.config")
    if config_module is None:
        return
    current = getattr(config_module, "_cache", None)
    if not isinstance(current, dict):
        return
    provider = current.get("openaiOAuth")
    if not isinstance(provider, dict):
        return
    if provider.get("codexProfileAutoUpdate", True) is not True:
        return
    if provider.get("codexCliVersion") and provider.get("codexProtocolProfile"):
        return
    config_module._normalize_openai_oauth_config(current, current)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    # This hook runs after fixture setup, including fixtures that install a
    # synthetic config cache, but before production code under test is called.
    _normalize_loaded_auto_codex_profile()


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
        from src.telegram import menu_cache, ui
    except Exception:
        menu_cache = None
        ui = None

    if ui is None:
        yield
        return

    if menu_cache is not None:
        menu_cache.reset_for_tests()
    orig_api = ui.api
    orig_session = getattr(ui, "_session", None)
    orig_session_enabled = getattr(ui, "_session_enabled", False)
    orig_bot_token = getattr(ui, "_bot_token", "")
    orig_admin_ids = set(getattr(ui, "_admin_ids", set()))
    try:
        yield
    finally:
        try:
            ui.close_session()
            ui.wait_session_idle(2.0)
        except Exception:
            pass
        ui.api = orig_api
        with ui._session_condition:
            ui._session = orig_session
            ui._session_holder = None
            ui._session_enabled = orig_session_enabled
        ui._bot_token = orig_bot_token
        ui._admin_ids = set(orig_admin_ids)
        if menu_cache is not None:
            menu_cache.reset_for_tests()


@pytest.fixture(autouse=True, scope="module")
def _restore_isolated_config_baseline():
    """每个测试模块开跑前把 config 恢复到隔离基线。

    多个测试文件会用 ``copy.deepcopy(DEFAULT_CONFIG)`` 整体替换配置，而
    ``config.update()`` 会同时改写内存缓存与磁盘上的 config.json 且不做还原。
    后跑的模块因此会读到前一个模块留下的配置（例如 DEFAULT_CONFIG 里
    ``oauth.mockMode`` 为 False），导致本应走 mock 的用例真的去连上游。
    串行时收集顺序固定尚且侥幸，模块顺序一旦变化（并行、或新增文件）就会
    随机失败。

    在每个模块开始时统一恢复基线，使"模块之间互不影响"；模块内部的测试
    顺序依赖不受影响。
    """
    from src import config as config_module

    from . import _isolation

    try:
        _isolation.write_minimal_config(os.environ["ANTHROPIC_PROXY_CONFIG"])
        config_module.reload()
    except Exception:
        # 恢复失败不应遮蔽真正的测试失败；模块自身的 fixture 仍会建立所需状态。
        pass
    yield


@pytest.fixture(autouse=True)
def _isolate_channel_generation_globals():
    """Process-lifetime tombstones must not leak between independent tests."""
    from src import channel_state, concurrency

    def reset() -> None:
        with channel_state.mutation_lock, concurrency._slots_guard:
            channel_state._transition_keys.clear()
            channel_state._aliases.clear()
            channel_state._deleted_keys.clear()
            channel_state._generation_targets.clear()
            channel_state._legacy_api_generations.clear()
            channel_state._legacy_oauth_generations.clear()
            concurrency._retired_keys.clear()
            concurrency._retired_limits.clear()
            concurrency._deleted_retire_targets.clear()

    reset()
    try:
        yield
    finally:
        reset()


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
