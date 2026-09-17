from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src.management_auth import AuthMethod, Capability
from src.management_control import ManagementContext, ManagementError
from src.tests.management_auxiliary_support import bearer, build_auxiliary_app, create_session


BASE = "/api/management/v1"
def _poll(client, headers, operation_id):
    response = client.get(BASE + f"/operations/{operation_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_update_all_operations_prepare_commit_polling_and_replay(tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        initial = client.get(BASE + "/updates/settings", headers=headers)
        assert initial.status_code == 200
        revision = initial.json()["data"]["revision"]

        updated = client.patch(
            BASE + "/updates/settings",
            json={"enabled": False, "intervalSeconds": 7200},
            headers={**headers, "If-Match": revision},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["intervalSeconds"] == 7200

        checked = client.post(BASE + "/updates/actions/check", headers=headers)
        assert checked.status_code == 200
        assert checked.json()["data"]["candidateVersion"] == "0.32.0"
        assert checked.json()["data"]["changelog"] == "changes"

        ignored = client.put(BASE + "/updates/ignored-versions/0.32.0", headers=headers)
        assert ignored.status_code == 200
        assert ignored.json()["data"]["ignoredVersions"] == ["0.32.0"]
        unignored = client.delete(BASE + "/updates/ignored-versions/0.32.0", headers=headers)
        assert unignored.status_code == 204
        missing = client.delete(BASE + "/updates/ignored-versions/9.9.9", headers=headers)
        assert missing.status_code == 404

        backups = client.get(
            BASE + "/updates/backups?mode=docker&sort=createdAtDesc&page=1&pageSize=1",
            headers=headers,
        )
        assert backups.status_code == 200
        assert backups.json()["meta"]["total"] == 1
        assert backups.json()["data"]["items"][0]["ref"] == "b2"
        assert backups.json()["data"]["items"][0]["createdAt"] == "2026-02-02T03:04:05Z"
        assert backups.json()["data"]["items"][0]["revision"].startswith("rev_")

        failure_log = client.get(BASE + "/updates/failure-log", headers=headers)
        assert failure_log.status_code == 200
        assert failure_log.json()["data"]["content"] == (
            "api_token=***\nhealth failed"
        )

        stage_headers = {**headers, "Idempotency-Key": "stage-0.32.0"}
        staged = client.post(BASE + "/updates/0.32.0/actions/stage", headers=stage_headers)
        assert staged.status_code == 202, staged.text
        stage_data = staged.json()["data"]
        stage_operation_id = stage_data["id"]
        plan_token = stage_data["activationPlanToken"]
        assert isinstance(plan_token, str) and len(plan_token) >= 16
        stage_terminal = _poll(client, headers, stage_operation_id)
        assert stage_terminal["status"] == "succeeded"
        plan = stage_terminal["result"]
        assert plan == {
            "stagedVersion": "0.32.0",
            "expectedRevision": plan["expectedRevision"],
            "expiresAt": "2026-01-02T00:10:00Z",
        }
        assert "activationPlanToken" not in str(stage_terminal)
        assert plan_token not in str(stage_terminal)
        assert plan_token not in repr(fixture.controls.updates._plans)
        assert fixture.update_gateway.stage_calls == ["0.32.0"]

        duplicate = client.post(BASE + "/updates/0.32.0/actions/stage", headers=stage_headers)
        assert duplicate.status_code == 202
        assert duplicate.json()["data"]["id"] == stage_operation_id
        assert duplicate.json()["data"]["activationPlanToken"] is None
        assert plan_token not in duplicate.text
        assert fixture.update_gateway.stage_calls == ["0.32.0"]

        conflicting = client.post(BASE + "/updates/0.33.0/actions/stage", headers=stage_headers)
        assert conflicting.status_code == 409
        assert conflicting.json()["error"]["code"] == "STATE_CONFLICT"

        activate_headers = {
            **headers,
            "Idempotency-Key": "activate-0.32.0",
            "If-Match": plan["expectedRevision"],
        }
        activated = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": plan_token},
            headers=activate_headers,
        )
        assert activated.status_code == 202, activated.text
        activation_terminal = _poll(client, headers, activated.json()["data"]["id"])
        assert activation_terminal["status"] == "succeeded"
        assert activation_terminal["result"] == {"activated": True}

        replay = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": plan_token},
            headers=activate_headers,
        )
        assert replay.status_code == 409
        assert replay.json()["error"]["code"] == "STATE_CONFLICT"

        staged_again = client.post(
            BASE + "/updates/0.33.0/actions/stage",
            headers={**headers, "Idempotency-Key": "stage-0.33.0"},
        )
        assert staged_again.status_code == 202
        cancelled = client.delete(BASE + "/updates/staged", headers=headers)
        assert cancelled.status_code == 204
        assert fixture.update_gateway.cancel_calls == 1

    assert {record.actor for record in fixture.audit.snapshot()} == {"administrator"}


