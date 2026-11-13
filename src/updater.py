"""Parrot 自更新执行器（源码 systemd / Docker 双形态）。

与 update_checker 的分工：
- update_checker：只负责"发现新版"（拉 GitHub Release、比对版本、banner、推送通知）。
- updater（本模块）：负责"执行更新"——备份 → 拉取 → 等用户二次确认 → 重启生效 → 健康校验/回滚。

核心设计：持久化状态机（StateStore snapshots 单行 `app_self_update`），跨重启不丢。
交互流程（双重确认）：

    [检测到新版] → 通知带按钮 [🚀 更新] [🔕 忽略]
        │ 点更新
        ▼
    ① 备份(强制) → ② 拉取(git fetch / docker pull)
        │ 完成，停在 staged 态
        ▼
    通知带按钮 [✅ 确认重启生效] [↩️ 取消并回滚]   ← 第二道确认
        │ 点确认
        ▼
    ③ 重启 / recreate → ④ 健康检查 → ✅ vX.Y.Z / ❌ 自动回滚

形态检测：
- docker：容器内（/.dockerenv 存在，或 cgroup 含 docker/containerd）。
- systemd：宿主机源码部署且能 systemctl（INVOCATION_ID 环境变量或父进程 systemd）。
- bare：源码部署但非 systemd（用 exec 自重启兜底）。

安全红线：
- 仅 admin 能触发（路由层保证）。
- 所有外部命令参数固定，无字符串拼接注入面。
- 源码形态有未提交改动 → 拒绝更新（避免 reset 吃掉本地改动）。
- 全局更新锁，防并发重复触发。
- 重启前 StateStore 必须严格 flush 并验证 runtime/durable JSON snapshots；失败则拒绝重启。
- 重启后健康检查失败 → 自动回滚到备份。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sysconfig
import uuid
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from . import __version__, config, network, notifier, state_db
from . import update_supervisor


# ─── 形态检测 ────────────────────────────────────────────────────

MODE_DOCKER = "docker"
MODE_SYSTEMD = "systemd"
MODE_BARE = "bare"

_mode_cache: Optional[str] = None


def _detect_mode() -> str:
    global _mode_cache
    if _mode_cache:
        return _mode_cache
    # 1) 显式覆盖（测试/特殊部署用）
    override = (config.get().get("updateChecker") or {}).get("runtimeMode")
    if override in (MODE_DOCKER, MODE_SYSTEMD, MODE_BARE):
        _mode_cache = override
        return _mode_cache
    # 2) docker 检测
    if os.path.exists("/.dockerenv"):
        _mode_cache = MODE_DOCKER
        return _mode_cache
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as f:
            cg = f.read()
        if "docker" in cg or "containerd" in cg or "kubepods" in cg:
            _mode_cache = MODE_DOCKER
            return _mode_cache
    except Exception:
        pass
    # 3) systemd 检测：被 systemd 拉起的进程会有 INVOCATION_ID
    if os.environ.get("INVOCATION_ID"):
        _mode_cache = MODE_SYSTEMD
        return _mode_cache
    # 4) 兜底：源码裸进程
    _mode_cache = MODE_BARE
    return _mode_cache


def get_mode() -> str:
    return _detect_mode()


# ─── 路径常量 ────────────────────────────────────────────────────

def _app_dir() -> str:
    """源码根目录（server.py 所在目录）。"""
    return config.BASE_DIR


def _backup_root() -> str:
    """备份根目录：放 DATA_DIR/backups 下，确保 docker 形态落在挂载卷里。"""
    d = os.path.join(config.DATA_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    return d


# ─── 配置 ────────────────────────────────────────────────────────

def _cfg() -> dict:
    root = config.get()
    uc = root.get("updateChecker") or {}
    shutdown = root.get("shutdown") or {}
    raw_keep = uc.get("keepBackups", 1)
    try:
        keep_backups = 1 if raw_keep is None or isinstance(raw_keep, bool) else int(raw_keep)
    except (TypeError, ValueError):
        keep_backups = 1
    return {
        "enabled": bool(uc.get("enabled", True)),
        "autoUpdate": bool(uc.get("autoUpdate", False)),
        "ignoredVersions": list(uc.get("ignoredVersions") or []),
        "silentUpdate": bool(uc.get("silentUpdate", False)),
        "repo": str(uc.get("repo") or "danger-dream/Parrot").strip(),
        "serviceName": str(uc.get("serviceName") or "parrot.service").strip(),
        "composeDir": str(uc.get("composeDir") or "").strip(),
        "composeService": str(uc.get("composeService") or "parrot").strip(),
        "containerName": str(uc.get("containerName") or "parrot").strip(),
        "image": str(uc.get("image") or "ghcr.io/danger-dream/parrot:latest").strip(),
        "keepBackups": max(0, min(20, keep_backups)),
        "healthTimeoutSeconds": int(uc.get("healthTimeoutSeconds", 90) or 90),
        "gracefulStopSeconds": max(
            10, min(600, int(shutdown.get("drainTimeoutSeconds", 80) or 80) + 20)
        ),
        "updaterImage": str(uc.get("updaterImage") or "docker:cli").strip(),
    }


# ─── 状态机持久化（StateStore durable domain）────────────────────

# 状态机阶段：
#   idle          空闲
#   backing_up    备份中
#   pulling       拉取中
#   staged        已拉取完成，等待用户二次确认重启
#   restarting    重启生效中（重启前置位，重启后由新进程读取）
#   verifying     新进程已起，健康检查中
#   success       更新成功
#   failed        更新失败
#   rolled_back   已回滚
STAGE_IDLE = "idle"
STAGE_BACKING_UP = "backing_up"
STAGE_PULLING = "pulling"
STAGE_STAGED = "staged"
STAGE_RESTARTING = "restarting"
STAGE_VERIFYING = "verifying"
STAGE_SUCCESS = "success"
STAGE_FAILED = "failed"
STAGE_ROLLED_BACK = "rolled_back"


def _ensure_schema() -> None:
    """Compatibility no-op; StateStore owns its versioned schema."""


def load_state() -> dict:
    try:
        return state_db.updater_load()
    except Exception:
        return {"stage": STAGE_IDLE}


def save_state(**fields) -> None:
    state_db.updater_save(fields)


def reset_state() -> None:
    save_state(
        stage=STAGE_IDLE, mode=None, from_version=None, to_version=None,
        target_tag=None, backup_ref=None, message=None,
        chat_id=None, notify_msg_id=None, silent=False,
        source_target_commit=None, source_guard_digest=None,
    )


# ─── 更新锁（防并发）─────────────────────────────────────────────

_op_lock = threading.Lock()
_stopping = threading.Event()
_resume_thread: threading.Thread | None = None


def begin_shutdown() -> None:
    """Stop admitting updates and interrupt bounded command/poll waits."""
    _stopping.set()


async def stop() -> None:
    begin_shutdown()
    while _op_lock.locked() or (_resume_thread is not None and getattr(_resume_thread, "is_alive", lambda: False)()):
        await asyncio.sleep(0.02)


def start() -> None:
    _stopping.clear()


# 中间态卡死超时（秒）：备份/拉取/重启/健康检查若超过此时长没推进，判定为卡死。
# 取一个宽松值，覆盖大镜像拉取 + 重建 + 健康门控的最坏情况。
_STALE_INTERMEDIATE_SECONDS = 1800  # 30 分钟


_TRANSIENT_STAGES = (
    STAGE_BACKING_UP, STAGE_PULLING, STAGE_RESTARTING, STAGE_VERIFYING,
)


def _heal_stale_state(st: dict) -> dict:
    """中间态卡死自愈：若处于瞬态阶段且超过 _STALE_INTERMEDIATE_SECONDS 未更新，
    复位为 failed，避免状态机永久卡住挡死后续更新。staged 态不超时（等用户确认）。
    返回（可能已复位的）状态。"""
    stage = st.get("stage")
    if stage not in _TRANSIENT_STAGES:
        return st
    updated = st.get("updated_at") or 0
    try:
        age = time.time() - int(updated)
    except Exception:
        age = 0
    if age > _STALE_INTERMEDIATE_SECONDS:
        msg = f"更新在 {stage} 阶段卡死超过 {_STALE_INTERMEDIATE_SECONDS//60} 分钟，已自动复位"
        print(f"[updater] {msg} (age={int(age)}s)")
        save_state(stage=STAGE_FAILED, message=msg)
        return load_state()
    return st


def is_busy() -> bool:
    st = _heal_stale_state(load_state())
    return st.get("stage") not in (
        STAGE_IDLE, STAGE_SUCCESS, STAGE_FAILED, STAGE_ROLLED_BACK, None
    )


# ─── 命令执行助手 ────────────────────────────────────────────────

def _run(cmd: list[str], *, cwd: Optional[str] = None, timeout: int = 300) -> tuple[int, str]:
    """执行固定参数命令，返回 (returncode, combined_output)。绝不走 shell。"""
    if _stopping.is_set():
        return 130, "update interrupted by shutdown"
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                out, _ = p.communicate(timeout=0.2)
                return p.returncode, (out or "").strip()
            except subprocess.TimeoutExpired:
                if not _stopping.is_set() and time.monotonic() < deadline:
                    continue
                try: os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: p.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    p.communicate()
                return (130, "update interrupted by shutdown") if _stopping.is_set() else (124, f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except Exception as exc:
        return 1, f"exec error: {exc}"


# ─── 健康检查 ────────────────────────────────────────────────────

def _health_url() -> str:
    cfg = config.get()
    port = (cfg.get("listen") or {}).get("port", 22122)
    return f"http://127.0.0.1:{port}/health"


def wait_healthy(timeout: int = 90) -> tuple[bool, str]:
    """轮询 /health 直到 200 或超时。返回 (ok, detail)。"""
    url = _health_url()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline and not _stopping.is_set():
        try:
            resp = network.get_sync(url, timeout=5)
            if resp.status_code == 200:
                return True, "health 200"
            last = f"http {resp.status_code}"
        except Exception as exc:
            last = type(exc).__name__
        _stopping.wait(0.2)
    return False, f"health timeout ({last})"


# ─── 源码形态执行器（systemd / bare）────────────────────────────

def _git(args: list[str], timeout: int = 120) -> tuple[int, str]:
    return _run(["git", "-C", _app_dir()] + args, timeout=timeout)


def _src_has_local_changes() -> bool:
    """工作树有未提交改动 → True（拒绝自动更新，避免 reset 吃掉本地改动）。"""
    rc, out = _git(["status", "--porcelain"])
    if rc != 0:
        # 不是 git 仓库或 git 不可用，按"无改动"处理，让上层用其它判断兜底
        return False
    return bool(out.strip())


def _src_is_git_repo() -> bool:
    rc, _ = _git(["rev-parse", "--git-dir"])
    return rc == 0


def _src_current_commit() -> str:
    rc, out = _git(["rev-parse", "HEAD"])
    return out.strip() if rc == 0 else ""


def _source_data_excludes() -> list[str]:
    """Protect custom runtime files even if an operator put them under src/."""
    app = Path(_app_dir()).resolve()
    cfg = config.get()
    runtime, durable, legacy = state_db._paths()
    paths = [config.path(), _backup_root(), runtime, durable, legacy, state_db._manifest_path(runtime)]
    if Path(config.DATA_DIR).resolve() != app:
        paths.append(config.DATA_DIR)
    for value in (cfg.get("logDir", "logs"),
                  (cfg.get("images") or {}).get("dbPath", "image_logs.db"),
                  ((cfg.get("openai") or {}).get("store") or {}).get("dbPath", "openai_response_store.db")):
        paths.append(value if os.path.isabs(value) else os.path.join(config.DATA_DIR, value))
    excluded = []
    for path in paths:
        resolved = Path(path).resolve()
        if resolved != app and resolved.is_relative_to(app):
            relative = str(resolved.relative_to(app))
            excluded.extend(relative + suffix for suffix in ("", ".bak", ".bak.1", ".bak.2", ".bak.3", ".lock", "-wal", "-shm", "-journal"))
    return excluded


def _source_restore_args(commit: str, excludes=()) -> list[str]:
    # Reset --hard would also overwrite a tracked operator config/state file.
    return ["restore", "--source", commit, "--staged", "--worktree", "--", ".",
            *(f":(exclude){path}" for path in excludes)]


def _src_backup(target_tag: str) -> tuple[bool, str, str]:
    """源码备份：tar 打包当前源码 + 记录 commit。返回 (ok, backup_ref, detail)。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    ref = f"src-{__version__}-{ts}"
    dest = os.path.join(_backup_root(), ref + ".tar.gz")
    app = _app_dir()
    excludes = _source_data_excludes()
    try:
        update_supervisor.backup_code(app, dest, excludes)
    except Exception as exc:
        return False, "", f"source archive failed: {type(exc).__name__}"
    commit = _src_current_commit()
    meta = {"ref": ref, "tar": dest, "commit": commit, "version": __version__,
            "target_tag": target_tag, "mode": "src", "ts": ts,
            "created_at": time.time(), "excludes": excludes}
    monitor = Path(_backup_root()) / (ref + ".monitor.py")
    shutil.copyfile(update_supervisor.__file__, monitor)
    os.chmod(monitor, 0o600)
    update_supervisor.write_json(Path(_backup_root()) / (ref + ".json"), meta)
    return True, ref, f"backup ok ({dest})"


