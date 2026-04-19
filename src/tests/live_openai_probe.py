"""MS-8 真实 OpenAI 中转联调脚本（非自动化测试套，不 commit 任何密钥）。

目的：用真实上游 API 跑完整代理链路的 8 个组合 + previous_response_id
续接，确认 MS-1 ~ MS-7 在实际网络条件下端到端正常。

环境变量：
  OPENAI_PROBE_BASE_URL   必填，例：https://api.openai.com
  OPENAI_PROBE_API_KEY    必填
  OPENAI_PROBE_MODEL      选填，默认 gpt-5.4

运行：
  export OPENAI_PROBE_BASE_URL=...
  export OPENAI_PROBE_API_KEY=...
  ./venv/bin/python -m src.tests.live_openai_probe

未设环境变量时脚本直接跳过，不触网。
"""

from __future__ import annotations

import os as _ap_os, sys as _ap_sys
_ap_sys.path.insert(0, _ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.dirname(_ap_os.path.abspath(__file__)))))
from src.tests import _isolation
_isolation.isolate()

import json
import os
import sys
import time

from fastapi.testclient import TestClient


BASE_URL = os.environ.get("OPENAI_PROBE_BASE_URL") or ""
API_KEY  = os.environ.get("OPENAI_PROBE_API_KEY") or ""
MODEL    = os.environ.get("OPENAI_PROBE_MODEL", "gpt-5.4")

DOWNSTREAM_KEY = "ccp-liveprobe-test"


# ─── Setup：写配置 + 启动 TestClient ─────────────────────────────

def _write_config(tmp_dir: str, protocol: str) -> None:
    """把配置写入 isolated config.json；protocol 决定挂哪种 openai 渠道。"""
    cfg_path = os.environ["ANTHROPIC_PROXY_CONFIG"]
    cfg = {
        "listen": {"host": "127.0.0.1", "port": 0},
        "apiKeys": {
            "liveprobe": {"key": DOWNSTREAM_KEY, "allowedModels": [], "allowedProtocols": []},
        },
        "oauthAccounts": [],
        "channels": [{
            "name": f"liveprobe-{protocol}",
            "type": "api",
            "baseUrl": BASE_URL,
            "apiKey": API_KEY,
            "protocol": protocol,
            "models": [{"real": MODEL, "alias": MODEL}],
            "enabled": True,
        }],
        "stateDbPath": os.path.join(tmp_dir, "state.db"),
        "logDir":      os.path.join(tmp_dir, "logs"),
        "telegram": {"botToken": "", "adminIds": []},
        "oauth": {"mockMode": True},
        "timeouts": {"connect": 10, "firstByte": 60, "idle": 60, "total": 180},
        "openai": {
            "store": {"enabled": True, "ttlMinutes": 60, "cleanupIntervalSeconds": 300},
            "reasoningBridge": "passthrough",
            "translation": {"enabled": True, "rejectOnBuiltinTools": True,
                            "rejectOnMultiCandidate": True},
        },
    }
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    # 强制 src.config 重载（若已加载）
    mod = sys.modules.get("src.config")
    if mod is not None:
        try:
            mod.reload()
        except Exception:
            pass


# ─── 单项测试 ────────────────────────────────────────────────────

def _headers() -> dict:
    return {"Authorization": f"Bearer {DOWNSTREAM_KEY}",
            "Content-Type": "application/json"}


def _chat_body(stream: bool = False) -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say exactly: LIVEPROBE_OK"}],
        "max_completion_tokens": 40,
        "stream": stream,
    }


def _responses_body(stream: bool = False) -> dict:
    return {
        "model": MODEL,
        "input": "Say exactly: LIVEPROBE_OK",
        "max_output_tokens": 40,
        "stream": stream,
    }


