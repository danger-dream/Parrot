"""Static contracts for the existing Actions publish DAG; never invoke GitHub/GHCR."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _workflow(name):
    path = WORKFLOWS / name
    if not path.exists():
        pytest.skip("workflow sources are intentionally excluded from the runtime image")
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
        "workflow_call", "workflow_dispatch",
    }


@pytest.mark.parametrize("event", ["workflow_call", "workflow_dispatch"])
def test_reusable_and_manual_release_require_explicit_tag(event):
    trigger = _block(_workflow("release.yml"), event, 2)
    tag = _block(trigger, "tag", 6)
    assert "        required: true" in tag
    assert "        type: string" in tag
    resolver = _workflow("release.yml").split("- name: Resolve tag name", 1)[1].split("- name: Checkout", 1)[0]
    assert "TAG: ${{ inputs.tag }}" in resolver
    assert "github.event.inputs" not in resolver
    assert "GITHUB_REF" not in resolver


def test_automatic_release_is_downstream_of_successful_quality_and_image_push():
    docker = _workflow("docker-publish.yml")
    quality = _block(docker, "quality", 2)
    build = _block(docker, "build-and-push", 2)
    release = _block(docker, "release", 2)
    assert "    needs: quality" in build
    assert "    needs: build-and-push" in release
    assert "continue-on-error:" not in quality + build + release
    assert "always()" not in build + release
    assert "python src/tests/isolated_pytest.py -q" in quality
    assert "uses: docker/build-push-action@v6" in build
    assert "          platforms: linux/amd64,linux/arm64" in build
    assert "          push: true" in build
    assert "    if: ${{ success() && startsWith(github.ref, 'refs/tags/v') }}" in release
    assert "    uses: ./.github/workflows/release.yml" in release
    assert "      tag: ${{ github.ref_name }}" in release
    assert "      contents: write" in _block(release, "permissions", 4)
    # The tag-prefix guard above excludes manual docker workflow runs on main.
    assert "  workflow_dispatch:" in _block(docker, "on", 0)
    assert "gh release create" not in docker


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
