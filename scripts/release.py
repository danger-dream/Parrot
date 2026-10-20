#!/usr/bin/env python3
"""Fixed, resumable Parrot release entrypoint. Uses only the Python standard library."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


PREPARE_WORKFLOW = "release-prepare.yml"
PUBLISH_WORKFLOW = "docker-publish.yml"
CANDIDATE_PREFIX = "release-candidate/"
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
VERSION_RE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
STAGES = ("candidate", "local", "cloud", "restart", "git_publish", "publication", "cleanup")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReleaseError(Exception):
    def __init__(self, stage, message, *, code=1, details=None):
        super().__init__(message)
        self.stage, self.code, self.details = stage, code, details or {}


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_version(repo):
    text = (Path(repo) / "src/__init__.py").read_text()
    match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]\s*$', text, re.M)
    if not match or not VERSION_RE.fullmatch(match[1]):
        raise ReleaseError("candidate", "Cannot read a stable semantic version from src/__init__.py")
    return match[1]


def previous_release(github, new):
    """Highest public release below ``new``; GitHub compares generated notes against it."""
    if not VERSION_RE.fullmatch(new):
        raise ReleaseError("candidate", f"Invalid release version: {new}")
    target, earlier, page, releases = tuple(map(int, new.split("."))), [], 0, None
    while releases is None or len(releases) == 100:
        page += 1
        try:
            releases = github.request(f"releases?per_page=100&page={page}") or []
        except ReleaseError as exc:
            raise ReleaseError("candidate", f"Cannot list published releases: {exc}", details=exc.details) from exc
        tags = [str(item.get("tag_name") or "") for item in releases if not item.get("draft") and not item.get("prerelease")]
        versions = [tuple(map(int, tag[1:].split("."))) for tag in tags if tag.startswith("v") and VERSION_RE.fullmatch(tag[1:])]
        earlier += [value for value in versions if value < target]
    if not earlier:
        raise ReleaseError("candidate", "No published release precedes this version")
    return ".".join(map(str, max(earlier)))


def validate_notes(notes, old, new, *, base=None):
    if not VERSION_RE.fullmatch(new) or tuple(map(int, new.split('.'))) <= tuple(map(int, old.split('.'))):
        raise ReleaseError("candidate", f"Version must advance from {old}: {new}")
    base = base or old
    if len(notes.splitlines()) < 5 or f"v{base}" not in notes or f"v{new}" not in notes:
        raise ReleaseError("candidate", "Notes must explain user value, version span, and grouped changes")
    if "Full Changelog" in notes:
        raise ReleaseError("candidate", "Do not handwrite Full Changelog; GitHub generates it")


def github_repository(url):
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", url.strip())
    if not match:
        raise ReleaseError("candidate", "origin must identify a GitHub repository without embedded credentials")
    return match[1]


def release_workers():
    try:
        cpus = len(os.sched_getaffinity(0))
    except (OSError, AttributeError):
        cpus = os.cpu_count() or 1
    return max(1, min(8, cpus))


def process_start_ticks(pid):
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text()
        return text[text.rfind(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = handle.name
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, repository, token):
        self.repository, self.token = repository, token
        self.base = "https://api.github.com/repos/" + repository + "/"
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, *, method="GET", data=None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "Parrot-release",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        payload = None if data is None else json.dumps(data).encode()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        url = "https://api.github.com" + path if path.startswith("/") else (self.base + path if path else self.base.rstrip('/'))
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "GET":
                return None
            if exc.code in (301, 302, 303, 307, 308) and method == "GET":
                url = exc.headers.get("Location", "")
                if not url.startswith("https://"):
                    raise ReleaseError("cloud", "Invalid GitHub download redirect") from exc
                # Signed log downloads receive no GitHub credential headers.
                with urllib.request.urlopen(url, timeout=60) as response:
                    return response.read()
            raise ReleaseError("cloud", f"GitHub {method} {path} returned HTTP {exc.code}",
                               details={"http_status": exc.code, "request_rejected": 400 <= exc.code < 500}) from exc
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ReleaseError("cloud", f"GitHub request failed ({type(exc).__name__}); reconcile remote state before retrying") from exc

    def prepare_runs(self, commit, branch=None):
        value = self.request(f"actions/workflows/{PREPARE_WORKFLOW}/runs?head_sha={commit}&per_page=100") or {}
        return [run for run in value.get("workflow_runs", [])
                if run.get("head_sha") == commit and run.get("event") == "push"
                and run.get("head_branch", "").startswith(CANDIDATE_PREFIX)
                and (branch is None or run.get("head_branch") == branch)]

    def artifacts(self, run_id):
        value = self.request(f"actions/runs/{run_id}/artifacts?per_page=100") or {}
        return value.get("artifacts", [])

    def jobs(self, run_id):
        value = self.request(f"actions/runs/{run_id}/jobs?per_page=100") or {}
        return value.get("jobs", [])

    def verified_candidate(self, commit):
        if not SHA_RE.fullmatch(commit):
            raise ReleaseError("cloud", "Expected a full candidate commit SHA")
        for run in self.prepare_runs(commit):
            if run.get("status") == "completed" and run.get("conclusion") == "success":
                verify_jobs(self.jobs(run["id"]))
                artifacts = self.artifacts(run["id"])
                for arch in ("amd64", "arm64"):
                    name = f"parrot-candidate-{commit}-{arch}"
                    if not any(a.get("name") == name and not a.get("expired") for a in artifacts):
                        raise ReleaseError("cloud", f"Missing or expired candidate receipt: {name}; rebuild the candidate, not the publication")
                return run
        raise ReleaseError("cloud", f"No successful candidate quality/build workflow for {commit}")


def verify_jobs(jobs):
    quality = [job for job in jobs if job.get("name") == "quality"]
    builds = [job for job in jobs if job.get("name", "").startswith("build")]
    if len(quality) != 1 or quality[0].get("conclusion") != "success":
        raise ReleaseError("cloud", "Candidate quality gate is not successful")
    for architecture in ("amd64", "arm64"):
        selected = [job for job in builds if re.search(rf'\b{architecture}\b', job.get("name", ""))]
        if len(selected) != 1 or selected[0].get("conclusion") != "success":
            raise ReleaseError("cloud", f"Candidate {architecture} build is not successful")


def ci_info(repo, output):
    version = read_version(repo)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if not SHA_RE.fullmatch(sha):
        raise ReleaseError("candidate", "Invalid checkout commit")
    with Path(output).open("a") as handle:
        handle.write(f"version={version}\ncommit={sha}\n")


def ci_verify(repo, commit, output):
    repository = os.environ.get("GITHUB_REPOSITORY") or github_repository(
        subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=repo, text=True))
    gh = GitHub(repository, os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if checkout != commit:
        raise ReleaseError("cloud", "Checkout and requested artifact commit differ")
    run = gh.verified_candidate(commit)
    with Path(output).open("a") as handle:
        handle.write(f"run_id={run['id']}\nversion={read_version(repo)}\ncommit={commit}\n")


def ci_record(repo, architecture, image_digest, repository, output):
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    if (architecture not in ("amd64", "arm64") or not DIGEST_RE.fullmatch(image_digest)
            or not SHA_RE.fullmatch(commit) or not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/parrot", repository)):
        raise ReleaseError("candidate", "Invalid candidate image receipt")
    write_json(output, {"commit": commit, "version": read_version(repo), "architecture": architecture,
                        "repository": repository, "digest": image_digest})


def ci_publish(repo, images, commit, repository, tags_file, execute=subprocess.check_output, *, rehearsal=False):
    if not SHA_RE.fullmatch(commit) or not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/parrot", repository):
        raise ReleaseError("publication", "Invalid image repository or candidate SHA")
    version = read_version(repo)
    if rehearsal:
        # The rehearsal interface cannot accept any official version or alias.
        tags = [f"{repository}:rehearsal-{commit}"]
    else:
        tags = list(dict.fromkeys(Path(tags_file).read_text().split()))
        allowed = {version, "v" + version, ".".join(version.split('.')[:2]), version.split('.')[0], "latest", "sha-" + commit[:7]}
        if (not tags or repository + ":" + version not in tags
                or any(tag.rsplit(':', 1)[0] != repository or tag.rsplit(':', 1)[1] not in allowed for tag in tags)):
            raise ReleaseError("publication", "Image metadata contains invalid, foreign, or unrelated version tags")
    sources = []
    for architecture in ("amd64", "arm64"):
        path = Path(images) / f"image-{architecture}.json"
        if not path.is_file():
            raise ReleaseError("publication", f"Missing candidate digest receipt: {path.name}")
        receipt = json.loads(path.read_text())
        if (receipt.get("commit") != commit or receipt.get("version") != version
                or receipt.get("architecture") != architecture or receipt.get("repository") != repository
                or not DIGEST_RE.fullmatch(receipt.get("digest", ""))):
            raise ReleaseError("publication", f"Candidate receipt mismatch: {architecture}")
        reference = repository + "@" + receipt["digest"]
        image = json.loads(execute(["docker", "buildx", "imagetools", "inspect", reference,
                                   "--format", "{{json .Image}}"], text=True))
        labels = image.get("config", {}).get("Labels") or {}
        if (image.get("architecture") != architecture or image.get("os") != "linux"
                or labels.get("org.opencontainers.image.revision") != commit
                or labels.get("org.opencontainers.image.version") != version):
            raise ReleaseError("publication", f"Artifact identity mismatch: {architecture}")
        sources.append(reference)
    # Candidate layers are already uploaded. Only publish a manifest after BOTH
    # immutable digests, platforms and source labels have been verified.
    command = ["docker", "buildx", "imagetools", "create"]
    for tag in tags:
        command += ["--tag", tag]
    execute(command + sources, text=True)
    for tag in tags:
        manifest = json.loads(execute(["docker", "buildx", "imagetools", "inspect", tag, "--raw"], text=True))
        actual = {(entry.get("platform", {}).get("architecture"), entry.get("digest"))
                  for entry in manifest.get("manifests", []) if entry.get("platform", {}).get("os") == "linux"}
        expected = {(arch, ref.split("@", 1)[1]) for arch, ref in zip(("amd64", "arm64"), sources)}
        if actual != expected:
            raise ReleaseError("publication", f"Published manifest differs from verified candidates: {tag}")


class ReleaseRun:
    def __init__(self, repo, directory, state, *, github=None):
        self.repo, self.directory, self.state = Path(repo).resolve(), Path(directory).resolve(), state
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs = self.directory / "logs"
        self.logs.mkdir(exist_ok=True)
        self.token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.github = github or GitHub(state["repository"], self.token)
        self.local_process = None
        self.local_handle = None
        self.work = self.directory / "work"
        self.auth_directory = tempfile.TemporaryDirectory(prefix="parrot-release-auth-")
        self.askpass = Path(self.auth_directory.name) / "askpass.sh"
        self.askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" x-access-token ;; *) printf "%s\\n" "$PARROT_RELEASE_TOKEN" ;; esac\n')
        self.askpass.chmod(0o700)
        self.env = {**os.environ, "GIT_ASKPASS": str(self.askpass), "GIT_TERMINAL_PROMPT": "0",
                    "PARROT_RELEASE_TOKEN": self.token or ""}

    def save(self):
        self.state["updated_at"] = time.time()
        write_json(self.directory / "state.json", self.state)

    def mark(self, stage, status, **values):
        previous = self.state.setdefault("stages", {}).get(stage, {})
        now = time.time()
        started = now if status == "running" else previous.get("started_at", now)
        self.state["stages"][stage] = {"status": status, "started_at": started,
            **({"finished_at": now, "seconds": round(now - started, 3)} if status != "running" else {}), **values}
        self.save()
        if previous.get("status") != status:
            print(f"[{stage}] {status}", flush=True)

    def report(self):
        now = time.time()
        invocations = self.state.get("invocations", [])
        active = sum(i.get("finished_at", now) - i["started_at"] for i in invocations)
        elapsed = now - self.state.get("created_at", now)
        value = {"run_id": self.state["run_id"], "mode": self.state.get("mode", "release"),
                 "candidate": self.state.get("candidate"), "completed": self.state.get("completed", False),
                 "elapsed_seconds": round(elapsed, 3), "active_seconds": round(active, 3),
                 "outside_process_seconds": round(max(0, elapsed - active), 3),
                 "candidate_prepare_seconds": self.state.get("candidate_prepare_seconds"),
                 "stages": self.state.get("stages", {}), "cloud_jobs": self.state.get("cloud_jobs", [])}
        write_json(self.directory / "timings.json", value)
        print(f"Timing report: {self.directory / 'timings.json'} (active={active:.1f}s)", flush=True)
        return value

    def redact(self, text):
        return str(text).replace(self.token, "[REDACTED]") if self.token else str(text)

    def execute(self, stage, args, *, cwd=None, check=True):
        path = self.logs / f"{stage}.log"
        with path.open("a") as handle:
            handle.write(self.redact(f"$ {args!r}\n"))
        try:
            result = subprocess.run(args, cwd=cwd or self.repo, env=self.env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180)
        except (subprocess.TimeoutExpired, OSError) as exc:
            output = getattr(exc, "stdout", "") or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            with path.open("a") as handle:
                handle.write(self.redact(f"{output}\n{type(exc).__name__}: {exc}\n"))
            raise ReleaseError(stage, f"Command failed ({type(exc).__name__}); reconcile effects before retrying",
                               details={"command": args, "exit_code": None, "log": str(path),
                                        "tail": self.redact(str(output)[-6000:])}) from exc
        with path.open("a") as handle:
            handle.write(self.redact(f"{result.stdout}\n"))
        if check and result.returncode:
            raise ReleaseError(stage, f"Command exited {result.returncode}", details={
                "command": args, "exit_code": result.returncode, "log": str(path),
                "tail": self.redact(result.stdout[-6000:])})
        return result

    def git(self, *args, stage="candidate", check=True):
        return self.execute(stage, ["git", "-c", "credential.helper=", *args], check=check)

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def changes(self):
        raw = self.git("status", "--porcelain=v1", "--untracked-files=all", "-z").stdout
        values, index, result = raw.split("\0"), 0, []
        while index < len(values):
            item = values[index]
            index += 1
            if not item:
                continue
            result.append(item[3:])
            if "R" in item[:2] or "C" in item[:2]:
                result.append(values[index]); index += 1
        return sorted(set(result))

    def native_identity(self, *, load=False):
        if not (self.repo / "vendor/parrot-go-tls-client/SOURCE.json").is_file():
            return None
        command = [self.state["python"], str(self.repo / "scripts/check_go_tls.py"), "--root", str(self.repo)]
        if load:
            command.append("--load")
        value = self.execute("native", command).stdout.strip()
        return json.loads(value)

    def environment_fingerprint(self):
        code = ('import importlib.metadata as m,json,sys;print(json.dumps({'
                '"python":sys.version,"packages":sorted((d.metadata["Name"],d.version) for d in m.distributions())}))')
        value = self.execute("environment", [self.state["python"], "-c", code]).stdout.strip()
        return digest({"python_environment": json.loads(value), "native": self.native_identity()})

    def runtime_fingerprint(self):
        # Runtime resources include protocol JSON and prompt TXT, not only Python.
        names = self.git("ls-files", "-z", "--", "server.py", "src", "requirements.txt", "docker-entrypoint.sh",
                         "Dockerfile", ".dockerignore", "vendor/parrot-go-tls-client",
                         "scripts/build_go_tls.py", "scripts/check_go_tls.py", "scripts/install_go_tls.sh",
                         "scripts/sync_go_tls_vendor.py").stdout.split("\0")
        paths = [Path(name) for name in names if name and "tests" not in Path(name).parts]
        return digest([(str(path), digest((self.repo / path).read_bytes()) if (self.repo / path).is_file() else None)
                       for path in sorted(paths)])

    def assert_candidate(self):
        if self.state.get("mode") == "rehearsal":
            raise ReleaseError("candidate", "A rehearsal can never restart or publish the online release")
        if self.changes() or self.head() != self.state["candidate"]:
            raise ReleaseError("candidate", "Source changed after validation; resume to freeze and retest the corrected candidate")
        if self.runtime_fingerprint() != self.state["runtime_fingerprint"]:
            raise ReleaseError("candidate", "Deployment content differs from the validated candidate")
        receipt = self.state.get("stages", {}).get("local", {})
        if receipt.get("status") != "passed" or receipt.get("commit") != self.state["candidate"]:
            raise ReleaseError("candidate", "The current candidate lacks a passed local regression")
        environment = self.environment_fingerprint()
        if receipt.get("environment") != environment:
            raise ReleaseError("candidate", "Runtime dependencies changed after validation; resume to retest")
        return environment

    def preflight(self):
        if self.github.repository != self.state["repository"]:
            raise ReleaseError("candidate", "Repository identity mismatch")
        origin = github_repository(self.git("remote", "get-url", "origin").stdout)
        if origin != self.state["repository"]:
            raise ReleaseError("candidate", "origin changed since this release was created")
        self.execute("preflight", [self.state["python"], "-c", "import pytest, pytest_asyncio, xdist; import sys; print(sys.version)"])
        value = self.github.request("") or {}
        if not value.get("permissions", {}).get("push"):
            raise ReleaseError("candidate", "GitHub token cannot push the selected repository")
        self.state["owner_type"] = value.get("owner", {}).get("type", "User")
        self.mark("preflight", "passed")

    def prepare_rehearsal(self):
        allowed = set(self.state["files"])
        changed = set(self.changes())
        if changed - allowed:
            raise ReleaseError("candidate", "Unreviewed files changed; add them explicitly to --files", details={"files": sorted(changed - allowed)})
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in allowed):
            raise ReleaseError("candidate", "Rehearsal files must be repository-relative")
        # An isolated index and commit-tree never change main, the real index or version.
        temporary = self.directory / "candidate.index"
        temporary.unlink(missing_ok=True)
        old_env = self.env
        self.env = {**old_env, "GIT_INDEX_FILE": str(temporary)}
        try:
            self.git("read-tree", "HEAD")
            self.git("add", "--", *sorted(allowed))
            hook = self.repo / ".githooks/pre-commit"
            if hook.is_file():
                self.execute("candidate", ["bash", str(hook)])
            tree = self.git("write-tree").stdout.strip()
            if tree == self.state.get("candidate_tree") and self.head() == self.state.get("base_head"):
                return
            self.audit_dependencies()
            base = self.head()
            commit = self.git("commit-tree", tree, "-p", self.state.get("candidate", base), "-m", "Release rehearsal " + self.state["run_id"]).stdout.strip()
        finally:
            self.env = old_env
            temporary.unlink(missing_ok=True)
        self.state.update(candidate=commit, candidate_tree=tree, base_head=base, stages={}, version=read_version(self.repo))
        for key in ("retry_intent", "publish_retry_intent", "prepare_run", "publish_run"):
            self.state.pop(key, None)
        self.mark("candidate", "passed", commit=commit, tree=tree)

    def audit_dependencies(self):
        # Inspect the frozen runtime tree; hidden imports are covered by clean CI execution.
        code = '''import ast,importlib.metadata as m,json,re,sys
from pathlib import Path
roots=set()
for path in [Path("server.py"),*Path("src").rglob("*.py")]:
 if "tests" in path.parts:continue
 for node in ast.walk(ast.parse(path.read_text(),filename=str(path))):
  if isinstance(node,ast.Import):roots.update(a.name.split(".")[0] for a in node.names)
  elif isinstance(node,ast.ImportFrom) and node.level==0 and node.module:roots.add(node.module.split(".")[0])
declared=set()
for line in Path("requirements.txt").read_text().splitlines():
 raw=line.strip()
 if not raw or raw.startswith("#"):continue
 target=raw.split(";",1)[0].strip()
 if target=="./vendor/parrot-go-tls-client":
  receipt=json.loads(Path(target,"SOURCE.json").read_text())
  if receipt.get("package")!="parrot-go-tls-client":raise ValueError("invalid native package receipt")
  declared.add(receipt["package"])
 else:declared.add(re.split(r"[<>=!~\\[\\s]",raw,maxsplit=1)[0].lower().replace("_","-"))
packages=m.packages_distributions()
missing=[name for name in sorted(roots-set(sys.stdlib_module_names)-{"src","server"}) if not any(d.lower().replace("_","-") in declared for d in packages.get(name,[]))]
print(json.dumps({"missing_direct_dependencies":missing}))
sys.exit(bool(missing))'''
        self.execute("dependencies", [self.state["python"], "-c", code])
        self.native_identity(load=True)

    def prepare(self):
        if self.state.get("mode") == "rehearsal":
            return self.prepare_rehearsal()
        if self.git("branch", "--show-current").stdout.strip() != "main":
            raise ReleaseError("candidate", "Releases must start from the main development branch")
        if not self.state.get("candidate") and self.github.request("releases/tags/v" + self.state["version"]) is not None:
            raise ReleaseError("candidate", "This version is already published; use a new version")
        changed = self.changes()
        prior = self.state.get("candidate")
        current = self.head()
        if prior and not changed and current == prior:
            return
        if prior and current != prior:
            raise ReleaseError("candidate", "HEAD moved outside this release; do not silently adopt unrelated commits")
        allowed = set(self.state["files"]) | {"src/__init__.py"}
        if set(changed) - allowed:
            raise ReleaseError("candidate", "Unreviewed files changed; add them explicitly to --files", details={"files": sorted(set(changed) - allowed)})
        if any(Path(name).is_absolute() or ".." in Path(name).parts or name in {"config.json", ".env"}
               or name.endswith((".db", ".db-wal", ".db-shm")) for name in allowed):
            raise ReleaseError("candidate", "Candidate list contains unsafe paths or runtime data")
        source = self.repo / "src/__init__.py"
        text = source.read_text()
        if not prior:
            text, count = re.subn(r'^(__version__\s*=\s*)[\'"][^\'"]+[\'"]',
                                 rf'\g<1>"{self.state["version"]}"', text, count=1, flags=re.M)
            if count != 1:
                raise ReleaseError("candidate", "Cannot bump the authoritative version")
            source.write_text(text)
        if read_version(self.repo) != self.state["version"]:
            raise ReleaseError("candidate", "Version was changed outside this release")
        self.audit_dependencies()
        self.git("add", "--", *sorted(allowed))
        hook = self.repo / ".githooks/pre-commit"
        configured = self.git("config", "--get", "core.hooksPath", check=False).stdout.strip()
        if hook.is_file() and configured != ".githooks":
            self.execute("candidate", ["bash", str(hook)])
        staged = set(self.git("diff", "--cached", "--name-only").stdout.splitlines())
        if staged - allowed:
            raise ReleaseError("candidate", "Staging area contains unrelated changes")
        message = self.directory / "commit-message.txt"
        message.write_text(f'v{self.state["version"]}: {self.state["notes"].splitlines()[0]}\n\n{self.state["notes"]}')
        if not staged:
            raise ReleaseError("candidate", "No candidate changes to commit")
        # Persist the intended tree before commit; an interrupted commit is never blindly replayed.
        self.state["commit_intent"] = {"parent": current, "tree": self.git("write-tree").stdout.strip()}
        self.save()
        self.git("commit", "--cleanup=verbatim", "-F", str(message))
        self.state.pop("commit_intent", None)
        previous_restart = self.state.get("stages", {}).get("restart")
        self.state["candidate"] = self.head()
        self.state["runtime_fingerprint"] = self.runtime_fingerprint()
        self.state["stages"] = {}
        if (previous_restart and previous_restart.get("status") == "passed"
                and previous_restart.get("runtime") == self.state["runtime_fingerprint"]):
            self.state["stages"]["restart"] = previous_restart
        self.state.pop("prepare_run", None)
        self.state.pop("retry_intent", None)
        self.state.pop("publish_retry_intent", None)
        self.state.pop("publish_run", None)
        self.mark("candidate", "passed", commit=self.state["candidate"])

    def reconcile_commit(self):
        intent = self.state.get("commit_intent")
        if not intent:
            return
        head = self.head()
        if head == intent["parent"]:
            self.state.pop("commit_intent", None); self.save(); return
        parent = self.git("rev-parse", "HEAD^").stdout.strip()
        tree = self.git("rev-parse", "HEAD^{tree}").stdout.strip()
        if parent != intent["parent"] or tree != intent["tree"] or self.changes():
            raise ReleaseError("candidate", "Interrupted commit cannot be reconciled automatically")
        self.state["candidate"] = head
        self.state["runtime_fingerprint"] = self.runtime_fingerprint()
        self.state["stages"] = {"candidate": {"status": "passed", "commit": head}}
        self.state.pop("commit_intent", None); self.save()

    def remote_ref(self, ref):
        value = self.git("ls-remote", "origin", ref, stage="remote").stdout.split()
        return value[0] if value else None

    def push_candidate(self):
        branch = self.state["branch"]
        if self.remote_ref("refs/heads/" + branch) == self.state["candidate"]:
            return
        self.git("push", "origin", f'{self.state["candidate"]}:refs/heads/{branch}', stage="cloud")

    def start_local(self):
        self.reap_local()
        if self.work.exists():
            if self.work.is_symlink():
                raise ReleaseError("local", "Refusing a symlinked candidate workspace")
            shutil.rmtree(self.work)
        self.work.mkdir(exist_ok=True)
        source = self.work / "source"
        self.execute("snapshot", ["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(self.repo), str(source)])
        self.execute("snapshot", ["git", "fetch", "--quiet", str(self.repo), self.state["candidate"]], cwd=source)
        self.execute("snapshot", ["git", "checkout", "--quiet", self.state["candidate"]], cwd=source)
        path = self.logs / "local-regression.log"
        self.local_handle = path.open("w")
        environment = dict(os.environ)
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "PARROT_RELEASE_TOKEN", "GIT_ASKPASS"):
            environment.pop(key, None)
        self.local_process = subprocess.Popen([
            self.state["python"], str(source / "src/tests/isolated_pytest.py"),
            "-q", "-ra", "-n", str(self.state["workers"]), "--dist=worksteal", "src/tests",
        ], cwd=source, env=environment, stdout=self.local_handle, stderr=subprocess.STDOUT, start_new_session=True)
        self.state["local_intent"] = {"pid": self.local_process.pid, "start_ticks": process_start_ticks(self.local_process.pid)}
        self.mark("local", "running", commit=self.state["candidate"], log=str(path))

    def validation(self, retry_ci=False):
        environment = self.environment_fingerprint()
        receipt = self.state.get("stages", {}).get("local", {})
        local_passed = (receipt.get("status") == "passed" and receipt.get("commit") == self.state["candidate"]
                        and receipt.get("environment") == environment)
        if not local_passed:
            self.start_local()
        self.push_candidate()
        if self.state.get("stages", {}).get("cloud", {}).get("status") != "passed":
            self.mark("cloud", "running", commit=self.state["candidate"])
        deadline = time.monotonic() + self.state["timeout"]
        while time.monotonic() < deadline:
            if self.local_process:
                code = self.local_process.poll()
                if code is not None:
                    self.local_handle.close(); self.local_handle = None
                    self.local_process = None
                    self.state.pop("local_intent", None)
                    if code:
                        log = self.logs / "local-regression.log"
                        text = log.read_text(errors="replace")
                        raise ReleaseError("local", f"Local complete regression failed ({code})", details={
                            "exit_code": code, "log": str(log), "failed_tests": re.findall(r'^FAILED .+$', text, re.M), "tail": text[-8000:]})
                    local_passed = True
                    self.mark("local", "passed", commit=self.state["candidate"], environment=environment,
                              log=str(self.logs / "local-regression.log"))
            runs = self.github.prepare_runs(self.state["candidate"], self.state["branch"])
            if runs:
                run = runs[0]
                self.state["prepare_run"] = run["id"]; self.save()
                retry = self.state.get("retry_intent")
                if retry and run.get("run_attempt", 1) <= retry["attempt"]:
                    if time.time() - retry.get("requested_at", time.time()) > 60:
                        if retry_ci and run.get("status") == "completed":
                            self.state.pop("retry_intent", None); self.save()
                        else:
                            raise ReleaseError("cloud", "Retry has not started; inspect the completed run then resume with --retry-ci", code=2)
                    else:
                        time.sleep(3); continue
                if retry:
                    self.state.pop("retry_intent", None); self.save()
                jobs = self.github.jobs(run["id"])
                failed = [job for job in jobs if job.get("conclusion") in ("failure", "cancelled", "timed_out")]
                if failed or (run.get("status") == "completed" and run.get("conclusion") != "success"):
                    if retry_ci and run.get("status") == "completed":
                        self.request_retry("retry_intent", run)
                        retry_ci = False
                        time.sleep(3); continue
                    log = self.logs / "cloud-failure.log"
                    for job in failed:
                        try:
                            body = self.github.request(f'actions/jobs/{job["id"]}/logs')
                            if isinstance(body, bytes):
                                with log.open("a") as handle:handle.write(self.redact(body.decode(errors="replace")))
                        except ReleaseError:
                            pass
                    raise ReleaseError("cloud", "Candidate quality/build failed; no publication was attempted", details={
                        "run_id": run["id"], "url": run.get("html_url"), "failed_jobs": [job.get("name") for job in failed],
                        "log": str(log) if log.exists() else None})
                if run.get("status") == "completed" and run.get("conclusion") == "success":
                    verify_jobs(jobs)
                    if self.state.get("mode") == "rehearsal":
                        promotion = [j for j in jobs if j.get("name") == "rehearsal"]
                        cleanup = [j for j in jobs if j.get("name") == "rehearsal-cleanup"]
                        if len(promotion) != 1 or promotion[0].get("conclusion") != "success":
                            raise ReleaseError("cloud", "Rehearsal manifest promotion did not pass")
                        if len(cleanup) != 1 or cleanup[0].get("conclusion") != "success":
                            raise ReleaseError("cleanup", "Rehearsal registry cleanup did not pass")
                    self.state["cloud_jobs"] = jobs
                    self.mark("cloud", "passed", commit=self.state["candidate"], run_id=run["id"], url=run.get("html_url"))
                    if local_passed:
                        return
            time.sleep(3)
        raise ReleaseError("validation", "Validation deadline exceeded; receipts are preserved, do not republish blindly")

    def request_retry(self, key, run):
        self.state[key] = {"attempt": run.get("run_attempt", 1), "run_id": run["id"], "requested_at": time.time()}
        self.save()
        try:
            self.github.request(f'actions/runs/{run["id"]}/rerun-failed-jobs', method="POST", data={})
        except ReleaseError as exc:
            if exc.details.get("request_rejected"):
                self.state.pop(key, None); self.save()
            raise

    def health(self):
        with urllib.request.urlopen(self.state["health_url"], timeout=5) as response:
            return json.load(response)

    def service_pid(self):
        return self.execute("restart", ["systemctl", "show", self.state["service"], "-p", "MainPID", "--value"]).stdout.strip()

    def service_instance(self):
        pid = self.service_pid()
        return {"pid": pid, "start_ticks": process_start_ticks(pid)}

    def restart(self, approved=False, reconcile=False):
        environment = self.assert_candidate()
        receipt = self.state.get("stages", {}).get("restart", {})
        intent = self.state.get("restart_intent")
        instance = self.service_instance()
        if (receipt.get("status") == "passed" and receipt.get("runtime") == self.state["runtime_fingerprint"]
                and receipt.get("environment") == environment
                and receipt.get("instance") == instance):
            value = self.health()
            if value.get("status") == "ok" and value.get("version") == self.state["version"]:
                return
        if intent and reconcile:
            job = self.execute("restart", ["systemctl", "show", self.state["service"], "-p", "Job", "--value"]).stdout.strip()
            if job not in ("", "0"):
                raise ReleaseError("restart", "A systemd job is still pending; cannot reconcile/repeat restart", code=2)
            if not approved:
                raise ReleaseError("restart", "Reconciliation requires explicit restart approval", code=2)
            self.state.pop("restart_intent", None); self.save(); intent = None
        if intent:
            if (intent.get("runtime") != self.state["runtime_fingerprint"]
                    or intent.get("environment") != environment
                    or instance == intent.get("old_instance")):
                raise ReleaseError("restart", "Restart outcome or candidate changed; inspect service, then use --reconcile-restart --approve-restart", code=2)
        else:
            if not approved:
                raise ReleaseError("restart", "User confirmation required before restarting the online service", code=2,
                                   details={"resume_option": "--approve-restart", "service": self.state["service"]})
            self.mark("restart", "running")
            self.state["restart_intent"] = {"old_instance": instance, "started_at": time.time(),
                                            "runtime": self.state["runtime_fingerprint"], "environment": environment,
                                            "candidate": self.state["candidate"]}
            self.save()
            self.execute("restart", ["systemctl", "restart", self.state["service"]])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                value = self.health()
                if value.get("status") == "ok" and value.get("version") == self.state["version"]:
                    intent = self.state["restart_intent"]
                    instance = self.service_instance()
                    if instance == intent.get("old_instance") or instance.get("pid") in ("", "0"):
                        raise ReleaseError("restart", "Health belongs to the old or absent service instance")
                    invocation = self.execute("restart", ["systemctl", "show", self.state["service"], "-p", "InvocationID", "--value"]).stdout.strip()
                    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
                        raise ReleaseError("restart", "Cannot identify the current service invocation for startup logs")
                    since = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(intent["started_at"]))
                    text = self.execute("restart", ["journalctl", "-u", self.state["service"], "_SYSTEMD_INVOCATION_ID=" + invocation,
                                                    "--since", since, "--no-pager", "-o", "cat"]).stdout
                    if re.search(r'(^ERROR\b|\bERROR:|Traceback|Application startup failed|phase=\S+ status=(?:error|failed))', text, re.M):
                        raise ReleaseError("restart", "Startup errors detected; inspect restart.log")
                    self.assert_candidate()
                    if self.service_instance() != instance:
                        raise ReleaseError("restart", "Service instance changed during startup validation")
                    self.state.pop("restart_intent", None)
                    self.mark("restart", "passed", runtime=self.state["runtime_fingerprint"], environment=environment,
                              version=value["version"], instance=instance)
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(2)
        raise ReleaseError("restart", "Health did not confirm the new version; restart will not be repeated automatically")

    def tag_annotation(self, tag):
        value = self.git("cat-file", "tag", tag, stage="git_publish", check=False)
        return value.stdout.split("\n\n", 1)[1] if value.returncode == 0 else None

    def check_publish_ready(self, approved_tag=None):
        """Check known publication blockers before deployment; recheck before push."""
        tag, candidate = "v" + self.state["version"], self.state["candidate"]
        self.assert_candidate()
        self.github.verified_candidate(candidate)
        remote_tag = self.remote_ref("refs/tags/" + tag)
        local_commit = self.git("rev-parse", f"{tag}^{{}}", stage="git_publish", check=False)
        local_same = local_commit.returncode == 0 and local_commit.stdout.strip() == candidate and self.tag_annotation(tag) == self.state["notes"]
        tag_object = self.git("rev-parse", tag, stage="git_publish", check=False).stdout.strip() if local_same else None
        rewrite = remote_tag is not None and not (local_same and remote_tag == tag_object)
        if rewrite:
            if self.github.request("releases/tags/" + tag) is not None:
                raise ReleaseError("git_publish", "The version already has a published Release; use a new version")
            if approved_tag != remote_tag:
                raise ReleaseError("git_publish", "Confirmation required to replace the failed remote tag", code=2,
                                   details={"tag": tag, "expected_old_object": remote_tag, "resume_option": "--approve-tag-rewrite " + remote_tag})
        self.git("fetch", "origin", "main", stage="git_publish")
        self.git("merge-base", "--is-ancestor", "origin/main", candidate, stage="git_publish")
        return tag, candidate, remote_tag, local_same, rewrite

    def git_publish(self, approved_tag=None):
        tag, candidate, remote_tag, local_same, rewrite = self.check_publish_ready(approved_tag)
        if not local_same:
            notes = self.directory / "notes.md"; notes.write_text(self.state["notes"])
            args = ["tag", "-a", "--cleanup=verbatim", tag, "-F", str(notes)]
            if self.git("rev-parse", "--verify", tag, stage="git_publish", check=False).returncode == 0:
                args.insert(1, "-f")
            self.git(*args, stage="git_publish")
        if self.tag_annotation(tag) != self.state["notes"]:
            raise ReleaseError("git_publish", "Annotated tag differs byte-for-byte from approved notes")
        if self.remote_ref("refs/heads/main") != candidate:
            self.git("push", "origin", f"{candidate}:refs/heads/main", stage="git_publish")
        local_object = self.git("rev-parse", tag, stage="git_publish").stdout.strip()
        if remote_tag != local_object:
            args = ["push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"]
            if rewrite:
                args.insert(1, f"--force-with-lease=refs/tags/{tag}:{remote_tag}")
            self.git(*args, stage="git_publish")
        self.mark("git_publish", "passed", commit=candidate, tag_object=local_object)

    def publication(self, retry_publish=False):
        deadline = time.monotonic() + self.state["timeout"]
        while time.monotonic() < deadline:
            value = self.github.request(f'actions/workflows/{PUBLISH_WORKFLOW}/runs?head_sha={self.state["candidate"]}&per_page=100') or {}
            runs = [run for run in value.get("workflow_runs", []) if run.get("head_sha") == self.state["candidate"] and run.get("head_branch") == "v" + self.state["version"]]
            if runs:
                run = runs[0]
                self.state["publish_run"] = run["id"]; self.save()
                intent = self.state.get("publish_retry_intent")
                if intent and run.get("run_attempt", 1) <= intent["attempt"]:
                    if time.time() - intent.get("requested_at", time.time()) > 60:
                        if retry_publish and run.get("status") == "completed":
                            self.state.pop("publish_retry_intent", None); self.save()
                        else:
                            raise ReleaseError("publication", "Retry has not started; inspect run then resume with --retry-publish", code=2)
                    else:
                        time.sleep(3); continue
                if intent:
                    self.state.pop("publish_retry_intent", None); self.save()
                if run.get("status") == "completed" and run.get("conclusion") != "success":
                    if retry_publish:
                        self.request_retry("publish_retry_intent", run)
                        retry_publish = False
                        time.sleep(3); continue
                    raise ReleaseError("publication", "Artifact publishing failed; preserve the existing tag and diagnose this run",
                                       details={"run_id": run["id"], "url": run.get("html_url"), "resume_option": "--retry-publish"})
                if run.get("status") == "completed" and run.get("conclusion") == "success":
                    release = self.github.request("releases/tags/v" + self.state["version"])
                    body = (release or {}).get("body", "") or ""
                    compare = f'https://github.com/{self.state["repository"]}/compare/v{self.state["old_version"]}...v{self.state["version"]}'
                    if not body.startswith(self.state["notes"].rstrip()) or compare not in body:
                        raise ReleaseError("publication", "Release body or generated comparison link differs from approved notes")
                    self.mark("publication", "passed", commit=self.state["candidate"], url=release["html_url"])
                    ref = "refs/heads/" + self.state["branch"]
                    remote = self.remote_ref(ref)
                    if remote is not None:
                        if remote != self.state["candidate"]:
                            raise ReleaseError("cleanup", "Candidate branch changed externally; do not delete another owner's changes")
                        self.git("push", f"--force-with-lease={ref}:{remote}", "origin", ":" + ref, stage="cleanup")
                    self.mark("cleanup", "passed")
                    self.state["completed"] = True; self.save()
                    return
            time.sleep(3)
        raise ReleaseError("publication", "Publication timed out; inspect the saved Actions run before resuming")

    def reap_local(self):
        intent = self.state.get("local_intent")
        if not intent:
            return
        pid, ticks = intent.get("pid"), intent.get("start_ticks")
        if ticks and process_start_ticks(pid) == ticks:
            if os.getpgid(pid) != pid:
                raise ReleaseError("local", "Saved test process no longer owns its group; inspect before cleanup")
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 10
            while process_start_ticks(pid) == ticks and time.monotonic() < deadline:
                time.sleep(0.1)
            if process_start_ticks(pid) == ticks:
                os.killpg(pid, signal.SIGKILL)
        self.state.pop("local_intent", None); self.save()

    def cleanup_rehearsal_registry(self):
        commit = self.state["candidate"]
        commits = [commit]
        branch = self.state.get("branch", "")
        if branch.startswith(CANDIDATE_PREFIX + "rehearsal-"):
            expected_subject = "Release rehearsal " + branch.removeprefix(CANDIDATE_PREFIX)
            cursor = commit
            commits = []
            while cursor:
                subject = self.git("show", "-s", "--format=%s", cursor, stage="cleanup").stdout.strip()
                if subject != expected_subject:
                    break
                commits.append(cursor)
                parents = self.git("show", "-s", "--format=%P", cursor, stage="cleanup").stdout.split()
                if len(parents) > 1:
                    raise ReleaseError("cleanup", "Rehearsal history unexpectedly contains a merge")
                cursor = parents[0] if parents else None
            if not commits:
                raise ReleaseError("cleanup", "Candidate is not owned by the selected rehearsal run")
        owned = {tag for sha in commits for tag in
                 (f"candidate-{sha}-amd64", f"candidate-{sha}-arm64", f"rehearsal-{sha}")}
        owner = self.state["repository"].split('/')[0]
        if self.state.get("owner_type") == "Organization":
            base = f"/orgs/{owner}/packages/container/parrot"
        else:
            base = f"/users/{owner}/packages/container/parrot"
        deleted = []
        # Collect before deletion so pagination cannot shift under our own deletes.
        selected = []
        for page in range(1, 21):
            versions = self.github.request(base + f"/versions?per_page=100&page={page}") or []
            for version in versions:
                tags = set(version.get("metadata", {}).get("container", {}).get("tags", []))
                if tags & owned:
                    if tags - owned:
                        raise ReleaseError("cleanup", "Candidate digest also has non-rehearsal tags; refusing deletion")
                    selected.append(version["id"])
            if len(versions) < 100:
                break
        for version_id in selected:
            # Re-read tags immediately before deleting this specifically owned version.
            version = self.github.request(base + f"/versions/{version_id}")
            if version is None:
                continue
            tags = set(version.get("metadata", {}).get("container", {}).get("tags", []))
            if not tags or not tags <= owned:
                raise ReleaseError("cleanup", "Package version tags changed; refusing deletion")
            self.github.request(base + f"/versions/{version_id}", method="DELETE")
            deleted.append(version_id)
        self.state["deleted_rehearsal_versions"] = deleted; self.save()

    def finish_rehearsal(self):
        self.mark("publication", "passed", commit=self.state["candidate"], rehearsal=True,
                  url=self.state["stages"]["cloud"].get("url"))
        # The successful rehearsal-cleanup CI job used the repository's scoped
        # package token; the local PAT never needs broader package/delete scopes.
        ref = "refs/heads/" + self.state["branch"]
        remote = self.remote_ref(ref)
        if remote:
            if remote != self.state["candidate"]:
                raise ReleaseError("cleanup", "Candidate branch changed externally; refusing deletion")
            self.git("push", f"--force-with-lease={ref}:{remote}", "origin", ":" + ref, stage="cleanup")
        self.mark("cleanup", "passed")
        self.state["completed"] = True; self.save()

    def cleanup(self, failed=False):
        if self.local_process:
            if self.local_process.poll() is None:
                os.killpg(self.local_process.pid, signal.SIGTERM)
                try:self.local_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(self.local_process.pid, signal.SIGKILL);self.local_process.wait(timeout=10)
            self.local_process = None
            self.state.pop("local_intent", None); self.save()
        if self.local_handle:
            self.local_handle.close();self.local_handle = None
        if failed and self.state.get("prepare_run"):
            try:
                run = self.github.request(f'actions/runs/{self.state["prepare_run"]}')
                if run and run.get("status") != "completed":
                    self.github.request(f'actions/runs/{run["id"]}/cancel', method="POST", data={})
            except ReleaseError:
                pass
        log = self.logs / "local-regression.log"
        if log.exists():
            found = re.search(r'ISOLATION_PREEXEC_OK root=(\S+)', log.read_text(errors="replace"))
            if found:
                root = Path(found[1])
                if root.parent == Path(tempfile.gettempdir()) and root.name.startswith("parrot-isolated-pytest-") and not root.is_symlink():
                    shutil.rmtree(root, ignore_errors=True)
        if self.work.exists() and not self.work.is_symlink():
            shutil.rmtree(self.work)
        self.auth_directory.cleanup()

    def run(self, *, approve_restart=False, approve_tag=None, retry_ci=False, retry_publish=False, reconcile_restart=False):
        failed = True
        invocation = {"started_at": time.time()}
        self.state.setdefault("invocations", []).append(invocation)
        self.save()
        try:
            if self.state.get("completed"):
                print("Release already completed:", self.state.get("stages", {}).get("publication", {}).get("url"));failed=False;return
            if not self.token:
                raise ReleaseError("candidate", "GH_TOKEN or GITHUB_TOKEN is required; do not store it in run state")
            self.preflight()
            self.reconcile_commit()
            started = time.time()
            self.prepare()
            self.state["candidate_prepare_seconds"] = round(time.time() - started, 3)
            self.validation(retry_ci)
            if self.state.get("mode") == "rehearsal":
                self.finish_rehearsal()
            else:
                self.check_publish_ready(approve_tag)
                self.restart(approve_restart, reconcile_restart)
                self.git_publish(approve_tag)
                self.mark("publication", "running")
                self.publication(retry_publish)
            failed = False
            self.state.pop("last_error", None);self.save()
            print("Release verified:", self.state["stages"]["publication"]["url"])
        except ReleaseError as exc:
            self.mark(exc.stage, "needs_confirmation" if exc.code == 2 else "failed", **exc.details)
            self.state["last_error"] = {"stage": exc.stage, "message": str(exc), "details": exc.details,
                                        "resume": f'{self.state["python"]} scripts/release.py resume {self.state["run_id"]}'}
            self.save()
            print(self.redact(json.dumps(self.state["last_error"], ensure_ascii=False, indent=2)), file=sys.stderr)
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            failure = ReleaseError("runtime", f"{type(exc).__name__}: {exc}; reconcile saved state before resuming")
            self.state["last_error"] = {"stage": failure.stage, "message": self.redact(str(failure)), "details": {},
                                        "resume": f'{self.state["python"]} scripts/release.py resume {self.state["run_id"]}'}
            self.save();print(json.dumps(self.state["last_error"], ensure_ascii=False, indent=2), file=sys.stderr)
            raise failure from exc
        finally:
            self.cleanup(failed)
            invocation["finished_at"] = time.time()
            self.save()
            self.report()


def main(argv=None):
    if __package__:
        from .release_cli import main as cli_main
    else:
        from release_cli import main as cli_main
    return cli_main(argv, api=sys.modules[__name__])


if __name__ == "__main__":
    raise SystemExit(main())
