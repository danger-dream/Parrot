"""Platform selection preserves confirmed identities and uses matching issuer fields."""
import copy

import pytest

from src import oauth_manager as om
from src.management_control import ManagementError
from src.management_control.oauth import OAuthProvider
from src.oauth.qoder import device_platforms, device_profile, device_blocks, device_identity, umid_protocol
from src.telegram import states
from src.telegram.menus import oauth_menu as menu
from src.tests.test_qoder_device_flows import setup, prepare, confirm, context, auth_client
from src.tests.test_qoder_telegram import tg, click, buttons, open_detail


@pytest.mark.parametrize("platform,os_name,process", [
    ("linux-x64", "x86_64_linux", "runtime-info-linux-x64"),
    ("linux-arm64", "aarch64_linux", "runtime-info-linux-arm64"),
])
def test_platform_changes_wire_and_machine_os(platform, os_name, process):
    profile = device_profile.generate(platform=platform)
    fields = device_blocks.decode_blocks(device_profile.validate(profile))[1]
    assert len(fields) == 43 and fields[6] == b"Linux" and fields[8].decode() == process
    assert profile["machine_os"] == os_name
    wire = umid_protocol.request_bytes("issuer.invalid", "fixture", process=process)
    assert f"pn={process}&os=8&sv=1.5.0".encode() in wire
    assert device_profile.summary({"qoder_device_profile": profile})["platform_id"] == platform


def test_default_profile_and_upgrade_do_not_change_existing_identity():
    profile = device_profile.generate("original-machine")
    assert "platform" not in profile
    before = copy.deepcopy(profile)
    digest = device_profile.fingerprint(profile)
    assert device_profile.upgrade(profile) == before
    assert device_profile.fingerprint(profile) == digest
    arm = device_profile.generate(platform="linux-arm64")
    assert device_profile.upgrade(arm) == arm


@pytest.mark.parametrize("field,value", [
    ("platform", "windows-x64"), ("platform", "macos-arm64"), ("platform", []),
    ("machine_os", "aarch64_darwin"), ("machine_os", "x86_64_linux"),
])
def test_platform_metadata_cannot_disagree_with_blocks(field, value):
    profile = device_profile.generate(platform="linux-arm64")
    profile[field] = value
    with pytest.raises(ValueError):
        device_profile.validate(profile)


@pytest.mark.parametrize("platform,machine_os", [("linux-x64", "x86_64_linux"), ("linux-arm64", "aarch64_linux"), ("macos-arm64", "aarch64_darwin"), ("windows-x64", "x86_64_win32")])
def test_selection_regeneration_confirmation_and_relogin_keep_platform(setup, platform, machine_os):
    ctl, calls, _ = setup
    draft = prepare(ctl)
    arm = ctl.regenerate_qoder_device(context(), draft.flow_id, draft.flow_secret,
                                     draft.device_preview["revision"], platform=platform)
    assert calls == [] and arm.device_preview["platform_id"] == platform
    with pytest.raises(ManagementError):
        confirm(ctl, draft)
    again = ctl.regenerate_qoder_device(context(), arm.flow_id, arm.flow_secret, arm.device_preview["revision"])
    assert again.device_preview["platform_id"] == platform
    assert again.device_preview["revision"] != arm.device_preview["revision"]
    confirm(ctl, again)
    with pytest.raises(ManagementError):
        ctl.regenerate_qoder_device(context(), again.flow_id, again.flow_secret,
                                   again.device_preview["revision"], platform="linux-x64")
    saved = ctl.poll_login_flow(context(), again.flow_id, again.flow_secret)
    original = copy.deepcopy(om.get_account(saved.account_id)["qoder_device_profile"])
    assert original["machine_os"] == machine_os
    relogin = ctl.start_login_flow(context(), OAuthProvider.QODER, relogin_account_id=saved.account_id)
    assert relogin.device_preview["platform_id"] == platform
    assert relogin.device_preview["can_regenerate"] is False
    with pytest.raises(ManagementError):
        ctl.regenerate_qoder_device(context(), relogin.flow_id, relogin.flow_secret,
                                   relogin.device_preview["revision"], platform="linux-x64")
    confirm(ctl, relogin)
    ctl.poll_login_flow(context(), relogin.flow_id, relogin.flow_secret)
    assert om.get_account(saved.account_id)["qoder_device_profile"] == original


