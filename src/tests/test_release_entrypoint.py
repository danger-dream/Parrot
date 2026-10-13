"""Fixed release orchestration: offline state-machine and artifact-contract tests."""
from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
from unittest.mock import Mock

import pytest

from scripts import release as r


SHA = "a" * 40
OTHER = "b" * 40
NOTES = "套餐标签更清晰。\n\n从 v0.34.4 升级到 v0.34.5。\n\n## 🔧 优化\n• 套餐显示准确。\n"


class FakeGitHub:
    def __init__(self):
        self.repository = "fake/repo"
        self.calls = []
        self.runs = [{"id": 10, "head_sha": SHA, "head_branch": "release-candidate/fixture",
                      "event": "push", "status": "completed", "conclusion": "success", "run_attempt": 1,
                      "html_url": "https://github.com/fake/repo/actions/runs/10"}]
        self.job_list = [{"id": 1, "name": "quality", "conclusion": "success"},
                         {"id": 2, "name": "build (amd64)", "conclusion": "success"},
                         {"id": 3, "name": "build (arm64)", "conclusion": "success"}]
        self.release = None
        self.publish_runs = []

    def prepare_runs(self, sha, branch=None):
        self.calls.append(("prepare_runs", sha, branch))
        return self.runs

    def jobs(self, run_id):
        return self.job_list

    def verified_candidate(self, sha):
        r.verify_jobs(self.job_list)
        return self.runs[0]

    def request(self, path, *, method="GET", data=None):
        self.calls.append((method, path))
        if path.startswith("releases/tags/"):
            return self.release
        if path.startswith("actions/workflows/docker-publish.yml/"):
            return {"workflow_runs": self.publish_runs}
        if path.endswith("/logs"):
            return b"FAILED fixture_test - useful failure detail\n"
        if method == "POST":
            return None
        if path.startswith("actions/runs/"):
            return self.runs[0]
        return None


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fixture_release_secret")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/__init__.py").write_text('__version__ = "0.34.5"\n')
    (repo / "server.py").write_text("import json\n")
    (repo / "requirements.txt").write_text("")
    (repo / ".gitignore").write_text(".release-runs/\n")
    for args in (("init", "-b", "main"), ("config", "user.name", "fixture"),
                 ("config", "user.email", "fixture@fake.invalid"), ("add", "."), ("commit", "-m", "fixture")):
        subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.PIPE)
    state = {"schema": 1, "run_id": "0.34.5-0123456789ab", "repository": "fake/repo",
             "old_version": "0.34.4", "version": "0.34.5", "notes": NOTES,
             "files": ["src/__init__.py"], "python": sys.executable, "workers": 8,
             "timeout": 5, "service": "parrot.service", "health_url": "http://127.0.0.1:12345/health",
             "candidate": SHA, "branch": "release-candidate/fixture", "runtime_fingerprint": "runtime",
             "stages": {}}
    value = r.ReleaseRun(repo, repo / ".release-runs" / state["run_id"], state, github=FakeGitHub())
    yield value
    value.cleanup()


@pytest.mark.parametrize("version", ["0.34.4", "0.34.3", "v0.34.5", "0.34.5;echo", "01.34.5"])
def test_candidate_version_must_advance(version):
    with pytest.raises(r.ReleaseError):
        r.validate_notes(NOTES, "0.34.4", version)


def test_notes_and_repository_contract():
    r.validate_notes(NOTES, "0.34.4", "0.34.5")
    assert r.github_repository("https://github.com/fake/repo.git") == "fake/repo"
    assert r.github_repository("git@github.com:fake/repo.git") == "fake/repo"
    with pytest.raises(r.ReleaseError):
        r.github_repository("https://token@github.com/fake/repo")
    with pytest.raises(r.ReleaseError):
        r.validate_notes(NOTES + "Full Changelog", "0.34.4", "0.34.5")


