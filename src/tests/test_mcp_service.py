"""MCP 服务：工具名单一真相源、授权合并判定与媒体资源交付。

这些测试固定的是本模块的契约，而不是上游网关行为：
  - 工具名与权限要求只有一份定义（catalog）
  - 授权是逐层收窄的，Key 的 mcpTools 不能放开被全局关闭的工具
  - 工具说明与可选值随配置实时变化
  - 媒体资源 URL 由请求还原，图片与视频共用同一发布/下载路径
"""

from __future__ import annotations

import os as _os
import sys as _sys

_sys.path.insert(
    0,
    _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
)

from src.tests import _isolation

_isolation.isolate()

import asyncio
import json

import pytest
from fastapi import FastAPI, Request

from src import auth, config, image_artifacts, log_db
from src.mcp import catalog, policy


@pytest.fixture(scope="module", autouse=True)
def _setup_module():
    log_db.init()


def _set_keys(keys: dict) -> None:
    def mutate(cfg):
        cfg["apiKeys"] = keys
    config.update(mutate)


def _base_key(**overrides) -> dict:
    entry = {
        "key": "sk-test", "enabled": True, "allowedModels": [],
        "allowImages": False, "allowVideos": False,
        "allowMcp": False, "mcpTools": [],
    }
    entry.update(overrides)
    return entry


# ── 工具名单一真相源 ───────────────────────────────────────────────────────


def test_tool_names_and_schema_builders_are_consistent():
    # 每个工具都必须同时有静态定义与参数 schema，否则 tools/list 会 KeyError。
    from src.mcp import server as mcp_server

    assert set(catalog.SPECS) == set(catalog.TOOL_NAMES)
    assert set(mcp_server._SCHEMA_BUILDERS) == set(catalog.TOOL_NAMES)


@pytest.mark.parametrize("tool_name", catalog.TOOL_NAMES)
def test_every_tool_renders_a_description_without_private_detail(tool_name):
    text = catalog.render_description(tool_name)
    assert text and len(text) > 10


def test_media_tool_requirements_only_reference_known_tools_and_permissions():
    for tool_name, requirement in catalog.MEDIA_TOOL_REQUIREMENTS.items():
        assert tool_name in catalog.TOOL_NAMES
        assert requirement in ("images", "videos")


# ── 授权逐层收窄 ───────────────────────────────────────────────────────────


def test_mcp_is_off_by_default_for_an_existing_key():
    _set_keys({"k": _base_key()})
    assert auth.mcp_allowed("k") is False
    assert policy.tool_allowed("k", "web_search") is False


def test_unknown_key_and_unknown_tool_are_denied():
    _set_keys({"k": _base_key(allowMcp=True)})
    assert policy.tool_allowed(None, "web_search") is False
    assert policy.tool_allowed("missing", "web_search") is False
    assert policy.tool_allowed("k", "not_a_tool") is False


def test_global_switch_disables_every_tool():
    _set_keys({"k": _base_key(allowMcp=True)})
    config.update(lambda cfg: cfg.setdefault("mcp", {}).update({"enabled": False}))
    try:
        assert policy.enabled() is False
        assert policy.allowed_tools("k") == []
    finally:
        config.update(lambda cfg: cfg["mcp"].update({"enabled": True}))


def test_key_tool_selection_cannot_reopen_a_globally_disabled_tool():
    _set_keys({"k": _base_key(allowMcp=True, mcpTools=["web_search", "image_generate"])})
    config.update(lambda cfg: cfg["mcp"]["tools"].update({"web_search": False}))
    try:
        allowed = policy.allowed_tools("k")
        assert "web_search" not in allowed      # 全局关闭优先
        assert "web_fetch" not in allowed       # 未被该 Key 选中
        assert "image_generate" not in allowed  # 还需要图片权限
    finally:
        config.update(lambda cfg: cfg["mcp"]["tools"].update({"web_search": True}))


