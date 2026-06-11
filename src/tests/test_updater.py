"""自更新执行器（updater）单测。

覆盖：
  - 形态检测 _detect_mode（docker/systemd/bare + config override）
  - 状态机持久化：save_state/load_state/reset_state 跨"连接"读写
  - is_busy 判定 + 中间态卡死自愈 _heal_stale_state
  - 版本对比 / 健康 URL
  - 配置读取 _cfg 默认值
  - 更新日志 append/reset/get_update_log（含截断）
  - sidecar 脚本生成 _compose_up_inner（关键安全要素：compose 校验/digest 回滚/
    回滚后健康验证/ROLLBACK 标记/不使用 rename）
  - stage_update：备份失败 / 拉取失败 / 成功进 staged（全 mock 外部命令）
  - 并发锁：stage 持锁时再次 stage 被拒
  - confirm_restart / cancel_staged 的状态前置校验
  - 备份列表 list_backups / 清理 _prune_backups

运行：
  cd /opt/src-space/parrot && python -m pytest src/tests/test_updater.py -v
"""

from __future__ import annotations

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))))
from src.tests import _isolation
_tmpdir = _isolation.isolate()

import json
import time

import pytest

from src import config, state_db, updater


# ─── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_updater():
    """每个测试前：初始化 state_db、复位状态机、清形态缓存、复位配置。"""
    state_db.init()
    updater._mode_cache = None
    # 复位 updateChecker 配置到一个已知基线
    config.update(lambda c: c.__setitem__("updateChecker", {
        "enabled": True,
        "autoUpdate": False,
        "repo": "danger-dream/Parrot",
        "serviceName": "parrot.service",
        "composeDir": _tmpdir,
        "composeService": "anthropic-proxy",
        "containerName": "parrot",
        "image": "ghcr.io/danger-dream/parrot:latest",
        "keepBackups": 5,
        "healthTimeoutSeconds": 90,
        "updaterImage": "docker:cli",
    }))
    try:
        updater.reset_state()
    except Exception:
        pass
    try:
        updater.reset_update_log()
    except Exception:
        pass
    yield


# ─── 形态检测 ─────────────────────────────────────────────────────

class TestDetectMode:
    def test_config_override_docker(self):
        updater._mode_cache = None
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        assert updater.get_mode() == updater.MODE_DOCKER

    def test_config_override_systemd(self):
        updater._mode_cache = None
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "systemd"))
        assert updater.get_mode() == updater.MODE_SYSTEMD

    def test_config_override_bare(self):
        updater._mode_cache = None
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "bare"))
        assert updater.get_mode() == updater.MODE_BARE

    def test_cache_is_used(self):
        updater._mode_cache = updater.MODE_BARE
        # 即使改 config，缓存仍生效
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        assert updater.get_mode() == updater.MODE_BARE


# ─── 配置读取 ─────────────────────────────────────────────────────

class TestCfg:
    def test_defaults_when_empty(self):
        config.update(lambda c: c.__setitem__("updateChecker", {}))
        cfg = updater._cfg()
        assert cfg["repo"] == "danger-dream/Parrot"
        assert cfg["composeService"] == "anthropic-proxy"
        assert cfg["containerName"] == "parrot"
        assert cfg["keepBackups"] == 5
        assert cfg["healthTimeoutSeconds"] == 90
        assert cfg["updaterImage"] == "docker:cli"
        assert cfg["autoUpdate"] is False

    def test_health_url_uses_listen_port(self):
        config.update(lambda c: c.__setitem__("listen", {"host": "0.0.0.0", "port": 22122}))
        assert updater._health_url() == "http://127.0.0.1:22122/health"


# ─── Docker image tag 解析 ────────────────────────────────────────