def test_atomic_state_does_not_store_credentials(runner):
    runner.save()
    path = runner.directory / "state.json"
    assert json.loads(path.read_text())["run_id"] == runner.state["run_id"]
    assert "fixture_release_secret" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(runner.directory.glob("tmp*"))


def test_direct_dependency_audit_and_environment_fingerprint(runner):
    runner.audit_dependencies()
    assert len(runner.environment_fingerprint()) == 64
    (runner.repo / "server.py").write_text("import pytest\n")
    (runner.repo / "requirements.txt").write_text("pytest>=9\n")
    runner.audit_dependencies()
    (runner.repo / "requirements.txt").write_text("")
    with pytest.raises(r.ReleaseError, match="Command exited"):
        runner.audit_dependencies()


@pytest.mark.parametrize("change", ["quality", "arm64", "missing", "duplicate"])
def test_every_cloud_gate_must_pass(runner, change):
    jobs = copy.deepcopy(runner.github.job_list)
    if change == "quality":jobs[0]["conclusion"] = "failure"
    elif change == "arm64":jobs[2]["conclusion"] = "cancelled"
    elif change == "missing":jobs.pop()
    else:jobs.append(copy.deepcopy(jobs[1]))
    with pytest.raises(r.ReleaseError):r.verify_jobs(jobs)


def test_cloud_candidate_is_bound_to_sha_and_owned_branch(monkeypatch):
    gh = r.GitHub("fake/repo", "secret")
    monkeypatch.setattr(gh, "request", lambda path: {"workflow_runs": [
        {"head_sha": SHA, "event": "push", "head_branch": "release-candidate/a"},
        {"head_sha": OTHER, "event": "push", "head_branch": "release-candidate/a"},
        {"head_sha": SHA, "event": "push", "head_branch": "main"},
        {"head_sha": SHA, "event": "pull_request", "head_branch": "release-candidate/a"},
    ]})
    assert len(gh.prepare_runs(SHA)) == 1
    assert gh.prepare_runs(SHA, "release-candidate/other") == []


def test_log_redirect_does_not_forward_github_credential(monkeypatch):
    gh = r.GitHub("fake/repo", "secret")
    gh.opener = Mock()
    gh.opener.open.side_effect = urllib.error.HTTPError("api", 302, "redirect", {"Location": "https://logs.example.invalid/signed"}, None)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b"logs"
    download = Mock(return_value=response)
    monkeypatch.setattr(r.urllib.request, "urlopen", download)
    assert gh.request("actions/jobs/1/logs") == b"logs"
    assert download.call_args.args == ("https://logs.example.invalid/signed",)
    assert "Authorization" not in download.call_args.kwargs


def test_local_pass_receipt_reused_without_second_regression(runner, monkeypatch):
    runner.state["stages"]["local"] = {"status": "passed", "commit": SHA, "environment": "env"}
    monkeypatch.setattr(runner, "environment_fingerprint", lambda: "env")
    monkeypatch.setattr(runner, "push_candidate", Mock())
    start = Mock(side_effect=AssertionError("must not repeat passed regression"))
    monkeypatch.setattr(runner, "start_local", start)
    runner.validation()
    start.assert_not_called()
    assert runner.state["stages"]["cloud"]["status"] == "passed"


def test_local_and_cloud_validation_overlap(runner, monkeypatch):
    events = []
    process = Mock()
    process.poll.side_effect = [None, 0]
    monkeypatch.setattr(runner, "environment_fingerprint", lambda: "env")
    def start():
        events.append("local_started")
        runner.local_process = process
        runner.local_handle = io.StringIO()
    monkeypatch.setattr(runner, "start_local", start)
    monkeypatch.setattr(runner, "push_candidate", lambda: events.append("cloud_started"))
    monkeypatch.setattr(r.time, "sleep", lambda seconds: None)
    runner.validation()
    assert events == ["local_started", "cloud_started"]
    assert runner.state["stages"]["local"]["status"] == "passed"
    assert runner.state["stages"]["cloud"]["status"] == "passed"


