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

import httpx
from anyio.from_thread import start_blocking_portal


BASE_URL = os.environ.get("OPENAI_PROBE_BASE_URL") or ""
API_KEY  = os.environ.get("OPENAI_PROBE_API_KEY") or ""
MODEL    = os.environ.get("OPENAI_PROBE_MODEL", "gpt-5.4")

DOWNSTREAM_KEY = "ccp-liveprobe-test"


# ─── Setup：写配置 + 启动 _AsgiProbeClient ─────────────────────────────

def _write_config(tmp_dir: str, protocol: str) -> None:
    """把配置写入 isolated config.json；protocol 决定挂哪种 openai 渠道。"""
    cfg_path = os.environ["ANTHROPIC_PROXY_CONFIG"]
    # live probe 使用隔离配置，不继承生产代理/后台监控设置；DNS 直接采用系统
    # resolv.conf，避免默认 8.8.8.8 在容器环境里不可达。
    dns_servers = []
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0] == "nameserver" and parts[1] not in dns_servers:
                    dns_servers.append(parts[1])
    except OSError:
        pass
    if not dns_servers:
        dns_servers = ["8.8.8.8"]
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
        "network": {
            "dns": {
                "servers": dns_servers,
                "bootstrapFromSystem": False,
                "bootstrapped": True,
                "timeoutSeconds": 3,
                "cacheTtlSeconds": 300,
            },
            "socks5": {"enabled": False, "url": ""},
            "monitor": {
                "enabled": False,
                "intervalSeconds": 60,
                "timeoutSeconds": 5,
                "dns": False,
                "socks5": False,
                "channels": {"enabled": False, "byKey": {}},
                "core": {"openai": False, "claude": False, "cloudflare": False},
            },
        },
        "statusMonitor": {"enabled": False},
        "updateChecker": {"enabled": False},
        "timeouts": {"connect": 10, "firstByte": 60, "idle": 60, "total": 180},
        "openai": {
            "store": {
                "enabled": True,
                "dbPath": os.path.join(tmp_dir, "openai_response_store.db"),
                "ttlMinutes": 60,
                "cleanupIntervalSeconds": 300,
            },
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


class _SyncStreamResponse:
    def __init__(self, *, status_code: int, headers: httpx.Headers, content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def read(self) -> bytes:
        return self.content

    def iter_bytes(self):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _AsgiProbeClient:
    def __init__(self, app):
        self._app = app
        self._portal_cm = None
        self._portal = None
        self._client: httpx.AsyncClient | None = None

    async def _async_enter(self) -> None:
        # Intentionally avoid FastAPI lifespan here. The production lifespan
        # starts public-IP/update/status/probe background work; this live probe
        # should only initialize the request path and contact the configured
        # non-OAuth upstream.
        from src import (
            affinity, cooldown, image_db, log_db, network, scorer, state_db,
            translation, upstream,
        )
        from src.channel import registry
        from src.openai import store as openai_store
        from src.openai.channel.registration import register_factories

        network.init()
        state_db.init()
        log_db.init()
        image_db.init()
        translation.init()
        affinity.init()
        affinity.client_init()
        cooldown.init()
        scorer.init()
        register_factories()
        openai_store.init()
        registry.rebuild_from_config()
        upstream.create_client()

        transport = httpx.ASGITransport(app=self._app)
        self._client = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        )

    async def _async_exit(self) -> None:
        from src import upstream

        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
        await upstream.close_client()

    def post(self, url: str, **kwargs) -> httpx.Response:
        async def _run():
            if self._client is None:
                raise RuntimeError("_AsgiProbeClient must be used as a context manager")
            return await self._client.post(url, **kwargs)

        if self._portal is None:
            raise RuntimeError("_AsgiProbeClient must be used as a context manager")
        return self._portal.call(_run)

    def stream(self, method: str, url: str, **kwargs) -> _SyncStreamResponse:
        async def _run():
            if self._client is None:
                raise RuntimeError("_AsgiProbeClient must be used as a context manager")
            async with self._client.stream(method, url, **kwargs) as resp:
                content = await resp.aread()
                return _SyncStreamResponse(
                    status_code=resp.status_code,
                    headers=resp.headers,
                    content=content,
                )

        if self._portal is None:
            raise RuntimeError("_AsgiProbeClient must be used as a context manager")
        return self._portal.call(_run)

    def __enter__(self):
        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        try:
            self._portal.call(self._async_enter)
        except BaseException:
            self._portal_cm.__exit__(*sys.exc_info())
            self._portal_cm = None
            self._portal = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._portal is not None:
                self._portal.call(self._async_exit)
        finally:
            if self._portal_cm is not None:
                self._portal_cm.__exit__(exc_type, exc, tb)
            self._portal_cm = None
            self._portal = None
        return False


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
    # 第三方 Responses 兼容服务不一定使用 OpenAI 官方的 `resp_` 前缀；
    # 只要求 id 非空，previous_response_id 专项用例会单独验证本地 Store 生成的 id。
    assert obj.get("id"), obj
    output = obj.get("output") or []
    # 应至少含一个 message / reasoning item
    types = [it.get("type") for it in output]
    assert "message" in types, f"expected message item: {types}"
    return obj


def _collect_chat_stream(tc: _AsgiProbeClient, url: str, body: dict) -> tuple[str, list[dict]]:
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


def _collect_responses_stream(tc: _AsgiProbeClient, url: str, body: dict) -> tuple[str, list[tuple[str, dict]]]:
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
    """上下文：切换 channel protocol 后重建 registry；返回 _AsgiProbeClient。"""
    _write_config(tmp_dir, protocol)
    # 重置 server 进程内的状态（若之前有 _AsgiProbeClient 起过）
    from src.channel import registry
    try:
        from src import config as _cfg
        _cfg.reload()
    except Exception:
        pass
    registry.rebuild_from_config()
    return None  # 调用方直接用共享 _AsgiProbeClient


# ─── 测试用例 ────────────────────────────────────────────────────


def run_case(name: str, tmp_dir: str, tc: _AsgiProbeClient, protocol: str, fn) -> bool:
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


def case_chat_to_chat_nonstream(tc: _AsgiProbeClient):
    r = tc.post("/v1/chat/completions", headers=_headers(), json=_chat_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _chat_assert_non_stream(r.content)


def case_chat_to_chat_stream(tc: _AsgiProbeClient):
    _collect_chat_stream(tc, "/v1/chat/completions", _chat_body(stream=True))


def case_responses_to_responses_nonstream(tc: _AsgiProbeClient):
    r = tc.post("/v1/responses", headers=_headers(), json=_responses_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _resp_assert_non_stream(r.content)


def case_responses_to_responses_stream(tc: _AsgiProbeClient):
    _collect_responses_stream(tc, "/v1/responses", _responses_body(stream=True))


def case_chat_to_responses_nonstream(tc: _AsgiProbeClient):
    r = tc.post("/v1/chat/completions", headers=_headers(), json=_chat_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _chat_assert_non_stream(r.content)


def case_chat_to_responses_stream(tc: _AsgiProbeClient):
    _collect_chat_stream(tc, "/v1/chat/completions", _chat_body(stream=True))


def case_responses_to_chat_nonstream(tc: _AsgiProbeClient):
    r = tc.post("/v1/responses", headers=_headers(), json=_responses_body(stream=False))
    assert r.status_code == 200, r.text[:500]
    _resp_assert_non_stream(r.content)


def case_responses_to_chat_stream(tc: _AsgiProbeClient):
    _collect_responses_stream(tc, "/v1/responses", _responses_body(stream=True))


def case_prev_id_followup(tc: _AsgiProbeClient):
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

    # 首次用任一 protocol 初始化，后续 _AsgiProbeClient 会做最小运行时初始化。
    _write_config(tmp_dir, "openai-chat")

    # server.py 位于项目根（与 src/ 同级）
    _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from server import app  # noqa: F401
    tc = _AsgiProbeClient(app)

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
