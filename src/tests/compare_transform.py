"""cc-proxy vs anthropic-proxy — CC 伪装字节级对比测试（完全离线）。

此测试不向 api.anthropic.com 发送任何真实请求。
它同时 import cc-proxy 和 anthropic-proxy 的转换代码，对同一批 fixture
分别运行 transform_request + sign_body，比对字节序列。

使用方法：
    cd /opt/src-space/anthropic-proxy
    ./venv/bin/python -m src.tests.compare_transform
"""

import hashlib
import importlib.util
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CC_PROXY = os.path.join(os.path.dirname(BASE), "cc-proxy")


# ─── 动态加载 cc-proxy/server.py，避免与本项目 server.py 冲突 ─────

def _load_cc_proxy_server():
    """把 cc-proxy/server.py 作为独立模块加载。

    它会尝试 import db / tgbot，我们只需要 transform_* 相关函数，
    所以先把 cc-proxy 目录放到 sys.path 头部。"""
    sys.path.insert(0, CC_PROXY)
    spec = importlib.util.spec_from_file_location(
        "cc_proxy_server_mod", os.path.join(CC_PROXY, "server.py")
    )
    mod = importlib.util.module_from_spec(spec)
    # cc-proxy/server.py 顶层会读 config.json、oauth.json 等文件
    # 这里让它以 cc-proxy 目录作为 cwd
    saved_cwd = os.getcwd()
    os.chdir(CC_PROXY)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.chdir(saved_cwd)
        sys.path.pop(0)
    return mod


# ─── Fixture 构造 ────────────────────────────────────────────────

def fixture_basic_user():
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "messages": [
            {"role": "user", "content": "Hello, how are you?"},
        ],
    }


def fixture_string_system():
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "system": "You are a helpful translator.",
        "messages": [
            {"role": "user", "content": "Translate 'hello' to French"},
        ],
    }


def fixture_list_system():
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "system": [
            {"type": "text", "text": "You are strict."},
            {"type": "text", "text": "Always reply in uppercase."},
        ],
        "messages": [
            {"role": "user", "content": "hello"},
        ],
    }


def fixture_multi_turn():
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "messages": [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "q3"},
        ],
    }


def fixture_with_tools_small():
    # 工具数 ≤ 5 → 不触发动态映射，走静态前缀
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "messages": [{"role": "user", "content": "do it"}],
        "tools": [
            {"name": "read_file", "description": "Read a file",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "sessions_list", "description": "List sessions",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "session_get", "description": "Get session",
             "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
        ],
    }


def fixture_with_tools_large():
    # 工具数 > 5 → 触发动态映射；映射由 hash(tuple(names)) 种子决定，双侧一致
    names = [f"tool_{chr(ord('a') + i)}" for i in range(8)]
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "messages": [{"role": "user", "content": "use the right tool"}],
        "tools": [
            {"name": n, "description": f"desc {n}",
             "input_schema": {"type": "object", "properties": {}}}
            for n in names
        ],
        "tool_choice": {"type": "tool", "name": "tool_c"},
    }


def fixture_thinking_ctx():
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 4096,
        "stream": True,
        "thinking": {"type": "enabled", "budget_tokens": 2000},
        "messages": [{"role": "user", "content": "Think step by step: 12*34?"}],
    }


def fixture_multi_turn_with_images():
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "messages": [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                {"type": "text", "text": "What's in this image?"},
            ]},
            {"role": "assistant", "content": "A picture."},
            {"role": "user", "content": "Describe it more."},
        ],
    }


def fixture_tool_result_turn():
    # 含 assistant 的 tool_use + user 的 tool_result
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "messages": [
            {"role": "user", "content": "List files in /tmp"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tool_123", "name": "read_file",
                 "input": {"path": "/tmp"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tool_123",
                 "content": "[a.txt, b.txt]"},
            ]},
        ],
        "tools": [
            {"name": "read_file", "description": "read",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        ],
    }


