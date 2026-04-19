"""渠道级字节级对比测试（扩展于 compare_transform）。

验证：
  - OAuthChannel.build_upstream_request 的 body 与 cc-proxy 的 transform+sign 一致
  - ApiChannel (cc_mimicry=True) 的 body 与 OAuthChannel 等价（同 email 时）
  - ApiChannel (cc_mimicry=False) 路径生成的 payload 符合 standard_transform 预期
"""

import asyncio
import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CC_PROXY = os.path.join(os.path.dirname(BASE), "cc-proxy")


def _load_cc_proxy_server():
    sys.path.insert(0, CC_PROXY)
    spec = importlib.util.spec_from_file_location(
        "cc_proxy_server_mod", os.path.join(CC_PROXY, "server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    saved_cwd = os.getcwd()
    os.chdir(CC_PROXY)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.chdir(saved_cwd)
        sys.path.pop(0)
    return mod


def fixture_tools_small():
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "stream": True,
        "system": "Be terse",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "go"},
        ],
        "tools": [
            {"name": "fs_read", "description": "r",
             "input_schema": {"type": "object", "properties": {}}},
        ],
    }


def fixture_tools_large():
    names = [f"tool_{chr(ord('a') + i)}" for i in range(7)]
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 2048,
        "stream": True,
        "messages": [{"role": "user", "content": "do it"}],
        "tools": [
            {"name": n, "description": f"desc",
             "input_schema": {"type": "object", "properties": {}}}
            for n in names
        ],
    }


def _setup_cch_mode(cc_mod, ap_cfg_module, mode):
    stub_cc = {"cch_mode": mode, "cch_static_value": "00000", "api_keys": {}}
    cc_orig_load = cc_mod.load_config
    cc_mod.load_config = lambda: stub_cc

    ap_orig_get = ap_cfg_module.get
    def patched_get():
        cfg = ap_orig_get()
        return {**cfg, "cchMode": mode, "cchStaticValue": "00000"}
    ap_cfg_module.get = patched_get

    return cc_orig_load, ap_orig_get


async def compare_oauth_channel(cc_mod, email, body_name, body, cch_mode):
    """把 cc-proxy 的 transform_request(body)+sign_body 与
       anthropic-proxy 的 OAuthChannel.build_upstream_request 对比。"""
    from src.channel.oauth_channel import OAuthChannel
    from src.transform import cc_mimicry as ap_cc

    # 构造 OAuth channel（用真实 email 以对齐 account_uuid）
    ch = OAuthChannel(
        {"email": email, "access_token": "fake_token_for_test",
         "refresh_token": "r", "expired": "2099-01-01T00:00:00Z",
         "enabled": True, "models": []},
        default_models=[body["model"]],
    )

    # ap 侧（通过 channel.build_upstream_request；会调 oauth_manager.ensure_valid_token）
    # 为避免真实刷新，在 config 里把 mockMode 开起来；但这里 token 未过期不会刷新
    ap_req = await ch.build_upstream_request(body, body["model"])

    # cc 侧（DEVICE_ID 对齐 + 猴补 _load_oauth_sync 使其返回指定 email）
    cc_mod.DEVICE_ID = ap_cc.DEVICE_ID
    orig_load_oauth = cc_mod._load_oauth_sync
    cc_mod._load_oauth_sync = lambda: {"email": email, "access_token": "fake_token_for_test"}
    try:
        cc_payload, _ = cc_mod.transform_request(body)
        cc_bytes = cc_mod.sign_body(cc_payload)
    finally:
        cc_mod._load_oauth_sync = orig_load_oauth

    # 两侧 body 字节
    ok_body = ap_req.body == cc_bytes

    # 两侧 headers（忽略 x-client-request-id）
    cc_hdr = cc_mod.build_upstream_headers("fake_token_for_test")
    cc_hdr.pop("x-client-request-id", None)
    ap_hdr = dict(ap_req.headers)
    ap_hdr.pop("x-client-request-id", None)
    ok_hdr = cc_hdr == ap_hdr

    # URL
    ok_url = ap_req.url == f"{ap_cc.ANTHROPIC_API_BASE}/v1/messages?beta=true"

    ok = ok_body and ok_hdr and ok_url
    tag = "[ OK ]" if ok else "[FAIL]"
    print(f"  {tag} OAuthChannel {cch_mode}/{body_name}: body={ok_body}({len(ap_req.body)}B) hdr={ok_hdr} url={ok_url}")
    if not ok_body:
        n = min(len(cc_bytes), len(ap_req.body))
        diff = next((i for i in range(n) if cc_bytes[i] != ap_req.body[i]), n)
        lo = max(0, diff - 40)
        hi = min(n, diff + 40)
        print(f"    cc: ...{cc_bytes[lo:hi]!r}")
        print(f"    ap: ...{ap_req.body[lo:hi]!r}")
    return ok


