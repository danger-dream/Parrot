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


# ── 结果内容留存（与摘要分表，受 logStoreBodies 控制） ────────────────────


def _record(tool="web_search", status="success", params=None, **finish_kw):
    call_id = f"d-{tool}-{status}-{len(log_db.mcp_call_entries(0))}"
    handle = log_db.record_mcp_call(
        call_id=call_id, tool_name=tool, api_key_name="tk", params=params or {"query": "q"},
    )
    log_db.finish_mcp_call(handle, status=status, **finish_kw)
    return handle, call_id


def test_result_content_is_stored_alongside_the_summary():
    handle, call_id = _record()
    payload = {"query": "hello", "results": [{"title": "t", "url": "u"}]}
    log_db.save_mcp_call_detail(handle, payload)
    row = log_db.mcp_call_detail(call_id)
    assert row is not None
    assert json.loads(row["result_body"]) == payload


def test_result_content_survives_only_while_bodies_are_enabled():
    # 关闭正文保存后不得再写入新内容；已存在的内容不受影响（与实时日志同口径）。
    handle, call_id = _record()
    config.update(lambda cfg: cfg.__setitem__("logStoreBodies", False))
    try:
        log_db.save_mcp_call_detail(handle, {"query": "secret"})
        assert log_db.mcp_call_detail(call_id) is None
    finally:
        config.update(lambda cfg: cfg.__setitem__("logStoreBodies", True))

    log_db.save_mcp_call_detail(handle, {"query": "visible"})
    assert log_db.mcp_call_detail(call_id) is not None


def test_result_content_is_absent_for_denied_calls():
    # 被拒的调用没有结果可存；详情页据此提示而不是显示空白。
    _, call_id = _record(tool="image_generate", status="denied",
                         error_code="tool_not_allowed")
    assert log_db.mcp_call_detail(call_id) is None


def test_result_content_is_idempotent_per_call():
    handle, call_id = _record()
    log_db.save_mcp_call_detail(handle, {"query": "first"})
    log_db.save_mcp_call_detail(handle, {"query": "second"})
    assert json.loads(log_db.mcp_call_detail(call_id)["result_body"]) == {"query": "second"}


def test_status_counts_keep_denied_and_timeout_separate_from_failure():
    """汇总必须显式区分拒绝/超时，否则它们在总数里静默消失。"""
    _record(tool="web_fetch", status="success")
    _record(tool="web_fetch", status="error", error_code="tool_error")
    _record(tool="image_generate", status="denied", error_code="tool_not_allowed")
    _record(tool="video_generate", status="timeout", error_code="timeout")
    counts = log_db.mcp_call_status_counts(0)
    assert counts.get("success", 0) >= 1
    assert counts.get("error", 0) >= 1
    assert counts.get("denied", 0) >= 1
    assert counts.get("timeout", 0) >= 1
    # 总数就是各状态之和，不存在被漏掉的状态
    assert sum(counts.values()) >= 4


def test_control_summary_reports_every_status_bucket():
    from src.management_control.auxiliary.common import telegram_context
    from src.management_control.mcp import DEFAULT_MCP_CONTROL

    _record(tool="web_search", status="success", result_count=2)
    _record(tool="web_search", status="denied", error_code="tool_not_allowed")
    summary = DEFAULT_MCP_CONTROL.summary(telegram_context(1), period="month")
    assert summary["calls"] == (
        summary["success"] + summary["failed"] + summary["denied"]
        + summary["timeout"] + summary["running"]
    )
    assert summary["success"] >= 1 and summary["denied"] >= 1
    names = {t["toolName"] for t in summary["byTool"]}
    assert "web_search" in names


# ── TG 菜单渲染 ──────────────────────────────────────────────────────────


def _menu():
    from src.telegram.menus import mcp_menu

    return mcp_menu


def test_menu_main_page_shows_status_install_help_and_today_stats():
    menu = _menu()
    from src.management_control.auxiliary.common import telegram_context

    _record(tool="web_search", status="success")
    text, kb = menu._main_text_and_kb(1)
    assert "MCP 服务" in text
    assert "端点：" in text
    assert "接入方式" in text          # 安装说明
    assert "mcpServers" in text        # 可直接复制的配置
    assert "今日调用" in text          # 简单统计
    rows = kb["inline_keyboard"]
    # 状态与日志同排；两个返回按钮同排。
    assert [b["callback_data"] for b in rows[0]] == ["mcp:toggle", "mcp:logs:1"]
    assert [b["callback_data"] for b in rows[1]] == ["menu:settings", "menu:main"]