class TestDockerImageTarget:
    def test_release_v_tag_maps_to_semver_docker_tag(self):
        assert updater._docker_tag_from_release("v0.23.1") == "0.23.1"
        assert updater._docker_tag_from_release("0.23.1") == "0.23.1"
        assert updater._docker_pull_ref_for_target(
            "ghcr.io/danger-dream/parrot:latest", "v0.23.1"
        ) == "ghcr.io/danger-dream/parrot:0.23.1"
        assert updater._docker_pull_ref_for_target(
            "ghcr.io/danger-dream/parrot:v0.23.0", "v0.23.1"
        ) == "ghcr.io/danger-dream/parrot:0.23.1"

    def test_docker_pull_engine_pulls_semver_and_tags_compose_image(self, monkeypatch):
        config.update(lambda c: c["updateChecker"].__setitem__(
            "image", "ghcr.io/danger-dream/parrot:v0.23.1"
        ))
        calls = []
        monkeypatch.setattr(updater, "_has_local_docker", lambda: False)
        monkeypatch.setattr(updater, "_engine_pull_image", lambda image: calls.append(("pull", image)) or (True, "pulled"))
        monkeypatch.setattr(updater, "_engine_tag_image", lambda src, repo, tag: calls.append(("tag", src, repo, tag)) or True)
        ok, detail = updater._docker_pull("v0.23.1")
        assert ok is True, detail
        assert calls == [
            ("pull", "ghcr.io/danger-dream/parrot:0.23.1"),
            ("tag", "ghcr.io/danger-dream/parrot:0.23.1", "ghcr.io/danger-dream/parrot", "v0.23.1"),
        ]

    def test_docker_pull_cli_pulls_semver_and_tags_latest(self, monkeypatch):
        config.update(lambda c: c["updateChecker"].__setitem__(
            "image", "ghcr.io/danger-dream/parrot:latest"
        ))
        calls = []
        monkeypatch.setattr(updater, "_has_local_docker", lambda: True)
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return 0, ""
        monkeypatch.setattr(updater, "_run", fake_run)
        ok, detail = updater._docker_pull("v0.23.2")
        assert ok is True, detail
        assert calls == [
            ["docker", "pull", "ghcr.io/danger-dream/parrot:0.23.2"],
            ["docker", "tag", "ghcr.io/danger-dream/parrot:0.23.2", "ghcr.io/danger-dream/parrot:latest"],
        ]


# ─── 状态机持久化 ─────────────────────────────────────────────────

class TestStateMachine:
    def test_initial_idle(self):
        updater.reset_state()
        st = updater.load_state()
        assert st.get("stage") == updater.STAGE_IDLE

    def test_save_and_load(self):
        updater.save_state(stage=updater.STAGE_STAGED, mode="docker",
                           from_version="0.22.1", target_tag="v0.23.0",
                           chat_id=123, notify_msg_id=456, backup_ref="ref-x")
        st = updater.load_state()
        assert st["stage"] == updater.STAGE_STAGED
        assert st["mode"] == "docker"
        assert st["from_version"] == "0.22.1"
        assert st["target_tag"] == "v0.23.0"
        assert st["chat_id"] == 123
        assert st["notify_msg_id"] == 456
        assert st["backup_ref"] == "ref-x"

    def test_partial_update_preserves_fields(self):
        updater.save_state(stage=updater.STAGE_STAGED, backup_ref="keep-me")
        updater.save_state(stage=updater.STAGE_RESTARTING)  # 只改 stage
        st = updater.load_state()
        assert st["stage"] == updater.STAGE_RESTARTING
        assert st["backup_ref"] == "keep-me"  # 未传的字段保留

    def test_reset(self):
        updater.save_state(stage=updater.STAGE_STAGED, backup_ref="x")
        updater.reset_state()
        st = updater.load_state()
        assert st["stage"] == updater.STAGE_IDLE
        assert st.get("backup_ref") is None


# ─── is_busy + 卡死自愈 ───────────────────────────────────────────

