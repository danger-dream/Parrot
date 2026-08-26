"""方案 A 主键迁移 + 同邮箱双账号共存专用测试。

覆盖（覆盖的改动均来自 2026-04-20 同邮箱 Claude + OpenAI 共存修复）：

  - src/oauth_ids.py              工具函数正确性
  - src/state_db.py               composite-key 迁移幂等性 + 事务完整性
  - src/oauth_manager.py          add_account / get_account / delete_account
                                  / set_enabled / update_models / _refresh_locks
                                  对联合键的精确匹配语义
  - src/channel/oauth_channel.py  self.account_key / self.key 新格式
  - src/channel/openai_oauth_channel.py 同上
  - src/channel/registry.py       get_channel 新老格式兜底
  - src/telegram/menus/oauth_menu.py _resolve_to_account_key 兜底

运行：./venv/bin/python -m src.tests.test_composite_key_migration
"""

from __future__ import annotations

# 测试隔离：把 config.json / state.db / logs 重定向到 tmpdir，不污染生产
import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import os
import sqlite3
import sys
import traceback


def _import_modules():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from src import config, oauth_ids, oauth_manager, state_db
    from src.channel import oauth_channel, openai_oauth_channel, registry
    from src.telegram.menus import oauth_menu
    return {
        "config": config,
        "oauth_ids": oauth_ids,
        "oauth_manager": oauth_manager,
        "state_db": state_db,
        "OAuthChannel": oauth_channel.OAuthChannel,
        "OpenAIOAuthChannel": openai_oauth_channel.OpenAIOAuthChannel,
        "registry": registry,
        "oauth_menu": oauth_menu,
    }


def _setup(m):
    """每个测试前清配置 + 清 state.db 相关表。"""
    state_db = m["state_db"]
    state_db.init()
    # 清所有 oauth / channel 状态，避免跨测试污染
    for row in state_db.quota_load_all():
        state_db.quota_delete(row["account_key"])
    state_db.perf_delete(); state_db.error_delete(); state_db.affinity_delete()
    state_db.client_affinity_delete(); state_db.schema_meta_delete(state_db.COMPOSITE_KEY_FLAG)

    def clear_accounts(c):
        c["oauthAccounts"] = []
        c.setdefault("oauth", {})["mockMode"] = True
    m["config"].update(clear_accounts)
    # 清刷新锁 dict，保证每个测试独立
    m["oauth_manager"]._refresh_locks.clear()


# ==============================================================
# oauth_ids 工具函数
# ==============================================================

def test_account_key_from_dict(m):
    ak = m["oauth_ids"].account_key({
        "email": "a@b.c", "provider": "openai",
        "chatgpt_account_id": "acct-a",
    })
    assert ak == "openai:a@b.c:acct-a", ak
    print("  [PASS] account_key(dict) uses OpenAI composite identity")


def test_account_key_default_provider(m):
    ak = m["oauth_ids"].account_key({"email": "a@b.c"})  # 无 provider → claude
    assert ak == "claude:a@b.c", ak
    print("  [PASS] account_key defaults provider='claude' when missing")


def test_account_key_explicit_args(m):
    ak = m["oauth_ids"].account_key("openai", "x@y")
    assert ak == "openai:x@y", ak
    print("  [PASS] account_key(provider, email) positional form")


def test_split_account_key_threeseg(m):
    prov, email = m["oauth_ids"].split_account_key("openai:a@b.c")
    assert prov == "openai" and email == "a@b.c", (prov, email)
    print("  [PASS] split_account_key: three-segment form")


def test_split_account_key_fallback(m):
    # 无 ":"：整段当 email，provider 回退默认
    prov, email = m["oauth_ids"].split_account_key("a@b.c")
    assert prov == "claude" and email == "a@b.c", (prov, email)
    print("  [PASS] split_account_key: no-colon fallback")


def test_channel_key_roundtrip(m):
    ck = m["oauth_ids"].channel_key_for({
        "email": "a@b.c", "provider": "openai",
        "chatgpt_account_id": "acct-a",
    })
    assert ck == "oauth:openai:a@b.c:acct-a", ck
    assert m["oauth_ids"].email_from_channel_key(ck) == "a@b.c:acct-a"
    assert m["oauth_ids"].provider_from_channel_key(ck) == "openai"
    print("  [PASS] channel_key_for + reverse extractors roundtrip")


# ==============================================================
# state_db 主键迁移
# ==============================================================

def test_migration_idempotent_when_flag_set(m):
    _setup(m); sdb=m["state_db"]
    sdb.schema_meta_set(sdb.COMPOSITE_KEY_FLAG,sdb.COMPOSITE_KEY_VERSION)
    stats=sdb.run_composite_key_migration({"a@b.c":"claude:a@b.c"})
    assert stats["skipped"] is True