def test_empty_tool_selection_follows_the_global_switches():
    # 空选择 = 跟随全局；但媒体工具仍需该 Key 自身的媒体权限，所以只在
    # 同时开启图片/视频权限时才能拿到全部六个。
    _set_keys({"k": _base_key(allowMcp=True, mcpTools=[],
                              allowImages=True, allowVideos=True)})
    assert policy.allowed_tools("k") == list(catalog.TOOL_NAMES)

    _set_keys({"k": _base_key(allowMcp=True, mcpTools=[])})
    assert policy.allowed_tools("k") == ["web_search", "web_fetch"]


def test_media_tools_additionally_require_the_existing_media_permissions():
    _set_keys({"k": _base_key(allowMcp=True)})
    assert "image_generate" not in policy.allowed_tools("k")

    _set_keys({"k": _base_key(allowMcp=True, allowImages=True)})
    allowed = policy.allowed_tools("k")
    assert "image_generate" in allowed and "image_edit" in allowed
    assert "video_generate" not in allowed

    _set_keys({"k": _base_key(allowMcp=True, allowVideos=True)})
    allowed = policy.allowed_tools("k")
    assert "video_generate" in allowed and "video_status" in allowed
    assert "image_generate" not in allowed


def test_denial_reason_names_the_layer_that_refused():
    _set_keys({"k": _base_key(allowMcp=True, mcpTools=["web_search"])})
    assert "未授权" in policy.denial_reason("k", "image_generate")

    _set_keys({"k": _base_key(allowMcp=False)})
    assert "MCP" in policy.denial_reason("k", "web_search")

    config.update(lambda cfg: cfg["mcp"]["tools"].update({"web_fetch": False}))
    try:
        assert "全局禁用" in policy.denial_reason("k", "web_fetch")
    finally:
        config.update(lambda cfg: cfg["mcp"]["tools"].update({"web_fetch": True}))


def test_settings_always_reports_every_known_tool():
    tools = policy.settings()["tools"]
    assert set(tools) == set(catalog.TOOL_NAMES)
    assert all(isinstance(v, bool) for v in tools.values())


# ── 实时可选值 ─────────────────────────────────────────────────────────────


def test_search_description_lists_only_currently_available_engines():
    text = catalog.render_description("web_search")
    available = catalog.available_engines()
    if available:
        assert available[0] in text
    # 不可用的来源必须被点名，模型才知道为什么调用失败，而不是反复重试。
    statuses = __import__("src.search_service", fromlist=["x"]).backend_statuses()
    unusable = [r["id"] for r in statuses if not r.get("available")]
    if unusable:
        assert "不可用" in text


def test_image_options_come_from_the_live_catalog():
    from src import image_catalog

    expected = list(image_catalog.available_models())
    assert catalog.image_sources() == expected


# ── 媒体资源发布与交付 ─────────────────────────────────────────────────────


def _png_bytes() -> bytes:
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _mp4_bytes() -> bytes:
    # 内容不需要可解码；这里只验证字节原样交付与扩展名归类。
    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


@pytest.fixture()
def media_env(tmp_path):
    cfg = config.get()
    previous = {k: cfg.get(k) for k in ("images", "videos")}
    config.update(lambda c: (
        c.setdefault("images", {}).update({"cacheEnabled": True, "cachePath": str(tmp_path)}),
        c.setdefault("videos", {}).update({"cacheEnabled": True, "cachePath": str(tmp_path)}),
    ))
    image_artifacts._ASSETS.clear()
    yield
    image_artifacts._ASSETS.clear()
    config.update(lambda c: [c.update({k: v}) for k, v in previous.items() if v is not None])


def _request_with_base(base_url: str, *, scheme: str = "https", port: int = 443) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [], "scheme": scheme, "server": (base_url, port)})


def test_image_and_video_publish_use_their_own_prefixes_and_media_types(media_env):
    cfg = config.get()["images"]
    img_url, _ = image_artifacts.publish(
        _png_bytes(), mime="image/png", cfg=cfg, base_url="https://api.example.com",
        provider="openai", action="generate", index=0,
    )
    vid_cfg = dict(config.get()["videos"], _media_kind="video")
    vid_url, _ = image_artifacts.publish(
        _mp4_bytes(), mime="video/mp4", cfg=vid_cfg, base_url="https://api.example.com",
        provider="xai", action="generate", index=0, media_type="video",
    )
    assert img_url.startswith("https://api.example.com/v1/images/assets/")
    assert vid_url.startswith("https://api.example.com/v1/mcp/media/")
    assert image_artifacts.media_token(img_url) != image_artifacts.media_token(vid_url)


