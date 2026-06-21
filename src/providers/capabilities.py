"""Provider capability metadata and target request allowlists.

The allowlists here describe target payloads that a provider/protocol path may
emit.  They are not source request validators.  Semantic loss is still handled
by the protocol matrix and translators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


CHAT_REQ_ALLOWED: frozenset[str] = frozenset({
    "model", "messages", "stream", "stream_options",
    "temperature", "top_p", "n",
    "max_completion_tokens", "max_tokens", "stop",
    "frequency_penalty", "presence_penalty",
    "logprobs", "top_logprobs", "logit_bias",
    "tools", "tool_choice", "parallel_tool_calls",
    "functions", "function_call",
    "response_format", "modalities", "audio",
    "store", "metadata", "seed", "prediction",
    "reasoning_effort", "verbosity", "web_search_options",
    "service_tier", "user", "safety_identifier",
    "prompt_cache_key", "prompt_cache_retention",
})


RESPONSES_REQ_ALLOWED: frozenset[str] = frozenset({
    "model", "input", "stream", "stream_options", "instructions",
    "previous_response_id", "conversation", "context_management",
    "include", "temperature", "top_p", "top_logprobs",
    "max_output_tokens", "max_tool_calls",
    "tools", "tool_choice", "parallel_tool_calls",
    "text", "reasoning", "truncation",
    "store", "metadata", "prompt", "background",
    "service_tier", "user", "safety_identifier",
    "prompt_cache_key", "prompt_cache_retention",
    "client_metadata",
})


ANTHROPIC_MESSAGES_REQ_ALLOWED: frozenset[str] = frozenset({
    "model", "messages", "max_tokens",
    "cache_control",
    "container", "context_management", "mcp_servers",
    "metadata", "output_config", "service_tier",
    "stop_sequences", "stream", "system",
    "temperature", "thinking", "tool_choice", "tools",
    "top_k", "top_p",
})


ANTHROPIC_BRIDGE_REQ_ALLOWED: frozenset[str] = frozenset({
    "model", "messages", "max_tokens", "cache_control", "metadata", "service_tier",
    "stop_sequences", "stream", "system", "temperature", "tool_choice",
    "tools", "top_p",
})


STATEFUL_RESOURCE_REDLINES: frozenset[str] = frozenset({
    "audio",
    "conversation",
    "item_reference",
    "file_id",
    "hosted_tool_state",
    "custom_tool_call_history",
    "encrypted_reasoning_replay",
    "opaque_thinking_replay",
    "multi_candidate_aggregation",
})


@dataclass(frozen=True)
class ProviderCapabilities:
    adapter_name: str
    family: str
    protocols: frozenset[str]
    transports: frozenset[str]
    passthrough_request_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)
    bridge_request_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)
    native_state: frozenset[str] = frozenset()
    redlines: frozenset[str] = STATEFUL_RESOURCE_REDLINES
    notes: tuple[str, ...] = ()

    def request_allowlist(self, protocol: str, *, bridge: bool = False) -> frozenset[str] | None:
        fields = self.bridge_request_fields if bridge else self.passthrough_request_fields
        return fields.get(protocol)


ANTHROPIC_STANDARD_CAPABILITIES = ProviderCapabilities(
    adapter_name="anthropic-standard",
    family="anthropic",
    protocols=frozenset({"anthropic"}),
    transports=frozenset({"http", "sse"}),
    passthrough_request_fields={"anthropic": ANTHROPIC_MESSAGES_REQ_ALLOWED},
    bridge_request_fields={"anthropic": ANTHROPIC_BRIDGE_REQ_ALLOWED},
    notes=("native Anthropic API channel; provider quirks stay in standard/cc_mimicry transforms",),
)


CC_MIMICRY_CAPABILITIES = ProviderCapabilities(
    adapter_name="cc-mimicry",
    family="anthropic",
    protocols=frozenset({"anthropic"}),
    transports=frozenset({"http", "sse"}),
    passthrough_request_fields={"anthropic": ANTHROPIC_MESSAGES_REQ_ALLOWED},
    bridge_request_fields={"anthropic": ANTHROPIC_BRIDGE_REQ_ALLOWED},
    native_state=frozenset({"cache_control", "claude_code_headers"}),
    notes=("Anthropic-compatible provider with Claude Code mimicry request/response restore",),
)


ANTHROPIC_OAUTH_CAPABILITIES = ProviderCapabilities(
    adapter_name="anthropic-oauth",
    family="anthropic",
    protocols=frozenset({"anthropic"}),
    transports=frozenset({"http", "sse"}),
    passthrough_request_fields={"anthropic": ANTHROPIC_MESSAGES_REQ_ALLOWED},
    bridge_request_fields={"anthropic": ANTHROPIC_BRIDGE_REQ_ALLOWED},
    native_state=frozenset({"cache_control", "claude_code_headers", "oauth_rate_limit_headers"}),
    notes=("Anthropic OAuth always uses CC mimicry and OAuth headers",),
)


OPENAI_API_CAPABILITIES = ProviderCapabilities(
    adapter_name="openai-api",
    family="openai",
    protocols=frozenset({"openai-chat", "openai-responses"}),
    transports=frozenset({"http", "sse", "ws"}),
    passthrough_request_fields={
        "openai-chat": CHAT_REQ_ALLOWED,
        "openai-responses": RESPONSES_REQ_ALLOWED,
    },
    native_state=frozenset({
        "previous_response_id",
        "conversation",
        "item_reference",
        "hosted_tools",
        "custom_tool_history",
        "encrypted_reasoning_replay",
        "file_id",
        "audio",
        "background",
    }),
    notes=("OpenAI-compatible API provider; target request payloads are allowlist filtered",),
)


OPENAI_CODEX_CAPABILITIES = ProviderCapabilities(
    adapter_name="openai-codex",
    family="openai",
    protocols=frozenset({"openai-responses"}),
    transports=frozenset({"sse", "ws"}),
    passthrough_request_fields={"openai-responses": RESPONSES_REQ_ALLOWED},
    native_state=frozenset({
        "prompt_cache_key",
        "encrypted_reasoning_replay",
        "item_reference",
        "custom_tool_history",
        "codex_identity_headers",
    }),
    notes=("ChatGPT/Codex OAuth forces store=false/stream=true and uses replay cache for encrypted reasoning",),
)