class TestIsBusy:
    def test_idle_not_busy(self):
        updater.reset_state()
        assert updater.is_busy() is False

    def test_terminal_states_not_busy(self):
        for stage in (updater.STAGE_SUCCESS, updater.STAGE_FAILED,
                      updater.STAGE_ROLLED_BACK):
            updater.save_state(stage=stage)
            assert updater.is_busy() is False, stage

    def test_staged_is_busy(self):
        updater.save_state(stage=updater.STAGE_STAGED)
        assert updater.is_busy() is True

    def test_transient_states_busy(self):
        for stage in (updater.STAGE_BACKING_UP, updater.STAGE_PULLING,
                      updater.STAGE_RESTARTING, updater.STAGE_VERIFYING):
            updater.save_state(stage=stage)
            assert updater.is_busy() is True, stage

    def test_stale_transient_self_heals(self):
        # 模拟一个很久以前进入 restarting 的卡死状态
        updater.save_state(stage=updater.STAGE_RESTARTING)
        # 手动把 updated_at 改老（超过自愈阈值）
        conn = state_db._get_conn()
        old_ts = int(time.time()) - (updater._STALE_INTERMEDIATE_SECONDS + 100)
        with state_db._write_lock:
            conn.execute("UPDATE app_self_update SET updated_at=? WHERE id=1", (old_ts,))
            conn.commit()
        # is_busy 触发自愈 → 复位为 failed → 不再 busy
        assert updater.is_busy() is False
        assert updater.load_state()["stage"] == updater.STAGE_FAILED

    def test_staged_does_not_self_heal(self):
        # staged 等用户确认，可以等很久，不应超时自愈
        updater.save_state(stage=updater.STAGE_STAGED)
        conn = state_db._get_conn()
        old_ts = int(time.time()) - (updater._STALE_INTERMEDIATE_SECONDS + 100)
        with state_db._write_lock:
            conn.execute("UPDATE app_self_update SET updated_at=? WHERE id=1", (old_ts,))
            conn.commit()
        assert updater.is_busy() is True
        assert updater.load_state()["stage"] == updater.STAGE_STAGED


# ─── 更新日志 ─────────────────────────────────────────────────────

class TestUpdateLog:
    def test_reset_and_append(self):
        updater.reset_update_log()
        updater.append_update_log("step one")
        updater.append_update_log("step two")
        log = updater.get_update_log()
        assert "step one" in log
        assert "step two" in log

    def test_empty_log(self):
        # 删掉日志文件
        try:
            _os.remove(updater._update_log_path())
        except FileNotFoundError:
            pass
        assert updater.get_update_log() == ""

    def test_truncation(self):
        updater.reset_update_log()
        for i in range(2000):
            updater.append_update_log(f"line {i} " + "x" * 50)
        log = updater.get_update_log(max_chars=1000)
        # 截断后含省略标记，且长度受控
        assert "前略" in log
        assert len(log) < 1200


# ─── sidecar 脚本生成（关键安全要素）──────────────────────────────

class TestComposeUpInner:
    def test_contains_safety_elements(self):
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        updater._mode_cache = None
        script = updater._compose_up_inner(backup_digest="sha256:deadbeef")
        # 校验 compose 合法
        assert "docker compose config" in script
        # 健康门控
        assert "wait_health" in script or "/health" in script
        # 失败回滚函数
        assert "fail_rollback" in script
        # 回滚用备份 digest
        assert "sha256:deadbeef" in script
        # 回滚后健康验证 + 极端标记
        assert "ROLLBACK" in script
        assert "ROLLBACK_FAILED" in script
        # 成功标记
        assert "OK" in script
        # 关键：不再使用 rename 留存（会被 compose label 误重建）
        assert "rename" not in script

    def test_health_tries_from_config(self):
        config.update(lambda c: c["updateChecker"].__setitem__("healthTimeoutSeconds", 30))
        updater._mode_cache = None
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        script = updater._compose_up_inner(backup_digest="sha256:x")
        # 30/3 = 10 次
        assert "seq 1 10" in script


# ─── 版本对比 ─────────────────────────────────────────────────────