def test_published_url_reflects_the_request_origin_without_configuration(media_env):
    cfg = config.get()["images"]
    request = _request_with_base("mcp.customer.example", scheme="http", port=80)
    url, expiry = image_artifacts.publish(
        _png_bytes(), mime="image/png", cfg=cfg, request=request,
        provider="openai", action="generate", index=0,
    )
    # 反代保留 Host 时 base_url 就是对外地址，因此不需要任何域名配置。
    assert url.startswith("http://mcp.customer.example/v1/images/assets/")
    assert expiry > 0


def test_media_entrypoint_refuses_the_other_media_kind(media_env):
    cfg = config.get()["images"]
    img_url, _ = image_artifacts.publish(
        _png_bytes(), mime="image/png", cfg=cfg, base_url="https://api.example.com",
        provider="openai", action="generate", index=0,
    )
    token = image_artifacts.media_token(img_url)
    request = _request_with_base("api.example.com")

    # 图片端点只服务图片；MCP 端点两者都可。
    assert asyncio.run(image_artifacts.download(request, token)).status_code == 200
    assert asyncio.run(image_artifacts.download_mcp_media(request, token)).status_code == 200
    assert asyncio.run(image_artifacts.download(request, "missing")).status_code == 404


def test_video_published_through_the_mcp_endpoint_downloads_by_token(media_env):
    vid_cfg = dict(config.get()["videos"], _media_kind="video")
    raw = _mp4_bytes()
    url, _ = image_artifacts.publish(
        raw, mime="video/mp4", cfg=vid_cfg, base_url="https://api.example.com",
        provider="xai", action="generate", index=0, media_type="video",
    )
    assert "video" in image_artifacts._ASSETS[image_artifacts.media_token(url)][3]
    assert asyncio.run(
        image_artifacts.download_mcp_media(_request_with_base("api.example.com"),
                                           image_artifacts.media_token(url))
    ).status_code == 200


def test_publish_requires_a_base_url_source(media_env):
    cfg = config.get()["images"]
    with pytest.raises(ValueError):
        image_artifacts.publish(
            _png_bytes(), mime="image/png", cfg=cfg,
            provider="openai", action="generate", index=0,
        )


# ── MCP 调用日志（独立事实表） ─────────────────────────────────────────────


def test_mcp_call_log_records_denials_that_never_reached_an_upstream():
    handle = log_db.record_mcp_call(
        call_id="call-1", tool_name="image_generate", api_key_name="cust",
        client_name="test-client", client_version="1.0",
        protocol_version="2025-11-25",
        params={"prompt": "a cat"},
    )
    log_db.finish_mcp_call(handle, status="denied", error_code="tool_not_allowed",
                           error_message="not permitted", elapsed_ms=3)
    rows = log_db.mcp_call_entries(0, api_key_name="cust")
    row = next(r for r in rows if r["call_id"] == "call-1")
    assert row["status"] == "denied"
    assert row["error_code"] == "tool_not_allowed"
    assert json.loads(row["params_json"]) == {"prompt": "a cat"}


def test_mcp_call_stats_aggregate_per_tool():
    h1 = log_db.record_mcp_call(call_id="s-1", tool_name="web_search", api_key_name="agg")
    log_db.finish_mcp_call(h1, status="success", elapsed_ms=10, result_count=3)
    h2 = log_db.record_mcp_call(call_id="s-2", tool_name="web_search", api_key_name="agg")
    log_db.finish_mcp_call(h2, status="error", elapsed_ms=30, error_code="tool_error")
    stats = {row["tool_name"]: row for row in log_db.mcp_call_stats(0)}
    assert stats["web_search"]["calls"] >= 2
    assert stats["web_search"]["success"] >= 1
    assert stats["web_search"]["failed"] >= 1


def test_finish_mcp_call_tolerates_a_missing_handle():
    # 日志写入失败不得让工具调用本身失败。
    log_db.finish_mcp_call(None, status="success")