def _prepare_source_dependencies() -> dict:
    ref = load_state().get("backup_ref") or ""
    meta_path = Path(_backup_root()) / (ref + ".json")
    meta = json.loads(meta_path.read_text())
    paths = sysconfig.get_paths()
    snapshot = update_supervisor.snapshot_dependencies(
        str(Path(_backup_root()) / (ref + ".deps")),
        list(dict.fromkeys(paths[key] for key in ("purelib", "platlib", "scripts"))),
    )
    meta["dependencies"] = str(Path(snapshot["root"]) / "snapshot.json")
    update_supervisor.write_json(meta_path, meta)
    return snapshot


def _src_stage(target_tag: str) -> tuple[bool, str]:
    rc, out = _git(["fetch", "--all", "--tags", "--prune"], timeout=180)
    if rc != 0:
        return False, f"git fetch failed: {out[:300]}"
    rc, commit = _git(["rev-parse", "--verify", f"{target_tag}^{{commit}}"], timeout=30)
    if rc != 0:
        return False, "target release commit could not be verified"
    save_state(source_target_commit=commit.strip(),
               source_guard_digest=update_supervisor.code_digest(_app_dir(), _source_data_excludes()))
    return True, f"downloaded {target_tag}; source switch deferred until process stop"


def _src_pull(target_tag: str, prev_commit: str = "") -> tuple[bool, str]:
    """git fetch + checkout 到目标 tag。不重启。返回 (ok, detail)。

    prev_commit：更新前的 commit（来自备份 meta），用于稳健地判断 requirements.txt
    是否变化（替代脆弱的 HEAD@{1} reflog 依赖）。
    """
    rollback_commit = prev_commit.strip() if prev_commit else _src_current_commit()
    rc, out = _git(["fetch", "--all", "--tags", "--prune"], timeout=180)
    if rc != 0:
        return False, f"git fetch failed: {out[:300]}"
    # Change tracked source, never operator-owned runtime paths (even tracked ones).
    target_ref = target_tag
    excludes = _source_data_excludes()
    rc, out = _git(_source_restore_args(target_ref, excludes), timeout=120)
    if rc != 0:
        target_ref = f"origin/{target_tag}"
        rc2, out2 = _git(_source_restore_args(target_ref, excludes), timeout=120)
        if rc2 != 0:
            return False, f"checkout {target_tag} failed: {out[:200]} / {out2[:200]}"
    rc, out = _git(["reset", "--soft", target_ref], timeout=30)
    if rc != 0:
        return False, f"source revision update failed: {out[:200]}"
    # 依赖变更才装：优先用备份 commit 作 diff 基准；缺失则回退 HEAD@{1}；
    # 再不行就保守地"装一次"（宁可多装也不漏依赖导致启动失败）。
    need_install = False
    base = prev_commit.strip() if prev_commit else ""
    if base:
        rc, diff = _git(["diff", "--name-only", base, "HEAD"], timeout=30)
        need_install = (rc == 0 and "requirements.txt" in (diff or ""))
        if rc != 0:
            need_install = True   # diff 失败 → 保守安装
    else:
        rc, diff = _git(["diff", "--name-only", "HEAD@{1}", "HEAD"], timeout=30)
        need_install = (rc != 0) or ("requirements.txt" in (diff or ""))
    if need_install:
        # Install into the interpreter actually running the service, including
        # .venv/custom venv/system installs. Never silently skip missing pip,
        # escalate privileges, or bypass externally-managed-environment guards.
        snapshot = None
        try:
            snapshot = _prepare_source_dependencies()
            rc3, out3 = _run([sys.executable, "-m", "pip", "install", "-r",
                              os.path.join(_app_dir(), "requirements.txt")], timeout=600)
            if rc3 == 0:
                rc3, out3 = _run([sys.executable, "-m", "pip", "check"], timeout=60)
        except Exception as exc:
            rc3, out3 = 1, f"dependency backup/install failed: {type(exc).__name__}"
        finally:
            if snapshot is not None:
                update_supervisor.seal_dependencies(snapshot)
        if rc3 != 0:
            restored = False
            if rollback_commit:
                rc4, _ = _git(_source_restore_args(rollback_commit, _source_data_excludes()), timeout=120)
                restored = rc4 == 0
                if restored:
                    _git(["reset", "--soft", rollback_commit], timeout=30)
            ref = load_state().get("backup_ref")
            if ref:
                restored, repair = _src_rollback(ref)
            elif snapshot is not None:
                update_supervisor.restore_dependencies(str(Path(snapshot["root"]) / "snapshot.json"))
            return False, (f"dependency install/check failed: {out3[-300:]}; "
                           f"source/dependency rollback={restored}")
    save_state(target_code_digest=update_supervisor.code_digest(_app_dir(), _source_data_excludes()))
    return True, f"checked out {target_tag}"


def _src_restart() -> tuple[bool, str]:
    """Dispatch an independent, stdlib-only monitor before stopping this process."""
    try:
        st = load_state()
        ref = st.get("backup_ref") or ""
        root = Path(_backup_root())
        meta = json.loads((root / (ref + ".json")).read_text())
        monitor = root / (ref + ".monitor.py")
        if not monitor.is_file():
            shutil.copyfile(update_supervisor.__file__, monitor)
            os.chmod(monitor, 0o600)
        update_id = uuid.uuid4().hex
        result_path = str(root / (ref + ".result.json"))
        mode = st.get("mode") or _detect_mode()
        plan = {
            "update_id": update_id, "result_path": result_path,
            "app": _app_dir(), "archive": meta["tar"], "excludes": meta.get("excludes", []),
            "dependencies": meta.get("dependencies"), "mode": mode,
            "target_code_digest": st.get("target_code_digest"),
            "service": _cfg()["serviceName"], "old_pid": os.getpid(),
            "old_identity": update_supervisor.process_identity(os.getpid()),
            "command": [sys.executable, *sys.argv], "environment": dict(os.environ),
            "health_url": _health_url(), "health_timeout": _cfg()["healthTimeoutSeconds"],
            "stop_timeout": _cfg()["gracefulStopSeconds"],
            "target_version": st.get("target_tag") or st.get("to_version"),
            "from_version": st.get("from_version") or meta["version"],
            "rollback_commands": ([["git", "-C", _app_dir(), *_source_restore_args(meta["commit"], meta.get("excludes", []))],
                                   ["git", "-C", _app_dir(), "reset", "--soft", meta["commit"]]]
                                  if meta.get("commit") else []),
        }
        if st.get("source_target_commit"):
            if _src_has_local_changes() or _src_current_commit() != meta.get("commit"):
                return False, "source changed after staging; refusing to overwrite local work"
            target_commit = st["source_target_commit"]
            rc, diff = _git(["diff", "--name-only", meta["commit"], target_commit], timeout=30)
            if rc != 0:
                return False, "cannot verify staged dependency changes"
            paths = sysconfig.get_paths()
            plan["target_code_digest"] = st.get("source_guard_digest")
            plan["source_update"] = {
                "commands": [["git", "-C", _app_dir(), *_source_restore_args(target_commit, meta.get("excludes", []))],
                             ["git", "-C", _app_dir(), "reset", "--soft", target_commit]],
                "install": "requirements.txt" in diff.splitlines(),
                "python": sys.executable,
                "dependency_root": str(root / (ref + ".deps")),
                "dependency_paths": list(dict.fromkeys(paths[key] for key in ("purelib", "platlib", "scripts"))),
                "meta_path": str(root / (ref + ".json")),
            }
        plan_path = str(root / (ref + ".plan.json"))
        update_supervisor.write_json(plan_path, plan)
        save_state(supervisor_id=update_id, supervisor_result=result_path)
        command = [sys.executable, "-S", str(monitor), plan_path]
        if mode == MODE_SYSTEMD:
            # setsid alone does NOT escape the service's KillMode=control-group.
            # A transient one-shot unit survives the target unit's stop/restart.
            rc, detail = _run(["systemd-run", "--quiet", "--collect",
                               f"--unit=parrot-update-{update_id}",
                               f"--uid={os.getuid()}", f"--gid={os.getgid()}",
                               *command], timeout=15)
            return rc == 0, "source update monitor dispatched" if rc == 0 else detail
        with open(_update_log_path(), "a") as output:
            subprocess.Popen(command, cwd=_app_dir(), stdout=output, stderr=output,
                             start_new_session=True, close_fds=True)
        return True, "source update monitor dispatched"
    except Exception as exc:
        return False, f"source update monitor failed: {type(exc).__name__}"