def test_update_stage_requires_idempotency_and_activation_plan_expires(tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        missing_key = client.post(BASE + "/updates/0.32.0/actions/stage", headers=headers)
        assert missing_key.status_code == 422
        assert missing_key.json()["error"]["fields"][0]["path"] == "Idempotency-Key"

        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**headers, "Idempotency-Key": "stage-expiring"},
        )
        plan_token = staged.json()["data"]["activationPlanToken"]
        plan = _poll(client, headers, staged.json()["data"]["id"])["result"]
        fixture.controls.updates._clock = lambda: datetime(2026, 1, 2, 1, tzinfo=timezone.utc)
        expired = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": plan_token},
            headers={
                **headers,
                "Idempotency-Key": "activate-expired",
                "If-Match": plan["expectedRevision"],
            },
        )
        assert expired.status_code == 409
        assert expired.json()["error"]["code"] == "STATE_CONFLICT"


def test_reaudit3_update_token_cancelled_plan_never_revives_on_same_version_restage(tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))

        def stage(idempotency_key: str) -> tuple[str, str]:
            response = client.post(
                BASE + "/updates/0.32.0/actions/stage",
                headers={**headers, "Idempotency-Key": idempotency_key},
            )
            assert response.status_code == 202, response.text
            data = response.json()["data"]
            terminal = _poll(client, headers, data["id"])
            assert terminal["status"] == "succeeded"
            return data["activationPlanToken"], terminal["result"]["expectedRevision"]

        old_token, old_revision = stage("stage-same-version-a")
        cancelled = client.delete(
            BASE + "/updates/staged",
            headers={**headers, "If-Match": old_revision},
        )
        assert cancelled.status_code == 204, cancelled.text
        assert fixture.update_gateway.cancel_calls == 1

        new_token, new_revision = stage("stage-same-version-b")
        assert new_token != old_token
        # The public state revision is deliberately identical: plan identity, not
        # a coincidental version/state difference, must make the old token stale.
        assert new_revision == old_revision

        stale = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": old_token},
            headers={
                **headers,
                "Idempotency-Key": "activate-cancelled-token",
                "If-Match": new_revision,
            },
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "STATE_CONFLICT"
        assert fixture.update_gateway.activate_calls == 0

        activated = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": new_token},
            headers={
                **headers,
                "Idempotency-Key": "activate-current-token",
                "If-Match": new_revision,
            },
        )
        assert activated.status_code == 202, activated.text
        terminal = _poll(client, headers, activated.json()["data"]["id"])
        assert terminal["status"] == "succeeded"
        assert terminal["result"] == {"activated": True}
        assert fixture.update_gateway.activate_calls == 1


def test_cancel_success_skips_unavailable_post_state_and_retires_plan(tmp_path):
    app, runtime, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**headers, "Idempotency-Key": "stage-before-successful-direct-cancel"},
        )
        assert staged.status_code == 202, staged.text
        terminal = _poll(client, headers, staged.json()["data"]["id"])
        assert terminal["status"] == "succeeded"
        active_digest = fixture.controls.updates._active_plan_digest
        assert active_digest is not None

        state_reads = 0
        original_state = fixture.update_gateway.state

        def unavailable_state():
            nonlocal state_reads
            state_reads += 1
            raise OSError("post-cancel state is unavailable")

        def successful_cancel_then_break_state_read():
            fixture.update_gateway.cancel_calls += 1
            fixture.update_gateway.update_state = {"stage": "idle", "mode": "docker"}
            fixture.update_gateway.state = unavailable_state
            return True, "cancelled"

        fixture.update_gateway.cancel = successful_cancel_then_break_state_read
        direct_session = runtime.sessions.issue_for_principal(
            subject_id="telegram:update-admin",
            auth_method=AuthMethod.TELEGRAM_APPROVAL,
            roles=(),
            capabilities=(Capability.READ, Capability.UPDATE),
        )
        direct_context = ManagementContext(
            request_id="successful-direct-cancel-without-read-back",
            actor=direct_session.principal,
        )

        cancelled = fixture.controls.updates.cancel_direct(direct_context)
        assert cancelled == (True, "cancelled")
        assert state_reads == 0
        assert fixture.update_gateway.cancel_calls == 1
        assert fixture.controls.updates._active_plan_digest is None
        assert fixture.controls.updates._plans[active_digest].consumed is True
        fixture.update_gateway.state = original_state


