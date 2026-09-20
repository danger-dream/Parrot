"""Deployment/update compatibility guards; never contact a real service or Docker engine."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.tests import _isolation
_isolation.isolate()
from src import updater

ROOT = Path(__file__).resolve().parents[2]


def test_source_dependency_install_uses_running_interpreter(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(updater, "_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "_git", lambda args, **kw: (0, "requirements.txt" if args[0] == "diff" else ""))
    monkeypatch.setattr(updater, "_run", lambda cmd, **kw: calls.append(cmd) or (0, ""))
    ok, detail = updater._src_pull("v-next", prev_commit="old")
    assert ok, detail
    assert calls[0] == [sys.executable, "-m", "pip", "install", "-r", str(tmp_path / "requirements.txt")]
    assert calls[1] == [sys.executable, "-m", "pip", "check"]


def test_source_failed_dependency_install_restores_checkout(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(updater, "_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "_git", lambda args, **kw: calls.append(args) or (0, "requirements.txt" if args[0] == "diff" else "old"))
    monkeypatch.setattr(updater, "_run", lambda *a, **kw: (1, "permission denied"))
    ok, detail = updater._src_pull("v-next", prev_commit="old")
    assert not ok
    assert "permission denied" in detail
    assert ["reset", "--hard", "old"] in calls


@pytest.mark.parametrize("cli", [True, False])
def test_docker_backup_requires_successful_tag(monkeypatch, tmp_path, cli):
    monkeypatch.setattr(updater, "_cfg", lambda: {"image": "test:latest"})
    monkeypatch.setattr(updater, "_backup_root", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "_docker_current_image_digest", lambda: "sha256:old")
    monkeypatch.setattr(updater, "_has_local_docker", lambda: cli)
    monkeypatch.setattr(updater, "_run", lambda *a, **kw: (1, "tag denied"))
    monkeypatch.setattr(updater, "_engine_tag_image", lambda *a: False)
    ok, ref, detail = updater._docker_backup("v-next")
    assert not ok
    assert not ref
    assert list(tmp_path.iterdir()) == []


def _deploy_functions():
    path = ROOT / "deploy.sh"
    if not path.exists():
        pytest.skip("deploy.sh is not included in the runtime image")
    return path.read_text().rsplit('main "$@"', 1)[0]


def test_deploy_upgrade_preserves_existing_compose(tmp_path):
    compose = 'services:\n  parrot:\n    image: custom:latest\n    ports:\n      - "127.0.0.1:23456:22122"\n    volumes:\n      - ./data:/app/data\n'
    (tmp_path / "docker-compose.yml").write_text(compose)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/config.json").write_text('{"keep":true}')
    result = subprocess.run(["bash", "-c", _deploy_functions() + '\nINSTALL_DIR="$1"; MODE=upgrade; PORT=""; write_files', "test", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "docker-compose.yml").read_text() == compose
    assert (tmp_path / "data/config.json").read_text() == '{"keep":true}'


def test_deploy_health_cannot_be_satisfied_by_unrelated_host_service():
    result = subprocess.run(["bash", "-c", _deploy_functions() + '''
# No target container; a host listener would return HTTP 200.
docker() { return 1; }
curl() { return 0; }
container_healthy parrot
'''], capture_output=True, text=True)
    assert result.returncode == 1, result.stderr


def test_sidecar_forbids_pull_during_recreate_and_rollback(monkeypatch):
    monkeypatch.setattr(updater, "_cfg", lambda: {"composeService": "parrot", "containerName": "parrot", "image": "test:latest", "composeDir": "/tmp/test"})
    script = updater._compose_up_inner(backup_digest="sha256:old")
    assert script.count('docker compose up -d --force-recreate --pull never "$SVC"') == 2
    assert script.index('echo "ROLLBACK"', script.index("fail_rollback()")) > script.index("&& wait_health", script.index("fail_rollback()"))


@pytest.mark.parametrize("restored", [True, False])
def test_cancel_docker_restores_tag_without_restart(monkeypatch, tmp_path, restored):
    (tmp_path / "backup.json").write_text(json.dumps({"image": "test:latest", "digest": "sha256:old"}))
    monkeypatch.setattr(updater, "_backup_root", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "load_state", lambda: {"stage": "staged", "mode": "docker", "backup_ref": "backup"})
    calls = []
    states = []
    monkeypatch.setattr(updater, "_tag_image_for_compose", lambda *a: calls.append(a) or (restored, "retag"))
    monkeypatch.setattr(updater, "reset_state", lambda: states.append("idle"))
    monkeypatch.setattr(updater, "save_state", lambda **kw: states.append(kw["stage"]))
    monkeypatch.setattr(updater, "_docker_sidecar_recreate", lambda **kw: pytest.fail("cancel must not restart"))
    assert updater.cancel_staged()[0] is restored
    assert calls == [("sha256:old", "test:latest")]
    assert states == ["idle" if restored else "staged"]


def test_resume_does_not_claim_failed_rollback_succeeded(monkeypatch):
    states = []
    monkeypatch.setattr(updater, "load_state", lambda: {"stage": "restarting", "mode": "bare", "backup_ref": "missing"})
    monkeypatch.setattr(updater, "save_state", lambda **kw: states.append(kw["stage"]))
    monkeypatch.setattr(updater, "wait_healthy", lambda timeout: (False, "unhealthy"))
    monkeypatch.setattr(updater, "_src_rollback", lambda ref: (False, "missing backup"))
    monkeypatch.setattr(updater, "_notify_cross_process", lambda *a, **kw: None)
    class InlineThread:
        def __init__(self, target, **kw): self.target = target
        def start(self): self.target()
    monkeypatch.setattr(updater.threading, "Thread", InlineThread)
    updater.resume_after_restart()
    assert states == ["verifying", "failed"]


@pytest.mark.asyncio
async def test_real_websocket_through_local_socks_loads_hidden_dependency():
    """Exercise python-socks via websockets, not a patched connect function."""
    import websockets
    from src.proxy.connector import SOCKS5Connector
    from src.transports.ws_runtime import connect_upstream_ws

    async def echo(ws):
        async for message in ws:
            await ws.send(message)

    done = asyncio.Event()
    errors = []
    async def socks(reader, writer):
        upstream = None
        try:
            version, count = await reader.readexactly(2)
            assert version == 5
            await reader.readexactly(count)
            writer.write(b"\x05\x00")
            await writer.drain()
            version, cmd, reserved, atyp = await reader.readexactly(4)
            assert (version, cmd, reserved) == (5, 1, 0)
            if atyp == 1:
                host = ".".join(str(b) for b in await reader.readexactly(4))
            else:
                assert atyp == 3
                length = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(length)).decode()
            port = int.from_bytes(await reader.readexactly(2), "big")
            assert host == "127.0.0.1"
            upreader, upstream = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            async def relay(src, dst):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
                dst.close()
            await asyncio.gather(relay(reader, upstream), relay(upreader, writer))
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()
            if upstream is not None: upstream.close()
            done.set()

    async with websockets.serve(echo, "127.0.0.1", 0) as ws_server:
        ws_port = ws_server.sockets[0].getsockname()[1]
        async with await asyncio.start_server(socks, "127.0.0.1", 0) as proxy:
            port = proxy.sockets[0].getsockname()[1]
            conn = await connect_upstream_ws(f"ws://127.0.0.1:{ws_port}", headers={},
                connector=SOCKS5Connector("test", f"socks5://127.0.0.1:{port}"),
                proxy_bytes=None, open_timeout=5)
            try:
                await conn.send("isolated-dependency-probe")
                assert await conn.recv() == "isolated-dependency-probe"
            finally:
                await conn.close()
            await asyncio.wait_for(done.wait(), 5)
    assert not errors


@pytest.mark.parametrize("binding", ["23456:9999", "127.0.0.1:23456:9999", "[::1]:23456:9999/tcp"])
def test_migrate_preserves_host_port_and_bind_address(tmp_path, binding):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(f'services:\n  parrot:\n    image: old:tag\n    container_name: parrot\n    ports:\n      - "{binding}"\n    volumes:\n      - ./data:/app/data\n')
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text('{"listen":{"port":9999},"channels":[]}')
    result = subprocess.run(["bash", "-c", _deploy_functions() + '''
docker() { return 0; }
graceful_remove_container() { return 0; }
container_healthy() { return 0; }
cmd_update "$1"
''', "test", str(tmp_path)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert binding.replace(":9999", ":22122") in compose.read_text()
    assert json.loads((data / "config.json").read_text())["listen"]["port"] == 22122
    assert list(tmp_path.glob("docker-compose.yml.bak.*"))