def test_migration_orphan_cleanup_deletes_only_exact_bare_storage_key(m):
    _setup(m); sdb=m["state_db"]
    email = "shared@example.com"
    sdb.quota_save(email, {"five_hour_util": 99.0}, email=email)
    sdb.quota_save("claude:" + email, {"five_hour_util": 10.0}, email=email)
    sdb.quota_save("openai:" + email + ":workspace", {"five_hour_util": 20.0}, email=email)
    sdb.run_composite_key_migration({})
    keys = {row["account_key"] for row in sdb.quota_load_all()}
    assert email not in keys
    assert "claude:" + email in keys
    assert "openai:" + email + ":workspace" in keys


def test_migration_renames_rows_and_drops_bare_orphan(m):
    _setup(m); sdb=m["state_db"]
    sdb.quota_save("x@y.com", {"five_hour_util":15.0})
    sdb.quota_save("gone@y.com", {"five_hour_util":99.0})
    sdb.perf_save("oauth:x@y.com","model",{"last_updated":1})
    sdb.error_save("oauth:x@y.com","model",1,None,None)
    stats=sdb.run_composite_key_migration({"x@y.com":"claude:x@y.com"})
    assert stats["skipped"] is False
    assert stats["migrated_quota_rows"]==1 and stats["migrated_channel_rows"]==2
    assert sdb.quota_load("claude:x@y.com")["five_hour_util"]==15.0
    assert all(r["channel_key"]=="oauth:claude:x@y.com" for r in sdb.perf_load_all()+sdb.error_load_all())
    assert not any(r["account_key"]=="gone@y.com" for r in sdb.quota_load_all())


# ==============================================================
# oauth_manager —— 同邮箱 Claude + OpenAI 共存的核心场景
# ==============================================================

def test_add_same_email_different_provider_ok(m):
    """同邮箱 Claude + OpenAI 可并存，不再报 'email already exists'。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "dup@x.com", "provider": "claude",
        "access_token": "c-at", "refresh_token": "c-rt",
    })
    # 同邮箱 + 不同 provider：应该成功
    om.add_account({
        "email": "dup@x.com", "provider": "openai",
        "access_token": "o-at", "refresh_token": "o-rt",
    })
    accounts = om.list_accounts()
    providers = sorted(a.get("provider") for a in accounts if a.get("email") == "dup@x.com")
    assert providers == ["claude", "openai"], providers
    print("  [PASS] add_account allows same email across different providers")


def test_add_same_email_openai_different_workspaces_ok(m):
    """同邮箱 OpenAI 可挂多个 workspace；email 只是展示字段。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "same@x.com", "provider": "openai",
        "access_token": "a", "refresh_token": "r",
        "chatgpt_account_id": "acct-one",
    })
    om.add_account({
        "email": "same@x.com", "provider": "openai",
        "access_token": "a2", "refresh_token": "r2",
        "chatgpt_account_id": "acct-two",
    })
    assert om.get_account("openai:same@x.com:acct-one")["refresh_token"] == "r"
    assert om.get_account("openai:same@x.com:acct-two")["refresh_token"] == "r2"
    assert om.get_account("openai:same@x.com") is None
    print("  [PASS] add_account allows same OpenAI email across workspaces")


def test_add_openai_same_workspace_different_email_ok(m):
    """同 workspace_id 下不同邮箱必须各自保留，不再 registry/key 覆盖。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "first@x.com", "provider": "openai",
        "access_token": "a", "refresh_token": "r1",
        "chatgpt_account_id": "shared-workspace",
    })
    om.add_account({
        "email": "second@x.com", "provider": "openai",
        "access_token": "a2", "refresh_token": "r2",
        "chatgpt_account_id": "shared-workspace",
    })
    assert om.get_account("openai:first@x.com:shared-workspace")["refresh_token"] == "r1"
    assert om.get_account("openai:second@x.com:shared-workspace")["refresh_token"] == "r2"
    assert om.get_account("openai:shared-workspace") is None
    print("  [PASS] add_account allows same OpenAI workspace across different emails")


def test_get_account_isolates_by_provider(m):
    """同邮箱两账号，get_account 按 account_key 精确定位。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "iso@x.com", "provider": "claude",
                    "access_token": "CLAUDE-AT", "refresh_token": "c"})
    om.add_account({"email": "iso@x.com", "provider": "openai",
                    "access_token": "OPENAI-AT", "refresh_token": "o",
                    "chatgpt_account_id": "acct-iso"})

    claude = om.get_account("claude:iso@x.com")
    openai = om.get_account("openai:iso@x.com:acct-iso")
    assert claude and claude["access_token"] == "CLAUDE-AT"
    assert openai and openai["access_token"] == "OPENAI-AT"
    # 纯 email 在同邮箱多 provider 时有歧义，不应静默选中。
    assert om.get_account("iso@x.com") is None
    print("  [PASS] get_account(account_key) isolates same-email dual accounts")


