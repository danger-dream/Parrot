#!/usr/bin/env python3
"""Decide whether a git tag should also move Docker ``latest``.

Only a plain ``X.Y.Z`` tag that is greater than or equal to every other
plain ``X.Y.Z`` tag is latest.  Prerelease and backfill tags are not.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

_RELEASE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def release_tuple(tag: str) -> tuple[int, int, int] | None:
    match = _RELEASE_TAG.fullmatch(str(tag or "").strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_latest_release_tag(candidate: str, tags: list[str]) -> bool:
    current = release_tuple(candidate)
    if current is None:
        return False
    others = [item for item in (release_tuple(tag) for tag in tags) if item is not None]
    return all(current >= item for item in others)


def _git_tags() -> list[str]:
    result = subprocess.run(
        ["git", "tag", "-l", "v*"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate")
    parser.add_argument("tags", nargs="*")
    parser.add_argument("--from-git", action="store_true")
    args = parser.parse_args(argv)
    tags = _git_tags() if args.from_git else list(args.tags)
    sys.stdout.write("true\n" if is_latest_release_tag(args.candidate, tags) else "false\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
