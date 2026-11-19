"""Unified pre-update source integrity verification for source and Docker deployments.

Verifies that managed Parrot source files have not been patched, added, deleted,
renamed, committed locally off the upstream baseline, or shadowed by container
bind mounts. Never provides a force-update or bypass mechanism.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Sequence


MANIFEST_SCHEMA = 1
DEFAULT_MANIFEST_PATH = "/usr/local/share/parrot/source-manifest.json"
FALLBACK_MANIFEST_FILENAME = ".parrot-source-manifest.json"
MANAGED_SOURCE_ROOTS: tuple[str, ...] = ("server.py", "src")

KIND_MODIFIED = "modified"
KIND_ADDED = "added"
KIND_DELETED = "deleted"
KIND_RENAMED = "renamed"
KIND_MOUNT_OVERRIDE = "mount_override"

KIND_LABELS: dict[str, str] = {
    KIND_MODIFIED: "已修改",
    KIND_ADDED: "新增",
    KIND_DELETED: "已删除",
    KIND_RENAMED: "重命名",
    KIND_MOUNT_OVERRIDE: "外部挂载覆盖",
}

_VERSION_RE = re.compile(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]\s*$', re.M)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_SUMMARY_MAX_ITEMS = 8
_LOG_MAX_ITEMS = 200


@dataclass(frozen=True, slots=True)
class SourceChange:
    path: str
    kind: str
    detail: str = ""

    def format_label(self) -> str:
        label = KIND_LABELS.get(self.kind, self.kind)
        if self.detail:
            return f"{self.path} [{label}，{self.detail}]"
        return f"{self.path} [{label}]"


@dataclass(frozen=True, slots=True)
class SourceIntegrityReport:
    ok: bool
    mode: str
    source_kind: str
    changes: tuple[SourceChange, ...] = ()
    error: str = ""
    summary: str = ""
    detail_lines: tuple[str, ...] = ()

    @property
    def modified_files(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.changes)


GitRunner = Callable[[list[str], int], tuple[int, str]]


def _normalize_rel(raw: str) -> str:
    text = str(raw or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _normalize_excludes(excludes: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for item in excludes or ():
        norm = _normalize_rel(item)
        if norm:
            out.append(norm)
    return tuple(dict.fromkeys(out))


def _is_excluded(rel_posix: str, excludes: tuple[str, ...]) -> bool:
    if not excludes:
        return False
    norm = _normalize_rel(rel_posix)
    return any(norm == ex or norm.startswith(ex + "/") for ex in excludes)


def _is_ignored_bytecode(rel_posix: str) -> bool:
    parts = Path(rel_posix).parts
    return "__pycache__" in parts


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _read_app_version(app_dir: Path) -> str:
    init_path = app_dir / "src" / "__init__.py"
    try:
        text = init_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _VERSION_RE.search(text)
    return match.group(1).strip() if match else ""


def _norm_version(version: str) -> str:
    return str(version or "").strip().lstrip("vV")


def _describe_entry(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink():
            return {"type": "symlink", "target": os.readlink(path)}
        if path.is_file():
            digest, size = _sha256_file(path)
            return {"type": "file", "sha256": digest, "size": size}
        return {"type": "special"}
    except OSError as exc:
        return {"type": "unreadable", "error": type(exc).__name__}


def scan_managed_files(
    app_dir: str | Path,
    *,
    roots: Sequence[str] = MANAGED_SOURCE_ROOTS,
    excludes: Sequence[str] = (),
) -> dict[str, dict[str, object]]:
    """Scan managed source roots and return relative POSIX path -> file fingerprint."""
    root = Path(app_dir).resolve()
    norm_excludes = _normalize_excludes(excludes)
    entries: dict[str, dict[str, object]] = {}

    def _on_walk_error(exc: OSError) -> None:
        raw_path = getattr(exc, "filename", None)
        if not raw_path:
            return
        try:
            rel = Path(raw_path).resolve().relative_to(root).as_posix()
        except Exception:
            return
        if rel and not _is_ignored_bytecode(rel) and not _is_excluded(rel, norm_excludes):
            entries[rel] = {"type": "unreadable", "error": type(exc).__name__}

    for raw_name in roots:
        name = _normalize_rel(raw_name)
        if not name or ".." in Path(name).parts:
            continue
        target = root / name
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_symlink() or not target.is_dir():
            if not _is_ignored_bytecode(name) and not _is_excluded(name, norm_excludes):
                entries[name] = _describe_entry(target)
            continue
        for current_dir, dirnames, filenames in os.walk(target, followlinks=False, onerror=_on_walk_error):
            current_path = Path(current_dir)
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames):
                child = current_path / dirname
                rel = child.relative_to(root).as_posix()
                if dirname == "__pycache__" or _is_excluded(rel, norm_excludes):
                    continue
                if child.is_symlink():
                    entries[rel] = _describe_entry(child)
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
            for filename in sorted(filenames):
                child = current_path / filename
                rel = child.relative_to(root).as_posix()
                if _is_ignored_bytecode(rel) or _is_excluded(rel, norm_excludes):
                    continue
                entries[rel] = _describe_entry(child)
    return dict(sorted(entries.items()))


def build_manifest(
    app_dir: str | Path,
    *,
    version: str | None = None,
    roots: Sequence[str] = MANAGED_SOURCE_ROOTS,
    excludes: Sequence[str] = (),
) -> dict[str, object]:
    root = Path(app_dir).resolve()
    resolved_version = _norm_version(version if version is not None else _read_app_version(root))
    clean_roots = [r for r in (_normalize_rel(item) for item in roots) if r]
    files = scan_managed_files(root, roots=clean_roots, excludes=excludes)
    return {
        "schema": MANIFEST_SCHEMA,
        "version": resolved_version,
        "roots": clean_roots,
        "files": files,
    }


def write_manifest(
    output_path: str | Path,
    app_dir: str | Path,
    *,
    version: str | None = None,
    roots: Sequence[str] = MANAGED_SOURCE_ROOTS,
    excludes: Sequence[str] = (),
) -> dict[str, object]:
    manifest = build_manifest(app_dir, version=version, roots=roots, excludes=excludes)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return manifest


def _valid_manifest_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    entry_type = entry.get("type")
    if entry_type == "file":
        sha = entry.get("sha256")
        size = entry.get("size")
        return (
            isinstance(sha, str)
            and bool(_SHA256_RE.fullmatch(sha))
            and (size is None or (isinstance(size, int) and not isinstance(size, bool) and size >= 0))
        )
    if entry_type == "symlink":
        return isinstance(entry.get("target"), str)
    return False


def _entries_equal(expected: dict[str, object], actual: dict[str, object]) -> bool:
    if expected.get("type") != actual.get("type"):
        return False
    if expected.get("type") == "file":
        return expected.get("sha256") == actual.get("sha256")
    if expected.get("type") == "symlink":
        return expected.get("target") == actual.get("target")
    return False


def verify_manifest(
    app_dir: str | Path,
    manifest_path: str | Path,
    *,
    expected_version: str = "",
    excludes: Sequence[str] = (),
) -> tuple[list[SourceChange], str]:
    path = Path(manifest_path)
    if not path.exists() and not path.is_symlink():
        return [], f"缺少受管源码清单（{path.name}）"
    if path.is_symlink() or not path.is_file():
        return [], f"受管源码清单路径非法（{path.name} 不是普通文件）"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [], f"受管源码清单读取或解析失败（{type(exc).__name__}）"
    if not isinstance(raw, dict) or raw.get("schema") != MANIFEST_SCHEMA:
        return [], "受管源码清单格式或 schema 版本不受支持"
    roots = raw.get("roots")
    files = raw.get("files")
    if (
        not isinstance(roots, list)
        or not roots
        or any(not isinstance(item, str) or not _normalize_rel(item) or ".." in Path(item).parts for item in roots)
    ):
        return [], "受管源码清单缺少有效的受管根目录定义"
    if not isinstance(files, dict) or not files:
        return [], "受管源码清单为空或结构非法"

    manifest_version = _norm_version(str(raw.get("version") or ""))
    running_version = _norm_version(expected_version)
    version_error = ""
    if running_version and manifest_version and running_version != manifest_version:
        version_error = f"源码清单版本（v{manifest_version}）与当前运行版本（v{running_version}）不一致"

    expected_files: dict[str, dict[str, object]] = {}
    for rel_key, entry in files.items():
        if not isinstance(rel_key, str) or not _valid_manifest_entry(entry):
            return [], "受管源码清单包含非法条目"
        norm = _normalize_rel(rel_key)
        if not norm or ".." in Path(norm).parts:
            return [], "受管源码清单包含非法文件路径"
        expected_files[norm] = entry

    actual_files = scan_managed_files(app_dir, roots=roots, excludes=excludes)
    changes: list[SourceChange] = []
    for rel in sorted(expected_files.keys() | actual_files.keys()):
        exp = expected_files.get(rel)
        act = actual_files.get(rel)
        if exp is None:
            detail = "无法读取" if isinstance(act, dict) and act.get("type") == "unreadable" else ""
            changes.append(SourceChange(rel, KIND_ADDED, detail))
        elif act is None:
            changes.append(SourceChange(rel, KIND_DELETED))
        elif not _entries_equal(exp, act):
            detail = "无法读取" if act.get("type") == "unreadable" else ""
            changes.append(SourceChange(rel, KIND_MODIFIED, detail))
    return changes, version_error


def _unescape_mount_path(raw: str) -> str:
    return _MOUNT_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), raw)


def detect_mount_overrides(
    app_dir: str | Path,
    *,
    data_dir: str | Path | None = None,
    roots: Sequence[str] = MANAGED_SOURCE_ROOTS,
    excludes: Sequence[str] = (),
    manifest_path: str | Path | None = None,
    mountinfo_path: str | Path = "/proc/self/mountinfo",
) -> tuple[list[SourceChange], str]:
    """Detect bind mounts shadowing managed source paths or the manifest inside a container."""
    info_path = Path(mountinfo_path)
    try:
        content = info_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], f"无法读取容器挂载信息（{info_path.name}：{type(exc).__name__}）"

    app_root = Path(app_dir).resolve()
    app_roots = [app_root]
    raw_app = Path(os.path.normpath(os.path.abspath(str(app_dir))))
    if raw_app != app_root:
        app_roots.append(raw_app)

    manifest_candidates: list[Path] = []
    if manifest_path is not None:
        raw_manifest = Path(os.path.normpath(os.path.abspath(str(manifest_path))))
        manifest_candidates.append(raw_manifest)
        try:
            resolved_manifest = Path(manifest_path).resolve()
            if resolved_manifest != raw_manifest:
                manifest_candidates.append(resolved_manifest)
        except OSError:
            pass

    excluded_abs: list[Path] = []
    if data_dir:
        for candidate_data in (Path(data_dir).resolve(), Path(os.path.normpath(os.path.abspath(str(data_dir))))):
            if any(candidate_data != ar and candidate_data.is_relative_to(ar) for ar in app_roots):
                if candidate_data not in excluded_abs:
                    excluded_abs.append(candidate_data)
    for ex in _normalize_excludes(excludes):
        for ar in app_roots:
            candidate = (ar / ex).resolve()
            if candidate != ar and candidate.is_relative_to(ar) and candidate not in excluded_abs:
                excluded_abs.append(candidate)

    managed_roots = tuple(_normalize_rel(r) for r in roots if _normalize_rel(r))
    overrides: list[SourceChange] = []
    seen_paths: set[str] = set()
    manifest_mount_error = ""

    for line in content.splitlines():
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        mp_raw = _unescape_mount_path(fields[4])
        if not mp_raw or mp_raw == "/":
            continue
        mp = Path(os.path.normpath(mp_raw))
        if manifest_candidates and not manifest_mount_error:
            if any(mc == mp or mc.is_relative_to(mp) for mc in manifest_candidates):
                manifest_mount_error = f"受管源码清单所在路径被外部挂载覆盖（{mp.as_posix()}）"
        if any(mp == ex or mp.is_relative_to(ex) for ex in excluded_abs):
            continue
        matched_root = next((ar for ar in app_roots if mp == ar or mp.is_relative_to(ar)), None)
        if matched_root is None:
            continue
        if mp == matched_root:
            for root_name in managed_roots:
                if root_name not in seen_paths:
                    seen_paths.add(root_name)
                    overrides.append(SourceChange(root_name, KIND_MOUNT_OVERRIDE, "应用目录整体被外部挂载覆盖"))
            continue
        rel = mp.relative_to(matched_root).as_posix()
        top = Path(rel).parts[0] if Path(rel).parts else ""
        if top in managed_roots and rel not in seen_paths:
            seen_paths.add(rel)
            overrides.append(SourceChange(rel, KIND_MOUNT_OVERRIDE))
    return sorted(overrides, key=lambda item: item.path), manifest_mount_error


def _default_git_runner(app_dir: Path) -> GitRunner:
    def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(app_dir), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)
            return proc.returncode, output
        except FileNotFoundError as exc:
            return 127, str(exc)
        except Exception as exc:
            return 1, str(exc)

    return _run


def _parse_porcelain_z(raw: str, excludes: tuple[str, ...]) -> list[SourceChange]:
    tokens = raw.split("\0")
    index = 0
    changes: list[SourceChange] = []
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if not entry or len(entry) < 4:
            continue
        xy = entry[:2]
        path = _normalize_rel(entry[3:])
        old_path = ""
        if "R" in xy or "C" in xy:
            if index < len(tokens):
                old_path = _normalize_rel(tokens[index])
                index += 1
        if not path or _is_ignored_bytecode(path) or _is_excluded(path, excludes):
            continue
        if xy == "??":
            changes.append(SourceChange(path, KIND_ADDED))
        elif "R" in xy:
            detail = f"原 {old_path}" if old_path else ""
            changes.append(SourceChange(path, KIND_RENAMED, detail))
        elif "C" in xy:
            detail = f"复制自 {old_path}" if old_path else ""
            changes.append(SourceChange(path, KIND_ADDED, detail))
        elif "D" in xy and "A" not in xy:
            changes.append(SourceChange(path, KIND_DELETED))
        elif "A" in xy and "D" not in xy:
            changes.append(SourceChange(path, KIND_ADDED))
        else:
            changes.append(SourceChange(path, KIND_MODIFIED))
    return changes


def _parse_diff_name_status_z(raw: str, excludes: tuple[str, ...], *, detail_suffix: str = "") -> list[SourceChange]:
    tokens = raw.split("\0")
    index = 0
    changes: list[SourceChange] = []
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            continue
        code = status[0]
        if code in ("R", "C"):
            if index + 1 >= len(tokens):
                break
            old_path = _normalize_rel(tokens[index])
            new_path = _normalize_rel(tokens[index + 1])
            index += 2
            if not new_path or _is_ignored_bytecode(new_path) or _is_excluded(new_path, excludes):
                continue
            parts = [f"原 {old_path}" if code == "R" else f"复制自 {old_path}"]
            if detail_suffix:
                parts.append(detail_suffix)
            changes.append(SourceChange(new_path, KIND_RENAMED if code == "R" else KIND_ADDED, "，".join(p for p in parts if p)))
        else:
            if index >= len(tokens):
                break
            path = _normalize_rel(tokens[index])
            index += 1
            if not path or _is_ignored_bytecode(path) or _is_excluded(path, excludes):
                continue
            kind = {
                "A": KIND_ADDED,
                "D": KIND_DELETED,
            }.get(code, KIND_MODIFIED)
            changes.append(SourceChange(path, kind, detail_suffix))
    return changes


def _git_verify_commit(run_git: GitRunner, ref: str) -> str:
    rc, out = run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], 15)
    return out.strip() if rc == 0 else ""


def _git_is_ancestor(run_git: GitRunner, commit: str, ref: str) -> bool:
    rc, _ = run_git(["merge-base", "--is-ancestor", commit, ref], 15)
    return rc == 0


def _git_remote_refs(run_git: GitRunner) -> tuple[list[str], list[str]]:
    """Return (priority_refs, other_origin_refs) for upstream baseline selection."""
    priority: list[str] = []
    rc, upstream = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], 15)
    if rc == 0 and upstream.strip():
        priority.append(upstream.strip())

    rc, raw = run_git(["for-each-ref", "--format=%(refname)", "refs/remotes/origin"], 15)
    origin_refs = [line.strip() for line in raw.splitlines() if line.strip()] if rc == 0 and raw.strip() else []
    for preferred in ("refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/remotes/origin/master"):
        if preferred in origin_refs and preferred not in priority:
            priority.append(preferred)
    others = [ref for ref in origin_refs if ref not in priority]
    return priority, others


def _git_head_in_origin(run_git: GitRunner, head: str, priority_refs: list[str], other_refs: list[str]) -> bool:
    for ref in priority_refs:
        if _git_is_ancestor(run_git, head, ref):
            return True
    if not other_refs:
        return False
    rc, containing = run_git(["for-each-ref", "--contains", head, "--format=%(refname)", "refs/remotes/origin"], 15)
    if rc == 0:
        return bool(containing.strip())
    return any(_git_is_ancestor(run_git, head, ref) for ref in other_refs)


def _git_closest_baseline(
    run_git: GitRunner,
    head: str,
    version_tag_commit: str,
    priority_refs: list[str],
    other_refs: list[str],
) -> str:
    candidates: list[str] = []
    if version_tag_commit and _git_is_ancestor(run_git, version_tag_commit, head):
        candidates.append(version_tag_commit)
    for ref in priority_refs:
        rc_mb, mb = run_git(["merge-base", head, ref], 15)
        if rc_mb == 0 and mb.strip() and mb.strip() not in candidates:
            candidates.append(mb.strip())
    if not candidates:
        for ref in other_refs:
            rc_mb, mb = run_git(["merge-base", head, ref], 15)
            if rc_mb == 0 and mb.strip() and mb.strip() not in candidates:
                candidates.append(mb.strip())
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        best = candidates[0]
        best_dist: int | None = None
        for cand in candidates:
            rc_cnt, cnt_out = run_git(["rev-list", "--count", f"{cand}..{head}"], 15)
            if rc_cnt == 0:
                try:
                    dist = int(cnt_out.strip())
                except ValueError:
                    continue
                if best_dist is None or dist < best_dist:
                    best = cand
                    best_dist = dist
        return best
    return version_tag_commit


def verify_git_source(
    app_dir: str | Path,
    *,
    current_version: str = "",
    excludes: Sequence[str] = (),
    git_runner: GitRunner | None = None,
    expected_commit: str | None = None,
) -> tuple[list[SourceChange], str]:
    root = Path(app_dir).resolve()
    run_git = git_runner or _default_git_runner(root)
    norm_excludes = _normalize_excludes(excludes)

    # 1. Working tree and index changes (modified, staged, deleted, renamed, untracked).
    rc, status_out = run_git(["status", "--porcelain=v1", "--untracked-files=all", "-z"], 30)
    if rc != 0:
        return [], "git status 执行失败，无法验证工作树是否干净"
    changes = _parse_porcelain_z(status_out, norm_excludes)

    # 1b. Detect ignored untracked files inside managed source roots (e.g. .git/info/exclude or stray .pyc).
    rc_ign, ign_out = run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", *MANAGED_SOURCE_ROOTS],
        30,
    )
    if rc_ign != 0:
        return _merge_changes(changes), "git ls-files 执行失败，无法检查受管源码目录中的忽略文件"
    for raw_item in ign_out.split("\0"):
        path = _normalize_rel(raw_item)
        if not path or _is_ignored_bytecode(path) or _is_excluded(path, norm_excludes):
            continue
        changes.append(SourceChange(path, KIND_ADDED, "被忽略的未受管文件"))

    # 2. Local committed patches diverging from official release tag / upstream refs.
    head = _git_verify_commit(run_git, "HEAD")
    if not head:
        return _merge_changes(changes), "无法读取当前 Git 提交（HEAD）"

    if expected_commit:
        expected = expected_commit.strip()
        if head != expected:
            rc_diff, diff_out = run_git(["diff", "--name-status", "-z", expected, head], 30)
            if rc_diff == 0:
                diff_changes = _parse_diff_name_status_z(diff_out, norm_excludes, detail_suffix="暂存后提交变更")
                if diff_changes:
                    changes.extend(diff_changes)
                else:
                    return _merge_changes(changes), f"当前 Git 提交（{head[:10]}）已偏离暂存提交（{expected[:10]}）"
            else:
                return _merge_changes(changes), f"当前 Git 提交（{head[:10]}）已偏离暂存提交（{expected[:10]}）"
        return _merge_changes(changes), ""

    norm_ver = _norm_version(current_version)
    version_tag_commit = _git_verify_commit(run_git, f"refs/tags/v{norm_ver}") if norm_ver else ""
    if version_tag_commit and head == version_tag_commit:
        return _merge_changes(changes), ""

    priority_refs, other_refs = _git_remote_refs(run_git)
    if _git_head_in_origin(run_git, head, priority_refs, other_refs):
        return _merge_changes(changes), ""

    # HEAD matches neither v{version} nor any origin/upstream ref -> diff against closest baseline.
    baseline = _git_closest_baseline(run_git, head, version_tag_commit, priority_refs, other_refs)

    if baseline:
        rc_diff, diff_out = run_git(["diff", "--name-status", "-z", baseline, head], 30)
        if rc_diff != 0:
            return _merge_changes(changes), "无法比对本地提交与官方基线的源码差异"
        committed_changes = _parse_diff_name_status_z(diff_out, norm_excludes, detail_suffix="本地提交补丁")
        if committed_changes:
            changes.extend(committed_changes)
            return _merge_changes(changes), ""
        return _merge_changes(changes), f"本地 Git 提交（{head[:10]}）偏离官方基线（{baseline[:10]}）"

    return _merge_changes(changes), "无法确认官方版本基线（缺少对应版本标签与远端跟踪分支）"


def _merge_changes(changes: Sequence[SourceChange]) -> list[SourceChange]:
    by_path: dict[str, list[SourceChange]] = {}
    for change in changes:
        by_path.setdefault(change.path, []).append(change)

    priority = {
        KIND_MODIFIED: 0,
        KIND_ADDED: 1,
        KIND_DELETED: 2,
        KIND_RENAMED: 3,
        KIND_MOUNT_OVERRIDE: 4,
    }
    merged: list[SourceChange] = []
    for path in sorted(by_path):
        items = by_path[path]
        ordered = sorted(items, key=lambda c: (priority.get(c.kind, 99), c.detail))
        primary = ordered[0]
        extra_details: list[str] = []
        if primary.detail:
            extra_details.append(primary.detail)
        for item in ordered[1:]:
            if item.kind == KIND_MOUNT_OVERRIDE:
                text = item.detail or KIND_LABELS[KIND_MOUNT_OVERRIDE]
                if text not in extra_details:
                    extra_details.append(text)
            elif item.detail and item.detail not in extra_details:
                extra_details.append(item.detail)
        merged.append(SourceChange(path, primary.kind, "，".join(extra_details)))
    return merged


def _build_report(
    *,
    mode: str,
    source_kind: str,
    changes: Sequence[SourceChange],
    error: str = "",
) -> SourceIntegrityReport:
    merged = tuple(_merge_changes(changes))
    if not merged and not error:
        return SourceIntegrityReport(ok=True, mode=mode, source_kind=source_kind)

    detail_lines: list[str] = [
        f"源码完整性校验未通过（形态={mode}，校验方式={source_kind}）",
    ]
    if error:
        detail_lines.append(f"原因：{error}")
    if merged:
        detail_lines.append(f"检测到以下本地修改/覆盖文件（共 {len(merged)} 个）：")
        for item in merged[:_LOG_MAX_ITEMS]:
            detail_lines.append(f"  - {item.format_label()}")
        if len(merged) > _LOG_MAX_ITEMS:
            detail_lines.append(f"  …其余 {len(merged) - _LOG_MAX_ITEMS} 个文件已省略")
    detail_lines.append("已阻止本次更新（不提供强制更新，请还原上述文件或使用未修改的官方版本后重试）")

    if merged:
        shown = "、".join(item.format_label() for item in merged[:_SUMMARY_MAX_ITEMS])
        tail = f"…等共 {len(merged)} 个文件，详见更新日志" if len(merged) > _SUMMARY_MAX_ITEMS else ""
        if error:
            summary = f"检测到本地源码修改（共 {len(merged)} 个文件：{shown}{tail}；另：{error}），已阻止更新"
        else:
            summary = f"检测到本地源码修改（共 {len(merged)} 个文件：{shown}{tail}），已阻止更新"
    else:
        summary = f"无法验证源码完整性（{error}），已阻止更新"

    return SourceIntegrityReport(
        ok=False,
        mode=mode,
        source_kind=source_kind,
        changes=merged,
        error=error,
        summary=summary,
        detail_lines=tuple(detail_lines),
    )


def check_source_integrity(
    *,
    mode: str,
    app_dir: str | Path,
    data_dir: str | Path | None = None,
    current_version: str = "",
    excludes: Sequence[str] = (),
    manifest_path: str | Path | None = None,
    mountinfo_path: str | Path = "/proc/self/mountinfo",
    git_runner: GitRunner | None = None,
    expected_commit: str | None = None,
) -> SourceIntegrityReport:
    """Run unified pre-update source integrity checks for Docker or source installs."""
    root = Path(app_dir).resolve()
    if mode not in ("docker", "systemd", "bare"):
        return _build_report(
            mode=str(mode or "unknown"),
            source_kind="unverified",
            changes=(),
            error=f"不支持的运行形态（{mode}）",
        )

    if mode == "docker":
        target_manifest = Path(manifest_path) if manifest_path is not None else Path(DEFAULT_MANIFEST_PATH)
        manifest_changes, manifest_error = verify_manifest(
            root,
            target_manifest,
            expected_version=current_version,
            excludes=excludes,
        )
        mount_changes, mount_error = detect_mount_overrides(
            root,
            data_dir=data_dir,
            excludes=excludes,
            manifest_path=target_manifest,
            mountinfo_path=mountinfo_path,
        )
        combined_error = "；".join(e for e in (manifest_error, mount_error) if e)
        return _build_report(
            mode=mode,
            source_kind="manifest",
            changes=[*manifest_changes, *mount_changes],
            error=combined_error,
        )

    run_git = git_runner or _default_git_runner(root)
    rc_git, _ = run_git(["rev-parse", "--git-dir"], 15)
    if rc_git == 0:
        git_changes, git_error = verify_git_source(
            root,
            current_version=current_version,
            excludes=excludes,
            git_runner=run_git,
            expected_commit=expected_commit,
        )
        return _build_report(
            mode=mode,
            source_kind="git",
            changes=git_changes,
            error=git_error,
        )

    # Non-git source deployment: fall back to shipped source manifest if present, else fail closed.
    candidates: list[Path] = []
    if manifest_path is not None:
        candidates.append(Path(manifest_path))
    else:
        candidates.append(root / FALLBACK_MANIFEST_FILENAME)
        candidates.append(Path(DEFAULT_MANIFEST_PATH))
    chosen = next((p for p in candidates if p.exists() or p.is_symlink()), None)
    if chosen is not None:
        manifest_changes, manifest_error = verify_manifest(
            root,
            chosen,
            expected_version=current_version,
            excludes=excludes,
        )
        return _build_report(
            mode=mode,
            source_kind="manifest",
            changes=manifest_changes,
            error=manifest_error,
        )
    return _build_report(
        mode=mode,
        source_kind="unverified",
        changes=(),
        error="源码目录既非 Git 仓库也缺少受管源码清单",
    )


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify Parrot managed source integrity manifest.")
    parser.add_argument("--app-dir", default=".", help="Parrot application root containing server.py and src/")
    parser.add_argument("--version", default=None, help="Explicit version string (defaults to src/__init__.__version__)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write-manifest", help="Write managed source manifest JSON to this path")
    group.add_argument("--verify-manifest", help="Verify app-dir against an existing manifest JSON")
    args = parser.parse_args(argv)

    if args.write_manifest:
        manifest = write_manifest(args.write_manifest, args.app_dir, version=args.version)
        print(f"wrote source manifest: version={manifest['version']} files={len(manifest['files'])}")
        return 0

    changes, error = verify_manifest(
        args.app_dir,
        args.verify_manifest,
        expected_version=args.version or _read_app_version(Path(args.app_dir).resolve()),
    )
    report = _build_report(mode="docker", source_kind="manifest", changes=changes, error=error)
    if report.ok:
        print("source manifest verified ok")
        return 0
    for line in report.detail_lines:
        print(line, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