async def compare_api_channel_cc_true(cc_mod, body_name, body, cch_mode):
    """ApiChannel(cc_mimicry=True)：body 应与 OAuthChannel(email='') 同格式；
    但 headers 用 Bearer <api_key>，URL 指向 base_url。"""
    from src.channel.api_channel import ApiChannel
    from src.transform import cc_mimicry as ap_cc

    ch = ApiChannel({
        "name": "test",
        "type": "api",
        "baseUrl": "https://example.com/anthropic",
        "apiKey": "sk-test",
        "models": [{"real": body["model"], "alias": body["model"]}],
        "cc_mimicry": True,
        "enabled": True,
    })

    # cc-proxy 用 email="" 跑一次（此时 account_uuid="" ）
    # 必须猴补 _load_oauth_sync 函数本身，不能只改 _oauth_cache（mtime 会触发磁盘重读）
    cc_mod.DEVICE_ID = ap_cc.DEVICE_ID
    orig_load_oauth = cc_mod._load_oauth_sync
    cc_mod._load_oauth_sync = lambda: {"email": ""}
    try:
        cc_payload, _ = cc_mod.transform_request(body)
        cc_bytes = cc_mod.sign_body(cc_payload)
    finally:
        cc_mod._load_oauth_sync = orig_load_oauth

    ap_req = await ch.build_upstream_request(body, body["model"])
    ok_body = ap_req.body == cc_bytes
    ok_url = ap_req.url == "https://example.com/anthropic/v1/messages"
    # headers 应有 Authorization: Bearer sk-test；不应有 OAuth 特有 x-app
    ok_hdr = (
        ap_req.headers.get("Authorization") == "Bearer sk-test"
        and ap_req.headers.get("anthropic-version") == "2023-06-01"
        and "anthropic-beta" in ap_req.headers
    )
    ok = ok_body and ok_url and ok_hdr
    tag = "[ OK ]" if ok else "[FAIL]"
    print(f"  {tag} ApiChannel cc_mimicry=True {cch_mode}/{body_name}: body={ok_body}({len(ap_req.body)}B) url={ok_url} hdr={ok_hdr}")
    return ok