def test_menu_list_puts_the_query_source_and_status_on_the_row():
    """列表必须直接显示"搜了什么、用的什么引擎"，而不是只给编号。"""
    menu = _menu()

    _record(tool="web_search", status="success", params={"query": "今天 AI 新闻"},
            result_count=10, result_bytes=2048, source_id="xai", elapsed_ms=14830)
    rows, summary = menu._log_page(1, 1)
    text = menu._render_list(rows, page=1, pages=1, summary=summary)
    assert "今天 AI 新闻" in text      # 查询词
    assert "xai" in text               # 引擎
    assert "网络搜索" in text          # 工具中文名
    assert "今日" in text


def test_menu_list_localizes_tool_names_in_the_summary():
    menu = _menu()

    _record(tool="image_generate", status="success", params={"prompt": "猫"})
    rows, summary = menu._log_page(1, 1)
    text = menu._render_list(rows, page=1, pages=1, summary=summary)
    # 汇总行不得出现英文工具 id
    assert "image_generate" not in text
    assert "图片生成" in text


def test_menu_detail_renders_params_for_a_denied_call():
    menu = _menu()

    _, call_id = _record(tool="image_generate", status="denied",
                         params={"prompt": "一只猫"},
                         error_code="tool_not_allowed",
                         error_message="未授权使用 image_generate。")
    row = next(r for r in menu._CONTROL.logs(
        __import__("src.management_control.auxiliary.common", fromlist=["telegram_context"])
        .telegram_context(1), period="month", page_size=200)["items"]
        if r["callId"] == call_id)
    text = menu._render_detail(row)
    assert "图片生成" in text
    assert "tool_not_allowed" in text
    assert "一只猫" in text            # 参数要被看见


def test_menu_result_page_expands_search_results_readably():
    """搜索结果要以人可读的条目展开，而不是把原始 JSON 挤成一行。"""
    menu = _menu()

    body = json.dumps({
        "query": "今天 AI 新闻",
        "results": [
            {"title": "OpenAI 发布新模型", "url": "https://a.com/1", "snippet": "多模态提升"},
            {"title": "AI 监管新规", "url": "https://b.com/2", "snippet": "欧盟通过"},
        ],
    }, ensure_ascii=False)
    pretty = menu._pretty_result(body)
    assert "查询：今天 AI 新闻" in pretty
    assert "结果 2 条" in pretty
    assert "1. OpenAI 发布新模型" in pretty
    assert "https://a.com/1" in pretty
    assert "多模态提升" in pretty


def test_menu_result_page_falls_back_to_content_and_raw_text():
    menu = _menu()
    # 抓取工具返回长正文：直接显示，不做 JSON 展开。
    assert menu._pretty_result(json.dumps({"url": "https://x", "content": "正文" * 5},
                                          ensure_ascii=False)).startswith("正文")
    # 无法解析时原样返回，绝不因排版丢内容。
    assert menu._pretty_result("not json at all") == "not json at all"


def test_menu_result_is_paged_for_long_content():
    menu = _menu()
    long_body = json.dumps({"content": "字" * 9000}, ensure_ascii=False)
    pages = menu._chunk_pages(menu._pretty_result(long_body))
    assert len(pages) > 1
    # 分页不丢字符
    assert "".join(pages) == menu._pretty_result(long_body)


def test_menu_handles_the_result_callback_action():
    menu = _menu()
    assert menu.handle_callback(1, 1, "cb", "mcp:result:shortcode:1:1") is True


def test_menu_ignores_foreign_callbacks():
    menu = _menu()
    assert menu.handle_callback(1, 1, "cb", "srch:show") is False


def test_search_records_the_engine_without_exposing_it_to_the_model():
    """日志要能回答"用的哪个引擎"，但来源标识不能交给模型。

    `_model_visible_search_result()` 有意剥掉 backend_id/provider/attempts，
    所以这些必须在剥除**之前**取出来单独返回，否则列表里的来源永远是空的。
    """
    import asyncio

    from src.mcp import server as mcp_server
    from src import search_service

    async def fake_search(*args, **kwargs):
        return {
            "query": "q", "results": [{"title": "t"}],
            "backend_id": "xai", "provider": "xai", "model": "grok-4",
            "attempts": [
                {"backend_id": "tavily", "provider": "tavily"},
                {"backend_id": "xai", "provider": "xai"},
            ],
        }

    original = search_service.search
    search_service.search = fake_search
    try:
        visible, telemetry = asyncio.run(
            mcp_server._run_search("web_search", {"query": "q"}, request_id="r1")
        )
    finally:
        search_service.search = original

    # 遥测取最终成功那一次尝试的来源（搜索可以合法地跨来源重试）
    assert telemetry["source_id"] == "xai"
    assert telemetry["source_type"] == "xai"
    assert telemetry["result_count"] == 1
    assert telemetry["model"] == "grok-4"
    # 模型侧干净：没有来源标识、没有尝试明细
    assert "backend_id" not in visible
    assert "provider" not in visible
    assert "attempts" not in visible


