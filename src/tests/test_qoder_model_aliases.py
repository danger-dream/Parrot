"""Official Qoder names become account-local aliases without changing wire IDs."""
import copy
from dataclasses import replace

import httpx
import pytest

from src import config, model_state
from src.channel import registry
from src.channel.qoder_oauth_channel import QoderOAuthChannel
from src.management_control.models import ModelCenterControl, ModelFilters, ModelSourceRef
from src.oauth.qoder import catalog
from src.telegram.menus import model_center_menu, oauth_account_models_menu
from src.telegram.menus.model_center_catalog import _model_display_name, _qoder_rate_lines
from src.tests import test_protocol_fake_upstreams as fake
from src.tests.test_qoder_channel import env, call, frames, request_body


def test_aliases_use_official_names_not_guessed_vendor_models():
    records = [{"id": key, "name": name} for key, name in (
        ("cmodel", "Cantus"), ("smodel", "Sonus"), ("dmodel", "DeepSeek-V4-Pro"),
        ("dfmodel", "DeepSeek-Flash"), ("gmodel", "GLM-5.3"), ("qfmodel", "Qwen3.8-Flash"))]
    assert catalog.model_aliases(records) == {
        "cmodel": "qoder-cantus", "smodel": "qoder-sonus", "dmodel": "qoder-deepseek-v4-pro",
        "dfmodel": "qoder-deepseek-flash", "gmodel": "qoder-glm-5.3", "qfmodel": "qoder-qwen3.8-flash"}
    assert catalog.model_aliases([{"id": "unknown", "name": "unknown"}]) == {}
    duplicated = [{"id": "first", "name": "Same Name"}, {"id": "second", "name": "Same Name"}]
    aliases = catalog.model_aliases(duplicated)
    assert len(set(aliases.values())) == 2
    assert aliases == catalog.model_aliases(list(reversed(duplicated)))


def test_aliases_follow_each_accounts_catalog_and_preserve_old_ids(env):
    _, channel = env
    first = copy.deepcopy(channel.account)
    first.update(models=["dmodel"], account_model_catalog={"models": [
        {"id": "dmodel", "name": "DeepSeek-V4-Pro", "qoderConfig": {"key": "dmodel"}}]})
    before = copy.deepcopy(first)
    older = copy.deepcopy(first)
    older["account_model_catalog"]["models"][0]["name"] = "DeepSeek-V3"
    current_channel, old_channel = QoderOAuthChannel(first), QoderOAuthChannel(older)
    assert current_channel.list_client_models() == ["qoder-deepseek-v4-pro"]
    assert current_channel.supports_model("qoder-deepseek-v4-pro") == "dmodel"
    assert current_channel.supports_model("dmodel") == "dmodel"
    assert current_channel.supports_model("qoder-deepseek-v3") is None
    assert old_channel.supports_model("qoder-deepseek-v4-pro") is None
    assert old_channel.supports_model("qoder-deepseek-v3") == "dmodel"
    assert first == before
    first["disabledModels"] = ["dmodel"]
    disabled = QoderOAuthChannel(first)
    assert disabled.supports_model("dmodel") is None
    assert disabled.supports_model("qoder-deepseek-v4-pro") is None


