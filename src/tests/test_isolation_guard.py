"""Direct tests for the import-before-src isolation and network fail-closed gate."""

from __future__ import annotations

import os
from pathlib import Path
import socket

import pytest


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