def test_delete_account_only_targets_one_of_same_email(m):
    """删除 account_key 只影响对应那一条，同邮箱另一个 provider 保留。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "d@x.com", "provider": "claude",
                    "access_token": "a", "refresh_token": "b"})
    om.add_account({"email": "d@x.com", "provider": "openai",
                    "access_token": "a", "refresh_token": "b",
                    "chatgpt_account_id": "acct-d"})

    om.delete_account("claude:d@x.com")
    remaining = [a for a in om.list_accounts() if a.get("email") == "d@x.com"]
    assert len(remaining) == 1 and remaining[0]["provider"] == "openai", remaining
    print("  [PASS] delete_account targets exactly one (provider, email) pair")


def test_set_enabled_only_targets_one_of_same_email(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "t@x.com", "provider": "claude",
                    "access_token": "a", "refresh_token": "b"})
    om.add_account({"email": "t@x.com", "provider": "openai",
                    "access_token": "a", "refresh_token": "b",
                    "chatgpt_account_id": "acct-t"})

    om.set_enabled("claude:t@x.com", False, reason="user")
    claude = om.get_account("claude:t@x.com")
    openai = om.get_account("openai:t@x.com:acct-t")
    assert claude["enabled"] is False and claude["disabled_reason"] == "user"
    assert openai["enabled"] is True and openai.get("disabled_reason") in (None, "",)
    print("  [PASS] set_enabled isolates state per (provider, email)")


def test_update_models_only_targets_one_of_same_email(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "u@x.com", "provider": "claude",
                    "access_token": "a", "refresh_token": "b"})
    om.add_account({"email": "u@x.com", "provider": "openai",
                    "access_token": "a", "refresh_token": "b",
                    "chatgpt_account_id": "acct-u"})

    om.update_models("openai:u@x.com:acct-u", ["gpt-5"])
    claude = om.get_account("claude:u@x.com")
    openai = om.get_account("openai:u@x.com:acct-u")
    assert openai["models"] == ["gpt-5"], openai.get("models")
    assert claude.get("models") != ["gpt-5"], claude.get("models")
    print("  [PASS] update_models isolates per (provider, email)")


def test_refresh_locks_separated_per_account_key(m):
    """同邮箱不同 provider → 两把独立刷新锁，互不阻塞。"""
    _setup(m)
    om = m["oauth_manager"]
    lock_claude = om._get_refresh_lock("claude:p@x.com")
    lock_openai = om._get_refresh_lock("openai:p@x.com")
    assert lock_claude is not lock_openai
    print("  [PASS] refresh locks are isolated per account_key")


# ==============================================================
# Channel 层
# ==============================================================

def test_oauth_channel_key_format(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "ch@x.com", "provider": "claude",
                    "access_token": "a", "refresh_token": "b"})
    acc = om.get_account("claude:ch@x.com")
    ch = m["OAuthChannel"](acc, [])
    assert ch.account_key == "claude:ch@x.com", ch.account_key
    assert ch.key == "oauth:claude:ch@x.com", ch.key
    assert ch.email == "ch@x.com"
    print("  [PASS] OAuthChannel uses three-segment key format")


def test_openai_oauth_channel_key_format(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "co@x.com", "provider": "openai",
        "access_token": "a", "refresh_token": "b",
        "chatgpt_account_id": "acct-x", "plan_type": "plus",
    })
    acc = om.get_account("openai:co@x.com:acct-x")
    ch = m["OpenAIOAuthChannel"](acc)
    assert ch.account_key == "openai:co@x.com:acct-x", ch.account_key
    assert ch.key == "oauth:openai:co@x.com:acct-x", ch.key
    print("  [PASS] OpenAIOAuthChannel uses composite key format")


def test_registry_get_channel_new_and_legacy_key(m):
    """registry.get_channel 对新三段式 + 老两段式都能命中。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "reg@x.com", "provider": "claude",
                    "access_token": "a", "refresh_token": "b"})
    m["registry"].rebuild_from_config()

    ch_new = m["registry"].get_channel("oauth:claude:reg@x.com")
    assert ch_new is not None, "new key format should hit"
    # 老格式 fallback
    ch_old = m["registry"].get_channel("oauth:reg@x.com")
    assert ch_old is not None, "legacy key format should still hit via fallback"
    assert ch_new is ch_old
    print("  [PASS] registry.get_channel: both new and legacy key hit same channel")


# ==============================================================
# TG menu 辅助
# ==============================================================