def _src_rollback(backup_ref: str) -> tuple[bool, str]:
    """Restore code only; revert precisely the dependency files changed by pip."""
    if not backup_ref:
        return False, "no backup ref"
    meta_path = Path(_backup_root()) / (backup_ref + ".json")
    if not meta_path.is_file():
        return False, f"backup meta missing: {backup_ref}"
    try:
        meta = json.loads(meta_path.read_text())
        restored = False
        if meta.get("commit") and _src_is_git_repo():
            rc, _ = _git(_source_restore_args(meta["commit"],
                          (*meta.get("excludes", []), *_source_data_excludes())), timeout=120)
            restored = rc == 0
            if restored:
                _git(["reset", "--soft", meta["commit"]], timeout=30)
        if meta.get("tar") and os.path.isfile(meta["tar"]):
            update_supervisor.restore_code(_app_dir(), meta["tar"],
                                           (*meta.get("excludes", []), *_source_data_excludes()))
            restored = True
        if meta.get("dependencies"):
            update_supervisor.restore_dependencies(meta["dependencies"])
        return restored, "source/dependencies restored" if restored else "no usable code backup"
    except Exception as exc:
        return False, f"source/dependency restore failed: {type(exc).__name__}"


# ─── Docker 形态执行器 ───────────────────────────────────────────
#
# 关键约束：parrot 容器是 slim 镜像，**容器内根本没有 docker CLI**，连 sidecar
# 都不能用 `docker run` 起（那也要 docker CLI）。容器里唯一能操作 Docker 的途径
# 是挂载进来的 /var/run/docker.sock —— 直接走 Docker Engine HTTP API（httpx over UDS）。
#
# 因此两条路径：
#   - 宿主机部署（systemd/bare，装了 docker CLI）：直接 `docker ...` 命令。
#   - 容器内（无 CLI）：全部走 Engine API。
#     · inspect 当前镜像  → GET  /containers/{name}/json
#     · 备份打 tag        → POST /images/{name}/tag
#     · 拉新镜像          → POST /images/create?fromImage=&tag=
#     · 重建自己（难点）  → 用 Engine API 创建并启动一个一次性 docker:cli sidecar，
#                          由它在宿主侧跑 `docker compose up -d` 把 parrot 换掉。
#                          parrot 被杀也不影响 sidecar 跑完。


DOCKER_SOCK = "/var/run/docker.sock"


def _has_local_docker() -> bool:
    """当前进程能否直接调用 docker CLI（宿主机部署）。"""
    return shutil.which("docker") is not None


def _in_container() -> bool:
    return _detect_mode() == MODE_DOCKER and not _has_local_docker()


def _docker_compose_dir() -> str:
    return _cfg()["composeDir"]


# ── Engine API over unix socket（容器内用）─────────────────────

def _engine_client(timeout: int = 60):
    import httpx
    transport = httpx.HTTPTransport(uds=DOCKER_SOCK)
    return httpx.Client(transport=transport, base_url="http://localhost", timeout=timeout)


def _engine_inspect_image(name: str) -> str:
    """GET /containers/{name}/json → .Image（当前容器镜像 digest）。"""
    try:
        with _engine_client(30) as c:
            r = c.get(f"/containers/{name}/json")
            if r.status_code == 200:
                return (r.json() or {}).get("Image", "") or ""
    except Exception as exc:
        print(f"[updater] engine inspect failed: {exc}")
    return ""


def _engine_tag_image(image_ref: str, repo: str, tag: str) -> bool:
    """POST /images/{name}/tag?repo=&tag= 给镜像打新 tag。"""
    try:
        with _engine_client(30) as c:
            r = c.post(f"/images/{image_ref}/tag", params={"repo": repo, "tag": tag})
            return r.status_code in (200, 201)
    except Exception as exc:
        print(f"[updater] engine tag failed: {exc}")
        return False


def _engine_list_backup_tags() -> list[tuple[str, str, int | None]]:
    """GET /images/json 列出所有 parrot-backup:* 镜像标签 (tag, image_id, created_ts)。"""
    import urllib.parse
    if not os.path.exists(DOCKER_SOCK):
        return []
    flt = urllib.parse.quote(json.dumps({"reference": ["parrot-backup:*"]}))
    out: list[tuple[str, str, int | None]] = []
    try:
        with _engine_client(15) as c:
            r = c.get(f"/images/json?filters={flt}")
            if r.status_code != 200:
                return []
            for item in r.json() or []:
                if not isinstance(item, dict):
                    continue
                img_id = str(item.get("Id") or "")
                created = item.get("Created")
                created_ts = int(created) if isinstance(created, (int, float)) else None
                for tag in item.get("RepoTags") or []:
                    if isinstance(tag, str) and tag.startswith("parrot-backup:") and tag != "parrot-backup:<none>":
                        out.append((tag, img_id, created_ts))
    except Exception as exc:
        print(f"[updater] engine list backup tags failed: {exc}")
    return out


def _engine_remove_backup_tag(backup_tag: str) -> bool:
    """DELETE /images/{backup_tag} 移除单个 parrot-backup:* 标签（404 视为已不存在）。"""
    import urllib.parse
    if not backup_tag.startswith("parrot-backup:"):
        return False
    if not os.path.exists(DOCKER_SOCK):
        return False
    encoded = urllib.parse.quote(backup_tag, safe="")
    try:
        with _engine_client(30) as c:
            r = c.delete(f"/images/{encoded}")
            return r.status_code in (200, 404)
    except Exception as exc:
        print(f"[updater] engine remove backup tag failed ({backup_tag}): {exc}")
        return False


def _split_image_ref(image: str) -> tuple[str, str]:
    """拆 Docker image 为 (repo, tag)。不支持 digest 自更新时改 tag。"""
    image = (image or "").strip()
    if "@" in image:
        return image, ""
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        repo, _, tag = image.rpartition(":")
        return repo, tag or "latest"
    return image, "latest"


def _join_image_ref(repo: str, tag: str) -> str:
    return f"{repo}:{tag}" if tag else repo


def _docker_tag_from_release(target_tag: str) -> str:
    """GitHub release tag → Docker semver tag。

    GitHub Release 使用 vX.Y.Z；docker/metadata-action 的 semver tag 默认发布为
    X.Y.Z。Docker 自更新必须拉 Docker tag，而不是原样拿 release tag 去拉，
    否则 Engine API 会返回 404 manifest unknown。
    """
    tag = (target_tag or "").strip()
    if len(tag) > 1 and tag[0] in "vV" and tag[1].isdigit():
        return tag[1:]
    return tag


def _docker_pull_ref_for_target(image: str, target_tag: str) -> str:
    """本次更新应拉取的远端镜像。

    compose 仍然使用配置里的 image（可能是 latest、main、v0.23.0 或其它固定 tag）。
    这里先按目标 release 精确拉 repo:X.Y.Z，拉完再把本地配置 image tag 指向它，
    既避免 v 前缀 404，也避免 latest 漂移。
    """
    repo, _cur_tag = _split_image_ref(image)
    if "@" in repo:
        return image
    target_docker_tag = _docker_tag_from_release(target_tag)
    if target_docker_tag:
        return _join_image_ref(repo, target_docker_tag)
    return image


def _tag_image_for_compose(source_ref: str, compose_image: str) -> tuple[bool, str]:
    """把已拉取的 source_ref 打成本地 compose 使用的 image tag。"""
    if source_ref == compose_image:
        return True, "image tag already matches compose image"
    repo, tag = _split_image_ref(compose_image)
    if not tag or "@" in repo:
        return False, f"cannot tag digest image for compose: {compose_image}"
    if _has_local_docker():
        rc, out = _run(["docker", "tag", source_ref, compose_image], timeout=120)
        if rc != 0:
            return False, f"docker tag failed: {out[-200:]}"
        return True, f"tagged {source_ref} -> {compose_image}"
    ok = _engine_tag_image(source_ref, repo, tag)
    return (ok, f"tagged {source_ref} -> {compose_image}" if ok else f"engine tag failed: {source_ref} -> {compose_image}")


def _engine_pull_image(image: str) -> tuple[bool, str]:
    """POST /images/create?fromImage=&tag= 拉镜像（流式响应，读完即拉完）。"""
    if ":" in image.rsplit("/", 1)[-1]:
        from_image, _, tag = image.rpartition(":")
    else:
        from_image, tag = image, "latest"
    try:
        with _engine_client(600) as c:
            with c.stream("POST", "/images/create",
                          params={"fromImage": from_image, "tag": tag}) as r:
                last = ""
                for line in r.iter_lines():
                    if line:
                        last = line
                if r.status_code != 200:
                    detail = f"pull http {r.status_code} for {image}"
                    if last:
                        detail += f": {last[:200]}"
                    return False, detail
                if "error" in last.lower():
                    return False, f"pull error for {image}: {last[:200]}"
        return True, f"image pulled (engine api): {image}"
    except Exception as exc:
        return False, f"engine pull failed for {image}: {exc}"


def _engine_run_sidecar(inner_cmd: str) -> tuple[bool, str]:
    """用 Engine API 创建并启动一次性 docker:cli sidecar，执行 inner_cmd。

    sidecar 挂 docker.sock + composeDir，自带 compose；HostConfig.AutoRemove 自清理。
    用于 recreate（会把 parrot 自己换掉）。
    """
    updater_image = _cfg()["updaterImage"]
    cdir = _docker_compose_dir()
    # 关键：compose 文件里的相对挂载（如 ./data）按"compose 工作目录"解析。
    # 必须把 composeDir 挂到 sidecar 里它真实的宿主路径（而非 /workspace），
    # 否则 ./data 会解析成 sidecar 视角的错误路径，新容器挂到空目录、config 丢失。
    body = {
        "Image": updater_image,
        "Cmd": ["sh", "-c", inner_cmd],
        "WorkingDir": cdir,
        "HostConfig": {
            "AutoRemove": True,
            "Binds": [
                f"{DOCKER_SOCK}:{DOCKER_SOCK}",
                f"{cdir}:{cdir}",
            ],
        },
    }
    try:
        with _engine_client(120) as c:
            # 确保 sidecar 镜像在（容器内无法 docker pull，靠 Engine API 拉）
            insp = c.get(f"/images/{updater_image}/json")
            if insp.status_code != 200:
                ok, detail = _engine_pull_image(updater_image)
                if not ok:
                    return False, f"sidecar image pull failed: {detail}"
            r = c.post("/containers/create", params={"name": "parrot-updater"}, json=body)
            if r.status_code == 409:
                # 同名残留，先删再建
                c.delete("/containers/parrot-updater", params={"force": "true"})
                r = c.post("/containers/create", params={"name": "parrot-updater"}, json=body)
            if r.status_code not in (200, 201):
                return False, f"sidecar create http {r.status_code}: {r.text[:200]}"
            cid = (r.json() or {}).get("Id", "")
            r2 = c.post(f"/containers/{cid}/start")
            if r2.status_code not in (204, 200):
                return False, f"sidecar start http {r2.status_code}"
        return True, "sidecar dispatched (engine api)"
    except Exception as exc:
        return False, f"engine sidecar failed: {exc}"


