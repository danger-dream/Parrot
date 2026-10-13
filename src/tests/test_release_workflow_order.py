"""Contracts for parallel candidate gates and artifact-only publishing; no GitHub/GHCR calls."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _workflow(name):
    path = WORKFLOWS / name
    if not WORKFLOWS.exists():
        pytest.skip("workflow sources are intentionally excluded from the runtime image")
    assert path.is_file(), f"required workflow is missing: {name}"
    return path.read_text()


def _block(text, key, indent):
    """Read one indentation-delimited mapping block, without a runtime YAML dependency."""
    lines = text.splitlines()
    header = " " * indent + key + ":"
    starts = [i for i, line in enumerate(lines) if line == header]
    assert len(starts) == 1, (key, starts)
    start = starts[0]
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.lstrip().startswith("#"):
            if len(line) - len(line.lstrip()) <= indent:
                break
        end += 1
    return "\n".join(lines[start:end])


def test_release_has_no_independent_push_trigger():
    triggers = _block(_workflow("release.yml"), "on", 0)
    assert set(re.findall(r"^  ([a-z_]+):$", triggers, re.M)) == {
        "workflow_call",
    }


@pytest.mark.parametrize("event", ["workflow_call"])
def test_reusable_and_manual_release_require_explicit_tag(event):
    trigger = _block(_workflow("release.yml"), event, 2)
    tag = _block(trigger, "tag", 6)
    assert "        required: true" in tag
    assert "        type: string" in tag
    resolver = _workflow("release.yml").split("- name: Resolve tag name", 1)[1].split("- name: Checkout", 1)[0]
    assert "TAG: ${{ inputs.tag }}" in resolver
    assert "github.event.inputs" not in resolver
    assert "GITHUB_REF" not in resolver


def _step(text, name):
    header = f"      - name: {name}\n"
    assert text.count(header) == 1, name
    return text.split(header, 1)[1].split("\n      - ", 1)[0]


def _shell_script(step):
    raw = step.split("        run: ", 1)[1]
    if not raw.startswith("|\n"):
        return raw.splitlines()[0]
    lines = []
    for line in raw.splitlines()[1:]:
        if line.startswith("          "):
            lines.append(line[10:])
        elif line.strip():
            break
    return "\n".join(lines)


def test_candidate_only_runs_on_candidate_branches_and_keeps_gates_parallel():
    candidate = _workflow("release-prepare.yml")
    triggers = _block(candidate, "on", 0)
    assert set(re.findall(r"^  ([a-z_]+):$", triggers, re.M)) == {"push"}
    assert '      - "release-candidate/**"' in triggers
    assert set(re.findall(r"^  ([a-z_-]+):$", _block(candidate, "jobs", 0), re.M)) == {
        "quality", "quality-shard", "build", "rehearsal", "rehearsal-cleanup",
    }
    for job in ("quality-shard", "build"):
        block = _block(candidate, job, 2)
        assert "needs:" not in block
        assert "continue-on-error:" not in block
    assert "packages: write" in _block(candidate, "build", 2)
    rehearsal = _block(candidate, "rehearsal", 2)
    assert "startsWith(github.ref_name, 'release-candidate/rehearsal-')" in rehearsal
    assert "needs: [quality, build]" in rehearsal
    assert "ci-publish --rehearsal" in rehearsal
    assert "--tags-file" not in rehearsal
    cleanup = _block(candidate, "rehearsal-cleanup", 2)
    assert "needs: rehearsal" in cleanup
    assert "ci-cleanup-rehearsal" in cleanup
    assert "startsWith(github.ref_name, 'release-candidate/rehearsal-')" in cleanup
    assert "gh release" not in candidate
    assert "release.yml" not in candidate


def test_candidate_quality_keeps_full_clean_regression_and_failure_logs():
    quality = _block(_workflow("release-prepare.yml"), "quality-shard", 2)
    for fragment in (
        "uses: actions/setup-python@v5",
        'python-version: "3.11"',
        "fetch-depth: 0",
        "python -m pip install -r requirements-dev.txt",
        "python scripts/sync_go_tls_vendor.py --check",
        "python scripts/check_go_tls.py --load",
        "python -m compileall -q server.py src",
        'python -c "import server; from src import state_db; from src.state_store import StateStore"',
        "python src/tests/isolated_pytest.py -q -ra src/tests",
    ):
        assert fragment in quality
    assert quality.count("python src/tests/isolated_pytest.py") == 1
    assert 'WORKERS="$(nproc)"' in quality
    assert '-n "$WORKERS" --dist=worksteal' in quality
    for name in ("Install test dependencies", "Compile and import", "Full isolated regression"):
        script = _shell_script(_step(quality, name))
        assert "pipefail" in script
        assert "2>&1 | tee quality-logs/" in script
        assert "|| true" not in script
    logs = _step(quality, "Preserve quality logs")
    assert "if: ${{ always() }}" in logs
    assert "uses: actions/upload-artifact@v4" in logs
    assert "path: quality-logs" in logs
    # Do not introduce a second container regression or weaken isolation with env switches.
    assert "container:" not in quality
    assert "docker run" not in quality
    assert re.findall(r"^      ([A-Z_]+):", quality, re.M) == ["PARROT_REQUIRE_GO_TLS"]
    assert 'PARROT_REQUIRE_GO_TLS: "1"' in quality
    dependencies = (WORKFLOWS.parent.parent / "requirements-dev.txt").read_text()
    for required in ("-r requirements.txt", "pytest>=8.0", "pytest-asyncio>=1.0", "pytest-xdist>=3.5"):
        assert required in dependencies
    assert "--ignore" not in quality
    assert "--deselect" not in quality


@pytest.mark.parametrize("name,log", [
    ("Install test dependencies", "install.log"),
    ("Compile and import", "compile-import.log"),
    ("Full isolated regression", "regression.log"),
])
def test_candidate_quality_fails_closed_while_retaining_logs(name, log, tmp_path):
    quality = _block(_workflow("release-prepare.yml"), "quality-shard", 2)
    script = _shell_script(_step(quality, name)).replace("${{ matrix.shard }}", "1")
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\necho 'simulated failure' >&2\nexit 7\n")
    python.chmod(0o755)
    (tmp_path / "quality-logs").mkdir()
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["bash", "-e", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True,
    )
    assert result.returncode == 7, result.stderr
    assert (tmp_path / "quality-logs" / log).read_text() == "simulated failure\n"


def test_candidate_shards_have_an_all_pass_and_complete_coverage_gate():
    candidate = _workflow("release-prepare.yml")
    shards = _block(candidate, "quality-shard", 2)
    assert "shard: [1, 2]" in shards
    assert "fail-fast: false" in shards
    assert "--shard-index ${{ matrix.shard }} --shard-count 2" in shards
    assert "--shard-report quality-logs/shard-${{ matrix.shard }}.json" in shards
    assert "name: parrot-quality-${{ github.sha }}-${{ matrix.shard }}" in shards
    gate = _block(candidate, "quality", 2)
    assert "if: ${{ always() }}" in gate and "needs: quality-shard" in gate
    assert "SHARD_RESULT: ${{ needs.quality-shard.result }}" in gate
    assert 'run: test "$SHARD_RESULT" = success' in gate
    assert "pattern: parrot-quality-${{ github.sha }}-*" in gate
    assert "python3 scripts/test_shards.py --count 2 quality-reports/*/shard-*.json" in gate
    assert "continue-on-error:" not in gate and "|| true" not in gate


@pytest.mark.parametrize("result,code", [("success", 0), ("failure", 1), ("cancelled", 1), ("skipped", 1)])
def test_candidate_shard_gate_fails_closed(result, code):
    gate = _block(_workflow("release-prepare.yml"), "quality", 2)
    script = _shell_script(_step(gate, "Require every shard to pass"))
    assert subprocess.run(["bash", "-e", "-c", script],
                          env={**os.environ, "SHARD_RESULT": result}).returncode == code


def test_candidate_builds_two_native_digests_with_cross_branch_registry_cache():
    candidate = _workflow("release-prepare.yml")
    build = _block(candidate, "build", 2)
    assert re.findall(r"- arch: (\w+)\n +runner: ([\w.-]+)", build) == [
        ("amd64", "ubuntu-latest"), ("arm64", "ubuntu-24.04-arm"),
    ]
    for fragment in (
        "name: build (${{ matrix.arch }})",
        "runs-on: ${{ matrix.runner }}",
        "fail-fast: false",
        'python3 scripts/release.py ci-info --output "$GITHUB_OUTPUT"',
        "uses: docker/setup-buildx-action@v3",
        "uses: docker/build-push-action@v6",
        "platforms: linux/${{ matrix.arch }}",
        "push: true",
        "provenance: false",
        "tags: ${{ steps.info.outputs.repository }}:candidate-${{ github.sha }}-${{ matrix.arch }}",
        "org.opencontainers.image.revision=${{ github.sha }}",
        "org.opencontainers.image.version=${{ steps.info.outputs.version }}",
        "cache-from: type=registry,ref=${{ steps.info.outputs.cache }}:${{ matrix.arch }}",
        "cache-to: type=registry,ref=${{ steps.info.outputs.cache }}:${{ matrix.arch }},mode=max",
        "uses: actions/upload-artifact@v4",
        "name: parrot-candidate-${{ github.sha }}-${{ matrix.arch }}",
        "path: image-${{ matrix.arch }}.json",
        "if-no-files-found: error",
    ):
        assert fragment in build
    assert "type=docker" not in build
    assert "ci-record" in build
    assert "overwrite: true" in build
    assert "setup-qemu" not in candidate


def test_formal_publish_only_reuses_verified_artifacts_for_checked_out_commit():
    docker = _workflow("docker-publish.yml")
    assert set(re.findall(r"^  ([a-z_-]+):$", _block(docker, "jobs", 0), re.M)) == {
        "publish", "release",
    }
    publish = _block(docker, "publish", 2)
    for fragment in (
        "ref: refs/tags/${{ steps.tag.outputs.tag }}",
        "fetch-depth: 0",
        'SOURCE_SHA="$(git rev-parse HEAD)"',
        'echo "sha=$SOURCE_SHA" >> "$GITHUB_OUTPUT"',
        "SOURCE_SHA: ${{ steps.source.outputs.sha }}",
        'python3 scripts/release.py ci-verify-candidate --commit "$SOURCE_SHA" --output "$GITHUB_OUTPUT"',
        "uses: actions/download-artifact@v4",
        "run-id: ${{ steps.candidate.outputs.run_id }}",
        "github-token: ${{ secrets.GITHUB_TOKEN }}",
        "pattern: parrot-candidate-${{ steps.source.outputs.sha }}-*",
        "merge-multiple: true",
        "path: release-images",
        "uses: docker/setup-buildx-action@v3",
        "uses: docker/login-action@v3",
        'python3 scripts/release.py ci-publish --images release-images --commit "$SOURCE_SHA" --repository "$IMAGE_REPOSITORY" --tags-file tags.txt',
    ):
        assert fragment in publish
    assert "      actions: read" in _block(publish, "permissions", 4)
    assert "      packages: write" in _block(publish, "permissions", 4)
    assert publish.index("ci-verify-candidate") < publish.index("actions/download-artifact")
    assert publish.index("actions/download-artifact") < publish.index("docker/login-action")
    assert publish.index("docker/login-action") < publish.index("ci-publish")
    for forbidden in (
        "isolated_pytest.py", "setup-python", "pip install", "compileall",
        "build-push-action", "setup-qemu", "docker build", "continue-on-error:", "always()",
    ):
        assert forbidden not in publish
    assert "--commit \"$GITHUB_SHA\"" not in publish
    assert "parrot-image-${{ github.sha }}" not in publish


def test_formal_publish_preserves_aliases_and_gates_latest_for_resolved_tag():
    docker = _workflow("docker-publish.yml")
    assert 'repository=${REGISTRY}/${GITHUB_REPOSITORY_OWNER,,}/parrot' in docker
    latest = _step(docker, "Decide whether this tag moves latest")
    assert "TAG: ${{ steps.tag.outputs.tag }}" in latest
    assert 'python3 scripts/is_latest_release_tag.py "$TAG" --from-git' in latest
    meta = _step(docker, "Extract metadata (tags, labels)")
    for fragment in (
        "uses: docker/metadata-action@v5",
        "context: git",
        "images: ${{ steps.source.outputs.repository }}",
        "flavor: latest=false",
        "type=raw,value=${{ steps.tag.outputs.tag }}",
        "type=semver,pattern={{version}},value=${{ steps.tag.outputs.tag }}",
        "type=semver,pattern={{major}}.{{minor}},value=${{ steps.tag.outputs.tag }}",
        "type=semver,pattern={{major}},value=${{ steps.tag.outputs.tag }}",
        "type=sha,prefix=sha-",
        "type=raw,value=latest,enable=${{ steps.latest.outputs.is_latest == 'true' }}",
    ):
        assert fragment in meta
    push = _step(docker, "Promote verified digests without rebuilding or re-uploading")
    assert "IMAGE_TAGS: ${{ steps.meta.outputs.tags }}" in push
    assert "printf '%s\\n' \"$IMAGE_TAGS\" > tags.txt" in push


def test_automatic_release_is_downstream_of_successful_official_publish():
    docker = _workflow("docker-publish.yml")
    release = _block(docker, "release", 2)
    assert "    needs: publish" in release
    assert "    if: ${{ success() }}" in release
    assert "    uses: ./.github/workflows/release.yml" in release
    assert "      tag: ${{ needs.publish.outputs.tag }}" in release
    assert "      contents: write" in _block(release, "permissions", 4)
    assert "continue-on-error:" not in release
    assert "always()" not in release
    assert "gh release create" not in docker
    triggers = _block(docker, "on", 0)
    assert '      - "v*"' in _block(triggers, "push", 2)
    tag = _block(_block(triggers, "workflow_dispatch", 2), "tag", 6)
    assert "required: true" in tag
    assert "type: string" in tag


@pytest.mark.parametrize("tag,ok", [
    ("v0.34.5", True), ("v0.34.5-rc.1", True),
    ("main", False), ("refs/tags/v0.34.5", False), ("", False),
    ("v0.34.5\nsha=other", False),
])
def test_publisher_requires_a_safe_explicit_release_tag(tag, ok, tmp_path):
    script = _shell_script(_step(_workflow("docker-publish.yml"), "Resolve tag name"))
    output = tmp_path / "github-output"
    env = {**os.environ, "TAG": tag, "GITHUB_OUTPUT": str(output),
           "GITHUB_EVENT_NAME": "push", "GITHUB_REF": f"refs/tags/{tag}"}
    result = subprocess.run(["bash", "-e", "-c", script], env=env, text=True, capture_output=True)
    assert (result.returncode == 0) is ok, result.stderr
    if ok:
        assert output.read_text() == f"tag={tag}\n"
    else:
        assert not output.exists()


@pytest.mark.parametrize("git_ok", [True, False])
def test_publisher_source_uses_checked_out_head_not_dispatch_context(tmp_path, git_ok):
    script = _shell_script(_step(_workflow("docker-publish.yml"), "Resolve source commit and image repository"))
    # A local read-only git stub proves HEAD is used, without creating any commits or tags.
    git = tmp_path / "git"
    source_sha = "a" * 40
    git.write_text(
        f'#!/bin/sh\n[ "$*" = "rev-parse HEAD" ] || exit 2\nprintf "%s\\n" "{source_sha}"\n'
        if git_ok else "#!/bin/sh\nexit 2\n"
    )
    git.chmod(0o755)
    output = tmp_path / "github-output"
    env = {
        **os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output), "GITHUB_SHA": "b" * 40,
        "GITHUB_REPOSITORY_OWNER": "MixedCaseOwner", "REGISTRY": "ghcr.io",
    }
    result = subprocess.run(["bash", "-e", "-c", script], env=env, text=True, capture_output=True)
    assert (result.returncode == 0) is git_ok, result.stderr
    if git_ok:
        assert output.read_text().splitlines() == [
            f"sha={source_sha}", "repository=ghcr.io/mixedcaseowner/parrot",
        ]
    else:
        assert not output.exists()


@pytest.mark.parametrize("name", ["release-prepare.yml", "docker-publish.yml", "release.yml"])
def test_workflow_run_blocks_are_valid_bash(name):
    text = _workflow(name)
    starts = [match.start() for match in re.finditer(r"^        run: ", text, re.M)]
    assert starts
    for start in starts:
        script = _shell_script(text[start:])
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


def test_release_body_prerelease_and_idempotency_contracts_are_preserved():
    release = _workflow("release.yml")
    for fragment in (
        'MSG="$(git tag -l --format=\'%(contents)\' "$TAG" || true)"',
        'MSG="$(git log -1 --pretty=%B "$TAG")"',
        'if [[ "$TAG" =~ -(rc|alpha|beta|pre|dev) ]]; then',
        'gh release view "$TAG"',
        "if: steps.exist.outputs.exists == 'false'",
        'gh release create "$TAG"',
        '--notes "$BODY"',
        '--generate-notes',
        'EXTRA_FLAGS="--prerelease"',
        "ref: ${{ steps.tag.outputs.tag }}",
        "contents: write",
    ):
        assert fragment in release


@pytest.mark.parametrize("tag,prerelease,ok", [
    ("v0.33.0", "false", True),
    ("v0.33.0-rc.1", "true", True),
    ("", None, False),
])
def test_shared_tag_resolver_shell(tag, prerelease, ok, tmp_path):
    resolver = _workflow("release.yml").split("- name: Resolve tag name", 1)[1].split("- name: Checkout", 1)[0]
    raw = resolver.split("        run: |\n", 1)[1]
    script = "\n".join(line[10:] for line in raw.splitlines() if line.startswith("          "))
    output = tmp_path / "github-output"
    env = {**os.environ, "TAG": tag, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(["bash", "-e", "-c", script], env=env, text=True, capture_output=True)
    assert (result.returncode == 0) is ok, result.stderr
    if ok:
        assert output.read_text().splitlines() == [f"tag={tag}", f"prerelease={prerelease}"]
    else:
        assert not output.exists()