def _chat_assert_non_stream(body: bytes) -> dict:
    obj = json.loads(body)
    assert obj.get("object") == "chat.completion", f"expected object=chat.completion: {obj}"
    msg = (obj.get("choices") or [{}])[0].get("message") or {}
    assert msg.get("role") == "assistant"
    assert isinstance(msg.get("content"), str) and msg["content"], f"empty content: {msg}"
    assert (obj.get("usage") or {}).get("prompt_tokens", 0) > 0
    return obj


def _resp_assert_non_stream(body: bytes) -> dict:
    obj = json.loads(body)
    assert obj.get("object") == "response", f"expected object=response: {obj}"
    assert obj.get("status") == "completed"
    assert obj.get("id", "").startswith("resp_")
    output = obj.get("output") or []
    # 应至少含一个 message / reasoning item
    types = [it.get("type") for it in output]
    assert "message" in types, f"expected message item: {types}"
    return obj


def _collect_chat_stream(tc: TestClient, url: str, body: dict) -> tuple[str, list[dict]]:
    with tc.stream("POST", url, headers=_headers(), json=body) as resp:
        assert resp.status_code == 200, f"status={resp.status_code} body={resp.read()!r}"
        raw = b""
        for chunk in resp.iter_bytes():
            raw += chunk
    text = raw.decode("utf-8", errors="replace")
    assert "[DONE]" in text, f"chat stream 应以 [DONE] 结尾:\n{text[-400:]}"
    objs: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                objs.append(json.loads(data))
            except Exception:
                pass
    content = "".join(
        o["choices"][0]["delta"].get("content") or ""
        for o in objs if o.get("choices")
    )
    assert content, f"chat stream 未累积到 content：{objs[:3]}"
    return text, objs


def _collect_responses_stream(tc: TestClient, url: str, body: dict) -> tuple[str, list[tuple[str, dict]]]:
    with tc.stream("POST", url, headers=_headers(), json=body) as resp:
        assert resp.status_code == 200, f"status={resp.status_code} body={resp.read()!r}"
        raw = b""
        for chunk in resp.iter_bytes():
            raw += chunk
    text = raw.decode("utf-8", errors="replace")
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = ""
        data_str = ""
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if not data_str:
            continue
        try:
            events.append((ev, json.loads(data_str)))
        except Exception:
            pass
    names = [n for n, _ in events]
    assert "response.created" in names
    assert "response.completed" in names, f"responses stream 无 completed: {names[:20]}"
    # 至少一个 output_text.delta
    text_deltas = [p.get("delta") for n, p in events if n == "response.output_text.delta"]
    assert any(text_deltas), f"responses stream 无 output_text.delta: {names}"
    return text, events


def _with_protocol(tmp_dir: str, protocol: str):
    """上下文：切换 channel protocol 后重建 registry；返回 TestClient。"""
    _write_config(tmp_dir, protocol)
    # 重置 server 进程内的状态（若之前有 TestClient 起过）
    from src.channel import registry
    try:
        from src import config as _cfg
        _cfg.reload()
    except Exception:
        pass
    registry.rebuild_from_config()
    return None  # 调用方直接用共享 TestClient


# ─── 测试用例 ────────────────────────────────────────────────────


def run_case(name: str, tmp_dir: str, tc: TestClient, protocol: str, fn) -> bool:
    print(f"\n▶ {name} (channel protocol={protocol})")
    _with_protocol(tmp_dir, protocol)
    t0 = time.time()
    try:
        fn(tc)
    except AssertionError as e:
        print(f"  [FAIL] {e}")
        import traceback; traceback.print_exc()
        return False
    except Exception as e:
        print(f"  [ERR ] {e}")
        import traceback; traceback.print_exc()
        return False
    print(f"  [PASS] {int((time.time()-t0)*1000)}ms")
    return True


