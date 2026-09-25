"""Direct tests for the import-before-src isolation and network fail-closed gate."""

from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path
import socket

import pytest

from src.tests import isolated_pytest as runner


def test_bootstrap_paths_are_absolute_and_below_fresh_root():
    assert os.environ["PARROT_TEST_CONFTEST_PROBE"] == "absolute-paths-ok-before-collection"
    assert os.environ["PARROT_TEST_NETWORK_GUARD"] == "loopback-only"
    root = Path(os.environ["PARROT_TEST_ROOT"]).resolve()
    assert root.is_absolute()
    for name in (
        "ANTHROPIC_PROXY_DATA_DIR",
        "ANTHROPIC_PROXY_CONFIG",
        "PARROT_TEST_STATE_PATH",
        "PARROT_TEST_LOG_DIR",
        "PARROT_TEST_IMAGE_PATH",
    ):
        path = Path(os.environ[name]).resolve()
        assert path.is_absolute()
        path.relative_to(root)


def test_non_loopback_socket_and_dns_are_blocked_before_syscall():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="blocked non-loopback"):
            sock.connect(("198.51.100.7", 443))
    finally:
        sock.close()

    with pytest.raises(RuntimeError, match="DNS blocked non-loopback"):
        socket.getaddrinfo("example.invalid", 443)


def test_local_socketpair_remains_available():
    left, right = socket.socketpair()
    try:
        left.sendall(b"ok")
        assert right.recv(2) == b"ok"
    finally:
        left.close()
        right.close()


def test_runner_handoff_preserves_argv_and_environment(monkeypatch, tmp_path):
    candidate = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(runner, "_controlled_python", lambda _root: candidate)
    monkeypatch.setattr(runner, "_running_in", lambda _candidate: False)
    monkeypatch.delenv(runner._HANDOFF_MARKER, raising=False)
    monkeypatch.setenv("PARROT_HANDOFF_TEST_VALUE", "preserved")
    observed = {}

    def fake_execve(executable, command, env):
        observed.update(executable=executable, command=command, env=env)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(runner.os, "execve", fake_execve)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        runner._ensure_controlled_interpreter(["test_one.py", "-q"])

    assert observed["executable"] == str(candidate)
    assert observed["command"] == [
        str(candidate),
        str(Path(runner.__file__).resolve()),
        "test_one.py",
        "-q",
    ]
    assert observed["env"][runner._HANDOFF_MARKER] == str(candidate)
    assert observed["env"]["PARROT_HANDOFF_TEST_VALUE"] == "preserved"


def test_runner_handoff_marker_fails_closed_in_wrong_interpreter(monkeypatch, tmp_path):
    candidate = tmp_path / "venv" / "bin" / "python"
    monkeypatch.setattr(runner, "_controlled_python", lambda _root: candidate)
    monkeypatch.setattr(runner, "_running_in", lambda _candidate: False)
    monkeypatch.setenv(runner._HANDOFF_MARKER, str(candidate))

    with pytest.raises(SystemExit, match="did not select that venv; refusing to loop"):
        runner._ensure_controlled_interpreter([])


def test_runner_missing_websockets_fails_fast():
    def missing(_distribution):
        raise metadata.PackageNotFoundError("websockets")

    with pytest.raises(SystemExit, match=r"requires websockets>=15.*not installed"):
        runner._require_websockets_contract(missing)


def test_runner_old_websockets_fails_fast():
    with pytest.raises(SystemExit, match=r"requires websockets>=15.*13\.1"):
        runner._require_websockets_contract(lambda _distribution: "13.1")


def test_runner_compliant_websockets_continues():
    runner._require_websockets_contract(lambda _distribution: "16.0")


def test_runner_without_venv_checks_current_interpreter(monkeypatch):
    checked = []
    monkeypatch.setattr(runner, "_controlled_python", lambda _root: None)
    monkeypatch.setattr(runner, "_require_websockets_contract", lambda: checked.append(True))
    monkeypatch.delenv(runner._HANDOFF_MARKER, raising=False)

    runner._ensure_controlled_interpreter([])

    assert checked == [True]


@pytest.mark.parametrize("cpus,expected", [(1, 1), (2, 2), (8, 4)])
def test_default_workers_respect_cpu_affinity(monkeypatch, cpus, expected):
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: set(range(cpus)), raising=False)
    assert runner._default_workers() == expected


def test_default_workers_fall_back_when_affinity_unavailable(monkeypatch):
    monkeypatch.delattr(runner.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: None)
    assert runner._default_workers() == 1


@pytest.mark.parametrize("argv,extra,expected", [
    (["src/tests", "-q"], "", ["-n", "4", "--dist=worksteal", "--durations=10"]),
    (["-n", "0"], "", ["--dist=worksteal", "--durations=10"]),
    (["-n0"], "", ["--dist=worksteal", "--durations=10"]),
    (["--numprocesses=2"], "", ["--dist=worksteal", "--durations=10"]),
    (["--numprocesses", "2"], "", ["--dist=worksteal", "--durations=10"]),
    (["--dist", "loadfile"], "", ["-n", "4", "--durations=10"]),
    (["--dist=loadfile"], "", ["-n", "4", "--durations=10"]),
    (["--durations=0"], "", ["-n", "4", "--dist=worksteal"]),
    (["--durations", "20"], "", ["-n", "4", "--dist=worksteal"]),
    (["src/tests"], "-n 0 --dist=no --durations=0", []),
    (["--collect-only"], "", ["--durations=10"]),
    (["--help"], "", ["--durations=10"]),
])
def test_runner_fast_defaults_preserve_explicit_options(monkeypatch, argv, extra, expected):
    monkeypatch.setenv("PYTEST_ADDOPTS", extra)
    monkeypatch.setattr(runner, "_default_workers", lambda: 4)
    assert runner._pytest_defaults(argv, xdist_available=True) == expected


def test_runner_without_xdist_does_not_add_unknown_options(monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    assert runner._pytest_defaults(["src/tests"], xdist_available=False) == ["--durations=10"]