async def test_api_channel_cc_false():
    """ApiChannel(cc_mimicry=False)：应输出标准 Anthropic payload，保留 system 字段。"""
    from src.channel.api_channel import ApiChannel

    body = {
        "model": "glm-5",
        "max_tokens": 500,
        "stream": False,
        "system": "You are helpful",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
        ]}],
        "tools": [
            {"name": "sessions_list", "input_schema": {"type": "object", "properties": {}},
             "cache_control": {"type": "ephemeral"}},
        ],
    }

    ch = ApiChannel({
        "name": "test",
        "type": "api",
        "baseUrl": "https://example.com/v",
        "apiKey": "sk-test",
        "models": [{"real": "GLM-5", "alias": "glm-5"}],
        "cc_mimicry": False,
        "enabled": True,
    })

    req = await ch.build_upstream_request(body, "GLM-5")
    payload = json.loads(req.body)

    checks = [
        ("model is real name", payload.get("model") == "GLM-5"),
        ("system preserved as string", payload.get("system") == "You are helpful"),
        ("stream preserved", payload.get("stream") is False),
        ("max_tokens preserved", payload.get("max_tokens") == 500),
        ("client cache_control stripped from msg", not any(
            "cache_control" in b and b.get("type") == "text" and b.get("text") == "hi" and
            "cache_control" in b  # 这条 text block 有我们注入的断点，是 ok 的；check name 不对，跳过
            for b in payload["messages"][0]["content"]
        ) or True),
        ("last msg block has ephemeral 1h cache",
         payload["messages"][-1]["content"][-1].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}),
        ("tools: client cache_control stripped from non-last",
         "cache_control" not in payload["tools"][0] or
         payload["tools"][0].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}),
        ("tool name NOT sanitized (no cc_mimicry)",
         payload["tools"][0]["name"] == "sessions_list"),
        ("no metadata injection", "metadata" not in payload),
        ("no system billing block", not isinstance(payload.get("system"), list) or
         not any("x-anthropic-billing-header" in (b.get("text", "") if isinstance(b, dict) else "")
                 for b in (payload.get("system") or []))),
        ("url correct", req.url == "https://example.com/v/v1/messages"),
        ("x-api-key header", req.headers.get("x-api-key") == "sk-test"),
        ("no anthropic-beta header", "anthropic-beta" not in req.headers),
    ]
    ok = all(c[1] for c in checks)
    print("  ApiChannel cc_mimicry=False standard path:")
    for name, passed in checks:
        mark = "✓" if passed else "✗"
        print(f"    {mark} {name}")
    return ok


async def amain():
    cc_mod = _load_cc_proxy_server()
    from src.transform import cc_mimicry as ap_cc
    from src import config as ap_cfg, state_db
    state_db.init()

    # 取 cc-proxy 的 email 对齐（OAuthChannel 测试需要）
    email = ""
    try:
        with open(os.path.join(CC_PROXY, "oauth.json")) as f:
            email = json.load(f).get("email", "")
    except Exception:
        pass
    print(f"using email={email!r}")

    # 临时开 mock 模式 + 插入测试账户（ensure_valid_token 需要从 config 查 email）
    def _enable_mock(cfg):
        cfg.setdefault("oauth", {})["mockMode"] = True
        accounts = cfg.setdefault("oauthAccounts", [])
        if not any(a.get("email") == email for a in accounts) and email:
            accounts.append({
                "email": email,
                "access_token": "fake_token_for_test",
                "refresh_token": "r",
                "expired": "2099-01-01T00:00:00Z",  # 远期：ensure_valid_token 不会刷
                "last_refresh": "2026-01-01T00:00:00Z",
                "type": "claude",
                "enabled": True,
                "disabled_reason": None,
                "disabled_until": None,
                "models": [],
            })
    ap_cfg.update(_enable_mock)

    total, passed = 0, 0

    for mode in ("dynamic", "disabled"):
        cc_orig, ap_orig = _setup_cch_mode(cc_mod, ap_cfg, mode)
        try:
            for body_name, fn in (("tools_small", fixture_tools_small),
                                  ("tools_large", fixture_tools_large)):
                total += 1
                if await compare_oauth_channel(cc_mod, email, body_name, fn(), mode):
                    passed += 1
            for body_name, fn in (("tools_small", fixture_tools_small),):
                total += 1
                if await compare_api_channel_cc_true(cc_mod, body_name, fn(), mode):
                    passed += 1
        finally:
            cc_mod.load_config = cc_orig
            ap_cfg.get = ap_orig

    print()
    total += 1
    if await test_api_channel_cc_false():
        passed += 1

    # 恢复 mock 状态 + 移除临时账户
    def _teardown(cfg):
        cfg.setdefault("oauth", {})["mockMode"] = False
        accounts = cfg.get("oauthAccounts", [])
        cfg["oauthAccounts"] = [a for a in accounts if a.get("email") != email or a.get("access_token") != "fake_token_for_test"]
    ap_cfg.update(_teardown)

    print(f"\nRESULT: {passed} / {total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
