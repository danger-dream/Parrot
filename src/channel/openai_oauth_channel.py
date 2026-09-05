"""OpenAI OAuth (Codex / ChatGPT) 渠道。

对接 ChatGPT internal API `https://chatgpt.com/backend-api/codex/responses`。
实现 ChatGPT Codex OAuth 上游所需的请求转换、鉴权与响应处理。

默认服务本家族入口（openai-chat / openai-responses）；Phase 8 起，当
ProtocolMatrix 判定请求能力可安全表达时，也允许 Anthropic 入口先翻译成
Responses shape 再走 Codex 上游。

运行期流程（每次请求独立，无并发共享状态）：
  1. oauth_manager.ensure_valid_token(email) 拿有效 access_token
     （内部已按 provider 分派到 src.oauth.openai.refresh_sync）
  2. 按 ingress_protocol 准备 Responses shape 请求体：
     - responses ingress → provider adapter target allowlist
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

from .. import cache_hints, config, model_metadata, model_pricing, network, oauth_manager
from ..providers import registry as provider_registry
from ..openai import reasoning_replay
from ..openai.codex_device_fingerprint import (
    apply_device_fingerprint,
    canonical_uuid4,
)
from ..openai.transform import (
    anthropic_to_responses,
    chat_to_responses,
    codex_oauth_transform,
    guard,
)
from .base import Channel, ChannelDisplay, UpstreamRequest
from .compatibility import apply_reasoning_effort_capability


def _provider_cfg() -> dict:
    """读取 OpenAI OAuth 配置。

    新入口：config.openaiOAuth。
    旧入口：config.oauth.providers.openai 继续兼容；加载旧配置时 config.py 会
    自动把旧值补齐到 openaiOAuth。这里仍保留运行时 fallback，照顾单测/局部配置。
    """
    default = config.DEFAULT_CONFIG.get("openaiOAuth") or {}
    cfg = config.get()
    legacy = (((cfg.get("oauth") or {}).get("providers") or {}).get("openai") or {})
    current = cfg.get("openaiOAuth") or {}
    default = default if isinstance(default, dict) else {}
    legacy = legacy if isinstance(legacy, dict) else {}
    current = current if isinstance(current, dict) else {}

    merged = dict(default)
    # 运行时兼容：如果调用方仍只改旧路径，且 openaiOAuth 还等于默认值，
    # 旧路径生效；一旦用户显式改了新入口，则新入口优先。
    if legacy and current == default:
        source = legacy
    else:
        source = current
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged.get(key) or {})
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _isolate_session_id(api_key_name: str, raw: str) -> str:
    """把 api_key_name 混入 raw，防止不同 API Key 的会话粘性交叉污染。

    使用带 API Key 身份前缀的原始会话锚点，
    做 sha256 取前 16 hex 字符。我们用 sha256 而非 xxhash（无新依赖）。
    """
    if not raw:
        return ""
    material = f"k{api_key_name or '-'}:{raw}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _request_api_key_name(body: dict) -> str:
    """Return the downstream API key name for OpenAI and Anthropic ingress.

    OpenAI handlers inject ``_api_key_name`` while the Anthropic /v1/messages
    route injects ``_parrot_api_key_name``.  OAuth Codex session isolation and
    cross-protocol prompt-cache keys must treat them equivalently.
    """
    return str(body.get("_api_key_name") or body.get("_parrot_api_key_name") or "")


# ─── 常量 ────────────────────────────────────────────────────────

CODEX_UPSTREAM_URL = "https://chatgpt.com/backend-api/codex/responses"
from ..openai.codex_constants import (
    CODEX_CLI_USER_AGENT,
    CODEX_ORIGINATOR,
    CODEX_RESPONSES_LITE_HEADER,
    CODEX_ROUTING_HINT_HEADER,
    build_codex_routing_hint,
    codex_cli_user_agent,
    codex_cli_version,
    codex_model_uses_responses_lite,
    codex_version_meets_minimum,
    normalize_codex_service_tier,
)

_CODEX_UNSUPPORTED_STATEFUL_INPUT_TYPES = frozenset({
    "web_search_call", "file_search_call", "computer_call",
    "image_generation_call", "code_interpreter_call",
    "mcp_call", "mcp_list_tools", "mcp_approval_request",
    "mcp_approval_response",
})

_CODEX_UNSUPPORTED_HOSTED_TOOL_TYPES = frozenset({
    "web_search_preview", "file_search", "computer_use_preview",
    "code_interpreter", "image_generation", "mcp", "local_shell",
    "web_search", "web_search_2025_08_26", "web_search_preview_2025_03_11",
    "computer", "computer_use", "apply_patch", "function_shell",
})


def _codex_unsupported_state_label(body: dict) -> str | None:
    if body.get("conversation"):
        return "conversation"
    if body.get("background") is True:
        return "background"
    tools = body.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") in _CODEX_UNSUPPORTED_HOSTED_TOOL_TYPES:
                return f"tools:{tool.get('type')}"
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        typ = choice.get("type")
        if typ in _CODEX_UNSUPPORTED_HOSTED_TOOL_TYPES:
            return f"tool_choice:{typ}"
        if typ == "allowed_tools":
            nested = choice.get("tools")
            if isinstance(nested, list):
                for tool in nested:
                    if isinstance(tool, dict) and tool.get("type") in _CODEX_UNSUPPORTED_HOSTED_TOOL_TYPES:
                        return f"tool_choice:allowed_tools:{tool.get('type')}"
    items: list = []
    instructions = body.get("instructions")
    if isinstance(instructions, list):
        items.extend(instructions)
    inp = body.get("input")
    if isinstance(inp, list):
        items.extend(inp)
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") in _CODEX_UNSUPPORTED_STATEFUL_INPUT_TYPES:
            return str(item.get("type"))
        if item.get("type") == "input_image" and item.get("file_id") is not None:
            return "input_image.file_id"
        if item.get("type") == "input_file" and item.get("file_id") is not None:
            return "input_file.file_id"
        if item.get("type") == "input_audio":
            return "input_audio"
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "input_audio":
                    return "input_audio"
                if part.get("type") == "input_image" and part.get("file_id") is not None:
                    return "input_image.file_id"
                if part.get("type") == "input_file" and part.get("file_id") is not None:
                    return "input_file.file_id"
    return None


class OpenAIOAuthChannel(Channel):
    """provider="openai" 的 OAuth 账户。protocol 声明为 openai-responses。

    上游 chatgpt.com/backend-api/codex/responses 仅支持 SSE 流式，因此
    upstream_stream_only=True；failover 遇到非流式下游请求会走 SSE 聚合路径。
    """

    type = "oauth"
    cc_mimicry = False                     # 不走 Anthropic CC 伪装
    protocol = "openai-responses"          # 上游走 codex responses
    upstream_stream_only = True            # chatgpt.com codex 只支持 stream=true

    def __init__(self, account: dict, default_models: list[str] | None = None):
        from ..oauth_ids import account_key as _account_key
        from .. import oauth_manager as _oauth_manager
        self.email = account["email"]
        self.account_key = _account_key(account)   # openai:<email>:<workspace_id>
        self.key = f"oauth:{self.account_key}"
        self.workspace_id = str(account.get("workspace_id") or account.get("chatgpt_account_id") or "")
        self.workspace_name = str(account.get("workspace_name") or "")
        self.workspace_type = str(account.get("workspace_type") or "")
        same_email_count = sum(
            1 for item in _oauth_manager.list_accounts()
            if _oauth_manager.provider_of(item) == "openai"
            and str(item.get("email") or "") == self.email
        )
        if same_email_count > 1:
            label = self.workspace_name or self.workspace_type or "workspace"
            self.display_name = f"{self.email} · {label}"
        else:
            self.display_name = self.email
        self.workspace_label = self.display_name
        self.enabled = bool(account.get("enabled", True))
        self.disabled_reason = account.get("disabled_reason")
        try:
            self.max_concurrent = int(account.get("maxConcurrent", 0) or 0)
        except (TypeError, ValueError):
            self.max_concurrent = 0

        # Codex workspace meta。老版本账号可能没有该字段：继续沿用旧行为，
        # 不主动发送 chatgpt-account-id；只有新增/重登同邮箱多 workspace 时才需要补齐。
        self.chatgpt_account_id = str(
            account.get("workspace_id") or account.get("chatgpt_account_id") or ""
        )
        self.plan_type = str(account.get("plan_type") or "")
        device_enabled = account.get("codexDeviceConvergenceEnabled") is not False
        self.codex_device_installation_id = (
            canonical_uuid4(account.get("codexDeviceInstallationId"))
            if device_enabled else ""
        )
        if self.codex_device_installation_id and not self.chatgpt_account_id:
            raise ValueError(
                "codexDeviceInstallationId requires a nonempty OpenAI workspace/chatgpt account ID"
            )

        # 账户 models 优先级：
        #   1) 账户 entry 自带 models（TG 面板里手动填的）
        #   2) 构造参数 default_models（registry 注入，向后兼容；当前为 None）
        #   3) config.openaiOAuth.defaultModels（默认常用 codex 模型）
        # 上游 codex endpoint 只认规范名，transform 把别名映射过去；所以这里
        # 只要列出对外暴露的名字即可。
        models = account.get("models") or []
        if models:
            selected_models = list(models)
        elif default_models:
            selected_models = list(default_models)
        else:
            selected_models = list(
                _provider_cfg().get("defaultModels")
                or (config.DEFAULT_CONFIG.get("openaiOAuth") or {}).get("defaultModels")
                or []
            )
        disabled_models = {
            str(model).strip() for model in account.get("disabledModels") or []
            if str(model).strip()
        }
        self.models = [model for model in selected_models if model not in disabled_models]
        catalog_records = (
            (account.get("account_model_catalog") or {}).get("models") or []
        )
        self._account_model_records = {
            str(item.get("id") or "").strip(): dict(item)
            for item in catalog_records
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }

    # ─── 模型查询 ─────────────────────────────────────────────

    # Codex 模型在不同 plan_type 下的可用性限制。来自上游 400 错误：
    #   "The 'gpt-5.2-codex' model is not supported when using Codex with a ChatGPT account."
    # Plus / Pro / Enterprise 的 ChatGPT 账号都算 "ChatGPT account"；只有 API
    # 账号可以调老 codex 系列。这里硬过滤，避免 scheduler 选中后浪费重试。
    _CHATGPT_UNSUPPORTED_MODELS = frozenset({"gpt-5.2-codex"})

    def supports_model(self, requested_model: str) -> Optional[str]:
        """OpenAI OAuth 账户里 models 列表直接是"真实名"列表（不做 alias 映射）。

        codex 规范化放在 build_upstream_request 的 transform 步骤里做。
        """
        if requested_model not in self.models:
            return None
        # ChatGPT 账号（plan_type 非空）不能调 _CHATGPT_UNSUPPORTED_MODELS 里的模型
        if self.plan_type and requested_model in self._CHATGPT_UNSUPPORTED_MODELS:
            return None
        return requested_model

    def list_client_models(self) -> list[str]:
        return list(self.models)

    def service_tier_catalog_status(
        self,
        model: str,
        service_tier: str | None,
    ) -> str:
        """Return ``advertised``, ``not_advertised``, ``standard`` or ``unknown``.

        The account-scoped authenticated Codex ``/models`` response is the only
        official preflight signal for service tiers.  Older/failed catalogs did
        not persist ``serviceTiers``; those remain unknown and are allowed to
        reach the upstream for backward compatibility.
        """
        tier = str(service_tier or "").strip().lower()
        if not tier or tier in {"default", "auto"}:
            return "standard"
        record = self._account_model_records.get(str(model or "").strip())
        if not isinstance(record, dict) or "serviceTiers" not in record:
            return "unknown"
        tiers = record.get("serviceTiers")
        if not isinstance(tiers, list):
            return "unknown"
        advertised = {
            str(item.get("id") or "").strip().lower()
            for item in tiers
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }
        return "advertised" if tier in advertised else "not_advertised"

    def responses_lite_catalog_value(self, model: str) -> bool | None:
        """Return the account catalog's explicit Lite flag, if it has one."""
        record = self._account_model_records.get(str(model or "").strip())
        if not isinstance(record, dict):
            return None
        value = record.get("useResponsesLite")
        return value if isinstance(value, bool) else None

    def model_uses_responses_lite(self, model: str) -> bool:
        """Resolve account-scoped catalog authority before legacy name fallback."""
        return codex_model_uses_responses_lite(
            model, self.responses_lite_catalog_value(model),
        )

    # ─── 请求构造 ─────────────────────────────────────────────

    async def build_upstream_request(
        self, requested_body: dict, resolved_model: str,
        *, ingress_protocol: str = "responses",
        defer_device_fingerprint: bool = False,
        responses_transport: str = "http",
    ) -> UpstreamRequest:
        if ingress_protocol not in ("anthropic", "chat", "responses"):
            raise ValueError(
                "OpenAIOAuthChannel only serves anthropic / openai-chat / openai-responses "
                f"ingress; got {ingress_protocol!r}. ProtocolMatrix should have guarded this route."
            )
        if responses_transport not in ("http", "websocket"):
            raise ValueError(
                f"unsupported OpenAI OAuth Responses transport: {responses_transport!r}"
            )
        if responses_transport == "websocket" and ingress_protocol != "responses":
            raise ValueError(
                "OpenAI OAuth native Responses WebSocket requires responses ingress"
            )
        # One immutable provider-config snapshot keeps models/headers/UA coherent
        # even if a hot reload lands while this request is being constructed.
        prov_cfg = _provider_cfg()

        # Step A: 准备 Responses shape
        # OAuth HTTP SSE 上游被强制 store=false，不能让 previous_response_id
        # 直接穿透到 chatgpt.com，否则上游会按持久化响应查找并 404。
        # 原生 Responses WebSocket v2 是显式例外：同一 WS 内官方 Codex 用
        # previous_response_id 续接预热/上一轮 response。
        if (
            responses_transport == "http"
            and ingress_protocol == "responses"
            and str(requested_body.get("previous_response_id") or "").strip()
        ):
            raise ValueError(
                "previous_response_id is not supported on OpenAI OAuth Codex route "
                "because upstream is forced to store=false; use prompt_cache_key/session_id "
                "or route this request to an OpenAI API channel."
            )
        if ingress_protocol == "responses":
            unsupported_state = _codex_unsupported_state_label(requested_body)
            if unsupported_state:
                raise ValueError(
                    f"{unsupported_state} is not supported on OpenAI OAuth Codex route; "
                    "route this request to an OpenAI API Responses channel."
                )

        if ingress_protocol == "responses":
            payload = provider_registry.filter_request_payload(
                self,
                requested_body,
                protocol="openai-responses",
            )
            translator_ctx = None      # 同协议透传无需响应反向；replay scope 稍后会补进 ctx
        elif ingress_protocol == "chat":
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
        else:
            # anthropic ingress → responses 上游（跨家族，非流式下游）。Codex HTTP
            # endpoint 仍会被 apply_codex_oauth_transform 强制 stream=true，由
            # failover 的 upstream_stream_only 聚合路径再反向成 Anthropic message。
            payload = anthropic_to_responses.translate_request(
                requested_body,
                target_model=resolved_model,
                codex_oauth=True,
            )
            translator_ctx = {
                "ingress": "anthropic",
                "upstream_protocol": "openai-responses",
                "response_translator": "anthropic_to_responses",
                "model_for_response": resolved_model,
                "request_body": requested_body,
            }

        payload["model"] = resolved_model
        payload = provider_registry.filter_request_payload(
            self,
            payload,
            protocol="openai-responses",
        )
        metadata = model_metadata.get_metadata(
            requested_body.get("model") or resolved_model,
            scope_key=self.key,
            outbound_model=resolved_model,
        )
        efforts = metadata.get("reasoningEfforts")
        if not efforts:
            official = model_pricing.catalog_metadata(f"openai/{resolved_model}") or {}
            efforts = official.get("reasoningEfforts")
        apply_reasoning_effort_capability(
            payload,
            efforts,
            protocol="openai-responses",
        )

        codex_unsupported_state = _codex_unsupported_state_label(payload)
        if codex_unsupported_state:
            raise ValueError(
                f"{codex_unsupported_state} is not supported on OpenAI OAuth Codex route; "
                "route this request to an OpenAI API channel."
            )

        # Step B: Codex reasoning replay scope。必须在 codex transform 剥 metadata
        # 等字段前计算 scope；没有 prompt_cache_key / metadata / Codex
        # turn/window/session 锚点时不启用，避免跨会话串状态。
        replay_scope = reasoning_replay.scope_from_payload(
            resolved_model, payload, account_key=self.account_key,
        )

        # Step B.5: Anthropic ingress 的 cache_control 在 Anthropic→Responses
        # translator 中会被剥离；在进入 Codex transform 前补 OpenAI/Codex 可用
        # 的 prompt_cache_key。放在 replay_scope 之后，避免改变既有 metadata
        # session_id reasoning replay 作用域。
        if ingress_protocol == "anthropic":
            cache_hints.apply_anthropic_cache_to_openai_payload(
                requested_body,
                payload,
                model=resolved_model,
                api_key_name=_request_api_key_name(requested_body),
                client_ip=requested_body.get("_parrot_client_ip"),
            )

        # Step C: codex 兼容改造（store=false 等硬约束）。带 encrypted_content 的
        # replay reasoning 只做透明透传，非法/陈旧 EC 由 failover 清 scope 后降级重试。
        # The authenticated account catalog is authoritative for Lite, including
        # explicit false; missing metadata retains the compatibility fallback.
        responses_lite = self.model_uses_responses_lite(resolved_model)
        payload = codex_oauth_transform.apply_codex_oauth_transform(
            payload,
            resolved_model=resolved_model,
            default_instructions=prov_cfg.get("defaultInstructions"),
            transport=responses_transport,
            use_responses_lite=responses_lite,
        )
        model_id = str(payload.get("model") or resolved_model).strip()
        model_record = self._account_model_records.get(model_id)
        minimum_client_version = (
            str(model_record.get("minimalClientVersion") or "").strip()
            if isinstance(model_record, dict) else ""
        )
        effective_client_version = codex_cli_version(prov_cfg)
        if (
            minimum_client_version
            and codex_version_meets_minimum(
                effective_client_version, minimum_client_version,
            ) is False
        ):
            raise guard.GuardError(
                400,
                "invalid_request_error",
                f"Configured Codex CLI version {effective_client_version!r} is below "
                f"model {model_id!r} minimum {minimum_client_version!r}",
                param="model",
                scope="candidate",
            )

        raw_service_tier = payload.get("service_tier")
        service_tier = ""
        if raw_service_tier is not None:
            normalized_tier = normalize_codex_service_tier(raw_service_tier)
            if normalized_tier is None:
                raise guard.GuardError(
                    400,
                    "invalid_request_error",
                    "service_tier must be a nonempty safe ASCII token",
                    param="service_tier",
                    scope="request",
                )
            payload["service_tier"] = normalized_tier
            service_tier = normalized_tier.lower()
        if self.service_tier_catalog_status(
            payload.get("model") or resolved_model,
            service_tier,
        ) == "not_advertised":
            raise guard.GuardError(
                400,
                "invalid_request_error",
                f"OpenAI OAuth account catalog does not advertise service tier "
                f"{service_tier!r} for model {resolved_model!r}",
                param="service_tier",
                scope="candidate",
            )

        # Step D: transform 后 input 已规范成 Responses list，再插入 replay items。
        replay_injected = reasoning_replay.inject_replay_items(payload, replay_scope)
        if replay_scope is not None:
            if translator_ctx is None:
                translator_ctx = {
                    "ingress": ingress_protocol,
                    "upstream_protocol": "openai-responses",
                    "model_for_response": resolved_model,
                }
            translator_ctx["codex_reasoning_replay"] = replay_scope
            translator_ctx["codex_reasoning_replay_injected"] = replay_injected

        # Step E: 拿 access_token（会在此触发 refresh if 过期）。旧账号可能在
        # 这次 refresh 中才首次取得 workspace，并由 oauth_manager 同事务生成设备
        # UUID；当前 Channel 是 refresh 前的快照，因此必须重新读取已提交账户，
        # 让第一条请求就使用新 workspace/device，而不是等下一次 registry 调度。
        access_token = await oauth_manager.ensure_valid_token(self.account_key)
        from .. import channel_state
        current_account_key = self.account_key
        resolved_channel_key = channel_state.resolve(self.key)
        if resolved_channel_key.startswith("oauth:"):
            current_account_key = resolved_channel_key[len("oauth:"):]
        current_account = oauth_manager.get_account(current_account_key)
        current_workspace_id = self.chatgpt_account_id
        current_device_installation_id = self.codex_device_installation_id
        if current_account is not None:
            current_workspace_id = str(
                current_account.get("workspace_id")
                or current_account.get("chatgpt_account_id")
                or ""
            )
            device_enabled = (
                current_account.get("codexDeviceConvergenceEnabled") is not False
            )
            current_device_installation_id = (
                canonical_uuid4(current_account.get("codexDeviceInstallationId"))
                if device_enabled else ""
            )
            if current_device_installation_id and not current_workspace_id:
                raise ValueError(
                    "codexDeviceInstallationId requires a nonempty "
                    "OpenAI workspace/chatgpt account ID"
                )
            # Deferred WS builders read the channel after this method returns.
            # Refresh-derived identity must therefore update this live snapshot as
            # well as the local HTTP variables, so the very first WS request is
            # converged too.
            self.chatgpt_account_id = current_workspace_id
            self.workspace_id = current_workspace_id
            self.codex_device_installation_id = current_device_installation_id

        headers = self._build_headers(access_token, provider_config=prov_cfg)
        if current_workspace_id:
            headers["chatgpt-account-id"] = current_workspace_id
        if responses_lite:
            headers[CODEX_RESPONSES_LITE_HEADER] = "true"
        # session_id / conversation_id 隔离（可配置）：基于 prompt_cache_key
        # 派生，避免同 OAuth 账户下不同下游 API Key 之间会话粘性碰撞。
        if prov_cfg.get("isolateSessionId", True):
            api_key_name = _request_api_key_name(requested_body)
            prompt_cache_key = str(payload.get("prompt_cache_key") or "").strip()
            if api_key_name and prompt_cache_key:
                iso = _isolate_session_id(api_key_name, prompt_cache_key)
                if iso:
                    headers["session_id"] = iso
                    # conversation_id deprecated by Codex — no longer sent.

        # Delete deprecated conversation_id header if present.
        headers.pop("conversation_id", None)

        # Realtime calls _build_headers() directly, so installation identity is
        # deliberately finalized here only for Codex Responses HTTP.  Applicability
        # follows the final upstream transport, not the accepted ingress shape:
        # responses, chat, and anthropic all reach the same HTTP endpoint here.
        # WS paths defer until after their existing identity-confuse/session updates.
        if (
            current_device_installation_id
            and not defer_device_fingerprint
        ):
            headers, payload = apply_device_fingerprint(
                headers, payload, current_device_installation_id,
                create_client_metadata=True,
            )

        # Official Codex sends this on both HTTP requests and WebSocket
        # handshakes.  The WS bridge reuses these headers when it dials the
        # upstream, so one authoritative construction keeps both transports in
        # sync with the final transformed model/tier.
        routing_hint = build_codex_routing_hint(
            payload.get("model") or resolved_model,
            payload.get("service_tier"),
        )
        if routing_hint:
            headers[CODEX_ROUTING_HINT_HEADER] = routing_hint

        return UpstreamRequest(
            url=str(prov_cfg.get("codexUpstreamUrl") or CODEX_UPSTREAM_URL),
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

        构造一条最小请求，只读取响应头中的用量快照，
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
                state_db.quota_save_openai_snapshot(self.account_key, snap, normalized, email=self.email)
            return {"ok": True, "reason": "mock"}


        # 构造最小探测请求体。走 build_upstream_request 能顺带用到 codex
        # transform（store=false / stream=true / 模型规范化 / instructions 兜底 / ...）
        prov_cfg = _provider_cfg()
        probe_cfg = prov_cfg.get("quotaProbe") if isinstance(prov_cfg.get("quotaProbe"), dict) else {}
        fallback_model = str(probe_cfg.get("fallbackModel") or "gpt-5.2")
        probe_model = self.models[0] if self.models else fallback_model
        test_body = {
            "model": probe_model,
            "input": str(probe_cfg.get("input") or "1"),
            # 极短 instructions，减少 input token
            "instructions": str(probe_cfg.get("instructions") or "reply ok"),
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
            async with network.async_client(
                timeout=timeout_s,
                proxy_purpose="oauth_openai",
                proxy_channel=self.key,
                proxy_model=probe_model,
            ) as client:
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
                state_db.quota_save_openai_snapshot(self.account_key, snap, normalized, email=self.email)
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
            display_name=self.display_name,
            enabled=self.enabled,
            disabled_reason=self.disabled_reason,
            models=list(self.models),
        )

    # ─── 内部 ─────────────────────────────────────────────────

    async def build_realtime_headers(self) -> dict[str, str]:
        """Return existing Codex OAuth identity headers for a realtime handshake.

        Token lifecycle remains entirely in ``oauth_manager.ensure_valid_token``;
        this is only a small transport-specific view of the already-established
        OAuth channel identity.
        """
        access_token = await oauth_manager.ensure_valid_token(self.account_key)
        headers = self._build_headers(access_token)
        # These are specific to the SSE Responses endpoint.  Realtime callers
        # supply their own content negotiation / beta headers where needed.
        for name in ("host", "accept", "content-type", "openai-beta"):
            headers.pop(name, None)
        return headers

    def _build_headers(
        self,
        access_token: str,
        *,
        provider_config: dict | None = None,
    ) -> dict[str, str]:
        prov_cfg = provider_config if isinstance(provider_config, dict) else _provider_cfg()
        client_version = codex_cli_version(prov_cfg)
        headers = {
            # Host 头：httpx 通常会按 URL 自动设置，这里显式兜底保险
            "host": "chatgpt.com",
            "authorization": f"Bearer {access_token}",
            "originator": CODEX_ORIGINATOR,
            "version": client_version,
            "accept": "text/event-stream",
            "content-type": "application/json",
            # x-client-request-id: set downstream by session/identity-confuse logic;
            # not included here to avoid sending an empty value if nothing overwrites it.
        }
        if self.chatgpt_account_id:
            headers["chatgpt-account-id"] = self.chatgpt_account_id
        # forceCodexCLI=True（默认）→ 强制伪装 UA；False 则不设，交给 httpx 默认
        if prov_cfg.get("forceCodexCLI", True):
            headers["user-agent"] = codex_cli_user_agent(prov_cfg)
        return headers
