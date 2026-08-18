"""Parrot 自更新执行器（源码 systemd / Docker 双形态）。

与 update_checker 的分工：
- update_checker：只负责"发现新版"（拉 GitHub Release、比对版本、banner、推送通知）。
- updater（本模块）：负责"执行更新"——备份 → 拉取 → 等用户二次确认 → 重启生效 → 健康校验/回滚。

核心设计：持久化状态机（state.db 单行 `app_self_update`），跨重启不丢。
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
- 重启前 state.db 必须通过严格 checkpoint、在线备份和完整性验证；失败则拒绝重启。
- 重启后健康检查失败 → 自动回滚到备份。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from . import __version__, config, network, notifier, state_db


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
    uc = config.get().get("updateChecker") or {}
    return {
        "autoUpdate": bool(uc.get("autoUpdate", False)),
        "repo": str(uc.get("repo") or "danger-dream/Parrot").strip(),
        "serviceName": str(uc.get("serviceName") or "parrot.service").strip(),
        "composeDir": str(uc.get("composeDir") or "").strip(),
        "composeService": str(uc.get("composeService") or "parrot").strip(),
        "containerName": str(uc.get("containerName") or "parrot").strip(),
        "image": str(uc.get("image") or "ghcr.io/danger-dream/parrot:latest").strip(),
        "keepBackups": int(uc.get("keepBackups", 5) or 5),
        "healthTimeoutSeconds": int(uc.get("healthTimeoutSeconds", 90) or 90),
        "updaterImage": str(uc.get("updaterImage") or "docker:cli").strip(),
    }


# ─── 状态机持久化（state.db 单行）───────────────────────────────

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
    sql = """
    CREATE TABLE IF NOT EXISTS app_self_update (
      id              INTEGER PRIMARY KEY CHECK (id=1),
      stage           TEXT,
      mode            TEXT,
      from_version    TEXT,
      to_version      TEXT,
      target_tag      TEXT,
      backup_ref      TEXT,
      message         TEXT,
      chat_id         INTEGER,
      notify_msg_id   INTEGER,
      updated_at      INTEGER
    );
    """
    conn = state_db._get_conn()
    with state_db._write_lock:
        conn.executescript(sql)
        conn.commit()


def load_state() -> dict:
    try:
        _ensure_schema()
    except Exception:
        return {"stage": STAGE_IDLE}
    conn = state_db._get_conn()
    row = conn.execute(
        "SELECT stage, mode, from_version, to_version, target_tag, backup_ref, "
        "message, chat_id, notify_msg_id, updated_at FROM app_self_update WHERE id=1"
    ).fetchone()
    if not row:
        return {"stage": STAGE_IDLE}
    return {
        "stage": row[0] or STAGE_IDLE,
        "mode": row[1],
        "from_version": row[2],
        "to_version": row[3],
        "target_tag": row[4],
        "backup_ref": row[5],
        "message": row[6],
        "chat_id": row[7],
        "notify_msg_id": row[8],
        "updated_at": row[9],
    }


def save_state(**fields) -> None:
    _ensure_schema()
    cur = load_state()
    cur.update(fields)
    conn = state_db._get_conn()
    with state_db._write_lock:
        conn.execute(
            "INSERT OR REPLACE INTO app_self_update("
            "id, stage, mode, from_version, to_version, target_tag, backup_ref, "
            "message, chat_id, notify_msg_id, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cur.get("stage"), cur.get("mode"), cur.get("from_version"),
                cur.get("to_version"), cur.get("target_tag"), cur.get("backup_ref"),
                cur.get("message"), cur.get("chat_id"), cur.get("notify_msg_id"),
                int(time.time()),
            ),
        )
        conn.commit()


def reset_state() -> None:
    save_state(
        stage=STAGE_IDLE, mode=None, from_version=None, to_version=None,
        target_tag=None, backup_ref=None, message=None,
        chat_id=None, notify_msg_id=None,
    )


# ─── 更新锁（防并发）─────────────────────────────────────────────

_op_lock = threading.Lock()


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
    try:
        p = subprocess.run(
            cmd, cwd=cwd, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        return p.returncode, (p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except Exception as exc:
        return 1, f"exec error: {exc}"


# ─── 健康检查 ────────────────────────────────────────────────────

def _health_url() -> str:
    cfg = config.get()
    port = (cfg.get("listen") or {}).get("port", 18082)
    return f"http://127.0.0.1:{port}/health"


def wait_healthy(timeout: int = 90) -> tuple[bool, str]:
    """轮询 /health 直到 200 或超时。返回 (ok, detail)。"""
    url = _health_url()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            resp = network.get_sync(url, timeout=5)
            if resp.status_code == 200:
                return True, "health 200"
            last = f"http {resp.status_code}"
        except Exception as exc:
            last = type(exc).__name__
        time.sleep(3)
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


def _src_backup(target_tag: str) -> tuple[bool, str, str]:
    """源码备份：tar 打包当前源码 + 记录 commit。返回 (ok, backup_ref, detail)。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    ref = f"src-{__version__}-{ts}"
    dest = os.path.join(_backup_root(), ref + ".tar.gz")
    app = _app_dir()
    # 只打包源码，排除运行时数据（data/venv/.git/__pycache__/*.db）
    cmd = [
        "tar", "czf", dest,
        "--exclude=./data", "--exclude=./venv", "--exclude=./.git",
        "--exclude=./__pycache__", "--exclude=*.db", "--exclude=*.db-wal",
        "--exclude=*.db-shm", "--exclude=./logs", "--exclude=./backups",
        "-C", app, ".",
    ]
    rc, out = _run(cmd, timeout=180)
    if rc != 0:
        return False, "", f"tar failed: {out[:300]}"
    commit = _src_current_commit()
    meta = {"ref": ref, "tar": dest, "commit": commit, "version": __version__,
            "target_tag": target_tag, "mode": "src", "ts": ts}
    with open(os.path.join(_backup_root(), ref + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return True, ref, f"backup ok ({dest})"


def _src_pull(target_tag: str, prev_commit: str = "") -> tuple[bool, str]:
    """git fetch + checkout 到目标 tag。不重启。返回 (ok, detail)。

    prev_commit：更新前的 commit（来自备份 meta），用于稳健地判断 requirements.txt
    是否变化（替代脆弱的 HEAD@{1} reflog 依赖）。
    """
    rc, out = _git(["fetch", "--all", "--tags", "--prune"], timeout=180)
    if rc != 0:
        return False, f"git fetch failed: {out[:300]}"
    # 优先按 tag checkout；tag 不存在则尝试 origin/<tag>
    rc, out = _git(["checkout", "-f", target_tag], timeout=120)
    if rc != 0:
        rc2, out2 = _git(["reset", "--hard", f"origin/{target_tag}"], timeout=120)
        if rc2 != 0:
            return False, f"checkout {target_tag} failed: {out[:200]} / {out2[:200]}"
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
        pip = os.path.join(_app_dir(), "venv", "bin", "pip")
        if os.path.exists(pip):
            rc3, out3 = _run([pip, "install", "-r",
                              os.path.join(_app_dir(), "requirements.txt")], timeout=600)
            if rc3 != 0:
                return False, f"pip install failed: {out3[-300:]}"
    return True, f"checked out {target_tag}"


def _src_restart() -> tuple[bool, str]:
    """触发重启。systemd 用 systemctl（detached，不在进程树里）；bare 用 exec 自替换。"""
    mode = _detect_mode()
    if mode == MODE_SYSTEMD:
        svc = _cfg()["serviceName"]
        # detached：systemctl restart 会 SIGTERM 当前进程，但命令本身由 systemd 执行，
        # 即使当前进程被杀，重启动作照常完成。用 Popen 不等待。
        try:
            # 延迟一拍再触发，确保 state.db checkpoint 落盘 + 当前 TG 响应发出
            def _delayed_systemctl():
                time.sleep(1.5)
                subprocess.Popen(
                    ["systemctl", "restart", svc],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            threading.Thread(target=_delayed_systemctl, daemon=True).start()
            return True, f"systemctl restart {svc} dispatched"
        except Exception as exc:
            return False, f"systemctl restart failed: {exc}"
    else:
        # bare：延迟 exec 自替换，给 HTTP 响应留出返回时间
        def _delayed_exec():
            time.sleep(2)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_delayed_exec, daemon=True).start()
        return True, "self-exec restart scheduled"


def _src_rollback(backup_ref: str) -> tuple[bool, str]:
    """源码回滚：从备份 tar 还原源码 + checkout 回原 commit。"""
    if not backup_ref:
        return False, "no backup ref"
    meta_path = os.path.join(_backup_root(), backup_ref + ".json")
    if not os.path.exists(meta_path):
        return False, f"backup meta missing: {backup_ref}"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    commit = meta.get("commit") or ""
    if commit and _src_is_git_repo():
        rc, out = _git(["reset", "--hard", commit], timeout=120)
        if rc == 0:
            return True, f"rolled back to {commit[:8]}"
    # git 还原失败 → 从 tar 还原
    tar = meta.get("tar") or ""
    if tar and os.path.exists(tar):
        rc, out = _run(["tar", "xzf", tar, "-C", _app_dir()], timeout=180)
        if rc == 0:
            return True, "rolled back from tar"
        return False, f"tar restore failed: {out[:200]}"
    return False, "rollback failed: no usable backup"


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
    1) 名字冲突：发起更新时旧容器仍在跑，compose up 撞名 → 先 `docker rm -f` 旧容器
       （秒级停机，单实例自更新的固有代价）。
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
    tries = max(10, min(60, int(cfg.get("healthTimeoutSeconds", 90) or 90) // 3))
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
    #   ③ stop+rm 旧容器 → compose up 起新容器（compose 靠 service label 管理，
    #      不能用 rename 留存——rename 后 compose 仍能通过 label 找回它并误重建）。
    #   ④ 健康门控。
    #   ⑤ 失败：把镜像 tag 指回备份 digest，compose up 重建旧版本，并**再次健康验证**，
    #      确保回滚后服务真的可用；只有回滚后健康才写 ROLLBACK（成功回滚），
    #      否则写 ROLLBACK_FAILED（回滚也没起来，极端情况，需人工）。
    script = f"""set +e
LOG={log_q}
FLAG_DIR={flag_dir_q}
SVC={svc_q}
NAME={name_q}
IMAGE={image_q}
BACKUP_DIGEST={backup_digest_q}
: > "$LOG" 2>/dev/null || true
logln() {{ echo "[$(date -u +%H:%M:%S)] $*" >> "$LOG" 2>/dev/null || true; }}
health_ok() {{ docker exec "$NAME" curl -fsS http://127.0.0.1:22122/health >/dev/null 2>&1; }}
wait_health() {{
  ok=0
  for i in $(seq 1 {tries}); do
    sleep 3
    if health_ok; then ok=1; break; fi
  done
  return $([ "$ok" = "1" ] && echo 0 || echo 1)
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
  echo "ROLLBACK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
  if [ -n "$BACKUP_DIGEST" ]; then
    docker tag "$BACKUP_DIGEST" "$IMAGE" 2>/dev/null && logln "已把镜像 tag 指回备份 digest" || logln "⚠️ 回滚 tag 失败"
  else
    logln "⚠️ 无备份 digest，无法回滚镜像"
  fi
  docker rm -f "$NAME" 2>/dev/null || true
  RB_OUT="$(docker compose up -d --force-recreate "$SVC" 2>&1)"
  echo "$RB_OUT" | tail -10 >> "$LOG" 2>/dev/null || true
  logln "回滚重建完成，验证健康…"
  if wait_health; then
    logln "✅ 回滚成功，旧版本已恢复并健康"
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
# ③ stop+rm 旧容器（镜像 digest 已备份，可回滚），compose up 新容器
docker rm -f "$NAME" 2>/dev/null || true
logln "启动新容器…"
UP_OUT="$(docker compose up -d --force-recreate "$SVC" 2>&1)"
UP_RC=$?
echo "$UP_OUT" | tail -20 >> "$LOG" 2>/dev/null || true
if [ $UP_RC -ne 0 ]; then logln "❌ compose up 失败（rc=$UP_RC）"; fail_rollback; fi
logln "新容器已启动，等待健康检查（最多 {tries*3}s）…"
# ④ 健康门控
if ! wait_health; then
  logln "❌ 健康检查未通过（{tries*3}s 内 /health 未就绪）"
  logln "新容器最近日志："
  docker logs --tail 30 "$NAME" >> "$LOG" 2>&1 || true
  fail_rollback
fi
# ⑤ 成功
logln "✅ 更新成功，健康检查通过"
echo "OK" > "$FLAG_DIR/.update_result" 2>/dev/null || true
exit 0
"""
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
    ref = f"docker-{__version__}-{ts}"
    backup_repo = "parrot-backup"
    backup_label = f"{__version__}-{ts}"
    backup_tag = f"{backup_repo}:{backup_label}"
    if _has_local_docker():
        _run(["docker", "tag", digest, backup_tag], timeout=30)
    else:
        _engine_tag_image(digest, backup_repo, backup_label)
    meta = {"ref": ref, "image": image, "digest": digest,
            "backup_tag": backup_tag, "version": __version__,
            "target_tag": target_tag, "mode": "docker", "ts": ts}
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
    inner = _compose_up_inner(backup_digest=backup_digest)
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

# ─── 高层编排（TG 按钮调用入口）─────────────────────────────────
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


def _emit(stage: str, text: str) -> None:
    if _progress_cb:
        try:
            _progress_cb(stage, text)
        except Exception as exc:
            print(f"[updater] progress cb failed: {exc}")
    print(f"[updater:{stage}] {text}")


def stage_update(target_tag: str, *, chat_id: Optional[int] = None,
                 notify_msg_id: Optional[int] = None) -> tuple[bool, str]:
    """第一阶段：备份 → 拉取 → staged。线程安全，幂等拒绝并发。"""
    if not _op_lock.acquire(blocking=False):
        return False, "另一个更新操作正在进行中"
    try:
        if is_busy():
            return False, "已有更新在进行中（staged/restarting）"
        mode = _detect_mode()
        reset_update_log()
        append_update_log(f"开始更新：{__version__} → {target_tag}（形态 {mode}）")
        save_state(stage=STAGE_BACKING_UP, mode=mode, from_version=__version__,
                   target_tag=target_tag, to_version=target_tag,
                   chat_id=chat_id, notify_msg_id=notify_msg_id, message="开始备份")
        _emit(STAGE_BACKING_UP, f"📦 正在备份当前版本 v{__version__} …")

        # 源码形态：先挡掉未提交改动
        if mode in (MODE_SYSTEMD, MODE_BARE) and _src_is_git_repo():
            if _src_has_local_changes():
                save_state(stage=STAGE_FAILED, message="工作树有未提交改动，已拒绝更新")
                _emit(STAGE_FAILED, "❌ 源码目录有未提交改动，已拒绝自动更新（避免覆盖本地改动）")
                return False, "工作树有未提交改动"

        # ① 备份
        if mode == MODE_DOCKER:
            ok, ref, detail = _docker_backup(target_tag)
        else:
            ok, ref, detail = _src_backup(target_tag)
        if not ok:
            append_update_log(f"❌ 备份失败：{detail}")
            save_state(stage=STAGE_FAILED, message=f"备份失败: {detail}")
            _emit(STAGE_FAILED, f"❌ 备份失败：{detail}")
            return False, detail
        append_update_log(f"✅ 备份完成：{detail}")
        save_state(stage=STAGE_PULLING, backup_ref=ref, message=f"备份完成: {detail}")
        _emit(STAGE_PULLING, f"✅ 备份完成 → ⬇️ 正在拉取 {target_tag} …")

        # ② 拉取
        if mode == MODE_DOCKER:
            ok, detail = _docker_pull(target_tag)
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
            append_update_log(f"❌ 拉取失败：{detail}")
            save_state(stage=STAGE_FAILED, message=f"拉取失败: {detail}")
            _emit(STAGE_FAILED, f"❌ 拉取失败：{detail}")
            return False, detail
        append_update_log(f"✅ 拉取完成：{detail}")

        # ③ 进入 staged（停下，等用户二次确认）
        _prune_backups()
        save_state(stage=STAGE_STAGED, message=f"已拉取 {target_tag}，等待确认重启")
        _emit(STAGE_STAGED,
              f"✅ 已备份并拉取 <b>{target_tag}</b>，<b>尚未重启</b>。\n"
              f"请二次确认是否立即重启生效。")
        return True, "staged"
    finally:
        _op_lock.release()


def _state_db_restart_backup_path(st: dict) -> str:
    """Return a traversal-safe backup path tied to the staged release backup."""
    raw_ref = str(st.get("backup_ref") or "").strip()
    safe_ref = "".join(ch for ch in raw_ref if ch.isalnum() or ch in "._-")[:160]
    if not safe_ref:
        safe_ref = f"update-{time.strftime('%Y%m%d-%H%M%S')}"
    return os.path.join(_backup_root(), safe_ref + ".state.db")


def _prepare_state_db_restart(st: dict) -> tuple[str, tuple[int, int, int]]:
    """Checkpoint, online-backup and verify state.db before any restart."""
    checkpoint_result = state_db.checkpoint(mode="FULL", strict=True)
    destination = _state_db_restart_backup_path(st)
    state_db.online_backup(destination, verify=True)
    return destination, checkpoint_result


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
        "❌ state.db 检查点、在线备份或完整性验证失败，已阻止重启。\n"
        f"详情：{detail}",
    )
    return False, public_detail


def confirm_restart() -> tuple[bool, str]:
    """第二阶段：数据库保护成功后才允许重启并由新进程 resume。

    用 _op_lock 防止「连点两次确认」触发两次备份或重启（TOCTOU）。
    """
    if not _op_lock.acquire(blocking=False):
        return False, "另一个更新操作正在进行中"
    try:
        st = load_state()
        if st.get("stage") != STAGE_STAGED:
            return False, f"当前不在 staged 态（{st.get('stage')}），无法确认重启"
        mode = st.get("mode") or _detect_mode()

        # Fail closed：严格 FULL checkpoint → SQLite Online Backup → backup
        # integrity_check。任何一步失败都保持 staged，不派发重启。
        try:
            db_backup, checkpoint_result = _prepare_state_db_restart(st)
        except Exception as exc:
            return _restart_guard_failed(str(exc))
        busy, log_pages, checkpointed_pages = checkpoint_result
        append_update_log(
            "✅ state.db 重启保护完成："
            f"checkpoint={busy}|{log_pages}|{checkpointed_pages}; backup={db_backup}"
        )

        # 备份保留 staged 状态，便于恢复；随后持久化 restarting，并再次严格
        # checkpoint，确保新进程一定能读取到 resume 标记。
        try:
            save_state(stage=STAGE_RESTARTING, message="用户已确认，正在重启生效")
            state_db.checkpoint(mode="FULL", strict=True)
        except Exception as exc:
            return _restart_guard_failed(f"persist restarting state: {exc}")

        _emit(STAGE_RESTARTING, "🔄 数据库保护完成，正在重启生效 …")
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
            _emit(STAGE_FAILED, f"❌ 重启触发失败：{detail}")
            return False, detail
        return True, detail
    finally:
        _op_lock.release()


def cancel_staged() -> tuple[bool, str]:
    """staged 态取消：源码 checkout 回原 commit；docker 无需动镜像（未 recreate）。

    用 _op_lock 防止与 confirm_restart 竞争（避免「同时取消又确认」）。
    """
    if not _op_lock.acquire(blocking=False):
        return False, "另一个更新操作正在进行中"
    try:
        st = load_state()
        if st.get("stage") != STAGE_STAGED:
            return False, f"当前不在 staged 态（{st.get('stage')}）"
        mode = st.get("mode") or _detect_mode()
        backup_ref = st.get("backup_ref") or ""
        if mode in (MODE_SYSTEMD, MODE_BARE):
            ok, detail = _src_rollback(backup_ref)
        else:
            ok, detail = True, "docker 未重建，丢弃已拉取镜像即可"
        reset_state()
        _emit(STAGE_IDLE, f"↩️ 已取消更新：{detail}")
        return ok, detail
    finally:
        _op_lock.release()


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
    path = os.path.join(config.DATA_DIR, ".update_result")
    deadline = time.time() + max(10, wait_seconds)
    while time.time() < deadline:
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass
        time.sleep(3)
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
    if st.get("stage") != STAGE_RESTARTING:
        return

    def _worker():
        save_state(stage=STAGE_VERIFYING, message="重启完成，健康检查中")
        target = st.get("target_tag") or st.get("to_version") or "?"
        timeout = _cfg()["healthTimeoutSeconds"]
        mode = st.get("mode") or _detect_mode()
        now_ver = __version__

        # docker 形态：sidecar 已经做过健康门控 + 回滚，这里优先读它写的结果标记。
        # 若标记为 ROLLBACK，说明新容器起不来、sidecar 已回滚（当前进程其实是旧版）。
        # 等待窗口要覆盖 sidecar 的健康门控(≤180s) + 回滚重建(~30s)，故放宽到 timeout*2+60。
        if mode == MODE_DOCKER:
            result = _read_update_result(wait_seconds=timeout * 2 + 60)
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
        # 版本对比兜底：健康通过，但若当前版本仍是"更新前版本"（=回滚发生），
        # 说明 sidecar 已回滚（哪怕标记文件丢了），按回滚汇报而非误报成功。
        from_ver = (st.get("from_version") or "").lstrip("vV")
        target_norm = (target or "").lstrip("vV")
        rolled_back_by_version = (
            mode == MODE_DOCKER and ok
            and target_norm and now_ver.lstrip("vV") == from_ver
            and target_norm != from_ver
        )
        if ok and not rolled_back_by_version:
            save_state(stage=STAGE_SUCCESS, to_version=now_ver,
                       message=f"更新成功 → v{now_ver}")
            msg = (f"✅ <b>更新成功</b>，当前已运行 <code>v{now_ver}</code>（目标 {target}）\n"
                   f"<i>已备份旧版本，如需回退可在「🗂 备份列表」查看。</i>")
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
                    _src_restart()
            append_update_log(f"回滚结果：{rb_detail}")
            save_state(stage=STAGE_ROLLED_BACK,
                       message=f"健康检查失败已回滚: {rb_detail}")
            rb_msg = (f"❌ <b>更新失败，已自动回滚</b>\n健康检查未通过：{detail}\n"
                      f"回滚结果：{rb_detail}\n请检查日志后重试。")
            _emit(STAGE_ROLLED_BACK, rb_msg)
            _notify_cross_process(rb_msg, st.get("chat_id"), st.get("notify_msg_id"),
                                  reply_markup=_faillog_buttons())

    threading.Thread(target=_worker, daemon=True, name="updater-resume").start()


# ─── 备份清理 ────────────────────────────────────────────────────

def _prune_backups() -> None:
    """保留最近 keepBackups 份，旧的删掉。"""
    keep = _cfg()["keepBackups"]
    root = _backup_root()
    try:
        metas = [f for f in os.listdir(root) if f.endswith(".json")]
        metas.sort(reverse=True)  # 时间戳在文件名里，逆序=最新在前
        for old in metas[keep:]:
            ref = old[:-5]
            for ext in (".json", ".tar.gz", ".state.db"):
                p = os.path.join(root, ref + ext)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
    except Exception as exc:
        print(f"[updater] prune backups failed: {exc}")


def list_backups() -> list[dict]:
    root = _backup_root()
    out = []
    try:
        for f in sorted(os.listdir(root), reverse=True):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(root, f), "r", encoding="utf-8") as fp:
                        out.append(json.load(fp))
                except Exception:
                    pass
    except Exception:
        pass
    return out