def test_failed_direct_cancel_preserves_contract_when_post_state_read_fails(tmp_path):
    app, runtime, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**headers, "Idempotency-Key": "stage-before-unreadable-failed-cancel"},
        )
        assert staged.status_code == 202, staged.text
        terminal = _poll(client, headers, staged.json()["data"]["id"])
        assert terminal["status"] == "succeeded"
        active_digest = fixture.controls.updates._active_plan_digest
        assert active_digest is not None

        state_reads = 0
        original_state = fixture.update_gateway.state
        cancel_detail = "original cancel failure"

        def unavailable_state():
            nonlocal state_reads
            state_reads += 1
            raise OSError("post-cancel state is unavailable")

        def failed_cancel_then_break_state_read():
            fixture.update_gateway.cancel_calls += 1
            fixture.update_gateway.state = unavailable_state
            return False, cancel_detail

        fixture.update_gateway.cancel = failed_cancel_then_break_state_read
        direct_session = runtime.sessions.issue_for_principal(
            subject_id="telegram:update-admin",
            auth_method=AuthMethod.TELEGRAM_APPROVAL,
            roles=(),
            capabilities=(Capability.READ, Capability.UPDATE),
        )
        direct_context = ManagementContext(
            request_id="failed-direct-cancel-with-unreadable-state",
            actor=direct_session.principal,
        )

        cancelled = fixture.controls.updates.cancel_direct(direct_context)
        assert cancelled == (False, cancel_detail)
        assert state_reads == 1
        assert fixture.update_gateway.cancel_calls == 1
        assert fixture.controls.updates._active_plan_digest == active_digest
        assert fixture.controls.updates._plans[active_digest].consumed is False
        fixture.update_gateway.state = original_state


def test_reaudit4_failed_cancel_that_clears_stage_retires_plan_before_direct_restage(tmp_path):
    app, runtime, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**headers, "Idempotency-Key": "stage-before-failed-direct-cancel"},
        )
        assert staged.status_code == 202, staged.text
        old_token = staged.json()["data"]["activationPlanToken"]
        terminal = _poll(client, headers, staged.json()["data"]["id"])
        assert terminal["status"] == "succeeded"
        old_revision = terminal["result"]["expectedRevision"]

        cancel_detail = "source rollback failed after state reset"

        def fail_cancel_after_reset():
            fixture.update_gateway.cancel_calls += 1
            fixture.update_gateway.update_state = {"stage": "idle", "mode": "docker"}
            return False, cancel_detail

        fixture.update_gateway.cancel = fail_cancel_after_reset
        direct_session = runtime.sessions.issue_for_principal(
            subject_id="telegram:update-admin",
            auth_method=AuthMethod.TELEGRAM_APPROVAL,
            roles=(),
            capabilities=(Capability.READ, Capability.UPDATE),
        )
        direct_context = ManagementContext(
            request_id="failed-direct-cancel",
            actor=direct_session.principal,
        )

        cancelled = fixture.controls.updates.cancel_direct(direct_context)
        assert cancelled == (False, cancel_detail)
        assert fixture.update_gateway.cancel_calls == 1

        restaged = fixture.controls.updates.stage_direct(
            direct_context,
            "0.32.0",
            progress=lambda _stage, _message: None,
            chat_id=123,
            notify_msg_id=456,
        )
        assert restaged == (True, "ready")
        new_revision = fixture.controls.updates.state(direct_context).revision
        assert new_revision == old_revision

        stale = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": old_token},
            headers={
                **headers,
                "Idempotency-Key": "activate-after-failed-direct-cancel",
                "If-Match": new_revision,
            },
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "STATE_CONFLICT"
        assert fixture.update_gateway.activate_calls == 0


