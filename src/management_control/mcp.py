"""MCP 管理控制：配置读写与调用日志查询，供 API 与 TG 共用。

与 SearchControl 同构：一个稀疏、保留密钥的配置边界，写入走 ``config.update``
的串行锁，并用稳定的 revision 做乐观并发控制。
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any

from src import config, log_db
from src.mcp import catalog, mount, policy

from .models.common import DomainControl, stable_revision
from .errors import ManagementError, ManagementErrorCode

SETTING_FIELDS = ("enabled", "mediaTtlSeconds", "maxRequestBodyBytes", "tools")
TOOL_FIELDS = catalog.TOOL_NAMES

# 资源 URL 有效期：1 分钟到 7 天。
MEDIA_TTL_RANGE = (60, 7 * 24 * 3600)
# 请求体上限：至少 1 MiB（图片编辑要传 data URL），至多 256 MiB。
MAX_BODY_RANGE = (1024 * 1024, 256 * 1024 * 1024)


def _normalize(raw: Any) -> dict:
    """把任意来源的配置规整成完整、可序列化的生效值。"""
    defaults = (config.DEFAULT_CONFIG.get("mcp") or {})
    current = raw if isinstance(raw, dict) else {}
    result: dict[str, Any] = {
        "enabled": bool(current.get("enabled", defaults.get("enabled", True))),
        "mediaTtlSeconds": int(current.get("mediaTtlSeconds", defaults.get("mediaTtlSeconds", 3600))),
        "maxRequestBodyBytes": int(current.get("maxRequestBodyBytes", defaults.get("maxRequestBodyBytes", 33554432))),
    }
    tools = dict(defaults.get("tools") or {})
    raw_tools = current.get("tools")
    if isinstance(raw_tools, dict):
        tools.update(raw_tools)
    result["tools"] = {name: bool(tools.get(name, True)) for name in TOOL_FIELDS}
    return result


class MCPControl(DomainControl):
    """MCP 设置与调用日志的管理入口。"""

    def __init__(self, *, audit_sink=None, operations=None):
        super().__init__(audit_sink=audit_sink)
        self.operations = operations

    # ----- 读取 ---------------------------------------------------------

    def get(self, context):
        self._read(context)
        cfg = _normalize(config.get().get("mcp"))
        cfg["revision"] = stable_revision(cfg)
        cfg["runtime"] = self.runtime_status()
        return cfg

    def runtime_status(self) -> dict:
        """当前运行时装配状态与端点信息（不含任何密钥）。"""
        state = mount.runtime_state()
        return {
            "mounted": bool(state.get("mounted")),
            "path": state.get("path") or "/mcp",
            "mediaPath": "/v1/mcp/media",
            "reason": state.get("reason"),
            "enabled": policy.enabled(),
        }

    # ----- 写入 ---------------------------------------------------------

    def _commit(self, context, action, mutate, expected_revision=None, *, read_back=True):
        self._write(context)
        try:
            def apply(root):
                # 在 config.update 的串行锁内执行；基线取当前生效值而非任意入参。
                effective = _normalize(root.get("mcp"))
                self._check_revision(expected_revision, stable_revision(effective))
                mutate(effective)
                effective.pop("revision", None)
                root["mcp"] = effective
            config.update(apply)
        except ManagementError:
            self._audit(context, action, "mcp", "failed")
            raise
        except Exception:
            self._audit(context, action, "mcp", "failed")
            raise ManagementError(ManagementErrorCode.DEPENDENCY_UNAVAILABLE) from None
        self._audit(context, action, "mcp", "succeeded")
        return self.get(context) if read_back else None

    @staticmethod
    def _invalid(field, message="Invalid MCP setting"):
        return DomainControl._validation(field, "invalid_value", message)

    def patch(self, context, patch, *, expected_revision=None):
        self._write(context)
        if not isinstance(patch, dict) or not patch or set(patch) - set(SETTING_FIELDS):
            raise self._invalid("body")
        for key, value in patch.items():
            if key == "enabled":
                if type(value) is not bool:
                    raise self._invalid(key, "Expected a boolean")
            elif key == "mediaTtlSeconds":
                low, high = MEDIA_TTL_RANGE
                if type(value) is not int or not low <= value <= high:
                    raise self._invalid(key, f"Expected integer in {low}..{high}")
            elif key == "maxRequestBodyBytes":
                low, high = MAX_BODY_RANGE
                if type(value) is not int or not low <= value <= high:
                    raise self._invalid(key, f"Expected integer in {low}..{high}")
            elif key == "tools":
                if not isinstance(value, dict) or set(value) - set(TOOL_FIELDS):
                    raise self._invalid(key, "Unknown MCP tool name")
                if any(type(flag) is not bool for flag in value.values()):
                    raise self._invalid(key, "Tool switches must be booleans")
        return self._commit(context, "mcp.settings.update",
                            lambda cfg: cfg.update(copy.deepcopy(patch)), expected_revision)

    def set_tool(self, context, tool_name, enabled_flag, *, expected_revision=None):
        self._write(context)
        if tool_name not in TOOL_FIELDS:
            raise self._invalid("tool", "Unknown MCP tool name")
        if type(enabled_flag) is not bool:
            raise self._invalid("enabled", "Expected a boolean")

        def mutate(cfg):
            cfg["tools"][tool_name] = enabled_flag
        return self._commit(context, "mcp.tool.update", mutate, expected_revision)

    # ----- 调用日志 -----------------------------------------------------

    def logs(self, context, *, period="today", api_key_name=None, tool_name=None,
             page=1, page_size=50):
        self._read(context)
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 50), 200))
        since = _period_start(period)
        rows = log_db.mcp_call_entries(
            since, api_key_name=api_key_name or None, tool_name=tool_name or None,
            limit=page_size, offset=(page - 1) * page_size,
        )
        return {
            "items": [_log_view(row) for row in rows],
            "page": page,
            "pageSize": page_size,
            "period": period,
            "hasNext": len(rows) >= page_size,
        }

    def stats(self, context, *, period="month"):
        self._read(context)
        since = _period_start(period)
        rows = log_db.mcp_call_stats(since)
        for row in rows:
            attempts = row.get("elapsed_n") or 0
            row["avgElapsedMs"] = int(row["elapsed_sum"] / attempts) if attempts else 0
            row["label"] = _tool_label(row.get("tool_name"))
        return {"period": period, "items": rows}


def _period_start(period: str) -> float:
    now = time.time()
    if period == "today":
        # 北京时间当日 00:00。
        local = time.localtime(now)
        return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    if period == "month":
        local = time.localtime(now)
        return time.mktime((local.tm_year, local.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    # 默认回退到当天，避免无边界查询。
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def _tool_label(tool_name: Any) -> str:
    spec = catalog.SPECS.get(str(tool_name or ""))
    return spec.name if spec else str(tool_name or "")


def _log_view(row: dict) -> dict:
    """把一行日志整理成公开视图（管理端只读，不含任何凭据）。"""
    params = row.get("params_json")
    try:
        params_value = json.loads(params) if params else None
    except Exception:
        params_value = None
    elapsed = row.get("elapsed_ms")
    return {
        "id": int(row.get("id") or 0),
        "callId": str(row.get("call_id") or ""),
        "createdAt": float(row.get("created_at") or 0.0),
        "apiKeyName": row.get("api_key_name"),
        "clientName": row.get("client_name"),
        "clientVersion": row.get("client_version"),
        "protocolVersion": row.get("protocol_version"),
        "toolName": str(row.get("tool_name") or ""),
        "toolLabel": _tool_label(row.get("tool_name")),
        "params": params_value,
        "status": str(row.get("status") or ""),
        "errorCode": row.get("error_code"),
        "errorMessage": row.get("error_message"),
        "elapsedMs": int(elapsed) if elapsed is not None else None,
        "sourceId": row.get("source_id"),
        "sourceType": row.get("source_type"),
        "model": row.get("model"),
        "provider": row.get("provider"),
        "accountKey": row.get("account_key"),
        "resultCount": int(row.get("result_count") or 0),
        "resultBytes": int(row.get("result_bytes") or 0),
        "mediaTokens": _load_tokens(row.get("media_tokens")),
        "videoRequestId": row.get("video_request_id"),
        "inputTokens": int(row.get("input_tokens") or 0),
        "outputTokens": int(row.get("output_tokens") or 0),
    }


def _load_tokens(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


DEFAULT_MCP_CONTROL = MCPControl()
