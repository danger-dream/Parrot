"""安全、受限的 OpenAI-compatible ``/models`` 自动发现。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import network

MAX_RESPONSE_BYTES = 1_000_000
MAX_MODELS = 2_000
MAX_MODEL_ID_LENGTH = 256
ALLOWED_AUTH = {"bearer", "x-api-key", "anthropic-x-api-key", "none"}
ALLOWED_PARSERS = {"openai-data-id", "dashscope-output-model"}


class ModelsDiscoveryError(Exception):
    """可安全展示给用户的简短错误；绝不包含上游响应或凭据。"""



def derive_custom_models_url(base_url: str, api_path: str | None = None) -> str:
    """按同源规则从 custom URL 推导 models endpoint。"""
    raw = base_url.rstrip("/") + ((api_path or "") if api_path else "")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ModelsDiscoveryError("无法从 URL 推导模型列表地址")
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/messages", "/responses"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if path.endswith("/v1"):
        models_path = path + "/models"
    elif path.endswith("/models"):
        models_path = path
    else:
        models_path = path + "/v1/models"
    return urlunsplit((parts.scheme, parts.netloc, models_path or "/v1/models", "", ""))


def _headers(auth: str, api_key: str) -> dict[str, str]:
    if auth not in ALLOWED_AUTH:
        raise ModelsDiscoveryError("不支持的模型发现鉴权方式")
    if auth == "bearer":
        return {"Authorization": f"Bearer {api_key}"}
    if auth == "x-api-key":
        return {"x-api-key": api_key}
    if auth == "anthropic-x-api-key":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {}


def _normalize_model_ids(items: list[object], field: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items[:MAX_MODELS + 1]:
        model_id = item.get(field) if isinstance(item, dict) else None
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or len(model_id) > MAX_MODEL_ID_LENGTH or model_id in seen:
            continue
        seen.add(model_id)
        out.append(model_id)
        if len(out) >= MAX_MODELS:
            break
    if not out:
        raise ModelsDiscoveryError("上游未返回可用模型")
    return out


def _parse_openai_data_id(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ModelsDiscoveryError("模型列表响应格式不受支持")
    return _normalize_model_ids(payload["data"], "id")


def _parse_dashscope_output_model(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        raise ModelsDiscoveryError("模型列表响应格式不受支持")
    output = payload.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("models"), list):
        raise ModelsDiscoveryError("模型列表响应格式不受支持")
    return _normalize_model_ids(output["models"], "model")


async def discover_models(endpoint: str, api_key: str, *, auth: str = "bearer",
                          parser: str = "openai-data-id", total_timeout: float = 12.0,
                          client_factory: Callable[..., httpx.AsyncClient] | None = None) -> list[str]:
    """发现模型；禁用重定向并限制读取体积，异常仅返回安全摘要。"""
    if parser not in ALLOWED_PARSERS:
        raise ModelsDiscoveryError("不支持的模型列表解析格式")
    factory = client_factory or network.async_client
    timeout = httpx.Timeout(8.0, connect=4.0, read=8.0, write=4.0, pool=4.0)
    try:
        async with asyncio.timeout(total_timeout):
            async with factory(timeout=timeout, follow_redirects=False,
                               proxy_purpose="models-discovery") as client:
                async with client.stream("GET", endpoint, headers=_headers(auth, api_key)) as response:
                    if response.is_redirect:
                        raise ModelsDiscoveryError("模型服务返回了重定向，已拒绝携带 Key 跟随")
                    if not 200 <= response.status_code < 300:
                        raise ModelsDiscoveryError(f"模型服务请求失败（HTTP {response.status_code}）")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ModelsDiscoveryError("模型列表响应过大")
    except ModelsDiscoveryError:
        raise
    except TimeoutError as exc:
        raise ModelsDiscoveryError("模型发现超时") from exc
    except (httpx.HTTPError, OSError) as exc:
        raise ModelsDiscoveryError("无法连接模型服务") from exc
    except Exception as exc:
        # 代理路由/客户端构造等异常也只能折叠为安全摘要，避免把内部细节带到 UI。
        raise ModelsDiscoveryError("模型发现失败") from exc
    try:
        payload = json.loads(bytes(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelsDiscoveryError("模型服务返回了非 JSON 响应") from exc
    if parser == "dashscope-output-model":
        return _parse_dashscope_output_model(payload)
    return _parse_openai_data_id(payload)
