from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def production_python():
    yield ROOT / "server.py"
    for path in (ROOT / "src").rglob("*.py"):
        if "tests" not in path.parts:
            yield path


def test_no_private_state_store_coupling_or_state_sqlite_runtime():
    forbidden = ("state_db._get_conn", "state_db._write_lock", "state_db._db_path",
                 "state_db.online_backup", "bootstrap_recover")
    failures = []
    for path in production_python():
        text = path.read_text(encoding="utf-8")
        if path.name not in {"state_migration.py"}:
            for token in forbidden:
                if token in text: failures.append(f"{path.relative_to(ROOT)}: {token}")
        if path.name == "state_db.py":
            assert "import sqlite3" not in text
    assert not failures, "\n".join(failures)


def test_business_modules_do_not_know_json_snapshot_paths():
    allowed = {"state_store.py", "state_db.py", "config.py"}
    failures = []
    for path in production_python():
        if path.name in allowed: continue
        text = path.read_text(encoding="utf-8")
        if "runtime-cache.json" in text or "durable-state.json" in text:
            failures.append(str(path.relative_to(ROOT)))
    assert not failures
