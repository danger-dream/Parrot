from __future__ import annotations

from pathlib import Path
import runpy

from ._isolation import isolate

isolate()

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "is_latest_release_tag.py"
_mod = runpy.run_path(str(_SCRIPT))
is_latest_release_tag = _mod["is_latest_release_tag"]
release_tuple = _mod["release_tuple"]
main = _mod["main"]


def test_plain_release_tuple_parses_v_prefix_and_rejects_prerelease():
    assert release_tuple("v0.30.1") == (0, 30, 1)
    assert release_tuple("0.30.1") == (0, 30, 1)
    assert release_tuple("v0.29.11") == (0, 29, 11)
    assert release_tuple("v0.30.1-rc.1") is None
    assert release_tuple("main") is None
    assert release_tuple("v0.30") is None


def test_newer_patch_moves_latest_backfill_does_not():
    tags = ["v0.29.11", "v0.30.0"]
    assert is_latest_release_tag("v0.30.1", tags) is True
    assert is_latest_release_tag("0.30.1", tags) is True
    assert is_latest_release_tag("v0.29.11", ["v0.30.0", "v0.29.11"]) is False
    assert is_latest_release_tag("v0.30.0", ["v0.30.0", "v0.29.11"]) is True


def test_equal_current_release_may_refresh_latest():
    assert is_latest_release_tag("v0.30.3", ["v0.30.3", "v0.30.2"]) is True


def test_prerelease_never_moves_latest():
    assert is_latest_release_tag("v0.31.0-rc.1", ["v0.30.3"]) is False


def test_cli_prints_true_false(capsys):
    assert main(["v0.30.1", "v0.30.0", "v0.29.11"]) == 0
    assert capsys.readouterr().out == "true\n"
    assert main(["v0.29.11", "v0.30.0", "v0.29.11"]) == 0
    assert capsys.readouterr().out == "false\n"
