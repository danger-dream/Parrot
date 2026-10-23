from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "webui"
BUNDLE = ROOT / "src" / "webui"
# Must stay identical to webui/vite.config.ts::sourceDigest.
SOURCE_FILES = ("index.html", "package.json", "pnpm-lock.yaml", "tsconfig.json", "vite.config.ts")
SOURCE_DIRS = ("src",)


def source_digest() -> str:
    files = [SOURCE / name for name in SOURCE_FILES]
    for directory in SOURCE_DIRS:
        files.extend(path for path in (SOURCE / directory).rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for relative in sorted(path.relative_to(SOURCE).as_posix() for path in files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((SOURCE / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_committed_webui_bundle_matches_frontend_source():
    meta_path = BUNDLE / "build-meta.json"
    assert meta_path.is_file(), "src/webui is missing; run `pnpm install && pnpm build` in webui/"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["sourceDigest"] == source_digest(), (
        "webui/ changed after the last build; run `pnpm build` in webui/ and commit src/webui"
    )


def test_webui_index_references_existing_hashed_assets():
    index = (BUNDLE / "index.html").read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="/ui/(assets/[^"]+)"', index)
    assert assets, "index.html does not reference any bundled assets"
    for asset in assets:
        assert (BUNDLE / asset).is_file(), asset
