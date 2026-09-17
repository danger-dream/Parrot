"""Controller-found API source availability and final panel presentation boundaries."""
from __future__ import annotations
import copy
import uuid
import pytest

from src.management_control.models import ModelCenterControl
from src.telegram import states, ui
from src.telegram.menus import model_center_menu as menu
from src.tests.test_media_panel_independence import media_env, CTX


def test_api_source_available_requires_one_routable_model_and_explains_container_gate(media_env):
    store, images, _videos, _channel, _tmp = media_env
    api = dict(name='image-api', protocol='openai-chat', providerId='openai', enabled=True,
        generationId=uuid.uuid4().hex, apiKey='synthetic', baseUrl='https://example.invalid',
        models=[{'real':'gpt-image-a'}, {'real':'gpt-image-b'}])
    store.value['channels'] = [api]
    store.value['modelCenter'] = {'disabledModels':['gpt-image-a','gpt-image-b']}
    source = next(s for s in images.list_sources(CTX) if s['source_id']=='api:image-api')
    assert source['enabled'] is True  # configured purpose, not effective routing state
    assert source['effective_available'] is False
    assert '模型' in source['unavailable_reason']
    store.value['modelCenter']['disabledModels'] = ['gpt-image-a']
    source = next(s for s in images.list_sources(CTX) if s['source_id']=='api:image-api')
    assert source['effective_available'] is True  # second model is usable
    api['enabled'] = False
    source = next(s for s in images.list_sources(CTX) if s['source_id']=='api:image-api')
    assert source['effective_available'] is False
    assert '渠道' in source['unavailable_reason']
    original=copy.deepcopy(api)
    images.update_source(CTX,source['source_id'],enabled=False,expected_revision=source['revision'])
    assert store.value['channels'][0]==original  # never changes ordinary conversation state


def test_final_panel_cache_timeout_logs_buttons_have_icons_and_direct_parent(media_env, monkeypatch):
    _store, images, videos, _channel, _tmp=media_env
    monkeypatch.setattr(menu, '_CONTROL', ModelCenterControl(images=images,videos=videos))
    monkeypatch.setattr(ui,'is_admin',lambda chat: chat==91)
    menu.reset_for_tests(); states.clear_all()
    try:
        for kind in ('image','video'):
            renderer=menu._image_settings_render if kind=='image' else menu._video_settings_render
            _,kb=renderer(91)
            buttons=[b for row in kb['inline_keyboard'] for b in row]
            labels={b['text'] for b in buttons}
            assert {'📁 设置缓存目录','⏱️ 设置请求超时','📋 多媒体日志','◀ 返回主菜单','📅 设置保留天数','💾 设置空间上限'} <= labels
            assert next(b for b in buttons if b['text']=='◀ 返回主菜单')['callback_data']=='menu:main'
            if kind=='video': assert '⏳ 设置任务 TTL' in labels
            for b in buttons: assert len(b['callback_data'].encode())<=64
    finally:
        menu.reset_for_tests(); states.clear_all()
