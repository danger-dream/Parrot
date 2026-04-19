"""OpenAI OAuth (Codex / ChatGPT) 渠道。

对接 ChatGPT internal API `https://chatgpt.com/backend-api/codex/responses`。
参考 sub2api 的 openai_gateway_service.buildUpstreamRequest（OAuth 分支）。

仅服务本家族入口（openai-chat / openai-responses）；anthropic 入口被 scheduler
按模型家族过滤掉（本类 list_client_models 都是 codex 家族模型）。

运行期流程（每次请求独立，无并发共享状态）：
  1. oauth_manager.ensure_valid_token(email) 拿有效 access_token
     （内部已按 provider 分派到 src.oauth.openai.refresh_sync）
  2. 按 ingress_protocol 准备 Responses shape 请求体：
     - responses ingress → filter_responses_passthrough
     - chat ingress      → chat_to_responses.translate_request
  3. codex_oauth_transform 对请求体做 codex 兼容改造（store=false / stream=true /
     删不支持字段 / 模型名规范化 / input 字符串包列表 / system 提 instructions
     / instructions 兜底）
  4. 拼 Codex CLI 必备 headers（包括从 id_token 解出的 chatgpt-account-id）

配额（codex 限额）不在这里管——failover 层拿到 upstream response 后调
src.oauth.openai.parse_rate_limit_headers 解析头并落库（Commit 3）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from .. import config, oauth_manager
from ..openai.transform import (
    chat_to_responses,
    codex_oauth_transform,
    common,
    guard,
)
from .base import Channel, ChannelDisplay, UpstreamRequest


def _provider_cfg() -> dict:
    """读取 config.oauth.providers.openai（缺省字段回退默认值）。"""
    cfg = (config.get().get("oauth") or {}).get("providers") or {}
    return cfg.get("openai") or {}


def _isolate_session_id(api_key_name: str, raw: str) -> str:
    """把 api_key_name 混入 raw，防止不同 API Key 的会话粘性交叉污染。

    与 sub2api isolateOpenAISessionID 语义等价：前缀 "k<key>:" + raw，
    做 sha256 取前 16 hex 字符。我们用 sha256 而非 xxhash（无新依赖）。
    """
    if not raw:
        return ""
    material = f"k{api_key_name or '-'}:{raw}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


# ─── 常量 ────────────────────────────────────────────────────────

CODEX_UPSTREAM_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_CLI_USER_AGENT = "codex_cli_rs/0.104.0"


class OpenAIOAuthChannel(Channel):
    """provider="openai" 的 OAuth 账户。protocol 声明为 openai-responses。"""

    type = "oauth"
    cc_mimicry = False                     # 不走 Anthropic CC 伪装
    protocol = "openai-responses"          # 上游走 codex responses

    def __init__(self, account: dict, default_models: list[str] | None = None):
        self.email = account["email"]
        self.key = f"oauth:{self.email}"
        self.display_name = self.email
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")

        # Codex 请求必备 meta（缺失也允许注册，build 时再校验）
        self.chatgpt_account_id = str(account.get("chatgpt_account_id") or "")
        self.plan_type = str(account.get("plan_type") or "")

        # 账户 models 优先级：
        #   1) 账户 entry 自带 models（TG 面板里手动填的）
        #   2) 构造参数 default_models（registry 注入，向后兼容；当前为 None）
        #   3) config.oauth.providers.openai.defaultModels（默认 4 个常用 codex 模型）
        # 上游 codex endpoint 只认规范名，transform 把别名映射过去；所以这里
        # 只要列出对外暴露的名字即可。
        models = account.get("models") or []
        if models:
            self.models = list(models)
        elif default_models:
            self.models = list(default_models)
        else:
            self.models = list(_provider_cfg().get("defaultModels") or [])

    # ─── 模型查询 ─────────────────────────────────────────────

    def supports_model(self, requested_model: str) -> Optional[str]:
        """OpenAI OAuth 账户里 models 列表直接是"真实名"列表（不做 alias 映射）。

        codex 规范化放在 build_upstream_request 的 transform 步骤里做。
        """
        return requested_model if requested_model in self.models else None

    def list_client_models(self) -> list[str]:
        return list(self.models)

    # ─── 请求构造 ─────────────────────────────────────────────

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "responses",
    ) -> UpstreamRequest:
        if ingress_protocol not in ("chat", "responses"):
            raise ValueError(
                "OpenAIOAuthChannel only serves openai-chat / openai-responses "
                f"ingress; got {ingress_protocol!r}. Scheduler family filter "
                "should have excluded this channel for anthropic ingress."
            )
        if not self.chatgpt_account_id:
            raise ValueError(
                f"OpenAI OAuth account {self.email!r} missing chatgpt_account_id; "
                "re-login via TG bot to refresh metadata."
            )

        # Step A: 准备 Responses shape
        if ingress_protocol == "responses":
            payload = common.filter_responses_passthrough(requested_body)
            translator_ctx = None      # 同协议透传，无需响应反向
        else:
            # chat ingress → responses 上游（同家族跨变体）
            guard.guard_chat_to_responses(requested_body)
            payload = chat_to_responses.translate_request(requested_body)
            # 下游 chat 是否显式要求 usage 末帧
            stream_opts = requested_body.get("stream_options") or {}
            include_usage = (
                bool(stream_opts.get("include_usage"))
                if isinstance(stream_opts, dict) else False
            )
            translator_ctx = {
                "ingress": "chat",
                "upstream_protocol": "openai-responses",
                "response_translator": "chat_to_responses",
                "model_for_response": resolved_model,
                "include_usage": include_usage,
            }

        payload["model"] = resolved_model

        # Step B: codex 兼容改造（store=false 等硬约束）
        payload = codex_oauth_transform.apply_codex_oauth_transform(
            payload, resolved_model=resolved_model,
        )

        # Step C: 拿 access_token（会在此触发 refresh if 过期）
        access_token = await oauth_manager.ensure_valid_token(self.email)

        headers = self._build_headers(access_token)
        # session_id / conversation_id 隔离（可配置）：基于 prompt_cache_key
        # 派生，避免同 OAuth 账户下不同下游 API Key 之间会话粘性碰撞。
        prov_cfg = _provider_cfg()
        if prov_cfg.get("isolateSessionId", True):
            api_key_name = str(requested_body.get("_api_key_name") or "")
            prompt_cache_key = str(payload.get("prompt_cache_key") or "").strip()
            if api_key_name and prompt_cache_key:
                iso = _isolate_session_id(api_key_name, prompt_cache_key)
                if iso:
                    headers["session_id"] = iso
                    headers["conversation_id"] = iso

        return UpstreamRequest(
            url=CODEX_UPSTREAM_URL,
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            dynamic_tool_map=None,
            translator_ctx=translator_ctx,
        )

    # ─── 响应字节流 ───────────────────────────────────────────

    async def restore_response(self, chunk: bytes,
                               dynamic_map: Optional[dict] = None) -> bytes:
        # OpenAI 家族不做工具名还原
        return chunk

    # ─── 主动探测：拉 Codex 用量 snapshot ────────────────────────

    async def probe_usage(self, *, timeout_s: float = 20.0) -> dict:
        """主动发一条最小 codex 请求，读响应头更新 Codex 用量 snapshot。

        对齐 sub2api account_test_service 的做法：构造一个"hi" 级别的小请求，
        拿到响应头即可 close 流，不等完整回复。响应头里的 x-codex-* 字段喂给
        state_db.quota_save_openai_snapshot，相当于"显式刷新一次用量"。

        用户在 TG bot 主动点按钮时调用；不触发 failover 节流桶（那个只在请求
        链路里生效），这里直接写库。

        返回 {"ok": bool, "reason": str (错误时)}。
        副作用：成功时更新 oauth_quota_cache。

        成本提示：上游会产生少量 output token（几到几十），计入 Codex 配额；
        用户主动触发，知情同意。
        """
        # 延迟 import 以免循环依赖
        from .. import oauth_manager, state_db
        from ..oauth import openai as openai_provider

        # mockMode 短路：不发真实 HTTP，合成一组 snapshot 写库便于测试
        if oauth_manager.mock_mode_enabled():
            mock_headers = {
                "x-codex-primary-used-percent": "3",
                "x-codex-primary-reset-after-seconds": "3600",
                "x-codex-primary-window-minutes": "10080",
                "x-codex-secondary-used-percent": "1",
                "x-codex-secondary-reset-after-seconds": "180",
                "x-codex-secondary-window-minutes": "300",
            }
            snap = openai_provider.parse_rate_limit_headers(mock_headers)
            if snap:
                normalized = openai_provider.normalize_codex_snapshot(snap)
                state_db.quota_save_openai_snapshot(self.email, snap, normalized)
            return {"ok": True, "reason": "mock"}

        if not self.chatgpt_account_id:
            return {"ok": False, "reason": "missing chatgpt_account_id"}

        # 构造最小探测请求体。走 build_upstream_request 能顺带用到 codex
        # transform（store=false / stream=true / 模型规范化 / instructions 兜底 / ...）
        probe_model = self.models[0] if self.models else "gpt-5.2"
        test_body = {
            "model": probe_model,
            "input": "1",
            # 极短 instructions，减少 input token
            "instructions": "reply ok",
            # 不设 stream 让 transform 强制 stream=true
        }
        try:
            req = await self.build_upstream_request(
                test_body, probe_model, ingress_protocol="responses",
            )
        except Exception as exc:
            return {"ok": False, "reason": f"build upstream request: {exc}"}

        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                # stream 模式：拿到响应头即可，不消费 body 直接关流
                # （上游会继续生成一小段 token 直到发现连接关闭，算作探测成本）
                async with client.stream(
                    "POST", req.url,
                    headers=req.headers, content=req.body,
                ) as resp:
                    status = resp.status_code
                    headers_snapshot = dict(resp.headers)
        except httpx.TimeoutException:
            return {"ok": False, "reason": f"timeout > {timeout_s}s"}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)[:200]}

        # 即使非 200，codex 也可能在头里带速率限制信息；能写就写
        snap = openai_provider.parse_rate_limit_headers(headers_snapshot)
        if snap:
            normalized = openai_provider.normalize_codex_snapshot(snap)
            try:
                state_db.quota_save_openai_snapshot(self.email, snap, normalized)
            except Exception as exc:
                return {"ok": False, "reason": f"quota write: {exc}"}

        if status != 200:
            return {"ok": False, "reason": f"HTTP {status}"}
        if not snap:
            return {"ok": False,
                    "reason": "upstream 200 but no x-codex-* headers"}
        return {"ok": True, "reason": "probed"}

    # ─── UI ──────────────────────────────────────────────────

    def display(self) -> ChannelDisplay:
        return ChannelDisplay(
            key=self.key,
            type="oauth",
            display_name=self.email,
            enabled=self.enabled,
            disabled_reason=self.disabled_reason,
            models=list(self.models),
        )

    # ─── 内部 ─────────────────────────────────────────────────

    def _build_headers(self, access_token: str) -> dict[str, str]:
        prov_cfg = _provider_cfg()
        headers = {
            # Host 头：httpx 通常会按 URL 自动设置，这里显式兜底保险
            "host": "chatgpt.com",
            "authorization": f"Bearer {access_token}",
            "chatgpt-account-id": self.chatgpt_account_id,
            "openai-beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "accept": "text/event-stream",
            "content-type": "application/json",
        }
        # forceCodexCLI=True（默认）→ 强制伪装 UA；False 则不设，交给 httpx 默认
        if prov_cfg.get("forceCodexCLI", True):
            headers["user-agent"] = CODEX_CLI_USER_AGENT
        return headers