def test_environment_change_invalidates_local_receipt(runner, monkeypatch):
    runner.state["stages"]["local"] = {"status": "passed", "commit": SHA, "environment": "old"}
    monkeypatch.setattr(runner, "environment_fingerprint", lambda: "new")
    def start():raise r.ReleaseError("local", "fresh regression required")
    monkeypatch.setattr(runner, "start_local", start)
    with pytest.raises(r.ReleaseError, match="fresh regression required"):runner.validation()


def test_local_failure_stops_publication_and_reports_test_name(runner, monkeypatch, capsys):
    log = runner.logs / "local-regression.log"
    log.write_text("FAILED src/tests/test_fixture.py::test_case - assertion\n")
    monkeypatch.setattr(runner, "preflight", lambda: None)
    monkeypatch.setattr(runner, "reconcile_commit", lambda: None)
    monkeypatch.setattr(runner, "prepare", lambda: None)
    monkeypatch.setattr(runner, "environment_fingerprint", lambda: "env")
    def start():
        runner.local_process = Mock(poll=Mock(return_value=1))
        runner.local_handle = io.StringIO()
    monkeypatch.setattr(runner, "start_local", start)
    monkeypatch.setattr(runner, "push_candidate", Mock())
    publish = Mock(side_effect=AssertionError("publication is forbidden"))
    monkeypatch.setattr(runner, "git_publish", publish)
    with pytest.raises(r.ReleaseError):runner.run()
    publish.assert_not_called()
    state = json.loads((runner.directory / "state.json").read_text())
    assert state["stages"]["local"]["status"] == "failed"
    assert state["last_error"]["details"]["failed_tests"] == ["FAILED src/tests/test_fixture.py::test_case - assertion"]
    assert "resume 0.34.5-0123456789ab" in capsys.readouterr().err


def test_cloud_failure_preserves_action_log(runner, monkeypatch):
    runner.state["stages"]["local"] = {"status": "passed", "commit": SHA, "environment": "env"}
    runner.github.runs[0]["conclusion"] = "failure"
    runner.github.job_list[0]["conclusion"] = "failure"
    monkeypatch.setattr(runner, "environment_fingerprint", lambda: "env")
    monkeypatch.setattr(runner, "push_candidate", Mock())
    with pytest.raises(r.ReleaseError, match="no publication"):runner.validation()
    assert "useful failure detail" in (runner.logs / "cloud-failure.log").read_text()


def test_restart_is_a_separate_confirmation_gate(runner, monkeypatch):
    monkeypatch.setattr(runner, "assert_candidate", lambda: None)
    monkeypatch.setattr(runner, "service_instance", lambda: {"pid": "1", "start_ticks": "1"})
    with pytest.raises(r.ReleaseError) as caught:runner.restart()
    assert caught.value.code == 2
    assert caught.value.details["resume_option"] == "--approve-restart"
    assert "restart_intent" not in runner.state


def test_already_healthy_runtime_is_not_restarted(runner, monkeypatch):
    monkeypatch.setattr(runner, "assert_candidate", lambda: "env")
    monkeypatch.setattr(runner, "service_instance", lambda: {"pid": "1", "start_ticks": "1"})
    runner.state["stages"]["restart"] = {"status": "passed", "runtime": "runtime", "environment": "env",
                                         "instance": {"pid": "1", "start_ticks": "1"}}
    monkeypatch.setattr(runner, "health", lambda: {"status": "ok", "version": "0.34.5"})
    execute = Mock(side_effect=AssertionError("restart must not repeat"))
    monkeypatch.setattr(runner, "execute", execute)
    runner.restart()
    execute.assert_not_called()


def test_ambiguous_restart_is_reconciled_not_replayed(runner, monkeypatch):
    monkeypatch.setattr(runner, "assert_candidate", lambda: None)
    runner.state["restart_intent"] = {"old_instance": {"pid": "1", "start_ticks": "1"}, "runtime": "runtime", "started_at": 1}
    monkeypatch.setattr(runner, "service_instance", lambda: {"pid": "1", "start_ticks": "1"})
    with pytest.raises(r.ReleaseError, match="inspect service"):runner.restart(True)