class TestVersionLogic:
    def test_backup_root_created(self):
        root = updater._backup_root()
        assert _os.path.isdir(root)

    def test_list_backups_empty(self):
        # 清空备份目录
        root = updater._backup_root()
        for f in _os.listdir(root):
            try:
                _os.remove(_os.path.join(root, f))
            except Exception:
                pass
        assert updater.list_backups() == []

    def test_list_backups_reads_meta(self):
        root = updater._backup_root()
        meta = {"ref": "src-0.22.1-xxx", "version": "0.22.1",
                "target_tag": "v0.23.0", "mode": "src"}
        with open(_os.path.join(root, "src-0.22.1-xxx.json"), "w") as f:
            json.dump(meta, f)
        backups = updater.list_backups()
        assert any(b.get("ref") == "src-0.22.1-xxx" for b in backups)


# ─── stage_update（mock 外部命令）─────────────────────────────────

class TestStageUpdate:
    def test_backup_failure_keeps_idle_recoverable(self, monkeypatch):
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        updater._mode_cache = None
        # docker 备份失败（inspect 不到镜像）
        monkeypatch.setattr(updater, "_docker_backup",
                            lambda tag: (False, "", "cannot inspect"))
        ok, detail = updater.stage_update("v0.23.0")
        assert ok is False
        assert "cannot inspect" in detail
        st = updater.load_state()
        assert st["stage"] == updater.STAGE_FAILED
        # failed 不算 busy，可重试
        assert updater.is_busy() is False

    def test_pull_failure_after_backup(self, monkeypatch):
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        updater._mode_cache = None
        monkeypatch.setattr(updater, "_docker_backup",
                            lambda tag: (True, "ref-1", "backup ok"))
        monkeypatch.setattr(updater, "_docker_pull",
                            lambda tag: (False, "pull http 404"))
        ok, detail = updater.stage_update("v0.23.0")
        assert ok is False
        assert "404" in detail
        assert updater.load_state()["stage"] == updater.STAGE_FAILED

    def test_success_enters_staged(self, monkeypatch):
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        updater._mode_cache = None
        monkeypatch.setattr(updater, "_docker_backup",
                            lambda tag: (True, "ref-1", "backup ok"))
        monkeypatch.setattr(updater, "_docker_pull",
                            lambda tag: (True, "pulled"))
        monkeypatch.setattr(updater, "_prune_backups", lambda: None)
        ok, detail = updater.stage_update("v0.23.0", chat_id=999, notify_msg_id=1)
        assert ok is True
        st = updater.load_state()
        assert st["stage"] == updater.STAGE_STAGED
        assert st["backup_ref"] == "ref-1"
        assert st["chat_id"] == 999

    def test_src_local_changes_rejected(self, monkeypatch):
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "systemd"))
        updater._mode_cache = None
        monkeypatch.setattr(updater, "_src_is_git_repo", lambda: True)
        monkeypatch.setattr(updater, "_src_has_local_changes", lambda: True)
        ok, detail = updater.stage_update("v0.23.0")
        assert ok is False
        assert "未提交改动" in detail or "改动" in detail
        assert updater.load_state()["stage"] == updater.STAGE_FAILED


# ─── 并发锁 ───────────────────────────────────────────────────────

class TestConcurrencyLock:
    def test_second_stage_rejected_while_locked(self, monkeypatch):
        config.update(lambda c: c["updateChecker"].__setitem__("runtimeMode", "docker"))
        updater._mode_cache = None
        # 手动占住锁，模拟另一个操作进行中
        acquired = updater._op_lock.acquire(blocking=False)
        assert acquired
        try:
            ok, detail = updater.stage_update("v0.23.0")
            assert ok is False
            assert "进行中" in detail
        finally:
            updater._op_lock.release()

    def test_confirm_restart_wrong_state_rejected(self):
        updater.reset_state()  # idle
        ok, detail = updater.confirm_restart()
        assert ok is False
        assert "staged" in detail

    def test_cancel_staged_wrong_state_rejected(self):
        updater.reset_state()  # idle
        ok, detail = updater.cancel_staged()
        assert ok is False
        assert "staged" in detail
