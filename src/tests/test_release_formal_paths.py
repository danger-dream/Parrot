"""Formal-release boundaries using actual workflow shells and disposable local Git.

GitHub responses and service operations are simulated: never publish or restart.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import Mock

import pytest

from scripts import release as r
from src.tests.test_release_entrypoint import NOTES, SHA, runner
from src.tests.test_release_workflow_order import _shell_script, _step, _workflow


def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.PIPE).strip()


def _body(repo, tmp_path):
    script = _shell_script(_step(_workflow("release.yml"), "Build release body"))
    output = tmp_path / "body-output"
    result = subprocess.run(["bash", "-e", "-c", script], cwd=repo, text=True, capture_output=True,
                            env={**os.environ, "TAG": "v0.34.5", "GITHUB_OUTPUT": str(output)})
    assert result.returncode == 0, result.stderr
    return output.read_text().removeprefix("body<<__END_OF_BODY__\n").removesuffix("__END_OF_BODY__\n")


@pytest.mark.parametrize("first_line", ["v0.34.5：套餐标签更清晰。", "套餐标签更清晰。"])
def test_actual_annotated_body_passes_publication_verifier(runner, tmp_path, monkeypatch, first_line):
    notes = first_line + "\n" + NOTES.split("\n", 1)[1]
    r.validate_notes(notes, "0.34.4", "0.34.5")
    note_file = tmp_path / "notes"
    note_file.write_text(notes)
    _git(runner.repo, "tag", "-a", "--cleanup=verbatim", "v0.34.5", "-F", str(note_file))
    body = _body(runner.repo, tmp_path)
    assert body == notes
    runner.state["notes"] = notes
    runner.github.publish_runs = [{"id": 20, "head_sha": SHA, "head_branch": "v0.34.5",
                                   "event": "workflow_dispatch", "status": "completed", "conclusion": "success"}]
    runner.github.release = {"body": body + "\n**Full Changelog**: https://github.com/fake/repo/compare/v0.34.4...v0.34.5",
                             "html_url": "https://github.com/fake/repo/releases/tag/v0.34.5"}
    monkeypatch.setattr(runner, "remote_ref", lambda ref: None)
    runner.publication()
    assert runner.state["completed"]


def test_lightweight_commit_message_keeps_legacy_title_handling(runner, tmp_path):
    _git(runner.repo, "commit", "--allow-empty", "-m", "v0.34.5: legacy title\n\nLegacy body")
    _git(runner.repo, "tag", "v0.34.5")
    assert _body(runner.repo, tmp_path) == "Legacy body\n"


@pytest.mark.parametrize("ref,ok", [("refs/tags/v0.34.5", True), ("refs/heads/main", False),
                                    ("refs/tags/v0.34.4", False)])
def test_manual_recovery_requires_matching_tag_ref(tmp_path, ref, ok):
    script = _shell_script(_step(_workflow("docker-publish.yml"), "Resolve tag name"))
    output = tmp_path / "tag-output"
    result = subprocess.run(["bash", "-e", "-c", script], text=True, capture_output=True,
                            env={**os.environ, "TAG": "v0.34.5", "GITHUB_OUTPUT": str(output),
                                 "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": ref})
    assert (result.returncode == 0) is ok, result.stderr
    if ok:
        assert output.read_text() == "tag=v0.34.5\n"
    else:
        assert "target tag ref" in result.stderr
        assert not output.exists()


@pytest.mark.parametrize("draft,view_code,expected", [("true", 0, None), ("false", 0, "true"),
                                                       ("", 1, "false"), ("null", 0, None)])
def test_real_release_existence_shell_rejects_draft_without_publishing(tmp_path, draft, view_code, expected):
    script = _shell_script(_step(_workflow("release.yml"), "Check if release already exists"))
    script = script.replace("${{ github.repository }}", "fake/repo")
    gh = tmp_path / "gh"
    gh.write_text('#!/bin/sh\n[ "$1 $2" = "release view" ] || exit 9\n'
                  'printf "%s\\n" "$*" > "$GH_CALLS"\n'
                  f'printf "%s\\n" "{draft}"\nexit {view_code}\n')
    gh.chmod(0o755)
    output, calls = tmp_path / "exists-output", tmp_path / "calls"
    result = subprocess.run(["bash", "-e", "-c", script], text=True, capture_output=True,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "TAG": "v0.34.5",
                                 "GITHUB_OUTPUT": str(output), "GH_CALLS": str(calls)})
    assert "--json isDraft --jq .isDraft" in calls.read_text()
    if expected is None:
        assert result.returncode != 0 and "草稿或状态未知" in result.stderr
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"exists={expected}\n"


def _local_publication(runner, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
    _git(runner.repo, "remote", "add", "origin", str(remote))
    _git(runner.repo, "push", "-u", "origin", "main")
    base = runner.head()
    _git(runner.repo, "commit", "--allow-empty", "-m", "candidate")
    runner.state["candidate"] = runner.head()
    runner.state["runtime_fingerprint"] = runner.runtime_fingerprint()
    runner.state["stages"]["local"] = {"status": "passed", "commit": runner.head(), "environment": "env"}
    monkeypatch.setattr(runner, "environment_fingerprint", lambda: "env")
    monkeypatch.setattr(runner, "preflight", lambda: None)
    monkeypatch.setattr(runner, "validation", lambda *args: None)
    return remote, base


@pytest.mark.parametrize("conflict", ["main", "tag"])
def test_known_git_conflicts_block_before_restart(runner, tmp_path, monkeypatch, conflict):
    remote, base = _local_publication(runner, tmp_path, monkeypatch)
    if conflict == "main":
        other = tmp_path / "other"
        subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
        _git(other, "config", "user.name", "fixture"); _git(other, "config", "user.email", "fixture@example.invalid")
        _git(other, "commit", "--allow-empty", "-m", "parallel main")
        _git(other, "push")
    else:
        _git(runner.repo, "tag", "-a", "v0.34.5", base, "-m", "old failed tag")
        _git(runner.repo, "push", "origin", "refs/tags/v0.34.5")
    restart = Mock(side_effect=AssertionError("must not deploy when publication is blocked"))
    monkeypatch.setattr(runner, "restart", restart)
    with pytest.raises(r.ReleaseError) as caught:
        runner.run(approve_restart=True)
    assert caught.value.stage == "git_publish"
    assert caught.value.code == (2 if conflict == "tag" else 1)
    restart.assert_not_called()
    assert _git(remote, "rev-parse", "main") != runner.state["candidate"]


def test_normal_git_publish_still_pushes_verified_main_and_exact_annotation(runner, tmp_path, monkeypatch):
    remote, base = _local_publication(runner, tmp_path, monkeypatch)
    runner.check_publish_ready()
    assert not _git(runner.repo, "tag", "--list", "v0.34.5")
    assert _git(remote, "rev-parse", "main") == base  # the precheck cannot publish
    runner.git_publish()
    assert _git(remote, "rev-parse", "main") == runner.state["candidate"]
    assert _git(remote, "rev-parse", "v0.34.5^{}") == runner.state["candidate"]
    assert runner.tag_annotation("v0.34.5") == NOTES


@pytest.mark.parametrize("old_environment", ["old-env", None])
def test_changed_or_legacy_restart_receipt_requires_fresh_restart(runner, tmp_path, monkeypatch, old_environment):
    _local_publication(runner, tmp_path, monkeypatch)
    old, new = {"pid": "11", "start_ticks": "1"}, {"pid": "22", "start_ticks": "2"}
    runner.state["stages"]["restart"] = {"status": "passed", "runtime": runner.state["runtime_fingerprint"],
                                         "environment": old_environment, "instance": old}
    monkeypatch.setattr(runner, "service_instance", lambda: old)
    monkeypatch.setattr(runner, "health", lambda: {"status": "ok", "version": "0.34.5"})
    with pytest.raises(r.ReleaseError) as caught:
        runner.restart(approved=False)
    assert caught.value.code == 2
    monkeypatch.setattr(runner, "service_instance", Mock(side_effect=[old, new, new]))
    original, service_calls = runner.execute, []
    def execute(stage, args, **kwargs):
        if args[0] in ("systemctl", "journalctl"):
            service_calls.append(args)
            if args[:2] == ["systemctl", "restart"]:
                assert runner.state["restart_intent"]["environment"] == "env"
            output = "a" * 32 if "InvocationID" in args else ""
            return subprocess.CompletedProcess(args, 0, output, "")
        return original(stage, args, **kwargs)
    monkeypatch.setattr(runner, "execute", execute)
    runner.restart(approved=True)
    assert sum(args[:2] == ["systemctl", "restart"] for args in service_calls) == 1
    assert runner.state["stages"]["restart"]["environment"] == "env"
    assert runner.state["stages"]["restart"]["instance"] == new


@pytest.mark.parametrize("old_environment", ["old-env", None])
def test_uncertain_restart_with_changed_dependencies_cannot_be_adopted(runner, tmp_path, monkeypatch, old_environment):
    _local_publication(runner, tmp_path, monkeypatch)
    runner.state["restart_intent"] = {"old_instance": {"pid": "11", "start_ticks": "1"},
                                     "started_at": 1, "runtime": runner.state["runtime_fingerprint"],
                                     "environment": old_environment}
    monkeypatch.setattr(runner, "service_instance", lambda: {"pid": "22", "start_ticks": "2"})
    health = Mock(side_effect=AssertionError("do not certify an old dependency environment"))
    monkeypatch.setattr(runner, "health", health)
    with pytest.raises(r.ReleaseError, match="inspect service"):
        runner.restart(approved=True)
    health.assert_not_called()
    assert "restart_intent" in runner.state