@pytest.mark.parametrize("platform", ["macos-x64", "windows-arm64", "other", []])
def test_unsupported_platform_is_rejected_without_changing_draft(setup, platform):
    ctl, calls, _ = setup
    draft = prepare(ctl, login_method="pat")
    with pytest.raises(ManagementError) as error:
        ctl.regenerate_qoder_device(context(), draft.flow_id, draft.flow_secret,
                                   draft.device_preview["revision"], platform=platform)
    assert error.value.code.value == "VALIDATION_FAILED"
    assert calls == []
    assert confirm(ctl, draft).device_preview["revision"] == draft.device_preview["revision"]


@pytest.mark.parametrize("platform", ["linux-arm64", "macos-arm64", "windows-x64"])
def test_api_platform_selection_and_locked_confirmation(tmp_path, platform):
    client, headers, _, _, _ = auth_client(tmp_path)
    root = "/api/management/v1/oauth/login-flows"
    try:
        draft = client.post(root, headers=headers, json={"provider": "qoder", "loginMethod": "pat"}).json()["data"]
        assert draft["devicePreview"]["platform_id"] == "macos-arm64"
        path = root + "/" + draft["flowId"] + "/qoder-device/"
        args = {"flowSecret": draft["flowSecret"], "revision": draft["devicePreview"]["revision"]}
        failed = client.post(path + "regenerate", headers=headers, json={**args, "platform": "macos-x64"})
        assert failed.status_code >= 400
        switched = client.post(path + "regenerate", headers=headers, json={**args, "platform": platform})
        assert switched.status_code == 200, switched.text
        preview = switched.json()["data"]["devicePreview"]
        assert preview["platform_id"] == platform
        assert [v["id"] for v in preview["platform_choices"]] == ["macos-arm64", "windows-x64", "linux-x64", "linux-arm64"]
        assert next(v for v in preview["platform_choices"] if v["id"] == "macos-arm64")["supported"]
        assert next(v for v in preview["platform_choices"] if v["id"] == "windows-x64")["supported"]
        args["revision"] = preview["revision"]
        assert client.post(path + "confirm", headers=headers, json={**args, "platform": "linux-x64"}).status_code == 422
        assert client.post(path + "confirm", headers=headers, json=args).status_code == 200
        assert client.post(path + "regenerate", headers=headers, json={**args, "platform": "linux-x64"}).status_code >= 400
    finally:
        client.close()


