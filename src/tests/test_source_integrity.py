"""Comprehensive tests for pre-update source integrity checks (source & Docker)."""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from src import config, drain, source_integrity, state_db, updater
from src.management_control.auxiliary.updates import UpdateControl
from src.tests.management_auxiliary_support import bearer, build_auxiliary_app, create_session


def _create_app_tree(root: Path, *, version: str = "0.36.10") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.py").write_text("print('parrot server')\n", encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "src" / "failover.py").write_text("RETRY = True\n", encoding="utf-8")
    (root / "src" / "providers").mkdir(exist_ok=True)
    (root / "src" / "providers" / "anthropic.py").write_text("NAME = 'anthropic'\n", encoding="utf-8")
    return root


def _git_cmd(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Parrot Test",
        "GIT_AUTHOR_EMAIL": "test@parrot.invalid",
        "GIT_COMMITTER_NAME": "Parrot Test",
        "GIT_COMMITTER_EMAIL": "test@parrot.invalid",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


def _create_git_deployment(tmp_path: Path, *, version: str = "0.36.10") -> tuple[Path, Path, str]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    work = tmp_path / "app"
    _create_app_tree(work, version=version)
    (work / ".gitignore").write_text("config.json\nlogs/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    _git_cmd(work, "init", "-b", "main")
    _git_cmd(work, "remote", "add", "origin", str(origin))
    _git_cmd(work, "add", ".")
    _git_cmd(work, "commit", "-m", f"release v{version}")
    _git_cmd(work, "tag", f"v{version}")
    _git_cmd(work, "push", "-u", "origin", "main", "--tags")
    head = _git_cmd(work, "rev-parse", "HEAD")
    return work, origin, head


def _write_mountinfo(path: Path, mount_points: list[str]) -> Path:
    lines = []
    for idx, mp in enumerate(mount_points, 1):
        escaped = mp.replace("\\", "\\134").replace(" ", "\\040")
        lines.append(f"{idx} 0 8:1 / {escaped} rw,relatime - ext4 /dev/sda1 rw")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestManifestAndDockerChecks:
    def test_manifest_roundtrip_and_runtime_bytecode_exclusion(self, tmp_path):
        app = _create_app_tree(tmp_path / "app")
        manifest_path = tmp_path / "share" / "source-manifest.json"
        manifest = source_integrity.write_manifest(manifest_path, app)
        assert manifest["schema"] == 1
        assert manifest["version"] == "0.36.10"
        assert set(manifest["files"].keys()) == {
            "server.py",
            "src/__init__.py",
            "src/failover.py",
            "src/providers/anthropic.py",
        }

        # Standard __pycache__ artifacts and runtime data paths do not trigger false positives.
        cache_dir = app / "src" / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "failover.cpython-311.pyc").write_bytes(b"bytecode")
        (app / "data").mkdir()
        (app / "data" / "config.json").write_text("{}", encoding="utf-8")
        mountinfo = _write_mountinfo(tmp_path / "mountinfo", ["/", str(app / "data"), "/var/run/docker.sock"])

        report = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=mountinfo,
        )
        assert report.ok is True
        assert report.changes == ()
        assert report.error == ""

    def test_docker_detects_modified_added_deleted_symlink_and_stray_pyc(self, tmp_path):
        app = _create_app_tree(tmp_path / "app")
        manifest_path = tmp_path / "source-manifest.json"
        source_integrity.write_manifest(manifest_path, app)
        mountinfo = _write_mountinfo(tmp_path / "mountinfo", ["/", str(app / "data")])

        # Modify a managed file, add an extra patch file, add a stray .pyc outside __pycache__,
        # delete a managed file, and replace a managed file with a symlink.
        (app / "src" / "failover.py").write_text("RETRY = False  # user patch\n", encoding="utf-8")
        (app / "src" / "user_patch.py").write_text("PATCH = 1\n", encoding="utf-8")
        (app / "src" / "stray.pyc").write_bytes(b"stray")
        (app / "src" / "providers" / "anthropic.py").unlink()
        (app / "server.py").unlink()
        (app / "server.py").symlink_to("/tmp/evil_server.py")

        report = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=mountinfo,
        )
        assert report.ok is False
        by_path = {c.path: c.kind for c in report.changes}
        assert by_path == {
            "server.py": source_integrity.KIND_MODIFIED,
            "src/failover.py": source_integrity.KIND_MODIFIED,
            "src/providers/anthropic.py": source_integrity.KIND_DELETED,
            "src/stray.pyc": source_integrity.KIND_ADDED,
            "src/user_patch.py": source_integrity.KIND_ADDED,
        }
        for expected in ("server.py", "src/failover.py", "src/providers/anthropic.py", "src/user_patch.py"):
            assert expected in report.summary
        assert "已阻止更新" in report.summary

    def test_docker_detects_bind_mount_overrides_even_with_identical_content(self, tmp_path):
        app = _create_app_tree(tmp_path / "app")
        manifest_path = tmp_path / "source-manifest.json"
        source_integrity.write_manifest(manifest_path, app)

        # Identical file content but bind-mounted into /app/src/failover.py and /app/src/providers.
        mountinfo = _write_mountinfo(
            tmp_path / "mountinfo",
            [
                "/",
                str(app / "data"),
                str(app / "src" / "failover.py"),
                str(app / "src" / "providers"),
            ],
        )
        report = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=mountinfo,
        )
        assert report.ok is False
        assert {c.path: c.kind for c in report.changes} == {
            "src/failover.py": source_integrity.KIND_MOUNT_OVERRIDE,
            "src/providers": source_integrity.KIND_MOUNT_OVERRIDE,
        }
        assert "src/failover.py [外部挂载覆盖]" in report.summary

        # If the bind-mounted file is ALSO modified, changes merge cleanly without duplicate paths.
        (app / "src" / "failover.py").write_text("RETRY = 'patched'\n", encoding="utf-8")
        report_both = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=mountinfo,
        )
        assert report_both.ok is False
        matched = [c for c in report_both.changes if c.path == "src/failover.py"]
        assert len(matched) == 1
        assert matched[0].kind == source_integrity.KIND_MODIFIED
        assert "外部挂载覆盖" in matched[0].detail

    def test_docker_detects_app_root_mount_override_and_escaped_spaces(self, tmp_path):
        app = _create_app_tree(tmp_path / "my app")
        manifest_path = tmp_path / "source-manifest.json"
        source_integrity.write_manifest(manifest_path, app)
        mountinfo = _write_mountinfo(tmp_path / "mountinfo", ["/", str(app)])

        report = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=mountinfo,
        )
        assert report.ok is False
        assert set(report.modified_files) == {"server.py", "src"}

    def test_docker_unreadable_mounted_file_fails_closed_without_crashing(self, monkeypatch, tmp_path):
        app = _create_app_tree(tmp_path / "app")
        manifest_path = tmp_path / "source-manifest.json"
        source_integrity.write_manifest(manifest_path, app)
        mountinfo = _write_mountinfo(tmp_path / "mountinfo", ["/", str(app / "src" / "failover.py")])

        real_sha = source_integrity._sha256_file

        def deny_failover(path: Path):
            if path.name == "failover.py":
                raise PermissionError("root-owned bind mount")
            return real_sha(path)

        monkeypatch.setattr(source_integrity, "_sha256_file", deny_failover)
        report = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=mountinfo,
        )
        assert report.ok is False
        assert report.modified_files == ("src/failover.py",)
        assert "无法读取" in report.summary
        assert "外部挂载覆盖" in report.summary

    def test_docker_unreadable_mountinfo_and_manifest_mount_override_fail_closed(self, tmp_path):
        app = _create_app_tree(tmp_path / "app")
        manifest_dir = tmp_path / "share" / "parrot"
        manifest_path = manifest_dir / "source-manifest.json"
        source_integrity.write_manifest(manifest_path, app)

        # Missing mountinfo must fail closed instead of silently skipping mount detection.
        report_no_mountinfo = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=tmp_path / "missing-mountinfo",
        )
        assert report_no_mountinfo.ok is False
        assert "挂载信息" in report_no_mountinfo.error

        # Bind-mounting over the manifest file or its parent directory must fail closed.
        for shadowed in (manifest_path, manifest_dir):
            mountinfo = _write_mountinfo(tmp_path / "mountinfo", ["/", str(app / "data"), str(shadowed)])
            report_shadowed = source_integrity.check_source_integrity(
                mode="docker",
                app_dir=app,
                data_dir=app / "data",
                current_version="0.36.10",
                manifest_path=manifest_path,
                mountinfo_path=mountinfo,
            )
            assert report_shadowed.ok is False
            assert "受管源码清单所在路径被外部挂载覆盖" in report_shadowed.error

    def test_unknown_mode_fails_closed(self, tmp_path):
        app = _create_app_tree(tmp_path / "app")
        report = source_integrity.check_source_integrity(mode="unknown", app_dir=app)
        assert report.ok is False
        assert "不支持的运行形态" in report.error

    @pytest.mark.parametrize("case", ["missing", "symlink", "corrupt_json", "bad_schema", "version_mismatch"])
    def test_docker_manifest_problems_fail_closed(self, tmp_path, case):
        app = _create_app_tree(tmp_path / "app")
        manifest_path = tmp_path / "source-manifest.json"
        mountinfo = _write_mountinfo(tmp_path / "mountinfo", ["/"])

        if case == "missing":
            pass
        elif case == "symlink":
            real = tmp_path / "real.json"
            source_integrity.write_manifest(real, app)
            manifest_path.symlink_to(real)
        elif case == "corrupt_json":
            manifest_path.write_text("{broken", encoding="utf-8")
        elif case == "bad_schema":
            manifest_path.write_text(json.dumps({"schema": 999, "files": {}}), encoding="utf-8")
        elif case == "version_mismatch":
            source_integrity.write_manifest(manifest_path, app, version="0.36.9")
            (app / "src" / "__init__.py").write_text('__version__ = "0.36.10"\n# edited\n', encoding="utf-8")

        report = source_integrity.check_source_integrity(
            mode="docker",
            app_dir=app,
            data_dir=app / "data",
            current_version="0.36.10",
            manifest_path=manifest_path,
            mountinfo_path=mountinfo,
        )
        assert report.ok is False
        assert "已阻止更新" in report.summary
        if case == "version_mismatch":
            assert "src/__init__.py" in report.modified_files
            assert "版本" in report.error

    def test_cli_write_and_verify_manifest(self, tmp_path):
        app = _create_app_tree(tmp_path / "app")
        manifest_path = tmp_path / "manifest.json"
        write_proc = subprocess.run(
            [sys.executable, "-m", "src.source_integrity", "--write-manifest", str(manifest_path), "--app-dir", str(app)],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert write_proc.returncode == 0, write_proc.stderr

        ok_proc = subprocess.run(
            [sys.executable, "-m", "src.source_integrity", "--verify-manifest", str(manifest_path), "--app-dir", str(app)],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert ok_proc.returncode == 0, ok_proc.stderr

        (app / "src" / "failover.py").write_text("PATCHED = True\n", encoding="utf-8")
        bad_proc = subprocess.run(
            [sys.executable, "-m", "src.source_integrity", "--verify-manifest", str(manifest_path), "--app-dir", str(app)],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        assert bad_proc.returncode == 1
        assert "src/failover.py" in bad_proc.stderr


class TestGitAndNonGitSourceChecks:
    def test_git_clean_release_tag_and_upstream_main_pass(self, tmp_path):
        app, _, _ = _create_git_deployment(tmp_path, version="0.36.10")
        # Ignored runtime files must not trigger false positives.
        (app / "config.json").write_text('{"port": 22122}\n', encoding="utf-8")
        (app / "logs").mkdir()
        (app / "logs" / "parrot.log").write_text("ok\n", encoding="utf-8")

        report = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            data_dir=app,
            current_version="0.36.10",
            excludes=("config.json", "logs"),
        )
        assert report.ok is True
        assert report.source_kind == "git"

        # Upstream official commit on origin/main ahead of v0.36.10 is allowed.
        (app / "src" / "failover.py").write_text("RETRY = 'official'\n", encoding="utf-8")
        _git_cmd(app, "add", "src/failover.py")
        _git_cmd(app, "commit", "-m", "official upstream change")
        _git_cmd(app, "push", "origin", "main")

        report_upstream = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            data_dir=app,
            current_version="0.36.10",
        )
        assert report_upstream.ok is True

    def test_git_detects_unstaged_staged_untracked_deleted_renamed_precisely(self, tmp_path):
        app, _, _ = _create_git_deployment(tmp_path)
        # Unstaged modification (leading space in porcelain output must never be stripped!).
        (app / "src" / "failover.py").write_text("RETRY = False\n", encoding="utf-8")
        report_single = source_integrity.check_source_integrity(
            mode="bare",
            app_dir=app,
            current_version="0.36.10",
        )
        assert report_single.ok is False
        assert report_single.modified_files == ("src/failover.py",)
        assert "src/failover.py [已修改]" in report_single.summary

        # Add staged edit, untracked file, deletion, and rename.
        (app / "server.py").write_text("print('staged edit')\n", encoding="utf-8")
        _git_cmd(app, "add", "server.py")
        (app / "src" / "custom_patch.py").write_text("X = 1\n", encoding="utf-8")
        _git_cmd(app, "mv", "src/providers/anthropic.py", "src/providers/anthropic_custom.py")

        report_multi = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            current_version="0.36.10",
        )
        assert report_multi.ok is False
        by_path = {c.path: c.kind for c in report_multi.changes}
        assert by_path["src/failover.py"] == source_integrity.KIND_MODIFIED
        assert by_path["server.py"] == source_integrity.KIND_MODIFIED
        assert by_path["src/custom_patch.py"] == source_integrity.KIND_ADDED
        assert by_path["src/providers/anthropic_custom.py"] == source_integrity.KIND_RENAMED

    def test_git_detects_local_committed_patch_on_branch_and_detached_head(self, tmp_path):
        app, _, _ = _create_git_deployment(tmp_path, version="0.36.10")
        # User commits a local patch on main (working tree becomes clean, but HEAD diverges from origin/main & v0.36.10).
        (app / "src" / "failover.py").write_text("RETRY = 'local-commit-patch'\n", encoding="utf-8")
        (app / "src" / "extra_hook.py").write_text("HOOK = 1\n", encoding="utf-8")
        _git_cmd(app, "add", "src/failover.py", "src/extra_hook.py")
        _git_cmd(app, "commit", "-m", "local user patch")

        report = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            current_version="0.36.10",
        )
        assert report.ok is False
        assert set(report.modified_files) == {"src/failover.py", "src/extra_hook.py"}
        assert "本地提交补丁" in report.summary

        # Detached HEAD off v0.36.10 with a local commit must also be rejected.
        _git_cmd(app, "checkout", "--detach", "v0.36.10")
        (app / "server.py").write_text("print('detached local patch')\n", encoding="utf-8")
        _git_cmd(app, "add", "server.py")
        _git_cmd(app, "commit", "-m", "detached patch")

        report_detached = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            current_version="0.36.10",
        )
        assert report_detached.ok is False
        assert report_detached.modified_files == ("server.py",)

    def test_git_multi_branch_picks_closest_baseline_and_catches_excluded_source_files(self, tmp_path):
        app, _, _ = _create_git_deployment(tmp_path, version="0.36.9")
        # Create an older remote branch alphabetically before origin/main.
        _git_cmd(app, "branch", "archive-old")
        _git_cmd(app, "push", "origin", "archive-old")

        # Advance official origin/main to v0.36.10 with many changes.
        (app / "src" / "__init__.py").write_text('__version__ = "0.36.10"\n', encoding="utf-8")
        (app / "src" / "providers" / "anthropic.py").write_text("NAME = 'anthropic-v2'\n", encoding="utf-8")
        _git_cmd(app, "add", ".")
        _git_cmd(app, "commit", "-m", "release v0.36.10")
        _git_cmd(app, "tag", "v0.36.10")
        _git_cmd(app, "push", "origin", "main", "--tags")

        # Local patch commit modifies only src/failover.py.
        (app / "src" / "failover.py").write_text("RETRY = 'patched'\n", encoding="utf-8")
        _git_cmd(app, "add", "src/failover.py")
        _git_cmd(app, "commit", "-m", "single-file local patch")

        report = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            current_version="0.36.10",
        )
        assert report.ok is False
        # Must report ONLY src/failover.py, never unrelated diffs against archive-old.
        assert report.modified_files == ("src/failover.py",)

        # Reset back to v0.36.10 and test hidden ignored files inside src/ (.git/info/exclude & stray .pyc).
        _git_cmd(app, "reset", "--hard", "v0.36.10")
        (app / ".git" / "info" / "exclude").write_text("src/hidden_patch.py\n", encoding="utf-8")
        (app / "src" / "hidden_patch.py").write_text("HIDDEN = 1\n", encoding="utf-8")
        (app / "src" / "stray.pyc").write_bytes(b"pyc")
        report_hidden = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            current_version="0.36.10",
        )
        assert report_hidden.ok is False
        assert set(report_hidden.modified_files) == {"src/hidden_patch.py", "src/stray.pyc"}

    def test_source_runtime_spool_and_update_result_do_not_false_positive(self, monkeypatch, tmp_path):
        app, _, _ = _create_git_deployment(tmp_path, version="0.36.10")
        spool = app / "queued-body-spool"
        spool.mkdir()
        (spool / "req-1.body").write_bytes(b"queued body")
        (app / ".update_result.abc123").write_text("OK\n", encoding="utf-8")
        (app / ".update_log").write_text("log\n", encoding="utf-8")

        monkeypatch.setattr(updater, "_app_dir", lambda: str(app))
        monkeypatch.setattr(updater, "__version__", "0.36.10")
        monkeypatch.setattr(updater.config, "DATA_DIR", str(app))
        report = updater._check_source_integrity(mode=updater.MODE_BARE)
        assert report.ok is True, report.summary

    def test_git_empty_local_commit_and_missing_baseline_fail_closed(self, tmp_path):
        app, _, _ = _create_git_deployment(tmp_path, version="0.36.10")
        _git_cmd(app, "commit", "--allow-empty", "-m", "empty local commit")
        report_empty = source_integrity.check_source_integrity(
            mode="systemd",
            app_dir=app,
            current_version="0.36.10",
        )
        assert report_empty.ok is False
        assert "偏离官方基线" in report_empty.error

        # Standalone repo with neither version tag nor origin remote fails closed.
        orphan = _create_app_tree(tmp_path / "orphan")
        _git_cmd(orphan, "init", "-b", "main")
        _git_cmd(orphan, "add", ".")
        _git_cmd(orphan, "commit", "-m", "local init")
        report_orphan = source_integrity.check_source_integrity(
            mode="bare",
            app_dir=orphan,
            current_version="0.36.10",
        )
        assert report_orphan.ok is False
        assert "官方版本基线" in report_orphan.error

    def test_non_git_source_uses_manifest_or_fails_closed(self, tmp_path):
        app = _create_app_tree(tmp_path / "non_git")
        # Without manifest -> fail closed.
        missing = source_integrity.check_source_integrity(
            mode="bare",
            app_dir=app,
            current_version="0.36.10",
            manifest_path=tmp_path / "absent.json",
        )
        assert missing.ok is False
        assert missing.source_kind == "unverified"
        assert "既非 Git 仓库也缺少受管源码清单" in missing.error

        # With fallback manifest -> clean passes, patched fails with exact file list.
        manifest_path = app / source_integrity.FALLBACK_MANIFEST_FILENAME
        source_integrity.write_manifest(manifest_path, app)
        clean = source_integrity.check_source_integrity(mode="bare", app_dir=app, current_version="0.36.10")
        assert clean.ok is True
        assert clean.source_kind == "manifest"

        (app / "src" / "failover.py").write_text("PATCH = 1\n", encoding="utf-8")
        dirty = source_integrity.check_source_integrity(mode="bare", app_dir=app, current_version="0.36.10")
        assert dirty.ok is False
        assert dirty.modified_files == ("src/failover.py",)


