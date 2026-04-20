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
    # 清所有 oauth / channel 表，避免跨测试污染
    conn = state_db._get_conn()
    conn.execute("DELETE FROM oauth_quota_cache")
    conn.execute("DELETE FROM performance_stats")
    conn.execute("DELETE FROM channel_errors")
    conn.execute("DELETE FROM cache_affinities")
    conn.commit()

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
    ak = m["oauth_ids"].account_key({"email": "a@b.c", "provider": "openai"})
    assert ak == "openai:a@b.c", ak
    print("  [PASS] account_key(dict) returns provider:email")


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
    ck = m["oauth_ids"].channel_key_for({"email": "a@b.c", "provider": "openai"})
    assert ck == "oauth:openai:a@b.c", ck
    assert m["oauth_ids"].email_from_channel_key(ck) == "a@b.c"
    assert m["oauth_ids"].provider_from_channel_key(ck) == "openai"
    print("  [PASS] channel_key_for + reverse extractors roundtrip")


# ==============================================================
# state_db 主键迁移
# ==============================================================

def test_migration_idempotent_when_flag_set(m):
    _setup(m)
    sdb = m["state_db"]
    # 先手动置 flag
    sdb.schema_meta_set(sdb.COMPOSITE_KEY_FLAG, sdb.COMPOSITE_KEY_VERSION)
    stats = sdb.run_composite_key_migration({"a@b.c": "claude:a@b.c"})
    assert stats["skipped"] is True
    assert stats["reason"] == "flag already set"
    print("  [PASS] migration skipped when flag already set")


def test_migration_noop_on_fresh_schema(m):
    """新装库：schema 已是新格式（account_key 列存在）→ 只补 flag、不动表结构。"""
    _setup(m)
    sdb = m["state_db"]
    # 手动清 flag 但保留 account_key 列（模拟 "之前迁过但 flag 丢" 场景）
    conn = sdb._get_conn()
    conn.execute("DELETE FROM schema_meta WHERE key=?", (sdb.COMPOSITE_KEY_FLAG,))
    conn.commit()
    stats = sdb.run_composite_key_migration({})
    assert stats["skipped"] is True, stats
    assert "account_key column already exists" in stats["reason"], stats
    # flag 应被补上
    assert sdb.composite_key_migration_done() is True
    print("  [PASS] migration backfills flag when account_key col already exists")


def test_migration_transforms_legacy_schema(m):
    """老 schema（email PK，无 account_key 列）+ 有数据 → 迁移成功。"""
    _setup(m)
    sdb = m["state_db"]
    conn = sdb._get_conn()

    # 构造"老 schema"：drop 当前表，建回老格式
    conn.execute("DROP TABLE IF EXISTS oauth_quota_cache")
    conn.execute(
        "CREATE TABLE oauth_quota_cache ("
        "email TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL,"
        "five_hour_util REAL, seven_day_util REAL)"
    )
    conn.execute(
        "INSERT INTO oauth_quota_cache (email, fetched_at, five_hour_util, seven_day_util) "
        "VALUES (?,?,?,?)",
        ("x@y.com", 1000, 15.0, 30.0),
    )
    # channel_key 构造几条 "oauth:<email>" 老格式
    conn.execute(
        "INSERT INTO performance_stats (channel_key, model, last_updated) VALUES (?,?,?)",
        ("oauth:x@y.com", "claude-opus-4-7", 1000),
    )
    conn.execute(
        "INSERT INTO channel_errors (channel_key, model) VALUES (?,?)",
        ("oauth:x@y.com", "claude-opus-4-7"),
    )
    # 清 flag 让迁移真的跑
    conn.execute("DELETE FROM schema_meta WHERE key=?", (sdb.COMPOSITE_KEY_FLAG,))
    conn.commit()

    stats = sdb.run_composite_key_migration({"x@y.com": "claude:x@y.com"})
    assert stats["skipped"] is False, stats
    assert stats["migrated_quota_rows"] == 1, stats
    assert stats["migrated_channel_rows"] >= 2, stats

    # 验证新表结构
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(oauth_quota_cache)")}
    assert "account_key" in cols and "email" in cols, cols
    row = sdb.quota_load("claude:x@y.com")
    assert row and row["email"] == "x@y.com" and row["five_hour_util"] == 15.0, row

    # channel_key 已升级
    rs = conn.execute("SELECT channel_key FROM performance_stats").fetchall()
    ks = [r["channel_key"] for r in rs]
    assert "oauth:claude:x@y.com" in ks, ks
    assert "oauth:x@y.com" not in ks, ks

    # flag 已打
    assert sdb.composite_key_migration_done() is True
    print("  [PASS] migration transforms legacy schema + updates channel_key prefix")


def test_migration_drops_orphan_rows(m):
    """email_to_key 里没有的行（已删账户的孤儿 quota）应在迁移中被丢弃。"""
    _setup(m)
    sdb = m["state_db"]
    conn = sdb._get_conn()
    conn.execute("DROP TABLE IF EXISTS oauth_quota_cache")
    conn.execute(
        "CREATE TABLE oauth_quota_cache (email TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO oauth_quota_cache (email, fetched_at) VALUES (?,?)", ("live@x.com", 1))
    conn.execute("INSERT INTO oauth_quota_cache (email, fetched_at) VALUES (?,?)", ("gone@x.com", 2))
    conn.execute("DELETE FROM schema_meta WHERE key=?", (sdb.COMPOSITE_KEY_FLAG,))
    conn.commit()

    sdb.run_composite_key_migration({"live@x.com": "claude:live@x.com"})
    rows = sdb.quota_load("claude:live@x.com")
    assert rows is not None
    # orphan 丢弃
    cnt = conn.execute("SELECT COUNT(*) AS c FROM oauth_quota_cache").fetchone()["c"]
    assert cnt == 1, cnt
    print("  [PASS] migration drops orphan rows not in email_to_key mapping")


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


