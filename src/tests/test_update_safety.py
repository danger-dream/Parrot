"""Update safety regressions; all external operations are replaced with local fakes."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config, drain, state_db, update_checker, update_supervisor, updater


@pytest.fixture(autouse=True)
def isolated_update(monkeypatch, tmp_path):
    monkeypatch.setattr(update_checker, "__version__", "0.36.5")
    monkeypatch.setattr(updater, "__version__", "0.36.5")
    state_db.init()
    updater.start()
    update_checker.start()
    updater.reset_state()
    updater.save_state(cancelled_target=None, cancelled_repo=None, backup_owner=None,
                       silent_error_notified=None, silent_error_attempt_at=None)
    update_checker._set_cache(None)
    drain.reset_for_tests()
    config.update(lambda c: c.update(updateChecker={
        "enabled": True, "silentUpdate": True, "repo": "test/Parrot", "keepBackups": 1,
        "includePrerelease": False, "intervalSeconds": 3600,
    }))
    monkeypatch.setattr(updater, "_backup_root", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "_detect_mode", lambda: updater.MODE_BARE)
    monkeypatch.setattr(updater, "_docker_list_backup_tags", lambda: [])
    monkeypatch.setattr(updater, "_progress_cb", None)
    monkeypatch.setattr(updater, "_src_is_git_repo", lambda: False)
    monkeypatch.setattr(updater, "_prepare_state_restart", lambda st: "verified")
    def forbidden(*args, **kwargs):
        raise AssertionError("external updater operation not stubbed")
    for name in ("_run", "_git", "_src_restart", "_docker_sidecar_recreate"):
        monkeypatch.setattr(updater, name, forbidden)
    monkeypatch.setattr(updater.notifier, "notify_event", lambda *a, **kw: True)
    yield
    updater.begin_shutdown()
    drain.reset_for_tests()


def staged(**extra):
    updater.save_state(stage=updater.STAGE_STAGED, target_tag="v0.36.6", target_repo="test/Parrot",
                       mode="bare", silent=True, **extra)


def checked(*targets):
    update_checker._set_cache({"repo": "test/Parrot", "checked_at": time.time(),
        "silent_checked_from": update_checker.__version__,
        "silent_checks": {tag: {"blocked": False, "prerelease": False} for tag in targets}})


def test_backup_management_refuses_while_update_lock_owned(tmp_path):
    ref = "src-current"
    (tmp_path / (ref + ".json")).write_text(json.dumps({"ref": ref, "mode": "src"}))
    archive = tmp_path / (ref + ".tar.gz")
    archive.write_bytes(b"backup")
    with updater._op_lock:
        updater.save_state(stage=updater.STAGE_BACKING_UP, backup_ref=None)
        assert updater.delete_backup(ref) == "busy"
        assert updater.clear_backups()["busy"] is True
        updater._prune_backups()
        assert archive.exists()
        updater.save_state(stage=updater.STAGE_PULLING, backup_ref=ref)
    assert updater.delete_backup(ref) == "protected"
    assert updater.clear_backups()["keptProtected"] is True
    assert archive.exists()


def test_foreign_and_unknown_docker_tags_are_never_removed(monkeypatch):
    updater.save_state(backup_owner="owner-a")
    tags = [("parrot-backup:owner-b-0.36.4-20261112-150000", "other", None),
            ("parrot-backup:0.35.0-20261001-100000", "legacy", None)]
    monkeypatch.setattr(updater, "_docker_list_backup_tags", lambda: tags)
    removed = []
    monkeypatch.setattr(updater, "_docker_remove_backup_tag", lambda tag: removed.append(tag) or True)
    rows = updater.list_backups()
    assert all(row["unowned"] and row["protected"] for row in rows)
    assert updater.delete_backup(rows[0]["ref"]) == "protected"
    updater._prune_backups()
    assert updater.clear_backups()["keptUnowned"] == 2
    assert removed == []


def test_new_docker_backup_has_persistent_owner_and_recovers_own_orphan(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "_has_local_docker", lambda: False)
    monkeypatch.setattr(updater, "_docker_current_image_digest", lambda: "sha256:owned")
    tags = []
    monkeypatch.setattr(updater, "_engine_tag_image", lambda image, repo, tag: tags.append(f"{repo}:{tag}") or True)
    assert updater._docker_backup("v0.36.6")[0]
    owner = updater._backup_owner()
    assert owner and tags[0].startswith("parrot-backup:" + owner + "-")
    updater.reset_state()
    assert updater._backup_owner() == owner
    for path in tmp_path.glob("*.json"):
        path.unlink()
    monkeypatch.setattr(updater, "_docker_list_backup_tags", lambda: [(tags[0], "sha256:owned", None)])
    row = updater.list_backups()[0]
    assert row["orphan"] and not row["unowned"] and not row["protected"]


def test_backup_partial_file_failure_keeps_metadata(monkeypatch, tmp_path):
    ref = "src-partial"
    meta = {"ref": ref, "mode": "src"}
    path = tmp_path / (ref + ".json")
    path.write_text(json.dumps(meta))
    deps = tmp_path / (ref + ".deps")
    deps.mkdir()
    monkeypatch.setattr(updater.shutil, "rmtree", lambda p: (_ for _ in ()).throw(OSError("denied")))
    assert updater._remove_backup_entry(meta) is False
    assert path.exists()


@pytest.mark.parametrize("body", ["* feat(api)!: remove legacy API", "- **fix!: incompatible**", "### feat!: changed", "BREAKING CHANGE: format v2"])
def test_breaking_markers_in_markdown(body, monkeypatch):
    monkeypatch.setattr(update_checker, "__version__", "0.36.5")
    assert update_checker.is_breaking_release("0.36.5", "v0.36.6", {"body": body})[0]


def test_intermediate_breaking_release_prevents_stage(monkeypatch):
    monkeypatch.setattr(update_checker, "__version__", "0.36.5")
    monkeypatch.setattr(update_checker, "_http_get_json", lambda *a: [
        {"tag_name": "v0.36.7", "body": "bugfix"},
        {"tag_name": "v0.36.6", "body": "BREAKING CHANGE: format v2"},
    ])
    calls = []
    monkeypatch.setattr(updater, "stage_update", lambda *a, **kw: calls.append(a) or (True, "staged"))
    update_checker._check_once(push=False)
    assert calls == []
    assert update_checker.get_cached()["silent_checks"]["v0.36.7"]["blocked"]
    assert update_checker.get_update_banner() is not None


def test_release_pagination_and_missing_page_fail_closed(monkeypatch):
    monkeypatch.setattr(update_checker, "__version__", "0.36.5")
    first = [{"tag_name": "v0.36.7", "body": "fix"}] * 100
    calls = []
    def fetch(url):
        calls.append(url)
        if "page=2" in url:
            return [{"tag_name": "v0.36.6", "body": "BREAKING CHANGE: migration"}]
        return first.copy()
    monkeypatch.setattr(update_checker, "_http_get_json", fetch)
    monkeypatch.setattr(updater, "stage_update", lambda *a, **kw: pytest.fail("unsafe stage"))
    update_checker._check_once(push=False)
    assert len(calls) == 2
    assert "v0.36.6" in update_checker.get_cached()["silent_checks"]["v0.36.7"]["reason"]
    monkeypatch.setattr(update_checker, "_http_get_json", lambda url: None if "page=2" in url else first.copy())
    update_checker._check_once(push=False)
    assert "不完整" in update_checker.get_cached()["silent_checks"]["v0.36.7"]["reason"]


def test_cancel_suppresses_only_cancelled_target(monkeypatch):
    staged(source_target_commit="downloaded", backup_ref="ref")
    assert updater.cancel_staged()[0]
    checked("v0.36.6", "v0.36.7")
    calls = []
    monkeypatch.setattr(updater, "stage_update", lambda tag, **kw: calls.append(tag) or (False, "mock"))
    update_checker._drive_silent_update("v0.36.6")
    assert calls == []
    update_checker._drive_silent_update("v0.36.7")
    assert calls == ["v0.36.7"]


def test_staged_target_advances_even_when_latest_changes(monkeypatch):
    staged()
    checked("v0.36.6", "v0.36.7")
    calls = []
    monkeypatch.setattr(updater, "try_silent_activate", lambda: calls.append("activate"))
    update_checker._drive_silent_update("v0.36.7")
    assert calls == ["activate"]


@pytest.mark.parametrize("patch", [{"enabled": False}, {"silentUpdate": False}, {"ignoredVersions": ["v0.36.6"]}, {"repo": "other/repo"}])
def test_changed_settings_block_activation(patch, monkeypatch):
    staged()
    checked("v0.36.6")
    config.update(lambda c: c["updateChecker"].update(patch))
    calls = []
    monkeypatch.setattr(updater, "_src_restart", lambda: calls.append(True) or (True, "mock"))
    assert updater.try_silent_activate(quiet_seconds=0)[0] is False
    update_checker._drive_silent_update("v0.36.6")
    assert calls == []


def test_silent_source_staging_downloads_without_mutating_live_source(monkeypatch):
    monkeypatch.setattr(updater, "_src_backup", lambda tag: (True, "src-safe", "snapshot"))
    commands = []
    def git(args, **kw):
        commands.append(args)
        return 0, "a" * 40 if args[0] == "rev-parse" else ""
    monkeypatch.setattr(updater, "_git", git)
    monkeypatch.setattr(updater, "_source_data_excludes", lambda: [])
    monkeypatch.setattr(update_supervisor, "code_digest", lambda *a: "old-digest")
    work = drain.enter_work()
    try:
        assert updater.stage_update("v0.36.6", silent=True)[0]
        assert drain.active_work_count() == 1
        assert [cmd[0] for cmd in commands] == ["fetch", "rev-parse"]
        assert updater.load_state()["source_target_commit"] == "a" * 40
        assert updater.try_silent_activate(quiet_seconds=0)[0] is False
        assert updater.cancel_staged()[0]
    finally:
        work.close()


def test_activation_fences_new_work_and_releases_on_dispatch_failure(monkeypatch):
    staged()
    notices = []
    monkeypatch.setattr(updater.notifier, "notify_event", lambda *a, **kw: notices.append(a) or True)
    def checkpoint(st):
        with pytest.raises(drain.AdmissionClosed):
            drain.enter_work()
        return "verified"
    monkeypatch.setattr(updater, "_prepare_state_restart", checkpoint)
    monkeypatch.setattr(updater, "_src_restart", lambda: (False, "monitor launch failed"))
    assert updater.try_silent_activate(quiet_seconds=0)[0] is False
    assert len(notices) == 1
    update_checker._drive_silent_update("v0.36.6")
    assert len(notices) == 1
    work = drain.enter_work()
    work.close()


def test_snapshot_failure_releases_fence_and_notifies_once(monkeypatch):
    staged()
    notices = []
    monkeypatch.setattr(updater.notifier, "notify_event", lambda *a, **kw: notices.append(a) or True)
    monkeypatch.setattr(updater, "_prepare_state_restart", lambda st: (_ for _ in ()).throw(OSError("disk full")))
    for _ in range(2):
        assert not updater.try_silent_activate(quiet_seconds=0)[0]
        assert drain.is_idle_for_update(0)
    assert len(notices) == 1
    assert updater.load_state()["stage"] == updater.STAGE_STAGED


@pytest.mark.parametrize("delivery", [False, "exception"])
def test_failure_notification_retries_without_repeating_success(monkeypatch, delivery):
    staged()
    updater.save_state(stage=updater.STAGE_FAILED, message="restart failed")
    calls = []
    def notify(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            if delivery == "exception":
                raise RuntimeError("notification transport down")
            return False
        return True
    monkeypatch.setattr(updater.notifier, "notify_event", notify)
    update_checker._drive_silent_update("v0.36.6")
    update_checker._drive_silent_update("v0.36.6")
    assert len(calls) == 1
    updater.save_state(silent_error_attempt_at=time.time() - 61)
    update_checker._drive_silent_update("v0.36.6")
    assert len(calls) == 2
    updater.save_state(silent_error_attempt_at=time.time() - 61)
    update_checker._drive_silent_update("v0.36.6")
    assert len(calls) == 2


def test_stage_failure_notifies_once_across_checker_ticks(monkeypatch):
    calls = []
    monkeypatch.setattr(updater.notifier, "notify_event", lambda *a, **kw: calls.append(a) or True)
    monkeypatch.setattr(updater, "_src_backup", lambda tag: (False, "", "no disk space"))
    assert not updater.stage_update("v0.36.6", silent=True)[0]
    updater.save_state(silent_error_attempt_at=time.time() - 61)
    update_checker._drive_silent_update("v0.36.6")
    assert len(calls) == 1


def test_monitor_preflight_failure_releases_fence(monkeypatch, tmp_path):
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"update_id": "one", "result": "ROLLBACK_FAILED"}))
    staged(supervisor_id="one", supervisor_result=str(result))
    updater.save_state(stage=updater.STAGE_RESTARTING)
    token = drain.try_reserve_update(0)
    updater._watch_silent_dispatch(token, updater.load_state())
    assert updater.load_state()["stage"] == updater.STAGE_FAILED
    assert drain.is_idle_for_update(0)


@pytest.mark.parametrize("apply_fails", [False, True])
def test_monitor_applies_source_only_after_stop_and_rolls_back_on_apply_failure(monkeypatch, tmp_path, apply_fails):
    events = []
    plan = {"mode": "systemd", "service": "fake.service", "stop_timeout": 1,
            "app": str(tmp_path), "archive": str(tmp_path / "old.tar.gz"), "target_version": "2",
            "from_version": "1", "result_path": str(tmp_path / "result.json"), "update_id": "id"}
    def run_command(args, **kwargs):
        events.append(args[1])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(update_supervisor.subprocess, "run", run_command)
    def apply(p):
        assert events == ["stop"]
        events.append("apply")
        if apply_fails:
            raise RuntimeError("install failed")
    monkeypatch.setattr(update_supervisor, "apply_source_update", apply)
    monkeypatch.setattr(update_supervisor, "wait_health", lambda p, v: events.append("health:" + v) or True)
    monkeypatch.setattr(update_supervisor, "restore_code", lambda *a: events.append("restore"))
    assert update_supervisor.run(plan) == ("ROLLBACK" if apply_fails else "OK")
    assert events == (["stop", "apply", "stop", "restore", "start", "health:1"] if apply_fails
                      else ["stop", "apply", "start", "health:2"])


def test_dependency_install_failure_seals_and_retains_recovery_snapshot(monkeypatch, tmp_path):
    meta = tmp_path / "backup.json"
    meta.write_text("{}")
    snapshot = {"root": str(tmp_path / "deps")}
    calls = []
    def run_command(args, **kwargs):
        calls.append(args)
        if "install" in args:
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(update_supervisor.subprocess, "run", run_command)
    monkeypatch.setattr(update_supervisor, "snapshot_dependencies", lambda *a: snapshot)
    sealed = []
    monkeypatch.setattr(update_supervisor, "seal_dependencies", sealed.append)
    plan = {"app": str(tmp_path), "source_update": {"commands": [["git", "restore"]], "install": True,
            "python": "fake-python", "dependency_root": snapshot["root"], "dependency_paths": [], "meta_path": str(meta)}}
    with pytest.raises(subprocess.CalledProcessError):
        update_supervisor.apply_source_update(plan)
    assert sealed == [snapshot]
    assert json.loads(meta.read_text())["dependencies"] == plan["dependencies"]
    assert calls[0] == ["git", "restore"]


@pytest.mark.parametrize("broken", [False, True])
def test_real_monitor_deferred_source_switch_and_rollback(tmp_path, broken):
    app = tmp_path / "app"
    app.mkdir()
    health = tmp_path / "health.json"
    pidfile = tmp_path / "pid"
    source = app / "server.py"
    template = f'''import os, signal, sys, time, json
from pathlib import Path
health = Path({str(health)!r})
Path({str(pidfile)!r}).write_text(str(os.getpid()))
def stop(*args):
    health.unlink(missing_ok=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
health.write_text(json.dumps({{"status": "ok", "version": VERSION}}))
while True: time.sleep(.02)
'''
    source.write_text('VERSION="1.0"\n' + template)
    archive = tmp_path / "source.tar.gz"
    update_supervisor.backup_code(str(app), str(archive))
    new_source = 'raise ImportError("broken candidate")\n' if broken else 'VERSION="2.0"\n' + template
    apply = tmp_path / "apply.py"
    apply.write_text(f"from pathlib import Path\nassert not Path({str(health)!r}).exists(), 'old process still serving'\n"
                     f"Path({str(source)!r}).write_text({new_source!r})\n")
    old = subprocess.Popen([sys.executable, "-S", str(source)])
    runner = None
    try:
        deadline = time.monotonic() + 3
        while not health.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert health.exists()
        plan = dict(app=str(app), archive=str(archive), mode="bare", old_pid=old.pid,
                    old_identity=update_supervisor.process_identity(old.pid),
                    command=[sys.executable, "-S", str(source)], health_url=health.as_uri(),
                    health_timeout=.5, stop_timeout=2, target_version="2.0", from_version="1.0",
                    target_code_digest=update_supervisor.code_digest(str(app)), dispatch_delay=0,
                    update_id="deferred", result_path=str(tmp_path / "result.json"),
                    source_update={"commands": [[sys.executable, "-S", str(apply)]], "install": False})
        planfile = tmp_path / "plan.json"
        update_supervisor.write_json(planfile, plan)
        monitor = tmp_path / "monitor.py"
        monitor.write_text(Path(update_supervisor.__file__).read_text())
        with (tmp_path / "monitor.log").open("wb") as log:
            runner = subprocess.Popen([sys.executable, "-S", str(monitor), str(planfile)], stdout=log, stderr=log)
        runner.wait(timeout=5)
        assert runner.returncode == 0, (tmp_path / "monitor.log").read_text()
        assert json.loads(Path(plan["result_path"]).read_text())["result"] == ("ROLLBACK" if broken else "OK")
        assert json.loads(health.read_text())["version"] == ("1.0" if broken else "2.0")
    finally:
        if runner is not None and runner.poll() is None:
            runner.kill()
            runner.wait()
        if pidfile.exists():
            pid = int(pidfile.read_text())
            update_supervisor.stop_process(pid, update_supervisor.process_identity(pid), 1)
        if old.poll() is None:
            old.terminate()
        old.wait(timeout=2)