def test_resolve_to_account_key_upgrades_plain_email(m):
    """_resolve_to_account_key 对纯 email 入参自动回查 provider 补成 account_key。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "r@x.com", "provider": "openai",
                    "access_token": "a", "refresh_token": "b",
                    "chatgpt_account_id": "acct-r"})
    ak = m["oauth_menu"]._resolve_to_account_key("r@x.com")
    assert ak == "openai:r@x.com:acct-r", ak
    # 已经是 account_key 时原样返回
    ak2 = m["oauth_menu"]._resolve_to_account_key("openai:r@x.com:acct-r")
    assert ak2 == "openai:r@x.com:acct-r"
    # None 传入：原样返回 None
    assert m["oauth_menu"]._resolve_to_account_key(None) is None
    print("  [PASS] _resolve_to_account_key upgrades bare email to account_key")



def test_openai_workspace_key_migration_unique_rows_and_config(m):
    """unique email→workspace：state / logs / image / priorityOrders 一并迁移。"""
    _setup(m)
    config = m["config"]
    om = m["oauth_manager"]
    sdb = m["state_db"]

    def seed_cfg(c):
        c["oauthAccounts"] = [{
            "email": "uniq@openai.test", "provider": "openai",
            "access_token": "at", "refresh_token": "rt",
            "chatgpt_account_id": "acct-uniq",
        }]
        c["loadBalancing"] = {
            "priorityOrders": {
                "openai": ["oauth:openai:uniq@openai.test"],
                "anthropic": ["oauth:openai:uniq@openai.test"],
            }
        }
        images = c.setdefault("images", {})
        images["disabledAccounts"] = [
            "openai:uniq@openai.test",
            "oauth:openai:uniq@openai.test",
            "uniq@openai.test",
        ]
    config.update(seed_cfg)

    # state rows
    sdb.quota_save_openai_snapshot("openai:uniq@openai.test", {"primary_used_pct": 42, "primary_window_min": 10080}, email="uniq@openai.test")
    sdb.perf_save("oauth:openai:uniq@openai.test", "gpt-5", {"last_updated": 1})
    sdb.error_save("oauth:openai:uniq@openai.test", "gpt-5", 0, None, None)
    sdb.affinity_upsert("fp", "oauth:openai:uniq@openai.test", "gpt-5", 1)
    sdb.client_affinity_upsert("client", "oauth:openai:uniq@openai.test", "gpt-5", 1)

    # log rows
    from src import log_db, image_db
    log_db.init()
    log_db.insert_pending("req-uniq", "1.1.1.1", "ak", "gpt-5", True, 1, 0, {}, {}, ingress_protocol="responses")
    log_db.finish_success("req-uniq", "oauth:openai:uniq@openai.test", "oauth", "gpt-5")
    log_db.record_retry_attempt("req-uniq", 1, "oauth:openai:uniq@openai.test", "oauth", "gpt-5", 1.0)

    # image rows
    image_db.init()
    image_log_id = image_db.start_call(
        request_id="img-uniq", api_key_name="ak", action="generate",
        main_model="gpt-image-2", tool_model="gpt-image-2", size="1024x1024",
        prompt_preview="p", prompt_hash="h",
    )
    image_db.finish_call(image_log_id, status="success", account_key="openai:uniq@openai.test", account_email="uniq@openai.test")
    image_db.start_attempt(image_log_id, request_id="img-uniq", account_key="openai:uniq@openai.test", account_email="uniq@openai.test")

    stats = om.bootstrap_openai_workspace_key_migration()
    assert stats["mapping_count"] == 3, stats
    assert sdb.quota_load("openai:uniq@openai.test:acct-uniq")["email"] == "uniq@openai.test"
    # quota_load 允许 legacy openai:<email> 在唯一 email 时兜底解析；底层 PK 必须已迁移。
    assert not any(r["account_key"] == "openai:uniq@openai.test" for r in sdb.quota_load_all())
    assert all(r["channel_key"] == "oauth:openai:uniq@openai.test:acct-uniq" for r in sdb.perf_load_all())
    assert all(r["channel_key"] == "oauth:openai:uniq@openai.test:acct-uniq" for r in sdb.error_load_all())
    assert all(r["channel_key"] == "oauth:openai:uniq@openai.test:acct-uniq" for r in sdb.affinity_load_all())
    assert all(r["channel_key"] == "oauth:openai:uniq@openai.test:acct-uniq" for r in sdb.client_affinity_load_all())

    cfg = config.get()
    assert cfg["oauthAccounts"][0]["workspace_id"] == "acct-uniq"
    assert cfg["loadBalancing"]["priorityOrders"]["openai"] == ["oauth:openai:uniq@openai.test:acct-uniq"]
    assert cfg["loadBalancing"]["priorityOrders"]["anthropic"] == ["oauth:openai:uniq@openai.test:acct-uniq"]
    assert cfg["images"]["disabledAccounts"] == ["openai:uniq@openai.test:acct-uniq", "oauth:openai:uniq@openai.test:acct-uniq"]

    lconn = log_db._get_conn()
    assert lconn.execute("SELECT final_channel_key FROM request_log WHERE request_id=?", ("req-uniq",)).fetchone()[0] == "oauth:openai:uniq@openai.test:acct-uniq"
    assert lconn.execute("SELECT channel_key FROM retry_chain WHERE request_id=?", ("req-uniq",)).fetchone()[0] == "oauth:openai:uniq@openai.test:acct-uniq"
    iconn = image_db._get_conn()
    assert iconn.execute("SELECT account_key FROM image_call_logs WHERE request_id=?", ("img-uniq",)).fetchone()[0] == "openai:uniq@openai.test:acct-uniq"
    assert iconn.execute("SELECT account_key FROM image_attempt_logs WHERE request_id=?", ("img-uniq",)).fetchone()[0] == "openai:uniq@openai.test:acct-uniq"

    # idempotent second run: same mapping scope skips state rows and config/log/image stay stable.
    stats2 = om.bootstrap_openai_workspace_key_migration()
    assert stats2["state"]["skipped"] is True
    assert sdb.quota_load("openai:uniq@openai.test:acct-uniq") is not None
    print("  [PASS] openai workspace-key migration moves unique rows/config/logs/images idempotently")


def test_openai_workspace_key_migration_later_unique_mapping_still_runs(m):
    """早期映射已迁后，后续新增的 unique legacy mapping 仍能迁。"""
    _setup(m)
    config = m["config"]
    om = m["oauth_manager"]
    sdb = m["state_db"]

    def seed_first(c):
        c["oauthAccounts"] = [
            {"email": "first@openai.test", "provider": "openai", "access_token": "a1", "refresh_token": "r1", "chatgpt_account_id": "acct-first"},
        ]
    config.update(seed_first)
    sdb.quota_save_openai_snapshot("openai:first@openai.test", {"primary_used_pct": 1, "primary_window_min": 10080}, email="first@openai.test")
    om.bootstrap_openai_workspace_key_migration()

    def seed_second(c):
        c["oauthAccounts"].append(
            {"email": "second@openai.test", "provider": "openai", "access_token": "a2", "refresh_token": "r2", "chatgpt_account_id": "acct-second"}
        )
    config.update(seed_second)
    sdb.quota_save_openai_snapshot("openai:second@openai.test", {"primary_used_pct": 2, "primary_window_min": 10080}, email="second@openai.test")

    stats = om.bootstrap_openai_workspace_key_migration()
    assert stats["mapping_count"] == 6, stats
    assert stats["state"]["skipped"] is False, stats
    assert sdb.quota_load("openai:second@openai.test:acct-second")["email"] == "second@openai.test"
    assert not any(r["account_key"] == "openai:second@openai.test" for r in sdb.quota_load_all())
    print("  [PASS] openai workspace-key migration can process later unique mappings")


def test_openai_workspace_key_migration_skips_ambiguous_email(m):
    """同邮箱多个 OpenAI workspace：legacy email row 不迁，legacy key 不解析。"""
    _setup(m)
    config = m["config"]
    om = m["oauth_manager"]
    sdb = m["state_db"]

    def seed_cfg(c):
        c["oauthAccounts"] = [
            {"email": "amb@openai.test", "provider": "openai", "access_token": "a1", "refresh_token": "r1", "chatgpt_account_id": "acct-one"},
            {"email": "amb@openai.test", "provider": "openai", "access_token": "a2", "refresh_token": "r2", "chatgpt_account_id": "acct-two"},
        ]
        c["loadBalancing"] = {"priorityOrders": {"openai": ["oauth:openai:amb@openai.test"]}}
    config.update(seed_cfg)
    sdb.quota_save_openai_snapshot("openai:amb@openai.test", {"primary_used_pct": 9, "primary_window_min": 10080}, email="amb@openai.test")

    stats = om.bootstrap_openai_workspace_key_migration()
    assert stats["mapping_count"] == 4, stats
    assert sdb.quota_load("openai:amb@openai.test")["account_key"] == "openai:amb@openai.test"
    assert config.get()["loadBalancing"]["priorityOrders"]["openai"] == [
        "oauth:openai:amb@openai.test:acct-one",
        "oauth:openai:amb@openai.test:acct-two",
    ]
    assert om.get_account("openai:amb@openai.test") is None
    assert om.resolve_account_key("openai:amb@openai.test:acct-one") == "openai:amb@openai.test:acct-one"
    print("  [PASS] openai workspace-key migration skips ambiguous same-email workspaces")

# ==============================================================
# flatten_usage 单位透传（2026-04-20 朋友反馈的 1%→100% bug 防回退）
#
# 上游账户用量的百分比字段应保持 0..100 单位。
# （line 1208: Utilization: resp.FiveHour.Utilization 直接透传）。
# Anthropic /api/oauth/usage JSON body 返回的 utilization 已经是 0..100 百分比，
# 不应再做任何 × 100 或启发式单位换算。
# ==============================================================

def test_flatten_usage_one_percent_stays_one_percent(m):
    """⚠ 核心回归：用户用量 1% → 上游返回 utilization=1.0 → Parrot 存 1.0（不是 100.0）。

    历史启发式 'v <= 1.0 → v*100' 把 1.0 误判为 100%；新逻辑直接透传。
    """
    out = m["oauth_manager"].flatten_usage({
        "five_hour": {"utilization": 1.0, "resets_at": "x"},
        "seven_day": {"utilization": 1.0, "resets_at": "x"},
    })
    assert out["five_hour_util"] == 1.0, out["five_hour_util"]
    assert out["seven_day_util"] == 1.0, out["seven_day_util"]
    print("  [PASS] flatten_usage: 1.0 stays 1% (not 100%)")


def test_flatten_usage_preserves_percentage_units(m):
    """百分比值按 0..100 单位透传：5.0 → 5%，65.2 → 65.2%。"""
    for input_util, expected in [(5.0, 5.0), (65.2, 65.2), (99.9, 99.9)]:
        out = m["oauth_manager"].flatten_usage({
            "five_hour": {"utilization": input_util, "resets_at": "x"},
        })
        assert abs(out["five_hour_util"] - expected) < 1e-9, (input_util, out["five_hour_util"])
    print("  [PASS] flatten_usage: typical values (5.0/65.2/99.9) pass-through")


def test_flatten_usage_full_hundred_percent(m):
    """utilization=100.0 直接透传 100.0（即 100%），不会被误乘再变 10000%。"""
    out = m["oauth_manager"].flatten_usage({
        "five_hour": {"utilization": 100.0, "resets_at": "x"},
    })
    assert out["five_hour_util"] == 100.0, out["five_hour_util"]
    print("  [PASS] flatten_usage: 100.0 stays 100%")


def test_flatten_usage_zero(m):
    out = m["oauth_manager"].flatten_usage({
        "five_hour": {"utilization": 0.0, "resets_at": None},
    })
    assert out["five_hour_util"] == 0.0
    print("  [PASS] flatten_usage: 0.0 stays 0%")


def test_flatten_usage_fractional_sub_one(m):
    """utilization=0.5 意为 0.5%（不是 50%）——直接透传。"""
    out = m["oauth_manager"].flatten_usage({
        "seven_day_sonnet": {"utilization": 0.5, "resets_at": "x"},
        "seven_day_opus": {"utilization": 0.01, "resets_at": "x"},
    })
    assert out["sonnet_util"] == 0.5, out["sonnet_util"]
    assert out["opus_util"] == 0.01, out["opus_util"]
    print("  [PASS] flatten_usage: fractional <1 values pass through literally")


def test_flatten_usage_missing_utilization(m):
    out = m["oauth_manager"].flatten_usage({
        "five_hour": {"resets_at": None},
        "seven_day": None,
    })
    assert out["five_hour_util"] is None
    assert out["seven_day_util"] is None
    print("  [PASS] flatten_usage: None-safe for missing utilization / empty window")


def test_flatten_usage_preserves_resets_and_extra(m):
    """reset 时间与 extra_usage 字段照常展平；extra 金额按当前缓存单位 /100。"""
    out = m["oauth_manager"].flatten_usage({
        "five_hour": {"utilization": 42.5, "resets_at": "2026-04-20T12:00:00Z"},
        "seven_day": {"utilization": 80.0, "resets_at": "2026-04-27T00:00:00Z"},
        "extra_usage": {
            "is_enabled": True, "used_credits": 12.5,
            "monthly_limit": 50.0, "utilization": 25.0,
        },
    })
    assert out["five_hour_util"] == 42.5
    assert out["five_hour_reset"] == "2026-04-20T12:00:00Z"
    assert out["seven_day_util"] == 80.0
    assert out["extra_used"] == 0.125
    assert out["extra_limit"] == 0.5
    assert out["extra_util"] == 25.0
    print("  [PASS] flatten_usage: preserves resets_at and normalized extra_usage fields")


def test_flatten_usage_preserves_openai_thirty_day(m):
    out = m["oauth_manager"].flatten_usage({
        "five_hour": {},
        "seven_day": {},
        "openai": {
            "thirty_day": {
                "utilization": 8.0,
                "resets_at": "2026-07-12T00:43:10Z",
            }
        },
    })
    assert out["five_hour_util"] is None
    assert out["seven_day_util"] is None
    assert out["thirty_day_util"] == 8.0
    assert out["thirty_day_reset"] == "2026-07-12T00:43:10Z"
    assert m["oauth_manager"].extract_utils_percent({
        "five_hour": {},
        "seven_day": {},
        "openai": {"thirty_day": {"utilization": 8.0}},
    })[:3] == [None, None, 8.0]
    print("  [PASS] flatten_usage: preserves OpenAI 30d quota")



def test_usage_from_quota_row_preserves_openai_thirty_day(m):
    row = {
        "five_hour_util": None,
        "five_hour_reset": None,
        "seven_day_util": None,
        "seven_day_reset": None,
        "thirty_day_util": 1.0,
        "thirty_day_reset": "2026-07-19T21:23:07Z",
        "sonnet_util": None,
        "sonnet_reset": None,
        "opus_util": None,
        "opus_reset": None,
        "extra_used": 0,
        "extra_limit": 0,
        "extra_util": 0,
    }
    usage = m["oauth_manager"].usage_from_quota_row(row)
    assert usage["openai"]["thirty_day"]["utilization"] == 1.0
    assert usage["openai"]["thirty_day"]["resets_at"] == "2026-07-19T21:23:07Z"
    assert m["oauth_manager"].extract_utils_percent(usage)[:3] == [None, None, 1.0]
    print("  [PASS] usage_from_quota_row: preserves OpenAI 30d quota")


def test_flatten_usage_reads_fable_from_weekly_scoped_limit(m):
    reset = "2026-08-30T10:00:00.161832+00:00"
    out = m["oauth_manager"].flatten_usage({
        "five_hour": {"utilization": 2.0, "resets_at": None},
        "seven_day": {"utilization": 37.0, "resets_at": reset},
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "limits": [{
            "kind": "weekly_scoped",
            "percent": 6,
            "resets_at": reset,
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        }],
    })
    assert out["fable_util"] == 6.0
    assert out["fable_reset"] == reset
    assert out["sonnet_util"] is None
    assert out["opus_util"] is None
    assert m["oauth_manager"].extract_utils_percent({
        "limits": [{
            "kind": "weekly_scoped",
            "percent": 6,
            "resets_at": reset,
            "scope": {"model": {"display_name": "F5"}},
        }],
    })[5] == 6.0
    matcher = m["oauth_manager"]._is_fable_model_label
    for label in ("Fable", "Claude Fable", "F5", "Claude-F5", "claude-fable-5"):
        assert matcher(label), label
    for label in ("notfable", "claude-f5-preview", "claude-opus-4-5"):
        assert not matcher(label), label
    print("  [PASS] flatten_usage: Claude Fable / F5 weekly_scoped limit")


def test_usage_from_quota_row_reads_fable_from_raw_limits(m):
    reset = "2026-08-30T10:00:00Z"
    usage = m["oauth_manager"].usage_from_quota_row({
        "five_hour_util": 2.0,
        "seven_day_util": 37.0,
        "sonnet_util": None,
        "opus_util": None,
        "raw_data": (
            '{"limits":[{"kind":"weekly_scoped","percent":6,'
            '"resets_at":"2026-08-30T10:00:00Z",'
            '"scope":{"model":{"display_name":"Fable"}}}]}'
        ),
    })
    assert usage["seven_day_fable"]["utilization"] == 6.0
    assert usage["seven_day_fable"]["resets_at"] == reset
    print("  [PASS] usage_from_quota_row: Fable fallback from raw limits")


def test_fable_usage_selects_active_scoped_entry(m):
    usage = {
        "seven_day_fable": {
            "utilization": 100,
            "resets_at": "2030-09-01T00:00:00Z",
        },
        "limits": [
            {
                "kind": "weekly_scoped",
                "is_active": False,
                "percent": 99,
                "resets_at": "2030-08-31T00:00:00Z",
                "scope": {"model": {"display_name": "Fable"}},
            },
            {
                "kind": "weekly_scoped",
                "percent": 88,
                "resets_at": "2030-09-02T00:00:00Z",
                "scope": {"model": {"display_name": "Claude F5 legacy"}},
            },
            {
                "kind": "weekly_scoped",
                "is_active": True,
                "percent": 12,
                "resets_at": "2030-08-30T10:00:00Z",
                "scope": {"model": {"display_name": "Fable"}},
            },
        ],
    }
    block = m["oauth_manager"].fable_usage_block(usage)
    assert block == {
        "utilization": 12.0,
        "resets_at": "2030-08-30T10:00:00Z",
    }
    assert m["oauth_manager"].flatten_usage(usage)["fable_util"] == 12.0

    inactive_only = dict(usage)
    inactive_only["limits"] = [usage["limits"][0]]
    assert m["oauth_manager"].fable_usage_block(inactive_only) == {}
    assert m["oauth_manager"].flatten_usage(inactive_only)["fable_util"] is None
    assert m["oauth_manager"].fable_display_block(inactive_only) == {
        "utilization": 99.0,
        "resets_at": "2030-08-31T00:00:00Z",
    }
    assert m["oauth_manager"].fable_display_block(usage) == {
        "utilization": 12.0,
        "resets_at": "2030-08-30T10:00:00Z",
    }
    print("  [PASS] Fable usage: inactive/history entries cannot shadow active scope")


def test_fable_display_falls_back_to_inactive_scoped_window(m):
    reset = "2026-08-30T09:59:59.911926+00:00"
    usage = {
        "five_hour": {"utilization": 0.0, "resets_at": None},
        "seven_day": {"utilization": 38.0, "resets_at": reset},
        "seven_day_sonnet": None,
        "seven_day_opus": None,
        "limits": [
            {
                "kind": "session",
                "percent": 0,
                "is_active": False,
                "scope": None,
            },
            {
                "kind": "weekly_all",
                "percent": 38,
                "is_active": True,
                "resets_at": reset,
                "scope": None,
            },
            {
                "kind": "weekly_scoped",
                "percent": 6,
                "is_active": False,
                "resets_at": reset,
                "scope": {"model": {"id": None, "display_name": "Fable"}},
            },
        ],
    }
    assert m["oauth_manager"].fable_usage_block(usage) == {}
    assert m["oauth_manager"].flatten_usage(usage)["fable_util"] is None
    assert m["oauth_manager"].fable_display_block(usage) == {
        "utilization": 6.0,
        "resets_at": reset,
    }
    row = {
        "five_hour_util": 0.0,
        "seven_day_util": 38.0,
        "fable_util": None,
        "fable_reset": None,
        "raw_data": m["oauth_manager"].flatten_usage(usage)["raw_data"],
    }
    assert m["oauth_manager"].fable_from_quota_row(row) == (None, None)
    util, shown_reset = m["oauth_manager"].fable_display_from_quota_row(row)
    assert util == 6.0
    assert shown_reset == reset
    print("  [PASS] Fable display: inactive-only scoped window stays visible")

# ==============================================================
# main
# ==============================================================

def main():
    m = _import_modules()
    tests = [
        # oauth_ids
        test_account_key_from_dict,
        test_account_key_default_provider,
        test_account_key_explicit_args,
        test_split_account_key_threeseg,
        test_split_account_key_fallback,
        test_channel_key_roundtrip,
        # state_db 迁移
        test_migration_idempotent_when_flag_set,
        test_migration_noop_on_fresh_schema,
        test_migration_transforms_legacy_schema,
        test_migration_drops_orphan_rows,
        # oauth_manager 联合键语义
        test_add_same_email_different_provider_ok,
        test_add_same_email_openai_different_workspaces_ok,
        test_get_account_isolates_by_provider,
        test_delete_account_only_targets_one_of_same_email,
        test_set_enabled_only_targets_one_of_same_email,
        test_update_models_only_targets_one_of_same_email,
        test_refresh_locks_separated_per_account_key,
        # Channel 层
        test_oauth_channel_key_format,
        test_openai_oauth_channel_key_format,
        test_registry_get_channel_new_and_legacy_key,
        # TG menu
        test_resolve_to_account_key_upgrades_plain_email,
        # OpenAI workspace-key migration
        test_openai_workspace_key_migration_unique_rows_and_config,
        test_openai_workspace_key_migration_later_unique_mapping_still_runs,
        test_openai_workspace_key_migration_skips_ambiguous_email,
        # flatten_usage 保持上游 0..100 百分比单位。
        test_flatten_usage_one_percent_stays_one_percent,
        test_flatten_usage_preserves_percentage_units,
        test_flatten_usage_full_hundred_percent,
        test_flatten_usage_zero,
        test_flatten_usage_fractional_sub_one,
        test_flatten_usage_missing_utilization,
        test_flatten_usage_preserves_resets_and_extra,
        test_flatten_usage_preserves_openai_thirty_day,
        test_usage_from_quota_row_preserves_openai_thirty_day,
        test_flatten_usage_reads_fable_from_weekly_scoped_limit,
        test_usage_from_quota_row_reads_fable_from_raw_limits,
        test_fable_usage_selects_active_scoped_entry,
        test_fable_display_falls_back_to_inactive_scoped_window,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t(m)
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception:
            failed += 1
            print(f"  [ERR]  {t.__name__}:")
            traceback.print_exc()
    print(f"\nRESULT: {passed} / {passed + failed} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
