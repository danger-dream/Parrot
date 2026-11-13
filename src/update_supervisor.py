"""Private, one-shot source-update monitor; executable with Python -S.

Only stdlib imports: a broken application import/dependency cannot disable rollback.
The updater copies this file and a 0600 plan outside the source checkout before
stopping it. No application configuration or StateStore is imported here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request

CODE_ROOTS = frozenset({"server.py", "src", "scripts", "requirements.txt", "requirements-dev.txt",
                        "pyproject.toml", "Dockerfile", "docker-entrypoint.sh", "deploy.sh",
                        "docker-compose.yml"})


def write_json(path: str | Path, value: dict) -> None:
    path = Path(path)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with open(temp, "x", encoding="utf-8") as out:
            os.chmod(temp, 0o600)
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def code_member(name: str, excludes=()) -> bool:
    path = Path(name)
    parts = path.parts
    return bool(parts and not path.is_absolute() and ".." not in parts
                and parts[0] in CODE_ROOTS and "__pycache__" not in parts
                and not name.endswith((".pyc", ".db", ".db-wal", ".db-shm", ".lock"))
                and not any(name == p or name.startswith(p.rstrip('/') + '/') for p in excludes))


def code_digest(app: str, excludes=()) -> str:
    entries = []
    root = Path(app)
    for name in sorted(CODE_ROOTS):
        path = root / name
        files = _files(path) if path.is_dir() else [path]
        for item in files:
            relative = str(item.relative_to(root))
            if code_member(relative, excludes) and item.exists():
                entries.append((relative, _signature(item)))
    return hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest()


def backup_code(app: str, destination: str, excludes=()) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        def select(member):
            return member if code_member(member.name, excludes) and (member.isfile() or member.isdir()) else None
        for name in sorted(CODE_ROOTS):
            path = Path(app) / name
            if path.exists() and not path.is_symlink():
                archive.add(path, arcname=name, filter=select)
    os.chmod(destination, 0o600)


def restore_code(app: str, archive_path: str, excludes=()) -> None:
    root = Path(app).resolve()
    with tarfile.open(archive_path) as archive:
        members = [m for m in archive.getmembers()
                   if code_member(str(Path(m.name)), excludes) and (m.isfile() or m.isdir())]
        # Refuse links already on disk too; a code restore must not follow them
        # into operator data. Older tar backups are filtered by the same allowlist.
        for member in members:
            dest = root / member.name
            if not dest.resolve().is_relative_to(root) or any(p.is_symlink() for p in (dest, *dest.parents) if p != root.parent):
                raise ValueError("unsafe source restore path")
        for member in members:
            dest = root / member.name
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                assert stream is not None
                with stream, open(dest, "wb") as out:
                    shutil.copyfileobj(stream, out)
                os.chmod(dest, member.mode & 0o777)


def _signature(path: Path):
    if path.is_symlink(): return ["link", os.readlink(path)]
    if not path.is_file(): return None
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while block := stream.read(1024 * 1024): digest.update(block)
    return ["file", digest.hexdigest(), path.stat().st_mode & 0o777]


def _files(root: Path):
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            if (Path(parent) / name).is_symlink():
                dirs.remove(name)
                files.append(name)
        for name in files:
            yield Path(parent) / name


def snapshot_dependencies(destination: str, roots: list[str]) -> dict:
    """Local rollback material, never a pip download or a new deployment env."""
    target = Path(destination)
    target.mkdir(mode=0o700)
    snapshot = {"root": str(target), "roots": [], "before": {}}
    # Keep only distinct/non-nested installation directories (purelib/platlib/bin).
    for raw in roots:
        root = Path(raw).absolute()
        if root.is_dir() and not any(root.is_relative_to(p) for p in snapshot["roots"]):
            snapshot["roots"].append(str(root))
    for index, raw in enumerate(snapshot["roots"]):
        for path in _files(Path(raw)):
            key = f"{index}/{path.relative_to(raw)}"
            snapshot["before"][key] = _signature(path)
            copy = target / key
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy, follow_symlinks=False)
    write_json(target / "snapshot.json", snapshot)
    return snapshot


def seal_dependencies(snapshot: dict) -> dict:
    after = {}
    for index, raw in enumerate(snapshot["roots"]):
        for path in _files(Path(raw)):
            after[f"{index}/{path.relative_to(raw)}"] = _signature(path)
    before = snapshot["before"]
    snapshot["changes"] = {key: after.get(key) for key in before.keys() | after.keys()
                           if before.get(key) != after.get(key)}
    write_json(Path(snapshot["root"]) / "snapshot.json", snapshot)
    return snapshot


def restore_dependencies(snapshot_path: str) -> None:
    snapshot = json.loads(Path(snapshot_path).read_text())
    changes = snapshot.get("changes")
    if changes is None:
        raise RuntimeError("dependency snapshot was not sealed after install")
    targets = []
    for key, after in changes.items():
        index, relative = key.split("/", 1)
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("unsafe dependency snapshot path")
        dest = Path(snapshot["roots"][int(index)]) / relative
        before = snapshot["before"].get(key)
        observed = _signature(dest)
        if observed not in (after, before):
            raise RuntimeError(f"dependency changed after staging: {relative}")
        targets.append((key, dest, before))
    for key, dest, before in targets:
        if dest.is_symlink() or dest.is_file(): dest.unlink()
        if before is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(snapshot["root"]) / key, dest, follow_symlinks=False)


def process_identity(pid: int) -> str | None:
    try:
        # stat's comm may contain spaces or parentheses.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def stop_process(pid: int, identity: str | None, timeout: float) -> bool:
    if identity is None:
        try: os.kill(pid, 0)
        except ProcessLookupError: return True
        # No identity evidence: never stop/rewrite underneath an unknown process.
        return False
    if process_identity(pid) != identity: return True
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while process_identity(pid) == identity:
        if time.monotonic() >= deadline: return False
        time.sleep(0.1)
    return True


def wait_health(plan: dict, version: str) -> bool:
    deadline = time.monotonic() + float(plan["health_timeout"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with opener.open(plan["health_url"], timeout=max(0.05, min(3, deadline-time.monotonic()))) as response:
                body = json.load(response)
            if (body.get("status") not in {"draining", "error"}
                    and str(body.get("version", "")).lstrip("vV") == version.lstrip("vV")):
                return True
        except Exception:
            pass
        time.sleep(min(0.2, max(0, deadline-time.monotonic())))
    return False


def apply_source_update(plan: dict) -> None:
    update = plan.get("source_update")
    if not update:
        return
    for args in update["commands"]:
        subprocess.run(args, timeout=120, check=True)
    if not update.get("install"):
        return
    snapshot = snapshot_dependencies(update["dependency_root"], update["dependency_paths"])
    plan["dependencies"] = str(Path(snapshot["root"]) / "snapshot.json")
    meta_path = Path(update["meta_path"])
    meta = json.loads(meta_path.read_text())
    meta["dependencies"] = plan["dependencies"]
    write_json(meta_path, meta)
    try:
        subprocess.run([update["python"], "-m", "pip", "install", "-r",
                        str(Path(plan["app"]) / "requirements.txt")], timeout=600, check=True)
        subprocess.run([update["python"], "-m", "pip", "check"], timeout=60, check=True)
    finally:
        seal_dependencies(snapshot)


def run(plan: dict) -> str:
    """Stop/start, check exact version, restore code+changed dependencies on failure."""
    child = None
    def command(args):
        return subprocess.run(args, timeout=plan["stop_timeout"], check=False,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    def start():
        nonlocal child
        if plan["mode"] == "systemd":
            return command(["systemctl", "start", plan["service"]])
        child = subprocess.Popen(plan["command"], cwd=plan["app"], env=plan.get("environment"),
                                 start_new_session=True)
        return True
    def stop():
        if plan["mode"] == "systemd":
            return command(["systemctl", "stop", plan["service"]])
        if child is not None and child.poll() is not None:
            child.wait()
            return True
        pid = child.pid if child is not None else plan["old_pid"]
        identity = process_identity(pid) if child is not None else plan["old_identity"]
        if child is not None and identity is None and child.poll() is not None:
            child.wait()
            return True
        stopped = stop_process(pid, identity, plan["stop_timeout"])
        if stopped and child is not None: child.wait(timeout=1)
        return stopped
    result = "ROLLBACK_FAILED"
    try:
        if plan.get("target_code_digest") and code_digest(plan["app"], plan.get("excludes", ())) != plan["target_code_digest"]:
            raise RuntimeError("staged source identity changed")
        if not stop():
            # Never restore files underneath a live old/new interpreter.
            raise RuntimeError("process did not stop safely")
        applied = False
        try:
            apply_source_update(plan)
            applied = True
        except Exception as exc:
            print(f"[source-updater] source apply failed: {type(exc).__name__}", flush=True)
        if applied and start() and wait_health(plan, plan["target_version"]) and (child is None or child.poll() is None):
            result = "OK"
        else:
            if not stop(): raise RuntimeError("failed version did not stop safely")
            for rollback in plan.get("rollback_commands", []):
                if not command(rollback): break  # tar remains an independent fallback.
            restore_code(plan["app"], plan["archive"], plan.get("excludes", ()))
            if plan.get("dependencies"):
                restore_dependencies(plan["dependencies"])
            if start() and wait_health(plan, plan["from_version"]) and (child is None or child.poll() is None):
                result = "ROLLBACK"
    except Exception as exc:
        # Do not include plan/environment (which can contain runtime credentials).
        print(f"[source-updater] monitor failed: {type(exc).__name__}", flush=True)
    finally:
        write_json(plan["result_path"], {"update_id": plan["update_id"], "result": result})
    return result


def main() -> None:
    plan = json.loads(Path(sys.argv[1]).read_text())
    # Let the confirmation response leave before initiating process stop.
    time.sleep(float(plan.get("dispatch_delay", 1.5)))
    try:
        run(plan)
    finally:
        # The launch plan contains the inherited environment; do not retain it.
        Path(sys.argv[1]).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