def test_add_same_email_same_provider_rejected(m):
    """同邮箱同 provider 必须拒绝，避免真正的重复。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({
        "email": "same@x.com", "provider": "openai",
        "access_token": "a", "refresh_token": "r",
    })
    try:
        om.add_account({
            "email": "same@x.com", "provider": "openai",
            "access_token": "a2", "refresh_token": "r2",
        })
    except ValueError as exc:
        assert "already exists" in str(exc), str(exc)
        print("  [PASS] add_account rejects same (provider, email) combo")
        return
    assert False, "expected ValueError"


def test_get_account_isolates_by_provider(m):
    """同邮箱两账号，get_account 按 account_key 精确定位。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "iso@x.com", "provider": "claude",
                    "access_token": "CLAUDE-AT", "refresh_token": "c"})
    om.add_account({"email": "iso@x.com", "provider": "openai",
                    "access_token": "OPENAI-AT", "refresh_token": "o"})

    claude = om.get_account("claude:iso@x.com")
    openai = om.get_account("openai:iso@x.com")
    assert claude and claude["access_token"] == "CLAUDE-AT"
    assert openai and openai["access_token"] == "OPENAI-AT"
    # 纯 email（老语义）返回第一个匹配，不 crash
    any_match = om.get_account("iso@x.com")
    assert any_match is not None
    print("  [PASS] get_account(account_key) isolates same-email dual accounts")


def test_delete_account_only_targets_one_of_same_email(m):
    """删除 account_key 只影响对应那一条，同邮箱另一个 provider 保留。"""
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "d@x.com", "provider": "claude",
                    "access_token": "a", "refresh_token": "b"})
    om.add_account({"email": "d@x.com", "provider": "openai",
                    "access_token": "a", "refresh_token": "b"})

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
                    "access_token": "a", "refresh_token": "b"})

    om.set_enabled("claude:t@x.com", False, reason="user")
    claude = om.get_account("claude:t@x.com")
    openai = om.get_account("openai:t@x.com")
    assert claude["enabled"] is False and claude["disabled_reason"] == "user"
    assert openai["enabled"] is True and openai.get("disabled_reason") in (None, "",)
    print("  [PASS] set_enabled isolates state per (provider, email)")


def test_update_models_only_targets_one_of_same_email(m):
    _setup(m)
    om = m["oauth_manager"]
    om.add_account({"email": "u@x.com", "provider": "claude",
                    "access_token": "a", "refresh_token": "b"})
    om.add_account({"email": "u@x.com", "provider": "openai",
                    "access_token": "a", "refresh_token": "b"})

    om.update_models("openai:u@x.com", ["gpt-5"])
    claude = om.get_account("claude:u@x.com")
    openai = om.get_account("openai:u@x.com")
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
    acc = om.get_account("openai:co@x.com")
    ch = m["OpenAIOAuthChannel"](acc)
    assert ch.account_key == "openai:co@x.com", ch.account_key
    assert ch.key == "oauth:openai:co@x.com", ch.key
    print("  [PASS] OpenAIOAuthChannel uses three-segment key format")


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
                    "access_token": "a", "refresh_token": "b"})
    ak = m["oauth_menu"]._resolve_to_account_key("r@x.com")
    assert ak == "openai:r@x.com", ak
    # 已经是 account_key 时原样返回
    ak2 = m["oauth_menu"]._resolve_to_account_key("openai:r@x.com")
    assert ak2 == "openai:r@x.com"
    # None 传入：原样返回 None
    assert m["oauth_menu"]._resolve_to_account_key(None) is None
    print("  [PASS] _resolve_to_account_key upgrades bare email to account_key")


# ==============================================================
# flatten_usage 单位透传（2026-04-20 朋友反馈的 1%→100% bug 防回退）
#
# 参考实现：sub2api backend/internal/service/account_usage_service.go::buildUsageInfo
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


def test_flatten_usage_matches_sub2api_typical_values(m):
    """对齐 sub2api 产线实际值：5.0 → 5%、65.2 → 65.2%、99.9 → 99.9%。"""
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
    """reset 时间与 extra_usage 字段照常展平；util 透传不影响其他字段。"""
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
    assert out["extra_used"] == 12.5
    assert out["extra_limit"] == 50.0
    assert out["extra_util"] == 25.0
    print("  [PASS] flatten_usage: preserves resets_at and extra_usage fields")


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
        test_add_same_email_same_provider_rejected,
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
        # flatten_usage 单位透传（2026-04-20 朋友反馈 + sub2api 对齐）
        test_flatten_usage_one_percent_stays_one_percent,
        test_flatten_usage_matches_sub2api_typical_values,
        test_flatten_usage_full_hundred_percent,
        test_flatten_usage_zero,
        test_flatten_usage_fractional_sub_one,
        test_flatten_usage_missing_utilization,
        test_flatten_usage_preserves_resets_and_extra,
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