def test_runtime_fingerprint_ignores_test_only_changes(runner):
    before = runner.runtime_fingerprint()
    (runner.repo / "src/tests").mkdir()
    (runner.repo / "src/tests/test_sample.py").write_text("assert False\n")
    assert runner.runtime_fingerprint() == before
    (runner.repo / "server.py").write_text("import os\n")
    assert runner.runtime_fingerprint() != before


@pytest.mark.parametrize("name", [
    "vendor/parrot-go-tls-client/transport.go",
    "vendor/parrot-go-tls-client/prebuilt/linux_amd64/libparrot_go_tls_client.so",
    "vendor/parrot-go-tls-client/SOURCE.json",
    "Dockerfile", "scripts/install_go_tls.sh", "scripts/check_go_tls.py",
])
def test_native_and_build_changes_invalidate_runtime_fingerprint(runner, name):
    path = runner.repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"before")
    subprocess.check_call(["git", "add", "--", name], cwd=runner.repo)
    before = runner.runtime_fingerprint()
    path.write_bytes(b"after")
    assert runner.runtime_fingerprint() != before


def test_same_version_native_rebuild_changes_environment_fingerprint(runner, monkeypatch):
    identity = {"version": "0.2.1", "source_sha256": "one", "sha256": "binary-one"}
    monkeypatch.setattr(runner, "native_identity", lambda **kw: dict(identity))
    before = runner.environment_fingerprint()
    identity["sha256"] = "binary-two"
    assert runner.environment_fingerprint() != before


def test_direct_dependency_audit_recognizes_verified_vendor_requirement(runner, monkeypatch):
    package = runner.repo / "vendor/parrot-go-tls-client"
    package.mkdir(parents=True)
    (package / "SOURCE.json").write_text(json.dumps({"package": "parrot-go-tls-client"}))
    (runner.repo / "requirements.txt").write_text('./vendor/parrot-go-tls-client ; sys_platform == "linux"\n')
    (runner.repo / "server.py").write_text("import parrot_go_tls_client\n")
    installed = runner.repo / "installed"
    installed.mkdir()
    (installed / "parrot_go_tls_client.py").write_text("")
    metadata = installed / "parrot_go_tls_client-0.2.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: parrot-go-tls-client\nVersion: 0.2.1\n")
    (metadata / "top_level.txt").write_text("parrot_go_tls_client\n")
    runner.env["PYTHONPATH"] = str(installed)
    calls = []
    monkeypatch.setattr(runner, "native_identity", lambda **kw: calls.append(kw))
    runner.audit_dependencies()
    assert calls == [{"load": True}]


