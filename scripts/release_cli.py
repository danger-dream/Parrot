"""Command-line adapter for the fixed release engine; no independent execution."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


def main(argv=None, *, api):
    ReleaseError, ReleaseRun, GitHub = api.ReleaseError, api.ReleaseRun, api.GitHub
    release_workers, read_version, validate_notes = api.release_workers, api.read_version, api.validate_notes
    previous_release = api.previous_release
    github_repository, write_json = api.github_repository, api.write_json
    ci_info, ci_verify, ci_record, ci_publish = api.ci_info, api.ci_verify, api.ci_record, api.ci_publish
    CANDIDATE_PREFIX, SHA_RE = api.CANDIDATE_PREFIX, api.SHA_RE
    parser = argparse.ArgumentParser(description=api.__doc__)
    parser.add_argument("--repo", default=str(Path(api.__file__).resolve().parents[1]))
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("version")
    start.add_argument("--notes", required=True)
    start.add_argument("--files", nargs="+", required=True, help="Explicitly reviewed release candidate files")
    start.add_argument("--python", default=None)
    start.add_argument("--workers", type=int, default=release_workers())
    start.add_argument("--service", default="parrot.service")
    start.add_argument("--health-url", default=None)
    start.add_argument("--timeout", type=int, default=1800)
    start.add_argument("--approve-restart", action="store_true", help="Only with prior authorization for this release/service")
    rehearsal = commands.add_parser("rehearse", help="One-off real validation; never update main, version tags, latest or service")
    rehearsal.add_argument("--files", nargs="+", required=True)
    rehearsal.add_argument("--python", default=None)
    rehearsal.add_argument("--workers", type=int, default=release_workers())
    rehearsal.add_argument("--timeout", type=int, default=1800)
    resume = commands.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--approve-restart", action="store_true")
    resume.add_argument("--reconcile-restart", action="store_true", help="After inspecting a failed/uncertain restart; checks systemd has no pending job")
    resume.add_argument("--approve-tag-rewrite")
    resume.add_argument("--retry-ci", action="store_true")
    resume.add_argument("--retry-publish", action="store_true")
    resume.add_argument("--files", nargs="+", help="Explicitly add files corrected after a failure")
    status = commands.add_parser("status");status.add_argument("run_id")
    info = commands.add_parser("ci-info");info.add_argument("--output", required=True)
    verify = commands.add_parser("ci-verify-candidate");verify.add_argument("--commit", required=True);verify.add_argument("--output", required=True)
    publish = commands.add_parser("ci-publish")
    publish.add_argument("--images", required=True);publish.add_argument("--commit", required=True)
    publish.add_argument("--repository", required=True);publish.add_argument("--tags-file")
    publish.add_argument("--rehearsal", action="store_true")
    record = commands.add_parser("ci-record")
    record.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    record.add_argument("--digest", required=True);record.add_argument("--repository", required=True)
    record.add_argument("--output", required=True)
    cleanup = commands.add_parser("ci-cleanup-rehearsal")
    cleanup.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.command == "ci-cleanup-rehearsal":
            branch = os.environ.get("GITHUB_REF_NAME", "")
            if not branch.startswith(CANDIDATE_PREFIX + "rehearsal-") or args.commit != os.environ.get("GITHUB_SHA") or not SHA_RE.fullmatch(args.commit):
                raise ReleaseError("cleanup", "Cleanup only accepts this workflow's rehearsal commit")
            repository = github_repository(subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=repo, text=True))
            gh = GitHub(repository, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
            info = gh.request("") or {}
            with tempfile.TemporaryDirectory(prefix="parrot-rehearsal-cleanup-") as tmp:
                runner = ReleaseRun(repo, tmp, {"repository": repository, "candidate": args.commit, "branch": branch,
                    "owner_type": info.get("owner", {}).get("type", "User")}, github=gh)
                try:
                    runner.cleanup_rehearsal_registry()
                    print("Deleted owned rehearsal versions:", runner.state.get("deleted_rehearsal_versions", []))
                finally:
                    runner.cleanup()
            return 0
        if args.command == "ci-info":ci_info(repo, args.output);return 0
        if args.command == "ci-verify-candidate":ci_verify(repo, args.commit, args.output);return 0
        if args.command == "ci-record":ci_record(repo, args.architecture, args.digest, args.repository, args.output);return 0
        if args.command == "ci-publish":
            if not args.rehearsal and not args.tags_file:
                raise ReleaseError("publication", "Formal publication requires --tags-file")
            ci_publish(repo, args.images, args.commit, args.repository, args.tags_file, rehearsal=args.rehearsal);return 0
        root = repo / ".release-runs";root.mkdir(exist_ok=True, mode=0o700)
        with (root / "lock").open("a") as lock:
            try:fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:raise ReleaseError("lock", "Another release process owns this repository")
            if args.command in ("start", "rehearse"):
                old = read_version(repo)
                is_rehearsal = args.command == "rehearse"
                notes = "" if is_rehearsal else Path(args.notes).read_text().rstrip() + "\n"
                version = old if is_rehearsal else args.version
                repository = github_repository(subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=repo, text=True))
                if not is_rehearsal:
                    # A bumped but unpublished local version is not the span GitHub links to.
                    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                    base = previous_release(GitHub(repository, token), version)
                    validate_notes(notes, old, version, base=base)
                    old = base
                if args.workers < 1 or args.timeout < 1:
                    raise ReleaseError("candidate", "Worker count and timeout must be positive")
                run_id = f'{"rehearsal" if is_rehearsal else version}-{uuid.uuid4().hex[:12]}'
                python = args.python or str(repo / "venv/bin/python")
                if not Path(python).is_file():raise ReleaseError("candidate", "Use the actual online Python interpreter via --python")
                health_url = getattr(args, "health_url", None)
                if not is_rehearsal and not health_url:
                    cfg = json.loads((repo / "config.json").read_text())
                    health_url = f'http://127.0.0.1:{int(cfg["listen"]["port"])}/health'
                state = {"schema": 1, "run_id": run_id, "repository": repository, "old_version": old,
                         "mode": "rehearsal" if is_rehearsal else "release",
                         "version": version, "notes": notes, "files": args.files, "python": str(Path(python).absolute()),
                         "workers": args.workers, "timeout": args.timeout, "service": getattr(args, "service", None), "health_url": health_url,
                         "branch": CANDIDATE_PREFIX + run_id, "created_at": time.time(), "stages": {}}
                directory = root / run_id;write_json(directory / "state.json", state)
                print("Release run:", run_id, flush=True)
            else:
                if not re.fullmatch(r"(?:\d+\.\d+\.\d+|rehearsal)-[0-9a-f]{12}", args.run_id):
                    raise ReleaseError("state", "Invalid release run ID")
                directory = root / args.run_id
                state = json.loads((directory / "state.json").read_text())
                if state.get("schema") != 1 or state.get("run_id") != args.run_id:
                    raise ReleaseError("state", "Unsupported or mismatched run state")
                if args.command == "status":print(json.dumps(state, ensure_ascii=False, indent=2));return 0
                if args.files:
                    state["files"] = sorted(set(state["files"]) | set(args.files))
                    write_json(directory / "state.json", state)
            runner = ReleaseRun(repo, directory, state)
            runner.run(approve_restart=getattr(args, "approve_restart", False),
                       approve_tag=getattr(args, "approve_tag_rewrite", None), retry_ci=getattr(args, "retry_ci", False),
                       retry_publish=getattr(args, "retry_publish", False),
                       reconcile_restart=getattr(args, "reconcile_restart", False))
            return 0
    except ReleaseError as exc:
        if args.command.startswith("ci-") or exc.stage in ("state", "lock", "candidate"):
            print(json.dumps({"stage": exc.stage, "error": str(exc), "details": exc.details}, ensure_ascii=False), file=sys.stderr)
        return exc.code
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"stage": "preflight", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1