# ── 宿主 CLI 路径（systemd/bare 装了 docker）─────────────────────

def _compose_up_inner(backup_digest: str = "", health_port: int = 0) -> str:
    """sidecar 内执行的"重建 + 健康门控 + 自动回滚"脚本。

    docker 自更新的两个固有难题，都在 sidecar 侧解决：
    1) 名字冲突：发起更新时旧容器仍在跑，compose up 会撞名 → 先发送 SIGTERM，
       等待 Parrot 排空请求并完成 StateStore 快照收尾，再移除已停止的旧容器。
    2) 新容器崩溃谁来回滚：新版若起不来，容器内的 resume 跑不了 → 由 sidecar 承担
       健康检查；失败则把 image tag 指回备份 digest 并重建旧版，写回滚标记到 data。

    health_port：容器内端口（compose 里映射的容器侧端口，固定 22122 走容器网络不通，
    这里用 `docker exec` 到容器内 curl /health 最稳）。
    backup_digest：健康检查失败时回滚的目标镜像 digest。
    """
    cfg = _cfg()
    svc = cfg["composeService"]
    name = cfg["containerName"]
    image = cfg["image"]
    cdir = _docker_compose_dir()
    flag_dir = f"{cdir}/data"   # sidecar 挂的是 composeDir:composeDir，真实路径在这
    health_seconds = max(1, int(cfg.get("healthTimeoutSeconds", 90) or 90))
    health_port = int(health_port or 22122)
    if not 1 <= health_port <= 65535:
        raise ValueError("invalid health port")
    st = load_state()
    target_version_q = shlex.quote(str(st.get("target_tag") or "").lstrip("vV"))
    from_version_q = shlex.quote(str(st.get("from_version") or __version__).lstrip("vV"))
    stop_tries = max(10, min(600, int(cfg.get("gracefulStopSeconds", 100) or 100)))
    log = f"{flag_dir}/.update_log"

    # 这些值来自用户部署环境/config，虽然正常情况下只会是 parrot/镜像名/绝对路径，
    # 但 sidecar 脚本是 shell 字符串；统一 shlex.quote，避免空格/特殊字符把脚本拼坏。
    svc_q = shlex.quote(svc)
    name_q = shlex.quote(name)
    image_q = shlex.quote(image)
    flag_dir_q = shlex.quote(flag_dir)
    log_q = shlex.quote(log)
    backup_digest_q = shlex.quote(backup_digest) if backup_digest else "''"
    # 全程把关键步骤 + 错误输出写入 .update_log，供 TG「查看失败日志」读取。
    #
    # 零裸奔重建策略（修订版，避开 rename+compose label 冲突）：
    #   ① 先 `compose config` 校验 compose 合法——不合法直接放弃，**完全不动旧容器**。
    #   ② 旧容器的镜像 digest 已在 stage 阶段备份（backup_digest）= 回滚锚点。
    #   ③ SIGTERM + 等待退出 + rm 已停止容器 → compose up 起新容器（compose 靠 service label 管理，
    #      不能用 rename 留存——rename 后 compose 仍能通过 label 找回它并误重建）。
    #   ④ 健康门控。
    #   ⑤ 失败：把镜像 tag 指回备份 digest，compose up 重建旧版本，并**再次健康验证**，
    #      确保回滚后服务真的可用；只有回滚后健康才写 ROLLBACK（成功回滚），
    #      否则写 ROLLBACK_FAILED（回滚也没起来，极端情况，需人工）。
    health_code = ('import json,sys; d=json.load(sys.stdin); '
                   'sys.exit(0 if d.get("status") not in ("error","draining") and '
                   '(not sys.argv[1] or str(d.get("version", "")).lstrip("vV")==sys.argv[1]) else 1)')
    health_command = shlex.quote(f'curl --connect-timeout 2 --max-time 3 -fsS '
                                 f'http://127.0.0.1:{health_port}/health | '
                                 f'python -c {shlex.quote(health_code)} "$1"')
    script = f"""set +e
LOG={log_q}
FLAG_DIR={flag_dir_q}
SVC={svc_q}
NAME={name_q}
IMAGE={image_q}
BACKUP_DIGEST={backup_digest_q}
: > "$LOG" 2>/dev/null || true
logln() {{ echo "[$(date -u +%H:%M:%S)] $*" >> "$LOG" 2>/dev/null || true; }}
# Bound every Engine CLI wait; a wedged daemon must not block rollback forever.
docker() {{ timeout -s TERM -k 2 30 docker "$@"; }}
EXPECTED_VERSION={target_version_q}
FROM_VERSION={from_version_q}
EXPECTED_DIGEST="$(docker image inspect -f '{{{{.Id}}}}' "$IMAGE" 2>/dev/null)"
health_ok() {{ [ -n "$EXPECTED_DIGEST" ] && timeout -s TERM -k 1 "$PROBE_TIMEOUT" docker exec "$NAME" sh -c {health_command} sh "$EXPECTED_VERSION" >/dev/null 2>&1 && [ "$(timeout -s TERM -k 1 "$PROBE_TIMEOUT" docker inspect -f '{{{{.Image}}}}' "$NAME" 2>/dev/null)" = "$EXPECTED_DIGEST" ]; }}
wait_health() {{
  deadline=$(( $(date +%s) + {health_seconds} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    left=$(( deadline - $(date +%s) ))
    PROBE_TIMEOUT=$left
    [ "$PROBE_TIMEOUT" -le 5 ] || PROBE_TIMEOUT=5
    [ "$PROBE_TIMEOUT" -gt 0 ] || return 1
    if health_ok; then return 0; fi
    sleep 1
  done
  return 1
}}
container_running() {{
  [ "$(docker inspect -f '{{{{.State.Running}}}}' "$NAME" 2>/dev/null || true)" = "true" ]
}}
graceful_remove() {{
  if ! docker inspect "$NAME" >/dev/null 2>&1; then return 0; fi
  if container_running; then
    logln "向容器发送 SIGTERM，等待 Parrot 优雅退出（最多 {stop_tries}s）…"
    if ! docker kill --signal=TERM "$NAME" >/dev/null 2>&1; then
      logln "❌ 无法向容器发送 SIGTERM；拒绝强制移除"
      return 1
    fi
    stopped=0
    for i in $(seq 1 {stop_tries}); do
      if ! container_running; then stopped=1; break; fi
      sleep 1
    done
    if [ "$stopped" != "1" ]; then
      logln "❌ 容器未在 {stop_tries}s 内优雅退出；拒绝使用 SIGKILL"
      return 1
    fi
  fi
  if ! docker rm "$NAME" >/dev/null 2>&1; then
    logln "❌ 已停止容器无法安全移除"
    return 1
  fi
  logln "✅ 旧容器已优雅停止并移除"
  return 0
}}
service_exists() {{
  printf '%s\n' "$SERVICES" | grep -Fx -- "$1" >/dev/null 2>&1
}}
resolve_service() {{
  SERVICES="$(docker compose config --services 2>&1)"
  SERVICES_RC=$?
  if [ $SERVICES_RC -ne 0 ]; then
    logln "❌ compose 服务列表读取失败："
    echo "$SERVICES" | head -20 >> "$LOG" 2>/dev/null || true
    echo "ROLLBACK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
    exit 1
  fi
  if [ -n "$SVC" ] && service_exists "$SVC"; then
    return 0
  fi
  OLD_SVC="$SVC"
  LABEL_SVC="$(docker inspect -f '{{{{ index .Config.Labels "com.docker.compose.service" }}}}' "$NAME" 2>/dev/null || true)"
  if [ -n "$LABEL_SVC" ] && [ "$LABEL_SVC" != "<no value>" ] && service_exists "$LABEL_SVC"; then
    SVC="$LABEL_SVC"
    logln "⚠️ composeService=$OLD_SVC 不存在，自动使用当前容器的 compose service：$SVC"
    return 0
  fi
  for candidate in "$NAME" "parrot" "anthropic-proxy"; do
    if [ -n "$candidate" ] && service_exists "$candidate"; then
      SVC="$candidate"
      logln "⚠️ composeService=$OLD_SVC 不存在，自动使用检测到的服务：$SVC"
      return 0
    fi
  done
  SERVICE_COUNT="$(printf '%s\n' "$SERVICES" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
  if [ "$SERVICE_COUNT" = "1" ]; then
    SVC="$(printf '%s\n' "$SERVICES" | sed '/^[[:space:]]*$/d' | head -1)"
    logln "⚠️ composeService=$OLD_SVC 不存在，自动使用唯一 compose 服务：$SVC"
    return 0
  fi
  logln "❌ composeService=$OLD_SVC 不存在，且无法自动判断。可用服务：$(printf '%s' "$SERVICES" | tr '\n' ' ')"
  echo "ROLLBACK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
  exit 1
}}

fail_rollback() {{
  logln "❌ 更新失败，开始回滚到备份版本"
  # Do not publish ROLLBACK until the old image is restored AND healthy.
  if [ -z "$BACKUP_DIGEST" ] || ! docker tag "$BACKUP_DIGEST" "$IMAGE" 2>/dev/null; then
    logln "❌ 无可用备份 digest 或回滚 tag 失败；需人工介入"
    echo "ROLLBACK_FAILED" > "$FLAG_DIR/.update_result" 2>/dev/null || true
    exit 1
  fi
  logln "已把镜像 tag 指回备份 digest"
  if ! graceful_remove; then
    logln "❌ 无法安全移除失败容器，停止回滚以保护数据库"
    echo "ROLLBACK_FAILED" > "$FLAG_DIR/.update_result" 2>/dev/null || true
    exit 1
  fi
  RB_OUT="$(docker compose up -d --force-recreate --pull never "$SVC" 2>&1)"
  RB_RC=$?
  echo "$RB_OUT" | tail -10 >> "$LOG" 2>/dev/null || true
  logln "回滚重建完成，验证健康…"
  EXPECTED_VERSION="$FROM_VERSION"
  EXPECTED_DIGEST="$BACKUP_DIGEST"
  if [ $RB_RC -eq 0 ] && wait_health; then
    logln "✅ 回滚成功，旧版本已恢复并健康"
    echo "ROLLBACK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
  else
    logln "❌ 回滚后健康检查仍未通过（需人工介入）"
    echo "ROLLBACK_FAILED" > "$FLAG_DIR/.update_result" 2>/dev/null || true
  fi
  exit 1
}}

sleep 1
logln "开始重建：service=$SVC container=$NAME image=$IMAGE"
# ① 校验 compose 合法；不合法 → 完全不动旧容器
CFG_ERR="$(docker compose config 2>&1 >/dev/null)"
if [ $? -ne 0 ]; then
  logln "❌ compose 文件校验失败，未改动旧容器："
  echo "$CFG_ERR" | head -20 >> "$LOG" 2>/dev/null || true
  echo "ROLLBACK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
  exit 1
fi
logln "✅ compose 校验通过（备份 digest=$BACKUP_DIGEST）"
# ② 先确认目标 service 存在；配置错时尽量按 container_name / 常见服务名 / 唯一服务自动修正。
#    这一步必须在 rm 旧容器前完成，避免 service 名失配时先停服务再报 no such service。
resolve_service
logln "使用 compose service=$SVC"
if [ -z "$EXPECTED_DIGEST" ]; then
  logln "❌ 无法确认目标镜像身份；未停止旧容器"
  echo "ROLLBACK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
  exit 1
fi
# ③ SIGTERM + 等待优雅退出后移除旧容器；任何失败都拒绝 SIGKILL。
if ! graceful_remove; then
  logln "❌ 旧容器未能安全停止，已中止更新"
  echo "ROLLBACK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
  exit 1
fi
logln "启动新容器…"
UP_OUT="$(docker compose up -d --force-recreate --pull never "$SVC" 2>&1)"
UP_RC=$?
echo "$UP_OUT" | tail -20 >> "$LOG" 2>/dev/null || true
if [ $UP_RC -ne 0 ]; then logln "❌ compose up 失败（rc=$UP_RC）"; fail_rollback; fi
logln "新容器已启动，等待健康检查（最多 {health_seconds}s）…"
# ④ 健康门控
if ! wait_health; then
  logln "❌ 健康检查未通过（{health_seconds}s 内 /health 未就绪）"
  logln "新容器最近日志："
  docker logs --tail 30 "$NAME" >> "$LOG" 2>&1 || true
  fail_rollback
fi
# ⑤ 成功
logln "✅ 更新成功，健康检查通过"
echo "OK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
exit 0
"""
    result_id = st.get("result_id")
    if result_id:
        script = script.replace('/.update_result"', '/.update_result.' + str(result_id) + '"')
    return script