def test_tag_rewrite_requires_exact_remote_object(runner, monkeypatch):
    monkeypatch.setattr(runner, "assert_candidate", lambda: None)
    monkeypatch.setattr(runner, "changes", lambda: [])
    monkeypatch.setattr(runner, "head", lambda: SHA)
    monkeypatch.setattr(runner, "remote_ref", lambda ref: OTHER)
    monkeypatch.setattr(runner, "git", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", ""))
    with pytest.raises(r.ReleaseError) as caught:runner.git_publish()
    assert caught.value.code == 2
    assert caught.value.details["expected_old_object"] == OTHER
    runner.github.release = {"body": NOTES}
    with pytest.raises(r.ReleaseError, match="already has a published"):runner.git_publish(OTHER)


def test_existing_main_and_tag_are_not_republished(runner, monkeypatch):
    monkeypatch.setattr(runner, "assert_candidate", lambda: None)
    calls = []
    monkeypatch.setattr(runner, "changes", lambda: [])
    monkeypatch.setattr(runner, "head", lambda: SHA)
    monkeypatch.setattr(runner, "remote_ref", lambda ref: OTHER if ref.startswith("refs/tags") else SHA)
    monkeypatch.setattr(runner, "tag_annotation", lambda tag: NOTES)
    def git(*args, **kwargs):
        calls.append(args)
        output = SHA if len(args) > 1 and args[1].endswith("^{}") else OTHER
        return subprocess.CompletedProcess(args, 0, output + "\n", "")
    monkeypatch.setattr(runner, "git", git)
    runner.git_publish()
    assert not any(args[0] in ("push", "tag") for args in calls)


def test_publication_verifies_body_and_only_deletes_owned_candidate(runner, monkeypatch):
    runner.github.publish_runs = [{"id": 20, "head_sha": SHA, "head_branch": "v0.34.5",
                                  "status": "completed", "conclusion": "success"}]
    runner.github.release = {"body": NOTES + "\n**Full Changelog**: https://github.com/fake/repo/compare/v0.34.4...v0.34.5", "html_url": "https://github.com/fake/repo/releases/tag/v0.34.5"}
    monkeypatch.setattr(runner, "remote_ref", lambda ref: SHA)
    calls = []
    monkeypatch.setattr(runner, "git", lambda *args, **kw: calls.append(args))
    runner.publication()
    assert runner.state["completed"] is True
    assert calls == [("push", f"--force-with-lease=refs/heads/release-candidate/fixture:{SHA}", "origin", ":refs/heads/release-candidate/fixture")]


def test_publication_does_not_delete_changed_candidate_branch(runner, monkeypatch):
    runner.github.publish_runs = [{"id": 20, "head_sha": SHA, "head_branch": "v0.34.5", "status": "completed", "conclusion": "success"}]
    runner.github.release = {"body": NOTES + "https://github.com/fake/repo/compare/v0.34.4...v0.34.5", "html_url": "url"}
    monkeypatch.setattr(runner, "remote_ref", lambda ref: OTHER)
    with pytest.raises(r.ReleaseError, match="changed externally"):runner.publication()
    assert not runner.state.get("completed")


def test_completed_run_never_repeats_side_effects(runner, monkeypatch):
    runner.state["completed"] = True
    monkeypatch.setattr(runner, "prepare", Mock(side_effect=AssertionError("must not prepare again")))
    runner.run()
    runner.prepare.assert_not_called()


def test_command_timeout_retains_stage_command_and_redacted_output(runner, monkeypatch):
    command = ["git", "push", "origin", "candidate"]
    monkeypatch.setattr(r.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired(
        command, 180, output=b"partial fixture_release_secret output")))
    with pytest.raises(r.ReleaseError) as caught:
        runner.execute("cloud", command)
    assert caught.value.stage == "cloud"
    assert caught.value.details["command"] == command
    assert caught.value.details["exit_code"] is None
    assert "partial [REDACTED] output" in Path(caught.value.details["log"]).read_text()
    assert "fixture_release_secret" not in caught.value.details["tail"]


def test_same_candidate_ci_retry_does_not_repeat_local_regression(runner, monkeypatch):
    runner.state["stages"]["local"] = {"status": "passed", "commit": SHA, "environment": "env"}
    runner.github.runs[0].update(conclusion="failure")
    runner.github.job_list[0]["conclusion"] = "failure"
    monkeypatch.setattr(runner, "environment_fingerprint", lambda: "env")
    monkeypatch.setattr(runner, "push_candidate", Mock())
    start = Mock(side_effect=AssertionError("passed local regression must be reused"))
    monkeypatch.setattr(runner, "start_local", start)
    original = runner.github.request
    def request(path, *, method="GET", data=None):
        result = original(path, method=method, data=data)
        if method == "POST" and path.endswith("/rerun-failed-jobs"):
            runner.github.runs[0].update(conclusion="success", run_attempt=2)
            runner.github.job_list[0]["conclusion"] = "success"
        return result
    monkeypatch.setattr(runner.github, "request", request)
    monkeypatch.setattr(r.time, "sleep", lambda seconds: None)
    runner.validation(retry_ci=True)
    start.assert_not_called()
    assert runner.github.calls.count(("POST", "actions/runs/10/rerun-failed-jobs")) == 1
    assert "retry_intent" not in runner.state