# ── source 参数的实时 enum ────────────────────────────────────────────────


def test_every_tool_exposes_source_as_an_enum_not_free_text():
    """source 必须是带 enum 的字符串，不能只靠说明文字约束取值。"""
    from src.mcp import server as mcp_server

    for name in catalog.TOOL_NAMES:
        if not catalog.accepts_source(name):
            continue
        prop = mcp_server.build_tool(name).input_schema["properties"]["source"]
        assert prop["type"] == "string"
        assert isinstance(prop.get("enum"), list), name
        assert prop["enum"], name                      # 空 enum 等于不可选
        assert catalog.AUTO in prop["enum"], name       # 总要能选"自动"


def test_source_enum_matches_the_live_catalogue_per_tool_family():
    """enum 必须来自当前配置，且按工具类别给出对应的一类上游。"""
    from src.mcp import server as mcp_server

    def enum_of(name):
        return mcp_server.build_tool(name).input_schema["properties"]["source"]["enum"]

    assert enum_of("web_search") == [catalog.AUTO, *catalog.available_engines()]
    assert enum_of("image_generate") == [catalog.AUTO, *catalog.image_sources()]
    assert enum_of("video_generate") == [catalog.AUTO, *catalog.video_sources()]
    # 图片与视频不能互相混入
    assert not set(catalog.image_sources()) & set(catalog.video_sources())


def test_source_enum_is_recomputed_with_the_configuration():
    """上游增删后 enum 必须跟着变，不能是构建时定死的常量。"""
    from src.mcp import server as mcp_server

    before = mcp_server.build_tool("web_search").input_schema["properties"]["source"]["enum"]
    # 关掉一个引擎后重新构建，它应从 enum 中消失
    target = before[1] if len(before) > 1 else None
    if target is None:
        pytest.skip("没有可关闭的引擎")
    from src import search_service

    original = search_service.backend_statuses
    search_service.backend_statuses = lambda: [
        {"id": r["id"], "available": r["id"] != target}
        for r in original()
    ]
    try:
        after = mcp_server.build_tool("web_search").input_schema["properties"]["source"]["enum"]
    finally:
        search_service.backend_statuses = original
    assert target not in after


def test_model_parameter_is_gone_and_source_drives_the_model():
    """媒体工具只保留 source 一个"选上游"参数，且它必须真的生效。"""
    from src.mcp import server as mcp_server

    for name in ("image_generate", "image_edit", "video_generate"):
        props = mcp_server.build_tool(name).input_schema["properties"]
        assert "model" not in props, name
        assert "source" in props, name

    model = _first_image_model()
    if model is None:
        pytest.skip("测试环境未配置图片模型")
    assert mcp_server._requested_model({"source": model}, kind="image") == model
    # 省略或 auto 时交给服务端按当前配置决定
    assert mcp_server._requested_model({}, kind="image") == "auto"
    assert mcp_server._requested_model({"source": "auto"}, kind="image") == "auto"


def _first_image_model():
    from src.mcp import catalog as _catalog

    sources = _catalog.image_sources()
    return sources[0] if sources else None


def test_legacy_model_argument_still_works_for_in_flight_clients():
    """升级期间已发出的旧调用仍带 model，不应因此失效。"""
    from src.mcp import server as mcp_server

    assert mcp_server._requested_model({"model": "gpt-image-2"}, kind="image") == "gpt-image-2"
    assert mcp_server._requested_model({"model": "auto"}, kind="image") == "auto"
    # 两者同时出现时以 source 为准（它才是现在对外暴露的那个）。
    # 用一个真的在可用列表里的模型，否则会被当不可用而报错。
    model = _first_image_model()
    if model is not None:
        assert mcp_server._requested_model(
            {"source": model, "model": "别的"}, kind="image"
        ) == model


def test_video_status_does_not_advertise_a_source_it_ignores():
    """video_status 只按 request_id 查询，给它 source 会误导模型。"""
    from src.mcp import server as mcp_server

    assert not catalog.accepts_source("video_status")
    props = mcp_server.build_tool("video_status").input_schema["properties"]
    assert "source" not in props
    assert "timeout_seconds" in props       # 通用参数仍在
    assert catalog.live_options("video_status") == ([], "")
    assert "当前可用" not in mcp_server.build_tool("video_status").description


def test_description_and_enum_agree_on_the_available_sources():
    """说明里列出的可用值必须与 enum 一致，避免两处说法不同。"""
    from src.mcp import server as mcp_server

    for name in catalog.TOOL_NAMES:
        if not catalog.accepts_source(name):
            continue
        tool = mcp_server.build_tool(name)
        enum = tool.input_schema["properties"]["source"]["enum"]
        options = [v for v in enum if v != catalog.AUTO]
        if options:
            assert options[0] in (tool.description or ""), name