@pytest.mark.parametrize("cancel_surface", ["direct", "api"])
def test_failed_cancel_serializes_direct_restage_before_post_state_read(tmp_path, cancel_surface):
    app, runtime, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**headers, "Idempotency-Key": f"stage-before-racing-{cancel_surface}-cancel"},
        )
        assert staged.status_code == 202, staged.text
        old_token = staged.json()["data"]["activationPlanToken"]
        terminal = _poll(client, headers, staged.json()["data"]["id"])
        assert terminal["status"] == "succeeded"
        old_revision = terminal["result"]["expectedRevision"]
        control = fixture.controls.updates
        old_digest = control._active_plan_digest
        assert old_digest is not None

        direct_session = runtime.sessions.issue_for_principal(
            subject_id="telegram:update-admin",
            auth_method=AuthMethod.TELEGRAM_APPROVAL,
            roles=(),
            capabilities=(Capability.READ, Capability.UPDATE),
        )
        direct_context = ManagementContext(
            request_id=f"racing-{cancel_surface}-cancel",
            actor=direct_session.principal,
        )
        cancel_detail = "source rollback failed after state reset"

        def failed_cancel_after_reset():
            fixture.update_gateway.cancel_calls += 1
            fixture.update_gateway.update_state = {"stage": "idle", "mode": "docker"}
            return False, cancel_detail

        fixture.update_gateway.cancel = failed_cancel_after_reset
        original_retire_after_cancel = control._retire_plan_after_cancel
        helper_waiting = threading.Event()
        allow_post_state_read = threading.Event()
        stage_attempted = threading.Event()
        stage_done = threading.Event()
        results = {}

        def pause_after_cancel_return(digest, *, ok):
            # Both cancel wrappers call this only after their real gateway cancel
            # invocation has returned.  Keep the helper's real state read gated.
            assert ok is False
            helper_waiting.set()
            assert allow_post_state_read.wait(timeout=5), "post-state read was never released"
            original_retire_after_cancel(digest, ok=ok)

        control._retire_plan_after_cancel = pause_after_cancel_return

        def run_cancel():
            try:
                if cancel_surface == "direct":
                    results["cancel"] = control.cancel_direct(direct_context)
                else:
                    results["cancel"] = client.delete(
                        BASE + "/updates/staged",
                        headers={**headers, "If-Match": old_revision},
                    )
            except BaseException as exc:  # surfaced in the main test thread below
                results["cancel_error"] = exc

        def run_direct_restage():
            stage_attempted.set()
            try:
                results["restage"] = control.stage_direct(
                    direct_context,
                    "0.32.0",
                    progress=lambda _stage, _message: None,
                    chat_id=123,
                    notify_msg_id=456,
                )
            except BaseException as exc:  # surfaced in the main test thread below
                results["restage_error"] = exc
            finally:
                stage_done.set()

        cancel_thread = threading.Thread(target=run_cancel, name=f"{cancel_surface}-cancel")
        stage_thread = threading.Thread(target=run_direct_restage, name="direct-restage")
        stage_thread_started = False
        try:
            cancel_thread.start()
            assert helper_waiting.wait(timeout=5), "cancel did not reach its post-state helper"
            assert fixture.update_gateway.cancel_calls == 1
            assert fixture.update_gateway.update_state["stage"] == "idle"

            stage_thread.start()
            stage_thread_started = True
            assert stage_attempted.wait(timeout=5), "direct restage thread did not start"
            stage_completed_before_post_read = stage_done.wait(timeout=1)
        finally:
            allow_post_state_read.set()
            cancel_thread.join(timeout=5)
            if stage_thread_started:
                stage_thread.join(timeout=5)
            control._retire_plan_after_cancel = original_retire_after_cancel

        assert not cancel_thread.is_alive()
        assert not stage_thread.is_alive()
        assert "cancel_error" not in results
        assert "restage_error" not in results
        if cancel_surface == "direct":
            assert results["cancel"] == (False, cancel_detail)
        else:
            cancelled = results["cancel"]
            assert cancelled.status_code == 503, cancelled.text
            assert cancelled.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
            assert cancelled.json()["error"]["message"] == "A required dependency is unavailable"
            assert cancelled.json()["error"]["retryable"] is True
            assert cancel_detail not in cancelled.text
        assert results["restage"] == (True, "ready")
        assert fixture.update_gateway.stage_calls == ["0.32.0", "0.32.0"]

        new_revision = control.state(direct_context).revision
        assert new_revision == old_revision
        stale = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": old_token},
            headers={
                **headers,
                "Idempotency-Key": f"activate-after-racing-{cancel_surface}-cancel",
                "If-Match": new_revision,
            },
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "STATE_CONFLICT"
        assert fixture.update_gateway.activate_calls == 0
        assert control._plans[old_digest].consumed is True
        assert control._active_plan_digest is None
        assert stage_completed_before_post_read is False