class TestUpdaterAndApiIntegration:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch, tmp_path):
        state_db.init()
        updater.start()
        updater._mode_cache = None
        updater.reset_state()
        updater.save_state(
            cancelled_target=None,
            cancelled_repo=None,
            silent_error_notified=None,
            silent_error_attempt_at=None,
        )
        updater.reset_update_log()
        drain.reset_for_tests()
        monkeypatch.setattr(updater, "_backup_root", lambda: str(tmp_path / "backups"))
        (tmp_path / "backups").mkdir(exist_ok=True)
        monkeypatch.setattr(updater, "_progress_cb", None)
        yield
        updater.begin_shutdown()
        drain.reset_for_tests()

    def test_no_force_update_bypass_exists_on_public_interfaces(self):
        forbidden = {"force", "allow_dirty", "skip_check", "skip_source_check", "ignore_local_changes", "bypass"}
        for fn in (
            updater.stage_update,
            updater.confirm_restart,
            updater.try_silent_activate,
            source_integrity.check_source_integrity,
            UpdateControl.stage_update,
            UpdateControl.stage_direct,
        ):
            params = set(inspect.signature(fn).parameters.keys())
            assert params.isdisjoint(forbidden), f"forbidden force parameter found on {fn}: {params & forbidden}"

    def test_docker_stage_and_confirm_restart_block_patches_and_mounts(self, monkeypatch, tmp_path):
        app = _create_app_tree(tmp_path / "container_app", version="0.36.10")
        data_dir = app / "data"
        data_dir.mkdir()
        manifest_path = tmp_path / "share" / "source-manifest.json"
        source_integrity.write_manifest(manifest_path, app, version="0.36.10")
        mountinfo = _write_mountinfo(tmp_path / "mountinfo", ["/", str(data_dir)])

        monkeypatch.setattr(updater, "__version__", "0.36.10")
        monkeypatch.setattr(updater, "_detect_mode", lambda: updater.MODE_DOCKER)
        monkeypatch.setattr(updater, "_app_dir", lambda: str(app))
        monkeypatch.setattr(source_integrity, "DEFAULT_MANIFEST_PATH", str(manifest_path))
        monkeypatch.setattr(
            updater,
            "_check_source_integrity",
            lambda *, mode=None, expected_commit=None: source_integrity.check_source_integrity(
                mode=mode or updater.MODE_DOCKER,
                app_dir=app,
                data_dir=data_dir,
                current_version="0.36.10",
                excludes=(),
                manifest_path=manifest_path,
                mountinfo_path=mountinfo,
            ),
        )

        # 1) Patched file inside Docker blocks stage_update BEFORE backup or pull.
        (app / "src" / "failover.py").write_text("RETRY = 'docker-patch'\n", encoding="utf-8")
        monkeypatch.setattr(updater, "_docker_backup", lambda tag: pytest.fail("backup must not run when patched"))
        monkeypatch.setattr(updater, "_docker_pull", lambda tag: pytest.fail("pull must not run when patched"))
        notices: list[str] = []
        monkeypatch.setattr(updater.notifier, "notify_event", lambda ev, text, **kw: notices.append(text) or True)

        ok, detail = updater.stage_update("v0.36.11", silent=True)
        assert ok is False
        assert "src/failover.py" in detail
        assert "已阻止更新" in detail
        assert updater.load_state()["stage"] == updater.STAGE_FAILED
        assert "src/failover.py" in updater.load_state()["message"]
        assert "src/failover.py" in updater.get_update_log()
        assert len(notices) == 1 and "src/failover.py" in notices[0]

        # 2) Restore clean file -> stage succeeds; then patch after staging -> confirm_restart blocks activation.
        (app / "src" / "failover.py").write_text("RETRY = True\n", encoding="utf-8")
        updater.reset_state()
        monkeypatch.setattr(updater, "_docker_backup", lambda tag: (True, "docker-ref-1", "backup ok"))
        monkeypatch.setattr(updater, "_docker_pull", lambda tag: (True, "pulled"))
        monkeypatch.setattr(updater, "_prune_backups_locked", lambda: None)
        ok_stage, _ = updater.stage_update("v0.36.11", silent=False)
        assert ok_stage is True
        assert updater.load_state()["stage"] == updater.STAGE_STAGED

        # Patch after staging via bind mount override -> manual confirm_restart rejects and keeps staged rollback anchor.
        _write_mountinfo(mountinfo, ["/", str(data_dir), str(app / "server.py")])
        monkeypatch.setattr(updater, "_docker_sidecar_recreate", lambda **kw: pytest.fail("recreate must not run"))
        ok_restart, restart_detail = updater.confirm_restart()
        assert ok_restart is False
        assert "server.py" in restart_detail
        assert "外部挂载覆盖" in restart_detail
        assert updater.load_state()["stage"] == updater.STAGE_STAGED
        assert "server.py" in updater.load_state()["message"]

        # Silent activation with post-stage patch rolls back compose tag, marks failed, and notifies admin.
        retagged: list[tuple[str, str]] = []
        (Path(updater._backup_root()) / "docker-ref-1.json").write_text(
            json.dumps({"ref": "docker-ref-1", "digest": "sha256:old", "image": "ghcr.io/danger-dream/parrot:latest"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(updater, "_tag_image_for_compose", lambda src, dst: retagged.append((src, dst)) or (True, "ok"))
        notices.clear()
        updater.save_state(silent=True, silent_error_notified=None, silent_error_attempt_at=None)
        ok_silent, silent_detail = updater.confirm_restart(silent=True)
        assert ok_silent is False
        assert "server.py" in silent_detail
        assert retagged == [("sha256:old", "ghcr.io/danger-dream/parrot:latest")]
        assert updater.load_state()["stage"] == updater.STAGE_FAILED
        assert len(notices) == 1 and "server.py" in notices[0]

    def test_source_stage_and_confirm_restart_block_unstaged_and_committed_patches(self, monkeypatch, tmp_path):
        app, _, base_commit = _create_git_deployment(tmp_path, version="0.36.10")
        monkeypatch.setattr(updater, "__version__", "0.36.10")
        monkeypatch.setattr(updater, "_detect_mode", lambda: updater.MODE_SYSTEMD)
        monkeypatch.setattr(updater, "_app_dir", lambda: str(app))
        monkeypatch.setattr(updater, "_source_data_excludes", lambda: [])

        # Local patch blocks stage_update before backup/pull.
        (app / "src" / "failover.py").write_text("RETRY = 'patched'\n", encoding="utf-8")
        monkeypatch.setattr(updater, "_src_backup", lambda tag: pytest.fail("backup must not run"))
        ok, detail = updater.stage_update("v0.36.11")
        assert ok is False
        assert "src/failover.py" in detail
        assert updater.load_state()["stage"] == updater.STAGE_FAILED

        # Restore file -> stage silent succeeds; commit a local patch after staging -> confirm_restart blocks.
        _git_cmd(app, "checkout", "--", "src/failover.py")
        updater.reset_state()
        (Path(updater._backup_root()) / "src-ref-1.json").write_text(
            json.dumps({"ref": "src-ref-1", "commit": base_commit}),
            encoding="utf-8",
        )
        monkeypatch.setattr(updater, "_src_backup", lambda tag: (True, "src-ref-1", "backup ok"))
        monkeypatch.setattr(
            updater,
            "_src_stage",
            lambda tag: (updater.save_state(source_target_commit="b" * 40) or True, "downloaded"),
        )
        monkeypatch.setattr(updater, "_prune_backups_locked", lambda: None)
        ok_stage, _ = updater.stage_update("v0.36.11", silent=True)
        assert ok_stage is True

        (app / "src" / "providers" / "anthropic.py").write_text("NAME = 'post-stage-patch'\n", encoding="utf-8")
        _git_cmd(app, "add", "src/providers/anthropic.py")
        _git_cmd(app, "commit", "-m", "post-stage local patch")
        monkeypatch.setattr(updater, "_src_restart", lambda: pytest.fail("restart must not run"))

        ok_restart, restart_detail = updater.confirm_restart(silent=True)
        assert ok_restart is False
        assert "src/providers/anthropic.py" in restart_detail
        assert updater.load_state()["stage"] == updater.STAGE_FAILED
        assert "src/providers/anthropic.py" in updater.get_update_log()

    def test_manual_source_confirm_restart_blocks_post_stage_edit_and_preserves_staged(self, monkeypatch, tmp_path):
        app, _, base_commit = _create_git_deployment(tmp_path, version="0.36.10")
        monkeypatch.setattr(updater, "__version__", "0.36.10")
        monkeypatch.setattr(updater, "_detect_mode", lambda: updater.MODE_BARE)
        monkeypatch.setattr(updater, "_app_dir", lambda: str(app))
        monkeypatch.setattr(updater, "_source_data_excludes", lambda: [])

        updater.save_state(
            stage=updater.STAGE_STAGED,
            mode=updater.MODE_BARE,
            from_version="0.36.10",
            target_tag="v0.36.11",
            source_staged_commit=base_commit,
            silent=False,
        )
        # Modify a file after manual stage before confirming restart.
        (app / "src" / "failover.py").write_text("RETRY = 'edited-while-staged'\n", encoding="utf-8")
        monkeypatch.setattr(updater, "_src_restart", lambda: pytest.fail("restart must not run"))

        ok, detail = updater.confirm_restart()
        assert ok is False
        assert "src/failover.py" in detail
        assert "已阻止更新" in detail
        assert updater.load_state()["stage"] == updater.STAGE_STAGED
        assert "src/failover.py" in updater.load_state()["message"]

    def test_confirm_restart_digest_backstop_blocks_when_code_digest_drifts(self, monkeypatch, tmp_path):
        app = _create_app_tree(tmp_path / "app", version="0.36.10")
        monkeypatch.setattr(updater, "_detect_mode", lambda: updater.MODE_BARE)
        monkeypatch.setattr(updater, "_app_dir", lambda: str(app))
        monkeypatch.setattr(updater, "_source_data_excludes", lambda: [])
        monkeypatch.setattr(
            updater,
            "_check_source_integrity",
            lambda *, mode=None, expected_commit=None: source_integrity.SourceIntegrityReport(
                ok=True, mode=mode or updater.MODE_BARE, source_kind="git"
            ),
        )
        monkeypatch.setattr(updater, "_src_restart", lambda: pytest.fail("restart must not run"))

        updater.save_state(
            stage=updater.STAGE_STAGED,
            mode=updater.MODE_BARE,
            from_version="0.36.10",
            target_tag="v0.36.11",
            target_code_digest="0" * 64,
            silent=False,
        )
        ok, detail = updater.confirm_restart()
        assert ok is False
        assert "受管源码摘要发生变化" in detail
        assert updater.load_state()["stage"] == updater.STAGE_STAGED

    def test_management_api_and_telegram_expose_modified_files(self, monkeypatch, tmp_path):
        from src.telegram.menus import update_menu

        app, _, fixture = build_auxiliary_app(tmp_path)
        report = source_integrity._build_report(
            mode="docker",
            source_kind="manifest",
            changes=[
                source_integrity.SourceChange("src/failover.py", source_integrity.KIND_MODIFIED, "外部挂载覆盖"),
                source_integrity.SourceChange("src/local_patch.py", source_integrity.KIND_ADDED),
            ],
        )

        def blocked_stage(_version: str, *, chat_id=None, notify_msg_id=None):
            fixture.update_gateway.update_state = {
                "stage": "failed",
                "mode": "docker",
                "from_version": "0.36.10",
                "to_version": "0.36.11",
                "target_tag": "0.36.11",
                "message": report.summary,
            }
            fixture.update_gateway.failure_log = lambda: "\n".join(report.detail_lines)
            return False, report.summary

        fixture.update_gateway.stage = blocked_stage

        with TestClient(app) as client:
            headers = bearer(create_session(client))
            staged = client.post(
                "/api/management/v1/updates/0.36.11/actions/stage",
                headers={**headers, "Idempotency-Key": "stage-blocked-integrity"},
            )
            assert staged.status_code == 202
            op_id = staged.json()["data"]["id"]
            op = client.get(f"/api/management/v1/operations/{op_id}", headers=headers).json()["data"]
            assert op["status"] == "failed"

            state = client.get("/api/management/v1/updates/state", headers=headers).json()["data"]
            assert state["stage"] == "failed"
            assert "src/failover.py" in state["message"]
            assert "src/local_patch.py" in state["message"]
            assert "已阻止更新" in state["message"]

            log = client.get("/api/management/v1/updates/failure-log", headers=headers).json()["data"]["content"]
            assert "src/failover.py" in log
            assert "src/local_patch.py" in log
            assert "不提供强制更新" in log

        # Verify Telegram menu displays the modified files directly when stage is blocked.
        edits: list[str] = []
        monkeypatch.setattr(update_menu, "_CONTROL", fixture.controls.updates)
        monkeypatch.setattr(update_menu.ui, "answer_cb", lambda *a, **kw: None)
        monkeypatch.setattr(update_menu.ui, "edit", lambda chat_id, msg_id, text, reply_markup=None: edits.append(text))
        update_menu._stage(42, 77, "cb-1", "v0.36.11")
        assert any("src/failover.py" in text and "src/local_patch.py" in text for text in edits)
