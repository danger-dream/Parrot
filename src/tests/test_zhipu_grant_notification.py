"""Grant notifications show observed additions and use working navigation/close buttons."""
import copy
import time
from datetime import datetime, timezone, timedelta

import pytest
from src import oauth_manager as om, notifier
from src.oauth.zhipu import actions, common
from src.telegram import bot, states, ui
from src.telegram.menus import oauth_menu as menu, zhipu_oauth_menu as zh
from src.tests.test_zhipu_provider import account_env, credential
from src.tests.test_zhipu_management import ctl, reset_env


def cards(five=(), week=()):
    return {"available_five_hour_resets":[{"expire_at":n} for n in five],
            "available_week_resets":[{"expire_at":n} for n in week],
            "latest_five_hour_reset_history":None,"latest_week_reset_history":None,"has_unread_history":False}


def test_notification_separates_new_cards_inventory_and_expiry():
    expiry = (time.time()+86400)*1000
    before = cards(week=[expiry])
    after = cards(five=[expiry, expiry], week=[expiry])
    account = credential("oauth", label="账户 <A>")
    text, keyboard = zh.reset_grant_notification("key",account,before,after)
    assert "账户 &lt;A&gt;" in text
    assert "5小时额度重置卡：<b>2 张</b>" in text
    assert "周额度重置卡：" not in text
    assert "当前可用：5小时卡 2 张 · 周卡 1 张" in text
    expected = datetime.fromtimestamp(expiry/1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    assert expected in text and "北京时间" in text and "（2 张）" in text
    assert [b["text"] for row in keyboard["inline_keyboard"] for b in row] == ["♻️ 查看重置卡","👤 查看账户","✖️ 关闭"]
    assert all(len(b["callback_data"].encode())<=64 for row in keyboard["inline_keyboard"] for b in row)


@pytest.mark.parametrize("before,after", [(None,cards(week=[1e14])), (cards(week=[1e14]),cards(week=[1e14])), (cards(),None)])
def test_missing_delta_never_labels_existing_inventory_as_granted(before,after):
    text,_=zh.reset_grant_notification("key",credential("oauth"),before,after)
    assert "张数尚未核实" in text and "<b>本次查询确认新增</b>" not in text
    if after is not None:
        assert "周卡 1 张" in text
    else:
        assert "当前可用" not in text


@pytest.mark.parametrize("read_failure",[False,True])
def test_real_grant_path_sends_enriched_notification_once(reset_env,monkeypatch,read_failure):
    _,key,state,_=reset_env
    expiry=(time.time()+86400)*1000
    before=cards(week=[expiry]);after=cards(five=[expiry],week=[expiry])
    wire=common.request
    def request(url,**kw):
        if url.endswith("/status"):
            if read_failure:raise common.ZhipuError("reset_status","network")
            return after
        return wire(url,**kw)
    monkeypatch.setattr(common,"request",request)
    sent=[]
    monkeypatch.setattr(notifier,"notify",lambda text,**kw:sent.append((text,kw)))
    result=actions.auto_claim(key,copy.deepcopy(om.get_account(key)),before)
    assert result["status"]=="succeeded"
    actions.auto_claim(key,copy.deepcopy(om.get_account(key)),before)
    assert len(sent)==1 and "reply_markup" in sent[0][1]
    assert ("5小时额度重置卡：<b>1 张</b>" in sent[0][0]) is (not read_failure)
    assert ("张数尚未核实" in sent[0][0]) is read_failure
    assert sum(kw.get("method")=="POST" for _,kw in state["calls"])==1


def test_notification_buttons_resolve_account_after_shortcode_cache_loss(reset_env,monkeypatch):
    control,key,_,_=reset_env
    monkeypatch.setattr(zh,"control",control)
    monkeypatch.setattr(menu,"oauth_control",control)
    _,keyboard=zh.reset_grant_notification(key,om.get_account(key),cards(),cards(five=[1e14]))
    buttons=[b for row in keyboard["inline_keyboard"] for b in row]
    monkeypatch.setattr(ui,"_code_to_name",{})
    monkeypatch.setattr(ui,"answer_cb",lambda *a,**kw:None)
    calls=[]
    monkeypatch.setattr(zh,"_reset_page",lambda chat,mid,account:calls.append(("cards",account)))
    monkeypatch.setattr(menu,"on_view",lambda chat,mid,cb,short,*a,**kw:calls.append(("account",menu._account_key_from_short(short))))
    for button in buttons[:2]:
        assert menu.handle_callback(42,500,"cb",button["callback_data"])
    assert calls==[("cards",key),("account",key)]


@pytest.mark.parametrize("authorized",[True,False])
def test_close_notification_only_deletes_target_and_preserves_pending_input(monkeypatch,authorized):
    monkeypatch.setattr(ui,"is_admin",lambda chat:authorized)
    monkeypatch.setattr(ui,"answer_cb",lambda *a,**kw:None)
    deleted=[]
    monkeypatch.setattr(ui,"delete_message",lambda chat,mid:deleted.append((chat,mid)))
    states.set_state(42,"mc_input",{"original":"untouched"})
    before=copy.deepcopy(states.get_state(42))
    bot._handle_callback({"id":"cb","message":{"message_id":500,"chat":{"id":42}},"data":"oa:zh:notice_close"})
    assert deleted==([(42,500)] if authorized else [])
    assert states.get_state(42)==before
    states.pop_state(42)