def test_reaudit4_failed_cancel_while_still_staged_keeps_plan_retryable(tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**headers, "Idempotency-Key": "stage-before-retryable-cancel"},
        )
        assert staged.status_code == 202, staged.text
        plan_token = staged.json()["data"]["activationPlanToken"]
        terminal = _poll(client, headers, staged.json()["data"]["id"])
        assert terminal["status"] == "succeeded"
        revision = terminal["result"]["expectedRevision"]

        private_detail = "PRIVATE_STILL_STAGED_CANCEL_FAILURE"

        def fail_cancel_while_staged():
            fixture.update_gateway.cancel_calls += 1
            return False, private_detail

        fixture.update_gateway.cancel = fail_cancel_while_staged
        cancelled = client.delete(
            BASE + "/updates/staged",
            headers={**headers, "If-Match": revision},
        )
        assert cancelled.status_code == 503, cancelled.text
        assert cancelled.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert cancelled.json()["error"]["message"] == "A required dependency is unavailable"
        assert cancelled.json()["error"]["retryable"] is True
        assert private_detail not in cancelled.text
        assert fixture.update_gateway.cancel_calls == 1
        assert fixture.update_gateway.update_state["stage"] == "staged"

        activated = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": plan_token},
            headers={
                **headers,
                "Idempotency-Key": "activate-after-retryable-cancel",
                "If-Match": revision,
            },
        )
        assert activated.status_code == 202, activated.text
        terminal = _poll(client, headers, activated.json()["data"]["id"])
        assert terminal["status"] == "succeeded"
        assert terminal["result"] == {"activated": True}
        assert fixture.update_gateway.activate_calls == 1