def case_chat_to_chat_nonstream(tc: TestClient):
    r = tc.post("/v1/chat/completions", headers=_headers(), json=_chat_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _chat_assert_non_stream(r.content)


def case_chat_to_chat_stream(tc: TestClient):
    _collect_chat_stream(tc, "/v1/chat/completions", _chat_body(stream=True))


def case_responses_to_responses_nonstream(tc: TestClient):
    r = tc.post("/v1/responses", headers=_headers(), json=_responses_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _resp_assert_non_stream(r.content)


def case_responses_to_responses_stream(tc: TestClient):
    _collect_responses_stream(tc, "/v1/responses", _responses_body(stream=True))


def case_chat_to_responses_nonstream(tc: TestClient):
    r = tc.post("/v1/chat/completions", headers=_headers(), json=_chat_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _chat_assert_non_stream(r.content)


def case_chat_to_responses_stream(tc: TestClient):
    _collect_chat_stream(tc, "/v1/chat/completions", _chat_body(stream=True))


def case_responses_to_chat_nonstream(tc: TestClient):
    r = tc.post("/v1/responses", headers=_headers(), json=_responses_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _resp_assert_non_stream(r.content)


def case_responses_to_chat_stream(tc: TestClient):
    _collect_responses_stream(tc, "/v1/responses", _responses_body(stream=True))


def case_prev_id_followup(tc: TestClient):
    """responses 入口 + openai-chat 上游：第一轮拿 resp_id；第二轮续接。"""
    r1 = tc.post("/v1/responses", headers=_headers(), json={
        "model": MODEL,
        "input": "Remember the word 'ZEBRA' and say 'ok'.",
        "max_output_tokens": 40,
        "stream": False,
    })
    assert r1.status_code == 200, r1.text[:500]
    obj1 = json.loads(r1.content)
    resp_id = obj1["id"]
    assert resp_id.startswith("resp_")

    r2 = tc.post("/v1/responses", headers=_headers(), json={
        "model": MODEL,
        "previous_response_id": resp_id,
        "input": "What word did I ask you to remember?",
        "max_output_tokens": 60,
        "stream": False,
    })
    assert r2.status_code == 200, r2.text[:500]
    obj2 = json.loads(r2.content)
    assert obj2["status"] == "completed"
    # 上游能看到 zebra 说明续接生效
    text = obj2.get("output_text") or ""
    lower = text.lower()
    assert "zebra" in lower, f"续接失败，模型未引用 ZEBRA：{text!r}"


# ─── 驱动 ────────────────────────────────────────────────────────


def main() -> int:
    if not BASE_URL or not API_KEY:
        print("skipped: OPENAI_PROBE_BASE_URL 或 OPENAI_PROBE_API_KEY 未设置")
        return 0

    tmp_dir = _isolation._TMP_DIR or "/tmp"

    # 首次用任一 protocol 初始化（TestClient 的 lifespan 会启动 registry / state_db）
    _write_config(tmp_dir, "openai-chat")

    # server.py 位于项目根（与 src/ 同级）
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from server import app  # noqa: F401
    tc = TestClient(app)

    cases = [
        ("chat ingress → openai-chat 上游（非流式）",   "openai-chat",      case_chat_to_chat_nonstream),
        ("chat ingress → openai-chat 上游（流式）",     "openai-chat",      case_chat_to_chat_stream),
        ("responses ingress → openai-responses 上游（非流式）", "openai-responses", case_responses_to_responses_nonstream),
        ("responses ingress → openai-responses 上游（流式）",   "openai-responses", case_responses_to_responses_stream),
        ("chat ingress → openai-responses 上游（跨变体非流式）", "openai-responses", case_chat_to_responses_nonstream),
        ("chat ingress → openai-responses 上游（跨变体流式）",   "openai-responses", case_chat_to_responses_stream),
        ("responses ingress → openai-chat 上游（跨变体非流式）", "openai-chat",      case_responses_to_chat_nonstream),
        ("responses ingress → openai-chat 上游（跨变体流式）",   "openai-chat",      case_responses_to_chat_stream),
        ("previous_response_id 续接（跨变体）",           "openai-chat",      case_prev_id_followup),
    ]
    passed = 0
    with tc:
        for name, proto, fn in cases:
            if run_case(name, tmp_dir, tc, proto, fn):
                passed += 1

    print(f"\nRESULT: {passed} / {len(cases)} passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