def test_failed_publication_retries_existing_run_without_moving_tag(runner, monkeypatch):
    runner.github.publish_runs = [{"id": 20, "head_sha": SHA, "head_branch": "v0.34.5",
                                  "status": "completed", "conclusion": "failure", "run_attempt": 1}]
    runner.github.release = {"body": NOTES + "https://github.com/fake/repo/compare/v0.34.4...v0.34.5", "html_url": "url"}
    monkeypatch.setattr(runner, "remote_ref", lambda ref: None)
    git = Mock(side_effect=AssertionError("no push or tag should be repeated"))
    monkeypatch.setattr(runner, "git", git)
    original = runner.github.request
    def request(path, *, method="GET", data=None):
        result = original(path, method=method, data=data)
        if method == "POST" and path.endswith("/rerun-failed-jobs"):
            runner.github.publish_runs[0].update(conclusion="success", run_attempt=2)
        return result
    monkeypatch.setattr(runner.github, "request", request)
    monkeypatch.setattr(r.time, "sleep", lambda seconds: None)
    runner.publication(retry_publish=True)
    git.assert_not_called()
    assert runner.github.calls.count(("POST", "actions/runs/20/rerun-failed-jobs")) == 1
    assert runner.state["completed"]


def test_repository_lock_blocks_another_release_process(runner):
    root = runner.repo / ".release-runs"
    with (root / "lock").open("a") as lock:
        r.fcntl.flock(lock.fileno(), r.fcntl.LOCK_EX | r.fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, str(Path(r.__file__).resolve()), "--repo", str(runner.repo),
                                 "status", runner.state["run_id"]], text=True, capture_output=True)
    assert result.returncode == 1
    assert json.loads(result.stderr)["stage"] == "lock"


def test_interrupted_commit_is_adopted_only_if_parent_and_tree_match(runner, monkeypatch):
    runner.state["commit_intent"] = {"parent": OTHER, "tree": "c" * 40}
    monkeypatch.setattr(runner, "head", lambda: SHA)
    monkeypatch.setattr(runner, "changes", lambda: [])
    def git(*args, **kwargs):
        value = OTHER if args[1] == "HEAD^" else "c" * 40
        return subprocess.CompletedProcess(args, 0, value + "\n", "")
    monkeypatch.setattr(runner, "git", git)
    runner.reconcile_commit()
    assert runner.state["candidate"] == SHA
    assert runner.state["stages"] == {"candidate": {"status": "passed", "commit": SHA}}
    assert "commit_intent" not in runner.state


def _image_files(tmp_path):
    repo = tmp_path / "image-repo"; (repo / "src").mkdir(parents=True)
    (repo / "src/__init__.py").write_text('__version__ = "0.34.5"\n')
    images = tmp_path / "images"; images.mkdir()
    for arch, letter in (("amd64", "a"), ("arm64", "b")):
        (images / f"image-{arch}.json").write_text(json.dumps({"architecture": arch, "commit": SHA,
            "version": "0.34.5", "repository": "ghcr.io/fake/parrot", "digest": "sha256:" + letter * 64}))
    tags = tmp_path / "tags.txt"; tags.write_text("ghcr.io/fake/parrot:0.34.5\nghcr.io/fake/parrot:v0.34.5\n")
    return repo, images, tags