def test_tg_selection_and_no_outbound_ip(tg):
    key, _, output = tg
    open_detail(key)
    assert "出口 IP" not in output[-1][0]
    menu.handle_callback(42, 900, "cb", "oa:add:qoder")
    click(output, "国际区 · PAT")
    text, kb = output[-1]
    assert "出口 IP" not in text and "代理:" in text
    assert all(len(b["callback_data"].encode()) <= 64 for b in buttons(kb))
    rows = kb["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 3]
    assert "✅ macOS · Apple Silicon" in rows[0][0]["text"]
    assert rows[0][1]["text"] == "Windows · x64"
    assert [b["text"] for b in rows[1]] == ["Linux · x86_64", "Linux · ARM64"]
    assert [b["text"] for b in rows[2]] == ["❌ 取消", "🔄 重新生成", "✅ 确认并继续"]
    assert "平台: macOS · Apple Silicon" in text
    assert not any("待适配" in b["text"] or "Intel" in b["text"] or "Windows · ARM64" in b["text"] for b in buttons(kb))
    previous = states.get_state(42)["data"]["nonce"]
    click(output, "Linux · ARM64")
    assert "平台: Linux · ARM64" in output[-1][0]
    assert states.get_state(42)["data"]["nonce"] != previous
    click(output, "重新生成")
    assert "平台: Linux · ARM64" in output[-1][0]
    click(output, "macOS · Apple Silicon")
    assert "平台: macOS · Apple Silicon" in output[-1][0]
    click(output, "重新生成")
    assert "平台: macOS · Apple Silicon" in output[-1][0]
    click(output, "Windows · x64")
    assert "平台: Windows · x64" in output[-1][0]
    click(output, "重新生成")
    assert "平台: Windows · x64" in output[-1][0]
    state = copy.deepcopy(states.get_state(42))
    menu.handle_callback(42, 900, "cb", "oa:qd:device_platform:" + state["data"]["nonce"] + ":macos-x64")
    assert "尚未适配" in output[-1][0]
    assert states.get_state(42) == state


def test_issuer_uses_selected_process(monkeypatch):
    profile = device_profile.generate(platform="linux-arm64")
    wire = []
    class Stream:
        def settimeout(self, timeout): pass
        def sendall(self, data): wire.append(data)
        def close(self): pass
    monkeypatch.setattr(device_identity.common, "_network_disabled", lambda: False)
    monkeypatch.setattr(device_identity.network, "open_sync_stream", lambda *a, **kw: Stream())
    monkeypatch.setattr(device_identity.umid_http, "read_response", lambda *a: b"fixture")
    monkeypatch.setattr(umid_protocol, "parse_response", lambda raw: "fixture-token")
    assert device_identity._fetch("issuer.invalid", device_profile.validate(profile), "") == "fixture-token"
    assert len(wire) == 1 and b"pn=runtime-info-linux-arm64&os=8" in wire[0]


def test_macos_request_uses_observed_sdk_and_os():
    wire = umid_protocol.request_bytes("issuer.invalid", "fixture", process="runtime-info-darwin-arm64")
    assert b"pn=runtime-info-darwin-arm64&os=7&sv=1.1.2" in wire
    assert b"HTTP/1.1\r\nHost: issuer.invalid\r\n" in wire
    assert b"Content-Length: 7\r\n" in wire
    assert wire.endswith(b"\r\n\r\nfixture")
    for invalid in (None, [], "runtime-info-other"):
        with pytest.raises(ValueError):
            device_platforms.for_process(invalid)


@pytest.mark.parametrize("tls_fails", [False, True])
def test_macos_tls_uses_same_routed_stream_and_verifies_certificate(monkeypatch, tls_fails):
    import ssl
    blocks = device_profile.validate(device_profile.generate(platform="macos-arm64"))
    events = []
    class Stream:
        def settimeout(self, timeout): pass
        def start_tls(self, context, *, server_hostname):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            assert server_hostname == "issuer.invalid"
            events.append("tls")
            if tls_fails:
                raise ssl.SSLError("fixture certificate failure")
            return self
        def sendall(self, data):
            assert b"pn=runtime-info-darwin-arm64&os=7&sv=1.1.2" in data
            events.append("send")
        def close(self): events.append("close")
    def connect(host, port, **kwargs):
        assert host == "issuer.invalid" and port == 443
        assert kwargs["proxy_provider"] == "qoder" and kwargs["proxy_channel"] == "oauth:account-fixture"
        events.append("connect")
        return Stream()
    monkeypatch.setattr(device_identity.common, "_network_disabled", lambda: False)
    monkeypatch.setattr(device_identity.network, "open_sync_stream", connect)
    monkeypatch.setattr(device_identity.umid_http, "read_response", lambda *a: b"fixture")
    monkeypatch.setattr(umid_protocol, "parse_response", lambda raw: "fixture-token")
    if tls_fails:
        with pytest.raises(device_identity.common.QoderError):
            device_identity._fetch("issuer.invalid", blocks, "account-fixture")
        assert events == ["connect", "tls", "close"]
    else:
        assert device_identity._fetch("issuer.invalid", blocks, "account-fixture") == "fixture-token"
        assert events == ["connect", "tls", "send", "close"]