def fixture_client_cache_control():
    # 客户端在 messages 上打了 cache_control，应被 strip
    return {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "stream": True,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}},
            ]},
        ],
        "tools": [
            {"name": "read_file", "description": "r",
             "cache_control": {"type": "ephemeral"},
             "input_schema": {"type": "object", "properties": {}}},
        ],
    }


FIXTURES = [
    ("basic_user", fixture_basic_user),
    ("string_system", fixture_string_system),
    ("list_system", fixture_list_system),
    ("multi_turn", fixture_multi_turn),
    ("tools_small", fixture_with_tools_small),
    ("tools_large", fixture_with_tools_large),
    ("thinking", fixture_thinking_ctx),
    ("multi_turn_images", fixture_multi_turn_with_images),
    ("tool_result", fixture_tool_result_turn),
    ("client_cache_control", fixture_client_cache_control),
]


# ─── 对比执行 ────────────────────────────────────────────────────

def _setup_matching_cch_mode(cc_mod, mode="dynamic"):
    """让两侧的 CCH 模式一致。

    本测试默认 dynamic（体现 xxhash 签名路径），运行完恢复。
    cc-proxy 和 anthropic-proxy 各自有自己的 config 读取逻辑，
    直接猴补其顶层 load_config / config.get，返回统一的值。
    """
    stub_cc = {
        "cch_mode": mode,
        "cch_static_value": "00000",
        "api_keys": {},
    }

    cc_orig_load = cc_mod.load_config

    def cc_patched_load():
        return stub_cc

    cc_mod.load_config = cc_patched_load

    from src import config as ap_config
    ap_original_get = ap_config.get

    def patched_get():
        cfg = ap_original_get()
        return {**cfg, "cchMode": mode, "cchStaticValue": "00000"}

    ap_config.get = patched_get

    return cc_orig_load, ap_original_get


def _restore_config(cc_mod, cc_orig_load, ap_original_get):
    cc_mod.load_config = cc_orig_load
    from src import config as ap_config
    ap_config.get = ap_original_get


def _align_device_ids(cc_mod, ap_mod):
    """两侧 DEVICE_ID 必然不同（各自持久化在自己的 ids 文件中）。
    为了比对，把 cc_mod.DEVICE_ID 覆盖为 ap_mod.DEVICE_ID。"""
    cc_mod.DEVICE_ID = ap_mod.DEVICE_ID


def compare_one(name, body, email, cc_mod, ap_mod):
    # 1) 两侧 transform
    cc_payload, cc_map = cc_mod.transform_request(body)
    ap_payload, ap_map = ap_mod.transform_request(body, email=email)

    # cc-proxy 的 build_metadata 从全局 oauth 读 email；我们这里通过覆盖 oauth_cache 对齐
    # 这部分在 _align_email_for_cc_proxy 里处理

    # 2) 两侧 sign_body
    cc_bytes = cc_mod.sign_body(cc_payload)
    ap_bytes = ap_mod.sign_body(ap_payload)

    ok = cc_bytes == ap_bytes
    if not ok:
        # 输出简要差异
        print(f"  [FAIL] {name}: bytes differ (cc={len(cc_bytes)}B, ap={len(ap_bytes)}B)")
        # 找出第一个差异位置
        n = min(len(cc_bytes), len(ap_bytes))
        diff_at = next((i for i in range(n) if cc_bytes[i] != ap_bytes[i]), n)
        lo = max(0, diff_at - 40)
        hi = min(n, diff_at + 40)
        print(f"    first diff at byte {diff_at}:")
        print(f"    cc: ...{cc_bytes[lo:hi]!r}...")
        print(f"    ap: ...{ap_bytes[lo:hi]!r}...")
        return False
    else:
        print(f"  [ OK ] {name}: {len(cc_bytes)}B, map={_map_summary(ap_map)}")
        return True


def _map_summary(dmap):
    if not dmap:
        return None
    return f"{len(dmap)} tools"