def test_both_image_identities_checked_before_registry_write(tmp_path):
    repo, images, tags = _image_files(tmp_path)
    calls = []
    def execute(args, text=True):
        calls.append(args)
        if args[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            if args[-1] == "--raw":
                return json.dumps({"manifests": [{"platform": {"os": "linux", "architecture": arch},
                    "digest": "sha256:" + letter * 64} for arch, letter in (("amd64", "a"), ("arm64", "b"))]})
            arch = "amd64" if args[4].endswith("a" * 64) else "arm64"
            return json.dumps({"architecture": arch, "os": "linux", "config": {"Labels": {
                "org.opencontainers.image.revision": SHA, "org.opencontainers.image.version": "0.34.5"}}})
        return "ok"
    r.ci_publish(repo, images, SHA, "ghcr.io/fake/parrot", tags, execute)
    inspect = [i for i, args in enumerate(calls) if args[-1] == "{{json .Image}}"]
    promote = [i for i, args in enumerate(calls) if args[:4] == ["docker", "buildx", "imagetools", "create"]]
    assert max(inspect) < min(promote)
    assert not any(args[1] in ("push", "load", "build") for args in calls)
    assert calls[-1][-1] == "--raw"


def test_mismatched_artifact_cannot_be_pushed(tmp_path):
    repo, images, tags = _image_files(tmp_path)
    calls = []
    def execute(args, text=True):
        calls.append(args)
        if args[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            return json.dumps({"architecture": "amd64", "config": {"Labels": {"org.opencontainers.image.revision": OTHER}}})
        return "ok"
    with pytest.raises(r.ReleaseError, match="identity mismatch"):
        r.ci_publish(repo, images, SHA, "ghcr.io/fake/parrot", tags, execute)
    assert not any(args[:4] == ["docker", "buildx", "imagetools", "create"] for args in calls)


@pytest.mark.parametrize("tag", ["ghcr.io/other/parrot:0.34.5", "ghcr.io/fake/parrot:0.20.0", "ghcr.io/fake/parrot:x;bad"])
def test_image_destination_must_match_selected_version(tmp_path, tag):
    repo, images, tags = _image_files(tmp_path)
    tags.write_text("ghcr.io/fake/parrot:0.34.5\n" + tag)
    with pytest.raises(r.ReleaseError, match="unrelated version tags"):
        r.ci_publish(repo, images, SHA, "ghcr.io/fake/parrot", tags, Mock())


def test_prepare_freezes_real_git_and_invalidates_changed_candidate(tmp_path, monkeypatch):
    repo = tmp_path / "git-repo"; (repo / "src").mkdir(parents=True)
    (repo / "src/__init__.py").write_text('__version__ = "0.34.4"\n')
    (repo / "server.py").write_text("import json\n")
    (repo / "requirements.txt").write_text("")
    (repo / ".gitignore").write_text(".release-runs/\n")
    def git(*args):return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
    git("init", "-b", "main");git("config", "user.name", "fixture");git("config", "user.email", "fixture@fake.invalid")
    git("add", ".");git("commit", "-m", "fixture base")
    (repo / "server.py").write_text("import json\nimport os\n")
    monkeypatch.setenv("GH_TOKEN", "fixture_release_secret")
    state = {"run_id": "0.34.5-0123456789ab", "repository": "fake/repo", "version": "0.34.5", "notes": NOTES,
             "files": ["server.py"], "python": sys.executable, "stages": {}}
    runner = r.ReleaseRun(repo, repo / ".release-runs" / state["run_id"], state, github=FakeGitHub())
    try:
        runner.prepare()
        first = runner.state["candidate"]
        assert r.read_version(repo) == "0.34.5"
        assert git("status", "--short") == ""
        runner.state["stages"]["local"] = {"status": "passed", "commit": first}
        runner.state["publish_retry_intent"] = {"attempt": 1}
        runner.state["publish_run"] = 123
        runner.prepare()
        assert runner.state["candidate"] == first and runner.state["stages"]["local"]["status"] == "passed"
        (repo / "server.py").write_text("import json\nimport time\n")
        runner.prepare()
        assert runner.state["candidate"] != first
        assert "local" not in runner.state["stages"]
        assert "publish_run" not in runner.state and "publish_retry_intent" not in runner.state
        (repo / "unreviewed.py").write_text("pass\n")
        with pytest.raises(r.ReleaseError, match="Unreviewed"):runner.prepare()
    finally:runner.cleanup()
