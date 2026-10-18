"""OpenAI OAuth 低价区导出文件解析。

支持三类常见来源：
  - OpenAI 原生 OAuth 账号 JSON / ZIP
  - Sub2API 导出的 JSON / ZIP
  - CPA 导出的 JSON / ZIP

本模块只做本地解析，提取 email + refresh_token；真正的 token 刷新与账号保存
仍由 Telegram OAuth 菜单复用现有 OpenAI refresh_token 导入路径。
"""

from __future__ import annotations

from src.safe_diagnostics import diagnose

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable


class OpenAIImportParseError(ValueError):
    pass


@dataclass(frozen=True)
class OpenAIImportCandidate:
    email: str
    refresh_token: str
    source: str = ""

    def as_state_item(self) -> dict[str, str]:
        return {
            "email": self.email,
            "refresh_token": self.refresh_token,
            "source": self.source,
        }


_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
_REFRESH_RE = re.compile(r"[A-Za-z0-9_\-.]{20,}")
_SUPPORTED_KINDS = {"openai", "sub2api", "cpa"}
_MAX_ZIP_JSON_FILES = 200
_MAX_ZIP_JSON_BYTES = 10 * 1024 * 1024


@diagnose('openai', 'import', 'import_parse')
def parse_openai_import_payload(kind: str, payload: bytes | str, *, filename: str = "") -> list[OpenAIImportCandidate]:
    """解析上传/粘贴内容，返回去重后的候选账号。"""
    kind = _normalize_kind(kind)
    filename = filename or ""

    if isinstance(payload, bytes) and _looks_like_zip(payload, filename):
        candidates = _parse_zip(kind, payload)
    else:
        obj = _load_json(payload, filename or "pasted-json")
        candidates = list(_extract_candidates_from_json(kind, obj, filename or "pasted-json"))

    candidates = _dedupe_candidates(candidates)
    if not candidates:
        raise OpenAIImportParseError("未识别到包含 refresh_token 的 OpenAI OAuth 账号")
    return candidates


def _normalize_kind(kind: str) -> str:
    k = (kind or "").strip().lower()
    if k not in _SUPPORTED_KINDS:
        raise OpenAIImportParseError(f"不支持的导入类型: {kind!r}")
    return k


def _looks_like_zip(payload: bytes, filename: str) -> bool:
    if filename.lower().endswith(".zip"):
        return True
    return payload[:4] == b"PK\x03\x04"


def _parse_zip(kind: str, payload: bytes) -> list[OpenAIImportCandidate]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise OpenAIImportParseError(f"ZIP 文件无效: {exc}") from exc

    candidates: list[OpenAIImportCandidate] = []
    json_count = 0
    total_bytes = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.lower().endswith(".json"):
                continue
            json_count += 1
            if json_count > _MAX_ZIP_JSON_FILES:
                raise OpenAIImportParseError(f"ZIP 内 JSON 文件过多（>{_MAX_ZIP_JSON_FILES}）")
            total_bytes += max(0, int(info.file_size or 0))
            if total_bytes > _MAX_ZIP_JSON_BYTES:
                raise OpenAIImportParseError("ZIP 内 JSON 总大小超过 10MB")
            try:
                data = zf.read(info)
            except Exception as exc:
                raise OpenAIImportParseError(f"读取 ZIP 内文件失败: {name}: {exc}") from exc
            obj = _load_json(data, name)
            candidates.extend(_extract_candidates_from_json(kind, obj, name))

    if json_count == 0:
        raise OpenAIImportParseError("ZIP 内没有 JSON 文件")
    return candidates


def _load_json(payload: bytes | str, source: str) -> Any:
    try:
        if isinstance(payload, bytes):
            text = payload.decode("utf-8-sig")
        else:
            text = str(payload or "")
        return json.loads(text.strip())
    except Exception as exc:
        raise OpenAIImportParseError(f"JSON 解析失败（{source}）: {exc}") from exc


def _extract_candidates_from_json(kind: str, obj: Any, source: str) -> Iterable[OpenAIImportCandidate]:
    if kind == "openai":
        yield from _extract_openai_candidates(obj, source)
        return
    if kind == "sub2api":
        yield from _extract_sub2api_candidates(obj, source)
        return
    if kind == "cpa":
        yield from _extract_cpa_candidates(obj, source)
        return
    raise OpenAIImportParseError(f"不支持的导入类型: {kind!r}")