def test_image_and_xai_all_operations_no_oauth_secret_and_revision_conflict(tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        image = client.get(BASE + "/images/settings", headers=headers)
        assert image.status_code == 200
        image_data = image.json()["data"]
        changed = client.patch(
            BASE + "/images/settings",
            json={
                "cacheEnabled": True,
                "cachePath": "media/images",
                "cacheRetentionDays": 30,
                "cacheMaxBytes": 2048,
            },
            headers={**headers, "If-Match": image_data["revision"]},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["data"]["cachePath"] == "media/images"
        assert fixture.config.value["images"]["cacheMaxBytes"] == 2048

        stale = client.patch(
            BASE + "/images/settings",
            json={"enabled": False},
            headers={**headers, "If-Match": image_data["revision"]},
        )
        assert stale.status_code == 409

        account_path = BASE + "/images/accounts/openai%3Auser%40example.com"
        account = client.get(account_path, headers=headers)
        assert account.status_code == 200, account.text
        account_data = account.json()["data"]
        assert "token" not in account.text.lower()
        assert account_data["imageCooldownUntil"] is None
        disabled = client.patch(
            account_path,
            json={"enabled": False},
            headers={**headers, "If-Match": account_data["revision"]},
        )
        assert disabled.status_code == 200
        assert disabled.json()["data"]["imageEnabled"] is False
        assert "openai:user@example.com" in fixture.config.value["images"]["disabledAccounts"]
        assert client.get(BASE + "/images/accounts/missing", headers=headers).status_code == 404

        xai = client.get(BASE + "/xai/media-settings", headers=headers)
        assert xai.status_code == 200
        assert xai.json()["data"]["jobTtlSeconds"] == 10800
        xai_changed = client.patch(
            BASE + "/xai/media-settings",
            json={
                "imageModels": ["grok-image-new"],
                "videoModels": [],
                "jobTtlSeconds": 7200,
                "requestTimeoutSeconds": 240,
            },
            headers={**headers, "If-Match": xai.json()["data"]["revision"]},
        )
        assert xai_changed.status_code == 200
        assert xai_changed.json()["data"]["imageModels"] == ["grok-image-new"]
        assert fixture.config.value['image_models']['xai'] == ['grok-image-new']
        assert fixture.config.value['video_models']['xai'] == []
        assert fixture.config.value['videos']['jobTtlSeconds'] == 7200
        assert fixture.config.value['videos']['requestTimeoutSeconds'] == 240


def test_image_cache_path_escape_and_xai_model_limits_are_rejected(tmp_path):
    app, _, _ = build_auxiliary_app(tmp_path)
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        escaped = client.patch(
            BASE + "/images/settings",
            json={"cachePath": "../outside"},
            headers=headers,
        )
        assert escaped.status_code == 422
        assert escaped.json()["error"]["fields"][0]["code"] == "PATH_ESCAPE"

        too_long = client.patch(
            BASE + "/xai/media-settings",
            json={"imageModels": ["x" * 129]},
            headers=headers,
        )
        assert too_long.status_code == 422
        assert too_long.json()["error"]["fields"][0]["path"] == "imageModels[0]"


def test_update_check_maps_upstream_failure_without_leaking_detail(tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    marker = "UPSTREAM_PRIVATE_MARKER"

    def fail_refresh():
        raise RuntimeError(f"Bearer {marker}")

    fixture.update_gateway.check_latest = fail_refresh
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        response = client.post(BASE + "/updates/actions/check", headers=headers)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
        assert response.json()["error"]["retryable"] is True
        assert marker not in response.text
    audits = fixture.audit.snapshot()
    assert any(record.action == "updates.check" and record.result == "failed" for record in audits)
    assert marker not in repr(audits)


def test_update_failure_log_requires_body_capability_and_audits(tmp_path):
    app, runtime, fixture = build_auxiliary_app(tmp_path)
    raw_log = "x" * 4000 + "\nhealth verification failed at backup step"
    fixture.update_gateway.failure_log = lambda: raw_log

    restricted = runtime.sessions.issue_for_principal(
        subject_id="read-only",
        auth_method=AuthMethod.MANAGEMENT_KEY,
        roles=(),
        capabilities=(Capability.READ,),
    )
    restricted_context = ManagementContext(
        request_id="direct-read-only", actor=restricted.principal,
    )
    with pytest.raises(ManagementError) as denied:
        fixture.controls.updates.failure_log(restricted_context)
    assert denied.value.code.value == "CAPABILITY_DENIED"
    assert fixture.controls.updates.failure_log_raw(restricted_context) == raw_log

    with TestClient(app) as client:
        denied_response = client.get(
            BASE + "/updates/failure-log", headers=bearer(restricted.credential),
        )
        assert denied_response.status_code == 403
        headers = bearer(create_session(client))
        response = client.get(BASE + "/updates/failure-log", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["content"] == raw_log[-3500:]

    assert fixture.update_gateway.failure_log() == raw_log
    assert any(
        record.action == "updates.failure-log.read"
        for record in fixture.audit.snapshot()
    )

def test_stage_plan_is_actor_bound_and_failed_stage_cannot_activate(tmp_path):
    cross_path = tmp_path / "cross-actor"
    cross_path.mkdir()
    app, _, _ = build_auxiliary_app(cross_path)
    with TestClient(app) as client:
        owner_headers = bearer(create_session(client))
        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**owner_headers, "Idempotency-Key": "stage-cross-actor"},
        )
        token = staged.json()["data"]["activationPlanToken"]
        result = _poll(client, owner_headers, staged.json()["data"]["id"])["result"]
        other_headers = bearer(create_session(client))
        denied = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": token},
            headers={
                **other_headers,
                "Idempotency-Key": "activate-cross-actor",
                "If-Match": result["expectedRevision"],
            },
        )
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "STATE_CONFLICT"
        assert token not in denied.text

    failed_path = tmp_path / "failed-stage"
    failed_path.mkdir()
    app, _, fixture = build_auxiliary_app(failed_path)
    fixture.update_gateway.stage_ok = False
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        staged = client.post(
            BASE + "/updates/0.32.0/actions/stage",
            headers={**headers, "Idempotency-Key": "stage-fails"},
        )
        token = staged.json()["data"]["activationPlanToken"]
        terminal = _poll(client, headers, staged.json()["data"]["id"])
        assert terminal["status"] == "failed"
        assert terminal["result"] is None
        assert token not in str(terminal)
        assert token not in repr(fixture.controls.updates._plans)
        denied = client.post(
            BASE + "/updates/staged/actions/restart",
            json={"planToken": token},
            headers={
                **headers,
                "Idempotency-Key": "activate-failed-stage",
                "If-Match": "rev_not_ready",
            },
        )
        assert denied.status_code == 409
        assert fixture.update_gateway.activate_calls == 0
        assert token not in denied.text


def test_public_update_and_image_times_normalize_or_become_null(tmp_path, monkeypatch):
    from src.openai import images_simple
    monkeypatch.setattr(images_simple, "_IMAGE_COOLDOWNS", {"openai:user@example.com": 1767323045})
    app, _, fixture = build_auxiliary_app(tmp_path)
    fixture.update_gateway.release["latest_published_at"] = 1767323045
    fixture.media_gateway.image_cooldown_until = 1767323045
    with TestClient(app) as client:
        headers = bearer(create_session(client))
        checked = client.post(BASE + "/updates/actions/check", headers=headers)
        assert checked.json()["data"]["publishedAt"] == "2026-01-02T03:04:05Z"
        account = client.get(
            BASE + "/images/accounts/openai%3Auser%40example.com",
            headers=headers,
        )
        assert account.json()["data"]["imageCooldownUntil"] == "2026-01-02T03:04:05Z"

        fixture.update_gateway.release["latest_published_at"] = "not-a-time"
        fixture.media_gateway.image_cooldown_until = "not-a-time"
        images_simple._IMAGE_COOLDOWNS["openai:user@example.com"] = "not-a-time"
        assert client.post(BASE + "/updates/actions/check", headers=headers).json()["data"]["publishedAt"] is None
        assert client.get(
            BASE + "/images/accounts/openai%3Auser%40example.com",
            headers=headers,
        ).json()["data"]["imageCooldownUntil"] is None

        fixture.update_gateway.backups = lambda: [
            {
                "ref": "valid-time",
                "version": "0.31.13",
                "target_tag": "0.32.0",
                "mode": "docker",
                "ts": "20260102-030405",
            },
            {
                "ref": "invalid-time",
                "version": "0.31.13",
                "target_tag": "0.32.0",
                "mode": "docker",
                "ts": "not-a-time",
            },
        ]
        backups = client.get(BASE + "/updates/backups", headers=headers).json()["data"]["items"]
        backup = next(item for item in backups if item["ref"] == "invalid-time")
        assert backup["createdAt"] is None


def test_image_api_reconciles_all_legacy_account_identities(tmp_path):
    app, _, fixture = build_auxiliary_app(tmp_path)
    fixture.media_gateway.account_key = "openai:user@example.com:acct-1"
    fixture.config.value["oauthAccounts"][0]["workspace_id"] = "acct-1"
    aliases = [
        fixture.media_gateway.account_key,
        f"oauth:{fixture.media_gateway.account_key}",
        fixture.media_gateway.account_email,
        f"openai:{fixture.media_gateway.account_email}",
    ]
    fixture.config.value["images"]["disabledAccounts"] = [*aliases, "unrelated-account"]
    account_path = BASE + "/images/accounts/" + quote(fixture.media_gateway.account_key, safe="")

    with TestClient(app) as client:
        headers = bearer(create_session(client))
        account = client.get(account_path, headers=headers).json()["data"]
        assert account["imageEnabled"] is False

        unchanged = client.patch(
            account_path,
            json={"enabled": False},
            headers={**headers, "If-Match": account["revision"]},
        )
        assert unchanged.status_code == 200
        assert fixture.config.value["images"]["disabledAccounts"] == [*aliases, "unrelated-account"]
        authoritative = client.get(account_path, headers=headers).json()["data"]
        assert unchanged.json()["data"]["revision"] == authoritative["revision"]

        enabled = client.patch(
            account_path,
            json={"enabled": True},
            headers={**headers, "If-Match": authoritative["revision"]},
        )
        assert enabled.status_code == 200
        assert fixture.config.value["images"]["disabledAccounts"] == ["unrelated-account"]
        assert enabled.json()["data"]["imageEnabled"] is True
        final = client.get(account_path, headers=headers).json()["data"]
        assert enabled.json()["data"]["revision"] == final["revision"]
