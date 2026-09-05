"""Strict, configuration-selected Codex protocol and model profiles.

Codex wire identity changes with the client release.  Production callers must
select a versioned data profile with ``openaiOAuth.codexProtocolProfile`` and
must configure the matching ``openaiOAuth.codexCliVersion``.  This module has no
runtime release, user-agent, WebSocket beta, or model-name fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


class CodexConfigurationError(ValueError):
    """A required Codex runtime configuration or profile value is invalid."""


_CODEX_VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?$"
)
_CODEX_PROFILE_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
_CODEX_COMPONENT_ALLOWED = frozenset("._:/-")
_PROFILE_ROOT = Path(__file__).with_name("codex_profiles")
_MAX_PROFILE_BYTES = 1024 * 1024
_MAX_INSTRUCTIONS_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CodexModelPolicy:
    model: str
    use_responses_lite: bool
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    default_verbosity: str | None
    multi_agent_reasoning_effort: str | None
    minimal_client_version: str | None
    base_instructions: str | None
    from_profile: bool


@dataclass(frozen=True)
class CodexProtocolProfile:
    profile_id: str
    client_version: str
    originator: str
    user_agent: str
    responses_websocket_beta: str
    models: Mapping[str, CodexModelPolicy]

    def model_policy(self, model: str | None) -> CodexModelPolicy | None:
        return self.models.get(str(model or "").strip())


def normalize_codex_cli_version(value: Any) -> str | None:
    """Return one safe semantic Codex version, or ``None`` when malformed."""
    if not isinstance(value, str):
        return None
    version = value.strip()
    return version if _CODEX_VERSION_RE.fullmatch(version) else None


def _safe_header_value(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise CodexConfigurationError(f"Codex profile {field} must be a string")
    text = value.strip()
    if not text or len(text) > 512 or "\r" in text or "\n" in text:
        raise CodexConfigurationError(f"Codex profile {field} is empty or unsafe")
    return text


def _safe_policy_token(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise CodexConfigurationError(f"Codex model policy {field} must be a string")
    text = value.strip().lower()
    if not text or len(text) > 64 or not all(
        ch.isascii() and (ch.isalnum() or ch in "._-") for ch in text
    ):
        raise CodexConfigurationError(f"Codex model policy {field} is empty or unsafe")
    return text


def _safe_model_id(value: Any) -> str:
    if not isinstance(value, str):
        raise CodexConfigurationError("Codex profile model ID must be a string")
    model = value.strip()
    if not model or len(model) > 256 or not all(
        ch.isascii() and (ch.isalnum() or ch in _CODEX_COMPONENT_ALLOWED)
        for ch in model
    ):
        raise CodexConfigurationError(f"Codex profile model ID is empty or unsafe: {value!r}")
    return model


def _profile_file(profile_id: str) -> Path:
    if not isinstance(profile_id, str) or not _CODEX_PROFILE_ID_RE.fullmatch(profile_id.strip()):
        raise CodexConfigurationError(
            "openaiOAuth.codexProtocolProfile must be a nonempty profile ID"
        )
    root = _PROFILE_ROOT.resolve()
    path = (root / f"{profile_id.strip()}.json").resolve()
    if path.parent != root:
        raise CodexConfigurationError("Codex protocol profile path escapes the profile directory")
    return path


def _read_base_instructions(
    root: Path,
    relative_path: Any,
    expected_sha256: Any,
    *,
    model: str,
) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise CodexConfigurationError(
            f"Codex profile model {model!r} baseInstructionsFile must be nonempty"
        )
    if not isinstance(expected_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ):
        raise CodexConfigurationError(
            f"Codex profile model {model!r} baseInstructionsSha256 is invalid"
        )
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise CodexConfigurationError(
            f"Codex profile model {model!r} baseInstructionsFile escapes the profile directory"
        ) from exc
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CodexConfigurationError(
            f"Cannot read Codex base instructions for model {model!r}: {path}"
        ) from exc
    if not raw or len(raw) > _MAX_INSTRUCTIONS_BYTES:
        raise CodexConfigurationError(
            f"Codex base instructions for model {model!r} are empty or too large"
        )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise CodexConfigurationError(
            f"Codex base instructions checksum mismatch for model {model!r}: "
            f"expected {expected_sha256}, got {digest}"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CodexConfigurationError(
            f"Codex base instructions for model {model!r} are not UTF-8"
        ) from exc


def _parse_profile_model(root: Path, model: str, raw: Any) -> CodexModelPolicy:
    if not isinstance(raw, dict):
        raise CodexConfigurationError(f"Codex profile model {model!r} must be an object")
    lite = raw.get("useResponsesLite")
    if not isinstance(lite, bool):
        raise CodexConfigurationError(
            f"Codex profile model {model!r} useResponsesLite must be boolean"
        )
    raw_efforts = raw.get("reasoningEfforts")
    if not isinstance(raw_efforts, list):
        raise CodexConfigurationError(
            f"Codex profile model {model!r} reasoningEfforts must be an array"
        )
    efforts: list[str] = []
    for value in raw_efforts:
        effort = _safe_policy_token(value, field=f"{model}.reasoningEfforts")
        assert effort is not None
        if effort not in efforts:
            efforts.append(effort)

    default_effort = _safe_policy_token(
        raw.get("defaultReasoningEffort"),
        field=f"{model}.defaultReasoningEffort",
        optional=True,
    )
    default_verbosity = _safe_policy_token(
        raw.get("defaultVerbosity"),
        field=f"{model}.defaultVerbosity",
        optional=True,
    )
    multi_agent_effort = _safe_policy_token(
        raw.get("multiAgentReasoningEffort"),
        field=f"{model}.multiAgentReasoningEffort",
        optional=True,
    )
    minimum_raw = raw.get("minimalClientVersion")
    minimum = None
    if minimum_raw is not None:
        minimum = normalize_codex_cli_version(minimum_raw)
        if minimum is None:
            raise CodexConfigurationError(
                f"Codex profile model {model!r} minimalClientVersion is invalid"
            )
    base_instructions = None
    has_base_path = "baseInstructionsFile" in raw
    has_base_checksum = "baseInstructionsSha256" in raw
    if has_base_path != has_base_checksum:
        raise CodexConfigurationError(
            f"Codex profile model {model!r} must configure both base instructions file and checksum"
        )
    if has_base_path:
        base_instructions = _read_base_instructions(
            root,
            raw.get("baseInstructionsFile"),
            raw.get("baseInstructionsSha256"),
            model=model,
        )
    return CodexModelPolicy(
        model=model,
        use_responses_lite=lite,
        reasoning_efforts=tuple(efforts),
        default_reasoning_effort=default_effort,
        default_verbosity=default_verbosity,
        multi_agent_reasoning_effort=multi_agent_effort,
        minimal_client_version=minimum,
        base_instructions=base_instructions,
        from_profile=True,
    )


@lru_cache(maxsize=16)
def _load_profile(profile_id: str) -> CodexProtocolProfile:
    path = _profile_file(profile_id)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise CodexConfigurationError(
            f"Unknown or unreadable Codex protocol profile {profile_id!r}: {path}"
        ) from exc
    if not raw_bytes or len(raw_bytes) > _MAX_PROFILE_BYTES:
        raise CodexConfigurationError(
            f"Codex protocol profile {profile_id!r} is empty or too large"
        )
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexConfigurationError(
            f"Codex protocol profile {profile_id!r} is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
        raise CodexConfigurationError(
            f"Codex protocol profile {profile_id!r} has unsupported schemaVersion"
        )
    if raw.get("id") != profile_id:
        raise CodexConfigurationError(
            f"Codex protocol profile ID mismatch: selected {profile_id!r}, file has {raw.get('id')!r}"
        )
    client_version = normalize_codex_cli_version(raw.get("clientVersion"))
    if client_version is None:
        raise CodexConfigurationError(
            f"Codex protocol profile {profile_id!r} clientVersion is invalid"
        )
    identity = raw.get("identity")
    protocol = raw.get("protocol")
    if not isinstance(identity, dict) or not isinstance(protocol, dict):
        raise CodexConfigurationError(
            f"Codex protocol profile {profile_id!r} identity/protocol must be objects"
        )
    models_raw = raw.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise CodexConfigurationError(
            f"Codex protocol profile {profile_id!r} models must be a nonempty object"
        )
    root = _PROFILE_ROOT.resolve()
    models: dict[str, CodexModelPolicy] = {}
    for raw_model, raw_policy in models_raw.items():
        model = _safe_model_id(raw_model)
        if model in models:
            raise CodexConfigurationError(
                f"Codex protocol profile {profile_id!r} duplicates model {model!r}"
            )
        models[model] = _parse_profile_model(root, model, raw_policy)
    return CodexProtocolProfile(
        profile_id=profile_id,
        client_version=client_version,
        originator=_safe_header_value(identity.get("originator"), field="identity.originator"),
        user_agent=_safe_header_value(identity.get("userAgent"), field="identity.userAgent"),
        responses_websocket_beta=_safe_header_value(
            protocol.get("responsesWebsocketBeta"),
            field="protocol.responsesWebsocketBeta",
        ),
        models=models,
    )


def _runtime_provider_config() -> Mapping[str, Any]:
    """Read only the normalized top-level OpenAI OAuth configuration."""
    from .. import config

    cfg = config.get()
    current = cfg.get("openaiOAuth") if isinstance(cfg, Mapping) else None
    return current if isinstance(current, Mapping) else {}


def codex_protocol_profile(
    provider_config: Mapping[str, Any] | None = None,
) -> CodexProtocolProfile:
    """Resolve and validate the explicitly selected Codex protocol profile."""
    cfg = provider_config if isinstance(provider_config, Mapping) else _runtime_provider_config()
    configured_version = normalize_codex_cli_version(cfg.get("codexCliVersion"))
    if configured_version is None:
        raise CodexConfigurationError(
            "openaiOAuth.codexCliVersion is required and must be a valid semantic version"
        )
    profile_id = cfg.get("codexProtocolProfile")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise CodexConfigurationError(
            "openaiOAuth.codexProtocolProfile is required and must select a versioned profile"
        )
    profile = _load_profile(profile_id.strip())
    if profile.client_version != configured_version:
        raise CodexConfigurationError(
            f"Codex protocol profile {profile.profile_id!r} requires client version "
            f"{profile.client_version!r}, but openaiOAuth.codexCliVersion is "
            f"{configured_version!r}"
        )
    return profile


def codex_cli_version(provider_config: Mapping[str, Any] | None = None) -> str:
    return codex_protocol_profile(provider_config).client_version


def codex_cli_user_agent(provider_config: Mapping[str, Any] | None = None) -> str:
    return codex_protocol_profile(provider_config).user_agent


def codex_originator(provider_config: Mapping[str, Any] | None = None) -> str:
    return codex_protocol_profile(provider_config).originator


def codex_responses_websocket_beta(
    provider_config: Mapping[str, Any] | None = None,
) -> str:
    return codex_protocol_profile(provider_config).responses_websocket_beta


def _catalog_token(record: Mapping[str, Any], key: str) -> tuple[bool, str | None]:
    if key not in record:
        return False, None
    return True, _safe_policy_token(
        record.get(key), field=f"account catalog {key}", optional=True
    )


def _catalog_efforts(record: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    if "reasoningEfforts" not in record:
        return False, ()
    raw = record.get("reasoningEfforts")
    if not isinstance(raw, list):
        raise CodexConfigurationError("account catalog reasoningEfforts must be an array")
    values: list[str] = []
    for item in raw:
        value = item.get("effort") if isinstance(item, Mapping) else item
        token = _safe_policy_token(value, field="account catalog reasoningEfforts")
        assert token is not None
        if token not in values:
            values.append(token)
    return True, tuple(values)


def resolve_codex_model_policy(
    model: str | None,
    catalog_record: Mapping[str, Any] | None = None,
    provider_config: Mapping[str, Any] | None = None,
) -> CodexModelPolicy:
    """Resolve account-catalog fields first, then the selected profile record.

    Lite policy has no model-name fallback.  If neither source explicitly
    declares ``useResponsesLite``, the request is rejected.
    """
    model_id = _safe_model_id(model)
    profile_policy = codex_protocol_profile(provider_config).model_policy(model_id)
    record = catalog_record if isinstance(catalog_record, Mapping) else {}

    if "useResponsesLite" in record:
        lite = record.get("useResponsesLite")
        if not isinstance(lite, bool):
            raise CodexConfigurationError(
                f"account catalog useResponsesLite for model {model_id!r} must be boolean"
            )
    elif profile_policy is not None:
        lite = profile_policy.use_responses_lite
    else:
        raise CodexConfigurationError(
            f"No explicit Responses Lite policy for Codex model {model_id!r} in the "
            "account catalog or selected protocol profile"
        )

    has_efforts, efforts = _catalog_efforts(record)
    if not has_efforts:
        efforts = profile_policy.reasoning_efforts if profile_policy else ()

    has_default_effort, default_effort = _catalog_token(record, "defaultReasoningEffort")
    if not has_default_effort:
        default_effort = profile_policy.default_reasoning_effort if profile_policy else None

    has_verbosity, default_verbosity = _catalog_token(record, "defaultVerbosity")
    if not has_verbosity:
        default_verbosity = profile_policy.default_verbosity if profile_policy else None

    has_multi_effort, multi_effort = _catalog_token(record, "multiAgentReasoningEffort")
    if not has_multi_effort:
        multi_effort = profile_policy.multi_agent_reasoning_effort if profile_policy else None

    minimum = None
    if "minimalClientVersion" in record:
        raw_minimum = record.get("minimalClientVersion")
        if raw_minimum is not None:
            minimum = normalize_codex_cli_version(raw_minimum)
            if minimum is None:
                raise CodexConfigurationError(
                    f"account catalog minimalClientVersion for model {model_id!r} is invalid"
                )
    elif profile_policy is not None:
        minimum = profile_policy.minimal_client_version

    return CodexModelPolicy(
        model=model_id,
        use_responses_lite=lite,
        reasoning_efforts=efforts,
        default_reasoning_effort=default_effort,
        default_verbosity=default_verbosity,
        multi_agent_reasoning_effort=multi_effort,
        minimal_client_version=minimum,
        base_instructions=profile_policy.base_instructions if profile_policy else None,
        from_profile=profile_policy is not None,
    )


def codex_model_uses_responses_lite(
    model: str | None,
    catalog_value: bool | None = None,
    provider_config: Mapping[str, Any] | None = None,
) -> bool:
    """Resolve Lite from an explicit catalog value or selected profile record."""
    record: Mapping[str, Any]
    if catalog_value is None:
        record = {}
    elif isinstance(catalog_value, bool):
        record = {"useResponsesLite": catalog_value}
    else:
        raise CodexConfigurationError("catalog useResponsesLite must be boolean or absent")
    return resolve_codex_model_policy(
        model, record, provider_config
    ).use_responses_lite


def codex_version_meets_minimum(current: Any, minimum: Any) -> bool | None:
    """Compare numeric SemVer cores; return ``None`` for an unknown schema."""
    current_text = normalize_codex_cli_version(current)
    minimum_text = normalize_codex_cli_version(minimum)
    if not current_text or not minimum_text:
        return None
    current_match = _CODEX_VERSION_RE.fullmatch(current_text)
    minimum_match = _CODEX_VERSION_RE.fullmatch(minimum_text)
    if not current_match or not minimum_match:
        return None
    current_core = tuple(int(value) for value in current_match.groups())
    minimum_core = tuple(int(value) for value in minimum_match.groups())
    return current_core >= minimum_core


def normalize_codex_service_tier(value: Any) -> str | None:
    """Validate one opaque Codex tier token without guessing its semantics."""
    if not isinstance(value, str):
        return None
    tier = value.strip()
    if not tier or len(tier) > 64:
        return None
    if not all(
        ch.isascii() and (ch.isalnum() or ch in _CODEX_COMPONENT_ALLOWED)
        for ch in tier
    ):
        return None
    return tier


# Header names are protocol-stable labels; their values live in selected data.
CODEX_ROUTING_HINT_HEADER = "x-codex-routing-hint"
CODEX_RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"
CODEX_RESPONSES_LITE_WS_METADATA_KEY = (
    "ws_request_header_x_openai_internal_codex_responses_lite"
)


def build_codex_routing_hint(
    model: str | None,
    service_tier: str | None = None,
) -> str | None:
    """Build the official ``model=<id>[;tier=<id>]`` routing hint safely."""

    def component(value: str | None, *, max_length: int = 256) -> str:
        text = str(value or "").strip()
        if not text or len(text) > max_length or not all(
            ch.isascii() and (ch.isalnum() or ch in _CODEX_COMPONENT_ALLOWED)
            for ch in text
        ):
            return ""
        return text

    model_id = component(model)
    if not model_id:
        return None
    tier_id = normalize_codex_service_tier(service_tier) or ""
    if tier_id.lower() in {"default", "auto"}:
        tier_id = ""
    return f"model={model_id}" + (f";tier={tier_id}" if tier_id else "")