def _extract_openai_candidates(obj: Any, source: str) -> Iterable[OpenAIImportCandidate]:
    """Extract native OpenAI account JSON without trusting stored identity fields."""
    if isinstance(obj, list):
        for idx, item in enumerate(obj):
            yield from _extract_openai_candidates(item, f"{source}#{idx + 1}")
        return
    if not isinstance(obj, dict):
        return

    accounts = obj.get("accounts")
    if isinstance(accounts, list) and not obj.get("refresh_token"):
        for idx, item in enumerate(accounts):
            yield from _extract_openai_candidates(item, f"{source}#accounts[{idx}]")
        return

    provider = str(obj.get("provider") or obj.get("platform") or "").strip().lower()
    account_type = str(obj.get("type") or "").strip().lower()
    if provider and provider != "openai":
        return
    if account_type and account_type not in {"openai", "oauth"}:
        return
    creds = obj.get("credentials") if isinstance(obj.get("credentials"), dict) else {}
    rt = _clean_refresh_token(obj.get("refresh_token") or creds.get("refresh_token"))
    if not rt:
        return
    email = _clean_email(obj.get("email") or creds.get("email") or _email_from_source(source))
    if not email:
        email = _fallback_email(source)
    yield OpenAIImportCandidate(email=email, refresh_token=rt, source=source)


def _extract_cpa_candidates(obj: Any, source: str) -> Iterable[OpenAIImportCandidate]:
    if isinstance(obj, list):
        for idx, item in enumerate(obj):
            yield from _extract_cpa_candidates(item, f"{source}#{idx + 1}")
        return

    if not isinstance(obj, dict):
        return

    # 兼容少数 CPA 包一层 accounts 的变体。
    accounts = obj.get("accounts")
    if isinstance(accounts, list) and not obj.get("refresh_token"):
        for idx, item in enumerate(accounts):
            yield from _extract_cpa_candidates(item, f"{source}#accounts[{idx}]")
        return

    creds = obj.get("credentials") if isinstance(obj.get("credentials"), dict) else {}
    rt = _clean_refresh_token(obj.get("refresh_token") or creds.get("refresh_token"))
    if not rt:
        return
    email = _clean_email(obj.get("email") or creds.get("email") or _email_from_source(source))
    if not email:
        email = _fallback_email(source)
    yield OpenAIImportCandidate(email=email, refresh_token=rt, source=source)


def _extract_sub2api_candidates(obj: Any, source: str) -> Iterable[OpenAIImportCandidate]:
    if isinstance(obj, list):
        for idx, item in enumerate(obj):
            yield from _extract_sub2api_account(item, f"{source}#{idx + 1}")
        return

    if not isinstance(obj, dict):
        return

    accounts = obj.get("accounts")
    if isinstance(accounts, list):
        for idx, item in enumerate(accounts):
            yield from _extract_sub2api_account(item, f"{source}#accounts[{idx}]")
        return

    # 兼容用户只上传了 accounts[] 里的单个 account 对象。
    yield from _extract_sub2api_account(obj, source)


def _extract_sub2api_account(account: Any, source: str) -> Iterable[OpenAIImportCandidate]:
    if not isinstance(account, dict):
        return
    platform = str(account.get("platform") or "").strip().lower()
    acc_type = str(account.get("type") or "").strip().lower()
    if platform and platform != "openai":
        return
    if acc_type and acc_type != "oauth":
        return

    creds = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    rt = _clean_refresh_token(
        creds.get("refresh_token") or account.get("refresh_token") or extra.get("refresh_token")
    )
    if not rt:
        return
    email = _clean_email(
        creds.get("email")
        or extra.get("email")
        or account.get("email")
        or _email_from_text(str(account.get("name") or ""))
        or _email_from_source(source)
    )
    if not email:
        email = _fallback_email(source)
    yield OpenAIImportCandidate(email=email, refresh_token=rt, source=source)


def _clean_refresh_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if len(raw) >= 20 and " " not in raw and "\n" not in raw and "\t" not in raw:
        return raw
    match = _REFRESH_RE.search(raw)
    return match.group(0) if match else ""


def _clean_email(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = _EMAIL_RE.search(value.strip())
    return match.group(0) if match else ""


def _email_from_text(text: str) -> str:
    return _clean_email(text)


def _email_from_source(source: str) -> str:
    name = PurePosixPath(source or "").name
    if name.lower().endswith(".json"):
        name = name[:-5]
    return _email_from_text(name)


def _fallback_email(source: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "-", PurePosixPath(source or "import").stem).strip("-").lower()
    if not safe:
        safe = "account"
    return f"unnamed-openai-import-{safe[:48]}@local"


def _dedupe_candidates(candidates: Iterable[OpenAIImportCandidate]) -> list[OpenAIImportCandidate]:
    out: list[OpenAIImportCandidate] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        email = _clean_email(item.email) or item.email.strip()
        rt = _clean_refresh_token(item.refresh_token)
        if not rt:
            continue
        key = (email.lower(), rt)
        if key in seen:
            continue
        seen.add(key)
        out.append(OpenAIImportCandidate(email=email, refresh_token=rt, source=item.source))
    return out