@pytest.mark.parametrize("ingress", ["chat", "responses", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
async def test_alias_calls_keep_original_wire_id_and_readable_response_model(env, ingress, stream):
    _, channel = env
    body = request_body(ingress, stream)
    body["model"] = "qoder-ultimate"
    before = copy.deepcopy(config.get()["oauthAccounts"])
    response, text, seen = await call(env, ingress, body, lambda req:
        httpx.Response(200, stream=fake.ChunkedByteStream(frames()), headers={"content-type": "text/event-stream"}))
    assert response.status_code == 200, text
    assert len(seen) == 1 and seen[0]["model_config"]["key"] == "ultimate"
    assert '"model":"qoder-ultimate"' in text.replace(" ", "")
    assert config.get()["oauthAccounts"] == before
    assert channel.supports_model("ultimate") == "ultimate"


def test_legacy_global_disable_and_hide_apply_to_alias_discovery(env):
    _, channel = env
    assert registry._model_discovery_visible(channel, "qoder-ultimate")
    config.update(lambda current: model_state.set_visible_in_config(current, ["ultimate"], False))
    assert not registry._model_discovery_visible(channel, "qoder-ultimate")
    config.update(lambda current: model_state.set_visible_in_config(current, ["ultimate"], True))
    config.update(lambda current: model_state.set_global_enabled_in_config(current, ["ultimate"], False))
    assert not registry._model_discovery_visible(channel, "qoder-ultimate")


def test_model_center_and_account_pages_show_only_downstream_names(env, monkeypatch):
    _, channel = env
    control = ModelCenterControl()
    page = control.list_models(filters=ModelFilters(text="Ultimate"))
    view = next(item for item in page.items if item.model_id == "ultimate")
    assert view.aliases == ("qoder-ultimate",)
    assert _model_display_name(view, None) == "qoder-ultimate"
    assert view.sources[0].outbound_model == "ultimate"
    assert _model_display_name(replace(view, sources=(replace(view.sources[0], provider="openai"),)), None) == "ultimate"
    account_text, _ = oauth_account_models_menu.render(channel.account_key)
    assert "<code>qoder-ultimate</code>" in account_text
    assert "调用名" not in account_text
    assert "<code>Ultimate</code>" not in account_text
    assert "💳 积分倍率：×2" in account_text
    assert _qoder_rate_lines(view, None) == ["💳 Qoder 积分倍率：×2"]
    detail_text, _ = oauth_account_models_menu._detail_render(channel.account_key, "ultimate",
        model_page=1, account_page=1, filter_key="all")
    assert "完整 ID: <code>qoder-ultimate</code>" in detail_text
    assert "显示名称" not in detail_text
    assert "积分倍率: <code>×2</code>" in detail_text
    monkeypatch.setattr(model_center_menu, "_CONTROL", control)
    monkeypatch.setattr(model_center_menu, "_ctx", control.bind_telegram_actor)
    monkeypatch.setitem(model_center_menu._sessions, 987654, model_center_menu._Session())
    list_text, _ = model_center_menu._model_list_render(987654)
    assert "<code>qoder-ultimate</code>" in list_text
    assert "调用名" not in list_text
    assert "<code>Ultimate</code>" not in list_text
    center_detail, _ = model_center_menu._detail_render(987654, view.resource_key)
    assert "<b>qoder-ultimate</b>" in center_detail
    assert "别名：" not in center_detail
    assert "上游名：<code>ultimate</code>" not in center_detail
    assert "💳 Qoder 积分倍率：×2" in center_detail


@pytest.mark.parametrize("record,label", [
    ({}, ""), ({"qoderRate": None, "qoderFree": False}, ""),
    ({"qoderRate": 2.0}, "×2"), ({"qoderRate": 0.5}, "×0.5"),
    ({"qoderRate": 0.0, "qoderFree": True, "qoderRateWas": 0.1}, "免费（原×0.1）"),
    ({"qoderRate": 0.2, "qoderRateWas": 1.0}, "×0.2（原×1）"),
    ({"qoderRate": None, "qoderFree": True}, "免费"),
])
def test_credit_rate_labels_use_known_fees_and_free_flags(record, label):
    assert catalog.credit_label(record) == label
    missing = catalog.model_record({"key": "unpriced", "display_name": "Unpriced"}, "app")
    assert missing["qoderRate"] is None and not missing["qoderFree"]
    assert catalog.credit_label(missing) == ""


def test_credit_rates_stay_account_scoped_when_rates_differ(env):
    control = ModelCenterControl()
    view = next(item for item in control.list_models().items if item.model_id == "ultimate")
    first = view.sources[0]
    second = replace(first, id="qoder:global:second", effective_metadata={"qoderRate": 8.0})
    combined = replace(view, sources=(first, second))
    assert _qoder_rate_lines(combined, ModelSourceRef(first.type, first.id)) == ["💳 Qoder 积分倍率：×2"]
    assert _qoder_rate_lines(combined, ModelSourceRef(second.type, second.id)) == ["💳 Qoder 积分倍率：×8"]
    assert _qoder_rate_lines(combined, None) == ["💳 Qoder 积分倍率：×2 / ×8（随账户不同，详见详情）"]
    absent = replace(view, sources=(replace(first, effective_metadata={}),))
    assert _qoder_rate_lines(absent, None) == []