def compare_headers(cc_mod, ap_mod, token="fake_token_for_test"):
    cc_h = cc_mod.build_upstream_headers(token)
    ap_h = ap_mod.build_upstream_headers(token)
    # x-client-request-id 是 uuid4 随机生成，两侧必然不同，忽略
    cc_h.pop("x-client-request-id", None)
    ap_h.pop("x-client-request-id", None)
    ok = cc_h == ap_h
    if ok:
        print("  [ OK ] build_upstream_headers: identical (ignoring x-client-request-id)")
    else:
        print(f"  [FAIL] build_upstream_headers differ:\n    cc={cc_h}\n    ap={ap_h}")
    return ok


def compare_restore(cc_mod, ap_mod):
    """测试 _restore_tool_names_in_chunk 还原逻辑。"""
    cases = [
        (b"the cc_sess_list method was called", None),
        (b'{"name":"cc_ses_get"}', None),
        (b'{"type":"tool_use","name":"analyze_too00","input":{}}',
         {"tool_alpha": "analyze_too00"}),
    ]
    all_ok = True
    for i, (chunk, dmap) in enumerate(cases):
        a = cc_mod._restore_tool_names_in_chunk(chunk, dmap)
        b = ap_mod._restore_tool_names_in_chunk(chunk, dmap)
        if a == b:
            print(f"  [ OK ] restore_tool_names case {i}: {len(a)}B -> {a!r}")
        else:
            print(f"  [FAIL] restore case {i}: cc={a!r} ap={b!r}")
            all_ok = False
    return all_ok


def main():
    print("=" * 60)
    print("Loading cc-proxy/server.py ...")
    cc_mod = _load_cc_proxy_server()
    print("cc-proxy CC_VERSION =", cc_mod.CC_VERSION)

    from src.transform import cc_mimicry as ap_mod
    print("anthropic-proxy CC_VERSION =", ap_mod.CC_VERSION)
    assert cc_mod.CC_VERSION == ap_mod.CC_VERSION, "CC_VERSION mismatch"
    assert cc_mod.FINGERPRINT_SALT == ap_mod.FINGERPRINT_SALT
    assert cc_mod.CCH_SEED == ap_mod.CCH_SEED
    assert cc_mod.BETAS == ap_mod.BETAS
    print("constants match")

    # cc-proxy 的 build_metadata 从全局 oauth_cache 读 email；
    # 用 ap 读 oauth.json 对应的那个 email，注入到 cc_mod._oauth_cache 里
    email = ""
    try:
        with open(os.path.join(CC_PROXY, "oauth.json")) as f:
            oauth_raw = json.load(f)
            email = oauth_raw.get("email", "")
    except Exception:
        pass
    cc_mod._oauth_cache = {"email": email, "access_token": "x", "refresh_token": "x", "expired": ""}
    cc_mod._oauth_mtime = -1  # 禁重新读磁盘

    _align_device_ids(cc_mod, ap_mod)
    print(f"using DEVICE_ID={ap_mod.DEVICE_ID[:16]}... email={email!r}")

    total, passed = 0, 0

    for mode in ("dynamic", "static", "disabled"):
        cc_orig_load, ap_original_get = _setup_matching_cch_mode(cc_mod, mode)
        try:
            print(f"\n── Fixtures (cchMode={mode}) ────────────")
            for name, f in FIXTURES:
                total += 1
                if compare_one(f"{mode}/{name}", f(), email, cc_mod, ap_mod):
                    passed += 1
        finally:
            _restore_config(cc_mod, cc_orig_load, ap_original_get)

    print("\n── Headers ──────────────────────────────")
    if compare_headers(cc_mod, ap_mod):
        total += 1; passed += 1
    else:
        total += 1

    print("\n── Restore ──────────────────────────────")
    total += 1
    if compare_restore(cc_mod, ap_mod):
        passed += 1

    print("\n" + "=" * 60)
    print(f"RESULT: {passed} / {total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
