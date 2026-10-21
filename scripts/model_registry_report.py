#!/usr/bin/env python3
"""Shadow-compare today's model listing and routing with the unified registry.

Copies config and state into a throwaway directory first, because rebuilding
channels writes channel state; the live data directory is only read.

    ./venv/bin/python -I scripts/model_registry_report.py --out /tmp/registry.md \
        --merge deepseek-flash=deepseek-v4.1-flash
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPIED = ("config.json", "durable-state.json", "runtime-cache.json", "models_dev_catalog.json.gz")


def requested_names(log_dir: str, days: int) -> set[str]:
    since = time.time() - days * 86400
    names: set[str] = set()
    for path in sorted(glob.glob(os.path.join(log_dir, "*.db"))):
        if os.path.getmtime(path) < since:
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as db:
            rows = db.execute("SELECT DISTINCT requested_model FROM request_log WHERE created_at >= ?", (since,))
            names.update(str(row[0]).strip() for row in rows if row[0] and str(row[0]).strip())
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=ROOT, help="live data directory to read (default: repo root)")
    parser.add_argument("--out", required=True, help="markdown report path")
    parser.add_argument("--merge", action="append", default=[], metavar="FROM=INTO",
                        help="proposed manual merge rule, repeatable")
    parser.add_argument("--days", type=int, default=30, help="also compare names requested in the last N days")
    args = parser.parse_args()

    rules = []
    for item in args.merge:
        source, _, target = item.partition("=")
        if not source or not target:
            parser.error(f"--merge expects FROM=INTO, got {item!r}")
        rules.append({"type": "merge", "from": source, "into": target})

    with tempfile.TemporaryDirectory(prefix="parrot-registry-") as tmp:
        for name in COPIED:
            if os.path.exists(os.path.join(args.data_dir, name)):
                shutil.copy2(os.path.join(args.data_dir, name), tmp)
        os.environ["ANTHROPIC_PROXY_DATA_DIR"] = tmp
        sys.path.insert(0, ROOT)
        from src import config, state_db
        state_db.init()
        from src.openai.channel.registration import register_factories
        register_factories()
        from src.channel import registry
        registry.rebuild_from_config()
        from src.model_registry import report

        cfg = config.get()
        log_dir = os.path.join(args.data_dir, cfg.get("logDir", "logs"))
        names = requested_names(log_dir, args.days) if args.days > 0 else set()
        rules += list((cfg.get("modelRegistry") or {}).get("rules") or [])
        result = report.compare(cfg, rules=rules, extra_names=names)
        text = report.render(result, cfg)
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {args.out}: {len(result.registry.models)} models, {len(result.routes)} names compared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
