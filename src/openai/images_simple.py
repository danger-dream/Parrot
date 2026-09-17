"""Simplified image endpoints wrap the direct Images API; no Responses tool executor."""
from __future__ import annotations
import json
import re
import time
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from .. import config, errors, oauth_manager, media_config
from ..oauth import normalize_provider
from ..oauth_ids import account_key as make_account_key
from .codex_constants import CODEX_ROUTING_HINT_HEADER, build_codex_routing_hint, codex_cli_user_agent, codex_cli_version, codex_originator

_DEFAULTS = {key: value for key, value in config.DEFAULT_CONFIG['images'].items() if key not in ('mainModel', 'toolModel')}
_IMAGE_COOLDOWNS: dict[str, float] = {}

def settings() -> dict:
    return media_config.settings('image')

def _json_error(status: int, err_type: str, msg: str) -> JSONResponse:
    return errors.json_error_openai(status, err_type, msg)


def _cooldown_active(account_key: str) -> bool:
    until = _IMAGE_COOLDOWNS.get(account_key)
    if not until:
        return False
    if until <= time.time():
        _IMAGE_COOLDOWNS.pop(account_key, None)
        return False
    return True


def list_image_accounts(include_disabled: bool = False) -> list[dict]:
    from ..image_catalog import openai_account_state
    root = config.get()
    out: list[dict] = []
    for acc in oauth_manager.list_accounts():
        if normalize_provider(acc.get("provider")) != "openai":
            continue
        ak = make_account_key(acc)
        email = str(acc.get("email") or "")
        image_state = openai_account_state(acc, root)
        image_disabled = not image_state['image_enabled']
        row = {
            **image_state,
            "account": acc,
            "account_key": ak,
            "email": email,
            "enabled": image_state['oauth_enabled'],
            "effective_available": image_state['effective_available'],
            "image_disabled": image_disabled,
            "image_cooldown_until": _IMAGE_COOLDOWNS.get(ak, 0),
            "missing_account_id": image_state['missing_account_id'],
        }
        if include_disabled or (row["effective_available"] and not _cooldown_active(ak)):
            out.append(row)
    return out


def _build_headers(
    access_token: str,
    account_id: str,
    model: str | None = None,
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": codex_cli_user_agent(),
        "Version": codex_cli_version(),
        "Accept": "text/event-stream",
        "Connection": "Keep-Alive",
        "Originator": codex_originator(),
        "Chatgpt-Account-Id": account_id,
    }
    routing_hint = build_codex_routing_hint(model)
    if routing_hint:
        headers[CODEX_ROUTING_HINT_HEADER] = routing_hint
    return headers


def _normalize_image_input(value: str, *, max_bytes: int) -> str:
    s = str(value or "").strip()
    if not s:
        raise ValueError("image is required")
    if s.startswith("data:"):
        b64 = s.split(",", 1)[1] if "," in s else ""
        approx = len(b64) * 3 // 4
        if approx > max_bytes:
            raise ValueError(f"image is too large; max {max_bytes} bytes")
        return s
    # 兼容直接传裸 base64，默认按 png 包装。
    if re.fullmatch(r"[A-Za-z0-9+/=\s]+", s) and len(s) > 100:
        compact = "".join(s.split())
        approx = len(compact) * 3 // 4
        if approx > max_bytes:
            raise ValueError(f"image is too large; max {max_bytes} bytes")
        return "data:image/png;base64," + compact
    # 允许直接透传 http(s) 图片 URL。
    if s.startswith("http://") or s.startswith("https://"):
        return s
    raise ValueError("image must be data URL, raw base64, or http(s) URL")


async def _handle(request: Request, *, action: str) -> JSONResponse:
    """Legacy endpoint wrapper; uses identical routing, permissions and output bytes."""
    from .images_openai_compat import _run_handler
    response = await _run_handler(request, action=action)
    if response.status_code < 400:
        body = json.loads(response.body)
        body.update({"object": f"parrot.image.{action}", "action": action,
                     "image_model": body.get("model"), "id": str(uuid.uuid4())})
        return JSONResponse(body, status_code=response.status_code)
    return response


async def handle_generate(request: Request) -> JSONResponse:
    return await _handle(request, action="generate")


async def handle_edit(request: Request) -> JSONResponse:
    return await _handle(request, action="edit")
