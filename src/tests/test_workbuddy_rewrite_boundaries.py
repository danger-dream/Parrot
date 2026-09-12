"""Independent-review regressions: markup exclusion and rejected config writes."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from src import config, oauth_manager
from src.tests.test_workbuddy_channel import env
from src.tests.test_workbuddy_request_rewrite import private_config, ENV, ENV_SAFE, IDENTITY
from src.workbuddy_request_rewrite import RewriteConfigError

BLOCK = f"<env>\n{ENV}: main\n</env>"


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("prefix,suffix,changed", [
    ("<example>Quoted template:\n", "\n</example>", False),
    ("<!--\n", "\n-->", False),
    ("<!-- quoted template:\n", "\n-->text", False),
    ("<![CDATA[\n", "\n]]>", False),
    ('<quote note="a > b">Example:\n', "\n</quote>", False),
    ('<quote\n note="inline">Example:\n', "\n</quote>", False),
    ('<context note="unrelated" />\n', "", True),
    ('<context note="a > b"/>\n', "", True),
    ('<context\n note="unrelated" />\n', "", True),
    ("<context/>\n", "", True),
    ("<context>unrelated inline text</context>\n", "", True),
    ("```xml\n<quote>\n```\n", "", True),
    ("", "", True),
])
async def test_inline_markup_and_selfclosing_scope_through_real_builder(env, ingress, prefix, suffix, changed):
    _, ch = env
    text = prefix + BLOCK + suffix
    body = {"model": "glm-fixture"}
    if ingress == "anthropic":
        body.update(system=[{"type": "text", "text": prefix}, {"type": "text", "text": BLOCK + suffix}],
                    max_tokens=16, messages=[{"role": "user", "content": "ping"}])
    elif ingress == "responses":
        body.update(instructions=text, input="ping")
    else:
        body.update(messages=[{"role": "system", "content": text}, {"role": "user", "content": "ping"}])
    before = copy.deepcopy(body)
    emitted = json.loads((await ch.build_upstream_request(body, "glm-fixture", ingress_protocol=ingress)).body)
    system = emitted["messages"][1]["content"]
    assert (ENV_SAFE in system) is changed
    assert (ENV in system) is (not changed)
    assert body == before
    # References and comments remain intact, not merely stripped from the body.
    if not changed:
        assert BLOCK in system


async def test_nested_quote_inside_real_env_does_not_change_quoted_label(env):
    _, ch = env
    text = f"{IDENTITY}\n<env>\n<quote>Example:\n{ENV}: quoted\n</quote>\n{ENV}: real\n</env>"
    body = {"messages": [{"role": "system", "content": text}, {"role": "user", "content": "ping"}]}
    output = json.loads((await ch.build_upstream_request(body, "glm-fixture", ingress_protocol="chat")).body)["messages"][1]["content"]
    assert ENV + ": quoted" in output and ENV_SAFE + ": real" in output
    assert ENV_SAFE + ": quoted" not in output and body["messages"][0]["content"] == text


def manual_edit(path, cfg):
    previous = path.stat().st_mtime
    path.write_text(json.dumps(cfg))
    os.utime(path, (previous + 2, previous + 2))


@pytest.mark.parametrize("read_first", ["get", "reload", "none"])
@pytest.mark.parametrize("writer", ["save", "update", "update-skip"])
def test_invalid_disk_is_write_protected_before_mutator(private_config, read_first, writer):
    path, _ = private_config
    old = config.get()
    original_bytes = path.read_bytes()
    bad = copy.deepcopy(old)
    bad["workbuddy"]["requestRewrite"]["rules"][0]["scope"] = "typo_scope"
    bad["timeouts"]["firstByte"] = 47
    manual_edit(path, bad)
    rejected = path.read_bytes()
    callbacks, mutations = [], []
    config.on_reload(lambda cfg: callbacks.append(cfg))
    try:
        if read_first == "get":
            assert config.get() is old
        elif read_first == "reload":
            with pytest.raises(RewriteConfigError):
                config.reload()
        with pytest.raises(RewriteConfigError, match="correct the config file"):
            if writer == "save":
                config.save()
            else:
                config.update(lambda c: mutations.append(1), skip_if_unchanged=writer == "update-skip")
        assert path.read_bytes() == rejected and config.get() is old
        assert not mutations and not callbacks
        # Correct the actual file, preserving the OTHER manual edit. The next
        # unrelated update must use this version, not the previous LKG snapshot.
        bad["workbuddy"]["requestRewrite"]["rules"][0]["scope"] = "system_prefix_line"
        manual_edit(path, bad)
        config.update(lambda c: c.update(review_write_probe=True))
        saved = json.loads(path.read_text())
        assert saved["timeouts"]["firstByte"] == 47 and saved["review_write_probe"] is True
        assert len(callbacks) == 1 and config._rejected_rewrite_version is None
    finally:
        path.write_bytes(original_bytes)
        config.reload()


@pytest.mark.parametrize("read_first", [False, True])
async def test_real_account_update_cannot_overwrite_rejected_manual_file(env, read_first):
    _, ch = env
    path = Path(config.path())
    old = config.get()
    original_bytes = path.read_bytes()
    bad = copy.deepcopy(old)
    bad["workbuddy"]["requestRewrite"]["rules"][0]["scope"] = "typo_scope"
    bad["timeouts"]["firstByte"] = 47
    manual_edit(path, bad)
    rejected = path.read_bytes()
    try:
        if read_first:
            assert config.get() is old
        with pytest.raises(RewriteConfigError):
            oauth_manager.set_enabled(ch.account_key, False, reason="user")
        assert path.read_bytes() == rejected
        assert oauth_manager.get_account(ch.account_key) == old["oauthAccounts"][0]
        assert oauth_manager.get_account(ch.account_key).get("enabled", True) is True
        bad["workbuddy"]["requestRewrite"]["rules"][0]["scope"] = "system_prefix_line"
        manual_edit(path, bad)
        oauth_manager.set_enabled(ch.account_key, False, reason="user")
        saved = json.loads(path.read_text())
        assert saved["timeouts"]["firstByte"] == 47
        assert saved["oauthAccounts"][0]["enabled"] is False
    finally:
        path.write_bytes(original_bytes)
        config.reload()


@pytest.mark.parametrize("writer", ["save", "noop-update"])
def test_valid_manual_reload_via_write_notifies_callbacks_once(private_config, writer):
    path, _ = private_config
    cfg = copy.deepcopy(config.get())
    cfg["timeouts"]["firstByte"] = 47
    manual_edit(path, cfg)
    callbacks = []
    config.on_reload(lambda snapshot: callbacks.append(snapshot))
    if writer == "save":
        config.save()
    else:
        config.update(lambda c: None, skip_if_unchanged=True)
    assert len(callbacks) == 1 and callbacks[0]["timeouts"]["firstByte"] == 47
    assert config.get()["timeouts"]["firstByte"] == 47 and len(callbacks) == 1