def _docker_current_image_digest() -> str:
    """当前运行容器的镜像 digest（回滚锚点）。"""
    name = _cfg()["containerName"]
    if _has_local_docker():
        rc, out = _run(["docker", "inspect", "--format", "{{.Image}}", name], timeout=30)
        return out.strip() if rc == 0 else ""
    return _engine_inspect_image(name)


def _docker_backup(target_tag: str) -> tuple[bool, str, str]:
    """Docker 备份：记录当前镜像 digest + 打 backup tag，便于回滚。"""
    image = _cfg()["image"]
    digest = _docker_current_image_digest()
    if not digest:
        return False, "", "cannot inspect current container image"
    ts = time.strftime("%Y%m%d-%H%M%S")
    owner = _backup_owner(create=True)
    backup_repo = "parrot-backup"
    backup_label = f"{owner}-{__version__}-{ts}"
    ref = f"docker-{backup_label}"
    backup_tag = f"{backup_repo}:{backup_label}"
    if _has_local_docker():
        rc, _ = _run(["docker", "tag", digest, backup_tag], timeout=30)
        tagged = rc == 0
    else:
        tagged = _engine_tag_image(digest, backup_repo, backup_label)
    if not tagged:
        return False, "", f"backup tag failed: {backup_tag}"
    meta = {"ref": ref, "image": image, "digest": digest,
            "backup_tag": backup_tag, "owner": owner, "version": __version__,
            "target_tag": target_tag, "mode": "docker", "ts": ts, "created_at": time.time()}
    with open(os.path.join(_backup_root(), ref + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return True, ref, f"backup tag {backup_tag} (digest {digest[:19]})"


def _docker_pull(target_tag: str) -> tuple[bool, str]:
    """拉新镜像，不重建。

    关键点：GitHub release tag 通常是 vX.Y.Z，但 GHCR semver tag 是 X.Y.Z。
    先拉目标 Docker tag，再把本地 compose 使用的 image tag 指向它。
    """
    image = _cfg()["image"]
    pull_ref = _docker_pull_ref_for_target(image, target_tag)
    if _has_local_docker():
        rc, out = _run(["docker", "pull", pull_ref], timeout=600)
        if rc != 0:
            return False, f"docker pull failed for {pull_ref}: {out[-300:]}"
        ok, detail = _tag_image_for_compose(pull_ref, image)
        if not ok:
            return False, detail
        return True, f"image pulled: {pull_ref}; {detail}"
    # 容器内：Engine API 拉目标镜像，再打成本地 compose image tag
    ok, detail = _engine_pull_image(pull_ref)
    if not ok:
        return False, detail
    ok, tag_detail = _tag_image_for_compose(pull_ref, image)
    if not ok:
        return False, tag_detail
    return True, f"{detail}; {tag_detail}"


def _docker_sidecar_recreate(backup_digest: str = "") -> tuple[bool, str]:
    """起一次性 sidecar 执行 compose up -d，parrot 容器被 recreate（自己被换掉）。

    宿主有 CLI → 直接 docker run 起 sidecar；容器内 → Engine API 起 sidecar。
    backup_digest：传给 sidecar，健康检查失败时回滚到此 digest。
    """
    inner = _compose_up_inner(backup_digest=backup_digest,
                              health_port=int((config.get().get("listen") or {}).get("port", 22122)))
    if _has_local_docker():
        cdir = _docker_compose_dir()
        cmd = [
            "docker", "run", "-d", "--rm", "--name", "parrot-updater",
            "-v", f"{DOCKER_SOCK}:{DOCKER_SOCK}",
            "-v", f"{cdir}:{cdir}", "-w", cdir,
            _cfg()["updaterImage"], "sh", "-c", inner,
        ]
        rc, out = _run(cmd, timeout=120)
        if rc != 0:
            return False, f"sidecar launch failed: {out[-300:]}"
        return True, "sidecar recreate dispatched"
    return _engine_run_sidecar(inner)


def _docker_rollback(backup_ref: str) -> tuple[bool, str]:
    """Docker 回滚：把镜像 tag 指回 backup digest，再 sidecar recreate。"""
    if not backup_ref:
        return False, "no backup ref"
    meta_path = os.path.join(_backup_root(), backup_ref + ".json")
    if not os.path.exists(meta_path):
        return False, f"backup meta missing: {backup_ref}"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    digest = meta.get("digest") or ""
    image = meta.get("image") or _cfg()["image"]
    if not digest:
        return False, "backup digest missing"
    # 把 image:tag 重新指向旧 digest（回滚镜像）
    repo, _, tag = image.rpartition(":")
    if not repo:
        repo, tag = image, "latest"
    if _has_local_docker():
        rc, out = _run(["docker", "tag", digest, image], timeout=30)
        ok_tag = (rc == 0)
    else:
        ok_tag = _engine_tag_image(digest, repo, tag)
    if not ok_tag:
        return False, "retag rollback failed"
    ok, detail = _docker_sidecar_recreate()
    return ok, f"rollback retag ok; {detail}"

SILENT_IDLE_QUIET_SECONDS = 180.0


def try_silent_activate(*, quiet_seconds: float = SILENT_IDLE_QUIET_SECONDS) -> tuple[bool, str]:
    """若当前有待激活的 staged 更新且满足空闲窗口，则自动触发重启。"""
    cfg = _cfg()
    if _stopping.is_set() or not cfg["enabled"] or not cfg["silentUpdate"]:
        return False, "silent update disabled"
    st = load_state()
    if st.get("target_tag") in cfg["ignoredVersions"] or st.get("target_repo") != cfg["repo"]:
        return False, "target ignored or repository changed"
    if st.get("stage") != STAGE_STAGED:
        return False, "not staged"
    from . import drain
    if not drain.is_idle_for_update(quiet_seconds):
        return False, "busy"
    return confirm_restart(silent=True, require_idle_seconds=quiet_seconds)
#
# 两阶段双重确认：
#   stage_update()   ：备份 + 拉取 → 进入 staged 态（不重启）
#   confirm_restart()：用户二次确认后重启生效
#   cancel_staged()  ：staged 态下取消（源码回滚 checkout / docker 不动镜像）
#   resume_after_restart()：新进程启动时调用，完成健康检查 + 成功/回滚

_progress_cb = None  # 由 TG 层注册：cb(stage:str, text:str) -> None


def set_progress_callback(cb) -> None:
    global _progress_cb
    _progress_cb = cb


def _emit(stage: str, text: str, *, silent: bool = False) -> None:
    if _progress_cb and not silent:
        try:
            _progress_cb(stage, text)
        except Exception as exc:
            print(f"[updater:{stage}] progress cb failed: {exc}")
    print(f"[updater:{stage}] {text}")


def stage_update(target_tag: str, *, chat_id: Optional[int] = None,
                 notify_msg_id: Optional[int] = None,
                 silent: bool = False) -> tuple[bool, str]:
    """第一阶段：备份 → 拉取 → staged。线程安全，幂等拒绝并发。"""
    if _stopping.is_set():
        return False, "服务正在关闭，未执行更新操作"
    if not _op_lock.acquire(blocking=False):
        return False, "另一个更新操作正在进行中"
    try:
        if _stopping.is_set():
            return False, "服务正在关闭，未执行更新操作"
        if is_busy():
            return False, "已有更新在进行中（staged/restarting）"
        mode = _detect_mode()
        reset_update_log()
        append_update_log(f"开始更新：{__version__} → {target_tag}（形态 {mode}{' · 静默' if silent else ''}）")
        save_state(stage=STAGE_BACKING_UP, mode=mode, from_version=__version__,
                   target_tag=target_tag, to_version=target_tag, target_repo=_cfg()["repo"],
                   chat_id=None if silent else chat_id,
                   notify_msg_id=None if silent else notify_msg_id,
                   silent=bool(silent),
                   message="开始备份",
                   supervisor_id=None, supervisor_result=None, target_code_digest=None,
                   source_target_commit=None, source_guard_digest=None,
                   silent_error_notified=None, silent_error_attempt_at=None, cancelled_target=None)
        _emit(STAGE_BACKING_UP, f"📦 正在备份当前版本 v{__version__} …", silent=silent)

        # 源码形态：先挡掉未提交改动
        if mode in (MODE_SYSTEMD, MODE_BARE) and _src_is_git_repo():
            if _src_has_local_changes():
                save_state(stage=STAGE_FAILED, message="工作树有未提交改动，已拒绝更新")
                fail_msg = "❌ 源码目录有未提交改动，已拒绝自动更新（避免覆盖本地改动）"
                _emit(STAGE_FAILED, fail_msg, silent=silent)
                if silent:
                    notify_silent_failure("工作树有未提交改动，已拒绝更新")
                return False, "工作树有未提交改动"

        # ① 备份
        if mode == MODE_DOCKER:
            ok, ref, detail = _docker_backup(target_tag)
        else:
            ok, ref, detail = _src_backup(target_tag)
        if not ok:
            append_update_log(f"❌ 备份失败：{detail}")
            save_state(stage=STAGE_FAILED, message=f"备份失败: {detail}")
            _emit(STAGE_FAILED, f"❌ 备份失败：{detail}", silent=silent)
            if silent:
                notify_silent_failure(f"备份失败: {detail}")
            return False, detail
        append_update_log(f"✅ 备份完成：{detail}")
        save_state(stage=STAGE_PULLING, backup_ref=ref, message=f"备份完成: {detail}")
        _emit(STAGE_PULLING, f"✅ 备份完成 → ⬇️ 正在拉取 {target_tag} …", silent=silent)

        # ② 拉取
        if mode == MODE_DOCKER:
            ok, detail = _docker_pull(target_tag)
        elif silent:
            ok, detail = _src_stage(target_tag)
        else:
            # 从备份 meta 取更新前 commit，供 _src_pull 稳健判断依赖变化
            prev_commit = ""
            try:
                _mp = os.path.join(_backup_root(), ref + ".json")
                if os.path.exists(_mp):
                    with open(_mp, encoding="utf-8") as _f:
                        prev_commit = (json.load(_f) or {}).get("commit", "") or ""
            except Exception:
                pass
            ok, detail = _src_pull(target_tag, prev_commit=prev_commit)
        if not ok:
            if mode != MODE_DOCKER and not silent:
                restored, restore_detail = _src_rollback(ref)
                detail += f"; rollback={restored}: {restore_detail}"
            append_update_log(f"❌ 拉取失败：{detail}")
            save_state(stage=STAGE_FAILED, message=f"拉取失败: {detail}")
            _emit(STAGE_FAILED, f"❌ 拉取失败：{detail}", silent=silent)
            if silent:
                notify_silent_failure(f"拉取失败: {detail}")
            return False, detail
        if _stopping.is_set():
            if mode != MODE_DOCKER and not silent:
                _src_rollback(ref)
            save_state(stage=STAGE_FAILED, message="更新因关停中断；未进入 staged")
            return False, "update interrupted by shutdown"
        append_update_log(f"✅ 拉取完成：{detail}")

        # ③ 进入 staged（手动模式停下等二次确认；静默模式等待空闲窗口自动重启）
        _prune_backups_locked()
        staged_msg = f"已静默拉取 {target_tag}，等待空闲自动重启" if silent else f"已拉取 {target_tag}，等待确认重启"
        save_state(stage=STAGE_STAGED, message=staged_msg)
        _emit(STAGE_STAGED,
              f"✅ 已备份并拉取 <b>{target_tag}</b>，<b>尚未重启</b>。\n"
              f"{'等待当前业务空闲后自动重启生效。' if silent else '请二次确认是否立即重启生效。'}",
              silent=silent)
        return True, "staged"
    finally:
        _op_lock.release()


def _prepare_state_restart(st: dict) -> str:
    """Synchronously install and verify all pending StateStore snapshots."""
    state_db.flush(strict=True)
    return "verified StateStore snapshots"


def _restart_guard_failed(detail: str) -> tuple[bool, str]:
    """Keep a staged update recoverable when database protection fails closed."""
    public_detail = f"state database protection failed: {detail}"
    append_update_log(f"❌ 重启前数据库保护失败，已阻止重启：{detail}")
    try:
        save_state(stage=STAGE_STAGED, message=f"数据库保护失败，已阻止重启: {detail}")
    except Exception as state_exc:
        append_update_log(f"⚠️ staged 状态回写失败：{state_exc}")
    _emit(
        STAGE_STAGED,
        "❌ 状态快照写入或完整性验证失败，已阻止重启。\n"
        f"详情：{detail}",
        silent=bool(load_state().get("silent")),
    )
    notify_silent_failure(public_detail)
    return False, public_detail


def confirm_restart(*, silent: bool = False, require_idle_seconds: float | None = None) -> tuple[bool, str]:
    """第二阶段：数据库保护成功后才允许重启并由新进程 resume。

    用 _op_lock 防止「连点两次确认」触发两次备份或重启（TOCTOU）。
    """
    if _stopping.is_set():
        return False, "服务正在关闭，未执行更新操作"
    if not _op_lock.acquire(blocking=False):
        return False, "另一个更新操作正在进行中"
    reservation = None
    dispatched = False
    from . import drain
    try:
        if _stopping.is_set():
            return False, "服务正在关闭，未执行更新操作"
        st = load_state()
        if st.get("stage") != STAGE_STAGED:
            return False, f"当前不在 staged 态（{st.get('stage')}），无法确认重启"
        if require_idle_seconds is not None:
            cfg = _cfg()
            if (not cfg["enabled"] or not cfg["silentUpdate"] or st.get("target_tag") in cfg["ignoredVersions"]
                    or st.get("target_repo") != cfg["repo"]):
                return False, "silent update disabled or target changed"
            reservation = drain.try_reserve_update(require_idle_seconds)
            if reservation is None:
                return False, "当前仍有请求在运行或尚未达到空闲静默窗口"
        is_silent = bool(silent or st.get("silent"))
        mode = st.get("mode") or _detect_mode()

        # Fail closed: both snapshots must be installed and read-back verified.
        try:
            snapshot_status = _prepare_state_restart(st)
        except Exception as exc:
            return _restart_guard_failed(str(exc))
        append_update_log(f"✅ 状态快照重启保护完成：{snapshot_status}")

        # Durable StateStore guarantees the restarting state is verified before return.
        try:
            save_state(stage=STAGE_RESTARTING,
                       silent=is_silent,
                       message="检测到业务空闲，正在静默重启生效" if is_silent else "用户已确认，正在重启生效",
                       result_id=uuid.uuid4().hex if mode == MODE_DOCKER else None)
        except Exception as exc:
            return _restart_guard_failed(f"persist restarting state: {exc}")

        _emit(STAGE_RESTARTING, "🔄 状态快照保护完成，正在重启生效 …", silent=is_silent)
        if mode == MODE_DOCKER:
            # 取备份 digest 传给 sidecar，供健康检查失败时自动回滚
            backup_digest = ""
            bref = st.get("backup_ref") or ""
            if bref:
                mp = os.path.join(_backup_root(), bref + ".json")
                if os.path.exists(mp):
                    try:
                        with open(mp, encoding="utf-8") as f:
                            backup_digest = (json.load(f) or {}).get("digest", "") or ""
                    except Exception:
                        pass
            # 清掉上一次的结果标记，避免误读
            try:
                os.remove(os.path.join(config.DATA_DIR, ".update_result"))
            except Exception:
                pass
            ok, detail = _docker_sidecar_recreate(backup_digest=backup_digest)
        else:
            ok, detail = _src_restart()
        if not ok:
            save_state(stage=STAGE_FAILED, message=f"重启触发失败: {detail}")
            _emit(STAGE_FAILED, f"❌ 重启触发失败：{detail}", silent=is_silent)
            notify_silent_failure(f"重启触发失败: {detail}")
            return False, detail
        dispatched = True
        if reservation is not None:
            threading.Thread(target=_watch_silent_dispatch, args=(reservation, load_state()),
                             daemon=True, name="silent-update-dispatch").start()
        return True, detail
    except Exception as exc:
        notify_silent_failure(f"更新激活失败: {type(exc).__name__}")
        raise
    finally:
        if reservation is not None and not dispatched:
            drain.release_update(reservation)
        _op_lock.release()


def cancel_staged() -> tuple[bool, str]:
    """staged 态取消：源码 checkout 回原 commit；docker 恢复本地 compose 镜像 tag。

    用 _op_lock 防止与 confirm_restart 竞争（避免「同时取消又确认」）。
    """
    if _stopping.is_set():
        return False, "服务正在关闭，未执行更新操作"
    if not _op_lock.acquire(blocking=False):
        return False, "另一个更新操作正在进行中"
    try:
        if _stopping.is_set():
            return False, "服务正在关闭，未执行更新操作"
        st = load_state()
        if st.get("stage") != STAGE_STAGED:
            return False, f"当前不在 staged 态（{st.get('stage')}）"
        mode = st.get("mode") or _detect_mode()
        backup_ref = st.get("backup_ref") or ""
        if mode in (MODE_SYSTEMD, MODE_BARE):
            if st.get("source_target_commit"):
                ok, detail = True, "source was downloaded but not applied"
            else:
                ok, detail = _src_rollback(backup_ref)
        else:
            # _docker_pull already moved the compose tag. Restore it without
            # recreating the still-running old container, or a later ordinary
            # compose up would silently activate a cancelled update.
            try:
                with open(os.path.join(_backup_root(), backup_ref + ".json"), encoding="utf-8") as f:
                    meta = json.load(f)
                digest = meta.get("digest") or ""
                if not digest:
                    raise ValueError("backup digest missing")
                ok, detail = _tag_image_for_compose(digest, meta.get("image") or _cfg()["image"])
            except Exception as exc:
                ok, detail = False, f"cannot restore compose image tag: {exc}"
        if ok:
            reset_state()
            if st.get("target_tag"):
                save_state(cancelled_target=st["target_tag"], cancelled_repo=_cfg()["repo"])
            _emit(STAGE_IDLE, f"↩️ 已取消更新：{detail}")
        else:
            save_state(stage=STAGE_STAGED, message=f"取消回滚失败: {detail}")
        return ok, detail
    finally:
        _op_lock.release()


def notify_silent_failure(detail: str) -> None:
    st = load_state()
    if not st.get("silent"):
        return
    key = f"{st.get('target_tag')}:{detail}"
    if st.get("silent_error_notified") == key:
        return
    now = time.time()
    if now - float(st.get("silent_error_attempt_at") or 0) < 60:
        return
    try:
        sent = notifier.notify_event(
            "app_update",
            f"⚠️ <b>静默更新未完成（{notifier.escape_html(str(st.get('target_tag') or '?'))}）</b>\n"
            f"<code>{notifier.escape_html(detail[:500])}</code>",
            reply_markup=_faillog_buttons(),
        )
        save_state(silent_error_notified=key if sent is not False else None,
                   silent_error_attempt_at=now)
    except Exception as exc:
        print(f"[updater] silent failure notification failed: {type(exc).__name__}")
        try:
            save_state(silent_error_attempt_at=now)
        except Exception:
            pass


def _watch_silent_dispatch(reservation, st: dict) -> None:
    from . import drain
    mode = st.get("mode")
    identity_key = "result_id" if mode == MODE_DOCKER else "supervisor_id"
    identity = st.get(identity_key)
    if not identity:
        return
    path = (Path(config.DATA_DIR) / (".update_result." + identity) if mode == MODE_DOCKER
            else Path(st["supervisor_result"]))
    while not _stopping.wait(0.2):
        current = load_state()
        if current.get(identity_key) != identity or current.get("stage") != STAGE_RESTARTING:
            return
        try:
            if mode == MODE_DOCKER:
                result = path.read_text().strip()
            else:
                value = json.loads(path.read_text())
                result = value.get("result") if value.get("update_id") == identity else None
        except (OSError, ValueError):
            continue
        if result and result != "OK" and _op_lock.acquire(blocking=False):
            try:
                current = load_state()
                if _stopping.is_set() or current.get(identity_key) != identity:
                    return
                save_state(stage=STAGE_FAILED, message="独立更新监护未能安全启动更新，请查看日志")
                drain.release_update(reservation)
                notify_silent_failure("独立更新监护未能安全启动更新，请查看日志")
            finally:
                _op_lock.release()
            return


def _faillog_buttons() -> dict:
    """失败/回滚通知用的 inline 按钮：查看失败日志 + 进入版本更新菜单。"""
    return {
        "inline_keyboard": [
            [{"text": "📋 查看失败日志", "callback_data": "upd:faillog"}],
            [{"text": "🆕 版本更新", "callback_data": "menu:update"}],
        ]
    }


def _notify_cross_process(text: str, chat_id, notify_msg_id, reply_markup=None) -> None:
    """跨进程通知：重启后新进程没有内存里的 TG 进度回调，这里直接用 TG ui
    edit 之前记录的通知消息；edit 失败（如消息太旧）则用 notifier 发新消息兜底。
    reply_markup：可选 inline 键盘（失败时带「查看失败日志」按钮）。
    """
    edited = False
    if chat_id and notify_msg_id:
        try:
            from .telegram import ui as _ui
            result = _ui.edit(int(chat_id), int(notify_msg_id), _ui.truncate(text), reply_markup=reply_markup)
            edited = bool(isinstance(result, dict) and result.get("ok"))
            if not edited:
                desc = ""
                if isinstance(result, dict):
                    desc = str(result.get("description") or result)[:200]
                print(f"[updater] cross-process edit not ok: {desc or result!r}")
        except Exception as exc:
            print(f"[updater] cross-process edit failed: {exc}")
    if not edited:
        try:
            notifier.notify_event("app_update", text, reply_markup=reply_markup)
        except Exception as exc:
            print(f"[updater] cross-process notify failed: {exc}")


def _update_log_path() -> str:
    return os.path.join(config.DATA_DIR, ".update_log")


def append_update_log(text: str) -> None:
    """追加一行到 .update_log（systemd/bare 路径与编排层用；docker sidecar 自己写）。"""
    try:
        ts = time.strftime("%H:%M:%S", time.gmtime())
        with open(_update_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {text}\n")
    except Exception:
        pass


def reset_update_log() -> None:
    """开始一次新更新时清空日志。"""
    try:
        with open(_update_log_path(), "w", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S', time.gmtime())}] === 更新日志开始 ===\n")
    except Exception:
        pass


def get_update_log(max_chars: int = 3500) -> str:
    """读 .update_log（供 TG「查看失败日志」）。返回末尾 max_chars 字符。"""
    try:
        with open(_update_log_path(), "r", encoding="utf-8") as f:
            data = f.read()
        if not data.strip():
            return ""
        if len(data) > max_chars:
            data = "…(前略)\n" + data[-max_chars:]
        return data
    except FileNotFoundError:
        return ""
    except Exception as exc:
        return f"(读取日志失败: {exc})"


def _read_update_result(wait_seconds: int = 60) -> str:
    """读 sidecar 写的 .update_result 标记（OK / ROLLBACK）。最多等 wait_seconds。

    sidecar 在 composeDir/data 下写该文件 = 容器内的 /app/data/.update_result。
    """
    result_id = load_state().get("result_id")
    suffix = "." + str(result_id) if result_id else ""
    path = os.path.join(config.DATA_DIR, ".update_result" + suffix)
    deadline = time.time() + max(10, wait_seconds)
    while time.time() < deadline and not _stopping.is_set():
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass
        _stopping.wait(0.2)
    return ""


def resume_after_restart() -> None:
    """新进程启动时调用：若处于 restarting 态，做健康检查 + 判定成功/回滚。

    在后台线程跑，避免阻塞 lifespan。
    """
    try:
        _ensure_schema()
    except Exception:
        return
    st = load_state()
    if st.get("stage") not in (STAGE_RESTARTING, STAGE_VERIFYING) or _stopping.is_set():
        return
    if not _op_lock.acquire(blocking=False):
        return

    def _worker():
        save_state(stage=STAGE_VERIFYING, message="重启完成，健康检查中")
        target = st.get("target_tag") or st.get("to_version") or "?"
        timeout = _cfg()["healthTimeoutSeconds"]
        mode = st.get("mode") or _detect_mode()
        now_ver = __version__

        if mode != MODE_DOCKER and st.get("supervisor_id"):
            deadline = time.monotonic() + timeout * 2 + _cfg()["gracefulStopSeconds"] * 2 + 30
            result = ""
            while time.monotonic() < deadline and not _stopping.is_set():
                try:
                    value = json.loads(Path(st["supervisor_result"]).read_text())
                    if value.get("update_id") == st["supervisor_id"]:
                        result = value.get("result", "")
                        break
                except (OSError, ValueError):
                    pass
                _stopping.wait(0.2)
            if _stopping.is_set(): return  # preserve verifying for the next process
            if result == "OK" and now_ver.lstrip("vV") == target.lstrip("vV"):
                stage = STAGE_SUCCESS
                if st.get("silent"):
                    from_disp = (st.get("from_version") or "?").lstrip("vV")
                    message = (
                        f"✅ <b>静默自动更新完成</b>\n"
                        f"已趁空闲时段自动完成 <code>v{notifier.escape_html(from_disp)}</code> → "
                        f"<code>v{notifier.escape_html(now_ver)}</code> 更新并通过健康检查。"
                    )
                else:
                    message = f"更新成功 → v{now_ver}（独立监护已验证）"
            elif result == "ROLLBACK" and now_ver.lstrip("vV") == str(st.get("from_version", "")).lstrip("vV"):
                stage, message = STAGE_ROLLED_BACK, "独立监护已回滚源码/依赖并验证旧版本健康"
            else:
                stage, message = STAGE_FAILED, "独立监护未确认目标版本或回滚健康，请查看更新日志"
            save_state(stage=stage, to_version=now_ver,
                       message=f"更新成功 → v{now_ver}" if stage == STAGE_SUCCESS else message)
            if stage == STAGE_SUCCESS:
                _prune_backups_locked()
            _emit(stage, message)
            _notify_cross_process(
                message, st.get("chat_id"), st.get("notify_msg_id"),
                reply_markup=None if stage == STAGE_SUCCESS else _faillog_buttons(),
            )
            return

        # docker 形态：sidecar 已经做过健康门控 + 回滚，这里优先读它写的结果标记。
        # 若标记为 ROLLBACK，说明新容器起不来、sidecar 已回滚（当前进程其实是旧版）。
        # 等待窗口要覆盖 sidecar 的健康门控(≤180s) + 回滚重建(~30s)，故放宽到 timeout*2+60。
        if mode == MODE_DOCKER:
            result = _read_update_result(wait_seconds=timeout * 2 + 60)
            if _stopping.is_set(): return
            if result == "ROLLBACK":
                save_state(stage=STAGE_ROLLED_BACK,
                           message="新版本健康检查失败，sidecar 已自动回滚")
                rb_msg = (f"❌ <b>更新失败，已自动回滚</b>\n"
                          f"目标 {target} 启动后健康检查未通过，已回滚到旧版本 "
                          f"<code>v{now_ver}</code>。请点下方查看失败日志。")
                _emit(STAGE_ROLLED_BACK, rb_msg)
                _notify_cross_process(rb_msg, st.get("chat_id"), st.get("notify_msg_id"),
                                      reply_markup=_faillog_buttons())
                return
            if result == "ROLLBACK_FAILED":
                # 极端：回滚后旧版本也没起来，需人工介入
                save_state(stage=STAGE_FAILED,
                           message="更新失败且回滚后健康检查未通过，需人工介入")
                rb_msg = (f"🆘 <b>更新失败，自动回滚也未恢复</b>\n"
                          f"目标 {target} 失败，回滚后健康检查仍未通过。\n"
                          f"<b>请尽快人工检查</b>（查看失败日志定位）。")
                _emit(STAGE_FAILED, rb_msg)
                _notify_cross_process(rb_msg, st.get("chat_id"), st.get("notify_msg_id"),
                                      reply_markup=_faillog_buttons())
                return
            # OK 或无标记 → 落到下面的常规健康确认

        ok, detail = wait_healthy(timeout)
        if _stopping.is_set(): return
        # 版本对比兜底：健康通过，但若当前版本仍是"更新前版本"（=回滚发生），
        # 说明 sidecar 已回滚（哪怕标记文件丢了），按回滚汇报而非误报成功。
        from_ver = (st.get("from_version") or "").lstrip("vV")
        target_norm = (target or "").lstrip("vV")
        rolled_back_by_version = (
            mode == MODE_DOCKER and ok
            and target_norm and now_ver.lstrip("vV") == from_ver
            and target_norm != from_ver
        )
        if ok and now_ver.lstrip("vV") != target_norm and not rolled_back_by_version:
            ok, detail = False, f"wrong version: expected {target}, observed {now_ver}"
        if ok and not rolled_back_by_version:
            save_state(stage=STAGE_SUCCESS, to_version=now_ver,
                       message=f"更新成功 → v{now_ver}")
            _prune_backups_locked()
            keep_n = _cfg()["keepBackups"]
            tail_hint = (
                "<i>已按保留策略清理旧备份，如需管理可在「🗂 备份列表」查看。</i>"
                if keep_n > 0
                else "<i>已按设置自动清理本次升级备份。</i>"
            )
            if st.get("silent"):
                from_disp = from_ver or "?"
                msg = (
                    f"✅ <b>静默自动更新完成</b>\n"
                    f"已趁空闲时段自动完成 <code>v{notifier.escape_html(from_disp)}</code> → "
                    f"<code>v{notifier.escape_html(now_ver)}</code> 更新并通过健康检查。\n{tail_hint}"
                )
            else:
                msg = f"✅ <b>更新成功</b>，当前已运行 <code>v{now_ver}</code>（目标 {target}）\n{tail_hint}"
            _emit(STAGE_SUCCESS, msg)
            _notify_cross_process(msg, st.get("chat_id"), st.get("notify_msg_id"))
        elif rolled_back_by_version:
            save_state(stage=STAGE_ROLLED_BACK,
                       message=f"新版本未生效（仍为 v{now_ver}），判定已回滚")
            rb_msg = (f"❌ <b>更新失败，已自动回滚</b>\n"
                      f"目标 {target} 未能生效，当前仍为旧版本 <code>v{now_ver}</code>"
                      f"（健康正常）。请检查日志后重试。")
            _emit(STAGE_ROLLED_BACK, rb_msg)
            _notify_cross_process(rb_msg, st.get("chat_id"), st.get("notify_msg_id"),
                                  reply_markup=_faillog_buttons())
        else:
            # 健康检查失败 → 回滚（systemd/bare 路径；docker 已在 sidecar 处理）
            append_update_log(f"❌ 健康检查失败：{detail}，开始回滚")
            _emit(STAGE_FAILED, f"❌ 健康检查失败（{detail}），开始回滚 …")
            backup_ref = st.get("backup_ref") or ""
            if mode == MODE_DOCKER:
                rb_ok, rb_detail = _docker_rollback(backup_ref)
            else:
                rb_ok, rb_detail = _src_rollback(backup_ref)
                if rb_ok:
                    rb_ok, restart_detail = _src_restart()
                    rb_detail += f"; {restart_detail}"
            append_update_log(f"回滚结果：{rb_detail}")
            final_stage = STAGE_ROLLED_BACK if rb_ok else STAGE_FAILED
            outcome = "已回滚源码/已触发容器回滚" if rb_ok else "回滚失败，需人工介入"
            save_state(stage=final_stage,
                       message=f"健康检查失败，{outcome}: {rb_detail}")
            rb_msg = (f"❌ <b>更新失败，{outcome}</b>\n健康检查未通过：{detail}\n"
                      f"回滚结果：{rb_detail}\n请检查日志后重试。")
            _emit(final_stage, rb_msg)
            _notify_cross_process(rb_msg, st.get("chat_id"), st.get("notify_msg_id"),
                                  reply_markup=_faillog_buttons())

    def _owned_worker():
        try:
            _worker()
        except Exception as exc:
            if not _stopping.is_set():
                save_state(stage=STAGE_FAILED, message=f"更新校验失败: {type(exc).__name__}")
        finally:
            _op_lock.release()
    global _resume_thread
    try:
        _resume_thread = threading.Thread(target=_owned_worker, daemon=True, name="updater-resume")
        _resume_thread.start()
    except BaseException:
        _op_lock.release()
        raise


# ─── 备份清理与管理 ──────────────────────────────────────────────

_PROTECTED_BACKUP_STAGES = frozenset({
    STAGE_BACKING_UP, STAGE_PULLING, STAGE_STAGED, STAGE_RESTARTING, STAGE_VERIFYING,
})


def _protected_backup_ref() -> str | None:
    st = load_state()
    if st.get("stage") in _PROTECTED_BACKUP_STAGES:
        ref = str(st.get("backup_ref") or "").strip()
        return ref or None
    return None


def _parse_stamp_ts(stamp: str) -> float | None:
    try:
        return float(time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S")))
    except (TypeError, ValueError):
        return None


def _docker_list_backup_tags() -> list[tuple[str, str, int | None]]:
    """列出宿主机/Engine 中现有的 parrot-backup:* 标签（仅在 Docker 形态下探测）。"""
    if _detect_mode() != MODE_DOCKER:
        return []
    if _has_local_docker():
        rc, out = _run(
            ["docker", "image", "ls", "parrot-backup", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}"],
            timeout=15,
        )
        if rc != 0:
            return []
        rows: list[tuple[str, str, int | None]] = []
        for line in out.splitlines():
            parts = line.strip().split("\t")
            tag = parts[0] if parts else ""
            img_id = parts[1] if len(parts) > 1 else ""
            if tag.startswith("parrot-backup:") and not tag.endswith(":<none>"):
                rows.append((tag, img_id, None))
        return rows
    return _engine_list_backup_tags()


def _docker_remove_backup_tag(backup_tag: str) -> bool:
    """安全移除 parrot-backup:<label> 标签（不强制删正在运行的容器或其它标签）。"""
    if not isinstance(backup_tag, str) or not backup_tag.startswith("parrot-backup:"):
        return False
    if _has_local_docker():
        rc, out = _run(["docker", "image", "rm", backup_tag], timeout=30)
        if rc == 0 or "No such image" in (out or ""):
            return True
        print(f"[updater] docker image rm failed ({backup_tag}): {out[-200:]}")
        return False
    if os.path.exists(DOCKER_SOCK):
        return _engine_remove_backup_tag(backup_tag)
    return False


def _backup_owner(*, create: bool = False) -> str:
    owner = str(load_state().get("backup_owner") or "")
    if not owner and create:
        owner = uuid.uuid4().hex
        save_state(backup_owner=owner)
    return owner


def _orphan_entry_from_tag(backup_tag: str, img_id: str, created_ts: int | None) -> dict:
    label = backup_tag.split(":", 1)[1]
    owner = _backup_owner()
    owned = bool(owner and label.startswith(owner + "-"))
    version_label = label[len(owner) + 1:] if owned else label
    parts = version_label.rsplit("-", 2)
    if len(parts) == 3 and len(parts[1]) == 8 and len(parts[2]) == 6 and parts[1].isdigit() and parts[2].isdigit():
        version, ts = parts[0], f"{parts[1]}-{parts[2]}"
    else:
        version, ts = version_label, time.strftime("%Y%m%d-%H%M%S", time.gmtime(created_ts)) if created_ts else ""
    parsed_ts = _parse_stamp_ts(ts)
    created = parsed_ts if parsed_ts is not None else float(created_ts or 0)
    return {
        "ref": f"docker-{label}",
        "image": _cfg()["image"],
        "digest": img_id,
        "backup_tag": backup_tag,
        "version": version,
        "target_tag": "",
        "mode": "docker",
        "ts": ts,
        "created_at": created,
        "orphan": True,
        "unowned": not owned,
    }


def _backup_entries() -> list[dict]:
    root = Path(_backup_root())
    entries: list[tuple[float, dict]] = []
    known_tags: set[str] = set()
    known_refs: set[str] = set()
    for path in root.glob("*.json"):
        try:
            meta = json.loads(path.read_text())
            if not isinstance(meta, dict) or meta.get("ref") != path.stem:
                continue  # monitor plans/results are not backup metadata
            try:
                created = float(meta["created_at"])
            except (KeyError, ValueError, TypeError):
                stamp = meta.get("ts") or "-".join(path.stem.rsplit("-", 2)[-2:])
                parsed = _parse_stamp_ts(stamp)
                created = parsed if parsed is not None else path.stat().st_mtime
            entries.append((created, meta))
            known_refs.add(meta["ref"])
            if isinstance(meta.get("backup_tag"), str) and meta["backup_tag"]:
                known_tags.add(meta["backup_tag"])
        except (OSError, ValueError, TypeError):
            continue
    for tag, img_id, created_ts in _docker_list_backup_tags():
        if tag in known_tags:
            continue
        orphan = _orphan_entry_from_tag(tag, img_id, created_ts)
        if orphan["ref"] not in known_refs:
            entries.append((float(orphan.get("created_at") or 0), orphan))
            known_refs.add(orphan["ref"])
    return [meta for _, meta in sorted(entries, key=lambda item: (item[0], str(item[1].get("ref") or "")), reverse=True)]


def _remove_backup_entry(meta: dict) -> bool:
    ref = str(meta.get("ref") or "").strip()
    if not ref or "/" in ref or "\\" in ref or ".." in ref or meta.get("unowned"):
        return False
    backup_tag = meta.get("backup_tag")
    if not backup_tag and meta.get("mode") == "docker" and ref.startswith("docker-"):
        backup_tag = f"parrot-backup:{ref.removeprefix('docker-')}"
    if isinstance(backup_tag, str) and backup_tag.startswith("parrot-backup:"):
        if not _docker_remove_backup_tag(backup_tag):
            return False
    root = Path(_backup_root())
    try:
        for suffix in (".tar.gz", ".state.db", ".monitor.py", ".plan.json", ".result.json"):
            (root / (ref + suffix)).unlink(missing_ok=True)
        deps = root / (ref + ".deps")
        if deps.is_dir():
            shutil.rmtree(deps)
        (root / (ref + ".json")).unlink(missing_ok=True)
    except OSError as exc:
        print(f"[updater] remove backup files failed ({ref}): {exc}")
        return False
    return True


def _prune_backups() -> None:
    if not _op_lock.acquire(blocking=False):
        return
    try:
        _prune_backups_locked()
    finally:
        _op_lock.release()


def _prune_backups_locked() -> None:
    """Caller owns the update lock through listing, protection checks and removal."""
    keep = max(0, _cfg()["keepBackups"])
    protected = _protected_backup_ref()
    kept = 0
    for meta in _backup_entries():
        ref = meta.get("ref")
        if meta.get("unowned") or (protected and ref == protected):
            continue
        if kept < keep:
            kept += 1
            continue
        _remove_backup_entry(meta)


def delete_backup(ref: str) -> str:
    """删除指定备份。返回 'deleted' | 'protected' | 'not_found' | 'failed'。"""
    target = str(ref or "").strip()
    if not target or "/" in target or "\\" in target or ".." in target:
        return "not_found"
    if not _op_lock.acquire(blocking=False):
        return "busy"
    try:
        protected = _protected_backup_ref()
        if protected and target == protected:
            return "protected"
        entry = next((item for item in _backup_entries() if item.get("ref") == target), None)
        if entry is None:
            return "not_found"
        if entry.get("unowned"):
            return "protected"
        return "deleted" if _remove_backup_entry(entry) else "failed"
    finally:
        _op_lock.release()


def clear_backups() -> dict:
    """清空除当前活跃更新保护锚点外的全部备份。"""
    result = {"deleted": 0, "keptProtected": False, "keptUnowned": 0, "failed": 0}
    if not _op_lock.acquire(blocking=False):
        return {**result, "busy": True}
    try:
        protected = _protected_backup_ref()
        for entry in _backup_entries():
            if entry.get("unowned"):
                result["keptUnowned"] += 1
                continue
            if protected and entry.get("ref") == protected:
                result["keptProtected"] = True
                continue
            if _remove_backup_entry(entry):
                result["deleted"] += 1
            else:
                result["failed"] += 1
        return result
    finally:
        _op_lock.release()


def list_backups() -> list[dict]:
    protected = _protected_backup_ref()
    result = []
    for item in _backup_entries():
        row = dict(item)
        row["protected"] = bool(row.get("unowned") or (protected and row.get("ref") == protected))
        result.append(row)
    return result
