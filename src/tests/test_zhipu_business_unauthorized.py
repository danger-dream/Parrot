"""#43: management endpoints report an expired login as HTTP 200 + body code 401."""
import httpx
import pytest
from fastapi.testclient import TestClient

from src import oauth_manager as om, oauth_errors
from src.management_api.routers.oauth import router
from src.management_api.routers.oauth_support import get_oauth_control_dependency
from src.management_control.oauth.account_orchestration import OAuthAccountOrchestrationControlMixin
from src.oauth.zhipu import auth, common
from src.tests.test_management_api_foundation import build_app, bearer, create_session
from src.tests.test_zhipu_management import ctl
from src.tests.test_zhipu_provider import account_env, credential
from src.tests.test_zhipu_read_retries import wire

EXPIRED = {"code": 401, "msg": "登录状态已过期", "success": False}


@pytest.mark.parametrize("site", ["bigmodel", "zai"])
def test_biz_body_401_is_an_expired_login(wire, site):
    replies, calls, sleeps = wire
    replies.append(httpx.Response(200, json=EXPIRED))
    with pytest.raises(common.ZhipuError) as failed:
        auth.entitlement(credential("oauth", site))
    assert failed.value.auth_error and failed.value.unauthorized
    assert failed.value.status_code == 200 and failed.value.code == 401
    assert len(calls) == 1 and not sleeps


@pytest.mark.parametrize("url,body", [
    # A permission refusal is not an expired login.
    ("https://bigmodel.cn/api/biz/subscription/list", {"code": 403, "success": False}),
    # Model-key endpoints keep their own meaning for the same body code.
    ("https://open.bigmodel.cn/api/monitor/usage/quota/limit", EXPIRED),
])
def test_other_business_errors_are_not_login_failures(wire, url, body):
    replies, calls, _ = wire
    replies.append(httpx.Response(200, json=body))
    with pytest.raises(common.ZhipuError) as failed:
        common.request(url, headers={"authorization": "fixture"})
    assert not failed.value.auth_error and not failed.value.unauthorized


async def test_expired_bigmodel_login_asks_for_relogin(account_env, wire):
    replies, calls, _ = wire
    replies.extend(httpx.Response(200, json=EXPIRED) for _ in range(5))
    a = credential("oauth", organization_id="org", project_id="project")
    om.add_account(a)
    key = om.get_account_key(a)
    assert not OAuthAccountOrchestrationControlMixin.account_authentication_failed(om.get_account(key))
    with pytest.raises(common.ZhipuError):
        await om.force_refresh(key)
    current = om.get_account(key)
    assert current["management_status"] == "relogin_required"
    assert OAuthAccountOrchestrationControlMixin.account_authentication_failed(current)
    assert all("/api/biz/" in str(request.url) for request in calls)


def test_webui_detail_offers_relogin_after_failed_refresh(ctl, wire, tmp_path):
    control, _ = ctl
    replies, _, _ = wire
    replies.extend(httpx.Response(200, json=EXPIRED) for _ in range(5))
    a = credential("oauth", organization_id="org", project_id="project")
    om.add_account(a)
    key = om.get_account_key(a)
    app, _, _ = build_app(tmp_path)
    app.include_router(router, prefix="/api/management/v1")
    app.dependency_overrides[get_oauth_control_dependency] = lambda: control
    base = "/api/management/v1/oauth"
    relogin = {"provider": "zhipu", "site": "bigmodel", "reloginAccountId": key}
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        assert client.post(base + "/login-flows", headers=headers, json=relogin).status_code != 201
        # 「更新凭据」 fails, but the account it leaves behind is what the detail page renders.
        assert client.post(f"{base}/accounts/{key}/actions/refresh-token", headers=headers).status_code >= 400
        detail = client.get(f"{base}/accounts/{key}", headers=headers).json()["data"]
        assert detail["zhipu"]["managementStatus"] == "relogin_required"
        response = client.post(base + "/login-flows", headers=headers, json=relogin)
        assert response.status_code == 201, response.text


def test_usage_refresh_also_asks_for_relogin(account_env, wire):
    replies, _, _ = wire
    replies.extend(httpx.Response(200, json=EXPIRED) for _ in range(5))
    a = credential("oauth", organization_id="org", project_id="project")
    om.add_account(a)
    key = om.get_account_key(a)
    from src.oauth.zhipu import runtime
    with pytest.raises(common.ZhipuError) as failed:
        runtime.fetch_usage_sync(om.get_account(key), key)
    assert om.get_account(key)["management_status"] == "relogin_required"
    shown = oauth_errors.describe_oauth_error(failed.value, provider="zhipu", operation="refresh_token")
    assert shown.title == "智谱管理登录已过期" and shown.auth_error


def test_zai_biz_body_401_exchanges_saved_source_once(account_env, wire):
    replies, calls, _ = wire
    a = credential("oauth", "zai", organization_id="org", project_id="project",
                   zhipu_oauth_token="original-oauth")
    om.add_account(a)
    key = om.get_account_key(a)
    replies.extend([
        httpx.Response(200, json=EXPIRED),
        httpx.Response(200, json={"code": 200, "data": {"access_token": "renewed-business"}}),
        httpx.Response(200, json={"code": 200, "data": {"organizations": []}}),
    ])
    assert auth.project_choices(om.get_account(key), account_key=key) == []
    assert [request.url.path for request in calls] == [
        "/api/biz/customer/getCustomerInfo", "/api/auth/z/login", "/api/biz/customer/getCustomerInfo"]
    assert calls[2].headers["authorization"] == "renewed-business"
    assert om.get_account(key)["access_token"] == "renewed-business"
